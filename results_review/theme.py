"""Colours and spacing for the review window.

The rule the palette follows: a row that the pipeline decided and a row that a
PERSON decided must never look the same. Automatic flags are grey-blue and
recede; anything a person changed is amber and comes forward. A spot flagged
in the data review, before the run, is red: a warning still waiting for a
decision here, rather than a decision. That is the one
distinction this whole tool exists to make visible, so it is carried by hue and
by a text tag in the row, never by colour alone -- the flag column always spells
it out, which also keeps it readable for a colourblind reader and in a printed
screenshot.
"""

from __future__ import annotations

from uikit import dpi, tokens

# --- spacing ----------------------------------------------------------------
#
# In pixels at 96 dpi. The window is DPI-aware (see `app.main`), so on a scaled
# display these are multiplied by `SCALE` -- otherwise a 200% laptop gets every
# gap, column and pane at half size. Fonts are given in points and scale on
# their own.

PAD = 8
GAP = 4
SIDEBAR_W = 260        # candidate list; wide enough for "16 Hours · middle"
SUMMARY_W = 270        # strain means; "1.000 ± 0.146" plus the strain name
THUMB_H = 64           # filmstrip thumbnails
SHEET_MIN_H = 240      # never squeeze the sheet below this

_BASE = {"PAD": PAD, "GAP": GAP, "SIDEBAR_W": SIDEBAR_W,
         "SUMMARY_W": SUMMARY_W, "THUMB_H": THUMB_H,
         "SHEET_MIN_H": SHEET_MIN_H}
#: Screen pixels per 96-dpi pixel: 1.0 unless the process is DPI-aware on a
#: scaled display.
SCALE = 1.0


def px(n: float) -> int:
    """A 96-dpi pixel size in this display's pixels."""
    return int(round(n * SCALE))


def set_scale(widget) -> float:
    """Read the display's scale off a Tk widget and rescale the spacing.

    Tk reports the true dpi only to a DPI-aware process; an unaware one is
    told 96 and stretched by Windows instead, and then this stays at 1.0.
    """
    global SCALE
    try:
        SCALE = max(1.0, float(widget.winfo_fpixels("1i")) / 96.0)
    except Exception:                           # pragma: no cover - defensive
        SCALE = 1.0
    for name, value in _BASE.items():
        globals()[name] = px(value)
    return SCALE


def enable_dpi_awareness() -> None:
    """Draw at the display's real resolution on Windows. Before any Tk root.

    Without it Windows renders the window at 96 dpi and enlarges the result,
    so on a 200% display every image is shown at half its resolution and
    doubled -- the spot photos and graphs look pixelated however sharp the
    files are.
    """
    dpi.enable_dpi_awareness()

# --- neutrals ---------------------------------------------------------------
#
# The shared ones, so this window wears the same chrome as every other tool.
# The colours under "meaning" below are this tool's own.

BG = tokens.BG
PANEL = tokens.SURFACE
SUNKEN = tokens.SURFACE_ALT
LINE = tokens.LINE
TEXT = tokens.TEXT
MUTED = tokens.TEXT_MUTED
ACCENT = tokens.ACCENT
ACCENT_SOFT = tokens.ACCENT_SOFT

# --- meaning ----------------------------------------------------------------

#: The pipeline's own pick, marked in the candidate list.
DEFAULT_MARK = tokens.ACCENT
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

#: Flagged as bad data in the data review, before the run. A warning, not a
#: decision: the row still counts until somebody omits it here. Red, as the
#: data review draws its flags.
REVIEWED_BAD = "#B3261E"
REVIEWED_BAD_SOFT = "#fdecea"

CONTROL_SOFT = "#eaf1fb"

ERROR = tokens.ERROR
WARNING = tokens.WARNING
OK = tokens.OK

# --- fonts ------------------------------------------------------------------

FONT = (tokens.FONT_FAMILY, tokens.SIZE_BODY)
FONT_SMALL = (tokens.FONT_FAMILY, tokens.SIZE_SMALL)
FONT_BOLD = (tokens.FONT_FAMILY, tokens.SIZE_BODY, "bold")
FONT_HEAD = (tokens.FONT_FAMILY, 11, "bold")
FONT_MONO = (tokens.MONO_FAMILY, tokens.SIZE_BODY)


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
    # Above the automatic flags, below anything a person did HERE: once the
    # flagged spot has been omitted (or kept) in review, that is the news.
    if row.get("data_review"):
        at = tags.index("manual") if "manual" in tags else len(tags)
        tags.insert(at, "datareview")
    return tuple(tags)


def configure_tags(tree) -> None:
    """Apply the palette to a ttk.Treeview."""
    tree.tag_configure("control", background=CONTROL_SOFT)
    tree.tag_configure("artifact", background=AUTO_SOFT, foreground=AUTO_FLAG)
    tree.tag_configure("auto", background=AUTO_SOFT, foreground=AUTO_FLAG)
    tree.tag_configure("dropped", background=DROPPED_SOFT, foreground=DROPPED)
    tree.tag_configure("datareview", background=REVIEWED_BAD_SOFT,
                       foreground=REVIEWED_BAD)
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
    if row.get("data_review"):
        parts.append(f"⚑ data review: {row['data_review']}")
    return ", ".join(parts)
