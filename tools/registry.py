"""
Tool registry for the Agentic SDLC pipeline orchestrator.

All tools are standalone async functions that the orchestrator LLM can invoke
by name. Each returns a ToolResult so the orchestrator always receives a
structured, consistent response regardless of success or failure.
"""

from __future__ import annotations

import asyncio
import glob
import json
import os
import re
from pathlib import Path
from typing import Any, Callable

import httpx
from pydantic import BaseModel
from rich.console import Console

console = Console()

# ---------------------------------------------------------------------------
# ToolResult — universal return type for every tool
# ---------------------------------------------------------------------------


class ToolResult(BaseModel):
    """Structured result returned by every tool in the registry."""

    tool: str = ""
    success: bool
    output: str
    error: str | None = None
    metadata: dict[str, Any] = {}


# ---------------------------------------------------------------------------
# shell_exec
# ---------------------------------------------------------------------------


async def shell_exec(
    command: str,
    cwd: str | None = None,
    timeout: int = 120,
) -> ToolResult:
    """
    Execute an arbitrary shell command asynchronously.

    Special handling:
    - Commands containing "docker compose up": enforced detached mode (-d),
      container names captured in metadata["containers"].
    - Commands containing "npx cypress run": timeout extended to 300 s.
    - Commands containing "gradlew" or "gradle": timeout extended to 180 s.

    Args:
        command: Shell command string to execute.
        cwd:     Working directory (None = inherit).
        timeout: Hard timeout in seconds. Returns error ToolResult on expiry.

    Returns:
        ToolResult with stdout in output, stderr in error (on failure),
        and exit_code in metadata.
    """
    # Adjust timeout for known slow commands
    if "npx cypress run" in command:
        timeout = max(timeout, 300)
    elif "gradlew" in command or "gradle " in command:
        timeout = max(timeout, 180)

    # Ensure docker compose up always runs detached
    if "docker compose up" in command and "-d" not in command:
        command = command.replace("docker compose up", "docker compose up -d")

    try:
        proc = await asyncio.wait_for(
            asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            ),
            timeout=10,  # process creation timeout
        )

        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=float(timeout),
        )

        exit_code = proc.returncode or 0
        out_text = stdout.decode(errors="replace")
        err_text = stderr.decode(errors="replace")
        success = exit_code == 0

        metadata: dict[str, Any] = {"exit_code": exit_code}

        # Capture container names from docker compose output
        if "docker compose" in command and "up" in command:
            containers = re.findall(r"Container (\S+)\s+(?:Started|Running|Healthy)", out_text)
            metadata["containers"] = containers

        return ToolResult(
            tool="shell_exec",
            success=success,
            output=out_text[:8000],
            error=err_text[:2000] if not success else None,
            metadata=metadata,
        )

    except asyncio.TimeoutError:
        return ToolResult(
            tool="shell_exec",
            success=False,
            output="",
            error=f"Timeout after {timeout}s",
            metadata={"exit_code": -1},
        )
    except Exception as exc:  # noqa: BLE001
        return ToolResult(
            tool="shell_exec",
            success=False,
            output="",
            error=str(exc),
            metadata={"exit_code": -1},
        )


# ---------------------------------------------------------------------------
# file_read
# ---------------------------------------------------------------------------


async def file_read(path: str) -> ToolResult:
    """
    Read a file from disk and return its contents.

    Output is truncated to 8000 characters; "[truncated]" is appended if cut.

    Args:
        path: Absolute or relative path to the file.

    Returns:
        ToolResult with file content in output field.
    """
    try:
        content = Path(path).read_text(encoding="utf-8", errors="replace")
        truncated = False
        if len(content) > 8000:
            content = content[:8000]
            truncated = True
        return ToolResult(
            tool="file_read",
            success=True,
            output=content + ("[truncated]" if truncated else ""),
            metadata={"path": path, "truncated": truncated},
        )
    except FileNotFoundError:
        return ToolResult(
            tool="file_read",
            success=False,
            output="",
            error=f"File not found: {path}",
        )
    except Exception as exc:  # noqa: BLE001
        return ToolResult(
            tool="file_read",
            success=False,
            output="",
            error=str(exc),
        )


