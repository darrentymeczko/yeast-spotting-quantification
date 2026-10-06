"""Combined choices retain the exact saved reading and explicit interpretations."""

from experiments.gui.organization import reading_choices
from experiments.intake import ImageFile
from experiments.profiles import FacetRule, apply_rule, capture_tree


def files(*paths):
    return [ImageFile(p, tuple(p.split("/"))) for p in paths]


def test_current_custom_reading_survives_even_when_no_photos_match():
    current = FacetRule("plate", "stem", -1, (r"dish-(\d+)",), "int")
    options = reading_choices(files("_9.JPG"), "plate", current, {})
    assert any(rule is current for rule in options.values())
    assert "saved custom reading" in next(label for label, rule in options.items() if rule is current)


def test_numeric_hours_and_replicate_plate_meanings_are_explicit_choices():
    photos = files("16/R1/_9.JPG", "40/R2/_9.JPG")
    hours = reading_choices(photos, "timepoint", None, {})
    label, rule = next((label, rule) for label, rule in hours.items()
                       if rule and rule.source == "segment" and rule.depth == -3)
    assert label.startswith("Use numbers") and label.endswith("as hours")
    assert apply_rule(photos[0].parts, rule, {})[0] == 16
    plates = reading_choices(photos, "plate", None, {})
    label, rule = next((label, rule) for label, rule in plates.items()
                       if rule and rule.source == "segment" and rule.depth == -2)
    assert label.startswith("Use R1, R2") and label.endswith("as plate numbers")
    assert apply_rule(photos[1].parts, rule, {})[0] == 2


def test_detected_hours_have_one_folder_choice_and_keep_original_rule():
    photos = files("16 Hours/Glucose/Plate 1/_9.JPG")
    current = capture_tree().rule_for("timepoint")
    options = reading_choices(photos, "timepoint", current, {})
    matches = [(label, rule) for label, rule in options.items()
               if rule and rule.source == "segment" and rule.depth == -4]
    assert len(matches) == 1
    assert matches[0][1] is current
    assert "16 Hours" in matches[0][0]
    assert "Keep detected name format" not in matches[0][0]
