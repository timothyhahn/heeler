use crate::memory::WorkstreamChange;
use crate::model::{Record, Workstream, validate_id};
use anyhow::{Context, Result, ensure};
use fs2::FileExt;
use serde::{Deserialize, Serialize};
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

const MAX_BYTES: u64 = 2 * 1024 * 1024;
const LOCK_TIMEOUT: Duration = Duration::from_secs(1);
const LOCK_RETRY_INTERVAL: Duration = Duration::from_millis(10);

#[derive(Debug, Clone)]
pub struct Store {
    root: PathBuf,
}

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Document {
    version: u32,
    records: Vec<Record>,
}

impl Store {
    pub fn new(root: PathBuf) -> Self {
        Self { root }
    }

    fn lock(&self) -> Result<File> {
        create_state_dir(&self.root)?;
        let mut options = OpenOptions::new();
        options.read(true).write(true).create(true).truncate(false);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options.mode(0o600);
        }
        let file = options
            .open(self.root.join("workstreams.lock"))
            .context("could not open workstream store lock")?;
        let deadline = Instant::now() + LOCK_TIMEOUT;
        loop {
            match FileExt::try_lock_exclusive(&file) {
                Ok(()) => return Ok(file),
                Err(error)
                    if error.raw_os_error() == fs2::lock_contended_error().raw_os_error()
                        || error.kind() == std::io::ErrorKind::Interrupted => {}
                Err(error) => return Err(error).context("could not lock workstream store"),
            }
            let remaining = deadline.saturating_duration_since(Instant::now());
            ensure!(
                !remaining.is_zero(),
                "workstream store is busy: lock acquisition timed out after 1 second; retry later"
            );
            std::thread::sleep(remaining.min(LOCK_RETRY_INTERVAL));
        }
    }

    fn read_locked(&self) -> Result<Vec<Record>> {
        let file = match File::open(self.root.join("workstreams.json")) {
            Ok(file) => file,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(vec![]),
            Err(error) => return Err(error.into()),
        };
        let mut bytes = Vec::new();
        file.take(MAX_BYTES + 1).read_to_end(&mut bytes)?;
        ensure!(
            bytes.len() as u64 <= MAX_BYTES,
            "workstream store exceeds size limit"
        );
        let document: Document = serde_json::from_slice(&bytes)
            .context("invalid workstream store; original left untouched")?;
        ensure!(
            matches!(document.version, 1 | 2),
            "unsupported workstream store version {}",
            document.version
        );
        let mut ids = std::collections::HashSet::new();
        for record in &document.records {
            record.workstream.validate()?;
            ensure!(
                record.revision > 0 && ids.insert(&record.workstream.id),
                "invalid or duplicate workstream record"
            );
        }
        Ok(document.records)
    }

    pub fn list(&self) -> Result<Vec<Record>> {
        let _lock = self.lock()?;
        self.read_locked()
    }

    pub fn get(&self, workstream_id: &str) -> Result<Record> {
        validate_id(workstream_id)?;
        let _lock = self.lock()?;
        self.read_locked()?
            .into_iter()
            .find(|record| record.workstream.id == workstream_id)
            .context("workstream not found")
    }

    /// Read, validate and replace under the same lock. No partial batch writes or retries.
    pub fn update(
        &self,
        workstream_id: &str,
        expected_revision: u64,
        change: WorkstreamChange,
    ) -> Result<Record> {
        validate_id(workstream_id)?;
        ensure!(
            expected_revision > 0,
            "updates require a current nonzero revision"
        );
        let _lock = self.lock()?;
        let records = self.read_locked()?;
        let current = records
            .iter()
            .find(|record| record.workstream.id == workstream_id)
            .context("workstream not found")?;
        ensure!(
            current.revision == expected_revision,
            "revision conflict; reread and reconcile"
        );
        let mut workstream = current.workstream.clone();
        change.apply(&mut workstream)?;
        ensure!(workstream != current.workstream, "change has no effect");
        self.save_locked(records, workstream, expected_revision)
    }

    /// Replace a whole workstream using optimistic concurrency. Zero means create-only.
    pub fn save(&self, workstream: Workstream, expected_revision: u64) -> Result<Record> {
        workstream.validate()?;
        let _lock = self.lock()?;
        self.save_locked(self.read_locked()?, workstream, expected_revision)
    }

    fn save_locked(
        &self,
        mut records: Vec<Record>,
        workstream: Workstream,
        expected_revision: u64,
    ) -> Result<Record> {
        workstream.validate()?;
        let position = records
            .iter()
            .position(|record| record.workstream.id == workstream.id);
        let actual = position.map_or(0, |index| records[index].revision);
        ensure!(
            actual == expected_revision,
            "revision conflict: expected {expected_revision}, found {actual}; reread and reconcile"
        );
        let record = Record {
            revision: actual.checked_add(1).context("revision overflow")?,
            updated_at_unix: SystemTime::now().duration_since(UNIX_EPOCH)?.as_secs(),
            workstream,
        };
        if let Some(index) = position {
            records[index] = record.clone();
        } else {
            records.push(record.clone());
        }
        ensure!(records.len() <= 256, "workstream limit reached");
        records.sort_by(|a, b| a.workstream.id.cmp(&b.workstream.id));
        let bytes = serde_json::to_vec_pretty(&Document {
            version: 2,
            records,
        })?;
        ensure!(
            bytes.len() as u64 <= MAX_BYTES,
            "workstream store exceeds size limit"
        );
        let mut temporary = tempfile::NamedTempFile::new_in(&self.root)?;
        temporary.write_all(&bytes)?;
        temporary.as_file().sync_all()?;
        temporary.persist(self.root.join("workstreams.json"))?;
        sync_directory(&self.root)
            .context("notes were replaced but directory sync failed; reread before retrying")?;
        Ok(record)
    }
}

