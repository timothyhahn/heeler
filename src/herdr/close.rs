use super::{Herdr, TabInfo, WorkspaceInfo, decode};
use crate::model::validate_tab_id;
use anyhow::{Context, Result, ensure};
use serde::{Deserialize, Serialize};
use std::time::Duration;

#[derive(Debug, Serialize)]
pub struct ClosedTab {
    pub session: String,
    pub tab_id: String,
    /// Native acknowledgement, not independent verification of process exit.
    pub acknowledged: bool,
}

#[derive(Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
enum TabResult {
    TabInfo { tab: TabInfo },
}

#[derive(Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
enum WorkspaceResult {
    WorkspaceInfo { workspace: WorkspaceInfo },
}

#[derive(Deserialize)]
#[serde(tag = "type", rename_all = "snake_case", deny_unknown_fields)]
enum CloseResult {
    Ok {},
}

impl Herdr {
    /// Destructive, never retried. Preflights cannot prevent concurrent topology changes.
    pub async fn close_tab(&self, session: &str, tab_id: &str) -> Result<ClosedTab> {
        validate_tab_id(tab_id)?;
        let workspace_id = tab_id.split_once(":t").context("invalid tab ID")?.0;
        let value = self
            .session_json(
                session,
                vec!["tab".into(), "get".into(), tab_id.into()],
                Duration::from_secs(5),
                false,
            )
            .await?;
        let TabResult::TabInfo { tab } = decode(value).context("invalid tab preflight response")?;
        ensure!(
            tab.tab_id == tab_id && tab.workspace_id == workspace_id,
            "tab preflight does not match the requested target"
        );
        let value = self
            .session_json(
                session,
                vec!["workspace".into(), "get".into(), workspace_id.into()],
                Duration::from_secs(5),
                false,
            )
            .await?;
        let WorkspaceResult::WorkspaceInfo { workspace } =
            decode(value).context("invalid workspace preflight response")?;
        ensure!(
            workspace.workspace_id == workspace_id,
            "workspace preflight does not match the requested target"
        );
        // Native tab.close also closes the workspace for its last tab. Never
        // deliberately widen this tab-only operation to a workspace/group close.
        ensure!(
            workspace.tab_count > 1,
            "refusing last-tab close: native herdr would close the workspace; no close was sent"
        );
        let value = self
            .session_json(
                session,
                vec!["tab".into(), "close".into(), tab_id.into()],
                Duration::from_secs(5),
                true,
            )
            .await?;
        let CloseResult::Ok {} = decode(value).context(
            "invalid close acknowledgement; close may have taken effect; inspect before retrying",
        )?;
        Ok(ClosedTab {
            session: session.into(),
            tab_id: tab_id.into(),
            acknowledged: true,
        })
    }
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    use serde_json::{Value, json};
    use std::os::unix::fs::PermissionsExt;

    fn responses() -> (Value, Value) {
        let fixture: Value = serde_json::from_str(include_str!(
            "../../tests/fixtures/herdr-0.9.5-snapshot.json"
        ))
        .unwrap();
        let snapshot = &fixture["result"]["snapshot"];
        let tab = json!({"result": {"type": "tab_info", "tab": snapshot["tabs"][0]}});
        let mut workspace =
            json!({"result": {"type": "workspace_info", "workspace": snapshot["workspaces"][0]}});
        workspace["result"]["workspace"]["tab_count"] = 2.into();
        (tab, workspace)
    }

