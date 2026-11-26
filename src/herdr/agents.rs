use super::{Agent, Herdr, decode};
use crate::model::{validate_pane_id, validate_session};
use anyhow::{Context, Result, bail, ensure};
use schemars::JsonSchema;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::time::Duration;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct AgentTarget {
    pub session: String,
    pub pane_id: String,
    pub terminal_id: String,
    pub agent: String,
    /// Native conversation reference when herdr has one. None means unverified.
    pub agent_session: Option<Value>,
}

impl AgentTarget {
    pub fn validate(&self) -> Result<()> {
        validate_session(&self.session)?;
        validate_pane_id(&self.pane_id)?;
        ensure!(
            !self.terminal_id.is_empty() && self.terminal_id.len() <= 128,
            "invalid terminal ID"
        );
        ensure!(
            !self.agent.is_empty() && self.agent.len() <= 64,
            "invalid agent kind"
        );
        if let Some(session) = &self.agent_session {
            ensure!(
                serde_json::to_vec(session)?.len() <= 8192,
                "agent session reference is too large"
            );
        }
        Ok(())
    }

    fn check(&self, agent: &Agent) -> Result<()> {
        ensure!(
            agent.pane_id == self.pane_id && agent.terminal_id == self.terminal_id,
            "agent location changed; inspect the pane again"
        );
        ensure!(
            agent.agent.as_deref() == Some(&self.agent),
            "agent kind changed; inspect the pane again"
        );
        if let Some(expected) = &self.agent_session {
            ensure!(
                agent.agent_session.as_ref() == Some(expected),
                "agent conversation changed; inspect the pane again"
            );
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct AgentHandle {
    pub target: AgentTarget,
    pub agent: Agent,
}

#[derive(Debug, Serialize)]
pub struct PromptAcknowledgement {
    /// Supplied identity checked where known; None conversation remains unverified.
    pub target: AgentTarget,
    pub agent: Agent,
    pub acknowledged: bool,
    /// Native metadata can predate input; it is not a fresh final observation.
    pub metadata_timing: &'static str,
}

impl AgentHandle {
    fn new(session: &str, agent: Agent) -> Result<Self> {
        let target = AgentTarget {
            session: session.into(),
            pane_id: agent.pane_id.clone(),
            terminal_id: agent.terminal_id.clone(),
            agent: agent
                .agent
                .clone()
                .context("agent kind is unknown; inspect session inventory")?,
            agent_session: agent.agent_session.clone(),
        };
        target.validate()?;
        Ok(Self { target, agent })
    }
}

#[derive(Debug, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct StartAgent {
    pub pane_id: String,
    pub name: String,
    /// A supported herdr kind, such as pi, claude, codex, or opencode.
    pub kind: String,
    #[serde(default)]
    pub args: Vec<String>,
    #[serde(default = "startup_timeout")]
    pub timeout_ms: u64,
}

fn startup_timeout() -> u64 {
    30_000
}

#[derive(Debug, Clone, Copy, Deserialize, Serialize, JsonSchema)]
#[serde(rename_all = "snake_case")]
pub enum ReadSource {
    Visible,
    RecentUnwrapped,
}

impl ReadSource {
    fn as_arg(self) -> &'static str {
        match self {
            Self::Visible => "visible",
            Self::RecentUnwrapped => "recent-unwrapped",
        }
    }
}

#[derive(Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
enum AgentResult {
    #[serde(rename = "agent_info")]
    Info { agent: Agent },
    #[serde(rename = "agent_started")]
    Started { agent: Agent },
    #[serde(rename = "agent_prompted")]
    Prompted { agent: Agent },
}

impl Herdr {
    pub async fn inspect_agent(&self, session: &str, pane_id: &str) -> Result<AgentHandle> {
        validate_pane_id(pane_id)?;
        let value = self
            .session_json(
                session,
                vec!["agent".into(), "get".into(), pane_id.into()],
                Duration::from_secs(5),
                false,
            )
            .await?;
        let AgentResult::Info { agent } = decode(value)? else {
            bail!("expected agent_info response");
        };
        AgentHandle::new(session, agent)
    }

    pub(crate) async fn checked_agent(&self, target: &AgentTarget) -> Result<AgentHandle> {
        target.validate()?;
        let current = self.inspect_agent(&target.session, &target.pane_id).await?;
        target.check(&current.agent)?;
        Ok(current)
    }

