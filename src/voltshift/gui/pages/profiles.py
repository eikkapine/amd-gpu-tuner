"""Profile capture, offline editing and explicit hardware application."""

from __future__ import annotations

import json
import os

import customtkinter as ctk

from .. import theme
from ..widgets import Card
from ...bridgeclient import BridgeError
from ... import profiles as profile_store
from .base import Page


class ProfilesPage(Page):
    title = "Profiles"

    def build(self) -> None:
        self.grid_columnconfigure(0, weight=1)

        capture = Card(self, title="Capture current state")
        capture.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        body = capture.body()
        body.grid_columnconfigure(0, weight=1)
        self._profile_name = ctk.CTkEntry(body, placeholder_text="Profile name",
                                  fg_color=theme.SURFACE_2, border_color=theme.BORDER)
        self._profile_name.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        ctk.CTkButton(body, text="Capture & save", fg_color=theme.ACCENT,
                      hover_color=theme.ACCENT_HOVER, command=self._save
                      ).grid(row=0, column=1)
        ctk.CTkLabel(body, text="Choose sections to capture or apply. Saving reads the GPU; it does not change it.",
                     font=(theme.FONT, 12), text_color=theme.TEXT_DIM, anchor="w"
                     ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(10, 6))
        choices = ctk.CTkFrame(body, fg_color="transparent")
        choices.grid(row=2, column=0, columnspan=2, sticky="w")
        self._sections = {}
        for i, (key, label) in enumerate((
                ("engine", "Engine"), ("tuning", "Tuning"), ("fans", "Fans"),
                ("gfx", "Graphics"), ("display", "Displays"), ("media", "Media"))):
            variable = ctk.BooleanVar(value=True)
            self._sections[key] = variable
            ctk.CTkCheckBox(choices, text=label, variable=variable, width=100,
                            fg_color=theme.ACCENT).grid(row=i // 3, column=i % 3,
                                                       sticky="w", padx=(0, 16), pady=5)

        listing = Card(self, title="Saved profiles")
        listing.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        self._list_body = listing.body()
        self._list_body.grid_columnconfigure(0, weight=1)

        editor = Card(self, title="Review & edit")
        editor.grid(row=2, column=0, sticky="ew", pady=(0, 12))
        body = editor.body()
        body.grid_columnconfigure(0, weight=1)
        self._summary = ctk.CTkLabel(body, text="Open a saved profile to review it.",
                                     font=(theme.FONT, 12), text_color=theme.TEXT_DIM,
                                     anchor="w", justify="left", wraplength=620)
        self._summary.grid(row=0, column=0, sticky="w", pady=(0, 8))
        self._edit_name = ctk.CTkEntry(body, placeholder_text="Save edits as…",
                                      fg_color=theme.SURFACE_2, border_color=theme.BORDER)
        self._edit_name.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        self._editor = ctk.CTkTextbox(body, height=260, font=("Consolas", 12),
                                      fg_color=theme.SURFACE_2, text_color=theme.TEXT)
        self._editor.grid(row=2, column=0, sticky="ew")
        actions = ctk.CTkFrame(body, fg_color="transparent")
        actions.grid(row=3, column=0, sticky="w", pady=(10, 6))
        for i, (label, command, color) in enumerate((
                ("Save edits", self._save_edits, theme.ACCENT),
                ("Load engine", self._load_engine, theme.SURFACE_3),
                ("Apply selected", self._apply, theme.ACCENT_2))):
            ctk.CTkButton(actions, text=label, width=130, fg_color=color,
                          text_color="#08130f" if color == theme.ACCENT_2 else theme.TEXT,
                          command=command).grid(row=0, column=i, padx=(0, 8))
        ctk.CTkLabel(body, text=("Load engine updates the Dynamic Voltage editor without starting it.\n"
                                 "Apply selected writes hardware settings; stop running tuning modes first."),
                     font=(theme.FONT, 12), text_color=theme.TEXT_DIM, anchor="w", justify="left"
                     ).grid(row=4, column=0, sticky="w")

    def on_show(self) -> None:
        self._refresh_list()

    def _selected_sections(self) -> set[str]:
        selected = {key for key, variable in self._sections.items() if variable.get()}
        if not selected:
            raise ValueError("Select at least one profile section")
        return selected

    def _refresh_list(self) -> None:
        for child in self._list_body.winfo_children():
            child.destroy()
        try:
            profiles = profile_store.list_profiles()
        except OSError as exc:
            self.state.log(f"Cannot list profiles: {exc}", "error")
            return
        if not profiles:
            ctk.CTkLabel(self._list_body, text="No profiles saved yet.",
                         font=(theme.FONT, 12), text_color=theme.TEXT_FAINT, anchor="w"
                         ).grid(row=0, column=0, sticky="w")
            return
        for i, path in enumerate(profiles):
            row = ctk.CTkFrame(self._list_body, fg_color=theme.SURFACE_2, corner_radius=8)
            row.grid(row=i, column=0, sticky="ew", pady=3)
            row.grid_columnconfigure(0, weight=1)
            name = os.path.splitext(os.path.basename(path))[0]
            ctk.CTkLabel(row, text=name, font=(theme.FONT, 13), text_color=theme.TEXT,
                         anchor="w").grid(row=0, column=0, sticky="w", padx=12, pady=8)
            ctk.CTkButton(row, text="Open", width=70, fg_color=theme.ACCENT,
                          hover_color=theme.ACCENT_HOVER, command=lambda p=path: self._open(p)
                          ).grid(row=0, column=1, padx=4)
            ctk.CTkButton(row, text="Delete", width=70, fg_color=theme.SURFACE_3,
                          hover_color=theme.DANGER, command=lambda p=path: self._delete(p)
                          ).grid(row=0, column=2, padx=(0, 8))

    def _save(self) -> None:
        name = self._profile_name.get().strip()
        if not name:
            self.state.log("Enter a profile name first", "warn")
            return
        if not self.state.connected:
            self.state.log("Cannot capture — bridge not connected", "error")
            return
        try:
            profile = profile_store.capture(self.state.bridge, self.state.engine_config,
                                             self._selected_sections())
            path = profile_store.save(profile, name)
            self.state.log(f"Profile saved → {os.path.basename(path)}", "volt")
            self._profile_name.delete(0, "end")
            self._refresh_list()
            self._open(path)
        except (BridgeError, ValueError, OSError) as exc:
            self.state.log(f"Profile save failed: {exc}", "error")

    def _open(self, path: str) -> None:
        try:
            profile = profile_store.load(path)
            self._editor.delete("1.0", "end")
            self._editor.insert("1.0", json.dumps(profile, indent=2))
            self._edit_name.delete(0, "end")
            self._edit_name.insert(0, os.path.splitext(os.path.basename(path))[0])
            present = [key for key in profile_store.SECTIONS if key in profile]
            for key, variable in self._sections.items():
                variable.set(key in present)
            binding = profile.get("binding", {})
            gpu = binding.get("name", profile.get("gpu", "Unknown GPU"))
            bios = binding.get("bios", {}).get("version", "not recorded")
            self._summary.configure(text=f"{gpu} · VBIOS: {bios}\nSections: {', '.join(present)}")
            self.state.log(f"Opened {os.path.basename(path)} for review; hardware unchanged")
        except (ValueError, OSError) as exc:
            self.state.log(f"Cannot open profile: {exc}", "error")

    def _edited_profile(self) -> dict:
        text = self._editor.get("1.0", "end").strip()
        if not text:
            raise ValueError("Open a profile first")
        return profile_store.parse(text)

    def _save_edits(self) -> None:
        try:
            profile = self._edited_profile()
            path = profile_store.save(profile, self._edit_name.get().strip())
            self._refresh_list()
            self._open(path)
            self.state.log(f"Saved edits → {os.path.basename(path)}", "volt")
        except (ValueError, OSError) as exc:
            self.state.log(f"Cannot save edits: {exc}", "error")

    def _load_engine(self) -> None:
        try:
            config = profile_store.engine_config(self._edited_profile())
            previous = self.state.engine_config
            self.state.engine_config = config
            try:
                self.state.save_settings()
            except OSError:
                self.state.engine_config = previous
                raise
            self.state.log("Engine settings loaded. Review them in Dynamic Voltage before starting.", "volt")
            if self.state.engine_running:
                self.state.log("The running engine keeps its current settings until restarted", "warn")
        except (ValueError, OSError) as exc:
            self.state.log(f"Cannot load engine settings: {exc}", "error")

    def _apply(self) -> None:
        if not self.state.connected:
            self.state.log("Cannot apply — bridge not connected", "error")
            return
        if any((self.state.engine_running, self.state.autotune_running,
                self.state.governor_running, self.state.appboost_active)):
            self.state.log("Stop the engine, Auto-Tune, Adaptive and App Boost before applying a profile", "warn")
            return
        try:
            profile = self._edited_profile()
            selected = self._selected_sections() - {"engine"}
            if not selected & set(profile):
                raise ValueError("Select a hardware section in this profile; use Load engine for engine settings")
            results = profile_store.apply(self.state.bridge, profile, selected)
            applied = sum(1 for result in results if result.startswith("applied:"))
            skipped = sum(1 for result in results if result.startswith("skipped:"))
            self.state.log(f"Profile result — {applied} applied, {skipped} skipped")
            for line in results:
                self.state.log(f"  {line}", "volt" if line.startswith("applied:") else "warn")
        except (BridgeError, ValueError, OSError) as exc:
            self.state.log(f"Profile apply failed: {exc}", "error")

    def _delete(self, path: str) -> None:
        try:
            os.remove(path)
            self.state.log(f"Deleted {os.path.basename(path)}")
            self._refresh_list()
        except OSError as exc:
            self.state.log(f"Delete failed: {exc}", "error")
