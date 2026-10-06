"""Dilution layouts: any number of levels, through the time course.

The time course used to score exactly three dilution levels -- `ROW_SETS` of
(0, 3), (1, 4), (2, 5) -- and every downstream step (tidy output, montage
blocks, sheet names, the review tool's rebuild) assumed the same three. A
`DilutionLayout` now carries whatever levels a plate template declares.

Two properties are tested:

  1. `classic_layout()` is exactly the old arithmetic, so a lab-standard run is
     unchanged -- scores, tidy frames, cache keys and filenames;
  2. other layouts are scored, named and cached correctly.

The engine is stubbed wherever a photograph would be read: what is under test is
which spots are read and how they are labelled, not the optics.
"""

from dataclasses import replace
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
pd = pytest.importorskip("pandas")

import spotting_batch as sb  # noqa: E402
import spotting_quant as sq  # noqa: E402
import spotting_timecourse as tc  # noqa: E402
import spotting_timecourse_figures as tcf  # noqa: E402


def uniform_layout(levels: int, reps: int, plates=(1, 2), rows=6, cols=8):
    """`levels` levels, each spotted `reps` times down the plate, blocks in order."""
    out = []
    for d in range(levels):
        per_plate = []
        for p in plates:
            cells = tuple(
                sb.LevelCell(block * levels + d, c, c + 1,
                             (p - 1) * reps + block + 1)
                for block in range(reps) for c in range(cols)
                if block * levels + d < rows)
            per_plate.append((p, cells))
        out.append(sb.DilutionLevel(d, f"level {d + 1}", tuple(per_plate)))
    return sb.DilutionLayout(tuple(out), rows, cols)


class FakePlate:
    """`PlateData` stand-in: net[r, c] = r * 100 + c + 10, so reads are traceable."""

    def __init__(self, plate, rows=6, cols=8, path="p.JPG"):
        self.ref = sb.PhotoRef(Path(path), "TC", plate, "TC")
        self.net = np.array([[r * 100 + c + 10 for c in range(cols)]
                             for r in range(rows)], float)
        self.rim = np.zeros((rows, cols), bool)
        self.bg_samples = np.ones(5)
        self.radius = 50.0
        self.centers = np.zeros((rows, cols, 2))


# --- the classic layout is the old table ------------------------------------


def test_classic_levels_are_the_dilutions_table():
    layout = sb.classic_layout()
    assert layout.names == sb.DILUTION_ORDER
    for level in layout.levels:
        for plate in (1, 2):
            assert level.rows(plate) == tuple(sb.DILUTIONS[level.name])
            assert level.quant_rows(plate) == tuple(
                r + 1 for r in sb.DILUTIONS[level.name])


def test_classic_replicates_are_the_old_formula():
    for level in sb.classic_layout().levels:
        for plate in (1, 2):
            for cell in level.cells(plate):
                assert cell.replicate == (plate - 1) * 2 + 1 + cell.row // 3
                assert cell.slot == cell.col + 1


def test_classic_row_sets_are_the_precomputed_pairs():
    assert sb.classic_layout().row_sets(1) == sb.ALL_ROW_SETS


def test_the_module_constants_still_agree_with_the_classic_layout():
    layout = sb.classic_layout()
    assert [lv.rows(1) for lv in layout.levels] == [tuple(r) for r in tc.ROW_SETS]
    assert layout.names == tc.ROW_NAMES


@pytest.mark.parametrize("name", ["least", "middle", "most"])
def test_the_classic_tidy_builders_agree(name):
    """`build_tidy_level` must equal `build_tidy` cell for cell on the lab design."""
    plates = [FakePlate(1), FakePlate(2)]
    strains = ["WT", "a", None, "c", "d", "e", "f", "g"]
    old = sb.build_tidy("4|GLY", plates, strains, 1,
                        {"mode": "combo", "choice": name}, [5])
    new = sb.build_tidy_level(plates, strains, 1,
                              sb.classic_layout().by_name(name), [5],
                              experiment="Set 4 GLY", treatment="GLY",
                              set_label="4")
    assert list(old.columns) == list(new.columns)
    assert old.shape == new.shape
    for col in old.columns:
        same = (old[col] == new[col]) | (old[col].isna() & new[col].isna())
        assert same.all(), col


