use crate::assignment::{Assignment, Delivery};
use crate::model::{Record, validate_id};
use crate::store::Store;
use anyhow::{Context, Result, ensure};
use serde::Serialize;
use std::sync::Arc;
use std::time::Duration;
use tokio::task::JoinHandle;
use tokio::time::Instant;

const POLL_INTERVAL: Duration = Duration::from_millis(500);

#[derive(Debug, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum OutcomeResult {
    Reported,
    Pending,
    Timeout,
}

#[derive(Debug, Serialize)]
pub struct OutcomeSnapshot {
    pub revision: u64,
    pub delivery: Delivery,
    pub reported_text: Option<String>,
    pub observed_elapsed_ms: u64,
}

#[derive(Debug, Serialize)]
pub struct OutcomeObservation {
    pub workstream_id: String,
    pub assignment_id: String,
    pub result: OutcomeResult,
    pub snapshot: Option<OutcomeSnapshot>,
    pub wait_ms: u64,
    pub elapsed_ms: u64,
    pub untrusted: bool,
    pub verified: bool,
}

struct CompletedRead {
    at: Instant,
    record: Result<Record>,
}

// Dropping the request can abort queued work, not an already running filesystem read.
struct ReadTask(JoinHandle<CompletedRead>);
impl Drop for ReadTask {
    fn drop(&mut self) {
        self.0.abort();
    }
}

fn definition_matches(left: &Assignment, right: &Assignment) -> bool {
    left.id == right.id
        && left.role == right.role
        && left.target == right.target
        && left.brief == right.brief
        && left.constraints == right.constraints
        && left.acceptance_criteria == right.acceptance_criteria
        && left.report_path == right.report_path
}

fn delivery_rank(delivery: Delivery) -> u8 {
    match delivery {
        Delivery::Prepared => 0,
        Delivery::Uncertain => 1,
        Delivery::Submitted => 2,
    }
}

#[derive(Default)]
struct ObservedState {
    previous: Option<Record>,
}
impl ObservedState {
    fn accept(
        &mut self,
        record: Record,
        assignment_id: &str,
        at: Instant,
        started: Instant,
    ) -> Result<OutcomeSnapshot> {
        let assignment = record
            .workstream
            .assignments
            .iter()
            .find(|a| a.id == assignment_id)
            .context("assignment not found or removed")?;
        if let Some(previous) = &self.previous {
            ensure!(
                record.revision >= previous.revision,
                "workstream replaced: revision regressed"
            );
            ensure!(
                record.revision != previous.revision
                    || (record.updated_at_unix == previous.updated_at_unix
                        && record.workstream == previous.workstream),
                "workstream replaced: content changed at the same revision"
            );
            let old = previous
                .workstream
                .assignments
                .iter()
                .find(|a| a.id == assignment_id)
                .context("assignment binding unavailable")?;
            ensure!(
                definition_matches(old, assignment),
                "assignment definition replaced while waiting"
            );
            ensure!(
                delivery_rank(assignment.delivery) >= delivery_rank(old.delivery),
                "assignment delivery regressed while waiting"
            );
        }
        let snapshot = OutcomeSnapshot {
            revision: record.revision,
            delivery: assignment.delivery,
            reported_text: assignment.outcome.clone(),
            observed_elapsed_ms: millis(at.saturating_duration_since(started)),
        };
        self.previous = Some(record);
        Ok(snapshot)
    }
}

fn millis(duration: Duration) -> u64 {
    duration.as_millis().min(u64::MAX as u128) as u64
}

pub async fn read(
    store: Store,
    workstream_id: String,
    assignment_id: String,
    wait_ms: u64,
) -> Result<OutcomeObservation> {
    let id = workstream_id.clone();
    observe(workstream_id, assignment_id, wait_ms, move || {
        store.get(&id)
    })
    .await
}

