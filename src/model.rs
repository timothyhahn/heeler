use anyhow::{Result, ensure};
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
}

impl Workstream {
    pub fn validate(&self) -> Result<()> {
        ensure!(
            !self.id.is_empty()
                && self.id.len() <= 64
                && self
                    .id
                    .bytes()
                    .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-'),
            "id must contain 1–64 lowercase ASCII letters, digits, or hyphens"
        );
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
        Ok(())
    }
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
