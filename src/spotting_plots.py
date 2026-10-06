#!/usr/bin/env python3
"""Prism-style spotting figures and paired tests, entirely in Python.

The experiment-specific statistics and layout live here; ``pyprism_plot``
provides the reusable Matplotlib theme and significance-bracket primitive.
This replaces the former R/ggplot2/ggprism plotting subprocess while retaining
its input columns, output names, filtering rules, and statistics table.
"""

from __future__ import annotations

import math
import re
from pathlib import Path

import numpy as np
import pandas as pd

DETECT_GRAY = 0.4
# Figure growth for large panels (see `_draw_one`): inches per strain along x,
# fixed x overhead (y-axis label and ticks), the least vertical room one
# significance bracket gets, and the height taken by everything that is not
# the data area (tick labels, axis title).
STRAIN_PITCH_IN = 0.34
STRAIN_PAD_IN = 1.0
# A tier must hold its bracket AND its star label below the next tier up, or
# a label reads as belonging to the bracket above it.
BRACKET_IN = 0.30
AXIS_FURNITURE_IN = 1.6
P_ADJUST_METHODS = {
    "none": None,
    "holm": "holm",
    "bonferroni": "bonferroni",
    "sidak": "sidak",
    "BH": "fdr_bh",
    "bh": "fdr_bh",
}
# Post-hoc tests that follow a one-way ANOVA to say WHICH pairs differ.
# "none" keeps the ANOVA omnibus-only. Dunnett compares every strain with one
# reference; Tukey HSD controls the error rate over every pair; the last three
# are pairwise t-tests on the ANOVA's pooled variance, corrected over the
# comparisons actually requested.
POSTHOC_METHODS = {
    "dunnett": "Dunnett",
    "tukey": "Tukey HSD",
    "holm": "Holm",
    "bonferroni": "Bonferroni",
    "sidak": "Šidák",
    "none": "none (omnibus only)",
}


def describe_test(statistical_test: str = "t_test", p_adjust: str = "none",
                  posthoc: str = "none", alpha: float = 0.05,
                  **_ignored) -> str:
    """The test in a few words, e.g. "ANOVA + Tukey HSD, p <= 0.05".

    Takes `run_plots`' keyword arguments, so a whole statistics dict can be
    passed straight in; the ones that do not change the test are ignored.
    """
    if statistical_test == "anova":
        test = ("ANOVA, omnibus only" if posthoc == "none"
                else f"ANOVA + {POSTHOC_METHODS.get(posthoc, posthoc)}")
    elif p_adjust == "none":
        test = "ratio t-test, uncorrected"
    else:
        test = f"ratio t-test, {POSTHOC_METHODS.get(p_adjust, p_adjust)}"
    return f"{test}, p <= {float(alpha):g}"


def safe_name(value: str) -> str:
    """Return the filename token used throughout the spotting pipeline."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(value)).strip("_")


def stars(p_value: float) -> str:
    """GraphPad's conventional significance labels."""
    if not np.isfinite(p_value):
        return ""
    if p_value < 1e-4:
        return "****"
    if p_value < 1e-3:
        return "***"
    if p_value < 1e-2:
        return "**"
    if p_value < 0.05:
        return "*"
    return "ns"


def _bool_column(frame: pd.DataFrame, name: str, default: bool) -> pd.Series:
    if name not in frame:
        return pd.Series(default, index=frame.index, dtype=bool)
    values = frame[name]
    if values.dtype == bool:
        return values.fillna(default)
    return values.fillna(default).map(
        lambda value: str(value).strip().lower() in {"1", "true", "t", "yes"}
    )


def _filtered(data: pd.DataFrame, value: str, *, keep_artifacts: bool,
              keep_outliers: bool) -> pd.DataFrame:
    required = {"treatment", "replicate", "strain", "strain_col", value}
    missing = sorted(required - set(data.columns))
    if missing:
        raise ValueError(f"Input is missing column(s): {', '.join(missing)}")

    keep = pd.Series(True, index=data.index)
    if not keep_artifacts:
        keep &= ~_bool_column(data, "artifact", False)
    if not keep_outliers:
        outliers = _bool_column(data, "outlier", False)
        # The review CSV records a deliberate override separately from the
        # pipeline flag.  Treat that provenance as authoritative as well as
        # the boolean so a formerly automatic outlier cannot disappear again
        # during the write/read boundary used by graph redraws.
        if "outlier_source" in data:
            cleared = data["outlier_source"].fillna("").astype(str).eq("cleared")
            outliers &= ~cleared
        keep &= ~outliers
    keep &= ~_bool_column(data, "excluded", False)
    keep &= _bool_column(data, "control_ok", True)

    out = data.loc[keep].copy()
    out["value"] = pd.to_numeric(out[value], errors="coerce")
    out["strain_col"] = pd.to_numeric(out["strain_col"], errors="raise").astype(int)
    out = out[np.isfinite(out["value"])].copy()
    out["strain"] = out["strain"].astype(str)
    out["treatment"] = out["treatment"].astype(str)
    return out


