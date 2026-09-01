# Ground-truth capture — hand ImageJ measurement

The program's numbers are calibrated against **one plate measured by hand in
ImageJ**. This folder holds that reference. Everything here exists because the
previous reference (`tests/legacy/expected_relative_growth.csv`) could not be
tied to a specific image file, which made a Pearson r of 0.61 impossible to
diagnose — we could not tell whether the program was wrong or the target was.

**Record the image hash.** That one field is what stops this from happening again.

---

## Why this much detail

The whole usable signal on these plates is about **28 gray units** (spots read
~146–178 against ~151 agar). Anything that costs 1–2 gray is a 5–10% per-spot
error that then compounds when you divide by the control. So settings that look
like trivia — the RGB conversion checkbox, whether the ball radius was 107 or
214 — are first-order. Record them all; don't reconstruct them later from memory.

---

## Step 0 — Generate the ROIs

Run this on the plate you're going to measure:

```bash
python spotting_quant.py "path/to/plate.JPG" --export-rois "tests/gt/<plate_id>/rois"
```

That writes `rois.csv` and `rois.ijm`. The macro places all **53 ROIs** (48
spots + 5 background) exactly where the program would measure them.

This matters: you are measuring the *same geometry the program uses*, so any
disagreement in gray value can only be the arithmetic, never the placement. If
a circle looks wrong, nudge it — the correction comes back through
`gt_rois.csv` and the program will follow it.

---

## Step 1 — Open and convert

1. Open the plate image in Fiji.
2. **Image ▸ Type ▸ 8-bit**
3. **Do not** click Apply in Brightness/Contrast. Dragging the sliders is fine
   (that's display-only); Apply permanently rewrites pixel values.

Then check **Edit ▸ Options ▸ Conversions** and write down whether
*"Weighted RGB conversions"* is ticked. Measured impact on these plates: ~1.6
gray of differential signal. Not optional.

---

## Step 2 — Load the ROIs

Drag `rois.ijm` onto the Fiji main window, press **Run**. The ROI Manager fills
with 53 entries named `spot_r1c1` … `spot_r6c8` and `bg_1` … `bg_5`.

Adjust any that are misplaced. Move them with the **arrow keys** — that
translates by whole pixels and keeps the mask identical. Do **not** resize any
ROI: protocol step 21 requires one fixed selection size for every reading on the
plate, because the area itself changes the mean.

---

## Step 3 — Set measurements

**Analyze ▸ Set Measurements…** — tick:

- Area
- Mean gray value
- Min & max gray value
- Standard deviation
- Centroid
- Bounding rectangle
- Display label

Decimal places: **3**.

`Area` and `Bounding rectangle` are what let the harness prove the geometry
matches before it compares any values — a pixel-count mismatch is an instant,
unambiguous "the ROIs aren't the same", which is much easier to act on than a
vague numeric disagreement.

---

## Step 4 — Measure twice

### Pass 1 — before background subtraction

1. ROI Manager ▸ **Deselect** ▸ **Measure**
2. Results ▸ File ▸ Save As → `gt_measurements_raw8.csv`
3. Clear the Results table.

### Pass 2 — after background subtraction

4. **Process ▸ Subtract Background…**
   - Write down the **exact number** in the Rolling ball radius field.
   - Write down every checkbox: *Light background*, *Sliding paraboloid*,
     *Disable smoothing*, *Preview*.
   - Click OK **once**. Note how many times you ran it (should be 1 — see below).
5. ROI Manager ▸ **Deselect** ▸ **Measure**
6. Results ▸ File ▸ Save As → `gt_measurements_rollingball.csv`
7. ROI Manager ▸ More ▸ **Save…** → `gt_rois.zip` (archival backup)

> **On iterating.** The protocol says to repeat background subtraction until the
> five background readings agree within 3 gray units. Please run it **once** and
> record the spread you actually get. Rolling-ball subtraction isn't idempotent
> — once the background is flat, the spots are the only structure left, so
> further passes start eating them. If one pass doesn't flatten it, the right
> fix is a different ball radius, and we want to know that. Record what happens
> rather than iterating until it looks right.

> **The single most useful number you can give us** is `bg_spread_after` — the
> max minus min of your five background readings after ONE pass. The Python side
> currently gets 5.56 gray at ImageJ's own downscale setting, which fails the
> protocol's 3-gray test; it only reaches 2.96 by downscaling 4× less, at 50×
> the runtime. If ImageJ gets under 3 in one pass at the standard setting, then
> ImageJ's rolling ball is doing something materially different from
> scikit-image's and we should drive Fiji directly rather than reimplement it.
> Your spread number is what decides that.

**Measure all 53 ROIs at both stages** — not just the two rows you'd normally
quantify. The rows sitting at background level are the single most diagnostic
thing in this file; they're exactly where the old code's clipping bug lived.

---

## Step 5 — Fill in `gt_meta.csv`

Copy `gt_meta_TEMPLATE.csv` to `gt_meta.csv` and fill every row. Get the SHA-256
with:

```bash
python -c "import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())" "path/to/plate.JPG"
```

---

## What you do NOT need to record

Ratios, means, standard deviations, relative growth. The harness recomputes all
of that from the `Mean` column, so transcription error can't get in. If you
already have an Excel sheet of relative growth values, drop it in as
`gt_relative_growth.csv` with columns `replicate,strain,relative_growth` — it's
used only to check the normalization arithmetic in isolation.

---

## Step 6 — Run the calibration

```bash
python tests/calibrate.py --gt tests/gt/<plate_id>
```

It reports a staged ladder. **Read down and stop at the first FAIL** — that
localizes the bug:

| Stage | Checks | If it fails |
|---|---|---|
| S0 | auto-detected vs your ROI positions | grid detection |
| S0b | pixel counts vs ImageJ `Area` | ROI mask rule |
| **S1** | **raw gray at your ROIs** | **grayscale conversion — fix before anything else** |
| S3 | rolling-ball output | the background algorithm |
| S4 | background scalar | background ROI placement |
| S5 | net values | subtraction order |
| S6a | normalizer run over *your* numbers | the normalization maths alone |
| S6b | end-to-end relative growth | the headline number |

S1 is the gate. If reading the same pixels at the same coordinates doesn't
reproduce ImageJ's mean to within 0.5 gray, nothing downstream is worth tuning.

---

## Files in a complete ground-truth folder

```
tests/gt/<plate_id>/
  gt_meta.csv                     settings + provenance (you fill in)
  gt_measurements_raw8.csv        53 rows, from Fiji
  gt_measurements_rollingball.csv 53 rows, from Fiji
  gt_rois.csv                     final ROI geometry (from rois.csv, nudged)
  gt_rois.zip                     ImageJ ROI archive (backup)
  gt_relative_growth.csv          optional, if you have hand ratios already
```
