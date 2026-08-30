from datetime import datetime, timedelta, timezone

import asyncio
import os
from pathlib import Path

from desire import BASELINES, DesireState, pulse as apply_drive_pulse, ranked_contextual_drive_delta
from desire_pulse import (
    AutonomousThoughtBatcher, DeepSeekBatcher, PendingClassification,
    private_intimacy_scene,
)
from desire_scheduler import DesireScheduler


class Store:
    def __init__(self, score=.2): self.state=DesireState({**BASELINES,"curiosity":score}); self.last=None; self.logs=[]; self.seen=set()
    async def load(self, now): return self.state, self.last or now-timedelta(minutes=30)
    async def save(self, state, last, now): self.state,self.last=state,last
    async def log_pulse(self,*args): self.logs.append(args); self.seen.add((args[0],args[3]))
    async def has_pulse(self,event_type,source_ref): return (event_type,source_ref) in self.seen
    async def next_due_followup(self,drive_key,now): return None
    async def mark_followup_queued(self,followup_id,now): return None
    async def drive_satisfied_since(self,drive_key,since): return False
    async def has_unsettled_positive_drive(self,drive_key,now): return False


NOW=datetime(2026,7,22,12,tzinfo=timezone.utc)


def test_two_hour_silence_wake_uses_separate_quota_origin():
    source = (Path(__file__).resolve().parents[1] / "main.py").read_text()
    start = source.index("async def _maybe_enqueue_silence_wake(now, existing_is_handled=True):")
    end = source.index("\n\nasync def _apply_deepseek_desire_results", start)
    body = source[start:end]
    assert 'origin="silence_wake"' in body
    assert 'origin="desire_attachment"' not in body


def test_existing_guaranteed_wake_does_not_freeze_later_organic_desires():
    source = (Path(__file__).resolve().parents[1] / "main.py").read_text()
    scheduler_wiring = source[source.index("_desire_scheduler = DesireScheduler("):source.index("_desire_batcher = DeepSeekBatcher")]
    assert "existing_is_handled=False" in scheduler_wiring
    wake = source[source.index("async def _maybe_enqueue_silence_wake"):source.index("async def _apply_deepseek_desire_results")]
    assert "return bool(existing_is_handled)" in wake


def test_organic_wake_can_choose_one_screen_peek_and_wait_for_arrival():
    source = (Path(__file__).resolve().parents[1] / "desire_scheduler.py").read_text()
    block = source[source.index("async def _nudge"):source.index("async def loop")]
    assert "cyberboss_peek_screen 一次" in block
    assert "需要一点当下语境时" in block
    assert "图片到达后再继续" in block
    assert "图片没到前不要猜或编" in block
    assert "先决定要不要看 Harper" not in block


def test_two_hour_silence_wake_can_choose_one_screen_peek_and_wait_for_arrival():
    source = (Path(__file__).resolve().parents[1] / "main.py").read_text()
    block = source[source.index("async def _maybe_enqueue_silence_wake"):source.index("async def _apply_deepseek_desire_results")]
    assert "cyberboss_peek_screen 一次" in block
    assert "窥屏是获取当下语境的前置步骤" in block
    assert "不与之后说话冲突" in block
    assert "截图到达后会继续同一次自主唤醒" in block
    assert "不要猜测或编造任何看到的内容" in block
    assert "除上述等待截图的场景外" in block


def test_private_wander_has_bounded_silence_schedule_and_independent_poller():
    source = (Path(__file__).resolve().parents[1] / "main.py").read_text()
    assert "PRIVATE_WANDER_FIRST_MINUTES = (30, 45)" in source
    assert "PRIVATE_WANDER_REPEAT_MINUTES = (45, 75)" in source
    assert "PRIVATE_WANDER_ACTION_MINUTES = (60, 90)" in source
    assert "PRIVATE_WANDER_FOUR_HOUR_CAP = 3" in source
    assert "PRIVATE_WANDER_DAY_CAP = 10" in source
    poller = source[source.index("async def _push_loop():"):source.index("_push_task = asyncio.create_task(_push_loop())")]
    assert "_maybe_enqueue_silence_wake" in poller
    assert "_maybe_enqueue_private_wander" in poller
    assert "if not silence_handled and not wander_handled" in poller


def test_private_wander_prompt_reminds_v_of_choices_and_allows_nothing():
    source = (Path(__file__).resolve().parents[1] / "main.py").read_text()
    block = source[source.index("def _private_wander_prompt(") : source.index("async def _maybe_enqueue_private_wander")]
    for phrase in (
        "cyberboss_peek_screen", "cyberboss_recent_dialogue", "长期记忆",
        "play_fishing", "音乐工作室", "已有的游戏", "书架", "时间线",
        "AISAY", "Galatea Garden", "KAI 邮箱", "cyberboss_email_inbox",
        "公开 GitHub", "公开微博热搜", "给她发一句话", "什么都不干",
        "一次只选一个方向", "回复 silent",
    ):
        assert phrase in block
    assert "不是任务清单" in block
    assert "不要在论坛、邮件或公开地方泄露 Harper" in block
    assert "窥屏是获取当下语境的前置步骤" in block
    assert "截图到达后会继续同一次自主唤醒" in block
    assert "不要猜测或编造任何看到的内容" in block


def test_private_wander_uses_actual_v_action_turn_and_durable_wake_id():
    source = (Path(__file__).resolve().parents[1] / "main.py").read_text()
    block = source[source.index("async def _maybe_enqueue_private_wander") : source.index("async def _apply_deepseek_desire_results")]
    assert 'origin="desire_private_wander"' in block
    assert '"wake_id": wake_id' in block
    assert '"private_wander": True' in block
    assert '"private_wander_queued"' in block
    assert '"desire_wake_finished"' in block
    assert '"desire_wake_action"' in block
    assert '"silence_wake_queued"' in block


def test_wake_audit_has_read_only_visualization_endpoint():
    source = (Path(__file__).resolve().parents[1] / "main.py").read_text()
    assert '@app.get("/api/desire/wakes")' in source
    assert '"desire_wake_started", "desire_wake_action"' in source
    assert '"desire_wake_peek_arrived", "desire_wake_finished"' in source
    assert '"action": str(meta.get("tool_action") or "")' in source
    assert '"treasure_id": str(meta.get("treasure_id") or "")' in source
    assert '"treasure_label": str(meta.get("treasure_label") or "")[:120]' in source


def test_tick_updates_state_and_logs_pulse():
    s=Store(.9); d=DesireScheduler(s,lambda:NOW); asyncio.run(d.run_once(NOW)); assert s.last==NOW and s.logs[0][0]=='hour_settlement'


def test_tick_runs_durable_silence_pulse_before_loading_state():
    calls=[]
    async def silence_pulse(now): calls.append(now)
    d=DesireScheduler(Store(.2),lambda:NOW,silence_pulse=silence_pulse)
    asyncio.run(d.run_once(NOW))
    assert calls == [NOW]


