#!/usr/bin/env python3
"""Lead → implementation → review, using synthetic local agents and temporary notes."""
from concurrent.futures import ThreadPoolExecutor
import copy
import json
from pathlib import Path
import socket
import sys
import tempfile

from stdio_smoke import Client, structured


def native_state(root):
    return json.loads((root / "native.json").read_text())


def change_native(root, change):
    state = native_state(root)
    change(state)
    (root / "native.json").write_text(json.dumps(state))


def assignment(root, name, role, target, brief):
    return {
        "id": name, "role": role, "target": target, "brief": brief,
        "constraints": ["Keep the change focused", "Do not commit or push"],
        "acceptance_criteria": ["Explain the change or findings", "Report checks actually run"],
        "report_path": str(root / f"{name}-report.md"), "delivery": "prepared", "outcome": None,
    }


def workstream(name, handoff, locations=None):
    return {"id": name, "title": name, "purpose": "Test a local handoff", "status": "active",
            "next_action": "Inspect the result", "blocker": None, "locations": locations or [],
            "assignments": [handoff]}


def save(client, work, revision=0):
    return structured(client.tool("workstream_save", {
        "workstream": work, "expected_revision": revision,
    }))["record"]


def record(client, name):
    return next(r for r in structured(client.tool("workstream_list"))["workstreams"]
                if r["workstream"]["id"] == name)


def send(client, name, handoff, revision):
    return client.tool("assignment_send", {
        "workstream_id": name, "assignment_id": handoff, "expected_revision": revision,
    })


def expect_error(result, text):
    assert result.get("isError"), result
    assert text in result["structuredContent"]["error"], result


def open_location(client, location):
    return structured(client.tool("location_open", {"session": "default", "location": location}))


def start(client, pane_id, name, kind):
    return structured(client.tool("agent_start", {
        "session": "default", "start": {"pane_id": pane_id, "name": name, "kind": kind},
    }))["target"]