    pub async fn start_agent(&self, session: &str, start: StartAgent) -> Result<AgentHandle> {
        validate_pane_id(&start.pane_id)?;
        ensure!(
            start.name.len() <= 32
                && start
                    .name
                    .as_bytes()
                    .first()
                    .is_some_and(u8::is_ascii_lowercase)
                && start.name.bytes().all(|b| b.is_ascii_lowercase()
                    || b.is_ascii_digit()
                    || matches!(b, b'_' | b'-')),
            "agent name must start with a lowercase letter and contain at most 32 lowercase letters, digits, underscores or hyphens"
        );
        ensure!(
            start
                .kind
                .as_bytes()
                .first()
                .is_some_and(u8::is_ascii_lowercase)
                && start.kind.len() <= 32
                && start.kind.bytes().all(|b| b.is_ascii_lowercase()
                    || b.is_ascii_digit()
                    || matches!(b, b'_' | b'-')),
            "invalid agent kind"
        );
        ensure!(
            (4000..=60_000).contains(&start.timeout_ms),
            "startup timeout must be 4000–60000 milliseconds"
        );
        ensure!(
            start.args.len() <= 64
                && start.args.iter().map(String::len).sum::<usize>() <= 8192
                && start.args.iter().all(|arg| !arg.contains('\0')),
            "invalid or oversized agent arguments"
        );
        let mut args = vec![
            "agent".into(),
            "start".into(),
            start.name,
            "--kind".into(),
            start.kind,
            "--pane".into(),
            start.pane_id,
            "--timeout".into(),
            start.timeout_ms.to_string(),
            "--".into(),
        ];
        args.extend(start.args);
        let value = self
            .session_json(
                session,
                args,
                Duration::from_millis(start.timeout_ms + 5000),
                true,
            )
            .await?;
        let AgentResult::Started { agent } =
            decode(value).context("invalid launch response; inspect the pane before retrying")?
        else {
            bail!("unexpected launch response; inspect the pane before retrying");
        };
        AgentHandle::new(session, agent)
    }

    /// One ordinary message to a settled worker, not a steering/queue/abort API.
    pub async fn prompt_settled_agent(
        &self,
        target: &AgentTarget,
        text: String,
    ) -> Result<PromptAcknowledgement> {
        // Native globals are consumed across argv before the leaf protects TEXT.
        if let Some(option) = ["--session", "--remote", "--remote-keybindings"]
            .into_iter()
            .find(|option| {
                text.strip_prefix(*option)
                    .is_some_and(|suffix| suffix.is_empty() || suffix.starts_with('='))
            })
        {
            bail!(
                "text matches native herdr CLI global option {option}, which is consumed before prompt parsing; rephrase the message; no native calls were made"
            );
        }
        ensure!(
            !text.trim().is_empty()
                && text.len() <= 32_768
                && text
                    .chars()
                    .all(|c| !c.is_control() || matches!(c, '\n' | '\t')),
            "text must be nonblank, at most 32768 UTF-8 bytes, without controls except LF and tab"
        );
        let handle = self.prompt_agent(target, text).await?;
        Ok(PromptAcknowledgement {
            target: target.clone(),
            agent: handle.agent,
            acknowledged: true,
            metadata_timing: "pre_submission",
        })
    }

    /// Acknowledges terminal submission, not task completion. Never retries.
    pub(crate) async fn prompt_agent(
        &self,
        target: &AgentTarget,
        text: String,
    ) -> Result<AgentHandle> {
        let current = self.checked_agent(target).await?;
        ensure!(
            current.agent.ready_for_prompt(),
            "agent is no longer ready; inspect it before retrying"
        );
        ensure!(
            !text.trim().is_empty() && text.len() <= 32_768 && !text.contains('\0'),
            "invalid or oversized prompt"
        );
        async {
            let value = self
                .session_json(
                    &target.session,
                    vec![
                        "agent".into(),
                        "prompt".into(),
                        target.pane_id.clone(),
                        text,
                    ],
                    Duration::from_secs(10),
                    true,
                )
                .await?;
            let AgentResult::Prompted { agent } = decode(value)
                .context("invalid submission response; inspect the agent before retrying")?
            else {
                bail!("unexpected submission response; inspect the agent before retrying");
            };
            target.check(&agent)?;
            AgentHandle::new(&target.session, agent)
        }
        .await
        .context("prompt possibly submitted; inspect before retrying; do not resend automatically")
    }

    pub async fn read_agent(
        &self,
        target: &AgentTarget,
        source: ReadSource,
        lines: u32,
    ) -> Result<String> {
        ensure!((1..=500).contains(&lines), "read lines must be 1–500");
        self.checked_agent(target).await?;
        let args = Self::session_args(
            &target.session,
            vec![
                "agent".into(),
                "read".into(),
                target.pane_id.clone(),
                "--source".into(),
                source.as_arg().into(),
                "--lines".into(),
                lines.to_string(),
                "--format".into(),
                "text".into(),
            ],
        )?;
        let text = self.run(&args, Duration::from_secs(5)).await?;
        self.checked_agent(target).await?;
        Ok(text)
    }