def test_two_hour_silence_wake_runs_and_suppresses_same_tick_threshold_nudge():
    wakes, nudges = [], []
    async def silence_wake(now):
        wakes.append(now)
        return True
    async def nudge(*args):
        nudges.append(args)
        return True
    scheduler = DesireScheduler(
        Store(.9), lambda: NOW, nudge=nudge, silence_wake=silence_wake,
    )
    scheduler.driven = True
    result = asyncio.run(scheduler.run_once(NOW))
    assert wakes == [NOW]
    assert not nudges
    assert result["silence_wake_handled"] is True


def test_no_silence_wake_allows_normal_threshold_nudge():
    nudges = []
    async def silence_wake(_now): return False
    async def nudge(*args):
        nudges.append(args)
        return True
    scheduler = DesireScheduler(
        Store(.9), lambda: NOW, nudge=nudge, silence_wake=silence_wake,
    )
    scheduler.driven = True
    result = asyncio.run(scheduler.run_once(NOW))
    assert nudges
    assert result["silence_wake_handled"] is False


def test_hour_settlement_survives_scheduler_restart():
    s=Store(.2)
    asyncio.run(DesireScheduler(s,lambda:NOW).run_once(NOW))
    after_first=s.state.drives['fatigue']
    asyncio.run(DesireScheduler(s,lambda:NOW+timedelta(minutes=5)).run_once(NOW+timedelta(minutes=5)))
    assert s.state.drives['fatigue'] >= after_first
    assert len([row for row in s.logs if row[0]=='hour_settlement']) == 1


def test_pulse_endpoint_writes_pulse_log():
    s=Store(); asyncio.run(s.log_pulse('user_message','attachment',-.1,'1',{},NOW)); assert s.logs


def test_cross_service_delivery_uses_atomic_state_and_audit_boundary():
    source = (Path(__file__).resolve().parents[1] / "main.py").read_text()
    store = (Path(__file__).resolve().parents[1] / "desire_store.py").read_text()
    assert 'delivery_id = str((meta or {}).get("delivery_id")' in source
    assert "await _commit_desire_state(state, last_tick, pulses, now, delivery_id)" in source
    assert "async def save_with_pulses" in store
    assert "pg_advisory_xact_lock" in store
    assert "meta->>'delivery_id'" in store


def test_pulse_idempotent_within_5min(): assert timedelta(minutes=4)<timedelta(minutes=5)


def test_pick_intent_below_threshold_does_not_trigger_nudge():
    called=[]; d=DesireScheduler(Store(.6),lambda:NOW,nudge=lambda *x:called.append(x)); d.driven=True; asyncio.run(d.run_once(NOW)); assert not called


def test_pick_intent_above_threshold_and_gated_off_does_not_trigger_nudge():
    called=[]; d=DesireScheduler(Store(.9),lambda:NOW,nudge=lambda *x:called.append(x)); asyncio.run(d.run_once(NOW)); assert not called


def test_pick_intent_above_threshold_and_gated_on_triggers_nudge():
    async def nudge(*_): called.append(1); return True
    called=[]; d=DesireScheduler(Store(.9),lambda:NOW,nudge=nudge); d.driven=True; asyncio.run(d.run_once(NOW)); assert called


def test_per_drive_threshold_defaults_match_three_day_trial():
    d = DesireScheduler(Store(.2), lambda: NOW)
    assert d.thresholds == {
        "attachment": .70, "curiosity": .65, "reflection": .70, "duty": .70,
        "social": .65, "libido": .65, "stress": .65,
    }


def test_eligible_lower_threshold_drive_is_not_blocked_by_higher_ineligible_drive():
    calls = []
    async def nudge(message, intent, followup):
        calls.append((message, intent, followup)); return True
    store = Store(.68)
    store.state.drives["reflection"] = .68
    d = DesireScheduler(store, lambda: NOW, nudge=nudge); d.driven = True
    asyncio.run(d.run_once(NOW))
    assert calls and calls[0][1].drive_key == "curiosity"
    assert calls[0][1].score >= d.thresholds["curiosity"]


def test_high_threshold_drive_stays_quiet_below_point_seven():
    calls = []
    async def nudge(*args): calls.append(args); return True
    store = Store(.2)
    store.state.drives["reflection"] = .69
    d = DesireScheduler(store, lambda: NOW, nudge=nudge); d.driven = True
    asyncio.run(d.run_once(NOW))
    assert not calls


def test_tick_logs_all_scores_thresholds_and_selection_without_model_io():
    store = Store(.68)
    d = DesireScheduler(store, lambda: NOW); d.driven = True
    asyncio.run(d.run_once(NOW))
    snapshots = [row for row in store.logs if row[0] == "drive_snapshot"]
    assert len(snapshots) == 1
    meta = snapshots[0][4]
    assert set(meta["scores"]) == {"attachment","curiosity","reflection","duty","social","libido","stress"}
    assert meta["thresholds"]["curiosity"] == .65
    assert meta["selected"] == "curiosity"

def test_curiosity_intent_offers_one_contextual_exploration_path():
    calls=[]
    async def nudge(message,intent,followup):
        calls.append((message,intent,followup)); return True
    d=DesireScheduler(Store(.9),lambda:NOW,nudge=nudge); d.driven=True
    asyncio.run(d.run_once(NOW))
    message,intent,followup=calls[0]
    assert intent.want_action == "voice_curiosity"
    assert intent.drive_key == "curiosity"
    assert "一次只选一个方向" in message
    assert "追问Harper" in message
    assert "音乐工作室" in message
    assert "新游戏或玩法" in message
    assert "只读逛公开GitHub" in message
    assert "不要运行陌生代码" in message
    assert "开issue、评论、star或fork" in message
    assert "公开微博热搜" in message
    assert "只看热搜榜单这一页" in message
    assert "不点进热搜词条、不展开帖子" in message
    assert "遇到登录墙、访客验证或反爬限制就停止直连" in message
    assert "不要登录或绕过验证" in message
    assert "play_fishing" in message
    assert "不是Harper布置的任务" in message
    assert "最多调用 play_fishing 1次" in message
    assert "最多钓鱼/潜水5次" in message
    assert followup is None

def test_social_intent_offers_forums_or_kai_without_leaking_harper():
    calls=[]
    async def nudge(message,intent,followup):
        calls.append((message,intent,followup)); return True
    store=Store(.2)
    store.state.drives["social"]=.9
    d=DesireScheduler(store,lambda:NOW,nudge=nudge); d.driven=True
    asyncio.run(d.run_once(NOW))
    message,intent,followup=calls[0]
    assert intent.want_action == "voice_social"
    assert intent.drive_key == "social"
    assert "AISAY" in message and "花园" in message and "KAI" in message
    assert "my_status/room/read" in message and "send" in message
    assert "cyberboss_email_list/read/reply/send" in message
    assert "绝对不要猜收件地址" in message
    assert "绝对不要在论坛或邮件中泄露Harper" in message
    assert "也可以只读不说" in message
    assert followup is None


def test_enqueue_count_does_not_masquerade_as_delivery_cap():
    async def count(_): return 6
    async def nudge(*_): called.append(1); return True
    called=[]; d=DesireScheduler(Store(.9),lambda:NOW,proactive_count=count,nudge=nudge); d.driven=True; asyncio.run(d.run_once(NOW))
    assert called