def _control_for(group: pd.DataFrame, explicit: str | None = None) -> str:
    if explicit:
        return explicit
    marked = _bool_column(group, "is_control", False)
    if marked.any():
        return str(group.loc[marked, "strain"].iloc[0])
    first_col = int(group["strain_col"].min())
    return str(group.loc[group["strain_col"] == first_col, "strain"].iloc[0])


def _ordered_strains(group: pd.DataFrame, control: str) -> list[str]:
    ordered = (group[["strain", "strain_col"]]
               .drop_duplicates().sort_values("strain_col")["strain"].tolist())
    ordered = list(dict.fromkeys(ordered))
    return ([control] + [name for name in ordered if name != control]
            if control in ordered else ordered)


def comparison_pairs(names: list[str], control: str, extra_references=(),
                     all_pairs: bool = False) -> list[tuple[str, str]]:
    """The (reference, strain) pairs to test, in the order they are drawn.

    By default every strain against the control -- exactly the comparisons the
    pipeline always made. `extra_references` adds further strains that every
    other strain is also compared with; `all_pairs` compares every strain with
    every other. A pair is tested once however many ways it is requested.
    """
    names = list(names)
    if all_pairs:
        return [(a, b) for i, a in enumerate(names) for b in names[i + 1:]]
    refs = [control] + [r for r in dict.fromkeys(extra_references or ())
                        if r in names and r != control]
    seen, out = set(), []
    for ref in refs:
        for name in names:
            key = frozenset((ref, name))
            if name == ref or key in seen:
                continue
            seen.add(key)
            out.append((ref, name))
    return out


def _floored(sub: pd.DataFrame) -> pd.DataFrame:
    """Finite values of one strain, raised to the detection floor.

    A spot below DETECT_GRAY is indistinguishable from no growth, so its ratio
    is floored at the ratio that grey level would give rather than left to run
    to zero and blow up on the log scale. Returns (replicate, value) rows.
    """
    values = sub["value"].to_numpy(dtype=float)
    finite = np.isfinite(values)
    values = values[finite]
    if "control_raw" in sub:
        controls = pd.to_numeric(sub["control_raw"], errors="coerce").to_numpy()[finite]
        limits = DETECT_GRAY / controls
        valid_limits = np.isfinite(limits)
        values[valid_limits] = np.maximum(values[valid_limits], limits[valid_limits])
    reps = (sub["replicate"].to_numpy()[finite] if "replicate" in sub
            else np.arange(len(values)))
    return pd.DataFrame({"replicate": reps, "value": values})


def _adjust(result: pd.DataFrame, method: str) -> pd.DataFrame:
    """Fill `p_adj` from `p` over the whole family of rows."""
    result["p_adj"] = result["p"]
    finite = result["p"].notna()
    if P_ADJUST_METHODS[method] is not None and finite.any():
        from statsmodels.stats.multitest import multipletests

        result.loc[finite, "p_adj"] = multipletests(
            result.loc[finite, "p"], method=P_ADJUST_METHODS[method])[1]
    return result


def _ratio_tests(group: pd.DataFrame, control: str, p_adjust: str,
                 pairs: "list[tuple[str, str]] | None" = None) -> pd.DataFrame:
    """Log-ratio one-sample tests, matching the former R implementation.

    A comparison with the control tests the strain's normalised ratios against
    1, exactly as always. A comparison between two other strains pairs them by
    replicate -- both were normalised to the same plate's control -- and tests
    the per-replicate log ratio B/A against 0. The correction runs over every
    requested comparison, so asking for more comparisons makes each one
    correspondingly harder to call.
    """
    from scipy import stats

    if p_adjust not in P_ADJUST_METHODS:
        raise ValueError(
            "p_adjust must be 'none', 'holm', 'bonferroni', 'sidak', or 'BH'")
    if pairs is None:
        pairs = comparison_pairs(_ordered_strains(group, control), control)
    by_strain = {name: _floored(group[group["strain"] == name])
                 for name in {s for pair in pairs for s in pair}}

    rows = []
    for ref, strain in pairs:
        if control in (ref, strain):
            other = strain if ref == control else ref
            logs = np.log(np.maximum(by_strain[other]["value"].to_numpy(), 1e-9))
            ok = by_strain[other]["value"].to_numpy() > 0
            sign = 1.0 if other == strain else -1.0
        else:
            # Replicates named twice (a strain in two columns) are averaged
            # on the log scale before pairing.
            def per_rep(name):
                f = by_strain[name]
                f = f[f["value"] > 0]
                return np.log(f["value"]).groupby(f["replicate"]).mean()
            a, b = per_rep(ref), per_rep(strain)
            common = a.index.intersection(b.index)
            logs = (b[common] - a[common]).to_numpy()
            ok = np.isfinite(logs)
            sign = 1.0
        n_ok = len(logs)
        mean_ratio = float(np.exp(sign * np.mean(logs))) if n_ok else np.nan
        p_value = np.nan
        if n_ok >= 2 and np.all(ok):
            p_value = float(stats.ttest_1samp(logs, 0).pvalue)
        rows.append({"group1": ref, "group2": strain, "n": n_ok,
                     "mean_ratio": mean_ratio, "p": p_value})

    result = pd.DataFrame(rows, columns=["group1", "group2", "n",
                                         "mean_ratio", "p"])
    return _adjust(result, p_adjust)


