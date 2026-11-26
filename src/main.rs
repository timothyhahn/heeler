mod assets;

use std::io::{self, Write};
use std::path::PathBuf;

use anyhow::{Context, Result};
use clap::{Parser, Subcommand, ValueEnum};
use heeler::{herdr::Herdr, mcp::Heeler, store::Store};
use rmcp::ServiceExt;

/// Heeler's stdio MCP entry point, launched by your coding-agent harness.
#[derive(Parser)]
#[command(version, about)]
struct Args {
    /// Directory for private workstream notes. Defaults to the OS local data directory/heeler.
    #[arg(long, env = "HEELER_STATE_DIR")]
    state_dir: Option<PathBuf>,
    /// Local herdr executable. No shell command strings are accepted.
    #[arg(long, env = "HEELER_HERDR_BIN", default_value = "herdr")]
    herdr_bin: PathBuf,
    #[command(subcommand)]
    command: Option<Command>,
}

#[derive(Subcommand)]
enum Command {
    /// Print static portable instructions to stdout without starting MCP or accessing state/herdr.
    Export {
        #[arg(value_enum)]
        format: ExportFormat,
    },
}

#[derive(Clone, Copy, ValueEnum)]
enum ExportFormat {
    /// Exact canonical lead instructions; reading them does not authorize adoption.
    Lead,
    /// Standalone skill, for explicitly selected manual-only loading.
    Skill,
}

#[tokio::main]
async fn main() -> Result<()> {
    let args = Args::try_parse().unwrap_or_else(|error| {
        if error.use_stderr() {
            // Invalid operands can contain arbitrary text; never echo an unbounded diagnostic.
            eprintln!(
                "{}",
                error.to_string().chars().take(4096).collect::<String>()
            );
            std::process::exit(error.exit_code());
        }
        error.exit()
    });
    if let Some(Command::Export { format }) = args.command {
        let mut output = io::stdout().lock();
        match format {
            ExportFormat::Lead => assets::write_lead(&mut output),
            ExportFormat::Skill => assets::write_skill(&mut output),
        }
        .context("could not export instructions to stdout")?;
        output
            .flush()
            .context("could not flush exported instructions")?;
        return Ok(());
    }
    let state_dir = match args.state_dir {
        Some(path) => path,
        None => dirs::data_local_dir()
            .context("no local data directory; specify --state-dir")?
            .join("heeler"),
    };
    let service = Heeler::new(Store::new(state_dir), Herdr::new(args.herdr_bin))
        .serve(rmcp::transport::stdio())
        .await?;
    service.waiting().await?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn static_exports_are_optional_and_reject_extra_controls() {
        assert!(Args::try_parse_from(["heeler"]).unwrap().command.is_none());
        for name in ["lead", "skill"] {
            assert!(matches!(
                Args::try_parse_from(["heeler", "export", name])
                    .unwrap()
                    .command,
                Some(Command::Export { .. })
            ));
        }
        for args in [
            vec!["heeler", "export"],
            vec!["heeler", "export", "unknown"],
            vec!["heeler", "export", "lead", "extra"],
            vec!["heeler", "export", "skill", "--output", "somewhere"],
            vec!["heeler", "install"],
        ] {
            assert!(Args::try_parse_from(args).is_err());
        }
    }
}
