"""Manual tuning with staged edits, capability gating and driver readback."""

from __future__ import annotations

import customtkinter as ctk

from .. import theme
from ..widgets import Card, LabeledSlider
from ...adlxenums import MEMORY_TIMINGS
from ...bridgeclient import BridgeError
from .base import Page


# key: group, driver value, driver range, display label, unit
CONTROLS = {
    "voltage": ("gfx", "voltageMv", "voltageRange", "Voltage offset", "mV"),
    "min_clock": ("gfx", "minFreqMhz", "minFreqRange", "Min clock", "MHz"),
    "max_clock": ("gfx", "maxFreqMhz", "maxFreqRange", "Max clock", "MHz"),
    "vram_clock": ("vram", "maxFreqMhz", "maxFreqRange", "VRAM max clock", "MHz"),
    "power_limit": ("power", "powerLimit", "powerLimitRange", "Power limit", "%"),
    "tdc": ("power", "tdcLimit", "tdcRange", "TDC limit", "A"),
}


def control_range(section: dict, key: str, range_key: str, *, voltage=False):
    """Return a usable range only when the driver supplied every required value."""
    limits = section.get(range_key)
    if "unsupported" in section or not isinstance(limits, dict):
        return None
    values = [section.get(key), limits.get("min"), limits.get("max"), limits.get("step")]
    if any(type(value) is not int for value in values):
        return None
    current, lower, upper, step = values
    if step <= 0 or lower > upper or not lower <= current <= upper:
        return None
    # Absolute voltage is not an offset control. Positive writes are blocked.
    if voltage:
        if lower > 0 or current > 0:
            return None
        upper = min(upper, 0)
    if upper - lower < step:
        return None
    return lower, upper, step


