mod agents;
mod close;
mod locations;
mod moves;
mod renames;

pub use agents::{AgentHandle, AgentTarget, PromptAcknowledgement, ReadSource, StartAgent};
pub use close::ClosedTab;
pub use locations::{Direction, OpenLocation, OpenedLocation};
pub use moves::{AgentMoveReceipt, MoveReason, NativeLocation};
pub use renames::{RenamedTab, RenamedWorkspace, TabInfo, WorkspaceInfo};

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
    pub launch_pending: bool,
    #[serde(default)]
    pub interactive_ready: bool,
    #[serde(default)]
    pub completion_seq: Option<u64>,
    #[serde(default)]
    pub agent_session: Option<serde_json::Value>,
}

impl Agent {
    // Native Codex startup may be interactive before status detection settles.
    pub fn ready_for_prompt(&self) -> bool {
        !self.launch_pending
            && (matches!(self.agent_status.as_str(), "idle" | "done")
                || (self.agent.as_deref() == Some("codex")
                    && self.agent_status == "unknown"
                    && self.interactive_ready))
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Snapshot {
    pub version: String,
    pub protocol: u32,
    pub focused_workspace_id: Option<String>,
    pub focused_tab_id: Option<String>,
    pub focused_pane_id: Option<String>,
    pub workspaces: Vec<Workspace>,
    pub tabs: Vec<TabInfo>,
    pub panes: Vec<Pane>,
    pub agents: Vec<Agent>,
}

impl Herdr {
    pub fn new(binary: PathBuf) -> Self {
        Self { binary }
    }

    async fn run(&self, args: &[String], timeout: Duration) -> Result<String> {
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
        let (status, _, _) = tokio::time::timeout(timeout, async {
            tokio::try_join!(
                child.wait(),
                stdout.read_to_end(&mut out),
                stderr.read_to_end(&mut err)
            )
        })
        .await
        .context("herdr command timed out")??;
        ensure!(
            out.len() as u64 <= MAX_OUTPUT && err.len() as u64 <= MAX_OUTPUT,
            "herdr output exceeded limit"
        );
        if !status.success() {
            bail!(
                "herdr command failed: {}",
                String::from_utf8_lossy(&err)
                    .chars()
                    .take(2000)
                    .collect::<String>()
            );
        }
        String::from_utf8(out).context("herdr returned invalid UTF-8")
    }

    async fn json(&self, args: &[String]) -> Result<serde_json::Value> {
        parse_json(&self.run(args, Duration::from_secs(5)).await?)
    }

    fn session_args(session: &str, args: Vec<String>) -> Result<Vec<String>> {
        validate_session(session)?;
        let mut routed = vec![format!("--session={session}")];
        routed.extend(args);
        Ok(routed)
    }

    async fn session_json(
        &self,
        session: &str,
        args: Vec<String>,
        timeout: Duration,
        mutates: bool,
    ) -> Result<serde_json::Value> {
        let args = Self::session_args(session, args)?;
        let result = async { parse_json(&self.run(&args, timeout).await?) }.await;
        if mutates {
            result.with_context(|| format!(
                "herdr operation in session {session} may have taken effect; inspect before retrying"
            ))
        } else {
            result
        }
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
struct Response<T> {
    result: T,
}

fn parse_json(text: &str) -> Result<serde_json::Value> {
    let value: serde_json::Value =
        serde_json::from_str(text).context("herdr returned invalid JSON")?;
    if let Some(error) = value.get("error") {
        bail!("herdr API error: {error}");
    }
    Ok(value)
}

fn decode<T: serde::de::DeserializeOwned>(value: serde_json::Value) -> Result<T> {
    let response: Response<T> = serde_json::from_value(value).context("invalid herdr response")?;
    Ok(response.result)
}

#[derive(Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
enum SnapshotResult {
    SessionSnapshot { snapshot: Snapshot },
}

fn decode_snapshot(value: serde_json::Value) -> Result<Snapshot> {
    let SnapshotResult::SessionSnapshot { snapshot } =
        decode(value).context("invalid herdr snapshot response")?;
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

    #[test]
    fn readiness_accepts_only_settled_or_native_ready_codex_agents() {
        for kind in ["pi", "codex", "claude"] {
            for status in [
                "idle",
                "done",
                "unknown",
                "blocked",
                "working",
                "unsupported",
            ] {
                for ready in [false, true] {
                    for pending in [false, true] {
                        let agent: Agent = serde_json::from_value(serde_json::json!({
                            "pane_id": "w1:p1", "terminal_id": "term_test", "agent": kind,
                            "agent_status": status, "interactive_ready": ready, "launch_pending": pending,
                        })).unwrap();
                        let expected = !pending
                            && (matches!(status, "idle" | "done")
                                || (kind == "codex" && status == "unknown" && ready));
                        assert_eq!(agent.ready_for_prompt(), expected, "{agent:?}");
                    }
                }
            }
        }
        let legacy: Agent = serde_json::from_value(serde_json::json!({
            "pane_id": "w1:p1", "terminal_id": "term_test", "agent": "codex", "agent_status": "unknown",
        })).unwrap();
        assert!(!legacy.interactive_ready && !legacy.launch_pending);
        assert!(!legacy.ready_for_prompt());
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
        assert_eq!(snapshot.tabs[0].tab_id, "w1:t1");
        assert_eq!(snapshot.tabs[0].label.as_deref(), Some("Notes"));
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

    #[test]
    fn snapshot_preserves_tab_metadata_without_inventing_missing_labels() {
        let fixture: serde_json::Value =
            serde_json::from_str(include_str!("../tests/fixtures/herdr-0.9.5-snapshot.json"))
                .unwrap();
        let snapshot = decode_snapshot(fixture.clone()).unwrap();
        assert_eq!(
            serde_json::to_value(&snapshot.tabs).unwrap(),
            fixture["result"]["snapshot"]["tabs"]
        );
        for label in [
            serde_json::json!("impl → inventory"),
            serde_json::json!(""),
            serde_json::Value::Null,
        ] {
            let mut response = fixture.clone();
            response["result"]["snapshot"]["tabs"][0]["label"] = label;
            let snapshot = decode_snapshot(response.clone()).unwrap();
            assert_eq!(
                serde_json::to_value(snapshot.tabs).unwrap(),
                response["result"]["snapshot"]["tabs"]
            );
        }
        let mut response = fixture;
        response["result"]["snapshot"]["tabs"][0]
            .as_object_mut()
            .unwrap()
            .remove("label");
        assert!(decode_snapshot(response).unwrap().tabs[0].label.is_none());
    }

    #[test]
    fn snapshot_requires_native_tabs_and_rejects_malformed_entries() {
        let fixture: serde_json::Value =
            serde_json::from_str(include_str!("../tests/fixtures/herdr-0.9.5-snapshot.json"))
                .unwrap();
        let mut missing = fixture.clone();
        missing["result"]["snapshot"]
            .as_object_mut()
            .unwrap()
            .remove("tabs");
        assert!(
            decode_snapshot(missing)
                .unwrap_err()
                .to_string()
                .contains("invalid herdr snapshot response")
        );
        for tabs in [
            serde_json::Value::Null,
            serde_json::json!({}),
            serde_json::json!("tabs"),
            serde_json::json!([null]),
        ] {
            let mut response = fixture.clone();
            response["result"]["snapshot"]["tabs"] = tabs;
            assert!(decode_snapshot(response).is_err());
        }
        for field in [
            "tab_id",
            "workspace_id",
            "number",
            "focused",
            "pane_count",
            "agent_status",
        ] {
            let mut response = fixture.clone();
            response["result"]["snapshot"]["tabs"][0]
                .as_object_mut()
                .unwrap()
                .remove(field);
            assert!(decode_snapshot(response).is_err(), "missing {field}");
        }
        for (field, value) in [
            ("tab_id", serde_json::json!(1)),
            ("workspace_id", serde_json::Value::Null),
            ("number", serde_json::json!("1")),
            ("focused", serde_json::json!("true")),
            ("pane_count", serde_json::json!(-1)),
            ("agent_status", serde_json::json!({})),
            ("label", serde_json::json!(42)),
        ] {
            let mut response = fixture.clone();
            response["result"]["snapshot"]["tabs"][0][field] = value;
            assert!(decode_snapshot(response).is_err(), "malformed {field}");
        }
        let mut empty = fixture;
        for collection in ["workspaces", "tabs", "panes", "agents", "layouts"] {
            empty["result"]["snapshot"][collection] = serde_json::json!([]);
        }
        for focus in ["focused_workspace_id", "focused_tab_id", "focused_pane_id"] {
            empty["result"]["snapshot"][focus] = serde_json::Value::Null;
        }
        assert!(decode_snapshot(empty).unwrap().tabs.is_empty());
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
    async fn renames_route_session_and_keep_labels_in_one_argument() {
        let label = "Review & notes; $(not-a-command) \"quoted\"";
        let fixture: serde_json::Value =
            serde_json::from_str(include_str!("../tests/fixtures/herdr-0.9.5-snapshot.json"))
                .unwrap();
        for area in ["workspace", "tab"] {
            let mut metadata = fixture["result"]["snapshot"][format!("{area}s")][0].clone();
            metadata["label"] = label.into();
            let id = if area == "workspace" { "w1" } else { "w1:t1" };
            let response = serde_json::json!({"result": {
                "type": format!("{area}_info"), area: metadata,
            }});
            let script = format!(
                "test \"$#\" = 5 && test \"$1\" = '--session=work.test' && test \"$2\" = '{area}' && test \"$3\" = rename && test \"$4\" = '{id}' && test \"$5\" = '{label}' || exit 9\nprintf '%s' '{response}'"
            );
            let (_dir, herdr) = fake(&script);
            let result = if area == "workspace" {
                serde_json::to_value(
                    herdr
                        .rename_workspace("work.test", id, label)
                        .await
                        .unwrap(),
                )
            } else {
                serde_json::to_value(herdr.rename_tab("work.test", id, label).await.unwrap())
            }
            .unwrap();
            assert_eq!(result["session"], "work.test");
            assert_eq!(result[area], metadata);
        }
    }

    #[tokio::test]
    async fn invalid_rename_inputs_never_launch_native_commands() {
        let herdr = Herdr::new(PathBuf::from("/nonexistent/heeler-test-herdr"));
        for (area, valid_id) in [("workspace", "w1"), ("tab", "w1:t1")] {
            let rename = async |session: &str, id: &str, label: &str| {
                if area == "workspace" {
                    herdr.rename_workspace(session, id, label).await.map(|_| ())
                } else {
                    herdr.rename_tab(session, id, label).await.map(|_| ())
                }
            };
            for id in [
                "",
                "--help",
                "--focus=w1",
                "w1;echo",
                "w1:t1 --focus",
                "../w1",
            ] {
                let error = rename("default", id, "Label").await.unwrap_err();
                assert!(error.to_string().contains("ID"), "{error:#}");
            }
            let oversized = "a".repeat(257);
            let multibyte = "é".repeat(129);
            for label in [
                "", "  ", "\n", "a\0b", "a\tb", "a\u{7f}b", "--help", "--", "-x", &oversized,
                &multibyte,
            ] {
                let error = rename("default", valid_id, label).await.unwrap_err();
                assert!(error.to_string().contains("label"), "{error:#}");
            }
            for session in ["", "../bad", "ssh:host", "default --remote=host"] {
                let error = rename(session, valid_id, "Label").await.unwrap_err();
                assert!(error.to_string().contains("session"), "{error:#}");
            }
        }
    }

    #[tokio::test]
    async fn rename_failures_are_uncertain_and_never_retried() {
        let fixture: serde_json::Value =
            serde_json::from_str(include_str!("../tests/fixtures/herdr-0.9.5-snapshot.json"))
                .unwrap();
        for area in ["workspace", "tab"] {
            let mut metadata = fixture["result"]["snapshot"][format!("{area}s")][0].clone();
            metadata["label"] = "Label".into();
            let response = serde_json::json!({"result": {
                "type": format!("{area}_info"), area: metadata,
            }});
            let mut wrong_id = response.clone();
            wrong_id["result"][area][format!("{area}_id")] = "w9".into();
            let mut wrong_label = response.clone();
            wrong_label["result"][area]["label"] = "Other".into();
            let mut incomplete = response.clone();
            incomplete["result"][area]
                .as_object_mut()
                .unwrap()
                .remove("number");
            let mut wrong_kind = response.clone();
            wrong_kind["result"]["type"] = "session_snapshot".into();
            let mut wrong_workspace = response.clone();
            if area == "tab" {
                wrong_workspace["result"]["tab"]["workspace_id"] = "w9".into();
            } else {
                wrong_workspace["result"]["workspace"] = serde_json::Value::Null;
            }
            let responses = [
                "printf '%s' 'native rename denied' >&2; exit 1".into(),
                "printf '%s' '{\"error\":\"native API denied\"}'".into(),
                "printf '%s' 'not-json'".into(),
                format!("printf '%s' '{}'", response["result"][area]),
                format!("printf '%s' '{wrong_kind}'"),
                format!("printf '%s' '{incomplete}'"),
                format!("printf '%s' '{wrong_id}'"),
                format!("printf '%s' '{wrong_label}'"),
                format!("printf '%s' '{wrong_workspace}'"),
            ];
            for (index, script) in responses.iter().enumerate() {
                let (dir, herdr) = fake(&format!("printf x >> \"$0.calls\"\n{script}"));
                let error = if area == "workspace" {
                    herdr
                        .rename_workspace("default", "w1", "Label")
                        .await
                        .map(|_| ())
                } else {
                    herdr
                        .rename_tab("default", "w1:t1", "Label")
                        .await
                        .map(|_| ())
                }
                .unwrap_err();
                let message = format!("{error:#}");
                assert!(message.contains("inspect"), "{message}");
                if index == 0 {
                    assert!(message.contains("native rename denied"), "{message}");
                } else if index == 1 {
                    assert!(message.contains("native API denied"), "{message}");
                }
                assert_eq!(std::fs::read(dir.path().join("herdr.calls")).unwrap(), b"x");
            }
        }
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
