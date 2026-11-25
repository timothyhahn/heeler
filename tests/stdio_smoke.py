#!/usr/bin/env python3
"""End-to-end stdio MCP check using only temporary state and a fake herdr."""
import copy
import json
import os
from pathlib import Path
import selectors
import subprocess
import sys
import tempfile
import time


class Client:
    def __init__(self, binary, root):
        env = {k: v for k, v in os.environ.items() if not k.startswith("HERDR_")}
        env.update(HERDR_ENV="1", HERDR_SESSION="default", HERDR_PANE_ID="w9:p1")
        self.stderr = tempfile.TemporaryFile()
        self.process = subprocess.Popen(
            [binary, "--state-dir", str(root / "state"), "--herdr-bin", str(root / "herdr")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.stderr, env=env,
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


def main():
    binary = str(Path(sys.argv[1]).resolve())
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
                "workstream_save", "workstream_overview", "location_open", "agent_inspect",
                "agent_start", "assignment_send", "agent_read", "agent_wait", "agent_focus",
                "workspace_rename", "tab_rename",
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
    print("stdio smoke: discovery, renames/validation/native failures, snapshots, notes/conflicts, overview, restart, lock contention, and shutdown passed")


if __name__ == "__main__":
    main()
