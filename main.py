"""
Agentic SDLC — CLI entry point.

Parses arguments, validates prerequisites, builds the initial PipelineState,
and hands off to the orchestrator loop.

Usage:
    python3 main.py --requirements my_reqs.txt
    python3 main.py --interactive
    python3 main.py --resume artifacts/run_20260318_120000/checkpoints/step_3.json
    python3 main.py --from-run artifacts/run_20260318_120000 --requirements new_features.txt
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.panel import Panel

console = Console()


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """
    Build the CLI argument parser.

    Returns:
        Configured ArgumentParser instance.
    """
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Agentic SDLC — LLM-orchestrated software development pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 main.py --requirements my_project.txt
  python3 main.py --interactive
  python3 main.py --requirements reqs.txt --auto --model gpt-4o
  python3 main.py --resume artifacts/run_20260318_120000/checkpoints/step_3.json
  python3 main.py --from-run artifacts/run_20260318_120000 --requirements new_features.txt
        """,
    )

    input_group = parser.add_mutually_exclusive_group()
    input_group.add_argument(
        "--requirements",
        metavar="FILE",
        default="requirements.txt",
        help="Path to requirements text file (default: requirements.txt)",
    )
    input_group.add_argument(
        "--interactive",
        action="store_true",
        help="Enter requirements interactively at the terminal (end with Ctrl+D / EOF)",
    )

    parser.add_argument(
        "--config",
        metavar="FILE",
        default=None,
        help="Load settings from pipeline.yaml",
    )
    parser.add_argument(
        "--spec",
        metavar="FILE",
        action="append",
        default=[],
        help="Pass an existing spec file (repeatable). Content included in spec context.",
    )
    parser.add_argument(
        "--tech-constraints",
        metavar="STR",
        default="",
        help="Technology constraints string (e.g. 'must use Kotlin 1.9')",
    )
    parser.add_argument(
        "--arch-constraints",
        metavar="STR",
        default="",
        help="Architectural constraints string (e.g. 'stateless services, JWT only')",
    )
    parser.add_argument(
        "--from-run",
        metavar="DIR",
        default=None,
        help=(
            "Load existing OpenAPI + DDL from a previous run's generated/specs/ directory. "
            "All existing paths will be marked x-existing: true (incremental mode)."
        ),
    )
    parser.add_argument(
        "--output-dir",
        metavar="DIR",
        default=None,
        help="Output directory (default: artifacts/run_YYYYMMDD_HHMMSS)",
    )
    parser.add_argument(
        "--auto",
        action="store_true",
        help="Skip all human checkpoints and run fully automatically",
    )
    parser.add_argument(
        "--resume",
        metavar="FILE",
        default=None,
        help="Resume from a saved checkpoint JSON file",
    )
    parser.add_argument(
        "--model",
        metavar="STR",
        default=None,
        help="Override LLM model (default: gpt-4o or PIPELINE_MODEL env var)",
    )

    return parser


# ---------------------------------------------------------------------------
# Prerequisites check
# ---------------------------------------------------------------------------


