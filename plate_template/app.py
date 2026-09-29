"""Plate Template Designer -- tkinter entry point.

    py -m plate_template.app [template.json] [--preset lab8x6]

Opens a blank grid by default; presets are something you choose, not something
you have to undo.
"""

from __future__ import annotations

import argparse
import sys
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, font as tkfont, messagebox, simpledialog, ttk

from . import theme
from .autofill import (
    Plan,
    RunFillSpec,
    SeriesFillSpec,
    plan_duplicate_plate,
    plan_paint,
    plan_run_fill,
    plan_series_fill,
)
from .gui.controller import EditorController
from .gui.grid_canvas import GridCanvas
from .gui.panels import InspectorBar, StatusBar, ValidationPanel
from .gui.toolbar import TOOL_HINTS, ContextBar, Toolbar
from .model import Cell, CellKind, Template
from .presets import blank, list_presets
from .schema import TemplateError, load, save
from .validate import Issue, Severity, validate

APP_TITLE = "Plate Template Designer"

EMPTY_HINT = (
    "Empty grid. Pick the dot tool on the toolbar, "
    "then drag across a row of cells."
)

QUICK_START = """\
A template describes the SHAPE of your assay, not which strains are on it.
The same template can be reused for any experiment that follows the protocol.

Hover any toolbar button to see what it does.

  1. Grid size  [#]  sets how many rows and columns the plate has.

  2. Place samples  [*]  -- drag across a row of cells. The first cell gets
     the "First slot" number, the next gets the one after it, and so on.
     A backwards drag numbers backwards, which is how you enter a mirrored
     panel. What you see outlined while dragging is exactly what gets written.

  3. Add dilutions  [v]  -- drag outwards from a spot you already placed, to
     where the last dilution should land. The drag gives both the direction and
     the number of levels, and the whole run of matching spots beside it comes
     along, so one drag dilutes a full row. Spacing sets the gap between levels.

  4. Set control  [o]  -- click one spot. Its sample slot becomes this plate's
     positive control. Every plate needs one, because each sample is
     relativised to a control on its own plate.

  5. Add plate  [+]  for a second physical plate, or Duplicate  [=]  to copy
     this layout with the replicate numbers shifted up. Left and Right arrow
     keys switch plates; right-click a plate tab to rename, duplicate or
     delete it.

Anything wrong or suspicious appears in the Findings list at the bottom.
Errors mean the design cannot be analysed; warnings are worth a look.

Double-click any cell to edit it exactly. "Mark empty" says a cell is
deliberately blank; "Erase" puts it back to undecided.

The  [T]  button replaces the drawing with the literal text that gets saved,
which is the quickest way to check a plate against the bench.

The bar under the toolbar shows only the settings the current tool reads, and
the line under the grid tells you what that tool does.
"""


def _enable_dpi_awareness() -> None:
    """Without this tkinter is blurry on high-DPI Windows and the digits in
    each spot become unreadable. Must run before the Tk root is created."""
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # type: ignore[attr-defined]
    except Exception:
        pass


def default_template_dir() -> Path:
    # Packaged as the single .exe, this package is unpacked to a temporary
    # folder, so the templates folder is the one beside the .exe instead.
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "Plate Templates"
    return Path(__file__).resolve().parents[1] / "Plate Templates"


