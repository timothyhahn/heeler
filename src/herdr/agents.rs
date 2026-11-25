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