async def check_prerequisites(model: str) -> None:
    """
    Verify that required external tools are available before starting the pipeline.

    Checks:
    1. GitHub CLI is authenticated (`gh auth token`)
    2. Docker is running (`docker info`)

    Args:
        model: Model name to display in startup output.

    Raises:
        SystemExit: If GitHub CLI is not authenticated (hard requirement).
    """
    import asyncio

    # Check gh auth
    try:
        proc = await asyncio.create_subprocess_exec(
            "gh", "auth", "token",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0 or not stdout.decode().strip():
            console.print(
                Panel(
                    "[red]GitHub CLI is not authenticated.[/red]\n\n"
                    "Run: [cyan]gh auth login[/cyan]\n\n"
                    "A GitHub account with Copilot or Models API access is required.",
                    title="❌ Authentication Error",
                    border_style="red",
                )
            )
            sys.exit(1)
        console.print("[green]✓ GitHub CLI authenticated[/green]")
    except FileNotFoundError:
        console.print(
            Panel(
                "[red]GitHub CLI ('gh') not found.[/red]\n\n"
                "Install from: [cyan]https://cli.github.com[/cyan]",
                title="❌ Missing Dependency",
                border_style="red",
            )
        )
        sys.exit(1)

    # Check Docker (soft warning — pipeline may not need it immediately)
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "info",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.communicate()
        if proc.returncode == 0:
            console.print("[green]✓ Docker is running[/green]")
        else:
            console.print(
                "[yellow]⚠ Docker is not running. "
                "Infrastructure and deployment stages will fail.[/yellow]"
            )
    except FileNotFoundError:
        console.print(
            "[yellow]⚠ Docker not found. "
            "Infrastructure and deployment stages will fail.[/yellow]"
        )


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


def load_config(config_path: str | None) -> dict:
    """
    Load pipeline.yaml configuration file if provided.

    Args:
        config_path: Path to YAML config file, or None.

    Returns:
        Dict of config values (empty dict if no config file).
    """
    if not config_path:
        return {}

    try:
        import yaml  # type: ignore[import-untyped]
        with open(config_path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        console.print(f"[yellow]Config file not found: {config_path}[/yellow]")
        return {}
    except Exception as exc:  # noqa: BLE001
        console.print(f"[yellow]Failed to load config: {exc}[/yellow]")
        return {}


# ---------------------------------------------------------------------------
# Requirements loading
# ---------------------------------------------------------------------------


def load_requirements(args: argparse.Namespace, config: dict) -> str:
    """
    Load the requirements string from file or interactive stdin.

    Precedence: --interactive > --requirements flag > config file requirements key.

    Args:
        args:   Parsed CLI arguments.
        config: Loaded pipeline config dict.

    Returns:
        Requirements text string.

    Raises:
        SystemExit: If requirements cannot be loaded.
    """
    if args.interactive:
        console.print(
            Panel(
                "Enter your requirements below.\n"
                "End with [bold]Ctrl+D[/bold] (Unix) or [bold]Ctrl+Z[/bold] (Windows).",
                title="Interactive Requirements Input",
                border_style="cyan",
            )
        )
        lines = []
        try:
            while True:
                line = input()
                lines.append(line)
        except EOFError:
            pass
        requirements = "\n".join(lines).strip()
        if not requirements:
            console.print("[red]No requirements entered. Exiting.[/red]")
            sys.exit(1)
        return requirements

    # File-based requirements
    req_path = args.requirements
    if not req_path and config.get("requirements"):
        req_path = config["requirements"]

    if not req_path:
        console.print("[red]No requirements source specified. Use --requirements or --interactive.[/red]")
        sys.exit(1)

    req_file = Path(req_path)
    if not req_file.exists():
        console.print(f"[red]Requirements file not found: {req_path}[/red]")
        sys.exit(1)

    return req_file.read_text(encoding="utf-8").strip()


# ---------------------------------------------------------------------------
# from-run: load existing spec
# ---------------------------------------------------------------------------


def load_existing_spec(from_run_dir: str) -> str:
    """
    Load existing OpenAPI YAML from a previous run's generated/specs/ directory.

    Args:
        from_run_dir: Path to the previous run's root output directory.

    Returns:
        OpenAPI YAML string with existing content (or empty string if not found).
    """
    specs_dir = Path(from_run_dir) / "generated" / "specs"
    openapi_path = specs_dir / "openapi.yaml"

    if openapi_path.exists():
        content = openapi_path.read_text(encoding="utf-8")
        console.print(f"[green]✓ Loaded existing spec from {openapi_path}[/green]")
        return content

    console.print(f"[yellow]No openapi.yaml found in {specs_dir}[/yellow]")
    return ""


# ---------------------------------------------------------------------------
# Main async entry point
# ---------------------------------------------------------------------------


async def main() -> None:
    """
    Main async entry point for the Agentic SDLC CLI.

    Parses args, validates prerequisites, builds state, and starts the pipeline.
    """
    import os

    from orchestrator import PipelineState, PipelineHaltError, run as orchestrator_run

    parser = build_parser()
    args = parser.parse_args()

    # Determine model
    model = (
        args.model
        or os.environ.get("PIPELINE_MODEL")
        or "gpt-4o"
    )

    # Load config
    config = load_config(args.config)
    if not args.model and config.get("model"):
        model = config["model"]

    # ------------------------------------------------------------------
    # Resume mode
    # ------------------------------------------------------------------
    if args.resume:
        from checkpoints import load_and_resume
        state = await load_and_resume(args.resume)
        state.config["model"] = model
        try:
            final_state = await orchestrator_run(state, auto=args.auto)
            _print_final_summary(final_state)
        except PipelineHaltError as exc:
            console.print(
                Panel(str(exc), title="[red]Pipeline Halted[/red]", border_style="red")
            )
            sys.exit(1)
        return

    # ------------------------------------------------------------------
    # Fresh run
    # ------------------------------------------------------------------
    await check_prerequisites(model)

    requirements = load_requirements(args, config)

    # Output directory
    output_dir = (
        args.output_dir
        or config.get("output_dir")
        or f"artifacts/run_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    )
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    # Build constraints dict
    constraints: dict[str, str] = {}
    tech = args.tech_constraints or config.get("spec", {}).get("tech_constraints", "")
    arch = args.arch_constraints or config.get("spec", {}).get("arch_constraints", "")
    if tech:
        constraints["tech_constraints"] = tech
    if arch:
        constraints["arch_constraints"] = arch

    # --from-run: load existing spec into initial artifacts
    initial_artifacts: dict = {}
    if args.from_run:
        existing_spec = load_existing_spec(args.from_run)
        if existing_spec:
            initial_artifacts["existing_spec"] = existing_spec
            constraints["incremental_mode"] = (
                f"Extending existing spec from {args.from_run}. "
                "All existing API paths must be preserved with x-existing: true."
            )

    # Load any --spec files
    spec_files = []
    for spec_path in args.spec:
        p = Path(spec_path)
        if p.exists():
            spec_files.append(p.read_text(encoding="utf-8"))
            console.print(f"[green]✓ Loaded spec file: {spec_path}[/green]")
        else:
            console.print(f"[yellow]Spec file not found: {spec_path}[/yellow]")

    if spec_files:
        initial_artifacts["spec_files"] = spec_files

    # Build initial state
    state = PipelineState(
        requirements=requirements,
        output_dir=output_dir,
        artifacts=initial_artifacts,
        constraints=constraints,
        config={
            "model": model,
            "auto": args.auto,
            "from_run": args.from_run,
        },
    )

    # Startup panel
    console.print(
        Panel(
            f"[bold]Model:[/bold]       {model}\n"
            f"[bold]Output dir:[/bold]  {output_dir}\n"
            f"[bold]Requirements:[/bold] {len(requirements)} chars\n"
            f"[bold]Auto mode:[/bold]   {args.auto}\n"
            f"[bold]From run:[/bold]    {args.from_run or 'N/A'}\n"
            f"[bold]Constraints:[/bold] {len(constraints)} loaded",
            title="[bold green]🚀 Agentic SDLC Pipeline[/bold green]",
            border_style="green",
        )
    )

    try:
        final_state = await orchestrator_run(state, auto=args.auto)
        _print_final_summary(final_state)
    except PipelineHaltError as exc:
        console.print(
            Panel(str(exc), title="[red]Pipeline Halted[/red]", border_style="red")
        )
        sys.exit(1)
    except RuntimeError as exc:
        # Covers LLM rate-limit and connection failures that escape the orchestrator
        console.print(
            Panel(
                str(exc),
                title="[red]Pipeline Error[/red]",
                border_style="red",
            )
        )
        sys.exit(1)
    except KeyboardInterrupt:
        console.print("\n[yellow]Pipeline interrupted by user.[/yellow]")
        sys.exit(130)


def _print_final_summary(state: "PipelineState") -> None:  # type: ignore[name-defined]  # noqa: F821
    """
    Print a final summary panel after pipeline completion.

    Args:
        state: Final pipeline state.
    """
    completed = "\n".join(f"  ✓ {s}" for s in state.completed_steps)
    artifacts = "\n".join(f"  • {k}" for k in state.artifacts)
    console.print(
        Panel(
            f"[bold green]Pipeline completed successfully![/bold green]\n\n"
            f"[bold]Output directory:[/bold] {state.output_dir}\n\n"
            f"[bold]Completed stages:[/bold]\n{completed}\n\n"
            f"[bold]Artifacts generated:[/bold]\n{artifacts}",
            title="✅ Agentic SDLC Complete",
            border_style="green",
        )
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def cli() -> None:
    """Synchronous console-script entry point (wraps the async main coroutine)."""
    asyncio.run(main())


if __name__ == "__main__":
    asyncio.run(main())
