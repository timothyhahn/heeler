use super::{Herdr, decode, locations::validate_label};
use crate::model::{validate_tab_id, validate_workspace_id};
use anyhow::{Context, Result, bail, ensure};
use serde::{Deserialize, Serialize};
use std::time::Duration;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct WorkspaceInfo {
    pub workspace_id: String,
    pub number: u32,
    pub label: Option<String>,
    pub focused: bool,
    pub pane_count: u32,
    pub tab_count: u32,
    pub active_tab_id: Option<String>,
    pub agent_status: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TabInfo {
    pub tab_id: String,
    pub workspace_id: String,
    pub number: u32,
    pub label: Option<String>,
    pub focused: bool,
    pub pane_count: u32,
    pub agent_status: String,
}

#[derive(Debug, Serialize)]
pub struct RenamedWorkspace {
    pub session: String,
    pub workspace: WorkspaceInfo,
}

#[derive(Debug, Serialize)]
pub struct RenamedTab {
    pub session: String,
    pub tab: TabInfo,
}

#[derive(Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
enum RenameResult {
    WorkspaceInfo { workspace: WorkspaceInfo },
    TabInfo { tab: TabInfo },
}

impl Herdr {
    /// Changes only the workspace label; never focuses or retries.
    pub async fn rename_workspace(
        &self,
        session: &str,
        workspace_id: &str,
        label: &str,
    ) -> Result<RenamedWorkspace> {
        validate_workspace_id(workspace_id)?;
        let result = self
            .rename(session, "workspace", workspace_id, label)
            .await?;
        let RenameResult::WorkspaceInfo { workspace } = result else {
            bail!("unexpected rename response; inspect the session before retrying");
        };
        ensure!(
            workspace.workspace_id == workspace_id && workspace.label.as_deref() == Some(label),
            "rename response does not match the request; inspect the session before retrying"
        );
        Ok(RenamedWorkspace {
            session: session.into(),
            workspace,
        })
    }

    /// Changes only the tab label; never focuses or retries.
    pub async fn rename_tab(&self, session: &str, tab_id: &str, label: &str) -> Result<RenamedTab> {
        validate_tab_id(tab_id)?;
        let result = self.rename(session, "tab", tab_id, label).await?;
        let RenameResult::TabInfo { tab } = result else {
            bail!("unexpected rename response; inspect the session before retrying");
        };
        ensure!(
            tab.tab_id == tab_id
                && tab_id.starts_with(&format!("{}:t", tab.workspace_id))
                && tab.label.as_deref() == Some(label),
            "rename response does not match the request; inspect the session before retrying"
        );
        Ok(RenamedTab {
            session: session.into(),
            tab,
        })
    }

    async fn rename(
        &self,
        session: &str,
        area: &str,
        id: &str,
        label: &str,
    ) -> Result<RenameResult> {
        validate_label(label)?;
        let value = self
            .session_json(
                session,
                vec![area.into(), "rename".into(), id.into(), label.into()],
                Duration::from_secs(5),
                true,
            )
            .await?;
        decode(value).context("invalid rename response; inspect the session before retrying")
    }
}
