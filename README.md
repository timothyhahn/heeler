# Heeler

A small Rust management layer for [herdr](https://github.com/herdrdev/herdr).
Your coding agent acts as lead, using MCP tools to keep track of what you've
started, where it lives, and what needs attention.

Still early. It can keep workstream notes, inspect local sessions, and compare
the two. It can't open, focus, prompt, or close panes yet.

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
```

The fixtures target herdr 0.9.5. Checks use temporary state and fake executables,
not live panes. The stdio smoke test currently requires macOS or Linux.

[MIT licensed](LICENSE).
