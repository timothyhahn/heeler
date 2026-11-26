use crate::herdr::{AgentHandle, AgentTarget, Herdr, validate_prompt_text};
use crate::model::{Record, validate_id};
use crate::store::Store;
use anyhow::{Context, Result, ensure};
use schemars::JsonSchema;
use serde::{Deserialize, Serialize};
use std::path::Path;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(rename_all = "snake_case")]
pub enum Role {
    Implementation,
    Review,
}

#[derive(Debug, Default, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(rename_all = "snake_case")]
pub enum Delivery {
    #[default]
    Prepared,
    /// An attempt was recorded; input may or may not have arrived. Inspect first.
    Uncertain,
    /// Herdr acknowledged submission, not execution or completion.
    Submitted,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct Assignment {
    pub id: String,
    pub role: Role,
    pub target: AgentTarget,
    pub brief: String,
    #[serde(default)]
    pub constraints: Vec<String>,
    pub acceptance_criteria: Vec<String>,
    /// Absolute local path where the agent should write its report. Heeler does not read it implicitly.
    pub report_path: String,
    #[serde(default)]
    pub delivery: Delivery,
    pub outcome: Option<String>,
}

impl Assignment {
    pub fn validate(&self) -> Result<()> {
        validate_id(&self.id)?;
        self.target.validate()?;
        ensure!(
            !self.brief.trim().is_empty() && self.brief.len() <= 8192 && !self.brief.contains('\0'),
            "assignment brief must contain 1–8192 bytes without NUL"
        );
        ensure!(
            !self.acceptance_criteria.is_empty() && self.acceptance_criteria.len() <= 16,
            "provide 1–16 acceptance criteria"
        );
        ensure!(
            self.constraints.len() <= 16,
            "too many assignment constraints"
        );
        for note in self.constraints.iter().chain(&self.acceptance_criteria) {
            ensure!(
                !note.trim().is_empty() && note.len() <= 1024 && !note.contains('\0'),
                "assignment criteria and constraints must contain 1–1024 bytes without NUL"
            );
        }
        ensure!(
            Path::new(&self.report_path).is_absolute()
                && self.report_path.len() <= 4096
                && !self.report_path.chars().any(char::is_control),
            "report path must be absolute and contain no control characters"
        );
        if let Some(outcome) = &self.outcome {
            ensure!(outcome.len() <= 8192, "assignment outcome is too large");
        }
        Ok(())
    }