def _log_groups(group: pd.DataFrame, names: list[str]) -> dict:
    """{strain: log relative growth} over the values ANOVA can use (> 0)."""
    out = {}
    for name in names:
        values = _floored(group[group["strain"] == name])["value"].to_numpy()
        values = values[values > 0]
        if len(values):
            out[name] = np.log(values)
    return out


DUNNETT_SEED = 0


def _dunnett(stats, samples: list, control: np.ndarray):
    """`scipy.stats.dunnett` with a fixed seed.

    Its p-values come from a randomised numerical integration, so unseeded
    they shift in the fourth significant figure between two identical calls --
    enough to move a borderline strain across the cutoff between one redraw
    and the next. SciPy renamed the seed argument in 1.15.
    """
    try:
        return stats.dunnett(*samples, control=control,
                             rng=np.random.default_rng(DUNNETT_SEED))
    except TypeError:
        return stats.dunnett(*samples, control=control,
                             random_state=np.random.default_rng(DUNNETT_SEED))


def _posthoc_tests(logs: dict, method: str,
                   pairs: list[tuple[str, str]]) -> pd.DataFrame:
    """Which of the requested pairs differ, after a one-way ANOVA.

    Groups with fewer than two values carry no variance estimate and are left
    out, their comparisons reported as NaN.

    * Dunnett: each reference's comparisons are one Dunnett family -- that
      reference against every other strain -- and the requested pairs are
      read out of it. Families for different references are not corrected
      against each other; that is what Dunnett means.
    * Tukey HSD: one family over every pair, whatever subset is requested.
    * Holm / Bonferroni / Šidák: t-tests on the ANOVA's pooled variance
      (Prism's "multiple comparisons" after an ANOVA), corrected over the
      requested pairs.
    """
    from scipy import stats

    if method not in POSTHOC_METHODS or method == "none":
        raise ValueError(f"unknown post-hoc test {method!r}")
    valid = {k: v for k, v in logs.items() if len(v) >= 2}
    names = list(valid)
    label = f"{POSTHOC_METHODS[method]} post-hoc"
    p_raw: dict = {}
    p_adj: dict = {}

    usable = [(a, b) for a, b in pairs if a in valid and b in valid]
    if method == "dunnett" and len(names) >= 2:
        for ref in dict.fromkeys(a for a, _ in usable):
            others = [n for n in names if n != ref]
            res = _dunnett(stats, [valid[o] for o in others], valid[ref])
            got = dict(zip(others, np.atleast_1d(res.pvalue)))
            for a, b in usable:
                if a == ref:
                    p_raw[(a, b)] = p_adj[(a, b)] = float(got[b])
    elif method == "tukey" and len(names) >= 2:
        # Tukey-Kramer, as `scipy.stats.tukey_hsd` computes it, but only for
        # the pairs requested: its studentized-range tail is a numerical
        # integral per pair, and tukey_hsd does all k(k-1)/2 of them -- 276
        # for 24 strains, ~1.7 s -- when the default asks for 23.
        n_total = sum(len(v) for v in valid.values())
        df = n_total - len(names)
        mse = (sum(float(((v - v.mean()) ** 2).sum()) for v in valid.values())
               / df) if df > 0 else np.nan
        q = np.array([abs(valid[b].mean() - valid[a].mean())
                      / math.sqrt(mse / 2 * (1 / len(valid[a]) + 1 / len(valid[b])))
                      for a, b in usable]) if usable and mse > 0 else np.array([])
        p = (stats.studentized_range.sf(q, len(names), df) if len(q)
             else np.array([]))
        for (a, b), value in zip(usable, np.atleast_1d(p)):
            p_raw[(a, b)] = p_adj[(a, b)] = float(min(1.0, value))
    elif len(names) >= 2:
        n_total = sum(len(v) for v in valid.values())
        df = n_total - len(names)
        mse = (sum(float(((v - v.mean()) ** 2).sum()) for v in valid.values())
               / df) if df > 0 else np.nan
        raw = []
        for a, b in usable:
            se = math.sqrt(mse * (1 / len(valid[a]) + 1 / len(valid[b])))
            t = (valid[b].mean() - valid[a].mean()) / se if se > 0 else np.nan
            p = float(2 * stats.t.sf(abs(t), df)) if np.isfinite(t) else np.nan
            p_raw[(a, b)] = p
            raw.append(p)
        frame = _adjust(pd.DataFrame({"p": raw}), method)
        for (a, b), p in zip(usable, frame["p_adj"]):
            p_adj[(a, b)] = float(p)

    rows = []
    for a, b in pairs:
        va, vb = logs.get(a, np.array([])), logs.get(b, np.array([]))
        rows.append({
            "test": label, "group1": a, "group2": b, "n_groups": np.nan,
            "n": len(va) + len(vb), "f": np.nan,
            "mean_ratio": (float(np.exp(vb.mean() - va.mean()))
                           if len(va) and len(vb) else np.nan),
            "p": p_raw.get((a, b), np.nan), "p_adj": p_adj.get((a, b), np.nan),
        })
    return pd.DataFrame(rows)


