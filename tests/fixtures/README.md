# Herdr contract fixture

`herdr-0.9.5-snapshot.json` is synthetic data, not a captured user session.
It follows the protocol-22 success-response schema bundled with herdr 0.9.5:
`result.type` is `session_snapshot`, and the inventory is in `result.snapshot`.

Reference: herdr commit `5bd900d88c80d95f5b145397e45bbeea398b8165`,
`src/cli/api.rs`, `src/api/schema/response.rs`, and
`docs/next/api/herdr-api.schema.json`. Inspect an installed binary's bundled
contract without contacting its server using `herdr api schema --json`.

Rust adapter tests and the stdio test share this fixture. Change the fixture only
when the upstream contract changes, not merely to accommodate Heeler's parser.

`../fake_herdr.py` adds stateful synthetic create/start/prompt/read/wait/focus
responses for the handoff test. Creation returns `workspace_created` or
`tab_created` with `root_pane`, or `pane_info` with `pane`. Start returns
`agent_started`, prompt returns `agent_prompted`, and get/wait/focus return
`agent_info`. Agent reads are plain CLI text, not JSON. These shapes were checked
against the bundled schema and pinned `src/app/api/agents.rs` source above.
The fake's Codex startup uses the native-supported `unknown` status with
`interactive_ready: true` and no pending launch; see pinned `src/cli/agent.rs`
(`wait_for_named_agent`). Readiness refusals are exercised at both send checks.

`heeler-v1-location-hints.json` was saved by Heeler HEAD `f3a7c0a` using only
synthetic notes. It contains retired workspace hints, long/empty pane suffixes,
and the old 128-byte workspace / 160-byte pane boundaries (including UTF-8).
Reads must not rewrite it; later v2 saves must preserve its hints. These are
remembered references, not valid targets for strict live native operations.
