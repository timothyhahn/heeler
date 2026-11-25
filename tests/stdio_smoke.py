#!/usr/bin/env python3
"""End-to-end stdio MCP check using only temporary state and a fake herdr."""
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


def main():
    binary = str(Path(sys.argv[1]).resolve())
    with tempfile.TemporaryDirectory(prefix="heeler-smoke-") as directory:
        root = Path(directory)
        fake = root / "herdr"
        fixture = Path(__file__).parent / "fixtures" / "herdr-0.9.5-snapshot.json"
        response = json.loads(fixture.read_text())
        fake.write_text(
            f"#!{sys.executable}\nimport json, sys\n"
            f"response = {response!r}\n"
            "args = sys.argv[1:]\n"
            "if args == ['session', 'list', '--json']:\n"
            "    print(json.dumps({'sessions': [{'name': 'default', 'default': True, 'running': True}]}))\n"
            "elif args == ['--session=default', 'api', 'snapshot']:\n"
            "    print(json.dumps(response))\n"
            "else:\n"
            "    print('No such local session', file=sys.stderr); sys.exit(1)\n"
        )
        fake.chmod(0o700)
        client = Client(binary, root)
        try:
            tools = {tool["name"]: tool for tool in client.call("tools/list", {})["tools"]}
            assert set(tools) == {"caller_context", "session_list", "session_snapshot", "workstream_list", "workstream_save", "workstream_overview"}
            assert tools["session_snapshot"]["annotations"]["readOnlyHint"]
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
    print("stdio smoke: discovery, snapshots, notes/conflicts, overview, restart, lock contention, and shutdown passed")


if __name__ == "__main__":
    main()
