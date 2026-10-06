"""Spot lattices of sizes other than the lab's 8 x 6.

`detect_grid` used to fit exactly N_ROWS x N_COLS, with the expected spot pitch
fixed at `SPACING_FRAC * r_eq` -- a constant calibrated for eight columns. The
grid size is now an argument and the pitch is derived from it, so a 12 x 16
design is looked for at a 12 x 16 pitch instead of being read as a sparse 8 x 6.

Two properties matter and are tested separately:

  1. the DEFAULT grid is untouched -- every number it produces must be what it
     has always been, or every existing result shifts;
  2. other sizes are actually recovered.

(2) is tested against `_fit_lattice` directly, on synthetic centroids, rather
than through `detect_grid` on a synthetic photograph. The lattice fit is the
whole of the generalisation and runs in milliseconds; the surrounding image
pipeline (top-hat, full-resolution re-centring) is slow enough on synthetic
plates to be unusable in a test suite, and is exercised on real photographs by
the calibration harness.
"""

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("skimage")

import spotting_batch as sb  # noqa: E402
import spotting_quant as sq  # noqa: E402


# --- the default grid is untouched ------------------------------------------


def test_the_default_pitch_is_the_calibrated_constant():
    """`expected_pitch` must reproduce SPACING_FRAC exactly for 8 columns."""
    for r_eq in (100.0, 1000.0, 1234.5):
        assert sq.expected_pitch(r_eq) == sq.SPACING_FRAC * r_eq


def test_the_default_spot_radius_is_the_calibrated_constant():
    r_eq = 1000.0
    derived = sq.expected_pitch(r_eq) * sq.SPOT_RADIUS_PER_PITCH
    assert derived == pytest.approx(sq.SPOT_RADIUS_FRAC * r_eq, abs=1e-9)


def test_the_span_constant_is_derived_from_the_calibrated_pitch():
    assert sq.GRID_SPAN_FRAC == pytest.approx(sq.SPACING_FRAC * (sq.N_COLS - 1))


def test_asking_for_the_default_shape_explicitly_changes_nothing():
    cen = _lattice_points(6, 8, pitch=40.0, cx=300.0, cy=300.0)
    a = sq._fit_lattice(cen, 300.0, 300.0, 200.0)
    b = sq._fit_lattice(cen, 300.0, 300.0, 200.0, 6, 8)
    assert np.array_equal(a[1], b[1])
    assert a[0] == b[0] and a[2] == b[2]


def test_the_cache_key_is_unchanged_for_the_default_shape(tmp_path):
    """Appending the grid unconditionally would strand 450 cached plates."""
    photo = tmp_path / "1.1GLU.JPG"
    photo.write_bytes(b"x")
    from dataclasses import replace

    base = sq.MeasureOptions()
    explicit = replace(sq.MeasureOptions(), n_rows=sq.N_ROWS, n_cols=sq.N_COLS)
    assert sb._cache_key(photo, explicit) == sb._cache_key(photo, base)


def test_the_cache_key_changes_with_the_grid(tmp_path):
    """An 8x6 measurement must never be served for a 12x8 request."""
    photo = tmp_path / "1.1GLU.JPG"
    photo.write_bytes(b"x")
    from dataclasses import replace

    base = sq.MeasureOptions()
    keys = {sb._cache_key(photo, base)}
    for rows, cols in ((8, 12), (12, 8), (12, 16)):
        keys.add(sb._cache_key(
            photo, replace(sq.MeasureOptions(), n_rows=rows, n_cols=cols)))
    assert len(keys) == 4


# --- the pitch scales with the grid -----------------------------------------


@pytest.mark.parametrize("rows, cols", [(6, 8), (8, 12), (12, 8), (12, 16),
                                        (16, 24), (4, 4), (2, 2)])
