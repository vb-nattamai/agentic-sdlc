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
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

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
# Prior-run context loader
# ---------------------------------------------------------------------------


def load_prior_run_artifacts(from_run_dir: str) -> dict[str, Any]:
    """
    Load ALL artifact JSON files from a previous pipeline run.

    Pre-populates state.artifacts so the orchestrator can see what was already
    built and skip stages that are already complete.  When prior discovery,
    architecture, or spec artifacts are present the LLM skips those stages
    automatically and proceeds directly to extending the existing project.

    Artifacts loaded:
        discovery, architecture, engineering, spec, review  (named JSONs)
        completed_artifacts[<name>]                         (service JSONs)
        existing_spec                                       (openapi.yaml)

    Args:
        from_run_dir: Path to the previous run's root output directory.

    Returns:
        Dict mapping artifact keys to their loaded content.
    """
    loaded: dict[str, Any] = {}
    run_dir = Path(from_run_dir)

    if not run_dir.exists():
        console.print(f"[yellow]Prior run directory not found: {from_run_dir}[/yellow]")
        return loaded

    named: dict[str, str] = {
        "01_discovery_artifact.json": "discovery",
        "02_architecture_artifact.json": "architecture",
        "03_engineering_artifact.json": "engineering",
        "04_generated_spec_artifact.json": "spec",
        "04_review_artifact.json": "review",
    }
    for filename, key in named.items():
        path = run_dir / filename
        if path.exists():
            try:
                loaded[key] = json.loads(path.read_text(encoding="utf-8"))
                console.print(f"[green]✓ Loaded prior {key}[/green]")
            except Exception as exc:  # noqa: BLE001
                console.print(f"[yellow]Could not load {filename}: {exc}[/yellow]")

    for svc_path in sorted(run_dir.glob("*_service_artifact.json")):
        name = svc_path.stem.replace("_service_artifact", "")
        try:
            data = json.loads(svc_path.read_text(encoding="utf-8"))
            loaded.setdefault("completed_artifacts", {})[name] = data
            console.print(f"[green]✓ Loaded prior service '{name}'[/green]")
        except Exception as exc:  # noqa: BLE001
            console.print(f"[yellow]Could not load {svc_path.name}: {exc}[/yellow]")

    openapi_path = run_dir / "generated" / "specs" / "openapi.yaml"
    if openapi_path.exists():
        loaded["existing_spec"] = openapi_path.read_text(encoding="utf-8")
        console.print("[green]✓ Loaded existing OpenAPI spec[/green]")

    console.print(f"[dim]Prior context: {len(loaded)} artifacts loaded from {from_run_dir}[/dim]")
    return loaded


# ---------------------------------------------------------------------------
# Small focused helpers (extracted to keep main() under 15 lines)
# ---------------------------------------------------------------------------


def _resolve_model(args: argparse.Namespace, config: dict) -> str:
    """Pick model: --model flag > PIPELINE_MODEL env > config file > gpt-4o."""
    return args.model or os.environ.get("PIPELINE_MODEL") or config.get("model") or "gpt-4o"


