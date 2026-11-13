from __future__ import annotations

from pathlib import Path

import pytest

from tests.requirement_inventory import REQUIREMENTS, Requirement, load, problems, scan

TESTS = Path(__file__).parent


def test_every_requirement_is_proven_or_owed_under_a_task_and_every_marker_is_listed():
    markers, unreadable = scan(TESTS)
    assert problems(REQUIREMENTS, markers, unreadable) == []


@pytest.mark.parametrize(
    ("requirements", "markers", "expected"),
    [
        (
            (Requirement("A.ONE", "does one thing.", "built"),),
            {},
            "A.ONE is listed built but no test carries its marker",
        ),
        (
            (Requirement("A.ONE", "does one thing.", "not built", "T9"),),
            {"A.ONE": ("test_x.py::test_y",)},
            "A.ONE is listed not built (T9) but test_x.py::test_y prove it: mark it built",
        ),
        (
            (Requirement("A.ONE", "does one thing.", "untested", "T29"),),
            {"A.ONE": ("test_x.py::test_y",)},
            "A.ONE is listed untested (T29) but test_x.py::test_y prove it: mark it built",
        ),
        ((), {"GONE.1": ("test_x.py::test_y",)}, "test_x.py::test_y name GONE.1, which the inventory does not list"),
        (
            (
                Requirement("A.ONE", "does one thing.", "open", "O7"),
                Requirement("A.ONE", "again.", "open", "O7"),
            ),
            {},
            "A.ONE is listed twice",
        ),
        ((Requirement("A.ONE", "one\ntwo", "open", "O7"),), {}, "A.ONE needs a one-line statement"),
        (
            (Requirement("A.ONE", 'he said "go"', "open", "O7"),),
            {},
            "A.ONE quotes someone; a statement is technical only",
        ),
        ((Requirement("A.ONE", "does one thing.", "not built", "later"),), {}, "which is not a task id"),
        (
            (Requirement("A.ONE", "does one thing.", "open"),),
            {},
            "A.ONE is listed open under None, which is not a task id",
        ),
        ((Requirement("a one", "does one thing.", "open", "O7"),), {}, "'a one' is not a requirement id"),
        ((Requirement("A.ONE", "does one thing.", "maybe", "O7"),), {}, "A.ONE has the proof 'maybe'"),
        (
            (Requirement("A.ONE", "does one thing.", "built", "T9"),),
            {"A.ONE": ("test_x.py::test_y",)},
            "A.ONE is built and still names the task T9",
        ),
    ],
)
def test_the_gate_names_each_kind_of_drift(requirements, markers, expected):
    assert any(expected in problem for problem in problems(requirements, markers))


def test_a_consistent_inventory_has_no_problem():
    requirements = (
        Requirement("A.ONE", "does one thing.", "built"),
        Requirement("A.TWO", "not yet.", "not built", "S0"),
        Requirement("A.THREE", "unproven.", "untested", "T29"),
        Requirement("A.FOUR", "a question.", "open", "O7"),
    )
    assert problems(requirements, {"A.ONE": ("test_x.py::test_y",)}) == []


def test_the_inventory_file_loads_every_field_and_refuses_an_unknown_one(tmp_path):
    good = tmp_path / "good.toml"
    good.write_text('[[requirement]]\nid = "A.ONE"\nstatement = "does one thing."\nproof = "open"\ntask = "O7"\n')
    assert load(good) == (Requirement("A.ONE", "does one thing.", "open", "O7"),)
    bad = tmp_path / "bad.toml"
    bad.write_text('[[requirement]]\nid = "A.ONE"\nstatement = "x."\nproof = "open"\nowner = "me"\n')
    with pytest.raises(TypeError, match="owner"):
        load(bad)


def test_the_scan_finds_markers_on_functions_methods_and_classes_and_refuses_a_non_literal(tmp_path):
    (tmp_path / "test_sample.py").write_text(
        "import pytest\n"
        "ID = 'X.4'\n"
        "@pytest.mark.req('X.1', 'X.2')\n"
        "def test_a():\n    pass\n"
        "class TestB:\n"
        "    @pytest.mark.parametrize('n', [1])\n"
        "    @pytest.mark.req('X.1')\n"
        "    def test_c(self, n):\n        pass\n"
        "@pytest.mark.req('X.3')\n"
        "class TestD:\n    pass\n"
        "@pytest.mark.req(ID)\n"
        "def test_e():\n    pass\n"
    )
    (tmp_path / "helper.py").write_text("import pytest\n@pytest.mark.req('IGNORED.1')\ndef test_z():\n    pass\n")
    markers, unreadable = scan(tmp_path)
    assert markers == {
        "X.1": ("test_sample.py::test_a", "test_sample.py::test_c"),
        "X.2": ("test_sample.py::test_a",),
        "X.3": ("test_sample.py::TestD",),
    }
    assert unreadable == ("test_sample.py::test_e line 14",)
    assert problems((), {}, unreadable) == [
        "a req marker whose argument is not a literal string, at test_sample.py::test_e line 14"
    ]
