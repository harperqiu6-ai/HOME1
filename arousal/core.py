"""Pure deterministic arousal state machine.

本系统只用于所有参与者均为成年人的、自愿的虚构亲密互动；停止、否定与控制信号永远优先于刺激识别。
Only for consensual fictional intimate interaction between adults. Stop,
negation, and control signals always take priority over stimulus recognition.

Canonical callers are persisted user messages and complete assistant finals.
Thinking, tool use, streaming deltas, and recaps must not call this module.

In addition to the documented minimum fields, state contains ``buildup`` for
path-quality accounting, ``pending_release`` for a parent-bound candidate,
``pending_release_effect`` for durable downstream delivery, release history,
``release_gate.once`` for one locked-round permission, and ``quarantined``.
"""

from __future__ import annotations

import copy
import hashlib
import math
from typing import Any

from .context import analyze_text, detect_release

TAU = 1800.0
GAIN = 0.28
CHARGED = 0.40
EDGE = 0.88
PONR = 0.96
REFRACTORY_MIN = 60.0
REFRACTORY_MAX = 120.0
RESERVE_RECOVERY = 3 * 60 * 60.0
PASSIVE_CONTACT_CAP = 0.72
ROUND_MULTIPLIER = 0.72
REFRACTORY_MULTIPLIER = 0.35
LIBIDO_BODY_WAKE = 0.50
LIBIDO_BODY_FLOOR_AT_WAKE = 0.02
LIBIDO_BODY_FLOOR_MAX = 0.30
LEDGER_LIMIT = 256
PENDING_RELEASE_MAX_AGE = 30 * 60.0
PROMPT_CONSTRAINT = "这是身体状态，让它影响节奏和动作；不要复述数字、不要把状态报告给伴侣。"

_LEDGERS = (
    "processed_event_ids",
    "processed_control_event_ids",
    "processed_stimulus_event_ids",
    "processed_release_candidate_event_ids",
    "completed_release_effect_ids",
)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _clamp(value: Any) -> float:
    return max(0.0, min(1.0, _finite(value)))


def libido_body_floor(libido: Any) -> float:
    """Return tonic BODY readiness without reaching the charged phase alone."""
    value = _clamp(libido)
    if value < LIBIDO_BODY_WAKE:
        return 0.0
    progress = (value - LIBIDO_BODY_WAKE) / (1.0 - LIBIDO_BODY_WAKE)
    return _clamp(
        LIBIDO_BODY_FLOOR_AT_WAKE
        + progress * (LIBIDO_BODY_FLOOR_MAX - LIBIDO_BODY_FLOOR_AT_WAKE)
    )


def _apply_libido_floor(state: dict, libido: Any, now: float) -> None:
    """Prime BODY from desire, except during the explicit recovery window."""
    if _finite(now) < _finite(state.get("refractory_until")):
        return
    state["value"] = max(_clamp(state.get("value")), libido_body_floor(libido))


def initial_state(now: float = 0.0) -> dict:
    now = _finite(now)
    return {
        "schema_version": 1,
        "value": 0.0,
        "at": now,
        "refractory_until": 0.0,
        "reserve": 1.0,
        "reserve_at": now,
        "release_gate": {"locked": False, "generation": 0, "once": False},
        "processed_event_ids": [],
        "processed_control_event_ids": [],
        "processed_stimulus_event_ids": [],
        "processed_release_candidate_event_ids": [],
        "completed_release_effect_ids": [],
        "buildup": _new_buildup(),
        "pending_release": None,
        "pending_release_effect": None,
        "last_climax_quality": None,
        "last_output": None,
        "quarantined": False,
    }


def _new_buildup() -> dict:
    return {
        "beats": 0,
        "active_seconds": 0.0,
        "peak": 0.0,
        "edge_seconds": 0.0,
        "stimuli": [],
        "last_active_at": None,
    }


