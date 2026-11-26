use crate::assignment::{Assignment, Delivery, Role};
use crate::model::{Location, Record, Status, Workstream, validate_id};
use anyhow::{Context, Result, ensure};
use schemars::JsonSchema;
use serde::{Deserialize, Deserializer, Serialize};

/// Read-only projection, deliberately not a writable Workstream/Record shape.
#[derive(Debug, Serialize)]
pub struct RecordSummary {
    pub workstream_id: String,
    pub revision: u64,
    pub updated_at_unix: u64,
    pub title: String,
    pub status: Status,
    pub next_action: Option<String>,
    pub blocker: Option<String>,
    pub locations: Vec<Location>,
    pub assignment_count: usize,
    pub assignments: Vec<AssignmentHeader>,
}

#[derive(Debug, Serialize)]
pub struct AssignmentHeader {
    pub id: String,
    pub role: Role,
    pub delivery: Delivery,
    pub has_outcome: bool,
}

impl From<&Record> for RecordSummary {
    fn from(record: &Record) -> Self {
        let work = &record.workstream;
        Self {
            workstream_id: work.id.clone(),
            revision: record.revision,
            updated_at_unix: record.updated_at_unix,
            title: work.title.clone(),
            status: work.status,
            next_action: work.next_action.clone(),
            blocker: work.blocker.clone(),
            locations: work.locations.clone(),
            assignment_count: work.assignments.len(),
            assignments: work
                .assignments
                .iter()
                .map(|assignment| AssignmentHeader {
                    id: assignment.id.clone(),
                    role: assignment.role,
                    delivery: assignment.delivery,
                    has_outcome: assignment.outcome.is_some(),
                })
                .collect(),
        }
    }
}

#[derive(Debug, Deserialize, JsonSchema)]
#[serde(tag = "type", rename_all = "snake_case", deny_unknown_fields)]
pub enum TextChange {
    Set { value: String },
    Clear {},
}

impl TextChange {
    fn apply(self, target: &mut Option<String>) -> Result<()> {
        *target = match self {
            Self::Set { value } => {
                ensure!(
                    !value.trim().is_empty() && !value.contains('\0'),
                    "set text must be nonblank and contain no NUL; use clear to remove it"
                );
                Some(value)
            }
            Self::Clear {} => None,
        };
        Ok(())
    }
}

// Omission means unchanged; explicit null must not silently become omission.
fn present<'de, D, T>(deserializer: D) -> std::result::Result<Option<T>, D::Error>
where
    D: Deserializer<'de>,
    T: Deserialize<'de>,
{
    T::deserialize(deserializer).map(Some)
}

#[derive(Debug, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct AssignmentOutcome {
    pub assignment_id: String,
    pub outcome: TextChange,
}

#[derive(Debug, Deserialize, JsonSchema)]
#[serde(tag = "type", rename_all = "snake_case", deny_unknown_fields)]
pub enum WorkstreamChange {
    Notes {
        #[serde(default, deserialize_with = "present")]
        #[schemars(with = "Status", skip_serializing_if = "Option::is_none")]
        status: Option<Status>,
        #[serde(default, deserialize_with = "present")]
        #[schemars(with = "TextChange", skip_serializing_if = "Option::is_none")]
        next_action: Option<TextChange>,
        #[serde(default, deserialize_with = "present")]
        #[schemars(with = "TextChange", skip_serializing_if = "Option::is_none")]
        blocker: Option<TextChange>,
    },
    AppendAssignments {
        assignments: Vec<Assignment>,
    },
    Outcomes {
        outcomes: Vec<AssignmentOutcome>,
    },
}

