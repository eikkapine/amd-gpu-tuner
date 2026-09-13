"""Reusable CustomTkinter building blocks for AMD GPU Tuner pages.

Each widget is small and self-contained: a card, a stat tile, a section
header, a toggle row, a labeled slider, a segmented choice, and a scrolling
line-graph canvas. Pages compose these instead of hand-rolling widgets, so
spacing and styling stay consistent.
"""

from __future__ import annotations

import tkinter as tk
from collections import deque
from typing import Callable, Optional

import customtkinter as ctk

from . import theme


class Card(ctk.CTkFrame):
    """A rounded panel with an optional title, the base container on pages."""

    def __init__(self, master, title: Optional[str] = None, **kwargs):
        super().__init__(master, fg_color=theme.SURFACE, corner_radius=theme.RADIUS,
                         border_width=1, border_color=theme.BORDER, **kwargs)
        self._body_row = 0
        if title:
            header = ctk.CTkLabel(self, text=title, anchor="w",
                                  font=(theme.FONT, 15, "bold"), text_color=theme.TEXT)
            header.grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 8))
            self.grid_columnconfigure(0, weight=1)
            self._body_row = 1

    def body(self) -> "ctk.CTkFrame":
        """A transparent frame under the title for page content."""
        frame = ctk.CTkFrame(self, fg_color="transparent")
        frame.grid(row=self._body_row, column=0, sticky="nsew", padx=16, pady=(0, 14))
        self.grid_rowconfigure(self._body_row, weight=1)
        self.grid_columnconfigure(0, weight=1)
        return frame


class StatTile(ctk.CTkFrame):
    """A compact live metric readout: big value, small label and unit."""

    def __init__(self, master, label: str, unit: str = "", accent: str = theme.ACCENT):
        super().__init__(master, fg_color=theme.SURFACE_2, corner_radius=theme.RADIUS,
                         border_width=0)
        self._unit = unit
        self.grid_columnconfigure(0, weight=1)
        self._value = ctk.CTkLabel(self, text="—", font=(theme.FONT_MONO, 26, "bold"),
                                   text_color=accent)
        self._value.grid(row=0, column=0, sticky="w", padx=14, pady=(12, 0))
        self._label = ctk.CTkLabel(self, text=label.upper(), font=(theme.FONT, 10, "bold"),
                                   text_color=theme.TEXT_DIM)
        self._label.grid(row=1, column=0, sticky="w", padx=14, pady=(0, 12))

    def set(self, value, fmt: str = "{}") -> None:
        text = "—" if value is None else fmt.format(value)
        if self._unit and value is not None:
            text = f"{text} {self._unit}"
        self._value.configure(text=text)


class SectionLabel(ctk.CTkLabel):
    def __init__(self, master, text: str):
        super().__init__(master, text=text.upper(), anchor="w",
                         font=(theme.FONT, 11, "bold"), text_color=theme.TEXT_DIM)


class ToggleRow(ctk.CTkFrame):
    """A labeled switch with optional description and supported-state gating."""

    def __init__(self, master, label: str, command: Callable[[bool], None],
                 description: str = ""):
        super().__init__(master, fg_color="transparent")
        self.grid_columnconfigure(0, weight=1)
        self._command = command
        self._var = ctk.BooleanVar(value=False)

        text_col = ctk.CTkFrame(self, fg_color="transparent")
        text_col.grid(row=0, column=0, sticky="w", pady=6)
        ctk.CTkLabel(text_col, text=label, anchor="w", font=(theme.FONT, 13),
                     text_color=theme.TEXT).grid(row=0, column=0, sticky="w")
        if description:
            ctk.CTkLabel(text_col, text=description, anchor="w",
                         font=(theme.FONT, 10), text_color=theme.TEXT_FAINT
                         ).grid(row=1, column=0, sticky="w")

        self._switch = ctk.CTkSwitch(self, text="", variable=self._var, width=44,
                                     command=self._on_toggle, progress_color=theme.ACCENT_2,
                                     button_color=theme.TEXT, button_hover_color=theme.TEXT)
        self._switch.grid(row=0, column=1, sticky="e", padx=(12, 0))

    def _on_toggle(self) -> None:
        self._command(self._var.get())

    def set_state(self, enabled: bool, supported: bool = True) -> None:
        self._var.set(enabled)
        self._switch.configure(state="normal" if supported else "disabled")

    def set_supported(self, supported: bool) -> None:
        self._switch.configure(state="normal" if supported else "disabled")


