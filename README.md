# Spotting Assay Quantification

Automated quantification of yeast serial-dilution spotting assays, from plate
photographs to publication figures and statistics.

The method follows the grey-value protocol of Petropavlovskiy et al.,
*STAR Protocols* **1**:100182 (2020) — 8-bit conversion, sliding-paraboloid
background subtraction, a circular ROI over each spot, and normalisation to a
positive control. It replaces the manual ImageJ → Excel → Prism workflow, which
takes hours per experiment and places ~48 regions of interest per plate by hand.

Everything a run produces is reproducible from the photographs plus two small
JSON files, and every decision the program makes about a spot is recorded rather
than applied silently.

---

## What is in here

| | |
|---|---|
| `run_spotting.bat` | **Main pipeline.** A folder of chosen photos → measurements, statistics, figures. |
| `run_timecourse.bat` | **Selection pipeline.** A raw capture tree → which photos are worth quantifying. |
| `src/spotting_quant.py` | Measurement engine: plate finding, grid detection, ROI placement and sizing, background subtraction, normalisation, statistics. |
| `src/spotting_batch.py` | Driver for the main pipeline: discovery, prompts, caching, exports. |
| `src/spotting_montage.py` | Figure of the spots themselves, one block per biological replicate. |
| `src/spotting_pptx.py` | Slide deck pairing each montage with its graph. |
| `src/spotting_timecourse.py` | Scores every timepoint × photo pairing × dilution and ranks them. |
| `src/spotting_timecourse_figures.py` | Turns each of those candidates into a comparison sheet: marked spots beside their graph. |
| `src/plot_spotting.R` | Prism-style dot plots and the significance tests. |
| `tests/` | Calibration harness and the hand-measured ground truth. |

---

## Requirements

**Python 3.11+**

```
pip install -r requirements.txt
```

