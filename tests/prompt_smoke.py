"""Settled ordinary prompts against a source-grounded strict native double."""
import copy
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile


from mcp_client import Client, structured


def expect_error(result, text):
    assert result.get("isError"), result
    message = result.get("structuredContent", {}).get("error", result["content"][0].get("text", ""))
    assert text in message, result
    return message


def check_prompts(binary, tool):
    schema = tool["inputSchema"]
    assert set(schema["required"]) == {"target", "text"} and schema["additionalProperties"] is False
    assert schema["properties"]["text"]["type"] == "string"
    assert tool["annotations"]["readOnlyHint"] is False
    assert tool["annotations"]["destructiveHint"] is True and tool["annotations"]["idempotentHint"] is False
    source = Path(__file__).parent
    original_snapshot = json.loads((source / "fixtures" / "herdr-0.9.5-snapshot.json").read_text())["result"]["snapshot"]
    with tempfile.TemporaryDirectory(prefix="heeler-prompt-", dir="/tmp") as directory:
        root = Path(directory)
        state_path = root / "native.json"
        fake = root / "herdr"
        fake.write_text(f"#!{sys.executable}\n" + (source / "fake_herdr.py").read_text())
        fake.chmod(0o700)

        def reset(kind="pi", status="idle", pending=False, ready=True, **fields):
            snapshot = copy.deepcopy(original_snapshot)
            agent = snapshot["agents"][0]
            agent.update(name="worker", agent=kind, agent_status=status, launch_pending=pending,
                         interactive_ready=ready, agent_session={"source": "herdr:" + kind, "agent": kind,
                                                                  "kind": "id", "value": "synthetic-session"})
            for collection in ["workspaces", "tabs", "panes"]:
                snapshot[collection][0]["agent_status"] = status
            state_path.write_text(json.dumps({"snapshot": snapshot, **fields}))
            return {"session": "default", "pane_id": agent["pane_id"], "terminal_id": agent["terminal_id"],
                    "agent": kind, "agent_session": copy.deepcopy(agent["agent_session"])}

        def state():
            return json.loads(state_path.read_text())

        def prompt(target, text="Clarify the finding"):
            return client.tool("agent_prompt", {"target": target, "text": text}, timeout=20)

        target = reset()
        client = Client(binary, root)
        try:
            history = {"id": "old", "role": "review", "target": target, "brief": "Historical brief",
                       "constraints": ["Keep history"], "acceptance_criteria": ["Report findings"],
                       "report_path": str(root / "report.md"), "delivery": "submitted", "outcome": "Existing outcome"}
            work = {"id": "feature", "title": "Feature", "purpose": "Keep intent", "status": "paused",
                    "next_action": "Inspect reports", "blocker": "Needs a decision", "assignments": [history]}
            saved_record = structured(client.tool("workstream_save", {"workstream": work, "expected_revision": 0}))["record"]
            notes_path = root / "state" / "workstreams.json"
            notes = notes_path.read_bytes()
            for kind, status in [(kind, status) for kind in ["pi", "codex", "claude"] for status in ["idle", "done"]] + [("codex", "unknown")]:
                for text in ["Explain α\n\t'quoted' $() ; words", "--help", "--wait", "--", "--mode=steer", "x" * 32768, "é" * 16384]:
                    target = reset(kind, status)
                    before = state()["snapshot"]
                    result = structured(prompt(target, text))
                    assert result["target"] == target and result["acknowledged"] is True
                    assert result["metadata_timing"] == "pre_submission" and result["agent"]["agent_status"] == status
                    assert "text" not in result
                    after = state()
                    assert after["calls"] == [["--session=default", "agent", "get", target["pane_id"]],
                                              ["--session=default", "agent", "prompt", target["pane_id"], text]]
                    assert after["prompts"] == [{"pane_id": target["pane_id"], "text": text}]
                    assert after["snapshot"] == before and notes_path.read_bytes() == notes

            safe_texts = [
                "--handoff", "--handoff=anything", "--sessions=other", "--sessionish=other",
                "--remoteish=host", "--remote-keybindingsish=local", "--SESSION=other", "--Remote=host",
                "--session other", "--remote host", "--remote-keybindings local", "--session:other",
                "prefix --session=other", " --session=other", "\n--remote=host", "--session\tother",
                "--machine=synthetic", "Explain α\n\t--remote-keybindings=local",
            ]
            for text in safe_texts:
                target = reset()
                result = structured(prompt(target, text))
                assert result["acknowledged"] is True
                assert state()["prompts"] == [{"pane_id": target["pane_id"], "text": text}]
                assert state()["calls"][-1] == ["--session=default", "agent", "prompt", target["pane_id"], text]
                assert state()["routing"][-1]["session"] == "default"
                assert state()["routing"][-1]["after_remote"] == ["agent", "prompt", target["pane_id"], text]
                assert state()["routing"][-1]["handoff"] == (text == "--handoff")
                assert notes_path.read_bytes() == notes

            target = reset()
            for option in ["--session", "--remote", "--remote-keybindings"]:
                for text in [option] + [option + "=" + value for value in ["", "other", "../bad", "-bad", "local", "server", "invalid value", "x\ny"]]:
                    message = expect_error(prompt(target, text), "native herdr CLI global option")
                    assert option in message and "rephrase" in message and "no native calls" in message
                    assert "possibly submitted" not in message and "resend" not in message
                    assert state().get("calls", []) == [] and not state().get("prompts")
                    assert notes_path.read_bytes() == notes

            # Independently call the fake CLI to prove it consumes globals BEFORE
            # literal text protection. Do not use Heeler's rejection rule in the fake.
            for option in ["--session", "--remote", "--remote-keybindings"]:
                for text in [option, option + "=", option + "=other", option + "=local", option + "=-bad"]:
                    target = reset()
                    result = subprocess.run([str(fake), "--session=default", "agent", "prompt", target["pane_id"], text], capture_output=True, timeout=3)
                    assert result.returncode != 0 and not state().get("prompts")
                    assert len(state()["calls"]) == 1
                    if text == "--session=other":
                        assert state()["routing"][0]["session"] == "other"
            target = reset()
            result = subprocess.run([str(fake), "--session=default", "agent", "prompt", target["pane_id"], "--session=default"], capture_output=True, timeout=3)
            assert result.returncode != 0 and not state().get("prompts")
            assert state()["routing"][0]["after_remote"] == ["agent", "prompt", target["pane_id"]]

            target = reset(advance_prompt_status="working")
            unverified = {**target, "agent_session": None}
            result = structured(prompt(unverified))
            assert result["target"]["agent_session"] is None, "do not manufacture conversation verification"
            assert result["agent"]["agent_status"] == "idle" and state()["snapshot"]["agents"][0]["agent_status"] == "working"
            assert result["metadata_timing"] == "pre_submission", "ack metadata is not a fresh final observation"

            target = reset()
            for text in ["", " \t\n", "a\0b", "a\rb", "\x1b[A", "\x03", "\x7f", "\x85", "x" * 32769, "é" * 16385]:
                expect_error(prompt(target, text), "UTF-8 bytes")
            assert state().get("calls", []) == []
            for arguments in [{"target": target}, {"text": "Hello"}] + [
                {"target": target, "text": "Hello", field: True} for field in ["mode", "force", "keys", "wait", "assignment_id"]
            ]:
                result = client.tool("agent_prompt", arguments)
                expect_error(result, "failed to deserialize parameters")
            assert state().get("calls", []) == []
            for field, value in [("session", "../bad"), ("session", "ssh:host"), ("session", "a" * 65),
                                 ("pane_id", "--help"), ("pane_id", "w1:t1"), ("pane_id", "w1:p"),
                                 ("pane_id", "w1:p" + "a" * 33), ("terminal_id", ""), ("terminal_id", "a" * 129),
                                 ("agent", ""), ("agent", "a" * 65), ("agent_session", "a" * 8193)]:
                expect_error(prompt({**target, field: value}), "too large" if field == "agent_session" else "invalid")
            assert state().get("calls", []) == []
            expect_error(prompt({**target, "session": "work.test"}), "Selected local session does not exist")
            assert state()["calls"] == [["--session=work.test", "agent", "get", target["pane_id"]]]

            for kind, status, ready, pending in [
                ("pi", "working", True, False), ("codex", "working", True, False), ("claude", "working", True, False),
                ("pi", "blocked", True, False), ("codex", "blocked", True, False), ("claude", "blocked", True, False),
                ("pi", "unknown", True, False), ("claude", "unknown", True, False), ("codex", "unknown", False, False),
                ("codex", "unknown", True, True), ("pi", "idle", True, True), ("claude", "done", True, True),
            ]:
                target = reset(kind, status, ready=ready, pending=pending)
                expect_error(prompt(target), "no longer ready")
                assert len(state()["calls"]) == 1 and not state().get("prompts")
            for field, value in [("terminal_id", "term_other"), ("agent", "claude"),
                                 ("agent_session", {"kind": "id", "value": "different"})]:
                target = reset()
                expect_error(prompt({**target, field: value}), "changed")
                assert len(state()["calls"]) == 1 and not state().get("prompts")
            target = reset()
            expect_error(prompt({**target, "pane_id": "w1:p99"}), "agent not found")
            assert len(state()["calls"]) == 1 and not state().get("prompts")
            for response in [{"result": None}, {"result": {"type": "agent_info", "agent": {}}},
                             {"result": {"type": "agent_started", "agent": state()["snapshot"]["agents"][0]}}]:
                target = reset(get_response=response)
                result = prompt(target)
                assert result.get("isError") and len(state()["calls"]) == 1 and not state().get("prompts")

            cases = [
                ({"prompt_error": {"code": "agent_blocked", "message": "blocked after preflight"}}, "agent_blocked", 0),
                ({"fail_prompt": True}, "connection lost", 1),
                ({"prompt_output": "not-json"}, "invalid JSON", 1),
                ({"prompt_output": "invalid_utf8"}, "invalid UTF-8", 1),
                ({"prompt_output": "x" * (4 * 1024 * 1024 + 1)}, "output exceeded limit", 1),
                ({"prompt_response": {"result": {"type": "agent_prompted", "agent": {}}}}, "invalid submission", 1),
                ({"prompt_response": {"result": {"type": "ok"}}}, "invalid submission", 1),
                ({"prompt_response": {"result": {"type": "agent_info", "agent": original_snapshot["agents"][0]}}}, "unexpected submission", 1),
                ({"prompt_agent_overrides": {"pane_id": "w1:p9"}}, "location changed", 1),
                ({"prompt_agent_overrides": {"terminal_id": "term_other"}}, "location changed", 1),
                ({"prompt_agent_overrides": {"agent": "claude"}}, "kind changed", 1),
                ({"prompt_agent_overrides": {"agent_session": None}}, "conversation changed", 1),
                ({"stall_prompt": True}, "timed out", 1),
            ]
            for fields, error, inputs in cases:
                target = reset(**fields)
                message = expect_error(prompt(target), error)
                assert "possibly submitted" in message and "inspect before retrying" in message and "do not resend automatically" in message
                assert len(state()["calls"]) == 2 and len(state().get("prompts", [])) == inputs
                assert notes_path.read_bytes() == notes
            target = reset()
            current = state()
            current["snapshot"]["agents"][0]["foreground_matches"] = False
            state_path.write_text(json.dumps(current))
            expect_error(prompt(target), "agent_not_ready")
            assert len(state()["calls"]) == 2 and not state().get("prompts")

            # Independent native fake accepts working/unknown ordinary input; it is not
            # secretly implementing Heeler's settled predicate or a logical message queue.
            for kind in ["pi", "codex", "claude"]:
                for status in ["working", "unknown"]:
                    target = reset(kind, status, ready=False)
                    result = subprocess.run([str(fake), "--session=default", "agent", "prompt", target["pane_id"], "ordinary"], capture_output=True, timeout=3)
                    assert result.returncode == 0, result.stderr
                    response = json.loads(result.stdout)
                    assert response["id"] == "cli:agent:prompt" and response["result"]["type"] == "agent_prompted"
                    assert response["result"]["agent"]["agent_status"] == status
            for operation in [["prompt", "w1:p1", "hello", "--mode", "steer"], ["interrupt", "w1:p1"],
                              ["stop", "w1:p1"], ["steer", "w1:p1", "hello"]]:
                reset()
                result = subprocess.run([str(fake), "--session=default", "agent", *operation], capture_output=True, timeout=3)
                assert result.returncode != 0 and not state().get("prompts")

            for operation, calls, inputs in [("get", 1, 0), ("prompt", 2, 1)]:
                with socket.socket(socket.AF_UNIX) as listener:
                    listener.settimeout(3)
                    listener.bind(str(root / f"{operation}.sock"))
                    listener.listen(1)
                    target = reset(dispatch_gate={"command": operation, "socket": f"{operation}.sock"})
                    client.counter += 1
                    request_id = client.counter
                    client.send({"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": {
                        "name": "agent_prompt", "arguments": {"target": target, "text": "One follow-up"},
                    }})
                    connection, _ = listener.accept()
                    with connection:
                        connection.settimeout(2)
                        assert connection.recv(1) == b"x"
                        before_cancel = state()
                        client.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": request_id}})
                        assert client.call("ping", {}, timeout=2) == {}
                        # EOF before releasing the gate proves cancellation dropped the
                        # native future. In-flight input remains recorded, never rolled back.
                        assert connection.recv(1) == b""
                    assert state() == before_cancel and len(state()["calls"]) == calls
                    assert len(state().get("prompts", [])) == inputs
                    assert notes_path.read_bytes() == notes
            assert structured(client.tool("workstream_get", {"workstream_id": "feature"}))["record"] == saved_record
            assert notes_path.read_bytes() == notes
        finally:
            client.close()
    print("settled prompt smoke: native contract, identity/text/readiness, literal argv, uncertainty/timeout, cancellation, and unchanged notes passed")