def _anova_test(group: pd.DataFrame, control: str, posthoc: str = "none",
                pairs: "list[tuple[str, str]] | None" = None) -> pd.DataFrame:
    """One-way ANOVA of log relative growth, then an optional post-hoc test.

    Relative growth is a ratio, so the log transform puts equal fold changes
    above and below one on the same scale, matching the t-test path. The
    ANOVA itself is omnibus: it answers whether any strain mean differs and
    does not say which. With `posthoc` other than "none" the requested pairs
    (by default every strain against the control) follow it as further rows,
    and those rows are what say which strains differ.
    """
    from scipy import stats

    names = _ordered_strains(group, control)
    logs = _log_groups(group, names)
    arrays = list(logs.values())
    n_total = sum(len(v) for v in arrays)
    p_value, statistic = np.nan, np.nan
    if len(arrays) >= 2 and all(len(values) >= 2 for values in arrays):
        result = stats.f_oneway(*arrays)
        statistic, p_value = float(result.statistic), float(result.pvalue)
    out = pd.DataFrame([{
        "test": "one-way ANOVA (log relative growth)",
        "group1": control,
        "group2": "all strains",
        "n_groups": len(arrays),
        "n": n_total,
        "f": statistic,
        "mean_ratio": np.nan,
        "p": p_value,
        "p_adj": p_value,
    }])
    if posthoc == "none":
        return out
    if pairs is None:
        pairs = comparison_pairs(names, control)
    return pd.concat([out, _posthoc_tests(logs, posthoc, pairs)],
                     ignore_index=True)


def vs_control(group: pd.DataFrame, control: str, *,
               statistical_test: str = "t_test", p_adjust: str = "none",
               posthoc: str = "none", extra_references=(),
               all_pairs: bool = False) -> dict[str, tuple[float, float]]:
    """Each strain's comparison with the control, tested as the graph tests it.

    {strain: (p, ratio)}: `p` is the value the graph decides significance on
    (`p_adj`), `ratio` the strain's mean relative growth over the control's.
    The correction or post-hoc test runs over every requested comparison --
    extra references and all pairs included -- exactly as in `draw`, so the p
    here is the p beside the strain's bracket. An omnibus-only ANOVA does not
    say which strains differ, so it compares no strain: {}.

    `group` is one treatment's rows as `_filtered` leaves them.
    """
    if statistical_test not in {"t_test", "anova"}:
        raise ValueError("statistical_test must be 't_test' or 'anova'")
    check_posthoc(statistical_test, posthoc, all_pairs)
    if statistical_test == "anova" and posthoc == "none":
        return {}
    pairs = comparison_pairs(_ordered_strains(group, control), control,
                             extra_references, all_pairs)
    tests = (_ratio_tests(group, control, p_adjust, pairs)
             if statistical_test == "t_test"
             else _anova_test(group, control, posthoc, pairs))
    out = {}
    for r in tests.itertuples():
        if control not in (r.group1, r.group2) or r.group2 == "all strains":
            continue
        # `mean_ratio` is always group2 over group1.
        if r.group1 == control:
            other, ratio = r.group2, float(r.mean_ratio)
        else:
            other = r.group1
            ratio = 1.0 / float(r.mean_ratio) if r.mean_ratio else math.inf
        out[str(other)] = (float(r.p_adj), ratio)
    return out


def _center_ylabel_on_spine(fig, ax, y_bottom: float, y_top: float) -> None:
    """Centre the y-axis title on the visible axis, not the whole axes.

    With significance brackets the axes extend above `y_top` to hold them
    while the spine stops at `y_top`, so matplotlib's default -- centred on
    the axes -- puts the title well above the middle of the scale it names.
    Horizontally it keeps the spot matplotlib chose, just clear of the tick
    labels, which is what `set_label_coords` would otherwise throw away.
    """
    fig.canvas.draw()
    lo, hi = ax.get_ylim()
    frac = ((y_bottom + y_top) / 2 - lo) / (hi - lo)
    label = ax.yaxis.label
    x_disp = label.get_window_extent().x1          # right edge = the text's base
    x_axes = ax.transAxes.inverted().transform((x_disp, 0))[0]
    ax.yaxis.set_label_coords(x_axes, frac)


def check_posthoc(statistical_test: str, posthoc: str, all_pairs: bool) -> None:
    """Refuse a post-hoc choice that cannot answer the question asked."""
    if posthoc not in POSTHOC_METHODS:
        raise ValueError(f"posthoc must be one of {', '.join(POSTHOC_METHODS)}")
    if statistical_test == "anova" and posthoc == "dunnett" and all_pairs:
        raise ValueError("Dunnett's test compares strains with a reference; "
                         "use Tukey HSD to compare every pair")


