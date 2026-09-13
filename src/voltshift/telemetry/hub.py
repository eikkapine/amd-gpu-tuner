"""TelemetryHub — the single poller everything else subscribes to.

Before 2.0 the GUI polled the bridge for its top bar, the engine runner
polled it again for the voltage loop, and any new consumer would have added a
third. The hub polls once, fuses the reading with frame statistics, and fans
the result out. Subscribers are called on the hub thread and must not block.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Callable, Optional

from ..bridgeclient import BridgeClient, BridgeError
from .frames import FrameSource, NullFrameSource, detect_frame_source
from .sample import Sample

DEFAULT_INTERVAL_SEC = 0.5
HISTORY_SIZE = 1200  # 10 minutes at 0.5 s


class TelemetryHub:
    def __init__(self, bridge: BridgeClient,
                 frame_source: Optional[FrameSource] = None,
                 interval_sec: float = DEFAULT_INTERVAL_SEC,
                 frame_window_sec: float = 2.0):
        self._bridge = bridge
        self._frames = frame_source if frame_source is not None else NullFrameSource()
        self._interval = max(0.1, interval_sec)
        self._frame_window = frame_window_sec
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._history: deque[Sample] = deque(maxlen=HISTORY_SIZE)
        self._subscribers: list[Callable[[Sample], None]] = []
        self._latest: Optional[Sample] = None
        self._applied_offset_mv: Optional[int] = None
        self.on_error: Optional[Callable[[str], None]] = None
        self.consecutive_errors = 0

    # ── configuration ────────────────────────────────────────────────────────

    @property
    def frame_source(self) -> FrameSource:
        return self._frames

    def use_frame_source(self, source: FrameSource) -> None:
        """Swap the frame backend, restarting it if the hub is already live."""
        old = self._frames
        self._frames = source
        if self.running:
            old.stop()
            source.start()

    def autodetect_frames(self, prefer: Optional[str] = None) -> FrameSource:
        self.use_frame_source(detect_frame_source(prefer))
        return self._frames

    def set_applied_offset(self, mv: Optional[int]) -> None:
        """Record what the app believes is applied, so samples carry it."""
        self._applied_offset_mv = mv

    # ── lifecycle ────────────────────────────────────────────────────────────

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._frames.start()
        self._thread = threading.Thread(target=self._run, name="voltshift-telemetry",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=self._interval + 3)
            self._thread = None
        self._frames.stop()

    # ── subscription ─────────────────────────────────────────────────────────

    def subscribe(self, callback: Callable[[Sample], None]) -> Callable[[], None]:
        """Register a sample callback; returns an unsubscribe function."""
        with self._lock:
            self._subscribers.append(callback)

        def unsubscribe() -> None:
            with self._lock:
                if callback in self._subscribers:
                    self._subscribers.remove(callback)

        return unsubscribe

    # ── reads ────────────────────────────────────────────────────────────────

    @property
    def latest(self) -> Optional[Sample]:
        return self._latest

    def history(self, seconds: Optional[float] = None) -> list[Sample]:
        with self._lock:
            samples = list(self._history)
        if seconds is None:
            return samples
        cutoff = time.monotonic() - seconds
        return [s for s in samples if s.t >= cutoff]

    def poll_once(self) -> Optional[Sample]:
        """Take one reading synchronously (used by the CLI and by tests)."""
        try:
            metrics = self._bridge.metrics()
            self.consecutive_errors = 0
        except BridgeError as exc:
            self.consecutive_errors += 1
            self._latest = None
            if self.on_error:
                self.on_error(str(exc))
            return None
        try:
            frames = self._frames.stats(self._frame_window)
        except Exception as exc:
            # Optional frame capture must never stop GPU safety telemetry.
            frames = None
            if self.on_error:
                self.on_error(f"frame source unavailable: {exc}")
        sample = Sample.from_metrics(metrics, time.monotonic(), frames,
                                     self._applied_offset_mv)
        self._publish(sample)
        return sample

    def _publish(self, sample: Sample) -> None:
        self._latest = sample
        with self._lock:
            self._history.append(sample)
            subscribers = list(self._subscribers)
        for callback in subscribers:
            try:
                callback(sample)
            except Exception:
                # A broken subscriber must not take down telemetry for
                # everyone else — notably the safety monitors.
                pass

    def _run(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            self.poll_once()
            if self.consecutive_errors >= 5 and self.on_error:
                self.on_error("bridge unreachable — telemetry stopping")
                break
            elapsed = time.monotonic() - started
            self._stop.wait(max(0.05, self._interval - elapsed))
