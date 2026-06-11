from __future__ import annotations

from pathlib import Path

import pytest

from pacl.substrate import LocalSubstrate, Substrate


@pytest.fixture
def substrate(tmp_path: Path) -> Substrate:
    return LocalSubstrate(root=tmp_path)


def test_write_then_read_roundtrip(substrate: Substrate):
    substrate.write("agents/dylan-coding.md", "# Dylan Coding\n\nintent: fix bug")
    content = substrate.read("agents/dylan-coding.md")
    assert content == "# Dylan Coding\n\nintent: fix bug"


def test_read_missing_returns_none(substrate: Substrate):
    assert substrate.read("agents/nonexistent.md") is None


def test_list_returns_paths_in_directory(substrate: Substrate):
    substrate.write("agents/a.md", "A")
    substrate.write("agents/b.md", "B")
    substrate.write("events/2026-05-22.md", "E")
    listing = sorted(substrate.list("agents"))
    assert listing == ["agents/a.md", "agents/b.md"]


def test_list_empty_directory_returns_empty(substrate: Substrate):
    assert list(substrate.list("agents")) == []


def test_append_to_events(substrate: Substrate):
    substrate.append("events/2026-05-22.md", "## entry 1\n")
    substrate.append("events/2026-05-22.md", "## entry 2\n")
    content = substrate.read("events/2026-05-22.md")
    assert content == "## entry 1\n## entry 2\n"


def test_delete_removes_file(substrate: Substrate):
    substrate.write("agents/x.md", "X")
    substrate.delete("agents/x.md")
    assert substrate.read("agents/x.md") is None
