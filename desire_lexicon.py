"""Safe, auditable edits for HOME1's private desire lexicon."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path


LEXICON_PATH = Path(os.getenv(
    "V_DESIRE_PRIVATE_LEXICON_PATH",
    "/opt/home1/private/desire-intimacy-lexicon.json",
))
AUDIT_PATH = Path(os.getenv(
    "V_DESIRE_LEXICON_AUDIT_PATH",
    "/opt/home1/private/desire-intimacy-lexicon.audit.jsonl",
))
ALLOWED_GROUPS = ("openers", "implicit_terms", "nonsexual_phrases")
ALLOWED_ACTORS = ("harper", "v")
MAX_TERM_CHARS = 80
MAX_TERMS_PER_GROUP = 500
_EDIT_LOCK = threading.Lock()


class LexiconError(ValueError):
    """A safe, user-facing lexicon validation error."""


def _clean_term(value) -> str:
    term = str(value or "").strip()
    if not term:
        raise LexiconError("词不能为空")
    if len(term) > MAX_TERM_CHARS:
        raise LexiconError(f"单个词不能超过 {MAX_TERM_CHARS} 个字符")
    if any(ord(char) < 32 for char in term):
        raise LexiconError("词里不能包含换行或控制字符")
    return term


def _clean_group(value) -> str:
    group = str(value or "").strip()
    if group not in ALLOWED_GROUPS:
        raise LexiconError("只能修改开场词、亲密词或排除短语")
    return group


def _clean_actor(value) -> str:
    actor = str(value or "").strip().lower()
    if actor not in ALLOWED_ACTORS:
        raise LexiconError("修改人只能是 Harper 或 V")
    return actor


def _normalized_lexicon(data) -> dict:
    if not isinstance(data, dict):
        raise LexiconError("词表文件格式不正确")
    result = dict(data)
    try:
        result["window_minutes"] = max(1, min(180, int(data.get("window_minutes", 45))))
    except (TypeError, ValueError):
        result["window_minutes"] = 45
    for group in ALLOWED_GROUPS:
        raw = data.get(group, [])
        if not isinstance(raw, list):
            raise LexiconError(f"{group} 必须是词语列表")
        values = []
        seen = set()
        for item in raw:
            term = _clean_term(item)
            folded = term.casefold()
            if folded not in seen:
                seen.add(folded)
                values.append(term)
        if len(values) > MAX_TERMS_PER_GROUP:
            raise LexiconError(f"{group} 最多保存 {MAX_TERMS_PER_GROUP} 个词")
        result[group] = values
    return result


def read_lexicon(path: Path = LEXICON_PATH) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise LexiconError("私有词表文件还不存在") from error
    except (OSError, json.JSONDecodeError) as error:
        raise LexiconError("私有词表暂时无法读取或 JSON 格式有误") from error
    return _normalized_lexicon(data)


def read_audit(limit: int = 30, path: Path = AUDIT_PATH) -> list[dict]:
    safe_limit = max(1, min(100, int(limit or 30)))
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    except OSError:
        return []
    records = []
    for line in lines[-safe_limit:]:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            records.append(item)
    return records


def snapshot(audit_limit: int = 30, lexicon_path: Path = LEXICON_PATH,
             audit_path: Path = AUDIT_PATH) -> dict:
    data = read_lexicon(lexicon_path)
    return {
        "window_minutes": data["window_minutes"],
        "groups": {group: list(data[group]) for group in ALLOWED_GROUPS},
        "audit": read_audit(audit_limit, audit_path),
    }


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent,
        prefix=f".{path.name}.", suffix=".tmp", delete=False,
    )
    temp_path = Path(handle.name)
    try:
        with handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, path)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def _append_audit(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        payload = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(path, 0o600)


def mutate(action: str, group: str, term: str, actor: str, reason: str = "",
           lexicon_path: Path = LEXICON_PATH, audit_path: Path = AUDIT_PATH) -> dict:
    operation = str(action or "").strip().lower()
    if operation not in ("add", "remove"):
        raise LexiconError("操作只能是 add 或 remove")
    safe_group = _clean_group(group)
    safe_term = _clean_term(term)
    safe_actor = _clean_actor(actor)
    safe_reason = str(reason or "").strip()[:160]

    with _EDIT_LOCK:
        data = read_lexicon(lexicon_path)
        values = list(data[safe_group])
        matches = [index for index, value in enumerate(values) if value.casefold() == safe_term.casefold()]
        changed = False
        if operation == "add":
            if not matches:
                if len(values) >= MAX_TERMS_PER_GROUP:
                    raise LexiconError(f"这个分组最多保存 {MAX_TERMS_PER_GROUP} 个词")
                values.append(safe_term)
                changed = True
        elif matches:
            values.pop(matches[0])
            changed = True

        if changed:
            data[safe_group] = values
            _atomic_write_json(lexicon_path, data)
        record = {
            "at": datetime.now(timezone.utc).isoformat(),
            "actor": safe_actor,
            "action": operation,
            "group": safe_group,
            "term": safe_term,
            "changed": changed,
            "reason": safe_reason,
        }
        _append_audit(audit_path, record)
        return {
            "ok": True,
            "changed": changed,
            "record": record,
            "window_minutes": data["window_minutes"],
            "groups": {name: list(data[name]) for name in ALLOWED_GROUPS},
        }