**FIJI / ImageJ** — used for background subtraction, called headlessly. The code
looks for it in the usual install locations (e.g.
`C:\Program Files\Fiji.app\ImageJ-win64.exe`). If it is not found the run falls
back to an equivalent implementation in `src/spotting_quant.py`, which is
analytically correct but flattens the background slightly less well
(measured: background spread 0.32 vs FIJI's 0.08 on the same plate).

**R** — only needed for the figures and statistics:

```r
install.packages(c("ggplot2", "ggprism", "dplyr", "tidyr", "readr"))
```

Without R the run still writes every CSV; it just draws no graphs.

---

## Pipeline 1 — quantify chosen photos

Put the photographs in a folder and name them so the program can tell what they
are:

```
<set>.<plate><TREATMENT>.JPG        e.g.  4.1GLU.JPG, 7.2K-OAc.JPG
```

- **set** — which strain panel is on the plate
- **plate** — `1` carries biological replicates 1 and 2, `2` carries 3 and 4
- **TREATMENT** — anything: `GLU`, `GLY`, `K-OAc`, …

Then:

```
run_spotting.bat
```

The first run asks, per set, for the eight strain names and which column is the
positive control; then, per set-treatment combination, which strains to exclude
and which dilution row to score. Answers are saved to `spotting_config.json`
beside the photos, so later runs only ask about things they have not seen.
`--reask` starts the questions over.

### Plate layout

Eight columns (strains) by six rows (dilutions). Rows 1–3 are one biological
replicate, rows 4–6 the other, so two plates give four replicates:

| rows | dilution |
|---|---|
| 1 and 4 | least dilute |
| 2 and 5 | middle |
| 3 and 6 | most dilute |

One dilution is scored per replicate. A column can be left empty.

### What a run writes

Into `Results/Spotting/` **beside the code**, not beside the photos. The
photographs live on OneDrive, and anything written next to them is synced back
up and easily mistaken for part of the raw capture. The two pipelines keep
separate folders — `Results/Spotting/` here, `Results/Timecourse/<set>/` for
pipeline 2 — because they answer different questions and one must never
overwrite the other:

| file | contents |
|---|---|
| `spotting_results_normalized.csv` | one row per spot: raw grey, relative growth, and every flag |
| `spotting_results_summary.csv` | mean relative growth per strain |
| `spotting_relative_growth_by_condition.{csv,xlsx}` | strain × medium matrix with significance marks |
| `figures/` | one Prism-style dot plot per combination, plus `spotting_paired_ttests.csv` |
| `montages/` | the spots themselves, four replicate blocks per figure |
| `spotting_figures.pptx` | one slide per combination: montage left, graph right |
| `previews/` | full-resolution ROI overlays, for checking placement by eye |

Useful switches: `--no-graphs`, `--no-montages`, `--no-pptx`, `-y` (skip the
montage prompt), `--keep-outliers`, `--p-adjust holm|BH|none`, `--debug`.

---

## Pipeline 2 — decide which photos to quantify

Plates are often photographed at several timepoints, sometimes more than once.
This pipeline takes the raw capture tree, where the condition is carried by
folder names, and scores every option instead of choosing by eye:

```
Set09/
  16 Hours/Glucose/Plate 1 (Rep 1+2)/*.jpg
  16 Hours/Glucose/Plate 2 (Rep 3+4)/*.jpg
  40 Hours/Glycerol/Plate 1 (Rep 1+2)/*.jpg
  ...
```

```
run_timecourse.bat                            # folder picker opens
run_timecourse.bat "D:\Set09" --estimate      # how much work it is
run_timecourse.bat "D:\Set09"                 # score everything
```

**Several sets at once.** Run it with no argument and a multi-select folder
picker opens — Ctrl- or Shift-click to choose as many as you like, and it
reopens so you can add folders from elsewhere. Pointing at the folder that
*contains* your sets runs every capture tree inside it, so ten sets take one
click. Folders can also be dragged onto the `.bat`, or passed as arguments.

Every question — which extra sessions to include, and each set's strain panel —
is asked up front, before any measuring, so a long batch can be left alone once
it starts.

**Extra sessions.** A set folder often holds a second capture beside its
timepoint folders (`Take02`, or somebody's name), which the plain walk would
skip silently — on this project six of ten sets hid 249 such photos. These are
detected, reported with their photo counts, and offered for inclusion. An
included session runs as its **own** capture tree with its own results folder
(`Set01 - Take02`), so a photo from one session is never paired with a photo
from another. `--include-takes` / `--no-takes` answer for everything without
prompting.

For each medium it tries every timepoint, every pairing of a plate-1 photo with
a plate-2 photo (technical replicates are handled by trying all combinations),
and all three dilution choices. Every candidate is scored into
`timecourse_candidates.csv`; the best per medium is printed.

The root folder being named for a set (`Set09`) is how the strain panel is
found — it is read from the main pipeline's `spotting_config.json` so the names
cannot drift between the two tools.

### The comparison sheets

A table of coefficients of variation cannot tell you whether the spots are
actually quantifiable, so every candidate is also drawn as one sheet in
`Results/Timecourse/<set>/figures/<medium>/`: the four replicate blocks on the
left with the quantified dilution row outlined in amber, and on the right the
relative-growth graph for exactly that candidate.

The graph is not a mock-up. It goes through the same tidy-frame builder and the
same `plot_spotting.R` the main pipeline uses, so it is what quantifying that
candidate would actually give you.

Filenames are prefixed `<hours>h_d<0|1|2>_`, so sorting by name walks the time
course in order with the three dilution choices grouped under each timepoint.
Any three consecutive sheets share an identical spot image — only the amber row
and the graph change — which is what makes the dilution choice comparable at a
glance. `--figures all|none|N` controls how many are drawn (default `all`).

Each photo's display background subtraction is done once, at that photo's
largest ROI radius across the three dilutions, so the plate looks the same on
all of its sheets; the radius used is stated in every sheet's footer. No
brightness or contrast adjustment is applied to any spot image.

### On ranking

The default (`--rank-by combined`) ranks on **both** replicate spread and the
number of strains separating from the control, because the output is a triage
list: it decides what to open first, and every winner is still inspected before
anything is reported.

Be aware of what that means. Comparing candidates partly on their own results is
selecting on the outcome, so the winning candidate's p-values are optimistic as
a final claim. `--rank-by variability` ranks on spread alone and does not have
this property. Whichever is used is stamped into the CSV.

### If the photos are on OneDrive

Cloud-only photos are handled without any preparation on your part. A
placeholder file is copied to a local temporary file before it is measured, so a
OneDrive re-sync cannot change it underneath the read — reading one while it is
still materialising can return partial data, which has produced a nonsense
measurement in this project before. The copy is deleted immediately afterwards,
and re-runs hit the cache without touching the photo at all. The run reports how
many files it will need to fetch.

Use `--cache-dir <local path>` to keep the measurement cache off OneDrive. It is
only a few KB per measurement, but it is one less thing for sync to touch.

---

## How the measurement works

Each stage exists because the obvious approach failed on real plates.

**Finding the plate.** The agar disc is located as the largest *inscribed*
circle via a distance transform, which is immune to the bright metal bracket
that touches the plate edge in these photographs.

**Finding the grid.** A white top-hat isolates spot-sized blobs; a rigid lattice
with near-constant spacing is fitted to them. Tilt is estimated from the
directions between neighbouring spots, folded modulo 90° — plates are not always
photographed square, and the worst in this corpus was 4.6°, which drifts a full
spot radius across the plate. The lattice window is chosen by spot count subject
to the grid being centred on the plate, so a column that did not grow cannot
shift the whole assignment.

**Placing the ROIs.** Detection runs downscaled for speed, but ROI centring is
then redone at full resolution: at a 4× downscale one detection pixel is four
real ones, which put centres ~20 px out — a quarter of the ROI radius. Centring
correlates a spot-sized disc against agar only (the meniscus reads +27 grey
where a real spot reads 4–8, and the black table beyond the plate reads
negative), and iterates to a fixed point.

**Sizing the ROIs.** The ROI is the largest circle that fits inside every spot of
the rows being scored. Spot size is measured from the radial profile of local
*texture*, not brightness: a spot is speckled with micro-colonies and agar is
not, and — unlike brightness — texture does not extend into the diffuse optical
halo around a spot. The edge is taken at the inner boundary of the fall-off, and
the outlier rejection is MAD-based, since one bad spot would otherwise size the
whole plate.

**Background subtraction.** FIJI's sliding paraboloid, radius = spot diameter +
20 px, run on a 32-bit copy so negatives are preserved. An 8-bit result clips at
zero, which rectifies the agar noise and biases faint spots upward.

**Normalisation.** Per plate, the control spots in the scored rows are averaged
and every spot on that plate is divided by that average. Artifact-flagged and
grossly outlying control spots are excluded from the divisor first — a bad
control rescales every strain on its plate.

**Statistics.** A ratio paired t-test, computed on logs (the pairing is already
inside the ratio). Values are censored at the detection limit first, since a
replicate reading zero is unmeasurable rather than small. **No multiple-testing
correction by default**, matching the manual GraphPad Prism workflow these
experiments are analysed with; `--p-adjust holm` or `BH` re-enable it, and both
raw and adjusted p-values are always written.

### What gets flagged

Spots are flagged, never silently dropped, and every flag is a column in the
tidy CSV:

- **artifact** — the ROI overlaps the plate rim, label or a bolt, on evidence
  (a proportion of rim-bright pixels), not merely position
- **unverifiable placement** — a spot with real signal whose edge cannot be
  resolved, so its ROI position cannot be confirmed
- **outlier** — a single replicate dominating a strain's spread; only removed
  when it is both extreme *and* halves the variance
- **excluded** — a strain you chose to drop

Weak or sparse growth is never treated as an artifact. It is usually the
phenotype being looked for.

### Manual override

Detection places 48 ROIs per plate correctly in almost every case, but a faint
spot near the rim can defeat every automatic estimator at once. Those are
flagged, and can be corrected by hand in `spotting_config.json`:

```json
"nudge": { "2.1K-OAc.JPG": { "1,1": [-20, 0] } }
```

Row and column are 1-based; the shift is in full-resolution pixels. It is part
of the cache key, so editing it re-measures that plate.

---

## Calibration and limits

The measurement is calibrated against **one plate measured by hand in ImageJ**
(`tests/gt/`), which records the image hash alongside the numbers so the target
can always be tied to a specific file.

Known limits, stated plainly:

- **Absolute grey values are not comparable between plates.** Only ratios to the
  control are, which is what the protocol normalises for. The ROI is sized per
  plate, so raw values move with it.
- **The control strain matters more than anything else.** These experiments use
  WT BY, which has an inherent growth defect on respiring media; where it grows
  weakly and erratically, every strain on the plate inherits that instability.
  Variability tracks the control's signal-to-noise more strongly than any other
  factor measured here.
- **Constants tuned on one medium break on another.** Glucose spots are ~10×
  brighter than K-OAc or glycerol. Thresholds throughout are expressed relative
  to each plate's own measured noise for this reason; several bugs in this
  project's history were absolute constants that worked on glucose.
- **Cache invalidation is manual.** `_cache_key` in `src/spotting_batch.py` ends in a
  version tag. Change any measurement logic and it must be bumped, or stale
  results are served from code that no longer exists.

---

## Repository layout

```
.
├── run_spotting.bat            main pipeline launcher
├── run_timecourse.bat          selection pipeline launcher
├── src/
│   ├── spotting_quant.py       measurement engine
│   ├── spotting_batch.py       main pipeline driver
│   ├── spotting_montage.py     spot montages
│   ├── spotting_pptx.py        slide deck export
│   ├── spotting_timecourse.py  time-course scoring
│   ├── spotting_timecourse_figures.py   comparison sheets
│   └── plot_spotting.R         figures and statistics
├── tests/                      calibration harness and ground truth
├── requirements.txt
├── LICENSE
└── .gitignore                  keeps photographs and results out of the repo
```

Photographs, the measurement cache, per-folder configuration and everything a
run writes are deliberately not tracked. `Results/` is created beside the code
when a pipeline runs.

---

## Licence

MIT — see `LICENSE`.

The measurement method is that of Petropavlovskiy et al., *STAR Protocols*
**1**:100182 (2020); please cite the paper if you use this for published work.