def _stack_brackets(x1: list, x2: list) -> list[int]:
    """A tier (1 = lowest) for each bracket, sharing tiers where they fit.

    Shortest brackets go lowest, and a bracket joins the lowest tier where it
    touches nothing already there -- so comparisons between neighbouring
    strains sit side by side instead of each taking a line of its own. When
    every bracket starts at the control, as by default, none can share and
    this is exactly one bracket per tier in order of reach, as before.
    """
    order = sorted(range(len(x1)), key=lambda i: (x2[i] - x1[i], x1[i]))
    tiers: list[list[tuple[float, float]]] = []
    out = [0] * len(x1)
    for i in order:
        for t, held in enumerate(tiers):
            if all(x1[i] > b or x2[i] < a for a, b in held):
                held.append((x1[i], x2[i]))
                out[i] = t + 1
                break
        else:
            tiers.append([(x1[i], x2[i])])
            out[i] = len(tiers)
    return out


def _mean_sd(values: np.ndarray) -> tuple[float, float]:
    mean = float(np.mean(values))
    sd = float(np.std(values, ddof=1)) if len(values) > 1 else 0.0
    return mean, sd


#: Resolution the graph PNG is written at.
GRAPH_DPI = 400
#: PNG text key recording where each strain's tick sits in the written PNG, so
#: a viewer can line other pictures up under the strains (see spotting_sheet).
GRAPH_KEY = "spotting-graph"


def _save_bbox(fig):
    """The region of `fig` a PNG will hold, in inches.

    The Prism style crops to the drawn content ("savefig.bbox": "tight"), so
    the PNG does not start at the figure's corner. Worked out here and handed
    to savefig, so the tick map and the pixels agree exactly.
    """
    import matplotlib.pyplot as plt
    from matplotlib.transforms import Bbox

    fig.canvas.draw()
    if plt.rcParams.get("savefig.bbox") == "tight":
        pad = plt.rcParams.get("savefig.pad_inches", 0.1)
        return fig.get_tightbbox(fig.canvas.get_renderer()).padded(pad)
    w, h = fig.get_size_inches()
    return Bbox.from_bounds(0, 0, w, h)


#: Subfolder, beside the vertical PNGs, holding the same graphs turned on their
#: side (strains down the left, upright). A subfolder so nothing that collects
#: `spotting_*.png` from the figures folder mistakes one for another candidate.
HORIZONTAL_DIR = "horizontal"
#: Horizontal graph sizing: value-axis length, and room for the strain names
#: and the value axis around it, in inches.
HORIZ_DATA_IN = 4.2
HORIZ_FURNITURE_IN = 2.0


def _tick_map_horizontal(fig, ax, names: list, bbox) -> str:
    """Strain tick rows and the value axis' left end, in written-PNG pixels."""
    import json

    x0 = ax.get_xlim()[0]
    pts = [((x / fig.dpi - bbox.x0) * GRAPH_DPI,
            (bbox.y1 - y / fig.dpi) * GRAPH_DPI)
           for x, y in ax.transData.transform([(x0, i)
                                               for i in range(len(names))])]
    pitch = abs(pts[1][1] - pts[0][1]) if len(pts) > 1 else 0.0
    return json.dumps({
        "orientation": "horizontal",
        "size": [round(bbox.width * GRAPH_DPI), round(bbox.height * GRAPH_DPI)],
        "axis_x": round(pts[0][0], 1) if pts else 0,
        "pitch": round(pitch, 2),
        "ticks": [{"name": n, "y": round(float(y), 1)}
                  for n, (_, y) in zip(names, pts)],
    })


def _center_xlabel_on_spine(fig, ax, x_left: float, x_right: float) -> None:
    """The horizontal graph's twin of `_center_ylabel_on_spine`."""
    fig.canvas.draw()
    lo, hi = ax.get_xlim()
    frac = ((x_left + x_right) / 2 - lo) / (hi - lo)
    label = ax.xaxis.label
    y_disp = label.get_window_extent().y1
    y_axes = ax.transAxes.inverted().transform((0, y_disp))[1]
    ax.xaxis.set_label_coords(frac, y_axes)


