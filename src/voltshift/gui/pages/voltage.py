"""Dynamic Voltage — AMD GPU Tuner identity page.

Edit the clock->voltage thresholds, start/stop the engine, and watch the
live core clock with the threshold offsets drawn as reference lines. The
engine applies a different voltage offset per clock range, adapting live.
"""

from __future__ import annotations

import customtkinter as ctk

from .. import theme
from ..widgets import Card, ScrollGraph
from ...engine import EngineConfig, Threshold
from .base import Page


class ThresholdRow(ctk.CTkFrame):
    """One editable clock/offset pair with a color dot and remove button."""

    def __init__(self, master, color: str, clock: int, offset: int, on_remove):
        super().__init__(master, fg_color="transparent")
        self.grid_columnconfigure(3, weight=1)

        dot = ctk.CTkLabel(self, text="●", text_color=color, font=(theme.FONT, 14))
        dot.grid(row=0, column=0, padx=(0, 8))

        ctk.CTkLabel(self, text="≥", font=(theme.FONT, 12), text_color=theme.TEXT_DIM
                     ).grid(row=0, column=1)
        self.clock_var = ctk.StringVar(value=str(clock))
        ctk.CTkEntry(self, textvariable=self.clock_var, width=70,
                     fg_color=theme.SURFACE_2, border_color=theme.BORDER
                     ).grid(row=0, column=2, padx=4, pady=4)
        ctk.CTkLabel(self, text="MHz  →", font=(theme.FONT, 12), text_color=theme.TEXT_DIM
                     ).grid(row=0, column=3, sticky="w")

        self.offset_var = ctk.StringVar(value=str(offset))
        ctk.CTkEntry(self, textvariable=self.offset_var, width=70,
                     fg_color=theme.SURFACE_2, border_color=theme.BORDER
                     ).grid(row=0, column=4, padx=4)
        ctk.CTkLabel(self, text="mV", font=(theme.FONT, 12), text_color=theme.TEXT_DIM
                     ).grid(row=0, column=5, padx=(0, 8))

        ctk.CTkButton(self, text="✕", width=28, fg_color=theme.SURFACE_2,
                      hover_color=theme.DANGER, text_color=theme.TEXT_DIM,
                      command=lambda: on_remove(self)).grid(row=0, column=6)

    def values(self) -> tuple[int, int] | None:
        try:
            return int(self.clock_var.get()), int(self.offset_var.get())
        except ValueError:
            return None


_ROW_COLORS = [theme.GRAPH_CLOCK, theme.ACCENT_2, theme.GRAPH_TEMP,
               theme.GRAPH_VOLT, theme.DANGER, theme.WARN]


