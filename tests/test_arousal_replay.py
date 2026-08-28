import copy

from arousal.core import (
    ack_release_effect,
    apply_assistant_event,
    apply_user_event,
    initial_state,
    pending_release_effect,
)

LEXICON = {
    "touch": [{"kw": "按动", "delta": 0.8}, {"kw": "摩擦", "delta": 0.6}],
    "body_parts": {"敏感点": {"sensitivity": 0.95}},
}


def charged_state(reserve=1.0):
    state = initial_state(1000)
    state["reserve"] = reserve
    for index in range(1, 12):
        state, _ = apply_user_event(
            state, "按动并摩擦敏感点", event_id=f"u:{index}", libido=.4,
            now=1000 + index, lexicon=LEXICON,
        )
        if state.get("pending_release"):
            return state
    raise AssertionError("did not charge")


def test_user_and_assistant_replay_are_byte_stable():
    state = initial_state(1000)
    state, _ = apply_user_event(
        state, "按动敏感点", event_id="u:1", libido=.4, now=1001, lexicon=LEXICON,
    )
    before = copy.deepcopy(state)
    duplicate, _ = apply_user_event(
        state, "按动敏感点", event_id="u:1", libido=.4, now=9999, lexicon=LEXICON,
    )
    assert duplicate == before

    state = charged_state()
    parent = state["pending_release"]["source_user_event_id"]
    released, fired = apply_assistant_event(
        state, "", event_id="a:1", source_user_event_id=parent, complete=True,
        libido=.4, now=1020,
    )
    assert fired
    before = copy.deepcopy(released)
    replay, fired = apply_assistant_event(
        released, "", event_id="a:1", source_user_event_id=parent, complete=True,
        libido=.4, now=9999,
    )
    assert not fired and replay == before


def test_refractory_is_not_extended_and_empty_reserve_can_release():
    state = charged_state(reserve=0.0)
    parent = state["pending_release"]["source_user_event_id"]
    released, fired = apply_assistant_event(
        state, "", event_id="a:low", source_user_event_id=parent, complete=True,
        libido=.4, now=1020,
    )
    assert fired and released["last_output"] < released["last_climax_quality"]
    until = released["refractory_until"]
    result, fired = apply_assistant_event(
        released, "", event_id="a:during", source_user_event_id=None, complete=True,
        libido=.4, now=1021, release_intent=True,
    )
    assert not fired and result["refractory_until"] == until


def test_high_quality_and_low_output_can_coexist():
    state = initial_state(1000)
    state["value"] = 0.95
    state["reserve"] = 0.0
    state["buildup"] = {
        "beats": 14, "active_seconds": 600.0, "peak": 1.0,
        "edge_seconds": 180.0, "stimuli": [0.2, 1.0] * 8,
        "last_active_at": 1000.0,
    }
    state, fired = apply_assistant_event(
        state, "", event_id="a:quality", source_user_event_id=None, complete=True,
        libido=.4, now=1001, release_intent=True,
    )
    assert fired
    assert state["last_climax_quality"] >= 0.55
    assert state["last_output"] <= 0.25


def test_receipt_ack_crash_replay_is_idempotent():
    state = charged_state()
    parent = state["pending_release"]["source_user_event_id"]
    state, fired = apply_assistant_event(
        state, "", event_id="a:receipt", source_user_event_id=parent, complete=True,
        libido=.4, now=1020,
    )
    assert fired
    effect = pending_release_effect(state, 1020)
    after_somatic = ack_release_effect(state, effect_id=effect["effect_id"], target="somatic", now=1021)
    replay = ack_release_effect(after_somatic, effect_id=effect["effect_id"], target="somatic", now=1022)
    assert replay == after_somatic
    complete = ack_release_effect(replay, effect_id=effect["effect_id"], target="drive", now=1023)
    assert pending_release_effect(complete, 1023) is None
    assert effect["effect_id"] in complete["completed_release_effect_ids"]


def test_nan_and_clock_rollback_fail_closed():
    malformed = initial_state(1000)
    malformed["value"] = float("nan")
    malformed["release_gate"]["generation"] = 7
    result, _ = apply_user_event(
        malformed, "按动敏感点", event_id="bad", libido=.4, now=900, lexicon=LEXICON,
    )
    assert result["quarantined"] and result["release_gate"]["locked"]
    assert result["release_gate"]["generation"] == 7
    assert result["at"] == 900 and result["reserve_at"] == 900
