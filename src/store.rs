use crate::model::{Record, Workstream};
use anyhow::{Context, Result, ensure};
use fs2::FileExt;
use serde::{Deserialize, Serialize};
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Write};
use std::path::PathBuf;
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
        #[cfg(unix)]
        {
            use std::os::unix::fs::DirBuilderExt;
            fs::DirBuilder::new()
                .recursive(true)
                .mode(0o700)
                .create(&self.root)?;
        }
        #[cfg(not(unix))]
        fs::create_dir_all(&self.root)?;
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
            document.version == 1,
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

    /// Replace a whole workstream using optimistic concurrency. Zero means create-only.
    pub fn save(&self, workstream: Workstream, expected_revision: u64) -> Result<Record> {
        workstream.validate()?;
        let _lock = self.lock()?;
        let mut records = self.read_locked()?;
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
            version: 1,
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
        Ok(record)
    }
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
        }
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
    fn refuses_to_overwrite_corrupt_or_future_data() {
        let dir = tempfile::tempdir().unwrap();
        let store = Store::new(dir.path().to_owned());
        for bytes in [b"not json".as_slice(), br#"{"version":2,"records":[]}"#] {
            fs::write(dir.path().join("workstreams.json"), bytes).unwrap();
            assert!(store.save(sample(), 0).is_err());
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
