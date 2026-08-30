import random

from desire import (BASELINES, DESIRE_THOUGHT_MAX, DesireState, Thought,
                    autonomous_thought_drive_delta, contextual_drive_delta, ranked_contextual_drive_delta, max_event_credit_gap, ease_drives, feed_thought, pick_intent,
                    pulse, satisfy, satisfy_to_baseline, tick_thoughts)
from desire_pulse import classify_rules
from desire_store import _row_with_decoded_meta


def test_desire_pulse_jsonb_meta_is_decoded_for_api_consumers():
    row = _row_with_decoded_meta({
        "event_type": "desire_wake_started",
        "meta": '{"wake_id":"wake-1","origin":"silence_wake"}',
    })
    assert row["meta"] == {"wake_id": "wake-1", "origin": "silence_wake"}


def test_invalid_desire_pulse_jsonb_meta_becomes_empty_object():
    assert _row_with_decoded_meta({"meta": "not-json"})["meta"] == {}


def test_ease_drives_converges_to_baseline():
    state = DesireState({k: 1.0 for k in BASELINES})
    for _ in range(100): state = ease_drives(state, 1800)
    assert all(abs(state.drives[k] - BASELINES[k]) < 0.001 for k in BASELINES)


def test_ease_drives_stays_in_bounds():
    for dt in (-999, 0, 1, 10**9):
        state = ease_drives(DesireState({k: random.uniform(-4, 4) for k in BASELINES}), dt)
        assert all(0 <= value <= 1 for value in state.drives.values())


def test_pulse_marginal_decreasing():
    state = DesireState(); gains = []
    for _ in range(4):
        before = state.drives["curiosity"]; state = pulse(state, ("curiosity", .2)); gains.append(state.drives["curiosity"] - before)
    assert gains == sorted(gains, reverse=True)


def test_contextual_delta_strengthens_in_the_correct_direction():
    assert contextual_drive_delta("libido", "reciprocated", 1) > contextual_drive_delta("libido", "reciprocated", 0)
    assert contextual_drive_delta("libido", "satisfied", 1) < contextual_drive_delta("libido", "satisfied", 0)
    assert contextual_drive_delta("stress", "relieved", 1) < contextual_drive_delta("stress", "relieved", 0)
    assert contextual_drive_delta("unknown", "state", 1) is None


def test_ranked_contextual_delta_is_fixed_by_code_not_model_score():
    assert ranked_contextual_drive_delta("libido", "reciprocated", "primary") == .15
    assert ranked_contextual_drive_delta("libido", "reciprocated", "secondary") == .09
    assert ranked_contextual_drive_delta("attachment", "seeking", "secondary") == .05
    assert ranked_contextual_drive_delta("libido", "satisfied", "primary") == -.32
    assert ranked_contextual_drive_delta("libido", "satisfied", "secondary") == -.18
    assert ranked_contextual_drive_delta("libido", "unresolved_intimate", "primary") == .05


def test_same_event_libido_uses_max_credit_instead_of_sum():
    contextual_wins = max_event_credit_gap(.506, .15, .065)
    assert contextual_wins["target_credit"] == contextual_wins["contextual_credit"]
    assert 0 < contextual_wins["gap"] < contextual_wins["contextual_credit"]
    body_wins = max_event_credit_gap(.506, .15, .12)
    assert body_wins["target_credit"] == .12
    assert body_wins["gap"] == 0


def test_grounded_autonomous_thought_gets_small_bounded_drive_delta():
    low = autonomous_thought_drive_delta(.35, .75)
    high = autonomous_thought_drive_delta(.55, 1)
    assert .03 <= low < high <= .06
    assert autonomous_thought_drive_delta(0, 1) == 0
    assert autonomous_thought_drive_delta(.5, 0) == 0


def test_attachment_is_hard_capped_at_point_nine():
    state = pulse(DesireState({**BASELINES, "attachment": .89}), ("attachment", .5))
    assert state.drives["attachment"] == .9
    assert pick_intent({"drives": {**BASELINES, "attachment": 9}}).score == .9
    with_fixation = DesireState({**BASELINES, "attachment": .9}, [Thought("opaque", "attachment", "fixation", 1)])
    assert pick_intent(with_fixation).score == .9


