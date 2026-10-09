from __future__ import annotations

from pathlib import Path

import pytest

from tests.requirement_inventory import BUILT, REQUIREMENTS, NotBuilt, Requirement, problems, scan

TESTS = Path(__file__).parent


def test_every_requirement_is_proven_or_listed_not_built_and_every_marker_is_listed():
    markers, unreadable = scan(TESTS)
    assert problems(REQUIREMENTS, markers, unreadable) == []


@pytest.mark.parametrize(
    ("requirements", "markers", "expected"),
    [
        ((Requirement("A.ONE", "does one thing.", BUILT),), {}, "A.ONE is listed built but no test carries its marker"),
        (
            (Requirement("A.ONE", "does one thing.", NotBuilt("T9")),),
            {"A.ONE": ("test_x.py::test_y",)},
            "A.ONE is listed not built (T9) but test_x.py::test_y prove it: mark it built",
        ),
        ((), {"GONE.1": ("test_x.py::test_y",)}, "test_x.py::test_y name GONE.1, which the inventory does not list"),
        (
            (Requirement("A.ONE", "does one thing.", NotBuilt("T9")), Requirement("A.ONE", "again.", NotBuilt("T9"))),
            {},
            "A.ONE is listed twice",
        ),
        ((Requirement("A.ONE", "one\ntwo", NotBuilt("T9")),), {}, "A.ONE needs a one-line statement"),
        (
            (Requirement("A.ONE", 'he said "go"', NotBuilt("T9")),),
            {},
            "A.ONE quotes someone; a statement is technical only",
        ),
        ((Requirement("A.ONE", "does one thing.", NotBuilt("later")),), {}, "which is not a task id"),
        ((Requirement("a one", "does one thing.", NotBuilt("T9")),), {}, "'a one' is not a requirement id"),
    ],
)
def test_the_gate_names_each_kind_of_drift(requirements, markers, expected):
    assert any(expected in problem for problem in problems(requirements, markers))


def test_a_consistent_inventory_has_no_problem():
    requirements = (Requirement("A.ONE", "does one thing.", BUILT), Requirement("A.TWO", "not yet.", NotBuilt("S0")))
    assert problems(requirements, {"A.ONE": ("test_x.py::test_y",)}) == []


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
