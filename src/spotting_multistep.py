"""Optional technical-plate and repeated-measures analysis.

Analyses are per strain versus its control, conditional on the selected hours
and dilution. Biological block IDs are matched between strain and control;
different strains are NOT pooled as interchangeable biological observations.
Full hierarchy: block, physical plate, repeated spot pair, plate x time, and
(with >=3 times) an independent block time slope. Two-step: technical mean
per block/time, followed by t inference or a repeated-measures mixed model.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import warnings

import numpy as np
import pandas as pd
from scipy import stats

TEST_COLUMNS = ["condition", "strain", "test", "hours", "log_effect", "se", "df",
                "ratio", "ci_low", "ci_high", "statistic", "p", "method", "scope",
                "biological_n", "observation_n", "biological_n_total", "inference",
                "status", "p_holm", "family_size"]


@dataclass
class AnalysisResult:
    observations: pd.DataFrame
    contrasts: pd.DataFrame
    biological_means: pd.DataFrame
    estimates: pd.DataFrame
    tests: pd.DataFrame
    diagnostics: list[dict]


def _identity(*values):
    return json.dumps(values, ensure_ascii=False)


def prepare_contrasts(observations):
    """Keep every raw observation and record why a paired contrast is unusable.

    No numerical outlier rejection. Nonpositive/below-limit target growth is
    retained as censored, displayed at the detection limit, and never produces
    a Gaussian-model p-value. Failed controls cannot define a ratio.
    """
    raw = observations.copy()
    needed = {"condition", "strain", "strain_col", "replicate", "physical_plate",
              "hours", "raw_growth", "detection_limit", "is_control", "qc_reason"}
    if needed - set(raw):
        raise ValueError(f"Missing multi-step columns: {sorted(needed - set(raw))}")
    key = ["condition", "physical_plate", "replicate", "hours"]
    if raw.duplicated(key + ["strain_col"]).any():
        raise ValueError("Duplicate spot observation: physical plate/time/biological block must be unique")
    control = raw[raw.is_control].copy()
    if control.duplicated(key).any():
        raise ValueError("More than one control spot in a biological block")
    ctrl = control[key + ["raw_growth", "detection_limit", "qc_reason", "strain"]].rename(
        columns={"raw_growth": "control_growth", "detection_limit": "control_limit",
                 "qc_reason": "control_qc", "strain": "control_strain"})
    pairs = raw[~raw.is_control].merge(ctrl, on=key, how="left", validate="many_to_one")
    pairs["qc_reason"] = pairs.qc_reason.fillna("")
    for i, row in pairs.iterrows():
        reasons = [row.qc_reason] if row.qc_reason else []
        if pd.isna(row.control_strain):
            reasons.append("missing matched control")
        elif pd.notna(row.control_qc) and row.control_qc:
            reasons.append("matched control: " + str(row.control_qc))
        elif not np.isfinite(row.control_growth) or row.control_growth <= row.control_limit:
            reasons.append("matched control below quantification limit")
        if not np.isfinite(row.raw_growth):
            reasons.append("nonfinite growth measurement")
        if not np.isfinite(row.detection_limit) or row.detection_limit <= 0:
            reasons.append("invalid detection limit")
        pairs.at[i, "qc_reason"] = "; ".join(dict.fromkeys(reasons))
    pairs["included"] = pairs.qc_reason.eq("")
    pairs["censored"] = pairs.raw_growth <= pairs.detection_limit
    pairs["log_ratio"] = np.nan
    ok = pairs.included
    pairs.loc[ok, "log_ratio"] = np.log(np.maximum(
        pairs.loc[ok, "raw_growth"], pairs.loc[ok, "detection_limit"])) - np.log(pairs.loc[ok, "control_growth"])
    pairs["time"] = pairs.hours.map(lambda x: f"{x:g}")
    pairs["block"] = pairs.replicate.astype(str)
    pairs["spot_pair"] = [_identity(p, b) for p, b in zip(pairs.physical_plate, pairs.block)]
    pairs["plate_time"] = [_identity(p, t) for p, t in zip(pairs.physical_plate, pairs.time)]
    return raw, pairs


def _vc_formulas(data, full, repeated, diagnostic):
    """Remove aliased covariance terms, which cannot be estimated separately.

    For example a plate with one biological block has the same repeated-spot
    covariance as its plate covariance. Their sum, rather than two separate
    variances, is identifiable. Identity covariance is already residual error.
    """
    columns = ["block"] + (["physical_plate"] if full else [])
    if repeated and full:
        columns += ["spot_pair", "plate_time"]
    bases = [np.eye(len(data)).reshape(-1)]
    formulas = {}
    aliases = []
    for col in columns:
        ids = data[col].to_numpy()
        basis = (ids[:, None] == ids[None, :]).astype(float).reshape(-1)
        old = np.column_stack(bases)
        if np.linalg.matrix_rank(np.column_stack([old, basis])) == np.linalg.matrix_rank(old):
            aliases.append(col)
        else:
            formulas[col] = f"0 + C({col})"
            bases.append(basis)
    if repeated and data.hours.nunique() >= 3:
        t = data.scaled_time.to_numpy()
        b = data.block.to_numpy()
        basis = ((b[:, None] == b[None, :]) * np.outer(t, t)).reshape(-1)
        old = np.column_stack(bases)
        if np.linalg.matrix_rank(np.column_stack([old, basis])) > np.linalg.matrix_rank(old):
            formulas["block_time_slope"] = "0 + C(block):scaled_time"
    diagnostic["covariance_terms"] = list(formulas)
    diagnostic["aliased_terms_merged_with_other_variance"] = aliases
    return formulas


def _fit(data, full, repeated, diagnostic):
    import statsmodels.formula.api as smf
    data = data.copy()
    span = max(float(data.hours.max() - data.hours.min()), 1)
    data["scaled_time"] = (data.hours - data.hours.mean()) / span
    vc = _vc_formulas(data, full, repeated, diagnostic)
    if "block" not in vc:
        raise ValueError("Insufficient repeated observations to estimate biological variation separately")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        model = smf.mixedlm("log_ratio ~ 0 + C(time)", data,
                            groups=np.ones(len(data)), re_formula="0", vc_formula=vc)
        fit = model.fit(reml=True, method=["lbfgs", "powell"], maxiter=500, disp=False)
    diagnostic["warnings"] = list(dict.fromkeys(str(w.message) for w in caught))
    diagnostic["converged"] = bool(fit.converged)
    diagnostic["variance_components"] = dict(zip(model.exog_vc.names, map(float, fit.vcomp)))
    diagnostic["residual_variance"] = float(fit.scale)
    if not fit.converged:
        raise ValueError("Mixed model did not converge")
    if any("not positive definite" in str(w.message).lower() for w in caught):
        raise ValueError("Mixed-model Hessian is not positive definite; inference withheld")
    means = np.asarray(fit.fe_params)
    cov = np.asarray(fit.cov_params())[:len(means), :len(means)]
    if (not np.isfinite(means).all() or not np.isfinite(cov).all()
            or np.linalg.eigvalsh(cov).min() <= 0):
        raise ValueError("Mixed model has invalid fixed-effect covariance")
    # Exog rows come from the fitted design, never reconstruct categorical names.
    times = sorted(data.hours.unique())
    design = np.vstack([model.exog[np.flatnonzero(data.hours.to_numpy() == h)[0]] for h in times])
    return times, design @ means, design @ cov @ design.T


def _test_record(condition, strain, kind, hours, estimate, se, alpha, df=np.inf):
    reference = stats.norm if np.isinf(df) else stats.t(df)
    critical = float(reference.ppf(1 - alpha / 2))
    valid = np.isfinite(se) and se > 0
    return {"condition": condition, "strain": strain, "test": kind, "hours": hours,
            "log_effect": float(estimate), "se": float(se), "df": df,
            "ratio": float(np.exp(estimate)),
            "ci_low": float(np.exp(estimate - critical * se)) if valid else np.nan,
            "ci_high": float(np.exp(estimate + critical * se)) if valid else np.nan,
            "p": float(2 * reference.sf(abs(estimate / se))) if valid else np.nan}


def analyze(observations, settings, alpha=0.05):
    raw, contrasts = prepare_contrasts(observations)
    clean = contrasts[contrasts.included].copy()
    keys = ["condition", "strain", "replicate", "hours"]
    means = clean.groupby(keys, as_index=False).agg(
        log_ratio=("log_ratio", "mean"), technical_n=("physical_plate", "nunique"),
        censored=("censored", "any"))
    estimates, tests, diagnostics = [], [], []
    repeated = settings.scope == "repeated"
    full = settings.method == "hierarchical"
    for (condition, strain), all_rows in contrasts.groupby(["condition", "strain"], sort=False):
        data = all_rows[all_rows.included].copy()
        bio = means[(means.condition == condition) & (means.strain == strain)].copy()
        diagnostic = {"condition": condition, "strain": strain, "method": settings.method,
                      "scope": settings.scope, "biological_n": int(data.replicate.nunique()),
                      "observation_n": len(data), "excluded_n": int((~all_rows.included).sum()),
                      "censored_n": int(data.censored.sum()), "status": "ok",
                      "inference": "approximate normal/Wald" if full or repeated else "biological-block t test"}
        local_estimates, local_tests = [], []
        try:
            # Only hours at which this condition was photographed count.
            if any(bio[bio.hours == h].replicate.nunique() < 3 for h in bio.hours.unique()):
                raise ValueError("At least three usable biological blocks are required at each hour with photos")
            if data.censored.any():
                raise ValueError("Below-limit target growth retained for display; Gaussian-model inference withheld")
            if (full or repeated) and len(bio.replicate.unique()) < 6:
                diagnostic["small_sample_note"] = "Few biological blocks: mixed-model Wald inference is approximate and may be poorly calibrated."
            if full and not (bio.technical_n >= 2).any():
                raise ValueError("Full hierarchical analysis needs replicated technical plates within a biological block")
            if not repeated and not full:
                y = bio.log_ratio.to_numpy()
                effect, se = float(y.mean()), float(y.std(ddof=1) / np.sqrt(len(y)))
                if se <= 1e-12:
                    raise ValueError("No estimable between-biological-block variance")
                local_estimates.append(_test_record(condition, strain, "timepoint", float(bio.hours.iloc[0]), effect, se, alpha, len(y) - 1))
            else:
                if not full:
                    data = bio.copy()
                    data["block"] = data.replicate.astype(str)
                    data["time"] = data.hours.map(lambda x: f"{x:g}")
                if repeated and not (data.groupby("replicate").hours.nunique() > 1).any():
                    raise ValueError("No biological block has usable repeated measurements")
                hours, effects, cov = _fit(data, full, repeated, diagnostic)
                for j, h in enumerate(hours):
                    local_estimates.append(_test_record(condition, strain, "timepoint", h, effects[j], np.sqrt(cov[j, j]), alpha))
                if repeated:
                    weights = np.ones(len(hours)) / len(hours)
                    local_tests.append(_test_record(condition, strain, "equal-time mean", np.nan,
                                                   weights @ effects, np.sqrt(weights @ cov @ weights), alpha))
                    # Joint deviation from control, and change in the contrast over time.
                    for name, matrix in [("trajectory vs control", np.eye(len(hours))),
                                         ("strain-control difference changes with time", np.eye(len(hours))[1:] - np.eye(len(hours))[0])]:
                        delta, vcov = matrix @ effects, matrix @ cov @ matrix.T
                        chi2 = float(delta @ np.linalg.solve(vcov, delta))
                        local_tests.append({"condition": condition, "strain": strain,
                                            "test": name, "hours": np.nan, "df": len(delta),
                                            "statistic": chi2, "p": float(stats.chi2.sf(chi2, len(delta)))})
        except (ValueError, np.linalg.LinAlgError, RuntimeError) as exc:
            diagnostic["status"] = "descriptive only"
            diagnostic["reason"] = str(exc)
            local_tests = []
            local_estimates = []
            for h in settings.hours:
                y = bio.loc[bio.hours == h, "log_ratio"]
                if len(y):
                    local_estimates.append(_test_record(condition, strain, "timepoint", h, y.mean(), np.nan, alpha))
        for record in local_estimates + local_tests:
            observed = all_rows[all_rows.included]
            if record["test"] == "timepoint":
                observed = observed[observed.hours == record["hours"]]
            record.update(method=settings.method, scope=settings.scope,
                          biological_n=int(observed.replicate.nunique()), observation_n=len(observed),
                          biological_n_total=int(all_rows[all_rows.included].replicate.nunique()),
                          inference=diagnostic["inference"], status=diagnostic["status"])
        estimates.extend(local_estimates)
        tests.extend(local_estimates + local_tests)
        diagnostics.append(diagnostic)
    tests = pd.DataFrame(tests).reindex(columns=TEST_COLUMNS)
    if not tests.empty:
        from statsmodels.stats.multitest import multipletests
        tests["p_holm"] = np.nan
        tests["family_size"] = 0
        for _, group in tests.groupby("condition"):
            valid = group.p.notna()
            if valid.any():
                index = group.index[valid]
                tests.loc[index, "p_holm"] = multipletests(group.loc[index, "p"], method="holm")[1]
                tests.loc[group.index, "family_size"] = int(valid.sum())
    estimates = pd.DataFrame(estimates).reindex(columns=TEST_COLUMNS)
    if not estimates.empty and not tests.empty:
        estimates = tests[tests.test == "timepoint"].copy()
    return AnalysisResult(raw, contrasts, means, estimates, tests, diagnostics)


def write_results(result, outdir, settings, alpha=0.05, baseline=None):
    """Write a new run folder, preserving previous comparison analyses."""
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=False)
    for name, frame in (("observations", result.observations), ("paired_contrasts", result.contrasts),
                        ("biological_means", result.biological_means), ("estimates", result.estimates), ("tests", result.tests)):
        frame.to_csv(outdir / f"{name}.csv", index=False, encoding="utf-8-sig")
    from dataclasses import asdict
    metadata = {"settings": asdict(settings), "alpha": alpha,
                "normalization": "log(target / control) within the same template biological block, physical plate and time",
                "multiple_testing": "Holm across all estimable strain/timepoint and trajectory tests within each condition",
                "intervals": "pointwise; not simultaneous",
                "time": "categorical fixed effects; equal weighting of selected hours for the mean contrast",
                "diagnostics": result.diagnostics}
    (outdir / "analysis.json").write_text(json.dumps(metadata, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    _draw(result, outdir, settings, baseline, alpha)
    return outdir


def _draw(result, outdir, settings, baseline, alpha):
    # Figure/Agg avoids global pyplot state and works in background CLI runs.
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    import hashlib
    import re

    for condition, observations in result.observations.groupby("condition", sort=False):
        strains = list(dict.fromkeys(observations.loc[~observations.is_control, "strain"]))
        if not strains:
            continue
        cols = min(3, len(strains))
        rows = (len(strains) + cols - 1) // cols
        fig = Figure(figsize=(5 * cols, 3.7 * rows + 1.2), facecolor="white")
        FigureCanvasAgg(fig)
        axes = fig.subplots(rows, cols, squeeze=False).ravel()
        for ax, strain in zip(axes, strains):
            bio = result.biological_means
            bio = bio[(bio.condition == condition) & (bio.strain == strain)]
            est = result.estimates
            est = est[(est.condition == condition) & (est.strain == strain)] if not est.empty else est
            for _, group in bio.groupby("replicate"):
                group = group.sort_values("hours")
                ax.plot(group.hours, np.exp(group.log_ratio), "o-", color="#94a3b8", alpha=.6, lw=1, ms=4)
            if not est.empty:
                est = est.sort_values("hours")
                ax.plot(est.hours, est.ratio, "o-", color="#087e8b", lw=2, label="Additional analysis")
                ok = est.ci_low.notna() & est.ci_high.notna()
                if ok.any():
                    sub = est[ok]
                    ax.errorbar(sub.hours, sub.ratio,
                                yerr=[sub.ratio - sub.ci_low, sub.ci_high - sub.ratio],
                                fmt="none", ecolor="#087e8b", capsize=4)
            if baseline is not None and not baseline.empty and {"treatment", "strain", "relative_growth", "hours"} <= set(baseline):
                old = baseline[(baseline.treatment == condition) & (baseline.strain == strain)]
                for flag in ("artifact", "excluded", "outlier"):
                    if flag in old:
                        old = old[~old[flag].fillna(False).astype(bool)]
                for h, group in old.groupby("hours"):
                    values = group.relative_growth.dropna()
                    if len(values):
                        ax.scatter([h], [values.mean()], marker="D", facecolors="none", edgecolors="#c26d24", s=60, label="Current endpoint (mean)")
            ax.axhline(1, color="#475569", linestyle="--", lw=1)
            diagnostic = next(d for d in result.diagnostics if d["condition"] == condition and d["strain"] == strain)
            note = ("Descriptive only: " + diagnostic.get("reason", "")
                    if diagnostic["status"] != "ok" else f"Biological n={diagnostic['biological_n']}; {diagnostic['inference']}")
            if not est.empty and est.p_holm.notna().any() and len(est) <= 4:
                note += "\n" + "; ".join(f"{r.hours:g} h: Holm p={r.p_holm:.3g}" for r in est.itertuples() if np.isfinite(r.p_holm))
            import textwrap
            ax.set_title(strain, fontsize=12, fontweight="bold")
            ax.text(.02, .98, "\n".join(textwrap.fill(line, 58) for line in note.splitlines()), transform=ax.transAxes, va="top", fontsize=7,
                    bbox={"facecolor": "white", "alpha": .85, "edgecolor": "none"})
            ax.set_xlabel("Hours since spotting")
            ticks = set(settings.hours)
            if baseline is not None and not baseline.empty and {"treatment", "hours"} <= set(baseline):
                ticks.update(baseline.loc[baseline.treatment == condition, "hours"].dropna())
            ax.set_xticks(sorted(ticks))
            ax.set_ylabel("Growth / matched control (log scale)")
            ax.set_yscale("log")
            ax.spines[["top", "right"]].set_visible(False)
            ax.margins(y=.4, x=.15)
            handles, labels = ax.get_legend_handles_labels()
            if handles:
                unique = dict(zip(labels, handles))
                ax.legend(unique.values(), unique.keys(), fontsize=7, loc="lower left")
        for ax in axes[len(strains):]:
            ax.set_visible(False)
        title = (f"{condition} | {settings.method.replace('_', ' ')} | "
                 f"{'technical plates + repeated timepoints' if settings.scope == 'repeated' else 'technical plates'}")
        fig.suptitle(textwrap.fill(title, max(38, int(fig.get_figwidth() * 9))), fontsize=12)
        footer = (f"Grey: biological-block technical means. Teal: estimate and {100 * (1-alpha):g}% pointwise CI. "
                  "Orange: current endpoint with its existing plate-mean normalization and selected dilution. "
                  "Additional analysis uses matched controls. Mixed-model p-values are approximate; see analysis.json and tests.csv.")
        fig.text(.02, .02, textwrap.fill(footer, max(60, int(fig.get_figwidth() * 15))), fontsize=8)
        fig.tight_layout(rect=(0, .17 if rows == 1 else .10, 1, .90))
        safe = re.sub(r"[^A-Za-z0-9_-]", "_", str(condition))[:60]
        name = f"{safe}-{hashlib.sha256(str(condition).encode()).hexdigest()[:8]}-comparison"
        fig.savefig(outdir / f"{name}.png", dpi=160)
        fig.savefig(outdir / f"{name}.svg")
