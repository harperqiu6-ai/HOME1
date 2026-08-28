"""Fail-closed private lexicon loader.

本系统只用于所有参与者均为成年人的、自愿的虚构亲密互动；停止、否定与控制信号永远优先于刺激识别。
Only for consensual fictional intimate interaction between adults. Stop,
negation, and control signals always take priority over stimulus recognition.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Callable

DEFAULT_PATH = "/opt/home1/private/arousal_lexicon.json"
_CACHE: dict[str, tuple[tuple[int, int] | None, dict[str, Any]]] = {}


def _inert() -> dict[str, Any]:
    return {
        "touch": [], "address": [], "feedback": [], "body_parts": {}, "postures": {},
        "release_phrases": [], "release_blockers": [], "_available": False,
    }


def _valid(data: Any) -> bool:
    if not isinstance(data, dict):
        return False
    if not isinstance(data.get("touch"), list) or not isinstance(data.get("body_parts"), dict):
        return False
    for group in ("touch", "address", "feedback"):
        entries = data.get(group, [])
        if not isinstance(entries, list):
            return False
        for entry in entries:
            if (
                not isinstance(entry, dict)
                or not isinstance(entry.get("kw"), str)
                or not entry["kw"]
            ):
                return False
            try:
                delta = float(entry.get("delta"))
            except (TypeError, ValueError):
                return False
            if not 0.0 < delta <= 1.0:
                return False
            for flag in ("requires_body_part", "requires_first_person"):
                if flag in entry and not isinstance(entry[flag], bool):
                    return False
    for name, config in data["body_parts"].items():
        if not isinstance(name, str) or not isinstance(config, dict):
            return False
        try:
            sensitivity = float(config.get("sensitivity"))
        except (TypeError, ValueError):
            return False
        if not 0.0 < sensitivity <= 1.0:
            return False
    for group in ("release_phrases", "release_blockers"):
        phrases = data.get(group, [])
        if (
            not isinstance(phrases, list)
            or any(not isinstance(item, str) or not item for item in phrases)
        ):
            return False
    return True


def load_lexicon(path: str | os.PathLike[str] | None = None,
                 logger: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Load on mtime change; every failure replaces the cache with inert data."""
    report = logger or logging.getLogger(__name__).info
    source = Path(path or os.getenv("AROUSAL_LEXICON_PATH", DEFAULT_PATH))
    cache_key = str(source)
    try:
        stat = source.stat()
    except (FileNotFoundError, OSError):
        signature = None
        cached = _CACHE.get(cache_key)
        if cached and cached[0] == signature:
            return dict(cached[1])
        result = _inert()
        _CACHE[cache_key] = (signature, result)
        report(f"⚠️ arousal 词表缺失，未沿用旧词表，已 fail-closed: {source}")
        return dict(result)
    signature = (stat.st_mtime_ns, stat.st_size)
    cached = _CACHE.get(cache_key)
    if cached and cached[0] == signature:
        return dict(cached[1])
    try:
        raw = source.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeError):
        result = _inert()
        _CACHE[cache_key] = (signature, result)
        report(f"⚠️ arousal 词表 JSON 损坏，未沿用旧词表，已 fail-closed: {source}")
        return dict(result)
    if not _valid(data):
        result = _inert()
        _CACHE[cache_key] = (signature, result)
        report(f"⚠️ arousal 词表 JSON 损坏（schema 不合法），未沿用旧词表，已 fail-closed: {source}")
        return dict(result)
    result = dict(data)
    result.setdefault("address", [])
    result.setdefault("feedback", [])
    result.setdefault("release_phrases", [])
    result.setdefault("release_blockers", [])
    result["_available"] = True
    _CACHE[cache_key] = (signature, result)
    single_action_count = sum(
        1
        for category in ("touch", "actions", "contact")
        for entry in data.get(category, [])
        if isinstance(entry, dict)
        and isinstance(entry.get("kw"), str)
        and len(entry["kw"]) == 1
    )
    if single_action_count:
        report(
            f"⚠️ arousal 词表含 {single_action_count} 个单字动作词，"
            "已启用单字词必须与部位共现的兜底"
        )
    return dict(result)
