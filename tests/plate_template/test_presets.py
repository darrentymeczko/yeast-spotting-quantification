from plate_template.model import UNASSIGNED
from plate_template.presets import blank, lab_standard_8x6, list_presets


def test_lab_standard_reproduces_the_hardcoded_arithmetic():
    """The transcription test.

    `src/spotting_batch.py` derives a spot's meaning from its position:
        strain  = column                        (:755-756)
        rep_no  = (plate - 1) * 2 + rep_idx     (:754)
        dilution = DILUTION_ORDER[row % 3]      (:763)
    The shipped preset must reproduce that exactly, cell for cell, before
    anyone rewires the pipeline onto templates.
    """
    t = lab_standard_8x6()
    assert t.rows == 6 and t.cols == 8
    assert [p.id for p in t.plates] == ["1", "2"]

    for plate_number, first_replicate in ((1, 1), (2, 3)):
        plate = t.plate(str(plate_number))
        assert plate.control_slot == 1
        for r in range(6):
            for c in range(8):
                p = plate.get(r, c).placement
                assert p is not None, f"plate {plate_number} r{r} c{c} is not a spot"
                assert p.sample_slot == c + 1
                assert p.replicate == first_replicate + r // 3
                assert p.dilution == r % 3


def test_lab_standard_totals():
    t = lab_standard_8x6()
    assert t.sample_slots() == 8
    assert t.replicates() == {1, 2, 3, 4}
    assert t.spot_count() == 96          # 48 spots on each of 2 plates
    assert t.dilution.levels == 3
    assert t.dilution.labels == ["least", "middle", "most"]
    assert t.dilution.fold == 10


def test_lab_standard_every_dilution_is_scorable():
    # Each plate is fully populated and carries its control, so the analysis
    # is free to pick whichever dilution reads best.
    t = lab_standard_8x6()
    for plate in t.plates:
        assert plate.scorable_dilutions() == {0, 1, 2}


def test_lab_standard_is_deterministic():
    # Fixed id and created date, so the golden file never churns.
    a, b = lab_standard_8x6(), lab_standard_8x6()
    assert a == b
    assert a.id and a.created


def test_blank_is_empty_but_well_formed():
    t = blank()
    assert (t.rows, t.cols) == (6, 8)
    assert len(t.plates) == 1
    assert t.sample_slots() == 0
    assert t.spot_count() == 0
    assert all(c is UNASSIGNED for row in t.plates[0].cells for c in row)
    assert t.id  # a fresh uuid, so results can be traced back to this layout


def test_blank_respects_its_arguments():
    t = blank(rows=4, cols=3, levels=2, name="Custom", plates=2)
    assert (t.rows, t.cols) == (4, 3)
    assert t.name == "Custom"
    assert t.dilution.levels == 2
    assert [p.id for p in t.plates] == ["1", "2"]
    assert all(p.rows == 4 and p.cols == 3 for p in t.plates)


def test_blank_templates_get_distinct_ids():
    assert blank().id != blank().id


def test_list_presets_factories_all_build():
    presets = list_presets()
    assert presets
    for key, label, factory in presets:
        assert key and label
        assert factory().rows > 0
