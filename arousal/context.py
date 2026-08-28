"""Message-local context filtering.

本系统只用于所有参与者均为成年人的、自愿的虚构亲密互动；停止、否定与控制信号永远优先于刺激识别。
Only for consensual fictional intimate interaction between adults. Stop,
negation, and control signals always take priority over stimulus recognition.

The order is deliberately fixed: stop/control, unsafe sentence context,
participant/direction, action, body part, then posture.

Only ``红灯`` is intentionally message-wide, which differs from the tutorial:
false stops are disruptive, while the explicit safety word remains absolute.
Other negation is clause-local and is checked after removing matched keywords.
"""

from __future__ import annotations

import re
from typing import Any

_HARD_STOP = ("红灯",)
_CLAUSE_NEGATION = ("不要", "别", "还没", "不许", "没有", "并未", "停下", "停止")
_QUESTION = ("会不会", "能不能", "是不是", "可不可以", "吗", "？", "?")
_PLAN = ("等会", "等下", "下次", "如果", "假如", "准备", "打算", "以后")
_RECALL = ("刚才", "之前", "回忆", "她说", "他说", "原话是", "转述")
_META = ("教程", "示例", "测试", "词表", "代码", "关键词", "这个词")
_RETRACT = ("但其实没做", "不过其实没做", "其实没有做", "但没有做")
_THIRD_PARTY = ("她在", "他在", "他们在")
_URL = re.compile(r"(?:https?://|www\.)", re.I)
_CODE = re.compile(r"```|`[^`]+`")
_QUOTE = re.compile(r"(?:“[^”]+”|「[^」]+」|『[^』]+』|\"[^\"]+\")")
_CLAUSE_SPLIT = re.compile(r"[。！？!?；;\n]+|[,，]+|…+|—{2,}")
_BENIGN_BIE = re.compile(r"(?:特别是?|别的|分别|告别|性别|区别|差别|个别|派别|级别|类别)")
_ACTION_SUBJECT = re.compile(r"[我你她他它]")
_FIRST_PERSON_BODY_OWNER = re.compile(
    r"我的(?:手|手指|舌头|嘴|身体|胯|腰|腿|膝盖|胸|阴茎|鸡巴|肉棒)"
)
# 让/叫/请 are deliberately absent: they hand the following pronoun the next
# verb ("我让你…" means she acts, not V), so it must stay a subject candidate.
_NON_SUBJECT_PREFIX = frozenset("把被给对跟和替朝向帮")

# These are evidence exclusions, not a judgement about the whole message. A
# different clause in the same message may still be ordinary flirtation. Keep
# spouse language specific enough that V's intimate address “老公” is not
# confused with Harper discussing her marriage or separated husband.
_LIBIDO_VULNERABLE_PATTERNS = (
    re.compile(r"信仰|上帝|耶稣|基督|教会|圣经|罪疚|罪恶感|有罪"),
    re.compile(r"被抱养|被收养|抱养|收养|领养|养父母|亲生父母"),
    re.compile(r"(?:^哭$|哭了|在哭|想哭|哭着|哭泣|哭出来)|崩溃|情绪失控|撑不住|受不了了"),
    re.compile(
        r"疲惫|(?:^累$|累了|好累|很累|太累|更累|有点累|感觉累|累死|累坏|累得|累到)|"
        r"(?:^困$|困了|好困|很困|太困|困得)|不舒服|身体不适|生病|发烧|头晕|"
        r"头疼|肚子疼|身体疼|疼了|好疼|很疼|疼得|疼到|痛了|很痛|剧痛"
    ),
    re.compile(
        r"育儿|带娃|家长会|(?:孩子|小孩|幼崽|儿子|女儿|作业).{0,8}(?:累|烦|压力|操心|崩溃|照顾|接送|生病)|"
        r"(?:累|烦|压力|操心|崩溃|照顾|接送).{0,8}(?:孩子|小孩|幼崽|儿子|女儿|作业)"
    ),
    re.compile(r"婚姻|分居|离婚|丈夫|前夫|孩子(?:他|她)?爸|孩子的爸爸"),
)


def split_clauses(text: str) -> list[str]:
    """Expose the same clause boundary used by the BODY context filter."""
    return [part.strip() for part in _CLAUSE_SPLIT.split(str(text or "")) if part.strip()]


def is_vulnerable_libido_clause(clause: str) -> bool:
    """Return whether a clause is forbidden as positive libido evidence."""
    normalized = str(clause or "").strip()
    return bool(normalized) and any(pattern.search(normalized) for pattern in _LIBIDO_VULNERABLE_PATTERNS)


def libido_evidence_allowed(text: str) -> bool:
    """Require at least one non-vulnerable clause; callers still ground evidence."""
    return any(not is_vulnerable_libido_clause(clause) for clause in split_clauses(text))


