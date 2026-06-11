"""Unit tests for the pure alert-extraction helper. No LLM, no network.

Lives under demo/tests/ so it is NOT collected by the core suite (pytest
testpaths = ["tests"]); run explicitly with `uv run python -m pytest demo/tests`.
"""
from demo.agent import _classify_error, _extract_alerts


def test_classify_error_quota():
    for msg in ["429 RESOURCE_EXHAUSTED", "You exceeded your current quota",
                "billing account not configured", "monthly credit exceeded"]:
        assert _classify_error(Exception(msg)) == "quota", msg


def test_classify_error_transient():
    for msg in ["RemoteProtocolError: Server disconnected", "503 UNAVAILABLE", "random boom"]:
        assert _classify_error(Exception(msg)) == "transient", msg


def test_plain_dict():
    assert _extract_alerts({"ok": True, "alerts": ["a", "b"]}) == ["a", "b"]


def test_query_shape():
    assert _extract_alerts({"answer": "...", "alerts": ["x"]}) == ["x"]


def test_nested_and_json_string():
    # ADK can wrap the MCP result under content keys or hand it back as JSON text.
    payload = {"result": {"content": '{"ok": true, "alerts": ["x"]}'}}
    assert _extract_alerts(payload) == ["x"]

    listed = {"content": [{"type": "text", "text": '{"alerts": ["y", "z"]}'}]}
    assert _extract_alerts(listed) == ["y", "z"]


def test_dedupe_preserves_order():
    assert _extract_alerts({"alerts": ["a", "b", "a"]}) == ["a", "b"]


def test_empty_and_missing():
    assert _extract_alerts({"ok": True}) == []
    assert _extract_alerts({"alerts": []}) == []
    assert _extract_alerts("not json") == []
    assert _extract_alerts(None) == []
