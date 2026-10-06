# Photo intake and lab organization survey

The supplied `PhotoVariants.txt` Windows tree contains 19,722 lines, 39
top-level user folders, 13,453 supported image entries, and 33 ZIP entries.
Of the image entries, 4,024 use `_9.JPG` or `_9_<number>.JPG`. These are counts
of listing entries, not unique photographs or validated experiments. The tree
contains neither image contents nor the biological meaning of each number.

## Workflow

First import and preview the plate template on Panel. Then link one experiment
folder on Data. The program suggests conditions independently
of plate/timepoint completeness and adds them to Conditions. Review treatment
names there, then set or correct the organization on Data. Identifiers are
generated internally and are not shown in the ordinary treatment or photo
tables. Renaming a treatment changes its display name while preserving its
identifier and assignments. Photo corrections let users select a name or enter
a new one. The ordinary editor
uses one dropdown per detail. Each option combines a location and interpretation,
using folder names from the selected data and examples of the values it reads.
Only matching presets are offered alongside the exact saved reading and manual
assignment. Press **Use these choices** to apply them.
Signed folder indexes and regexes are confined to the optional advanced editor.
Per-photo corrections
take precedence over naming rules and can be applied to a multi-selection.
Unknown plate IDs or elapsed hours remain visible for correction. The existing
run validation still determines whether a time course can be assembled.

Both existing layouts remain supported: `<set>.<plate><treatment>.JPG` and
`<hours>/<medium>/Plate N/<photo>`. Handpicked mode continues to select actual
photographs for each template plate; detected metadata does not choose a photo.

## Multiple strain groups in one folder

Choose **Multiple strain groups - assign photos below**, then **Use these
choices**, or use **Add strain group**. The first named group preserves the
existing panel; the additional group starts blank and needs its own strains
and control on Panel. Additional groups share plate geometry and treatment
names, but keep independent condition controls, exclusions, picks and dilutions.
Select the group being edited on Panel, Conditions or Plates. This selector
does not filter the run: all defined groups run.

Group membership comes from the chosen naming rule or individual Data-table
assignments. Undefined/missing group assignments, empty groups and wrong-group
photo picks block multi-group runs, rather than silently excluding or pooling
them. Corrections survive re-scans, saves and undo/redo.

The bridge splits groups before building timecourse candidates or measuring
handpicked plates. Each gets a collision-resistant result subfolder, its own
CSV/figures and `experiment.json` snapshot. Review discovers nested group runs
and uses the saved photo paths and metadata when rebuilding, including manual
assignments; it never re-infers a group's membership from folder names. Old
results without saved assignments retain their original discovery behaviour.

## Patterns represented in the listing

| Organization / example | Current handling | Review or future work |
| --- | --- | --- |
| Hours / medium / Plate N / camera file | Existing capture profile | Confirm strain panel and template. |
| Medium / hours / R1 or R2 / camera file | Medium and hours suggested | R may mean replicate or plate; user assigns it or supplies a rule such as `R(\d+)` after confirming the template mapping. |
| 30C / Glucose / camera file | Composite treatment including temperature | Missing plate/time remain unresolved. |
| 30 / glucose / H2O2 - 17h.JPG | Folder and filename treatment, explicit hours | Verify the suggestion includes all biologically relevant details. |
| glucose_1.JPG, glycerol_2.JPG | Treatment text suggested | Suffixes do not establish biological replicate or plate. |
| 30C SD 2mM ZnSO4 r1+2.JPG | Dose and temperature retained in label | `r1+2` is not automatically a plate number. |
| Glucose P1 17h.JPG | Explicit plate and hours in filename | A whole-relative-path rule supports varying nesting depth. Conflicting explicit values stay unresolved. |
| Morning, later, next day, dated sessions | Session/date labels do not establish elapsed hours | Future: user-supplied spotting datetime plus confirmed capture times; timezone and missing-year handling. |
| 1a/1b, 1.1/1.2, R1+2/R3+4, #1/#2 | No implicit template plate mapping | Future: an explicit replicate-group-to-template-plate mapping editor with preview. |
| C Glu 30C 21H with Met+/Met- photos | Explicit hours and treatment text suggested; signs retained | Letter-coded panels and nutrient signs need user confirmation; future factor/panel vocabulary mappings. |
| Nested owner/project/date/treatment folders | Root-relative scanning, structural rules and per-photo edits | Choose one experiment root; unlabeled strain panels cannot be safely inferred across an entire lab archive. |
| Typos and abbreviations (rad/radicicol, glycerl/glycerol) | Kept distinct unless a known alias applies | Future: proposed alias merges with example paths and explicit acceptance, never automatic fuzzy pooling. |
| Heat shock followed by a different growth temperature | Distinct temperature text retained for review | Future: separate exposure and incubation factors; do not treat them as interchangeable. |
| Raw `_9` / Image_G counters, ZIP files, shortcuts | Camera counters carry no plate identity; archives and shortcuts are not imported | Extract an archive before linking it. Future image-label reading would require a separate review workflow. |

## Implementation boundaries

`experiments/names.py` supplies conservative text suggestions; it reads no pixels
and makes no strain assignment. Known treatment-bearing folders and filename
text are combined, retaining doses and signs. Explicit temperatures and common
bare incubation values (25, 30, 37, 42) can contribute to a condition, not elapsed
time. Dated folders are excluded from treatment-label heuristics because they
frequently name projects or sessions. A treatment found only in such a folder
needs a folder rule or manual assignment.

Naming profiles can read a folder segment, filename stem, whole relative path,
or composed treatment suggestion. Whole-path rules allow metadata to be split
across folders and filenames; existing fixed-depth rules remain editable.
Automatic discovery starts with the commonest nesting depth. Mixed-depth or
mixed-convention collections should be inspected in the table, with whole-path
rules or per-photo corrections used where the inferred level does not fit.

Short result codes are generated with collision suffixes so distinct full labels
are not pooled by eight-character truncation. Saved aliases and declared full
labels take precedence. Standard GLU, GLY and K-OAc mappings remain available.
Case and whitespace are normalized; spelling corrections are not inferred.

The model still stores a condition as one label/code. A future factor model
could represent medium, dose, temperature, exposure and nutrient selection
separately, then construct only combinations actually observed in the photos.
That extension should preserve existing condition IDs, per-condition controls,
photo assignments and output references. It should not generate a Cartesian
product of treatments that were never performed.

Regression examples live in `tests/experiments/test_data_discovery.py`; they
use synthesized paths from these naming patterns and require no lab photos.