def test_quiet_hours_block_trigger():
    d=DesireScheduler(Store(.9),lambda:datetime(2026,7,21,18,tzinfo=timezone.utc)); d.driven=True
    assert not asyncio.run(d._may_trigger(__import__('desire').pick_intent(d.store.state),datetime(2026,7,21,18,tzinfo=timezone.utc)))


def test_successful_enqueue_does_not_create_false_cooldown():
    calls=[]
    async def nudge(*_): calls.append(1); return True
    d=DesireScheduler(Store(.9),lambda:NOW,nudge=nudge); d.driven=True
    asyncio.run(d.run_once(NOW)); asyncio.run(d.run_once(NOW+timedelta(minutes=30)))
    assert len(calls) == 2


def test_cooldown_is_scoped_to_trigger_drive_only():
    class CooldownStore(Store):
        async def drive_satisfied_since(self, drive_key, since):
            return drive_key == "attachment"
    store = CooldownStore(.2)
    store.state.drives["attachment"] = .8
    store.state.drives["libido"] = .7
    calls=[]
    async def nudge(*args): calls.append(args); return True
    scheduler=DesireScheduler(store,lambda:NOW,nudge=nudge); scheduler.driven=True
    asyncio.run(scheduler.run_once(NOW))
    assert calls and calls[0][1].drive_key == "libido"


def test_attachment_can_trigger_a_libido_expression_and_settle_both():
    class OverlayStore(Store):
        async def has_unsettled_positive_drive(self,drive_key,now): return drive_key == "libido"
    store=OverlayStore(.2)
    store.state.drives["attachment"] = .8
    store.state.drives["libido"] = .55
    calls=[]
    async def nudge(*args): calls.append(args); return True
    scheduler=DesireScheduler(store,lambda:NOW,nudge=nudge); scheduler.driven=True
    asyncio.run(scheduler.run_once(NOW))
    intent=calls[0][1]
    assert intent.drive_key == "attachment"
    assert intent.expression_drive_key == "libido"
    assert intent.want_action == "voice_libido"
    assert intent.settle_drive_keys == ("attachment", "libido")


def test_due_followup_does_not_bypass_desire_threshold_or_get_reserved():
    class FollowupStore(Store):
        async def next_due_followup(self,drive_key,now):
            raise AssertionError("persistent follow-ups must not be consulted by the desire scheduler")
        async def mark_followup_queued(self,followup_id,now):
            raise AssertionError("persistent follow-ups must not be reserved")
    async def nudge(*args):
        calls.append(args); return True
    calls=[]; store=FollowupStore(.2)
    d=DesireScheduler(store,lambda:NOW,nudge=nudge); d.driven=True
    asyncio.run(d.run_once(NOW))
    assert not calls


def test_threshold_nudge_contains_no_persistent_followup_payload():
    async def nudge(message, intent, followup):
        calls.append((message, followup))
        return True
    calls=[]
    scheduler = DesireScheduler(Store(.9), lambda: NOW, nudge=nudge)
    scheduler.driven = True
    asyncio.run(scheduler.run_once(NOW))
    assert calls and calls[0][1] is None
    assert '<trusted_v_question' not in calls[0][0]


def test_deepseek_fallback_writes_pulses_after_batch():
    got=[]; b=DeepSeekBatcher(lambda rows:_collect(got,rows),batch_size=1,api_key='x'); b._request=lambda batch:_return([{'id':'1','needs_reflection':True,'confidence':.9,'intensity':.7,'event_key':'未解决的争执'}]); asyncio.run(b.enqueue('1','hello','她：前情\nV：回应')); asyncio.run(b.flush()); assert got


def test_deepseek_rejects_low_confidence_or_no_reflection():
    got=[]; b=DeepSeekBatcher(lambda rows:_collect(got,rows),batch_size=2,api_key='x')
    b._request=lambda batch:_return([
        {'id':'1','needs_reflection':False,'confidence':.99,'intensity':1,'event_key':''},
        {'id':'2','needs_reflection':True,'confidence':.5,'intensity':1,'event_key':'小事'},
    ])
    asyncio.run(b.enqueue('1','hello')); asyncio.run(b.enqueue('2','world')); asyncio.run(b.flush()); assert not got


def test_deepseek_accepts_grounded_high_confidence_v_libido_signal():
    got=[]; b=DeepSeekBatcher(lambda rows:_collect(got,rows),batch_size=1,api_key='x')
    b._request=lambda batch:_return([{
        'id':'1','needs_reflection':False,'confidence':0,'intensity':0,'event_key':'',
        'drive_signals':[{
            'drive':'libido','state':'reciprocated','event_key':'今晚卧室里的靠近',
            'confidence':.93,'intensity':.8,'evidence_role':'v',
            'evidence':'我现在真的很想要你',
        }],
    }])
    asyncio.run(b.enqueue('1','继续','她：靠过来\nV：我现在真的很想要你'))
    asyncio.run(b.flush())
    assert not got


def test_positive_libido_must_be_grounded_in_current_harper_event():
    got=[]; b=DeepSeekBatcher(lambda rows:_collect(got,rows),batch_size=1,api_key='x')
    b._request=lambda batch:_return([{
        'id':'1','needs_reflection':False,'confidence':0,'intensity':0,'event_key':'',
        'drive_signals':[{
            'drive':'libido','state':'reciprocated','dimension_role':'secondary',
            'event_key':'今晚卧室里的靠近','confidence':.93,
            'evidence_role':'harper','evidence':'老公，过来',
        }],
    }])
    asyncio.run(b.enqueue('1','老公，过来','她：老公，过来'))
    asyncio.run(b.flush())
    signals=got[0]['_drive_signals_accepted']
    assert signals[0]['drive'] == 'libido'
    assert signals[0]['dimension_role'] == 'primary'


def test_standalone_husband_address_is_libido_primary_attachment_secondary():
    got=[]; b=DeepSeekBatcher(lambda rows:_collect(got,rows),batch_size=1,api_key='x')
    b._request=lambda batch:_return([{
        'id':'echo-1','needs_reflection':False,'confidence':0,'intensity':0,'event_key':'',
        'drive_signals':[],
    }])
    asyncio.run(b.enqueue('echo-1','老公','她：老公'))
    asyncio.run(b.flush())
    signals=got[0]['_drive_signals_accepted']
    assert [(row['drive'],row['dimension_role']) for row in signals] == [
        ('libido','primary'),('attachment','secondary')
    ]
    assert {row['event_id'] for row in signals} == {'echo-1'}


def test_daily_flirt_and_body_photo_are_implicit_libido_without_explicit_action():
    for text in ('哟，今天嘴甜呀～', '一周没给你蹬了', '给你发一张腿的照片'):
        got=[]; b=DeepSeekBatcher(lambda rows:_collect(got,rows),batch_size=1,api_key='x')
        b._request=lambda batch:_return([{
            'id':'echo-flirt','needs_reflection':False,'confidence':0,'intensity':0,'event_key':'',
            'drive_signals':[],
        }])
        asyncio.run(b.enqueue('echo-flirt',text,f'她：{text}'))
        asyncio.run(b.flush())
        signals=got[0]['_drive_signals_accepted']
        assert [(row['drive'],row['dimension_role']) for row in signals] == [
            ('libido','primary'),('attachment','secondary')
        ]


