"""
tests/test_orchestrator_state.py — PipelineState serialisation, compact_summary,
save/load round-trip, and active_agents handling.

No LLM calls, no subprocess. Filesystem writes go to pytest's tmp_path.
"""

from __future__ import annotations

import json
import textwrap

import pytest

from orchestrator import PipelineState
from tests.conftest import make_blueprint


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_state(
    requirements: str = "build a todo app",
    output_dir: str = "artifacts/test",
    **kwargs,
) -> PipelineState:
    return PipelineState(requirements=requirements, output_dir=output_dir, **kwargs)


def _blueprint_dict(name: str, depends_on: list[str] | None = None) -> dict:
    bp = make_blueprint(name, depends_on=depends_on)
    return bp.model_dump()


# ---------------------------------------------------------------------------
# Construction and defaults
# ---------------------------------------------------------------------------

class TestPipelineStateDefaults:
    def test_defaults_are_empty(self):
        state = _make_state()
        assert state.artifacts == {}
        assert state.tool_history == []
        assert state.completed_steps == []
        assert state.failed_attempts == {}
        assert state.constraints == {}
        assert state.config == {}
        assert state.active_agents == []

    def test_requirements_stored(self):
        state = _make_state(requirements="my requirements")
        assert state.requirements == "my requirements"

    def test_output_dir_stored(self):
        state = _make_state(output_dir="/tmp/run_123")
        assert state.output_dir == "/tmp/run_123"


# ---------------------------------------------------------------------------
# compact_summary
# ---------------------------------------------------------------------------

class TestCompactSummary:
    def test_returns_valid_json(self):
        state = _make_state()
        summary_str = state.compact_summary()
        data = json.loads(summary_str)
        assert isinstance(data, dict)

    def test_required_keys_present(self):
        state = _make_state()
        data = json.loads(state.compact_summary())
        assert "completed_steps" in data
        assert "artifacts_available" in data
        assert "active_agents" in data
        assert "recent_tool_history" in data
        assert "failed_attempts" in data
        assert "constraints" in data
        assert "output_dir" in data
        assert "requirements_length" in data

    def test_completed_steps_reflected(self):
        state = _make_state()
        state.completed_steps = ["discovery", "architecture"]
        data = json.loads(state.compact_summary())
        assert data["completed_steps"] == ["discovery", "architecture"]

    def test_artifacts_keys_reflected(self):
        state = _make_state()
        state.artifacts["discovery"] = {"summary": "todo app"}
        data = json.loads(state.compact_summary())
        assert "discovery" in data["artifacts_available"]

    def test_active_agents_summarised(self):
        state = _make_state()
        state.active_agents = [_blueprint_dict("backend"), _blueprint_dict("bff")]
        data = json.loads(state.compact_summary())
        names = [a["name"] for a in data["active_agents"]]
        assert "backend" in names
        assert "bff" in names

    def test_active_agents_summary_omits_file_contents(self):
        """active_agents in compact_summary should NOT include files or key_contracts."""
        state = _make_state()
        bp = _blueprint_dict("backend")
        bp["files"] = {"src/Main.kt": "big content"}  # simulated extra field
        state.active_agents = [bp]
        data = json.loads(state.compact_summary())
        agent_entry = data["active_agents"][0]
        assert "files" not in agent_entry

    def test_tool_history_truncated_in_summary(self):
        state = _make_state()
        # Add a history entry with very long output
        state.tool_history.append({
            "tool": "delegate_agent",
            "success": True,
            "output": "x" * 2000,
        })
        data = json.loads(state.compact_summary())
        history_output = data["recent_tool_history"][0]["output"]
        assert len(history_output) <= 520  # 500 chars + "[truncated]"

    def test_tool_history_window_is_8(self):
        state = _make_state()
        for i in range(15):
            state.tool_history.append({"tool": f"step_{i}", "success": True, "output": ""})
        data = json.loads(state.compact_summary())
        # Should include only last 8
        assert len(data["recent_tool_history"]) == 8
        assert data["recent_tool_history"][-1]["tool"] == "step_14"

    def test_failed_attempts_excludes_zeros(self):
        state = _make_state()
        state.failed_attempts = {"discovery": 0, "architecture": 2}
        data = json.loads(state.compact_summary())
        assert "discovery" not in data["failed_attempts"]
        assert data["failed_attempts"]["architecture"] == 2

    def test_requirements_length_correct(self):
        req = "build something"
        state = _make_state(requirements=req)
        data = json.loads(state.compact_summary())
        assert data["requirements_length"] == len(req)


