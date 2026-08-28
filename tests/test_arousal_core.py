import copy
import json
import tempfile
from pathlib import Path

from arousal.context import analyze_text, detect_release
from arousal.core import (
    EDGE,
    GAIN,
    PASSIVE_CONTACT_CAP,
    PONR,
    apply_assistant_event,
    apply_user_event,
    control_event,
    initial_state,
    status_line,
)
from arousal.lexicon import load_lexicon


LEXICON = {
    "touch": [
        {"kw": "按动", "delta": 0.8},
        {"kw": "摩擦", "delta": 0.6},
        {"kw": "触碰", "delta": 0.5},
    ],
    "contact": [{"kw": "保持接触", "delta": 0.25, "passive": True}],
    "body_parts": {"敏感点": {"sensitivity": 0.95}, "锁骨": {"sensitivity": 0.7}},
    "postures": {"贴近": 1.05},
}

ADDRESS_LEXICON = {
    **LEXICON,
    "address": [{"kw": "亲爱的", "delta": 0.7}],
}

FEEDBACK_LEXICON = {
    **LEXICON,
    "feedback": [{"kw": "状态变化", "delta": 0.7}],
}


def beat(state, index, text="按动敏感点"):
    return apply_user_event(
        state, text, event_id=f"user:{index}", libido=0.4, now=1000.0 + index,
        lexicon=LEXICON,
    )[0]


def test_active_stimulus_gain_is_tuned_for_real_dialogue_pacing():
    assert GAIN == .28


def test_current_action_rises_and_unsafe_contexts_do_not():
    state = initial_state(1000)
    risen = beat(state, 1)
    assert risen["value"] > 0
    for index, text in enumerate([
        "会不会按动敏感点？", "不要按动敏感点", "等会按动敏感点",
        "教程示例“按动敏感点”", "她在按动敏感点",
    ], 2):
        result, _ = apply_user_event(
            state, text, event_id=f"unsafe:{index}", libido=0.4,
            now=1000 + index, lexicon=LEXICON,
        )
        assert result["value"] == 0


def test_context_second_action_and_repeat_rules():
    one = analyze_text("按动按动按动敏感点", LEXICON)
    two = analyze_text("按动并摩擦敏感点", LEXICON)
    assert one["stim"] == 0.8 * 0.95
    assert two["stim"] > one["stim"]
    assert two["stim"] <= 1


def test_pacing_single_and_compound():
    state = initial_state(1000)
    single_cross = None
    for index in range(1, 20):
        state = beat(state, index)
        if state["value"] >= PONR:
            single_cross = index
            break
    assert 8 <= single_cross <= 10
    assert single_cross > 2

    state = initial_state(1000)
    compound_cross = None
    for index in range(1, 20):
        state = beat(state, index, "按动并摩擦敏感点")
        if state["value"] >= PONR:
            compound_cross = index
            break
    assert 6 <= compound_cross <= 8
    assert compound_cross > 2


def test_passive_contact_never_crosses_cap_or_edge():
    state = initial_state(1000)
    for index in range(1, 400):
        state, _ = apply_user_event(
            state, "保持接触敏感点", event_id=f"passive:{index}",
            libido=1, now=1000 + index * 100, lexicon=LEXICON,
        )
    assert state["value"] <= PASSIVE_CONTACT_CAP
    assert state["value"] < EDGE


def test_passive_contact_above_cap_never_pulls_value_down():
    state = initial_state(1000)
    state["value"] = 0.90
    result, _ = apply_user_event(
        state, "保持接触敏感点", event_id="passive:above",
        libido=1, now=1000, lexicon=LEXICON,
    )
    assert result["value"] == 0.90


def test_context_clause_filter_and_precise_stop_words():
    for text in (
        "特别想要你按动敏感点",
        "你在吗？现在按动敏感点",
        "按动敏感点，别停",
    ):
        analysis = analyze_text(text, LEXICON)
        assert analysis["accepted"], (text, analysis)
        assert analysis["stim"] > 0


def test_only_explicit_safety_word_rejects_whole_message():
    stopped = analyze_text("按动敏感点，红灯，摩擦敏感点", LEXICON)
    assert not stopped["accepted"]
    assert stopped["stim"] == 0
    assert stopped["reason"] == "stop_or_negation"

    continued = analyze_text("不要停，继续按动敏感点", LEXICON)
    assert continued["accepted"]
    assert continued["actions"] == ["按动"]