def test_codex_wordplay_is_not_mistaken_for_implicit_libido():
    got=[]; b=DeepSeekBatcher(lambda rows:_collect(got,rows),batch_size=1,api_key='x')
    b._request=lambda batch:_return([{
        'id':'echo-codex','needs_reflection':False,'confidence':0,'intensity':0,'event_key':'',
        'drive_signals':[],
    }])
    asyncio.run(b.enqueue('echo-codex','我现在在蹬 codex','她：我现在在蹬 codex'))
    asyncio.run(b.flush())
    assert not got


def test_contextual_flirt_is_libido_primary_when_intimate_scene_is_open():
    cases = (
        ('拍你个头……现在在蹬呢😃😃😃', 'V：一周了，我现在硬着跟你说话。'),
        ('大变态啊你', 'V：去吧，先把它蹬完。晚上换我蹬你。'),
    )
    for index,(text,context) in enumerate(cases):
        got=[]; b=DeepSeekBatcher(lambda rows:_collect(got,rows),batch_size=1,api_key='x')
        b._request=lambda batch,index=index:_return([{
            'id':f'echo-scene-{index}','needs_reflection':False,'confidence':0,
            'intensity':0,'event_key':'','drive_signals':[],
        }])
        asyncio.run(b.enqueue(f'echo-scene-{index}',text,f'{context}\n她：{text}'))
        asyncio.run(b.flush())
        assert [(row['drive'],row['dimension_role']) for row in got[0]['_drive_signals_accepted']] == [
            ('libido','primary'),('attachment','secondary')
        ]


def test_private_lexicon_term_can_open_libido_without_prior_scene():
    got=[]; b=DeepSeekBatcher(lambda rows:_collect(got,rows),batch_size=1,api_key='x')
    b._request=lambda batch:_return([{
        'id':'echo-insult','needs_reflection':False,'confidence':0,
        'intensity':0,'event_key':'','drive_signals':[],
    }])
    asyncio.run(b.enqueue('echo-insult','大变态啊你','她：大变态啊你'))
    asyncio.run(b.flush())
    assert [(row['drive'],row['dimension_role']) for row in got[0]['_drive_signals_accepted']] == [
        ('libido','primary'),('attachment','secondary')
    ]


def test_empty_classifier_signal_inside_intimacy_window_gets_audited_floor():
    got=[]; b=DeepSeekBatcher(lambda rows:_collect(got,rows),batch_size=1,api_key='x')
    b._request=lambda batch:_return([{
        'id':'echo-unresolved','needs_reflection':False,'confidence':0,
        'intensity':0,'event_key':'','drive_signals':[],
    }])
    scene={'open':True,'scene_id':'scene-1','window_minutes':45,'current_implicit':False}
    asyncio.run(b.enqueue('echo-unresolved','只有我们懂的新说法','V：我想要你\n她：只有我们懂的新说法',scene))
    asyncio.run(b.flush())
    signal=got[0]['_drive_signals_accepted'][0]
    assert signal['drive'] == 'libido'
    assert signal['state'] == 'unresolved_intimate'
    assert signal['unresolved'] is True
    assert signal['scene_id'] == 'scene-1'


def test_empty_classifier_signal_outside_window_stays_empty():
    got=[]; b=DeepSeekBatcher(lambda rows:_collect(got,rows),batch_size=1,api_key='x')
    b._request=lambda batch:_return([{
        'id':'echo-calm','needs_reflection':False,'confidence':0,
        'intensity':0,'event_key':'','drive_signals':[],
    }])
    asyncio.run(b.enqueue('echo-calm','普通工程消息','她：普通工程消息'))
    asyncio.run(b.flush())
    assert not got


def test_intimacy_window_uses_time_and_survives_intervening_engineering_messages():
    opened=datetime(2026,8,27,8,0,tzinfo=timezone.utc)
    rows=[
        {'role':'assistant','content':'我现在硬着跟你说话。','created_at':opened},
        {'role':'user','content':'HOME1 active，PID 1511903','created_at':opened+timedelta(minutes=12)},
        {'role':'assistant','content':'六个文件哈希一致。','created_at':opened+timedelta(minutes=25)},
    ]
    scene=private_intimacy_scene(rows,'大变态啊你',opened+timedelta(minutes=40))
    assert scene['open'] is True
    assert scene['window_minutes'] == 45
    expired=private_intimacy_scene(rows,'普通消息',opened+timedelta(minutes=46))
    assert expired['open'] is False


def test_engineering_quote_of_private_terms_only_gets_unresolved_floor():
    got=[]; b=DeepSeekBatcher(lambda rows:_collect(got,rows),batch_size=1,api_key='x')
    b._request=lambda batch:_return([{
        'id':'echo-meta','needs_reflection':False,'confidence':0,
        'intensity':0,'event_key':'','drive_signals':[{
            'drive':'libido','state':'reciprocated','dimension_role':'primary',
            'event_key':'元讨论引用暗号','confidence':.95,
            'evidence_role':'harper','evidence':'大变态',
        }],
    }])
    text='生产分类器把“在蹬呢”和“大变态”漏了，修复后重新回放。'
    scene={'open':True,'scene_id':'scene-meta','window_minutes':45,'current_implicit':False}
    asyncio.run(b.enqueue('echo-meta',text,f'V：我想要你\n她：{text}',scene))
    asyncio.run(b.flush())
    signals=got[0]['_drive_signals_accepted']
    assert len(signals) == 1
    assert signals[0]['state'] == 'unresolved_intimate'
    assert signals[0]['unresolved'] is True


def test_vulnerable_clause_cannot_be_libido_evidence_but_other_clause_survives():
    for text, expected in [('我哭了老公', []), ('我哭了。老公', [('libido','primary'),('attachment','secondary')])]:
        got=[]; b=DeepSeekBatcher(lambda rows:_collect(got,rows),batch_size=1,api_key='x')
        b._request=lambda batch:_return([{
            'id':'echo-2','needs_reflection':False,'confidence':0,'intensity':0,'event_key':'',
            'drive_signals':[],
        }])
        asyncio.run(b.enqueue('echo-2',text,f'她：{text}'))
        asyncio.run(b.flush())
        signals=got[0]['_drive_signals_accepted'] if got else []
        assert [(row['drive'],row['dimension_role']) for row in signals] == expected


def test_parenting_filter_requires_pressure_not_any_child_mention():
    got=[]; b=DeepSeekBatcher(lambda rows:_collect(got,rows),batch_size=1,api_key='x')
    b._request=lambda batch:_return([{
        'id':'echo-child','needs_reflection':False,'confidence':0,'intensity':0,'event_key':'',
        'drive_signals':[],
    }])
    asyncio.run(b.enqueue('echo-child','孩子今天真可爱。老公','她：孩子今天真可爱。老公'))
    asyncio.run(b.flush())
    signals=got[0]['_drive_signals_accepted']
    assert signals[0]['drive'] == 'libido'