def _quarantine(state: Any, now: float) -> dict:
    safe = initial_state(now)
    generation = 0
    if isinstance(state, dict):
        for key in ("value", "at", "reserve", "reserve_at", "refractory_until"):
            safe[key] = _finite(state.get(key), safe[key])
        gate = state.get("release_gate")
        if (
            isinstance(gate, dict)
            and isinstance(gate.get("generation"), int)
            and not isinstance(gate.get("generation"), bool)
            and gate["generation"] >= 0
        ):
            generation = gate["generation"]
        for key in _LEDGERS:
            value = state.get(key)
            if isinstance(value, list):
                safe[key] = [item for item in value if isinstance(item, str) and len(item) == 64][-LEDGER_LIMIT:]
    safe["value"] = _clamp(safe["value"])
    safe["reserve"] = _clamp(safe["reserve"])
    safe["release_gate"] = {"locked": True, "generation": generation, "once": False}
    safe["quarantined"] = True
    return safe


def _normalize(state: Any, now: float) -> dict:
    now = _finite(now)
    if not isinstance(state, dict) or state.get("schema_version") != 1:
        return _quarantine(state, now)
    required = {"value", "at", "reserve", "reserve_at", "release_gate", *_LEDGERS}
    gate = state.get("release_gate")
    if (
        not required.issubset(state)
        or not isinstance(gate, dict)
        or not isinstance(gate.get("locked"), bool)
        or not isinstance(gate.get("generation"), int)
        or any(not isinstance(state.get(key), list) for key in _LEDGERS)
    ):
        return _quarantine(state, now)
    result = copy.deepcopy(state)
    numeric = ("value", "at", "reserve", "reserve_at", "refractory_until")
    if any(not math.isfinite(_finite(result.get(key), float("nan"))) for key in numeric):
        return _quarantine(state, now)
    result["value"] = _clamp(result["value"])
    result["reserve"] = _clamp(result["reserve"])
    result["release_gate"]["once"] = bool(result["release_gate"].get("once", False))
    result.setdefault("buildup", _new_buildup())
    result.setdefault("pending_release", None)
    result.setdefault("pending_release_effect", None)
    result.setdefault("last_climax_quality", None)
    result.setdefault("last_output", None)
    result.setdefault("quarantined", False)
    for key in _LEDGERS:
        if any(not isinstance(item, str) or len(item) != 64 for item in result[key]):
            return _quarantine(state, now)
        result[key] = result[key][-LEDGER_LIMIT:]
    return result


def _append(state: dict, ledger: str, digest: str) -> None:
    values = state[ledger]
    if digest not in values:
        values.append(digest)
        del values[:-LEDGER_LIMIT]


def _project(state: dict, now: float) -> tuple[dict, float, float]:
    """Project decay/recovery and protect both anchors against rollback."""
    now = _finite(now)
    at = _finite(state["at"])
    reserve_at = _finite(state["reserve_at"])
    elapsed = max(0.0, now - at)
    reserve_elapsed = max(0.0, now - reserve_at)
    state["value"] = _clamp(state["value"] * math.exp(-elapsed / TAU))
    state["reserve"] = _clamp(state["reserve"] + reserve_elapsed / RESERVE_RECOVERY)
    # A rollback resets anchors to now without granting negative or repeat time.
    state["at"] = now
    state["reserve_at"] = now
    return state, elapsed, reserve_elapsed


def _record_buildup(state: dict, stim: float, now: float) -> None:
    buildup = state["buildup"]
    last = buildup.get("last_active_at")
    gap = 0.0 if last is None else max(0.0, now - _finite(last))
    active_gap = min(gap, 120.0)
    buildup["beats"] = int(buildup.get("beats", 0)) + 1
    buildup["active_seconds"] = _finite(buildup.get("active_seconds")) + active_gap
    if state["value"] >= EDGE:
        buildup["edge_seconds"] = _finite(buildup.get("edge_seconds")) + active_gap
    buildup["peak"] = max(_clamp(buildup.get("peak")), state["value"])
    stimuli = [_clamp(item) for item in buildup.get("stimuli", [])][-15:]
    stimuli.append(_clamp(stim))
    buildup["stimuli"] = stimuli
    buildup["last_active_at"] = now