# ---------------------------------------------------------------------------
# file_write
# ---------------------------------------------------------------------------


async def file_write(path: str, content: str) -> ToolResult:
    """
    Write content to a file, creating parent directories as needed.

    Args:
        path:    Target file path (absolute or relative).
        content: String content to write.

    Returns:
        ToolResult indicating success or failure.
    """
    try:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return ToolResult(
            tool="file_write",
            success=True,
            output=f"Written {len(content)} chars to {path}",
            metadata={"path": path, "bytes": len(content.encode())},
        )
    except Exception as exc:  # noqa: BLE001
        return ToolResult(
            tool="file_write",
            success=False,
            output="",
            error=str(exc),
        )


# ---------------------------------------------------------------------------
# file_patch
# ---------------------------------------------------------------------------


async def file_patch(path: str, old_str: str, new_str: str) -> ToolResult:
    """
    Replace the FIRST occurrence of old_str with new_str in a file.

    Never silently succeeds if old_str is not found — returns an error ToolResult.

    Args:
        path:    Path to the file to patch.
        old_str: Exact string to search for (first occurrence).
        new_str: Replacement string.

    Returns:
        ToolResult. On success, output describes what was replaced.
        On failure, error explains why (file not found, pattern not found).
    """
    try:
        content = Path(path).read_text(encoding="utf-8")
        if old_str not in content:
            return ToolResult(
                tool="file_patch",
                success=False,
                output="",
                error=f"Pattern not found in {path}",
                metadata={"path": path},
            )
        new_content = content.replace(old_str, new_str, 1)
        Path(path).write_text(new_content, encoding="utf-8")
        return ToolResult(
            tool="file_patch",
            success=True,
            output=f"Patched {path}: replaced first occurrence of searched string.",
            metadata={"path": path},
        )
    except FileNotFoundError:
        return ToolResult(
            tool="file_patch",
            success=False,
            output="",
            error=f"File not found: {path}",
        )
    except Exception as exc:  # noqa: BLE001
        return ToolResult(
            tool="file_patch",
            success=False,
            output="",
            error=str(exc),
        )


# ---------------------------------------------------------------------------
# file_list
# ---------------------------------------------------------------------------


async def file_list(directory: str, pattern: str = "**/*") -> ToolResult:
    """
    List files matching a glob pattern within a directory.

    Args:
        directory: Root directory to search from.
        pattern:   Glob pattern relative to directory (default: "**/*").

    Returns:
        ToolResult with newline-separated relative paths in output.
    """
    try:
        root = Path(directory)
        if not root.exists():
            return ToolResult(
                tool="file_list",
                success=False,
                output="",
                error=f"Directory not found: {directory}",
            )
        matched = sorted(
            str(p.relative_to(root))
            for p in root.glob(pattern)
            if p.is_file()
        )
        return ToolResult(
            tool="file_list",
            success=True,
            output="\n".join(matched),
            metadata={"count": len(matched), "directory": directory},
        )
    except Exception as exc:  # noqa: BLE001
        return ToolResult(
            tool="file_list",
            success=False,
            output="",
            error=str(exc),
        )


# ---------------------------------------------------------------------------
# web_fetch
# ---------------------------------------------------------------------------


async def web_fetch(url: str, max_chars: int = 6000) -> ToolResult:
    """
    Fetch the content of a URL and return cleaned text.

    HTML tags are stripped with a regex. Content is truncated to max_chars.
    Useful for fetching API documentation, GitHub READMEs, or any web page.

    Args:
        url:       URL to fetch.
        max_chars: Maximum characters to return (default: 6000).

    Returns:
        ToolResult with cleaned page text in output.
    """
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
            response = await client.get(url, headers={"User-Agent": "AgenticSDLC/1.0"})
            response.raise_for_status()
            raw = response.text

        # Strip HTML tags
        text = re.sub(r"<[^>]+>", " ", raw)
        # Collapse whitespace
        text = re.sub(r"\s+", " ", text).strip()

        truncated = False
        if len(text) > max_chars:
            text = text[:max_chars]
            truncated = True

        return ToolResult(
            tool="web_fetch",
            success=True,
            output=text + ("[truncated]" if truncated else ""),
            metadata={"url": url, "truncated": truncated},
        )
    except httpx.HTTPStatusError as exc:
        return ToolResult(
            tool="web_fetch",
            success=False,
            output="",
            error=f"HTTP {exc.response.status_code}: {url}",
        )
    except Exception as exc:  # noqa: BLE001
        return ToolResult(
            tool="web_fetch",
            success=False,
            output="",
            error=str(exc),
        )