# ---------------------------------------------------------------------------
# save / load round-trip
# ---------------------------------------------------------------------------

class TestSaveLoad:
    def test_basic_round_trip(self, tmp_path):
        state = _make_state(output_dir=str(tmp_path))
        state.completed_steps = ["discovery"]
        state.artifacts["discovery"] = {"summary": "todo app"}
        path = str(tmp_path / "state.json")
        state.save(path)

        loaded = PipelineState.load(path)
        assert loaded.requirements == state.requirements
        assert loaded.completed_steps == ["discovery"]
        assert loaded.artifacts["discovery"]["summary"] == "todo app"

    def test_active_agents_round_trip(self, tmp_path):
        state = _make_state(output_dir=str(tmp_path))
        state.active_agents = [
            _blueprint_dict("backend", depends_on=["auth"]),
            _blueprint_dict("auth"),
        ]
        path = str(tmp_path / "state.json")
        state.save(path)
        loaded = PipelineState.load(path)
        names = [a["name"] for a in loaded.active_agents]
        assert "backend" in names
        assert "auth" in names

    def test_constraints_round_trip(self, tmp_path):
        state = _make_state(output_dir=str(tmp_path))
        state.constraints["tech"] = "Kotlin only, no Scala"
        path = str(tmp_path / "state.json")
        state.save(path)
        loaded = PipelineState.load(path)
        assert loaded.constraints["tech"] == "Kotlin only, no Scala"

    def test_failed_attempts_round_trip(self, tmp_path):
        state = _make_state(output_dir=str(tmp_path))
        state.failed_attempts["architecture"] = 2
        path = str(tmp_path / "state.json")
        state.save(path)
        loaded = PipelineState.load(path)
        assert loaded.failed_attempts["architecture"] == 2

    def test_save_is_valid_json(self, tmp_path):
        state = _make_state(output_dir=str(tmp_path))
        path = str(tmp_path / "state.json")
        state.save(path)
        with open(path) as f:
            data = json.load(f)
        assert "requirements" in data
        assert "completed_steps" in data

    def test_save_creates_parent_dirs(self, tmp_path):
        state = _make_state(output_dir=str(tmp_path))
        nested_path = str(tmp_path / "deep" / "nested" / "state.json")
        state.save(nested_path)
        loaded = PipelineState.load(nested_path)
        assert loaded.requirements == state.requirements

    def test_load_missing_keys_use_defaults(self, tmp_path):
        """A minimal JSON file with only required keys should load successfully."""
        minimal = {"requirements": "test", "output_dir": str(tmp_path)}
        path = str(tmp_path / "minimal.json")
        with open(path, "w") as f:
            json.dump(minimal, f)
        loaded = PipelineState.load(path)
        assert loaded.completed_steps == []
        assert loaded.active_agents == []
        assert loaded.artifacts == {}

    def test_save_atomic_no_partial_file(self, tmp_path):
        """Save writes .tmp first; the final file should be complete JSON."""
        state = _make_state(output_dir=str(tmp_path))
        path = str(tmp_path / "state.json")
        state.save(path)
        # .tmp should have been cleaned up
        assert not (tmp_path / "state.json.tmp").exists()
        # Final file is readable
        PipelineState.load(path)


# ---------------------------------------------------------------------------
# Active agents management
# ---------------------------------------------------------------------------

class TestActiveAgents:
    def test_initially_empty(self):
        state = _make_state()
        assert state.active_agents == []

    def test_adding_blueprints(self):
        state = _make_state()
        state.active_agents = [_blueprint_dict("backend"), _blueprint_dict("frontend")]
        assert len(state.active_agents) == 2

    def test_blueprint_depends_on_preserved(self):
        state = _make_state()
        state.active_agents = [_blueprint_dict("bff", depends_on=["backend"])]
        assert state.active_agents[0]["depends_on"] == ["backend"]