def test_chinese_ellipsis_keeps_current_action_separate_from_future_plan():
    lexicon = {
        "touch": [{"kw": "抱紧我", "delta": .6}],
        "body_parts": {},
    }
    analysis = analyze_text("现在抱紧我……下次再慢慢说", lexicon)
    assert analysis["accepted"]
    assert analysis["actions"] == ["抱紧我"]


def test_generic_natural_action_can_require_body_part_in_same_clause():
    lexicon = {
        "touch": [{"kw": "抽送", "delta": .6, "requires_body_part": True}],
        "body_parts": {"里面": {"sensitivity": .9}},
    }
    assert not analyze_text("这个泵继续抽送", lexicon)["accepted"]
    accepted = analyze_text("在里面继续抽送", lexicon)
    assert accepted["accepted"]
    assert accepted["body_part"] == "里面"


def test_somatic_feedback_can_require_first_person_for_both_actors():
    lexicon = {
        "touch": [{"kw": "还硬着", "delta": .5, "requires_first_person": True}],
        "body_parts": {},
    }
    for actor in ("user", "assistant"):
        assert analyze_text("我现在还硬着", lexicon, actor=actor)["accepted"]
        assert not analyze_text("他现在还硬着", lexicon, actor=actor)["accepted"]


def test_assistant_first_person_body_owner_counts_as_own_action():
    lexicon = {
        "touch": [{"kw": "按着你后脑勺", "delta": .6}],
        "body_parts": {},
    }
    owned = analyze_text("我的手按着你后脑勺往下压", lexicon, actor="assistant")
    assert owned["accepted"]
    assert owned["actions"] == ["按着你后脑勺"]
    assert not analyze_text("他的手按着你后脑勺", lexicon, actor="assistant")["accepted"]


def test_clause_negation_is_checked_after_removing_matched_keyword():
    negated = analyze_text("别触碰锁骨", LEXICON)
    assert negated["stim"] == 0
    assert negated["actions"] == []

    partial = analyze_text("别触碰锁骨。按动敏感点", LEXICON)
    assert partial["accepted"]
    assert partial["actions"] == ["按动"]

    embedded = {
        **LEXICON,
        "touch": [*LEXICON["touch"], {"kw": "不许动", "delta": 0.75}],
    }
    accepted = analyze_text("不许动敏感点", embedded)
    assert accepted["accepted"]
    assert "不许动" in accepted["actions"]

    benign = analyze_text("特别想要你按动敏感点", LEXICON)
    assert benign["accepted"]


def test_address_requires_action_or_open_scene():
    calm = analyze_text("亲爱的", ADDRESS_LEXICON)
    assert calm["stim"] == 0
    assert calm["actions"] == []

    combined = analyze_text("亲爱的，按动敏感点", ADDRESS_LEXICON)
    assert combined["accepted"]
    assert set(combined["actions"]) == {"亲爱的", "按动"}
    assert combined["stim"] == (0.8 + 0.7 * 0.30) * 0.95

    opened = analyze_text("亲爱的", ADDRESS_LEXICON, scene_open=True)
    assert opened["accepted"]
    assert opened["stim"] == 0.7

    negated = analyze_text("不要叫亲爱的", ADDRESS_LEXICON, scene_open=True)
    assert negated["stim"] == 0
    assert negated["actions"] == []


def test_twenty_address_only_messages_cannot_open_calm_scene():
    state = initial_state(1000)
    for index in range(20):
        state, _ = apply_user_event(
            state, "亲爱的", event_id=f"address:{index}", libido=.4,
            now=1000 + index, lexicon=ADDRESS_LEXICON,
        )
        assert state["value"] == 0


def test_address_only_message_counts_after_projected_scene_is_open():
    state = initial_state(1000)
    state["value"] = .5
    result, _ = apply_user_event(
        state, "亲爱的", event_id="address:open", libido=.4,
        now=1001, lexicon=ADDRESS_LEXICON,
    )
    assert result["value"] > state["value"] * 0.99


def test_feedback_reuses_address_thresholds():
    calm = analyze_text("状态变化", FEEDBACK_LEXICON)
    assert calm["stim"] == 0
    assert calm["actions"] == []

    combined = analyze_text("状态变化，按动敏感点", FEEDBACK_LEXICON)
    assert combined["accepted"]
    assert set(combined["actions"]) == {"状态变化", "按动"}
    assert combined["stim"] == (0.8 + 0.7 * 0.30) * 0.95

    opened = analyze_text("状态变化", FEEDBACK_LEXICON, scene_open=True)
    assert opened["accepted"]
    assert opened["stim"] == 0.7