    fn fake(tab: &str, workspace: &str, close: &str) -> (tempfile::TempDir, Herdr) {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("herdr");
        let script = format!(
            "#!/bin/sh\nprintf x >> \"$0.calls\"\ntest \"$#\" = 4 && test \"$1\" = '--session=work.test' || exit 9\ncase \"$2 $3 $4\" in\n 'tab get w1:t1') {tab};;\n 'workspace get w1') {workspace};;\n 'tab close w1:t1') {close};;\n *) exit 9;;\nesac\n"
        );
        std::fs::write(&path, script).unwrap();
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o700)).unwrap();
        (dir, Herdr::new(path))
    }

    fn emit(value: &Value) -> String {
        format!("printf '%s' '{value}'")
    }

    #[tokio::test]
    async fn routes_exact_target_and_accepts_only_native_ok_acknowledgement() {
        let (tab, workspace) = responses();
        let (dir, herdr) = fake(
            &emit(&tab),
            &emit(&workspace),
            "printf '%s' '{\"result\":{\"type\":\"ok\"}}'",
        );
        let ack = herdr.close_tab("work.test", "w1:t1").await.unwrap();
        assert_eq!(
            serde_json::to_value(ack).unwrap(),
            json!({"session": "work.test", "tab_id": "w1:t1", "acknowledged": true})
        );
        assert_eq!(
            std::fs::read(dir.path().join("herdr.calls")).unwrap(),
            b"xxx"
        );
    }

    #[tokio::test]
    async fn invalid_inputs_do_not_launch_a_native_command() {
        let dir = tempfile::tempdir().unwrap();
        let herdr = Herdr::new(dir.path().join("missing-herdr"));
        for id in [
            "",
            "--help",
            "--",
            "w1",
            "w1:p1",
            "w1:t",
            "w1:t1;echo",
            "w1:t1 --force",
            "w1:t1:t2",
        ] {
            assert!(
                herdr
                    .close_tab("work.test", id)
                    .await
                    .unwrap_err()
                    .to_string()
                    .contains("ID")
            );
        }
        for id in [
            format!("w1:t{}", "a".repeat(33)),
            format!("w{}:t1", "a".repeat(128)),
        ] {
            assert!(
                herdr
                    .close_tab("work.test", &id)
                    .await
                    .unwrap_err()
                    .to_string()
                    .contains("ID")
            );
        }
        for session in ["", "../bad", "ssh:host", "work --remote=host"] {
            assert!(
                herdr
                    .close_tab(session, "w1:t1")
                    .await
                    .unwrap_err()
                    .to_string()
                    .contains("session")
            );
        }
    }

    #[tokio::test]
    async fn preflight_refuses_last_tabs_mismatches_and_malformed_inventory_without_closing() {
        let (tab, workspace) = responses();
        let mut wrong_tab = tab.clone();
        wrong_tab["result"]["tab"]["tab_id"] = "w1:t9".into();
        let mut wrong_parent = tab.clone();
        wrong_parent["result"]["tab"]["workspace_id"] = "w9".into();
        let mut wrong_workspace = workspace.clone();
        wrong_workspace["result"]["workspace"]["workspace_id"] = "w9".into();
        let mut last_tab = workspace.clone();
        last_tab["result"]["workspace"]["tab_count"] = 1.into();
        let mut missing_count = workspace.clone();
        missing_count["result"]["workspace"]
            .as_object_mut()
            .unwrap()
            .remove("tab_count");
        for (tab_response, workspace_response, calls, message) in [
            (emit(&wrong_tab), emit(&workspace), b"x".as_slice(), "match"),
            (emit(&wrong_parent), emit(&workspace), b"x", "match"),
            ("printf not-json".into(), emit(&workspace), b"x", "invalid"),
            (
                emit(&json!({"result": {"type": "ok"}})),
                emit(&workspace),
                b"x",
                "invalid",
            ),
            (
                "printf 'tab not found' >&2; exit 1".into(),
                emit(&workspace),
                b"x",
                "tab not found",
            ),
            (emit(&tab), emit(&wrong_workspace), b"xx", "match"),
            (emit(&tab), emit(&last_tab), b"xx", "last-tab"),
            (emit(&tab), emit(&missing_count), b"xx", "invalid"),
            (emit(&tab), "printf not-json".into(), b"xx", "invalid"),
        ] {
            let (dir, herdr) = fake(&tab_response, &workspace_response, "exit 99");
            let error = format!(
                "{:#}",
                herdr.close_tab("work.test", "w1:t1").await.unwrap_err()
            );
            assert!(error.contains(message), "{error}");
            assert_eq!(
                std::fs::read(dir.path().join("herdr.calls")).unwrap(),
                calls
            );
        }
    }

    #[tokio::test]
    async fn close_errors_and_invalid_acknowledgements_are_uncertain_and_never_retried() {
        let (tab, workspace) = responses();
        for close in [
            "printf 'native close denied' >&2; exit 1".into(),
            emit(
                &json!({"error": {"code": "confirmation_required", "message": "requires human input"}}),
            ),
            "printf not-json".into(),
            emit(&json!({"type": "ok"})),
            emit(&json!({"result": {}})),
            emit(&json!({"result": {"type": "tab_closed"}})),
            emit(&json!({"result": {"type": "ok", "tab_id": "w9:t9"}})),
        ] {
            let (dir, herdr) = fake(&emit(&tab), &emit(&workspace), &close);
            let error = format!(
                "{:#}",
                herdr.close_tab("work.test", "w1:t1").await.unwrap_err()
            );
            assert!(error.contains("inspect before retrying"), "{error}");
            if close.contains("native close denied") {
                assert!(error.contains("native close denied"), "{error}");
            }
            if close.contains("confirmation_required") {
                assert!(error.contains("confirmation_required"), "{error}");
            }
            assert_eq!(
                std::fs::read(dir.path().join("herdr.calls")).unwrap(),
                b"xxx"
            );
        }
    }
}
