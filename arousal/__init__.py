"""Deterministic arousal v1.

本系统只用于所有参与者均为成年人的、自愿的虚构亲密互动；停止、否定与控制信号永远优先于刺激识别。
Only for consensual fictional intimate interaction between adults. Stop,
negation, and control signals always take priority over stimulus recognition.
"""

from .core import (
    PROMPT_CONSTRAINT,
    ack_release_effect,
    apply_assistant_event,
    apply_user_event,
    control_event,
    initial_state,
    pending_release_effect,
    public_snapshot,
    status_line,
)

__all__ = [
    "PROMPT_CONSTRAINT",
    "ack_release_effect",
    "apply_assistant_event",
    "apply_user_event",
    "control_event",
    "initial_state",
    "pending_release_effect",
    "public_snapshot",
    "status_line",
]