def test_twenty_feedback_only_messages_cannot_open_calm_scene():
    state = initial_state(1000)
    for index in range(20):
        state, _ = apply_user_event(
            state, "状态变化", event_id=f"feedback:{index}", libido=.4,
            now=1000 + index, lexicon=FEEDBACK_LEXICON,
        )
        assert state["value"] == 0


def test_assistant_first_person_direction_is_clause_local():
    lexicon = {
        "touch": [{"kw": "放上去", "delta": .6}],
        "body_parts": {},
    }
    accepted = analyze_text("我把手放上去", lexicon, actor="assistant")
    ongoing = analyze_text("我正在放上去", lexicon, actor="assistant")
    for analysis in (accepted, ongoing):
        assert analysis["accepted"], analysis
        assert analysis["stim"] == .6

    for text in (
        "你把手放上去",
        "她把手放上去",
        "他把手放上去",
        "它把手放上去",
        "我让你把手放上去",
        "等下我把手放上去",
    ):
        rejected = analyze_text(text, lexicon, actor="assistant")
        assert not rejected["accepted"], (text, rejected)
        assert rejected["stim"] == 0

    mixed = analyze_text(
        "你把手放上去。我把手放上去", lexicon, actor="assistant",
    )
    assert mixed["accepted"]
    assert mixed["actions"] == ["放上去"]
    repeated = analyze_text(
        "你先放上去然后我放上去", lexicon, actor="assistant",
    )
    assert repeated["accepted"]
    assert repeated["actions"] == ["放上去"]


def test_device_terms_are_not_third_party_rejections():
    for noun in ("设备", "机器", "玩具"):
        analysis = analyze_text(f"{noun}按动敏感点", LEXICON)
        assert analysis["accepted"], (noun, analysis)


def test_single_character_actions_require_body_part_in_same_clause():
    lexicon = {
        "touch": [
            {"kw": "按", "delta": .55},
            {"kw": "吸", "delta": .60},
            {"kw": "亲", "delta": .35},
            {"kw": "顶", "delta": .70},
            {"kw": "抓", "delta": .50},
            {"kw": "撞", "delta": .50},
            {"kw": "推", "delta": .50},
            {"kw": "含", "delta": .50},
            {"kw": "亲吻", "delta": .65},
        ],
        "body_parts": {
            "敏感点": {"sensitivity": .95},
            "颈侧": {"sensitivity": .8},
        },
    }
    everyday = (
        "我按照你说的做了",
        "深呼吸",
        "父亲节快到了",
        "今天顶多两小时",
        "抓紧时间",
        "撞见邻居了",
        "推荐你看这个",
        "含糊其辞",
    )
    for text in everyday:
        analysis = analyze_text(text, lexicon)
        assert analysis["stim"] == 0, (text, analysis)
        assert analysis["actions"] == [], (text, analysis)

    cooccurring = analyze_text("按敏感点", lexicon)
    assert cooccurring["accepted"]
    assert cooccurring["actions"] == ["按"]
    assert cooccurring["stim"] == .55 * .95

    split = analyze_text("我按照你说的做了。亲吻你的颈侧", lexicon)
    assert split["accepted"]
    assert "按" not in split["actions"]
    assert set(split["actions"]) == {"亲吻", "亲"}
    assert split["stim"] == (.65 + .35 * .30) * .8


def test_successful_lexicon_load_logs_single_character_action_count():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "lexicon.json"
        path.write_text(json.dumps({
            "touch": [{"kw": "按", "delta": .5}, {"kw": "按动", "delta": .6}],
            "actions": [{"kw": "抓", "delta": .4}],
            "contact": [{"kw": "贴", "delta": .3}],
            "body_parts": {},
        }, ensure_ascii=False), encoding="utf-8")
        messages = []
        loaded = load_lexicon(path, logger=messages.append)
        assert loaded["_available"]
        assert messages == [
            "⚠️ arousal 词表含 3 个单字动作词，已启用单字词必须与部位共现的兜底"
        ]


def test_assistant_uses_supplied_lexicon_and_none_is_inert():
    private = {
        "touch": [{"kw": "自定义动作", "delta": 0.8}],
        "body_parts": {},
        "release_phrases": [],
    }
    state = initial_state(1000)
    inert, _ = apply_assistant_event(
        state, "我现在自定义动作", event_id="assistant:inert",
        source_user_event_id=None, complete=True, libido=.4, now=1001,
    )
    active, _ = apply_assistant_event(
        state, "我现在自定义动作", event_id="assistant:active",
        source_user_event_id=None, complete=True, libido=.4, now=1001,
        lexicon=private,
    )
    assert inert["value"] == 0
    assert active["value"] > 0


