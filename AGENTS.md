# Working on Heeler

Heeler helps a human keep roughly five or six local workstreams straight:
what exists, what has started, what needs input, and what deserves attention next.
The interface is an ordinary coding agent running inside herdr. MCP is a tool
interface, not the product. The implementation is Rust.

## Boundaries

- No remote-machine access, autonomous scheduling, agent election, or workflow engine.
  These are product non-goals, not deferred features.
- Reuse herdr's sessions, workspaces, tabs, panes, views, and restoration rather
  than inventing parallel terminal organization.
- The lead role is instructions, not a special process. Its portable definition
  is `roles/lead/AGENTS.md`; do not adopt that role just because you are developing here.
- Prefer diagnostics and human judgment over singleton enforcement or self-protection.
- Keep intent separate from observations. A pane existing doesn't prove work started;
  an agent being idle doesn't prove a task succeeded.
- Never change real panes, focus, sessions, integrations, or agent configuration
  merely to test the project. Use fixtures/fake executables and temporary state.
- Don't read private pane contents or probe SSH hosts implicitly.
- Keep personal checkout paths, hostnames, and private project references out of
  source, docs, and fixtures. Use generic, synthetic examples.

## Style

Use plain domain structs and enums, small modules, and direct control flow.
Keep descriptive tests beside the code. Comments should explain a constraint or
non-obvious choice, not narrate the implementation. Keep the README short while
the project is small; don't turn it into an architecture pitch.

## Structure

- `src/model.rs`: durable workstream intent and native location references.
- `src/store.rs`: versioned JSON records, locking, and atomic replacement.
- `src/herdr.rs` and `src/herdr/`: bounded, explicit local CLI operations; no shell
  interpolation or remote routing.
- `src/assignment.rs`: handoff briefs and recorded submission attempts, not a job engine.
- `src/overview.rs`: factual reconciliation, not autonomous prioritization.
- `src/mcp.rs`: stdio tool adapter over the core.
- `roles/lead/AGENTS.md`: harness-independent lead behavior.

## Contracts

- Herdr 0.9.5 snapshot responses contain `result.type = session_snapshot` and
  `result.snapshot`. Keep `tests/fixtures/` faithful to upstream, not the parser.
- Native leaves use `--flag value` and put agent targets first. Legacy parsers
  reject `--flag=value` and bare `--` on get/prompt/read/wait/focus. Only agent
  start uses `--` to separate the launched agent's own arguments.
- Workstream saves replace a complete record and require its latest revision
  (`0` for create-only). Never reset corrupt or unsupported state implicitly.
- Readers accept state versions 1 and 2; saves write version 2. Old workstreams
  acquire empty assignment lists without modifying their files until a save.
- Dispatch records an uncertain attempt before sending. Submission acknowledgement
  isn't completion. A failed final note update must not turn an acknowledged send
  into an error that invites an automatic retry.
- Unix saves sync file contents and the replacement directory; new state directories
  sync their parents too. Other platforms currently provide atomic replacement only.
  A directory-sync failure may occur after replacement: reread before retrying.
- Store lock acquisition retries for up to one second before returning busy;
  ordinary CLI reads have a five-second timeout. Native launch/wait calls are
  explicitly bounded, with transport overhead. Neither is unattended monitoring.
- Overview observations are typed and must not modify remembered workstream intent.

## Verification

Run `cargo fmt --check`, `cargo test --locked`, and
`cargo clippy --locked --all-targets -- -D warnings`.
After `cargo build --locked`, run `python3 tests/stdio_smoke.py target/debug/heeler`
and `python3 tests/delegation_smoke.py target/debug/heeler`.
Keep `Cargo.lock` checked in. Never log diagnostic prose to MCP stdout.
