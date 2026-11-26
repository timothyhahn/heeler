"""Shared stdio MCP test client; no scenario imports or live harness configuration."""
import json
import os
import selectors
import subprocess
import tempfile
import time


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
