"""Explicit new-tab worker relocation using only synthetic native state."""
import copy
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile


from mcp_client import Client, structured


def error(result, marker):
    assert result.get("isError"), result
    message = result.get("structuredContent", {}).get("error", result["content"][0].get("text", ""))
    assert marker in message, result
    return message


def check_moves(binary, tool):
    assert set(tool["inputSchema"]["required"]) == {"target", "workspace_id", "label"}
    assert tool["inputSchema"]["additionalProperties"] is False
    assert tool["annotations"]["destructiveHint"] is True
    assert tool["annotations"]["readOnlyHint"] is False and tool["annotations"]["idempotentHint"] is False
    source = Path(__file__).parent
    template = json.loads((source / "fixtures/herdr-0.9.5-snapshot.json").read_text())["result"]["snapshot"]
    with tempfile.TemporaryDirectory(prefix="heeler-move-", dir="/tmp") as directory:
        root = Path(directory)
        path = root / "native.json"
        fake = root / "herdr"
        fake.write_text(f"#!{sys.executable}\n" + (source / "fake_herdr.py").read_text())
        fake.chmod(0o700)

        def reset(status="working", kind="pi", single=False, **fields):
            snapshot = {**template, "workspaces": [], "tabs": [], "panes": [], "agents": [], "layouts": []}
            for wid in range(1, 8):
                workspace_id = f"w{wid}"
                pane_number = 0
                for number in range(1, 3 if wid == 7 else 4):
                    tab_id = f"{workspace_id}:t{number}"
                    count = 2 if (wid, number) == (1, 1) else 1
                    snapshot["tabs"].append({**template["tabs"][0], "workspace_id": workspace_id, "tab_id": tab_id,
                                             "number": number, "label": "Worker", "focused": tab_id == "w1:t1", "pane_count": count})
                    for _ in range(count):
                        pane_number += 1
                        pane_id = f"{workspace_id}:p{pane_number}"
                        terminal = f"term_{wid}_{pane_number}"
                        snapshot["panes"].append({**template["panes"][0], "workspace_id": workspace_id, "tab_id": tab_id,
                                                  "pane_id": pane_id, "terminal_id": terminal, "focused": pane_id == "w1:p1"})
                        snapshot["agents"].append({**template["agents"][0], "workspace_id": workspace_id, "tab_id": tab_id,
                                                   "pane_id": pane_id, "terminal_id": terminal, "agent": "pi",
                                                   "focused": pane_id == "w1:p1", "launch_pending": False, "interactive_ready": True,
                                                   "agent_session": {"source": "herdr:pi", "agent": "pi", "kind": "id", "value": "synthetic"}})
                snapshot["workspaces"].append({**template["workspaces"][0], "workspace_id": workspace_id, "number": wid,
                                               "active_tab_id": f"{workspace_id}:t1", "tab_count": 2 if wid == 7 else 3,
                                               "pane_count": pane_number, "focused": wid == 1})
            worker = next(a for a in snapshot["agents"] if a["pane_id"] == "w1:p2")
            worker.update(agent=kind, agent_status=status, agent_session={"source": "herdr:" + kind, "agent": kind, "kind": "id", "value": "synthetic"})
            next(p for p in snapshot["panes"] if p["pane_id"] == "w1:p2")["agent_status"] = status
            if single:
                snapshot["tabs"] = [t for t in snapshot["tabs"] if t["workspace_id"] != "w1" or t["tab_id"] == "w1:t1"]
                for collection in ["panes", "agents"]:
                    snapshot[collection] = [p for p in snapshot[collection] if p["workspace_id"] != "w1" or p["pane_id"] == "w1:p2"]
                snapshot["tabs"][0].update(pane_count=1)
                snapshot["workspaces"][0].update(tab_count=1, pane_count=1)
                snapshot["focused_pane_id"] = "w1:p2"
                snapshot["panes"][0]["focused"] = True
                worker["focused"] = True
            path.write_text(json.dumps({"snapshot": snapshot, **fields}))
            return {"session": "default", "pane_id": worker["pane_id"], "terminal_id": worker["terminal_id"],
                    "agent": kind, "agent_session": copy.deepcopy(worker["agent_session"])}

        def state():
            return json.loads(path.read_text())

        def move(target, workspace="w2", label="Worker review α"):
            return client.tool("agent_move", {"target": target, "workspace_id": workspace, "label": label}, timeout=30)

        def move_calls():
            return [a for a in state().get("calls", []) if a[1:3] == ["pane", "move"]]

        target = reset()
        client = Client(binary, root)
        try:
            old_assignment = {"id": "history", "role": "implementation", "target": target, "brief": "Historical task",
                              "acceptance_criteria": ["Preserve intent"], "report_path": str(root / "report.md"), "delivery": "submitted", "outcome": "Reviewed"}
            saved = structured(client.tool("workstream_save", {"workstream": {"id": "feature", "title": "Feature", "purpose": "Remember intent",
                "status": "active", "locations": [{"session": "default", "workspace_id": "w1", "pane_id": "w1:p2"}], "assignments": [old_assignment]}, "expected_revision": 0}))["record"]
            notes_path = root / "state/workstreams.json"
            notes = notes_path.read_bytes()
            for kind, status in [("pi", "working"), ("claude", "blocked"), ("pi", "unknown"), ("codex", "unknown")]:
                for destination in ["w1", "w2"]:
                    target = reset(status, kind)
                    before = state()["snapshot"]
                    result = structured(move(target, destination))
                    current = result["target"]
                    assert result["changed"] is True and result["reason"] is None
                    assert result["previous_location"] == {"session": "default", "workspace_id": "w1", "tab_id": "w1:t1", "pane_id": "w1:p2"}
                    assert result["location"]["workspace_id"] == destination and result["location"]["pane_id"] == current["pane_id"]
                    assert current == {**target, "pane_id": current["pane_id"]}
                    assert (current["pane_id"] == target["pane_id"]) == (destination == "w1")
                    assert result["agent"]["terminal_id"] == target["terminal_id"] and result["agent"]["agent_status"] == status
                    assert result["agent"]["agent_session"] == target["agent_session"]
                    assert result["metadata_timing"] == "post_move_inspection" and result["no_focus_requested"] is True
                    assert result["tab"]["label"] == "Worker review α" and result["tab"]["tab_id"] == result["location"]["tab_id"]
                    assert result["closed_tab_id"] is None and result["closed_workspace_id"] is None
                    assert len(state()["snapshot"]["tabs"]) == 21 and len(state()["snapshot"]["workspaces"]) == 7
                    assert len(state()["snapshot"]["panes"]) == len(before["panes"]) and len(state()["snapshot"]["agents"]) == len(before["agents"])
                    assert state()["snapshot"]["focused_pane_id"] == before["focused_pane_id"]
                    assert state()["calls"] == [
                        ["--session=default", "agent", "get", target["pane_id"]], ["--session=default", "workspace", "get", destination],
                        ["--session=default", "pane", "get", target["pane_id"]], ["--session=default", "agent", "get", target["pane_id"]],
                        ["--session=default", "pane", "move", target["pane_id"], "--new-tab", "--workspace", destination, "--label", "Worker review α", "--no-focus"],
                        ["--session=default", "pane", "get", current["pane_id"]], ["--session=default", "tab", "get", result["location"]["tab_id"]],
                        ["--session=default", "agent", "get", current["pane_id"]]]
                    assert notes_path.read_bytes() == notes
            for label in ["é" * 128, " --session=other", "Text ; $() quoted"]:
                target = reset()
                assert structured(move(target, label=label))["tab"]["label"] == label
            for destination in ["w1", "w2"]:
                target = reset(single=True)
                result = structured(move(target, destination))
                assert result["closed_tab_id"] == "w1:t1"
                assert result["closed_workspace_id"] == ("w1" if destination == "w2" else None)
                assert len(state()["snapshot"]["workspaces"]) == (6 if destination == "w2" else 7)
                assert state()["snapshot"]["focused_tab_id"] != "w1:t1", "no-focus is not immutable focus"
            # Native default focus=true and launch-pending movement are modeled
            # independently of Heeler's explicit no-focus/pending-refusal policy.
            target = reset()
            direct_state = state()
            next(a for a in direct_state["snapshot"]["agents"] if a["pane_id"] == target["pane_id"])["launch_pending"] = True
            path.write_text(json.dumps(direct_state))
            direct = subprocess.run([str(fake), "--session=default", "pane", "move", target["pane_id"], "--new-tab", "--workspace", "w2", "--label", "Native default"], capture_output=True, timeout=3)
            assert direct.returncode == 0, direct.stderr
            native_move = json.loads(direct.stdout)["result"]["move_result"]
            assert native_move["changed"] and state()["moves"][0]["focus"] is True
            assert state()["snapshot"]["focused_pane_id"] == native_move["pane"]["pane_id"]

            target = reset(move_zoomed=True)
            before = copy.deepcopy(state()["snapshot"])
            noop = structured(move(target))
            assert noop["changed"] is False and noop["reason"] == "zoomed_tab" and noop["target"] == target
            assert noop["location"] == noop["previous_location"] and state()["snapshot"] == before
            target = reset()
            unverified = {**target, "agent_session": None}
            result = structured(move(unverified))
            assert result["target"]["agent_session"] is None
            old_pane = subprocess.run([str(fake), "--session=default", "pane", "get", target["pane_id"]], capture_output=True, timeout=3)
            assert json.loads(old_pane.stdout)["result"]["pane"]["pane_id"] == result["target"]["pane_id"]
            error(client.tool("agent_move", {"target": target, "workspace_id": "w2", "label": "Again"}), "agent not found")
            assert len(move_calls()) == 1, "old agent handle must not follow a pane alias"

            reference_a = {"source": "herdr:pi", "agent": "pi", "kind": "id", "value": "synthetic-a"}
            reference_b = {**reference_a, "value": "synthetic-b"}
            # Caller identity stays unverified; each independent get override is a
            # native observation, including a reference learned just before issue.
            for first, latest, after, succeeds, calls in [
                (None, reference_a, reference_b, False, 8),
                (None, reference_a, reference_a, True, 8),
                (reference_a, reference_a, reference_b, False, 8),
                (reference_a, reference_b, reference_b, False, 4),
                (None, None, reference_b, True, 8),
                (None, reference_a, None, False, 8),
            ]:
                target = reset(get_overrides=[{"agent_session": value} for value in [first, latest, after]])
                target["agent_session"] = None
                result = move(target)
                assert len(state()["calls"]) == calls
                assert len(move_calls()) == (0 if calls == 4 else 1)
                worker = next(pane for pane in state()["snapshot"]["panes"] if pane["terminal_id"] == target["terminal_id"])
                assert worker["workspace_id"] == ("w1" if calls == 4 else "w2")
                assert (worker["pane_id"] == target["pane_id"]) == (calls == 4)
                if succeeds:
                    receipt = structured(result)
                    assert receipt["changed"] is True and receipt["target"]["agent_session"] is None
                    assert receipt["agent"]["agent_session"] == after
                else:
                    message = error(result, "conversation changed")
                    if calls == 8:
                        assert "possibly moved" in message and "inspect before retrying" in message
                        assert "do not retry or recreate locations automatically" in message
                    else:
                        assert "possibly moved" not in message
                assert notes_path.read_bytes() == notes

            target = reset()
            for label in ["", " ", "--remote=synthetic", "--handoff", "bad\tlabel", "bad\nlabel", "bad\0label", "é" * 129]:
                error(move(target, label=label), "label")
            for workspace in ["", "--session=other", "retired-workspace", "w2:t1", "w" + "a" * 128]:
                error(move(target, workspace), "workspace ID")
            for field, value in [("session", "ssh:host"), ("pane_id", "--help"), ("terminal_id", ""), ("agent", "")]:
                error(move({**target, field: value}), "invalid")
            for extra in ["mode", "destination", "tab_id", "focus", "force", "new_workspace", "split"]:
                error(client.tool("agent_move", {"target": target, "workspace_id": "w2", "label": "Worker", extra: True}), "failed to deserialize")
            assert not state().get("calls"), "validation must precede native metadata"

            for fields, expected_calls, marker in [
                ({"get_overrides": [{"terminal_id": "term_other"}]}, 1, "changed"),
                ({"get_overrides": [{"agent": "claude"}]}, 1, "kind changed"),
                ({"get_overrides": [{"agent_session": None}]}, 1, "conversation changed"),
                ({"get_overrides": [{"launch_pending": True}]}, 1, "pending"),
                ({"get_overrides": [{}, {"launch_pending": True}]}, 4, "pending"),
                ({"get_overrides": [{}, {"agent_session": None}]}, 4, "conversation changed"),
                ({"workspace_get_overrides": [{"workspace_id": "w9"}]}, 2, "destination"),
                ({"pane_get_overrides": [{"terminal_id": "other"}]}, 3, "terminal changed"),
            ]:
                target = reset(**fields)
                error(move(target), marker)
                assert len(state()["calls"]) == expected_calls and not move_calls()
            target = reset()
            error(move(target, "w99"), "workspace not found")
            assert len(state()["calls"]) == 2 and not move_calls()

            for fields, calls, marker in [
                ({"move_error_before": {"code": "workspace_not_found", "message": "destination disappeared"}}, 5, "destination disappeared"),
                ({"move_error_after": {"code": "pane_move_failed", "message": "post-move unavailable"}}, 5, "post-move unavailable"),
                ({"move_output": "not-json"}, 5, "invalid JSON"),
                ({"move_response": {"result": {"type": "pane_info", "pane": {}}}}, 5, "invalid native pane move response"),
                ({"move_result_overrides": {"previous_tab_id": "w9:t1"}}, 5, "source/terminal mismatch"),
                ({"move_result_overrides": {"created_tab": None}}, 5, "missing created tab"),
                ({"move_result_overrides": {"created_workspace": template["workspaces"][0]}}, 5, "unexpected workspace creation"),
                ({"move_result_overrides": {"closed_workspace_id": "w7"}}, 5, "unexpected closed workspace"),
                ({"move_result_overrides": {"target_layout": {"workspace_id": "w9", "tab_id": "w9:t1", "focused_pane_id": "w9:p1"}}}, 5, "target layout mismatch"),
                ({"move_result_overrides": {"reason": "same_tab"}}, 5, "destination/new-tab mismatch"),
                ({"move_zoomed": True, "move_result_overrides": {"reason": "same_tab"}}, 5, "inconsistent native new-tab no-op"),
                ({"pane_get_overrides": [{}, {"terminal_id": "other"}]}, 6, "post-move pane"),
                ({"tab_get_overrides": [{"label": "Changed elsewhere"}]}, 7, "post-move new tab changed"),
                ({"get_overrides": [{}, {}, {"agent": "claude"}]}, 8, "kind changed"),
                ({"get_overrides": [{}, {}, {"agent_session": None}]}, 8, "conversation changed"),
                ({"stall_move": True}, 5, "timed out"),
            ]:
                target = reset(**fields)
                message = error(move(target), marker)
                assert "possibly moved" in message and "inspect before retrying" in message and "do not retry or recreate" in message
                assert len(state()["calls"]) == calls and len(move_calls()) == 1
                assert notes_path.read_bytes() == notes

            for operation, skip, calls, mutations in [("get", 0, 1, 0), ("workspace_get", 0, 2, 0), ("pane_get", 0, 3, 0),
                                                       ("get", 1, 4, 0), ("move", 0, 5, 1), ("pane_get", 1, 6, 1)]:
                with socket.socket(socket.AF_UNIX) as listener:
                    listener.settimeout(3)
                    listener.bind(str(root / f"{operation}-{skip}.sock"))
                    listener.listen(1)
                    target = reset(dispatch_gate={"command": operation, "skip": skip, "socket": f"{operation}-{skip}.sock"})
                    client.counter += 1
                    request_id = client.counter
                    client.send({"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": {
                        "name": "agent_move", "arguments": {"target": target, "workspace_id": "w2", "label": "Worker"}}})
                    connection, _ = listener.accept()
                    with connection:
                        connection.settimeout(2)
                        assert connection.recv(1) == b"x"
                        before_cancel = state()
                        client.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": request_id}})
                        assert client.call("ping", {}, timeout=2) == {}
                        assert connection.recv(1) == b"", "cancel must drop the native future before gate release"
                    assert state() == before_cancel and len(state()["calls"]) == calls and len(move_calls()) == mutations
                    assert notes_path.read_bytes() == notes
            assert structured(client.tool("workstream_get", {"workstream_id": "feature"}))["record"] == saved
            assert notes_path.read_bytes() == notes
        finally:
            client.close()
    print("agent move smoke: new-tab identities, aliases, containers/focus, no-op, pending/busy, uncertainty/cancellation and unchanged notes passed")
