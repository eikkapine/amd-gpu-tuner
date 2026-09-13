"""About — app identity, GPU details, safety notes, and credits."""

from __future__ import annotations

import customtkinter as ctk

from .. import theme
from ..widgets import Card
from ... import APP_NAME, __version__
from .base import Page


class AboutPage(Page):
    title = "About"

    def build(self) -> None:
        self.grid_columnconfigure(0, weight=1)

        head = Card(self)
        head.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        hbody = head.body()
        hbody.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(hbody, text=APP_NAME, font=(theme.FONT, 26, "bold"),
                     text_color=theme.TEXT, anchor="w").grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(hbody, text=f"Version {__version__} · Dynamic voltage control and "
                     "full tuning suite for AMD Radeon", font=(theme.FONT, 12),
                     text_color=theme.TEXT_DIM, anchor="w").grid(row=1, column=0, sticky="w")

        self._gpu = Card(self, title="Detected GPU")
        self._gpu.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        self._gpu_body = self._gpu.body()
        self._gpu_body.grid_columnconfigure(1, weight=1)

        safety = Card(self, title="Safety")
        safety.grid(row=2, column=0, sticky="ew", pady=(0, 12))
        sbody = safety.body()
        notes = [
            "Positive voltage offsets are rejected.",
            "Tuning values outside driver-reported ranges or steps are rejected.",
            "Opening the dashboard does not apply or reset GPU tuning.",
            "Manual settings persist until reset. Stopping an active engine requests a restore.",
            "Driver readback confirms settings; it does not prove stability.",
            "The crash logger is read-only; it never writes GPU state.",
            "If a crash leaves settings applied: Adrenalin → Performance → Tuning → Reset.",
        ]
        for i, note in enumerate(notes):
            ctk.CTkLabel(sbody, text=f"•  {note}", font=(theme.FONT, 12),
                         text_color=theme.TEXT_DIM, anchor="w", justify="left"
                         ).grid(row=i, column=0, sticky="w", pady=2)

        credit = Card(self, title="Credits")
        credit.grid(row=3, column=0, sticky="ew")
        cbody = credit.body()
        ctk.CTkLabel(cbody, text="Built on AMD's official ADLX SDK. The dynamic voltage "
                     "engine originates in ClawVolt; the tuning-suite scope is inspired by "
                     "dumbie/RadeonTuner. Workflow improvements also take inspiration from b00nz/mVolt.\n"
                     "Not affiliated with or endorsed by AMD.",
                     font=(theme.FONT, 12), text_color=theme.TEXT_DIM, anchor="w",
                     justify="left", wraplength=760).grid(row=0, column=0, sticky="w")

    def on_show(self) -> None:
        for child in self._gpu_body.winfo_children():
            child.destroy()
        info = self.state.info
        if not info:
            ctk.CTkLabel(self._gpu_body, text="No GPU connected.", font=(theme.FONT, 12),
                         text_color=theme.TEXT_FAINT).grid(row=0, column=0, sticky="w")
            return
        bios = info.get("bios", {})
        rows = [
            ("Name", info.get("name", "—")),
            ("VRAM", f"{info.get('vramMb', 0)} MB {info.get('vramType', '')}"),
            ("Device ID", f"{info.get('deviceId', '—')} (rev {info.get('revisionId', '—')})"),
            ("BIOS", f"{bios.get('version', '—')}  ({bios.get('date', '—')})"),
            ("Bridge", f"v{self.state.bridge.version or '—'}"),
        ]
        for i, (label, value) in enumerate(rows):
            ctk.CTkLabel(self._gpu_body, text=label, font=(theme.FONT, 12, "bold"),
                         text_color=theme.TEXT_DIM, anchor="w", width=90
                         ).grid(row=i, column=0, sticky="w", pady=2)
            ctk.CTkLabel(self._gpu_body, text=value, font=(theme.FONT, 12),
                         text_color=theme.TEXT, anchor="w").grid(row=i, column=1, sticky="w", pady=2)
