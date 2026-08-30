"""Pure formatting and validation rules for the memory-wall boundary."""

from __future__ import annotations

import re


MW_AUTHOR_CN = {
    "ruanruan": "Harper",
    "xiaoke": "V",
    "v": "V",
    "harper": "Harper",
}
MW_SUMMARY_THRESHOLD = 400
MW_SUMMARY_MAX_WORDS = 150


def compose_content(title, body, author, mood, created_at, summary):
    author_cn = MW_AUTHOR_CN.get(author, author or "")
    header = f"【回忆 · {str(created_at)[:10]} · {author_cn}" + (f" · {mood}" if mood else "") + "】"
    parts = [header + (title or "")]
    if summary:
        parts.append(f"〔检索摘要〕{summary}")
    if body:
        parts.append(body)
    return "\n\n".join(part for part in parts if part and part.strip())


def extract_body(content):
    parts = (content or "").split("\n\n")
    rest = parts[1:]
    if rest and rest[0].startswith("〔检索摘要〕"):
        rest = rest[1:]
    return "\n\n".join(rest)


def summary_is_valid(summary, author):
    """V-authored searchable cards must stay first-person, plain and complete."""
    value = str(summary or "").strip()
    if not value or "\n" in value or _word_count(value) > MW_SUMMARY_MAX_WORDS:
        return False
    if re.search(r"(^|\s)#{1,6}\s|[*_`]|用户", value):
        return False
    if not re.search(r"[。！？.!?]$", value):
        return False
    if str(author or "").strip().lower() in {"xiaoke", "v"} and "我" not in value:
        return False
    return True


def summary_fallback(_body, _author):
    """An arbitrary opening sentence must never masquerade as a generated digest."""
    return ""


def row_to_item(row):
    meta = row.get("mw_meta") or {}
    event_date = str(row["event_date"]) if row.get("event_date") else None
    return {
        "id": row["id"],
        "title": row.get("title") or meta.get("title") or "",
        "body": meta.get("body") or extract_body(row.get("content") or ""),
        "summary": meta.get("summary") or "",
        "author": meta.get("author"),
        "author_cn": meta.get("author_cn") or MW_AUTHOR_CN.get(meta.get("author"), meta.get("author")),
        "mood": meta.get("mood"),
        "source": meta.get("source"),
        "is_period_day": meta.get("is_period_day"),
        "location": meta.get("location"),
        "date": event_date or meta.get("date") or (str(row.get("created_at")) if row.get("created_at") else None),
        "event_date": event_date,
        "importance": row.get("importance"),
        "is_active": row.get("is_active"),
        "pinned": bool(meta.get("pinned")),
        "photos": row.get("photos", []),
    }


def _word_count(value):
    return len(re.findall(r"[\u4e00-\u9fff]|[A-Za-z0-9]+", str(value or "")))
