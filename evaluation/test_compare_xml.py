"""Tests guaranteeing compare_xml judges behavior, not structure.

testcases/ follows a naming convention that doubles as the test expectation:
<number>_<name>.xml is a base workflow, <number>_<name>_eq_<reason>.xml must
behave the same as its base, and <number>_<name>_ne_<reason>.xml must not.
Tests are generated from the filenames, so adding a testcase is just dropping
a file in testcases/. Run with:

    uv run pytest evaluation/ -q
"""

import re
from pathlib import Path

import pytest

from compare_xml import same_behavior

HERE = Path(__file__).parent
CASES = HERE / "testcases"

_VARIANT = re.compile(r"^(.+?)_(eq|ne)_.+\.xml$")

EQ = sorted(p.name for p in CASES.glob("*_eq_*.xml"))
NE = sorted(p.name for p in CASES.glob("*_ne_*.xml"))


def _base(variant: str) -> str:
    return _VARIANT.match(variant).group(1) + ".xml"


def _read(name: str) -> str:
    return (CASES / name).read_text(encoding="utf-8")


def test_naming_convention():
    """Every testcase is a base or an _eq_/_ne_ variant whose base exists."""
    assert EQ and NE, "no testcases found"
    for name in EQ + NE:
        assert (CASES / _base(name)).is_file(), f"{name} has no base file"


@pytest.mark.parametrize("variant", EQ, ids=lambda n: n.removesuffix(".xml"))
def test_equivalent(variant):
    """An _eq_ variant behaves the same as its base workflow."""
    assert same_behavior(_read(_base(variant)), _read(variant))


@pytest.mark.parametrize("variant", NE, ids=lambda n: n.removesuffix(".xml"))
def test_different(variant):
    """A _ne_ variant does not behave the same as its base workflow."""
    assert not same_behavior(_read(_base(variant)), _read(variant))
