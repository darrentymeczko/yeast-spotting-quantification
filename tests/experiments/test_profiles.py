"""The builtin profiles must agree, exactly, with the regexes they replace.

This is the load-bearing test of the whole intake layer. `capture_tree` and
`flat_lab` exist so that today's data keeps producing today's answers, and the
codes they emit end up in result folder names and figure filenames. A silent
drift here would rename every output and orphan every existing result.
"""

import re
import sys
from pathlib import Path

import pytest

from experiments import profiles
from experiments.profiles import (
    FacetRule,
    NamingProfile,
    ProfileError,
    apply_profile,
    condition_code,
    profile_from_dict,
    profile_to_dict,
)

SRC = Path(__file__).resolve().parents[2] / "src"


# --- the originals, copied here so the test does not need numpy to run -------
# `spotting_batch.NAME_RE`
NAME_RE = re.compile(r"^\s*(\d+)\s*\.\s*(\d+)\s*(.+?)\s*$")
# `spotting_timecourse.MEDIUM_CODES`
MEDIUM_CODES = {
    "glucose": "GLU",
    "glycerol": "GLY",
    "potassium acetate": "K-OAc",
    "k-oac": "K-OAc",
    "koac": "K-OAc",
}


def _hours(name: str):
    """`spotting_timecourse._hours`, verbatim."""
    m = re.search(r"(\d+(?:\.\d+)?)\s*h", name.strip(), re.I)
    if m:
        return float(m.group(1))
    m = re.match(r"\s*(\d+(?:\.\d+)?)\s*$", name)
    return float(m.group(1)) if m else None


def _plate_no(name: str):
    """`spotting_timecourse._plate_no`, verbatim."""
    m = re.search(r"plate\s*(\d+)", name, re.I)
    return int(m.group(1)) if m else None


def _medium_code(name: str) -> str:
    """`spotting_timecourse._medium_code`, verbatim."""
    key = re.sub(r"\s+", " ", name.strip().lower())
    if key in MEDIUM_CODES:
        return MEDIUM_CODES[key]
    return re.sub(r"[^A-Za-z0-9-]+", "", name.strip()).upper()[:8] or "MEDIUM"


def test_the_copied_originals_still_match_the_pipeline():
    """Guard the guard: if src/ changes these, this file must change too."""
    text = (SRC / "spotting_timecourse.py").read_text(encoding="utf-8")
    assert 'r"(\\d+(?:\\.\\d+)?)\\s*h"' in text
    assert 'r"plate\\s*(\\d+)"' in text
    assert '"potassium acetate": "K-OAc"' in text
    batch = (SRC / "spotting_batch.py").read_text(encoding="utf-8")
    assert r"^\s*(\d+)\s*\.\s*(\d+)\s*(.+?)\s*$" in batch


# --- flat_lab vs NAME_RE ----------------------------------------------------

FLAT_NAMES = [
    f"{s}.{p}{t}.JPG"
    for s in list(range(1, 11))
    for p in (1, 2)
    for t in ("GLU", "GLY", "K-OAc")
]


@pytest.mark.parametrize("name", FLAT_NAMES)
def test_flat_lab_agrees_with_name_re(name):
    stem = name[:-4]
    m = NAME_RE.match(stem)
    assert m is not None
    got = apply_profile((name,), profiles.flat_lab())
    assert got["set"] == m.group(1)
    assert got["plate"] == int(m.group(2))
    assert got["condition"] == _medium_code(m.group(3).strip())
    # The label is the treatment as written, not the whole filename.
    assert got["condition_label"] == m.group(3).strip()


def test_flat_lab_rejects_a_name_name_re_rejects():
    assert NAME_RE.match("_9") is None
    assert apply_profile(("_9.JPG",), profiles.flat_lab()).get("plate") is None


# --- capture_tree vs the folder walkers -------------------------------------

TREE_CASES = [
    ("40 Hours", "Potassium Acetate", "Plate 1 (Rep 1+2)", "_9.JPG"),
    ("16 Hours", "Glucose", "Plate 2 (Rep 3+4)", "_9_1.JPG"),
    ("114 Hours", "Glycerol", "Plate 1 (Rep 1+2)", "_9_4.JPG"),
    # Guest sessions: lower case, bare plate folders, abbreviated media.
    ("44 hours", "glucose", "plate 1", "_9.JPG"),
    ("22 Hours", "k acetate", "plate 2", "_9_2.JPG"),
]


