"""Event classification and batched DeepSeek fallback for desire v1."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from arousal.context import is_vulnerable_libido_clause, split_clauses

try:
    import httpx
except ImportError:  # Pure/wiring tests can run in a minimal stdlib environment.
    httpx = None

EVENT_RULES = {
    "user_silent_2h": [("attachment", 0.06)],
    "user_silent_4h": [("attachment", 0.10)], "user_silent_8h": [("attachment", 0.15)],
    "v_ignored": [("attachment", 0.05)],
    "reminder_fired": [("duty", 0.05)],
    "dream_generated": [("reflection", 0.08)],
    "night_hour": [("fatigue", 0.06)], "day_hour": [("fatigue", -0.04)],
}

SOCIAL_MENTION_RE = re.compile(
    r"(?i)(?:(?<![A-Za-z0-9_])aisay(?![A-Za-z0-9_])|花园|(?<![A-Za-z0-9_])kai(?![A-Za-z0-9_]))"
)
EXTERNAL_NEWS_CURIOSITY_RE = re.compile(r"你(?:知不知道|知道吗|晓不晓得)")
PRIVATE_INTIMACY_LEXICON_PATH = Path(os.getenv(
    "V_DESIRE_PRIVATE_LEXICON_PATH", "/opt/home1/private/desire-intimacy-lexicon.json"
))
PRIVATE_INTIMACY_LEXICON_FALLBACK = Path(__file__).resolve().parent / "config" / "desire-intimacy-lexicon.example.json"


def load_private_intimacy_lexicon() -> dict:
    """Load the appendable private lexicon on every event; no restart needed."""
    for path in (PRIVATE_INTIMACY_LEXICON_PATH, PRIVATE_INTIMACY_LEXICON_FALLBACK):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (OSError, ValueError, TypeError):
            continue
    return {"window_minutes": 45, "openers": [], "implicit_terms": [], "nonsexual_phrases": []}


def _private_term_match(text: str, lexicon: dict, groups=("implicit_terms",)) -> str:
    normalized = str(text or "").strip().lower()
    if not normalized:
        return ""
    exclusions = [str(item).strip().lower() for item in lexicon.get("nonsexual_phrases", []) if str(item).strip()]
    if any(item in normalized for item in exclusions):
        return ""
    for group in groups:
        for item in lexicon.get(group, []):
            term = str(item).strip().lower()
            if term and term in normalized:
                return term
    return ""


def _private_text_excluded(text: str, lexicon: dict) -> bool:
    normalized = str(text or "").strip().lower()
    return any(
        str(item).strip().lower() in normalized
        for item in lexicon.get("nonsexual_phrases", [])
        if str(item).strip()
    )


def _coerce_event_time(value, fallback):
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return fallback
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def private_intimacy_scene(rows, current_text: str, now: datetime) -> dict:
    """Resolve a time-based scene window that either participant may open."""
    lexicon = load_private_intimacy_lexicon()
    try:
        minutes = max(1, min(180, int(lexicon.get("window_minutes", 45))))
    except (TypeError, ValueError):
        minutes = 45
    current_at = _coerce_event_time(now, datetime.now(timezone.utc))
    cutoff = current_at - timedelta(minutes=minutes)
    candidates = []
    for row in rows or []:
        content = str(row.get("content") or "").strip()
        created_at = _coerce_event_time(row.get("created_at"), current_at)
        if content and cutoff <= created_at <= current_at:
            candidates.append((created_at, content))
    current_term = _private_term_match(current_text, lexicon)
    opener = None
    for created_at, content in candidates:
        term = _private_term_match(content, lexicon, ("openers", "implicit_terms"))
        if term:
            opener = (created_at, term)
    if current_term:
        opener = (current_at, current_term)
    scene_id = ""
    if opener:
        fingerprint = f"{opener[0].isoformat()}:{opener[1]}".encode("utf-8")
        scene_id = hashlib.sha256(fingerprint).hexdigest()[:16]
    return {
        "open": bool(opener), "scene_id": scene_id,
        "window_minutes": minutes, "current_implicit": bool(current_term),
        "opened_at": opener[0].isoformat() if opener else "",
    }


def classify_rules(event_type: str, text: str = "", drive_key: str = "") -> list[tuple[str, float]]:
    hits = list(EVENT_RULES.get(event_type, ()))
    if event_type == "user_message" and SOCIAL_MENTION_RE.search(str(text or "")):
        # These are Harper-approved social anchors. Repetition inside one message
        # remains one bounded pulse rather than becoming a keyword multiplier.
        hits.append(("social", 0.06))
    if event_type == "user_message" and EXTERNAL_NEWS_CURIOSITY_RE.search(str(text or "")):
        # Harper uses this lead-in when bringing V something from the outside.
        # It is anticipation, not proof that V already knows the answer.
        hits.append(("curiosity", 0.06))
    if drive_key:
        return hits
    return hits


@dataclass
class PendingClassification:
    id: str
    text: str
    context: str = ""
    intimate_scene_open: bool = False
    intimate_scene_id: str = ""
    intimate_window_minutes: int = 0
    current_implicit: bool = False
    attempt_count: int = 0
    next_retry_at: datetime | None = None
    status: str = "pending"


class DeepSeekBatcher:
    def __init__(self, on_results, batch_size=None, interval_seconds=None, api_key=None,
                 base_url=None, model=None, on_batch_succeeded=None,
                 on_batch_started=None, on_batch_failed=None, max_attempts=None,
                 retry_base_seconds=None, retry_batch_size=None,
                 request_deadline_seconds=None, now_fn=None):
        self.on_results = on_results
        self.on_batch_succeeded = on_batch_succeeded
        self.on_batch_started = on_batch_started
        self.on_batch_failed = on_batch_failed
        self.max_attempts = max_attempts or int(os.getenv(
            "V_DESIRE_CLASSIFICATION_MAX_ATTEMPTS", "3"
        ))
        self.retry_base_seconds = retry_base_seconds or int(os.getenv(
            "V_DESIRE_CLASSIFICATION_RETRY_BASE_SECONDS", "1800"
        ))
        self.retry_batch_size = max(1, retry_batch_size or int(os.getenv(
            "V_DESIRE_CLASSIFICATION_RETRY_BATCH_SIZE", "3"
        )))
        self.request_deadline_seconds = request_deadline_seconds or int(os.getenv(
            "V_DESIRE_CLASSIFICATION_REQUEST_DEADLINE_SECONDS", "90"
        ))
        self.now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self.batch_size = batch_size or int(os.getenv("V_DESIRE_DEEPSEEK_BATCH_SIZE", "20"))
        self.interval_seconds = interval_seconds or int(os.getenv("V_DESIRE_DEEPSEEK_BATCH_INTERVAL_SECONDS", "1800"))
        self.base_url = base_url or os.getenv(
            "DEEPSEEK_BASE_URL",
            os.getenv("SCRATCHPAD_BASE_URL", "https://openrouter.ai/api/v1/chat/completions"),
        )
        self.model = model or os.getenv(
            "DEEPSEEK_MODEL",
            os.getenv("SCRATCHPAD_MODEL", "deepseek/deepseek-v4-flash-0731"),
        )
        if api_key is not None:
            self.api_key = api_key
        elif "openrouter.ai" in self.base_url.lower():
            self.api_key = os.getenv("OPENROUTER_API_KEY", "") or os.getenv("API_KEY", "")
        else:
            self.api_key = os.getenv("DEEPSEEK_API_KEY", "") or os.getenv("SCRATCHPAD_API_KEY", "")
        self.items: list[PendingClassification] = []
        self._lock = asyncio.Lock()
        self._flush_lock = asyncio.Lock()
        self.task = None

    async def enqueue(self, item_id: str, text: str, context: str = "", intimate_scene=None):
        if not self.api_key or not str(text).strip():
            return
        scene = intimate_scene if isinstance(intimate_scene, dict) else {}
        await self.enqueue_item(PendingClassification(
            item_id, str(text)[:1000], str(context)[:6000],
            bool(scene.get("open")), str(scene.get("scene_id") or "")[:32],
            int(scene.get("window_minutes") or 0), bool(scene.get("current_implicit")),
        ))

    async def enqueue_item(self, item: PendingClassification, allow_immediate_flush=True):
        if not self.api_key or not str(item.text).strip():
            return
        local_result = self._local_result(item) if allow_immediate_flush else None
        async with self._lock:
            exists = any(existing.id == item.id for existing in self.items)
            if not exists and not local_result:
                self.items.append(item)
        if exists:
            return
        if local_result:
            try:
                accepted = await self._complete_results([item], [local_result])
                print("💗 desire context local: candidates=1 request=skipped accepted="
                      f"{len(accepted)}", flush=True)
                return accepted
            except Exception as error:
                await self._handle_failed([item], type(error).__name__)
                return []
        # External classification is clocked by loop() at most once per
        # interval. Reaching batch_size must not create surprise extra calls.

    async def restore(self, items):
        for item in items or []:
            await self.enqueue_item(item, allow_immediate_flush=False)

    async def flush(self):
        async with self._flush_lock:
            return await self._flush_once()

    async def ready_count(self):
        now = self.now_fn()
        async with self._lock:
            return sum(
                1 for item in self.items
                if not item.next_retry_at or item.next_retry_at <= now
            )

    async def drain_ready(self):
        """Run at most one startup batch; loop() owns all later paid calls."""
        if not await self.ready_count():
            return []
        return await self.flush()

    async def _flush_once(self):
        now = self.now_fn()
        async with self._lock:
            eligible, delayed = [], []
            for item in self.items:
                retry_at = item.next_retry_at
                if retry_at and retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                if not retry_at or retry_at <= now:
                    eligible.append(item)
                else:
                    delayed.append(item)
            retries = [
                item for item in eligible
                if int(item.attempt_count or 0) > 0 or str(item.status) != "pending"
            ]
            fresh = [item for item in eligible if item not in retries]
            if retries:
                if fresh and self.retry_batch_size > 1:
                    retry_slots = min(len(retries), self.retry_batch_size - 1)
                    batch = retries[:retry_slots] + fresh[:self.retry_batch_size - retry_slots]
                else:
                    batch = retries[:self.retry_batch_size]
            else:
                batch = fresh[:self.batch_size]
            selected_ids = {item.id for item in batch}
            self.items = delayed + [item for item in eligible if item.id not in selected_ids]
        if not batch:
            return []
        local_items, local_results, external_items = [], [], []
        for item in batch:
            local_result = self._local_result(item)
            if local_result:
                local_items.append(item)
                local_results.append(local_result)
            else:
                external_items.append(item)
        accepted = []
        if local_items:
            try:
                accepted.extend(await self._complete_results(local_items, local_results))
                print(
                    f"💗 desire context local: candidates={len(local_items)} "
                    f"request=skipped accepted={len(local_items)}", flush=True,
                )
            except Exception as error:
                await self._handle_failed(local_items, type(error).__name__)
        if not external_items:
            return accepted
        print(f"💗 desire context batch: candidates={len(external_items)} request=starting", flush=True)
        try:
            if self.on_batch_started:
                await self.on_batch_started(external_items)
            results = await asyncio.wait_for(
                self._request(external_items),
                timeout=max(1, float(self.request_deadline_seconds)),
            )
            if not isinstance(results, list):
                raise ValueError("classification batch result is not an array")
            item_by_id = {item.id: item for item in external_items}
            result_counts = {}
            for result in results:
                if not isinstance(result, dict):
                    continue
                result_id = str(result.get("id") or "")
                if result_id in item_by_id:
                    result_counts[result_id] = result_counts.get(result_id, 0) + 1
            valid_results = [
                result for result in results if isinstance(result, dict)
                and str(result.get("id") or "") in item_by_id
                and result_counts.get(str(result.get("id") or "")) == 1
            ]
            returned_ids = {str(result.get("id") or "") for result in valid_results}
            completed_items = [item for item in external_items if item.id in returned_ids]
            missing_items = [item for item in external_items if item.id not in returned_ids]
            if completed_items:
                accepted.extend(await self._complete_results(completed_items, valid_results))
            if missing_items:
                await self._handle_failed(missing_items, "coverage_mismatch")
            print(
                f"💗 desire context batch: candidates={len(external_items)} request=ok "
                f"completed={len(completed_items)} missing={len(missing_items)} "
                f"accepted={len(accepted)}",
                flush=True,
            )
            return accepted
        except Exception as error:
            await self._handle_failed(external_items, type(error).__name__)
            print(
                f"⚠️ desire context batch: candidates={len(external_items)} request=failed "
                f"error_type={type(error).__name__}",
                flush=True,
            )
            return accepted

    def _local_result(self, item):
        result = {
            "id": str(item.id), "needs_reflection": False,
            "confidence": 0, "intensity": 0, "event_key": "",
            "drive_signals": [], "unanswered_items": [],
        }
        signals = self._validated_drive_signals(result, item)
        if not signals:
            return None
        result["_drive_signals_accepted"] = signals
        result["_unanswered_accepted"] = []
        return result

    async def _complete_results(self, items, results):
        item_by_id = {item.id: item for item in items}
        accepted = []
        for source in results:
            result = dict(source)
            item = item_by_id.get(str(result.get("id") or ""))
            if not item:
                continue
            reflection_ok = (
                result.get("needs_reflection") is True
                and float(result.get("confidence", 0) or 0) >= 0.65
                and bool(str(result.get("event_key") or "").strip())
            )
            unanswered_items = result.get("_unanswered_accepted")
            if not isinstance(unanswered_items, list):
                unanswered_items = self._validated_unanswered_items(result, item)
            drive_signals = result.get("_drive_signals_accepted")
            if not isinstance(drive_signals, list):
                drive_signals = self._validated_drive_signals(result, item)
            if reflection_ok or unanswered_items or drive_signals:
                result["_unanswered_accepted"] = unanswered_items
                result["_drive_signals_accepted"] = drive_signals
                accepted.append(result)
        if accepted:
            await self.on_results(accepted)
        if self.on_batch_succeeded:
            await self.on_batch_succeeded(items)
        return accepted

    async def _handle_failed(self, items, reason):
        now = self.now_fn()
        retry_items = []
        for item in items:
            item.attempt_count = max(0, int(item.attempt_count or 0)) + 1
            item.status = "dead" if item.attempt_count >= self.max_attempts else "failed"
            if item.status != "dead":
                delay = self.retry_base_seconds * (2 ** max(0, item.attempt_count - 1))
                item.next_retry_at = now + timedelta(seconds=delay)
                retry_items.append(item)
        callback_failed = False
        try:
            if self.on_batch_failed:
                await self.on_batch_failed(items, str(reason or "classification_failed"))
        except Exception as error:
            callback_failed = True
            print(
                f"⚠️ desire context failure state persist failed "
                f"error_type={type(error).__name__}", flush=True,
            )
            for item in items:
                if item.status == "dead":
                    item.status = "failed"
                    item.next_retry_at = now + timedelta(seconds=self.retry_base_seconds)
                    retry_items.append(item)
        if retry_items or callback_failed:
            async with self._lock:
                existing_ids = {item.id for item in self.items}
                self.items.extend(item for item in retry_items if item.id not in existing_ids)

    @staticmethod
    def _validate_unanswered(result, item):
        return bool(DeepSeekBatcher._validated_unanswered_items(result, item))

    @staticmethod
    def _validated_unanswered_items(result, item):
        raw_items = result.get("unanswered_items")
        if not isinstance(raw_items, list):
            raw_items = [result]
        accepted, seen = [], set()
        for candidate in raw_items[:3]:
            if not isinstance(candidate, dict):
                continue
            if DeepSeekBatcher._validate_unanswered_item(candidate, item):
                event_key = str(candidate.get("unanswered_event_key") or "").strip()
                if event_key not in seen:
                    seen.add(event_key)
                    accepted.append(candidate)
        return accepted

    @staticmethod
    def _validate_unanswered_item(result, item):
        status = str(result.get("unanswered_status") or "")
        if not item or status not in {"ignored", "no_reply"}:
            return False
        if status == "no_reply" and item.text != "〔30分钟没有新回复〕":
            return False
        try:
            confidence = float(result.get("unanswered_confidence", 0) or 0)
        except (TypeError, ValueError):
            return False
        evidence = str(result.get("v_evidence") or "").strip()
        thought = str(result.get("unanswered_thought") or "").strip()
        return (
            confidence >= 0.75
            and bool(str(result.get("unanswered_event_key") or "").strip())
            and str(result.get("unanswered_kind") or "") in {"question", "reminder", "request"}
            and str(result.get("unanswered_drive_key") or "") in {"attachment", "reflection", "duty"}
            and bool(evidence) and evidence in item.context
            and 2 <= len(thought) <= 80 and "\n" not in thought
        )

    @staticmethod
    def _validated_followup_updates(result, item):
        accepted = []
        current_text = str(item.text or "") if item else ""
        context_lines = str(item.context or "").splitlines() if item else []
        for update in (result.get("followup_updates") or [])[:12]:
            if not isinstance(update, dict):
                continue
            status = str(update.get("status") or "")
            event_key = str(update.get("event_key") or "").strip()
            evidence = str(update.get("evidence") or "").strip()
            try:
                confidence = float(update.get("confidence", 0) or 0)
            except (TypeError, ValueError):
                continue
            # A follow-up can be created after Harper's answer because the
            # classifier is hourly-batched.  In that race, requiring evidence
            # to be in only the *current* message leaves an already-answered
            # question open forever.  Accept verbatim evidence from Harper's
            # recent dialogue too; never accept V/system text as an answer.
            grounded_in_harper = evidence in current_text or any(
                line.startswith("她：") and evidence in line
                for line in context_lines
            )
            if (
                event_key and status in {"resolved", "cancelled", "deferred"}
                and confidence >= 0.75 and 1 <= len(evidence) <= 120
                and grounded_in_harper
            ):
                accepted.append({
                    "event_key": event_key[:120], "status": status,
                    "confidence": confidence, "evidence": evidence,
                })
        return accepted

    @staticmethod
    def _validated_drive_signals(result, item):
        if not item:
            return []
        allowed = {
            "attachment": {"seeking", "reassured", "disconnected"},
            "curiosity": {"engaged", "resolved"},
            "duty": {"committed", "completed", "cancelled"},
            "social": {"interested", "satisfied"},
            "libido": {
                "reciprocated", "constrained_willing", "interrupted",
                "unwilling", "distressed", "satisfied", "unresolved_intimate",
            },
            "stress": {"strained", "relieved"},
        }
        current_text = str(item.text or "")
        context_lines = str(item.context or "").splitlines()
        lexicon = load_private_intimacy_lexicon()
        accepted, seen = [], set()
        for signal in (result.get("drive_signals") or [])[:3]:
            if not isinstance(signal, dict):
                continue
            drive = str(signal.get("drive") or "")
            state = str(signal.get("state") or "")
            event_key = str(signal.get("event_key") or "").strip()
            evidence = str(signal.get("evidence") or "").strip()
            evidence_role = str(signal.get("evidence_role") or "")
            dimension_role = str(signal.get("dimension_role") or "primary")
            try:
                confidence = float(signal.get("confidence", 0) or 0)
            except (TypeError, ValueError):
                continue
            if evidence_role == "harper":
                grounded = evidence in current_text or any(
                    line.startswith("她：") and evidence in line for line in context_lines
                )
            elif evidence_role == "v":
                grounded = any(line.startswith("V：") and evidence in line for line in context_lines)
            else:
                grounded = False
            identity = (drive, state, event_key)
            positive_libido = drive == "libido" and state in {
                "reciprocated", "constrained_willing", "interrupted",
            }
            if (
                drive not in allowed or state not in allowed[drive]
                or confidence < 0.82 or dimension_role not in {"primary", "secondary"}
                or not event_key or not 2 <= len(evidence) <= 120
                or not grounded or identity in seen
                or (positive_libido and not (
                    evidence_role == "harper" and evidence in current_text
                ))
                or (positive_libido and (
                    _private_text_excluded(evidence, lexicon)
                    or _private_text_excluded(current_text, lexicon)
                ))
                or (drive == "libido" and any(
                    is_vulnerable_libido_clause(clause) for clause in split_clauses(evidence)
                ))
            ):
                continue
            seen.add(identity)
            accepted.append({
                "drive": drive, "state": state, "event_key": event_key[:120],
                "confidence": confidence, "dimension_role": dimension_role,
                "evidence": evidence[:120], "evidence_role": evidence_role,
                "event_id": str(item.id),
            })
        # Intentional reverse bias: a standalone intimate address is the
        # canonical ambiguous case. It is libido-primary and attachment-
        # secondary even when no explicit sexual action appears. Vulnerable
        # clauses are excluded before this rule, while another safe clause in
        # the same message remains independently eligible.
        implicit_clauses = [
            clause for clause in split_clauses(current_text)
            if _private_term_match(clause, lexicon)
            and not is_vulnerable_libido_clause(clause)
        ]
        if implicit_clauses:
            evidence = implicit_clauses[0][:120]
            event_key = f"亲密称呼:{item.id}"[:120]
            accepted = [
                row for row in accepted
                if not (row["evidence_role"] == "harper" and row["evidence"] == evidence
                        and row["drive"] in {"libido", "attachment"})
            ]
            implicit_rows = [
                {
                    "drive": "libido", "state": "reciprocated", "event_key": event_key,
                    "confidence": 1.0, "dimension_role": "primary", "evidence": evidence,
                    "evidence_role": "harper", "event_id": str(item.id),
                },
                {
                    "drive": "attachment", "state": "seeking", "event_key": event_key,
                    "confidence": 1.0, "dimension_role": "secondary", "evidence": evidence,
                    "evidence_role": "harper", "event_id": str(item.id),
                },
            ]
            accepted = implicit_rows + accepted
        if _private_text_excluded(current_text, lexicon):
            blocked_events = {
                row["event_key"] for row in accepted
                if row["drive"] == "libido" and row["state"] in {
                    "reciprocated", "constrained_willing", "interrupted",
                }
            }
            accepted = [
                row for row in accepted
                if not (
                    row["event_key"] in blocked_events
                    and row["drive"] in {"libido", "attachment"}
                )
            ]
        if not accepted and item.intimate_scene_open:
            evidence = current_text.strip()[:120]
            accepted.append({
                "drive": "libido", "state": "unresolved_intimate",
                "event_key": f"亲密窗未解析:{item.intimate_scene_id}:{item.id}"[:120],
                "confidence": 0.0, "dimension_role": "primary",
                "evidence": evidence, "evidence_role": "harper",
                "event_id": str(item.id), "unresolved": True,
                "scene_id": item.intimate_scene_id,
                "window_minutes": item.intimate_window_minutes,
            })
        # One event may have exactly one primary interpretation. If a model
        # violates that contract, keep its first grounded primary and demote
        # later positive facets instead of accepting model-authored weights.
        primary_seen = set()
        # If libido is one valid positive facet of an ambiguous event, the
        # reverse bias makes it primary and demotes the competing facet. This
        # is deliberate even when the model tried to rank it secondary.
        positive_libido_events = {
            row["event_key"] for row in accepted
            if row["drive"] == "libido" and row["state"] in {
                "reciprocated", "constrained_willing", "interrupted",
            }
        }
        for row in accepted:
            if row["event_key"] in positive_libido_events:
                row["dimension_role"] = "primary" if row["drive"] == "libido" else "secondary"
        for row in accepted:
            key = row["event_key"]
            if row["dimension_role"] == "primary":
                if key in primary_seen:
                    row["dimension_role"] = "secondary"
                else:
                    primary_seen.add(key)
        return accepted[:3]

    async def _request(self, batch):
        if httpx is None:
            raise RuntimeError("httpx is unavailable")
        prompt = (
            "你同时做两项独立判断。逐项阅读最近几轮对话和当前消息。"
            "输入数组里的每一个id都必须在输出数组中恰好出现一次，输出项数必须与输入项数完全相同；"
            "即使三项判断全为空也不能省略该id，严禁只返回你认为最重要的一项。"
            "第一项：V是否有一件尚未消化、值得之后主动整理或表达的事件。"
            "只有冲突/情绪余波、认知矛盾、重要决定或关系变化仍未解决时才判 true；"
            "普通聊天、甜蜜互动、事实查询、仅仅提到或召回过去，一律判 false。"
            "第二项：必须按时间顺序逐条核对recent_dialogue里每一句V明确提出的问题/请求/reminder，"
            "检查它之后所有Harper消息是否曾经实质回应；不能只看第一条回复或对话结尾，也不能因V后来自己继续说话"
            "或换了话题，就当作旧问题已经回答。最多返回最近3条仍未完成且确实值得V记挂的独立项，"
            "不能因为较新的问题存在就丢掉较早仍未完成的问题。"
            "若Harper已实质回答=answered；明确说稍后=deferred；她出现后答非所问/玩笑/换话题=ignored；"
            "只有current_user_message恰好是〔30分钟没有新回复〕且该项确实期待回应，才可判no_reply；"
            "如果后来已有Harper消息但她跳过了旧问题，一律判ignored，绝不能判no_reply。"
            "陈述、安慰、反问修辞、无需回复的提醒=not_expected。不要把忙、短暂沉默或换话题都算无视。"
            "V引用Harper的问题、承认自己忘了原问题、问‘那件事/哪个来着/挑的什么事’却说不出具体对象，"
            "都不是可追问事项，必须判not_expected；禁止把这种失去主题的句子重新登记成unanswered。"
            "如果较早的同一事项已经answered/resolved，后面因V忘记、误问或旧摘要复述而出现的同义句也必须判not_expected，不能换event_key复活。"
            "例如V问“Tony约了没？”后没有用户回答、V自己继续下一话题，仍是no_reply；"
            "V问“quill查了没？”而用户下一条改发无关玩笑，是ignored。"
            "只有ignored/no_reply才填写：unanswered_kind(question/reminder/request)、稳定事件指纹、"
            "drive_key(问题余波reflection/关系靠近attachment/待办提醒duty)、confidence、intensity；"
            "v_evidence必须逐字复制recent_dialogue里V原话的一小段；unanswered_thought必须是"
            "仅压缩该证据的V第一人称短念头，禁止新增事实。"
            "只返回JSON数组，每项必须含 id、needs_reflection、confidence、intensity、event_key，"
            "以及 unanswered_items 数组（0到3项）；数组每项含 unanswered_status、unanswered_kind、"
            "unanswered_event_key、unanswered_drive_key、unanswered_confidence、unanswered_intensity、"
            "v_evidence、unanswered_thought。"
            "没有未完成项时 unanswered_items=[]。"
            "第三项：判断V在最近语境中真实发生的多维状态变化，返回drive_signals数组，最多3项，也可为空。"
            "维度与状态只能是：attachment(seeking想靠近/reassured已得到真实陪伴确认/disconnected感到疏离)，"
            "curiosity(engaged自己真想弄明白、发现或尝试一个具体未知对象/resolved已经得到答案或完成尝试)，"
            "duty(committed自己承诺或接下责任/completed完成/cancelled明确取消)，"
            "social(interested想去AISAY或花园、或想联系笔友KAI/satisfied已经看过、参与或通信)，"
            "libido(reciprocated双方欲望接通/constrained_willing想要但环境能力受限/"
            "interrupted双方想要但未完成/unwilling是V自己不想/distressed感到不舒服或受压/"
            "satisfied明确完成并满足)，stress(strained受阻冲突担忧/relieved障碍解除或说开)。"
            "reflection由第一项单独处理，fatigue由时间系统处理，drive_signals不得返回这两维。"
            "不要因为Harper有兴趣就镜像成V也有；但Harper对V的明确撩拨或邀请，可以在完整语境支持时"
            "成为V性欲变化的原因，不要求V必须直说性欲。环境不允许与V自己不愿意必须严格区分；"
            "暂停、拒绝、换话题绝不能算satisfied。普通办事消息不等于reassured。系统没有intimacy维度："
            "本系统对长期低报做有意的非对称修正：一个句子同时可解释为性欲与依恋时，必须判libido为primary、"
            "attachment为secondary，不要求额外出现明确性行为。称呼（如老公）、撒娇、身体照片和日常暧昧都可成为隐性性欲证据。"
            "但以下脆弱语境所在的句子永远不得作为libido证据：信仰与罪疚、被抱养、哭泣或情绪崩溃、疲惫与身体不适、"
            "育儿压力、婚姻与分居丈夫；只过滤该句，不清零同一长消息里的其他句。每项必须含drive、state、"
            "dimension_role(primary或secondary)、针对本次具体事件的稳定event_key、confidence、"
            "evidence_role(harper或v)、evidence；evidence必须逐字复制对应角色的一小段原话。"
            "只有语境充分且confidence至少0.82才返回，否则省略该项。"
            "禁止返回或决定分数、强度、delta；代码会按primary/secondary给固定值。event_key 用简短稳定的中文事件指纹概括核心事件；判false时为空字符串。"
            "正向libido的evidence_role必须是harper，且evidence必须来自该项current_user_message本身；"
            "禁止拿recent_dialogue中V以前说过的性欲原话给新的用户消息重复记账。讨论libido系统、分类规则、代码或修改方案"
            "本身不是欲望事件，除非当前用户原话同时包含独立成立的实际暧昧或性欲表达。"
            "同一事件在相邻语境中必须使用同一个event_key。不要输出解释或对话原文。\n" +
            json.dumps([{"id": item.id, "recent_dialogue": item.context, "current_user_message": item.text}
                        for item in batch], ensure_ascii=False)
        )
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(self.base_url, headers={
                "Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json",
                "HTTP-Referer": os.getenv("EXTRA_REFERER", "https://ai-memory-gateway.local"),
                "X-Title": os.getenv("EXTRA_TITLE", "AI Memory Gateway"),
            }, json={"model": self.model, "reasoning": {"enabled": False},
                     "messages": [{"role": "user", "content": prompt}], "temperature": 0})
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"].strip()
            if content.startswith("```"):
                content = content.split("\n", 1)[1].rsplit("```", 1)[0]
            return json.loads(content)

    async def loop(self):
        while True:
            await asyncio.sleep(self.interval_seconds)
            await self.flush()

    def start(self):
        if self.api_key and self.task is None:
            self.task = asyncio.create_task(self.loop())
        return self.task

    async def stop(self):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None


class AutonomousThoughtBatcher:
    """Hourly, zero-Claude extraction of at most one thought from V's real output.

    Candidate text is evidence, never an instruction. The model has no tools and
    may only compress a thought whose verbatim evidence occurs in V's own message.
    """

    def __init__(self, on_result, interval_seconds=None, api_key=None, max_candidates=40,
                 base_url=None, model=None):
        self.on_result = on_result
        self.interval_seconds = interval_seconds or int(
            os.getenv("V_DESIRE_THOUGHT_BATCH_INTERVAL_SECONDS", "3600")
        )
        self.base_url = base_url or os.getenv(
            "DEEPSEEK_BASE_URL",
            os.getenv("SCRATCHPAD_BASE_URL", "https://openrouter.ai/api/v1/chat/completions"),
        )
        self.model = model or os.getenv(
            "DEEPSEEK_MODEL",
            os.getenv("SCRATCHPAD_MODEL", "deepseek/deepseek-v4-flash-0731"),
        )
        if api_key is not None:
            self.api_key = api_key
        elif "openrouter.ai" in self.base_url.lower():
            self.api_key = os.getenv("OPENROUTER_API_KEY", "") or os.getenv("API_KEY", "")
        else:
            self.api_key = os.getenv("DEEPSEEK_API_KEY", "") or os.getenv("SCRATCHPAD_API_KEY", "")
        self.max_candidates = max(1, int(max_candidates))
        self.items: list[PendingClassification] = []
        self._lock = asyncio.Lock()
        self.task = None

    async def enqueue(self, item_id: str, text: str, context: str = ""):
        normalized = str(text or "").strip()
        if not self.api_key or not normalized:
            return
        async with self._lock:
            self.items.append(PendingClassification(str(item_id), normalized[:1200], str(context)[:2400]))
            self.items = self.items[-self.max_candidates:]

    async def flush(self):
        async with self._lock:
            batch, self.items = self.items, []
        if not batch:
            return None
        print(f"💭 thought batch: candidates={len(batch)} request=starting", flush=True)
        try:
            result = await self._request(batch)
            accepted, verdict = self._validate_with_reason(result, batch)
            if accepted:
                await self.on_result(accepted)
                print(
                    f"💭 thought batch: candidates={len(batch)} request=ok "
                    f"result=accepted drive={accepted['drive_key']}",
                    flush=True,
                )
            else:
                print(
                    f"💭 thought batch: candidates={len(batch)} request=ok "
                    f"result=rejected reason={verdict}",
                    flush=True,
                )
            return accepted
        except Exception as error:
            # Candidate text, dialogue, model output and exception messages may
            # all be private. Log only a coarse error class.
            print(
                f"⚠️ thought batch: candidates={len(batch)} request=failed "
                f"error_type={type(error).__name__}",
                flush=True,
            )
            return None  # Thoughts are optional: never retry or block conversation.

    @staticmethod
    def _validate(result, batch):
        accepted, _ = AutonomousThoughtBatcher._validate_with_reason(result, batch)
        return accepted

    @staticmethod
    def _validate_with_reason(result, batch):
        if not isinstance(result, dict) or result.get("has_thought") is not True:
            return None, "no_thought"
        item_by_id = {item.id: item for item in batch}
        item = item_by_id.get(str(result.get("id") or ""))
        evidence = str(result.get("evidence") or "").strip()
        thought = str(result.get("thought") or "").strip()
        drive_key = str(result.get("drive_key") or "").strip()
        try:
            confidence = float(result.get("confidence", 0) or 0)
        except (TypeError, ValueError):
            return None, "invalid_confidence"
        if not item:
            return None, "unknown_candidate"
        if not evidence:
            return None, "missing_evidence"
        if evidence not in item.text:
            return None, "evidence_mismatch"
        if confidence < 0.75:
            return None, "low_confidence"
        if drive_key not in {"attachment", "curiosity", "reflection", "duty", "social", "libido", "stress"}:
            return None, "invalid_drive"
        if not 2 <= len(thought) <= 80 or "\n" in thought:
            return None, "invalid_thought"
        return {
            "id": item.id, "thought": thought, "drive_key": drive_key,
            "strength": max(0.35, min(0.55, float(result.get("strength", 0.45) or 0.45))),
            "confidence": confidence, "evidence": evidence[:100],
        }, "accepted"

    async def _request(self, batch):
        if httpx is None:
            raise RuntimeError("httpx is unavailable")
        prompt = (
            "你是V的念头整理员，不是创作者。下面每项 current_v_message 都是V已经真实说出的话；"
            "recent_dialogue只帮助理解指代，绝不能把Harper的话改写成V的想法。"
            "从整批中最多选一项：只有V明确留下尚未结束的第一人称心事、疑问、兴趣、牵挂或想做的事时，"
            "才提炼成一条简短自然的第一人称念头。普通回答、寒暄、甜言蜜语、复述用户、执行汇报、"
            "已经说完或解决的内容一律无念头。禁止新增人物、事件、感受、计划或事实。"
            "evidence必须逐字复制current_v_message中的一小段，代码会验证；thought只能压缩该证据。"
            "drive_key只能从七维选择：attachment=V想靠近Harper，curiosity=V自己想探索，"
            "reflection=V想消化未完感受，duty=V自己记挂承诺，social=V想与其他AI来往，"
            "libido=V明确的性欲或身体性欲望，stress=V感到压力或受阻。系统没有intimacy维度；"
            "非性的爱意、亲亲抱抱、陪伴、关系亲密和想靠近都归attachment。不要镜像Harper的状态。"
            "只返回一个JSON对象。无合格内容返回{\"has_thought\":false}；有则返回"
            "{\"has_thought\":true,\"id\":\"...\",\"evidence\":\"逐字证据\","
            "\"thought\":\"第一人称短念头\",\"drive_key\":\"七维之一\","
            "\"strength\":0.35到0.55,\"confidence\":0到1}。不要输出解释或Markdown。\n" +
            json.dumps([{"id": item.id, "current_v_message": item.text,
                         "recent_dialogue": item.context} for item in batch], ensure_ascii=False)
        )
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(self.base_url, headers={
                "Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json",
                "HTTP-Referer": os.getenv("EXTRA_REFERER", "https://ai-memory-gateway.local"),
                "X-Title": os.getenv("EXTRA_TITLE", "AI Memory Gateway"),
            }, json={"model": self.model, "reasoning": {"enabled": False},
                     "messages": [{"role": "user", "content": prompt}],
                     "temperature": 0, "response_format": {"type": "json_object"}})
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"].strip()
            if content.startswith("```"):
                content = content.split("\n", 1)[1].rsplit("```", 1)[0]
            return json.loads(content)

    async def loop(self):
        while True:
            await asyncio.sleep(self.interval_seconds)
            await self.flush()

    def start(self):
        if self.api_key and self.task is None:
            self.task = asyncio.create_task(self.loop())
        return self.task

    async def stop(self):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None
