"""Crash-survival watchdog — journal before you write.

The failure mode that matters is the one where nothing gets to run
afterwards: an undervolt hangs the machine hard enough that no exception
handler, no `finally`, and no atexit hook fires. Nothing in-process can
recover from that, so recovery has to be arranged *before* the risky write.

The protocol:

  1. Before applying a config, journal it to disk as unverified.
  2. Apply it.
  3. If the machine is still healthy after the probation period, promote the
     journal entry to last-known-good and clear it.

On startup, an unverified journal entry means the previous session died while
that exact config was live. It is recorded as unsafe, the last-known-good
config is offered for restore, and the user is told what happened.

Writes are atomic (temp file plus os.replace) because the whole point is to
survive being killed mid-write.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from . import paths

JOURNAL_FILE = "voltshift_journal.json"
KNOWN_GOOD_FILE = "voltshift_known_good.json"

# How long a config must survive before it is considered proven.
DEFAULT_PROBATION_SEC = 25.0


def _journal_path() -> str:
    return os.path.join(paths.app_dir(), JOURNAL_FILE)


def _known_good_path() -> str:
    return os.path.join(paths.app_dir(), KNOWN_GOOD_FILE)


def _write_atomic(path: str, payload: dict) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _read_json(path: str) -> Optional[dict]:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


@dataclass(frozen=True)
class RecoveryReport:
    """What the previous session was doing when it stopped existing."""

    config: dict
    reason: str
    started_iso: str
    session_id: str
    known_good: Optional[dict]

    def summary(self) -> str:
        return (f"Previous session died while applying {self.reason}. "
                f"That configuration has been marked unsafe.")


class Watchdog:
    def __init__(self, probation_sec: float = DEFAULT_PROBATION_SEC,
                 on_log: Optional[Callable[[str, str], None]] = None):
        self._probation = probation_sec
        self._session_id = uuid.uuid4().hex[:12]
        self._pending: Optional[dict] = None
        self._pending_since = 0.0
        self._on_log = on_log

    def _log(self, message: str, level: str = "info") -> None:
        if self._on_log:
            self._on_log(message, level)

    # ── startup recovery ─────────────────────────────────────────────────────

    def check_previous_session(self, clear: bool = True) -> Optional[RecoveryReport]:
        """Detect a journal left unverified by a session that never returned."""
        entry = _read_json(_journal_path())
        if not isinstance(entry, dict) or not entry or entry.get("verified"):
            return None
        if not isinstance(entry.get("config"), dict):
            return None
        report = RecoveryReport(
            config=entry.get("config", {}),
            reason=entry.get("reason", "an unknown change"),
            started_iso=entry.get("started", ""),
            session_id=entry.get("session", ""),
            known_good=self.known_good(),
        )
        if clear:
            self.clear()
        self._log(report.summary(), "error")
        return report

    # ── journalling ──────────────────────────────────────────────────────────

    def journal(self, config: dict, reason: str = "tuning change") -> None:
        """Record a config as about to be applied and not yet proven safe."""
        self._pending = dict(config)
        self._pending_since = time.monotonic()
        _write_atomic(_journal_path(), {
            "session": self._session_id,
            "started": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "reason": reason,
            "config": self._pending,
            "verified": False,
        })

    def verify(self, force: bool = False) -> bool:
        """Promote the pending config to last-known-good once it has survived.

        Returns True when promotion happened. `force` skips the probation
        timer, for callers that have their own evidence the config is fine.
        """
        if self._pending is None:
            return False
        if not force and time.monotonic() - self._pending_since < self._probation:
            return False
        _write_atomic(_known_good_path(), {
            "config": self._pending,
            "verified_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        })
        self.clear()
        return True

    def abandon(self) -> None:
        """Drop the pending entry without promoting it (an explicit revert)."""
        self.clear()

    def clear(self) -> None:
        self._pending = None
        self._pending_since = 0.0
        try:
            os.remove(_journal_path())
        except OSError:
            pass

    @property
    def pending(self) -> Optional[dict]:
        return dict(self._pending) if self._pending else None

    @property
    def probation_remaining(self) -> float:
        if self._pending is None:
            return 0.0
        return max(0.0, self._probation - (time.monotonic() - self._pending_since))

    # ── known good ───────────────────────────────────────────────────────────

    def known_good(self) -> Optional[dict]:
        entry = _read_json(_known_good_path())
        config = entry.get("config") if isinstance(entry, dict) else None
        return config if isinstance(config, dict) else None

    def set_known_good(self, config: dict) -> None:
        """Record a config as proven without going through probation.

        Used for the machine's factory state at startup, which is safe by
        definition and is what a rollback ultimately falls back to.
        """
        _write_atomic(_known_good_path(), {
            "config": dict(config),
            "verified_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        })


class GuardedApply:
    """Context manager that journals, applies, and reverts on failure.

    Any exception inside the block reverts to the pre-change config and leaves
    the journal cleared, so the next startup does not blame a config that was
    already backed out.
    """

    def __init__(self, watchdog: Watchdog, config: dict, revert_to: dict,
                 apply_fn: Callable[[dict], Any], reason: str = "tuning change"):
        self._watchdog = watchdog
        self._config = config
        self._revert_to = revert_to
        self._apply = apply_fn
        self._reason = reason
        self.applied = False

    def __enter__(self) -> "GuardedApply":
        self._watchdog.journal(self._config, self._reason)
        try:
            self._apply(self._config)
        except Exception:
            self.revert()
            raise
        self.applied = True
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is not None:
            self.revert()
            return False
        return False

    def revert(self) -> None:
        self._apply(self._revert_to)
        self._watchdog.abandon()
        self.applied = False
