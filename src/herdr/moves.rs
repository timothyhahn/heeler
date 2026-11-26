use super::{
    Agent, AgentTarget, Herdr, Pane, TabInfo, WorkspaceInfo, decode, locations::validate_label,
};
use crate::model::{validate_pane_id, validate_tab_id, validate_workspace_id};
use anyhow::{Context, Result, ensure};
use serde::{Deserialize, Serialize};
use std::time::Duration;

#[derive(Debug, Serialize)]
pub struct NativeLocation {
    pub session: String,
    pub workspace_id: String,
    pub tab_id: String,
    pub pane_id: String,
}

impl NativeLocation {
    fn new(session: &str, pane: &Pane) -> Self {
        Self {
            session: session.into(),
            workspace_id: pane.workspace_id.clone(),
            tab_id: pane.tab_id.clone(),
            pane_id: pane.pane_id.clone(),
        }
    }
}

#[derive(Debug, Serialize)]
pub struct AgentMoveReceipt {
    pub changed: bool,
    pub reason: Option<MoveReason>,
    pub previous_location: NativeLocation,
    pub location: NativeLocation,
    pub target: AgentTarget,
    pub agent: Agent,
    pub tab: TabInfo,
    pub closed_tab_id: Option<String>,
    pub closed_workspace_id: Option<String>,
    pub no_focus_requested: bool,
    pub metadata_timing: &'static str,
}

#[derive(Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum MoveReason {
    SameTab,
    ZoomedTab,
}

#[derive(Deserialize)]
struct LayoutLocation {
    workspace_id: String,
    tab_id: String,
    focused_pane_id: String,
}

// Only identity/container fields are consumed; native layout geometry stays unexposed.
#[derive(Deserialize)]
struct MoveResult {
    changed: bool,
    reason: Option<MoveReason>,
    previous_pane_id: String,
    previous_workspace_id: String,
    previous_tab_id: String,
    pane: Pane,
    target_layout: LayoutLocation,
    created_workspace: Option<WorkspaceInfo>,
    created_tab: Option<TabInfo>,
    closed_workspace_id: Option<String>,
    closed_tab_id: Option<String>,
    focused_pane_id: String,
}

#[derive(Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
enum MoveResponse {
    PaneMove { move_result: MoveResult },
}
#[derive(Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
enum PaneResponse {
    PaneInfo { pane: Pane },
}
#[derive(Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
enum WorkspaceResponse {
    WorkspaceInfo { workspace: WorkspaceInfo },
}
#[derive(Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
enum TabResponse {
    TabInfo { tab: TabInfo },
}

fn check_location(pane: &Pane) -> Result<()> {
    validate_pane_id(&pane.pane_id)?;
    validate_workspace_id(&pane.workspace_id)?;
    validate_tab_id(&pane.tab_id)?;
    ensure!(
        pane.pane_id
            .starts_with(&format!("{}:p", pane.workspace_id))
            && pane.tab_id.starts_with(&format!("{}:t", pane.workspace_id)),
        "inconsistent native pane location"
    );
    Ok(())
}

impl Herdr {
    async fn move_pane_info(&self, session: &str, pane_id: &str) -> Result<Pane> {
        let value = self
            .session_json(
                session,
                vec!["pane".into(), "get".into(), pane_id.into()],
                Duration::from_secs(5),
                false,
            )
            .await?;
        let PaneResponse::PaneInfo { pane } =
            decode(value).context("invalid pane metadata response")?;
        check_location(&pane)?;
        ensure!(
            pane.pane_id == pane_id,
            "pane metadata is not the requested canonical location"
        );
        Ok(pane)
    }

