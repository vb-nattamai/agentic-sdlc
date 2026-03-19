"""
tests/test_peer_summary.py — _peer_summary contract extraction.

_peer_summary() takes a completed ServiceArtifact and its blueprint and
returns a concise dict containing:
  - service name, technology, port, role
  - full list of generated file paths
  - key contract files (interfaces, DTOs, protos, clients, OpenAPI specs,
    TypeScript types, schemas) truncated to 1500 chars

No LLM calls, no I/O.
"""

from __future__ import annotations

import pytest

from agents.engineering_agent import _peer_summary
from tests.conftest import make_blueprint, make_service_artifact


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

CONTRACT_KEYS = (
    ".proto", "interface", "client", "api", "contract",
    "openapi", "types.ts", "schema", "dto",
)


def _make_peer(name: str, files: dict[str, str], **bp_kwargs):
    bp = make_blueprint(name, **bp_kwargs)
    sa = make_service_artifact(name, files)
    return _peer_summary(sa, bp)


# ---------------------------------------------------------------------------
# Required keys
# ---------------------------------------------------------------------------

class TestRequiredKeys:
    def test_contains_all_required_keys(self):
        peer = _make_peer("backend", {"src/Main.kt": "fun main() {}"})
        assert "service" in peer
        assert "technology" in peer
        assert "port" in peer
        assert "role" in peer
        assert "files" in peer
        assert "key_contracts" in peer

    def test_service_name_correct(self):
        peer = _make_peer("auth", {})
        assert peer["service"] == "auth"

    def test_technology_propagated(self):
        peer = _make_peer("backend", {}, technology="Kotlin 1.9 + Spring Boot 3.3")
        assert peer["technology"] == "Kotlin 1.9 + Spring Boot 3.3"

    def test_port_propagated(self):
        peer = _make_peer("backend", {}, port=8081)
        assert peer["port"] == 8081

    def test_port_none_when_unset(self):
        peer = _make_peer("worker", {}, port=None)
        assert peer["port"] is None

    def test_role_propagated(self):
        peer = _make_peer("backend", {}, role="REST API serving mobile clients")
        assert peer["role"] == "REST API serving mobile clients"


# ---------------------------------------------------------------------------
# File list
# ---------------------------------------------------------------------------

class TestFileList:
    def test_all_files_listed(self):
        files = {
            "src/Main.kt": "fun main() {}",
            "src/api/UserApi.kt": "interface UserApi {}",
            "build.gradle.kts": "plugins {}",
        }
        peer = _make_peer("backend", files)
        assert set(peer["files"]) == set(files.keys())

    def test_empty_files(self):
        peer = _make_peer("worker", {})
        assert peer["files"] == []

    def test_files_is_list_not_dict(self):
        peer = _make_peer("backend", {"main.py": "x"})
        assert isinstance(peer["files"], list)


# ---------------------------------------------------------------------------
# Contract extraction
# ---------------------------------------------------------------------------

class TestContractExtraction:
    def test_proto_file_is_contract(self):
        files = {"proto/user.proto": "syntax = 'proto3'; message User {}"}
        peer = _make_peer("backend", files)
        assert "proto/user.proto" in peer["key_contracts"]

    def test_interface_file_is_contract(self):
        files = {"src/UserInterface.kt": "interface UserInterface { fun get(): User }"}
        peer = _make_peer("backend", files)
        assert any("interface" in k.lower() for k in peer["key_contracts"])

    def test_client_file_is_contract(self):
        files = {"src/BackendClient.kt": "class BackendClient {}"}
        peer = _make_peer("backend", files)
        assert any("client" in k.lower() for k in peer["key_contracts"])

    def test_openapi_file_is_contract(self):
        files = {"specs/openapi.yaml": "openapi: '3.0'"}
        peer = _make_peer("backend", files)
        assert "specs/openapi.yaml" in peer["key_contracts"]

    def test_types_ts_is_contract(self):
        files = {"src/types.ts": "export interface User { id: number }"}
        peer = _make_peer("frontend", files)
        assert "src/types.ts" in peer["key_contracts"]

    def test_dto_file_is_contract(self):
        files = {"src/UserDto.kt": "data class UserDto(val id: Long)"}
        peer = _make_peer("backend", files)
        assert any("dto" in k.lower() for k in peer["key_contracts"])

    def test_non_contract_file_excluded(self):
        files = {
            "src/Application.kt": "fun main() {}",
            "Dockerfile": "FROM openjdk:21",
            "build.gradle.kts": "plugins {}",
        }
        peer = _make_peer("backend", files)
        # None of these should be contract files
        assert peer["key_contracts"] == {}

    def test_contract_content_included(self):
        content = "interface UserApi { fun getUser(id: Long): User }"
        files = {"src/UserInterface.kt": content}
        peer = _make_peer("backend", files)
        contract_values = list(peer["key_contracts"].values())
        assert len(contract_values) == 1
        assert contract_values[0] == content

    def test_contract_content_truncated_at_1500(self):
        long_content = "x" * 3000
        files = {"src/BigInterface.kt": long_content}
        peer = _make_peer("backend", files)
        for v in peer["key_contracts"].values():
            assert len(v) <= 1500

    def test_multiple_contract_files(self):
        files = {
            "proto/user.proto": "message User {}",
            "src/UserDto.kt": "data class UserDto(val id: Long)",
            "src/UserApi.kt": "interface UserApi {}",
            "README.md": "# service",
        }
        peer = _make_peer("backend", files)
        # README should not be there; the others should
        assert "README.md" not in peer["key_contracts"]
        assert len(peer["key_contracts"]) == 3

    def test_api_in_path_is_contract(self):
        files = {"src/api/UserController.kt": "class UserController {}"}
        peer = _make_peer("backend", files)
        assert "src/api/UserController.kt" in peer["key_contracts"]

    def test_schema_file_is_contract(self):
        files = {"schema/user.graphql": "type User { id: ID! }"}
        peer = _make_peer("graphql_gateway", files)
        assert "schema/user.graphql" in peer["key_contracts"]
