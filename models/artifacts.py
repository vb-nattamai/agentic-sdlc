"""
Pydantic v2 models for all Agentic SDLC pipeline artifacts.

Each artifact corresponds to the output of a specialist agent and is persisted
as a numbered JSON file under the run's output directory.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


__all__ = [
    "AgentBlueprint",
    "DecisionRecord",
    "DiscoveryArtifact",
    "ArchitectureArtifact",
    "GeneratedSpecArtifact",
    "ServiceArtifact",
    "EngineeringArtifact",
    "InfrastructureArtifact",
    "ReviewArtifact",
    "TestingArtifact",
]


class AgentBlueprint(BaseModel):
    """
    Describes a short-lived dynamic agent that the orchestrator spawns based on
    what the ArchitectureArtifact says needs to be built.

    The orchestrator reads the architecture's component list, decides which
    components need code generation, and emits one AgentBlueprint per component.
    The `spawn_agent` tool instantiates a DynamicAgent from this blueprint —
    no hardcoded agent class is used.
    """

    name: str = Field(
        ...,
        description=(
            "Unique snake_case identifier for this agent, e.g. 'backend', 'worker', "
            "'graphql_gateway'. Must be unique within a pipeline run."
        ),
    )
    role: str = Field(
        ...,
        description="One sentence describing what this agent generates.",
    )
    technology: str = Field(
        ...,
        description="Technology stack string, e.g. 'Kotlin 1.9 + Spring Boot 3.3'.",
    )
    port: int | None = Field(
        default=None,
        description="Network port this component listens on, if applicable.",
    )
    output_subdir: str = Field(
        ...,
        description=(
            "Directory under generated/ where this agent writes files, "
            "e.g. 'backend', 'bff', 'frontend', 'worker'."
        ),
    )
    artifact_schema: str = Field(
        default="ServiceArtifact",
        description=(
            "Name of the Pydantic artifact model this agent returns. "
            "Currently supported: 'ServiceArtifact'."
        ),
    )
    extra_instructions: list[str] = Field(
        default_factory=list,
        description=(
            "Additional generation rules specific to this component "
            "(e.g. 'must expose /health endpoint', 'use coroutines throughout')."
        ),
    )
    depends_on: list[str] = Field(
        default_factory=list,
        description="Names of other blueprint agents this component calls at runtime.",
    )


class DecisionRecord(BaseModel):
    """Records an architectural or engineering decision with its rationale."""

    decision: str = Field(..., description="The decision that was made.")
    rationale: str = Field(..., description="Why this decision was chosen.")
    alternatives_rejected: list[str] = Field(
        default_factory=list,
        description="Alternative options that were considered and rejected.",
    )


class DiscoveryArtifact(BaseModel):
    """Output of the DiscoveryAgent — structured breakdown of project requirements."""

    requirements: list[str] = Field(
        ..., description="Functional and non-functional requirements extracted from input."
    )
    goals: list[str] = Field(..., description="High-level business and technical goals.")
    constraints: list[str] = Field(
        ..., description="Technical, business, or regulatory constraints."
    )
    scope: list[str] = Field(
        ..., description="What is explicitly in scope for this project."
    )
    risks: list[str] = Field(
        ..., description="Identified technical or delivery risks."
    )
    success_criteria: list[str] = Field(
        ..., description="Measurable criteria that define a successful delivery."
    )
    decisions: list[DecisionRecord] = Field(
        default_factory=list,
        description="Key decisions made during discovery.",
    )


class ArchitectureArtifact(BaseModel):
    """Output of the ArchitectureAgent — full system design."""

    style: str = Field(
        ..., description="Architecture style, e.g. 'layered monolith', 'microservices'."
    )
    components: list[dict[str, Any]] = Field(
        ...,
        description="List of components, each with: name, responsibility, technology, port.",
    )
    data_flow: list[dict[str, Any]] = Field(
        ...,
        description="Data flow descriptions: from, to, protocol, description.",
    )
    api_contracts: list[dict[str, Any]] = Field(
        ...,
        description="High-level endpoint summary per service.",
    )
    security_model: dict[str, Any] = Field(
        ..., description="Authentication, authorisation, and transport security design."
    )
    deployment_model: dict[str, Any] = Field(
        ..., description="Container and networking deployment topology."
    )
    agent_blueprints: list[AgentBlueprint] = Field(
        default_factory=list,
        description=(
            "Short-lived agent blueprints derived from the component list. "
            "The orchestrator reads this field to decide which dynamic agents to spawn "
            "for code generation — one blueprint per deployable component. "
            "Populated by ArchitectureAgent; consumed by the orchestrator's spawn_agent tool."
        ),
    )
    decisions: list[DecisionRecord] = Field(
        default_factory=list,
        description="Key architectural decisions.",
    )


class GeneratedSpecArtifact(BaseModel):
    """Output of the SpecAgent — forward contract between services."""

    openapi_yaml: str = Field(
        ..., description="Complete OpenAPI 3.0 YAML specification as a string."
    )
    sql_ddl: str = Field(
        ..., description="Complete SQL DDL schema as a string."
    )
    tech_constraints: list[str] = Field(
        ..., description="Technology stack constraints derived from architecture."
    )
    arch_constraints: list[str] = Field(
        ..., description="Architectural constraints (e.g. stateless services, JWT auth)."
    )
    existing_paths: list[str] = Field(
        default_factory=list,
        description="API paths marked x-existing: true (used in --from-run incremental mode).",
    )
    decisions: list[DecisionRecord] = Field(
        default_factory=list,
        description="Key spec design decisions.",
    )


class ServiceArtifact(BaseModel):
    """Output of a single service agent (BackendAgent, BffAgent, FrontendAgent)."""

    service: str = Field(
        ..., description="Service identifier: 'backend' | 'bff' | 'frontend'."
    )
    files: dict[str, str] = Field(
        ..., description="Map of relative_path -> file_content for all generated files."
    )
    decisions: list[DecisionRecord] = Field(
        default_factory=list,
        description="Key implementation decisions.",
    )


class EngineeringArtifact(BaseModel):
    """Output of the EngineeringAgent — all spawned DynamicAgent ServiceArtifacts combined."""

    services: dict[str, ServiceArtifact] = Field(
        default_factory=dict,
        description="Map of blueprint name -> ServiceArtifact for each spawned DynamicAgent.",
    )
    decisions: list[DecisionRecord] = Field(
        default_factory=list,
        description="Cross-cutting engineering decisions.",
    )


class InfrastructureArtifact(BaseModel):
    """Output of the InfrastructureAgent — Docker IaC and deployment results."""

    phase: Literal["plan", "apply"] = Field(
        ..., description="'plan' = generate IaC files; 'apply' = run docker compose."
    )
    files: dict[str, str] = Field(
        ...,
        description="Map of relative_path -> file_content (Dockerfiles, docker-compose.yml, etc.).",
    )
    services: list[str] = Field(
        ..., description="Container/service names managed by docker compose."
    )
    health_endpoints: dict[str, str] = Field(
        ..., description="Map of service_name -> health check URL."
    )
    apply_result: dict[str, Any] = Field(
        default_factory=dict,
        description="Populated during phase=apply with container status and health results.",
    )
    decisions: list[DecisionRecord] = Field(
        default_factory=list,
        description="Infrastructure decisions.",
    )


class ReviewArtifact(BaseModel):
    """Output of the ReviewAgent — security and quality gate result."""

    passed: bool = Field(
        ..., description="True only if critical_issues is empty and all scores meet thresholds."
    )
    iteration: int = Field(
        ..., description="Which review iteration this is (1-indexed)."
    )
    security_score: float = Field(
        ..., ge=0.0, le=1.0, description="Security score from 0.0 to 1.0."
    )
    reliability_score: float = Field(
        ..., ge=0.0, le=1.0, description="Reliability / resilience score from 0.0 to 1.0."
    )
    quality_score: float = Field(
        ..., ge=0.0, le=1.0, description="Code quality score from 0.0 to 1.0."
    )
    critical_issues: list[str] = Field(
        ..., description="Blocking issues that must be fixed before proceeding."
    )
    warnings: list[str] = Field(
        ..., description="Non-blocking issues to be aware of."
    )
    failed_services: list[str] = Field(
        default_factory=list,
        description="Service names (backend/bff/frontend) that have blocking issues.",
    )
    decisions: list[DecisionRecord] = Field(
        default_factory=list,
        description="Review methodology decisions.",
    )


class TestingArtifact(BaseModel):
    """Output of the TestingAgent — validation results at a specific pipeline stage."""

    stage: Literal["architecture", "live", "final"] = Field(
        ...,
        description=(
            "'architecture' = spec coverage check; "
            "'live' = HTTP tests against running containers; "
            "'final' = requirements traceability."
        ),
    )
    passed: bool = Field(..., description="True if all checks in this stage passed.")
    checks: list[dict[str, Any]] = Field(
        ...,
        description="Individual check results: check_name, passed (bool), detail (str).",
    )
    failed_services: list[str] = Field(
        default_factory=list,
        description="Services with failing live HTTP tests (stage=live only).",
    )
    cypress_specs_generated: bool = Field(
        default=False,
        description="True if Cypress e2e spec files were written (stage=live only).",
    )
    decisions: list[DecisionRecord] = Field(
        default_factory=list,
        description="Testing strategy decisions.",
    )