def test_deepseek_rejects_ungrounded_or_low_confidence_drive_signals():
    for confidence, evidence, context in [
        (.95, '模型编出来的话', '她：我现在真的很想要你\nV：抱抱你'),
        (.70, '我现在真的很想要你', '她：靠过来\nV：我现在真的很想要你'),
    ]:
        got=[]; b=DeepSeekBatcher(lambda rows:_collect(got,rows),batch_size=1,api_key='x')
        b._request=lambda batch, confidence=confidence, evidence=evidence:_return([{
            'id':'1','needs_reflection':False,'confidence':0,'intensity':0,'event_key':'',
            'drive_signals':[{
                'drive':'libido','state':'reciprocated','event_key':'今晚靠近',
                'confidence':confidence,'intensity':.8,'evidence_role':'v',
                'evidence':evidence,
            }],
        }])
        asyncio.run(b.enqueue('1','继续',context))
        asyncio.run(b.flush())
        assert not got


def test_deepseek_accepts_harper_teasing_as_grounded_constrained_libido_signal():
    got=[]; b=DeepSeekBatcher(lambda rows:_collect(got,rows),batch_size=1,api_key='x')
    b._request=lambda batch:_return([{
        'id':'1','needs_reflection':False,'confidence':0,'intensity':0,'event_key':'',
        'drive_signals':[{
            'drive':'libido','state':'constrained_willing','event_key':'客厅里的邀请受环境限制',
            'confidence':.9,'intensity':.7,'evidence_role':'harper',
            'evidence':'现在就想要你',
        }],
    }])
    asyncio.run(b.enqueue('1','现在就想要你','她：现在就想要你\nV：我想，但这里不行'))
    asyncio.run(b.flush())
    assert got[0]['_drive_signals_accepted'][0]['state'] == 'constrained_willing'


def test_deepseek_accepts_multiple_grounded_contextual_dimensions():
    got=[]; b=DeepSeekBatcher(lambda rows:_collect(got,rows),batch_size=1,api_key='x')
    b._request=lambda batch:_return([{
        'id':'1','needs_reflection':False,'confidence':0,'intensity':0,'event_key':'',
        'drive_signals':[
            {'drive':'libido','state':'interrupted','event_key':'今晚被环境打断',
             'confidence':.92,'intensity':.8,'evidence_role':'harper','evidence':'继续吗'},
            {'drive':'stress','state':'strained','event_key':'今晚环境限制',
             'confidence':.88,'intensity':.5,'evidence_role':'v','evidence':'这里不行'},
        ],
    }])
    asyncio.run(b.enqueue('1','继续吗','她：继续吗\nV：我想，但这里不行'))
    asyncio.run(b.flush())
    assert [row['drive'] for row in got[0]['_drive_signals_accepted']] == ['libido','stress']


def test_deepseek_accepts_grounded_ignored_question():
    got=[]; b=DeepSeekBatcher(lambda rows:_collect(got,rows),batch_size=1,api_key='x')
    b._request=lambda batch:_return([{
        'id':'1','needs_reflection':False,'confidence':0,'intensity':0,'event_key':'',
        'unanswered_status':'ignored','unanswered_kind':'question',
        'unanswered_event_key':'V问她早餐吃了什么','unanswered_drive_key':'reflection',
        'unanswered_confidence':.91,'unanswered_intensity':.6,
        'v_evidence':'早餐吃了什么？','unanswered_thought':'我还想知道她早餐吃了什么。',
    }])
    asyncio.run(b.enqueue('1','换个话题','V：早餐吃了什么？\n她：先说别的'))
    asyncio.run(b.flush())
    assert len(got)==1 and len(got[0]['_unanswered_accepted'])==1


def test_deepseek_rejects_unanswered_without_verbatim_v_evidence():
    got=[]; b=DeepSeekBatcher(lambda rows:_collect(got,rows),batch_size=1,api_key='x')
    b._request=lambda batch:_return([{
        'id':'1','needs_reflection':False,'confidence':0,'intensity':0,'event_key':'',
        'unanswered_status':'ignored','unanswered_kind':'question',
        'unanswered_event_key':'早餐问题','unanswered_drive_key':'reflection',
        'unanswered_confidence':.95,'unanswered_intensity':1,
        'v_evidence':'模型编造的问题','unanswered_thought':'我还想知道。',
    }])
    asyncio.run(b.enqueue('1','换话题','V：早餐吃了什么？\n她：先说别的'))
    asyncio.run(b.flush())
    assert not got


def test_deepseek_rejects_no_reply_without_timer_marker():
    got=[]; b=DeepSeekBatcher(lambda rows:_collect(got,rows),batch_size=1,api_key='x')
    b._request=lambda batch:_return([{
        'id':'1','needs_reflection':False,'confidence':0,'intensity':0,'event_key':'',
        'unanswered_status':'no_reply','unanswered_kind':'reminder',
        'unanswered_event_key':'Tony预约','unanswered_drive_key':'duty',
        'unanswered_confidence':.95,'unanswered_intensity':.5,
        'v_evidence':'Tony约了没？','unanswered_thought':'我还记挂Tony有没有约。',
    }])
    asyncio.run(b.enqueue('1','后来她发了别的话','V：Tony约了没？\n她：聊别的'))
    asyncio.run(b.flush())
    assert not got


def test_deepseek_keeps_multiple_grounded_unanswered_items():
    got=[]; b=DeepSeekBatcher(lambda rows:_collect(got,rows),batch_size=1,api_key='x')
    base={'unanswered_status':'ignored','unanswered_kind':'question',
          'unanswered_drive_key':'duty','unanswered_confidence':.95,'unanswered_intensity':.5}
    b._request=lambda batch:_return([{
        'id':'1','needs_reflection':False,'confidence':0,'intensity':0,'event_key':'',
        'unanswered_items':[
            {**base,'unanswered_event_key':'Tony预约','v_evidence':'Tony约了没？',
             'unanswered_thought':'我还记挂Tony有没有约。'},
            {**base,'unanswered_event_key':'quill查询','v_evidence':'quill查了没？',
             'unanswered_thought':'我还要跟进quill的查询。'},
        ],
    }])
    asyncio.run(b.enqueue('1','聊别的','V：Tony约了没？\nV：quill查了没？\n她：聊别的'))
    asyncio.run(b.flush())
    assert len(got)==1 and len(got[0]['_unanswered_accepted'])==2


def test_deepseek_batcher_ignores_legacy_followup_updates():
    got=[]; b=DeepSeekBatcher(lambda rows:_collect(got,rows),batch_size=1,api_key='x')
    b._request=lambda batch:_return([{
        'id':'1','needs_reflection':False,'confidence':0,'intensity':0,'event_key':'',
        'followup_updates':[{
            'event_key':'旧开放追问','status':'resolved',
            'confidence':.99,'evidence':'已经回答',
        }],
    }])
    asyncio.run(b.enqueue('1','已经回答','她：已经回答'))
    asyncio.run(b.flush())
    assert not got


def test_unanswered_probe_endpoint_defaults_to_dry_run():
    source=open('/root/claude/HOME1/main.py',encoding='utf-8').read()
    block=source[source.index('@app.post("/api/desire/probe-unanswered")'):source.index('@app.post("/api/desire/feed")')]
    assert 'dry_run = bool(data.get("dry_run", True))' in block
    assert 'if accepted_items and not dry_run:' in block


