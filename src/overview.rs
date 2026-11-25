use crate::herdr::{Agent, Snapshot};
use crate::model::{Location, Record};
use serde::Serialize;

#[derive(Debug, Serialize)]
pub struct Overview {
    pub inspected_session: String,
    pub session_error: Option<String>,
    pub workstreams: Vec<WorkstreamReport>,
    pub live_inventory: Option<Snapshot>,
    pub interpretation: &'static str,
}

#[derive(Debug, Serialize)]
pub struct WorkstreamReport {
    pub record: Record,
    pub locations: Vec<LocationReport>,
    pub unlinked: bool,
    pub recorded_blocker: Option<String>,
}

#[derive(Debug, Serialize)]
pub struct LocationReport {
    pub location: Location,
    #[serde(flatten)]
    pub observation: Observation,
}

#[derive(Debug, Serialize)]
#[serde(tag = "observation", rename_all = "snake_case")]
pub enum Observation {
    SessionNotInspected,
    SessionUnavailable {
        error: String,
    },
    WorkspaceMissing,
    WorkspacePresent {
        note: &'static str,
    },
    PaneMissing,
    PanePresent {
        terminal_id: String,
        agent: Option<Agent>,
        note: &'static str,
    },
}

/// Compare locations, never infer task progress from an agent's lifecycle state.
pub fn reconcile(
    records: Vec<Record>,
    session: String,
    snapshot: Result<Snapshot, String>,
) -> Overview {
    let (live_inventory, session_error) = match snapshot {
        Ok(snapshot) => (Some(snapshot), None),
        Err(error) => (None, Some(error)),
    };
    let workstreams = records
        .into_iter()
        .map(|record| {
            let locations = record
                .workstream
                .locations
                .iter()
                .map(|location| LocationReport {
                    location: location.clone(),
                    observation: observe(
                        location,
                        &session,
                        live_inventory.as_ref(),
                        session_error.as_deref(),
                    ),
                })
                .collect();
            WorkstreamReport {
                unlinked: record.workstream.locations.is_empty(),
                recorded_blocker: record.workstream.blocker.clone(),
                record,
                locations,
            }
        })
        .collect();
    Overview {
        inspected_session: session,
        session_error,
        workstreams,
        live_inventory,
        interpretation: "Unlinked does not mean unstarted. Idle/done does not prove task success. No neglected-work score or automatic status changes. Timestamps describe saved notes, not last human attention.",
    }
}

