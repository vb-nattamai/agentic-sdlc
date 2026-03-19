"""
SpecAgent — generates the forward contract: OpenAPI 3.0 YAML + SQL DDL.

This is the most critical agent in the pipeline. Its output is reviewed
by a human before any code generation begins. In --from-run incremental mode
it preserves existing API paths by marking them with x-existing: true.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from agents.base_agent import BaseAgent
from models.artifacts import GeneratedSpecArtifact


class SpecAgent(BaseAgent):
    """
    API designer agent.

    Produces a complete OpenAPI 3.0 YAML specification and SQL DDL schema
    derived from the ArchitectureArtifact. In incremental mode, it can
    receive an existing spec and extend it without breaking the existing contract.
    """

    name = "spec"
    prompt_file = "prompts/spec_agent.md"

    async def run(self, context: dict[str, Any]) -> GeneratedSpecArtifact:
        """
        Generate OpenAPI 3.0 YAML + SQL DDL from architecture.

        Writes spec files to {output_dir}/generated/specs/:
        - openapi.yaml
        - schema.sql

        Args:
            context: Must contain:
                - discovery (dict): DiscoveryArtifact as dict.
                - architecture (dict): ArchitectureArtifact as dict.
                - tech_constraints (str, optional): Technology constraints.
                - arch_constraints (str, optional): Architectural constraints.
                - existing_spec (str, optional): Existing OpenAPI YAML for
                  incremental mode — existing paths get x-existing: true.

        Returns:
            GeneratedSpecArtifact with full YAML and DDL as strings.

        Raises:
            ValueError: If LLM response cannot be parsed.
        """
        discovery = context.get("discovery", {})
        architecture = context.get("architecture", {})
        tech_constraints = context.get("tech_constraints", "")
        arch_constraints = context.get("arch_constraints", "")
        existing_spec = context.get("existing_spec", "")

        existing_section = ""
        if existing_spec:
            existing_section = (
                "\n\nEXISTING SPEC (incremental mode):\n"
                "All paths from this spec must appear in your output with "
                "x-existing: true appended to their metadata.\n\n"
                f"{existing_spec}"
            )

        constraint_section = ""
        if tech_constraints:
            constraint_section += f"\n\nTechnology constraints: {tech_constraints}"
        if arch_constraints:
            constraint_section += f"\n\nArchitectural constraints: {arch_constraints}"

        user_prompt = (
            "Generate a complete OpenAPI 3.0 YAML specification AND a complete SQL DDL schema "
            "for the following architecture. The spec is the forward contract — all teams depend on it.\n\n"
            f"--- DISCOVERY ---\n{discovery}\n--- END ---\n\n"
            f"--- ARCHITECTURE ---\n{architecture}\n--- END ---"
            f"{constraint_section}"
            f"{existing_section}\n\n"
            "Rules:\n"
            "1. The openapi_yaml field must contain the COMPLETE OpenAPI 3.0 YAML as a single string.\n"
            "2. The sql_ddl field must contain the COMPLETE SQL DDL as a single string.\n"
            "3. Every endpoint from the architecture must appear in the spec.\n"
            "4. Include JWT security scheme and apply it to all protected endpoints.\n"
            "5. Include request/response schemas for every endpoint.\n"
            "6. Mark existing paths with 'x-existing: true' in their path-level metadata.\n\n"
            "Return ONLY a valid JSON object matching the GeneratedSpecArtifact schema."
        )

        data = await self._llm_json(user_prompt, max_tokens=4096)
        artifact = GeneratedSpecArtifact.model_validate(data)

        # Write spec files to disk
        specs_dir = Path(self.output_dir) / "generated" / "specs"
        specs_dir.mkdir(parents=True, exist_ok=True)

        (specs_dir / "openapi.yaml").write_text(artifact.openapi_yaml, encoding="utf-8")
        (specs_dir / "schema.sql").write_text(artifact.sql_ddl, encoding="utf-8")

        self._save_artifact(artifact, "04_generated_spec_artifact.json")
        return artifact
