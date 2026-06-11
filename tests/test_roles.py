from __future__ import annotations

from pathlib import Path

import pytest

from pacl import roles
from pacl.substrate import LocalSubstrate


@pytest.fixture
def substrate(tmp_path: Path) -> LocalSubstrate:
    return LocalSubstrate(root=tmp_path)


def test_set_and_get_role_round_trips(substrate):
    roles.set_role(substrate, "ceo-assistant", role="CEO Assistant", authority=roles.DIRECTIVE)
    assert roles.get_role(substrate, "ceo-assistant") == {
        "role": "CEO Assistant",
        "authority": "directive",
    }


def test_unregistered_agent_has_no_authority(substrate):
    """The core enforcement: an agent not in the registry has no authority — no
    matter what it claims in a message."""
    assert roles.get_role(substrate, "rando") is None
    assert roles.has_authority(substrate, "rando") is False


def test_executor_role_has_no_directive_authority(substrate):
    roles.set_role(substrate, "dev-alice", role="Backend Engineer", authority=roles.EXECUTOR)
    assert roles.has_authority(substrate, "dev-alice") is False


def test_directive_role_has_authority(substrate):
    roles.set_role(substrate, "ceo-assistant", role="CEO Assistant", authority=roles.DIRECTIVE)
    assert roles.has_authority(substrate, "ceo-assistant") is True


def test_list_and_clear(substrate):
    roles.set_role(substrate, "a", role="A", authority=roles.DIRECTIVE)
    roles.set_role(substrate, "b", role="B", authority=roles.EXECUTOR)
    listed = roles.list_roles(substrate)
    assert set(listed) == {"a", "b"}
    assert listed["a"]["authority"] == "directive"
    assert listed["b"]["authority"] == "executor"

    roles.clear_roles(substrate)
    assert roles.list_roles(substrate) == {}


def test_agent_id_with_path_chars_is_safe(substrate):
    roles.set_role(substrate, "ns/weird id", role="X", authority=roles.DIRECTIVE)
    assert roles.has_authority(substrate, "ns/weird id") is True


def test_default_authority_is_executor(substrate):
    roles.set_role(substrate, "dev-bob", role="Engineer")
    assert roles.has_authority(substrate, "dev-bob") is False
