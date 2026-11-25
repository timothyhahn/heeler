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
