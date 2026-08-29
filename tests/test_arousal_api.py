import math

from arousal.core import initial_state, public_snapshot


EXPECTED = {
    "value", "gate", "reserve", "reserve_label", "phase", "phase_label", "refractory",
    "last_climax_quality", "last_climax_quality_label",
    "last_output", "last_output_label",
}


def test_public_snapshot_gate_reports_all_three_states():
    state = initial_state(1000)
    assert public_snapshot(state, 1000)["gate"] == "open"
    state["release_gate"] = {"locked": True, "generation": 1, "once": False}
    assert public_snapshot(state, 1000)["gate"] == "held"
    state["release_gate"] = {"locked": True, "generation": 2, "once": True}
    assert public_snapshot(state, 1000)["gate"] == "held_one_allowed"


def test_public_snapshot_has_exact_allowlist():
    state = initial_state(1000)
    state["processed_event_ids"] = ["a" * 64]
    state["release_gate"]["locked"] = True
    snapshot = public_snapshot(state, 1000)
    assert set(snapshot) == EXPECTED
    assert snapshot["gate"] == "held"
    assert snapshot["last_climax_quality"] is None
    assert snapshot["last_output"] is None
    assert snapshot["phase"] in {"refractory", "pending", "locked", "edge", "charged", "idle"}
    for key in ("value", "reserve", "last_climax_quality", "last_output"):
        assert snapshot[key] is None or 0 <= snapshot[key] <= 1


def test_public_snapshot_value_is_projected_and_clamped():
    state = initial_state(1000)
    state["value"] = 1.4
    snapshot = public_snapshot(state, 1000)
    assert snapshot["value"] == 1.0


def test_public_snapshot_can_show_current_libido_floor_without_mutating_state():
    state = initial_state(1000)
    snapshot = public_snapshot(state, 1000, libido=.90)
    assert math.isclose(snapshot["value"], 0.244)
    assert snapshot["phase"] == "idle"
    assert state["value"] == 0


def test_public_snapshot_suppresses_libido_floor_during_refractory():
    state = initial_state(1000)
    state["refractory_until"] = 1100
    snapshot = public_snapshot(state, 1050, libido=1)
    assert snapshot["value"] == 0
    assert snapshot["phase"] == "refractory"