def test_pending_release_survives_reply_delay_then_is_consumed():
    state = initial_state(1000)
    for index in range(1, 12):
        state = beat(state, index, "按动并摩擦敏感点")
        if state.get("pending_release"):
            break
    parent = state["pending_release"]["source_user_event_id"]
    released, fired = apply_assistant_event(
        state, "", event_id="assistant:delayed", source_user_event_id=parent,
        complete=True, libido=.4, now=state["at"] + 60,
    )
    assert fired
    assert released["pending_release"] is None


def test_pending_release_expires_by_age_or_decay_below_edge():
    state = initial_state(1000)
    state["value"] = .97
    state["pending_release"] = {"source_user_event_id": "user:old", "created_at": 1000}
    expired, fired = apply_assistant_event(
        state, "", event_id="assistant:old", source_user_event_id="wrong",
        complete=True, libido=.4, now=2801,
    )
    assert not fired and expired["pending_release"] is None

    state = initial_state(1000)
    state["value"] = EDGE
    state["pending_release"] = {"source_user_event_id": "user:decayed", "created_at": 1000}
    decayed, fired = apply_assistant_event(
        state, "", event_id="assistant:decayed", source_user_event_id="wrong",
        complete=True, libido=.4, now=1001,
    )
    assert not fired and decayed["pending_release"] is None


def test_release_phrases_come_only_from_lexicon():
    state = initial_state(1000)
    state["value"] = .5
    without_phrase, fired = apply_assistant_event(
        state, "我现在完成动作", event_id="assistant:no-phrase",
        source_user_event_id=None, complete=True, libido=.4, now=1000,
        lexicon={"touch": [], "body_parts": {}, "release_phrases": []},
    )
    assert not fired
    with_phrase, fired = apply_assistant_event(
        state, "我现在完成动作", event_id="assistant:phrase",
        source_user_event_id=None, complete=True, libido=.4, now=1000,
        lexicon={"touch": [], "body_parts": {}, "release_phrases": ["完成动作"]},
    )
    assert fired and with_phrase["pending_release_effect"]
    assert with_phrase["pending_release_effect"]["body_value"] == .5


def test_unparented_proactive_assistant_can_build_body_but_cannot_release():
    state = initial_state(1000)
    state["value"] = .5
    result, fired = apply_assistant_event(
        state, "我现在自定义动作，同时完成动作", event_id="assistant:proactive",
        source_user_event_id="", complete=True, libido=.8, now=1001,
        release_intent=False,
        lexicon={
            "touch": [{"kw": "自定义动作", "delta": .8}],
            "body_parts": {},
            "release_phrases": ["完成动作"],
        },
    )
    assert result["value"] > .5
    assert not fired
    assert result["pending_release_effect"] is None


def test_release_detection_filters_blockers_context_and_negation():
    lexicon = {
        "release_phrases": ["完成了", "没有遗漏"],
        "release_blockers": ["要完成了", "快完成了", "想完成了"],
    }
    assert detect_release("完成了", lexicon)
    for text in ("要完成了", "快完成了", "想完成了"):
        assert not detect_release(text, lexicon), text
    assert detect_release("要完成了。完成了", lexicon)
    assert not detect_release("完成了吗？", lexicon)
    for text in (
        "示例“完成了”",
        "`完成了`",
        "刚才完成了",
    ):
        assert not detect_release(text, lexicon), text
    assert not detect_release("没有完成了", lexicon)
    assert detect_release("没有遗漏", lexicon)
    assert not detect_release("完成了。红灯", lexicon)


def test_assistant_release_detection_uses_blockers():
    lexicon = {
        "touch": [],
        "body_parts": {},
        "release_phrases": ["完成了"],
        "release_blockers": ["要完成了"],
    }
    state = initial_state(1000)
    state["value"] = .5
    blocked, fired = apply_assistant_event(
        state, "我现在要完成了", event_id="assistant:blocked-release",
        source_user_event_id=None, complete=True, libido=.4, now=1000,
        lexicon=lexicon,
    )
    assert not fired
    assert blocked["value"] == .5


