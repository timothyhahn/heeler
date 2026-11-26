"""Bounded stored-claim observations; temporary notes, no native/report access."""
from concurrent.futures import ThreadPoolExecutor
import copy
import fcntl
import json
import time


from mcp_client import Client, structured


def expect_error(result, marker):
    assert result.get("isError"), result
    message = result.get("structuredContent", {}).get("error", result["content"][0].get("text", ""))
    assert marker in message, result


def check_outcomes(client, root, tools, binary):
    tool = tools["assignment_outcome"]
    schema = tool["inputSchema"]
    assert schema["additionalProperties"] is False and set(schema["required"]) == {"workstream_id", "assignment_id"}
    assert schema["properties"]["wait_ms"]["default"] == 0 and schema["properties"]["wait_ms"]["maximum"] == 60000
    assert tool["annotations"]["readOnlyHint"] is True
    path = root / "state/workstreams.json"
    native_path = root / "native.json"
    native_before = native_path.read_bytes()
    handoff = {"id": "review", "role": "review", "target": {"session": "default", "pane_id": "w1:p1", "terminal_id": "term_test", "agent": "pi", "agent_session": None},
               "brief": "BRIEF-HISTORY|" + "b" * 6000, "constraints": ["PRIVATE-CRITERIA|Keep intent"],
               "acceptance_criteria": ["Inspect report"], "report_path": str(root / "never-open-this-report"), "delivery": "prepared", "outcome": None}
    work = {"id": "handoff", "title": "Handoff", "purpose": "Intent", "status": "active", "next_action": "Inspect claims",
            "blocker": None, "locations": [], "assignments": [handoff]}
    record = structured(client.tool("workstream_save", {"workstream": work, "expected_revision": 0}))["record"]

    def read(wait=None, assignment="review", workstream="handoff", c=client):
        arguments = {"workstream_id": workstream, "assignment_id": assignment}
        if wait is not None:
            arguments["wait_ms"] = wait
        return c.tool("assignment_outcome", arguments, timeout=5)

    for delivery in ["prepared", "uncertain", "submitted"]:
        for claim in [None, "CLAIM complete\nChecks: passed", "CLAIM blocked\nNeeds: human decision", "", "odd\0legacy", "é" * 4096]:
            work["assignments"][0].update(delivery=delivery, outcome=claim)
            record = structured(client.tool("workstream_save", {"workstream": work, "expected_revision": record["revision"]}))["record"]
            before = path.read_bytes()
            observation = structured(read())
            assert observation["result"] == ("reported" if claim is not None else "pending")
            assert observation["snapshot"]["reported_text"] == claim and observation["snapshot"]["delivery"] == delivery
            assert observation["snapshot"]["revision"] == record["revision"] and observation["wait_ms"] == 0
            assert observation["untrusted"] is True and observation["verified"] is False
            assert "BRIEF-HISTORY|" not in json.dumps(observation) and "PRIVATE-CRITERIA|" not in json.dumps(observation)
            assert str(root / "never-open-this-report") not in json.dumps(observation)
            assert path.read_bytes() == before
    before = path.read_bytes()
    for arguments in [{"workstream_id": "handoff"}, {"assignment_id": "review"},
                      {"workstream_id": "handoff", "assignment_id": "review", "path": "not-a-path-control"},
                      {"workstream_id": "handoff", "assignment_id": "review", "wait_ms": None},
                      {"workstream_id": "handoff", "assignment_id": "review", "wait_ms": -1}]:
        expect_error(client.tool("assignment_outcome", arguments), "failed to deserialize")
    expect_error(read(60001), "wait_ms")
    expect_error(read(workstream="../bad"), "id must")
    expect_error(read(assignment="../bad"), "id must")
    expect_error(read(workstream="missing"), "workstream not found")
    expect_error(read(assignment="missing"), "assignment not found")
    assert path.read_bytes() == before

    work["assignments"][0]["outcome"] = None
    record = structured(client.tool("workstream_save", {"workstream": work, "expected_revision": record["revision"]}))["record"]
    before = path.read_bytes()
    timed = structured(read(30))
    assert timed["result"] == "timeout" and timed["snapshot"]["revision"] == record["revision"]
    assert timed["snapshot"]["reported_text"] is None and timed["elapsed_ms"] >= 30
    assert path.read_bytes() == before
    with (root / "state/workstreams.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        timed = structured(read(30))
        assert timed["result"] == "timeout" and timed["snapshot"] is None
        expect_error(read(), "store is busy")
        # Actual protocol cancellation while a validated Store read is gated on lock.
        client.counter += 1
        request_id = client.counter
        client.send({"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": {
            "name": "assignment_outcome", "arguments": {"workstream_id": "handoff", "assignment_id": "review", "wait_ms": 60000}}})
        assert client.call("ping", {}, timeout=2) == {}
        client.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": request_id}})
        assert client.call("ping", {}, timeout=2) == {}
    assert path.read_bytes() == before

    other = Client(binary, root)
    try:
        def publish(replace=False):
            time.sleep(0.1)
            current = structured(other.tool("workstream_get", {"workstream_id": "handoff"}))["record"]
            if replace:
                changed = copy.deepcopy(current["workstream"])
                changed["assignments"][0]["brief"] = "Replaced definition"
                changed["assignments"][0]["outcome"] = "CLAIM complete for replacement"
                return other.tool("workstream_save", {"workstream": changed, "expected_revision": current["revision"]})
            return other.tool("workstream_update", {"workstream_id": "handoff", "expected_revision": current["revision"],
                "change": {"type": "outcomes", "outcomes": [{"assignment_id": "review", "outcome": {"type": "set", "value": "CLAIM blocked\nNeeds: decision"}}]}})
        with ThreadPoolExecutor(max_workers=1) as pool:
            publication = pool.submit(publish)
            observed = structured(read(2000))
            structured(publication.result())
        assert observed["result"] == "reported" and observed["snapshot"]["reported_text"].startswith("CLAIM blocked")
        assert structured(read(60000))["result"] == "reported", "existing reports return immediately"
        # Clear only to prepare this isolated CAS race fixture, not as a worker retry convention.
        current = structured(client.tool("workstream_get", {"workstream_id": "handoff"}))["record"]
        structured(client.tool("workstream_update", {"workstream_id": "handoff", "expected_revision": current["revision"],
                   "change": {"type": "outcomes", "outcomes": [{"assignment_id": "review", "outcome": {"type": "clear"}}]}}))
        current = structured(client.tool("workstream_get", {"workstream_id": "handoff"}))["record"]
        request = {"workstream_id": "handoff", "expected_revision": current["revision"], "change": {"type": "outcomes",
                   "outcomes": [{"assignment_id": "review", "outcome": {"type": "set", "value": "CLAIM complete\nUnverified concurrent publisher"}}]}}
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda c: c.tool("workstream_update", request), [client, other]))
        assert sum(not result.get("isError", False) for result in results) == 1
        expect_error(next(result for result in results if result.get("isError")), "revision conflict")
        current = structured(client.tool("workstream_get", {"workstream_id": "handoff"}))["record"]
        structured(client.tool("workstream_update", {"workstream_id": "handoff", "expected_revision": current["revision"],
                   "change": {"type": "outcomes", "outcomes": [{"assignment_id": "review", "outcome": {"type": "clear"}}]}}))
        with ThreadPoolExecutor(max_workers=1) as pool:
            replacement = pool.submit(publish, True)
            expect_error(read(2000), "definition replaced")
            structured(replacement.result())
    finally:
        other.close()

    original = path.read_bytes()
    for corrupt in [b"not-json", b'{"version":3,"records":[]}']:
        path.write_bytes(corrupt)
        expect_error(read(60000), "store")
        assert path.read_bytes() == corrupt
    path.write_bytes(original)
    assert native_path.read_bytes() == native_before, "outcome reads/publication bookkeeping never call native metadata or controls"
    assert not (root / "never-open-this-report").exists()
    print("outcome smoke: raw untrusted claims, bounds/CAS/late publication/replacement/lock deadlines/cancellation, unchanged reads and no native/report calls passed")