class VoltagePage(Page):
    title = "Dynamic Voltage"

    def build(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self._rows: list[ThresholdRow] = []

        # Control bar.
        control = Card(self)
        control.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        cbody = control.body()
        cbody.grid_columnconfigure(3, weight=1)

        self._start_btn = ctk.CTkButton(
            cbody, text="▶  Start engine", width=150, height=40,
            fg_color=theme.ACCENT_2, hover_color=theme.ACCENT_2_HOVER,
            text_color="#08130f", font=(theme.FONT, 13, "bold"),
            command=self._toggle_engine)
        self._start_btn.grid(row=0, column=0, padx=(0, 10))

        self._status = ctk.CTkLabel(cbody, text="Stopped", font=(theme.FONT, 13),
                                    text_color=theme.TEXT_DIM)
        self._status.grid(row=0, column=1, sticky="w")

        # Live graph with threshold markers.
        graph_card = Card(self, title="Core clock (live)")
        graph_card.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        self._graph = ScrollGraph(graph_card.body(), height=200)
        self._graph.add_series("clock", theme.GRAPH_CLOCK, 0, 3400)
        self._graph.pack(fill="both", expand=True)

        # Threshold editor.
        editor = Card(self, title="Voltage thresholds")
        editor.grid(row=2, column=0, sticky="ew", pady=(0, 12))
        ebody = editor.body()
        ebody.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(ebody, text="Applied highest-clock-first; below all thresholds "
                     "the idle offset is used.", font=(theme.FONT, 11),
                     text_color=theme.TEXT_FAINT, anchor="w").grid(row=0, column=0,
                                                                   sticky="w", pady=(0, 8))
        self._rows_frame = ctk.CTkFrame(ebody, fg_color="transparent")
        self._rows_frame.grid(row=1, column=0, sticky="ew")
        self._rows_frame.grid_columnconfigure(0, weight=1)

        add_row = ctk.CTkFrame(ebody, fg_color="transparent")
        add_row.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        ctk.CTkButton(add_row, text="+ Add threshold", width=140,
                      fg_color=theme.SURFACE_2, hover_color=theme.SURFACE_3,
                      command=lambda: self._add_row(3000, -100)).pack(side="left")

        # Idle + hysteresis + poll settings.
        settings = Card(self, title="Engine settings")
        settings.grid(row=3, column=0, sticky="ew", pady=(0, 12))
        sbody = settings.body()
        sbody.grid_columnconfigure(1, weight=1)
        sbody.grid_columnconfigure(3, weight=1)

        self._idle_var = ctk.StringVar(value=str(self.state.engine_config.idle_offset_mv))
        self._hyst_var = ctk.StringVar(value=str(self.state.engine_config.hysteresis_count))
        self._poll_var = ctk.StringVar(value=str(self.state.engine_config.poll_interval_sec))
        self._labeled_entry(sbody, "Idle offset (mV)", self._idle_var, 0, 0)
        self._labeled_entry(sbody, "Hysteresis (reads)", self._hyst_var, 0, 2)
        self._labeled_entry(sbody, "Poll interval (s)", self._poll_var, 1, 0)

        save = ctk.CTkButton(sbody, text="Apply & save", width=130, fg_color=theme.ACCENT,
                             hover_color=theme.ACCENT_HOVER, command=self._apply_config)
        save.grid(row=1, column=3, sticky="e", pady=6)

        self._load_from_config()
        self.state.sample_sinks.append(self._on_sample)

    def _labeled_entry(self, parent, label, var, row, col) -> None:
        ctk.CTkLabel(parent, text=label, font=(theme.FONT, 12), text_color=theme.TEXT_DIM,
                     anchor="w").grid(row=row, column=col, sticky="w", padx=(0, 8), pady=6)
        ctk.CTkEntry(parent, textvariable=var, width=90, fg_color=theme.SURFACE_2,
                     border_color=theme.BORDER).grid(row=row, column=col + 1, sticky="w", pady=6)

    # ── threshold rows ───────────────────────────────────────────────────────

    def _add_row(self, clock: int, offset: int) -> None:
        color = _ROW_COLORS[len(self._rows) % len(_ROW_COLORS)]
        row = ThresholdRow(self._rows_frame, color, clock, offset, self._remove_row)
        row.grid(sticky="ew", pady=2)
        self._rows.append(row)
        self._refresh_markers()

    def _remove_row(self, row: ThresholdRow) -> None:
        row.destroy()
        self._rows.remove(row)
        self._refresh_markers()

    def _load_from_config(self) -> None:
        config = self.state.engine_config
        self._loaded_config = config.to_dict()
        self._idle_var.set(str(config.idle_offset_mv))
        self._hyst_var.set(str(config.hysteresis_count))
        self._poll_var.set(str(config.poll_interval_sec))
        for row in self._rows:
            row.destroy()
        self._rows.clear()
        for t in sorted(self.state.engine_config.thresholds,
                        key=lambda t: t.clock_mhz, reverse=True):
            self._add_row(t.clock_mhz, t.offset_mv)

    def _collect_config(self) -> EngineConfig:
        thresholds = []
        for row in self._rows:
            vals = row.values()
            if vals is None:
                raise ValueError(
                    f"Invalid threshold values in row: clock='{row.clock_var.get()}', "
                    f"offset='{row.offset_var.get()}'"
                )
            thresholds.append(Threshold(vals[0], vals[1]))
        try:
            idle = int(self._idle_var.get())
            hyst = int(self._hyst_var.get())
            poll = float(self._poll_var.get())
        except ValueError as exc:
            raise ValueError(f"Invalid engine setting: {exc}") from exc
        return EngineConfig(poll_interval_sec=poll, hysteresis_count=hyst,
                            idle_offset_mv=idle, thresholds=thresholds).clamped()

    def _refresh_markers(self) -> None:
        markers = []
        for i, row in enumerate(self._rows):
            vals = row.values()
            if vals:
                color = _ROW_COLORS[i % len(_ROW_COLORS)]
                markers.append((float(vals[0]), color))
        self._graph.set_markers(markers, "clock")

    def _apply_config(self) -> None:
        try:
            self.state.engine_config = self._collect_config()
        except ValueError as exc:
            self.state.log(str(exc), "error")
            return
        self.state.save_settings()
        self._refresh_markers()
        self.state.log("Engine config saved")
        if self.state.engine_running:
            self.state.log("Restart the engine to apply new thresholds", "warn")

    # ── engine control ───────────────────────────────────────────────────────

    def _toggle_engine(self) -> None:
        if self.state.engine_running:
            self.state.stop_engine()
            self._start_btn.configure(text="▶  Start engine", fg_color=theme.ACCENT_2)
            self._status.configure(text="Stopped", text_color=theme.TEXT_DIM)
        else:
            if not self.state.connected:
                self.state.log("Cannot start — bridge not connected", "error")
                return
            try:
                self.state.engine_config = self._collect_config()
            except ValueError as exc:
                self.state.log(str(exc), "error")
                return
            self.state.save_settings()
            self.state.start_engine()
            self._start_btn.configure(text="■  Stop engine", fg_color=theme.DANGER)
            self._status.configure(text="Running", text_color=theme.GOOD)

    def _on_sample(self, s: dict) -> None:
        clock = s.get("clockMhz")
        self._graph.push("clock", clock)
        self._graph.tick()
        if self.state.engine_running:
            offset = s.get("appliedOffsetMv")
            offset_txt = f"{offset:+d} mV" if offset is not None else "settling…"
            self._status.configure(text=f"Running · {clock} MHz · {offset_txt}",
                                   text_color=theme.GOOD)

    def on_show(self) -> None:
        if self.state.engine_config.to_dict() != self._loaded_config:
            self._load_from_config()
        # Reflect engine state if it was toggled from elsewhere.
        if self.state.engine_running:
            self._start_btn.configure(text="■  Stop engine", fg_color=theme.DANGER)
        else:
            self._start_btn.configure(text="▶  Start engine", fg_color=theme.ACCENT_2)
        self._refresh_markers()
