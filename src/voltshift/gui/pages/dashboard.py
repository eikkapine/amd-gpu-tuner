"""Dashboard — live metrics at a glance: stat tiles + scrolling graphs."""

from __future__ import annotations

import customtkinter as ctk

from .. import theme
from ..widgets import Card, ScrollGraph, StatTile
from .base import Page


class DashboardPage(Page):
    title = "Dashboard"

    def build(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(header, text="GPU overview", font=(theme.FONT, 23, "bold"),
                     text_color=theme.TEXT, anchor="w").grid(row=0, column=0, sticky="w")
        self._copy_button = ctk.CTkButton(header, text="Copy system summary", width=165,
                                         fg_color=theme.SURFACE_2, hover_color=theme.SURFACE_3,
                                         command=self._copy_summary)
        self._copy_button.grid(row=0, column=1)
        self._session_note = ctk.CTkLabel(self, text="", anchor="w", justify="left",
                                          wraplength=760, font=(theme.FONT, 12),
                                          text_color=theme.TEXT_DIM)
        self._session_note.grid(row=1, column=0, sticky="ew", pady=(0, 14))

        # Stat tiles row.
        tiles = ctk.CTkFrame(self, fg_color="transparent")
        tiles.grid(row=2, column=0, sticky="ew", pady=(0, 14))
        for i in range(4):
            tiles.grid_columnconfigure(i, weight=1)

        self._tiles: dict[str, StatTile] = {}
        specs = [
            ("clock", "Core clock", "MHz", theme.GRAPH_CLOCK),
            ("temp", "Temperature", "°C", theme.GRAPH_TEMP),
            ("power", "Board power", "W", theme.GRAPH_POWER),
            ("voltage", "Voltage", "mV", theme.GRAPH_VOLT),
        ]
        for i, (key, label, unit, color) in enumerate(specs):
            tile = StatTile(tiles, label, unit, color)
            tile.grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else 8, 0))
            self._tiles[key] = tile

        # Second tile row.
        tiles2 = ctk.CTkFrame(self, fg_color="transparent")
        tiles2.grid(row=3, column=0, sticky="ew", pady=(0, 14))
        for i in range(4):
            tiles2.grid_columnconfigure(i, weight=1)
        specs2 = [
            ("usage", "GPU load", "%", theme.ACCENT),
            ("hotspot", "Hotspot", "°C", theme.GRAPH_TEMP),
            ("vram", "VRAM clock", "MHz", theme.ACCENT_2),
            ("fan", "Fan", "RPM", theme.TEXT),
        ]
        for i, (key, label, unit, color) in enumerate(specs2):
            tile = StatTile(tiles2, label, unit, color)
            tile.grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else 8, 0))
            self._tiles[key] = tile

        # Frame pacing. Only meaningful when a frame source is present, so
        # the row states plainly when it has nothing to show rather than
        # sitting there with four empty dashes.
        tiles3 = ctk.CTkFrame(self, fg_color="transparent")
        tiles3.grid(row=4, column=0, sticky="ew", pady=(0, 14))
        for i in range(4):
            tiles3.grid_columnconfigure(i, weight=1)
        specs3 = [
            ("fps", "FPS", "", theme.ACCENT),
            ("fps_low", "1% low", "", theme.GRAPH_VOLT),
            ("frametime", "Frametime", "ms", theme.GRAPH_CLOCK),
            ("perf_w", "FPS / watt", "", theme.ACCENT_2),
        ]
        for i, (key, label, unit, color) in enumerate(specs3):
            tile = StatTile(tiles3, label, unit, color)
            tile.grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else 8, 0))
            self._tiles[key] = tile

        self._frame_note = ctk.CTkLabel(
            self, text="", font=(theme.FONT, 11), text_color=theme.TEXT_FAINT,
            anchor="w", justify="left")
        self._frame_note.grid(row=5, column=0, sticky="ew", pady=(0, 10))

        # Graphs.
        clock_card = Card(self, title="Core clock")
        clock_card.grid(row=6, column=0, sticky="ew", pady=(0, 12))
        self._clock_graph = ScrollGraph(clock_card.body(), height=190)
        self._clock_graph.add_series("clock", theme.GRAPH_CLOCK, 0, 3400)
        self._clock_graph.pack(fill="both", expand=True)

        frame_card = Card(self, title="Frametime")
        frame_card.grid(row=7, column=0, sticky="ew", pady=(0, 12))
        self._frame_graph = ScrollGraph(frame_card.body(), height=150)
        self._frame_graph.add_series("frametime", theme.GRAPH_VOLT, 0, 50)
        self._frame_graph.pack(fill="both", expand=True)

        dual = ctk.CTkFrame(self, fg_color="transparent")
        dual.grid(row=8, column=0, sticky="ew")
        dual.grid_columnconfigure(0, weight=1)
        dual.grid_columnconfigure(1, weight=1)

        temp_card = Card(dual, title="Temperature")
        temp_card.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self._temp_graph = ScrollGraph(temp_card.body(), height=150)
        self._temp_graph.add_series("temp", theme.GRAPH_TEMP, 20, 110)
        self._temp_graph.add_series("hotspot", theme.DANGER, 20, 110)
        self._temp_graph.pack(fill="both", expand=True)

        power_card = Card(dual, title="Board power")
        power_card.grid(row=0, column=1, sticky="ew", padx=(6, 0))
        self._power_graph = ScrollGraph(power_card.body(), height=150)
        self._power_graph.add_series("power", theme.GRAPH_POWER, 0, 360)
        self._power_graph.pack(fill="both", expand=True)

        self.state.sample_sinks.append(self._on_sample)

    def on_show(self) -> None:
        self._update_session_note()

    def _update_session_note(self) -> None:
        if not self.state.connected:
            self._session_note.configure(
                text=f"Connection unavailable: {self.state.connect_error or 'GPU not detected'}. See Logs for details.",
                text_color=theme.WARN)
            self._copy_button.configure(state="disabled")
            return
        active = [name for attr, name in (("engine_running", "Dynamic Voltage"),
                  ("autotune_running", "Auto-Tune"), ("governor_running", "Adaptive"),
                  ("appboost_active", "App Boost")) if getattr(self.state, attr, False)]
        if active:
            text = "Active: " + ", ".join(active)
        else:
            text = "Monitoring only · Automatic tuning is stopped. Opening this dashboard does not change GPU settings."
        self._session_note.configure(text=text, text_color=theme.GOOD if active else theme.TEXT_DIM)

    def _copy_summary(self) -> None:
        from ...diagnostics import collect_report, format_summary
        try:
            report = collect_report(self.state.bridge)
            self.clipboard_clear()
            self.clipboard_append(format_summary(report))
            self._copy_button.configure(text="Summary copied")
            self.after(2500, lambda: self._copy_button.configure(text="Copy system summary"))
        except Exception as exc:
            self._session_note.configure(text=f"Could not copy summary: {exc}", text_color=theme.WARN)
            self.state.log(f"Copy system summary failed: {exc}", "error")

    def _on_sample(self, s: dict) -> None:
        self._update_session_note()
        clock = s.get("clockMhz")
        temp = s.get("tempC")
        hotspot = s.get("hotspotC")
        power = s.get("boardPowerW")
        if power is None:
            power = s.get("powerW")
        voltage = s.get("voltageMv")

        self._tiles["clock"].set(clock)
        self._tiles["temp"].set(temp, "{:.0f}")
        self._tiles["power"].set(power, "{:.0f}")
        self._tiles["voltage"].set(voltage)
        self._tiles["usage"].set(s.get("usagePct"), "{:.0f}")
        self._tiles["hotspot"].set(hotspot, "{:.0f}")
        self._tiles["vram"].set(s.get("vramClockMhz"))
        self._tiles["fan"].set(s.get("fanRpm"))

        fps = s.get("fps")
        frametime = s.get("frametimeMs")
        self._tiles["fps"].set(fps, "{:.0f}")
        self._tiles["fps_low"].set(s.get("fpsLow1"), "{:.0f}")
        self._tiles["frametime"].set(frametime, "{:.1f}")
        self._tiles["perf_w"].set(
            (fps / power) if (fps and power) else None, "{:.2f}")

        if fps is None:
            self._frame_note.configure(
                text=("Waiting for game frame data. Start a game with PresentMon or RTSS available "
                      "to measure frame rate, frame pacing and efficiency."))
        else:
            self._frame_note.configure(
                text=f"{s.get('frameProcess', 'unknown')} "
                     f"via {s.get('frameSource', '?')}")

        self._clock_graph.push("clock", clock)
        self._temp_graph.push("temp", temp)
        self._temp_graph.push("hotspot", hotspot)
        self._power_graph.push("power", power)
        self._frame_graph.push("frametime", frametime)
        self._clock_graph.tick()
        self._temp_graph.tick()
        self._power_graph.tick()
        self._frame_graph.tick()