def _tick_map(fig, ax, names: list, y_bottom: float, bbox) -> str:
    """Strain tick positions and the x-axis line, in written-PNG pixels."""
    import json

    def to_png(x, y):
        return ((x / fig.dpi - bbox.x0) * GRAPH_DPI,
                (bbox.y1 - y / fig.dpi) * GRAPH_DPI)

    pts = [to_png(*p) for p in
           ax.transData.transform([(i, y_bottom) for i in range(len(names))])]
    lw = ax.spines["bottom"].get_linewidth() * GRAPH_DPI / 72
    axis_y = pts[0][1] if pts else bbox.height * GRAPH_DPI
    pitch = pts[1][0] - pts[0][0] if len(pts) > 1 else 0.0
    # The y-axis line, and how far its tick labels reach below their tick:
    # the "0" label sits across the x-axis, and a viewer cutting along the
    # axis has to keep that label whole.
    left = to_png(*ax.transAxes.transform((0, 0)))[0]
    labels = ax.get_yticklabels()
    size = labels[0].get_fontsize() if labels else 10
    return json.dumps({
        "size": [round(bbox.width * GRAPH_DPI), round(bbox.height * GRAPH_DPI)],
        "axis_y": round(axis_y + lw / 2 + 1),
        "left": round(left - lw, 1),
        "ylabel_half": round(size * GRAPH_DPI / 72 * 0.6, 1),
        "pitch": round(pitch, 2),
        "ticks": [{"name": n, "x": round(float(x), 1)}
                  for n, (x, _) in zip(names, pts)],
    })


