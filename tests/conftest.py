"""
Shared pytest fixtures and helpers.

These fixtures are available to all test modules without explicit import.
They use only stdlib + the project's own models — no LLM calls, no filesystem
writes, no subprocesses.  Every test in this suite must be fast (< 1 s each)
and runnable offline.
"""

from __future__ import annotations

import pytest

from models.artifacts import AgentBlueprint, ServiceArtifact


# ---------------------------------------------------------------------------
# Blueprint factory helpers
# ---------------------------------------------------------------------------

def make_blueprint(
    name: str,
    *,
    role: str = "test service",
    technology: str = "Python 3.12",
    port: int | None = 8080,
    output_subdir: str | None = None,
    depends_on: list[str] | None = None,
    extra_instructions: list[str] | None = None,
) -> AgentBlueprint:
    return AgentBlueprint(
        name=name,
        role=role,
        technology=technology,
        port=port,
        output_subdir=output_subdir or name,
        artifact_schema="ServiceArtifact",
        depends_on=depends_on or [],
        extra_instructions=extra_instructions or [],
    )


def make_service_artifact(name: str, files: dict[str, str] | None = None) -> ServiceArtifact:
    return ServiceArtifact(service=name, files=files or {})


# ---------------------------------------------------------------------------
# Commonly reused blueprint sets
# ---------------------------------------------------------------------------

@pytest.fixture
def linear_blueprints() -> list[AgentBlueprint]:
    """auth → backend → bff (strict chain, no parallelism)."""
    return [
        make_blueprint("auth"),
        make_blueprint("backend", depends_on=["auth"]),
        make_blueprint("bff", depends_on=["backend"]),
    ]


@pytest.fixture
def diamond_blueprints() -> list[AgentBlueprint]:
    """
    Classic diamond dependency:
        auth
       /    \\
    backend  worker
       \\    /
        bff
    """
    return [
        make_blueprint("auth"),
        make_blueprint("backend", depends_on=["auth"]),
        make_blueprint("worker", depends_on=["auth"]),
        make_blueprint("bff", depends_on=["backend", "worker"]),
    ]


@pytest.fixture
def independent_blueprints() -> list[AgentBlueprint]:
    """Three blueprints with no dependencies — all run in wave 0."""
    return [
        make_blueprint("svc_a"),
        make_blueprint("svc_b"),
        make_blueprint("svc_c"),
    ]


@pytest.fixture
def single_blueprint() -> list[AgentBlueprint]:
    return [make_blueprint("solo")]