# ---------------------------------------------------------------------------
# api_call
# ---------------------------------------------------------------------------


async def api_call(
    service: str,
    method: str,
    endpoint: str,
    payload: dict[str, Any] | None = None,
) -> ToolResult:
    """
    Make an API call to an external service.

    Supported services:
    - "github": Uses `gh api <endpoint>` subprocess with optional JSON body.
    - "jira":   Calls JIRA_URL + endpoint with Bearer token from JIRA_TOKEN env.
    - "linear": Calls Linear GraphQL API using LINEAR_TOKEN env var.
    - "slack":  Posts to Slack using SLACK_TOKEN env var.

    Args:
        service:  Service identifier ("github", "jira", "linear", "slack").
        method:   HTTP method ("GET", "POST", "PATCH", "DELETE").
        endpoint: Path or URL (service-specific).
        payload:  Optional JSON body as dict.

    Returns:
        ToolResult with parsed JSON response in output field.
    """
    if payload is None:
        payload = {}

    service_lower = service.lower()

    # ---------- GitHub via gh CLI ----------
    if service_lower == "github":
        gh_method = f"--method {method.upper()}"
        gh_field_args = " ".join(f"-f {k}={v}" for k, v in payload.items())
        cmd = f"gh api {endpoint} {gh_method} {gh_field_args}".strip()
        result = await shell_exec(cmd)
        result.tool = "api_call"
        return result

    # ---------- JIRA ----------
    if service_lower == "jira":
        base = os.environ.get("JIRA_URL", "")
        token = os.environ.get("JIRA_TOKEN", "")
        if not base or not token:
            return ToolResult(
                tool="api_call",
                success=False,
                output="",
                error="JIRA_URL and JIRA_TOKEN environment variables must be set.",
            )
        url = f"{base.rstrip('/')}/{endpoint.lstrip('/')}"
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.request(
                    method=method.upper(),
                    url=url,
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                    json=payload or None,
                )
                resp.raise_for_status()
                return ToolResult(
                    tool="api_call",
                    success=True,
                    output=resp.text[:4000],
                    metadata={"status_code": resp.status_code},
                )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(tool="api_call", success=False, output="", error=str(exc))

    # ---------- Linear ----------
    if service_lower == "linear":
        token = os.environ.get("LINEAR_TOKEN", "")
        if not token:
            return ToolResult(
                tool="api_call",
                success=False,
                output="",
                error="LINEAR_TOKEN environment variable must be set.",
            )
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    "https://api.linear.app/graphql",
                    headers={"Authorization": token, "Content-Type": "application/json"},
                    json=payload,
                )
                resp.raise_for_status()
                return ToolResult(
                    tool="api_call",
                    success=True,
                    output=resp.text[:4000],
                    metadata={"status_code": resp.status_code},
                )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(tool="api_call", success=False, output="", error=str(exc))

    # ---------- Slack ----------
    if service_lower == "slack":
        token = os.environ.get("SLACK_TOKEN", "")
        if not token:
            return ToolResult(
                tool="api_call",
                success=False,
                output="",
                error="SLACK_TOKEN environment variable must be set.",
            )
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    f"https://slack.com/api/{endpoint}",
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                    json=payload,
                )
                resp.raise_for_status()
                return ToolResult(
                    tool="api_call",
                    success=True,
                    output=resp.text[:4000],
                    metadata={"status_code": resp.status_code},
                )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(tool="api_call", success=False, output="", error=str(exc))

    return ToolResult(
        tool="api_call",
        success=False,
        output="",
        error=f"Unknown service: {service}",
    )


# ---------------------------------------------------------------------------
# delegate_agent
# ---------------------------------------------------------------------------


