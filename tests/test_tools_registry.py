"""
tests/test_tools_registry.py — ToolResult model + TOOL_REGISTRY shape.

Tests that don't require LLM calls or subprocesses:
- ToolResult Pydantic model validation and defaults
- TOOL_REGISTRY contains all expected tool names and callables
- file_write + file_read tool round-trip (real filesystem via tmp_path)
- file_patch applies correctly
- file_list returns expected paths
- shell_exec basic command execution
"""

from __future__ import annotations

import asyncio
import os

import pytest

from tools.registry import (
    TOOL_REGISTRY,
    ToolResult,
    file_list,
    file_patch,
    file_read,
    file_write,
    shell_exec,
)


# ---------------------------------------------------------------------------
# ToolResult model
# ---------------------------------------------------------------------------

class TestToolResult:
    def test_success_defaults(self):
        r = ToolResult(tool="file_read", success=True, output="hello")
        assert r.tool == "file_read"
        assert r.success is True
        assert r.output == "hello"
        assert r.error is None
        assert r.metadata == {}

    def test_failure_with_error(self):
        r = ToolResult(tool="shell_exec", success=False, output="", error="command not found")
        assert r.success is False
        assert r.error == "command not found"

    def test_metadata_stored(self):
        r = ToolResult(tool="spawn_agent", success=True, output="{}", metadata={"agent": "backend", "files_generated": 5})
        assert r.metadata["agent"] == "backend"
        assert r.metadata["files_generated"] == 5

    def test_round_trip(self):
        r = ToolResult(tool="file_write", success=True, output="written", metadata={"path": "/tmp/x"})
        restored = ToolResult.model_validate(r.model_dump())
        assert restored.tool == "file_write"
        assert restored.metadata["path"] == "/tmp/x"


# ---------------------------------------------------------------------------
# TOOL_REGISTRY completeness
# ---------------------------------------------------------------------------

EXPECTED_TOOLS = {
    "shell_exec",
    "file_read",
    "file_write",
    "file_patch",
    "file_list",
    "web_fetch",
    "api_call",
    "delegate_agent",
    "extract_blueprints",
    "spawn_agent",
}


class TestToolRegistryShape:
    def test_all_expected_tools_present(self):
        for tool in EXPECTED_TOOLS:
            assert tool in TOOL_REGISTRY, f"Missing tool: {tool}"

    def test_all_values_are_callable(self):
        for name, fn in TOOL_REGISTRY.items():
            assert callable(fn), f"TOOL_REGISTRY['{name}'] is not callable"

    def test_no_static_agent_tools(self):
        """backend / bff / frontend should NOT be in the registry any more."""
        for removed in ("backend", "bff", "frontend"):
            assert removed not in TOOL_REGISTRY

    def test_registry_has_no_extra_unknown_tools(self):
        """Catch accidental additions."""
        unknown = set(TOOL_REGISTRY.keys()) - EXPECTED_TOOLS
        assert unknown == set(), f"Unexpected tools in registry: {unknown}"


# ---------------------------------------------------------------------------
# file_write + file_read round-trip
# ---------------------------------------------------------------------------

class TestFileWriteRead:
    def test_write_then_read(self, tmp_path):
        path = str(tmp_path / "hello.txt")
        result = asyncio.run(file_write(path, "hello world"))
        assert result.success is True

        read_result = asyncio.run(file_read(path))
        assert read_result.success is True
        assert "hello world" in read_result.output

    def test_write_creates_parent_dirs(self, tmp_path):
        path = str(tmp_path / "deep" / "nested" / "file.txt")
        result = asyncio.run(file_write(path, "content"))
        assert result.success is True
        assert os.path.exists(path)

    def test_read_nonexistent_file(self, tmp_path):
        path = str(tmp_path / "nonexistent.txt")
        result = asyncio.run(file_read(path))
        assert result.success is False
        assert result.error is not None

    def test_write_overwrites_existing(self, tmp_path):
        path = str(tmp_path / "file.txt")
        asyncio.run(file_write(path, "original"))
        asyncio.run(file_write(path, "updated"))
        result = asyncio.run(file_read(path))
        assert "updated" in result.output
        assert "original" not in result.output

    def test_read_truncates_at_8000_chars(self, tmp_path):
        path = str(tmp_path / "large.txt")
        content = "x" * 10_000
        asyncio.run(file_write(path, content))
        result = asyncio.run(file_read(path))
        assert result.success is True
        assert len(result.output) <= 8100  # 8000 + possible truncation marker


# ---------------------------------------------------------------------------
# file_patch
# ---------------------------------------------------------------------------

class TestFilePatch:
    def test_patch_replaces_first_occurrence(self, tmp_path):
        path = str(tmp_path / "code.py")
        asyncio.run(file_write(path, "foo = 1\nfoo = 2\n"))
        result = asyncio.run(file_patch(path, "foo = 1", "foo = 99"))
        assert result.success is True
        read_result = asyncio.run(file_read(path))
        assert "foo = 99" in read_result.output
        assert "foo = 2" in read_result.output  # second occurrence untouched

    def test_patch_fails_if_old_str_not_found(self, tmp_path):
        path = str(tmp_path / "code.py")
        asyncio.run(file_write(path, "hello world"))
        result = asyncio.run(file_patch(path, "nonexistent string", "replacement"))
        assert result.success is False
        assert result.error is not None


# ---------------------------------------------------------------------------
# file_list
# ---------------------------------------------------------------------------

class TestFileList:
    def test_lists_files_matching_pattern(self, tmp_path):
        (tmp_path / "a.py").write_text("x")
        (tmp_path / "b.py").write_text("x")
        (tmp_path / "c.txt").write_text("x")
        result = asyncio.run(file_list(str(tmp_path), "*.py"))
        assert result.success is True
        assert "a.py" in result.output
        assert "b.py" in result.output
        assert "c.txt" not in result.output

    def test_lists_all_files_with_glob(self, tmp_path):
        (tmp_path / "x.json").write_text("{}")
        result = asyncio.run(file_list(str(tmp_path), "**/*"))
        assert result.success is True
        assert "x.json" in result.output

    def test_empty_directory(self, tmp_path):
        result = asyncio.run(file_list(str(tmp_path), "**/*"))
        assert result.success is True  # should not raise


# ---------------------------------------------------------------------------
# shell_exec
# ---------------------------------------------------------------------------

class TestShellExec:
    def test_simple_command(self):
        result = asyncio.run(shell_exec("echo hello"))
        assert result.success is True
        assert "hello" in result.output

    def test_exit_nonzero_is_failure(self):
        result = asyncio.run(shell_exec("exit 1"))
        assert result.success is False

    def test_cwd_respected(self, tmp_path):
        result = asyncio.run(shell_exec("pwd", cwd=str(tmp_path)))
        assert result.success is True
        assert str(tmp_path) in result.output

    def test_timeout_kills_command(self):
        result = asyncio.run(shell_exec("sleep 10", timeout=1))
        assert result.success is False
        assert result.error is not None