def test_the_grid_always_spans_the_same_part_of_the_plate(rows, cols):
    """A denser grid has a finer pitch, not a wider footprint.

    The frogger prints its grid centred and filling roughly the same area
    whatever its pin count, so this is the invariant, and it is what keeps the
    corner spots inside the eroded agar mask detection searches.
    """
    r_eq = 1000.0
    pitch = sq.expected_pitch(r_eq, rows, cols)
    span = pitch * max(rows - 1, cols - 1) / r_eq
    assert span == pytest.approx(sq.GRID_SPAN_FRAC)


def test_more_positions_mean_a_finer_pitch():
    r_eq = 1000.0
    assert (sq.expected_pitch(r_eq, 6, 8)
            > sq.expected_pitch(r_eq, 8, 12)
            > sq.expected_pitch(r_eq, 12, 16)
            > sq.expected_pitch(r_eq, 16, 24))


def test_a_single_position_does_not_divide_by_zero():
    assert sq.expected_pitch(1000.0, 1, 1) > 0


# --- the lattice fit recovers grids of any size -----------------------------


def _lattice_points(rows, cols, pitch, cx, cy, tilt=0.0, drop=()):
    """The centroids a perfect `rows` x `cols` grid would produce."""
    pts = []
    t = np.radians(tilt)
    for i in range(rows):
        for j in range(cols):
            if (i, j) in drop:
                continue
            y = (i - (rows - 1) / 2) * pitch
            x = (j - (cols - 1) / 2) * pitch
            pts.append([cx + x * np.cos(t) - y * np.sin(t),
                        cy + x * np.sin(t) + y * np.cos(t)])
    return np.array(pts, float)


def _truth(rows, cols, pitch, cx, cy, tilt=0.0):
    out = np.zeros((rows, cols, 2))
    t = np.radians(tilt)
    for i in range(rows):
        for j in range(cols):
            y = (i - (rows - 1) / 2) * pitch
            x = (j - (cols - 1) / 2) * pitch
            out[i, j] = (cy + x * np.sin(t) + y * np.cos(t),
                         cx + x * np.cos(t) - y * np.sin(t))
    return out


def _max_error(rows, cols, *, tilt=0.0, drop=(), r_eq=600.0):
    cx = cy = 700.0
    pitch = sq.expected_pitch(r_eq, rows, cols)
    cen = _lattice_points(rows, cols, pitch, cx, cy, tilt, drop)
    _s, got, _t = sq._fit_lattice(cen, cx, cy, r_eq, rows, cols)
    want = _truth(rows, cols, pitch, cx, cy, tilt)
    err = np.hypot(got[..., 0] - want[..., 0], got[..., 1] - want[..., 1])
    return float(err.max()) / pitch


@pytest.mark.parametrize("rows, cols", [
    (6, 8), (8, 12), (12, 8), (12, 16), (16, 24), (4, 4), (3, 5), (2, 2),
])
def test_a_full_grid_is_recovered(rows, cols):
    assert _max_error(rows, cols) < 0.05


@pytest.mark.parametrize("rows, cols", [(6, 8), (8, 12), (12, 16)])
@pytest.mark.parametrize("tilt", [-5.0, 3.0, 7.0])
def test_a_tilted_grid_is_recovered(rows, cols, tilt):
    assert _max_error(rows, cols, tilt=tilt) < 0.05


@pytest.mark.parametrize("rows, cols", [(6, 8), (8, 12), (12, 16)])
def test_a_grid_missing_a_whole_edge_column_is_recovered(rows, cols):
    """The case `_best_base` exists for, at every grid size."""
    assert _max_error(rows, cols, drop={(i, 0) for i in range(rows)}) < 0.05


@pytest.mark.parametrize("rows, cols", [(8, 12), (12, 16)])
def test_a_grid_missing_a_whole_edge_row_is_recovered(rows, cols):
    assert _max_error(rows, cols, drop={(0, j) for j in range(cols)}) < 0.05


