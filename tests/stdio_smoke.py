#!/usr/bin/env python3
"""End-to-end stdio MCP check using only temporary state and a fake herdr."""
import copy
import json
import os
from pathlib import Path
import selectors
import socket
import subprocess
import sys
import tempfile
import time

from memory_smoke import check_memory
from move_smoke import check_moves
from outcome_smoke import check_outcomes
from prompt_smoke import check_prompts
from packaging_smoke import check_packaging


class Client:
    def __init__(self, binary, root, *, extra_env=None, args=None, cwd=None):
        env = {k: v for k, v in os.environ.items() if not k.startswith("HERDR_")}
        env.update(HERDR_ENV="1", HERDR_SESSION="default", HERDR_PANE_ID="w9:p1")
        env.update(extra_env or {})
        if args is None:
            args = ["--state-dir", str(root / "state"), "--herdr-bin", str(root / "herdr")]
        self.stderr = tempfile.TemporaryFile()
        self.process = subprocess.Popen(
            [binary, *args],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.stderr, env=env, cwd=cwd,
        )
        assert self.process.stdin is not None and self.process.stdout is not None
        self.stdin = self.process.stdin
        self.stdout = self.process.stdout
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.stdout, selectors.EVENT_READ)
        self.buffer = b""
        self.counter = 0
        try:
            initialized = self.call("initialize", {
                "protocolVersion": "2025-11-25", "capabilities": {},
                "clientInfo": {"name": "heeler-smoke", "version": "1"},
            })
            assert initialized["serverInfo"]["name"] == "heeler"
            self.initialized = initialized
            self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        except BaseException:
            self.abort()
            raise

    def send(self, message):
        self.stdin.write(json.dumps(message).encode() + b"\n")
        self.stdin.flush()

    def call(self, method, params, timeout=15):
        self.counter += 1
        request_id = self.counter
        self.send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        while True:
            while b"\n" in self.buffer:
                line, self.buffer = self.buffer.split(b"\n", 1)
                message = json.loads(line)
                if message.get("id") == request_id:
                    assert "error" not in message, message
                    return message["result"]
            remaining = deadline - time.monotonic()
            assert remaining > 0 and self.selector.select(remaining), "MCP response timed out"
            chunk = os.read(self.stdout.fileno(), 65536)
            assert chunk, "MCP stdout closed before response"
            self.buffer += chunk

    def tool(self, name, arguments=None, timeout=15):
        return self.call("tools/call", {"name": name, "arguments": arguments or {}}, timeout)

    def close(self):
        try:
            self.stdin.close()
            code = self.process.wait(timeout=3)
            self.stderr.seek(0)
            assert code == 0, self.stderr.read().decode()
        finally:
            self.abort()

    def abort(self):
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=5)
        try:
            self.stdin.close()
        except BrokenPipeError:
            pass
        self.selector.close()
        self.stdout.close()
        self.stderr.close()


def structured(result):
    assert not result.get("isError"), result
    return result["structuredContent"]


