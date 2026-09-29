# Python background-subtraction validation

Validated on 2026-09-12 against ImageJ 1.54g's actual Subtract Background
command, using the previous application's macro sequence: open the JPEG,
convert to 8-bit, convert to 32-bit, subtract with `rolling=224 sliding`.
The Python arm opens the same JPEG independently and uses the new implementation.
Neither arm reads previously cached intensity measurements.

| Comparison | Result |
|---|---:|
| Full-resolution image | `Spotting Assays/4.1GLU.JPG`, 6000 × 4000 |
| Pixels compared | 24,000,000 |
| Maximum absolute pixel difference | **0** |
| Mean absolute pixel difference | **0** |
| Spots measured across all three dilutions | 48 |
| Maximum net spot intensity difference | **0 gray units** |
| Maximum relative growth difference | **0** |
| Changed outlier flags | **0** |
| ImageJ subtraction including process startup | 10.31 s |
| Python subtraction with compiled code cached | 10.33 s |

The acceptance limits, set before comparing, were 0.01 gray units in net
intensity, 0.001 in relative growth, and no changed outlier flags. All passed.
Both arms use the production dilution-specific ROI selection and measurement
functions with identical detected geometry, followed by the same normalization
and outlier functions. This isolates the replaced image-processing operation.
The local `Results/Validation/Background/spot_comparison.csv` contains the
individual before/after values and stays outside version control, like other
experimental results.

The real-data sample is one glucose plate, with two biological replicates and
three dilutions. The planned second plate (`4.2GLU.JPG`) was an unavailable
OneDrive placeholder. This is evidence of equivalence on the tested sample,
not a validation of every medium, camera, ImageJ version, or scientific assay.
No new significance tests were performed on this two-replicate subset.

Six additional frozen reference cases cover a constant image, noise, radius 1,
corner objects on a gradient, faint/noisy spots with radius 128, and a 3×3 image.
All match ImageJ bit for bit. `tests/background_reference.npz` stores their
inputs, radii, outputs, and reference version so normal tests need no Java.
The actual production measurement path was also checked against the reference,
using float64 ROI accumulation as in the original FIJI integration.
Montage processing was checked with its image-block cache cleared between
reference and Python runs; the displayed blocks and masks were identical.

## Reproduce

Install `requirements.txt` and `requirements-dev.txt`. Then run:

```powershell
python -m pytest tests/test_background.py -q
python tests/compare_background.py --java PATH/TO/java.exe --imagej PATH/TO/ij.jar --out comparison-output "Spotting Assays/4.1GLU.JPG"
python tests/generate_background_reference.py --java PATH/TO/java.exe --imagej PATH/TO/ij.jar
```

The second command needs ImageJ and Java **only as reference tools**. It saves
both full-size processed TIFFs, the fixed geometry, per-spot CSV, and summary.
Use a fresh output directory when changing an input image or detection code;
the comparison tool reuses its own saved geometry on repeated runs.
The third command regenerates the synthetic expected outputs using the stored
inputs. Large TIFFs and reference runtimes are excluded from the published report.

Application regression tests: 348 passed. The two test modules that rebuild
live capture trees (`test_export.py` and `test_matches_pipeline.py`, 24 tests)
were excluded from that run after the full run stalled on cloud data access.
The new background tests and the independent real-plate comparison passed.

## Implementation and provenance

The Python code follows Michael Schmid's sliding-paraboloid implementation in
[ImageJ BackgroundSubtracter.java, tag v1.54p](https://github.com/imagej/ImageJ/blob/v1.54p/ij/plugin/filter/BackgroundSubtracter.java).
[ImageJ is public-domain software](https://imagej.net/ij/docs/intro.html).
The reference runner used the downloadable ImageJ 1.54g distribution from
`https://wsr.imagej.net/distros/cross-platform/ij154.zip`.

The implementation retains float32 arithmetic, the separable maximum and mean
filters, the maximum-filter offset correction, corner correction, the exact
directional pass order, and unclipped output. Numba compiles Python locally;
no Java, FIJI, executable discovery, macro generation, or subprocess is used
for production subtraction. The old `fiji` and `paraboloid` command-line mode
names are compatibility aliases for the Python implementation. Cache version
23 forces remeasurement instead of reusing results from the previous fallback.

R remains a separate dependency for the existing statistical plotting path;
this change removes the FIJI dependency.
