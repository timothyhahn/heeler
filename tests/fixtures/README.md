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

Tab close uses `TabTarget { tab_id }` (`tab.close`) and returns only
`result: {"type":"ok"}`, not a `tab_closed` metadata envelope. The CLI mapping is
in pinned `src/cli/tab_bindings.rs`; success/error and last-tab behavior are in
`src/app/api/tabs.rs::handle_tab_close`. Native last-tab close can close the
workspace or refuse restoration/worktree-group closure. Heeler reads explicit
`tab get` / `workspace get` metadata and refuses observed last tabs; this is not
atomic against other clients changing topology. The strict fake exercises those
preflights, native acknowledgements/refusals, focus effects and cancellation.

Ordinary prompt contracts are grounded in the same pin's `src/app/api/agents.rs`,
`src/app/api_helpers.rs` and `src/cli/agent/bindings_tests.rs`: literal text plus
Enter, pre-submission AgentInfo, and `agent_prompted`, not a logical turn receipt.
The fake permits native working/unknown input independently of Heeler's settled
policy. Assignment smoke roots explicitly require saved uncertain attempts before
input; plain-message roots do not invent that native prerequisite. Faults/gates
exercise post-issue uncertainty and cancellation, never real agent input.

Outer native parsing runs before leaf TEXT protection (`src/main.rs:527–542`,
`src/session.rs:59–84`, `src/remote/args.rs:48–119` at the same pin). Exact
`--session`/`--remote`/`--remote-keybindings` and their equals forms are consumed
or rejected globally, so the text-only MCP entry refuses them before any native
call and asks for rephrasing. The fake independently scans whole argv first,
records effective routing, then parses the leaf; it does not copy that rejection
rule. With no remote target, native `--handoff` is removed and reappended, so it
remains literal TEXT in this target-first, text-last command. Safe and near-match
operands are tested without automatic whitespace prefixes or other rewriting.

`herdr-0.9.5-pane-move.json` is a synthetic full native `pane_move.move_result`
envelope, grounded in pinned `src/api/schema/panes.rs:592–621`,
`src/api/schema/response.rs:129–131` and `src/app/api/panes.rs:947–1366`.
It models an existing pane transferred to a new tab, preserving its terminal.
The strict fake models native public numbering/aliases, source-container removal,
zoomed-tab no-op and default focus=true separately from Heeler's fixed no-focus
and pending-launch refusal. Layout geometry is synthetic; movement identity,
container fields and target-layout (not global) focus follow the native shape.
Fresh pane/tab/agent readbacks, faults and cancellation use only temporary state.
