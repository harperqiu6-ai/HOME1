"""Reproduce the A/B/C/D acceptance scenarios from rework sheet #1."""

import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from arousal.context import analyze_text
from arousal.core import apply_assistant_event, apply_user_event, initial_state


LEXICON = {
    "touch": [
        {"kw": "按动", "delta": 0.8},
        {"kw": "摩擦", "delta": 0.6},
        {"kw": "自定义动作", "delta": 0.8},
    ],
    "contact": [{"kw": "保持接触", "delta": 0.25, "passive": True}],
    "body_parts": {"敏感点": {"sensitivity": 0.95}},
    "release_phrases": [],
}


def charged_state():
    state = initial_state(1000)
    state["value"] = .9633
    state["pending_release"] = {
        "source_user_event_id": "user:threshold",
        "created_at": 1000,
    }
    return state


state = charged_state()
parent = state["pending_release"]["source_user_event_id"]
print(f"A 越过不归点：value={state['value']:.4f}  pending={bool(state['pending_release'])}")
for delay in (0, 15, 30, 60):
    result, fired = apply_assistant_event(
        copy.deepcopy(state),
        "",
        event_id=f"assistant:delay:{delay}",
        source_user_event_id=parent,
        complete=True,
        libido=.4,
        now=state["at"] + delay,
    )
    print(
        f"  回复晚 {delay:2d}s → fired={fired!s:<5} "
        f"value={result['value']:.4f}  pending 残留={bool(result['pending_release'])}"
    )

inert, _ = apply_assistant_event(
    initial_state(1000),
    "我现在自定义动作",
    event_id="assistant:inert",
    source_user_event_id=None,
    complete=True,
    libido=.4,
    now=1001,
)
active, _ = apply_assistant_event(
    initial_state(1000),
    "我现在自定义动作",
    event_id="assistant:active",
    source_user_event_id=None,
    complete=True,
    libido=.4,
    now=1001,
    lexicon=LEXICON,
)
print(f"B 默认空词表 value={inert['value']:.4f}；传入词表 value={active['value']:.4f}")

for text in ("特别想要你按动敏感点", "你在吗？现在按动敏感点", "按动敏感点，别停"):
    result = analyze_text(text, LEXICON)
    print(f"C {text!r} → accepted={result['accepted']}  reason={result['reason']}  stim={result['stim']:.4f}")

state = initial_state(1000)
state["value"] = .90
result, _ = apply_user_event(
    state,
    "保持接触敏感点",
    event_id="passive:above",
    libido=1,
    now=1000,
    lexicon=LEXICON,
)
print(f"D 0.90 上持续接触 → {result['value']:.4f}")