fn observe(
    location: &Location,
    inspected_session: &str,
    snapshot: Option<&Snapshot>,
    session_error: Option<&str>,
) -> Observation {
    if location.session != inspected_session {
        return Observation::SessionNotInspected;
    }
    let Some(snapshot) = snapshot else {
        return Observation::SessionUnavailable {
            error: session_error.unwrap_or("session unavailable").to_owned(),
        };
    };
    if !snapshot
        .workspaces
        .iter()
        .any(|workspace| workspace.workspace_id == location.workspace_id)
    {
        return Observation::WorkspaceMissing;
    }
    let Some(pane_id) = &location.pane_id else {
        return Observation::WorkspacePresent {
            note: "No particular pane or agent linked.",
        };
    };
    let Some(pane) = snapshot.panes.iter().find(|pane| &pane.pane_id == pane_id) else {
        return Observation::PaneMissing;
    };
    let agent = snapshot
        .agents
        .iter()
        .find(|agent| agent.pane_id == pane.pane_id && agent.terminal_id == pane.terminal_id)
        .cloned();
    Observation::PanePresent {
        terminal_id: pane.terminal_id.clone(),
        agent,
        note: "Location match only; not proof this occupant is doing the recorded work.",
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::{Status, Workstream};

    fn inventory() -> Snapshot {
        let response: serde_json::Value =
            serde_json::from_str(include_str!("../tests/fixtures/herdr-0.9.5-snapshot.json"))
                .unwrap();
        serde_json::from_value(response["result"]["snapshot"].clone()).unwrap()
    }

    fn record(session: &str, pane: Option<&str>) -> Record {
        Record {
            revision: 1,
            updated_at_unix: 1,
            workstream: Workstream {
                id: "release".into(),
                title: "Release".into(),
                purpose: String::new(),
                status: Status::Planned,
                next_action: None,
                blocker: None,
                assignments: vec![],
                locations: vec![Location {
                    session: session.into(),
                    workspace_id: "w1".into(),
                    pane_id: pane.map(str::to_owned),
                }],
            },
        }
    }

    #[test]
    fn unavailable_is_not_missing_or_unstarted() {
        let result = reconcile(
            vec![record("default", Some("w1:p1"))],
            "default".into(),
            Err("stopped".into()),
        );
        assert!(matches!(
            &result.workstreams[0].locations[0].observation,
            Observation::SessionUnavailable { error } if error == "stopped"
        ));
        assert_eq!(result.session_error.as_deref(), Some("stopped"));
        assert!(result.live_inventory.is_none());
        assert_eq!(
            result.workstreams[0].record.workstream.status,
            Status::Planned
        );
    }

    #[test]
    fn never_claims_to_observe_another_session() {
        let result = reconcile(
            vec![record("other", None)],
            "default".into(),
            Err("stopped".into()),
        );
        assert!(matches!(
            result.workstreams[0].locations[0].observation,
            Observation::SessionNotInspected
        ));
    }

    #[test]
    fn distinguishes_workspace_and_pane_missing() {
        let mut missing_workspace = inventory();
        missing_workspace.workspaces.clear();
        for (snapshot, expected_workspace_missing) in
            [(missing_workspace, true), (inventory(), false)]
        {
            let result = reconcile(
                vec![record("default", Some("w1:p2"))],
                "default".into(),
                Ok(snapshot),
            );
            let observation = &result.workstreams[0].locations[0].observation;
            if expected_workspace_missing {
                assert!(matches!(observation, Observation::WorkspaceMissing));
            } else {
                assert!(matches!(observation, Observation::PaneMissing));
            }
            assert_eq!(
                result.workstreams[0].record.workstream.status,
                Status::Planned
            );
        }
    }

    #[test]
    fn workspace_only_link_does_not_imply_an_agent() {
        let result = reconcile(
            vec![record("default", None)],
            "default".into(),
            Ok(inventory()),
        );
        assert!(matches!(
            result.workstreams[0].locations[0].observation,
            Observation::WorkspacePresent { .. }
        ));
    }

    #[test]
    fn unlinked_work_remains_explicit_and_keeps_its_notes() {
        let mut work = record("default", None);
        work.workstream.locations.clear();
        work.workstream.blocker = Some("Needs a decision".into());
        let result = reconcile(vec![work], "default".into(), Ok(inventory()));
        let report = &result.workstreams[0];
        assert!(report.unlinked);
        assert!(report.locations.is_empty());
        assert_eq!(report.recorded_blocker.as_deref(), Some("Needs a decision"));
        assert_eq!(report.record.workstream.status, Status::Planned);
    }

    #[test]
    fn lifecycle_states_do_not_rewrite_workstream_intent() {
        for status in ["working", "blocked", "idle", "done", "unknown"] {
            let mut snapshot = inventory();
            snapshot.agents[0].agent_status = status.into();
            let result = reconcile(
                vec![record("default", Some("w1:p1"))],
                "default".into(),
                Ok(snapshot),
            );
            assert!(matches!(
                &result.workstreams[0].locations[0].observation,
                Observation::PanePresent { agent: Some(agent), .. } if agent.agent_status == status
            ));
            assert_eq!(
                result.workstreams[0].record.workstream.status,
                Status::Planned
            );
        }
    }

    #[test]
    fn ignores_agent_facts_for_a_different_terminal_occupant() {
        let mut snapshot = inventory();
        snapshot.agents[0].terminal_id = "term_old".into();
        let result = reconcile(
            vec![record("default", Some("w1:p1"))],
            "default".into(),
            Ok(snapshot),
        );
        assert!(matches!(
            result.workstreams[0].locations[0].observation,
            Observation::PanePresent { agent: None, .. }
        ));
    }

    #[test]
    fn serializes_the_public_observation_contract() {
        let result = reconcile(
            vec![record("default", Some("w1:p1"))],
            "default".into(),
            Ok(inventory()),
        );
        let value = serde_json::to_value(result).unwrap();
        let location = &value["workstreams"][0]["locations"][0];
        assert_eq!(location["observation"], "pane_present");
        assert_eq!(location["agent"]["agent_status"], "blocked");
        assert_eq!(location["location"]["pane_id"], "w1:p1");
        assert!(value["session_error"].is_null());
    }
}
