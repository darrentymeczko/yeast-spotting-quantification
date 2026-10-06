"""Home: the task manager every piece of work starts from.

Four cards across the top, one per step of the work -- design the plate,
set up and run the experiment, check its photos, review the results. The
selected card's actions are listed underneath, beside what was opened (or
changed) most recently of that kind. In the spirit of Gen5's Task Manager:
pick what you are doing on the left, then how to start it.
"""

from __future__ import annotations

import tkinter as tk
from datetime import datetime
from tkinter import messagebox, ttk
from typing import TYPE_CHECKING

from uikit import icons
from uikit import tokens as t
from uikit.dpi import px
from uikit.theme import FONT_DISPLAY, FONT_HEADING, FONT_SMALL, FONT_STRONG
from uikit.widgets import IconButton, LinkLabel

from .registry import BY_KEY, KINDS

if TYPE_CHECKING:                                  # pragma: no cover
    from .shell import Shell

#: How many recent files a card lists.
RECENT_SHOWN = 8

EXPLAINER = """\
A PLATE TEMPLATE is the shape of the assay: how big the grid is, which cell \
holds which sample slot, replicate and dilution, and which slot is the control \
on each plate. It names no strains, so one template serves every experiment \
spotted the same way.

An EXPERIMENT is one particular run of that assay: which strain was in each \
slot, what it was grown on, and where its photographs are. It is also where \
the photographs are measured -- Run quantification, on its last tab.

REVIEW DATA is looking at those photographs before trusting any statistics: \
flag a whole plate, or click single spots, that are not good data. The \
additional multi-step analysis leaves them out; Review results marks them.

REVIEW RESULTS then opens what a run produced: every candidate the time course \
scored, so you can keep the pipeline's pick or choose another, and correct \
individual spots."""


def relative_time(when: datetime | None, now: datetime | None = None) -> str:
    if when is None:
        return ""
    now = now or datetime.now()
    seconds = (now - when).total_seconds()
    if seconds < 60:
        return "just now"
    minutes = seconds / 60
    if minutes < 60:
        n = int(minutes)
        return f"{n} minute{'s' if n != 1 else ''} ago"
    hours = minutes / 60
    if hours < 24 and now.date() == when.date():
        n = int(hours)
        return f"{n} hour{'s' if n != 1 else ''} ago"
    days = (now.date() - when.date()).days
    if days <= 1:
        return "yesterday"
    if days < 7:
        return f"{days} days ago"
    if days < 35:
        n = days // 7
        return f"{n} week{'s' if n != 1 else ''} ago"
    return when.strftime("%d %b %Y")


class _Card(tk.Frame):
    """One step of the work, selectable."""

    def __init__(self, master, home: "HomePage", kind) -> None:
        super().__init__(master, background=t.SURFACE, cursor="hand2",
                         highlightthickness=2, highlightbackground=t.LINE,
                         highlightcolor=t.LINE)
        self.home = home
        self.kind = kind
        self.selected = False
        pad = px(self, 16)

        glyph, font = icons.get(self, kind.icon, 18)
        self.icon = tk.Label(self, text=glyph, font=font, foreground=t.ACCENT)
        self.icon.grid(row=0, column=0, rowspan=2, sticky="nw",
                       padx=(pad, px(self, 12)), pady=(pad, 0))
        self.step = tk.Label(self, text=f"STEP {kind.step}", font=FONT_SMALL,
                             foreground=t.TEXT_MUTED)
        self.step.grid(row=0, column=1, sticky="w", pady=(pad, 0), padx=(0, pad))
        self.title = tk.Label(self, text=kind.title, font=FONT_HEADING,
                              foreground=t.TEXT)
        self.title.grid(row=1, column=1, sticky="w", padx=(0, pad))
        self.blurb = tk.Label(self, text=kind.blurb, foreground=t.TEXT_MUTED,
                              justify="left", anchor="nw",
                              wraplength=px(self, 220))
        self.blurb.grid(row=2, column=0, columnspan=2, sticky="nw",
                        padx=pad, pady=(px(self, 8), pad))
        self.columnconfigure(1, weight=1)
        self.rowconfigure(2, weight=1)

        for widget in (self, self.icon, self.step, self.title, self.blurb):
            widget.bind("<Button-1>", lambda _e: home.select(kind.key))
            widget.bind("<Double-Button-1>", lambda _e: home.primary(kind.key))
            widget.bind("<Enter>", lambda _e: self._hover(True))
            widget.bind("<Leave>", lambda _e: self._hover(False))
        self.paint()

    def _hover(self, on: bool) -> None:
        if not self.selected:
            self.configure(highlightbackground=t.LINE_STRONG if on else t.LINE,
                           highlightcolor=t.LINE_STRONG if on else t.LINE)

    def paint(self) -> None:
        bg = t.ACCENT_SOFT if self.selected else t.SURFACE
        edge = t.ACCENT if self.selected else t.LINE
        self.configure(background=bg, highlightbackground=edge,
                       highlightcolor=edge)
        for widget in (self.icon, self.step, self.title, self.blurb):
            widget.configure(background=bg)

    def set_width(self, width: int) -> None:
        self.blurb.configure(wraplength=max(px(self, 140), width - px(self, 40)))