async def delegate_agent(
    agent_name: str,
    context: dict[str, Any],
    output_dir: str,
) -> ToolResult:
    """
    Dynamically instantiate a specialist agent and run it.

    The artifact is saved to output_dir using the canonical numbered filename.

    Artifact filename mapping:
        discovery      → 01_discovery_artifact.json
        architecture   → 02_architecture_artifact.json
        engineering    → 03_engineering_artifact.json
        spec           → 04_generated_spec_artifact.json
        review         → 04_review_artifact.json
        testing        → stage-dependent (architecture/live/final)
        infrastructure → phase-dependent (plan/apply)

    Args:
        agent_name: One of the registered agent names (see AGENT_MAP).
        context:    Context dict passed to agent.run().
        output_dir: Root output directory for saving the artifact JSON.

    Returns:
        ToolResult with artifact JSON string in output field.
    """
    # Import here to avoid circular imports at module load time
    from agents.architecture_agent import ArchitectureAgent
    from agents.discovery_agent import DiscoveryAgent
    from agents.infrastructure_agent import InfrastructureAgent
    from agents.review_agent import ReviewAgent
    from agents.spec_agent import SpecAgent
    from agents.testing_agent import TestingAgent

    # Fixed pipeline stages only — code generation is handled by spawn_agent / DynamicAgent
    AGENT_MAP: dict[str, type] = {
        "discovery": DiscoveryAgent,
        "architecture": ArchitectureAgent,
        "spec": SpecAgent,
        "infrastructure": InfrastructureAgent,
        "review": ReviewAgent,
        "testing": TestingAgent,
    }

    ARTIFACT_FILENAMES: dict[str, str] = {
        "discovery": "01_discovery_artifact.json",
        "architecture": "02_architecture_artifact.json",
        "engineering": "03_engineering_artifact.json",
        "spec": "04_generated_spec_artifact.json",
        "review": "04_review_artifact.json",
    }

    if agent_name not in AGENT_MAP:
        return ToolResult(
            tool="delegate_agent",
            success=False,
            output="",
            error=f"Unknown agent: {agent_name}. Available: {sorted(AGENT_MAP.keys())}",
        )

    try:
        model = context.get("model", "gpt-4o")
        agent_cls = AGENT_MAP[agent_name]
        agent = agent_cls(model=model, output_dir=output_dir)  # type: ignore[call-arg]

        artifact = await agent.run(context)
        artifact_dict = artifact.model_dump()

        # Determine output filename
        if agent_name == "testing":
            stage = context.get("stage", "architecture")
            stage_map = {
                "architecture": "05a_testing_architecture.json",
                "live": "05b_testing_infrastructure.json",
                "final": "05c_testing_review.json",
            }
            filename = stage_map.get(stage, f"testing_{stage}.json")
        elif agent_name == "infrastructure":
            phase = context.get("phase", "plan")
            filename = (
                "06a_infrastructure_plan_artifact.json"
                if phase == "plan"
                else "06b_infrastructure_apply_artifact.json"
            )
        else:
            filename = ARTIFACT_FILENAMES.get(agent_name, f"{agent_name}_artifact.json")

        # Write artifact to disk (atomic)
        out_path = Path(output_dir) / filename
        out_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = Path(output_dir) / f"{filename}.tmp"
        tmp_path.write_text(json.dumps(artifact_dict, indent=2), encoding="utf-8")
        os.replace(str(tmp_path), str(out_path))

        console.print(f"[green][delegate_agent] {agent_name} artifact → {out_path}[/green]")

        return ToolResult(
            tool="delegate_agent",
            success=True,
            output=json.dumps(artifact_dict),
            metadata={"agent": agent_name, "artifact_path": str(out_path)},
        )

    except Exception as exc:  # noqa: BLE001
        import traceback
        return ToolResult(
            tool="delegate_agent",
            success=False,
            output="",
            error=f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}",
            metadata={"agent": agent_name},
        )


# ---------------------------------------------------------------------------
# extract_blueprints — derive AgentBlueprints from an ArchitectureArtifact
# ---------------------------------------------------------------------------