def check_loop(client, root):
    opened = open_location(client, {
        "type": "workspace", "cwd": str(root / "project with spaces"), "label": "Feature",
    })
    target = start(client, opened["pane"]["pane_id"], "implementer", "pi")
    inspected = structured(client.tool("agent_inspect", {"session": "default", "pane_id": target["pane_id"]}))
    assert inspected["target"] == target
    handoff = assignment(root, "implement", "implementation", target, "Implement the requested small change.")
    saved = save(client, workstream("feature", handoff, [opened["location"]]))
    result = structured(send(client, "feature", "implement", saved["revision"]))
    assert result["delivery"] == "submitted" and result["record_error"] is None
    assert result["record"]["revision"] == 3
    assert result["record"]["workstream"]["status"] == "active"
    before = len(native_state(root)["prompts"])
    expect_error(send(client, "feature", "implement", 3), "already submitted or uncertain")
    assert len(native_state(root)["prompts"]) == before

    # Simulated report creation uses persisted metadata, not exact prompt prose.
    change_native(root, lambda state: state["prompts"][-1].update(text="No report stanza here."))
    change_native(root, lambda state: state.update(wait_stays_idle=True))
    idle = structured(client.tool("agent_wait", {"target": target, "timeout_ms": 1000}))
    assert idle["agent"]["agent_status"] == "idle"
    assert not Path(handoff["report_path"]).exists()
    assert record(client, "feature")["workstream"]["status"] == "active"
    waited = structured(client.tool("agent_wait", {"target": target, "timeout_ms": 1000}))
    assert waited["agent"]["agent_status"] == "done"
    assert Path(handoff["report_path"]).exists()
    output = structured(client.tool("agent_read", {"target": target}))
    assert output["untrusted"] and "Review the report" in output["text"]
    assert record(client, "feature")["workstream"]["assignments"][0]["outcome"] is None

    reviewer_location = open_location(client, {
        "type": "tab", "workspace_id": opened["location"]["workspace_id"],
        "cwd": str(root / "project with spaces"), "label": "Review",
    })
    reviewer = start(client, reviewer_location["pane"]["pane_id"], "reviewer", "codex")
    native_reviewer = structured(client.tool("agent_inspect", {"session": "default", "pane_id": reviewer["pane_id"]}))["agent"]
    assert native_reviewer["agent_status"] == "unknown" and native_reviewer["interactive_ready"]
    assert native_reviewer["launch_pending"] is False
    # Also exercise explicit sibling creation, without starting extra agents.
    sibling = open_location(client, {
        "type": "split", "pane_id": reviewer["pane_id"],
        "direction": "right", "cwd": str(root / "project with spaces"),
    })
    assert sibling["pane"]["tab_id"] == reviewer_location["pane"]["tab_id"]
    assert native_state(root)["snapshot"]["focused_pane_id"] == "w1:p1"

    review = assignment(root, "review", "review", reviewer,
                        f"Review the implementation. Read its report at {handoff['report_path']}.")
    current = record(client, "feature")
    current["workstream"]["locations"].append(reviewer_location["location"])
    current["workstream"]["assignments"].append(review)
    saved = save(client, current["workstream"], current["revision"])
    result = structured(send(client, "feature", "review", saved["revision"]))
    assert result["agent"]["agent"]["agent_status"] == "unknown"
    assert result["agent"]["agent"]["interactive_ready"] is True
    assert result["delivery"] == "submitted"
    text = native_state(root)["prompts"][-1]["text"]
    assert "Do not modify project/source files" in text and handoff["report_path"] in text
    structured(client.tool("agent_wait", {"target": reviewer, "timeout_ms": 1000}))
    assert Path(review["report_path"]).exists()
    structured(client.tool("agent_read", {"target": reviewer, "source": "recent_unwrapped", "lines": 40}))
    focused = structured(client.tool("agent_focus", {"target": reviewer}))
    assert focused["target"] == reviewer
    assert native_state(root)["snapshot"]["focused_pane_id"] == reviewer["pane_id"]
    current = record(client, "feature")
    assert current["workstream"]["status"] == "active", "idle/done must not complete a workstream"
    current["workstream"]["assignments"][1]["outcome"] = "Lead inspected the synthetic review report."
    save(client, current["workstream"], current["revision"])
    return target, reviewer


def check_readiness_refusals(client, root, targets):
    for target in targets:
        cases = [
            {"agent_status": "blocked", "interactive_ready": True},
            {"agent_status": "working", "interactive_ready": True},
            {"agent_status": "unknown", "interactive_ready": False},
            {"agent_status": "unknown", "interactive_ready": True, "launch_pending": True},
            {"agent_status": "idle", "launch_pending": True},
            {"agent_status": "done", "launch_pending": True},
            {"agent_status": "unsupported", "interactive_ready": True},
        ]
        if target["agent"] != "codex":
            cases.append({"agent_status": "unknown", "interactive_ready": True})
        for phase in [1, 2]:
            for index, fields in enumerate(cases):
                name = f"readiness-{target['agent']}-{phase}-{index}"
                save(client, workstream(name, assignment(root, name, "review", target, "Refuse unsafe readiness.")))
                before = len(native_state(root)["prompts"])
                change_native(root, lambda state: state.update(get_overrides=([{}] if phase == 2 else []) + [fields]))
                expect_error(send(client, name, name, 1), "not ready" if phase == 1 else "no longer ready")
                current = record(client, name)
                assert current["revision"] == phase
                assert current["workstream"]["assignments"][0]["delivery"] == ("prepared" if phase == 1 else "uncertain")
                assert len(native_state(root)["prompts"]) == before
                if phase == 2:
                    expect_error(send(client, name, name, 2), "already submitted or uncertain")
                    assert len(native_state(root)["prompts"]) == before