    /// Transfers an existing worker only; no input, lifecycle policy or remembered-note edits.
    pub async fn move_agent(
        &self,
        target: &AgentTarget,
        workspace_id: &str,
        label: &str,
    ) -> Result<AgentMoveReceipt> {
        target.validate()?;
        validate_workspace_id(workspace_id)?;
        validate_label(label)?;
        ensure!(
            !label.starts_with('-'),
            "move label must not start with '-'"
        );
        let observed = self.checked_agent(target).await?;
        ensure!(
            !observed.agent.launch_pending,
            "agent launch is pending; inspect before moving"
        );
        let value = self
            .session_json(
                &target.session,
                vec!["workspace".into(), "get".into(), workspace_id.into()],
                Duration::from_secs(5),
                false,
            )
            .await?;
        let WorkspaceResponse::WorkspaceInfo { workspace } =
            decode(value).context("invalid destination workspace response")?;
        ensure!(
            workspace.workspace_id == workspace_id,
            "destination workspace response mismatch"
        );
        let source = self
            .move_pane_info(&target.session, &target.pane_id)
            .await?;
        ensure!(
            source.terminal_id == target.terminal_id,
            "source pane terminal changed"
        );
        // Recheck even a newly observed conversation reference before issuing the move.
        let current = self.checked_agent(&observed.target).await?;
        ensure!(
            !current.agent.launch_pending,
            "agent launch is pending; inspect before moving"
        );
        async {
            let value = self
                .session_json(
                    &target.session,
                    vec![
                        "pane".into(),
                        "move".into(),
                        target.pane_id.clone(),
                        "--new-tab".into(),
                        "--workspace".into(),
                        workspace_id.into(),
                        "--label".into(),
                        label.into(),
                        "--no-focus".into(),
                    ],
                    Duration::from_secs(10),
                    true,
                )
                .await?;
            let MoveResponse::PaneMove { move_result: moved } =
                decode(value).context("invalid native pane move response")?;
            check_location(&moved.pane)?;
            ensure!(
                moved.previous_pane_id == source.pane_id
                    && moved.previous_workspace_id == source.workspace_id
                    && moved.previous_tab_id == source.tab_id
                    && moved.pane.terminal_id == target.terminal_id,
                "move response source/terminal mismatch"
            );
            ensure!(
                moved.target_layout.workspace_id == moved.pane.workspace_id
                    && moved.target_layout.tab_id == moved.pane.tab_id
                    && moved.target_layout.focused_pane_id == moved.focused_pane_id,
                "move response target layout mismatch"
            );
            validate_pane_id(&moved.focused_pane_id)?;
            ensure!(
                moved.focused_pane_id.starts_with(&format!("{}:p", moved.pane.workspace_id)),
                "move layout focus workspace mismatch"
            );
            ensure!(moved.created_workspace.is_none(), "unexpected workspace creation");
            if moved.changed {
                let tab = moved
                    .created_tab
                    .as_ref()
                    .context("missing created tab in move response")?;
                ensure!(
                    moved.reason.is_none()
                        && moved.pane.workspace_id == workspace_id
                        && moved.pane.tab_id != source.tab_id
                        && tab.tab_id == moved.pane.tab_id
                        && tab.workspace_id == workspace_id
                        && tab.label.as_deref() == Some(label)
                        && tab.pane_count == 1
                        && tab.number > 0
                        && moved.focused_pane_id == moved.pane.pane_id,
                    "move response destination/new-tab mismatch"
                );
                ensure!(
                    moved.closed_tab_id.as_ref().is_none_or(|id| id == &source.tab_id),
                    "unexpected closed tab"
                );
                ensure!(
                    moved.closed_workspace_id.as_ref().is_none_or(|id|
                        id == &source.workspace_id
                            && workspace_id != source.workspace_id
                            && moved.closed_tab_id.is_some()
                    ),
                    "unexpected closed workspace"
                );
            } else {
                ensure!(
                    moved.reason == Some(MoveReason::ZoomedTab)
                        && moved.created_tab.is_none()
                        && moved.closed_tab_id.is_none()
                        && moved.closed_workspace_id.is_none()
                        && moved.pane.pane_id == source.pane_id
                        && moved.pane.workspace_id == source.workspace_id
                        && moved.pane.tab_id == source.tab_id,
                    "inconsistent native new-tab no-op"
                );
            }
            let pane = self
                .move_pane_info(&target.session, &moved.pane.pane_id)
                .await?;
            ensure!(
                pane.terminal_id == target.terminal_id
                    && pane.workspace_id == moved.pane.workspace_id
                    && pane.tab_id == moved.pane.tab_id,
                "post-move pane identity/destination changed"
            );
            let value = self
                .session_json(
                    &target.session,
                    vec!["tab".into(), "get".into(), pane.tab_id.clone()],
                    Duration::from_secs(5),
                    false,
                )
                .await?;
            let TabResponse::TabInfo { tab } =
                decode(value).context("invalid post-move tab response")?;
            ensure!(
                tab.tab_id == pane.tab_id && tab.workspace_id == pane.workspace_id,
                "post-move tab location mismatch"
            );
            if moved.changed {
                ensure!(
                    tab.label.as_deref() == Some(label)
                        && tab.pane_count == 1
                        && moved.created_tab.as_ref().is_some_and(|created| created.number == tab.number),
                    "post-move new tab changed"
                );
            }
            let mut expected = current.target;
            expected.pane_id = pane.pane_id.clone();
            let fresh = self.checked_agent(&expected).await?;
            ensure!(!fresh.agent.launch_pending, "post-move agent launch is pending");
            let mut new_target = target.clone();
            new_target.pane_id = pane.pane_id.clone();
            Ok(AgentMoveReceipt {
                changed: moved.changed,
                reason: moved.reason,
                previous_location: NativeLocation::new(&target.session, &source),
                location: NativeLocation::new(&target.session, &pane),
                target: new_target,
                agent: fresh.agent,
                tab,
                closed_tab_id: moved.closed_tab_id,
                closed_workspace_id: moved.closed_workspace_id,
                no_focus_requested: true,
                metadata_timing: "post_move_inspection",
            })
        }
        .await
        .context("agent possibly moved; inspect before retrying; do not retry or recreate locations automatically")
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::{Value, json};

    #[test]
    fn native_move_fixture_uses_move_result_and_target_local_focus() {
        let value: Value = serde_json::from_str(include_str!(
            "../../tests/fixtures/herdr-0.9.5-pane-move.json"
        ))
        .unwrap();
        let MoveResponse::PaneMove { move_result } = decode(value.clone()).unwrap();
        assert!(move_result.changed);
        assert_eq!(move_result.pane.terminal_id, "term_worker");
        assert_eq!(move_result.focused_pane_id, "w2:p2");
        assert_eq!(
            move_result.created_tab.unwrap().label.as_deref(),
            Some("Worker review α")
        );
        let wrong =
            json!({"result": {"type": "pane_move", "move": value["result"]["move_result"]}});
        assert!(decode::<MoveResponse>(wrong).is_err());
    }

    #[tokio::test]
    async fn invalid_move_inputs_stop_before_native_metadata() {
        let dir = tempfile::tempdir().unwrap();
        let herdr = Herdr::new(dir.path().join("missing-herdr"));
        let target = AgentTarget {
            session: "default".into(),
            pane_id: "w1:p1".into(),
            terminal_id: "term_test".into(),
            agent: "pi".into(),
            agent_session: None,
        };
        for label in [
            "".into(),
            " ".into(),
            "--session=other".into(),
            "-label".into(),
            "bad\nlabel".into(),
            "bad\0label".into(),
            "é".repeat(129),
        ] {
            let error = herdr.move_agent(&target, "w2", &label).await.unwrap_err();
            assert!(error.to_string().contains("label"), "{error:#}");
        }
        for workspace in ["", "--remote=x", "retired-workspace", "w1:t1"] {
            assert!(
                herdr
                    .move_agent(&target, workspace, "Worker")
                    .await
                    .unwrap_err()
                    .to_string()
                    .contains("workspace ID")
            );
        }
    }
}