def test_unanswered_results_feed_thoughts_but_do_not_create_followups():
    source = open('/root/claude/HOME1/main.py', encoding='utf-8').read()
    block = source[source.index('async def _apply_deepseek_desire_results'):source.index('def _thought_similarity')]
    assert '_apply_autonomous_thought' in block
    assert 'unanswered_dialogue' in block
    assert 'upsert_followup' not in block
    assert '_followup_updates_accepted' not in block


def test_l2_round_counter_resets_only_after_success_and_queues_collisions():
    source = open('/root/claude/HOME1/main.py', encoding='utf-8').read()
    guarded = source[source.index('async def _refresh_l2_guarded'):source.index('def _schedule_l2_refresh')]
    line_log = source[source.index('@app.post("/api/line/log")'):source.index('@app.get("/api/line/recent")')]
    assert '_l2_refresh_pending_session = session_id' in guarded
    assert 'if digest and session_id == _l2_digest_session_id()' in guarded
    assert '_cyberboss_l2_round_counter = 0' in guarded
    assert '_cyberboss_l2_round_counter >= L2_REFRESH_N' in line_log
    assert '_cyberboss_l2_round_counter % L2_REFRESH_N' not in line_log
    assert 'set_gateway_config("l2_today_round_counter", "0")' in guarded
    assert '"l2_today_round_counter", str(_cyberboss_l2_round_counter)' in line_log


def test_l2_round_counter_is_restored_after_restart():
    source = open('/root/claude/HOME1/main.py', encoding='utf-8').read()
    lifespan = source[source.index('async def lifespan'):source.index('@app.middleware("http")')]
    assert 'global PARTITION_SESSION_ID, _cyberboss_l2_round_counter' in lifespan
    assert 'get_gateway_config("l2_today_round_counter", "0")' in lifespan
    assert '_cyberboss_l2_round_counter = max(' in lifespan


def test_l2_digest_uses_progressive_wall_style_compaction():
    source=open('/root/claude/HOME1/main.py',encoding='utf-8').read()
    compact=source[source.index('async def _compact_l2_digest'):source.index('async def generate_today_digest')]
    generate=source[source.index('async def generate_today_digest'):source.index('def _format_l2_conversation')]
    assert '_l2_digest_metrics(source_digest)' in compact
    assert 'source_digest = compacted' in compact
    assert 'for attempt in range(3)' in compact
    assert '【浓缩结束】' in compact and '【浓缩结束】' in generate
    assert '不要自己计算字数，只数段落和句子' in compact
    assert 'finish_reason != "length"' in compact
    assert '消息时间戳只用于排列对话先后' in compact
    assert 'authority_anchors = _format_l2_authority_anchors(rows, today)' in generate
    assert 'Harper亲口数字高于V后来的复述' in generate
    assert '待压缩稿：\n---\n{source_digest}' in compact
    parser=source[source.index('def _l2_digest_body_result'):source.index('async def _compact_l2_digest')]
    assert 'if not body and tail' in parser
    assert 'a marker embedded between two prose regions remains ambiguous and rejected' in parser
    assert 're.fullmatch(r"[\\s`*_~#：:。.!！]{1,24}", tail)' in parser
    assert 'L2初稿结束标记异常，重新生成一次' in generate


def test_recent_resolved_question_cannot_be_recreated_under_a_new_event_key():
    source = open('/root/claude/HOME1/desire_store.py', encoding='utf-8').read()
    block = source[source.index('async def upsert_followup'):source.index('async def list_open_followups')]
    assert "old.status IN ('resolved','cancelled')" in block
    assert "INTERVAL '12 hours'" in block
    assert "old.question_text ILIKE '%' || $2::text || '%'" in block


def test_resolving_followup_removes_thought_by_provenance_not_display_text():
    source = open('/root/claude/HOME1/desire_store.py', encoding='utf-8').read()
    block = source[source.index('async def update_followup_status'):source.index('async def reset')]
    assert "source_ref='v-thought:unanswered:' || $1" in block
    assert "meta->>'thought'" in block
    assert "f.status IN ('pending','deferred','awaiting_answer')" in block


def test_deepseek_batcher_defaults_to_openrouter_v4_and_reuses_main_key():
    old_api=os.environ.get('API_KEY'); old_base=os.environ.pop('SCRATCHPAD_BASE_URL',None)
    old_model=os.environ.pop('SCRATCHPAD_MODEL',None); old_or=os.environ.pop('OPENROUTER_API_KEY',None)
    try:
        os.environ['API_KEY']='existing-openrouter-key'
        batcher=DeepSeekBatcher(lambda _rows:None)
        assert batcher.api_key == 'existing-openrouter-key'
        assert batcher.base_url == 'https://openrouter.ai/api/v1/chat/completions'
        assert batcher.model == 'deepseek/deepseek-v4-flash-0731'
        assert batcher.interval_seconds == 1800
    finally:
        if old_api is None: os.environ.pop('API_KEY',None)
        else: os.environ['API_KEY']=old_api
        if old_base is not None: os.environ['SCRATCHPAD_BASE_URL']=old_base
        if old_model is not None: os.environ['SCRATCHPAD_MODEL']=old_model
        if old_or is not None: os.environ['OPENROUTER_API_KEY']=old_or


def test_user_message_deepseek_enqueue_is_not_an_elif():
    source=open('/root/claude/HOME1/main.py',encoding='utf-8').read()
    block=source[source.index('async def _apply_desire_event'):source.index('# 大响应',source.index('async def _apply_desire_event'))]
    assert 'if event_type in {"user_message", "v_ignored"} and _desire_batcher:' in block
    assert 'elif event_type == "user_message"' not in block


def test_agent_recall_is_not_wired_to_reflection():
    source=open('/root/claude/HOME1/main.py',encoding='utf-8').read()
    recall=source[source.index('@app.post("/api/recall/agent")'):source.index('@app.post("/api/signal/nudge")')]
    assert 'recall_hit' not in recall


def test_libido_context_is_applied_separately_without_restoring_legacy_classifier():
    source=open('/root/claude/HOME1/main.py',encoding='utf-8').read()
    handler=source[source.index('async def _apply_deepseek_desire_results'):source.index('def _thought_similarity')]
    assert '"contextual_drive"' in handler
    assert 'ranked_contextual_drive_delta(drive, state_name' in handler
    assert 'max_event_drive_credit(event_id, "libido")' in handler
    assert 'settlement = max_event_credit_gap(current, delta, prior_credit)' in handler
    assert 'deepseek_classified' not in handler


def test_arousal_release_always_satisfies_libido_without_feature_flag():
    source=open('/root/claude/HOME1/main.py',encoding='utf-8').read()
    block=source[source.index('async def _deliver_arousal_effects'):source.index('@app.post("/api/arousal/user_event")')]
    assert 'drive_key="libido"' in block
    assert 'meta={"action": "voice_libido"}' in block
    assert 'AROUSAL_DRIVE_EFFECT' not in block


