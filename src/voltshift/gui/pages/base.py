"""Base class for AMD GPU Tuner GUI pages."""

from __future__ import annotations

import customtkinter as ctk

from .. import theme
from ..state import AppState


class Page(ctk.CTkScrollableFrame):
    """A scrollable page hosted in the app's content area.

    Subclasses set `title` and implement `build()`. `on_show()` runs each
    time the page becomes visible (refresh live state there); `on_hide()`
    runs when navigating away.
    """

    title = "Page"

    def __init__(self, master, state: AppState):
        super().__init__(master, fg_color="transparent")
        self.state = state
        self.grid_columnconfigure(0, weight=1)
        self._built = False

    def ensure_built(self) -> None:
        if not self._built:
            self.build()
            self._built = True

    def build(self) -> None:  # pragma: no cover - UI
        raise NotImplementedError

    def on_show(self) -> None:
        pass

    def on_hide(self) -> None:
        pass

    # Small helper: a disabled-looking notice when a feature is unavailable.
    def unavailable(self, parent, text: str) -> ctk.CTkLabel:
        return ctk.CTkLabel(parent, text=text, font=(theme.FONT, 12),
                            text_color=theme.TEXT_FAINT, anchor="w", justify="left")
