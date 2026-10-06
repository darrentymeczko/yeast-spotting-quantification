# Additional multi-step analysis

In **Experiment Designer → Run → Statistics → Additional multi-step analysis**,
enable the extra analysis. Current candidate selection, graphs, statistics and
review remain available. The new analysis runs independently of the winning
candidate and writes separate graphs and data.

## Set up the design

1. Select **Technical replicates** or **Technical replicates + repeated measures**.
2. Select **Two-step analysis** or **Full hierarchical analysis**.
3. Choose one dilution and enter the hours to include, separated by commas.
   Technical-only needs exactly one hour; repeated measures needs at least two.
   All conditions/groups use these selections. Choose them before examining
   significance. Different dilutions are not treated as replicates.
4. Select photos in the dialog and assign a physical technical-plate label such
   as A or B. Keep that label at every timepoint for the same physical plate.
   Labels are local to strain group, condition and template plate position:
   template plate 1/A and template plate 2/A are different physical plates.
   File order and camera filenames never establish plate identity.
5. Exclude any failed plate observations with a documented reason. Exclusions
   in this dialog affect only the additional analysis. Photos marked Ignore
   in Data remain ignored by both workflows. Plates and spots flagged in the
   **data review** (see the README) are always excluded from this analysis
   with the reason `data review: ...`. A flagged plate needs no technical-plate
   label, and a flagged spot is excluded like an image artifact. The endpoint
   analysis does not drop them; the results review only marks them.
6. Save settings, choose **Full output**, and run normally. In handpicked mode,
   the additional analysis uses all resolved photos at the specified hours,
   independently of the photos picked for the existing endpoint analysis.

The template's replicate numbers identify biological cultures within a strain,
and matched experimental blocks between that strain and its control. Each block
must contain its control on the same plate. Ensure these IDs refer to the same
original cultures on every technical plate and throughout the time course.
The design cannot distinguish separate experiments/days that reuse these IDs;
run those as separate experiments until their biological IDs are made unique.

## What is tested

Each condition and strain group is analyzed separately. The additional analysis
compares each non-control strain with its condition's control. The existing
ANOVA/post-hoc/all-pairs choices continue to apply to the current output; they
do not change the additional model. Its alpha comes from the designer's p cutoff.

For each biological block `b`, physical technical plate `p` and time `t`, use

```
z[b,p,t] = log(raw growth of strain[b,p,t])
         - log(raw growth of matched control[b,p,t])
```

Both numerator and control variability therefore contribute. This differs from
the current endpoint's normalization against an arithmetic mean of all plate
controls. On a common plate, taking a ratio of the two currently normalized
values would cancel that common denominator and yield this same matched ratio.
Using matched controls avoids treating an estimated plate-wide control divisor
as an error-free constant. It may be noisier than a plate mean.

| Scope | Two-step | Full hierarchical |
| --- | --- | --- |
| Technical plates at one time | Mean log ratio within each biological block; one-sample t test of these biological means against zero | All log ratios enter a mixed model with crossed biological-block and physical-plate random intercepts |
| Technical plates across time | Mean log ratio within block/time, then a repeated-measures mixed model with categorical time and a block random intercept | All log ratios enter a mixed model with categorical time and crossed block/plate effects plus persistent spot-pair and plate-by-time effects |

With three or more timepoints, repeated models also include an independent
random linear time slope per biological block, using centered/scaled time.
Fixed time effects remain categorical; population growth is not forced to be
linear. Residual errors are independent conditional on the random effects.
This is a specific covariance model, not a general autoregressive model.
The persistent spot pair is the strain/control pair in a block on one physical
plate. Physical plates containing several biological cultures are crossed with
cultures, not incorrectly treated as nested within one culture.

The two-step method weights biological block means equally at a given time,
even when technical counts differ. It assumes shared multiplicative plate
effects have cancelled in the matched ratio; it does not separately estimate
remaining correlation between different block means from shared technical
plates. Use the full hierarchy when such residual plate variation matters.
The hierarchy pools variance estimates across observations of each strain's
contrast, not across unrelated strains. Confounded covariance components are
merged into an estimable variance component or the residual, and listed in
`analysis.json`; they must not be interpreted as separately measured variances.