class TuningPage(Page):
    title = "Tuning"

    def build(self) -> None:
        self._applied: dict[str, int] = {}
        self._supported: set[str] = set()
        self._sliders = {}
        self._buttons = {}
        self._group_status = {}
        self._timing_options: dict[str, int] = {}
        self._snapshot: dict = {}

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(header, text="Manual tuning", font=(theme.FONT, 23, "bold"),
                     text_color=theme.TEXT, anchor="w").grid(row=0, column=0, sticky="w")
        ctk.CTkButton(header, text="Reload applied values", width=160,
                      fg_color=theme.SURFACE_2, hover_color=theme.SURFACE_3,
                      command=self._refresh).grid(row=0, column=1)
        ctk.CTkLabel(header, text="Stage changes, then apply each section. Manual settings persist until reset.",
                     font=(theme.FONT, 11), text_color=theme.TEXT_DIM, anchor="w",
                     wraplength=690).grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))
        self._status = ctk.CTkLabel(self, text="Waiting for driver readback…", anchor="w",
                                    justify="left", wraplength=780, font=(theme.FONT, 12),
                                    text_color=theme.TEXT_DIM)
        self._status.grid(row=1, column=0, sticky="ew", pady=(0, 10))

        for row, (group, title, button_text) in enumerate((
            ("gfx", "Core & voltage", "Apply core & voltage"),
            ("vram", "Memory", "Apply memory"),
            ("power", "Power", "Apply power"),
        ), start=2):
            card = Card(self, title=title)
            card.grid(row=row, column=0, sticky="ew", pady=(0, 12))
            body = card.body()
            body.grid_columnconfigure(0, weight=1)
            item_row = 0
            for key, (control_group, _, _, label, unit) in CONTROLS.items():
                if control_group != group:
                    continue
                slider = LabeledSlider(body, label, 0, 1, 1, unit,
                                       command=lambda _value: self._update_staged())
                slider.set_supported(False)
                slider.grid(row=item_row, column=0, sticky="ew")
                self._sliders[key] = slider
                item_row += 1
            if group == "vram":
                timing_row = ctk.CTkFrame(body, fg_color="transparent")
                timing_row.grid(row=item_row, column=0, sticky="ew", pady=6)
                ctk.CTkLabel(timing_row, text="Memory timing", font=(theme.FONT, 12),
                             text_color=theme.TEXT, width=130, anchor="w").pack(side="left")
                self._timing_menu = ctk.CTkOptionMenu(
                    timing_row, values=["Unavailable"], state="disabled",
                    command=lambda _name: self._update_staged(),
                    fg_color=theme.SURFACE_2, button_color=theme.SURFACE_3,
                    button_hover_color=theme.BORDER)
                self._timing_menu.pack(side="left")
                item_row += 1
            status = ctk.CTkLabel(body, text="Not read", anchor="w", justify="left",
                                  wraplength=720, font=(theme.FONT, 11), text_color=theme.TEXT_DIM)
            status.grid(row=item_row, column=0, sticky="ew", pady=(8, 4))
            self._group_status[group] = status
            button = ctk.CTkButton(body, text=button_text, state="disabled",
                                   fg_color=theme.ACCENT, hover_color=theme.ACCENT_HOVER,
                                   command=lambda selected=group: self._apply_group(selected))
            button.grid(row=item_row + 1, column=0, sticky="e", pady=(6, 0))
            self._buttons[group] = button

        self._reset_button = ctk.CTkButton(self, text="Reset all tuning to factory defaults",
                                          state="disabled", fg_color=theme.DANGER,
                                          hover_color=theme.DANGER_HOVER, command=self._reset)
        self._reset_button.grid(row=5, column=0, sticky="ew")

    def on_show(self) -> None:
        self._refresh()

    def _notice(self, text: str, color=theme.TEXT_DIM) -> None:
        self._status.configure(text=text, text_color=color)

    def _disable(self) -> None:
        self._supported.clear()
        self._timing_options.clear()
        for slider in self._sliders.values():
            slider.set_supported(False)
        self._timing_menu.configure(state="disabled", values=["Unavailable"])
        self._timing_menu.set("Unavailable")
        for button in self._buttons.values():
            button.configure(state="disabled")
        self._reset_button.configure(state="disabled")

    def _refresh(self) -> bool:
        self._disable()
        if not self.state.connected:
            self._notice("No GPU connection. Controls are unavailable.", theme.WARN)
            return False
        try:
            self._load_snapshot(self.state.bridge.tuning_get())
        except BridgeError as exc:
            self._notice(f"Read failed: {exc}. Reload values before making changes.", theme.DANGER)
            self.state.log(f"Tuning read failed: {exc}", "error")
            return False
        self._notice("Driver values loaded. Disabled controls are unavailable on this GPU.")
        return True

    def _load_snapshot(self, tuning: dict) -> None:
        self._disable()
        self._snapshot = tuning
        self._applied.clear()
        for key, (group, field, range_key, _, _) in CONTROLS.items():
            section = tuning.get(group, {})
            limits = control_range(section, field, range_key, voltage=key == "voltage")
            if key == "tdc" and not section.get("tdcSupported"):
                limits = None
            slider = self._sliders[key]
            if key == "max_clock":
                offset = section.get("maxFreqRange", {}).get("min", 0) < 0
                slider._name.configure(text="Core clock offset" if offset else "Max clock")
            if limits is None:
                slider._value.configure(text="Unavailable")
                continue
            slider.configure_range(*limits)
            slider.set(section[field])
            slider.set_supported(True)
            self._applied[key] = section[field]
            self._supported.add(key)

        vram = tuning.get("vram", {})
        options = vram.get("timingOptions", [])
        current = vram.get("timing")
        if ("unsupported" not in vram and vram.get("timingSupported")
                and isinstance(options, list) and type(current) is int
                and current in options and all(type(value) is int for value in options)):
            self._timing_options = {MEMORY_TIMINGS.get(value, f"Timing {value}"): value for value in options}
            self._timing_menu.configure(values=list(self._timing_options), state="normal")
            self._timing_menu.set(next(name for name, value in self._timing_options.items() if value == current))
            self._applied["timing"] = current
            self._supported.add("timing")
        self._reset_button.configure(state="normal" if self.state.connected else "disabled")
        self._update_staged()

    def _pending(self, group: str) -> dict[str, int]:
        values = {key: self._sliders[key].get() for key in self._supported
                  if key in CONTROLS and CONTROLS[key][0] == group}
        if group == "vram" and "timing" in self._supported:
            values["timing"] = self._timing_options[self._timing_menu.get()]
        return {key: value for key, value in values.items() if value != self._applied[key]}

    def _update_staged(self) -> None:
        for group, status in self._group_status.items():
            current = []
            for key, value in self._applied.items():
                if key == "timing":
                    if group == "vram":
                        current.append(MEMORY_TIMINGS.get(value, f"Timing {value}"))
                elif CONTROLS[key][0] == group:
                    _, _, _, label, unit = CONTROLS[key]
                    if key == "max_clock" and self._snapshot.get("gfx", {}).get("maxFreqRange", {}).get("min", 0) < 0:
                        label = "Core clock offset"
                    current.append(f"{label}: {value} {unit}")
            pending = self._pending(group)
            text = "Applied: " + " · ".join(current) if current else "No supported controls reported by the driver."
            if pending:
                text += f"\n{len(pending)} staged change(s) — not applied"
            status.configure(text=text, text_color=theme.WARN if pending else theme.TEXT_DIM)
            enabled = bool(pending) and self.state.connected
            self._buttons[group].configure(state="normal" if enabled else "disabled",
                                           fg_color=theme.ACCENT if enabled else theme.SURFACE_3)

    def _can_write(self) -> bool:
        if not self.state.connected:
            self._notice("No GPU connection. No settings were written.", theme.DANGER)
            return False
        if any(getattr(self.state, attr, False) for attr in (
                "engine_running", "autotune_running", "governor_running", "appboost_active")):
            self._notice("Stop Dynamic Voltage, Auto-Tune, Adaptive and App Boost before manual tuning.", theme.WARN)
            return False
        return True

    @staticmethod
    def _read_value(snapshot: dict, key: str):
        if key == "timing":
            return snapshot.get("vram", {}).get("timing")
        group, field, *_ = CONTROLS[key]
        return snapshot.get(group, {}).get(field)

    def _apply_group(self, group: str) -> None:
        if not self._can_write():
            return
        pending = self._pending(group)
        if not pending:
            return
        try:
            # Reset or another utility may have changed settings since the read.
            fresh = self.state.bridge.tuning_get()
            tracked = [key for key in self._applied if
                       (key == "timing" and group == "vram") or
                       (key in CONTROLS and CONTROLS[key][0] == group)]
            if any(self._read_value(fresh, key) != self._applied[key] for key in tracked):
                self._load_snapshot(fresh)
                self._notice("Driver settings changed since the last read. Values reloaded; stage your changes again.", theme.WARN)
                return
            if group == "gfx":
                minimum = pending.get("min_clock", fresh.get("gfx", {}).get("minFreqMhz"))
                maximum = pending.get("max_clock", fresh.get("gfx", {}).get("maxFreqMhz"))
                signed_offset = fresh.get("gfx", {}).get("maxFreqRange", {}).get("min", 0) < 0
                if not signed_offset and minimum is not None and maximum is not None and minimum > maximum:
                    self._notice("Minimum core clock must not exceed maximum core clock. No settings were written.", theme.WARN)
                    return
                if "voltage" in pending:
                    self.state.bridge.set_voltage_offset(pending["voltage"])
                if "min_clock" in pending or "max_clock" in pending:
                    self.state.bridge.set_core_clocks(pending.get("min_clock"), pending.get("max_clock"))
            elif group == "vram":
                if "vram_clock" in pending:
                    self.state.bridge.set_vram_max(pending["vram_clock"])
                if "timing" in pending:
                    self.state.bridge.set_memory_timing(pending["timing"])
            elif group == "power":
                if "power_limit" in pending:
                    self.state.bridge.set_power_limit(pending["power_limit"])
                if "tdc" in pending:
                    self.state.bridge.set_tdc(pending["tdc"])
            readback = self.state.bridge.tuning_get()
            mismatched = [key for key, value in pending.items() if self._read_value(readback, key) != value]
            self._load_snapshot(readback)
            if mismatched:
                message = "Driver readback differs from the requested settings. Displaying the values actually reported."
                self._notice(message, theme.WARN)
                self.state.log(message, "warn")
            else:
                message = f"Applied {len(pending)} change(s); confirmed by driver readback. Stability has not been tested."
                self._notice(message, theme.GOOD)
                self.state.log(message, "volt")
        except BridgeError as exc:
            self._refresh()
            message = f"Apply failed: {exc}. Earlier writes may have succeeded; inspect the applied values before retrying."
            self._notice(message, theme.DANGER)
            self.state.log(message, "error")

    def _reset(self) -> None:
        if not self._can_write():
            return
        try:
            self.state.bridge.tuning_reset()
            if self._refresh():
                if self._snapshot.get("atFactory") is True:
                    self._notice("Factory tuning restored and confirmed by the driver.", theme.GOOD)
                    self.state.log("Factory tuning restored", "volt")
                else:
                    self._notice("Reset requested, but the driver did not confirm factory state. Inspect applied values.", theme.WARN)
        except BridgeError as exc:
            self._notice(f"Reset failed: {exc}", theme.DANGER)
            self.state.log(f"Reset failed: {exc}", "error")
