"""AMD GPU Tuner GUI application shell.

Left sidebar navigates between pages; the top bar shows the GPU name and a
compact always-on metrics strip; the content area hosts one page at a time.
A lightweight metrics poll runs whenever the dynamic voltage engine is not
already polling, so the top bar and Dashboard stay live either way.
"""

from __future__ import annotations

import customtkinter as ctk

from .. import APP_NAME, __version__
from ..bridgeclient import BridgeError
from . import theme
from .state import AppState
from .pages.dashboard import DashboardPage
from .pages.autotune import AutoTunePage
from .pages.adaptive import AdaptivePage
from .pages.voltage import VoltagePage
from .pages.tuning import TuningPage
from .pages.fans import FansPage
from .pages.graphics import GraphicsPage
from .pages.display import DisplayPage
from .pages.appboost import AppBoostPage
from .pages.profiles import ProfilesPage
from .pages.logs import LogsPage
from .pages.about import AboutPage

ctk.set_appearance_mode("dark")

NAV = [
    ("Dashboard", DashboardPage),
    ("Auto-Tune", AutoTunePage),
    ("Adaptive", AdaptivePage),
    ("Dynamic Voltage", VoltagePage),
    ("Tuning", TuningPage),
    ("Fans", FansPage),
    ("Graphics", GraphicsPage),
    ("Display", DisplayPage),
    ("App Boost", AppBoostPage),
    ("Profiles", ProfilesPage),
    ("Logs", LogsPage),
    ("About", AboutPage),
]

# How often to poll metrics for the top bar / dashboard when the engine is
# idle. The engine, when running, feeds samples itself.
IDLE_POLL_MS = 700


class AMDGPUTunerApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("1180x760")
        self.minsize(1000, 660)
        self.configure(fg_color=theme.BG)

        self.state_mgr = AppState(self)
        self.attributes("-topmost", self.state_mgr.always_on_top)
        self._pages: dict[str, object] = {}
        self._nav_buttons: dict[str, ctk.CTkButton] = {}
        self._current: str | None = None
        self._closing = False
        self._poll_job = None

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(1, weight=1)

        self._build_sidebar()
        self._build_topbar()
        self._build_content()

        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # Route engine samples into the top bar too.
        self.state_mgr.sample_sinks.append(self._update_topbar_metrics)

        self.after(100, self._startup)

    # ── layout ───────────────────────────────────────────────────────────────

    def _build_sidebar(self) -> None:
        sidebar = ctk.CTkFrame(self, fg_color=theme.SURFACE, corner_radius=0, width=210)
        sidebar.grid(row=0, column=0, rowspan=2, sticky="nsew")
        sidebar.grid_propagate(False)

        brand = ctk.CTkFrame(sidebar, fg_color="transparent")
        brand.pack(fill="x", padx=18, pady=(20, 24))
        ctk.CTkLabel(brand, text=APP_NAME, font=(theme.FONT, 18, "bold"),
                     text_color=theme.TEXT).pack(anchor="w")
        ctk.CTkLabel(brand, text=f"RADEON CONTROL  /  v{__version__}", font=(theme.FONT, 10),
                     text_color=theme.TEXT_FAINT).pack(anchor="w")

        for name, _cls in NAV:
            btn = ctk.CTkButton(
                sidebar, text=name, anchor="w", height=38, corner_radius=8,
                fg_color="transparent", hover_color=theme.SURFACE_3,
                text_color=theme.TEXT_DIM, font=(theme.FONT, 13),
                command=lambda n=name: self.show_page(n))
            btn.pack(fill="x", padx=12, pady=2)
            self._nav_buttons[name] = btn

        self._conn_label = ctk.CTkLabel(sidebar, text="● connecting…",
                                        font=(theme.FONT, 11), text_color=theme.WARN)
        self._conn_label.pack(side="bottom", anchor="w", padx=18, pady=16)
        self._topmost_var = ctk.BooleanVar(value=self.state_mgr.always_on_top)
        ctk.CTkSwitch(sidebar, text="Always on top", variable=self._topmost_var,
                       command=self._toggle_topmost, font=(theme.FONT, 11),
                       progress_color=theme.ACCENT).pack(side="bottom", anchor="w", padx=18, pady=8)

    def _toggle_topmost(self) -> None:
        self.state_mgr.always_on_top = bool(self._topmost_var.get())
        self.attributes("-topmost", self.state_mgr.always_on_top)

    def _build_topbar(self) -> None:
        bar = ctk.CTkFrame(self, fg_color=theme.BG, corner_radius=0, height=64)
        bar.grid(row=0, column=1, sticky="ew", padx=20, pady=(14, 0))
        bar.grid_propagate(False)

        self._gpu_label = ctk.CTkLabel(bar, text="Detecting GPU…",
                                       font=(theme.FONT, 16, "bold"), text_color=theme.TEXT)
        self._gpu_label.pack(side="left", anchor="w")

        strip = ctk.CTkFrame(bar, fg_color="transparent")
        strip.pack(side="right", anchor="e")
        self._mini = {}
        for key, label in (("clockMhz", "MHz"), ("tempC", "°C"),
                           ("boardPowerW", "W"), ("fanRpm", "RPM")):
            cell = ctk.CTkFrame(strip, fg_color=theme.SURFACE, corner_radius=8)
            cell.pack(side="left", padx=4)
            value = ctk.CTkLabel(cell, text="—", font=(theme.FONT_MONO, 15, "bold"),
                                 text_color=theme.ACCENT)
            value.pack(padx=12, pady=(6, 0))
            ctk.CTkLabel(cell, text=label, font=(theme.FONT, 9, "bold"),
                         text_color=theme.TEXT_DIM).pack(padx=12, pady=(0, 6))
            self._mini[key] = value

    def _build_content(self) -> None:
        self._content = ctk.CTkFrame(self, fg_color="transparent")
        self._content.grid(row=1, column=1, sticky="nsew", padx=20, pady=16)
        self._content.grid_rowconfigure(0, weight=1)
        self._content.grid_columnconfigure(0, weight=1)

    # ── navigation ───────────────────────────────────────────────────────────

    def show_page(self, name: str) -> None:
        if name == self._current:
            return
        if self._current and self._current in self._pages:
            self._pages[self._current].on_hide()
            self._pages[self._current].grid_forget()
            self._nav_buttons[self._current].configure(
                fg_color="transparent", text_color=theme.TEXT_DIM)

        page = self._pages.get(name)
        if page is None:
            cls = dict(NAV)[name]
            page = cls(self._content, self.state_mgr)
            self._pages[name] = page
        page.ensure_built()
        page.grid(row=0, column=0, sticky="nsew")
        page.on_show()
        self._nav_buttons[name].configure(fg_color=theme.SURFACE_3, text_color=theme.TEXT)
        self._current = name

    # ── startup + polling ────────────────────────────────────────────────────

    def _startup(self) -> None:
        if self.state_mgr.connect():
            self._gpu_label.configure(text=self.state_mgr.gpu_name())
            self._conn_label.configure(text="● connected", text_color=theme.GOOD)
            self.state_mgr.log(f"Connected: {self.state_mgr.gpu_name()} "
                               f"(bridge v{self.state_mgr.bridge.version})")
        else:
            self._gpu_label.configure(text="GPU not detected")
            self._conn_label.configure(text="● bridge error", text_color=theme.DANGER)
            self.state_mgr.log(f"Bridge failed: {self.state_mgr.connect_error}", "error")

        self.show_page("Dashboard")
        self._poll_idle()

    def _poll_idle(self) -> None:
        if self._closing:
            return
        # The telemetry hub, when running, is the single poller for the whole
        # app and already streams samples to every sink. This fallback only
        # covers the case where the hub could not be built.
        hub = self.state_mgr.stack.hub if self.state_mgr.stack else None
        hub_live = hub is not None and hub.running
        if self.state_mgr.connected and not hub_live and not self.state_mgr.engine_running:
            try:
                sample = self.state_mgr.bridge.metrics()
                for sink in self.state_mgr.sample_sinks:
                    sink(sample)
            except BridgeError:
                pass
        self._poll_job = self.after(IDLE_POLL_MS, self._poll_idle)

    def _update_topbar_metrics(self, sample: dict) -> None:
        if self._closing:
            return
        for key, widget in self._mini.items():
            value = sample.get(key)
            if key == "boardPowerW" and value is None:
                value = sample.get("powerW")
            if value is None:
                widget.configure(text="—")
            elif isinstance(value, float):
                widget.configure(text=f"{value:.0f}")
            else:
                widget.configure(text=str(value))

    def _on_close(self) -> None:
        # Stop the idle poll and mark closing so in-flight `after` callbacks
        # bail before touching destroyed widgets.
        self._closing = True
        if self._poll_job is not None:
            try:
                self.after_cancel(self._poll_job)
            except Exception:
                pass
        try:
            self.state_mgr.save_settings()
        except Exception:
            pass
        self.state_mgr.shutdown()
        # Exit the event loop before destroying widgets and scheduled jobs.
        self.quit()


def run() -> None:
    app = AMDGPUTunerApp()
    try:
        app.mainloop()
    finally:
        if not app._closing:
            app.state_mgr.shutdown()
        app.destroy()