# --- lookups ------------------------------------------------------------------


def test_levels_are_found_by_name_or_rows():
    layout = sb.classic_layout()
    assert layout.by_name("MIDDLE").index == 1
    assert layout.by_rows((1, 4)).name == "middle"
    with pytest.raises(KeyError):
        layout.by_name("nonesuch")
    with pytest.raises(KeyError):
        layout.by_rows((0, 1))


def test_a_level_absent_from_a_plate_says_so():
    with pytest.raises(KeyError, match="plate 9"):
        sb.classic_layout().levels[0].cells(9)


def test_as_level_accepts_both_forms():
    level = sb.classic_layout().levels[2]
    assert tc.as_level(level) is level
    assert tc.as_level((2, 5)) == level


# --- other layouts --------------------------------------------------------------


@pytest.mark.parametrize("levels, reps, block", [(3, 2, 3), (6, 1, 6), (2, 3, 2)])
def test_regular_layouts_report_their_replicate_blocks(levels, reps, block):
    assert uniform_layout(levels, reps).block_rows() == block


def test_an_irregular_layout_has_no_replicate_blocks():
    """Levels out of block order: drawn one block per plate instead."""
    base = uniform_layout(3, 2)
    swapped = tuple(
        sb.DilutionLevel(lv.index, lv.name, tuple(
            (p, tuple(replace(c, row=5 - c.row) if lv.index == 0 else c
                      for c in cells))
            for p, cells in lv.plates))
        for lv in base.levels)
    assert sb.DilutionLayout(swapped, 6, 8).block_rows() is None


def test_six_levels_each_occupy_one_row():
    layout = uniform_layout(6, 1)
    assert [lv.rows(1) for lv in layout.levels] == [(r,) for r in range(6)]
    assert layout.row_sets(1) == [(r + 1,) for r in range(6)]


def test_a_layout_round_trips_through_json():
    import json

    layout = uniform_layout(6, 1, rows=12, cols=16)
    again = sb.DilutionLayout.from_dict(json.loads(json.dumps(layout.to_dict())))
    assert again == layout


def test_a_layout_is_hashable_and_picklable():
    """It keys `_cached_measure` and crosses into worker processes."""
    import pickle

    layout = uniform_layout(6, 1)
    hash(layout)
    assert pickle.loads(pickle.dumps(layout)) == layout


def test_only_the_lab_design_is_classic():
    assert sb.classic_layout().is_classic()
    assert not uniform_layout(6, 1).is_classic()
    assert not uniform_layout(3, 2, rows=6, cols=12).is_classic()


# --- the time course scores whatever levels there are -----------------------


@pytest.fixture
def fake_cache(monkeypatch):
    """Serve `_cached_measure` from traceable fake plates, recording requests."""
    calls = []

    def fake(path, plate, rows, cache_dir, layout=None):
        calls.append((str(path), plate, tuple(rows), layout))
        grid = (layout.n_rows, layout.n_cols) if layout else (6, 8)
        return FakePlate(plate, *grid, path=path)

    monkeypatch.setattr(tc, "_cached_measure", fake)
    return calls


def candidate():
    p1 = tc.Shot(Path("a/p1.JPG"), 40.0, "40 Hours", "GLY", "Glycerol", 1)
    p2 = tc.Shot(Path("a/p2.JPG"), 40.0, "40 Hours", "GLY", "Glycerol", 2)
    return {"medium": "GLY", "medium_label": "Glycerol", "tp_label": "40 Hours",
            "hours": 40.0, "plate1": p1, "plate2": p2}


STRAINS = ["WT", "a", "b", "c", "d", "e", "f", "g"]
CFG = {"strains": STRAINS, "control_col": 1, "exclude": [],
       "media": {"GLY": {"control_col": 1, "exclude": []}}}


