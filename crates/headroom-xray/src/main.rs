//! `headroom-xray` binary entrypoint.

use anyhow::Result;
use clap::Parser;

/// headroom xray — multi-CLI context-bloat diagnostics.
///
/// Wraps CodeBurn (https://github.com/getagentseal/codeburn, MIT) and adds
/// a Headroom-specific compression-opportunity footer.
#[derive(Parser, Debug)]
#[command(name = "headroom-xray", version, about, long_about = None)]
struct Cli {
    /// Suppress the Headroom footer (CodeBurn output only).
    #[arg(long, env = "HEADROOM_XRAY_NO_FOOTER")]
    no_footer: bool,

    /// Emit debug logs about the footer pipeline to stderr.
    #[arg(long)]
    xray_debug: bool,

    /// Show CodeBurn's own --help (not headroom-xray's wrapper help).
    #[arg(long, conflicts_with_all = ["no_footer", "codeburn_args"])]
    help_codeburn: bool,

    /// All arguments forwarded to CodeBurn (e.g., `report`, `today`, `optimize`).
    #[arg(trailing_var_arg = true, allow_hyphen_values = true)]
    codeburn_args: Vec<String>,
}

#[tokio::main]
async fn main() -> Result<()> {
    // We bind `_cli` to verify the parser accepts all defined flags. The
    // stub does no real work; subsequent tasks replace this body.
    let _cli = Cli::parse();

    // TODO Task 2: Node detection.
    // TODO Task 3: spawn npx codeburn and forward stdio.
    // TODO Tasks 4-7: footer pipeline.

    eprintln!("headroom-xray: scaffolding only — binary not yet wired");
    Ok(())
}