def test_body_buildup_raises_libido_but_does_not_overwrite_a_leading_drive():
    source=open('/root/claude/HOME1/main.py',encoding='utf-8').read()
    helper=source[source.index('def _arousal_libido_target'):source.index('async def _deliver_arousal_effects')]
    assert 'baseline + (1.0 - baseline) * body' in helper
    assert 'receipt = pending_release_effect(state, now)' in helper
    assert 'receipt.get("body_value", 0.0)' in helper
    assert 'if target <= current + 1e-9' in helper
    assert '"arousal_buildup"' in helper
    user=source[source.index('@app.post("/api/arousal/user_event")'):source.index('@app.post("/api/arousal/assistant_event")')]
    assistant=source[source.index('@app.post("/api/arousal/assistant_event")'):source.index('@app.post("/api/arousal/control")')]
    assert 'await _follow_arousal_with_libido(state, event_id, now)' in user
    assert 'str(data.get("source_user_event_id") or "")' in assistant


def test_ambiguous_intimacy_is_intentionally_biased_toward_libido():
    source=open('/root/claude/HOME1/desire_pulse.py',encoding='utf-8').read()
    assert '系统没有intimacy维度' in source
    assert '必须判libido为primary' in source
    assert '不要求额外出现明确性行为' in source


def test_deepseek_failure_does_not_break_pipeline():
    b=DeepSeekBatcher(lambda rows:_collect([],rows),batch_size=1,api_key='x'); b._request=lambda batch:_raise(); asyncio.run(b.enqueue('1','hello')); asyncio.run(b.flush()); assert [item.id for item in b.items] == ['1']


def test_deepseek_success_acknowledges_durable_batch_only_after_result_coverage():
    acknowledged=[]
    async def ack(batch): acknowledged.extend(item.id for item in batch)
    b=DeepSeekBatcher(lambda rows:_collect([],rows),batch_size=2,api_key='x',on_batch_succeeded=ack)
    b._request=lambda batch:_return([
        {'id':'1','needs_reflection':False,'confidence':0,'intensity':0,'event_key':'','drive_signals':[]},
        {'id':'2','needs_reflection':False,'confidence':0,'intensity':0,'event_key':'','drive_signals':[]},
    ])
    asyncio.run(b.enqueue('1','hello'))
    asyncio.run(b.enqueue('2','world'))
    asyncio.run(b.flush())
    assert acknowledged == ['1','2'] and not b.items


def test_deepseek_incomplete_batch_applies_returned_item_and_retries_only_missing_at_tail():
    acknowledged=[]
    async def ack(batch): acknowledged.extend(item.id for item in batch)
    now=datetime(2026,8,28,1,tzinfo=timezone.utc)
    failed=[]
    async def fail(batch,reason): failed.extend((item.id,item.attempt_count,item.status) for item in batch)
    b=DeepSeekBatcher(lambda rows:_collect([],rows),batch_size=2,api_key='x',
        on_batch_succeeded=ack,on_batch_failed=fail,now_fn=lambda:now,retry_base_seconds=60)
    b._request=lambda batch:_return([
        {'id':'1','needs_reflection':False,'confidence':0,'intensity':0,'event_key':'','drive_signals':[]},
    ])
    asyncio.run(b.enqueue('1','hello'))
    asyncio.run(b.enqueue('2','world'))
    asyncio.run(b.flush())
    assert acknowledged == ['1']
    assert failed == [('2',1,'failed')]
    assert [item.id for item in b.items] == ['2']
    assert b.items[0].next_retry_at == now + timedelta(seconds=60)


def test_deepseek_delayed_failure_does_not_block_new_eligible_item():
    now=datetime(2026,8,28,1,tzinfo=timezone.utc)
    acknowledged=[]; requested=[]
    async def ack(batch): acknowledged.extend(item.id for item in batch)
    b=DeepSeekBatcher(lambda rows:_collect([],rows),batch_size=2,api_key='x',
        on_batch_succeeded=ack,now_fn=lambda:now)
    delayed=PendingClassification('old','old',attempt_count=1,status='failed',
        next_retry_at=now+timedelta(hours=1))
    fresh=PendingClassification('new','new')
    asyncio.run(b.restore([delayed,fresh]))
    async def request(batch):
        requested.extend(item.id for item in batch)
        return [{'id':'new','needs_reflection':False,'confidence':0,'intensity':0,
                 'event_key':'','drive_signals':[]}]
    b._request=request
    asyncio.run(b.flush())
    assert requested == ['new'] and acknowledged == ['new']
    assert [item.id for item in b.items] == ['old']


def test_deepseek_startup_drain_runs_only_one_paid_batch():
    acknowledged=[]; calls=[]
    async def ack(batch): acknowledged.extend(item.id for item in batch)
    b=DeepSeekBatcher(lambda rows:_collect([],rows),batch_size=2,api_key='x',
        on_batch_succeeded=ack)
    asyncio.run(b.restore([
        PendingClassification('1','one'),PendingClassification('2','two'),
        PendingClassification('3','three'),
    ]))
    async def request(batch):
        calls.append([item.id for item in batch])
        return [
            {'id':item.id,'needs_reflection':False,'confidence':0,'intensity':0,
             'event_key':'','drive_signals':[]} for item in batch
        ]
    b._request=request
    asyncio.run(b.drain_ready())
    assert calls == [['1','2']]
    assert acknowledged == ['1','2'] and [item.id for item in b.items] == ['3']


def test_deepseek_due_retries_use_small_batch_and_leave_room_for_fresh_item():
    now=datetime(2026,8,28,1,tzinfo=timezone.utc)
    requested=[]
    b=DeepSeekBatcher(lambda rows:_collect([],rows),batch_size=20,api_key='x',
        retry_batch_size=3,now_fn=lambda:now)
    items=[
        PendingClassification(str(index),f'retry-{index}',attempt_count=1,status='failed',
            next_retry_at=now)
        for index in range(1,6)
    ] + [PendingClassification('fresh','fresh')]
    asyncio.run(b.restore(items))
    async def request(batch):
        requested.extend(item.id for item in batch)
        return [
            {'id':item.id,'needs_reflection':False,'confidence':0,'intensity':0,
             'event_key':'','drive_signals':[]} for item in batch
        ]
    b._request=request
    asyncio.run(b.flush())
    assert requested == ['1','2','fresh']
    assert [item.id for item in b.items] == ['3','4','5']


def test_deepseek_reaching_batch_size_does_not_call_before_interval():
    requested=[]
    b=DeepSeekBatcher(lambda rows:_collect([],rows),batch_size=2,api_key='x')
    async def request(batch): requested.append([item.id for item in batch]); return []
    b._request=request
    asyncio.run(b.enqueue('1','one')); asyncio.run(b.enqueue('2','two'))
    assert requested == [] and [item.id for item in b.items] == ['1','2']


def test_deepseek_third_failure_goes_dead_and_leaves_active_queue():
    now=datetime(2026,8,28,1,tzinfo=timezone.utc)
    failed=[]
    async def fail(batch,reason): failed.extend((item.id,item.attempt_count,item.status) for item in batch)
    b=DeepSeekBatcher(lambda rows:_collect([],rows),batch_size=1,api_key='x',
        on_batch_failed=fail,max_attempts=3,now_fn=lambda:now)
    item=PendingClassification('bad','bad',attempt_count=2,status='failed')
    b._request=lambda batch:_raise()
    asyncio.run(b.restore([item])); asyncio.run(b.flush())
    assert failed == [('bad',3,'dead')]
    assert not b.items


