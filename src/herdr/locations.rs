use super::{Herdr, Pane, decode};
use crate::model::{Location, validate_pane_id, validate_workspace_id};
use anyhow::{Context, Result, ensure};
use schemars::JsonSchema;
use serde::{Deserialize, Serialize};
use std::path::Path;
use std::time::Duration;

#[derive(Debug, Deserialize, JsonSchema)]
#[serde(tag = "type", rename_all = "snake_case", deny_unknown_fields)]
pub enum OpenLocation {
    Workspace {
        cwd: String,
        label: String,
    },
    Tab {
        workspace_id: String,
        cwd: String,
        label: String,
    },
    Split {
        pane_id: String,
        cwd: String,
        direction: Direction,
    },
}

#[derive(Debug, Clone, Copy, Deserialize, JsonSchema)]
#[serde(rename_all = "snake_case")]
pub enum Direction {
    Right,
    Down,
}

#[derive(Debug, Serialize)]
pub struct OpenedLocation {
    pub location: Location,
    pub pane: Pane,
}

#[derive(Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
enum Created {
    #[serde(rename = "workspace_created")]
    Workspace { root_pane: Pane },
    #[serde(rename = "tab_created")]
    Tab { root_pane: Pane },
    #[serde(rename = "pane_info")]
    Pane { pane: Pane },
}

enum CreationKind {
    Workspace,
    Tab,
    Pane,
}

impl Herdr {
    /// Creates only the explicitly requested topology, without changing focus.
    pub async fn open_location(
        &self,
        session: &str,
        location: OpenLocation,
    ) -> Result<OpenedLocation> {
        let (mut args, cwd, kind) = match location {
            OpenLocation::Workspace { cwd, label } => {
                validate_label(&label)?;
                (
                    vec!["workspace".into(), "create".into(), "--label".into(), label],
                    cwd,
                    CreationKind::Workspace,
                )
            }
            OpenLocation::Tab {
                workspace_id,
                cwd,
                label,
            } => {
                validate_workspace_id(&workspace_id)?;
                validate_label(&label)?;
                (
                    vec![
                        "tab".into(),
                        "create".into(),
                        "--workspace".into(),
                        workspace_id,
                        "--label".into(),
                        label,
                    ],
                    cwd,
                    CreationKind::Tab,
                )
            }
            OpenLocation::Split {
                pane_id,
                cwd,
                direction,
            } => {
                validate_pane_id(&pane_id)?;
                let direction = match direction {
                    Direction::Right => "right",
                    Direction::Down => "down",
                };
                (
                    vec![
                        "pane".into(),
                        "split".into(),
                        "--pane".into(),
                        pane_id,
                        "--direction".into(),
                        direction.into(),
                    ],
                    cwd,
                    CreationKind::Pane,
                )
            }
        };
        ensure!(
            Path::new(&cwd).is_absolute()
                && cwd.len() <= 4096
                && !cwd.chars().any(char::is_control),
            "cwd must be an absolute local path without control characters"
        );
        args.extend(["--cwd".into(), cwd, "--no-focus".into()]);
        let value = self
            .session_json(session, args, Duration::from_secs(10), true)
            .await?;
        let created = decode::<Created>(value)
            .context("invalid creation response; inspect the session before retrying")?;
        let pane = match (kind, created) {
            (CreationKind::Workspace, Created::Workspace { root_pane }) => root_pane,
            (CreationKind::Tab, Created::Tab { root_pane }) => root_pane,
            (CreationKind::Pane, Created::Pane { pane }) => pane,
            _ => anyhow::bail!("unexpected creation response; inspect the session before retrying"),
        };
        Ok(OpenedLocation {
            location: Location {
                session: session.into(),
                workspace_id: pane.workspace_id.clone(),
                pane_id: Some(pane.pane_id.clone()),
            },
            pane,
        })
    }
}

pub(super) fn validate_label(label: &str) -> Result<()> {
    ensure!(
        !label.trim().is_empty() && label.len() <= 256 && !label.chars().any(char::is_control),
        "label must be 1–256 bytes without control characters"
    );
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn splits_return_pane_info() {
        // Shape observed from native herdr 0.9.5, not a pane_created response.
        let value = json!({
            "id": "cli:pane:split",
            "result": {
                "type": "pane_info",
                "pane": {
                    "pane_id": "w1:p2", "terminal_id": "term_ping",
                    "workspace_id": "w1", "tab_id": "w1:t1",
                    "cwd": "/work/ping", "label": null, "focused": false
                }
            }
        });
        let Created::Pane { pane } = decode::<Created>(value).unwrap() else {
            panic!("expected the new pane");
        };
        assert_eq!(pane.pane_id, "w1:p2");
        assert_eq!(pane.terminal_id, "term_ping");
    }
}