class LabeledSlider(ctk.CTkFrame):
    """A slider with a name on the left and a live value on the right."""

    def __init__(self, master, label: str, from_: int, to: int, step: int = 1,
                 unit: str = "", command: Optional[Callable[[int], None]] = None):
        super().__init__(master, fg_color="transparent")
        self.grid_columnconfigure(1, weight=1)
        self._unit = unit
        self._command = command
        self._releasing = False
        self._supported = True
        self._minimum = from_
        self._maximum = to
        self._step = max(1, step)
        self._current_value = from_

        self._name_label = ctk.CTkLabel(self, text=label, anchor="w", font=(theme.FONT, 12),
                                  text_color=theme.TEXT, width=120)
        self._name_label.grid(row=0, column=0, sticky="w", padx=(0, 10), pady=6)

        steps = max(1, int((to - from_) / step)) if to > from_ else 1
        self._slider = ctk.CTkSlider(self, from_=from_, to=to, number_of_steps=steps,
                                     command=self._on_move, progress_color=theme.ACCENT,
                                     button_color=theme.ACCENT, button_hover_color=theme.ACCENT_HOVER)
        self._slider.grid(row=0, column=1, sticky="ew", pady=6)
        self._slider.bind("<ButtonRelease-1>", self._on_release)

        self._value = ctk.CTkLabel(self, text="—", font=(theme.FONT_MONO, 12, "bold"),
                                   text_color=theme.ACCENT, width=70)
        self._value.grid(row=0, column=2, sticky="e", padx=(10, 0), pady=6)

    def _fmt(self, value: int) -> str:
        return f"{value}{(' ' + self._unit) if self._unit else ''}"

    def _on_move(self, value: float) -> None:
        if not self._supported:
            return
        self._current_value = min(self._maximum, max(self._minimum,
            self._minimum + round((value - self._minimum) / self._step) * self._step))
        self._value.configure(text=self._fmt(self._current_value))

    def _on_release(self, _event) -> None:
        if self._supported and self._command:
            self._command(self.get())

    def set(self, value: int) -> None:
        self._slider.set(value)
        # Keep the exact readback until the user moves the thumb. The driver
        # can report a current value between its advertised slider steps.
        self._current_value = int(value)
        self._value.configure(text=self._fmt(int(value)))

    def get(self) -> int:
        return self._current_value

    def configure_range(self, from_: int, to: int, step: int = 1) -> None:
        self._minimum = int(from_)
        self._step = max(1, int(step))
        steps = max(0, int((to - from_) // self._step))
        self._maximum = self._minimum + steps * self._step
        self._slider.configure(from_=self._minimum,
                               to=self._maximum if steps else self._minimum + 1,
                               number_of_steps=max(1, steps))

    def set_supported(self, supported: bool) -> None:
        state = "normal" if supported else "disabled"
        self._supported = supported
        self._slider.configure(state=state,
                               progress_color=theme.ACCENT if supported else theme.BORDER,
                               button_color=theme.ACCENT if supported else theme.TEXT_FAINT)
        self._name_label.configure(text_color=theme.TEXT if supported else theme.TEXT_FAINT)
        self._value.configure(text_color=theme.ACCENT if supported else theme.TEXT_FAINT)


class ChoiceRow(ctk.CTkFrame):
    """A labeled segmented control mapping display names to raw values."""

    def __init__(self, master, label: str, options: dict[int, str],
                 command: Callable[[int], None]):
        super().__init__(master, fg_color="transparent")
        self.grid_columnconfigure(1, weight=1)
        self._command = command
        self._values = list(options.keys())
        self._names = [options[k] for k in self._values]

        ctk.CTkLabel(self, text=label, anchor="w", font=(theme.FONT, 12),
                     text_color=theme.TEXT, width=120).grid(row=0, column=0, sticky="w",
                                                            padx=(0, 10), pady=6)
        self._seg = ctk.CTkSegmentedButton(self, values=self._names, command=self._on_pick,
                                           selected_color=theme.ACCENT,
                                           selected_hover_color=theme.ACCENT_HOVER,
                                           unselected_color=theme.SURFACE_2)
        self._seg.grid(row=0, column=1, sticky="ew", pady=6)

    def _on_pick(self, name: str) -> None:
        self._command(self._values[self._names.index(name)])

    def set_value(self, value: int) -> None:
        if value in self._values:
            self._seg.set(self._names[self._values.index(value)])

    def set_supported(self, supported: bool) -> None:
        self._seg.configure(state="normal" if supported else "disabled")


class ScrollGraph(ctk.CTkFrame):
    """A fixed-width scrolling multi-series line graph on a tk.Canvas.

    Series share the frame but each auto-scales to its own configured range.
    Optional horizontal marker lines (used for voltage thresholds).
    """

    def __init__(self, master, height: int = 220, samples: int = 160):
        super().__init__(master, fg_color=theme.SURFACE_2, corner_radius=theme.RADIUS)
        self._samples = samples
        self._canvas = tk.Canvas(self, height=height, bg=theme.SURFACE_2,
                                 highlightthickness=0, bd=0)
        self._canvas.pack(fill="both", expand=True, padx=8, pady=8)
        self._series: dict[str, dict] = {}
        self._markers: list[tuple[float, str]] = []
        self._canvas.bind("<Configure>", lambda _e: self._redraw())

    def add_series(self, key: str, color: str, lo: float, hi: float) -> None:
        self._series[key] = {"color": color, "lo": lo, "hi": hi,
                             "data": deque(maxlen=self._samples)}

    def set_markers(self, markers: list[tuple[float, str]], key: str) -> None:
        """Horizontal reference lines scaled against series `key`'s range."""
        self._marker_key = key
        self._markers = markers

    def push(self, key: str, value: Optional[float]) -> None:
        if key in self._series and value is not None:
            self._series[key]["data"].append(float(value))

    def tick(self) -> None:
        self._redraw()

    def _redraw(self) -> None:
        c = self._canvas
        c.delete("all")
        w = c.winfo_width()
        h = c.winfo_height()
        if w < 20 or h < 20:
            return
        pad = 6

        for gy in range(1, 4):
            y = pad + (h - 2 * pad) * gy / 4
            c.create_line(pad, y, w - pad, y, fill=theme.GRAPH_GRID)

        # Threshold markers, scaled against their series' range.
        marker_series = self._series.get(getattr(self, "_marker_key", ""))
        if marker_series and self._markers:
            lo, hi = marker_series["lo"], marker_series["hi"]
            span = (hi - lo) or 1
            for value, color in self._markers:
                y = pad + (h - 2 * pad) * (1 - (value - lo) / span)
                y = max(pad, min(h - pad, y))
                c.create_line(pad, y, w - pad, y, fill=color, dash=(3, 3))

        for series in self._series.values():
            data = series["data"]
            if len(data) < 2:
                continue
            lo, hi = series["lo"], series["hi"]
            span = (hi - lo) or 1
            n = len(data)
            step_x = (w - 2 * pad) / max(1, self._samples - 1)
            points = []
            start_x = w - pad - step_x * (n - 1)
            for i, value in enumerate(data):
                x = start_x + step_x * i
                y = pad + (h - 2 * pad) * (1 - (value - lo) / span)
                y = max(pad, min(h - pad, y))
                points.extend((x, y))
            if len(points) >= 4:
                c.create_line(*points, fill=series["color"], width=2, smooth=True)


class LogView(ctk.CTkTextbox):
    """A color-coded, append-only session log."""

    _COLORS = {"info": theme.TEXT_DIM, "volt": theme.GOOD,
               "warn": theme.WARN, "error": theme.DANGER}

    def __init__(self, master, **kwargs):
        super().__init__(master, fg_color=theme.SURFACE_2, corner_radius=theme.RADIUS,
                         font=(theme.FONT_MONO, 11), text_color=theme.TEXT_DIM,
                         wrap="word", **kwargs)
        for level, color in self._COLORS.items():
            self.tag_config(level, foreground=color)
        self.configure(state="disabled")

    def append(self, msg: str, level: str = "info") -> None:
        import time
        self.configure(state="normal")
        self.insert("end", f"[{time.strftime('%H:%M:%S')}] ", "info")
        self.insert("end", f"{msg}\n", level)
        self.see("end")
        self.configure(state="disabled")
