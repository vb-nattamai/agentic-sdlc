"""
tests/test_dynamic_agent_prompt.py — DynamicAgent system prompt synthesis.

Verifies that:
- The system prompt loads the universal base template
- Blueprint context is correctly appended (name, role, technology, port,
  depends_on, extra_instructions, output_subdir)
- Caching: system_prompt is only computed once
- Missing prompt file raises FileNotFoundError
- Various blueprint configurations produce the right prompt fragments

No LLM calls — we only test the prompt construction logic.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from agents.base_agent import DynamicAgent
from tests.conftest import make_blueprint


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _agent(name: str, **kwargs) -> DynamicAgent:
    bp = make_blueprint(name, **kwargs)
    return DynamicAgent(blueprint=bp, model="gpt-4o", output_dir="/tmp/test_run")


def _prompt(name: str, **kwargs) -> str:
    return _agent(name, **kwargs).system_prompt


# ---------------------------------------------------------------------------
# Blueprint context injection
# ---------------------------------------------------------------------------

class TestBlueprintContextInPrompt:
    def test_name_in_prompt(self):
        prompt = _prompt("auth_service")
        assert "auth_service" in prompt

    def test_role_in_prompt(self):
        prompt = _prompt("backend", role="REST API serving mobile clients")
        assert "REST API serving mobile clients" in prompt

    def test_technology_in_prompt(self):
        prompt = _prompt("backend", technology="Kotlin 1.9 + Spring Boot 3.3")
        assert "Kotlin 1.9 + Spring Boot 3.3" in prompt

    def test_port_in_prompt_when_set(self):
        prompt = _prompt("backend", port=8081)
        assert "8081" in prompt

    def test_port_absent_when_none(self):
        prompt = _prompt("worker", port=None)
        # Should not contain "port None" or similar
        assert "port None" not in prompt.lower()

    def test_output_subdir_in_prompt(self):
        prompt = _prompt("backend", output_subdir="backend_svc")
        assert "backend_svc" in prompt

    def test_depends_on_in_prompt(self):
        prompt = _prompt("bff", depends_on=["backend", "auth"])
        assert "backend" in prompt
        assert "auth" in prompt

    def test_depends_on_absent_when_empty(self):
        bp = make_blueprint("standalone", depends_on=[])
        agent = DynamicAgent(blueprint=bp, model="gpt-4o", output_dir="/tmp/x")
        prompt = agent.system_prompt
        # "calls these other services" section should not appear
        assert "calls these other services" not in prompt.lower()

    def test_extra_instructions_in_prompt(self):
        prompt = _prompt("backend", extra_instructions=[
            "Use JWT for all authenticated endpoints",
            "Expose /actuator/health",
        ])
        assert "Use JWT for all authenticated endpoints" in prompt
        assert "Expose /actuator/health" in prompt

    def test_extra_instructions_absent_when_empty(self):
        bp = make_blueprint("worker", extra_instructions=[])
        agent = DynamicAgent(blueprint=bp, model="gpt-4o", output_dir="/tmp/x")
        prompt = agent.system_prompt
        assert "Additional rules" not in prompt

    def test_generated_path_in_prompt(self):
        prompt = _prompt("backend", output_subdir="backend")
        assert "generated/backend/" in prompt

    def test_service_artifact_return_instruction(self):
        prompt = _prompt("my_svc")
        assert "ServiceArtifact" in prompt
        assert "my_svc" in prompt


# ---------------------------------------------------------------------------
# Universal base template included
# ---------------------------------------------------------------------------

class TestBaseTemplate:
    def test_base_template_content_present(self):
        """The prompt must include content from prompts/dynamic_agent.md."""
        base_content = Path("prompts/dynamic_agent.md").read_text(encoding="utf-8")
        prompt = _prompt("backend")
        # Check the first 200 chars of the base template appear in the prompt
        assert base_content[:200] in prompt

    def test_blueprint_block_appended_after_base(self):
        """Blueprint context must come AFTER the base template, not before."""
        base_content = Path("prompts/dynamic_agent.md").read_text(encoding="utf-8")
        prompt = _prompt("backend")
        base_end = prompt.index(base_content) + len(base_content)
        blueprint_start = prompt.index("THIS AGENT'S COMPONENT")
        assert blueprint_start >= base_end


# ---------------------------------------------------------------------------
# Prompt caching
# ---------------------------------------------------------------------------

class TestPromptCaching:
    def test_system_prompt_cached_after_first_call(self):
        agent = _agent("backend")
        prompt1 = agent.system_prompt
        prompt2 = agent.system_prompt
        assert prompt1 is prompt2  # same object in memory

    def test_cache_not_shared_between_agents(self):
        agent_a = _agent("svc_a", role="role A")
        agent_b = _agent("svc_b", role="role B")
        assert "role A" in agent_a.system_prompt
        assert "role B" in agent_b.system_prompt
        assert "role A" not in agent_b.system_prompt
        assert "role B" not in agent_a.system_prompt


# ---------------------------------------------------------------------------
# Error: missing prompt file
# ---------------------------------------------------------------------------

class TestMissingPromptFile:
    def test_raises_if_prompt_file_missing(self, tmp_path):
        bp = make_blueprint("backend")
        agent = DynamicAgent(blueprint=bp, model="gpt-4o", output_dir=str(tmp_path))
        agent.prompt_file = "prompts/nonexistent_file_xyz.md"
        agent._system_prompt = None  # clear any cached value
        with pytest.raises(FileNotFoundError):
            _ = agent.system_prompt


# ---------------------------------------------------------------------------
# Section delimiter
# ---------------------------------------------------------------------------

class TestSectionDelimiter:
    def test_delimiter_present(self):
        prompt = _prompt("backend")
        assert "THIS AGENT'S COMPONENT" in prompt

    def test_separator_between_base_and_blueprint(self):
        prompt = _prompt("backend")
        assert "---" in prompt
