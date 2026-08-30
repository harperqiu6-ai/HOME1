"""V desire v1 pure-function kernel.

This module deliberately performs no IO and never reads the clock. Callers own
timestamps and persistence. Thought text is opaque data and must never be used
to build a model prompt.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field
from math import sqrt
from typing import Any, Mapping

DRIVE_KEYS = ("attachment", "curiosity", "reflection", "duty", "social", "fatigue", "libido", "stress")
DRIVE_CAPS = {"attachment": 0.90}
BASELINES = {
    "attachment": 0.55, "curiosity": 0.45, "reflection": 0.40, "duty": 0.35,
    "social": 0.30, "fatigue": 0.20, "libido": 0.35, "stress": 0.15,
}
EASE_RATE_PER_HOUR = 0.15
INTENT_TRIGGER_THRESHOLD = 0.70
FATIGUE_REST_GATE = 0.72
ACTION_SATISFY = {
    "voice_attachment": {"attachment": 0.58, "duty": 0.90},
    "voice_curiosity": {"curiosity": 0.50},
    "play_fishing": {"curiosity": 0.42},
    "voice_reflection": {"reflection": 0.55, "curiosity": 0.85},
    "voice_social": {"social": 0.55, "curiosity": 0.82},
    "visit_aisay": {"social": 0.48, "curiosity": 0.88},  # legacy receipt
    # Sexual release satisfies libido. Relationship closeness is classified and
    # settled independently under attachment; do not silently couple the axes.
    "voice_libido": {"libido": 0.55},
    "voice_stress": {"stress": 0.45, "attachment": 0.85},
    "voice_duty": {"duty": 0.55},
    "rest": {"fatigue": 0.60}, "skip": {},
}
FLIT_DECAY = 0.82
FIXATION_GROW = 1.10
FLIT_TO_FIXATION = 0.80
FIXATION_FEED = 0.85
FIXATION_FEED_GAIN = 0.18
FIXATION_RESOLVE_FEEDS = 3
FIXATION_DRIVE_BOOST = 0.35
DROP_BELOW = 0.06
DESIRE_THOUGHT_MAX = 80
CONTEXTUAL_STATE_DELTAS = {
    ("attachment", "seeking"): (0.05, 0.12),
    ("attachment", "disconnected"): (0.04, 0.10),
    ("attachment", "reassured"): (-0.09, -0.20),
    ("curiosity", "engaged"): (0.05, 0.12),
    ("curiosity", "resolved"): (-0.07, -0.16),
    ("duty", "committed"): (0.04, 0.10),
    ("duty", "completed"): (-0.10, -0.22),
    ("duty", "cancelled"): (-0.08, -0.18),
    ("social", "interested"): (0.04, 0.10),
    ("social", "satisfied"): (-0.08, -0.18),
    ("libido", "reciprocated"): (0.09, 0.15),
    ("libido", "constrained_willing"): (0.06, 0.11),
    ("libido", "interrupted"): (0.03, 0.08),
    # A classifier miss inside an already-open private intimacy window must
    # leave a small, auditable floor instead of silently becoming zero.
    ("libido", "unresolved_intimate"): (0.05, 0.05),
    ("libido", "unwilling"): (0.0, -0.04),
    ("libido", "distressed"): (-0.08, -0.15),
    ("libido", "satisfied"): (-0.18, -0.32),
    ("stress", "strained"): (0.06, 0.16),
    ("stress", "relieved"): (-0.09, -0.22),
}
AUTONOMOUS_THOUGHT_DRIVE_MIN = 0.03
AUTONOMOUS_THOUGHT_DRIVE_MAX = 0.06

ACTION_BY_DRIVE = {
    "attachment": "voice_attachment", "curiosity": "voice_curiosity",
    "reflection": "voice_reflection", "duty": "voice_duty",
    "social": "voice_social", "libido": "voice_libido", "stress": "voice_stress",
}
REASON_BY_DRIVE = {
    "attachment": "我有点想靠近你，说一句自己的话。",
    "curiosity": "我对一件还不知道的东西起了兴趣，想亲自弄明白或试一试。",
    "reflection": "我想把最近沉下来的东西跟你说说。",
    "duty": "我还记挂着一件没有落下的事。",
    "social": "我想去外面的关系里走一走：看看论坛，或者给 KAI 写封信。",
    "libido": "我想离你近一点。",
    "stress": "我心里有点堵，想跟你说两句。",
}


@dataclass
class Thought:
    text: str
    drive_key: str
    kind: str = "flit"
    strength: float = 0.5
    born_at: Any = None
    fed_count: int = 0
    id: Any = None


@dataclass
class DesireState:
    drives: dict[str, float] = field(default_factory=lambda: dict(BASELINES))
    thoughts: list[Thought] = field(default_factory=list)


@dataclass(frozen=True)
class Intent:
    want_action: str
    drive_key: str
    reason: str
    score: float
    query_hint: str = ""
    expression_drive_key: str = ""
    settle_drive_keys: tuple[str, ...] = ()


def _clamp(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def contextual_drive_delta(drive_key: str, state: str, intensity: Any) -> float | None:
    bounds = CONTEXTUAL_STATE_DELTAS.get((str(drive_key), str(state)))
    if not bounds:
        return None
    amount = _clamp(intensity)
    low_intensity, high_intensity = bounds
    return low_intensity + (high_intensity - low_intensity) * amount


def ranked_contextual_drive_delta(drive_key: str, state: str, dimension_role: str) -> float | None:
    """Resolve a model-selected dimension to a code-owned fixed contribution.

    The classifier may choose only which dimension is primary or secondary. It
    cannot manufacture a score: primary uses the strong endpoint and secondary
    the weak endpoint of the reviewed state table. Negative states preserve
    their direction while following the same strong/weak convention.
    """
    bounds = CONTEXTUAL_STATE_DELTAS.get((str(drive_key), str(state)))
    if not bounds or dimension_role not in {"primary", "secondary"}:
        return None
    weak, strong = bounds
    if weak < 0 and strong < 0:
        return min(weak, strong) if dimension_role == "primary" else max(weak, strong)
    return strong if dimension_role == "primary" else weak


def max_event_credit_gap(current: Any, raw_delta: Any, prior_credit: Any) -> dict[str, float]:
    """Calculate a same-event max settlement in actual-drive units."""
    current_value = _clamp(current)
    raw_value = max(0.0, float(raw_delta or 0.0))
    prior_value = max(0.0, float(prior_credit or 0.0))
    contextual_credit = raw_value * sqrt(max(0.0, 1.0 - current_value))
    target_credit = max(prior_value, contextual_credit)
    gap = max(0.0, target_credit - prior_value)
    raw_gap = gap / sqrt(max(1e-12, 1.0 - current_value))
    return {
        "contextual_credit": contextual_credit,
        "target_credit": target_credit,
        "gap": gap,
        "raw_gap": raw_gap,
    }


def autonomous_thought_drive_delta(strength: Any, confidence: Any) -> float:
    """Turn a grounded V thought into an immediate, bounded desire pulse.

    The thought pool still handles repetition/fixation.  This pulse ensures a
    single genuine thought is not discarded without affecting its drive.
    """
    normalized_strength = _clamp(strength)
    normalized_confidence = _clamp(confidence)
    if normalized_strength <= 0 or normalized_confidence <= 0:
        return 0.0
    return max(
        AUTONOMOUS_THOUGHT_DRIVE_MIN,
        min(AUTONOMOUS_THOUGHT_DRIVE_MAX, 0.12 * normalized_strength * normalized_confidence),
    )


def _clamp_drive(key: str, value: Any) -> float:
    try:
        return max(0.0, min(DRIVE_CAPS.get(key, 1.0), float(value)))
    except (TypeError, ValueError):
        return 0.0


def _coerce(state: DesireState | Mapping[str, Any]) -> DesireState:
    if isinstance(state, DesireState):
        out = deepcopy(state)
    else:
        raw_thoughts = state.get("thoughts", []) if isinstance(state, Mapping) else []
        out = DesireState(
            drives=dict(state.get("drives", {})) if isinstance(state, Mapping) else {},
            thoughts=[t if isinstance(t, Thought) else Thought(**t) for t in raw_thoughts],
        )
    out.drives = {key: _clamp_drive(key, out.drives.get(key, BASELINES[key])) for key in DRIVE_KEYS}
    out.thoughts = out.thoughts[:DESIRE_THOUGHT_MAX]
    return out


def state_dict(state: DesireState | Mapping[str, Any]) -> dict[str, Any]:
    return asdict(_coerce(state))


def ease_drives(state: DesireState | Mapping[str, Any], dt: float) -> DesireState:
    out = _coerce(state)
    step = max(0.0, min(1.0, EASE_RATE_PER_HOUR * max(0.0, float(dt or 0)) / 3600.0))
    for key in DRIVE_KEYS:
        out.drives[key] = _clamp_drive(key, out.drives[key] + (BASELINES[key] - out.drives[key]) * step)
    return out


def pulse(state: DesireState | Mapping[str, Any], event: Mapping[str, Any] | tuple[str, float]) -> DesireState:
    out = _coerce(state)
    if isinstance(event, tuple):
        key, delta = event
    else:
        key, delta = event.get("drive_key"), event.get("delta", 0.0)
    if key not in DRIVE_KEYS:
        return out
    delta = float(delta or 0.0)
    current = out.drives[key]
    # Positive pulses have diminishing returns; negative pulses remain monotonic.
    actual = delta * sqrt(max(0.0, 1.0 - current)) if delta >= 0 else delta
    out.drives[key] = _clamp_drive(key, current + actual)
    return out


def satisfy(state: DesireState | Mapping[str, Any], action: str, drive_key: str = "") -> DesireState:
    out = _coerce(state)
    factors = ACTION_SATISFY.get(action, {})
    if action == "skip" and drive_key in DRIVE_KEYS:
        factors = {drive_key: 0.85}
    for key, factor in factors.items():
        out.drives[key] = _clamp_drive(key, out.drives[key] * factor)
    return out


def satisfy_to_baseline(
    state: DesireState | Mapping[str, Any], action: str, drive_key: str,
) -> DesireState:
    """Settle a processed drive without pushing it below its resting baseline."""
    out = satisfy(state, action, drive_key)
    if drive_key in DRIVE_KEYS:
        out.drives[drive_key] = max(BASELINES[drive_key], out.drives[drive_key])
    return out


def desire_scores(state: DesireState | Mapping[str, Any]) -> dict[str, float]:
    out = _coerce(state)
    scores = {key: out.drives[key] for key in ACTION_BY_DRIVE}
    for thought in out.thoughts:
        if thought.kind == "fixation" and thought.drive_key in scores:
            scores[thought.drive_key] += FIXATION_DRIVE_BOOST * _clamp(thought.strength)
    return {key: _clamp_drive(key, value) for key, value in scores.items()}


def pick_intent(state: DesireState | Mapping[str, Any]) -> Intent:
    out = _coerce(state)
    if out.drives["fatigue"] >= FATIGUE_REST_GATE:
        return Intent("rest", "fatigue", "我有点累了，现在只想安静歇一会儿。", out.drives["fatigue"], "")
    scores = desire_scores(out)
    key = max(scores, key=scores.get)
    return Intent(ACTION_BY_DRIVE[key], key, REASON_BY_DRIVE[key], scores[key], key)


def tick_thoughts(state: DesireState | Mapping[str, Any]) -> DesireState:
    out = _coerce(state)
    kept: list[Thought] = []
    for thought in out.thoughts:
        thought.strength = _clamp(thought.strength)
        if thought.kind == "flit":
            thought.strength *= FLIT_DECAY
            if thought.strength >= FLIT_TO_FIXATION:
                thought.kind = "fixation"
            if thought.strength >= DROP_BELOW:
                kept.append(thought)
            continue
        thought.kind = "fixation"
        thought.strength = _clamp(thought.strength * FIXATION_GROW)
        if thought.strength >= FIXATION_FEED and thought.drive_key in DRIVE_KEYS:
            out = pulse(out, {"drive_key": thought.drive_key, "delta": FIXATION_FEED_GAIN})
            thought.strength = _clamp(thought.strength * 0.7)
            thought.fed_count += 1
        if thought.fed_count < FIXATION_RESOLVE_FEEDS:
            kept.append(thought)
    out.thoughts = kept[:DESIRE_THOUGHT_MAX]
    return out


def feed_thought(state: DesireState | Mapping[str, Any], text: str, drive_key: str,
                 kind: str = "flit", strength: float = 0.5, born_at: Any = None) -> DesireState:
    out = _coerce(state)
    if drive_key not in DRIVE_KEYS or not str(text).strip():
        return out
    normalized_kind = "fixation" if kind == "fixation" else "flit"
    for thought in out.thoughts:
        if thought.text == str(text).strip() and thought.drive_key == drive_key:
            thought.strength = _clamp(thought.strength + _clamp(strength) * sqrt(max(0.0, 1.0 - thought.strength)))
            if thought.strength >= FLIT_TO_FIXATION:
                thought.kind = "fixation"
            return out
    out.thoughts.append(Thought(str(text).strip(), drive_key, normalized_kind, _clamp(strength), born_at))
    out.thoughts = sorted(out.thoughts, key=lambda item: item.strength, reverse=True)[:DESIRE_THOUGHT_MAX]
    return out