def test_every_level_of_a_six_level_design_is_scored(fake_cache):
    """Six levels, spotted twice down a 12-row plate: four replicates a strain."""
    layout = uniform_layout(6, 2, rows=12)
    for level in layout.levels:
        m = tc.score_candidate(candidate(), level, Path("c"), STRAINS, 1,
                               layout=layout)
        assert m is not None, level.name
    # Each level asked for its own two rows, on both plates.
    asked = {(plate, rows) for _p, plate, rows, _l in fake_cache}
    assert asked == {(p, (d + 1, d + 7)) for p in (1, 2) for d in range(6)}


def test_fewer_than_three_replicates_cannot_be_scored(fake_cache):
    """A floor of the statistics, not of the layout: n < 3 has no CV or t-test.

    Six levels spotted ONCE per plate leave two replicates across the pairing.
    """
    layout = uniform_layout(6, 1)
    assert tc.score_candidate(candidate(), layout.levels[0], Path("c"),
                              STRAINS, 1, layout=layout) is None


def test_scoring_reads_the_levels_own_rows(fake_cache):
    """Relative growth on a one-row level is (row*100 + col + 10)/(row*100 + 10)."""
    layout = uniform_layout(6, 1)
    level = layout.levels[4]
    tidy = tc.build_tidy_for_candidate(candidate(), level, CFG, Path("c"),
                                       "Set04", layout)
    assert set(tidy["dilution_row"]) == {5}
    assert set(tidy["dilution"]) == {"level 5"}
    assert set(tidy["raw_growth"]) == {400.0 + c + 10 for c in range(8)}
    assert sorted(tidy["replicate"].unique()) == ["rep1", "rep2"]


def test_a_level_spotted_three_times_scores_three_replicates_per_plate(fake_cache):
    layout = uniform_layout(2, 3)
    tidy = tc.build_tidy_for_candidate(candidate(), layout.levels[1], CFG,
                                       Path("c"), "Set04", layout)
    assert sorted(tidy["dilution_row"].unique()) == [2, 4, 6]
    assert sorted(tidy["replicate"].unique()) == [f"rep{i}" for i in range(1, 7)]


def test_a_bigger_grid_scores_every_column(fake_cache):
    layout = uniform_layout(4, 3, rows=12, cols=16)
    cfg = {**CFG, "strains": [f"s{i}" for i in range(1, 17)]}
    tidy = tc.build_tidy_for_candidate(candidate(), layout.levels[0], cfg,
                                       Path("c"), "Set04", layout)
    assert sorted(tidy["strain_col"].unique()) == list(range(1, 17))


def test_jobs_carry_the_grid_and_every_row_choice():
    layout = uniform_layout(6, 1, rows=12, cols=16)
    jobs = tc.build_jobs([candidate()], Path("c"), layout)
    assert len(jobs) == 2
    path, want, cache, rows, cols, sets = jobs[0]
    assert (rows, cols) == (12, 16)
    assert len(sets) == 6 and want == sets[0]


def test_classic_jobs_keep_their_old_shape():
    jobs = tc.build_jobs([candidate()], Path("c"))
    assert all(len(j) == 3 for j in jobs)
    assert tc.build_jobs([candidate()], Path("c"), sb.classic_layout()) == jobs


# --- sheet names ------------------------------------------------------------------


def test_classic_sheet_names_are_unchanged():
    level = sb.classic_layout().levels[1]
    assert tcf.candidate_id(candidate(), level) == "GLY_40_Hours_p1-p2_middle"
    assert tcf.candidate_id(candidate(), (1, 4)) == "GLY_40_Hours_p1-p2_middle"
    assert tcf._sort_prefix(candidate(), level) == "0040.0h_d1_"


def test_sheet_names_follow_the_designs_levels():
    layout = uniform_layout(6, 1)
    level = layout.levels[5]
    assert tcf.candidate_id(candidate(), level, layout).endswith("_level_6")
    assert tcf._sort_prefix(candidate(), level, layout) == "0040.0h_d5_"


def test_more_than_ten_levels_zero_pad_so_they_sort():
    layout = uniform_layout(12, 1, rows=12)
    names = [tcf._sort_prefix(candidate(), lv, layout) for lv in layout.levels]
    assert names == sorted(names)
    assert names[10] == "0040.0h_d10_" and names[2] == "0040.0h_d02_"