def check_concurrent_dispatch(client, root, binary, target):
    save(client, workstream("concurrent", assignment(root, "concurrent", "implementation", target,
                                                   "Only one lead should submit this handoff.")))
    other = Client(binary, root)
    before = len(native_state(root)["prompts"])
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(send, client, "concurrent", "concurrent", 1)
            second = pool.submit(send, other, "concurrent", "concurrent", 1)
            results = [first.result(), second.result()]
        assert sum(not result.get("isError", False) for result in results) == 1
        assert len(native_state(root)["prompts"]) == before + 1
        assert record(client, "concurrent")["workstream"]["assignments"][0]["delivery"] == "submitted"
    finally:
        other.close()


def check_failures(client, root, target):
    handoff = assignment(root, "try-once", "implementation", target, "Test an interrupted submission.")
    save(client, workstream("failure", handoff))
    before = len(native_state(root)["prompts"])
    change_native(root, lambda state: state.update(fail_prompt=True))
    expect_error(send(client, "failure", "try-once", 1), "recorded as uncertain")
    current = record(client, "failure")
    assert current["revision"] == 2
    assert current["workstream"]["assignments"][0]["delivery"] == "uncertain"
    expect_error(send(client, "failure", "try-once", 2), "already submitted or uncertain")
    assert len(native_state(root)["prompts"]) == before + 1

    blocked = structured(client.tool("agent_inspect", {"session": "default", "pane_id": "w1:p1"}))["target"]
    save(client, workstream("blocked", assignment(root, "blocked", "review", blocked, "Do not bypass a dialog.")))
    before = len(native_state(root)["prompts"])
    expect_error(send(client, "blocked", "blocked", 1), "not ready")
    assert record(client, "blocked")["revision"] == 1
    assert len(native_state(root)["prompts"]) == before

    save(client, workstream("stale", assignment(root, "stale", "implementation", target, "Reject stale identity.")))
    baseline = native_state(root)
    index = next(i for i, agent in enumerate(baseline["snapshot"]["agents"]) if agent["pane_id"] == target["pane_id"])
    for field, value in [("terminal_id", "term_replaced"), ("agent", "claude"),
                         ("agent_session", {"kind": "id", "value": "another-conversation"})]:
        changed = copy.deepcopy(baseline)
        changed["snapshot"]["agents"][index][field] = value
        (root / "native.json").write_text(json.dumps(changed))
        expect_error(send(client, "stale", "stale", 1), "changed")
        assert record(client, "stale")["revision"] == 1
    (root / "native.json").write_text(json.dumps(baseline))
    expect_error(send(client, "stale", "stale", 99), "revision conflict")

    save(client, workstream("ack-race", assignment(root, "ack-race", "implementation", target, "Preserve a competing edit.")))
    change_native(root, lambda state: state.update(conflict_after_prompt=True))
    before = len(native_state(root)["prompts"])
    acknowledged = structured(send(client, "ack-race", "ack-race", 1))
    assert acknowledged["delivery"] == "submitted" and acknowledged["record"] is None
    assert "do not resend" in acknowledged["record_error"]
    current = record(client, "ack-race")
    assert current["workstream"]["next_action"] == "Competing edit"
    assert current["workstream"]["assignments"][0]["delivery"] == "uncertain"
    expect_error(send(client, "ack-race", "ack-race", current["revision"]), "already submitted or uncertain")
    assert len(native_state(root)["prompts"]) == before + 1

    before = len(native_state(root)["calls"])
    expect_error(client.tool("location_open", {"session": "default", "location": {
        "type": "workspace", "cwd": "relative/path", "label": "Invalid",
    }}), "absolute")
    expect_error(client.tool("agent_wait", {"target": target, "timeout_ms": 60001}), "wait timeout")
    expect_error(client.tool("agent_read", {"target": target, "lines": 501}), "read lines")
    assert len(native_state(root)["calls"]) == before


