import json
from pathlib import Path

from plate_template.cli import main

ROOT = Path(__file__).resolve().parents[2]
GOLDEN = ROOT / "plate_template" / "templates" / "lab_standard_8x6.json"


def test_check_accepts_the_shipped_preset(capsys):
    assert main(["check", str(GOLDEN)]) == 0
    out = capsys.readouterr().out
    assert "OK" in out
    assert "0 error(s)" in out


def test_check_reports_errors_and_exits_one(tmp_path, capsys):
    broken = json.loads(GOLDEN.read_text(encoding="utf-8"))
    # Designate a control that is not spotted anywhere on plate 1.
    broken["plate_slots"][0]["control_slot"] = 9
    target = tmp_path / "broken.json"
    target.write_text(json.dumps(broken), encoding="utf-8")

    assert main(["check", str(target)]) == 1
    out = capsys.readouterr().out
    assert "no_control_on_plate" in out
    assert "NOT ANALYSABLE" in out


def test_check_reports_an_unreadable_file_as_two(tmp_path, capsys):
    target = tmp_path / "nope.json"
    target.write_text("{not json", encoding="utf-8")
    assert main(["check", str(target)]) == 2
    assert "not valid JSON" in capsys.readouterr().err


def test_quiet_hides_warnings(tmp_path, capsys):
    doc = json.loads(GOLDEN.read_text(encoding="utf-8"))
    doc["plate_slots"][0]["cells"][0][0] = "?"  # one unassigned cell
    target = tmp_path / "warn.json"
    target.write_text(json.dumps(doc), encoding="utf-8")

    main(["check", "--quiet", str(target)])
    out = capsys.readouterr().out
    assert "unassigned_cells" not in out


def test_worst_exit_code_wins_across_files(tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert main(["check", str(GOLDEN), str(bad)]) == 2


def test_no_command_prints_help(capsys):
    assert main([]) == 0
    assert "usage" in capsys.readouterr().out.lower()
