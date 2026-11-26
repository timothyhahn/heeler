# Heeler

A small Rust management layer for [herdr](https://github.com/herdrdev/herdr).
An ordinary coding agent helps a human keep a few local workstreams straight.
MCP is the tool interface, not a scheduler or special lead process. Live management
uses Heeler MCP; missing operations are flagged, never bypassed via CLI.

## Run without a checkout

A maintainer can produce a **host-native local archive** as described below.
No published downloads or cross-platform support matrix are promised yet.
Extract a trusted matching archive into a stable directory you choose. Users need
compatible native herdr and a full-agent MCP harness, not Rust, Cargo, Python or
the source checkout. Fixtures target herdr 0.9.5; OS/linkage compatibility needs
verification on each intended host.

Use the extracted executable's absolute path. Illustrative stdio MCP fields
(adapt the enclosing schema to your harness):

```json
{"command":"/opt/heeler/bin/heeler","args":["--herdr-bin","/opt/herdr/bin/herdr"]}
```

With no subcommand, Heeler starts stdio MCP. `--herdr-bin` / `HEELER_HERDR_BIN`
defaults to `herdr` on PATH. `--state-dir` / `HEELER_STATE_DIR` defaults to the OS
local data directory plus `heeler`; explicit flags take precedence over env.
Choose absolute paths yourself; no shell or `~` expansion is performed.
On Unix, each Store access rechecks resolved ancestor-directory barriers, including
retries/concurrent initialization; existing directories alone do not prove durability.
Synchronization errors are reported, never repaired by deleting state.

Configuration is harness/cwd/scope dependent. Deliberately select and configure
that scope; Heeler never edits configuration, trust or permissions. For explicitly
authorized worker publication, configure **each** participant to the same store
(e.g. append `--state-dir` and `/home/example/.local/share/heeler` consistently).
Parent MCP availability does not provision workers; `agent_start` does not either.

## Instructions are opt-in

Connecting to MCP or discovering assets does **not** adopt the lead role.
Only when the human explicitly selects a lead, read the shipped
`roles/lead/AGENTS.md` or print it using the configured absolute binary path:

```sh
/opt/heeler/bin/heeler export lead
/opt/heeler/bin/heeler export skill
```

These static stdout exports (also discoverable through `--help`) start no MCP,
access no Store/herdr and write no files. Deliberate shell redirection is up to
the user. The archive's `skills/heeler-lead/SKILL.md` is standalone: copy that
complete file only into a human-selected, supported manual-only skill scope.
The source wrapper formerly at that path was retired; the source
`assets/heeler-lead-skill-header.md` is a **template**, not an installable skill.
Exports combine that header with the one canonical role body at build time.
`disable-model-invocation: true` is a Pi-specific gate, not a universal Agent
Skills guarantee. Other harnesses must support manual-only loading or leave the
skill unregistered. Do not add the role to worker defaults automatically.

## Deliberate coordination, not automation

Compact list/overview reads show intent alongside observations; use focused
`workstream_get` / revision-checked `workstream_update` for details/bookkeeping.
Saved submitted/uncertain assignments must never be blindly resent. All live
prompts, including rendered assignments, share the 32 KiB LF/tab-only control
policy; historical stored text keeps its stable field bounds and is not rewritten.
Create/rename/move labels share a control-free, non-leading-dash 256-byte limit.
`assignment_outcome` reads one **unauthenticated, unverified** stored claim once
or waits at most 60 seconds. It reads no reports/panes and promises no parent
wake or restored background wait; verify evidence separately.

Settled `agent_prompt` acknowledgement is submission, not success. Native global
parsing consumes exact TEXT `--session`, `--remote`, `--remote-keybindings` and
those names plus `=...`; rephrase them. Busy steering, queued follow-up and
turn-abort/stop are unsupported. Request cancellation drops unsent read/wait/focus
steps, not the worker; it cannot undo issued input/actions. Newly observed
conversation references are pinned across checks where available, not atomically;
caller-None acknowledgement and stored historical targets remain unverified.
`agent_move` preserves the terminal but can remove empty source containers;
readbacks/identity checks are not atomic. `tab_close` requires explicit inspection
and human authorization, irreversibly terminates contents and refuses observed
last tabs. Focus may necessarily change with moves/close. No raw-key/process
fallback, automatic retries, approvals, remote control or scheduling. No 20-tab
scale or original-lead restart proof is claimed.

## Maintainer build and local archive

Maintainers need Rust/Cargo plus a native linker; Python 3 (stdlib only) is used
for archive construction/checks, not by installed users. With cached dependencies:

```sh
cargo fmt --check
cargo test --offline --locked
cargo clippy --offline --locked --all-targets -- -D warnings
cargo build --offline --locked
python3 tests/stdio_smoke.py target/debug/heeler
python3 tests/delegation_smoke.py target/debug/heeler
RUSTFLAGS="--remap-path-prefix=$(pwd)=/build/heeler --remap-path-prefix=$HOME=/build/home" cargo build --offline --release --locked
mkdir -p target/dist
python3 scripts/package.py --binary target/release/heeler --host "$(rustc -vV | awk '/^host:/ {print $2}')" --output-dir target/dist
```

The builder executes the explicitly selected **trusted** binary for version and
exports; it does not build, download, install or release anything. It packages
only binary, LICENSE, this README, canonical lead and generated skill, plus
`SHA256SUMS`, under `heeler-<version>-<host>/`. Executable mode is 0755, assets
0644; a separate `.tar.gz.sha256` covers the archive. Existing outputs are refused,
not overwritten. Publication is not an atomic archive/digest pair: failures may
leave partial or complete public outputs, which are never rolled back/deleted.
Inspect them manually before recovery; do not retry automatically. Only private
temporary handles are cleaned up. Checksums detect corruption, not authenticity.
The host label is supplied by the maintainer, not verified cross-target identity.
Inspect the actual binary/linkage and archive for private build paths before any
distribution; path-remapping alone is not proof. No static-link/minimum-OS,
all-harness discovery, release/signing/upload or installation guarantee is made.

Smoke checks use temporary state and fake executables, never live panes; they
require macOS or Linux. Packaging/static checks are integrated in stdio smoke
(and can run as `python3 tests/packaging_smoke.py /absolute/path/to/heeler`).
Source independence is embedded-byte audit plus isolated extracted/cwd tests,
not a filesystem sandbox. Three independent reviews precede any live trial.

[MIT licensed](LICENSE).