def test_a_scored_row_finds_its_level_by_name():
    layout = uniform_layout(6, 1)
    assert tcf._rows_from_row({"dilution": "level 3"}, layout).index == 2
    with pytest.raises(ValueError, match="unrecognised"):
        tcf._rows_from_row({"dilution": "level 9"}, layout)


def test_the_montage_marks_the_level_within_its_block():
    classic = sb.classic_layout()
    assert [tcf._mark_row(lv, classic) for lv in classic.levels] == [0, 1, 2]
    six = uniform_layout(6, 1)
    assert [tcf._mark_row(lv, six) for lv in six.levels] == [0, 1, 2, 3, 4, 5]


def mega_layout(declared_levels=3):
    """96MegaPlate as photographed template-top-left: 8 x 12, 24 strains, each
    in a 4-spot run along a row, all at one dilution; the other declared
    levels place nothing."""
    cells = tuple(sb.LevelCell(r, c, (c // 4) * 8 + (7 - r) + 1, c % 4 + 1)
                  for r in range(8) for c in range(12))
    levels = [sb.DilutionLevel(0, "level 1", ((1, cells),))]
    levels += [sb.DilutionLevel(i, f"level {i + 1}", ())
               for i in range(1, declared_levels)]
    return sb.DilutionLayout(tuple(levels), 8, 12)


def test_a_single_spotted_level_is_not_outlined():
    layout = mega_layout()
    assert [lv.index for lv in layout.populated_levels()] == [0]
    assert tcf._single_level(layout)
    assert tcf._mark_row(layout.levels[0], layout) is None
    assert not tcf._single_level(sb.classic_layout())


def test_an_irregular_level_outlines_every_row_it_occupies():
    cells = {0: [], 1: []}
    for r in range(8):                     # level 0 on rows 0-3, level 1 on 4-7
        cells[r // 4].extend(sb.LevelCell(r, c, c + 1, 1) for c in range(12))
    layout = sb.DilutionLayout(tuple(
        sb.DilutionLevel(i, f"level {i + 1}", ((1, tuple(cells[i])),))
        for i in (0, 1)), 8, 12)
    assert layout.block_rows() is None
    assert tcf._mark_row(layout.levels[1], layout) == {1: (4, 5, 6, 7)}


def test_montage_labels_follow_the_layout_not_the_slot_index():
    import spotting_montage as sm

    strains = [f"S{i}" for i in range(1, 25)]
    per_col = sm.column_strains(1, 8, 12, strains, mega_layout())
    assert per_col[0] == [f"S{i}" for i in range(8, 0, -1)]
    assert per_col[4] == [f"S{i}" for i in range(16, 8, -1)]
    spans = sm.label_spans(per_col)
    assert [(a, b) for a, b, _ in spans] == [(0, 3), (4, 7), (8, 11)]
    # The lab design is still one strain per column, by slot.
    classic = sm.column_strains(1, 3, 8, strains[:8], sb.classic_layout())
    assert classic == [[s] for s in strains[:8]]
    assert len(sm.label_spans(classic)) == 8


def _block_slots(layout, plate=1):
    return {rc: c.slot for rc, c in layout.cell_at(plate).items()}


def test_a_strain_run_along_a_row_is_labelled_beside_its_row():
    """96MegaPlate on its side: names go left of each 4-spot run, one lane
    per run of columns -- not stacked over the top of the plate."""
    import spotting_montage as sm

    strains = [f"S{i}" for i in range(1, 25)]
    plan = sm.plan_labels([_block_slots(mega_layout())], strains)
    assert plan.mode == "rows"
    assert plan.groups[0] == [(0, 3), (4, 7), (8, 11)]
    labels = {(g, r0): names[0] for g, r0, r1, names in plan.labels[0]}
    assert labels[(0, 0)] == "S8" and labels[(0, 7)] == "S1"
    assert labels[(1, 0)] == "S16" and labels[(2, 7)] == "S17"
    single, by_col, _ = sm.block_replicates(
        {rc: c.replicate for rc, c in mega_layout().cell_at(1).items()})
    assert single is None and by_col[0] == 1 and by_col[7] == 4


def test_a_strain_run_down_a_column_is_labelled_above_its_column():
    """The same plate photographed template-top-up: 12 x 8, runs down the
    columns, so the block splits between the three bands of strains."""
    import spotting_montage as sm

    at = {(r, c): (r // 4) * 8 + c + 1 for r in range(12) for c in range(8)}
    plan = sm.plan_labels([at], [f"S{i}" for i in range(1, 25)])
    assert plan.mode == "columns"
    assert plan.groups[0] == [(0, 3), (4, 7), (8, 11)]
    assert (2, 0, 0, ("S17",)) in plan.labels[0]


def test_the_lab_design_keeps_one_header_over_its_columns():
    import spotting_montage as sm

    classic = sb.classic_layout()
    at = _block_slots(classic)
    blocks = [{(i, j): s for (i, j), s in at.items() if i < 3},
              {(i - 3, j): s for (i, j), s in at.items() if i >= 3}]
    plan = sm.plan_labels(blocks, [f"S{i}" for i in range(1, 9)])
    assert plan.mode == "columns"
    assert plan.groups == [[(0, 2)], [(0, 2)]]
    assert [lab[1] for lab in plan.labels[0]] == list(range(8))


def test_a_strain_that_is_not_a_rectangle_falls_back_to_stacking():
    import spotting_montage as sm

    at = {(0, 0): 1, (1, 0): 1, (1, 1): 1, (0, 1): 2}
    assert sm.plan_labels([at], ["A", "B"]).mode == "stacked"


@pytest.mark.parametrize("which", ["mega", "classic"])
def test_the_montage_draws_with_its_planned_labels(tmp_path, monkeypatch, which):
    import types
    import spotting_montage as sm

    if which == "mega":
        layout, shape, plates_n = mega_layout(), (8, 12), [1]
    else:
        layout, shape, plates_n = None, (6, 8), [1, 2]
    rows, cols = shape
    plates = [types.SimpleNamespace(ref=types.SimpleNamespace(plate=p),
                                    centers=np.zeros((rows, cols, 2)))
              for p in plates_n]

    def fake_blocks(pd_, opts, radius=None, proc=None, layout=None,
                    whole=False):
        per = sm.block_height(pd_, layout, whole)
        h = int(round((per - 1 + 2 * sm.PAD_CELLS) * sm.CELL_PX))
        w = int(round((cols - 1 + 2 * sm.PAD_CELLS) * sm.CELL_PX))
        n = rows // per
        return ([np.full((h, w), 40, np.float32)] * n,
                [np.ones((h, w), bool)] * n)

    monkeypatch.setattr(sm, "plate_blocks", fake_blocks)
    out = sm.build_montage("TC|x", plates, [f"S{i}" for i in range(1, 25)],
                           sq.MeasureOptions(), tmp_path / "m.png",
                           rep_label="Replicate", mark_row=0,
                           mark_label="quantified", layout=layout)
    assert out.exists() and out.stat().st_size > 0


def _plate_data(plate, rows, cols):
    return sb.PlateData(
        ref=sb.PhotoRef(Path(f"p{plate}.JPG"), "TC", plate, "TC"),
        net=np.zeros((rows, cols)), rim=np.zeros((rows, cols), bool),
        bg_mean=0.0, spread=0.0,
        centers=np.stack(np.meshgrid(np.arange(rows) * 100.0,
                                     np.arange(cols) * 100.0, indexing="ij"),
                         axis=-1),
        radius=30.0, bg_samples=np.zeros(5), quant_rows=(),
        plate_center=(350.0, 550.0), plate_radius=900.0)


def test_a_quarter_turn_moves_every_cell_with_the_plate():
    import spotting_montage as sm

    pd_ = _plate_data(1, 8, 12)
    turned = sm.rotate_plate(pd_)
    lay = sm.rotate_layout(mega_layout(), [pd_])
    assert turned.centers.shape[:2] == (12, 8)
    assert (lay.n_rows, lay.n_cols) == (12, 8)
    # Cell (r, c) of the plate is cell (11 - c, r) of the turned plate, and
    # the turned grid's centre there is the same spot of the photograph.
    before = mega_layout().cell_at(1)
    after = lay.cell_at(1)
    for (r, c), cell in before.items():
        assert after[(11 - c, r)].slot == cell.slot
        assert (turned.centers[11 - c, r] == pd_.centers[r, c]).all()


def test_labels_are_replanned_for_the_turned_plate():
    """Turned upright, the 96-well's runs go down the columns, so the names
    move from beside the rows to above three bands of columns."""
    import spotting_montage as sm

    lay = sm.rotate_layout(mega_layout(), [_plate_data(1, 8, 12)])
    plan = sm.plan_labels([_block_slots(lay)], [f"S{i}" for i in range(1, 25)])
    assert plan.mode == "columns"
    assert plan.groups[0] == [(0, 3), (4, 7), (8, 11)]


def test_the_turned_montage_is_drawn_beside_the_photographed_one(tmp_path,
                                                                 monkeypatch):
    from PIL import Image
    import spotting_montage as sm

    def blocks(pd_, opts, radius=None, proc=None, layout=None, whole=False):
        rows, cols = pd_.centers.shape[:2]
        per = sm.block_height(pd_, layout, whole)
        h = int(round((per - 1 + 2 * sm.PAD_CELLS) * sm.CELL_PX))
        w = int(round((cols - 1 + 2 * sm.PAD_CELLS) * sm.CELL_PX))
        n = rows // per
        return ([np.full((h, w), 40, np.float32)] * n,
                [np.ones((h, w), bool)] * n)

    monkeypatch.setattr(sm, "plate_blocks", blocks)
    out, rot = tmp_path / "m.png", tmp_path / "m_rot.png"
    sm.build_montage("TC|x", [_plate_data(1, 8, 12)],
                     [f"S{i}" for i in range(1, 25)], sq.MeasureOptions(), out,
                     layout=mega_layout(), dpi=60, rotated_path=rot)
    with Image.open(out) as a, Image.open(rot) as b:
        assert a.width > a.height            # 12 across
        assert b.height > b.width            # turned: 12 down


def test_the_turned_copy_outlines_the_quantified_columns():
    import spotting_montage as sm

    plates = [_plate_data(p, 6, 8) for p in (1, 2)]
    assert sm._plate_mark_rows(plates, None, 1) == {1: (1, 4), 2: (1, 4)}


# --- the cache merges rather than evicts --------------------------------------------


def test_a_cache_miss_adds_to_the_file_instead_of_replacing_it(tmp_path, monkeypatch):
    """Two designs over one photo used to evict each other's entries forever."""
    photo = tmp_path / "p.JPG"
    photo.write_bytes(b"x")
    ref = sb.PhotoRef(photo, "TC", 1, "TC")
    measured = []

    def fake_multi(path, opts, row_sets, **kw):
        measured.append([tuple(r) for r in row_sets])
        grid = sq.Grid(centers=np.zeros((6, 8, 2)), spot_radius=10.0,
                       measure_radius=9.0, largest_diameter=20.0,
                       plate_center=(0.0, 0.0), plate_radius=100.0)
        m = sq.PlateMeasurement(raw=np.ones((6, 8)), net=np.ones((6, 8)),
                                bg_mean=0.0, bg_samples=[1.0] * 5,
                                n_px=np.ones((6, 8), int), bg_n_px=[1] * 5,
                                rim_flag=np.zeros((6, 8), bool))
        out = {tuple(r): (grid, m) for r in row_sets}
        out["_shared"] = (None, None, 0.0, None)
        return out

    monkeypatch.setattr(sq, "analyze_image_multi", fake_multi)
    opts = replace(sq.MeasureOptions(), quant_rows=(1,))
    sb.measure(ref, opts, tmp_path, row_sets=[(1,), (2,)])
    sb.measure(ref, replace(opts, quant_rows=(3,)), tmp_path, row_sets=[(3,)])
    # The first pass's choices must still be served without measuring again.
    sb.measure(ref, replace(opts, quant_rows=(1,)), tmp_path, row_sets=[(1,)])
    assert measured == [[(1,), (2,)], [(3,)]]
