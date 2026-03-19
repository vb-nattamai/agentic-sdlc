"""
Shared LLM infrastructure for all Agentic SDLC specialist agents.

Provides:
- GitHub Models API authentication via `gh auth token`
- Async LLM query with retry logic and concurrency control
- BaseAgent base class with prompt loading, artifact saving, and two-phase file generation
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any, Literal

from openai import AsyncOpenAI
from pydantic import BaseModel
from rich.console import Console

console = Console()

GITHUB_MODELS_BASE_URL = "https://models.inference.ai.azure.com"

# Global semaphore shared across all agents — caps concurrent LLM calls to 2
_LLM_SEMAPHORE: asyncio.Semaphore | None = None


def _get_semaphore() -> asyncio.Semaphore:
    """Return (or lazily create) the module-level LLM semaphore."""
    global _LLM_SEMAPHORE
    if _LLM_SEMAPHORE is None:
        _LLM_SEMAPHORE = asyncio.Semaphore(2)
    return _LLM_SEMAPHORE


async def get_github_token() -> str:
    """
    Obtain a GitHub personal access token via the GitHub CLI.

    Runs `gh auth token` as a subprocess and returns the token string.

    Raises:
        RuntimeError: If the `gh` CLI is not installed, not authenticated,
                      or returns a non-zero exit code.
    """
    proc = await asyncio.create_subprocess_exec(
        "gh",
        "auth",
        "token",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"Failed to obtain GitHub token via 'gh auth token'. "
            f"Ensure GitHub CLI is installed and you are authenticated.\n"
            f"stderr: {stderr.decode().strip()}"
        )
    token = stdout.decode().strip()
    if not token:
        raise RuntimeError(
            "'gh auth token' returned an empty token. "
            "Run 'gh auth login' to authenticate."
        )
    return token


async def _query_anthropic(
    system: str,
    user: str,
    model: str,
    max_tokens: int,
    response_format: Literal["text", "json"],
    semaphore: asyncio.Semaphore,
) -> str:
    """
    Send a single LLM request to the Anthropic API (claude-* models).

    Auth: reads ANTHROPIC_API_KEY from the environment.

    Args:
        system:          System prompt string.
        user:            User message string.
        model:           Anthropic model identifier (e.g. claude-sonnet-4-5).
        max_tokens:      Maximum tokens in the response.
        response_format: "text" or "json".  For "json", a JSON-only instruction
                         is appended to the system prompt (Anthropic has no
                         native json_object response format).
        semaphore:       Concurrency semaphore.

    Returns:
        Raw response text string.

    Raises:
        RuntimeError: If ANTHROPIC_API_KEY is unset or all retries are exhausted.
    """
    import anthropic as anthropic_sdk  # lazy import — only needed for claude-* models

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY environment variable is not set.\n"
            "Get a key at https://console.anthropic.com and add it to your .env file."
        )

    client = anthropic_sdk.AsyncAnthropic(api_key=api_key)

    system_prompt = system
    if response_format == "json":
        system_prompt = (
            system
            + "\n\nIMPORTANT: Respond ONLY with valid JSON. "
            "Do not include markdown fences, explanations, or any text outside the JSON object."
        )

    max_retries = 6
    backoff = 10

    async with semaphore:
        for attempt in range(1, max_retries + 1):
            try:
                response = await asyncio.wait_for(
                    client.messages.create(
                        model=model,
                        max_tokens=max_tokens,
                        system=system_prompt,
                        messages=[{"role": "user", "content": user}],
                    ),
                    timeout=120.0,
                )
                text = response.content[0].text
                # Strip markdown fences Claude sometimes adds despite instructions
                if response_format == "json":
                    text = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.IGNORECASE)
                    text = re.sub(r"\s*```$", "", text.strip())
                return text

            except asyncio.TimeoutError:
                err = f"Anthropic call timed out (attempt {attempt}/{max_retries})"
                console.print(f"[yellow]{err}[/yellow]")
                if attempt == max_retries:
                    raise RuntimeError(err)
                await asyncio.sleep(backoff * attempt)

            except Exception as exc:  # noqa: BLE001
                err_str = str(exc)
                is_rate_limit = (
                    "429" in err_str
                    or "rate_limit" in err_str.lower()
                    or "overloaded" in err_str.lower()
                )
                # Non-retriable billing / auth errors — fail immediately
                is_fatal = (
                    "credit balance" in err_str.lower()
                    or "invalid_api_key" in err_str.lower()
                    or "permission_error" in err_str.lower()
                )
                console.print(
                    f"[yellow]Anthropic error (attempt {attempt}/{max_retries}): {err_str}[/yellow]"
                )
                if is_fatal:
                    raise RuntimeError(
                        f"Anthropic call failed (non-retriable): {err_str}"
                    ) from exc
                if attempt == max_retries:
                    raise RuntimeError(
                        f"Anthropic call failed after {max_retries} attempts: {err_str}"
                    ) from exc
                # Rate limit: back off longer — up to 60s
                sleep_time = min(backoff * attempt * 2, 60) if is_rate_limit else backoff * attempt
                console.print(f"[yellow]Retrying in {sleep_time}s...[/yellow]")
                await asyncio.sleep(sleep_time)

    raise RuntimeError("Unreachable: _query_anthropic exhausted retries without returning")


async def query_llm(
    system: str,
    user: str,
    model: str = "gpt-4o",
    max_tokens: int = 4096,
    response_format: Literal["text", "json"] = "text",
    semaphore: asyncio.Semaphore | None = None,
) -> str:
    """
    Send a single LLM request.  Routes to Anthropic for claude-* models;
    uses the GitHub Models API (OpenAI-compatible) for everything else.

    Args:
        system:          System prompt string.
        user:            User message string.
        model:           Model identifier (default: gpt-4o).
        max_tokens:      Maximum tokens in the response.
        response_format: "text" for plain text; "json" appends a JSON instruction
                         and (for OpenAI) sets the response format to json_object.
        semaphore:       Optional asyncio.Semaphore to limit concurrency.
                         Falls back to the module-level semaphore (limit 2).

    Returns:
        Raw response content string (caller is responsible for JSON parsing).

    Raises:
        RuntimeError: If all retry attempts are exhausted.
    """
    sem = semaphore or _get_semaphore()

    # ------------------------------------------------------------------ #
    # Anthropic path — claude-* models                                    #
    # ------------------------------------------------------------------ #
    if model.startswith("claude-"):
        return await _query_anthropic(system, user, model, max_tokens, response_format, sem)

    # ------------------------------------------------------------------ #
    # GitHub Models / OpenAI path                                         #
    # ------------------------------------------------------------------ #
    token = await get_github_token()

    client = AsyncOpenAI(
        base_url=GITHUB_MODELS_BASE_URL,
        api_key=token,
    )

    system_prompt = system
    if response_format == "json":
        system_prompt = (
            system
            + "\n\nIMPORTANT: Respond ONLY with valid JSON. "
            "Do not include markdown fences, explanations, or any text outside the JSON object."
        )

    api_response_format = {"type": "json_object"} if response_format == "json" else {"type": "text"}

    max_retries = 3
    backoff = 5

    async with sem:
        for attempt in range(1, max_retries + 1):
            try:
                response = await asyncio.wait_for(
                    client.chat.completions.create(
                        model=model,
                        messages=[
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user},
                        ],
                        max_tokens=max_tokens,
                        response_format=api_response_format,  # type: ignore[arg-type]
                    ),
                    timeout=120.0,
                )
                content = response.choices[0].message.content or ""
                return content

            except asyncio.TimeoutError:
                err = f"LLM call timed out (attempt {attempt}/{max_retries})"
                console.print(f"[yellow]{err}[/yellow]")
                if attempt == max_retries:
                    raise RuntimeError(err)
                await asyncio.sleep(backoff * attempt)

            except Exception as exc:  # noqa: BLE001
                err_str = str(exc)
                is_rate_limit = "429" in err_str or "rate limit" in err_str.lower()
                console.print(
                    f"[yellow]LLM error (attempt {attempt}/{max_retries}): {err_str}[/yellow]"
                )
                if attempt == max_retries:
                    raise RuntimeError(
                        f"LLM call failed after {max_retries} attempts: {err_str}"
                    ) from exc
                sleep_time = backoff * attempt if is_rate_limit else backoff
                await asyncio.sleep(sleep_time)

    raise RuntimeError("Unreachable: query_llm exhausted retries without returning")


class BaseAgent:
    """
    Abstract base class for all Agentic SDLC specialist agents.

    Subclasses must define:
        name (str):        Short identifier used in logging and artifact naming.
        prompt_file (str): Path relative to project root for the agent's system prompt.

    Provides:
        system_prompt:      Lazily loaded content of prompt_file.
        run(context):       Override in subclasses to implement agent logic.
        _llm(user, ...):    Thin wrapper around query_llm using self.system_prompt.
        _generate_files():  Two-phase chunked file generation.
        _save_artifact():   Persist a Pydantic artifact model as JSON.
    """

    name: str = "base"
    prompt_file: str = ""

    def __init__(self, model: str = "gpt-4o", output_dir: str = "artifacts") -> None:
        """
        Initialise the agent.

        Args:
            model:      LLM model to use for this agent's calls.
            output_dir: Root output directory for the current pipeline run.
        """
        self.model = model
        self.output_dir = output_dir
        self._system_prompt: str | None = None

    @property
    def system_prompt(self) -> str:
        """Load and cache the agent's system prompt from its prompt file."""
        if self._system_prompt is None:
            prompt_path = Path(self.prompt_file)
            if not prompt_path.exists():
                # Try relative to the package root (one level up from agents/)
                prompt_path = Path(__file__).parent.parent / self.prompt_file
            if not prompt_path.exists():
                raise FileNotFoundError(
                    f"Prompt file not found: {self.prompt_file}"
                )
            self._system_prompt = prompt_path.read_text(encoding="utf-8")
        return self._system_prompt

    async def run(self, context: dict[str, Any]) -> BaseModel:
        """
        Execute the agent with the given context dict.

        Subclasses must override this method.

        Args:
            context: Agent-specific context dictionary.

        Returns:
            A Pydantic BaseModel artifact.
        """
        raise NotImplementedError(f"{self.__class__.__name__} must implement run()")

    async def _llm(
        self,
        user: str,
        max_tokens: int = 4096,
        response_format: Literal["text", "json"] = "text",
    ) -> str:
        """
        Call the LLM using this agent's system prompt.

        Args:
            user:            User message content.
            max_tokens:      Token limit for response.
            response_format: "text" or "json".

        Returns:
            Raw LLM response string.
        """
        return await query_llm(
            system=self.system_prompt,
            user=user,
            model=self.model,
            max_tokens=max_tokens,
            response_format=response_format,
        )

    async def _llm_json(self, user: str, max_tokens: int = 4096) -> dict[str, Any]:
        """
        Call the LLM expecting a JSON response and parse it.

        Retries up to 3 times, appending the parse error to the prompt on each failure.

        Args:
            user:       User message content.
            max_tokens: Token limit for response.

        Returns:
            Parsed JSON as a dict.

        Raises:
            RuntimeError: If JSON parsing fails after all retries.
        """
        prompt = user
        last_error: str = ""
        for attempt in range(1, 4):
            raw = await self._llm(prompt, max_tokens=max_tokens, response_format="json")
            try:
                return json.loads(raw)
            except json.JSONDecodeError as exc:
                last_error = str(exc)
                console.print(
                    f"[yellow][{self.name}] JSON parse error (attempt {attempt}/3): "
                    f"{last_error}[/yellow]"
                )
                prompt = (
                    f"{user}\n\n"
                    f"Previous response failed JSON validation with: {last_error}\n"
                    f"Previous response was:\n{raw}\n\n"
                    "Please correct and return valid JSON only."
                )
        raise RuntimeError(
            f"[{self.name}] Failed to obtain valid JSON after 3 attempts. "
            f"Last error: {last_error}"
        )

    async def _generate_files(
        self,
        plan_prompt: str,
        fill_system_hint: str = "",
        max_tokens_plan: int = 4096,
        max_tokens_fill: int = 4096,
    ) -> dict[str, str]:
        """
        Two-phase chunked file generation.

        Phase 1 — Plan:
            Ask the LLM to produce a JSON object mapping relative file paths to
            either full content or the placeholder string "__PENDING__".

        Phase 2 — Fill:
            For each file marked "__PENDING__", issue a focused LLM call that
            generates only that file's complete content.

        Args:
            plan_prompt:      User prompt for Phase 1 (file plan).
            fill_system_hint: Extra instruction appended to system prompt for
                              Phase 2 fill calls.
            max_tokens_plan:  Token limit for the plan LLM call.
            max_tokens_fill:  Token limit per file fill call.

        Returns:
            dict mapping relative_path -> complete file_content.
        """
        # ---------- Phase 1: file plan ----------
        plan_data = await self._llm_json(plan_prompt, max_tokens=max_tokens_plan)

        # The plan must be a dict mapping path -> content | "__PENDING__"
        if not isinstance(plan_data, dict):
            # Agents sometimes wrap in a "files" key
            plan_data = plan_data.get("files", plan_data)

        files: dict[str, str] = {}

        # ---------- Phase 2: fill pending files concurrently ----------
        pending_paths = [p for p, c in plan_data.items() if c == "__PENDING__"]
        already_filled = {p: c for p, c in plan_data.items() if c != "__PENDING__"}
        files.update(already_filled)

        async def fill_file(path: str) -> tuple[str, str]:
            fill_prompt = (
                f"Generate the COMPLETE content for the file: {path}\n\n"
                f"Context — the full file plan is:\n{json.dumps(plan_data, indent=2)}\n\n"
                f"{fill_system_hint}\n\n"
                "Return ONLY the raw file content with no markdown fences, "
                "no explanations, and no surrounding text."
            )
            content = await query_llm(
                system=self.system_prompt,
                user=fill_prompt,
                model=self.model,
                max_tokens=max_tokens_fill,
                response_format="text",
            )
            # Strip markdown code fences if present
            content = re.sub(r"^```[a-zA-Z]*\n?", "", content.strip())
            content = re.sub(r"\n?```$", "", content.strip())
            return path, content

        if pending_paths:
            fill_tasks = [fill_file(p) for p in pending_paths]
            results = await asyncio.gather(*fill_tasks)
            for path, content in results:
                files[path] = content

        return files

    def _save_artifact(self, artifact: BaseModel, filename: str) -> str:
        """
        Persist a Pydantic artifact as a JSON file in the output directory.

        The file is written atomically: first to a .tmp file, then renamed.

        Args:
            artifact: Any Pydantic BaseModel instance.
            filename: Target filename (e.g. '01_discovery_artifact.json').

        Returns:
            Absolute path to the saved file.
        """
        out_dir = Path(self.output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        target = out_dir / filename
        tmp = out_dir / f"{filename}.tmp"

        json_str = artifact.model_dump_json(indent=2)
        tmp.write_text(json_str, encoding="utf-8")
        os.replace(str(tmp), str(target))

        console.print(f"[green][{self.name}] Artifact saved → {target}[/green]")
        return str(target)


# ---------------------------------------------------------------------------
# DynamicAgent — short-lived agent synthesised from an AgentBlueprint
# ---------------------------------------------------------------------------


class DynamicAgent(BaseAgent):
    """
    A short-lived code generation agent whose identity and instructions are
    determined entirely at runtime from an AgentBlueprint.

    Unlike the static specialist agents (BackendAgent, BffAgent, etc.), this
    class has no hardcoded knowledge of any technology stack. The orchestrator
    reads the ArchitectureArtifact, decides what needs to be built, and spawns
    a DynamicAgent for each component — one per blueprint.

    The system prompt is synthesised on the fly by combining:
    1. A universal base prompt (prompts/dynamic_agent.md) that explains two-phase
       file generation, artifact schemas, and code quality rules.
    2. Blueprint-specific context: role, technology, port, output directory,
       depends_on list, and extra_instructions.

    This means the orchestrator can generate a GraphQL gateway, a message
    consumer worker, a CLI tool, or any other component the architecture
    calls for — without any pre-written agent class.
    """

    def __init__(
        self,
        blueprint: "AgentBlueprint",  # type: ignore[name-defined]  # noqa: F821
        model: str = "gpt-4o",
        output_dir: str = "artifacts",
    ) -> None:
        """
        Initialise a DynamicAgent from an AgentBlueprint.

        Args:
            blueprint:  AgentBlueprint describing the component to generate.
            model:      LLM model identifier.
            output_dir: Root output directory for the pipeline run.
        """
        super().__init__(model=model, output_dir=output_dir)
        self.blueprint = blueprint
        self.name = blueprint.name
        self.prompt_file = "prompts/dynamic_agent.md"  # universal base prompt

    @property
    def system_prompt(self) -> str:
        """
        Build the system prompt by combining the universal base template with
        blueprint-specific context injected as a structured section.
        """
        if self._system_prompt is not None:
            return self._system_prompt

        # Load universal base prompt
        base_path = Path(self.prompt_file)
        if not base_path.exists():
            base_path = Path(__file__).parent.parent / self.prompt_file
        if not base_path.exists():
            raise FileNotFoundError(f"Base dynamic agent prompt not found: {self.prompt_file}")

        base_template = base_path.read_text(encoding="utf-8")

        # Build blueprint context block
        depends_section = ""
        if self.blueprint.depends_on:
            depends_section = (
                "\nThis component calls these other services at runtime:\n"
                + "\n".join(f"  - {dep}" for dep in self.blueprint.depends_on)
            )

        extra_section = ""
        if self.blueprint.extra_instructions:
            extra_section = (
                "\nAdditional rules specific to this component:\n"
                + "\n".join(f"  - {rule}" for rule in self.blueprint.extra_instructions)
            )

        port_section = (
            f"\nThis component listens on port {self.blueprint.port}."
            if self.blueprint.port
            else ""
        )

        blueprint_block = (
            f"\n\n---\n## THIS AGENT'S COMPONENT\n\n"
            f"**Name**: {self.blueprint.name}\n"
            f"**Role**: {self.blueprint.role}\n"
            f"**Technology**: {self.blueprint.technology}\n"
            f"**Output directory**: generated/{self.blueprint.output_subdir}/"
            f"{port_section}"
            f"{depends_section}"
            f"{extra_section}\n"
            f"\nAll generated files must be placed under: "
            f"generated/{self.blueprint.output_subdir}/\n"
            f"Return a ServiceArtifact JSON with service='{self.blueprint.name}'.\n"
        )

        self._system_prompt = base_template + blueprint_block
        return self._system_prompt

    async def run(self, context: dict[str, Any]) -> "ServiceArtifact":  # type: ignore[name-defined]  # noqa: F821
        """
        Generate all source files for this blueprint's component.

        Context keys consumed:
        - spec (dict):          GeneratedSpecArtifact — the binding API contract.
        - discovery (dict):     DiscoveryArtifact — business context.
        - architecture (dict):  ArchitectureArtifact — design decisions.
        - feedback (list[str]): ReviewAgent issues (re-generation pass).
        - target_services (list[str]): If set, skip unless self.name is in the list.

        Returns:
            ServiceArtifact with all generated files.
        """
        from models.artifacts import ServiceArtifact

        # Skip if not targeted in a re-generation pass
        target_services: list[str] = context.get("target_services", [])
        if target_services and self.blueprint.name not in target_services:
            return ServiceArtifact(service=self.blueprint.name, files={})

        spec = context.get("spec", {})
        architecture = context.get("architecture", {})
        feedback: list[str] = context.get("feedback", [])

        feedback_section = ""
        if feedback:
            feedback_section = (
                "\n\nREVIEW FEEDBACK — fix every one of these before generating files:\n"
                + "\n".join(f"  - {f}" for f in feedback)
            )

        # Peer artifacts — injected by EngineeringAgent (wave executor) or spawn_agent tool
        # Contains the actual generated files of services this component depends on,
        # enabling correct generation of HTTP clients, proto stubs, typed SDKs, etc.
        peer_artifacts: dict[str, Any] = context.get("peer_artifacts", {})
        peer_section = ""
        if peer_artifacts:
            lines = ["\n--- PEER SERVICES (already generated — use these for integration) ---"]
            for peer_name, peer in peer_artifacts.items():
                lines.append(
                    f"\n{peer_name}  |  {peer.get('technology', '')}  |  "
                    f"port {peer.get('port', '?')}"
                )
                lines.append(f"  Role: {peer.get('role', '')}")
                file_list = peer.get("files", [])
                lines.append(f"  Files: {', '.join(file_list[:20])}")
                for cpath, ccontent in peer.get("key_contracts", {}).items():
                    lines.append(f"\n  [{cpath}]\n{ccontent}")
            lines.append("\n--- END PEER SERVICES ---")
            peer_section = "\n".join(lines)

        plan_prompt = (
            f"Generate a complete file plan for the '{self.blueprint.name}' component.\n\n"
            f"Technology: {self.blueprint.technology}\n"
            f"Role: {self.blueprint.role}\n"
            + (f"Port: {self.blueprint.port}\n" if self.blueprint.port else "")
            + (
                f"Calls: {', '.join(self.blueprint.depends_on)}\n"
                if self.blueprint.depends_on
                else ""
            )
            + f"\n--- OPENAPI SPEC ---\n{spec.get('openapi_yaml', '')[:3000]}\n--- END ---\n\n"
            f"--- SQL DDL ---\n{spec.get('sql_ddl', '')[:1000]}\n--- END ---\n\n"
            f"--- ARCHITECTURE ---\n{json.dumps(architecture, indent=2)[:1500]}\n--- END ---"
            f"{peer_section}"
            f"{feedback_section}\n\n"
            "Return a JSON object:\n"
            f'{{ "{self.blueprint.output_subdir}/<relative_path>": "__PENDING__" }}\n'
            "for every file. Small config files may include their content directly."
        )

        files = await self._generate_files(
            plan_prompt=plan_prompt,
            fill_system_hint=(
                f"You are generating a file for the '{self.blueprint.name}' component "
                f"({self.blueprint.technology}). Output ONLY the raw file content — "
                "no markdown fences, no explanations. Make the code production-quality."
            ),
            max_tokens_plan=4096,
            max_tokens_fill=4096,
        )

        # Write files to disk
        out_base = Path(self.output_dir) / "generated"
        for rel_path, content in files.items():
            target = out_base / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

        artifact = ServiceArtifact(service=self.blueprint.name, files=files)
        self._save_artifact(artifact, f"{self.blueprint.name}_service_artifact.json")
        return artifact