    pub async fn wait_agent(&self, target: &AgentTarget, timeout_ms: u64) -> Result<AgentHandle> {
        ensure!(
            (1..=60_000).contains(&timeout_ms),
            "wait timeout must be 1–60000 milliseconds"
        );
        self.checked_agent(target).await?;
        let value = self
            .session_json(
                &target.session,
                vec![
                    "agent".into(),
                    "wait".into(),
                    target.pane_id.clone(),
                    "--timeout".into(),
                    timeout_ms.to_string(),
                ],
                Duration::from_millis(timeout_ms + 5000),
                false,
            )
            .await?;
        let AgentResult::Info { agent } = decode(value)? else {
            bail!("expected agent_info response");
        };
        target.check(&agent)?;
        AgentHandle::new(&target.session, agent)
    }

    pub async fn focus_agent(&self, target: &AgentTarget) -> Result<AgentHandle> {
        self.checked_agent(target).await?;
        let value = self
            .session_json(
                &target.session,
                vec!["agent".into(), "focus".into(), target.pane_id.clone()],
                Duration::from_secs(5),
                true,
            )
            .await?;
        let AgentResult::Info { agent } = decode(value)? else {
            bail!("unexpected focus response; inspect the session before retrying");
        };
        target.check(&agent)?;
        AgentHandle::new(&target.session, agent)
    }
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    use serde_json::json;
    use std::os::unix::fs::PermissionsExt;

    fn target() -> AgentTarget {
        AgentTarget {
            session: "work.test".into(),
            pane_id: "w1:p1".into(),
            terminal_id: "term_test".into(),
            agent: "pi".into(),
            agent_session: None,
        }
    }

    #[tokio::test]
    async fn text_only_prompt_rejects_controls_and_byte_overflow_before_native_calls() {
        let dir = tempfile::tempdir().unwrap();
        let herdr = Herdr::new(dir.path().join("missing-herdr"));
        for text in [
            "".into(),
            " \n\t".into(),
            "bad\0text".into(),
            "bad\rtext".into(),
            "\u{1b}[A".into(),
            "\u{3}".into(),
            "\u{7f}".into(),
            "\u{85}".into(),
            "x".repeat(32769),
            "é".repeat(16385),
        ] {
            let error = herdr
                .prompt_settled_agent(&target(), text)
                .await
                .unwrap_err();
            assert!(error.to_string().contains("UTF-8 bytes"), "{error:#}");
        }
    }

    #[tokio::test]
    async fn reserved_native_global_text_is_rejected_before_metadata_without_uncertainty() {
        let dir = tempfile::tempdir().unwrap();
        let herdr = Herdr::new(dir.path().join("missing-herdr"));
        for option in ["--session", "--remote", "--remote-keybindings"] {
            for text in std::iter::once(option.to_string()).chain(
                [
                    "",
                    "other",
                    "../bad",
                    "-bad",
                    "local",
                    "server",
                    "invalid value",
                    "x\ny",
                ]
                .map(|value| format!("{option}={value}")),
            ) {
                let error = herdr
                    .prompt_settled_agent(&target(), text)
                    .await
                    .unwrap_err();
                let message = format!("{error:#}");
                assert!(
                    message.contains("native herdr CLI global option") && message.contains(option)
                );
                assert!(message.contains("rephrase") && message.contains("no native calls"));
                assert!(!message.contains("possibly submitted") && !message.contains("resend"));
            }
        }
    }

    #[tokio::test]
    async fn settled_prompt_routes_explicit_session_and_literal_text_once() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("herdr");
        let response: Value = serde_json::from_str(include_str!(
            "../../tests/fixtures/herdr-0.9.5-snapshot.json"
        ))
        .unwrap();
        let mut agent = response["result"]["snapshot"]["agents"][0].clone();
        agent["agent_status"] = "idle".into();
        let info = json!({"result": {"type": "agent_info", "agent": agent}});
        let ack =
            json!({"id": "cli:agent:prompt", "result": {"type": "agent_prompted", "agent": agent}});
        let script = format!(
            "#!/bin/sh\nprintf x >> \"$0.calls\"\ntest \"$1\" = '--session=work.test' && test \"$2\" = agent && test \"$4\" = 'w1:p1' || exit 9\ncase \"$3\" in\n get) test \"$#\" = 4 || exit 9; printf '%s' '{info}';;\n prompt) test \"$#\" = 5 && test \"$5\" = '--help' || exit 9; printf '%s' '{ack}';;\n *) exit 9;;\nesac\n"
        );
        std::fs::write(&path, script).unwrap();
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o700)).unwrap();
        let herdr = Herdr::new(path);
        let expected = target();
        let ack = herdr
            .prompt_settled_agent(&expected, "--help".into())
            .await
            .unwrap();
        assert_eq!(ack.target, expected);
        assert!(ack.acknowledged);
        assert_eq!(ack.metadata_timing, "pre_submission");
        assert_eq!(ack.agent.agent_status, "idle");
        assert!(serde_json::to_value(ack).unwrap().get("text").is_none());
        assert_eq!(
            std::fs::read(dir.path().join("herdr.calls")).unwrap(),
            b"xx"
        );
    }
}