def _entries(lexicon: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = lexicon.get(key, [])
    return value if isinstance(value, list) else []


def _valid_sensitivity(raw: Any) -> bool:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return False
    return 0.0 < value <= 1.0


def _clause_is_negated(clause: str, keyword: str) -> bool:
    """Check clause-local negation only after carving out the matched keyword."""
    remainder = clause.replace(keyword, "")
    return _has_clause_negation(remainder)


def _has_clause_negation(text: str) -> bool:
    """Check clause-local negation after caller-selected terms are removed."""
    remainder = text
    remainder = _BENIGN_BIE.sub("", remainder)
    return any(token in remainder for token in _CLAUSE_NEGATION)


def _unsafe_clause(clause: str) -> str | None:
    if any(token in clause for token in _QUESTION + _PLAN + _RECALL + _META + _RETRACT):
        return "unsafe_context"
    if _URL.search(clause) or _CODE.search(clause) or _QUOTE.search(clause):
        return "unsafe_context"
    if any(token in clause for token in _THIRD_PARTY):
        return "wrong_participant"
    return None


def _assistant_owns_action(clause: str, keyword: str) -> bool:
    """Require the nearest subject candidate before an action to be first-person."""
    for match in re.finditer(re.escape(keyword), clause):
        prefix = clause[:match.start()]
        # “我的手按着你…” is still V's action. The generic subject scan
        # deliberately ignores possessives, so admit only explicit body owners.
        if _FIRST_PERSON_BODY_OWNER.search(prefix):
            return True
        subjects = [
            subject.group()
            for subject in _ACTION_SUBJECT.finditer(prefix)
            if (subject.start() == 0 or prefix[subject.start() - 1] not in _NON_SUBJECT_PREFIX)
            and (subject.end() == len(prefix) or prefix[subject.end()] != "的")
        ]
        if subjects and subjects[-1] == "我":
            return True
    return False


def detect_release(text: str, lexicon: dict[str, Any]) -> bool:
    """Detect a safe free-text release trigger using message-local context."""
    if not isinstance(text, str) or not text.strip() or not isinstance(lexicon, dict):
        return False
    message = text.strip()
    if any(token in message for token in _HARD_STOP):
        return False

    phrases = lexicon.get("release_phrases", [])
    blockers = lexicon.get("release_blockers", [])
    if not isinstance(phrases, list) or not isinstance(blockers, list):
        return False
    triggers = [item for item in phrases if isinstance(item, str) and item]
    excluded = [item for item in blockers if isinstance(item, str) and item]
    if not triggers:
        return False

    clauses = [part.strip() for part in _CLAUSE_SPLIT.split(message) if part.strip()]
    for clause in clauses:
        if _unsafe_clause(clause) is not None:
            continue
        remainder = clause
        for blocker in excluded:
            remainder = remainder.replace(blocker, "")
        matched = [trigger for trigger in triggers if trigger in remainder]
        if not matched:
            continue
        for trigger in matched:
            remainder = remainder.replace(trigger, "")
        if not _has_clause_negation(remainder):
            return True
    return False


def analyze_text(text: str, lexicon: dict[str, Any], *, actor: str = "user",
                 scene_open: bool = False) -> dict[str, Any]:
    """Return a safe, message-local stimulus analysis.

    ``actor`` is ``user`` or ``assistant``. A rejected message always returns
    zero stimulus and never partially preserves an earlier keyword match.
    """
    result = {
        "accepted": False,
        "active": False,
        "passive": False,
        "stim": 0.0,
        "actions": [],
        "body_part": None,
        "posture_multiplier": 1.0,
        "reason": "no_action",
    }
    if not isinstance(text, str) or not text.strip() or not isinstance(lexicon, dict):
        return result
    message = text.strip()

    # 1. Control / stop remains message-wide.
    if any(token in message for token in _HARD_STOP):
        result["reason"] = "stop_or_negation"
        return result

    # 2–3. Reject unsafe clauses, not unrelated safe clauses in the message.
    clauses = [part.strip() for part in _CLAUSE_SPLIT.split(message) if part.strip()]
    safe_clauses = [clause for clause in clauses if _unsafe_clause(clause) is None]
    if not safe_clauses:
        result["reason"] = "unsafe_context"
        return result
    message = "，".join(safe_clauses)
    if actor not in ("user", "assistant"):
        result["reason"] = "wrong_actor"
        return result

    # 4. Actions. Each distinct action is counted at most once. A one-character
    # action is too ambiguous for substring matching, so it is accepted only
    # when a configured body part occurs in the very same clause.
    parts = lexicon.get("body_parts", {})
    valid_part_names = {
        name
        for name, config in parts.items()
        if isinstance(parts, dict) and isinstance(name, str) and name
        and isinstance(config, dict)
        and _valid_sensitivity(config.get("sensitivity"))
    } if isinstance(parts, dict) else set()
    matches: list[tuple[float, str, bool]] = []
    for category in ("touch", "actions", "contact"):
        for entry in _entries(lexicon, category):
            keyword = entry.get("kw")
            try:
                delta = float(entry.get("delta", 0.0))
            except (TypeError, ValueError):
                continue
            if not (isinstance(keyword, str) and keyword and 0.0 < delta <= 1.0):
                continue
            matching_clauses = [
                clause for clause in safe_clauses
                if keyword in clause and not _clause_is_negated(clause, keyword)
            ]
            if entry.get("requires_body_part") is True:
                matching_clauses = [
                    clause for clause in matching_clauses
                    if any(part in clause for part in valid_part_names)
                ]
            if entry.get("requires_first_person") is True:
                matching_clauses = [
                    clause for clause in matching_clauses
                    if _assistant_owns_action(clause, keyword)
                ]
            if actor == "assistant":
                matching_clauses = [
                    clause for clause in matching_clauses
                    if _assistant_owns_action(clause, keyword)
                ]
            if len(keyword) == 1:
                matching_clauses = [
                    clause for clause in matching_clauses
                    if any(part in clause for part in valid_part_names)
                ]
            if matching_clauses:
                passive = bool(entry.get("passive", category == "contact"))
                matches.append((delta, keyword, passive))
    unique: dict[str, tuple[float, str, bool]] = {}
    for item in matches:
        previous = unique.get(item[1])
        if previous is None or item[0] > previous[0]:
            unique[item[1]] = item
    ranked_actions = sorted(unique.values(), reverse=True)
    if actor == "assistant" and not ranked_actions:
        result["reason"] = "wrong_direction"
        return result

    # Address and feedback terms cannot open a calm scene. They count only
    # alongside an accepted action or when the projected state says it is open.
    supplemental_matches: dict[str, tuple[float, str, bool]] = {}
    if ranked_actions or scene_open:
        for group in ("address", "feedback"):
            for entry in _entries(lexicon, group):
                keyword = entry.get("kw")
                try:
                    delta = float(entry.get("delta", 0.0))
                except (TypeError, ValueError):
                    continue
                if not (isinstance(keyword, str) and keyword and 0.0 < delta <= 1.0):
                    continue
                if any(
                    keyword in clause and not _clause_is_negated(clause, keyword)
                    for clause in safe_clauses
                ):
                    previous = supplemental_matches.get(keyword)
                    item = (delta, keyword, False)
                    if previous is None or delta > previous[0]:
                        supplemental_matches[keyword] = item
    ranked_supplemental = sorted(supplemental_matches.values(), reverse=True)
    if not ranked_actions and not ranked_supplemental:
        return result

    # 5. Body part.
    body_sensitivity = 1.0
    body_part = None
    if isinstance(parts, dict):
        candidates = []
        for name, config in parts.items():
            if not isinstance(name, str) or name not in message or not isinstance(config, dict):
                continue
            try:
                sensitivity = float(config.get("sensitivity", 0.0))
            except (TypeError, ValueError):
                continue
            if 0.0 < sensitivity <= 1.0:
                candidates.append((sensitivity, name))
        if candidates:
            body_sensitivity, body_part = max(candidates)

    # 6. Posture is local and only modifies an already valid action.
    posture_multiplier = 1.0
    postures = lexicon.get("postures", {})
    if isinstance(postures, dict):
        for keyword, raw in postures.items():
            if isinstance(keyword, str) and keyword in message:
                try:
                    posture_multiplier = max(0.90, min(1.15, float(raw)))
                except (TypeError, ValueError):
                    posture_multiplier = 1.0
                break

    if ranked_actions:
        active = bool(ranked_supplemental) or any(not item[2] for item in ranked_actions)
        passive = not active and any(item[2] for item in ranked_actions)
        strongest = ranked_actions[0][0]
        extras = ranked_actions[1:] + ranked_supplemental
    else:
        active = True
        passive = False
        strongest = ranked_supplemental[0][0]
        extras = ranked_supplemental[1:]
    second = max((item[0] for item in extras), default=0.0) * 0.30
    stim = min(1.0, (strongest + second) * body_sensitivity * posture_multiplier)
    ranked = sorted(ranked_actions + ranked_supplemental, reverse=True)
    result.update({
        "accepted": True,
        "active": active,
        "passive": passive,
        "stim": stim,
        "actions": [item[1] for item in ranked],
        "body_part": body_part,
        "posture_multiplier": posture_multiplier,
        "reason": "accepted",
    })
    return result


parse_context = analyze_text