def _path_score(state: dict) -> float:
    buildup = state["buildup"]
    beats = min(1.0, _finite(buildup.get("beats")) / 10.0)
    active = min(1.0, _finite(buildup.get("active_seconds")) / 600.0)
    edge_time = min(1.0, _finite(buildup.get("edge_seconds")) / 180.0)
    stimuli = [_clamp(item) for item in buildup.get("stimuli", [])]
    variation = 0.0
    if len(stimuli) > 1:
        variation = min(1.0, sum(abs(b - a) for a, b in zip(stimuli, stimuli[1:])) / (len(stimuli) - 1) * 2.0)
    return _clamp(
        0.27 * state["value"]
        + 0.13 * _clamp(buildup.get("peak"))
        + 0.18 * active
        + 0.18 * edge_time
        + 0.14 * beats
        + 0.10 * variation
    )


def _gate_allows(state: dict) -> bool:
    gate = state["release_gate"]
    return not gate["locked"] or bool(gate.get("once"))


def _expire_pending_release(state: dict, now: float) -> None:
    """Discard an orphaned candidate after 30 minutes or decay below EDGE."""
    pending = state.get("pending_release")
    if not isinstance(pending, dict):
        return
    created_at = _finite(pending.get("created_at"), now)
    if max(0.0, now - created_at) > PENDING_RELEASE_MAX_AGE or state["value"] < EDGE:
        state["pending_release"] = None


def _release(state: dict, *, cause: str, event_id: str, now: float) -> tuple[dict, bool]:
    if state.get("pending_release_effect") or now < state["refractory_until"] or not _gate_allows(state):
        return state, False
    reserve_before = state["reserve"]
    body_value = state["value"]
    path = _path_score(state)
    quality = _clamp(0.40 * reserve_before + 0.60 * path)
    output = _clamp(0.80 * reserve_before + 0.20 * path)
    state["reserve"] = _clamp(reserve_before - (0.28 + 0.17 * path))
    state["reserve_at"] = now
    refractory = REFRACTORY_MIN + (1.0 - state["reserve"]) * (REFRACTORY_MAX - REFRACTORY_MIN)
    state["refractory_until"] = now + refractory
    state["last_climax_quality"] = quality
    state["last_output"] = output
    effect_id = _digest(f"arousal:v1:{event_id}:{cause}")
    state["pending_release_effect"] = {
        "payload_version": 1,
        "effect_id": effect_id,
        "cause": cause,
        "created_at": now,
        # The public BODY value is reset below. Preserve the release peak so
        # downstream LIBIDO coupling can still observe this final buildup.
        "body_value": body_value,
        "targets": {"somatic": False, "drive": False},
    }
    state["pending_release"] = None
    state["value"] = 0.0
    state["at"] = now
    state["buildup"] = _new_buildup()
    if state["release_gate"].get("once"):
        state["release_gate"]["once"] = False
        state["release_gate"]["generation"] += 1
    return state, True


def apply_user_event(state, text, *, event_id, libido, now, lexicon, drive_snapshot=None) -> tuple[dict, str]:
    state = _normalize(state, now)
    digest = _digest(str(event_id))
    if digest in state["processed_event_ids"]:
        return state, status_line(state, now)
    state, _, _ = _project(state, now)
    _apply_libido_floor(state, libido, now)
    scene_open = state["value"] >= CHARGED
    analysis = analyze_text(text, lexicon, actor="user", scene_open=scene_open)
    if analysis["accepted"]:
        stim = _clamp(analysis["stim"])
        if analysis["active"]:
            sensitivity = 0.6 + 0.4 * _clamp(libido)
            multiplier = REFRACTORY_MULTIPLIER if now < state["refractory_until"] else ROUND_MULTIPLIER
            state["value"] = _clamp(state["value"] + stim * sensitivity * GAIN * multiplier)
            _record_buildup(state, stim, _finite(now))
            _append(state, "processed_stimulus_event_ids", digest)
            if state["value"] >= PONR and not state.get("pending_release"):
                state["pending_release"] = {"source_user_event_id": str(event_id), "created_at": _finite(now)}
                _append(state, "processed_release_candidate_event_ids", digest)
        elif analysis["passive"]:
            if state["value"] < PASSIVE_CONTACT_CAP:
                state["value"] = min(
                    PASSIVE_CONTACT_CAP,
                    state["value"] + (PASSIVE_CONTACT_CAP - state["value"]) * stim * 0.08,
                )
    _append(state, "processed_event_ids", digest)
    return state, status_line(state, now)