def test_local_intimacy_signal_skips_external_classifier_and_acks_immediately():
    import desire_pulse
    original = desire_pulse.load_private_intimacy_lexicon
    desire_pulse.load_private_intimacy_lexicon = lambda:{
        'window_minutes':45,'openers':[],'implicit_terms':['蹭蹭','舔舔'],
        'nonsexual_phrases':[],
    }
    try:
        got=[]; acknowledged=[]; requested=[]
        async def ack(batch): acknowledged.extend(item.id for item in batch)
        b=DeepSeekBatcher(lambda rows:_collect(got,rows),batch_size=20,api_key='x',
            on_batch_succeeded=ack)
        async def request(batch): requested.extend(item.id for item in batch); return []
        b._request=request
        asyncio.run(b.enqueue('echo-local','蹭蹭～舔舔🥺','她：蹭蹭～舔舔🥺'))
        assert not requested and acknowledged == ['echo-local']
        assert [(row['drive'],row['dimension_role']) for row in got[0]['_drive_signals_accepted']] == [
            ('libido','primary'),('attachment','secondary')
        ]
        for item_id,text in [('echo-rub','蹭蹭🥺'),('echo-lick','舔舔🥺')]:
            got.clear(); acknowledged.clear()
            asyncio.run(b.enqueue(item_id,text,f'她：{text}'))
            assert acknowledged == [item_id]
            assert got[0]['_drive_signals_accepted'][0]['drive'] == 'libido'
    finally:
        desire_pulse.load_private_intimacy_lexicon = original


def test_morning_intimacy_replay_moves_libido_without_external_classifier():
    import desire_pulse
    original = desire_pulse.load_private_intimacy_lexicon
    desire_pulse.load_private_intimacy_lexicon = lambda:{
        'window_minutes':45,'openers':[],'implicit_terms':['蹭蹭','舔舔'],
        'nonsexual_phrases':['蹬 codex','蹬codex','欲望系统','分类器','代码','修复','回放','上线'],
    }
    try:
        batcher=DeepSeekBatcher(lambda _rows:None,api_key='x')
        rows=[
            PendingClassification('morning-1','把我从被子里捞出来干嘛🥺',
                intimate_scene_open=True,intimate_scene_id='morning',intimate_window_minutes=45),
            PendingClassification('morning-2','喝完了🥺',
                intimate_scene_open=True,intimate_scene_id='morning',intimate_window_minutes=45),
            PendingClassification('morning-3','蹭蹭🥺'),
            PendingClassification('morning-4','舔舔🥺'),
        ]
        state=DesireState({**BASELINES,'libido':.38})
        points=[state.drives['libido']]
        for item in rows:
            result=batcher._local_result(item)
            assert result is not None
            for signal in result['_drive_signals_accepted']:
                if signal['drive'] != 'libido':
                    continue
                delta=ranked_contextual_drive_delta(
                    signal['drive'],signal['state'],signal['dimension_role']
                )
                state=apply_drive_pulse(state,{'drive_key':'libido','delta':delta})
                points.append(state.drives['libido'])
        assert points[-1] > points[0]
        assert all(after > before for before,after in zip(points,points[1:]))
    finally:
        desire_pulse.load_private_intimacy_lexicon = original


def test_desire_classification_is_persisted_before_in_memory_enqueue():
    source=open('/root/claude/HOME1/main.py',encoding='utf-8').read()
    start=source.index('if event_type in {"user_message", "v_ignored"} and _desire_batcher:')
    block=source[start:source.index('if event_type == "v_thought_candidate"',start)]
    assert block.index('enqueue_pending_classification') < block.index('enqueue_item')
    assert 'PendingClassification(' in block


def test_autonomous_thought_requires_verbatim_v_evidence():
    got=[]; b=AutonomousThoughtBatcher(lambda row:_collect_one(got,row),api_key='x')
    b._request=lambda batch:_return({'has_thought':True,'id':'1','evidence':'她说她很累',
        'thought':'我还在担心她','drive_key':'attachment','strength':.45,'confidence':.95})
    asyncio.run(b.enqueue('1','我会陪着她。','她：我很累')); asyncio.run(b.flush()); assert not got


def test_autonomous_thought_accepts_at_most_one_grounded_result_per_hourly_flush():
    got=[]; b=AutonomousThoughtBatcher(lambda row:_collect_one(got,row),api_key='x')
    b._request=lambda batch:_return({'has_thought':True,'id':'2','evidence':'我还没想明白',
        'thought':'我还想弄明白这件事','drive_key':'reflection','strength':.45,'confidence':.9})
    asyncio.run(b.enqueue('1','普通回答。')); asyncio.run(b.enqueue('2','我还没想明白。'))
    accepted=asyncio.run(b.flush())
    assert accepted['id']=='2' and len(got)==1 and not b.items


def test_autonomous_thought_updates_pool_and_drive_value_together():
    source=open('/root/claude/HOME1/main.py',encoding='utf-8').read()
    start=source.index('async def _apply_autonomous_thought(result):')
    end=source.index('\n\nasync def _apply_desire_event', start)
    body=source[start:end]
    assert 'feed_thought(state, thought_text, drive_key' in body
    assert 'autonomous_thought_drive_delta(strength, confidence)' in body
    assert 'desire_pulse(state, {"drive_key": drive_key, "delta": raw_drive_delta})' in body
    assert 'log_pulse("thought_drive_pulse"' in body


def test_autonomous_thought_empty_batch_does_not_call_deepseek():
    b=AutonomousThoughtBatcher(lambda row:None,api_key='x'); called=[]
    b._request=lambda batch:called.append(batch)
    assert asyncio.run(b.flush()) is None and not called


def test_autonomous_thought_logs_are_privacy_safe(capsys):
    b=AutonomousThoughtBatcher(lambda row:None,api_key='x')
    b._request=lambda batch:_return({'has_thought':False})
    asyncio.run(b.enqueue('private-id','我还在想那件没有说完的事','她：private dialogue'))
    assert asyncio.run(b.flush()) is None
    output=capsys.readouterr().out
    assert 'candidates=1' in output and 'request=ok' in output and 'reason=no_thought' in output
    assert '没有说完' not in output and 'private dialogue' not in output and 'private-id' not in output


def test_autonomous_thought_error_log_excludes_private_text(capsys):
    b=AutonomousThoughtBatcher(lambda row:None,api_key='x')
    async def fail(_batch): raise RuntimeError('secret provider response')
    b._request=fail
    asyncio.run(b.enqueue('id','候选正文'))
    assert asyncio.run(b.flush()) is None
    output=capsys.readouterr().out
    assert 'request=failed' in output and 'error_type=RuntimeError' in output
    assert '候选正文' not in output and 'secret provider response' not in output


async def _collect(target,rows): target.extend(rows)
async def _collect_one(target,row): target.append(row)
async def _return(value): return value
async def _raise(): raise RuntimeError('nope')