def _draw_one(group: pd.DataFrame, tests: pd.DataFrame, control: str,
              output_stem: Path, *, title: str | None, y_label: str,
              width: float, height: float, base_size: float,
              statistical_test: str, p_adjust: str, alpha: float,
              show_ns: bool, horizontal: bool = False) -> None:
    """Draw one treatment's graph. `horizontal` turns it on its side: strains
    down the left, upright and readable, values along the bottom, brackets
    off to the right. It is written, PNG only, to `HORIZONTAL_DIR`."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import pyprism_plot as ppp

    names = _ordered_strains(group, control)
    arrays = [group.loc[group["strain"] == name, "value"].to_numpy(dtype=float)
              for name in names]
    summaries = [_mean_sd(values) for values in arrays]
    top = max([float(np.max(v)) for v in arrays] + [m + sd for m, sd in summaries])
    y_top = max(0.5, math.ceil(top / 0.5) * 0.5)
    low = min([float(np.min(v)) for v in arrays] + [m - sd for m, sd in summaries])
    y_bottom = low - 0.04 * y_top if low < 0 else 0.0

    # `p_adj` is the p-value that decides significance in every table: the
    # corrected value for t-tests and post-hoc rows, and equal to `p` wherever
    # there is nothing to correct (no correction, the ANOVA omnibus row).
    # Once post-hoc rows exist they say which strains differ, so the omnibus
    # row is not drawn as a bracket of its own.
    if statistical_test == "anova" and (tests["group2"] != "all strains").any():
        tests = tests[tests["group2"] != "all strains"]
    p_values = pd.to_numeric(tests["p_adj"], errors="coerce")
    annotated = tests.assign(label=p_values.map(stars), _p_value=p_values)
    significant = annotated["_p_value"].le(alpha)
    # Above the conventional 0.05 cutoff, a numeric label is more truthful
    # than inventing an extra star category for (for example) p=0.08.
    numeric = significant & annotated["label"].eq("ns")
    annotated.loc[numeric, "label"] = annotated.loc[numeric, "_p_value"].map(
        lambda p: f"p={p:.3g}")
    if not show_ns:
        annotated = annotated[significant]
    annotated = annotated.copy()
    # Bracket ends: the two strains compared, or the whole panel for the
    # omnibus ANOVA row.
    index = {name: i for i, name in enumerate(names)}
    omnibus = annotated["group2"] == "all strains"
    ends1 = annotated["group1"].map(index).where(~omnibus, 0)
    ends2 = annotated["group2"].map(index).where(~omnibus, len(names) - 1)
    annotated["x1"] = np.minimum(ends1, ends2)
    annotated["x2"] = np.maximum(ends1, ends2)
    annotated = annotated.dropna(subset=["x1", "x2"])
    annotated["tier"] = _stack_brackets(annotated["x1"].tolist(),
                                        annotated["x2"].tolist())

    # Grow the figure rather than squash the data. Width: a fixed pitch per
    # strain once the default width runs out (about 12 strains). Height: each
    # tier of significance brackets gets at least BRACKET_IN of height, so
    # past ~10 tiers the band above the axis grows instead of eating the data
    # area. Both leave the lab's 12-strain figures exactly as they were.
    n_bars = int(annotated["tier"].max()) if len(annotated) else 0
    if horizontal:
        # The same growth rule turned: strains set the height, and the
        # bracket tiers set how far the width grows past the data.
        height = max(height, STRAIN_PAD_IN + 0.4 + STRAIN_PITCH_IN * len(names))
        data_in = HORIZ_DATA_IN
        width = HORIZ_FURNITURE_IN + data_in
    else:
        width = max(width, STRAIN_PAD_IN + STRAIN_PITCH_IN * len(names))
        data_in = max(1.0, height - AXIS_FURNITURE_IN)
    step_fraction = (min(0.10, max(0.45 / n_bars, BRACKET_IN / data_in))
                     if n_bars else 0.0)
    growth = max(0.0, n_bars * step_fraction - 0.45) * data_in
    if horizontal:
        width += 0.45 * data_in + growth
    else:
        height += growth

    if horizontal:
        _draw_horizontal(arrays, summaries, names, annotated, n_bars,
                         step_fraction, y_bottom, y_top, y_label, title,
                         width, height, base_size, output_stem)
        return

    with ppp.prism_context(base_size=base_size, palette="colors"):
        fig, ax = plt.subplots(figsize=(width, height))
        for index, (values, (mean, sd)) in enumerate(zip(arrays, summaries)):
            jitter = (np.linspace(-0.085, 0.085, len(values))
                      if len(values) > 1 else np.array([0.0]))
            ax.errorbar(index, mean, yerr=sd, color="black", capsize=4,
                        elinewidth=1.1, capthick=1.1, fmt="none", zorder=1)
            ax.plot([index - 0.26, index + 0.26], [mean, mean], color="black",
                    linewidth=1.5, zorder=2)
            ax.scatter(index + jitter, values, s=28, color="black", zorder=3,
                       clip_on=False)

        ax.set_xticks(range(len(names)), names, rotation=45, ha="right",
                      fontstyle="italic")
        ax.set_ylabel(y_label)
        ax.set_xlabel(None)
        ax.set_xlim(-0.6, len(names) - 0.4)
        ax.set_ylim(y_bottom, y_top)
        ax.set_yticks(np.arange(0, y_top + 0.001, 0.5))
        ppp.style_axes(ax, axis_text_angle=45, line_width=1.0)
        for label in ax.get_xticklabels():
            label.set_fontstyle("italic")

        annotation_top = y_top
        if n_bars:
            span = y_top - y_bottom
            for row in annotated.itertuples():
                y = y_top + span * step_fraction * row.tier
                ppp.significance_bar(
                    ax, float(row.x1), float(row.x2), y, row.label,
                    # Star glyphs ride high in their line box, so the label
                    # sits almost on its bracket to read as that bracket's.
                    height=span * 0.012, text_offset=span * 0.002,
                    line_width=1.2, clip_on=False,
                )
                annotation_top = max(annotation_top, y + span * 0.055)
            # Include the annotations in the axes geometry so Matplotlib can
            # lay the figure out reliably, but stop the visible y-axis at its
            # clean final tick just as Prism does.
            ax.set_ylim(y_bottom, annotation_top)
            ax.spines["left"].set_bounds(y_bottom, y_top)
        if title:
            ax.set_title(title)
        fig.tight_layout()
        if n_bars:
            _center_ylabel_on_spine(fig, ax, y_bottom, y_top)
        output_stem.parent.mkdir(parents=True, exist_ok=True)
        bbox = _save_bbox(fig)
        fig.savefig(output_stem.with_suffix(".png"), dpi=GRAPH_DPI,
                    facecolor="white", bbox_inches=bbox,
                    metadata={GRAPH_KEY: _tick_map(fig, ax, names, y_bottom,
                                                   bbox)})
        fig.savefig(output_stem.with_suffix(".pdf"), facecolor="white")
        plt.close(fig)


def _draw_horizontal(arrays, summaries, names, annotated, n_bars,
                     step_fraction, y_bottom, y_top, y_label, title,
                     width, height, base_size, output_stem: Path) -> None:
    """The graph on its side, written as a PNG into `HORIZONTAL_DIR`."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import pyprism_plot as ppp

    n = len(names)
    with ppp.prism_context(base_size=base_size, palette="colors"):
        fig, ax = plt.subplots(figsize=(width, height))
        for index, (values, (mean, sd)) in enumerate(zip(arrays, summaries)):
            jitter = (np.linspace(-0.085, 0.085, len(values))
                      if len(values) > 1 else np.array([0.0]))
            ax.errorbar(mean, index, xerr=sd, color="black", capsize=4,
                        elinewidth=1.1, capthick=1.1, fmt="none", zorder=1)
            ax.plot([mean, mean], [index - 0.26, index + 0.26], color="black",
                    linewidth=1.5, zorder=2)
            ax.scatter(values, index + jitter, s=28, color="black", zorder=3,
                       clip_on=False)

        # Names stay horizontal -- the point of turning the graph on its side
        # is that they read at a glance -- first strain at the top.
        ax.set_yticks(range(n), names, fontstyle="italic")
        ax.set_xlabel(y_label)
        ax.set_ylabel(None)
        ax.set_ylim(n - 0.4, -0.6)
        ax.set_xlim(y_bottom, y_top)
        ax.set_xticks(np.arange(0, y_top + 0.001, 0.5))
        ppp.style_axes(ax, axis_text_angle=0, line_width=1.0)
        for label in ax.get_yticklabels():
            label.set_fontstyle("italic")

        annotation_right = y_top
        if n_bars:
            span = y_top - y_bottom
            for row in annotated.itertuples():
                x = y_top + span * step_fraction * row.tier
                h = span * 0.012
                ax.plot([x, x + h, x + h, x],
                        [float(row.x1), float(row.x1), float(row.x2),
                         float(row.x2)],
                        color="black", linewidth=1.2, clip_on=False)
                # Under the bracket's lower end, centred on its line: the
                # tiers sit a text-width apart at best, so a label beside
                # the line would run into the next bracket, and under the
                # end nothing else is in the way.
                ax.text(x + h / 2, float(row.x2) + 0.12, row.label,
                        ha="center", va="top", color="black", clip_on=False)
                annotation_right = max(annotation_right, x + span * 0.08)
            ax.set_xlim(y_bottom, annotation_right)
            ax.spines["bottom"].set_bounds(y_bottom, y_top)
        if title:
            ax.set_title(title)
        fig.tight_layout()
        if n_bars:
            _center_xlabel_on_spine(fig, ax, y_bottom, y_top)
        out = output_stem.parent / HORIZONTAL_DIR / output_stem.name
        out.parent.mkdir(parents=True, exist_ok=True)
        bbox = _save_bbox(fig)
        fig.savefig(out.with_suffix(".png"), dpi=GRAPH_DPI, facecolor="white",
                    bbox_inches=bbox,
                    metadata={GRAPH_KEY: _tick_map_horizontal(fig, ax, names,
                                                              bbox)})
        plt.close(fig)


