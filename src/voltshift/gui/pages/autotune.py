"""Auto-Tune — the one-click page.

Pick what you want optimised, press the button, keep playing. The page shows
the baseline it measured, every configuration it tried, and what it finally
applied, because a tuner that changes your hardware without showing its
reasoning is one you cannot trust.
"""

from __future__ import annotations

import customtkinter as ctk

from ... import gameproc
from ...optimizer.objective import GOALS
from ...optimizer.session import SessionConfig, SessionState
from .. import theme
from ..widgets import Card, LogView, SectionLabel, StatTile
from .base import Page


class AutoTunePage(Page):
    title = "Auto-Tune"

    def build(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self._goal_key = self.state.goal
        self._trial_rows: list[ctk.CTkLabel] = []

        self._build_goal_card()
        self._build_control_card()
        self._build_result_card()
        self._build_trials_card()
        self._refresh_availability()

    # ── layout ───────────────────────────────────────────────────────────────

    def _build_goal_card(self) -> None:
        card = Card(self, title="What should AMD GPU Tuner optimise for?")
        card.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        body = card.body()

        self._goal_buttons: dict[str, ctk.CTkButton] = {}
        row = ctk.CTkFrame(body, fg_color="transparent")
        row.pack(fill="x")
        for i in range(len(GOALS)):
            row.grid_columnconfigure(i, weight=1)

        for i, (key, weights) in enumerate(GOALS.items()):
            button = ctk.CTkButton(
                row, text=weights.label, height=42, corner_radius=theme.RADIUS,
                font=(theme.FONT, 13, "bold"),
                fg_color=theme.SURFACE_2, hover_color=theme.SURFACE_3,
                text_color=theme.TEXT_DIM,
                command=lambda k=key: self._pick_goal(k))
            button.grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else 8, 0))
            self._goal_buttons[key] = button

        self._goal_description = ctk.CTkLabel(
            body, text="", font=(theme.FONT, 12), text_color=theme.TEXT_DIM,
            anchor="w", justify="left", wraplength=760)
        self._goal_description.pack(fill="x", pady=(10, 0))
        self._pick_goal(self._goal_key)

    def _build_control_card(self) -> None:
        card = Card(self, title="Run")
        card.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        body = card.body()

        controls = ctk.CTkFrame(body, fg_color="transparent")
        controls.pack(fill="x")

        self._start_button = ctk.CTkButton(
            controls, text="⚡  Auto-Tune", height=48, width=200,
            corner_radius=theme.RADIUS, font=(theme.FONT, 15, "bold"),
            fg_color=theme.ACCENT, hover_color=theme.ACCENT_HOVER,
            command=self._toggle)
        self._start_button.pack(side="left")

        self._status = ctk.CTkLabel(controls, text="idle", font=(theme.FONT, 13),
                                    text_color=theme.TEXT_DIM, anchor="w")
        self._status.pack(side="left", padx=16)

        self._progress = ctk.CTkProgressBar(body, height=8,
                                            progress_color=theme.ACCENT_2)
        self._progress.set(0)
        self._progress.pack(fill="x", pady=(14, 4))

        self._hint = ctk.CTkLabel(
            body,
            text=("Start the game first and leave it running on a normal scene. "
                  "Each trial is measured against the current settings, so the "
                  "workload needs to stay roughly steady."),
            font=(theme.FONT, 11), text_color=theme.TEXT_FAINT,
            anchor="w", justify="left", wraplength=760)
        self._hint.pack(fill="x", pady=(6, 0))

    def _build_result_card(self) -> None:
        card = Card(self, title="Result")
        card.grid(row=2, column=0, sticky="ew", pady=(0, 12))
        body = card.body()

        tiles = ctk.CTkFrame(body, fg_color="transparent")
        tiles.pack(fill="x")
        for i in range(4):
            tiles.grid_columnconfigure(i, weight=1)

        self._tiles: dict[str, StatTile] = {}
        specs = [("trial", "Trial", "", theme.ACCENT),
                 ("best", "Best score", "", theme.ACCENT_2),
                 ("fps", "FPS 1% low", "", theme.GRAPH_CLOCK),
                 ("power", "Board power", "W", theme.GRAPH_POWER)]
        for i, (key, label, unit, color) in enumerate(specs):
            tile = StatTile(tiles, label, unit, color)
            tile.grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else 8, 0))
            self._tiles[key] = tile

        self._applied = ctk.CTkLabel(
            body, text="Nothing applied yet.", font=(theme.FONT_MONO, 12),
            text_color=theme.TEXT_DIM, anchor="w", justify="left", wraplength=760)
        self._applied.pack(fill="x", pady=(12, 0))

    def _build_trials_card(self) -> None:
        card = Card(self, title="Trials")
        card.grid(row=3, column=0, sticky="ew")
        self._log = LogView(card.body(), height=220)
        self._log.pack(fill="both", expand=True)

    # ── behaviour ────────────────────────────────────────────────────────────

    def _pick_goal(self, key: str) -> None:
        self._goal_key = key
        self.state.goal = key
        for name, button in self._goal_buttons.items():
            selected = name == key
            button.configure(
                fg_color=theme.ACCENT if selected else theme.SURFACE_2,
                text_color=theme.TEXT if selected else theme.TEXT_DIM)
        self._goal_description.configure(text=GOALS[key].description)

    def _refresh_availability(self) -> None:
        if self.state.has_stack:
            source = self.state.stack.hub.frame_source
            if source.name == "none":
                self._hint.configure(
                    text=("No frame source detected, so tuning will use power, "
                          "clocks and temperature only. Run "
                          "scripts/fetch_presentmon.ps1 to enable frame-rate "
                          "aware tuning."),
                    text_color=theme.WARN)
            return
        self._start_button.configure(state="disabled")
        self._status.configure(text="this GPU exposes no manual tuning controls",
                               text_color=theme.TEXT_FAINT)

    def _toggle(self) -> None:
        if self.state.autotune_running:
            self.state.stop_autotune()
            return

        game = gameproc.detect_game(
            self.state.stack.hub.frame_source if self.state.has_stack else None)
        exe = game.exe if game else "desktop"

        self._log.append(f"target workload: {exe}", "info")
        self._trial_rows.clear()

        session = self.state.start_autotune(
            SessionConfig(goal=self._goal_key), exe=exe)
        if session is None:
            return

        session.on_state = lambda s: self.state.post(self._on_state, s)
        session.on_progress = lambda label, f: self.state.post(self._on_progress,
                                                               label, f)
        session.on_trial = lambda t: self.state.post(self._on_trial, t)
        session.on_done = lambda r: self.state.post(self._on_done, r)
        session.on_log = lambda m, lvl="info": self.state.post(self._log.append, m, lvl)

        self._start_button.configure(text="■  Stop", fg_color=theme.DANGER,
                                     hover_color=theme.DANGER_HOVER)

    def _on_state(self, state: SessionState) -> None:
        self._status.configure(text=state.value, text_color=theme.TEXT_DIM)

    def _on_progress(self, label: str, fraction: float) -> None:
        self._progress.set(fraction)
        self._status.configure(text=label)

    def _on_trial(self, trial) -> None:
        self._tiles["trial"].set(trial.index + 1)
        best = max((t.value for t in self.state.autotune.trials if t.stable),
                   default=None)
        self._tiles["best"].set(best, "{:+.3f}")

        if trial.candidate_windows:
            window = trial.candidate_windows[0]
            self._tiles["fps"].set(window.fps_p1, "{:.0f}")
            self._tiles["power"].set(window.board_w, "{:.0f}")

        level = "error" if not trial.stable else "volt" if trial.value > 0 else "info"
        self._log.append(
            f"#{trial.index + 1}  {self.state.stack.space.describe(trial.config)}"
            f"   →  {trial.value:+.3f}  {trial.score.explain()}", level)

    def _on_done(self, report) -> None:
        self._start_button.configure(text="⚡  Auto-Tune", fg_color=theme.ACCENT,
                                     hover_color=theme.ACCENT_HOVER)
        self._progress.set(1.0)
        if report.best_config:
            self._applied.configure(
                text=(f"Applied  {self.state.stack.space.describe(report.best_config)}\n"
                      f"{report.best_score.explain()}"),
                text_color=theme.GOOD)
            self._log.append(f"applied: {report.message}", "volt")
        else:
            self._applied.configure(text=report.message, text_color=theme.TEXT_DIM)
            self._log.append(report.message, "warn")

    def on_show(self) -> None:
        self._refresh_availability()
