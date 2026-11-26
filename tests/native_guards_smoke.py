"""Live text/label/identity/cancellation guards using owned notes and a synthetic CLI."""
import copy
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile

from mcp_client import Client, structured

SOURCE = Path(__file__).parent
TEMPLATE = json.loads((SOURCE / "fixtures/herdr-0.9.5-snapshot.json").read_text())["result"]["snapshot"]
REFERENCE_A = {"agent": "pi", "source": "herdr:pi", "kind": "id", "value": "synthetic-a"}
REFERENCE_B = {**REFERENCE_A, "value": "synthetic-b"}


def install_fake(root):
    fake = root / "herdr"
    fake.write_text(f"#!{sys.executable}\n" + (SOURCE / "fake_herdr.py").read_text())
    fake.chmod(0o700)
    return fake


def snapshot():
    result = copy.deepcopy(TEMPLATE)
    worker = copy.deepcopy(result["agents"][0])
    worker.update(pane_id="w1:p2", terminal_id="term_guard", agent_status="idle",
                  interactive_ready=True, launch_pending=False, agent_session=REFERENCE_A)
    pane = copy.deepcopy(result["panes"][0])
    pane.update(pane_id="w1:p2", terminal_id="term_guard", focused=False)
    result["panes"].append(pane)
    result["agents"].append(worker)
    return result


def target(reference=None):
    return {"session": "default", "pane_id": "w1:p2", "terminal_id": "term_guard",
            "agent": "pi", "agent_session": reference}


def handoff(name="guard", reference=None):
    return {"id": name, "role": "review", "target": target(reference), "brief": "Synthetic task",
            "constraints": [], "acceptance_criteria": ["Temporary fakes only"],
            "report_path": "/tmp/synthetic-report.md", "delivery": "prepared", "outcome": None}


def save(client, name, assignment):
    return structured(client.tool("workstream_save", {
        "expected_revision": 0,
        "workstream": {"id": name, "title": "Synthetic", "purpose": "Guard regression",
                       "status": "active", "assignments": [assignment]},
    }))["record"]


def error(result, expected):
    assert result.get("isError"), result
    message = result["structuredContent"]["error"]
    assert expected in message, result
    return message


def check_labels(binary):
    with tempfile.TemporaryDirectory(prefix="heeler-label-") as directory:
        root = Path(directory)
        fake = install_fake(root)
        path = root / "native.json"
        initial = {"sessions": {"default": snapshot(), "--cwd": snapshot()}}
        path.write_text(json.dumps(initial))
        client = Client(binary, root)
        try:
            for area in ["workspace", "tab"]:
                for label in ["--session", "--session=other", "--remote=synthetic",
                              "--remote-keybindings=local", "--handoff", "-unsafe"]:
                    location = {"type": area, "cwd": "/work/project", "label": label}
                    if area == "tab":
                        location["workspace_id"] = "w1"
                    before = path.read_bytes()
                    error(client.tool("location_open", {"session": "default", "location": location}), "label must be")
                    assert path.read_bytes() == before, "unsafe labels must make zero native calls"
                location = {"type": area, "cwd": "/work/project", "label": "Review α --session=embedded"}
                if area == "tab":
                    location["workspace_id"] = "w1"
                result = structured(client.tool("location_open", {"session": "default", "location": location}))
                assert result["location"]["session"] == "default" and result["pane"]["cwd"] == "/work/project"
                state = json.loads(path.read_text())
                assert state["routing"][-1]["session"] == "default"
                assert state["sessions"]["--cwd"] == initial["sessions"]["--cwd"]
        finally:
            client.close()
        # Independent native-valid branch: global preprocessing consumes the unsafe label
        # and --cwd, leaving an optional-cwd create in the synthetic --cwd session.
        for area in ["workspace", "tab"]:
            path.write_text(json.dumps(initial))
            args = [str(fake), "--session=default", area, "create"]
            if area == "tab":
                args += ["--workspace", "w1"]
            args += ["--label", "--session", "--cwd", "/work/project", "--no-focus"]
            result = subprocess.run(args, capture_output=True, timeout=5)
            assert result.returncode == 0, result.stderr
            pane = json.loads(result.stdout)["result"]["root_pane"]
            assert pane["cwd"] == "/work/default"
            state = json.loads(path.read_text())
            assert state["routing"][-1]["session"] == "--cwd"
            assert state["sessions"]["default"] == initial["sessions"]["default"]
            assert state["sessions"]["--cwd"] != initial["sessions"]["--cwd"]