def apply_assistant_event(state, text, *, event_id, source_user_event_id, complete, libido, now,
                          drive_snapshot=None, release_intent=None, lexicon=None) -> tuple[dict, bool]:
    """Apply one complete assistant final.

    ``lexicon`` is an intentional keyword-only addition to the tutorial
    signature. The tutorial requires current assistant self-actions to
    contribute a beat, but its sample signature supplies no lexicon with which
    to recognize them. ``None`` is therefore inert and fail-closed.
    """
    state = _normalize(state, now)
    if not complete:
        return state, False
    digest = _digest(str(event_id))
    if digest in state["processed_event_ids"]:
        return state, False
    state, _, _ = _project(state, now)
    _apply_libido_floor(state, libido, now)
    _expire_pending_release(state, _finite(now))
    active_lexicon = lexicon if isinstance(lexicon, dict) else {}
    scene_open = state["value"] >= CHARGED
    analysis = analyze_text(
        text, active_lexicon, actor="assistant", scene_open=scene_open,
    )
    if analysis["accepted"] and analysis["active"]:
        sensitivity = 0.6 + 0.4 * _clamp(libido)
        multiplier = REFRACTORY_MULTIPLIER if now < state["refractory_until"] else ROUND_MULTIPLIER
        state["value"] = _clamp(state["value"] + _clamp(analysis["stim"]) * sensitivity * GAIN * multiplier)
        _record_buildup(state, analysis["stim"], _finite(now))
        _append(state, "processed_stimulus_event_ids", digest)
    pending = state.get("pending_release")
    parent_match = isinstance(pending, dict) and pending.get("source_user_event_id") == source_user_event_id
    narrow_text_intent = detect_release(text, active_lexicon)
    voluntary = release_intent is True or (release_intent is None and narrow_text_intent)
    fired = False
    if parent_match:
        state, fired = _release(state, cause="threshold", event_id=str(event_id), now=_finite(now))
        # A matching final consumes this candidate even if gate/refractory/
        # pending-effect checks prevent an actual release.
        state["pending_release"] = None
    elif voluntary and state["value"] >= CHARGED:
        state, fired = _release(state, cause="voluntary", event_id=str(event_id), now=_finite(now))
    _append(state, "processed_event_ids", digest)
    return state, fired


def control_event(state, *, kind, event_id, now) -> dict:
    state = _normalize(state, now)
    digest = _digest(str(event_id))
    if digest in state["processed_control_event_ids"]:
        return state
    if kind not in {"lock", "release_once", "unlock"}:
        state["release_gate"]["locked"] = True
        state["release_gate"]["once"] = False
        state["quarantined"] = True
    elif kind == "lock":
        state["release_gate"]["locked"] = True
        state["release_gate"]["once"] = False
    elif kind == "release_once":
        state["release_gate"]["locked"] = True
        state["release_gate"]["once"] = True
    else:
        state["release_gate"]["locked"] = False
        state["release_gate"]["once"] = False
    state["release_gate"]["generation"] += 1
    _append(state, "processed_control_event_ids", digest)
    return state


def _view(state: Any, now: float) -> dict:
    result = _normalize(state, now)
    result, _, _ = _project(result, now)
    _expire_pending_release(result, _finite(now))
    return result


