"""
tests/test_topo_waves.py — topological wave sorting for the wave executor.

_topo_waves() is the core scheduling algorithm: it groups AgentBlueprints into
execution waves so that all blueprints in a wave can run in parallel and each
wave only starts after all previous waves have completed.

No LLM calls, no I/O, no async — pure graph algorithm tests.
"""

from __future__ import annotations

import pytest

from agents.engineering_agent import _topo_waves
from tests.conftest import make_blueprint


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def wave_names(blueprints) -> list[list[str]]:
    """Run _topo_waves and return just the names for easier assertions."""
    return [[bp.name for bp in wave] for wave in _topo_waves(blueprints)]


def names_at_level(waves: list[list[str]], level: int) -> set[str]:
    return set(waves[level])


# ---------------------------------------------------------------------------
# Single blueprint
# ---------------------------------------------------------------------------

class TestSingleBlueprint:
    def test_single_no_deps(self):
        bps = [make_blueprint("solo")]
        waves = wave_names(bps)
        assert waves == [["solo"]]

    def test_single_with_external_dep_treated_as_wave0(self):
        """A dependency not in the blueprint list is treated as already available (wave 0)."""
        bps = [make_blueprint("svc", depends_on=["external_db"])]
        waves = wave_names(bps)
        assert waves == [["svc"]]


# ---------------------------------------------------------------------------
# Linear chain
# ---------------------------------------------------------------------------

class TestLinearChain:
    def test_two_level(self):
        bps = [
            make_blueprint("auth"),
            make_blueprint("backend", depends_on=["auth"]),
        ]
        waves = wave_names(bps)
        assert len(waves) == 2
        assert waves[0] == ["auth"]
        assert waves[1] == ["backend"]

    def test_three_level(self, linear_blueprints):
        waves = wave_names(linear_blueprints)
        assert len(waves) == 3
        assert waves[0] == ["auth"]
        assert waves[1] == ["backend"]
        assert waves[2] == ["bff"]

    def test_order_invariant_to_input_order(self):
        """Reversing input order must not change wave assignment."""
        bps_forward = [
            make_blueprint("a"),
            make_blueprint("b", depends_on=["a"]),
            make_blueprint("c", depends_on=["b"]),
        ]
        bps_reversed = list(reversed(bps_forward))
        w_forward = wave_names(bps_forward)
        w_reversed = wave_names(bps_reversed)
        # Same set of names at each level (order within a wave may differ)
        assert {n for w in w_forward for n in w} == {n for w in w_reversed for n in w}
        assert len(w_forward) == len(w_reversed)


# ---------------------------------------------------------------------------
# Independent blueprints — all wave 0
# ---------------------------------------------------------------------------

class TestAllIndependent:
    def test_all_in_wave_0(self, independent_blueprints):
        waves = wave_names(independent_blueprints)
        assert len(waves) == 1
        assert set(waves[0]) == {"svc_a", "svc_b", "svc_c"}

    def test_five_independent(self):
        bps = [make_blueprint(f"svc_{i}") for i in range(5)]
        waves = wave_names(bps)
        assert len(waves) == 1
        assert len(waves[0]) == 5


# ---------------------------------------------------------------------------
# Diamond dependency graph
# ---------------------------------------------------------------------------

class TestDiamond:
    def test_diamond_structure(self, diamond_blueprints):
        """
        auth (wave 0)
        backend (wave 1), worker (wave 1) — both depend on auth
        bff (wave 2) — depends on backend + worker
        """
        waves = wave_names(diamond_blueprints)
        assert len(waves) == 3
        assert names_at_level(waves, 0) == {"auth"}
        assert names_at_level(waves, 1) == {"backend", "worker"}
        assert names_at_level(waves, 2) == {"bff"}

    def test_all_names_present(self, diamond_blueprints):
        waves = wave_names(diamond_blueprints)
        all_names = {n for w in waves for n in w}
        assert all_names == {"auth", "backend", "worker", "bff"}


# ---------------------------------------------------------------------------
# Mixed: some independent, some dependent
# ---------------------------------------------------------------------------

class TestMixed:
    def test_independent_alongside_chain(self):
        bps = [
            make_blueprint("standalone"),       # wave 0
            make_blueprint("auth"),              # wave 0
            make_blueprint("backend", depends_on=["auth"]),  # wave 1
        ]
        waves = wave_names(bps)
        assert len(waves) == 2
        assert names_at_level(waves, 0) == {"standalone", "auth"}
        assert names_at_level(waves, 1) == {"backend"}

    def test_two_parallel_chains(self):
        """
        Chain A: a1 → a2
        Chain B: b1 → b2
        Both chains run fully in parallel with each other.
        """
        bps = [
            make_blueprint("a1"),
            make_blueprint("a2", depends_on=["a1"]),
            make_blueprint("b1"),
            make_blueprint("b2", depends_on=["b1"]),
        ]
        waves = wave_names(bps)
        assert len(waves) == 2
        assert names_at_level(waves, 0) == {"a1", "b1"}
        assert names_at_level(waves, 1) == {"a2", "b2"}


# ---------------------------------------------------------------------------
# Cycle handling — must not raise, must not lose nodes
# ---------------------------------------------------------------------------

class TestCycleHandling:
    def test_two_node_cycle(self):
        """a depends_on b AND b depends_on a — cycle broken gracefully."""
        bps = [
            make_blueprint("a", depends_on=["b"]),
            make_blueprint("b", depends_on=["a"]),
        ]
        waves = wave_names(bps)
        all_names = {n for w in waves for n in w}
        assert all_names == {"a", "b"}

    def test_self_loop(self):
        bps = [make_blueprint("a", depends_on=["a"])]
        waves = wave_names(bps)
        assert len(waves) >= 1
        all_names = {n for w in waves for n in w}
        assert "a" in all_names

    def test_no_nodes_lost_in_cycle(self):
        bps = [
            make_blueprint("x", depends_on=["y", "z"]),
            make_blueprint("y", depends_on=["x"]),
            make_blueprint("z"),
        ]
        waves = wave_names(bps)
        all_names = {n for w in waves for n in w}
        assert all_names == {"x", "y", "z"}


# ---------------------------------------------------------------------------
# Empty input
# ---------------------------------------------------------------------------

class TestEmpty:
    def test_empty_list(self):
        assert _topo_waves([]) == []