def test_v_ignored_replaces_v_reply_attachment_rule():
    assert classify_rules("v_reply") == []
    assert classify_rules("v_ignored") == [("attachment", .05)]


def test_user_messages_are_not_mechanically_treated_as_attachment_satisfaction():
    assert classify_rules("user_message", "宝宝，抱抱") == []


def test_free_text_keywords_do_not_bypass_contextual_scout():
    assert classify_rules("user_message", "我累了，想查资料，也想你") == []


def test_named_social_worlds_create_one_bounded_social_pulse():
    for text in ("去看看 AISAY 吧", "AISAY最近怎样", "花园最近怎么样", "KAI有回信吗", "kai 有回信吗"):
        assert classify_rules("user_message", text) == [("social", .06)]
    assert classify_rules("user_message", "AISAY 花园 KAI 都提到了") == [("social", .06)]
    assert classify_rules("user_message", "今天去公园") == []


def test_harper_external_news_leadin_creates_one_curiosity_pulse():
    for text in ("你知不知道……", "你知不知道我刚看到什么", "你知道吗，外面出了件事", "你晓不晓得这个消息"):
        assert classify_rules("user_message", text) == [("curiosity", .06)]
    assert classify_rules("user_message", "你知不知道，你知道吗") == [("curiosity", .06)]
    assert classify_rules("user_message", "我知道这个消息") == []


def test_memory_recall_does_not_change_reflection():
    assert classify_rules("recall_hit") == []


def test_satisfy_reduces_target_drive():
    state = DesireState({**BASELINES, "curiosity": .9, "stress": .4})
    after = satisfy(state, "voice_curiosity")
    assert after.drives["curiosity"] < .6 and after.drives["stress"] == .4


def test_diary_style_satisfaction_reduces_reflection_but_not_below_baseline():
    high = satisfy_to_baseline(
        DesireState({**BASELINES, "reflection": .9}), "voice_reflection", "reflection",
    )
    low = satisfy_to_baseline(
        DesireState({**BASELINES, "reflection": .45}), "voice_reflection", "reflection",
    )
    assert high.drives["reflection"] == .9 * .55
    assert low.drives["reflection"] == BASELINES["reflection"]


def test_diary_generation_is_not_a_positive_reflection_stimulus():
    assert classify_rules("diary_generated") == []


def test_sexual_release_satisfies_libido_without_silently_lowering_attachment():
    state = DesireState({**BASELINES, "libido": .8, "attachment": .7})
    after = satisfy(state, "voice_libido")
    assert after.drives["libido"] == .8 * .55
    assert after.drives["attachment"] == .7


def test_pick_intent_returns_max_drive():
    assert pick_intent(DesireState({**BASELINES, "social": .91})).drive_key == "social"


def test_pick_intent_ignores_fatigue_in_ranking():
    intent = pick_intent(DesireState({**BASELINES, "fatigue": .71, "duty": .69}))
    assert intent.drive_key == "duty"


def test_fatigue_gate_returns_rest():
    assert pick_intent(DesireState({**BASELINES, "fatigue": .72})).want_action == "rest"


def test_thoughts_flit_decays_and_gets_dropped():
    state = tick_thoughts(DesireState(thoughts=[Thought("x", "social", "flit", .06)]))
    assert not state.thoughts


def test_thoughts_fixation_grows_and_feeds_drive():
    state = DesireState(thoughts=[Thought("x", "reflection", "fixation", .8)])
    after = tick_thoughts(state)
    assert after.drives["reflection"] > state.drives["reflection"] and after.thoughts[0].fed_count == 1


def test_thoughts_fixation_resolves_after_n_feeds():
    state = DesireState(thoughts=[Thought("x", "reflection", "fixation", .9, fed_count=2)])
    assert not tick_thoughts(state).thoughts


def test_thoughts_max_capacity_enforced():
    state = DesireState()
    for i in range(DESIRE_THOUGHT_MAX + 20): state = feed_thought(state, str(i), "curiosity", strength=.5, born_at="t")
    assert len(state.thoughts) == DESIRE_THOUGHT_MAX
