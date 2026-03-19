"""
tests/test_models.py — Pydantic model validation and serialisation.

Tests that every artifact model accepts valid data, rejects invalid data,
round-trips through model_dump/model_validate, and has correct defaults.
No LLM calls, no I/O.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from models.artifacts import (
    AgentBlueprint,
    ArchitectureArtifact,
    DecisionRecord,
    DiscoveryArtifact,
    EngineeringArtifact,
    GeneratedSpecArtifact,
    InfrastructureArtifact,
    ReviewArtifact,
    ServiceArtifact,
    TestingArtifact,
)
from tests.conftest import make_blueprint, make_service_artifact


# ---------------------------------------------------------------------------
# AgentBlueprint
# ---------------------------------------------------------------------------

class TestAgentBlueprint:
    def test_minimal_valid(self):
        bp = AgentBlueprint(
            name="backend",
            role="REST API",
            technology="Python",
            output_subdir="backend",
        )
        assert bp.name == "backend"
        assert bp.depends_on == []
        assert bp.extra_instructions == []
        assert bp.port is None
        assert bp.artifact_schema == "ServiceArtifact"

    def test_full_valid(self):
        bp = make_blueprint(
            "worker",
            role="background processor",
            technology="Kotlin + Coroutines",
            port=9090,
            depends_on=["backend", "auth"],
            extra_instructions=["use exponential back-off"],
        )
        assert bp.port == 9090
        assert "backend" in bp.depends_on
        assert len(bp.extra_instructions) == 1

    def test_name_required(self):
        with pytest.raises(ValidationError):
            AgentBlueprint(role="x", technology="x", output_subdir="x")  # type: ignore[call-arg]

    def test_round_trip(self):
        bp = make_blueprint("svc", depends_on=["dep1"])
        restored = AgentBlueprint.model_validate(bp.model_dump())
        assert restored == bp

    def test_model_validate_from_dict(self):
        data = {
            "name": "frontend",
            "role": "UI",
            "technology": "React 18",
            "output_subdir": "frontend",
            "port": 3000,
            "depends_on": ["bff"],
        }
        bp = AgentBlueprint.model_validate(data)
        assert bp.port == 3000
        assert bp.depends_on == ["bff"]


# ---------------------------------------------------------------------------
# ServiceArtifact
# ---------------------------------------------------------------------------

class TestServiceArtifact:
    def test_empty_files(self):
        sa = ServiceArtifact(service="backend", files={})
        assert sa.files == {}

    def test_with_files(self):
        sa = make_service_artifact("backend", {"src/main.py": "print('hi')"})
        assert "src/main.py" in sa.files

    def test_round_trip(self):
        sa = make_service_artifact("worker", {"worker.py": "# worker"})
        restored = ServiceArtifact.model_validate(sa.model_dump())
        assert restored == sa


# ---------------------------------------------------------------------------
# EngineeringArtifact
# ---------------------------------------------------------------------------

class TestEngineeringArtifact:
    def test_empty_services(self):
        ea = EngineeringArtifact(services={})
        assert ea.services == {}
        assert ea.decisions == []

    def test_multiple_services(self):
        ea = EngineeringArtifact(
            services={
                "auth": make_service_artifact("auth"),
                "backend": make_service_artifact("backend", {"main.py": "x"}),
            }
        )
        assert "auth" in ea.services
        assert ea.services["backend"].files["main.py"] == "x"

    def test_round_trip(self):
        ea = EngineeringArtifact(
            services={"svc": make_service_artifact("svc", {"f.py": "code"})},
            decisions=[DecisionRecord(decision="Use async", rationale="perf", alternatives_rejected=["sync"])],
        )
        restored = EngineeringArtifact.model_validate(ea.model_dump())
        assert restored.services["svc"].files["f.py"] == "code"
        assert restored.decisions[0].decision == "Use async"


# ---------------------------------------------------------------------------
# ArchitectureArtifact
# ---------------------------------------------------------------------------

class TestArchitectureArtifact:
    def _base(self) -> dict:
        return {
            "style": "microservices",
            "components": [{"name": "backend", "type": "service"}],
            "data_flow": [{"from": "frontend", "to": "backend", "protocol": "HTTP"}],
            "api_contracts": [{"service": "backend", "endpoints": ["/health"]}],
            "security_model": {"auth": "JWT"},
            "deployment_model": {"platform": "docker"},
        }

    def test_valid_with_no_blueprints(self):
        aa = ArchitectureArtifact.model_validate(self._base())
        assert aa.agent_blueprints == []

    def test_valid_with_blueprints(self):
        data = self._base()
        data["agent_blueprints"] = [
            {
                "name": "backend",
                "role": "API",
                "technology": "Spring Boot",
                "output_subdir": "backend",
            }
        ]
        aa = ArchitectureArtifact.model_validate(data)
        assert len(aa.agent_blueprints) == 1
        assert aa.agent_blueprints[0].name == "backend"

    def test_round_trip(self):
        data = self._base()
        aa = ArchitectureArtifact.model_validate(data)
        restored = ArchitectureArtifact.model_validate(aa.model_dump())
        assert restored.style == "microservices"


# ---------------------------------------------------------------------------
# DecisionRecord
# ---------------------------------------------------------------------------

class TestDecisionRecord:
    def test_valid(self):
        dr = DecisionRecord(decision="Use JWT", rationale="stateless", alternatives_rejected=["sessions"])
        assert dr.decision == "Use JWT"
        assert dr.alternatives_rejected == ["sessions"]

    def test_empty_alternatives(self):
        dr = DecisionRecord(decision="t", rationale="r")
        assert dr.alternatives_rejected == []

    def test_round_trip(self):
        dr = DecisionRecord(decision="Use async", rationale="perf", alternatives_rejected=["sync", "threads"])
        restored = DecisionRecord.model_validate(dr.model_dump())
        assert restored == dr


# ---------------------------------------------------------------------------
# DiscoveryArtifact
# ---------------------------------------------------------------------------

class TestDiscoveryArtifact:
    def test_valid(self):
        da = DiscoveryArtifact(
            requirements=["create tasks", "sub-100ms response"],
            goals=["deliver todo app MVP"],
            constraints=["no Scala"],
            scope=["REST API", "web UI"],
            risks=["Postgres availability"],
            success_criteria=["all endpoints return 200"],
        )
        assert "create tasks" in da.requirements
        assert da.decisions == []

    def test_round_trip(self):
        da = DiscoveryArtifact(
            requirements=["req1"],
            goals=["goal1"],
            constraints=["c1"],
            scope=["s1"],
            risks=["r1"],
            success_criteria=["sc1"],
        )
        restored = DiscoveryArtifact.model_validate(da.model_dump())
        assert restored == da

    def test_missing_required_field(self):
        with pytest.raises(ValidationError):
            DiscoveryArtifact(requirements=["req1"], goals=["g1"])  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# GeneratedSpecArtifact
# ---------------------------------------------------------------------------

class TestGeneratedSpecArtifact:
    def test_valid(self):
        gsa = GeneratedSpecArtifact(
            openapi_yaml="openapi: '3.0.0'",
            sql_ddl="CREATE TABLE users (id SERIAL);",
            tech_constraints=["Java 21", "PostgreSQL 16"],
            arch_constraints=["stateless services", "JWT auth"],
        )
        assert gsa.openapi_yaml.startswith("openapi")
        assert "Java 21" in gsa.tech_constraints
        assert gsa.existing_paths == []
        assert gsa.decisions == []

    def test_round_trip(self):
        gsa = GeneratedSpecArtifact(
            openapi_yaml="openapi: '3.0.0'",
            sql_ddl="CREATE TABLE t (id INT);",
            tech_constraints=["Go 1.22"],
            arch_constraints=["async messaging"],
        )
        restored = GeneratedSpecArtifact.model_validate(gsa.model_dump())
        assert restored == gsa


# ---------------------------------------------------------------------------
# ReviewArtifact
# ---------------------------------------------------------------------------

class TestReviewArtifact:
    def test_passed(self):
        ra = ReviewArtifact(
            passed=True,
            iteration=1,
            security_score=0.9,
            reliability_score=0.95,
            quality_score=0.88,
            critical_issues=[],
            warnings=[],
        )
        assert ra.passed is True
        assert ra.iteration == 1
        assert ra.failed_services == []

    def test_failed_with_issues(self):
        ra = ReviewArtifact(
            passed=False,
            iteration=2,
            security_score=0.4,
            reliability_score=0.6,
            quality_score=0.5,
            critical_issues=["SQL injection risk"],
            warnings=["no rate limiting"],
        )
        assert ra.passed is False
        assert len(ra.critical_issues) == 1
        assert "no rate limiting" in ra.warnings

    def test_score_bounds(self):
        with pytest.raises(ValidationError):
            ReviewArtifact(
                passed=False,
                iteration=1,
                security_score=1.5,  # out of range
                reliability_score=0.5,
                quality_score=0.5,
                critical_issues=[],
                warnings=[],
            )


# ---------------------------------------------------------------------------
# InfrastructureArtifact
# ---------------------------------------------------------------------------

class TestInfrastructureArtifact:
    def test_plan_phase(self):
        ia = InfrastructureArtifact(
            phase="plan",
            files={"docker-compose.yml": "version: '3'"},
            services=["backend", "db"],
            health_endpoints={"backend": "http://localhost:8081/health"},
        )
        assert ia.phase == "plan"
        assert "docker-compose.yml" in ia.files

    def test_apply_phase(self):
        ia = InfrastructureArtifact(
            phase="apply",
            files={},
            services=["backend"],
            health_endpoints={},
            apply_result={"exit_code": 0},
        )
        assert ia.phase == "apply"
        assert ia.apply_result["exit_code"] == 0


# ---------------------------------------------------------------------------
# TestingArtifact
# ---------------------------------------------------------------------------

class TestTestingArtifactModel:
    def test_valid(self):
        ta = TestingArtifact(
            stage="live",
            passed=True,
            checks=[{"check_name": "GET /health", "passed": True, "detail": "200 OK"}],
        )
        assert ta.stage == "live"
        assert ta.passed is True
        assert ta.checks[0]["check_name"] == "GET /health"
        assert ta.failed_services == []
        assert ta.cypress_specs_generated is False

    def test_invalid_stage(self):
        with pytest.raises(ValidationError):
            TestingArtifact(stage="unknown", passed=True, checks=[])  # type: ignore[arg-type]

    def test_round_trip(self):
        ta = TestingArtifact(
            stage="architecture",
            passed=False,
            checks=[{"check_name": "coverage", "passed": False, "detail": "missing /users"}],
            failed_services=["backend"],
        )
        restored = TestingArtifact.model_validate(ta.model_dump())
        assert restored == ta
