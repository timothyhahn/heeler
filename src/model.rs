use crate::assignment::Assignment;
use anyhow::{Context, Result, ensure};
use schemars::JsonSchema;
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(rename_all = "snake_case")]
pub enum Status {
    Planned,
    Active,
    Paused,
    Done,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct Location {
    /// Exact local herdr session name, including "default".
    pub session: String,
    pub workspace_id: String,
    /// Optional pane location; references are hints to reconcile, not occupant identity.
    pub pane_id: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct Workstream {
    /// Stable user-chosen slug, independent of herdr IDs.
    pub id: String,
    pub title: String,
    pub purpose: String,
    pub status: Status,
    pub next_action: Option<String>,
    pub blocker: Option<String>,
    #[serde(default)]
    pub locations: Vec<Location>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub assignments: Vec<Assignment>,
}

impl Workstream {
    pub fn validate(&self) -> Result<()> {
        validate_id(&self.id)?;
        ensure!(
            !self.title.trim().is_empty() && self.title.len() <= 256,
            "title must contain 1–256 bytes"
        );
        ensure!(self.purpose.len() <= 4096, "purpose exceeds 4096 bytes");
        for note in [&self.next_action, &self.blocker].into_iter().flatten() {
            ensure!(note.len() <= 4096, "note exceeds 4096 bytes");
        }
        ensure!(self.locations.len() <= 32, "too many linked locations");
        for location in &self.locations {
            validate_session(&location.session)?;
            // Remembered hints keep the v1 contract, including retired identifiers.
            // Live native inputs and assignment targets use the strict validators below.
            ensure!(
                !location.workspace_id.is_empty() && location.workspace_id.len() <= 128,
                "invalid workspace ID"
            );
            if let Some(pane) = &location.pane_id {
                ensure!(
                    pane.starts_with(&format!("{}:p", location.workspace_id)) && pane.len() <= 160,
                    "pane ID must belong to the linked workspace"
                );
            }
        }
        ensure!(
            self.assignments.len() <= 16,
            "too many assignments in one workstream"
        );
        let mut ids = std::collections::HashSet::new();
        for assignment in &self.assignments {
            assignment.validate()?;
            ensure!(ids.insert(&assignment.id), "duplicate assignment ID");
        }
        Ok(())
    }
}

pub(crate) fn validate_id(id: &str) -> Result<()> {
    ensure!(
        !id.is_empty()
            && id.len() <= 64
            && id
                .bytes()
                .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-'),
        "id must contain 1–64 lowercase ASCII letters, digits, or hyphens"
    );
    Ok(())
}

pub fn validate_workspace_id(id: &str) -> Result<()> {
    ensure!(
        id.len() <= 128
            && id
                .strip_prefix('w')
                .is_some_and(|number| !number.is_empty()
                    && number.bytes().all(|b| b.is_ascii_alphanumeric())),
        "invalid workspace ID"
    );
    Ok(())
}

pub fn validate_tab_id(id: &str) -> Result<()> {
    let (workspace, number) = id.split_once(":t").context("invalid tab ID")?;
    validate_workspace_id(workspace)?;
    ensure!(
        !number.is_empty()
            && number.len() <= 32
            && number.bytes().all(|b| b.is_ascii_alphanumeric()),
        "invalid tab ID"
    );
    Ok(())
}

pub fn validate_pane_id(id: &str) -> Result<()> {
    let (workspace, number) = id.split_once(":p").context("invalid pane ID")?;
    validate_workspace_id(workspace)?;
    ensure!(
        !number.is_empty()
            && number.len() <= 32
            && number.bytes().all(|b| b.is_ascii_alphanumeric()),
        "invalid pane ID"
    );
    Ok(())
}

pub fn validate_session(name: &str) -> Result<()> {
    ensure!(
        !name.is_empty()
            && name.len() <= 64
            && name != "."
            && name != ".."
            && name
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'.' | b'-' | b'_')),
        "invalid local session name"
    );
    Ok(())
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Record {
    pub revision: u64,
    pub updated_at_unix: u64,
    pub workstream: Workstream,
}

#[cfg(test)]
mod tests {
    use super::*;

    pub fn sample(id: &str) -> Workstream {
        Workstream {
            id: id.into(),
            title: "Release".into(),
            purpose: "Ship it".into(),
            status: Status::Planned,
            next_action: None,
            blocker: None,
            locations: vec![],
            assignments: vec![],
        }
    }

    #[test]
    fn rejects_unsafe_ids_and_session_names() {
        for id in ["", "../foo", "Release", "a/b"] {
            assert!(sample(id).validate().is_err());
        }
        for name in ["", "../work", ".", "..", "ssh:host", "a b"] {
            assert!(validate_session(name).is_err());
        }
        assert!(validate_session("Work_2").is_ok());
    }

    #[test]
    fn validates_native_tab_ids() {
        for id in ["w1:t1", "wABC:t2", "w2:tXYZ"] {
            assert!(validate_tab_id(id).is_ok(), "{id}");
        }
        for id in [
            "",
            "--help",
            "w1",
            "w1:p1",
            "w1:t",
            "w1:t1:t2",
            "w1:t../x",
            "w1:t1 --focus",
        ] {
            assert!(validate_tab_id(id).is_err(), "{id}");
        }
        assert!(validate_tab_id(&format!("w1:t{}", "a".repeat(33))).is_err());
        assert!(validate_tab_id(&format!("w{}:t1", "a".repeat(128))).is_err());
    }

    #[test]
    fn remembered_hints_keep_legacy_bounds_without_loosening_live_ids() {
        let mut work = sample("legacy");
        work.locations.push(Location {
            session: "default".into(),
            workspace_id: "retired-workspace".into(),
            pane_id: None,
        });
        assert!(work.validate().is_ok());
        assert!(validate_workspace_id(&work.locations[0].workspace_id).is_err());
        work.locations[0].workspace_id = "w1".into();
        work.locations[0].pane_id = Some(format!("w1:p{}", "a".repeat(156)));
        assert!(work.validate().is_ok());
        assert!(validate_pane_id(work.locations[0].pane_id.as_ref().unwrap()).is_err());
        work.locations[0].pane_id.as_mut().unwrap().push('a');
        assert!(work.validate().is_err());
        work.locations[0].pane_id = None;
        for workspace in [String::new(), "a".repeat(129), "é".repeat(65)] {
            work.locations[0].workspace_id = workspace;
            assert!(work.validate().is_err());
        }
    }

    #[test]
    fn pane_must_match_workspace() {
        let mut work = sample("release");
        work.locations.push(Location {
            session: "default".into(),
            workspace_id: "w1".into(),
            pane_id: Some("w2:p1".into()),
        });
        assert!(work.validate().is_err());
    }
}
