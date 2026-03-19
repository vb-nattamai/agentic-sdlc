"""
Human control layer for the Agentic SDLC pipeline.

Provides interactive checkpoints where a human can:
- Proceed (press Enter)
- Inject a constraint that the orchestrator will act on
- Edit an artifact directly
- Save state and abort the run
- Resume from a saved checkpoint
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

console = Console()


async def human_checkpoint(
    reason: str,
    state: "PipelineState",  # type: ignore[name-defined]  # noqa: F821
    proposed_action: dict,
    auto: bool = False,
) -> bool:
    """
    Pause the pipeline and present options to the human operator.

    If `auto=True` or stdin is not a TTY (CI/CD environment), the checkpoint
    is logged silently and returns True (continue).

    Checkpoint is ALWAYS saved to disk BEFORE displaying the prompt, so
    the state is safe even if the operator kills the process.

    Options available at the prompt:
    - [Enter]     → Continue with proposed action
    - c <text>    → Inject a constraint; orchestrator will re-plan
    - e <name>    → Edit a named artifact via /tmp file
    - s           → Print resume command and return False (save-and-abort)
    - a           → Abort immediately (return False, no resume)

    Args:
        reason:          Why human review is required.
        state:           Current pipeline state.
        proposed_action: Dict with "action" and "params" keys.
        auto:            If True, skip interaction entirely.

    Returns:
        True  → Continue pipeline.
        False → Abort pipeline (state is already saved).
    """
    # Import here to avoid circular import
    from orchestrator import PipelineState  # noqa: F401 — used for type hint

    # Save checkpoint BEFORE displaying prompt
    checkpoint_dir = Path(state.output_dir) / "checkpoints"
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_path = checkpoint_dir / f"step_{len(state.completed_steps)}.json"
    state.save(str(checkpoint_path))

    # Silent continuation in auto/non-interactive mode
    if auto or not sys.stdin.isatty():
        console.print(
            Text(
                f"[AUTO] Checkpoint skipped: {reason}",
                style="dim yellow",
            )
        )
        return True

    # Build display panel
    artifact_list = "\n".join(f"  • {k}" for k in state.artifacts) or "  (none yet)"
    completed_list = "\n".join(f"  ✓ {s}" for s in state.completed_steps) or "  (none yet)"

    params_summary = json.dumps(proposed_action.get("params", {}), indent=4)[:500]
    if len(json.dumps(proposed_action.get("params", {}))) > 500:
        params_summary += "\n  ... [truncated]"

    console.print(
        Panel(
            f"[bold yellow]Reason:[/bold yellow] {reason}\n\n"
            f"[bold]Proposed next action:[/bold] [cyan]{proposed_action.get('action')}[/cyan]\n"
            f"[dim]{params_summary}[/dim]\n\n"
            f"[bold]Completed steps:[/bold]\n{completed_list}\n\n"
            f"[bold]Available artifacts:[/bold]\n{artifact_list}\n\n"
            f"[bold]Commands:[/bold]\n"
            f"  [green][Enter][/green]   → Continue\n"
            f"  [green]c <text>[/green]  → Inject constraint and continue\n"
            f"  [green]e <name>[/green]  → Edit artifact by name\n"
            f"  [green]s[/green]         → Save state and abort (resumable)\n"
            f"  [green]a[/green]         → Abort immediately\n\n"
            f"[dim]Checkpoint saved → {checkpoint_path}[/dim]",
            title="[bold yellow]⏸  Human Review Required[/bold yellow]",
            border_style="yellow",
        )
    )

    while True:
        try:
            user_input = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[yellow]Interrupted — aborting pipeline.[/yellow]")
            return False

        # ------ [Enter] / empty — continue ------
        if user_input == "":
            console.print("[green]Continuing pipeline...[/green]")
            return True

        # ------ c <text> — inject constraint ------
        if user_input.lower().startswith("c "):
            constraint_text = user_input[2:].strip()
            if constraint_text:
                key = f"human_{len(state.constraints)}"
                state.constraints[key] = constraint_text
                console.print(
                    f"[green]Constraint added:[/green] {constraint_text}\n"
                    "[dim]Orchestrator will re-plan with this constraint.[/dim]"
                )
                return True
            else:
                console.print("[yellow]Usage: c <constraint text>[/yellow]")
                continue

        # ------ e <name> — edit artifact ------
        if user_input.lower().startswith("e "):
            artifact_name = user_input[2:].strip()
            if artifact_name in state.artifacts:
                tmp_path = f"/tmp/edit_{artifact_name}.json"
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(state.artifacts[artifact_name], f, indent=2)
                console.print(
                    f"[cyan]Artifact written to:[/cyan] {tmp_path}\n"
                    "Edit the file, then press [Enter] to reload it."
                )
                try:
                    input("[Press Enter after editing] ")
                except (EOFError, KeyboardInterrupt):
                    console.print("[yellow]Edit cancelled.[/yellow]")
                    continue

                try:
                    with open(tmp_path, encoding="utf-8") as f:
                        state.artifacts[artifact_name] = json.load(f)
                    console.print(f"[green]Artifact '{artifact_name}' reloaded.[/green]")
                    # Save updated state
                    state.save(str(checkpoint_path))
                except (json.JSONDecodeError, OSError) as exc:
                    console.print(f"[red]Failed to reload artifact: {exc}[/red]")
                continue

            else:
                console.print(
                    f"[yellow]Unknown artifact '{artifact_name}'.[/yellow]\n"
                    f"Available: {', '.join(state.artifacts.keys()) or 'none'}"
                )
                continue

        # ------ s — save and abort (resumable) ------
        if user_input.lower() == "s":
            resume_cmd = f"python3 main.py --resume {checkpoint_path}"
            console.print(
                Panel(
                    f"[bold]State saved.[/bold] Resume with:\n\n"
                    f"[cyan]{resume_cmd}[/cyan]",
                    title="Saved & Aborted",
                    border_style="yellow",
                )
            )
            return False

        # ------ a — abort immediately ------
        if user_input.lower() == "a":
            console.print("[red]Aborting pipeline.[/red]")
            return False

        console.print(
            "[yellow]Unknown command. Options: [Enter], c <text>, e <name>, s, a[/yellow]"
        )


async def load_and_resume(checkpoint_path: str) -> "PipelineState":  # type: ignore[name-defined]  # noqa: F821
    """
    Load a PipelineState from a checkpoint file for pipeline resumption.

    Args:
        checkpoint_path: Path to a checkpoint JSON file saved by human_checkpoint
                         or state.save().

    Returns:
        Reconstructed PipelineState ready to be passed to orchestrator.run().

    Raises:
        FileNotFoundError: If checkpoint_path does not exist.
        json.JSONDecodeError: If the file is not valid JSON.
    """
    from orchestrator import PipelineState

    if not Path(checkpoint_path).exists():
        raise FileNotFoundError(f"Checkpoint not found: {checkpoint_path}")

    state = PipelineState.load(checkpoint_path)

    console.print(
        Panel(
            f"[bold green]Resuming pipeline[/bold green]\n"
            f"Checkpoint: {checkpoint_path}\n"
            f"Completed steps: {', '.join(state.completed_steps) or 'none'}\n"
            f"Artifacts: {', '.join(state.artifacts.keys()) or 'none'}",
            title="Resume",
            border_style="green",
        )
    )

    return state