def check_cancellation(binary):
    # Short Unix socket paths also work on macOS's 104-byte sockaddr_un limit.
    with tempfile.TemporaryDirectory(prefix="heeler-cancel-", dir="/tmp") as directory:
        root = Path(directory)
        source = Path(__file__).parent
        response = json.loads((source / "fixtures" / "herdr-0.9.5-snapshot.json").read_text())
        (root / "native.json").write_text(json.dumps({"snapshot": response["result"]["snapshot"]}))
        fake = root / "herdr"
        fake.write_text(f"#!{sys.executable}\n" + (source / "fake_herdr.py").read_text())
        fake.chmod(0o700)
        client = Client(binary, root)
        try:
            opened = open_location(client, {"type": "workspace", "cwd": str(root), "label": "Cancellation"})
            target = start(client, opened["pane"]["pane_id"], "worker", "pi")
            for name, command, skip, revision, delivery in [
                ("before-reservation", "get", 0, 1, "prepared"),
                ("after-reservation", "get", 1, 2, "uncertain"),
                ("in-flight", "prompt", 0, 2, "uncertain"),
            ]:
                handoff = assignment(root, name, "implementation", target, "Synthetic cancellation test.")
                save(client, workstream(name, handoff))
                before = len(native_state(root).get("prompts", []))
                with socket.socket(socket.AF_UNIX) as listener:
                    listener.settimeout(3)
                    listener.bind(str(root / f"{name}.sock"))
                    listener.listen(1)
                    change_native(root, lambda state: state.update(dispatch_gate={
                        "command": command, "skip": skip, "socket": f"{name}.sock",
                    }))
                    client.counter += 1
                    request_id = client.counter
                    client.send({"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": {
                        "name": "assignment_send", "arguments": {
                            "workstream_id": name, "assignment_id": name, "expected_revision": 1,
                        },
                    }})
                    connection, _ = listener.accept()
                    with connection:
                        connection.settimeout(2)
                        assert connection.recv(1) == b"x"
                        current = record(client, name)
                        assert current["revision"] == revision
                        assert current["workstream"]["assignments"][0]["delivery"] == delivery
                        client.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {
                            "requestId": request_id, "reason": "Withdraw synthetic dispatch",
                        }})
                        # Ordered ping proves rmcp processed cancellation. The cancelled
                        # request's response is suppressed, so do not wait for that receipt.
                        assert client.call("ping", {}, timeout=2) == {}
                        # The child is gated until release or kill. EOF while still gated
                        # proves cancellation dropped the dispatch/native future, not just
                        # that a snapshot happened before a later prompt. Old behavior times out.
                        assert connection.recv(1) == b""
                current = record(client, name)
                assert current["revision"] == revision
                assert current["workstream"]["assignments"][0]["delivery"] == delivery
                expected_prompts = before + (command == "prompt")
                assert len(native_state(root).get("prompts", [])) == expected_prompts
                if delivery == "uncertain":
                    expect_error(send(client, name, name, revision), "already submitted or uncertain")
                    assert len(native_state(root).get("prompts", [])) == expected_prompts
        finally:
            client.close()


def main():
    binary = str(Path(sys.argv[1]).resolve())
    with tempfile.TemporaryDirectory(prefix="heeler-delegation-") as directory:
        root = Path(directory)
        source = Path(__file__).parent
        response = json.loads((source / "fixtures" / "herdr-0.9.5-snapshot.json").read_text())
        (root / "native.json").write_text(json.dumps({"snapshot": response["result"]["snapshot"]}))
        fake = root / "herdr"
        fake.write_text(f"#!{sys.executable}\n" + (source / "fake_herdr.py").read_text())
        fake.chmod(0o700)
        client = Client(binary, root)
        try:
            target, reviewer = check_loop(client, root)
            check_readiness_refusals(client, root, [target, reviewer])
            check_failures(client, root, target)
            check_concurrent_dispatch(client, root, binary, target)
        finally:
            client.close()
        client = Client(binary, root)
        try:
            current = record(client, "feature")
            assert len(current["workstream"]["assignments"]) == 2
            assert current["workstream"]["assignments"][1]["outcome"]
        finally:
            client.close()
    check_cancellation(binary)
    print("delegation smoke: implementation/review loop, identity checks, readiness refusals, uncertain sends, cancellation gates, conflicts, and restart passed")


if __name__ == "__main__":
    main()
