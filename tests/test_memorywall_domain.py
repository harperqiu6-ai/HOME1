from datetime import datetime, timezone

from memorywall_domain import compose_content, extract_body, row_to_item, summary_is_valid


def test_memorywall_round_trip_keeps_summary_separate_from_body():
    content = compose_content("一天", "完整正文", "xiaoke", "安心", "2026-08-30", "我记住了今天。")
    assert content == "【回忆 · 2026-08-30 · V · 安心】一天\n\n〔检索摘要〕我记住了今天。\n\n完整正文"
    assert extract_body(content) == "完整正文"


def test_v_summary_requires_first_person_plain_complete_text():
    assert summary_is_valid("我记住了今天。", "xiaoke")
    assert not summary_is_valid("V记住了今天。", "xiaoke")
    assert not summary_is_valid("# 我记住了今天。", "xiaoke")
    assert not summary_is_valid("我记住了今天", "xiaoke")


def test_row_mapping_prefers_structured_meta_and_normalizes_dates():
    item = row_to_item({
        "id": 7,
        "content": "【回忆 · 2026-08-30 · V】旧标题\n\n旧正文",
        "event_date": "2026-08-29",
        "created_at": datetime(2026, 8, 30, tzinfo=timezone.utc),
        "mw_meta": {"title": "结构标题", "body": "结构正文", "author": "xiaoke", "pinned": 1},
    })
    assert item["title"] == "结构标题"
    assert item["body"] == "结构正文"
    assert item["author_cn"] == "V"
    assert item["date"] == "2026-08-29"
    assert item["pinned"] is True