def check_lock_contention(binary, root):
    # fs2 uses flock on Unix; hold the same lock from outside the server.
    import fcntl

    state = root / "state"
    original = (state / "workstreams.json").read_bytes()
    with (state / "workstreams.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        client = Client(binary, root)
        try:
            busy = client.tool("workstream_list", timeout=3)
            assert busy["isError"] and "store is busy" in busy["structuredContent"]["error"]
            record = {"id": "blocked-write", "title": "Blocked", "purpose": "Test", "status": "planned"}
            busy = client.tool("workstream_save", {"workstream": record, "expected_revision": 0}, timeout=3)
            assert busy["isError"] and "store is busy" in busy["structuredContent"]["error"]
        finally:
            client.close()

        # EOF while a lock acquisition is in flight must not strand the process.
        client = Client(binary, root)
        try:
            client.send({"jsonrpc": "2.0", "id": 999, "method": "tools/call",
                         "params": {"name": "workstream_list", "arguments": {}}})
            assert not client.selector.select(0.2), "lock should still be contended"
        finally:
            client.close()
        assert (state / "workstreams.json").read_bytes() == original

    client = Client(binary, root)
    try:
        assert len(structured(client.tool("workstream_list"))["workstreams"]) == 1
    finally:
        client.close()


def check_renames(client, root, tools):
    state_path = root / "native.json"
    for area, native_id in [("workspace", "w1"), ("tab", "w1:t1")]:
        name = area + "_rename"
        schema = tools[name]["inputSchema"]
        assert set(schema["required"]) == {"session", area + "_id", "label"}
        assert schema["additionalProperties"] is False
        annotations = tools[name]["annotations"]
        assert annotations["readOnlyHint"] is False
        assert annotations["destructiveHint"] is False
        assert annotations["idempotentHint"] is True
        arguments = {"session": "default", area + "_id": native_id, "label": "Lead"}
        before = json.loads(state_path.read_text())
        for label in ["Lead & notes; $(not-a-command) 'quoted' \\", "é" * 128, "a" * 256]:
            arguments["label"] = label
            result = structured(client.tool(name, arguments))
            expected = copy.deepcopy(before["snapshot"])
            expected[area + "s"][0]["label"] = label
            after = json.loads(state_path.read_text())
            assert after["snapshot"] == expected, "rename must change only the specified label, not focus"
            assert after["calls"] == before.get("calls", []) + [
                ["--session=default", area, "rename", native_id, label]]
            assert result == {"session": "default", area: expected[area + "s"][0]}
            before = after

        def expect_error(changes, message, native_calls=0):
            request = {**arguments, **changes}
            prior = json.loads(state_path.read_text())
            result = client.tool(name, request)
            assert result.get("isError"), result
            assert message in result["structuredContent"]["error"], result
            after = json.loads(state_path.read_text())
            assert after["snapshot"] == prior["snapshot"]
            assert len(after["calls"]) == len(prior["calls"]) + native_calls, "never retry a rename"
            if native_calls:
                assert after["calls"][-1] == [
                    "--session=" + request["session"], area, "rename", request[area + "_id"], request["label"]]

        for label in ["", "  ", "a\n", "a\r", "a\t", "a\0b", "a\x7fb", "a" * 257, "é" * 129,
                      "--help", "--focus=w1", "--", "-x"]:
            expect_error({"label": label}, "label")
        for native_id in ["", "--help", "../w1", "w1 --focus", "w1:t1;t2", "w" + "a" * 128]:
            expect_error({area + "_id": native_id}, "ID")
        if area == "tab":
            for native_id in ["w1", "w1:p1", "w1:t", "w1:t1:t2", "w1:t" + "a" * 33]:
                expect_error({"tab_id": native_id}, "ID")
        for session in ["", "../bad", "ssh:host", "default --remote=host"]:
            expect_error({"session": session}, "session")
        expect_error({"session": "work.test"}, "explicit default local session", native_calls=1)
        missing_id = "w99" if area == "workspace" else "w99:t1"
        expect_error({area + "_id": missing_id}, "not found", native_calls=1)
        for flag, message in [("fail_rename", "native rename denied"),
                              ("rename_api_error", "native rename API denied")]:
            state = json.loads(state_path.read_text())
            state[flag] = True
            state_path.write_text(json.dumps(state))
            expect_error({}, message, native_calls=1)


def check_named_inventory(client, root):
    state_path = root / "native.json"
    state = json.loads(state_path.read_text())
    snapshot = state["snapshot"]
    workspace_template = snapshot["workspaces"][0]
    tab_template = snapshot["tabs"][0]
    pane_template = snapshot["panes"][0]
    agent_template = snapshot["agents"][0]
    snapshot.update(workspaces=[], tabs=[], panes=[], agents=[])
    pane_numbers = {"w1": 0, "w2": 0}
    for workspace_id, number, label, status, count in [
        ("w1", 1, "impl → inventory", "working", 1),
        ("w1", 2, "Review", "idle", 2),
        ("w1", 3, None, "blocked", 1),
        ("w2", 1, "Review", "done", 1),
        ("w2", 2, "検証 with spaces", "unknown", 1),
        ("w2", 3, "", "idle", 1),
    ]:
        tab_id = f"{workspace_id}:t{number}"
        focused = tab_id == "w1:t1"
        snapshot["tabs"].append({**tab_template, "workspace_id": workspace_id, "tab_id": tab_id,
                                 "number": number, "label": label, "focused": focused,
                                 "pane_count": count, "agent_status": status})
        for index in range(count):
            pane_numbers[workspace_id] += 1
            pane_id = f"{workspace_id}:p{pane_numbers[workspace_id]}"
            terminal_id = f"term_{workspace_id}_{number}_{index}"
            snapshot["panes"].append({**pane_template, "workspace_id": workspace_id, "tab_id": tab_id,
                                     "pane_id": pane_id, "terminal_id": terminal_id, "focused": focused,
                                     "agent_status": status})
            name = "impl" if focused else f"worker-{workspace_id}-{number}-{index}"
            snapshot["agents"].append({**agent_template, "workspace_id": workspace_id, "tab_id": tab_id,
                                      "pane_id": pane_id, "terminal_id": terminal_id,
                                      "name": name, "focused": focused, "agent_status": status})
    for workspace_id in pane_numbers:
        tabs = [tab for tab in snapshot["tabs"] if tab["workspace_id"] == workspace_id]
        snapshot["workspaces"].append({**workspace_template, "workspace_id": workspace_id,
                                       "number": len(snapshot["workspaces"]) + 1, "label": "Project",
                                       "focused": workspace_id == "w1", "pane_count": pane_numbers[workspace_id],
                                       "tab_count": len(tabs), "active_tab_id": tabs[0]["tab_id"]})
    state_path.write_text(json.dumps(state))
    notes_path = root / "state" / "workstreams.json"
    notes = notes_path.read_bytes()
    remembered = structured(client.tool("workstream_list"))["workstreams"]

    def observe(expected):
        before = json.loads(state_path.read_text())
        inventories = []
        for surface in ["session_snapshot", "workstream_overview"]:
            result = structured(client.tool(surface, {"session": "default"}))
            if surface == "session_snapshot":
                inventory = result["snapshot"]
            else:
                assert result["session_error"] is None
                assert [report["record"] for report in result["workstreams"]] == remembered
                inventory = result["live_inventory"]
            assert inventory["tabs"] == expected["tabs"]
            for field in ["version", "protocol", "focused_workspace_id", "focused_tab_id", "focused_pane_id"]:
                assert inventory[field] == expected[field]
            tabs = {tab["tab_id"]: tab for tab in inventory["tabs"]}
            panes = {pane["pane_id"]: pane for pane in inventory["panes"]}
            for tab in inventory["tabs"]:
                assert tab["pane_count"] == sum(pane["tab_id"] == tab["tab_id"] for pane in inventory["panes"])
                assert any(workspace["workspace_id"] == tab["workspace_id"] for workspace in inventory["workspaces"])
            for pane in panes.values():
                assert tabs[pane["tab_id"]]["workspace_id"] == pane["workspace_id"]
            for agent in inventory["agents"]:
                pane = panes[agent["pane_id"]]
                assert pane["terminal_id"] == agent["terminal_id"]
                assert tabs[pane["tab_id"]]["agent_status"] == agent["agent_status"]
            inventories.append(inventory)
        assert inventories[0] == inventories[1]
        after = json.loads(state_path.read_text())
        assert after["snapshot"] == expected
        assert after["calls"] == before["calls"] + [["--session=default", "api", "snapshot"]] * 2
        assert notes_path.read_bytes() == notes
        return inventories[0]

    inventory = observe(snapshot)
    impl = next(agent for agent in inventory["agents"] if agent["name"] == "impl")
    pane = next(pane for pane in inventory["panes"] if pane["pane_id"] == impl["pane_id"])
    assert next(tab for tab in inventory["tabs"] if tab["tab_id"] == pane["tab_id"])["label"] == "impl → inventory"
    assert [tab["tab_id"] for tab in inventory["tabs"] if tab["label"] == "Review"] == ["w1:t2", "w2:t1"]
    renamed = "Inventory review α"
    structured(client.tool("tab_rename", {"session": "default", "tab_id": "w1:t1", "label": renamed}))
    snapshot["tabs"][0]["label"] = renamed
    inventory = observe(snapshot)
    assert next(agent for agent in inventory["agents"] if agent["name"] == "impl") == impl
    assert inventory["tabs"][0]["label"] == renamed

    corruptions = []
    missing = copy.deepcopy(snapshot)
    del missing["tabs"]
    corruptions.append(missing)
    for tabs in [None, {}, "tabs", [None]]:
        malformed = copy.deepcopy(snapshot)
        malformed["tabs"] = tabs
        corruptions.append(malformed)
    for field in ["tab_id", "workspace_id", "number", "focused", "pane_count", "agent_status"]:
        malformed = copy.deepcopy(snapshot)
        del malformed["tabs"][0][field]
        corruptions.append(malformed)
    for malformed in corruptions:
        current = json.loads(state_path.read_text())
        current["snapshot"] = malformed
        state_path.write_text(json.dumps(current))
        result = client.tool("session_snapshot", {"session": "default"})
        assert result.get("isError") and "invalid herdr snapshot response" in result["structuredContent"]["error"]
        overview = structured(client.tool("workstream_overview", {"session": "default"}))
        assert "invalid herdr snapshot response" in overview["session_error"]
        assert overview["live_inventory"] is None
        assert [report["record"] for report in overview["workstreams"]] == remembered
        assert overview["workstreams"][0]["locations"][0]["observation"] == "session_unavailable"
        after = json.loads(state_path.read_text())
        assert after["snapshot"] == malformed
        assert after["calls"] == current["calls"] + [["--session=default", "api", "snapshot"]] * 2
        assert notes_path.read_bytes() == notes
    current = json.loads(state_path.read_text())
    current["snapshot"] = snapshot
    state_path.write_text(json.dumps(current))
    assert structured(client.tool("workstream_list"))["workstreams"] == remembered


def check_tab_close(binary, tool):
    schema = tool["inputSchema"]
    assert set(schema["required"]) == {"session", "tab_id"}
    assert schema["additionalProperties"] is False
    assert all(schema["properties"][field]["type"] == "string" for field in ["session", "tab_id"])
    annotations = tool["annotations"]
    assert annotations["readOnlyHint"] is False and annotations["destructiveHint"] is True
    assert annotations["idempotentHint"] is False
    snapshot = json.loads((Path(__file__).parent / "fixtures" / "herdr-0.9.5-snapshot.json").read_text())["result"]["snapshot"]
    workspace, tab, pane = snapshot["workspaces"][0], snapshot["tabs"][0], snapshot["panes"][0]
    workspace.update(tab_count=2, pane_count=2)
    snapshot["tabs"].append({**tab, "tab_id": "w1:t2", "number": 2, "focused": False})
    snapshot["panes"].append({**pane, "pane_id": "w1:p2", "terminal_id": "term_worker", "tab_id": "w1:t2", "focused": False})
    snapshot["agents"].append({**snapshot["agents"][0], "pane_id": "w1:p2", "terminal_id": "term_worker", "tab_id": "w1:t2", "focused": False})
    snapshot["workspaces"].append({**workspace, "workspace_id": "w2", "active_tab_id": "w2:t1", "tab_count": 1, "pane_count": 1, "focused": False})
    snapshot["tabs"].append({**tab, "workspace_id": "w2", "tab_id": "w2:t1", "focused": False})
    snapshot["panes"].append({**pane, "workspace_id": "w2", "tab_id": "w2:t1", "pane_id": "w2:p1", "terminal_id": "term_other", "focused": False})
    with tempfile.TemporaryDirectory(prefix="heeler-close-", dir="/tmp") as directory:
        root = Path(directory)
        state_path = root / "native.json"
        fake = root / "herdr"
        fake.write_text(f"#!{sys.executable}\n" + (Path(__file__).parent / "fake_herdr.py").read_text())
        fake.chmod(0o700)

        def reset(**fields):
            state_path.write_text(json.dumps({"snapshot": snapshot, **fields}))

        def state():
            return json.loads(state_path.read_text())

        def close(tab_id="w1:t2", session="default"):
            return client.tool("tab_close", {"session": session, "tab_id": tab_id})

        def expect_error(result, text):
            assert result.get("isError") and text in result["structuredContent"]["error"], result

        reset()
        client = Client(binary, root)
        try:
            for target in ["w1:t2", "w1:t1"]:
                reset()
                assert structured(close(target)) == {"session": "default", "tab_id": target, "acknowledged": True}
                after = state()
                assert after["calls"] == [["--session=default", "tab", "get", target],
                                          ["--session=default", "workspace", "get", "w1"],
                                          ["--session=default", "tab", "close", target]]
                assert [t["tab_id"] for t in after["snapshot"]["tabs"]] == [t["tab_id"] for t in snapshot["tabs"] if t["tab_id"] != target]
                removed = {p["pane_id"] for p in snapshot["panes"] if p["tab_id"] == target}
                assert {p["pane_id"] for p in after["snapshot"]["panes"]} == {p["pane_id"] for p in snapshot["panes"]} - removed
                assert {a["pane_id"] for a in after["snapshot"]["agents"]} == {a["pane_id"] for a in snapshot["agents"]} - removed
                assert len(after["snapshot"]["workspaces"]) == 2
                for collection in ["workspaces", "tabs", "panes"]:
                    assert [item for item in after["snapshot"][collection] if item["workspace_id"] == "w2"] == [item for item in snapshot[collection] if item["workspace_id"] == "w2"]
                expected_focus = "w1:t1" if target == "w1:t2" else "w1:t2"
                assert after["snapshot"]["focused_tab_id"] == expected_focus
                assert after["snapshot"]["focused_workspace_id"] == "w1"

            reset()
            for arguments in [{"tab_id": "w1:t2"}, {"session": "default"},
                              {"session": "default", "tab_id": "w1:t2", "force": True}]:
                result = client.tool("tab_close", arguments)
                assert result.get("isError"), result
                assert "failed to deserialize parameters" in result["content"][0]["text"], result
            for target in ["", "--help", "--", "w1", "w1:p1", "w1:t", "w1:t1 --force", "w1:t1;echo", "w1:t1:t2", "w1:t" + "a" * 33]:
                expect_error(close(target), "ID")
            for session in ["", "../bad", "ssh:host", "work --remote=host", "a" * 65]:
                expect_error(close(session=session), "session")
            assert state().get("calls", []) == []
            expect_error(close(session="work.test"), "explicit default local session")
            assert state()["calls"] == [["--session=work.test", "tab", "get", "w1:t2"]]
            reset()
            expect_error(close("w1:t99"), "tab not found")
            assert len(state()["calls"]) == 1
            reset()
            expect_error(close("w2:t1"), "last-tab")
            assert len(state()["calls"]) == 2 and state()["snapshot"] == snapshot
            for fields, text, removed in [
                ({"close_failure": True}, "native close denied", False),
                ({"close_error": {"code": "confirmation_required", "message": "human input required"}}, "confirmation_required", False),
                ({"close_response": {"result": {"type": "tab_closed"}}}, "invalid close acknowledgement", True),
            ]:
                reset(**fields)
                result = close()
                expect_error(result, text)
                assert "inspect before retrying" in result["structuredContent"]["error"]
                assert len(state()["calls"]) == 3
                assert (not any(t["tab_id"] == "w1:t2" for t in state()["snapshot"]["tabs"])) == removed
                if not removed:
                    assert state()["snapshot"] == snapshot

            for operation, calls in [("tab_get", 1), ("workspace_get", 2), ("tab_close", 3)]:
                with socket.socket(socket.AF_UNIX) as listener:
                    listener.settimeout(3)
                    listener.bind(str(root / f"{operation}.sock"))
                    listener.listen(1)
                    reset(dispatch_gate={"command": operation, "socket": f"{operation}.sock"})
                    client.counter += 1
                    request_id = client.counter
                    client.send({"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": {
                        "name": "tab_close", "arguments": {"session": "default", "tab_id": "w1:t2"},
                    }})
                    connection, _ = listener.accept()
                    with connection:
                        connection.settimeout(2)
                        assert connection.recv(1) == b"x"
                        before_cancel = state()
                        client.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": request_id}})
                        assert client.call("ping", {}, timeout=2) == {}
                        # Gated-child EOF proves the future was dropped, not just an early snapshot.
                        assert connection.recv(1) == b""
                    assert state() == before_cancel and len(state()["calls"]) == calls
                    if operation != "tab_close":
                        assert state()["snapshot"] == snapshot
                    else:
                        assert not any(t["tab_id"] == "w1:t2" for t in state()["snapshot"]["tabs"])
        finally:
            client.close()


def main():
    binary = str(Path(sys.argv[1]).resolve())
    check_packaging(binary)
    with tempfile.TemporaryDirectory(prefix="heeler-smoke-") as directory:
        root = Path(directory)
        fake = root / "herdr"
        fixture = Path(__file__).parent / "fixtures" / "herdr-0.9.5-snapshot.json"
        response = json.loads(fixture.read_text())
        (root / "native.json").write_text(json.dumps({"snapshot": response["result"]["snapshot"]}))
        fake.write_text(f"#!{sys.executable}\n" + (Path(__file__).parent / "fake_herdr.py").read_text())
        fake.chmod(0o700)
        client = Client(binary, root)
        try:
            tools = {tool["name"]: tool for tool in client.call("tools/list", {})["tools"]}
            assert set(tools) == {
                "caller_context", "session_list", "session_snapshot", "workstream_list",
                "workstream_save", "workstream_overview", "workstream_get", "workstream_update", "location_open", "agent_inspect",
                "agent_start", "assignment_send", "assignment_outcome", "agent_prompt", "agent_move", "agent_read", "agent_wait", "agent_focus",
                "workspace_rename", "tab_rename", "tab_close",
            }
            assert tools["session_snapshot"]["annotations"]["readOnlyHint"]
            check_renames(client, root, tools)
            assert structured(client.tool("caller_context"))["launch_context"]["HERDR_PANE_ID"] == "w9:p1"
            assert structured(client.tool("session_list"))["sessions"][0]["running"]
            inventory = structured(client.tool("session_snapshot", {"session": "default"}))
            assert inventory["snapshot"]["version"] == "0.9.5"
            assert inventory["snapshot"]["panes"][0]["terminal_id"] == "term_test"
            work = {"id": "release", "title": "Release", "purpose": "Ship notes", "status": "planned", "next_action": "Draft notes", "blocker": None, "locations": [{"session": "default", "workspace_id": "w1", "pane_id": "w1:p1"}]}
            saved = structured(client.tool("workstream_save", {"workstream": work, "expected_revision": 0}))
            assert saved["record"]["revision"] == 1
            assert client.tool("workstream_save", {"workstream": work, "expected_revision": 0})["isError"]
            overview = structured(client.tool("workstream_overview", {"session": "default"}))
            assert overview["session_error"] is None
            assert overview["live_inventory"]["version"] == "0.9.5"
            observed = overview["workstreams"][0]
            assert observed["record"]["workstream"]["status"] == "planned"
            assert observed["locations"][0]["agent"]["agent_status"] == "blocked"
            assert tools["workstream_overview"]["annotations"]["readOnlyHint"]
            check_named_inventory(client, root)
            unavailable = structured(client.tool("workstream_overview", {"session": "stopped"}))
            assert unavailable["session_error"]
            assert client.tool("session_snapshot", {"session": "../bad"})["isError"]
        finally:
            client.close()
        client = Client(binary, root)
        try:
            assert structured(client.tool("workstream_list"))["workstreams"][0]["workstream"]["id"] == "release"
        finally:
            client.close()
        check_lock_contention(binary, root)
        client = Client(binary, root)
        try:
            check_memory(client, root, tools, binary)
            check_outcomes(client, root, tools, binary)
        finally:
            client.close()
    check_tab_close(binary, tools["tab_close"])
    check_prompts(binary, tools["agent_prompt"])
    check_moves(binary, tools["agent_move"])
    print("stdio smoke: discovery, renames/validation/native failures, tab close/isolation/cancellation, named tab inventory/joins/errors, notes/conflicts, overview, restart, lock contention, and shutdown passed")


if __name__ == "__main__":
    main()