def horizontal_of(graph_png) -> Path:
    """Where the horizontal twin of a graph PNG is written."""
    graph_png = Path(graph_png)
    return graph_png.parent / HORIZONTAL_DIR / graph_png.name


def draw(csv_path: Path, outdir: Path, *, control: str | None = None,
         value: str = "relative_growth", y_label: str = "Relative Growth",
         width: float = 5.2, height: float = 5.4, base_size: float = 12,
         statistical_test: str = "t_test", p_adjust: str = "none",
         alpha: float = 0.05,
         posthoc: str = "none", extra_references=(), all_pairs: bool = False,
         show_ns: bool = False,
         keep_artifacts: bool = False, keep_outliers: bool = False) -> list[Path]:
    """Draw every treatment in ``csv_path`` and write the combined test CSV.

    `posthoc` follows an ANOVA with that pairwise test (see POSTHOC_METHODS).
    `extra_references` and `all_pairs` widen the comparisons beyond every
    strain against the control -- see `comparison_pairs`; they apply to the
    t-tests and to the post-hoc test alike. References not in a treatment's
    panel are ignored for that treatment.
    """
    if statistical_test not in {"t_test", "anova"}:
        raise ValueError("statistical_test must be 't_test' or 'anova'")
    if p_adjust not in P_ADJUST_METHODS:
        raise ValueError(
            "p_adjust must be 'none', 'holm', 'bonferroni', 'sidak', or 'BH'")
    check_posthoc(statistical_test, posthoc, all_pairs)
    if not 0 < float(alpha) < 1:
        raise ValueError("alpha must be between 0 and 1")
    csv_path, outdir = Path(csv_path).resolve(), Path(outdir).resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    data = _filtered(pd.read_csv(csv_path, encoding="utf-8-sig"), value,
                     keep_artifacts=keep_artifacts,
                     keep_outliers=keep_outliers)
    treatments = list(dict.fromkeys(data["treatment"].tolist()))
    made, all_tests = [], []
    for treatment in treatments:
        group = data[data["treatment"] == treatment].copy()
        if group.empty:
            continue
        control_name = _control_for(group, control)
        if control_name not in set(group["strain"]):
            print(f"  ! control {control_name!r} not present in {treatment!r}; skipped")
            continue
        pairs = comparison_pairs(_ordered_strains(group, control_name),
                                 control_name, extra_references, all_pairs)
        tests = (_ratio_tests(group, control_name, p_adjust, pairs)
                 if statistical_test == "t_test"
                 else _anova_test(group, control_name, posthoc, pairs))
        tests.insert(0, "treatment", treatment)
        all_tests.append(tests)
        stem = outdir / f"spotting_{safe_name(treatment)}"
        for horizontal in (False, True):
            _draw_one(group, tests, control_name, stem,
                      title=treatment if len(treatments) > 1 else None,
                      y_label=y_label, width=width, height=height,
                      base_size=base_size, statistical_test=statistical_test,
                      p_adjust=p_adjust, alpha=float(alpha), show_ns=show_ns,
                      horizontal=horizontal)
        made.append(stem.with_suffix(".png"))
        print(f"  wrote {stem.with_suffix('.png')} / {stem.with_suffix('.pdf')}")

    stats_path = outdir / ("spotting_paired_ttests.csv"
                           if statistical_test == "t_test"
                           else "spotting_anova.csv")
    obsolete = outdir / ("spotting_anova.csv"
                         if statistical_test == "t_test"
                         else "spotting_paired_ttests.csv")
    if obsolete.exists():
        obsolete.unlink()
    if all_tests:
        pd.concat(all_tests, ignore_index=True).to_csv(
            stats_path, index=False, encoding="utf-8-sig")
        print(f"  wrote {stats_path}")
    elif stats_path.exists():
        stats_path.unlink()
    return made