@pytest.mark.parametrize("parts", TREE_CASES, ids=lambda p: "/".join(p))
def test_capture_tree_agrees_with_the_folder_walkers(parts):
    tp_dir, med_dir, plate_dir, _ = parts
    got = apply_profile(parts, profiles.capture_tree())
    assert got["timepoint"] == _hours(tp_dir)
    assert got["plate"] == _plate_no(plate_dir)
    assert got["condition"] == _medium_code(med_dir)
    # The timepoint label stays the whole folder name: the pipeline names
    # result folders and figure files after it verbatim.
    assert got["timepoint_label"] == tp_dir
    assert got["condition_label"] == med_dir


def test_capture_tree_survives_extra_folder_levels():
    """The case that defeats `spotting_timecourse.discover` today.

    `Set05/Andrea/Set 1/...` puts two extra levels above the timepoint. Negative
    depths are measured from the photo, so the rules still land correctly.
    """
    deep = ("Andrea", "Set 1", "40 Hours", "Glycerol", "Plate 2", "_9.JPG")
    shallow = ("40 Hours", "Glycerol", "Plate 2", "_9.JPG")
    profile = profiles.capture_tree()
    a, b = apply_profile(deep, profile), apply_profile(shallow, profile)
    assert a == b
    assert a["timepoint"] == 40.0 and a["condition"] == "GLY" and a["plate"] == 2


def test_a_path_too_short_reads_nothing_rather_than_guessing():
    got = apply_profile(("40 Hours", "_9.JPG"), profiles.capture_tree())
    assert "timepoint" not in got and "condition" not in got


# --- condition codes --------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["Glucose", "glucose", "GLUCOSE", "Potassium Acetate", "k-oac", "KOAc",
     "Glycerol", " Glycerol ", "Raffinose", "0.5% H2O2", ""],
)
def test_condition_code_matches_medium_code(name):
    assert condition_code(name, profiles.MEDIUM_ALIASES) == (
        _medium_code(name) if name.strip() else "CONDITION"
    )


def test_aliases_collapse_spellings_onto_one_code():
    for spelling in ("Potassium Acetate", "potassium acetate", "K-OAc", "KOAc"):
        assert condition_code(spelling, profiles.MEDIUM_ALIASES) == "K-OAc"


# --- rule validation --------------------------------------------------------


def test_a_pattern_with_no_capture_group_is_rejected():
    rule = FacetRule("plate", "segment", -2, (r"plate \d+",), "int")
    with pytest.raises(ProfileError, match="no capture group"):
        rule.check()


@pytest.mark.parametrize(
    "rule",
    [
        FacetRule("medium", "segment", -2, (r"(.+)",)),
        FacetRule("plate", "elsewhere", -2, (r"(.+)",)),
        FacetRule("plate", "segment", -2, (r"(.+)",), "magic"),
        FacetRule("plate", "segment", -2, ()),
        FacetRule("plate", "segment", -2, (r"(unclosed",)),
    ],
)
def test_malformed_rules_are_rejected(rule):
    with pytest.raises(ProfileError):
        rule.check()


def test_builtins_are_well_formed():
    for key, _ in profiles.list_builtins():
        profiles.builtin(key).check()


def test_unknown_builtin_names_itself():
    with pytest.raises(ProfileError, match="capture_tree"):
        profiles.builtin("nonesuch")


# --- round trip -------------------------------------------------------------


@pytest.mark.parametrize("key", ["capture_tree", "flat_lab"])
def test_profiles_round_trip_through_json(key):
    original = profiles.builtin(key)
    again = profile_from_dict(profile_to_dict(original))
    assert again.rules == original.rules
    assert again.aliases == original.aliases
    assert again.name == original.name


def test_alias_keys_are_normalised_on_the_way_in():
    p = profile_from_dict(
        {"name": "x", "rules": [], "aliases": {"  Potassium   Acetate ": "K-OAc"}}
    )
    assert p.aliases == {"potassium acetate": "K-OAc"}
    assert condition_code("POTASSIUM ACETATE", p.aliases) == "K-OAc"


def test_a_malformed_rule_in_a_saved_profile_names_its_position():
    with pytest.raises(ProfileError, match=r"rules\[1\]"):
        profile_from_dict(
            {
                "rules": [
                    {"facet": "plate", "patterns": [r"plate\s*(\d+)"]},
                    {"facet": "plate", "patterns": ["no group here"]},
                ]
            }
        )