    pub fn prompt(&self) -> String {
        let role = match self.role {
            Role::Implementation => "Implementation",
            Role::Review => "Review",
        };
        let mut text = format!(
            "# {role}: {}\n\n{}\n\nRead the repository's AGENTS.md before working.\n",
            self.id, self.brief
        );
        if self.role == Role::Review {
            text.push_str("Do not modify project/source files. Writing the requested review report is allowed.\n");
        }
        text.push_str("Stay within this brief. Do not commit, push, or change unrelated work unless explicitly asked.\n");
        text.push_str("\n## Constraints\n");
        for constraint in &self.constraints {
            text.push_str(&format!("- {constraint}\n"));
        }
        text.push_str("\n## Acceptance criteria\n");
        for criterion in &self.acceptance_criteria {
            text.push_str(&format!("- {criterion}\n"));
        }
        text.push_str(&format!("\n## Report\nWrite a concise report to:\n{}\nInclude changes or findings, checks actually run with results, and unresolved blockers. If blocked, ask for input instead of guessing or approving permission dialogs.\n", self.report_path));
        text
    }
}

#[derive(Debug, Serialize)]
pub struct DispatchReceipt {
    pub assignment_id: String,
    pub delivery: Delivery,
    pub agent: AgentHandle,
    pub record: Option<Record>,
    /// Submission succeeded even if a competing edit prevented the final note update.
    pub record_error: Option<String>,
}

/// The lead chooses when to send; this is not a scheduler or a completion tracker.
pub async fn send(
    store: Store,
    herdr: &Herdr,
    workstream_id: String,
    assignment_id: String,
    expected_revision: u64,
) -> Result<DispatchReceipt> {
    validate_id(&workstream_id)?;
    validate_id(&assignment_id)?;
    let read_store = store.clone();
    let mut record = tokio::task::spawn_blocking(move || read_store.get(&workstream_id)).await??;
    ensure!(
        record.revision == expected_revision,
        "revision conflict; reread the workstream before sending"
    );
    let index = record
        .workstream
        .assignments
        .iter()
        .position(|assignment| assignment.id == assignment_id)
        .context("assignment not found")?;
    let assignment = record.workstream.assignments[index].clone();
    ensure!(
        assignment.delivery == Delivery::Prepared,
        "assignment is already submitted or uncertain; inspect it instead of resending"
    );
    let text = assignment.prompt();
    validate_prompt_text(&text)?;
    let current = herdr.checked_agent(&assignment.target).await?;
    ensure!(
        current.agent.ready_for_prompt(),
        "agent is not ready for a new assignment; inspect it or wait for the human"
    );

    // Reserve the attempt before terminal input. Cancellation leaves an honest unknown.
    record.workstream.assignments[index].delivery = Delivery::Uncertain;
    let write_store = store.clone();
    let attempt =
        tokio::task::spawn_blocking(move || write_store.save(record.workstream, expected_revision))
            .await??;
    let agent = herdr.prompt_agent(&current.target, text).await
        .with_context(|| format!("assignment {assignment_id} attempt recorded as uncertain at revision {}; reread and inspect before retrying", attempt.revision))?;

    let mut workstream = attempt.workstream;
    workstream.assignments[index].delivery = Delivery::Submitted;
    let saved = tokio::task::spawn_blocking(move || store.save(workstream, attempt.revision)).await;
    let (record, record_error) = match saved {
        Ok(Ok(record)) => (Some(record), None),
        Ok(Err(error)) => (
            None,
            Some(format!(
                "Submission acknowledged; do not resend. Could not update notes: {error:#}"
            )),
        ),
        Err(error) => (
            None,
            Some(format!(
                "Submission acknowledged; do not resend. Note writer failed: {error}"
            )),
        ),
    };
    Ok(DispatchReceipt {
        assignment_id,
        delivery: Delivery::Submitted,
        agent,
        record,
        record_error,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn assignment() -> Assignment {
        Assignment {
            id: "review".into(),
            role: Role::Review,
            target: AgentTarget {
                session: "default".into(),
                pane_id: "w1:p1".into(),
                terminal_id: "term_example".into(),
                agent: "pi".into(),
                agent_session: None,
            },
            brief: "Review the change".into(),
            constraints: vec!["Keep the report short".into()],
            acceptance_criteria: vec!["Cite concrete findings".into()],
            report_path: std::env::temp_dir()
                .join("example-review.md")
                .to_string_lossy()
                .into_owned(),
            delivery: Delivery::Prepared,
            outcome: None,
        }
    }

    #[test]
    fn review_briefs_allow_a_report_but_not_source_edits() {
        let assignment = assignment();
        assignment.validate().unwrap();
        let prompt = assignment.prompt();
        assert!(prompt.contains("Do not modify project/source files"));
        assert!(prompt.contains("Writing the requested review report is allowed"));
        assert!(prompt.contains("Cite concrete findings"));
        assert!(prompt.contains(&assignment.report_path));
    }

    #[test]
    fn stable_fields_allow_near_and_over_budget_history_but_live_text_is_bounded() {
        let mut handoff = assignment();
        handoff.brief = "x".repeat(8192);
        handoff.constraints = vec!["x".repeat(1024); 16];
        handoff.acceptance_criteria = vec!["y".repeat(1024); 7];
        handoff.acceptance_criteria.push("z".into());
        let remaining = 32_768 - handoff.prompt().len();
        handoff
            .acceptance_criteria
            .last_mut()
            .unwrap()
            .push_str(&"z".repeat(remaining));
        handoff.validate().unwrap();
        assert_eq!(handoff.prompt().len(), 32_768);
        validate_prompt_text(&handoff.prompt()).unwrap();
        handoff.acceptance_criteria.last_mut().unwrap().push('z');
        handoff.validate().unwrap();
        assert_eq!(handoff.prompt().len(), 32_769);
        assert!(validate_prompt_text(&handoff.prompt()).is_err());
    }

    #[tokio::test]
    async fn invalid_live_assignments_leave_prepared_bytes_and_revision_unchanged() {
        let dir = tempfile::tempdir().unwrap();
        let herdr = Herdr::new(dir.path().join("must-not-run"));
        let store = Store::new(dir.path().join("state"));
        for field in ["brief", "constraint", "criterion", "size"] {
            let mut handoff = assignment();
            let control = "End paste\u{1b}[201~\u{3}".into();
            match field {
                "brief" => handoff.brief = control,
                "constraint" => handoff.constraints.push(control),
                "criterion" => handoff.acceptance_criteria.push(control),
                _ => {
                    handoff.constraints = vec!["x".repeat(1024); 16];
                    handoff.acceptance_criteria = vec!["y".repeat(1024); 16];
                }
            }
            let work = crate::model::Workstream {
                id: field.into(),
                title: "Synthetic".into(),
                purpose: String::new(),
                status: crate::model::Status::Active,
                next_action: None,
                blocker: None,
                locations: vec![],
                assignments: vec![handoff],
            };
            store.save(work, 0).unwrap();
            let path = dir.path().join("state/workstreams.json");
            let before = std::fs::read(&path).unwrap();
            let error = send(store.clone(), &herdr, field.into(), "review".into(), 1)
                .await
                .unwrap_err();
            assert!(error.to_string().contains("UTF-8 bytes"), "{error:#}");
            assert_eq!(std::fs::read(path).unwrap(), before);
            let saved = store.get(field).unwrap();
            assert_eq!(saved.revision, 1);
            assert_eq!(saved.workstream.assignments[0].delivery, Delivery::Prepared);
        }
    }

    #[test]
    fn rejects_incomplete_and_oversized_handoffs() {
        let mut handoff = assignment();
        handoff.report_path = "relative/report.md".into();
        assert!(handoff.validate().is_err());
        let mut handoff = assignment();
        handoff.acceptance_criteria.clear();
        assert!(handoff.validate().is_err());
        let mut handoff = assignment();
        handoff.brief = "x".repeat(8193);
        assert!(handoff.validate().is_err());
        let mut handoff = assignment();
        handoff.target.pane_id = "--current".into();
        assert!(handoff.validate().is_err());
        for pane_id in [
            "retired-workspace:p1".into(),
            format!("w1:p{}", "a".repeat(33)),
        ] {
            let mut handoff = assignment();
            handoff.target.pane_id = pane_id;
            assert!(handoff.validate().is_err());
        }
    }
}
