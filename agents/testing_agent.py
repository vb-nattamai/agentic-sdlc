"""
TestingAgent — validates the pipeline at three stages.

stage="architecture": Validates that the OpenAPI spec covers all discovery requirements.
stage="live":         Runs HTTP tests against running containers, generates Cypress specs.
stage="final":        Requirements traceability — confirms the running system
                      satisfies every requirement from DiscoveryArtifact.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from agents.base_agent import BaseAgent
from models.artifacts import TestingArtifact


class TestingAgent(BaseAgent):
    """
    QA engineer agent with three testing modes.

    Architecture mode: static analysis of spec completeness.
    Live mode: actual HTTP requests against deployed containers.
    Final mode: requirement-by-requirement traceability check.
    """

    name = "testing"
    prompt_file = "prompts/testing_agent.md"

    async def run(self, context: dict[str, Any]) -> TestingArtifact:
        """
        Run tests appropriate for the current pipeline stage.

        Args:
            context: Must contain:
                - stage (str): "architecture" | "live" | "final"
                - discovery (dict): DiscoveryArtifact.
                - spec (dict): GeneratedSpecArtifact.
                - architecture (dict): ArchitectureArtifact.
                - output_dir (str, optional): Override output directory.
                - base_urls (dict, optional): service -> URL for live stage.
                  Defaults: backend=http://localhost:8081,
                             bff=http://localhost:8080,
                             frontend=http://localhost:3000

        Returns:
            TestingArtifact with check results.
        """
        stage = context.get("stage", "architecture")
        output_dir = context.get("output_dir", self.output_dir)

        if stage == "architecture":
            return await self._test_architecture(context)
        elif stage == "live":
            return await self._test_live(context, output_dir)
        else:
            return await self._test_final(context)

    async def _test_architecture(self, context: dict[str, Any]) -> TestingArtifact:
        """
        Validate that the OpenAPI spec covers all discovery requirements.

        Uses the LLM to cross-reference every requirement from the
        DiscoveryArtifact against the endpoints in the OpenAPI spec.

        Args:
            context: Contains discovery and spec dicts.

        Returns:
            TestingArtifact with stage="architecture".
        """
        discovery = context.get("discovery", {})
        spec = context.get("spec", {})

        user_prompt = (
            "Validate that the OpenAPI spec covers all requirements from the discovery artifact.\n\n"
            f"--- REQUIREMENTS ---\n{json.dumps(discovery.get('requirements', []), indent=2)}\n--- END ---\n\n"
            f"--- SUCCESS CRITERIA ---\n{json.dumps(discovery.get('success_criteria', []), indent=2)}\n--- END ---\n\n"
            f"--- OPENAPI SPEC ---\n{spec.get('openapi_yaml', '')[:4000]}\n--- END ---\n\n"
            "For each requirement, determine whether the spec provides adequate coverage.\n"
            "Return ONLY a valid JSON object matching the TestingArtifact schema with stage='architecture'.\n"
            "Each item in checks must have: check_name (str), passed (bool), detail (str).\n"
            "passed at top level = true only if ALL checks passed."
        )

        data = await self._llm_json(user_prompt, max_tokens=1500)
        data["stage"] = "architecture"
        artifact = TestingArtifact.model_validate(data)
        self._save_artifact(artifact, "05a_testing_architecture.json")
        return artifact

    async def _test_live(
        self, context: dict[str, Any], output_dir: str
    ) -> TestingArtifact:
        """
        Run live HTTP tests against deployed containers.

        Generates test cases from the OpenAPI spec, executes them with httpx,
        writes Cypress e2e spec files to generated/cypress/, and identifies
        which services have failing tests.

        Args:
            context:    Contains spec, base_urls, and other context.
            output_dir: Root output directory for Cypress spec output.

        Returns:
            TestingArtifact with stage="live" and failed_services populated.
        """
        spec = context.get("spec", {})
        base_urls = context.get(
            "base_urls",
            {
                "backend": "http://localhost:8081",
                "bff": "http://localhost:8080",
                "frontend": "http://localhost:3000",
            },
        )

        # Generate test cases from spec using LLM
        test_gen_prompt = (
            "Generate HTTP test cases from this OpenAPI spec.\n\n"
            f"--- OPENAPI SPEC ---\n{spec.get('openapi_yaml', '')[:4000]}\n--- END ---\n\n"
            "Return a JSON array of test cases. Each test case:\n"
            "{ \"name\": str, \"method\": str, \"path\": str, \"service\": \"backend|bff\", "
            "\"expected_status\": int, \"headers\": {}, \"body\": {} }\n\n"
            "Include: health checks, auth endpoints, main CRUD operations. "
            "Use realistic but safe test data. Return JSON array only."
        )

        test_cases_raw = await self._llm_json(test_gen_prompt, max_tokens=1500)
        test_cases = test_cases_raw if isinstance(test_cases_raw, list) else test_cases_raw.get("tests", [])

        checks: list[dict[str, Any]] = []
        failed_services: set[str] = set()

        async with httpx.AsyncClient(timeout=15.0) as client:
            for tc in test_cases[:30]:  # Cap at 30 tests to avoid timeout
                service = tc.get("service", "bff")
                base = base_urls.get(service, base_urls.get("bff", "http://localhost:8080"))
                url = f"{base.rstrip('/')}/{tc.get('path', '').lstrip('/')}"
                method = tc.get("method", "GET").upper()
                expected = tc.get("expected_status", 200)
                name = tc.get("name", url)

                try:
                    resp = await client.request(
                        method=method,
                        url=url,
                        headers=tc.get("headers", {}),
                        json=tc.get("body") or None,
                    )
                    passed = resp.status_code == expected
                    detail = f"HTTP {resp.status_code} (expected {expected})"
                    if not passed:
                        failed_services.add(service)
                except Exception as exc:  # noqa: BLE001
                    passed = False
                    detail = f"Request failed: {exc}"
                    failed_services.add(service)

                checks.append({"check_name": name, "passed": passed, "detail": detail})

        # Generate Cypress e2e specs
        cypress_generated = await self._generate_cypress_specs(spec, base_urls, output_dir)

        all_passed = all(c["passed"] for c in checks)
        artifact = TestingArtifact(
            stage="live",
            passed=all_passed,
            checks=checks,
            failed_services=sorted(failed_services),
            cypress_specs_generated=cypress_generated,
        )
        self._save_artifact(artifact, "05b_testing_infrastructure.json")
        return artifact

    async def _test_final(self, context: dict[str, Any]) -> TestingArtifact:
        """
        Requirements traceability — confirm each original requirement is met.

        Args:
            context: Contains discovery, spec, and architecture dicts.

        Returns:
            TestingArtifact with stage="final".
        """
        discovery = context.get("discovery", {})
        spec = context.get("spec", {})
        architecture = context.get("architecture", {})

        user_prompt = (
            "Perform a final requirements traceability check.\n\n"
            f"--- ORIGINAL REQUIREMENTS ---\n"
            f"{json.dumps(discovery.get('requirements', []), indent=2)}\n--- END ---\n\n"
            f"--- SUCCESS CRITERIA ---\n"
            f"{json.dumps(discovery.get('success_criteria', []), indent=2)}\n--- END ---\n\n"
            f"--- IMPLEMENTED SPEC ---\n{spec.get('openapi_yaml', '')[:3000]}\n--- END ---\n\n"
            f"--- ARCHITECTURE ---\n{json.dumps(architecture, indent=2)[:2000]}\n--- END ---\n\n"
            "For each original requirement and success criterion, determine if it is met.\n"
            "Return ONLY a valid JSON object matching the TestingArtifact schema with stage='final'.\n"
            "Each check: check_name (requirement text), passed (bool), detail (explanation).\n"
            "Top-level passed = true only if ALL requirements are traced and met."
        )

        data = await self._llm_json(user_prompt, max_tokens=1500)
        data["stage"] = "final"
        artifact = TestingArtifact.model_validate(data)
        self._save_artifact(artifact, "05c_testing_review.json")
        return artifact

    async def _generate_cypress_specs(
        self,
        spec: dict[str, Any],
        base_urls: dict[str, str],
        output_dir: str,
    ) -> bool:
        """
        Generate Cypress e2e spec files from the OpenAPI specification.

        Args:
            spec:       GeneratedSpecArtifact dict.
            base_urls:  Service URL mapping.
            output_dir: Root output directory for cypress/ folder.

        Returns:
            True if Cypress specs were successfully written to disk.
        """
        try:
            cypress_prompt = (
                "Generate a Cypress e2e test file for this API.\n\n"
                f"--- OPENAPI SPEC ---\n{spec.get('openapi_yaml', '')[:3000]}\n--- END ---\n\n"
                f"BFF base URL: {base_urls.get('bff', 'http://localhost:8080')}\n"
                f"Frontend URL: {base_urls.get('frontend', 'http://localhost:3000')}\n\n"
                "Generate a complete Cypress TypeScript spec file that:\n"
                "- Tests all main user flows\n"
                "- Includes authentication flow\n"
                "- Uses cy.request() for API tests\n"
                "- Uses cy.visit() for UI tests\n\n"
                "Return ONLY the raw TypeScript file content."
            )

            cypress_content = await self._llm(cypress_prompt, max_tokens=1500, response_format="text")

            cypress_dir = Path(output_dir) / "generated" / "cypress"
            cypress_dir.mkdir(parents=True, exist_ok=True)
            (cypress_dir / "e2e_spec.cy.ts").write_text(cypress_content, encoding="utf-8")
            return True

        except Exception:  # noqa: BLE001
            return False