def check_live_assignments(binary):
    with tempfile.TemporaryDirectory(prefix="heeler-text-") as directory:
        root = Path(directory)
        install_fake(root)
        path = root / "native.json"
        path.write_text(json.dumps({"snapshot": snapshot(), "expect_saved_assignment": True}))
        client = Client(binary, root)
        try:
            case = 0
            for field in ["brief", "constraints", "acceptance_criteria"]:
                for text in ["end\x1b[201~\x03", "carriage\rreturn", "delete\x7f", "control\u0085"]:
                    case += 1
                    assignment = handoff()
                    assignment[field] = text if field == "brief" else [text]
                    name = f"controls-{case}"
                    save(client, name, assignment)
                    notes = root / "state/workstreams.json"
                    before = notes.read_bytes()
                    native_before = path.read_bytes()
                    error(client.tool("assignment_send", {"workstream_id": name, "assignment_id": "guard",
                                                         "expected_revision": 1}), "UTF-8 bytes")
                    assert notes.read_bytes() == before and path.read_bytes() == native_before
                    observed = structured(client.tool("workstream_get", {"workstream_id": name}))["record"]
                    assert observed["revision"] == 1 and observed["workstream"]["assignments"][0] == assignment
            # Use actual rendered bytes to calibrate the exact boundary without copying the template.
            assignment = handoff("boundary")
            assignment["brief"] = "x"
            assignment["constraints"] = ["x" * 1024] * 16
            assignment["acceptance_criteria"] = ["y" * 1024] * 7 + ["z"]
            save(client, "calibration", assignment)
            structured(client.tool("assignment_send", {"workstream_id": "calibration", "assignment_id": "boundary",
                                                       "expected_revision": 1}))
            rendered = json.loads(path.read_text())["prompts"][-1]["text"].encode()
            remaining = 32768 - len(rendered)
            brief_growth = min(remaining, 8191)
            assignment["brief"] += "x" * brief_growth
            assignment["acceptance_criteria"][-1] += "z" * (remaining - brief_growth)
            assert len(assignment["acceptance_criteria"][-1]) < 1024
            save(client, "exact-boundary", assignment)
            receipt = structured(client.tool("assignment_send", {"workstream_id": "exact-boundary",
                                                                  "assignment_id": "boundary", "expected_revision": 1}))
            assert receipt["delivery"] == "submitted"
            assert len(json.loads(path.read_text())["prompts"][-1]["text"].encode()) == 32768
            assignment["acceptance_criteria"][-1] += "z"
            assignment["outcome"] = "Raw historical claim\x00\x1b"
            save(client, "over-boundary", assignment)
            notes = root / "state/workstreams.json"
            before = notes.read_bytes()
            native_before = path.read_bytes()
            error(client.tool("assignment_send", {"workstream_id": "over-boundary", "assignment_id": "boundary",
                                                 "expected_revision": 1}), "UTF-8 bytes")
            structured(client.tool("workstream_list", {"compact": True}))
            observed = structured(client.tool("workstream_get", {"workstream_id": "over-boundary"}))["record"]
            assert observed["workstream"]["assignments"][0] == assignment
            claim = structured(client.tool("assignment_outcome", {"workstream_id": "over-boundary", "assignment_id": "boundary"}))
            assert claim["snapshot"]["reported_text"] == assignment["outcome"]
            assert notes.read_bytes() == before and path.read_bytes() == native_before
        finally:
            client.close()


def check_identity(binary):
    with tempfile.TemporaryDirectory(prefix="heeler-identity-") as directory:
        root = Path(directory)
        install_fake(root)
        path = root / "native.json"
        client = Client(binary, root)
        try:
            cases = [
                (None, REFERENCE_A, REFERENCE_A, False),
                (None, REFERENCE_A, REFERENCE_B, True),
                (None, REFERENCE_A, None, True),
                (REFERENCE_A, REFERENCE_A, REFERENCE_A, False),
                (REFERENCE_A, REFERENCE_B, REFERENCE_B, True),
                (REFERENCE_A, REFERENCE_A, None, True),
                (None, None, REFERENCE_A, False),
                (None, None, None, False),
            ]
            for operation in ["read", "wait", "focus", "prompt"]:
                for caller, first, final, refused in cases:
                    state = {"snapshot": snapshot()}
                    if operation == "read":
                        state["get_overrides"] = [{"agent_session": first}, {"agent_session": final}]
                    else:
                        state["get_overrides"] = [{"agent_session": first}]
                        state[operation + "_agent_overrides"] = {"agent_session": final}
                    path.write_text(json.dumps(state))
                    request: dict[str, object] = {"target": target(caller)}
                    if operation == "prompt":
                        request["text"] = "Ordinary synthetic message\nwith\ttab"
                    result = client.tool("agent_" + operation, request)
                    calls = json.loads(path.read_text())["calls"]
                    first_refused = caller is not None and first != caller
                    expected = ["get"] if first_refused else ["get", operation] + (["get"] if operation == "read" else [])
                    assert [call[2] for call in calls] == expected
                    if refused:
                        message = error(result, "conversation changed")
                        assert "text" not in result["structuredContent"]
                        if not first_refused and operation in ["prompt", "focus"]:
                            assert ("possibly submitted" if operation == "prompt" else "focus may have taken effect") in message
                    else:
                        result = structured(result)
                        if operation == "prompt":
                            assert result["target"]["agent_session"] == caller
                            assert result["acknowledged"] and result["metadata_timing"] == "pre_submission"
            # Dispatch must pass its first pin to the fresh get, and that latest pin to capture.
            send_cases = [
                (None, REFERENCE_A, REFERENCE_A, REFERENCE_A, False, True),
                (None, REFERENCE_A, REFERENCE_B, REFERENCE_B, True, False),
                (None, REFERENCE_A, None, None, True, False),
                (None, REFERENCE_A, REFERENCE_A, REFERENCE_B, True, True),
                (None, None, REFERENCE_A, REFERENCE_B, True, True),
                (None, None, None, None, False, True),
                (REFERENCE_A, REFERENCE_A, REFERENCE_A, REFERENCE_A, False, True),
                (REFERENCE_A, REFERENCE_B, REFERENCE_B, REFERENCE_B, True, False),
            ]
            for index, (caller, first, second, capture, refused, issued) in enumerate(send_cases):
                state = {"snapshot": snapshot(), "expect_saved_assignment": True,
                         "get_overrides": [{"agent_session": first}, {"agent_session": second}],
                         "prompt_agent_overrides": {"agent_session": capture}}
                path.write_text(json.dumps(state))
                name = f"identity-{index}"
                save(client, name, handoff(reference=caller))
                result = client.tool("assignment_send", {"workstream_id": name, "assignment_id": "guard",
                                                         "expected_revision": 1})
                state = json.loads(path.read_text())
                assert bool(state.get("prompts")) == issued
                record = structured(client.tool("workstream_get", {"workstream_id": name}))["record"]
                original = record["workstream"]["assignments"][0]
                assert original["target"] == target(caller)
                first_refused = caller is not None and first != caller
                if refused:
                    message = error(result, "conversation changed")
                    assert record["revision"] == (1 if first_refused else 2)
                    assert original["delivery"] == ("prepared" if first_refused else "uncertain")
                    if issued:
                        assert "possibly submitted" in message
                else:
                    assert structured(result)["delivery"] == "submitted"
                    assert record["revision"] == 3 and original["delivery"] == "submitted"
        finally:
            client.close()


