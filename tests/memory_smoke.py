"""Compact memory and focused bookkeeping, using temporary notes and fake metadata."""
import copy
from concurrent.futures import ThreadPoolExecutor
import json


def structured(result):
    assert not result.get("isError"), result
    return result["structuredContent"]


def expect_error(result, text=None):
    assert result.get("isError"), result
    if text is not None:
        message = result.get("structuredContent", {}).get("error", result["content"][0].get("text", ""))
        assert text in message, result


def check_memory(client, root, tools, binary):
    for name in ["workstream_list", "workstream_overview", "workstream_get"]:
        assert tools[name]["annotations"]["readOnlyHint"] is True
    assert tools["workstream_update"]["annotations"]["readOnlyHint"] is False
    assert tools["workstream_update"]["annotations"]["destructiveHint"] is True
    assert tools["workstream_update"]["annotations"]["idempotentHint"] is False
    for name in ["workstream_list", "workstream_overview"]:
        schema = tools[name]["inputSchema"]
        assert schema["properties"]["compact"]["type"] == "boolean"
        assert schema["properties"]["compact"]["default"] is False
        assert "compact" not in schema.get("required", [])
    assert set(tools["workstream_get"]["inputSchema"]["required"]) == {"workstream_id"}
    update_schema = tools["workstream_update"]["inputSchema"]
    assert set(update_schema["required"]) == {"workstream_id", "expected_revision", "change"}
    notes_schema = update_schema["$defs"]["WorkstreamChange"]["oneOf"][0]
    assert notes_schema["required"] == ["type"]
    for field in ["status", "next_action", "blocker"]:
        assert "default" not in notes_schema["properties"][field]
        assert "$ref" in notes_schema["properties"][field]

    def assignment(name, historical=True):
        return {"id": name, "role": "review", "target": {
            "session": "default", "pane_id": "w1:p1", "terminal_id": "term_test", "agent": "pi", "agent_session": None,
        }, "brief": "HISTORY-BRIEF|" + "b" * 2000, "constraints": ["HISTORY-CONSTRAINT|" + "c" * 500],
            "acceptance_criteria": ["HISTORY-CRITERION|" + "a" * 500],
            "report_path": "/tmp/synthetic-review.md", "delivery": "submitted" if historical else "prepared",
            "outcome": "HISTORY-OUTCOME|" + "o" * 2000 if historical else None}

    for index in range(9):
        work = {"id": f"memory-{index}", "title": f"Feature {index}", "purpose": "HISTORY-PURPOSE|Do useful work",
                "status": "planned", "next_action": "Inspect reports", "blocker": "Needs review",
                "locations": [{"session": "default", "workspace_id": "w1", "pane_id": "w1:p1"}],
                "assignments": [assignment(f"review-{number}") for number in range(8)]}
        structured(client.tool("workstream_save", {"workstream": work, "expected_revision": 0}))
    notes_path = root / "state" / "workstreams.json"
    native_path = root / "native.json"
    original = notes_path.read_bytes()
    calls = json.loads(native_path.read_text())["calls"]
    full_list = structured(client.tool("workstream_list"))
    assert structured(client.call("tools/call", {"name": "workstream_list"})) == full_list
    assert structured(client.tool("workstream_list", {"compact": False})) == full_list
    assert set(full_list) == {"workstreams"}
    compact_list = structured(client.tool("workstream_list", {"compact": True}))
    assert set(compact_list) == {"compact", "summaries"} and compact_list["compact"] is True
    records = full_list["workstreams"]
    for record, summary in zip(records, compact_list["summaries"], strict=True):
        work = record["workstream"]
        assert summary["workstream_id"] == work["id"] and summary["revision"] == record["revision"]
        for field in ["title", "status", "next_action", "blocker", "locations"]:
            assert summary[field] == work[field]
        assert summary["assignment_count"] == len(work.get("assignments", []))
        assert summary["assignments"] == [{"id": a["id"], "role": a["role"], "delivery": a["delivery"],
                                            "has_outcome": a["outcome"] is not None} for a in work.get("assignments", [])]
        assert structured(client.tool("workstream_get", {"workstream_id": work["id"]}))["record"] == record
    assert json.loads(native_path.read_text())["calls"] == calls, "memory reads never call native CLI"
    full_overview = structured(client.tool("workstream_overview", {"session": "default"}))
    assert structured(client.tool("workstream_overview", {"session": "default", "compact": False})) == full_overview
    compact_overview = structured(client.tool("workstream_overview", {"session": "default", "compact": True}))
    assert compact_overview["compact"] is True and "compact" not in full_overview
    for field in ["inspected_session", "session_error", "live_inventory", "interpretation"]:
        assert compact_overview[field] == full_overview[field]
    assert compact_overview["live_inventory"]["tabs"] == full_overview["live_inventory"]["tabs"]
    for full, compact in zip(full_overview["workstreams"], compact_overview["workstreams"], strict=True):
        for field in ["locations", "unlinked", "recorded_blocker"]:
            assert compact[field] == full[field]
        assert "record" not in compact
        assert compact["summary"]["status"] == full["record"]["workstream"]["status"]
    for compact in [compact_list, compact_overview]:
        body = json.dumps(compact)
        for marker in ["HISTORY-BRIEF|", "HISTORY-CONSTRAINT|", "HISTORY-CRITERION|", "HISTORY-OUTCOME|", "HISTORY-PURPOSE|", "/tmp/synthetic-review.md"]:
            assert marker not in body
    size = lambda value: len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())
    assert size(full_list) > 180_000 and size(compact_list) < size(full_list) / 5
    assert size(compact_overview) < size(full_overview) / 5
    print(f"memory compact sizes (structured UTF-8 bytes): list {size(full_list)} -> {size(compact_list)}; overview {size(full_overview)} -> {size(compact_overview)}")
    assert notes_path.read_bytes() == original
    for workstream_id in ["missing", "../bad", "", "a" * 65]:
        expect_error(client.tool("workstream_get", {"workstream_id": workstream_id}))
    expect_error(client.tool("workstream_save", {"workstream": compact_list["summaries"][0], "expected_revision": 1}))
    assert notes_path.read_bytes() == original

    # Compact mode projects the same unavailable observations, including malformed tabs.
    native = json.loads(native_path.read_text())
    bad = copy.deepcopy(native)
    del bad["snapshot"]["tabs"]
    native_path.write_text(json.dumps(bad))
    for session in ["default", "stopped"]:
        full = structured(client.tool("workstream_overview", {"session": session}))
        compact = structured(client.tool("workstream_overview", {"session": session, "compact": True}))
        assert full["session_error"] == compact["session_error"] and full["session_error"]
        assert full["live_inventory"] is None and compact["live_inventory"] is None
        for a, b in zip(full["workstreams"], compact["workstreams"], strict=True):
            assert a["locations"] == b["locations"]
    current_native = json.loads(native_path.read_text())
    current_native["snapshot"] = native["snapshot"]
    native_path.write_text(json.dumps(current_native))
    expect_error(client.tool("workstream_overview", {"session": "../bad", "compact": True}), "session")
    assert notes_path.read_bytes() == original

    current = structured(client.tool("workstream_get", {"workstream_id": "memory-0"}))["record"]
    expected = copy.deepcopy(current["workstream"])
    native_calls = json.loads(native_path.read_text())["calls"]

    def update(change, revision=None):
        return client.tool("workstream_update", {"workstream_id": "memory-0",
            "expected_revision": current["revision"] if revision is None else revision, "change": change})

    def accept(change):
        nonlocal current
        result = structured(update(change))
        assert set(result) == {"summary"} and "workstream" not in result["summary"]
        assert result["summary"]["revision"] == current["revision"] + 1
        assert size(result) < 3000
        current = structured(client.tool("workstream_get", {"workstream_id": "memory-0"}))["record"]
        assert current["workstream"] == expected
        assert result["summary"]["revision"] == current["revision"]

    expected["status"] = "paused"
    expected["next_action"] = "Inspect the three reviews"
    accept({"type": "notes", "status": "paused", "next_action": {"type": "set", "value": expected["next_action"]}})
    additions = [assignment(f"new-{index}", historical=False) for index in range(3)]
    expected["assignments"].extend(additions)
    accept({"type": "append_assignments", "assignments": additions})
    expected["assignments"][0]["outcome"] = "Reviewed one"
    expected["assignments"][1]["outcome"] = None
    expected["assignments"][2]["outcome"] = "Reviewed three"
    accept({"type": "outcomes", "outcomes": [
        {"assignment_id": "review-0", "outcome": {"type": "set", "value": "Reviewed one"}},
        {"assignment_id": "review-1", "outcome": {"type": "clear"}},
        {"assignment_id": "review-2", "outcome": {"type": "set", "value": "Reviewed three"}},
    ]})
    expected["next_action"] = None
    expected["blocker"] = None
    accept({"type": "notes", "next_action": {"type": "clear"}, "blocker": {"type": "clear"}})
    for invalid in [
        {"type": "notes"}, {"type": "notes", "status": "paused"},
        {"type": "notes", "next_action": None}, {"type": "notes", "status": None},
        {"type": "notes", "delivery": "prepared"},
        {"type": "notes", "blocker": {"type": "set", "value": "x" * 4097}},
        {"type": "append_assignments", "assignments": []},
        {"type": "append_assignments", "assignments": [assignment("review-0", historical=False)]},
        {"type": "append_assignments", "assignments": [assignment("nonprepared")]},
        {"type": "outcomes", "outcomes": [
            {"assignment_id": "review-0", "outcome": {"type": "clear"}},
            {"assignment_id": "missing", "outcome": {"type": "clear"}}]},
        {"type": "outcomes", "outcomes": [
            {"assignment_id": "review-0", "outcome": {"type": "clear"}},
            {"assignment_id": "review-0", "outcome": {"type": "set", "value": "duplicate"}}]},
        {"type": "replace", "assignments": []},
    ]:
        before = notes_path.read_bytes()
        expect_error(update(invalid))
        assert notes_path.read_bytes() == before
    for revision in [0, 1, current["revision"] + 100]:
        before = notes_path.read_bytes()
        expect_error(update({"type": "notes", "status": "active"}, revision=revision))
        assert notes_path.read_bytes() == before

    # Independent MCP requests compete on the same revision, with no blind retries.
    from stdio_smoke import Client
    other = Client(binary, root)
    try:
        request = {"workstream_id": "memory-0", "expected_revision": current["revision"],
                   "change": {"type": "notes", "status": "active"}}
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda c: c.tool("workstream_update", request), [client, other]))
        assert sum(not result.get("isError", False) for result in results) == 1
        expect_error(next(result for result in results if result.get("isError")), "revision conflict")
    finally:
        other.close()
    final = structured(client.tool("workstream_get", {"workstream_id": "memory-0"}))["record"]
    expected["status"] = "active"
    assert final["workstream"] == expected and final["revision"] == current["revision"] + 1
    for record in structured(client.tool("workstream_list"))["workstreams"]:
        if record["workstream"]["id"] != "memory-0":
            assert record == next(r for r in records if r["workstream"]["id"] == record["workstream"]["id"])
    assert json.loads(native_path.read_text())["calls"] == native_calls, "focused bookkeeping never dispatches or inspects native agents"