async def extract_blueprints(
    architecture: dict[str, Any],
    model: str = "gpt-4o",
    output_dir: str = "artifacts",
) -> ToolResult:
    """
    Read an ArchitectureArtifact and produce a list of AgentBlueprints describing
    exactly which code-generation agents should be spawned.

    The LLM receives the architecture's component list and decides:
    - Which components need code generated (skip databases, message brokers, etc.)
    - What technology each one uses
    - What port it listens on
    - What other services it depends on at runtime
    - Any extra generation rules (e.g. "must expose /health")

    The resulting blueprints are stored in state.active_agents by the orchestrator
    and used as inputs to spawn_agent calls.

    Args:
        architecture: ArchitectureArtifact dict (must contain 'components' list).
        model:        LLM model to use for blueprint synthesis.
        output_dir:   Output dir (unused here, kept for consistency).

    Returns:
        ToolResult whose output is a JSON array of AgentBlueprint dicts.
    """
    from agents.base_agent import query_llm
    from models.artifacts import AgentBlueprint

    components = architecture.get("components", [])
    if not components:
        return ToolResult(
            tool="extract_blueprints",
            success=False,
            output="",
            error="ArchitectureArtifact has no 'components' field.",
        )

    system_prompt = (
        "You are an expert software architect. Given a list of system components, "
        "you decide which ones need code generation and produce an AgentBlueprint for each.\n\n"
        "RULES:\n"
        "- Include only components that need SOURCE CODE generated (services, APIs, UIs, workers).\n"
        "- Exclude infrastructure-only components: databases, message brokers, caches, CDNs.\n"
        "- For each included component, infer the technology from the component definition.\n"
        "- output_subdir must be a safe directory name (snake_case, no spaces).\n"
        "- depends_on must list the 'name' of other blueprints this component calls at runtime.\n"
        "- extra_instructions: any non-obvious rules the code generator must follow.\n\n"
        "Return ONLY a JSON array of AgentBlueprint objects. No prose, no markdown fences.\n\n"
        "AgentBlueprint schema:\n"
        "{\n"
        '  "name": "snake_case_identifier",\n'
        '  "role": "one sentence describing what this generates",\n'
        '  "technology": "full tech stack string",\n'
        '  "port": 8080,\n'
        '  "output_subdir": "directory_name",\n'
        '  "artifact_schema": "ServiceArtifact",\n'
        '  "extra_instructions": ["rule1", "rule2"],\n'
        '  "depends_on": ["other_blueprint_name"]\n'
        "}"
    )

    user_prompt = (
        f"Architecture style: {architecture.get('style', 'unknown')}\n\n"
        f"Components:\n{json.dumps(components, indent=2)}\n\n"
        f"Security model:\n{json.dumps(architecture.get('security_model', {}), indent=2)}\n\n"
        f"Deployment model:\n{json.dumps(architecture.get('deployment_model', {}), indent=2)}\n\n"
        "Produce one AgentBlueprint per deployable code component. "
        "Return a JSON array."
    )

    try:
        raw = await query_llm(
            system=system_prompt,
            user=user_prompt,
            model=model,
            max_tokens=1200,
            response_format="json",
        )
        data = json.loads(raw)
        blueprints_raw = data if isinstance(data, list) else data.get("blueprints", [])

        # Validate each blueprint through Pydantic
        validated: list[dict[str, Any]] = []
        for bp in blueprints_raw:
            try:
                validated.append(AgentBlueprint.model_validate(bp).model_dump())
            except Exception as exc:  # noqa: BLE001
                console.print(f"[yellow][extract_blueprints] Skipping invalid blueprint: {exc}[/yellow]")

        if not validated:
            return ToolResult(
                tool="extract_blueprints",
                success=False,
                output="",
                error="No valid AgentBlueprints could be parsed from the LLM response.",
            )

        return ToolResult(
            tool="extract_blueprints",
            success=True,
            output=json.dumps(validated),
            metadata={"count": len(validated), "names": [b["name"] for b in validated]},
        )

    except Exception as exc:  # noqa: BLE001
        import traceback
        return ToolResult(
            tool="extract_blueprints",
            success=False,
            output="",
            error=f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}",
        )


# ---------------------------------------------------------------------------
# spawn_agent — instantiate and run a DynamicAgent from a blueprint
# ---------------------------------------------------------------------------