@pytest.mark.parametrize("rows, cols", [(6, 8), (12, 16), (2, 2)])
def test_the_fit_returns_the_shape_it_was_asked_for(rows, cols):
    r_eq = 200.0
    cen = _lattice_points(rows, cols, sq.expected_pitch(r_eq, rows, cols),
                          300.0, 300.0)
    _s, got, _t = sq._fit_lattice(cen, 300.0, 300.0, r_eq, rows, cols)
    assert got.shape == (rows, cols, 2)
    assert np.isfinite(got).all()


def test_a_hopeless_seed_falls_back_instead_of_raising():
    """Spots nowhere near the declared grid's pitch.

    Reachable by declaring the wrong grid size. The fit collapses to a single
    lattice node, and before the guard in `_fit_1d` the next division by a zero
    spacing raised LinAlgError out of numpy.
    """
    cen = _lattice_points(2, 2, 40.0, 300.0, 300.0)      # pitch 40
    _s, got, _t = sq._fit_lattice(cen, 300.0, 300.0, 2000.0, 2, 2)  # expects ~2758
    assert got.shape == (2, 2, 2)
    assert np.isfinite(got).all()


def test_too_few_spots_falls_back_to_a_centred_grid_of_the_right_size():
    cen = np.array([[300.0, 300.0], [340.0, 300.0]])
    _s, got, _t = sq._fit_lattice(cen, 300.0, 300.0, 200.0, 12, 16)
    assert got.shape == (12, 16, 2)
    assert np.isfinite(got).all()


# --- everything downstream follows the grid it is given ----------------------


def _grid(rows, cols, pitch=40.0):
    centers = _truth(rows, cols, pitch, 500.0, 500.0)
    return sq.Grid(centers=centers, spot_radius=pitch * 0.3,
                   measure_radius=pitch * 0.27, largest_diameter=pitch * 0.6,
                   plate_center=(500.0, 500.0), plate_radius=400.0)


@pytest.mark.parametrize("rows, cols", [(6, 8), (12, 16), (2, 2)])
def test_a_grid_reports_its_own_shape(rows, cols):
    assert _grid(rows, cols).shape == (rows, cols)


@pytest.mark.parametrize("rows, cols", [(6, 8), (8, 12), (12, 16)])
def test_measuring_follows_the_grid_shape(rows, cols):
    grid = _grid(rows, cols)
    img = np.full((1000, 1000), 120, dtype=np.uint8)
    m = sq.measure_plate(img, grid, sq.background_gap_centers(grid))
    assert m.raw.shape == (rows, cols)
    assert m.net.shape == (rows, cols)
    assert m.n_px.shape == (rows, cols)
    assert m.rim_flag.shape == (rows, cols)


@pytest.mark.parametrize("rows, cols", [(6, 8), (12, 16), (2, 2)])
def test_background_gaps_are_found_at_any_size(rows, cols):
    grid = _grid(rows, cols)
    pts = sq.background_gap_centers(grid)
    assert len(pts) == sq.N_BG_SAMPLES
    assert all(np.isfinite(p).all() for p in pts)


@pytest.mark.parametrize("shape", [(1, 8), (6, 1), (1, 1)])
def test_a_grid_too_thin_to_have_gaps_reports_none(shape):
    """Every sample is a midpoint BETWEEN two spots; one row has no gap.

    Returning nothing is better than inventing a position that may land on a
    spot and be subtracted from every reading on the plate.
    """
    assert sq.background_gap_centers(_grid(*shape)) == []


def test_the_roi_shrink_respects_the_grids_own_row_count():
    """`quant_rows` used to be filtered against the module's N_ROWS.

    On a 12-row plate that silently dropped rows 7-12 from the ROI sizing.
    """
    rows, cols = 12, 8
    radii = np.full((rows, cols), 10.0)
    exists = np.ones((rows, cols), bool)
    radii[10, :] = 4.0                      # the smallest spot is on row 11
    r, _n, _med = sq.roi_radius_for_rows(radii, exists, (11,), 10.0)
    assert r == pytest.approx(4.0)
