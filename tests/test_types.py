import dataclasses
from pathlib import Path

import pytest

from harness.types import (
    Capabilities,
    ExecRequest,
    ExecResult,
    Preflight,
    RunRow,
    Selection,
    UnitSpec,
    Verdict,
)

ALL = [
    UnitSpec, ExecRequest, ExecResult, Preflight, Capabilities,
    Selection, Verdict, RunRow,
]


def test_all_frozen_dataclasses():
    for cls in ALL:
        assert dataclasses.is_dataclass(cls), cls
        assert cls.__dataclass_params__.frozen, cls


def test_unitspec_is_immutable():
    unit = UnitSpec(id="u", path=Path("."), prompt="p", verify_cmd="true")
    assert unit.kind is None
    with pytest.raises(dataclasses.FrozenInstanceError):
        unit.id = "outro"


def test_backends_base_reexports_same_objects():
    from harness.backends import base

    assert base.Capabilities is Capabilities
    assert base.ExecRequest is ExecRequest
    assert base.ExecResult is ExecResult
    assert base.Preflight is Preflight