def _resolve_output_dir(args: argparse.Namespace, config: dict) -> str:
    """Pick output directory: flag > config > timestamped default."""
    return (
        args.output_dir
        or config.get("output_dir")
        or f"artifacts/run_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    )


def _resolve_constraints(args: argparse.Namespace, config: dict) -> dict[str, str]:
    """Build constraints dict from --tech-constraints / --arch-constraints / config."""
    constraints: dict[str, str] = {}
    tech = args.tech_constraints or config.get("spec", {}).get("tech_constraints", "")
    arch = args.arch_constraints or config.get("spec", {}).get("arch_constraints", "")
    if tech:
        constraints["tech_constraints"] = tech
    if arch:
        constraints["arch_constraints"] = arch
    return constraints


def _load_spec_files(paths: list[str]) -> list[str]:
    """Read --spec files from disk; warn and skip if a file is missing."""
    specs: list[str] = []
    for spec_path in paths:
        p = Path(spec_path)
        if p.exists():
            specs.append(p.read_text(encoding="utf-8"))
            console.print(f"[green]✓ Loaded spec file: {spec_path}[/green]")
        else:
            console.print(f"[yellow]Spec file not found: {spec_path}[/yellow]")
    return specs


def save_project_context(state: "PipelineState") -> str:  # type: ignore[name-defined]  # noqa: F821
    """
    Write PROJECT_CONTEXT.md to the output directory.

    This is the primary artifact for context-aware incremental development.
    Pass the output directory with --from-run on the next run and the pipeline
    loads every prior artifact automatically — no re-explaining needed.

    Args:
        state: Completed pipeline state.

    Returns:
        Absolute path to the written PROJECT_CONTEXT.md.
    """
    output_dir = Path(state.output_dir)
    req_preview = state.requirements[:400] + ("\u2026" if len(state.requirements) > 400 else "")

    lines: list[str] = [
        f"# Project Context \u2014 {output_dir.name}",
        "",
        "> Auto-generated by [Agentic SDLC](https://github.com/vb-nattamai/agentic-sdlc).",
        "> Pass this directory with `--from-run` to extend the project without re-explaining context.",
        "",
        "## Requirements",
        "",
        "```",
        req_preview,
        "```",
        "",
    ]

    arch = state.artifacts.get("architecture", {})
    if arch:
        lines += ["## Architecture", "", f"**Style**: {arch.get('style', 'unknown')}", ""]
        for c in arch.get("components", []):
            name = c.get("name", "?")
            tech = c.get("technology", c.get("tech", "?"))
            port = c.get("port", "\u2014")
            role = c.get("responsibility", c.get("role", ""))
            lines.append(f"- **{name}** ({tech}, port {port}) \u2014 {role}")
        lines.append("")
        decisions = arch.get("decisions", [])
        if decisions:
            lines.append("**Key Decisions**:")
            for d in decisions[:6]:
                lines.append(f"- {d.get('decision', '?')}: {d.get('rationale', '')}")
            lines.append("")

    if state.active_agents:
        lines += [
            "## Technology Stack", "",
            "| Service | Technology | Port | Role |",
            "|---------|-----------|------|------|",
        ]
        for bp in state.active_agents:
            lines.append(
                f"| {bp.get('name')} | {bp.get('technology')} | "
                f"{bp.get('port') or '\u2014'} | {bp.get('role')} |"
            )
        lines.append("")

    spec = state.artifacts.get("spec", {})
    openapi = (spec.get("openapi_yaml") or "") if spec else ""
    if openapi:
        lines += [
            "## API Contract", "", "```yaml",
            openapi[:800] + ("\u2026" if len(openapi) > 800 else ""),
            "```", "",
        ]

    engineering = state.artifacts.get("engineering", {})
    services = (engineering.get("services") or {}) if engineering else {}
    if services:
        lines += ["## Generated Code", "", "| Service | Files |", "|---------|-------|"]  
        for svc, data in services.items():
            lines.append(f"| {svc} | {len(data.get('files', {}))} files |")
        lines += ["", f"Source files are in `{output_dir}/generated/`.", ""]

    review = state.artifacts.get("review", {})
    if review:
        result = "\u2705 Passed" if review.get("passed") else "\u274c Failed"
        lines += [
            "## Quality Gate", "",
            "| Metric | Score |", "|--------|-------|",
            f"| Security | {review.get('security_score', '?')} |",
            f"| Reliability | {review.get('reliability_score', '?')} |",
            f"| Quality | {review.get('quality_score', '?')} |",
            "", f"**Result**: {result}", "",
        ]

    if state.completed_steps:
        lines += ["## Pipeline Stages Completed", ""]
        lines += [f"- \u2713 {step}" for step in state.completed_steps]
        lines.append("")

    lines += [
        "## Extend This Project",
        "",
        "```bash",
        "# Continue from the last checkpoint",
        f"python3 main.py --resume {output_dir}/checkpoints/step_N.json",
        "",
        "# Add new features — full prior context is loaded automatically",
        f"python3 main.py --from-run {output_dir} --requirements new_features.txt",
        "```",
        "",
        "---",
        f"_Generated {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}_",
    ]

    context_path = output_dir / "PROJECT_CONTEXT.md"
    context_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    console.print(f"[green]\u2713 Project context \u2192 {context_path}[/green]")
    return str(context_path)


def _print_startup_panel(
    state: "PipelineState",  # type: ignore[name-defined]  # noqa: F821
    model: str,
    from_run: str | None,
) -> None:
    """Print the pipeline startup information panel."""
    console.print(
        Panel(
            f"[bold]Model:[/bold]       {model}\n"
            f"[bold]Output dir:[/bold]  {state.output_dir}\n"
            f"[bold]Requirements:[/bold] {len(state.requirements)} chars\n"
            f"[bold]Auto mode:[/bold]   {state.config.get('auto', False)}\n"
            f"[bold]From run:[/bold]    {from_run or 'N/A'}\n"
            f"[bold]Constraints:[/bold] {len(state.constraints)} loaded",
            title="[bold green]\U0001f680 Agentic SDLC Pipeline[/bold green]",
            border_style="green",
        )
    )


async def _execute_pipeline(
    state: "PipelineState",  # type: ignore[name-defined]  # noqa: F821
    auto: bool,
    orchestrator_run: Any,
    pipeline_halt_error: type,
) -> None:
    """Run the orchestrator loop and handle all exit conditions."""
    try:
        final_state = await orchestrator_run(state, auto=auto)
        _print_final_summary(final_state)
    except pipeline_halt_error as exc:
        console.print(Panel(str(exc), title="[red]Pipeline Halted[/red]", border_style="red"))
        sys.exit(1)
    except RuntimeError as exc:
        console.print(Panel(str(exc), title="[red]Pipeline Error[/red]", border_style="red"))
        sys.exit(1)
    except KeyboardInterrupt:
        console.print("\n[yellow]Pipeline interrupted by user.[/yellow]")
        sys.exit(130)


async def _run_resume(
    checkpoint_path: str,
    model: str,
    auto: bool,
    orchestrator_run: Any,
    pipeline_halt_error: type,
) -> None:
    """Load a checkpoint file and resume the pipeline from that point."""
    from checkpoints import load_and_resume
    state = await load_and_resume(checkpoint_path)
    state.config["model"] = model
    await _execute_pipeline(state, auto, orchestrator_run, pipeline_halt_error)


async def _run_fresh(
    args: argparse.Namespace,
    model: str,
    config: dict,
    pipeline_state: type,
    orchestrator_run: Any,
    pipeline_halt_error: type,
) -> None:
    """Build a new PipelineState from CLI args and run a full pipeline."""
    await check_prerequisites(model)
    requirements = load_requirements(args, config)
    output_dir = _resolve_output_dir(args, config)
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    constraints = _resolve_constraints(args, config)
    initial_artifacts: dict[str, Any] = {}

    if args.from_run:
        initial_artifacts = load_prior_run_artifacts(args.from_run)
        if initial_artifacts:
            constraints["incremental_mode"] = (
                f"Extending existing project from {args.from_run}. "
                "All existing API paths must be preserved with x-existing: true. "
                "Prior artifacts are already loaded — skip stages that are already complete."
            )

    spec_files = _load_spec_files(args.spec)
    if spec_files:
        initial_artifacts["spec_files"] = spec_files

    state = pipeline_state(
        requirements=requirements,
        output_dir=output_dir,
        artifacts=initial_artifacts,
        constraints=constraints,
        config={"model": model, "auto": args.auto, "from_run": args.from_run},
    )
    _print_startup_panel(state, model, args.from_run)
    await _execute_pipeline(state, args.auto, orchestrator_run, pipeline_halt_error)


# ---------------------------------------------------------------------------
# Main async entry point
# ---------------------------------------------------------------------------


async def main() -> None:
    """Parse CLI arguments and dispatch to the appropriate pipeline runner."""
    from orchestrator import PipelineState, PipelineHaltError, run as orchestrator_run

    parser = build_parser()
    args = parser.parse_args()
    config = load_config(args.config)
    model = _resolve_model(args, config)

    if args.resume:
        await _run_resume(args.resume, model, args.auto, orchestrator_run, PipelineHaltError)
    else:
        await _run_fresh(args, model, config, PipelineState, orchestrator_run, PipelineHaltError)


def _print_final_summary(state: "PipelineState") -> None:  # type: ignore[name-defined]  # noqa: F821
    """Print the completion panel and write PROJECT_CONTEXT.md."""
    context_path = save_project_context(state)
    completed = "\n".join(f"  \u2713 {s}" for s in state.completed_steps)
    artifacts = "\n".join(f"  \u2022 {k}" for k in state.artifacts)
    console.print(
        Panel(
            f"[bold green]Pipeline completed successfully![/bold green]\n\n"
            f"[bold]Output directory:[/bold] {state.output_dir}\n"
            f"[bold]Project context:[/bold]  {context_path}\n\n"
            f"[bold]Completed stages:[/bold]\n{completed}\n\n"
            f"[bold]Artifacts generated:[/bold]\n{artifacts}",
            title="\u2705 Agentic SDLC Complete",
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
