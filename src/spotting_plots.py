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
P_ADJUST_METHODS = {
    "none": None,
    "holm": "holm",
    "bonferroni": "bonferroni",
    "sidak": "sidak",
    "BH": "fdr_bh",
    "bh": "fdr_bh",
}


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


def _ratio_tests(group: pd.DataFrame, control: str,
                 p_adjust: str) -> pd.DataFrame:
    """Log-ratio one-sample tests, matching the former R implementation."""
    from scipy import stats

    rows = []
    for strain in _ordered_strains(group, control):
        if strain == control:
            continue
        sub = group[group["strain"] == strain]
        values = sub["value"].to_numpy(dtype=float)
        finite = np.isfinite(values)
        values = values[finite]
        if "control_raw" in sub:
            controls = pd.to_numeric(sub["control_raw"], errors="coerce").to_numpy()[finite]
            limits = DETECT_GRAY / controls
            valid_limits = np.isfinite(limits)
            values[valid_limits] = np.maximum(values[valid_limits], limits[valid_limits])
        n_ok = len(values)
        mean_ratio = (float(np.exp(np.mean(np.log(np.maximum(values, 1e-9)))))
                      if n_ok else np.nan)
        p_value = np.nan
        if n_ok >= 2 and np.all(np.isfinite(values)) and np.all(values > 0):
            p_value = float(stats.ttest_1samp(np.log(values), 0).pvalue)
        rows.append({"group1": control, "group2": strain, "n": n_ok,
                     "mean_ratio": mean_ratio, "p": p_value})

    result = pd.DataFrame(rows, columns=["group1", "group2", "n",
                                         "mean_ratio", "p"])
    result["p_adj"] = result["p"]
    finite = result["p"].notna()
    if p_adjust not in P_ADJUST_METHODS:
        raise ValueError(
            "p_adjust must be 'none', 'holm', 'bonferroni', 'sidak', or 'BH'")
    if P_ADJUST_METHODS[p_adjust] is not None and finite.any():
        from statsmodels.stats.multitest import multipletests

        result.loc[finite, "p_adj"] = multipletests(
            result.loc[finite, "p"], method=P_ADJUST_METHODS[p_adjust])[1]
    return result


def _anova_test(group: pd.DataFrame, control: str) -> pd.DataFrame:
    """One-way ANOVA of log relative growth across all strain groups.

    Relative growth is a ratio, so the log transform puts equal fold changes
    above and below one on the same scale, matching the t-test path.  ANOVA is
    an omnibus test: it answers whether any strain mean differs and deliberately
    does not imply which strain is responsible.
    """
    from scipy import stats

    names = _ordered_strains(group, control)
    arrays, n_total = [], 0
    for name in names:
        sub = group[group["strain"] == name]
        values = sub["value"].to_numpy(dtype=float)
        finite = np.isfinite(values)
        values = values[finite]
        if "control_raw" in sub:
            controls = pd.to_numeric(
                sub["control_raw"], errors="coerce").to_numpy()[finite]
            limits = DETECT_GRAY / controls
            valid_limits = np.isfinite(limits)
            values[valid_limits] = np.maximum(
                values[valid_limits], limits[valid_limits])
        values = values[values > 0]
        if len(values):
            arrays.append(np.log(values))
            n_total += len(values)
    p_value, statistic = np.nan, np.nan
    if len(arrays) >= 2 and all(len(values) >= 2 for values in arrays):
        result = stats.f_oneway(*arrays)
        statistic, p_value = float(result.statistic), float(result.pvalue)
    return pd.DataFrame([{
        "test": "one-way ANOVA (log relative growth)",
        "group1": control,
        "group2": "all strains",
        "n_groups": len(arrays),
        "n": n_total,
        "f": statistic,
        "p": p_value,
        "p_adj": p_value,
    }])


def _mean_sd(values: np.ndarray) -> tuple[float, float]:
    mean = float(np.mean(values))
    sd = float(np.std(values, ddof=1)) if len(values) > 1 else 0.0
    return mean, sd


def _draw_one(group: pd.DataFrame, tests: pd.DataFrame, control: str,
              output_stem: Path, *, title: str | None, y_label: str,
              width: float, height: float, base_size: float,
              statistical_test: str, p_adjust: str, alpha: float,
              show_ns: bool) -> None:
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

    p_col = "p" if p_adjust == "none" or statistical_test == "anova" else "p_adj"
    p_values = pd.to_numeric(tests[p_col], errors="coerce")
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
    if statistical_test == "t_test":
        annotated["x"] = annotated["group2"].map(
            {name: i for i, name in enumerate(names)})
        annotated = annotated.dropna(subset=["x"]).sort_values("x")

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

        n_bars = len(annotated)
        annotation_top = y_top
        if n_bars:
            step_fraction = min(0.10, 0.45 / n_bars)
            span = y_top - y_bottom
            for level, row in enumerate(annotated.itertuples(), 1):
                y = y_top + span * step_fraction * level
                x1 = 0
                x2 = (float(row.x) if statistical_test == "t_test"
                      else float(len(names) - 1))
                ppp.significance_bar(
                    ax, x1, x2, y, row.label,
                    height=span * 0.012, text_offset=span * 0.012,
                    line_width=1.2, clip_on=False,
                )
                annotation_top = y + span * 0.055
            # Include the annotations in the axes geometry so Matplotlib can
            # lay the figure out reliably, but stop the visible y-axis at its
            # clean final tick just as Prism does.
            ax.set_ylim(y_bottom, annotation_top)
            ax.spines["left"].set_bounds(y_bottom, y_top)
        if title:
            ax.set_title(title)
        fig.tight_layout()
        output_stem.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(output_stem.with_suffix(".png"), dpi=400, facecolor="white")
        fig.savefig(output_stem.with_suffix(".pdf"), facecolor="white")
        plt.close(fig)


def draw(csv_path: Path, outdir: Path, *, control: str | None = None,
         value: str = "relative_growth", y_label: str = "Relative Growth",
         width: float = 5.2, height: float = 5.4, base_size: float = 12,
         statistical_test: str = "t_test", p_adjust: str = "none",
         alpha: float = 0.05,
         show_ns: bool = False,
         keep_artifacts: bool = False, keep_outliers: bool = False) -> list[Path]:
    """Draw every treatment in ``csv_path`` and write the combined test CSV."""
    if statistical_test not in {"t_test", "anova"}:
        raise ValueError("statistical_test must be 't_test' or 'anova'")
    if p_adjust not in P_ADJUST_METHODS:
        raise ValueError(
            "p_adjust must be 'none', 'holm', 'bonferroni', 'sidak', or 'BH'")
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
        tests = (_ratio_tests(group, control_name, p_adjust)
                 if statistical_test == "t_test"
                 else _anova_test(group, control_name))
        tests.insert(0, "treatment", treatment)
        all_tests.append(tests)
        stem = outdir / f"spotting_{safe_name(treatment)}"
        _draw_one(group, tests, control_name, stem,
                  title=treatment if len(treatments) > 1 else None,
                  y_label=y_label, width=width, height=height,
                  base_size=base_size, statistical_test=statistical_test,
                  p_adjust=p_adjust, alpha=float(alpha), show_ns=show_ns)
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