async def spawn_agent(
    blueprint: dict[str, Any],
    context: dict[str, Any],
    output_dir: str,
) -> ToolResult:
    """
    Instantiate a DynamicAgent from an AgentBlueprint dict and run it.

    This is the core of architecture-driven dynamic code generation. Instead of
    calling a hardcoded BackendAgent or FrontendAgent, the orchestrator passes
    a blueprint describing what to build and this tool creates the right agent
    on the fly.

    The DynamicAgent synthesises its own system prompt from:
    1. The universal base template (prompts/dynamic_agent.md)
    2. The blueprint's role, technology, port, dependencies, and extra_instructions

    Args:
        blueprint:   AgentBlueprint dict (as returned by extract_blueprints).
        context:     Context dict passed to agent.run() — should include spec,
                     discovery, architecture, and optionally feedback/target_services.
        output_dir:  Root output directory for saving the artifact JSON.

    Returns:
        ToolResult with ServiceArtifact JSON string in output field.
    """
    from agents.base_agent import DynamicAgent
    from models.artifacts import AgentBlueprint

    try:
        bp = AgentBlueprint.model_validate(blueprint)
    except Exception as exc:  # noqa: BLE001
        return ToolResult(
            tool="spawn_agent",
            success=False,
            output="",
            error=f"Invalid blueprint: {exc}",
        )

    try:
        model = context.get("model", "gpt-4o")

        # Build peer artifacts for this blueprint's direct dependencies.
        # The orchestrator should pass completed service artifact dicts under
        # context["completed_artifacts"] keyed by blueprint name so that
        # dependent agents can generate correct integration code.
        CONTRACT_PATTERNS = (
            ".proto", "interface", "client", "api", "contract",
            "openapi", "types.ts", "schema", "dto",
        )
        completed_artifacts: dict[str, Any] = context.get("completed_artifacts", {})
        peer_artifacts: dict[str, Any] = {}
        for dep in bp.depends_on:
            dep_artifact = completed_artifacts.get(dep, {})
            if dep_artifact:
                dep_files: dict[str, str] = dep_artifact.get("files", {})
                peer_artifacts[dep] = {
                    "service": dep,
                    "technology": dep_artifact.get("technology", ""),
                    "port": dep_artifact.get("port"),
                    "role": dep_artifact.get("role", ""),
                    "files": list(dep_files.keys()),
                    "key_contracts": {
                        k: v[:1500]
                        for k, v in dep_files.items()
                        if any(p in k.lower() for p in CONTRACT_PATTERNS)
                    },
                }

        run_context = {**context, "peer_artifacts": peer_artifacts} if peer_artifacts else context
        agent = DynamicAgent(blueprint=bp, model=model, output_dir=output_dir)
        artifact = await agent.run(run_context)
        artifact_dict = artifact.model_dump()

        filename = f"{bp.name}_service_artifact.json"
        out_path = Path(output_dir) / filename
        out_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = Path(output_dir) / f"{filename}.tmp"
        tmp_path.write_text(json.dumps(artifact_dict, indent=2), encoding="utf-8")
        os.replace(str(tmp_path), str(out_path))

        console.print(
            f"[green][spawn_agent] '{bp.name}' artifact → {out_path}[/green]"
        )

        return ToolResult(
            tool="spawn_agent",
            success=True,
            output=json.dumps(artifact_dict),
            metadata={
                "agent": bp.name,
                "technology": bp.technology,
                "artifact_path": str(out_path),
                "files_generated": len(artifact_dict.get("files", {})),
            },
        )

    except Exception as exc:  # noqa: BLE001
        import traceback
        return ToolResult(
            tool="spawn_agent",
            success=False,
            output="",
            error=f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}",
            metadata={"agent": blueprint.get("name", "unknown")},
        )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

TOOL_REGISTRY: dict[str, Callable[..., Any]] = {
    "shell_exec": shell_exec,
    "file_read": file_read,
    "file_write": file_write,
    "file_patch": file_patch,
    "file_list": file_list,
    "web_fetch": web_fetch,
    "api_call": api_call,
    "delegate_agent": delegate_agent,
    "spawn_agent": spawn_agent,
    "extract_blueprints": extract_blueprints,
}
