"""The panel: which strain is in which sample slot, and which one is the control.

The slot numbers come from the plate template, so this is the screen where a
template stops being geometry and becomes an experiment. Slot colours are
`plate_template.theme.slot_colour`, the same ones the designer draws with, so a
slot can be matched between the two windows by eye.

Leaving a slot blank is a positive statement that nothing was spotted there --
the existing configs already express it, as a null column -- so it is offered
rather than treated as an unfinished row.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Callable

from plate_template import theme
from uikit import tokens


class StrainPanel(ttk.Frame):
    """One row per sample slot: a colour chip, a name, and a control button."""

    def __init__(self, master, controller, on_change: Callable[[], None]):
        super().__init__(master, padding=(10, 8))
        self.ctl = controller
        self.on_change = on_change
        self.control = tk.IntVar(value=0)
        self._rows: list[tuple[int, tk.StringVar]] = []
        self._entries: dict[int, ttk.Entry] = {}
        from .groups import GroupSelector
        self.group_selector = GroupSelector(self, controller, on_change)
        self.group_selector.pack(fill="x", pady=(0, 8))

        panes = ttk.PanedWindow(self, orient="horizontal")
        self.panes = panes
        panes.pack(fill="both", expand=True)
        left = ttk.Frame(panes, padding=(0, 0, 10, 0))
        right = ttk.LabelFrame(panes, text="Plate template preview", padding=(10, 8))
        self.strain_column = left
        self.preview_column = right
        panes.add(left, weight=3)
        panes.add(right, weight=2)
        panes.bind("<Configure>", self._resize_panes)

        self.intro = ttk.Label(
            left,
            text=("Name each sample slot of the plate template. Leave a slot "
                  "blank if nothing was spotted there.\nThe control is the "
                  "strain every other one is reported relative to."),
            foreground=tokens.TEXT_MUTED, justify="left",
        )
        self.intro.pack(anchor="w", fill="x", pady=(0, 8))
        left.bind("<Configure>", lambda e: self.intro.configure(
            wraplength=max(160, e.width - 20)))

        head = ttk.Frame(left)
        head.pack(fill="x")
        ttk.Label(head, text="Slot", width=6, font=("", 9, "bold")).pack(side="left")
        ttk.Label(head, text="Strain", font=("", 9, "bold")).pack(side="left")
        ttk.Label(head, text="Control", font=("", 9, "bold")).pack(side="right",
                                                                   padx=(0, 12))

        # Keep the headings visible and scroll only the strain rows.  A plain
        # frame silently clips its children when a template has more slots
        # than the window can show (most noticeably with 24-slot templates).
        rows_area = ttk.Frame(left)
        rows_area.pack(fill="both", expand=True, pady=(4, 0))
        rows_area.columnconfigure(0, weight=1)
        rows_area.rowconfigure(0, weight=1)
        rows_background = ttk.Style(self).lookup("TFrame", "background")
        self.rows_canvas = tk.Canvas(
            rows_area, highlightthickness=0, borderwidth=0,
            background=rows_background,
        )
        self.rows_scroll = ttk.Scrollbar(
            rows_area, orient="vertical", command=self.rows_canvas.yview,
        )
        self.rows_canvas.configure(yscrollcommand=self.rows_scroll.set)
        self.rows_canvas.grid(row=0, column=0, sticky="nsew")
        self.rows_scroll.grid(row=0, column=1, sticky="ns")
        self.rows_scroll.grid_remove()

        self.rows_frame = ttk.Frame(self.rows_canvas)
        self._rows_window = self.rows_canvas.create_window(
            (0, 0), window=self.rows_frame, anchor="nw",
        )
        self.rows_frame.bind("<Configure>", self._rows_content_resized)
        self.rows_canvas.bind("<Configure>", self._rows_viewport_resized)
        self._bind_row_scrolling(self.rows_canvas)

        preview_bar = ttk.Frame(right)
        preview_bar.pack(fill="x", pady=(0, 6))
        self.choose_template_button = ttk.Button(
            preview_bar, text="Choose plate template...",
            command=self._choose_template)
        self.choose_template_button.pack(side="left")
        self.template_label = ttk.Label(
            preview_bar, text="", foreground=tokens.TEXT_MUTED, width=1, anchor="w")
        self.template_label.pack(side="left", fill="x", expand=True,
                                 padx=(10, 0))

        self.template_details = ttk.Label(
            right, text="", foreground=tokens.TEXT_MUTED, justify="left", anchor="nw",
        )
        self.template_details.pack(fill="x", pady=(0, 6))
        preview_area = ttk.Frame(right)
        preview_area.pack(fill="both", expand=True)
        preview_area.columnconfigure(0, weight=1)
        preview_area.rowconfigure(0, weight=1)
        self.template_preview = tk.Canvas(
            preview_area, highlightthickness=1, highlightbackground=tokens.LINE,
            background=tokens.SURFACE, width=360, height=280,
        )
        self.template_scroll = ttk.Scrollbar(
            preview_area, orient="horizontal", command=self.template_preview.xview)
        self.template_preview.configure(xscrollcommand=self.template_scroll.set)
        self.template_preview.grid(row=0, column=0, sticky="nsew")
        self.template_scroll.grid(row=1, column=0, sticky="ew")
        self.template_scroll.grid_remove()
        self.template_preview.bind("<Configure>", lambda _e: self._draw_preview())
        self.template_preview.bind("<Button-1>", self._preview_clicked)

    # -- building ------------------------------------------------------------

    def _resize_panes(self, event) -> None:
        """Give the diagram more room while keeping the strain form usable."""
        if event.width > 1:
            try:
                self.panes.sashpos(0, int(event.width * 0.40))
            except tk.TclError:
                pass

    def refresh(self) -> None:
        self.group_selector.refresh()
        e = self.ctl.panel_experiment
        if self.ctl.template is not None:
            self.template_label.configure(
                text=f"{self.ctl.template.name}  "
                     f"({self.ctl.template.sample_slots()} slots, "
                     f"{len(self.ctl.template.plates)} plates)"
            )
        elif self.ctl.template_error:
            self.template_label.configure(text=self.ctl.template_error)
        else:
            self.template_label.configure(text="none chosen")

        slots = max(e.slot_count(), 1)
        if len(self._rows) != slots:
            self._rebuild(slots)
        for slot, var in self._rows:
            name = e.strain(slot) or ""
            if var.get() != name:
                var.set(name)
        self.control.set(e.control_slot or 0)
        self._refresh_template_preview()

    def _refresh_template_preview(self) -> None:
        template = self.ctl.template
        if template is None:
            self.template_details.configure(
                text=self.ctl.template_error or
                "Choose a plate template to see its layout here."
            )
        else:
            self.template_details.configure(
                text=(f"{template.name}\n{template.sample_slots()} sample slots  ·  "
                      f"{len(template.plates)} plates  ·  "
                      f"{template.dilution.levels} dilution levels")
            )
        if template is not None and len(template.plates) > 2:
            self.template_scroll.grid()
        else:
            self.template_scroll.grid_remove()
        self.template_preview.configure(cursor="" if template else "hand2")
        self._draw_preview()

    def _draw_preview(self) -> None:
        canvas = self.template_preview
        canvas.delete("all")
        template = self.ctl.template
        width, height = canvas.winfo_width(), canvas.winfo_height()
        if template is None or width < 80 or height < 80:
            canvas.create_text(max(10, width // 2), max(10, height // 2),
                               text="Click here to choose a plate template",
                               fill=tokens.TEXT_MUTED, justify="center")
            return

        plates = template.plates
        visible = min(2, len(plates))
        gap = 12
        section_w = max(220, (width - gap * (visible + 1)) / max(1, visible))
        content_width = gap + len(plates) * (section_w + gap)
        canvas.configure(scrollregion=(0, 0, content_width, height))
        for index, plate in enumerate(plates):
            left = gap + index * (section_w + gap)
            if index:
                divider_x = left - gap / 2
                canvas.create_line(
                    divider_x, 10, divider_x, height - 11,
                    fill=theme.GRID_LINE, tags=("plate-divider",),
                )
            canvas.create_text(
                left + 9, 18, text=plate.label or f"Plate {plate.id}",
                anchor="w", fill=tokens.TEXT, tags=("plate", f"plate-{index}"),
            )
            if not plate.rows or not plate.cols:
                continue
            # Fit the plate's actual aspect ratio into its card.  The old
            # fixed 34 px ceiling left most of a large preview blank; now the
            # available width or height is the limiting dimension.  A generous
            # DPI-aware ceiling only prevents tiny templates (for example 1x1)
            # from becoming a single comically oversized disc.
            display_scale = max(1.0, self.winfo_fpixels("1i") / 96)
            cell = min((section_w - 42) / plate.cols,
                       (height - 58) / plate.rows,
                       72 * display_scale)
            cell = max(4, cell)
            grid_w = cell * plate.cols
            grid_h = cell * plate.rows
            x0 = left + (section_w - grid_w) / 2
            y0 = 34 + max(0, (height - 42 - grid_h) / 2)
            previous_replicate = None
            for row_index, row in enumerate(plate.cells):
                row_placement = next(
                    (spot.placement for spot in row if spot.placement), None)
                if (row_placement is not None and
                        row_placement.replicate != previous_replicate):
                    canvas.create_text(
                        x0,
                        y0 + row_index * cell + cell / 2,
                        text=str(row_placement.replicate), anchor="e",
                        fill=theme.REPLICATE_LABEL,
                        tags=("plate", f"plate-{index}", "replicate-label",
                              f"replicate-{row_placement.replicate}"),
                    )
                    previous_replicate = row_placement.replicate
                for col_index, spot in enumerate(row):
                    x = x0 + col_index * cell + cell / 2
                    y = y0 + row_index * cell + cell / 2
                    placement = spot.placement
                    # Replicate lightness is useful on the full editor, where
                    # badges and ample spacing disambiguate it. In this compact
                    # preview it made replicate 1's source row look diluted.
                    # Hold shade constant here so only dilution controls fade;
                    # the small rN labels retain the replicate information.
                    preview_replicate = 3
                    fill = (theme.spot_fill(
                        placement.sample_slot, preview_replicate,
                        placement.dilution)
                        if placement else theme.EMPTY_BG)
                    edge = (theme.spot_edge(
                        placement.sample_slot, preview_replicate,
                        placement.dilution)
                        if placement else theme.GRID_LINE)
                    radius = max(2, theme.spot_radius(cell))
                    tags = ["plate", f"plate-{index}"]
                    if placement:
                        tags.extend((f"slot-{placement.sample_slot}",
                                     f"dilution-{placement.dilution}"))
                    canvas.create_oval(x - radius, y - radius,
                                       x + radius, y + radius,
                                       fill=fill, outline=edge,
                                       tags=tuple(tags))

    def _rebuild(self, slots: int) -> None:
        for child in self.rows_frame.winfo_children():
            child.destroy()
        self._rows.clear()
        self._entries.clear()

        for slot in range(1, slots + 1):
            row = ttk.Frame(self.rows_frame)
            row.pack(fill="x", pady=1)

            chip = tk.Canvas(row, width=16, height=16, highlightthickness=0,
                             borderwidth=0)
            chip.pack(side="left", padx=(0, 6))
            chip.create_rectangle(0, 0, 16, 16, fill=theme.slot_colour(slot, 1),
                                  outline=tokens.LINE_STRONG)
            ttk.Label(row, text=str(slot), width=3).pack(side="left")

            var = tk.StringVar()
            entry = ttk.Entry(row, textvariable=var)
            entry.pack(side="left", fill="x", expand=True, padx=(4, 12))
            # Committed on leaving the field or pressing Return, not per
            # keystroke: an undo step per letter typed would be useless.
            entry.bind("<FocusOut>", lambda _e, s=slot, v=var: self._commit(s, v))
            entry.bind("<Return>",
                       lambda _e, s=slot, v=var: self._commit_and_move(s, v, 1))
            entry.bind("<Down>",
                       lambda _e, s=slot, v=var: self._commit_and_move(s, v, 1))
            entry.bind("<Up>",
                       lambda _e, s=slot, v=var: self._commit_and_move(s, v, -1))

            ttk.Radiobutton(row, variable=self.control, value=slot,
                            command=self._set_control).pack(side="right", padx=(0, 24))
            self._rows.append((slot, var))
            self._entries[slot] = entry

            # Mouse-wheel events go to the widget under the pointer rather
            # than bubbling to the containing canvas, so bind each row and
            # each of its controls to the row viewport.
            self._bind_row_scrolling(row)
            for child in row.winfo_children():
                self._bind_row_scrolling(child)

        self.rows_canvas.after_idle(self._update_rows_scrollbar)

    def _rows_content_resized(self, _event=None) -> None:
        """Track the complete row height and whether scrolling is necessary."""
        bbox = self.rows_canvas.bbox(self._rows_window)
        if bbox is not None:
            self.rows_canvas.configure(scrollregion=bbox)
        self._update_rows_scrollbar()

    def _rows_viewport_resized(self, event) -> None:
        """Make every row use the viewport width, then reassess overflow."""
        self.rows_canvas.itemconfigure(self._rows_window, width=event.width)
        self._update_rows_scrollbar()

    def _update_rows_scrollbar(self) -> None:
        content_height = self.rows_frame.winfo_reqheight()
        viewport_height = self.rows_canvas.winfo_height()
        if viewport_height > 1 and content_height > viewport_height:
            self.rows_scroll.grid()
        else:
            self.rows_scroll.grid_remove()
            self.rows_canvas.yview_moveto(0)

    def _bind_row_scrolling(self, widget) -> None:
        widget.bind("<MouseWheel>", self._scroll_rows, add="+")
        widget.bind("<Button-4>", self._scroll_rows, add="+")
        widget.bind("<Button-5>", self._scroll_rows, add="+")

    def _scroll_rows(self, event) -> str:
        """Scroll rows with the wheel on Windows, macOS, and Linux/Tk."""
        if not self.rows_scroll.winfo_ismapped():
            return "break"
        if getattr(event, "num", None) == 4:
            units = -1
        elif getattr(event, "num", None) == 5:
            units = 1
        else:
            delta = getattr(event, "delta", 0)
            units = -1 if delta > 0 else 1
        self.rows_canvas.yview_scroll(units, "units")
        return "break"

    # -- edits ---------------------------------------------------------------

    def _preview_clicked(self, _event=None) -> str | None:
        if self.ctl.template is None:
            self._choose_template()
            return "break"
        return None

    def _commit(self, slot: int, var: tk.StringVar) -> None:
        if self.ctl.set_strain(slot, var.get()):
            self.on_change()

    def _commit_and_move(self, slot: int, var: tk.StringVar,
                         direction: int) -> str:
        """Save this field, then make the adjacent slot ready for typing."""
        self._commit(slot, var)
        target_slot = max(1, min(len(self._entries), slot + direction))
        target = self._entries[target_slot]
        target.focus_set()
        target.selection_range(0, "end")
        target.icursor("end")
        return "break"

    def _set_control(self) -> None:
        if self.ctl.set_control(self.control.get()):
            self.on_change()

    def _choose_template(self) -> None:
        from pathlib import Path
        from tkinter import filedialog

        from .. import BUNDLE, REPO

        start = REPO / "Plate Templates"
        if not start.is_dir():
            start = BUNDLE / "plate_template" / "templates"
        chosen = filedialog.askopenfilename(
            title="Choose a plate template",
            initialdir=str(start if start.is_dir() else Path.cwd()),
            filetypes=[("Plate template", "*.json"), ("All files", "*.*")],
            parent=self,
        )
        if chosen:
            self.ctl.bind_template(Path(chosen))
            self.on_change()
