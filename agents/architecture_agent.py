"""
ArchitectureAgent — designs a three-tier Kotlin/React system architecture.

Produces an ArchitectureArtifact covering components (with assigned ports),
data flows, API contract summaries, security model, and deployment topology.
"""

from __future__ import annotations

from typing import Any

from agents.base_agent import BaseAgent
from models.artifacts import ArchitectureArtifact


class ArchitectureAgent(BaseAgent):
    """
    Solutions architect agent.

    Given a DiscoveryArtifact and optional existing spec files, designs a
    three-tier architecture using Kotlin Spring Boot (port 8081), Kotlin
    WebFlux BFF (port 8080), and React/TypeScript frontend (port 3000).
    """

    name = "architecture"
    prompt_file = "prompts/architecture_agent.md"

    async def run(self, context: dict[str, Any]) -> ArchitectureArtifact:
        """
        Design the system architecture from discovery outputs.

        Args:
            context: Must contain:
                - discovery (dict): DiscoveryArtifact as dict.
                - tech_constraints (str, optional): Technology constraints.
                - arch_constraints (str, optional): Architectural constraints.
                - spec_files (list[str], optional): Existing OpenAPI spec content
                  for --from-run incremental mode.

        Returns:
            ArchitectureArtifact describing the full system design.

        Raises:
            ValueError: If the LLM response cannot be parsed.
        """
        discovery = context.get("discovery", {})
        tech_constraints = context.get("tech_constraints", "")
        arch_constraints = context.get("arch_constraints", "")
        spec_files = context.get("spec_files", [])

        spec_section = ""
        if spec_files:
            spec_section = (
                "\n\nExisting specification files (mark compatible paths x-existing: true):\n"
                + "\n\n".join(spec_files)
            )

        constraint_section = ""
        if tech_constraints:
            constraint_section += f"\n\nTechnology constraints: {tech_constraints}"
        if arch_constraints:
            constraint_section += f"\n\nArchitectural constraints: {arch_constraints}"

        user_prompt = (
            "Design a complete system architecture based on the following discovery artifact.\n\n"
            f"--- DISCOVERY ARTIFACT ---\n{discovery}\n--- END ---"
            f"{constraint_section}"
            f"{spec_section}\n\n"
            "IMPORTANT: Also populate the 'agent_blueprints' field.\n"
            "For every component that needs source code generated, include one AgentBlueprint:\n"
            "{\n"
            '  "name": "snake_case_identifier",\n'
            '  "role": "one sentence",\n'
            '  "technology": "full stack string",\n'
            '  "port": 8081,\n'
            '  "output_subdir": "backend",\n'
            '  "artifact_schema": "ServiceArtifact",\n'
            '  "extra_instructions": [],\n'
            '  "depends_on": []\n'
            "}\n"
            "Exclude infrastructure-only components (databases, caches, brokers).\n"
            "Do NOT hardcode backend/bff/frontend — derive them from the actual requirements.\n"
            "If requirements call for a CLI tool instead of a frontend, blueprint a CLI agent.\n"
            "If requirements call for a message worker, blueprint a worker agent.\n\n"
            "Return ONLY a valid JSON object matching the ArchitectureArtifact schema."
        )

        data = await self._llm_json(user_prompt, max_tokens=2500)
        artifact = ArchitectureArtifact.model_validate(data)
        self._save_artifact(artifact, "02_architecture_artifact.json")
        return artifact