class DesignerApp:
    def __init__(
        self, root: tk.Tk, template: Template, path: Path | None = None
    ) -> None:
        self.root = root
        self.path = path
        self.show_tokens = tk.BooleanVar(value=False)
        self.tool = tk.StringVar(value="assign")
        self.canvases: dict[str, GridCanvas] = {}
        self.controller = EditorController(template, on_change=self.refresh)

        root.title(APP_TITLE)
        self._size_window()
        self._build_menu()
        self._build_body()
        # Changing replicate or dilution re-aims the slot counter at whatever
        # is unused there, so the two spinboxes stay in step without typing.
        for variable in (self.context.replicate, self.context.dilution):
            variable.trace_add("write", lambda *_: self._sync_first_slot())
        self.refresh()
        root.protocol("WM_DELETE_WINDOW", self._on_close)

    # -- construction --------------------------------------------------------

    def _size_window(self) -> None:
        """Size from the screen, not a fixed pixel count.

        The window is DPI-aware, so its geometry is in physical pixels, while
        ttk widgets and fonts scale with the display. A hardcoded size that
        looks right at 100% leaves the bottom bars with nowhere to go at 200%.
        """
        root = self.root
        root.update_idletasks()
        screen_w, screen_h = root.winfo_screenwidth(), root.winfo_screenheight()
        width = max(900, min(1700, int(screen_w * 0.70)))
        height = max(600, min(1100, int(screen_h * 0.80)))
        root.geometry(
            f"{width}x{height}+{max(0, (screen_w - width) // 2)}"
            f"+{max(0, (screen_h - height) // 3)}"
        )
        line = root.winfo_fpixels("1i") / 96 * 22
        root.minsize(int(line * 26), int(line * 16))

    def _build_menu(self) -> None:
        menubar = tk.Menu(self.root)

        file_menu = tk.Menu(menubar, tearoff=False)
        file_menu.add_command(
            label="New blank template", accelerator="Ctrl+N", command=self.new_blank
        )
        preset_menu = tk.Menu(file_menu, tearoff=False)
        for key, label, factory in list_presets():
            preset_menu.add_command(
                label=label, command=lambda f=factory: self._load(f(), None)
            )
        file_menu.add_cascade(label="New from preset", menu=preset_menu)
        file_menu.add_separator()
        file_menu.add_command(
            label="Open...", accelerator="Ctrl+O", command=self.open_file
        )
        file_menu.add_command(label="Save", accelerator="Ctrl+S", command=self.save_file)
        file_menu.add_command(label="Save As...", command=self.save_file_as)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self._on_close)
        menubar.add_cascade(label="File", menu=file_menu)

        edit_menu = tk.Menu(menubar, tearoff=False)
        edit_menu.add_command(label="Undo", accelerator="Ctrl+Z", command=self.undo)
        edit_menu.add_command(label="Redo", accelerator="Ctrl+Y", command=self.redo)
        edit_menu.add_separator()
        edit_menu.add_command(label="Rename template...", command=self.rename_template)
        menubar.add_cascade(label="Edit", menu=edit_menu)
        self.edit_menu = edit_menu

        view_menu = tk.Menu(menubar, tearoff=False)
        view_menu.add_checkbutton(
            label="Show tokens", accelerator="Ctrl+T",
            variable=self.show_tokens, command=self._apply_token_view,
        )
        menubar.add_cascade(label="View", menu=view_menu)

        help_menu = tk.Menu(menubar, tearoff=False)
        help_menu.add_command(label="Quick start", command=self.quick_start)
        help_menu.add_command(label="About", command=self._about)
        menubar.add_cascade(label="Help", menu=help_menu)

        self.root.config(menu=menubar)

        # Keys bound to a Canvas never fire unless it holds focus, so bind on
        # the toplevel and ignore the event while a text field has focus.
        for sequence, action in (
            ("<Control-n>", self.new_blank),
            ("<Control-o>", self.open_file),
            ("<Control-s>", self.save_file),
            ("<Control-t>", self._toggle_tokens),
            ("<Control-z>", self.undo),
            ("<Control-y>", self.redo),
            ("<Control-Shift-Z>", self.redo),
            ("<Left>", lambda: self._step_plate(-1)),
            ("<Right>", lambda: self._step_plate(1)),
        ):
            self.root.bind_all(sequence, self._guarded(action))

    def _guarded(self, action):
        def handler(_event=None):
            widget = self.root.focus_get()
            if isinstance(widget, (tk.Entry, ttk.Entry, ttk.Spinbox, ttk.Combobox)):
                return None
            action()
            return "break"

        return handler

    def _build_body(self) -> None:
        outer = ttk.Frame(self.root)
        outer.pack(fill="both", expand=True)

        self.status = StatusBar(outer)
        self.status.pack(side="bottom", fill="x")
        self.inspector = InspectorBar(outer)
        self.inspector.pack(side="bottom", fill="x")

        self.toolbar = Toolbar(
            outer,
            tool=self.tool,
            show_tokens=self.show_tokens,
            on_tool_change=self._on_tool_change,
            on_undo=self.undo,
            on_redo=self.redo,
            on_tokens=self._apply_token_view,
            on_grid_size=self.resize_grid,
            on_add_plate=self.add_plate,
            on_duplicate_plate=self.duplicate_plate,
            on_delete_plate=self.delete_plate,
        )
        self.toolbar.pack(side="top", fill="x")
        ttk.Separator(outer, orient="horizontal").pack(side="top", fill="x")

        self.context = ContextBar(outer, self.tool)
        self.context.pack(side="top", fill="x")

        split = ttk.PanedWindow(outer, orient="vertical")
        split.pack(side="top", fill="both", expand=True)

        top = ttk.Frame(split)
        base = tkfont.nametofont("TkDefaultFont")
        ttk.Style().configure(
            "TNotebook.Tab",
            padding=(20, 10),
            font=(base.cget("family"), base.cget("size") + 1, "bold"),
        )
        self.notebook = ttk.Notebook(top)
        self.notebook.pack(fill="both", expand=True)
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)
        self.notebook.bind("<Button-3>", self._on_tab_menu)
        split.add(top, weight=5)

        self.validation = ValidationPanel(split, on_select=self._on_issue_selected)
        split.add(self.validation, weight=1)

    # -- template lifecycle --------------------------------------------------

    def _load(self, template: Template, path: Path | None) -> None:
        if not self._confirm_discard():
            return
        self.controller.replace(template)
        self.path = path
        self.refresh()

    @property
    def template(self) -> Template:
        return self.controller.template

    def _sync_tabs(self) -> None:
        """Rebuild the plate tabs when the set of plates changes."""
        wanted = [p.id for p in self.template.plates]
        if wanted == list(self.canvases):
            return
        selected = self.current_plate_id()
        for tab in self.notebook.tabs():
            frame = self.notebook.nametowidget(tab)
            self.notebook.forget(tab)
            frame.destroy()  # forget() only unparents it; without this they pile up
        self.canvases.clear()

        for plate in self.template.plates:
            frame = ttk.Frame(self.notebook)
            canvas = GridCanvas(
                frame, self.controller, plate.id,
                on_hover=lambda cell, pid=plate.id: self.inspector.show(
                    self.template, pid, cell
                ),
            )
            canvas.show_tokens = self.show_tokens.get()
            canvas.empty_hint = EMPTY_HINT
            canvas.plan_provider = (
                lambda a, b, pid=plate.id: self._plan_for_drag(pid, a, b)
            )
            canvas.commit_handler = lambda plan, pid=plate.id: self._commit(pid, plan)
            canvas.edit_handler = lambda cell, pid=plate.id: self.edit_cell(pid, cell)
            canvas.pack(fill="both", expand=True)
            self.canvases[plate.id] = canvas
            self.notebook.add(frame, text=plate.label or f"Plate {plate.id}")

        if selected in self.canvases:
            self.select_plate(selected)
        self._on_tool_change()

    def current_plate_id(self) -> str | None:
        current = self.notebook.select()
        for plate_id, canvas in self.canvases.items():
            if str(canvas.master) == current:
                return plate_id
        return next(iter(self.canvases), None)

    def _step_plate(self, delta: int) -> None:
        """Left/Right arrows walk the plate tabs, wrapping at either end."""
        tabs = self.notebook.tabs()
        if len(tabs) < 2:
            return
        current = self.notebook.select()
        index = tabs.index(current) if current in tabs else 0
        self.notebook.select((index + delta) % len(tabs))

    def _on_tab_menu(self, event: tk.Event) -> None:
        try:
            index = self.notebook.index(f"@{event.x},{event.y}")
        except tk.TclError:
            return  # right-clicked the strip, not a tab
        self.notebook.select(index)
        menu = tk.Menu(self.root, tearoff=False)
        menu.add_command(label="Rename plate...", command=self.rename_plate)
        menu.add_command(label="Duplicate this plate", command=self.duplicate_plate)
        menu.add_separator()
        menu.add_command(label="Delete plate", command=self.delete_plate)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def select_plate(self, plate_id: str) -> None:
        canvas = self.canvases.get(plate_id)
        if canvas is None:
            return
        for index, tab in enumerate(self.notebook.tabs()):
            if str(canvas.master) == tab:
                self.notebook.select(index)
                return

    # -- editing -------------------------------------------------------------

    def _on_tool_change(self) -> None:
        self.inspector.set_idle(TOOL_HINTS.get(self.tool.get(), ""))
        self.context.update_visibility()
        wants_click = self.tool.get() == "control"
        for canvas in self.canvases.values():
            canvas.click_handler = (
                (lambda cell, pid=canvas.plate_id: self._set_control(pid, cell))
                if wants_click
                else None
            )

    @staticmethod
    def _line(anchor, current) -> tuple[str, int]:
        """Snap a drag to its dominant axis -- never a diagonal."""
        dr, dc = current[0] - anchor[0], current[1] - anchor[1]
        if abs(dr) >= abs(dc):
            return ("down" if dr >= 0 else "up"), abs(dr) + 1
        return ("right" if dc >= 0 else "left"), abs(dc) + 1

    @staticmethod
    def _next_free_slot(template: Template, replicate: int, dilution: int) -> int:
        """Lowest sample slot not yet placed for this replicate and dilution.

        Scoped to both, so laying out a row of 8 leaves the counter on 9, while
        switching to the next replicate -- which uses the same samples again --
        drops it back to 1. Lowest-free rather than highest-plus-one so that
        re-filling an erased gap lands on the gap.
        """
        used = {
            p.sample_slot
            for _plate, _r, _c, p in template.all_placements()
            if p.replicate == replicate and p.dilution == dilution
        }
        slot = 1
        while slot in used and slot < 99:
            slot += 1
        return slot

    def _sync_first_slot(self) -> None:
        try:
            replicate = self.context.replicate.get()
            dilution = self.context.dilution.get()
        except (tk.TclError, ValueError):
            return  # a spinbox mid-edit can hold something unparseable
        self.context.first_slot.set(
            self._next_free_slot(self.template, replicate, dilution)
        )

    @staticmethod
    def _peer_run(plate, anchor, direction, source) -> list[tuple[int, int]]:
        """The run of like spots lying across the drag, through the anchor.

        Dragging down from one spot in a row of eight sources should dilute all
        eight, so the sources are found rather than clicked -- the contiguous
        neighbours perpendicular to the drag that carry the same replicate and
        dilution. The drag preview shows exactly which cells were picked up, so
        this stays visible rather than implicit.
        """
        step = (0, 1) if direction in ("down", "up") else (1, 0)

        def alike(rr: int, cc: int) -> bool:
            if not (0 <= rr < plate.rows and 0 <= cc < plate.cols):
                return False
            other = plate.get(rr, cc).placement
            return (
                other is not None
                and other.replicate == source.replicate
                and other.dilution == source.dilution
            )

        cells = [anchor]
        for sign in (1, -1):
            rr, cc = anchor[0] + step[0] * sign, anchor[1] + step[1] * sign
            while alike(rr, cc):
                cells.append((rr, cc))
                rr, cc = rr + step[0] * sign, cc + step[1] * sign
        return sorted(cells)

    @staticmethod
    def _rectangle(anchor, current) -> list[tuple[int, int]]:
        r0, r1 = sorted((anchor[0], current[0]))
        c0, c1 = sorted((anchor[1], current[1]))
        return [(r, c) for r in range(r0, r1 + 1) for c in range(c0, c1 + 1)]

    def _plan_for_drag(self, plate_id, anchor, current) -> Plan | None:
        tool = self.tool.get()
        try:
            if tool == "assign":
                direction, count = self._line(anchor, current)
                return plan_run_fill(
                    self.template, plate_id,
                    RunFillSpec(
                        start=anchor, direction=direction, count=count,
                        first_slot=self.context.first_slot.get(),
                        replicate=self.context.replicate.get(),
                        dilution=self.context.dilution.get(),
                    ),
                )
            if tool == "series":
                plate = self.template.plate(plate_id)
                source = plate.get(*anchor).placement
                if source is None:
                    return None
                direction, spanned = self._line(anchor, current)
                spacing = max(1, self.context.spacing.get())
                # The drag ends where the LAST dilution should land, so with a
                # spacing of 2 a five-cell drag means three levels, not five.
                levels = (spanned - 1) // spacing + 1
                if levels < 2:
                    return None
                return plan_series_fill(
                    self.template, plate_id,
                    SeriesFillSpec(
                        sources=self._peer_run(plate, anchor, direction, source),
                        direction=direction,
                        spacing=spacing,
                        levels=levels,
                    ),
                )
            if tool in ("empty", "erase"):
                kind = CellKind.EMPTY if tool == "empty" else CellKind.UNASSIGNED
                return plan_paint(
                    self.template, plate_id, self._rectangle(anchor, current), kind
                )
        except (tk.TclError, ValueError):
            # A spinbox mid-edit can hold something unparseable.
            return None
        return None

    def _commit(self, plate_id: str, plan: Plan) -> None:
        labels = {
            "assign": "Place samples",
            "series": "Add dilutions",
            "empty": "Mark empty",
            "erase": "Erase",
        }
        tool = self.tool.get()
        self.controller.apply(plate_id, plan, labels.get(tool, "Edit"))
        if tool == "assign":
            # Walk the counter on, so placing 1-8 leaves it ready on 9.
            self._sync_first_slot()
        if plan.issues:
            self.inspector.message(
                "  ".join(i.message for i in plan.issues[:2]),
                alarm=any(i.severity is Severity.ERROR for i in plan.issues),
            )

    def _set_control(self, plate_id: str, cell: tuple[int, int]) -> None:
        placement = self.template.plate(plate_id).get(*cell).placement
        if placement is None:
            self.root.bell()
            self.inspector.message(
                "Click a spot, not a blank cell -- the control is a sample slot.",
                alarm=True,
            )
            return
        self.controller.set_control(plate_id, placement.sample_slot)

    def edit_cell(self, plate_id: str, cell: tuple[int, int]) -> None:
        """The manual escape hatch: set one cell to exactly what you mean."""
        r, c = cell
        plate = self.template.plate(plate_id)
        existing = plate.get(r, c)
        dialog = CellDialog(self.root, self.template, existing)
        if dialog.result is None:
            return
        self.controller.apply(
            plate_id, Plan([(r, c, dialog.result)], []), "Edit cell"
        )

    # -- grid and plates -----------------------------------------------------

    def resize_grid(self) -> None:
        rows = simpledialog.askinteger(
            "Grid size", "Rows:", parent=self.root,
            initialvalue=self.template.rows, minvalue=1, maxvalue=40,
        )
        if rows is None:
            return
        cols = simpledialog.askinteger(
            "Grid size", "Columns:", parent=self.root,
            initialvalue=self.template.cols, minvalue=1, maxvalue=40,
        )
        if cols is None:
            return
        if (rows, cols) == (self.template.rows, self.template.cols):
            return
        lost = sum(
            1
            for plate in self.template.plates
            for r, c, _ in plate.placements()
            if r >= rows or c >= cols
        )
        if lost and not messagebox.askyesno(
            "Shrink grid",
            f"Shrinking to {rows}x{cols} will discard {lost} placed spot(s).\n\n"
            f"Continue?",
        ):
            return
        self.controller.resize(rows, cols)

    def _next_plate_id(self) -> str:
        used = {p.id for p in self.template.plates}
        n = 1
        while str(n) in used:
            n += 1
        return str(n)

    def add_plate(self) -> None:
        plate_id = self._next_plate_id()
        self.controller.add_plate(plate_id, f"Plate {plate_id}")
        self.select_plate(plate_id)

    def duplicate_plate(self) -> None:
        source = self.current_plate_id()
        if source is None:
            return
        used = len(self.template.plate(source).replicates_present()) or 1
        offset = simpledialog.askinteger(
            "Duplicate plate",
            "Add this much to every replicate number on the copy:",
            parent=self.root, initialvalue=used, minvalue=0, maxvalue=99,
        )
        if offset is None:
            return
        new_id = self._next_plate_id()
        self.controller.add_plate(new_id, f"Plate {new_id}")
        plan = plan_duplicate_plate(self.template, source, new_id, offset)
        self.controller.apply(new_id, plan, f"Duplicate plate {source}")
        self.select_plate(new_id)

    def delete_plate(self) -> None:
        plate_id = self.current_plate_id()
        if plate_id is None:
            return
        if len(self.template.plates) == 1:
            messagebox.showinfo("Delete plate", "A template needs at least one plate.")
            return
        if not messagebox.askyesno(
            "Delete plate", f"Delete plate {plate_id} and everything on it?"
        ):
            return
        self.controller.remove_plate(plate_id)

    def rename_template(self) -> None:
        name = simpledialog.askstring(
            "Rename template", "Template name:",
            parent=self.root, initialvalue=self.template.name,
        )
        if name:
            self.controller.set_name(name)

    def rename_plate(self) -> None:
        plate_id = self.current_plate_id()
        if plate_id is None:
            return
        label = simpledialog.askstring(
            "Rename plate", "Tab label:",
            parent=self.root, initialvalue=self.template.plate(plate_id).label,
        )
        if label:
            self.controller.rename_plate(plate_id, label)

    # -- history -------------------------------------------------------------

    def undo(self) -> None:
        if self.controller.undo() is None:
            self.root.bell()

    def redo(self) -> None:
        if self.controller.redo() is None:
            self.root.bell()

    # -- files ---------------------------------------------------------------

    def new_blank(self) -> None:
        self._load(blank(), None)

    def _confirm_discard(self) -> bool:
        if not self.controller.dirty:
            return True
        answer = messagebox.askyesnocancel(
            "Unsaved changes", "Save the current template first?"
        )
        if answer is None:
            return False
        if answer:
            return self.save_file()
        return True

    def open_file(self) -> None:
        if not self._confirm_discard():
            return
        start = default_template_dir()
        chosen = filedialog.askopenfilename(
            title="Open plate template",
            initialdir=str(start if start.exists() else Path.cwd()),
            defaultextension=".json",
            filetypes=[("Plate template", "*.json"), ("All files", "*.*")],
        )
        if not chosen:
            return
        try:
            template = load(Path(chosen))
        except TemplateError as exc:
            messagebox.showerror("Could not open template", str(exc))
            return
        self.controller.replace(template)
        self.path = Path(chosen)
        self.refresh()

    def save_file(self) -> bool:
        if self.path is None:
            return self.save_file_as()
        errors = [i for i in validate(self.template) if i.severity is Severity.ERROR]
        if errors and not messagebox.askyesno(
            "Save anyway?",
            f"This template has {len(errors)} blocking error(s) and cannot be "
            f"used for analysis yet.\n\nSave it anyway?",
        ):
            return False
        try:
            save(self.template, self.path)
        except OSError as exc:
            messagebox.showerror("Could not save", str(exc))
            return False
        self.controller.mark_saved()
        return True

    def save_file_as(self) -> bool:
        start = default_template_dir()
        start.mkdir(parents=True, exist_ok=True)
        chosen = filedialog.asksaveasfilename(
            title="Save plate template",
            initialdir=str(start),
            initialfile=f"{self.template.name}.json",
            defaultextension=".json",
            filetypes=[("Plate template", "*.json"), ("All files", "*.*")],
        )
        if not chosen:
            return False
        self.path = Path(chosen)
        return self.save_file()

    def _on_close(self) -> None:
        if self._confirm_discard():
            self.root.destroy()

    # -- refresh -------------------------------------------------------------

    def refresh(self) -> None:
        # First, because an undo can remove a plate: redrawing a canvas whose
        # plate no longer exists raises, which used to abort the rest of the
        # refresh and leave a dead tab behind.
        self._sync_tabs()

        issues = validate(self.template)
        self.validation.set_issues(issues)

        errors = sum(1 for i in issues if i.severity is Severity.ERROR)
        warnings = sum(1 for i in issues if i.severity is Severity.WARNING)
        summary = next((i.message for i in issues if i.code == "summary"), "")
        self.status.set(
            summary, f"{errors} error(s), {warnings} warning(s)", alarm=errors > 0
        )

        stack = self.controller.undo_stack
        self.edit_menu.entryconfigure(
            0, label=f"Undo {stack.undo_label}".strip(),
            state="normal" if stack.can_undo else "disabled",
        )
        self.edit_menu.entryconfigure(
            1, label=f"Redo {stack.redo_label}".strip(),
            state="normal" if stack.can_redo else "disabled",
        )

        mark = "*" if self.controller.dirty else ""
        name = self.path.name if self.path else f"{self.template.name} (unsaved)"
        self.root.title(f"{mark}{name} - {APP_TITLE}")

        for canvas in self.canvases.values():
            canvas.redraw()

    def _apply_token_view(self) -> None:
        for canvas in self.canvases.values():
            canvas.show_tokens = self.show_tokens.get()
            canvas.redraw()

    def _toggle_tokens(self) -> None:
        self.show_tokens.set(not self.show_tokens.get())
        self._apply_token_view()

    def _on_tab_changed(self, _event: tk.Event) -> None:
        # A canvas only learns its real size once its tab is first shown.
        current = self.notebook.select()
        if not current:
            return
        for canvas in self.canvases.values():
            if str(canvas.master) == current:
                canvas.after_idle(canvas.redraw)

    def _on_issue_selected(self, issue: Issue) -> None:
        if not issue.plate_id or issue.plate_id not in self.canvases:
            return
        self.select_plate(issue.plate_id)
        if issue.cells:
            self.canvases[issue.plate_id].flash(issue.cells)

    # -- help ----------------------------------------------------------------

    def quick_start(self) -> None:
        window = tk.Toplevel(self.root)
        window.title("Quick start")
        window.transient(self.root)
        text = tk.Text(window, wrap="word", width=82, height=30, padx=12, pady=10)
        text.insert("1.0", QUICK_START)
        text.configure(state="disabled")
        text.pack(fill="both", expand=True)
        ttk.Button(window, text="Close", command=window.destroy).pack(pady=6)

    def _about(self) -> None:
        messagebox.showinfo(
            "About",
            f"{APP_TITLE}\n\n"
            "Design a spotting-assay plate layout and save it as a reusable, "
            "strain-agnostic template.",
        )