Repeated analyses report the ratio at each selected time, an equal-time-weighted
mean log contrast, a joint Wald test of all time-specific contrasts against
zero, and a Wald test of whether the strain-control contrast changes over time.
The latter is the interaction question for this strain/control pair. The mean
contrast is not an area under the curve; irregular times still get equal weight.
Holm correction covers all estimable tests across strains and timepoints within
each condition (including the mean and joint tests). Conditions and strain
groups are separate families. Confidence intervals are pointwise, not simultaneous.

## Quality control and interpretation

Every measured spot is exported, including failed measurements and exclusions.
Image artifacts and explicit exclusions prevent the affected spot/control pair
from entering the model. A bad control only invalidates contrasts that use it.
No statistical outlier trimming is applied. A technical observation that merely
disagrees with the others remains in the analysis.

Low or absent target growth is a phenotype, not a technical failure. Such
observations remain in the audit and display at the detection limit. If any
usable target value in a strain's selected data is censored, its Gaussian-model
p-values and CIs are withheld. A censored likelihood model would be needed for
inferential analysis in that situation. Failed/below-limit controls cannot define
a ratio and are excluded with a reason. Detection limits use the existing
measurement engine's plate background noise and minimum-control threshold.

At least three usable biological blocks are required at every selected hour.
Additional technical plates and timepoints never increase the biological count.
Missing observations are allowed when enough valid blocks remain. Mixed-model
interpretation assumes missingness is ignorable conditional on the model; loss
related to growth phenotype may violate this assumption.

Mixed models are fitted by REML with **approximate normal/Wald inference**.
They do not implement Kenward–Roger or Satterthwaite small-sample correction.
Few biological blocks are explicitly flagged. Nonconverged fits, invalid
covariance/Hessian, insufficient replication, and censoring produce descriptive
graphs with a reason instead of apparently valid significance. These checks do
not guarantee good small-sample calibration or increased power. Endpoint
two-step inference uses Student's t with biological `n-1` degrees of freedom.

The program does not choose a dilution/timepoint by significance for this extra
analysis. Retrospectively changing those selections after inspecting results
still introduces selection bias. The existing automatically selected endpoint
is displayed for comparison and retains its existing selection limitations.

## Outputs

Each run adds a timestamped directory:

```
<experiment results>/multi_step/<timestamp>-<scope>-<method>/
  <condition>-comparison.png / .svg
  observations.csv          raw measurements and QC reasons
  paired_contrasts.csv      matched control, censoring, inclusion and log ratio
  biological_means.csv      technical means, counts and censoring per block/time
  estimates.csv             time-specific estimates and pointwise CIs
  tests.csv                 raw and Holm-adjusted p-values, counts and status
  analysis.json             settings, model diagnostics and variance components
  selection.json            photograph selection and explicit exclusion reasons
  experiment.json           full experiment snapshot
  data_review.json          the data review's flags as they stood (when there is one)
```

`multi_step/latest.json` points to the newest additional run. Earlier runs and
existing endpoint results are retained. Comparison graphs show biological means
in grey, the extra analysis in teal, and the current endpoint's arithmetic mean
as an orange marker at its actual hour when available. Normalizations differ as
described above; the endpoint may also use a different selected dilution.
Review edits to the existing winner do not automatically change this analysis;
update its settings and rerun to create a new recorded result.

## References

- [Petropavlovskiy et al. (2020): yeast spotting quantification and physical technical plates](https://pmc.ncbi.nlm.nih.gov/articles/PMC7757406/).
- [Blainey, Krzywinski & Altman (2014): Replication](https://www.nature.com/articles/nmeth.3091).
- [Eisner (2021): Pseudoreplication in physiology](https://pmc.ncbi.nlm.nih.gov/articles/PMC7814346/).
- [Kulkarni, Wang & Bertozzi (2022): analyzing nested experimental designs](https://journals.plos.org/ploscompbiol/article?id=10.1371/journal.pcbi.1010061).
- [Statsmodels: linear mixed effects and crossed variance components](https://www.statsmodels.org/stable/mixed_linear.html).
