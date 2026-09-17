"""Tests for the thing that checks the tests.

A runner that reported success whatever happened would be worse than
none: it would turn a claim nobody verified into a claim everybody
trusts. So the runner itself gets tested, against controls that break
code it can reach.
"""

import pytest

from scripts.check_controls import CONTROLS, Control, apply, main


def test_every_declared_break_matches_exactly_once():
    # A break that matches nothing runs the UNMODIFIED code and passes,
    # which has happened by hand in this project more than once. It is
    # the failure mode this whole script exists to catch, so it cannot
    # be allowed in the script's own declarations.
    from scripts.check_controls import ROOT

    for control in CONTROLS:
        text = (ROOT / control.path).read_text()
        assert text.count(control.old) == 1, (
            f"{control.describes!r}: its break matches "
            f"{text.count(control.old)} places in {control.path}")


def test_every_control_names_the_tests_it_expects_to_fail():
    for control in CONTROLS:
        assert control.tests, control.describes
        # Individual tests, not whole files: a control that fires for
        # an unrelated reason should be visible as the wrong test
        # failing.
        assert all("::" in name for name in control.tests), control.tests


def test_the_break_is_reverted_even_when_the_tests_fail(tmp_path, monkeypatch):
    # A broken file left behind would look like a bug in the code
    # rather than in this script, and would be found by somebody else,
    # later, confused.
    from scripts import check_controls

    target = tmp_path / "subject.py"
    target.write_text("VALUE = 1\n")
    monkeypatch.setattr(check_controls, "ROOT", tmp_path)
    monkeypatch.setattr(check_controls, "CONTROLS", [
        Control(describes="a made-up guarantee", path="subject.py",
                old="VALUE = 1", new="VALUE = 2", tests=["tests/nonexistent.py::x"]),
    ])

    main([])
    assert target.read_text() == "VALUE = 1\n"


def test_a_control_that_does_not_fire_is_reported(tmp_path, monkeypatch, capsys):
    # THE point. A control that passes means either the break is not
    # the break it claims or the tests do not check what they appear
    # to -- and in this project it has been the second nine times out
    # of ten.
    from scripts import check_controls

    target = tmp_path / "subject.py"
    target.write_text("VALUE = 1\n")
    monkeypatch.setattr(check_controls, "ROOT", tmp_path)
    monkeypatch.setattr(check_controls, "CONTROLS", [
        Control(describes="a guarantee nothing guards", path="subject.py",
                old="VALUE = 1", new="VALUE = 2", tests=[]),
    ])
    # No tests named, so pytest exits zero and the control is silent.
    monkeypatch.setattr(check_controls, "run", lambda control: (False, "1 passed"))

    assert main([]) == 1
    printed = capsys.readouterr().out
    assert "SILENT" in printed
    assert "a guarantee nothing guards" in printed


def test_a_break_that_matches_nothing_stops_the_run(tmp_path, monkeypatch):
    from scripts import check_controls

    (tmp_path / "subject.py").write_text("VALUE = 1\n")
    monkeypatch.setattr(check_controls, "ROOT", tmp_path)
    control = Control(describes="a break that is not there", path="subject.py",
                      old="NOT PRESENT", new="x", tests=["a::b"])
    with pytest.raises(SystemExit, match="matches 0 times"):
        apply(control)


def test_there_are_controls_to_run():
    # A list that has quietly emptied would pass every assertion above.
    assert len(CONTROLS) >= 10
    assert len({control.describes for control in CONTROLS}) == len(CONTROLS)
