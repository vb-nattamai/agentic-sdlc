"""
Orchestrator — the dynamic decision engine of the Agentic SDLC pipeline.

After every tool call, the orchestrator LLM reads the full PipelineState summary
and decides exactly one next action. Nothing is predetermined — the flow is driven
entirely by the LLM reading state and reasoning about what is needed next.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from agents.base_agent import query_llm
from tools.registry import TOOL_REGISTRY

console = Console()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_ITERATIONS = 60
TOOL_OUTPUT_TRUNCATE = 2000
TOOL_HISTORY_WINDOW = 5
LOOP_DETECTION_WINDOW = 5


# ---------------------------------------------------------------------------
# OrchestratorDecision — the structured output the LLM must produce
# ---------------------------------------------------------------------------


class OrchestratorDecision(BaseModel):
    """Single decision returned by the orchestrator LLM each iteration."""

    reasoning: str
    action: str
    params: dict[str, Any]
    requires_human_review: bool = False
    human_review_reason: str | None = None
    done: bool = False
    done_reason: str | None = None


# ---------------------------------------------------------------------------
# PipelineHaltError
# ---------------------------------------------------------------------------


class PipelineHaltError(Exception):
    """Raised when the pipeline cannot continue and must stop."""

    def __init__(self, message: str, state: "PipelineState") -> None:
        super().__init__(message)
        self.state = state


# ---------------------------------------------------------------------------
# PipelineState
# ---------------------------------------------------------------------------


@dataclass
class PipelineState:
    """
    Full mutable state of a pipeline run.

    Persisted to disk as JSON after every iteration so the run can be resumed.
    """

    requirements: str
    output_dir: str
    artifacts: dict[str, Any] = field(default_factory=dict)
    tool_history: list[dict[str, Any]] = field(default_factory=list)
    completed_steps: list[str] = field(default_factory=list)
    failed_attempts: dict[str, int] = field(default_factory=dict)
    constraints: dict[str, str] = field(default_factory=dict)
    config: dict[str, Any] = field(default_factory=dict)
    active_agents: list[dict[str, Any]] = field(default_factory=list)
    """
    AgentBlueprint dicts populated by extract_blueprints after architecture is known.
    The orchestrator reads this to decide which spawn_agent calls to make.
    Empty until the architecture stage completes and extract_blueprints runs.
    """

    def compact_summary(self) -> str:
        """
        Build a compact JSON summary for the orchestrator LLM.

        Includes completed steps, artifact keys, last 8 tool history entries
        (output truncated to 300 chars, file content replaced with a size hint),
        and non-zero failed attempts.

        Returns:
            JSON string of the summary dict.
        """
        recent_history = []
        for entry in self.tool_history[-TOOL_HISTORY_WINDOW:]:
            compact_entry = dict(entry)
            raw_out = str(compact_entry.get("output", ""))
            action = compact_entry.get("action", "")
            # Replace raw file content with a size hint to prevent YAML/code
            # in tool history from breaking the orchestrator's JSON response.
            if action in ("file_read",) and len(raw_out) > 200:
                compact_entry["output"] = f"[file content {len(raw_out)} chars — use file_read again to inspect]"
            elif len(raw_out) > 300:
                compact_entry["output"] = raw_out[:300] + "[truncated]"
            recent_history.append(compact_entry)

        # Summarise blueprints: show name, role, technology only (skip file contents)
        agent_summary = [
            {
                "name": bp.get("name"),
                "role": bp.get("role"),
                "technology": bp.get("technology"),
                "port": bp.get("port"),
                "output_subdir": bp.get("output_subdir"),
                "depends_on": bp.get("depends_on", []),
            }
            for bp in self.active_agents
        ]

        summary = {
            "completed_steps": self.completed_steps,
            "artifacts_available": list(self.artifacts.keys()),
            "active_agents": agent_summary,
            "recent_tool_history": recent_history,
            "failed_attempts": {k: v for k, v in self.failed_attempts.items() if v > 0},
            "constraints": self.constraints,
            "output_dir": self.output_dir,
            "requirements_length": len(self.requirements),
            # Include full text only until discovery has run — after that the
            # discovery artifact carries the structured version, saving tokens.
            "requirements": (
                self.requirements[:2000] + ("[truncated]" if len(self.requirements) > 2000 else "")
                if "discovery" not in self.completed_steps
                else "[see discovery artifact]"
            ),
        }
        return json.dumps(summary, indent=2)

    def save(self, path: str) -> None:
        """
        Atomically persist the pipeline state to disk.

        Writes to a .tmp file first, then renames to the target path to ensure
        the file is never left in a partially-written state.

        Args:
            path: Target file path for the JSON state file.
        """
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        tmp = path + ".tmp"

        state_dict = {
            "requirements": self.requirements,
            "output_dir": self.output_dir,
            "artifacts": self.artifacts,
            "tool_history": self.tool_history,
            "completed_steps": self.completed_steps,
            "failed_attempts": self.failed_attempts,
            "constraints": self.constraints,
            "config": self.config,
            "active_agents": self.active_agents,
        }

        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state_dict, f, indent=2, default=str)

        os.replace(tmp, path)

    @classmethod
    def load(cls, path: str) -> "PipelineState":
        """
        Load a PipelineState from a JSON file.

        Args:
            path: Path to the JSON state file.

        Returns:
            Reconstructed PipelineState instance.

        Raises:
            FileNotFoundError: If the path does not exist.
            json.JSONDecodeError: If the file is not valid JSON.
        """
        with open(path, encoding="utf-8") as f:
            data = json.load(f)

        return cls(
            requirements=data.get("requirements", ""),
            output_dir=data.get("output_dir", ""),
            artifacts=data.get("artifacts", {}),
            tool_history=data.get("tool_history", []),
            completed_steps=data.get("completed_steps", []),
            failed_attempts=data.get("failed_attempts", {}),
            constraints=data.get("constraints", {}),
            config=data.get("config", {}),
            active_agents=data.get("active_agents", []),
        )


# ---------------------------------------------------------------------------
# Orchestrator system prompt loader
# ---------------------------------------------------------------------------


def _load_orchestrator_prompt() -> str:
    """
    Load the orchestrator system prompt from prompts/orchestrator.md.

    Searches relative to this file's location.

    Returns:
        Prompt string.

    Raises:
        FileNotFoundError: If the prompt file cannot be found.
    """
    candidates = [
        Path("prompts/orchestrator.md"),
        Path(__file__).parent / "prompts" / "orchestrator.md",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate.read_text(encoding="utf-8")
    raise FileNotFoundError("prompts/orchestrator.md not found")


# ---------------------------------------------------------------------------
# Loop detection
# ---------------------------------------------------------------------------


def _detect_loop(tool_history: list[dict[str, Any]], action: str, params: dict[str, Any]) -> bool:
    """
    Detect if the same (action, params) pair has appeared in the last N entries.

    Args:
        tool_history: Full tool history list.
        action:       Current action being considered.
        params:       Current params being considered.

    Returns:
        True if a loop is detected.
    """
    params_str = json.dumps(params, sort_keys=True)
    count = 0
    for entry in tool_history[-LOOP_DETECTION_WINDOW:]:
        if entry.get("action") == action and json.dumps(
            entry.get("params", {}), sort_keys=True
        ) == params_str:
            count += 1
    return count >= 2


# ---------------------------------------------------------------------------
# Main orchestrator loop
# ---------------------------------------------------------------------------


async def run(state: PipelineState, auto: bool = False) -> PipelineState:
    """
    Execute the main orchestrator loop until the pipeline completes or halts.

    Each iteration:
    1. Ask the LLM for the next decision (OrchestratorDecision)
    2. Handle human review checkpoints
    3. Execute the chosen tool
    4. Update state and detect loops
    5. Persist state to disk

    Args:
        state: The current pipeline state (may be freshly created or resumed).
        auto:  If True, skip all human checkpoints and continue automatically.

    Returns:
        Final PipelineState after completion or abort.

    Raises:
        PipelineHaltError: If the pipeline exceeds MAX_ITERATIONS or repeatedly
                           fails to parse the LLM response.
    """
    from checkpoints import human_checkpoint

    orchestrator_prompt = _load_orchestrator_prompt()
    state_file = str(Path(state.output_dir) / "orchestrator_state.json")

    console.print(
        Panel(
            f"[bold green]Agentic SDLC Pipeline Starting[/bold green]\n"
            f"Output dir: {state.output_dir}\n"
            f"Requirements: {len(state.requirements)} chars\n"
            f"Auto mode: {auto}",
            title="Pipeline",
            border_style="green",
        )
    )

    for iteration in range(1, MAX_ITERATIONS + 1):
        # ------------------------------------------------------------------ #
        # Step 1: Ask the orchestrator LLM for the next decision              #
        # ------------------------------------------------------------------ #
        summary = state.compact_summary()
        decision = await _get_decision(
            orchestrator_prompt=orchestrator_prompt,
            summary=summary,
            state=state,
            model=state.config.get("model", "gpt-4o"),
            iteration=iteration,
        )

        # ------------------------------------------------------------------ #
        # Step 2: Display the decision                                        #
        # ------------------------------------------------------------------ #
        console.print(
            Panel(
                f"[cyan]{decision.reasoning}[/cyan]",
                title=f"[bold]Step {iteration}: {decision.action}[/bold]",
                border_style="blue",
            )
        )

        # ------------------------------------------------------------------ #
        # Step 3: Handle completion                                           #
        # ------------------------------------------------------------------ #
        if decision.done:
            console.print(
                Panel(
                    f"[bold green]Pipeline complete![/bold green]\n\n"
                    f"{decision.done_reason or 'All stages passed.'}",
                    title="✅ Done",
                    border_style="green",
                )
            )
            state.save(state_file)
            return state

        # ------------------------------------------------------------------ #
        # Step 4: Human checkpoint                                           #
        # ------------------------------------------------------------------ #
        if decision.requires_human_review:
            should_continue = await human_checkpoint(
                reason=decision.human_review_reason or "Human review required",
                state=state,
                proposed_action={"action": decision.action, "params": decision.params},
                auto=auto,
            )
            if not should_continue:
                console.print("[yellow]Pipeline aborted by human. State saved.[/yellow]")
                state.save(state_file)
                return state

        # ------------------------------------------------------------------ #
        # Step 5: Validate action                                             #
        # ------------------------------------------------------------------ #
        if decision.action == "none":
            # Orchestrator chose to wait (e.g. after a checkpoint was skipped
            # in auto mode).  Record it and let the loop continue so the LLM
            # can decide the next real action on the next iteration.
            state.tool_history.append({
                "iteration": iteration,
                "action": "none",
                "params": {},
                "success": True,
                "output": "no-op",
            })
            state.save(state_file)
            continue

        if decision.action not in TOOL_REGISTRY:
            console.print(
                Panel(
                    f"Unknown action: {decision.action}\nAvailable: {sorted(TOOL_REGISTRY.keys())}",
                    title="[red]Invalid Action[/red]",
                    border_style="red",
                )
            )
            state.tool_history.append({
                "iteration": iteration,
                "action": decision.action,
                "params": decision.params,
                "success": False,
                "output": "",
                "error": f"Unknown action: {decision.action}",
            })
            continue

        # ------------------------------------------------------------------ #
        # Step 6: Loop detection                                              #
        # ------------------------------------------------------------------ #
        if _detect_loop(state.tool_history, decision.action, decision.params):
            constraint_key = f"loop_{iteration}"
            constraint_msg = (
                f"Repeated {decision.action} failures — try a different approach "
                "or use file_patch instead of re-running the full agent"
            )
            state.constraints[constraint_key] = constraint_msg
            state.failed_attempts[decision.action] = (
                state.failed_attempts.get(decision.action, 0) + 1
            )
            console.print(
                Panel(
                    f"[yellow]Loop detected for action '{decision.action}'. "
                    f"Injecting constraint: {constraint_msg}[/yellow]",
                    title="⚠️  Loop Detection",
                    border_style="yellow",
                )
            )

        # ------------------------------------------------------------------ #
        # Step 7: Execute the tool                                            #
        # ------------------------------------------------------------------ #

        # Ensure agent-calling tools always receive the correct model and
        # output_dir even if the orchestrator LLM omits them from its params.
        params = dict(decision.params)
        if decision.action in ("delegate_agent", "spawn_agent", "extract_blueprints"):
            model_name = state.config.get("model", "gpt-4o")
            if decision.action in ("delegate_agent", "spawn_agent"):
                ctx = dict(params.get("context", {}))
                ctx["model"] = model_name  # always force — LLM may suggest wrong model
                # Auto-inject requirements for discovery so it always gets the text
                if params.get("agent_name") == "discovery":
                    ctx.setdefault("requirements", state.requirements)
                    ctx.setdefault("constraints", state.constraints)
                params["context"] = ctx
            if decision.action in ("spawn_agent", "delegate_agent"):
                params.setdefault("output_dir", state.output_dir)
            if decision.action == "extract_blueprints":
                params["model"] = model_name  # always force — LLM may suggest wrong model
                params.setdefault("output_dir", state.output_dir)
                # Auto-inject architecture artifact so LLM doesn't need to embed it
                if "architecture" not in params and "architecture" in state.artifacts:
                    params["architecture"] = state.artifacts["architecture"]

        tool_fn = TOOL_REGISTRY[decision.action]
        try:
            result = await tool_fn(**params)
        except TypeError as exc:
            # Wrong params — record as failure and continue
            result_output = f"Parameter error: {exc}"
            state.tool_history.append({
                "iteration": iteration,
                "action": decision.action,
                "params": decision.params,
                "success": False,
                "output": result_output,
                "error": result_output,
            })
            console.print(
                Panel(result_output, title="[red]Tool Parameter Error[/red]", border_style="red")
            )
            state.save(state_file)
            continue

        # Truncate output for history / display
        truncated_output = (
            result.output[:TOOL_OUTPUT_TRUNCATE] + "[truncated]"
            if len(result.output) > TOOL_OUTPUT_TRUNCATE
            else result.output
        )

        # ------------------------------------------------------------------ #
        # Step 8: Record result                                               #
        # ------------------------------------------------------------------ #
        history_entry: dict[str, Any] = {
            "iteration": iteration,
            "action": decision.action,
            "params": decision.params,
            "success": result.success,
            "output": truncated_output,
        }
        if result.error:
            history_entry["error"] = result.error[:500]

        state.tool_history.append(history_entry)

        if result.success:
            console.print(
                Text(
                    f"✓ {decision.action} succeeded: {truncated_output[:200]}",
                    style="green",
                )
            )

            # Update artifacts and completed_steps for delegate_agent calls
            if decision.action == "delegate_agent":
                agent_name = decision.params.get("agent_name", "")
                try:
                    artifact_data = json.loads(result.output)
                    state.artifacts[agent_name] = artifact_data
                    step_key = _step_key(agent_name, decision.params)
                    if step_key not in state.completed_steps:
                        state.completed_steps.append(step_key)
                except ValueError:
                    pass  # Output may not be JSON — that's OK

            # Store blueprints when extract_blueprints succeeds
            elif decision.action == "extract_blueprints":
                try:
                    blueprints = json.loads(result.output)
                    if isinstance(blueprints, list):
                        state.active_agents = blueprints
                        names = [b.get("name") for b in blueprints]
                        console.print(
                            Text(
                                f"✓ Active agents set from architecture: {names}",
                                style="green",
                            )
                        )
                        if "blueprints_extracted" not in state.completed_steps:
                            state.completed_steps.append("blueprints_extracted")
                except json.JSONDecodeError:
                    pass

            # Track spawn_agent completions
            elif decision.action == "spawn_agent":
                bp = decision.params.get("blueprint", {})
                agent_name = bp.get("name", "")
                if agent_name:
                    try:
                        artifact_data = json.loads(result.output)
                        state.artifacts[agent_name] = artifact_data
                        step_key = f"spawn_{agent_name}"
                        if step_key not in state.completed_steps:
                            state.completed_steps.append(step_key)
                    except json.JSONDecodeError:
                        pass

        else:
            state.failed_attempts[decision.action] = (
                state.failed_attempts.get(decision.action, 0) + 1
            )
            error_str = result.error or ""
            # Halt immediately on rate-limit errors — no point retrying;
            # every iteration burns more daily quota.
            if "RateLimitReached" in error_str or "rate limit" in error_str.lower():
                state.save(state_file)
                raise PipelineHaltError(
                    f"GitHub Models rate limit hit at iteration {iteration}. "
                    "Resume with --resume when the limit resets (check the "
                    "'Please wait N seconds' value in the error above).\n\n"
                    f"Resume command:\n  python3 main.py --resume "
                    f"{state.output_dir}/checkpoints/step_{len(state.completed_steps)}.json "
                    f"--model {state.config.get('model', 'gpt-4o-mini')} --auto",
                    state=state,
                )
            console.print(
                Panel(
                    f"[red]Error:[/red] {result.error or 'Unknown error'}\n\n"
                    f"[dim]{(result.output or '')[:300]}[/dim]",
                    title=f"[red]✗ {decision.action} failed[/red]",
                    border_style="red",
                )
            )

        # ------------------------------------------------------------------ #
        # Step 9: Persist state                                               #
        # ------------------------------------------------------------------ #
        state.save(state_file)

    # Exceeded max iterations
    raise PipelineHaltError(
        f"Pipeline exceeded maximum {MAX_ITERATIONS} iterations without completing.",
        state=state,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _get_decision(
    orchestrator_prompt: str,
    summary: str,
    state: PipelineState,
    model: str,
    iteration: int,
) -> OrchestratorDecision:
    """
    Call the orchestrator LLM and parse its OrchestratorDecision response.

    Retries up to 3 times, appending the parse error to the prompt on failure.

    Args:
        orchestrator_prompt: The orchestrator system prompt string.
        summary:             Compact PipelineState JSON summary.
        state:               Pipeline state (used for error context on halt).
        model:               LLM model identifier.
        iteration:           Current iteration number (for logging).

    Returns:
        Parsed OrchestratorDecision.

    Raises:
        PipelineHaltError: If parsing fails after 3 attempts.
    """
    prompt = summary
    last_error = ""

    for attempt in range(1, 4):
        try:
            raw = await query_llm(
                system=orchestrator_prompt,
                user=prompt,
                model=model,
                max_tokens=1500,
                response_format="json",
            )
        except RuntimeError as exc:
            # Rate limit or connection failure — surface immediately as a clean halt
            raise PipelineHaltError(
                f"LLM unavailable at iteration {iteration}: {exc}",
                state=state,
            ) from exc
        try:
            # Strip markdown fences if the model wrapped the JSON anyway
            cleaned = raw.strip()
            if cleaned.startswith("```"):
                cleaned = re.sub(r"^```[a-zA-Z]*\n?", "", cleaned)
                cleaned = re.sub(r"\n?```$", "", cleaned.strip())
            data = json.loads(cleaned)
            return OrchestratorDecision.model_validate(data)
        except (json.JSONDecodeError, ValidationError) as exc:
            last_error = str(exc)
            console.print(
                f"[yellow]Orchestrator parse error (attempt {attempt}/3): {last_error}[/yellow]"
            )
            prompt = (
                f"{summary}\n\nPrevious response failed validation: {last_error}\n"
                f"Previous response was:\n{raw}\n\n"
                "Please return a valid OrchestratorDecision JSON object."
            )

    raise PipelineHaltError(
        f"Orchestrator failed to produce a valid decision after 3 attempts at iteration {iteration}. "
        f"Last error: {last_error}",
        state=state,
    )


def _step_key(agent_name: str, params: dict[str, Any]) -> str:
    """
    Build a unique step key for completed_steps tracking.

    For agents with phases/stages, the key includes that qualifier.

    Args:
        agent_name: Agent identifier string.
        params:     Params dict passed to delegate_agent.

    Returns:
        Step key string.
    """
    context = params.get("context", {})
    if agent_name == "infrastructure":
        phase = context.get("phase", "plan")
        return f"infrastructure_{phase}"
    elif agent_name == "testing":
        stage = context.get("stage", "architecture")
        return f"testing_{stage}"
    return agent_name