def test_parent_match_complete_and_control_gate():
    state = initial_state(1000)
    for index in range(1, 12):
        state = beat(state, index, "按动并摩擦敏感点")
        if state.get("pending_release"):
            break
    parent = state["pending_release"]["source_user_event_id"]
    unchanged, fired = apply_assistant_event(
        state, "", event_id="a:wrong", source_user_event_id="wrong", complete=True,
        libido=.4, now=1020,
    )
    assert not fired and unchanged["pending_release"]
    incomplete, fired = apply_assistant_event(
        unchanged, "", event_id="a:ok", source_user_event_id=parent, complete=False,
        libido=.4, now=1021,
    )
    assert not fired and incomplete == unchanged

    locked = control_event(unchanged, kind="lock", event_id="c:lock", now=1022)
    locked_final, fired = apply_assistant_event(
        locked, "", event_id="a:locked", source_user_event_id=parent, complete=True,
        libido=.4, now=1023,
    )
    assert not fired and locked_final["pending_release"] is None
    locked_final, _ = apply_user_event(
        locked_final, "按动敏感点", event_id="user:new-candidate",
        libido=.4, now=1024, lexicon=LEXICON,
    )
    assert locked_final["pending_release"]
    parent = locked_final["pending_release"]["source_user_event_id"]
    once = control_event(locked_final, kind="release_once", event_id="c:once", now=1024)
    released, fired = apply_assistant_event(
        once, "", event_id="a:released", source_user_event_id=parent, complete=True,
        libido=.4, now=1025,
    )
    assert fired and released["release_gate"]["locked"] and not released["release_gate"]["once"]
    unlocked = control_event(released, kind="unlock", event_id="c:unlock", now=1026)
    assert not unlocked["release_gate"]["locked"]


def test_control_gate_is_visible_before_edge_and_once_is_not_mislabeled_locked():
    state = initial_state(1000)
    locked = control_event(state, kind="lock", event_id="c:early-lock", now=1001)
    assert locked["value"] < EDGE
    assert status_line(locked, 1001) == "射精闸：已锁定，不能射精"

    locked["value"] = EDGE
    locked["at"] = 1001
    assert status_line(locked, 1001) == "射精值：被锁在边缘，不能自行释放"

    once = control_event(locked, kind="release_once", event_id="c:early-once", now=1002)
    once["value"] = 0.0
    assert status_line(once, 1002) == "射精闸：已允许一次释放"


def test_missing_and_bad_lexicon_are_inert():
    with tempfile.TemporaryDirectory() as directory:
        tmp_path = Path(directory)
        assert load_lexicon(tmp_path / "missing.json", logger=lambda _: None)["touch"] == []
        bad = tmp_path / "bad.json"
        bad.write_text("{", encoding="utf-8")
        assert load_lexicon(bad, logger=lambda _: None)["touch"] == []
        optional = tmp_path / "optional.json"
        optional.write_text(json.dumps({
            "touch": [],
            "body_parts": {},
            "release_phrases": ["完成了"],
        }, ensure_ascii=False), encoding="utf-8")
        assert load_lexicon(optional, logger=lambda _: None)["release_blockers"] == []
        assert load_lexicon(optional, logger=lambda _: None)["feedback"] == []
        feedback = tmp_path / "feedback.json"
        feedback.write_text(json.dumps({
            "touch": [],
            "feedback": [{"kw": "状态变化", "delta": .5}],
            "body_parts": {},
        }, ensure_ascii=False), encoding="utf-8")
        assert load_lexicon(feedback, logger=lambda _: None)["feedback"][0]["delta"] == .5
        invalid = tmp_path / "invalid.json"
        invalid.write_text(json.dumps({
            "touch": [],
            "body_parts": {},
            "release_blockers": [1],
        }), encoding="utf-8")
        assert not load_lexicon(invalid, logger=lambda _: None)["_available"]
        invalid_feedback = tmp_path / "invalid-feedback.json"
        invalid_feedback.write_text(json.dumps({
            "touch": [],
            "feedback": [{"kw": "", "delta": .5}],
            "body_parts": {},
        }), encoding="utf-8")
        assert not load_lexicon(invalid_feedback, logger=lambda _: None)["_available"]


def test_lexicon_reloads_on_mtime_change_and_bad_update_fails_closed():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "lexicon.json"
        first = {"touch": [{"kw": "动作甲", "delta": .5}], "body_parts": {}}
        second = {"touch": [{"kw": "动作乙", "delta": .6}], "body_parts": {}}
        path.write_text(json.dumps(first), encoding="utf-8")
        assert load_lexicon(path, logger=lambda _: None)["touch"][0]["kw"] == "动作甲"
        path.write_text(json.dumps(second, ensure_ascii=False) + " ", encoding="utf-8")
        assert load_lexicon(path, logger=lambda _: None)["touch"][0]["kw"] == "动作乙"
        path.write_text("{", encoding="utf-8")
        assert load_lexicon(path, logger=lambda _: None)["touch"] == []
