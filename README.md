# Heeler

A small Rust management layer for [herdr](https://github.com/herdrdev/herdr).
Your coding agent acts as lead, using MCP tools to keep track of what you've
started, where it lives, and what needs attention.

Still early. It keeps workstream notes, inspects local sessions, and hands saved
implementation or review assignments to agents in explicit panes. The lead can
reuse agents or open a location, rename workspace/tab labels without changing
focus, start an agent, read/wait, and focus it. Live management uses Heeler MCP;
missing operations are flagged, not bypassed via CLI. There are no automatic
retries, permission approvals, or background scheduling.

Use compact workstream list/overview reads for orientation, `workstream_get` for
one full history, and `workstream_update` for revision-checked notes, new prepared
assignments, or outcomes. Full reads and whole-record saves remain available.
`assignment_outcome` reads a stored untrusted claim once or waits up to 60 seconds;
it does not verify success, inspect report files, schedule work or promise a wake.
Worker publication uses existing updates only when explicitly authorized and
already configured to the same store, never overriding task constraints.
`agent_prompt` sends a one-shot ordinary message to an inspected settled worker,
without changing notes; acknowledgement is terminal submission, not success.
TEXT equal to `--session`, `--remote`, `--remote-keybindings`, or starting with
one of those names plus `=`, must be rephrased: native global parsing consumes it.
Steering, queued delivery and turn-abort/stop are unsupported.
`agent_move` transfers an inspected worker into a new labeled tab in an existing
workspace. It returns a checked new target without rewriting notes; native empty
source containers may disappear and focus can change despite no-focus intent.

`tab_close` irreversibly terminates one explicitly inspected, human-authorized
tab. It refuses observed last tabs to avoid workspace closure, but a concurrent
topology change can still make it a last tab. Focus may change; cancellation
cannot undo an issued close.

## Try it

```sh
cargo build --locked
```

Register `target/debug/heeler` with your agent harness as a local stdio MCP
server, using the binary's absolute path. Herdr must be on `PATH`, or supplied
with `--herdr-bin`. See `--help` for configuration options.

Inside herdr, ask your agent:

> Read `<checkout>/roles/lead/AGENTS.md` and adopt the lead role.

The same file can back a named agent or the skill in `skills/heeler-lead`.
Start by asking what's running and recording the workstreams you care about.

## Development

```sh
cargo fmt --check
cargo test --locked
cargo clippy --locked --all-targets -- -D warnings
cargo build --locked
python3 tests/stdio_smoke.py target/debug/heeler
python3 tests/delegation_smoke.py target/debug/heeler
```

The fixtures target herdr 0.9.5. Checks use temporary state and fake executables,
not live panes. The smoke tests require macOS or Linux. The next live trial is a
small implementation/review handoff inside herdr, with explicit permission to
create the test panes; the fixture loop isn't proof of live integration.

[MIT licensed](LICENSE).
