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
| `run_plate_designer.bat` | **Plate designer.** Draw the layout of the assay: which cell holds which sample, replicate and dilution. Strain-free, so one template serves many experiments. |
| `run_experiment_designer.bat` | **Experiment designer.** Bind a template to real strains, conditions and photographs, and drive either pipeline from it. |
| `run_spotting.bat` | **Main pipeline.** A folder of chosen photos → measurements, statistics, figures. |
| `run_timecourse.bat` | **Selection pipeline.** A raw capture tree → which photos are worth quantifying. |
| `run_review.bat` | **Review.** Every candidate the selection pipeline scored → the one you choose, with bad spots corrected. |
| `spotting_app.py` | **All five stages behind one launcher window.** See [One program, every stage](#one-program-every-stage). |
| `plate_template/` | The plate designer: model, schema, validation, autofill, and its window. |
| `experiments/` | The experiment layer: strain panels, conditions, photo intake, and the bridge that drives the pipelines. |
| `src/spotting_quant.py` | Measurement engine: plate finding, grid detection, ROI placement and sizing, background subtraction, normalisation, statistics. |
| `src/spotting_batch.py` | Driver for the main pipeline: discovery, prompts, caching, exports. |
| `src/spotting_montage.py` | Figure of the spots themselves, one block per biological replicate. |
| `src/spotting_pptx.py` | Slide deck pairing each montage with its graph. |
| `src/spotting_timecourse.py` | Scores every timepoint × photo pairing × dilution and ranks them. |
| `src/spotting_timecourse_figures.py` | Turns each of those candidates into a comparison sheet: marked spots beside their graph. |
| `src/spotting_plots.py` | PyPrism/Matplotlib dot plots and paired significance tests. |
| `results_review/` | The review window: browse every candidate, pick one, correct spots, export `chosen/`. |
| `tests/` | Calibration harness and the hand-measured ground truth. |

---

## Requirements

**Python 3.11+**

```
pip install -r requirements.txt
```

**Background subtraction runs entirely in Python.** FIJI and Java are not
required. `src/spotting_background.py` implements ImageJ's sliding paraboloid;
Numba (installed by `requirements.txt`) compiles its Python loops for speed.
The first measurement includes a one-time compilation cost. A full-resolution
plate and six synthetic reference cases matched ImageJ pixel for pixel; see
[the comparison report](validation/background/README.md).

Figures and statistics are also Python-native. `requirements.txt` installs
[`PyPrism_Plots`](https://github.com/darrentymeczko/PyPrism_Plots) directly
from GitHub; R, ggplot2, and ggprism are not required.

---

## Setting up an experiment

The three pipelines below read their photographs from one of two fixed layouts:
`<set>.<plate><TREATMENT>.JPG` in a flat folder, or
`<N Hours>/<Medium>/Plate N/` in a capture tree. Anything else is unreadable,
and who is in which column has to be typed in at a console prompt every time.

The **experiment designer** removes both constraints.

```
run_experiment_designer.bat
```

An *experiment* is one strain panel, spotted across one or more conditions, with
a positive control that every strain is reported relative to. The photo folder
is chosen at the top of the window, since both ways of quantifying need it.

1. **Panel** — choose the plate template the assay was spotted on, name each
   sample slot, and mark the control. Leaving a slot blank says nothing was
   spotted there.
2. **Conditions** — each medium or treatment. A condition may name its **own**
   control, which is what you want when a strain does not grow on one medium
   and so cannot be the reference there.
3. **Plates** — handpicked quantification only. One row per plate the template
   needs. Flip through the photographs, press *Use this photograph*, then set
   that plate's dilution row while you can see it.
4. **Run** — handpicked quantification, or the time course.

**The dilution row is chosen per plate, not per treatment.** Every spot is
divided by the control on its *own* plate, so two plates of one medium that grew
to different densities can each be scored at whichever row is actually readable
without the two becoming incomparable.

**However many dilution levels the template declares.** The console pipeline
assumes three, spotted twice down six rows — `least`, `middle`, `most`. The
experiment designer reads the levels off the plate template instead, so a design
that puts six levels down the plate with one replicate each, or two levels with
three, works without changing anything: the levels are numbered `1`, `2`, `3`…
and named only where the template names them. Which rows a level occupies, and
which biological replicate each row belongs to, come from the design rather than
from arithmetic.

**And grids other than 8 × 6.** `detect_grid` takes the grid size as an argument
and derives the expected spot pitch from it, so a 12 × 16 design is looked for at
a 12 × 16 pitch rather than being read as a sparse 8 × 6. The invariant it relies
on is that a frogger prints its grid centred and filling roughly the same part of
the dish whatever its pin count — so more positions mean a finer pitch, not a
wider footprint. The floor is 2 × 2: below that there is no lattice to fit and no
agar gap between diagonally adjacent spots to read the background from.

Recovery of the lattice is verified against synthetic plates of known geometry at
6 × 8, 8 × 12, 12 × 8, 12 × 16, 16 × 24 and 4 × 4, square-on and tilted, including
with a whole edge row or column failed to grow — see `tests/test_grid_sizes.py`.
The lab's own 8 × 6 remains the default and is bit-identical: same pitch, same
spot size, same cache keys, same measured numbers.

**The time course takes any number of dilution levels too.** Its scoring,
candidate table, best-candidate output, montages, comparison sheets and the
review window's rebuilds all read a `DilutionLayout` built from the template —
every level, on whichever rows it sits on each plate, with its own replicate
numbering. A photograph is measured once for all of its levels, and the
measurement cache now *adds* row choices to an existing entry rather than
replacing it, so two designs over the same photo never evict each other. The
lab's own design becomes exactly the classic layout, so its candidate CSVs, sheet
filenames and cache keys are unchanged.

**And any number of plates.** Every strain is divided by the mean of all the
control replicates on its *own* plate, so one plate carrying four control
replicates and four of every strain is a complete experiment — and so is a design
spread over three or four plates. The time course takes one photograph of every
plate the design has and tries every combination of their re-shots; the
candidates CSV gets a `plate1`, `plate2`, … column per plate (the lab's two-plate
runs keep exactly the columns they always had). A sitting missing any plate is
skipped, since a missing plate is missing replicates. The ranking's completeness
term now compares the control replicates found with the number the design
actually spotted on those plates, instead of assuming four.

One limit remains, refused up front rather than failing mid-run: the time course
ranks candidates on each strain's spread and significance, which need at least
three biological replicates in total. Handpicked quantification has no such
limit.

**Which photograph is which plate is never guessed.** In handpicked mode you say
so outright — which is why a folder of raw camera dumps, every file named
`_9.JPG`, works perfectly well. In a time course the folder layout is read
automatically, and where no folder names a plate those photos are reported as
unreadable rather than assigned. Inferring it would silently mislabel biological
replicates, and a mislabelled replicate still produces a confident-looking
result.

To set up a whole season at once, **File → New from several folders**: pick the
folders, one plate template and one set of conditions for all of them, then type
each folder's own strain panel. Slots left blank are empty, so the panels may
differ in how many strains they carry while staying the same assay on the same
layout.

The same thing without the window:

```
py -m experiments.cli scan    "<photo folder>"     # what is in there, and what it read
py -m experiments.cli check   "<experiment.json>"  # validate one
py -m experiments.cli run     "<experiment.json>"  # quantify it
py -m experiments.cli migrate "<folder or config>" # convert existing config files
```

`migrate` converts the answers the pipelines have already collected —
`spotting_config.json` and each `timecourse_config.json` — into experiment
files, so existing work is not re-entered. Where the two disagree about a
control the per-medium one wins, and the disagreement is printed rather than
resolved silently.

Experiments are saved to `Experiment Designs/`. They are git-ignored: they name
real strains and record an absolute path to the photo folder.

A run also writes `experiment.json` beside its results. That is purely
additive — every file the review window already reads is still written exactly
as before — and it lets the review show real strain names and find the
photographs without being pointed at them.

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

This layout is hardcoded in `src/spotting_batch.py` (`DILUTIONS`) and is what
this pipeline and the time course both assume. It is *not* the only layout the
program supports — the experiment designer reads the arrangement off a plate
template and handles any number of dilution levels, and grids other than 8 × 6.
See [Setting up an experiment](#setting-up-an-experiment).

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
same `spotting_plots.py` PyPrism renderer the main pipeline uses, so it is what
quantifying that candidate would actually give you.

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

## Pipeline 3 — review the result and correct it

Pipeline 2 ranks the candidates and exports one winner per medium. This is where
you look at all of them, keep that winner or choose a different one, and correct
individual spots:

```
run_review.bat                                # reopens the set you had open last
run_review.bat --pick                         # choose a different set
run_review.bat "Results\Timecourse\Set01"     # straight into one set
run_review.bat --apply                        # re-export every reviewed set, no window
```

Double-clicking goes straight back to where you left off — reviewing a set takes
more than one sitting. The first time, a list of the sets appears. No console
window is left behind; if the window cannot start at all, it says so in a dialog
and writes `review_error.log`.

Every candidate for the current medium is listed with the numbers it was ranked
on, and the pipeline's own pick is marked ★ and selected on opening — so the
starting point is what the run produced, and every difference from it is
something you chose. Selecting a row shows that candidate's comparison sheet;
double-click, or press **Use this candidate**, to choose it. Click any column
heading to sort by it, and again to reverse; a fresh column starts on its useful
end — highest score, but *lowest* spread.

### Getting around

| | |
|---|---|
| `←` `→` | previous / next medium |
| `↑` `↓` | previous / next candidate (previews it; it is not chosen until you press Enter) |
| `PgUp` `PgDn` | ten candidates at a time |
| ◀ ▶ in the top bar | previous / next set |
| `G` | swap between the pipeline's sheet and your redrawn graph |

Inside the spot table the arrows still move between spots, and inside a text box
they still move the cursor.

### Correcting spots

Below the sheet is the per-spot data behind whatever is chosen: every spot's grey
value, its relative growth, and its flags.

| | |
|---|---|
| double-click a grey value | type one you measured by hand |
| `O` | flag it as an outlier, or clear the flag |
| `N` | add a note |
| `Backspace` | forget every correction to that spot |
| **↻ Redraw graph** (`Ctrl+R`) | put the corrected numbers back through the PyPrism renderer |

The strain means beside the table move the instant you change something. The
*graph* waits for **↻ Redraw graph** — the button sits above the graph itself —
so flagging four spots in a row does not redraw four intermediate states you
never wanted to see. Until you press it the button reads `out of date`, so a figure can never
quietly predate the numbers under it. Anything slow shows a progress bar.

The redraw goes through the same `spotting_plots.py` path as the export, so it is the
figure you would get, not an impression of one — written to a scratch folder,
never into your results. The candidate's spot-image panel and amber quantified-row
outlines stay alongside the updated graph, so the redraw remains a complete
comparison sheet. `G` swaps back to the pipeline's original sheet.

Choose the analysis beside the medium tabs at the top of the review window. **Ratio paired
t-tests** compare each strain with its normalized control on the log-ratio
scale; the adjacent menu can leave the p-values unadjusted or control the
family-wise error rate with Holm, Bonferroni, or Šidák correction. **One-way
ANOVA** instead reports a single omnibus test across all strains, so the
multiple-comparison menu is disabled for that choice. The selection is saved
in `review.json` and is used by both **Redraw graph** and **Export chosen/**.

Corrections run through the pipeline's own normalisation, not over the top of
it. Re-measuring a **control** spot therefore changes that plate's divisor and
moves every strain on it. Flagging an outlier omits that observation from the
figure and statistical test.

Pressing a flag a second time clears your decision back to the pipeline's
opinion rather than asserting the opposite. "I have no view on this spot" and "I
checked this and it is fine" are different claims and the CSV records which one
you made.

### What a review writes

Decisions go to `Results/Timecourse/<set>/review.json` — which candidate per
medium, and what changed about which spot, with your reasons. Numbers are never
stored there; they are rebuilt from the photographs through the same functions
the pipeline uses, so a review still means something after a re-measure.

**Export chosen/** then writes the result to `Results/Timecourse/<set>/chosen/`,
laid out exactly like `best/` and produced by the same PyPrism renderer and the
same montage code:

| file | contents |
|---|---|
| `spotting_results_normalized.csv` | one row per spot, plus who changed what and why |
| `figures/` | one graph per medium, plus `spotting_paired_ttests.csv` or `spotting_anova.csv` for the selected analysis |
| `montages/` | the spots themselves |
| `chosen_summary.csv` | what was picked per medium, the selected statistical analysis, whether it was the pipeline's pick, and the reason |

`best/` is never written to, so re-running pipeline 2 cannot destroy a review,
and the automatic pick stays there to be compared against.

### What it needs

Browsing the sheets and choosing a candidate need nothing but the results
folder. **Editing** needs the photographs, because only the automatic winner's
per-spot data survives a run — every other candidate's numbers are regenerated
on demand from the capture tree and the measurement cache.

**Locate photos…** is how you point a set at its photographs. A results folder
does not record where its photographs were: the measurement cache is keyed on
file name, size and modification time rather than path, which is what lets you
reorganise the photos without invalidating it, but it also means the real files
have to be in hand before a cached measurement can be found again. The window
looks for them automatically under the usual data folders and remembers what it
finds, so normally you never press this. You need it when the photographs have
moved or been renamed since the run, when they live somewhere unusual, or when
the top bar says `not linked` — which it will, honestly, rather than quietly
falling back to a similarly-named folder holding different plates.

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

**Background subtraction.** Python implementation of ImageJ's sliding paraboloid, radius = spot diameter +
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
- **excluded** — a strain excluded by the experiment configuration (shown in
  review output, but not offered as a second manual omit action)

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

## One program, every stage

`spotting_app.py` puts all five stages behind one entry point. Run it bare for a
launcher window listing them in order — **1** design (plate template,
experiment), **2** measure (spotting, time course), **3** review — with a button
each. The `.bat` files are unchanged and still work; this is an alternative to
them, not a replacement.

Or name a stage. Everything after it goes to that stage exactly as the matching
`.bat` file would pass it on:

```
py spotting_app.py spotting
py spotting_app.py timecourse "D:\Set09" --workers 8
py spotting_app.py review --apply
py spotting_app.py --help
```

Stages are `plate`, `experiment`, `spotting`, `timecourse` and `review`. The
command-line tools behind them are `plate-cli`, `experiment-cli`, `montage`,
`pptx` and `quant`. Dragging capture-tree folders onto the file runs the time
course on them, as it does for `run_timecourse.bat`. Each stage opened from the
launcher runs as its own process, so one crashing cannot take the others with
it and several can be open side by side.

`--selftest` imports every stage and exercises the fragile parts — compiled
Numba code, every figure format, the slide template, Tk — which is the quickest
way to tell whether an environment is intact.

### Packaging

This is not packaged for distribution here. The intended shape is PyInstaller
`--onedir` feeding an [Inno Setup](https://jrsoftware.org/isinfo.php) installer:
an installer is what gives a non-technical user a Start Menu shortcut and an
uninstaller, and what an IT department expects to see. Both tools are free; only
code signing costs anything, and
[SignPath Foundation](https://signpath.org/) signs open-source projects at no
charge, which this MIT-licensed one qualifies for.

`spotting_app.py` is already written for that: when it detects it is running
packaged, the project folder becomes the folder holding the program, and this
project's own `.py` files are used ahead of any copy built into the package — so
an edit does not require a repackage.

Be aware that **Windows Smart App Control blocks unsigned binaries**, including
the `.pyd` files inside numpy, scipy, numba and CPython itself. It is on by
default on much new Windows 11 hardware. Test any distribution on a machine with
it enabled before publishing, whichever route you take.
- **Errors from a window** are written to `spotting_error.log` beside the
  program and shown in a dialog; there is no console to print them to.

---

## Repository layout

```
.
├── spotting_app.py             launcher window and stage dispatcher
├── run_plate_designer.bat      plate designer launcher
├── run_experiment_designer.bat experiment designer launcher
├── run_spotting.bat            main pipeline launcher
├── run_timecourse.bat          selection pipeline launcher
├── run_review.bat              review window launcher
├── plate_template/             the plate designer (model, schema, validation, GUI)
│   └── templates/              shipped layouts, e.g. the lab standard 8x6
├── experiments/
│   ├── model.py                an experiment: panel, conditions, control, mode
│   ├── profiles.py             how a photo's path says what it is
│   ├── intake.py               scan, infer the layout, resolve each photo
│   ├── migrate.py              convert the pipelines' existing config files
│   ├── run.py                  the bridge into src/ (the only place that imports it)
│   ├── cli.py                  scan / check / run / migrate
│   └── app.py                  the experiment designer window
├── results_review/             the review window
├── src/
│   ├── spotting_quant.py       measurement engine
│   ├── spotting_batch.py       main pipeline driver
│   ├── spotting_background.py  ImageJ sliding paraboloid, in Python
│   ├── spotting_paths.py       where the project folder is (beside the .exe when packaged)
│   ├── spotting_montage.py     spot montages
│   ├── spotting_pptx.py        slide deck export
│   ├── spotting_timecourse.py  time-course scoring
│   ├── spotting_timecourse_figures.py   comparison sheets
│   └── spotting_plots.py       PyPrism figures and statistics
├── Plate Templates/            saved plate layouts (tracked: strain-free geometry)
├── Experiment Designs/         saved experiments (ignored: strain names, local paths)
├── tests/                      calibration harness, ground truth, and the test suites
├── requirements.txt
├── LICENSE
└── .gitignore                  keeps photographs and results out of the repo
```

Photographs, the measurement cache, per-folder configuration, experiment files
and everything a run writes are deliberately not tracked. `Results/` is created
beside the code when a pipeline runs.

The two designers are kept free of the measurement stack: their cores import
nothing beyond the standard library, so they open on a machine that cannot run
a measurement, and they are tested without a display.
`experiments/run.py` is the single exception, and the only module allowed to
import `src/`.

---

## Licence

MIT — see `LICENSE`.

The measurement method is that of Petropavlovskiy et al., *STAR Protocols*
**1**:100182 (2020); please cite the paper if you use this for published work.