impl WorkstreamChange {
    /// Applies to an unsaved candidate; the store validates and commits it as one change.
    pub(crate) fn apply(self, work: &mut Workstream) -> Result<()> {
        match self {
            Self::Notes {
                status,
                next_action,
                blocker,
            } => {
                ensure!(
                    status.is_some() || next_action.is_some() || blocker.is_some(),
                    "notes change must specify at least one field"
                );
                if let Some(status) = status {
                    work.status = status;
                }
                if let Some(next_action) = next_action {
                    next_action.apply(&mut work.next_action)?;
                }
                if let Some(blocker) = blocker {
                    blocker.apply(&mut work.blocker)?;
                }
            }
            Self::AppendAssignments { assignments } => {
                ensure!(
                    (1..=4).contains(&assignments.len()),
                    "append 1–4 new prepared assignments"
                );
                for assignment in &assignments {
                    ensure!(
                        assignment.delivery == Delivery::Prepared && assignment.outcome.is_none(),
                        "new assignments must be prepared without an outcome"
                    );
                    assignment.validate()?;
                }
                work.assignments.extend(assignments);
            }
            Self::Outcomes { outcomes } => {
                ensure!(
                    (1..=4).contains(&outcomes.len()),
                    "set/clear 1–4 assignment outcomes"
                );
                let mut ids = std::collections::HashSet::new();
                for change in outcomes {
                    validate_id(&change.assignment_id)?;
                    ensure!(
                        ids.insert(change.assignment_id.clone()),
                        "duplicate assignment ID in outcomes"
                    );
                    let assignment = work
                        .assignments
                        .iter_mut()
                        .find(|a| a.id == change.assignment_id)
                        .context("assignment not found")?;
                    change.outcome.apply(&mut assignment.outcome)?;
                }
            }
        }
        work.validate()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn typed_changes_reject_null_ambiguous_and_unsupported_fields() {
        for value in [
            json!({"type": "notes", "status": null}),
            json!({"type": "notes", "next_action": null}),
            json!({"type": "notes", "blocker": null}),
            json!({"type": "notes", "status": "complete"}),
            json!({"type": "notes", "delivery": "prepared"}),
            json!({"type": "notes", "next_action": {"type": "clear", "value": "ambiguous"}}),
            json!({"type": "notes", "blocker": {"type": "set"}}),
            json!({"type": "notes", "blocker": {"type": "set", "value": null}}),
            json!({"type": "outcomes", "outcomes": [{"assignment_id": "review"}]}),
            json!({"type": "outcomes", "outcomes": [{"assignment_id": "review", "outcome": null}]}),
            json!({"type": "append_assignments", "assignments": [], "replace": true}),
            json!({"type": "replace", "workstream": {}}),
        ] {
            assert!(
                serde_json::from_value::<WorkstreamChange>(value.clone()).is_err(),
                "{value}"
            );
        }
        let value = json!({"type": "notes", "next_action": {"type": "clear"}});
        assert!(matches!(
            serde_json::from_value::<WorkstreamChange>(value).unwrap(),
            WorkstreamChange::Notes {
                status: None,
                next_action: Some(TextChange::Clear {}),
                blocker: None
            }
        ));
    }

    #[test]
    fn notes_schema_keeps_fields_optional_without_suggesting_null_defaults() {
        let schema = serde_json::to_value(schemars::schema_for!(WorkstreamChange)).unwrap();
        let notes = &schema["oneOf"][0];
        assert_eq!(notes["required"], json!(["type"]));
        for field in ["status", "next_action", "blocker"] {
            assert!(notes["properties"][field].get("default").is_none());
            assert!(notes["properties"][field].get("$ref").is_some());
        }
        assert_eq!(schema["$defs"]["Status"]["type"], "string");
    }

    #[test]
    fn summary_is_explicitly_not_a_writable_record_or_workstream() {
        let record = Record {
            revision: 7,
            updated_at_unix: 1,
            workstream: Workstream {
                id: "feature".into(),
                title: "Feature".into(),
                purpose: "Long intent".into(),
                status: Status::Paused,
                next_action: None,
                blocker: Some("Input needed".into()),
                locations: vec![],
                assignments: vec![],
            },
        };
        let summary = serde_json::to_value(RecordSummary::from(&record)).unwrap();
        assert_eq!(summary["revision"], 7);
        assert_eq!(summary["workstream_id"], "feature");
        assert_eq!(summary["assignment_count"], 0);
        assert!(summary.get("purpose").is_none());
        assert!(summary.get("workstream").is_none());
        assert!(serde_json::from_value::<Record>(summary.clone()).is_err());
        assert!(serde_json::from_value::<Workstream>(summary).is_err());
    }
}
