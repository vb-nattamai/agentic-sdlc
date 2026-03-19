"""
ReviewAgent — security and quality gate for all generated source code.

Reads generated files from disk and reviews them against OWASP Top 10,
reliability best practices, code quality standards, and spec compliance.
Returns a ReviewArtifact with numeric scores and blocking/non-blocking issues.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agents.base_agent import BaseAgent
from models.artifacts import ReviewArtifact


class ReviewAgent(BaseAgent):
    """
    Security auditor and code quality reviewer.

    Scans all generated source files for:
    - OWASP Top 10 vulnerabilities
    - Reliability and resilience issues (timeouts, retries, error handling)
    - Code quality (duplication, complexity, naming conventions)
    - Spec compliance (all endpoints implemented, correct status codes)

    Sets passed=True ONLY if critical_issues is empty.
    """

    name = "review"
    prompt_file = "prompts/review_agent.md"

    async def run(self, context: dict[str, Any]) -> ReviewArtifact:
        """
        Review all generated source files and return a ReviewArtifact.

        Args:
            context: Must contain:
                - engineering (dict): EngineeringArtifact for file listing.
                - spec (dict): GeneratedSpecArtifact for compliance checking.
                - iteration (int): Which review pass this is (1-indexed).
                - previous_review (dict, optional): Last ReviewArtifact for
                  tracking improvement between iterations.

        Returns:
            ReviewArtifact with scores, issues, and pass/fail verdict.
        """
        engineering = context.get("engineering", {})
        spec = context.get("spec", {})
        iteration = context.get("iteration", 1)
        previous_review = context.get("previous_review", None)

        # Collect generated source files for review
        generated_dir = Path(self.output_dir) / "generated"
        file_samples: dict[str, str] = {}

        if generated_dir.exists():
            # Read a sample of source files (prioritise Kotlin and TypeScript)
            priority_patterns = ["**/*.kt", "**/*.tsx", "**/*.ts", "**/*.yaml", "**/*.yml"]
            for pattern in priority_patterns:
                for p in generated_dir.glob(pattern):
                    if p.is_file() and len(file_samples) < 20:
                        rel = str(p.relative_to(generated_dir))
                        content = p.read_text(encoding="utf-8", errors="replace")
                        file_samples[rel] = content[:2000]  # Sample per file

        # Also read directly from engineering artifact files dict
        for service_key in ("backend", "bff", "frontend"):
            service_data = engineering.get(service_key, {})
            if isinstance(service_data, dict):
                for path, content in list(service_data.get("files", {}).items())[:5]:
                    if path not in file_samples:
                        file_samples[path] = str(content)[:2000]

        files_summary = json.dumps(
            {k: v[:500] for k, v in list(file_samples.items())[:15]}, indent=2
        )

        prev_section = ""
        if previous_review:
            prev_section = (
                f"\n\nPREVIOUS REVIEW (iteration {iteration - 1}):\n"
                f"- Security score: {previous_review.get('security_score', 'N/A')}\n"
                f"- Critical issues: {previous_review.get('critical_issues', [])}\n"
                "Focus on whether those issues have been resolved."
            )

        user_prompt = (
            f"Review the following generated source files (iteration {iteration}).\n\n"
            f"--- OPENAPI SPEC (for compliance) ---\n"
            f"{spec.get('openapi_yaml', '')[:2000]}\n--- END ---\n\n"
            f"--- SOURCE FILE SAMPLES ---\n{files_summary}\n--- END ---"
            f"{prev_section}\n\n"
            "Review checklist:\n"
            "1. OWASP Top 10 (injection, broken auth, sensitive data exposure, etc.)\n"
            "2. JWT implementation correctness\n"
            "3. Error handling and resilience patterns\n"
            "4. API spec compliance (all endpoints present, correct response codes)\n"
            "5. SQL injection prevention (parameterised queries / JPA)\n"
            "6. CORS configuration\n"
            "7. Sensitive data in logs or responses\n"
            "8. Code quality (null safety, error propagation)\n\n"
            f"Return ONLY a valid JSON object matching the ReviewArtifact schema. "
            f"passed must be false if critical_issues is non-empty. "
            f"iteration must be {iteration}."
        )

        data = await self._llm_json(user_prompt, max_tokens=4096)

        # Enforce iteration value from context
        data["iteration"] = iteration

        # Enforce passed=False if critical_issues exist
        if data.get("critical_issues"):
            data["passed"] = False

        artifact = ReviewArtifact.model_validate(data)
        self._save_artifact(artifact, "04_review_artifact.json")
        return artifact