class HomePage(ttk.Frame):
    def __init__(self, master, shell: "Shell") -> None:
        # Only a placed child lives here, so there is no natural requested
        # size. Tk can leave a 0-size page unmapped on the first grid restore;
        # a minimal request lets the parent's weighted grid stretch/map it.
        super().__init__(master, width=2, height=2)
        self.shell = shell
        self.current = shell.settings.get("home_card", "experiment")
        if self.current not in BY_KEY:
            self.current = "experiment"

        # A centred column of readable width, however wide the window is.
        self.column = ttk.Frame(self, padding=(px(self, 32), px(self, 28)))
        self.column.place(relx=0.5, y=0, anchor="n")
        self.bind("<Configure>", self._resize)

        ttk.Label(self.column, text="Spotting Quantification",
                  font=FONT_DISPLAY).pack(anchor="w")
        ttk.Label(self.column, style="Muted.TLabel",
                  text="Design a plate, describe the experiment and measure "
                       "it, then review what it measured.").pack(
            anchor="w", pady=(px(self, 2), px(self, 20)))

        cards = ttk.Frame(self.column)
        cards.pack(fill="x")
        self.cards: dict[str, _Card] = {}
        for i, kind in enumerate(KINDS):
            card = _Card(cards, self, kind)
            card.grid(row=0, column=i, sticky="nsew",
                      padx=(0 if i == 0 else px(self, 12), 0))
            cards.columnconfigure(i, weight=1, uniform="card")
            self.cards[kind.key] = card

        ttk.Separator(self.column).pack(fill="x", pady=px(self, 20))

        lower = ttk.Frame(self.column)
        lower.pack(fill="both", expand=True)
        lower.columnconfigure(0, weight=1, uniform="lower")
        lower.columnconfigure(1, weight=1, uniform="lower")

        self.actions = ttk.Frame(lower)
        self.actions.grid(row=0, column=0, sticky="nsew", padx=(0, px(self, 24)))
        self.recent = ttk.Frame(lower)
        self.recent.grid(row=0, column=1, sticky="nsew")

        ttk.Separator(self.column).pack(fill="x", pady=(px(self, 20),
                                                        px(self, 12)))
        foot = ttk.Frame(self.column)
        foot.pack(fill="x")
        LinkLabel(foot, "Template, experiment, review: what is the difference?",
                  self._explain).pack(side="left")
        self.show_home = tk.BooleanVar(
            value=bool(shell.settings.get("show_home", True)))
        ttk.Checkbutton(foot, text="Show Home at startup", variable=self.show_home,
                        command=self._show_home_changed).pack(side="right")

        tools = ttk.Frame(self.column)
        tools.pack(fill="x", pady=(px(self, 8), 0))
        for text, command in (("Open the project folder", shell.open_project_folder),
                              ("Check the installation", shell.run_selftest),
                              ("Read the manual", shell.open_readme)):
            LinkLabel(tools, text, command, foreground=t.TEXT_MUTED).pack(
                side="left", padx=(0, px(self, 18)))

        self.select(self.current)

    # -- layout ------------------------------------------------------------------

    def _resize(self, event) -> None:
        width = min(event.width, px(self, 1180))
        self.column.place_configure(width=width)
        n = max(1, len(self.cards))
        card_w = (width - px(self, 64) - (n - 1) * px(self, 12)) // n
        for card in self.cards.values():
            card.set_width(card_w)

    # -- the selected step -----------------------------------------------------------

    def select(self, key: str) -> None:
        self.current = key
        self.shell.settings.set("home_card", key)
        for k, card in self.cards.items():
            card.selected = k == key
            card.paint()
        self._fill_actions()
        self._fill_recent()

    def refresh(self) -> None:
        """Called whenever Home comes to the front: recents move on."""
        self._fill_actions()
        self._fill_recent()

    def primary(self, key: str) -> None:
        """Double-clicking a card: its first action."""
        self.select(key)
        actions = self._actions(key)
        if actions:
            actions[0][2]()

    def _actions(self, key: str) -> list[tuple[str, str, object]]:
        shell = self.shell
        if key == "plate":
            return [
                ("add", "New blank template", lambda: shell.open_document("plate")),
                ("grid", "New from a preset", self._presets),
                ("open", "Open a template…", lambda: shell.ask_open("plate")),
            ]
        if key == "experiment":
            return [
                ("add", "New experiment", lambda: shell.open_document("experiment")),
                ("copy", "Create several from photo folders…",
                 shell.bulk_create_experiments),
                ("import", "Import an existing configuration…",
                 shell.import_configuration),
                ("open", "Open an experiment…", lambda: shell.ask_open("experiment")),
            ]
        if key == "data-review":
            return [
                ("flag", "Review an experiment's photos…",
                 lambda: shell.ask_open("data-review")),
            ]
        actions = []
        last = shell.last_review_set()
        if last is not None:
            actions.append(("chart", f"Carry on with {last.name}",
                            lambda: shell.open_document("review", last)))
        actions += [
            ("open", "Open a result set…", lambda: shell.ask_open("review")),
            ("refresh", "Re-export every reviewed set", shell.reexport_reviews),
        ]
        return actions

    def _fill_actions(self) -> None:
        for child in self.actions.winfo_children():
            child.destroy()
        kind = BY_KEY[self.current]
        ttk.Label(self.actions, text=kind.title, font=FONT_STRONG).pack(
            anchor="w", pady=(0, px(self, 6)))
        for icon, text, command in self._actions(self.current):
            IconButton(self.actions, icon, text, command, background=t.BG,
                       foreground=t.ACCENT, hover=t.SURFACE_ALT,
                       anchor="w", pad=6).pack(anchor="w", fill="x")

    def _fill_recent(self) -> None:
        for child in self.recent.winfo_children():
            child.destroy()
        ttk.Label(self.recent, text="Recent", font=FONT_STRONG).pack(
            anchor="w", pady=(0, px(self, 6)))
        rows = self.shell.recent_files(self.current, RECENT_SHOWN)
        if not rows:
            ttk.Label(self.recent, style="Muted.TLabel",
                      text="Nothing here yet.").pack(anchor="w")
            return
        for path, when in rows:
            row = ttk.Frame(self.recent)
            row.pack(fill="x", pady=px(self, 1))
            LinkLabel(row, self.shell.display_name(self.current, path),
                      lambda p=path: self.shell.open_document(self.current, p),
                      foreground=t.TEXT).pack(side="left")
            ttk.Label(row, text=relative_time(when), style="Muted.TLabel").pack(
                side="right")

    # -- actions that need a little UI of their own ----------------------------------

    def _presets(self) -> None:
        from plate_template.presets import list_presets

        menu = tk.Menu(self, tearoff=False)
        for _key, label, factory in list_presets():
            menu.add_command(label=label, command=lambda f=factory:
                             self.shell.open_document("plate", None, f()))
        x, y = self.winfo_pointerxy()
        menu.tk_popup(x, y)

    def _explain(self) -> None:
        messagebox.showinfo("Template, experiment, review", EXPLAINER,
                            parent=self.winfo_toplevel())

    def _show_home_changed(self) -> None:
        self.shell.settings.set("show_home", bool(self.show_home.get()))
        self.shell.settings.save()
