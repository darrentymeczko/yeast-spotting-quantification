"""Colours and spacing for the review window.

The rule the palette follows: a row that the pipeline decided and a row that a
PERSON decided must never look the same. Automatic flags are grey-blue and
recede; anything a person changed is amber and comes forward. That is the one
distinction this whole tool exists to make visible, so it is carried by hue and
by a text tag in the row, never by colour alone -- the flag column always spells
it out, which also keeps it readable for a colourblind reader and in a printed
screenshot.
"""

from __future__ import annotations

# --- spacing ----------------------------------------------------------------

PAD = 8
GAP = 4
SIDEBAR_W = 260        # candidate list; wide enough for "16 Hours · middle"
SUMMARY_W = 270        # strain means; "1.000 ± 0.146" plus the strain name
THUMB_H = 64           # filmstrip thumbnails
SHEET_MIN_H = 240      # never squeeze the sheet below this

# --- neutrals ---------------------------------------------------------------

BG = "#fbfbfb"
PANEL = "#ffffff"
SUNKEN = "#f2f2f2"
LINE = "#cfcfcf"
TEXT = "#1c1c1c"
MUTED = "#8a8a8a"
ACCENT = "#1f6feb"
ACCENT_SOFT = "#e8f0fe"

# --- meaning ----------------------------------------------------------------

#: The pipeline's own pick, marked in the candidate list.
DEFAULT_MARK = "#1f6feb"
#: A pick that differs from the pipeline's.
CHOSEN = "#0b7a3b"
CHOSEN_SOFT = "#e6f4ec"

#: A person's edit. Amber throughout, everywhere, for anything hand-made.
MANUAL = "#9a6400"
MANUAL_SOFT = "#fdf3dd"

#: The pipeline's own flags.
AUTO_FLAG = "#5a6b7a"
AUTO_SOFT = "#eef2f5"

#: Rows that carry no signal: excluded strains, unusable spots.
DROPPED = "#8a8a8a"
DROPPED_SOFT = "#f0f0f0"

CONTROL_SOFT = "#eaf1fb"

ERROR = "#b00020"
WARNING = "#9a6400"
OK = "#0b7a3b"

# --- fonts ------------------------------------------------------------------

FONT = ("Segoe UI", 9)
FONT_SMALL = ("Segoe UI", 8)
FONT_BOLD = ("Segoe UI", 9, "bold")
FONT_HEAD = ("Segoe UI", 11, "bold")
FONT_MONO = ("Consolas", 9)


def row_tags(row) -> tuple[str, ...]:
    """Treeview tags for one rebuilt spot, most important last.

    Order matters: tkinter applies the LAST matching tag's colours, so a manual
    edit wins over an automatic flag, which wins over the plain control tint.
    A person needs to see their own change above everything else.
    """
    tags: list[str] = []
    if bool(row.get("is_control")):
        tags.append("control")
    if bool(row.get("artifact")):
        tags.append("artifact")
    if bool(row.get("outlier")):
        tags.append("auto" if row.get("outlier_source") == "auto" else "manual")
    if bool(row.get("excluded")):
        tags.append("dropped" if row.get("excluded_source") == "config"
                    else "manual")
    if row.get("excluded_source") in ("manual", "manual-cleared"):
        tags.append("manual")
    if row.get("outlier_source") in ("manual", "cleared"):
        tags.append("manual")
    if bool(row.get("manual")):
        tags.append("manual")
    return tuple(tags)


def configure_tags(tree) -> None:
    """Apply the palette to a ttk.Treeview."""
    tree.tag_configure("control", background=CONTROL_SOFT)
    tree.tag_configure("artifact", background=AUTO_SOFT, foreground=AUTO_FLAG)
    tree.tag_configure("auto", background=AUTO_SOFT, foreground=AUTO_FLAG)
    tree.tag_configure("dropped", background=DROPPED_SOFT, foreground=DROPPED)
    tree.tag_configure("manual", background=MANUAL_SOFT, foreground=MANUAL)


def flag_text(row) -> str:
    """The flag column: says in words what the colours say in hue.

    Every state is spelled out, including whose decision it was, because the CSV
    this produces will be read by somebody who was not in the room.
    """
    parts: list[str] = []
    if bool(row.get("is_control")):
        parts.append("control")
    if bool(row.get("artifact")):
        parts.append("artifact")
    if bool(row.get("excluded")):
        src = row.get("excluded_source") or ""
        parts.append("excluded" if src == "config" else "excluded (you)")
    elif row.get("excluded_source") == "manual-cleared":
        parts.append("kept (you)")
    if bool(row.get("outlier")):
        parts.append("outlier" if row.get("outlier_source") == "auto"
                     else "outlier (you)")
    elif row.get("outlier_source") == "cleared":
        parts.append("outlier cleared (you)")
    if bool(row.get("manual")):
        parts.append("re-measured")
    if not bool(row.get("control_ok", True)):
        parts.append("control too dim")
    return ", ".join(parts)