class CellDialog(simpledialog.Dialog):
    """Set one cell to exactly what you mean, regardless of the active tool."""

    def __init__(self, parent, template: Template, cell: Cell) -> None:
        self.template = template
        self.existing = cell
        self.result: Cell | None = None
        super().__init__(parent, title="Edit cell")

    def body(self, master):
        placement = self.existing.placement
        self.kind = tk.StringVar(value=self.existing.kind.value)
        self.slot = tk.IntVar(value=placement.sample_slot if placement else 1)
        self.replicate = tk.IntVar(value=placement.replicate if placement else 1)
        self.dilution = tk.IntVar(value=placement.dilution if placement else 0)

        for row, (label, variable, lo, hi) in enumerate(
            (
                ("Sample slot", self.slot, 1, 99),
                ("Replicate", self.replicate, 1, 99),
                ("Dilution", self.dilution, 0, 20),
            )
        ):
            ttk.Label(master, text=label).grid(row=row, column=0, sticky="w", padx=4, pady=2)
            ttk.Spinbox(
                master, from_=lo, to=hi, textvariable=variable, width=8
            ).grid(row=row, column=1, padx=4, pady=2)

        box = ttk.LabelFrame(master, text="This cell holds", padding=(6, 4))
        box.grid(row=3, column=0, columnspan=2, sticky="ew", padx=4, pady=(8, 2))
        for value, label in (
            ("spot", "a spot, as set above"),
            (".", "nothing (deliberately empty)"),
            ("?", "undecided"),
        ):
            ttk.Radiobutton(
                box, text=label, value=value, variable=self.kind
            ).pack(anchor="w")
        return None

    def apply(self) -> None:
        kind = self.kind.get()
        if kind == ".":
            self.result = Cell(CellKind.EMPTY)
        elif kind == "?":
            self.result = Cell(CellKind.UNASSIGNED)
        else:
            self.result = Cell.spot(
                self.slot.get(), self.replicate.get(), self.dilution.get()
            )


def _startup_template(args: argparse.Namespace) -> tuple[Template, Path | None]:
    if args.template:
        path = Path(args.template)
        return load(path), path
    if args.preset:
        for key, _label, factory in list_presets():
            if key == args.preset:
                return factory(), None
        raise SystemExit(
            f"unknown preset {args.preset!r}; choose from "
            f"{', '.join(k for k, _, _ in list_presets())}"
        )
    return blank(), None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="plate_template.app", description=APP_TITLE)
    parser.add_argument("template", nargs="?", help="a plate template .json to open")
    parser.add_argument("--preset", help="start from a named preset")
    parser.add_argument(
        "--selftest", action="store_true",
        help="build the window, render once, and exit (no interaction)",
    )
    args = parser.parse_args(argv)

    try:
        template, path = _startup_template(args)
    except TemplateError as exc:
        print(exc, file=sys.stderr)
        return 2

    _enable_dpi_awareness()
    root = tk.Tk()
    app = DesignerApp(root, template, path)

    if args.selftest:
        root.update_idletasks()
        root.update()
        app.refresh()
        root.destroy()
        print(f"selftest OK: rendered {len(app.canvases)} plate(s)")
        return 0

    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