fn create_state_dir(path: &Path) -> Result<()> {
    match fs::metadata(path) {
        Ok(metadata) => {
            ensure!(metadata.is_dir(), "state path is not a directory");
            return Ok(());
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
        Err(error) => return Err(error.into()),
    }
    let parent = path
        .parent()
        .filter(|parent| !parent.as_os_str().is_empty())
        .unwrap_or_else(|| Path::new("."));
    create_state_dir(parent)?;
    let mut builder = fs::DirBuilder::new();
    #[cfg(unix)]
    {
        use std::os::unix::fs::DirBuilderExt;
        builder.mode(0o700);
    }
    match builder.create(path) {
        Ok(()) => {}
        Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists && path.is_dir() => {}
        Err(error) => return Err(error.into()),
    }
    // Persist each new directory's name, including nested state-dir ancestors.
    sync_directory(parent)?;
    Ok(())
}

fn sync_directory(path: &Path) -> Result<()> {
    #[cfg(unix)]
    File::open(path)?.sync_all()?;
    #[cfg(not(unix))]
    let _ = path; // Directory flushing needs a platform-specific implementation.
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::Status;

    fn sample() -> Workstream {
        Workstream {
            id: "release".into(),
            title: "Release".into(),
            purpose: String::new(),
            status: Status::Planned,
            next_action: None,
            blocker: None,
            locations: vec![],
            assignments: vec![],
        }
    }

    fn change(value: serde_json::Value) -> WorkstreamChange {
        serde_json::from_value(value).unwrap()
    }

    fn assignment(id: &str) -> crate::assignment::Assignment {
        serde_json::from_value(serde_json::json!({
            "id": id, "role": "review",
            "target": {"session": "default", "pane_id": "w1:p1", "terminal_id": "term_test", "agent": "pi", "agent_session": null},
            "brief": "Historical brief", "constraints": ["Preserve this"],
            "acceptance_criteria": ["Report findings"],
            "report_path": std::env::temp_dir().join("synthetic-review.md"),
            "delivery": "prepared", "outcome": null,
        })).unwrap()
    }

    #[test]
    fn focused_changes_preserve_history_and_unrelated_fields() {
        use crate::assignment::Delivery;
        use serde_json::json;
        let dir = tempfile::tempdir().unwrap();
        let store = Store::new(dir.path().to_owned());
        let mut work = sample();
        work.next_action = Some("Keep this action".into());
        work.blocker = Some("Needs input".into());
        work.purpose = "Keep this purpose".into();
        work.assignments = vec![assignment("one"), assignment("two"), assignment("three")];
        work.assignments[0].delivery = Delivery::Submitted;
        work.assignments[1].delivery = Delivery::Uncertain;
        work.assignments[1].outcome = Some("Old outcome".into());
        store.save(work.clone(), 0).unwrap();
        let updated = store
            .update(
                "release",
                1,
                change(json!({"type": "notes", "status": "paused", "blocker": {"type": "clear"}})),
            )
            .unwrap();
        work.status = Status::Paused;
        work.blocker = None;
        assert_eq!(updated.workstream, work);
        assert_eq!(updated.revision, 2);
        let additions = vec![
            assignment("new-one"),
            assignment("new-two"),
            assignment("new-three"),
        ];
        let updated = store
            .update(
                "release",
                2,
                change(json!({"type": "append_assignments", "assignments": additions})),
            )
            .unwrap();
        work.assignments.extend(additions);
        assert_eq!(updated.workstream, work);
        let updated = store.update("release", 3, change(json!({"type": "outcomes", "outcomes": [
            {"assignment_id": "one", "outcome": {"type": "set", "value": "Reviewed one"}},
            {"assignment_id": "two", "outcome": {"type": "clear"}},
            {"assignment_id": "three", "outcome": {"type": "set", "value": "Reviewed three"}}
        ]}))).unwrap();
        work.assignments[0].outcome = Some("Reviewed one".into());
        work.assignments[1].outcome = None;
        work.assignments[2].outcome = Some("Reviewed three".into());
        assert_eq!(updated.workstream, work);
        let updated = store.update("release", 4, change(json!({"type": "notes", "next_action": {"type": "set", "value": "Inspect reports"}}))).unwrap();
        work.next_action = Some("Inspect reports".into());
        assert_eq!(updated.workstream, work);
        assert_eq!(updated.revision, 5);
        let bytes = fs::read(dir.path().join("workstreams.json")).unwrap();
        assert_eq!(store.get("release").unwrap().workstream, work);
        assert_eq!(
            fs::read(dir.path().join("workstreams.json")).unwrap(),
            bytes
        );
        assert!(
            store
                .get("missing")
                .unwrap_err()
                .to_string()
                .contains("not found")
        );
        assert!(store.get("../bad").is_err());
    }

    #[test]
    fn invalid_batches_conflicts_and_no_effect_changes_never_write() {
        use serde_json::json;
        let dir = tempfile::tempdir().unwrap();
        let store = Store::new(dir.path().to_owned());
        let mut work = sample();
        work.assignments.push(assignment("old"));
        store.save(work, 0).unwrap();
        let original = fs::read(dir.path().join("workstreams.json")).unwrap();
        let mut submitted = assignment("new");
        submitted.delivery = crate::assignment::Delivery::Submitted;
        let mut uncertain = submitted.clone();
        uncertain.delivery = crate::assignment::Delivery::Uncertain;
        let mut has_outcome = assignment("new");
        has_outcome.outcome = Some("Premature outcome".into());
        let mut unsafe_target = assignment("new");
        unsafe_target.target.pane_id = "retired-workspace:p1".into();
        for request in [
            json!({"type": "notes"}),
            json!({"type": "notes", "status": "planned"}),
            json!({"type": "notes", "blocker": {"type": "clear"}}),
            json!({"type": "notes", "status": "active", "next_action": {"type": "set", "value": "a".repeat(4097)}}),
            json!({"type": "notes", "blocker": {"type": "set", "value": " "}}),
            json!({"type": "notes", "blocker": {"type": "set", "value": "bad\0text"}}),
            json!({"type": "append_assignments", "assignments": []}),
            json!({"type": "append_assignments", "assignments": vec![assignment("new"); 5]}),
            json!({"type": "append_assignments", "assignments": [assignment("new"), assignment("old")]}),
            json!({"type": "append_assignments", "assignments": [assignment("new"), assignment("new")]}),
            json!({"type": "append_assignments", "assignments": [submitted]}),
            json!({"type": "append_assignments", "assignments": [uncertain]}),
            json!({"type": "append_assignments", "assignments": [has_outcome]}),
            json!({"type": "append_assignments", "assignments": [unsafe_target]}),
            json!({"type": "outcomes", "outcomes": []}),
            json!({"type": "outcomes", "outcomes": vec![json!({"assignment_id": "old", "outcome": {"type": "clear"}}); 5]}),
            json!({"type": "outcomes", "outcomes": [
                {"assignment_id": "old", "outcome": {"type": "set", "value": "First"}},
                {"assignment_id": "old", "outcome": {"type": "clear"}}]}),
            json!({"type": "outcomes", "outcomes": [
                {"assignment_id": "old", "outcome": {"type": "set", "value": "First"}},
                {"assignment_id": "missing", "outcome": {"type": "clear"}}]}),
            json!({"type": "outcomes", "outcomes": [{"assignment_id": "old", "outcome": {"type": "set", "value": "x".repeat(8193)}}]}),
        ] {
            assert!(
                store.update("release", 1, change(request.clone())).is_err(),
                "{request}"
            );
            assert_eq!(
                fs::read(dir.path().join("workstreams.json")).unwrap(),
                original
            );
        }
        for (id, revision) in [
            ("release", 0),
            ("release", 2),
            ("missing", 1),
            ("../bad", 1),
        ] {
            assert!(
                store
                    .update(
                        id,
                        revision,
                        change(json!({"type": "notes", "status": "active"}))
                    )
                    .is_err()
            );
            assert_eq!(
                fs::read(dir.path().join("workstreams.json")).unwrap(),
                original
            );
        }
        let mut full = sample();
        full.assignments = (0..16).map(|i| assignment(&format!("old-{i}"))).collect();
        store.save(full, 1).unwrap();
        let original = fs::read(dir.path().join("workstreams.json")).unwrap();
        assert!(
            store
                .update(
                    "release",
                    2,
                    change(
                        json!({"type": "append_assignments", "assignments": [assignment("new")]})
                    )
                )
                .is_err()
        );
        assert_eq!(
            fs::read(dir.path().join("workstreams.json")).unwrap(),
            original
        );
    }

    #[test]
    fn concurrent_focused_updates_cannot_overwrite_a_winning_revision() {
        let dir = tempfile::tempdir().unwrap();
        let store = Store::new(dir.path().to_owned());
        let mut work = sample();
        work.assignments.push(assignment("history"));
        store.save(work.clone(), 0).unwrap();
        let barrier = std::sync::Arc::new(std::sync::Barrier::new(8));
        let handles: Vec<_> = (0..8).map(|index| {
            let store = store.clone();
            let barrier = barrier.clone();
            std::thread::spawn(move || {
                barrier.wait();
                store.update("release", 1, change(serde_json::json!({"type": "notes", "next_action": {"type": "set", "value": format!("Action {index}")}})))
            })
        }).collect();
        let results: Vec<_> = handles
            .into_iter()
            .map(|handle| handle.join().unwrap())
            .collect();
        let winners: Vec<_> = results
            .iter()
            .filter_map(|result| result.as_ref().ok())
            .collect();
        assert_eq!(winners.len(), 1);
        for error in results.iter().filter_map(|result| result.as_ref().err()) {
            assert!(error.to_string().contains("revision conflict"));
        }
        let saved = store.get("release").unwrap();
        assert_eq!(saved.revision, 2);
        assert_eq!(saved.workstream, winners[0].workstream);
        assert_eq!(saved.workstream.assignments, work.assignments);
    }

    #[test]
    fn focused_updates_keep_legacy_hints_and_upgrade_only_on_write() {
        let dir = tempfile::tempdir().unwrap();
        let store = Store::new(dir.path().to_owned());
        let original = include_bytes!("../tests/fixtures/heeler-v1-location-hints.json");
        let path = dir.path().join("workstreams.json");
        fs::write(&path, original).unwrap();
        let legacy = store.get("legacy").unwrap();
        let _summary = crate::memory::RecordSummary::from(&legacy);
        assert_eq!(fs::read(&path).unwrap(), original);
        let changed = store
            .update(
                "legacy",
                legacy.revision,
                change(serde_json::json!({"type": "notes", "status": "paused"})),
            )
            .unwrap();
        assert_eq!(changed.workstream.locations, legacy.workstream.locations);
        assert!(changed.workstream.assignments.is_empty());
        assert_eq!(store.get("legacy").unwrap().revision, 2);
        let document: Document = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
        assert_eq!(document.version, 2);
    }

    #[test]
    fn focused_update_size_limit_does_not_replace_the_file() {
        let dir = tempfile::tempdir().unwrap();
        let store = Store::new(dir.path().to_owned());
        let mut document = Document {
            version: 2,
            records: vec![],
        };
        loop {
            let mut work = sample();
            work.id = format!("stream-{}", document.records.len());
            work.purpose = "p".repeat(4096);
            work.next_action = Some("n".repeat(4096));
            work.blocker = Some("b".repeat(4096));
            document.records.push(Record {
                revision: 1,
                updated_at_unix: 1,
                workstream: work,
            });
            if serde_json::to_vec_pretty(&document).unwrap().len() as u64 > MAX_BYTES - 1024 {
                document.records.pop();
                break;
            }
        }
        let original = serde_json::to_vec_pretty(&document).unwrap();
        let path = dir.path().join("workstreams.json");
        fs::write(&path, &original).unwrap();
        let assignments: Vec<_> = (0..4)
            .map(|i| {
                let mut assignment = assignment(&format!("new-{i}"));
                assignment.brief = "x".repeat(8192);
                assignment
            })
            .collect();
        let error = store
            .update(
                "stream-0",
                1,
                change(
                    serde_json::json!({"type": "append_assignments", "assignments": assignments}),
                ),
            )
            .unwrap_err();
        assert!(
            error.to_string().contains("store exceeds size limit"),
            "{error:#}"
        );
        assert_eq!(fs::read(path).unwrap(), original);
    }

    #[test]
    fn round_trip_and_conflict() {
        let dir = tempfile::tempdir().unwrap();
        let store = Store::new(dir.path().to_owned());
        assert!(store.list().unwrap().is_empty());
        let first = store.save(sample(), 0).unwrap();
        assert_eq!(first.revision, 1);
        assert!(store.save(sample(), 0).is_err());
        let mut changed = sample();
        changed.next_action = Some("Write notes".into());
        assert_eq!(store.save(changed, 1).unwrap().revision, 2);
        assert_eq!(
            Store::new(dir.path().to_owned()).list().unwrap()[0].revision,
            2
        );
    }

    #[test]
    fn saves_in_new_nested_state_directories() {
        let dir = tempfile::tempdir().unwrap();
        let store = Store::new(dir.path().join("nested").join("notes"));
        store.save(sample(), 0).unwrap();
        assert_eq!(store.list().unwrap()[0].revision, 1);
    }

    #[test]
    fn upgrades_version_one_only_when_saving() {
        let dir = tempfile::tempdir().unwrap();
        let store = Store::new(dir.path().to_owned());
        let old = serde_json::to_vec(&serde_json::json!({
            "version": 1,
            "records": [{"revision": 1, "updated_at_unix": 1, "workstream": sample()}]
        }))
        .unwrap();
        let path = dir.path().join("workstreams.json");
        fs::write(&path, &old).unwrap();
        let records = store.list().unwrap();
        assert!(records[0].workstream.assignments.is_empty());
        assert_eq!(fs::read(&path).unwrap(), old);
        let mut changed = records[0].workstream.clone();
        changed.next_action = Some("Draft notes".into());
        store.save(changed, 1).unwrap();
        let upgraded: serde_json::Value = serde_json::from_slice(&fs::read(path).unwrap()).unwrap();
        assert_eq!(upgraded["version"], 2);
        assert_eq!(upgraded["records"][0]["revision"], 2);
        assert_eq!(
            upgraded["records"][0]["workstream"]["next_action"],
            "Draft notes"
        );
    }

    #[test]
    fn head_accepted_location_hints_survive_reads_and_version_two_saves() {
        let dir = tempfile::tempdir().unwrap();
        let store = Store::new(dir.path().to_owned());
        let original = include_bytes!("../tests/fixtures/heeler-v1-location-hints.json");
        let path = dir.path().join("workstreams.json");
        fs::write(&path, original).unwrap();
        let records = store.list().unwrap();
        let legacy = &records[0];
        assert_eq!(legacy.workstream.locations.len(), 6);
        assert!(legacy.workstream.assignments.is_empty());
        assert_eq!(fs::read(&path).unwrap(), original);

        // An unrelated update upgrades the envelope, not the remembered hints.
        store.save(sample(), 0).unwrap();
        let upgraded: Document = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
        assert_eq!(upgraded.version, 2);
        let preserved = upgraded
            .records
            .iter()
            .find(|r| r.workstream.id == "legacy")
            .unwrap();
        assert_eq!(preserved.revision, legacy.revision);
        assert_eq!(preserved.updated_at_unix, legacy.updated_at_unix);
        assert_eq!(preserved.workstream, legacy.workstream);

        let mut changed = legacy.workstream.clone();
        changed.next_action = Some("Reconcile the retired hints".into());
        store.save(changed.clone(), legacy.revision).unwrap();
        let records = store.list().unwrap();
        let saved = records
            .iter()
            .find(|r| r.workstream.id == "legacy")
            .unwrap();
        assert_eq!(saved.revision, legacy.revision + 1);
        assert_eq!(saved.workstream, changed);
        assert_eq!(saved.workstream.locations, legacy.workstream.locations);
    }

    #[test]
    fn refuses_to_overwrite_corrupt_or_future_data() {
        let dir = tempfile::tempdir().unwrap();
        let store = Store::new(dir.path().to_owned());
        for bytes in [b"not json".as_slice(), br#"{"version":3,"records":[]}"#] {
            fs::write(dir.path().join("workstreams.json"), bytes).unwrap();
            assert!(store.save(sample(), 0).is_err());
            assert!(store.get("release").is_err());
            assert!(
                store
                    .update(
                        "release",
                        1,
                        change(serde_json::json!({"type": "notes", "status": "active"}))
                    )
                    .is_err()
            );
            assert_eq!(
                fs::read(dir.path().join("workstreams.json")).unwrap(),
                bytes
            );
        }
    }

    #[test]
    fn contention_returns_errors_without_changing_data_and_recovers() {
        let dir = tempfile::tempdir().unwrap();
        let store = Store::new(dir.path().to_owned());
        store.save(sample(), 0).unwrap();
        let original = fs::read(dir.path().join("workstreams.json")).unwrap();
        let held_lock = store.lock().unwrap();

        let read_error = store.list().unwrap_err();
        assert!(read_error.to_string().contains("store is busy"));
        let save_error = store.save(sample(), 1).unwrap_err();
        assert!(save_error.to_string().contains("store is busy"));
        assert!(
            store
                .get("release")
                .unwrap_err()
                .to_string()
                .contains("store is busy")
        );
        let error = store
            .update(
                "release",
                1,
                change(serde_json::json!({"type": "notes", "status": "active"})),
            )
            .unwrap_err();
        assert!(error.to_string().contains("store is busy"));
        assert_eq!(
            fs::read(dir.path().join("workstreams.json")).unwrap(),
            original
        );

        drop(held_lock);
        assert_eq!(store.list().unwrap()[0].revision, 1);
        assert_eq!(store.save(sample(), 1).unwrap().revision, 2);
    }

    #[test]
    fn concurrent_updates_cannot_overwrite_the_winning_revision() {
        let dir = tempfile::tempdir().unwrap();
        let store = Store::new(dir.path().to_owned());
        store.save(sample(), 0).unwrap();
        let barrier = std::sync::Arc::new(std::sync::Barrier::new(8));
        let handles: Vec<_> = (0..8)
            .map(|index| {
                let store = store.clone();
                let barrier = barrier.clone();
                std::thread::spawn(move || {
                    let mut work = sample();
                    work.next_action = Some(format!("Action {index}"));
                    barrier.wait();
                    store.save(work, 1)
                })
            })
            .collect();
        let results: Vec<_> = handles
            .into_iter()
            .map(|handle| handle.join().unwrap())
            .collect();
        let winners: Vec<_> = results
            .iter()
            .filter_map(|result| result.as_ref().ok())
            .collect();
        assert_eq!(winners.len(), 1);
        for error in results.iter().filter_map(|result| result.as_ref().err()) {
            assert!(error.to_string().contains("revision conflict"));
        }
        let saved = store.list().unwrap();
        assert_eq!(saved[0].revision, 2);
        assert_eq!(saved[0].workstream, winners[0].workstream);
    }

    #[test]
    fn concurrent_creates_do_not_lose_records() {
        let dir = tempfile::tempdir().unwrap();
        let handles: Vec<_> = (0..8)
            .map(|index| {
                let store = Store::new(dir.path().to_owned());
                std::thread::spawn(move || {
                    let mut work = sample();
                    work.id = format!("work-{index}");
                    store.save(work, 0).unwrap();
                })
            })
            .collect();
        for handle in handles {
            handle.join().unwrap();
        }
        assert_eq!(Store::new(dir.path().to_owned()).list().unwrap().len(), 8);
    }
}
