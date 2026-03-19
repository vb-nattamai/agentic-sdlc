"""
EngineeringAgent — spawns one DynamicAgent per blueprint in dependency order.

Blueprints are produced by extract_blueprints from the ArchitectureArtifact and
stored in state.active_agents. Agents are grouped into execution waves using a
topological sort of depends_on edges:

  Wave 0: blueprints with no dependencies — all run in parallel
  Wave N: blueprints whose all dependencies are in waves 0..N-1 — run in parallel;
          each receives the completed ServiceArtifacts of its direct dependencies
          as peer_artifacts so it can generate correct integration code
          (HTTP clients, proto stubs, typed SDKs, etc.)

The orchestrator can also call spawn_agent directly per blueprint; in that path
it should pass completed_artifacts in context for dependent services.
"""

from __future__ import annotations

import asyncio
from typing import Any

from agents.base_agent import BaseAgent, DynamicAgent
from models.artifacts import AgentBlueprint, EngineeringArtifact, ServiceArtifact


class EngineeringAgent(BaseAgent):
    """
    Engineering orchestrator agent.

    Reads the active_agents blueprints and executes them in dependency order:
    - Independent blueprints (no depends_on) all run in parallel — wave 0.
    - Blueprints whose dependencies have all completed run next — wave 1, 2, …
    - Each wave's agents receive the ServiceArtifacts of their direct dependencies
      as peer_artifacts in context, enabling correct generation of integration code:
      HTTP clients, proto stubs, typed SDKs, contract tests, etc.
    """

    name = "engineering"
    prompt_file = "prompts/dynamic_agent.md"  # base template; each DynamicAgent gets its own

    async def run(self, context: dict[str, Any]) -> EngineeringArtifact:
        """
        Spawn one DynamicAgent per blueprint, respecting depends_on ordering.

        Agents in the same wave run concurrently via asyncio.gather. Agents in
        later waves receive the completed artifacts of their direct dependencies
        injected as context["peer_artifacts"] so they can generate correct
        integration code (clients, stubs, etc.) without a separate messaging step.

        Args:
            context: Must contain:
                - active_agents (list[dict]): AgentBlueprint dicts from extract_blueprints.
                - spec (dict): GeneratedSpecArtifact as dict.
                - discovery (dict): DiscoveryArtifact for business context.
                - architecture (dict): ArchitectureArtifact for design decisions.
                - feedback (list[str], optional): ReviewAgent critical issues.
                - target_services (list[str], optional): Limit re-gen to named services.

        Returns:
            EngineeringArtifact with one ServiceArtifact per blueprint.
        """
        blueprints_raw: list[dict[str, Any]] = context.get("active_agents", [])
        if not blueprints_raw:
            return EngineeringArtifact(services={}, decisions=[])

        blueprints = [AgentBlueprint.model_validate(bp) for bp in blueprints_raw]
        bp_by_name = {bp.name: bp for bp in blueprints}

        # Shared result store — written after each agent completes, read by later waves
        completed: dict[str, ServiceArtifact] = {}
        lock = asyncio.Lock()

        async def run_one(bp: AgentBlueprint) -> ServiceArtifact:
            # Inject completed artifacts of this agent's direct dependencies
            peer_artifacts = {
                dep: _peer_summary(completed[dep], bp_by_name[dep])
                for dep in bp.depends_on
                if dep in completed
            }
            agent_context = {**context, "peer_artifacts": peer_artifacts}
            agent = DynamicAgent(blueprint=bp, model=self.model, output_dir=self.output_dir)
            result = await agent.run(agent_context)
            async with lock:
                completed[bp.name] = result
            return result

        # Execute waves in order — all agents within a wave run in parallel
        all_results: list[ServiceArtifact] = []
        for wave in _topo_waves(blueprints):
            wave_results: list[ServiceArtifact] = await asyncio.gather(
                *[run_one(bp) for bp in wave]
            )
            all_results.extend(wave_results)

        services = {sa.service: sa for sa in all_results}
        artifact = EngineeringArtifact(services=services, decisions=[])
        self._save_artifact(artifact, "03_engineering_artifact.json")
        return artifact


def _topo_waves(blueprints: list[AgentBlueprint]) -> list[list[AgentBlueprint]]:
    """
    Topological sort: group blueprints into execution waves.

    All blueprints in a wave can run in parallel. Wave N+1 only starts after all
    wave N blueprints have completed. Cycles are broken by treating the offending
    dependency as already available (level 0).

    Example:
        blueprints: auth, backend(depends_on=[auth]), bff(depends_on=[backend])
        waves: [[auth], [backend], [bff]]
    """
    bp_map = {bp.name: bp for bp in blueprints}
    level: dict[str, int] = {}

    def compute(name: str, visiting: frozenset) -> int:
        if name in level:
            return level[name]
        if name not in bp_map or name in visiting:
            return 0  # external dependency or cycle — treat as already available
        deps = bp_map[name].depends_on
        lvl = (max(compute(d, visiting | {name}) for d in deps) + 1) if deps else 0
        level[name] = lvl
        return lvl

    for bp in blueprints:
        compute(bp.name, frozenset())

    max_level = max(level.values(), default=0)
    waves: list[list[AgentBlueprint]] = [[] for _ in range(max_level + 1)]
    for bp in blueprints:
        waves[level[bp.name]].append(bp)
    return [w for w in waves if w]


def _peer_summary(artifact: ServiceArtifact, blueprint: AgentBlueprint) -> dict[str, Any]:
    """
    Build a concise peer-service summary to inject into a dependent agent's context.

    Includes all generated file paths and the full content of key contract files
    (interfaces, proto definitions, API clients, OpenAPI specs, TypeScript types,
    DTOs) so the dependent agent can generate correct integration code without
    any runtime communication between agents.
    """
    CONTRACT_PATTERNS = (
        ".proto", "interface", "client", "api", "contract",
        "openapi", "types.ts", "schema", "dto",
    )
    key_contracts: dict[str, str] = {}
    for path, content in artifact.files.items():
        if any(p in path.lower() for p in CONTRACT_PATTERNS):
            key_contracts[path] = content[:1500]  # truncate very large files

    return {
        "service": artifact.service,
        "technology": blueprint.technology,
        "port": blueprint.port,
        "role": blueprint.role,
        "files": list(artifact.files.keys()),
        "key_contracts": key_contracts,
    }
