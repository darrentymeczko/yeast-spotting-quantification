"""Compare the Python replacement against ImageJ, using real full-size plates.

Reference-only tool: Java and ij.jar are never needed by the application.
Run with --java PATH --imagej PATH --out PATH IMAGE [IMAGE ...].
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pickle
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import tifffile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import spotting_quant as sq
from spotting_background import subtract_background


def reference(java, jar, source, output, radius):
    macro = output.with_suffix(".ijm")
    def quote(path):
        return json.dumps(str(Path(path).resolve()).replace("\\", "/"))
    macro.write_text(
        'setBatchMode(true);\n'
        f'open({quote(source)});\nrun("8-bit");\nrun("32-bit");\n'
        f'run("Subtract Background...", "rolling={radius:.0f} sliding");\n'
        f'saveAs("Tiff", {quote(output)});\n'
        'print("ImageJ version: " + getVersion());\nclose();\n', encoding="utf-8")
    output.unlink(missing_ok=True)  # a failed run must never reuse an old baseline
    result = subprocess.run(
        [str(java), "-Xmx2g", "-cp", str(jar),
         "ij.ImageJ", "-batch", str(macro.resolve())],
        capture_output=True, text=True, timeout=600,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode or not output.exists():
        raise RuntimeError(result.stdout + result.stderr)
    return tifffile.imread(output), result.stdout.strip()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--java", type=Path, required=True)
    ap.add_argument("--imagej", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("images", type=Path, nargs="+")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    metrics, spots = [], []
    for image_index, path in enumerate(args.images):
        print(f"Detecting {path.name}", flush=True)
        # Only our own, local comparison artifacts are loaded here.
        detection_file = args.out / (path.stem + ".detection.pkl")
        if detection_file.exists():
            with detection_file.open("rb") as fh:
                detection = pickle.load(fh)
        else:
            detection = sq.detect_for_measure(path, sq.MeasureOptions())
            with detection_file.open("wb") as fh:
                pickle.dump(detection, fh)
        radius = detection["ball_radius"]
        print(f"ImageJ baseline, radius {radius:.0f}", flush=True)
        start = time.perf_counter()
        baseline, version = reference(args.java, args.imagej, path,
                                       args.out / (path.stem + ".imagej.tif"), radius)
        reference_seconds = time.perf_counter()-start
        print("Python replacement", flush=True)
        start = time.perf_counter()
        actual = subtract_background(detection["img8"], radius)
        python_seconds = time.perf_counter()-start
        tifffile.imwrite(args.out / (path.stem + ".python.tif"), actual)
        difference = actual.astype(float)-baseline
        pixel_mae = float(np.abs(difference).mean())
        pixel_max = float(np.abs(difference).max())
        # Use exactly the production, dilution-specific ROI geometry in both arms.
        rowsets = [(1, 4), (2, 5), (3, 6)]
        before = sq.analyze_image_multi(path, sq.MeasureOptions(), rowsets,
                                       proc=baseline.astype(np.float64), detection=detection)
        after = sq.analyze_image_multi(path, sq.MeasureOptions(), rowsets,
                                      proc=actual.astype(np.float64), detection=detection)
        errors = []
        for rows in rowsets:
            g, b = before[rows]
            _, a = after[rows]
            for replicate_index, row in enumerate(rows):
                for col in range(8):
                    errors.append(abs(a.net[row-1, col]-b.net[row-1, col]))
                    spots.append(dict(image=path.name, dilution=rows[0], row=row,
                                      strain_col=col+1, strain=f"column {col+1}",
                                      replicate=f"rep{2*image_index+replicate_index+1}",
                                      experiment=f"dilution {rows[0]}",
                                      before_raw=b.net[row-1, col], after_raw=a.net[row-1, col],
                                      before_bg=b.bg_mean, after_bg=a.bg_mean,
                                      artifact=bool(b.rim_flag[row-1, col])))
        metrics.append(dict(image=path.name, sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                            shape=list(actual.shape), radius=radius, reference=version,
                            pixel_mae=pixel_mae, pixel_max=pixel_max,
                            net_growth_max_abs=max(errors),
                            reference_seconds=reference_seconds, python_seconds=python_seconds))
        print(json.dumps(metrics[-1], indent=2), flush=True)
    table = pd.DataFrame(spots)
    # Four biological replicates per dilution for the included plate pair.
    for side in ("before", "after"):
        tidy = table.copy()
        tidy["raw_growth"] = tidy[f"{side}_raw"]
        tidy["excluded"] = False
        normalized = sq.add_relative_growth(tidy, control_col=1,
                                            group_keys=["experiment"])
        normalized = sq.flag_outliers(normalized, group_keys=["experiment", "strain"],
                                      verbose=False)
        table[f"{side}_relative"] = normalized["relative_growth"]
        table[f"{side}_outlier"] = normalized["outlier"]
    delta = (table.after_relative-table.before_relative).abs()
    summary = dict(images=metrics, measured_spots=len(table),
                   relative_growth_max_abs=float(delta.max()),
                   relative_growth_mae=float(delta.mean()),
                   outlier_flags_changed=int((table.before_outlier != table.after_outlier).sum()),
                   tolerances=dict(net_growth_max_abs=0.01, relative_growth_max_abs=0.001,
                                   outlier_flags_changed=0))
    summary["passed"] = (all(m["net_growth_max_abs"] <= 0.01 for m in metrics)
                         and summary["relative_growth_max_abs"] <= 0.001
                         and summary["outlier_flags_changed"] == 0)
    table.to_csv(args.out / "spot_comparison.csv", index=False)
    (args.out / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2), flush=True)
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
