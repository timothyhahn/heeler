use std::path::PathBuf;

use anyhow::{Context, Result};
use clap::Parser;
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
}

#[tokio::main]
async fn main() -> Result<()> {
    let args = Args::parse();
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