// Private reader seam keeps gated/deadline tests independent of filesystem speed.
// Production supplies only Store::get; no callback or file path is exposed by MCP.
async fn observe<F>(
    workstream_id: String,
    assignment_id: String,
    wait_ms: u64,
    reader: F,
) -> Result<OutcomeObservation>
where
    F: Fn() -> Result<Record> + Send + Sync + 'static,
{
    validate_id(&workstream_id)?;
    validate_id(&assignment_id)?;
    ensure!(wait_ms <= 60_000, "wait_ms must be 0–60000");
    let started = Instant::now();
    let deadline = (wait_ms > 0).then(|| started + Duration::from_millis(wait_ms));
    let reader = Arc::new(reader);
    let mut observed = ObservedState::default();
    let mut last = None;
    let finish = |result, snapshot| OutcomeObservation {
        workstream_id: workstream_id.clone(),
        assignment_id: assignment_id.clone(),
        result,
        snapshot,
        wait_ms,
        elapsed_ms: millis(started.elapsed()),
        untrusted: true,
        verified: false,
    };
    loop {
        // Do not enqueue another read after the positive budget has expired.
        if deadline.is_some_and(|limit| Instant::now() >= limit) {
            return Ok(finish(OutcomeResult::Timeout, last));
        }
        let read = Arc::clone(&reader);
        let mut task = ReadTask(tokio::task::spawn_blocking(move || {
            let record = read();
            CompletedRead {
                at: Instant::now(),
                record,
            }
        }));
        let completed = if let Some(limit) = deadline {
            tokio::select! {
                biased;
                // Completed errors win over a simultaneously ready timeout.
                result = &mut task.0 => result.context("outcome read task failed")?,
                _ = tokio::time::sleep_until(limit) => return Ok(finish(OutcomeResult::Timeout, last)),
            }
        } else {
            (&mut task.0).await.context("outcome read task failed")?
        };
        let snapshot = observed.accept(completed.record?, &assignment_id, completed.at, started)?;
        if deadline.is_some_and(|limit| completed.at > limit) {
            // A late successful read is not an eligible report or a current snapshot.
            return Ok(finish(OutcomeResult::Timeout, last));
        }
        let reported = snapshot.reported_text.is_some();
        last = Some(snapshot);
        if reported {
            return Ok(finish(OutcomeResult::Reported, last));
        }
        let Some(limit) = deadline else {
            return Ok(finish(OutcomeResult::Pending, last));
        };
        tokio::time::sleep_until((completed.at + POLL_INTERVAL).min(limit)).await;
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::sync::{
        Mutex,
        atomic::{AtomicUsize, Ordering},
        mpsc,
    };

    fn record(text: Option<&str>, delivery: Delivery, revision: u64) -> Record {
        serde_json::from_value(json!({"revision": revision, "updated_at_unix": revision,
            "workstream": {"id": "feature", "title": "Feature", "purpose": "Preserve intent", "status": "active",
                "next_action": null, "blocker": null, "locations": [], "assignments": [{
                    "id": "review", "role": "review", "target": {"session": "default", "pane_id": "w1:p1", "terminal_id": "term_test", "agent": "pi", "agent_session": null},
                    "brief": "Historical brief", "constraints": ["Keep this"], "acceptance_criteria": ["Report findings"],
                    "report_path": "/tmp/synthetic-report.md", "delivery": delivery, "outcome": text
                }]}})).unwrap()
    }

    #[tokio::test]
    async fn immediate_reads_once_and_preserves_raw_claims_and_delivery() {
        for delivery in [Delivery::Prepared, Delivery::Uncertain, Delivery::Submitted] {
            for text in [
                None,
                Some(""),
                Some("odd\0legacy"),
                Some("CLAIM complete\nChecks: passed"),
                Some("CLAIM blocked\nNeeds: human input"),
            ] {
                let calls = Arc::new(AtomicUsize::new(0));
                let counter = Arc::clone(&calls);
                let expected = record(text, delivery, 3);
                let response = observe("feature".into(), "review".into(), 0, move || {
                    counter.fetch_add(1, Ordering::SeqCst);
                    Ok(expected.clone())
                })
                .await
                .unwrap();
                assert_eq!(calls.load(Ordering::SeqCst), 1);
                assert_eq!(
                    response.result,
                    if text.is_some() {
                        OutcomeResult::Reported
                    } else {
                        OutcomeResult::Pending
                    }
                );
                let snapshot = response.snapshot.unwrap();
                assert_eq!(snapshot.reported_text.as_deref(), text);
                assert_eq!(snapshot.delivery, delivery);
                assert!(response.untrusted && !response.verified);
            }
        }
    }

    #[tokio::test]
    async fn invalid_ids_and_wait_budget_start_no_reads() {
        let calls = Arc::new(AtomicUsize::new(0));
        for (work, assignment, wait) in [
            ("../bad", "review", 0),
            ("feature", "", 0),
            ("feature", "review", 60001),
        ] {
            let counter = Arc::clone(&calls);
            assert!(
                observe(work.into(), assignment.into(), wait, move || {
                    counter.fetch_add(1, Ordering::SeqCst);
                    Ok(record(None, Delivery::Prepared, 1))
                })
                .await
                .is_err()
            );
        }
        assert_eq!(calls.load(Ordering::SeqCst), 0);
        let response = observe("feature".into(), "review".into(), 60000, || {
            Ok(record(Some("Raw claim"), Delivery::Prepared, 1))
        })
        .await
        .unwrap();
        assert_eq!(response.result, OutcomeResult::Reported);
    }

    #[test]
    fn observed_binding_revision_and_delivery_replacements_are_errors() {
        let before = record(None, Delivery::Uncertain, 5);
        let mut invalid = Vec::new();
        let mut changed = before.clone();
        changed.revision = 4;
        invalid.push(changed);
        let mut changed = before.clone();
        changed.workstream.next_action = Some("same revision edit".into());
        invalid.push(changed);
        let mut changed = before.clone();
        changed.updated_at_unix += 1;
        invalid.push(changed);
        let mut changed = before.clone();
        changed.revision += 1;
        changed.workstream.assignments.clear();
        invalid.push(changed);
        let mut changed = before.clone();
        changed.revision += 1;
        changed.workstream.assignments[0].delivery = Delivery::Prepared;
        invalid.push(changed);
        for field in [
            "role",
            "target",
            "brief",
            "constraints",
            "acceptance_criteria",
            "report_path",
        ] {
            let mut changed = before.clone();
            changed.revision += 1;
            let a = &mut changed.workstream.assignments[0];
            match field {
                "role" => a.role = crate::assignment::Role::Implementation,
                "target" => a.target.terminal_id = "term_other".into(),
                "brief" => a.brief.push_str(" changed"),
                "constraints" => a.constraints.push("changed".into()),
                "acceptance_criteria" => a.acceptance_criteria.push("changed".into()),
                _ => a.report_path.push_str(".new"),
            }
            invalid.push(changed);
        }
        let start = Instant::now();
        for changed in invalid {
            let mut state = ObservedState::default();
            state
                .accept(before.clone(), "review", start, start)
                .unwrap();
            assert!(state.accept(changed, "review", start, start).is_err());
        }
        let mut state = ObservedState::default();
        state
            .accept(before.clone(), "review", start, start)
            .unwrap();
        let mut after = before;
        after.revision += 1;
        after.workstream.blocker = Some("unrelated update".into());
        after.workstream.assignments[0].delivery = Delivery::Submitted;
        after.workstream.assignments[0].outcome = Some("CLAIM blocked".into());
        assert_eq!(
            state
                .accept(after, "review", start, start)
                .unwrap()
                .reported_text
                .as_deref(),
            Some("CLAIM blocked")
        );
    }

    #[tokio::test]
    async fn positive_wait_observes_later_publication_after_one_poll_interval() {
        let calls = Arc::new(AtomicUsize::new(0));
        let counter = Arc::clone(&calls);
        let result = observe("feature".into(), "review".into(), 2000, move || {
            let index = counter.fetch_add(1, Ordering::SeqCst);
            Ok(record(
                if index == 0 {
                    None
                } else {
                    Some("CLAIM complete")
                },
                Delivery::Submitted,
                index as u64 + 1,
            ))
        })
        .await
        .unwrap();
        assert_eq!(result.result, OutcomeResult::Reported);
        assert_eq!(calls.load(Ordering::SeqCst), 2);
        assert!(result.elapsed_ms >= 500);
    }

    #[tokio::test]
    async fn timeout_retains_only_last_completed_snapshot_and_starts_no_more_reads() {
        let calls = Arc::new(AtomicUsize::new(0));
        let counter = Arc::clone(&calls);
        let response = observe("feature".into(), "review".into(), 20, move || {
            counter.fetch_add(1, Ordering::SeqCst);
            Ok(record(None, Delivery::Uncertain, 9))
        })
        .await
        .unwrap();
        assert_eq!(response.result, OutcomeResult::Timeout);
        assert_eq!(response.snapshot.unwrap().revision, 9);
        tokio::time::sleep(Duration::from_millis(30)).await;
        assert_eq!(calls.load(Ordering::SeqCst), 1);
    }

    async fn entered(receiver: mpsc::Receiver<()>) {
        tokio::task::spawn_blocking(move || receiver.recv_timeout(Duration::from_secs(2)).unwrap())
            .await
            .unwrap();
    }

    #[tokio::test]
    async fn timeout_without_initial_read_allows_only_running_readonly_finish() {
        let (entered_tx, entered_rx) = mpsc::channel();
        let (release_tx, release_rx) = mpsc::channel();
        let release_rx = Mutex::new(release_rx);
        let calls = Arc::new(AtomicUsize::new(0));
        let counter = Arc::clone(&calls);
        let task = tokio::spawn(observe("feature".into(), "review".into(), 100, move || {
            counter.fetch_add(1, Ordering::SeqCst);
            entered_tx.send(()).unwrap();
            release_rx.lock().unwrap().recv().unwrap();
            Ok(record(Some("late claim"), Delivery::Submitted, 1))
        }));
        entered(entered_rx).await;
        let result = task.await.unwrap().unwrap();
        assert_eq!(result.result, OutcomeResult::Timeout);
        assert!(result.snapshot.is_none());
        release_tx.send(()).unwrap();
        tokio::time::sleep(Duration::from_millis(30)).await;
        assert_eq!(calls.load(Ordering::SeqCst), 1);
    }

    #[tokio::test]
    async fn completed_errors_win_over_ready_deadline_and_late_reports_are_not_accepted() {
        for fail in [true, false] {
            let (entered_tx, entered_rx) = mpsc::channel();
            let (release_tx, release_rx) = mpsc::channel();
            let release_rx = Mutex::new(release_rx);
            let task = tokio::spawn(observe("feature".into(), "review".into(), 30, move || {
                entered_tx.send(()).unwrap();
                release_rx.lock().unwrap().recv().unwrap();
                if fail {
                    anyhow::bail!("completed store error")
                }
                Ok(record(Some("late claim"), Delivery::Submitted, 1))
            }));
            entered(entered_rx).await;
            // Deliberately prevent the single-thread async executor from polling
            // until both completion and deadline are ready, using a bounded gate.
            std::thread::sleep(Duration::from_millis(40));
            release_tx.send(()).unwrap();
            std::thread::sleep(Duration::from_millis(40));
            let result = task.await.unwrap();
            if fail {
                assert!(
                    result
                        .unwrap_err()
                        .to_string()
                        .contains("completed store error")
                );
            } else {
                let result = result.unwrap();
                assert_eq!(result.result, OutcomeResult::Timeout);
                assert!(result.snapshot.is_none());
            }
        }
    }

    #[tokio::test]
    async fn dropping_request_stops_polling_and_running_read_does_not_reschedule() {
        let (entered_tx, entered_rx) = mpsc::channel();
        let (release_tx, release_rx) = mpsc::channel();
        let release_rx = Mutex::new(release_rx);
        let calls = Arc::new(AtomicUsize::new(0));
        let counter = Arc::clone(&calls);
        let task = tokio::spawn(observe(
            "feature".into(),
            "review".into(),
            60000,
            move || {
                counter.fetch_add(1, Ordering::SeqCst);
                entered_tx.send(()).unwrap();
                release_rx.lock().unwrap().recv().unwrap();
                Ok(record(None, Delivery::Prepared, 1))
            },
        ));
        entered(entered_rx).await;
        task.abort();
        assert!(task.await.unwrap_err().is_cancelled());
        release_tx.send(()).unwrap();
        tokio::time::sleep(Duration::from_millis(550)).await;
        assert_eq!(calls.load(Ordering::SeqCst), 1);
    }

    #[tokio::test]
    async fn validated_store_reads_surface_missing_corrupt_future_busy_and_io_errors() {
        use fs2::FileExt;
        let dir = tempfile::tempdir().unwrap();
        let store = Store::new(dir.path().to_owned());
        assert!(
            read(store.clone(), "feature".into(), "review".into(), 0)
                .await
                .unwrap_err()
                .to_string()
                .contains("not found")
        );
        let valid = record(None, Delivery::Prepared, 1);
        store.save(valid.workstream, 0).unwrap();
        let path = dir.path().join("workstreams.json");
        let original = std::fs::read(&path).unwrap();
        assert_eq!(
            read(store.clone(), "feature".into(), "review".into(), 0)
                .await
                .unwrap()
                .result,
            OutcomeResult::Pending
        );
        assert_eq!(std::fs::read(&path).unwrap(), original);
        let lock = std::fs::OpenOptions::new()
            .read(true)
            .write(true)
            .open(dir.path().join("workstreams.lock"))
            .unwrap();
        lock.lock_exclusive().unwrap();
        let error = read(store.clone(), "feature".into(), "review".into(), 0)
            .await
            .unwrap_err();
        assert!(error.to_string().contains("store is busy"));
        assert_eq!(std::fs::read(&path).unwrap(), original);
        drop(lock);
        for bytes in [
            b"not-json".to_vec(),
            br#"{"version":3,"records":[]}"#.to_vec(),
            vec![b'x'; 2 * 1024 * 1024 + 1],
        ] {
            std::fs::write(&path, &bytes).unwrap();
            assert!(
                read(store.clone(), "feature".into(), "review".into(), 60000)
                    .await
                    .is_err()
            );
            assert_eq!(std::fs::read(&path).unwrap(), bytes);
        }
        let legacy = include_bytes!("../tests/fixtures/heeler-v1-location-hints.json");
        std::fs::write(&path, legacy).unwrap();
        let error = read(store, "legacy".into(), "review".into(), 0)
            .await
            .unwrap_err();
        assert!(
            error.to_string().contains("assignment not found"),
            "{error:#}"
        );
        assert_eq!(std::fs::read(&path).unwrap(), legacy);
        let other = tempfile::tempdir().unwrap();
        std::fs::create_dir(other.path().join("workstreams.json")).unwrap();
        assert!(
            read(
                Store::new(other.path().to_owned()),
                "feature".into(),
                "review".into(),
                0
            )
            .await
            .is_err()
        );
    }

    #[test]
    fn dropping_queued_read_aborts_before_reader_starts() {
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_time()
            .max_blocking_threads(1)
            .build()
            .unwrap();
        runtime.block_on(async {
            let (entered_tx, entered_rx) = mpsc::channel();
            let (release_tx, release_rx) = mpsc::channel();
            let occupied = tokio::task::spawn_blocking(move || {
                entered_tx.send(()).unwrap();
                release_rx.recv().unwrap();
            });
            entered_rx.recv_timeout(Duration::from_secs(2)).unwrap();
            let calls = Arc::new(AtomicUsize::new(0));
            let counter = Arc::clone(&calls);
            let queued = ReadTask(tokio::task::spawn_blocking(move || {
                counter.fetch_add(1, Ordering::SeqCst);
                CompletedRead {
                    at: Instant::now(),
                    record: Ok(record(None, Delivery::Prepared, 1)),
                }
            }));
            drop(queued);
            release_tx.send(()).unwrap();
            occupied.await.unwrap();
            tokio::time::sleep(Duration::from_millis(20)).await;
            assert_eq!(calls.load(Ordering::SeqCst), 0);
        });
    }
}
