"""The planner drops selection arguments the run would ignore.

nimo's selection() reads `minimization` only for PHYSBO and the ptr
bounds only for PTR, so those arguments must not survive on other methods —
otherwise two workflows that behave identically render differently.

    uv run pytest evaluation/ -q
"""

import pytest

from nimo_controller.planner import (If, Repeat, ToolCall, Update, Workflow,
                                     build_catalog, normalize, validate)


def sel(**arguments) -> ToolCall:
    return ToolCall(server="nimo", name="selection", arguments=arguments)


@pytest.mark.parametrize("arguments,expected", [
    ({"method": "ES", "minimization": "true"}, {"method": "ES"}),
    ({"method": "RE", "minimization": "false"}, {"method": "RE"}),
    ({"method": "PHYSBO", "minimization": "true"},
     {"method": "PHYSBO", "minimization": "true"}),
    ({"method": "PHYSBO", "minimization": "false"},
     {"method": "PHYSBO", "minimization": "false"}),
    ({"method": "RE", "ptr_lower": "1"}, {"method": "RE"}),
    ({"method": "PTR", "ptr_lower": "1", "ptr_upper": "2"},
     {"method": "PTR", "ptr_lower": "1", "ptr_upper": "2"}),
], ids=lambda v: str(v.get("method", "")) if isinstance(v, dict) else "")
def test_selection_arguments(arguments, expected):
    """Only the methods that read an argument keep it."""
    call = sel(**arguments)
    normalize(Workflow(body=[call]))
    assert call.arguments == expected


def test_normalizes_nested_blocks():
    """Calls inside repeat / if / update are normalized too."""
    inner, branch, measured = (sel(method="ES", minimization="true"),
                               sel(method="DOE", minimization="true"),
                               ToolCall(server="s", name="t", arguments={}))
    normalize(Workflow(body=[Repeat(times=2, counter="i", body=[
        inner,
        If(counter="i", op="<", value=1, then=[branch],
           otherwise=[Update(tool=measured)]),
    ])]))
    assert inner.arguments == {"method": "ES"}
    assert branch.arguments == {"method": "DOE"}


def test_validate_normalizes():
    """validate() is the hook the planner and the XML parser both run."""
    catalog = build_catalog([
        {"server_id": "nimo", "name": "selection",
         "input_schema": {"properties": {
             "method": {"enum": ["RE", "ES", "PHYSBO", "PTR"]},
             "minimization": {"type": "boolean"}}}},
    ], ["x"])
    call = sel(method="ES", minimization="true")
    validate(Workflow(body=[call]), catalog)   # its verdict is beside the point
    assert call.arguments == {"method": "ES"}