def check_cancellation_and_issued_errors(binary):
    with tempfile.TemporaryDirectory(prefix="heeler-cancel-", dir="/tmp") as directory:
        root = Path(directory)
        install_fake(root)
        path = root / "native.json"
        client = Client(binary, root)
        try:
            for operation in ["focus", "wait", "read"]:
                for phase in ["get", operation]:
                    path.write_text(json.dumps({"snapshot": snapshot(), "dispatch_gate": {
                        "command": phase, "socket": "gate.sock",
                    }}))
                    with socket.socket(socket.AF_UNIX) as listener:
                        gate_path = root / "gate.sock"
                        listener.bind(str(gate_path))
                        listener.listen(1)
                        listener.settimeout(5)
                        client.counter += 1
                        request_id = client.counter
                        client.send({"jsonrpc": "2.0", "id": request_id, "method": "tools/call",
                                     "params": {"name": "agent_" + operation, "arguments": {"target": target()}}})
                        connection, _ = listener.accept()
                        with connection:
                            connection.settimeout(3)
                            assert connection.recv(1) == b"x"
                            client.send({"jsonrpc": "2.0", "method": "notifications/cancelled",
                                         "params": {"requestId": request_id, "reason": "Withdraw synthetic operation"}})
                            assert client.call("ping", {}, timeout=3) == {}
                            assert connection.recv(1) == b"", "cancel must drop the still-gated CLI child"
                    gate_path.unlink()
                    state = json.loads(path.read_text())
                    expected = ["get"] if phase == "get" else ["get", operation]
                    assert [call[2] for call in state["calls"]] == expected
                    assert state["snapshot"]["focused_pane_id"] == ("w1:p2" if operation == phase == "focus" else "w1:p1")
            # Decoding and handle-validation errors after an issued effect must not sound safe to retry.
            for response in [{"result": {"type": "agent_info", "agent": {}}},
                             {"result": {"type": "agent_started", "agent": {}}}]:
                path.write_text(json.dumps({"snapshot": snapshot(), "focus_response": response}))
                message = error(client.tool("agent_focus", {"target": target()}), "focus may have taken effect")
                assert "inspect" in message and "do not retry automatically" in message
                state = json.loads(path.read_text())
                assert state["snapshot"]["focused_pane_id"] == "w1:p2"
                assert len(state["calls"]) == 2
            initial = snapshot()
            initial["agents"] = initial["agents"][:1]
            path.write_text(json.dumps({"snapshot": initial, "start_agent_overrides": {"terminal_id": "x" * 129}}))
            message = error(client.tool("agent_start", {"session": "default", "start": {
                "pane_id": "w1:p2", "name": "synthetic", "kind": "pi",
            }}), "launch may have taken effect")
            assert "invalid terminal ID" in message and "inspect" in message
            state = json.loads(path.read_text())
            assert len(state["snapshot"]["agents"]) == 2 and len(state["calls"]) == 1
        finally:
            client.close()


def check_native_guards(binary):
    check_labels(binary)
    check_live_assignments(binary)
    check_identity(binary)
    check_cancellation_and_issued_errors(binary)
    print("native guards: zero-call text/label refusals, exact live budget/readable raw history, multi-session native control, learned conversation pins, cancellation gates and issued-error honesty passed")


if __name__ == "__main__":
    check_native_guards(str(Path(sys.argv[1]).resolve()))
