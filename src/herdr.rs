use crate::model::validate_session;
use anyhow::{Context, Result, bail, ensure};
use serde::{Deserialize, Serialize};
use std::path::PathBuf;
use std::process::Stdio;
use std::time::Duration;
use tokio::io::AsyncReadExt;
use tokio::process::Command;

const MAX_OUTPUT: u64 = 4 * 1024 * 1024;

#[derive(Debug, Clone)]
pub struct Herdr {
    binary: PathBuf,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Session {
    pub name: String,
    pub default: bool,
    pub running: bool,
    #[serde(default)]
    pub connection_error: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Workspace {
    pub workspace_id: String,
    pub label: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Pane {
    pub pane_id: String,
    pub terminal_id: String,
    pub workspace_id: String,
    pub tab_id: String,
    pub cwd: Option<String>,
    pub label: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Agent {
    pub pane_id: String,
    pub terminal_id: String,
    pub name: Option<String>,
    pub agent: Option<String>,
    pub agent_status: String,
    #[serde(default)]
    pub completion_seq: Option<u64>,
    #[serde(default)]
    pub agent_session: Option<serde_json::Value>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Snapshot {
    pub version: String,
    pub protocol: u32,
    pub focused_workspace_id: Option<String>,
    pub focused_tab_id: Option<String>,
    pub focused_pane_id: Option<String>,
    pub workspaces: Vec<Workspace>,
    pub panes: Vec<Pane>,
    pub agents: Vec<Agent>,
}

impl Herdr {
    pub fn new(binary: PathBuf) -> Self {
        Self { binary }
    }

    async fn json(&self, args: &[String]) -> Result<serde_json::Value> {
        let mut child = Command::new(&self.binary)
            .args(args)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .kill_on_drop(true)
            .spawn()
            .with_context(|| format!("could not run {}", self.binary.display()))?;
        let mut stdout = child
            .stdout
            .take()
            .context("missing stdout")?
            .take(MAX_OUTPUT + 1);
        let mut stderr = child
            .stderr
            .take()
            .context("missing stderr")?
            .take(MAX_OUTPUT + 1);
        let mut out = Vec::new();
        let mut err = Vec::new();
        let (status, _, _) = tokio::time::timeout(Duration::from_secs(5), async {
            tokio::try_join!(
                child.wait(),
                stdout.read_to_end(&mut out),
                stderr.read_to_end(&mut err)
            )
        })
        .await
        .context("herdr read timed out; session state is unavailable")??;
        ensure!(
            out.len() as u64 <= MAX_OUTPUT && err.len() as u64 <= MAX_OUTPUT,
            "herdr output exceeded limit"
        );
        if !status.success() {
            bail!(
                "herdr read failed: {}",
                String::from_utf8_lossy(&err)
                    .chars()
                    .take(2000)
                    .collect::<String>()
            );
        }
        let value: serde_json::Value =
            serde_json::from_slice(&out).context("herdr returned invalid JSON")?;
        if let Some(error) = value.get("error") {
            bail!("herdr API error: {error}");
        }
        Ok(value)
    }

    pub async fn sessions(&self) -> Result<Vec<Session>> {
        let value = self
            .json(&["session".into(), "list".into(), "--json".into()])
            .await?;
        serde_json::from_value(value.get("sessions").context("missing sessions")?.clone())
            .context("invalid herdr session list")
    }

    pub async fn snapshot(&self, session: &str) -> Result<Snapshot> {
        validate_session(session)?;
        let value = self
            .json(&[
                format!("--session={session}"),
                "api".into(),
                "snapshot".into(),
            ])
            .await?;
        decode_snapshot(value)
    }
}

/// Diagnostic launch context only: no terminal reads and no inferred singleton role.
pub fn caller_context() -> serde_json::Value {
    let keys = [
        "HERDR_ENV",
        "HERDR_SESSION",
        "HERDR_SOCKET_PATH",
        "HERDR_WORKSPACE_ID",
        "HERDR_TAB_ID",
        "HERDR_PANE_ID",
    ];
    let values: serde_json::Map<_, _> = keys
        .into_iter()
        .filter_map(|key| {
            std::env::var(key)
                .ok()
                .map(|value| (key.into(), value.into()))
        })
        .collect();
    serde_json::json!({"launch_context": values, "warning": "Launch-time environment, not current focus or proof of lead identity. Pane IDs may be aliases after moves."})
}

#[derive(Deserialize)]
struct SnapshotResponse {
    result: SnapshotResult,
}

#[derive(Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
enum SnapshotResult {
    SessionSnapshot { snapshot: Snapshot },
}

fn decode_snapshot(value: serde_json::Value) -> Result<Snapshot> {
    let response: SnapshotResponse =
        serde_json::from_value(value).context("invalid herdr snapshot response")?;
    let SnapshotResult::SessionSnapshot { snapshot } = response.result;
    Ok(snapshot)
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;

    fn fake(script: &str) -> (tempfile::TempDir, Herdr) {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("herdr");
        std::fs::write(&path, format!("#!/bin/sh\n{script}\n")).unwrap();
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o700)).unwrap();
        (dir, Herdr::new(path))
    }

    #[tokio::test]
    async fn routes_explicit_session_as_one_argument() {
        let fixture = include_str!("../tests/fixtures/herdr-0.9.5-snapshot.json");
        let script = format!(
            "test \"$#\" = 3 && test \"$1\" = '--session=work.test' && test \"$2\" = api && test \"$3\" = snapshot || exit 9\ncat <<'FIXTURE'\n{fixture}\nFIXTURE"
        );
        let (_dir, herdr) = fake(&script);
        let snapshot = herdr.snapshot("work.test").await.unwrap();
        assert_eq!(snapshot.protocol, 22);
        assert_eq!(snapshot.panes[0].terminal_id, "term_test");
        assert_eq!(snapshot.agents[0].agent_status, "blocked");
        assert!(herdr.snapshot("../bad").await.is_err());
    }

    #[test]
    fn rejects_flattened_wrong_kind_and_incomplete_responses() {
        let fixture: serde_json::Value =
            serde_json::from_str(include_str!("../tests/fixtures/herdr-0.9.5-snapshot.json"))
                .unwrap();
        let flattened = serde_json::json!({"result": fixture["result"]["snapshot"]});
        assert!(decode_snapshot(flattened).is_err());
        let mut wrong_kind = fixture.clone();
        wrong_kind["result"]["type"] = "workspace_list".into();
        assert!(decode_snapshot(wrong_kind).is_err());
        let mut incomplete = fixture;
        incomplete["result"]["snapshot"]
            .as_object_mut()
            .unwrap()
            .remove("version");
        assert!(decode_snapshot(incomplete).is_err());
    }

    #[tokio::test]
    async fn surfaces_errors_instead_of_empty_inventory() {
        let (_dir, herdr) = fake("printf '%s' 'session stopped' >&2; exit 1");
        assert!(
            herdr
                .snapshot("default")
                .await
                .unwrap_err()
                .to_string()
                .contains("session stopped")
        );
        let (_dir, herdr) = fake("printf '%s' 'not-json'");
        assert!(herdr.sessions().await.is_err());
    }

    #[tokio::test]
    async fn lists_only_local_sessions() {
        let (_dir, herdr) = fake(
            r#"test "$*" = 'session list --json' || exit 9
printf '%s' '{"sessions":[{"name":"default","default":true,"running":true}]}'"#,
        );
        assert_eq!(herdr.sessions().await.unwrap()[0].name, "default");
    }
}