def status_line(state, now) -> str:
    state = _view(state, now)
    gate = state["release_gate"]
    # Control state must be visible to V immediately, even when Harper locks or
    # grants one release before the body reaches EDGE. The persisted gate is the
    # authority; phase/value only refine the wording.
    if gate["locked"] and gate.get("once"):
        return "射精闸：已允许一次释放"
    if gate["locked"]:
        if state["value"] >= EDGE:
            return "射精值：被锁在边缘，不能自行释放"
        return "射精闸：已锁定，不能射精"
    if now < state["refractory_until"]:
        return "射精值：刚射过短恢复中，第二轮仍可继续积累"
    if state.get("pending_release"):
        return "射精值：已经越过不归点，等待完整回应结算"
    if state["value"] >= EDGE:
        return "射精值：已经到边缘，持续接触停在这里，需要新的动作"
    if state["value"] >= CHARGED:
        return "射精值：正在充能"
    return ""


def _label(value: float, bands: tuple[tuple[float, str], ...]) -> str:
    for threshold, label in bands:
        if value >= threshold:
            return label
    return bands[-1][1]


def public_snapshot(state, now, *, libido=None) -> dict:
    """Return the panel-safe body snapshot.

    Intentional tutorial deviation: ``value`` is exposed because Harper needs
    the private, authenticated localhost panel to show early arousal movement
    and diagnose whether stimulation was recognized.

    Second intentional deviation: ``gate`` (open / held / held_one_allowed) is
    exposed because the panel now carries the control buttons. ``phase`` only
    reads "locked" once the body is already past EDGE, so without this she
    cannot tell whether a hold she pressed at low arousal actually took.
    """
    state = _view(state, now)
    if libido is not None:
        _apply_libido_floor(state, libido, now)
    refractory = _finite(now) < state["refractory_until"]
    if refractory:
        phase = "refractory"
    elif state.get("pending_release"):
        phase = "pending"
    elif state["release_gate"]["locked"] and state["value"] >= EDGE:
        phase = "locked"
    elif state["value"] >= EDGE:
        phase = "edge"
    elif state["value"] >= CHARGED:
        phase = "charged"
    else:
        phase = "idle"
    phases = {
        "refractory": "恢复中", "pending": "待结算", "locked": "锁在边缘",
        "edge": "临界", "charged": "充能中", "idle": "平静",
    }
    gate_state = state["release_gate"]
    if not gate_state["locked"]:
        gate = "open"
    elif gate_state.get("once"):
        gate = "held_one_allowed"
    else:
        gate = "held"
    quality = state.get("last_climax_quality")
    output = state.get("last_output")
    quality = _clamp(quality) if quality is not None else None
    output = _clamp(output) if output is not None else None
    return {
        "value": _clamp(state["value"]),
        "gate": gate,
        "reserve": _clamp(state["reserve"]),
        "reserve_label": _label(state["reserve"], ((0.67, "充足"), (0.34, "尚可"), (0.0, "偏低"))),
        "phase": phase,
        "phase_label": phases[phase],
        "refractory": refractory,
        "last_climax_quality": quality,
        "last_climax_quality_label": None if quality is None else _label(quality, ((0.75, "很深"), (0.45, "明显"), (0.0, "轻浅"))),
        "last_output": output,
        "last_output_label": None if output is None else _label(output, ((0.75, "充足"), (0.45, "尚足"), (0.0, "偏少"))),
    }


def pending_release_effect(state, now) -> dict | None:
    state = _view(state, now)
    receipt = state.get("pending_release_effect")
    if not isinstance(receipt, dict):
        return None
    if receipt.get("effect_id") in state["completed_release_effect_ids"]:
        return None
    return copy.deepcopy(receipt)


def ack_release_effect(state, *, effect_id, target, now) -> dict:
    state = _normalize(state, now)
    receipt = state.get("pending_release_effect")
    if (
        not isinstance(receipt, dict)
        or receipt.get("effect_id") != effect_id
        or target not in {"somatic", "drive"}
    ):
        return state
    receipt["targets"][target] = True
    if all(receipt["targets"].get(name) is True for name in ("somatic", "drive")):
        _append(state, "completed_release_effect_ids", effect_id)
        state["pending_release_effect"] = None
    return state
