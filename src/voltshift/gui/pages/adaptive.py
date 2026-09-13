"""Adaptive — the live governor, and what it has learned.

Two halves. The top is the switch and its guardrails: how much the governor
is allowed to experiment while you play. The bottom is the memory: the best
configuration found per game, and the stability frontier learned for this
specific card.
"""

from __future__ import annotations

import customtkinter as ctk

from ...adaptive import ProbeBudget
from ...optimizer.objective import GOALS
from .. import theme
from ..widgets import Card, LabeledSlider, LogView, SectionLabel, StatTile
from .base import Page


class AdaptivePage(Page):
    title = "Adaptive"

    def build(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self._build_control_card()
        self._build_status_card()
        self._build_knowledge_card()
        self._build_log_card()
        self._refresh_availability()

    # ── layout ───────────────────────────────────────────────────────────────

    def _build_control_card(self) -> None:
        card = Card(self, title="Live governor")
        card.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        body = card.body()

        ctk.CTkLabel(
            body,
            text=("Detects the game you are playing, applies what was learned "
                  "for it, and keeps watching. Any sign of instability reverts "
                  "immediately and is remembered for good."),
            font=(theme.FONT, 12), text_color=theme.TEXT_DIM,
            anchor="w", justify="left", wraplength=760).pack(fill="x", pady=(0, 12))

        controls = ctk.CTkFrame(body, fg_color="transparent")
        controls.pack(fill="x")

        self._toggle_button = ctk.CTkButton(
            controls, text="▶  Start governor", height=44, width=190,
            corner_radius=theme.RADIUS, font=(theme.FONT, 14, "bold"),
            fg_color=theme.ACCENT_2, hover_color=theme.ACCENT_2_HOVER,
            text_color="#04241c", command=self._toggle)
        self._toggle_button.pack(side="left")

        self._state_label = ctk.CTkLabel(controls, text="stopped",
                                         font=(theme.FONT, 13),
                                         text_color=theme.TEXT_DIM)
        self._state_label.pack(side="left", padx=16)

        SectionLabel(body, "In-game experimentation").pack(fill="x", pady=(18, 4))
        ctk.CTkLabel(
            body,
            text=("Probing finds gains this card can reach that no preset knows "
                  "about, but it experiments on a live game. Set the budget to "
                  "zero to apply learned profiles only and never experiment."),
            font=(theme.FONT, 11), text_color=theme.TEXT_FAINT,
            anchor="w", justify="left", wraplength=760).pack(fill="x", pady=(0, 8))

        self._probes = LabeledSlider(body, "Probes per game", 0, 20, 1,
                                     command=lambda _v: None)
        self._probes.set(8)
        self._probes.pack(fill="x", pady=2)

        self._interval = LabeledSlider(body, "Minimum seconds between probes",
                                       30, 600, 30, command=lambda _v: None)
        self._interval.set(120)
        self._interval.pack(fill="x", pady=2)

    def _build_status_card(self) -> None:
        card = Card(self, title="Now")
        card.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        body = card.body()

        tiles = ctk.CTkFrame(body, fg_color="transparent")
        tiles.pack(fill="x")
        for i in range(4):
            tiles.grid_columnconfigure(i, weight=1)

        self._tiles: dict[str, StatTile] = {}
        specs = [("game", "Game", "", theme.ACCENT),
                 ("phase", "Workload", "", theme.ACCENT_2),
                 ("probes", "Probes left", "", theme.GRAPH_VOLT),
                 ("profile", "Profile", "", theme.TEXT)]
        for i, (key, label, unit, color) in enumerate(specs):
            tile = StatTile(tiles, label, unit, color)
            tile.grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else 8, 0))
            self._tiles[key] = tile

        self._applied = ctk.CTkLabel(body, text="—", font=(theme.FONT_MONO, 12),
                                     text_color=theme.TEXT_DIM, anchor="w",
                                     justify="left", wraplength=760)
        self._applied.pack(fill="x", pady=(12, 0))

    def _build_knowledge_card(self) -> None:
        card = Card(self, title="What this GPU has learned")
        card.grid(row=2, column=0, sticky="ew", pady=(0, 12))
        body = card.body()

        header = ctk.CTkFrame(body, fg_color="transparent")
        header.pack(fill="x")
        ctk.CTkButton(header, text="Refresh", width=90, height=30,
                      corner_radius=theme.RADIUS, fg_color=theme.SURFACE_2,
                      hover_color=theme.SURFACE_3, font=(theme.FONT, 12),
                      command=self._refresh_knowledge).pack(side="right")

        self._knowledge = ctk.CTkLabel(body, text="", font=(theme.FONT_MONO, 11),
                                       text_color=theme.TEXT_DIM, anchor="w",
                                       justify="left")
        self._knowledge.pack(fill="x", pady=(10, 0))

    def _build_log_card(self) -> None:
        card = Card(self, title="Governor log")
        card.grid(row=3, column=0, sticky="ew")
        self._log = LogView(card.body(), height=180)
        self._log.pack(fill="both", expand=True)

    # ── behaviour ────────────────────────────────────────────────────────────

    def _refresh_availability(self) -> None:
        if not self.state.has_stack:
            self._toggle_button.configure(state="disabled")
            self._state_label.configure(
                text="this GPU exposes no manual tuning controls",
                text_color=theme.TEXT_FAINT)

    def _toggle(self) -> None:
        if self.state.governor_running:
            self.state.stop_governor()
            self._toggle_button.configure(text="▶  Start governor",
                                          fg_color=theme.ACCENT_2,
                                          hover_color=theme.ACCENT_2_HOVER)
            self._state_label.configure(text="stopped", text_color=theme.TEXT_DIM)
            return

        budget = ProbeBudget(max_probes=self._probes.get(),
                             min_interval_sec=float(self._interval.get()))
        governor = self.state.start_governor(budget)
        if governor is None:
            return

        governor.on_log = lambda m, lvl="info": self.state.post(self._log.append, m, lvl)
        governor.on_status = lambda s: self.state.post(self._on_status, s)

        self._toggle_button.configure(text="■  Stop governor",
                                      fg_color=theme.DANGER,
                                      hover_color=theme.DANGER_HOVER)
        self._state_label.configure(text=f"running — {GOALS[self.state.goal].label}",
                                    text_color=theme.GOOD)

    def _on_status(self, status) -> None:
        self._tiles["game"].set(status.game or "—")
        self._tiles["phase"].set(status.phase.value)
        self._tiles["probes"].set(status.probes_left)
        self._tiles["profile"].set("learned" if status.learned else "exploring")
        if status.applied:
            self._applied.configure(
                text=self.state.stack.space.describe(status.applied))

    def _refresh_knowledge(self) -> None:
        if not self.state.has_stack:
            return
        stack = self.state.stack
        stats = stack.knowledge.stats(stack.gpu_key)
        lines = [
            f"observations {stats['observations']}    games {stats['games']}    "
            f"unsafe {stats['unsafe']}    frontier bands {stats['frontier_bands']}",
            "",
        ]
        games = stack.knowledge.known_games(stack.gpu_key)
        if games:
            lines.append("best per game")
            for entry in games[:10]:
                config = stack.knowledge.best_config(stack.gpu_key, entry["exe"],
                                                     entry["goal"])
                lines.append(f"  {entry['exe']:<26} {entry['goal']:<11} "
                             f"{stack.space.describe(config or {})}")
        else:
            lines.append("nothing learned yet — run Auto-Tune while a game is open")

        frontier = stack.knowledge.frontier(stack.gpu_key)
        if frontier:
            lines += ["", "stability frontier (lowest voltage that misbehaved)"]
            for band in frontier:
                lines.append(f"  ~{band['clock_mhz']:>5} MHz    "
                             f"{band['failed_mv']:+5d} mV    "
                             f"{band['failures']} event(s)")

        self._knowledge.configure(text="\n".join(lines))

    def on_show(self) -> None:
        self._refresh_availability()
        self._refresh_knowledge()
