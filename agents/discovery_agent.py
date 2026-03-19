"""
DiscoveryAgent — extracts and validates requirements from free-form text.

Produces a DiscoveryArtifact with structured requirements, goals, constraints,
scope, risks, success criteria, and decision records.
"""

from __future__ import annotations

import json
from typing import Any

from agents.base_agent import BaseAgent
from models.artifacts import DiscoveryArtifact


class DiscoveryAgent(BaseAgent):
    """
    Requirements analyst agent.

    Reads a plain-English requirements document and returns a fully
    structured DiscoveryArtifact capturing everything the LLM can infer
    about the project's goals, boundaries, risks, and success criteria.
    """

    name = "discovery"
    prompt_file = "prompts/discovery_agent.md"

    async def run(self, context: dict[str, Any]) -> DiscoveryArtifact:
        """
        Analyse raw requirements text and return a DiscoveryArtifact.

        Args:
            context: Must contain:
                - requirements (str): Raw requirements text.
                - constraints (dict, optional): Pre-loaded constraint strings.

        Returns:
            DiscoveryArtifact with fully populated fields.

        Raises:
            ValueError: If LLM response cannot be parsed into DiscoveryArtifact.
        """
        requirements_text = context.get("requirements", "")
        constraints = context.get("constraints", {})

        constraint_text = ""
        if constraints:
            constraint_text = "\n\nAdditional constraints from pipeline config:\n" + "\n".join(
                f"- {k}: {v}" for k, v in constraints.items()
            )

        user_prompt = (
            f"Analyse the following requirements and produce a DiscoveryArtifact.\n\n"
            f"--- REQUIREMENTS ---\n{requirements_text}\n--- END REQUIREMENTS ---"
            f"{constraint_text}\n\n"
            "Return ONLY a valid JSON object matching the DiscoveryArtifact schema. "
            "Every field is required. Use empty lists where nothing applies."
        )

        data = await self._llm_json(user_prompt, max_tokens=4096)
        artifact = DiscoveryArtifact.model_validate(data)
        self._save_artifact(artifact, "01_discovery_artifact.json")
        return artifact
