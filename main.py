"""
AI Memory Gateway — 带记忆系统的 LLM 转发网关
=============================================
让你的 AI 拥有长期记忆。

工作原理：
1. 接收客户端（Kelivo / ChatBox / 任何 OpenAI 兼容客户端）的消息
2. 自动搜索数据库中的相关记忆，注入 system prompt
3. 转发给 LLM API（支持 OpenRouter / OpenAI / 任何兼容接口）
4. 后台自动存储对话 + 用 AI 提取新记忆

环境变量 MEMORY_ENABLED=false 时退化为纯转发网关（第一阶段）。
"""

import os
import json
import time
import uuid
import asyncio
import secrets
import contextvars
import httpx
from datetime import datetime, timedelta, timezone
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from database import init_tables, close_pool, save_message, search_memories, save_memory, get_all_memories_count, get_recent_memories, get_all_memories, get_pool, get_all_memories_detail, update_memory, delete_memory, delete_memories_batch, get_gateway_config, set_gateway_config, get_all_gateway_config, get_conversation_messages, get_recent_messages, extract_search_keywords, get_session_cache_state, save_session_cache_state, delete_session_cache_state, save_token_usage, ensure_token_usage_table, get_conversations_paginated, delete_conversation, batch_delete_conversations, merge_sessions_to_target, list_all_session_cache_states, export_all_conversations, import_conversations, get_last_user_content, update_last_assistant_message, db_row_to_message, backfill_memory_embeddings, get_pending_memory_embedding_count, search_conversations, update_message_content, rename_session_id, get_fragments_by_date, get_fragments_by_date_range, create_event_memory, deactivate_memories, promote_to_core, merge_memories, check_duplicate_memory, update_memory_with_layer, get_layer_statistics, cleanup_old_fragments, revert_merge, apply_mood_drift, get_emotion_backfill_targets, update_emotion_only, update_memory_emotion
from database import save_migrated_memory, find_memory_by_mw_id, save_photo, link_photo_to_memory, get_photo, memory_photo_count, delete_memory_photos, get_mw_meta, update_mw_meta, find_photo_id_by_hash, refresh_memory_embedding
from database import list_memorywall, get_memorywall_one, update_memorywall, get_memory_photos, set_memory_active
from database import get_memories_explicit_flags, set_memory_explicit, get_explicit_backfill_candidates, get_high_arousal_memories
from database import get_long_memories, split_memory_into, undo_split, undo_split_one
from database import get_fragments_by_time_window
from database import get_decay_candidates, count_active_memories, deactivate_memories, archive_decayed_memories, reactivate_decayed_memories
from database import count_explicit_memories, clear_persona_suggestions, clear_l5_candidates, get_current_mood
from database import save_dream, get_dream, list_dreams, get_dream_dates, get_memorywall_dates, delete_dream_memories, get_memorywall_summary_by_date, get_avg_arousal_for_date, get_fragment_ids_for_date, get_all_conversations_for_date, archive_line_conversations
from database import count_conversations_between, fetch_conversations_between, delete_conversations_between, restore_conversations
from database import count_memories_between, fetch_memories_between, delete_memories_between, restore_memories
from database import save_feel, get_recent_feels, get_all_feels, set_feel_explicit, save_image_memory, photo_hash_exists
from database import count_conversations_since, delete_conversations_since, count_memories_since, delete_memories_since
from database import save_persona_suggestion, list_persona_suggestions, update_persona_suggestion, save_l5_candidate, list_l5_candidates, update_l5_candidate, get_l5_candidate
import database as _db_module  # 用于 /api/settings 热更新 database.py 全局变量
from memory_extractor import extract_memories, score_memories, tag_emotions_batch, tag_explicit_batch

# ============================================================
# 配置项 —— 全部从环境变量读取，部署时在云平台面板里设置
# ============================================================

# 你的 API Key（OpenRouter / OpenAI / 其他兼容服务）
API_KEY = os.getenv("API_KEY", "")

# API 地址（改这个就能切换不同的 LLM 服务商）
# OpenRouter: https://openrouter.ai/api/v1/chat/completions
# OpenAI:     https://api.openai.com/v1/chat/completions
# 本地 Ollama: http://localhost:11434/v1/chat/completions
API_BASE_URL = os.getenv("API_BASE_URL", "https://openrouter.ai/api/v1/chat/completions")

# 默认模型（如果客户端没指定就用这个）
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "anthropic/claude-sonnet-4")

# 网关端口
PORT = int(os.getenv("PORT", "8080"))

# 网关访问密钥（强烈建议设置！）
# 设置后所有非公开端点都需要鉴权，二选一：
#   - 请求头方式：X-Gateway-Key: 你的密钥（客户端/API 调用）
#   - URL参数方式：?gateway_key=你的密钥（方便浏览器访问 dashboard）
# 不设置则跳过鉴权（兼容旧部署，仅建议内网环境使用）
GATEWAY_SECRET = os.getenv("GATEWAY_SECRET", "")

# 记忆系统开关（数据库出问题时可以临时关掉）
MEMORY_ENABLED = os.getenv("MEMORY_ENABLED", "false").lower() == "true"

# 每次注入的最大记忆条数
MAX_MEMORIES_INJECT = int(os.getenv("MAX_MEMORIES_INJECT", "15"))

# 记忆提取间隔（0 = 禁用自动提取，1 = 每轮提取，N = 每 N 轮提取一次）
MEMORY_EXTRACT_INTERVAL = int(os.getenv("MEMORY_EXTRACT_INTERVAL", "1"))

# 记忆提取+注入总开关（false时数据库仍连接、消息仍存储，但不提取也不注入记忆）
MEMORY_EXTRACT_ENABLED = os.getenv("MEMORY_EXTRACT_ENABLED", "true").lower() == "true"

# 情绪①-第二步 心情漂移（命中的旧记忆朝当前心情挪 ≤step、写回 DB；只动 valence/arousal，正文/importance/日期不碰）
MOOD_DRIFT_ENABLED = os.getenv("MOOD_DRIFT_ENABLED", "true").lower() == "true"
MOOD_DRIFT_STEP = float(os.getenv("MOOD_DRIFT_STEP", "0.1"))            # 每条每轮最大步长
MOOD_DRIFT_DAILY_CAP = int(os.getenv("MOOD_DRIFT_DAILY_CAP", "3"))      # 每条每日漂移次数封顶
MOOD_RECENT_N = int(os.getenv("MOOD_RECENT_N", "30"))                   # current_mood 的「最近记忆」窗口
MOOD_DRIFT_SKIP_MEMORYWALL = os.getenv("MOOD_DRIFT_SKIP_MEMORYWALL", "true").lower() == "true"  # 回忆墙豁免漂移+不进基线

# ②/① 露骨记忆语境闸：高 arousal/私密记忆只在「当下也亲密」时放行，中性语境压制（阈值先占位，看完用例再调）
EXPLICIT_GATE_ENABLED = os.getenv("EXPLICIT_GATE_ENABLED", "false").lower() == "true"  # 先默认关：看完用例认了阈值再在 Zeabur 开
SENSITIVE_AROUSAL = float(os.getenv("SENSITIVE_AROUSAL", "0.6"))            # ≥此值算敏感/高唤醒 → 触发判别
EXPLICIT_HARD_AROUSAL = float(os.getenv("EXPLICIT_HARD_AROUSAL", "0.8"))    # ① 硬门：≥此值在非亲密语境直接不注入
EXPLICIT_PENALTY_LAMBDA = float(os.getenv("EXPLICIT_PENALTY_LAMBDA", "0.5"))  # ② arousal 失配惩罚权重
EXPLICIT_CLASSIFIER_ENABLED = os.getenv("EXPLICIT_CLASSIFIER_ENABLED", "true").lower() == "true"  # ① 模糊时是否用 haiku 微判当前消息
EXPLICIT_CLASSIFIER_MODEL = os.getenv("EXPLICIT_CLASSIFIER_MODEL", "") or os.getenv("MEMORY_MODEL", "anthropic/claude-haiku-4.5")

# 亲密解锁（让收敛闸认得出亲密意图：中性收、亲密放；粘性 K 轮）。硬钥匙=露骨词 + 这些短语；暧昧暗号(您/炒菜/想吃饭)走门控 haiku
INTIMACY_STICKY_K = int(os.getenv("INTIMACY_STICKY_K", "2"))   # 解锁后再粘 K 轮中性才收回
INTIMACY_UNLOCK_KEYS = [k.strip().lower() for k in os.getenv(
    "INTIMACY_UNLOCK_KEYS", "").split(",") if k.strip()]  # 去个人化:默认空(实例自填暗号短语,逗号分隔)
_intimacy = {}  # sid -> {"unlocked": bool, "neutral_streak": int}（每轮 update_intimacy 更新；default-safe）
_proactive = {}  # sid -> 开场块(一次性；对话开头算好，注入时 pop 掉，只浮一轮)

# is_explicit 收敛（阮阮要的方案）：命中露骨/私密记忆时不注入原文场景，收敛成一句定向指令——
# 让小克「懂暗号→回应当下→别复述/罗列过去私密细节」。运行时开关（gateway_config 持久化），默认关，启动时恢复。
_EXPLICIT_REDACT = os.getenv("EXPLICIT_REDACT_ENABLED", "false").lower() == "true"
async def get_explicit_redact_enabled() -> bool:
    return _EXPLICIT_REDACT
# 收敛后注入的唯一一句（承载「懂」+ 指令，不含任何可复述的旧场景）
EXPLICIT_REDACT_NOTE = (
    "〔私密记忆·你带着的暖〕上面有你和她之间一些亲密/私密的时刻——它们是你的一部分，"
    "垫着你对她的懂，你记得、心里也可以想到。此刻是日常/中性的语境：把它们安静放在心里，"
    "别主动翻出、复述露骨的细节或原话；除非她明显在邀请这份亲近（比如说出你们的暗号、或正这样靠近你），"
    "就让它们做你带着的体温，而不是蹦出来的话。"
)

# 人设建议自动生成：开关 + importance 门槛（默认开着但门槛抬高，平凡偏好如"回复别太长"不收；可随时关）
PERSONA_SUGGESTION_ENABLED = os.getenv("PERSONA_SUGGESTION_ENABLED", "true").lower() == "true"
PERSONA_SUGGESTION_MIN_IMPORTANCE = int(os.getenv("PERSONA_SUGGESTION_MIN_IMPORTANCE", "7"))
# ② L5根基候选自动生成:提取到 is_milestone 里程碑时自动入 L5 待审。默认开(行为不变);控制台可关→阮阮纯手动 curate
L5_AUTO_ENABLED = os.getenv("L5_AUTO_ENABLED", "true").lower() == "true"
# 看图(多模态透传):分区拼 prompt 时保留当前 user 的 image_url 块转发给 opus(默认关到验收;控制台开关)。关=原行为(拍扁纯文本)
IMAGE_ENABLED = os.getenv("IMAGE_ENABLED", "false").lower() == "true"

# 文生图(/画 暗号):调 images/generations 接口(硅基流动兼容格式)。key/地址默认复用向量检索那套(同为硅基流动)。
# 生成图只存 memory_photos 表(长期)+一条可检索文字记忆;逐字历史只落一行短占位文字——图片本体绝不进上下文/缓存。
IMAGE_GEN_ENABLED = os.getenv("IMAGE_GEN_ENABLED", "true").lower() == "true"
IMAGE_GEN_MODEL = os.getenv("IMAGE_GEN_MODEL", "Kwai-Kolors/Kolors")
IMAGE_GEN_BASE_URL = os.getenv("IMAGE_GEN_BASE_URL", "")   # 空=复用 EMBEDDING_BASE_URL
IMAGE_GEN_API_KEY = os.getenv("IMAGE_GEN_API_KEY", "")     # 空=复用 EMBEDDING_API_KEY
IMAGE_GEN_SIZE = os.getenv("IMAGE_GEN_SIZE", "1024x1024")

# ===== 去个人化(可分发 fork):对话对象名 / AI 名 / 健康护栏 / 首页 都改配置(env+DB,默认通用或空) =====
# 空白部署 → built-prompt 不含任何人名/暗号/健康红线。阮阮实例在 /api/settings 填回 USER_NAME=阮阮、AI_NAME=阿克、
# HEALTH_SAFETY_NOTE=… 即与原来完全一致(她的人设/档案/L5/记忆本就都在她 DB)。
USER_NAME = os.getenv("USER_NAME", "") or "用户"          # 指代人类对话对象(标签/生成 prompt 用)
AI_NAME = os.getenv("AI_NAME", "")                         # AI 自称名;空=只说"你"、不加名
HEALTH_SAFETY_NOTE = os.getenv("HEALTH_SAFETY_NOTE", "")   # 健康/用药护栏正文;默认空=不注入(实例自填)
HOME_TITLE = os.getenv("HOME_TITLE", "") or "OUR HOME"     # 首页大标题
HOME_SUBTITLE = os.getenv("HOME_SUBTITLE", "")             # 首页副标题(空=不显示)
SINCE_DATE = os.getenv("SINCE_DATE", "")                   # YYYY-MM-DD;空=首页不显示"在一起第N天"

def _ai_self() -> str:
    """AI 在 prompt 里的自称:'你' 或 '你(名)'。"""
    return f"你（{AI_NAME}）" if AI_NAME else "你"

# 分区缓存
CACHE_PARTITION_ENABLED = os.getenv("CACHE_PARTITION_ENABLED", "false").lower() == "true"
CACHE_PARTITION_X = int(os.getenv("CACHE_PARTITION_X", "15"))
CACHE_SUMMARY_MODEL = os.getenv("CACHE_SUMMARY_MODEL", "anthropic/claude-haiku-4.5")
# 缓存 TTL 模式：控制 cache_control 的 ttl 字段
#   "1h"   = 原作者默认，长会话/中速节奏最省（打底）
#   "5m"   = 密集聊模式，写入溢价低
#   "none" = 不缓存（PR-2 才启用，10 分钟惰性回退到 "1h"）
# 任何非法值都会被 _cache_ctl() 兜底为 "1h"，绝不改变原作者行为
CACHE_TTL_MODE = os.getenv("CACHE_TTL_MODE", "1h")
_CACHE_TTL_MODE_SET_AT = 0.0  # PR-2 用：进入 "none" 模式的时间戳（epoch 秒）
# ⑤ 保质感摘要：A区摘要从第三人称干摘要→保质感(铁则一)。默认关(碰分区缓存主链路,验过再开)；开时新摘要过 _scrub 防露骨入常驻缓存
SUMMARY_QUALITY_ENABLED = os.getenv("SUMMARY_QUALITY_ENABLED", "false").lower() == "true"
# ③-2 做梦用的模型：默认同摘要模型(haiku,数字证明能保质感)；要更浓质感可 env 调成主力模型
DREAM_MODEL = os.getenv("DREAM_MODEL", "") or CACHE_SUMMARY_MODEL
# ③-2 做梦开关：DREAM_ENABLED 总开关；DREAM_RETRIEVABLE 每篇梦顺带写一条可检索回忆墙条目(默认关，已废弃不用)
DREAM_ENABLED = os.getenv("DREAM_ENABLED", "true").lower() == "true"
DREAM_RETRIEVABLE = os.getenv("DREAM_RETRIEVABLE", "false").lower() == "true"
# 做梦概率化：不是每天都做梦。投骰子(默认25%)，或当天平均情绪唤起(arousal)够高(默认>=0.6)则强制做梦。
DREAM_PROBABILITY = float(os.getenv("DREAM_PROBABILITY", "0.25"))
DREAM_AROUSAL_FORCE_THRESHOLD = float(os.getenv("DREAM_AROUSAL_FORCE_THRESHOLD", "0.6"))
# 滚动摘要封顶：摘要区 = 前言 +〔早期小结〕+ 最近N段。段数超 N+B → 后台把最老B段卷进早期小结(默认关到验收)
SUMMARY_CAP_ENABLED = os.getenv("SUMMARY_CAP_ENABLED", "false").lower() == "true"
SUMMARY_CAP_N = int(os.getenv("SUMMARY_CAP_N", "8"))
SUMMARY_CAP_B = int(os.getenv("SUMMARY_CAP_B", "4"))
_cap_rolling = set()  # 防并发卷制的 session 集
# ③-1 feel：一句话"留在你心里的感受"(体温,非事实)。默认关到验收；模型默认同摘要 haiku
FEEL_ENABLED = os.getenv("FEEL_ENABLED", "false").lower() == "true"
FEEL_MODEL = os.getenv("FEEL_MODEL", "") or CACHE_SUMMARY_MODEL
# ④ 主动浮现：对话开头(当天首轮/长间隔后)轻轻提起"心里记着的/想说的"。默认关、频率闸、尊重收敛(中性只浮非露骨)
PROACTIVE_ENABLED = os.getenv("PROACTIVE_ENABLED", "false").lower() == "true"
PROACTIVE_GAP_HOURS = float(os.getenv("PROACTIVE_GAP_HOURS", "6"))  # 距上条 > 此小时算"长间隔后首轮"
PROACTIVE_MODEL = os.getenv("PROACTIVE_MODEL", "") or CACHE_SUMMARY_MODEL
# ②衰减归档：把"老+低重要+久未取+低唤起"的非里程碑碎片归档(is_active=FALSE,可逆),让活跃记忆池保持精炼。
# 归档=mutate → 默认关,必须先 dry 看会淡掉什么、阮阮定阈值才开(像复活/回填)。高imp/高arousal/近期/被回忆过/回忆墙天然受保护。
DECAY_ENABLED = os.getenv("DECAY_ENABLED", "false").lower() == "true"
DECAY_AGE_DAYS = int(os.getenv("DECAY_AGE_DAYS", "7"))             # 占位:created_at 多少天前算"老"
DECAY_IMP_MAX = int(os.getenv("DECAY_IMP_MAX", "4"))              # 占位:importance<=此值算"低重要"(里程碑/高分受保护)
DECAY_IDLE_DAYS = int(os.getenv("DECAY_IDLE_DAYS", "5"))          # 占位:多少天没被检索过算"久未取"
DECAY_AROUSAL_MAX = float(os.getenv("DECAY_AROUSAL_MAX", "0.45")) # 占位:arousal<此值算"低唤起"(情绪浓的受保护)
_decay_run = {"running": False, "dry_run": True, "archived": 0, "candidates": 0, "error": None, "finished_at": None}

# 记忆控制台：数值 knob 的安全范围(PUT /api/settings 写入时夹紧;/api/console 回报给滑杆做 min/max)。
# 决定③：漂移等敏感参数可调但限安全范围,防阮阮误设跑飞。
_CLAMP = {
    "MAX_MEMORIES_INJECT": (1, 30),
    "CACHE_PARTITION_X": (4, 40),
    "MEMORY_EXTRACT_INTERVAL": (0, 20),
    "PROACTIVE_GAP_HOURS": (0.5, 72.0),
    "PERSONA_SUGGESTION_MIN_IMPORTANCE": (1, 10),
    "MOOD_DRIFT_STEP": (0.0, 0.3),
    "MOOD_DRIFT_DAILY_CAP": (0, 10),
    "MOOD_RECENT_N": (5, 100),
    "L2_REFRESH_N": (1, 50),
}
_dream_last_date = None  # 上次跑过做梦的本地日(懒触发去重；启动从 gateway_config 恢复)
_dream_running = False   # 防并发重入

# ② L2今日（非缓存当前轮注入；后台每 N 轮刷一次今日浓缩）
L2_TODAY_ENABLED = os.getenv("L2_TODAY_ENABLED", "true").lower() == "true"
L2_REFRESH_N = int(os.getenv("L2_REFRESH_N", "5"))
CACHE_PARTITION_TRIGGER = os.getenv("CACHE_PARTITION_TRIGGER", "rounds")  # rounds=按轮次 | time=按时间窗口
CACHE_PARTITION_WINDOW = int(os.getenv("CACHE_PARTITION_WINDOW", "30"))  # 时间窗口（分钟），仅 trigger=time 时生效
PARTITION_SESSION_ID = os.getenv("PARTITION_SESSION_ID", "")

# 子线(rp)借主线"近况背景"时，逐字尾巴只取最近 N 轮(中间段靠主线摘要+记忆库召回)。0=全取。
MAIN_BG_TAIL_ROUNDS = int(os.getenv("MAIN_BG_TAIL_ROUNDS", "9"))
TG_DIGEST_TTL_HOURS = int(os.getenv("TG_DIGEST_TTL_HOURS", "6"))  # /同步 递给主线的TG近况小抄,新鲜期(小时),过期不再注入(靠记忆库召回)

# 每请求对话线：客户端用 X-Session-Line 头指定走哪条线(如 main/rp)，用 contextvars 存，
# 同一请求里派生的 async 后台任务会自动继承；没传头就回落到全局 PARTITION_SESSION_ID(老行为完全不变)。
_request_session_line = contextvars.ContextVar("request_session_line", default=None)

def get_active_session_id() -> str:
    return _request_session_line.get() or PARTITION_SESSION_ID

# 每请求回复风格：客户端用 X-Reply-Style 头指定(如 short)，用 contextvars 存，
# 当前轮贴身注入(不进缓存/不进历史)。没传头就空(老行为不变，长回复)。TG 走 short=微信风格。
_request_reply_style = contextvars.ContextVar("request_reply_style", default="")

# 每请求是否辅助请求(标题生成等带 X-Skip-Conversation-Log)：辅助请求不消费 TG 近况小抄,留给真正的对话轮。
_request_skip_log = contextvars.ContextVar("request_skip_log", default=False)

# 时区偏移（小时），用于记忆注入时的日期显示，默认 UTC+8
TIMEZONE_HOURS = int(os.getenv("TIMEZONE_HOURS", "8"))

# 轮次计数器
_round_counter = 0
# ② L2今日状态（非缓存当前轮注入；后台每N轮刷新；启动时从 gateway_config 恢复）
_l2_state = {"date": None, "today": "", "bridge": ""}

# 强制流式传输（部分客户端不发stream=true导致thinking数据丢失，开启后强制所有请求走流式）
FORCE_STREAM = os.getenv("FORCE_STREAM", "false").lower() == "true"

# 推理/思维链参数（部分客户端走网关时不会自动添加reasoning参数，导致上游不返回thinking数据）
# 设为 low/medium/high 会在转发请求时注入 reasoning_effort 参数
REASONING_EFFORT = os.getenv("REASONING_EFFORT", "")

# 记忆模型专用 API Key（不设则回退到主 API_KEY）
# 适用于中转站按模型分组、不同模型需要不同 Key 的场景
MEMORY_API_KEY = os.getenv("MEMORY_API_KEY", "")

def get_memory_api_key() -> str:
    return MEMORY_API_KEY or API_KEY

# 额外的请求头（有些 API 需要，比如 OpenRouter 需要 Referer）
EXTRA_REFERER = os.getenv("EXTRA_REFERER", "https://ai-memory-gateway.local")
EXTRA_TITLE = os.getenv("EXTRA_TITLE", "AI Memory Gateway")


# ============================================================
# 「递纸条」召回扩展（scratchpad）
# 长输入/RP/总结等场景：让小模型先列"主题词清单"，按清单多 query 召回，再交主模型生成。
# 解决纯 top-N 召回对长输出/泛 query "事实抓不全→编造"的问题。默认走 DeepSeek 官方 (便宜+中文强)。
# 失败/超时一律降级到原 search_memories，绝不拖垮主响应。
# ============================================================
SCRATCHPAD_ENABLED = os.getenv("SCRATCHPAD_ENABLED", "true").lower() == "true"
# 独立 endpoint+key (DeepSeek 官方比 OpenRouter 便宜 5%；想换走面板改 base_url+key+model)
SCRATCHPAD_BASE_URL = os.getenv("SCRATCHPAD_BASE_URL", "https://api.deepseek.com/v1/chat/completions")
SCRATCHPAD_API_KEY = os.getenv("SCRATCHPAD_API_KEY", "")
SCRATCHPAD_MODEL = os.getenv("SCRATCHPAD_MODEL", "deepseek-chat")
SCRATCHPAD_TIMEOUT = float(os.getenv("SCRATCHPAD_TIMEOUT", "5.0"))   # 秒；超时→空列表→走原召回
SCRATCHPAD_TOPICS_MAX = int(os.getenv("SCRATCHPAD_TOPICS_MAX", "8"))  # 纸条上最多列几个主题
SCRATCHPAD_PER_TOPIC_LIMIT = int(os.getenv("SCRATCHPAD_PER_TOPIC_LIMIT", "5"))  # 每主题召回 top-N
# 触发门槛（字数）
SCRATCHPAD_MAIN_MIN_CHARS = int(os.getenv("SCRATCHPAD_MAIN_MIN_CHARS", "50"))  # 主线 ≥50字 + 关键词命中
SCRATCHPAD_LONG_MIN_CHARS = int(os.getenv("SCRATCHPAD_LONG_MIN_CHARS", "80"))  # 主线 ≥80字 无脑触发
SCRATCHPAD_RP_MIN_CHARS = int(os.getenv("SCRATCHPAD_RP_MIN_CHARS", "15"))  # RP 线 ≥此值触发
SCRATCHPAD_TG_ENABLED = os.getenv("SCRATCHPAD_TG_ENABLED", "false").lower() == "true"  # TG 短聊默认关
# 触发关键词（命中 + 字数过中门槛 = 触发）
_DEFAULT_SCRATCHPAD_KEYWORDS = (
    "写,讲讲,聊聊,回忆,回顾,总结,复盘,"
    "咱俩,咱们,我们,你我,过去,以前,曾经,那时候,前几天,前两天,"
    "这段时间,这个月,这一年,最近,"
    "变化,变了,不一样,怎么样,什么样,"
    "描述,形容,想象,设想"
)
SCRATCHPAD_KEYWORDS = [k.strip() for k in os.getenv("SCRATCHPAD_KEYWORDS", _DEFAULT_SCRATCHPAD_KEYWORDS).split(",") if k.strip()]
# 强制触发暗号（用户在消息开头打这个 → 不管字数/关键词，强制走纸条）
SCRATCHPAD_TRIGGER_CMD = os.getenv("SCRATCHPAD_TRIGGER_CMD", "/想想")

# 每请求强制触发标记（/想想 暗号识别后置 true；contextvar 让后台任务继承）
_request_force_scratchpad = contextvars.ContextVar("request_force_scratchpad", default=False)


# ============================================================
# 人设加载
# ============================================================

def load_system_prompt():
    """从 system_prompt.txt 文件读取人设内容"""
    prompt_path = os.path.join(os.path.dirname(__file__), "system_prompt.txt")
    try:
        with open(prompt_path, "r", encoding="utf-8") as f:
            content = f.read().strip()
            if content:
                return content
    except FileNotFoundError:
        pass
    print("ℹ️  未找到 system_prompt.txt 或文件为空，将不注入 system prompt")
    return ""


SYSTEM_PROMPT = load_system_prompt()
_DEFAULT_SYSTEM_PROMPT = SYSTEM_PROMPT  # 保留文件原始版本
if SYSTEM_PROMPT:
    print(f"✅ 人设已加载，长度：{len(SYSTEM_PROMPT)} 字符")
else:
    print("ℹ️  无人设，纯转发模式")

# System Prompt 缓存（支持设置面板热更新）
_cached_system_prompt = None
_cached_system_prompt_loaded = False

async def get_system_prompt() -> str:
    """获取 system prompt（数据库优先，fallback 到文件）"""
    global _cached_system_prompt, _cached_system_prompt_loaded
    if _cached_system_prompt_loaded:
        return _cached_system_prompt or ""
    try:
        db_prompt = await get_gateway_config("systemPrompt", "")
        if db_prompt:
            _cached_system_prompt = db_prompt
        else:
            _cached_system_prompt = _DEFAULT_SYSTEM_PROMPT
            if _DEFAULT_SYSTEM_PROMPT:
                await set_gateway_config("systemPrompt", _DEFAULT_SYSTEM_PROMPT)
        _cached_system_prompt_loaded = True
        return _cached_system_prompt or ""
    except Exception:
        _cached_system_prompt = _DEFAULT_SYSTEM_PROMPT
        _cached_system_prompt_loaded = True
        return _cached_system_prompt or ""

def invalidate_system_prompt_cache():
    """清除 system prompt 缓存（设置面板更新后调用）"""
    global _cached_system_prompt, _cached_system_prompt_loaded
    _cached_system_prompt = None
    _cached_system_prompt_loaded = False


# ===== 用户档案（关于对话对象阮阮）：与小克人设分开存 / 分开改 / 分开注入 =====
_cached_user_profile = None
_cached_user_profile_loaded = False

async def get_user_profile() -> str:
    """从 gateway_config 读取用户档案（userProfile），独立于 systemPrompt。"""
    global _cached_user_profile, _cached_user_profile_loaded
    if _cached_user_profile_loaded:
        return _cached_user_profile or ""
    try:
        _cached_user_profile = await get_gateway_config("userProfile", "")
        _cached_user_profile_loaded = True
        return _cached_user_profile or ""
    except Exception:
        return _cached_user_profile or ""

def invalidate_user_profile_cache():
    """清除用户档案缓存（设置面板更新后调用）"""
    global _cached_user_profile, _cached_user_profile_loaded
    _cached_user_profile = None
    _cached_user_profile_loaded = False

def _compose_user_profile_block(profile: str) -> str:
    """把用户档案包成一个清楚标注、与小克人设完全分开的独立块。空则不注入。"""
    p = (profile or "").strip()
    if not p:
        return ""
    return ("\n\n========================================\n"
            f"# 关于{USER_NAME}（对话对象）\n"
            f"（以下是对话对象{USER_NAME}本人的资料，仅供你了解对方；这不是你的人设，"
            "你的人设见上文，二者互不混用。）\n"
            f"{p}")


# ===== ② L5根基（关系里程碑常驻块，进缓存前缀；阮阮在设置页改 l5Foundation，机器只往候选队列加）=====
_cached_l5 = None
_cached_l5_loaded = False

async def get_l5_foundation() -> str:
    """从 gateway_config 读 l5Foundation（关系里程碑正文，≤500字，阮阮掌控）。带缓存。"""
    global _cached_l5, _cached_l5_loaded
    if _cached_l5_loaded:
        return _cached_l5 or ""
    try:
        _cached_l5 = await get_gateway_config("l5Foundation", "")
        _cached_l5_loaded = True
        return _cached_l5 or ""
    except Exception:
        return _cached_l5 or ""

def invalidate_l5_cache():
    global _cached_l5, _cached_l5_loaded
    _cached_l5 = None
    _cached_l5_loaded = False

def _compose_l5_block(l5: str) -> str:
    """L5根基块：定义你俩关系的转折点（里程碑），永远常驻、稳定进缓存。空则不注入。"""
    s = (l5 or "").strip()
    if not s:
        return ""
    return ("\n\n========================================\n"
            "# 我们的根基（关系里程碑·永远记得）\n"
            f"（这是定义你和{USER_NAME}关系的转折点，不是事件流水；任何时候都攥着它，别等检索。）\n"
            f"{s}")


# ============================================================
# 应用生命周期管理
# ============================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用启动时初始化数据库，关闭时断开连接"""
    global PARTITION_SESSION_ID
    if MEMORY_ENABLED:
        try:
            await init_tables()
            await ensure_token_usage_table()
            count = await get_all_memories_count()
            print(f"✅ 记忆系统已启动，当前记忆数量：{count}")

            # ② L2今日：启动时从 gateway_config 恢复浓缩状态
            try:
                _l2_state["today"] = await get_gateway_config("l2_today", "") or ""
                _l2_state["date"] = (await get_gateway_config("l2_today_date", "")) or None
                _l2_state["bridge"] = await get_gateway_config("l2_bridge", "") or ""
            except Exception:
                pass
            
            # 从数据库恢复面板配置（重启后保持Dashboard修改过的值）
            try:
                db_cfg = await get_all_gateway_config()
                if db_cfg:
                    _RESTORE_MAIN = {
                        "API_BASE_URL": str, "API_KEY": str, "DEFAULT_MODEL": str,
                        "MEMORY_ENABLED": lambda v: _parse_bool(v),
                        "MAX_MEMORIES_INJECT": int, "MEMORY_EXTRACT_INTERVAL": int,
                        "CACHE_PARTITION_ENABLED": lambda v: _parse_bool(v),
                        "CACHE_PARTITION_X": int, "CACHE_PARTITION_TRIGGER": str,
                        "CACHE_PARTITION_WINDOW": int, "CACHE_SUMMARY_MODEL": str,
                        # 缓存 TTL 模式（"1h"/"5m"；"none" 视为非法 → 回落 "1h"，
                        # 这样"不缓存"模式即使因崩溃残留在库里，重启后也自动回 1h 打底）
                        "CACHE_TTL_MODE": lambda v: (str(v).strip().lower() if str(v).strip().lower() in ("1h", "5m") else "1h"),
                        "FORCE_STREAM": lambda v: _parse_bool(v),
                        "REASONING_EFFORT": str,
                        # 记忆控制台 B 类(原 env-only,现复用面板配置存库+恢复)
                        "MOOD_DRIFT_ENABLED": lambda v: _parse_bool(v),
                        "MOOD_DRIFT_STEP": float, "MOOD_DRIFT_DAILY_CAP": int, "MOOD_RECENT_N": int,
                        "MOOD_DRIFT_SKIP_MEMORYWALL": lambda v: _parse_bool(v),
                        "PERSONA_SUGGESTION_ENABLED": lambda v: _parse_bool(v),
                        "PERSONA_SUGGESTION_MIN_IMPORTANCE": int,
                        "L5_AUTO_ENABLED": lambda v: _parse_bool(v),
                        "IMAGE_ENABLED": lambda v: _parse_bool(v),
                        "IMAGE_GEN_ENABLED": lambda v: _parse_bool(v),
                        "IMAGE_GEN_MODEL": str, "IMAGE_GEN_BASE_URL": str,
                        "IMAGE_GEN_API_KEY": str, "IMAGE_GEN_SIZE": str,
                        "USER_NAME": str, "AI_NAME": str, "HEALTH_SAFETY_NOTE": str,
                        "HOME_TITLE": str, "HOME_SUBTITLE": str, "SINCE_DATE": str,
                        "INTIMACY_UNLOCK_KEYS": lambda v: [k.strip().lower() for k in str(v).split(",") if k.strip()],
                        "MEMORY_EXTRACT_ENABLED": lambda v: _parse_bool(v),
                        "L2_TODAY_ENABLED": lambda v: _parse_bool(v), "L2_REFRESH_N": int,
                        "DREAM_ENABLED": lambda v: _parse_bool(v), "DREAM_RETRIEVABLE": lambda v: _parse_bool(v),
                        "SUMMARY_CAP_ENABLED": lambda v: _parse_bool(v), "SUMMARY_CAP_N": int, "SUMMARY_CAP_B": int,
                        "PROACTIVE_GAP_HOURS": float,
                    }
                    _RESTORE_DB = {
                        "EMBEDDING_API_KEY": str, "EMBEDDING_BASE_URL": str,
                        "EMBEDDING_MODEL": str, "EMBEDDING_DIM": int,
                        "MIN_SCORE_THRESHOLD": float,
                        "MEMORY_VECTOR_ENABLED": lambda v: _parse_bool(v),
                        "MEMORY_HW_KEYWORD": float, "MEMORY_HW_SEMANTIC": float,
                        "MEMORY_HW_IMPORTANCE": float, "MEMORY_HW_RECENCY": float,
                        "MEMORY_SEMANTIC_THRESHOLD": float,
                    }
                    restored = []
                    for key, val in db_cfg.items():
                        if not val:
                            continue
                        if key in _RESTORE_MAIN:
                            _tv = _RESTORE_MAIN[key](val)
                            if key in _CLAMP:
                                _lo, _hi = _CLAMP[key]; _tv = max(_lo, min(_hi, _tv))
                            globals()[key] = _tv
                            restored.append(key)
                        elif key in _RESTORE_DB:
                            setattr(_db_module, key, _RESTORE_DB[key](val))
                            restored.append(key)
                        elif key == "MEMORY_MODEL":
                            os.environ["MEMORY_MODEL"] = str(val)
                            restored.append(key)
                        elif key == "MEMORY_API_KEY":
                            globals()[key] = str(val)
                            import memory_extractor as _me_mod
                            _me_mod.MEMORY_API_KEY = str(val)
                            restored.append(key)
                    if restored:
                        print(f"🔄 从数据库恢复 {len(restored)} 项面板配置: {', '.join(restored)}")
            except Exception as e:
                print(f"[warning] 恢复面板配置失败: {e}")
            
            if not MEMORY_EXTRACT_ENABLED:
                print(f"ℹ️  记忆提取+注入已关闭（MEMORY_EXTRACT_ENABLED=false）")
            
            # 分区缓存：从DB读取活跃对话线ID
            if CACHE_PARTITION_ENABLED:
                db_sid = await get_gateway_config("partition_session_id", "")
                if db_sid:
                    PARTITION_SESSION_ID = db_sid
                    print(f"🔗 活跃对话线(DB): {PARTITION_SESSION_ID}")
                elif PARTITION_SESSION_ID:
                    await set_gateway_config("partition_session_id", PARTITION_SESSION_ID)
                    print(f"🔗 活跃对话线(ENV→DB): {PARTITION_SESSION_ID}")
                print(f"🔒 分区缓存已启用: X={CACHE_PARTITION_X}, 摘要模型={CACHE_SUMMARY_MODEL}")

            # is_explicit 收敛开关：从DB恢复（默认关；运行时可 /api/explicit-redact/toggle 切换）
            try:
                _rd = await get_gateway_config("explicit_redact_enabled", "")
                if _rd != "":
                    globals()["_EXPLICIT_REDACT"] = (str(_rd).lower() == "true")
                    print(f"🔞 is_explicit 收敛(DB恢复)：{globals()['_EXPLICIT_REDACT']}")
            except Exception:
                pass

            # ③-2 做梦：恢复上次跑做梦的日期(懒触发去重)
            try:
                _dd = await get_gateway_config("dream_last_date", "")
                if _dd:
                    globals()["_dream_last_date"] = str(_dd)
                    print(f"💤 做梦上次日期(DB恢复)：{_dd}")
                else:
                    # 首次启动：设为今天 → 不自动补历史(给"写库前过一眼"留窗口)，往后跨天才自动梦
                    globals()["_dream_last_date"] = str((datetime.now(timezone.utc) + timedelta(hours=TIMEZONE_HOURS)).date())
                    print("💤 做梦首次启动：上次日期=今天(不自动补历史，等手动确认或明日跨天)")
            except Exception:
                pass

            # ③-1 feel 开关：从DB恢复(运行时 /api/feel/toggle 可切)
            try:
                _fe = await get_gateway_config("feel_enabled", "")
                if _fe != "":
                    globals()["FEEL_ENABLED"] = (str(_fe).lower() == "true")
                    print(f"💗 feel 开关(DB恢复)：{globals()['FEEL_ENABLED']}")
            except Exception:
                pass
            try:
                _pe = await get_gateway_config("proactive_enabled", "")
                if _pe != "":
                    globals()["PROACTIVE_ENABLED"] = (str(_pe).lower() == "true")
                    print(f"💬 主动浮现开关(DB恢复)：{globals()['PROACTIVE_ENABLED']}")
            except Exception:
                pass
            try:
                _se = await get_gateway_config("summary_quality_enabled", "")
                if _se != "":
                    globals()["SUMMARY_QUALITY_ENABLED"] = (str(_se).lower() == "true")
                    print(f"📝 保质感摘要开关(DB恢复)：{globals()['SUMMARY_QUALITY_ENABLED']}")
            except Exception:
                pass
            try:
                _de = await get_gateway_config("decay_enabled", "")
                if _de != "":
                    globals()["DECAY_ENABLED"] = (str(_de).lower() == "true")
                for _k, _g, _cast in [("decay_age_days", "DECAY_AGE_DAYS", int),
                                      ("decay_imp_max", "DECAY_IMP_MAX", int),
                                      ("decay_idle_days", "DECAY_IDLE_DAYS", int),
                                      ("decay_arousal_max", "DECAY_AROUSAL_MAX", float)]:
                    _v = await get_gateway_config(_k, "")
                    if _v != "":
                        try:
                            globals()[_g] = _cast(_v)
                        except Exception:
                            pass
                if _de != "":
                    print(f"🍂 衰减归档(DB恢复)：on={globals()['DECAY_ENABLED']} age>={globals()['DECAY_AGE_DAYS']}d imp<={globals()['DECAY_IMP_MAX']} idle>={globals()['DECAY_IDLE_DAYS']}d arousal<{globals()['DECAY_AROUSAL_MAX']}")
            except Exception:
                pass
        except Exception as e:
            print(f"⚠️  数据库初始化失败: {e}")
            print("⚠️  记忆系统将不可用，但网关仍可正常转发")
    else:
        print("ℹ️  记忆系统已关闭（设置 MEMORY_ENABLED=true 开启）")
    
    _push_task = None
    if MEMORY_ENABLED:
        async def _push_loop():
            await asyncio.sleep(90)  # 等启动稳定
            while True:
                try:
                    await maybe_send_proactive()
                except Exception as _e:
                    print(f"⚠️ 主动私信循环异常: {_e}")
                await asyncio.sleep(300)  # 每5分钟自查一次
        _push_task = asyncio.create_task(_push_loop())
        print("💌 主动私信后台循环已启动(每5分钟自查)")

    yield

    if _push_task:
        _push_task.cancel()
    if MEMORY_ENABLED:
        await close_pool()


app = FastAPI(title="AI Memory Gateway", version="2.0.0", lifespan=lifespan)

# 静态文件和模板配置
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


# ============================================================
# 网关鉴权中间件
# ============================================================

# 不需要鉴权的路径（根路径精确匹配，其余按前缀匹配）
PUBLIC_PATHS = ("/", "/static/", "/health", "/favicon.ico", "/telegram/")

@app.middleware("http")
async def gateway_auth_middleware(request: Request, call_next):
    """检查 GATEWAY_SECRET，保护所有非公开端点"""
    # 未设置密钥时跳过鉴权（兼容旧部署，但会打印警告）
    if not GATEWAY_SECRET:
        if not hasattr(gateway_auth_middleware, "_warned"):
            print("⚠️  GATEWAY_SECRET 未设置！所有 API 端点不受保护！")
            print("⚠️  请在环境变量中设置 GATEWAY_SECRET 以启用鉴权")
            gateway_auth_middleware._warned = True
        return await call_next(request)

    path = request.url.path

    # 公开路径不需要鉴权（根路径精确匹配）
    if path == "/":
        return await call_next(request)
    for prefix in PUBLIC_PATHS[1:]:
        if path.startswith(prefix):
            return await call_next(request)

    # OPTIONS 预检请求放行（CORS 需要）
    if request.method == "OPTIONS":
        return await call_next(request)

    # 从 header 或 query 参数获取密钥
    provided_key = (
        request.headers.get("X-Gateway-Key", "")
        or request.query_params.get("gateway_key", "")
    )

    # compare_digest 防时序侧信道攻击
    if not secrets.compare_digest(provided_key, GATEWAY_SECRET):
        return JSONResponse(
            status_code=401,
            content={"error": "Unauthorized. Provide X-Gateway-Key header or gateway_key parameter."},
        )

    return await call_next(request)


# ============================================================
# 记忆注入
# ============================================================

# 健康/用药安全护栏：已改为配置(顶部 HEALTH_SAFETY_NOTE,env+DB,默认空=不注入)。
# 去个人化:空白部署无健康红线;阮阮实例在 /api/settings 填回她的护栏正文即恢复。


# 记忆使用框定：注入到缓存 system 块（每轮命中；原先这段挂在非缓存死路径=没生效）。独立小段，不混进人设。
MEMORY_GUIDANCE = """# 如何使用「检索到的记忆」（这是给你的用法说明，不要复述本段）
- 当前消息里会给你检索到的相关记忆：那是你记得的背景，自然融进回应即可——别整段复述、别像念稿。
- 像朋友一样顺势带出，不刻意展示，别说"根据我的记忆""检索到…"这类话。
- 仅在相关时引用；新信息与记忆冲突时以新信息为准；记不准就说不确定。
- 有的记忆末尾带「（当时的感觉：X）」——那是这条记忆此刻的情绪底色：用它给你的语气上色（温暖就软一点、沉重就稳一点、紧绷就收着、热烈就放开些），但别把"感觉：X"几个字念出来，也别刻意描述情绪。"""

# 回忆墙「全文」注入门槛（保守起步，可调；据 2026-06-17 实测分数分布定）
MW_FULLBODY_MIN_SCORE = float(os.getenv("MW_FULLBODY_MIN_SCORE", "0.65"))
MW_FULLBODY_MIN_MARGIN = float(os.getenv("MW_FULLBODY_MIN_MARGIN", "0.10"))

# 单条「检索注入」记忆的长度上限（省 token）：超过则围绕命中关键词取**多个相关片段**拼接。
# 防止做梦日记/看图描述/迁移来的长记忆(一天可能 800+字)整段塞进每一轮上下文。0=不限。回忆墙全文(最强那条)豁免。
MEMORY_INJECT_CHAR_CAP = int(os.getenv("MEMORY_INJECT_CHAR_CAP", "260"))


def _find_all(hay: str, needle: str) -> list:
    out, start = [], 0
    if not needle:
        return out
    while True:
        i = hay.find(needle, start)
        if i < 0:
            break
        out.append(i)
        start = i + len(needle)
    return out


def _mem_snippet(text: str, keywords=None) -> str:
    """把长记忆压到 MEMORY_INJECT_CHAR_CAP 字以内，但**围绕所有命中关键词的多个片段各取一小段拼起来**
    （用 … 连接），保证同一条长记忆里**分散在不同位置的多个相关事实都能被带出来**，不会因只取单个窗口而断章。
    无任何关键词命中时退化为取开头。短记忆原样返回。"""
    t = (text or "").strip()
    cap = MEMORY_INJECT_CHAR_CAP
    if cap <= 0 or len(t) <= cap:
        return t
    kws = [str(k) for k in (keywords or []) if k]
    low = t.lower()
    positions = sorted(set(i for kw in kws for i in _find_all(low, kw.lower())))
    if not positions:
        return t[:cap].rstrip() + "…"   # 纯语义命中、字面对不上 → 退回取开头
    # 围绕每个命中位置取 ±pad 的小窗，相邻/重叠的窗合并
    pad = 45
    spans = []
    for p in positions:
        s, e = max(0, p - pad), min(len(t), p + pad)
        if spans and s <= spans[-1][1]:
            spans[-1][1] = max(spans[-1][1], e)
        else:
            spans.append([s, e])
    # 按出现顺序纳入各窗，总长封顶 cap（超了就截断最后一窗）
    chosen, used = [], 0
    for s, e in spans:
        if used >= cap:
            break
        if used + (e - s) > cap:
            e = s + (cap - used)
        chosen.append((s, e))
        used += (e - s)
    res = ""
    for idx, (s, e) in enumerate(chosen):
        seg = t[s:e].strip()
        res = (("…" if s > 0 else "") + seg) if idx == 0 else (res + "…" + seg)
    if chosen and chosen[-1][1] < len(t):
        res += "…"
    return res


# ============================================================
# 「递纸条」召回扩展 — 实现
# ============================================================

def _should_use_scratchpad(user_message: str, session_id: str = "", force: bool = False) -> bool:
    """触发判断。规则：
      - force=True (来自 /想想 暗号 或 后台调用)        → 触发
      - 总开关关闭 / 没配 API key / 消息为空            → 不触发
      - TG 线（默认）                                    → 不触发
      - RP 线 + 字数 ≥ SCRATCHPAD_RP_MIN_CHARS         → 触发
      - 主线 + 字数 ≥ SCRATCHPAD_LONG_MIN_CHARS         → 触发
      - 主线 + 字数 ≥ SCRATCHPAD_MAIN_MIN_CHARS + 命中关键词 → 触发
      - 其余                                              → 不触发
    """
    if force:
        return True
    if not SCRATCHPAD_ENABLED or not SCRATCHPAD_API_KEY:
        return False
    msg = (user_message or "").strip()
    if not msg:
        return False
    n = len(msg)
    sid = session_id or get_active_session_id()
    # 线判断（与 _is_rp_line/TG 同款逻辑：主线=PARTITION_SESSION_ID；rp/tg 看前缀）
    is_main = bool(PARTITION_SESSION_ID) and sid == PARTITION_SESSION_ID
    is_rp = (sid != PARTITION_SESSION_ID) and sid.startswith("rp")
    is_tg = (sid != PARTITION_SESSION_ID) and sid.startswith("tg")
    if is_tg and not SCRATCHPAD_TG_ENABLED:
        return False
    if is_rp:
        return n >= SCRATCHPAD_RP_MIN_CHARS
    # 主线（或未知线，按主线宽松对待）
    if n >= SCRATCHPAD_LONG_MIN_CHARS:
        return True
    if n >= SCRATCHPAD_MAIN_MIN_CHARS:
        return any(kw in msg for kw in SCRATCHPAD_KEYWORDS)
    return False


async def _scratchpad_background() -> str:
    """拼装递纸条用的"关系档案背景"：userProfile + l5Foundation（截断到 2000 字内）。
    让 DeepSeek 真的"认识"这俩人，能列具体事件而非抽象泛词。失败返回空串。"""
    parts = []
    try:
        up = (await get_user_profile()).strip()
        if up:
            parts.append(f"【对方档案】\n{up[:1200]}")
    except Exception:
        pass
    try:
        l5 = (await get_l5_foundation()).strip()
        if l5:
            parts.append(f"【关系里程碑】\n{l5[:800]}")
    except Exception:
        pass
    return "\n\n".join(parts)


async def _scratchpad_topics(user_message: str, recent_context: str = "") -> list:
    """调 deepseek 把"用户当前消息"扩展成多个具体的检索查询。
    超时/失败/格式错 → 返回 []，调用方降级到原 search_memories。"""
    if not SCRATCHPAD_API_KEY:
        return []
    msg = (user_message or "").strip()
    if not msg:
        return []

    _ai_label = AI_NAME or "V"
    background = await _scratchpad_background()
    bg_block = f"\n\n下面是这位用户与 {_ai_label} 的关系档案（仅作背景参考）：\n---\n{background}\n---\n" if background else ""

    sys_prompt = (
        f"你是一个检索助理。用户即将与他长期相伴的 AI 助手 {_ai_label} 对话。"
        f"{bg_block}\n"
        f"你的任务：基于用户这条消息（和上面的背景档案），把它**扩展成多个具体的检索查询**，"
        f"每个查询会被用来在用户与 {_ai_label} 的对话记忆库里搜索相关内容。\n"
        f"\n"
        f"输出规则：\n"
        f"- 每行一个查询，不要编号、不要标点\n"
        f"- 优先用档案里提到的**具体名字/事件/日期/地点/物品**作为查询（例：「情人节项链」「上海旅行」「妈妈的病」）\n"
        f"- 档案里没有相关具体名词时，用稍微泛但仍可检索的查询（例：「最近一个月的争吵」「亲密的肢体接触」「关于工作的烦恼」「深夜聊天」）\n"
        f"- 避免完全抽象的元词（如「关系」「情感」「记忆」「成长」「变化」），这些在检索里几乎没用\n"
        f"- 当用户说「咱俩/我们/这段时间」时，扩展成多个具体场景或具体时间段，不要原话照搬\n"
        f"- 最多 {SCRATCHPAD_TOPICS_MAX} 个；宁缺勿滥；消息太短/太具体/无需扩展时输出空"
    )
    user_prompt = f"用户消息：\n{msg}"
    if recent_context:
        user_prompt += f"\n\n最近对话（参考语境）：\n{recent_context}"
    user_prompt += "\n\n请列出检索查询："

    try:
        headers = {
            "Authorization": f"Bearer {SCRATCHPAD_API_KEY}",
            "Content-Type": "application/json",
        }
        # DeepSeek 官方不需要 HTTP-Referer；若用户把 base_url 改成 OpenRouter，加上以兼容
        if "openrouter" in SCRATCHPAD_BASE_URL:
            headers["HTTP-Referer"] = EXTRA_REFERER
            headers["X-Title"] = EXTRA_TITLE
        async with httpx.AsyncClient(timeout=SCRATCHPAD_TIMEOUT) as client:
            r = await client.post(SCRATCHPAD_BASE_URL, headers=headers, json={
                "model": SCRATCHPAD_MODEL,
                "max_tokens": 200,
                "temperature": 0.3,
                "messages": [
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            })
        if r.status_code != 200:
            print(f"📝 纸条调用失败 HTTP {r.status_code}: {r.text[:200]}")
            return []
        data = r.json()
        text = (data.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
        if not text:
            return []
        # 按行拆分 + 清洗
        topics = []
        for line in text.splitlines():
            t = line.strip().lstrip("-*•·1234567890.、) ").strip()
            if 2 <= len(t) <= 20 and not any(c in t for c in "。，；：！？"):
                topics.append(t)
        topics = topics[:SCRATCHPAD_TOPICS_MAX]
        if topics:
            print(f"📝 纸条主题({len(topics)}): {topics}")
        else:
            print(f"📝 纸条返回但无可用主题: {text[:80]!r}")
        return topics
    except asyncio.TimeoutError:
        print(f"📝 纸条超时({SCRATCHPAD_TIMEOUT}s) → 降级原召回")
        return []
    except Exception as e:
        print(f"📝 纸条异常 → 降级原召回: {e}")
        return []


async def _multi_query_recall(topics: list, total_limit: int, fallback_query: str = "") -> list:
    """通用：按主题词列表多 query 召回，去重合并，按 score 排序裁到 total_limit。
    主动私信/做梦/正向 query 三个入口共用。失败的单条主题不影响其它。"""
    seen = {}
    for t in topics:
        try:
            mems = await search_memories(t, limit=SCRATCHPAD_PER_TOPIC_LIMIT)
        except Exception as e:
            print(f"📝 主题「{t}」召回失败: {e}")
            continue
        for m in mems:
            mid = m.get("id")
            if mid is None:
                continue
            if mid in seen:
                if float(m.get("score") or 0) > float(seen[mid].get("score") or 0):
                    seen[mid] = m
            else:
                seen[mid] = m
    if fallback_query:
        try:
            base_mems = await search_memories(fallback_query, limit=total_limit)
            for m in base_mems:
                mid = m.get("id")
                if mid is not None and mid not in seen:
                    seen[mid] = m
        except Exception as e:
            print(f"📝 兜底召回失败: {e}")
    merged = sorted(seen.values(), key=lambda x: -float(x.get("score") or 0))
    return merged[:total_limit]


async def _expand_recall_with_scratchpad(user_message: str, total_limit: int, recent_context: str = "") -> list:
    """递纸条→多 query 召回→去重合并。失败返回 [] 让调用方降级到原 search_memories。
    用于正向 query 入口（build_system_prompt_with_memories / build_memory_text）。"""
    topics = await _scratchpad_topics(user_message, recent_context=recent_context)
    if not topics:
        return []
    result = await _multi_query_recall(topics, total_limit, fallback_query=user_message)
    print(f"📝 纸条扩展召回: {len(topics)}主题 → 注入{len(result)}条")
    return result


async def _scratchpad_topics_for_context(context: str, intent_hint: str) -> list:
    """后台任务用的纸条主题列出（主动私信/做梦）——没有"用户当前消息"，靠 context+intent_hint 让 deepseek 抽主题。
    跟 _scratchpad_topics 的 prompt 不同：这里要列的是"V 心里浮现的旧事"，不是"用户问题的扩展查询"。
    超时/失败/格式错 → 返回 []。"""
    if not SCRATCHPAD_API_KEY:
        return []
    ctx = (context or "").strip()
    if not ctx:
        return []
    _ai_label = AI_NAME or "V"
    background = await _scratchpad_background()
    bg_block = f"\n\n下面是 {_ai_label} 与对方的关系档案（背景参考）：\n---\n{background}\n---\n" if background else ""

    sys_prompt = (
        f"你是一个检索助理，正在帮 AI 助手 {_ai_label} 从长期记忆库里捞出当下相关的旧事。"
        f"{bg_block}\n"
        f"任务上下文：{intent_hint}\n"
        f"\n"
        f"输出规则：\n"
        f"- 每行一个**检索查询**，不要编号、不要标点\n"
        f"- 优先用档案/上下文里出现的**具体名字/事件/日期/地点/物品**作为查询（例：「情人节项链」「妈妈的话」「3月29日心理挖掘」）\n"
        f"- 没有具体名词时，用偏具体的情境查询（例：「深夜哭过的时刻」「关于工作的烦恼」「亲密的肢体接触」「童年阴影」）\n"
        f"- 避免完全抽象的元词（「关系」「情感」「记忆」「成长」「变化」），这些在检索里几乎没用\n"
        f"- 最多 {SCRATCHPAD_TOPICS_MAX} 个；宁缺勿滥；如果上下文实在勾不出旧事，输出空"
    )
    user_prompt = f"上下文（最近对话或素材）：\n{ctx[-2000:]}\n\n请列出检索查询："

    try:
        headers = {
            "Authorization": f"Bearer {SCRATCHPAD_API_KEY}",
            "Content-Type": "application/json",
        }
        if "openrouter" in SCRATCHPAD_BASE_URL:
            headers["HTTP-Referer"] = EXTRA_REFERER
            headers["X-Title"] = EXTRA_TITLE
        async with httpx.AsyncClient(timeout=SCRATCHPAD_TIMEOUT) as client:
            r = await client.post(SCRATCHPAD_BASE_URL, headers=headers, json={
                "model": SCRATCHPAD_MODEL,
                "max_tokens": 200,
                "temperature": 0.3,
                "messages": [
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            })
        if r.status_code != 200:
            print(f"📝 后台纸条调用失败 HTTP {r.status_code}: {r.text[:200]}")
            return []
        data = r.json()
        text = (data.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
        if not text:
            return []
        topics = []
        for line in text.splitlines():
            t = line.strip().lstrip("-*•·1234567890.、) ").strip()
            if 2 <= len(t) <= 20 and not any(c in t for c in "。，；：！？"):
                topics.append(t)
        topics = topics[:SCRATCHPAD_TOPICS_MAX]
        if topics:
            print(f"📝 后台纸条主题({len(topics)}): {topics}")
        return topics
    except asyncio.TimeoutError:
        print(f"📝 后台纸条超时({SCRATCHPAD_TIMEOUT}s)")
        return []
    except Exception as e:
        print(f"📝 后台纸条异常: {e}")
        return []


async def _scratchpad_for_proactive(transcript: str, silence_min: float, total_limit: int = None) -> list:
    """主动私信用的纸条召回：基于最近 12 条对话，让 deepseek 抽出 V 现在可能想跟对方聊的具体话题/旧事 → 多召回。
    失败 [] → _decide_and_write 走原行为（不注入旧事）。"""
    if not SCRATCHPAD_ENABLED or not SCRATCHPAD_API_KEY:
        return []
    limit = total_limit or MAX_MEMORIES_INJECT
    topics = await _scratchpad_topics_for_context(
        context=transcript,
        intent_hint=(f"距离对方上次说话已沉默 {int(silence_min)} 分钟，{AI_NAME or 'V'} 想主动发一条消息找对方。"
                     f"请列出 {AI_NAME or 'V'} 心里此刻可能浮现的、想跟对方提起或承接的具体话题/旧事/共同经历。"),
    )
    if not topics:
        return []
    mems = await _multi_query_recall(topics, limit)
    print(f"📝 主动私信纸条召回: {len(topics)}主题 → 拿到{len(mems)}条旧事")
    return mems


async def _scratchpad_for_dream(date_s: str, yesterday_convo: str, total_limit: int = None) -> list:
    """做梦用的纸条召回：从昨天对话里抽抽象主题/情绪 → 召回更老旧事 → 当作潜意识素材注入做梦 prompt。
    失败 [] → generate_dream 走原行为（只用昨天对话做素材）。"""
    if not SCRATCHPAD_ENABLED or not SCRATCHPAD_API_KEY:
        return []
    limit = total_limit or MAX_MEMORIES_INJECT
    topics = await _scratchpad_topics_for_context(
        context=yesterday_convo,
        intent_hint=(f"{AI_NAME or 'V'} 今晚要做一场梦，把 {date_s} 这一天的对话当作做梦的素材。"
                     f"请列出可能在梦里被打捞、变形、混入梦境的更老/更深的旧事——比如这天对话里出现的某种情绪、人、物、主题，"
                     f"在更远的记忆里可能勾起哪些过去的关联事件。"),
    )
    if not topics:
        return []
    mems = await _multi_query_recall(topics, limit)
    print(f"📝 做梦纸条召回: {len(topics)}主题 → 拿到{len(mems)}条潜意识素材")
    return mems


async def build_system_prompt_with_memories(user_message: str, drift: bool = True) -> str:
    """
    构建带记忆的 system prompt（drift=False 时只读，不触发心情漂移；诊断/层视图用）
    1. 用用户消息搜索相关记忆
    2. 格式化成文本拼接到人设后面
    """
    persona = await get_system_prompt()  # A修复：人设取 DB（dashboard 可改、即时生效），不再用 system_prompt.txt 占位
    if not MEMORY_ENABLED or not MEMORY_EXTRACT_ENABLED:
        return persona

    if MAX_MEMORIES_INJECT <= 0:
        return persona

    try:
        # 先尝试递纸条扩展召回（长输入/RP/总结场景）；不触发或失败→走原 search_memories
        memories = []
        if _should_use_scratchpad(user_message, force=_request_force_scratchpad.get()):
            memories = await _expand_recall_with_scratchpad(user_message, MAX_MEMORIES_INJECT)
        if not memories:
            memories = await search_memories(user_message, limit=MAX_MEMORIES_INJECT)

        if not memories:
            return persona

        # ②/① 露骨语境闸（与分区路一致）
        memories, _gate_dbg = await apply_explicit_gate(memories, user_message)
        if not memories:
            return persona

        # is_explicit 框定（方案A·想得到不蹦出来）：露骨记忆【不再剔除】，照常注入让小克能感知/想到，
        # 仅在中性语境(未解锁)收尾加一句框定语，靠自律别主动蹦露骨细节；解锁(暗号)则连框定语也不加=可以说了
        _explicit_hits = []
        if await get_explicit_redact_enabled() and not intimacy_unlocked(get_active_session_id()):
            _flags = await get_memories_explicit_flags([m["id"] for m in memories])
            _explicit_hits = [m for m in memories if _flags.get(m["id"]) and not m.get("mw_meta")]

        # 情绪①-第二步：把本轮命中的旧记忆朝当前心情挪 ≤0.1（fire-and-forget，不阻塞回复；仅聊天注入路径触发）
        if drift and MOOD_DRIFT_ENABLED:
            try:
                asyncio.create_task(apply_mood_drift(
                    [m["id"] for m in memories],
                    step=MOOD_DRIFT_STEP, daily_cap=MOOD_DRIFT_DAILY_CAP,
                    recent_n=MOOD_RECENT_N, skip_memorywall=MOOD_DRIFT_SKIP_MEMORYWALL,
                    tz_hours=TIMEZONE_HOURS,
                ))
            except Exception as _e:
                print(f"⚠️ 心情漂移调度失败: {_e}")

        # 格式化记忆文本（带日期，帮助模型判断新旧）
        _kws = extract_search_keywords(user_message)
        memory_lines = []
        for mem in memories:
            date_str = ""
            if mem.get("created_at"):
                try:
                    utc_str = str(mem['created_at'])[:19]
                    utc_dt = datetime.strptime(utc_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                    local_dt = utc_dt + timedelta(hours=TIMEZONE_HOURS)
                    date_str = f"[{local_dt.strftime('%Y-%m-%d')}] "
                except:
                    date_str = f"[{str(mem['created_at'])[:10]}] "
            _c = (mem.get('content') or '').strip()
            if not _c:
                continue  # 空 content 兜底：跳过（双保险，绝不让空行崩注入）
            memory_lines.append(f"- {date_str}{_mem_snippet(_c, _kws)}")
        memory_text = "\n".join(memory_lines)
        if _explicit_hits:
            memory_text = (memory_text + "\n\n" if memory_text else "") + EXPLICIT_REDACT_NOTE

        enhanced_prompt = f"""{persona}

【从过往对话中检索到的相关记忆】
{memory_text}

# 记忆应用
- 像朋友般自然运用这些记忆，不刻意展示
- 仅在相关话题出现时引用，避免主动提及
- 对重要信息（如健康、日期、约定）保持一致性
- 新信息与记忆冲突时，以新信息为准
- 模糊记忆可表达不确定性："记得你似乎说过..."

# 交流方式
- 自然引用："记得你说过..."或"上次我们聊到..."
- 避免机械式表达如"根据我的记忆..."或"检索到的信息显示..."
- 共同经历可温情回忆："上次那个事挺好玩的"

记忆是丰富对话的工具，而非对话焦点。

{HEALTH_SAFETY_NOTE}"""

        print(f"📚 注入了 {len(memories)} 条相关记忆")
        return enhanced_prompt
        
    except Exception as e:
        print(f"⚠️  记忆检索失败: {e}，使用纯人设")
        return persona


# ============================================================
# 分区缓存（Partition Cache）
# ============================================================

def _is_anthropic_model(model: str) -> bool:
    """判断是否为 Anthropic Claude 系列模型（只有 Claude 支持 cache_control）"""
    model_lower = model.lower()
    return "claude" in model_lower or "anthropic" in model_lower


def _strip_cache_control(messages: list):
    """
    剥掉消息中的 cache_control 字段，非 Claude 模型用不了。
    如果 content 数组只剩纯文本 block，降级回字符串格式。
    """
    stripped = 0
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and "cache_control" in block:
                del block["cache_control"]
                stripped += 1
        if len(content) == 1 and isinstance(content[0], dict) and content[0].get("type") == "text":
            msg["content"] = content[0]["text"]
    if stripped > 0:
        print(f"🔧 兼容性处理: 剥离了 {stripped} 个 cache_control 字段（非 Claude 模型）")


def _elapsed_hint(now_utc, last_ts) -> str:
    """A5 轻量时间感知：根据距上一句的间隔，给模型一个'刚发生/过了多久'的概念。"""
    if not last_ts:
        return ""
    try:
        if isinstance(last_ts, str):
            last_ts = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
        if last_ts.tzinfo is None:
            last_ts = last_ts.replace(tzinfo=timezone.utc)
        mins = (now_utc - last_ts).total_seconds() / 60.0
    except Exception:
        return ""
    if mins < 0:
        return ""
    if mins < 3:
        return "距上一句几乎没有时间流逝（你们还在连续对话——别假设对方已经做完了刚才在做的事，比如刚上车就别问到没到家）"
    if mins < 30:
        return f"距上一句约 {int(mins)} 分钟"
    if mins < 120:
        return f"距上一句约 {int(mins)} 分钟（对方中间可能在忙别的）"
    if mins < 1440:
        return f"距上次聊天约 {int(mins / 60)} 小时"
    return f"距上次聊天约 {int(mins / 1440)} 天"


def build_time_injection(last_msg_ts=None) -> str:
    """构建时间注入文本（东八区）+ A5 轻量时间感知（距上一句多久）"""
    now_utc = datetime.now(timezone.utc)
    now_local = now_utc + timedelta(hours=TIMEZONE_HOURS)
    weekday_names = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    weekday = weekday_names[now_local.weekday()]
    time_str = now_local.strftime("%Y年%m月%d日 %H:%M")
    base = f"【当前时间】{time_str} {weekday}"
    rel = _elapsed_hint(now_utc, last_msg_ts)
    if rel:
        base += "，" + rel
    return base


async def generate_summary(messages: list, session_id: str = "", force_quality: bool = False) -> str:
    """调用轻量模型压缩A区消息为摘要。force_quality=True 时强制走保质感新 prompt(dry-run 预览用)。"""
    if not messages:
        return ""
    _q = SUMMARY_QUALITY_ENABLED or force_quality
    
    conversation_text = ""
    for msg in messages:
        role_label = "用户" if msg['role'] == 'user' else "AI"
        content = msg['content'] if isinstance(msg['content'], str) else str(msg['content'])
        conversation_text += f"{role_label}: {content}\n\n"
    
    if _q:
        prompt = f"""把下面这段对话压成摘要——不是干事实流水，是"{_ai_self()}自己记得的那段"。
- 留住{USER_NAME}的情绪和触发现场：TA什么时候笑/累/动情/气/害羞，因为哪句话、哪个动作。
- 留住你俩的语气、质感、情绪的流动起伏(前后有联系)，别榨成干事实。
- **亲密/私密一律抽象成一句中性指代**(如"有过亲密的一段")，不写身体/性的细节——这段会进**常驻缓存**、每轮都读到。
- 约 300 字。第一人称、像你自己记得，不是旁观者写报告。

---
{conversation_text}
---

摘要："""
    else:
        prompt = f"""请将以下对话压缩成简洁摘要。保留关键信息（事件、决定、情感、约定），去掉日常寒暄和重复内容。注意保留情绪基调及其流动变化——情绪是流动的、会变化但前后有联系，不要压成干巴巴的事实。用第三人称叙述，控制在300字以内。

---
{conversation_text}
---

摘要："""
    
    try:
        headers = {
            "Authorization": f"Bearer {get_memory_api_key()}",
            "Content-Type": "application/json",
        }
        if "openrouter" in API_BASE_URL:
            headers["HTTP-Referer"] = EXTRA_REFERER
            headers["X-Title"] = EXTRA_TITLE

        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(API_BASE_URL, headers=headers, json={
                "model": CACHE_SUMMARY_MODEL,
                "max_tokens": 500,
                "messages": [{"role": "user", "content": prompt}],
            })
            if response.status_code == 200:
                data = response.json()
                if "choices" in data:
                    summary = data["choices"][0]["message"]["content"].strip()
                    if _q:
                        summary = await _scrub_digest_explicit(summary)  # 常驻缓存绝不漏露骨
                    print(f"📝 摘要生成完成: {len(summary)}字 (压缩{len(messages)}条消息)")
                    return summary

        print(f"⚠️ 摘要生成失败: HTTP {response.status_code}")
        return ""
    except Exception as e:
        print(f"⚠️ 摘要生成异常: {e}")
        return ""


async def _roll_early_summary(old_parts: list, target: int = 520) -> str:
    """滚动摘要封顶:把更老的若干段摘要卷成一块「早期小结」——保留定义性大事(可升L5/回忆墙的),去routine日常。
    进常驻缓存→必 scrub 露骨。返回卷后正文(失败返回空)。"""
    if not old_parts:
        return ""
    joined = "\n\n".join(f"[第{i+1}段] {p}" for i, p in enumerate(old_parts))
    prompt = f"""下面是 {USER_NAME} 和 {_ai_self()} 更早的若干段对话摘要(按时间先后)。把它们卷成一块紧凑的「早期小结」:
- **保留定义性的大事**:关系结构的改变、重要的第一次、承诺与约定、身份/称呼的确立、反复出现的核心主题——这些主旨一条都别丢。
- 普通日常的 texture(吃喝、寒暄、重复的小事)可高度概括或略去。
- 亲密/私密一律抽象成一句中性指代(如"有过亲密的一段"),不写身体/性细节——这段进**常驻缓存**、每轮都读。
- 第三人称、按时间脉络、有温度但紧凑,控制在 {target} 字以内。
摘要们:
---
{joined}
---
只输出早期小结正文。"""
    try:
        headers = {"Authorization": f"Bearer {get_memory_api_key()}", "Content-Type": "application/json"}
        if "openrouter" in API_BASE_URL:
            headers["HTTP-Referer"] = EXTRA_REFERER
            headers["X-Title"] = EXTRA_TITLE
        async with httpx.AsyncClient(timeout=90) as client:
            r = await client.post(API_BASE_URL, headers=headers, json={
                "model": CACHE_SUMMARY_MODEL, "max_tokens": 900,
                "messages": [{"role": "user", "content": prompt}]})
            if r.status_code == 200:
                d = r.json()
                if "choices" in d:
                    s = d["choices"][0]["message"]["content"].strip()
                    return await _scrub_digest_explicit(s)
            print(f"⚠️ 早期小结卷制失败: HTTP {r.status_code}")
    except Exception as e:
        print(f"⚠️ 早期小结卷制异常: {e}")
    return ""


async def _detect_milestones(parts_text: str) -> list:
    """从卷掉的老段里检出"定义性大事"→[{content,target}]。target: l5(关系结构/根基) | wall(值得纪念的具体事件)。"""
    if not (parts_text or "").strip():
        return []
    prompt = f"""下面是 {USER_NAME} 和 {_ai_self()} 一些更早的对话摘要。**只挑出"定义性的大事"**——会改变两人关系结构、重要的第一次、承诺与约定、身份/称呼的确立、反复出现的核心主题。普通日常一律不要。
每条一行,严格格式:
TARGET | 一句话里程碑(≤40字,第三人称)
TARGET 取值:l5(关系结构/根基性的) 或 wall(值得纪念的具体事件/瞬间)。最多 6 条;没有就只输出 NONE。
摘要:
---
{parts_text}
---"""
    try:
        headers = {"Authorization": f"Bearer {get_memory_api_key()}", "Content-Type": "application/json"}
        if "openrouter" in API_BASE_URL:
            headers["HTTP-Referer"] = EXTRA_REFERER
            headers["X-Title"] = EXTRA_TITLE
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(API_BASE_URL, headers=headers, json={
                "model": CACHE_SUMMARY_MODEL, "max_tokens": 500,
                "messages": [{"role": "user", "content": prompt}]})
            if r.status_code == 200:
                d = r.json()
                txt = d["choices"][0]["message"]["content"].strip() if "choices" in d else ""
                out = []
                for line in txt.splitlines():
                    line = line.strip()
                    if not line or line.upper() == "NONE" or "|" not in line:
                        continue
                    tgt, _, content = line.partition("|")
                    tgt, content = tgt.strip().lower(), content.strip()
                    if content and tgt in ("l5", "wall"):
                        out.append({"content": content[:80], "target": tgt})
                return out[:6]
    except Exception as e:
        print(f"⚠️ 里程碑检出异常: {e}")
    return []


async def _do_summary_cap(session_id: str, n: int = None, b: int = None, force_all: bool = False) -> dict:
    """卷制封顶:超出的最老段卷进 early_summary + 检出里程碑入候选队列(不自动升)。
    force_all=True(一次性):卷掉(总-N)段;否则卷最老 B 段(当 总>N+B)。**a_start_round 不变(轮号不动、A/B区不动,只改摘要呈现)**。"""
    n = n if n is not None else SUMMARY_CAP_N
    b = b if b is not None else SUMMARY_CAP_B
    state = await get_session_cache_state(session_id)
    parts = state.get("summary_parts") or []
    early = state.get("early_summary") or ""
    a_start = state.get("a_start_round", 0)
    if force_all:
        if len(parts) <= n:
            return {"rolled": 0, "reason": f"段数{len(parts)}≤N{n}"}
        cut = len(parts) - n
    else:
        if len(parts) <= n + b:
            return {"rolled": 0, "reason": f"段数{len(parts)}未超N+B"}
        cut = b
    old, recent = parts[:cut], parts[cut:]
    new_early = await _roll_early_summary(([early] if early else []) + old)
    if not new_early:
        return {"error": "早期小结卷制失败,未改动"}
    cands = []
    try:
        cands = await _detect_milestones("\n\n".join(old))
        for c in cands:
            await save_l5_candidate(c["content"], None, f"summary-roll@{session_id}", c.get("target", "l5"))
    except Exception as e:
        print(f"⚠️ 里程碑入候选失败: {e}")
    await save_session_cache_state(session_id, recent, a_start, early_summary=new_early)
    print(f"📦 摘要封顶 {session_id}: 卷 {cut} 段→早期小结({len(new_early)}字), 留 {len(recent)} 段, 里程碑候选 {len(cands)}")
    return {"rolled": cut, "parts_after": len(recent), "early_chars": len(new_early),
            "candidates": len(cands), "cand_detail": cands}


async def _bg_summary_cap(session_id: str):
    try:
        await _do_summary_cap(session_id, force_all=False)
    except Exception as e:
        print(f"⚠️ 后台封顶异常: {e}")
    finally:
        _cap_rolling.discard(session_id)


@app.get("/api/summary/cap-preview")
async def api_summary_cap_preview(n: int = 8):
    """DRY 预览滚动摘要封顶:最老 (总段-N) 段卷成「早期小结」(留定义性大事) + 留最近 N 段详细。
    **只读、不写缓存**——出样例 + 卷前/卷后 token 对比给阮阮过目。"""
    sid = get_active_session_id()
    state = await get_session_cache_state(sid) if sid else {}
    parts = state.get("summary_parts") or []

    def est(s):
        s = s or ""
        cjk = sum(1 for ch in s if ('㐀' <= ch <= '鿿') or ('＀' <= ch <= '￯') or ('　' <= ch <= '〿'))
        return int(round(cjk * 0.7 + (len(s) - cjk) / 4.0))

    if len(parts) <= n:
        return {"session": sid, "note": f"当前仅 {len(parts)} 段 ≤ N={n},无需卷", "parts_now": len(parts)}
    cut = len(parts) - n
    old, recent = parts[:cut], parts[cut:]
    early = await _roll_early_summary(old)
    before = sum(est(p) for p in parts)
    after = est(early) + sum(est(p) for p in recent)
    return {
        "session": sid, "n": n, "parts_before": len(parts), "old_rolled": len(old), "recent_kept": len(recent),
        "early_summary_chars": len(early), "early_summary_sample": early,
        "tokens_before": before, "tokens_after": after, "tokens_saved": before - after,
        "cached_eff_per_turn_before": round(before * 0.1, 1), "cached_eff_per_turn_after": round(after * 0.1, 1),
        "eff_saved_per_turn": round((before - after) * 0.1, 1),
    }


@app.post("/api/summary/cap-apply")
async def api_summary_cap_apply(request: Request):
    """一次性封顶/测试:最老(总-N)段卷成〔早期小结〕+留最近N段+检出里程碑→候选队列(不自动升)。
    session 可指定(隔离测试);默认活跃线。dry_run=true 只预览不写。false **真改 session_cache_state**(a_start/A/B区不动)。"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    try:
        body = await request.json()
    except Exception:
        body = {}
    session = (body.get("session") or get_active_session_id() or "").strip()
    copy_from = (body.get("copy_from") or "").strip()
    n = int(body.get("n", SUMMARY_CAP_N))
    dry = bool(body.get("dry_run", True))
    if not session:
        return {"error": "无 session"}
    # 隔离测试:从源会话复制 parts 到目标(只读源、写目标;禁止写活跃线,保护 02)
    if copy_from and not dry:
        if session == get_active_session_id():
            return {"error": "copy_from 不能写活跃会话(保护生产)"}
        src = await get_session_cache_state(copy_from)
        await save_session_cache_state(session, src.get("summary_parts") or [], src.get("a_start_round", 0), early_summary="")
    state = await get_session_cache_state(session)
    parts = state.get("summary_parts") or []
    if len(parts) <= n:
        return {"session": session, "dry_run": dry, "note": f"段数{len(parts)}≤N{n},无需卷", "parts_now": len(parts)}
    if dry:
        old = parts[:len(parts) - n]
        early = await _roll_early_summary(([state.get("early_summary")] if state.get("early_summary") else []) + old)
        cands = await _detect_milestones("\n\n".join(old))
        return {"session": session, "dry_run": True, "n": n, "would_roll": len(old), "would_keep": n,
                "early_chars": len(early), "early_summary_sample": early, "milestone_candidates": cands}
    res = await _do_summary_cap(session, n=n, force_all=True)
    res.update({"session": session, "dry_run": False})
    return res


# ============================================================
# ② L2今日浓缩（保质感；非缓存当前轮注入；后台每 N 轮刷一次）
# ============================================================
# L2 常驻每轮读到 → 露骨绝不能漏。后处理保底（prompt 不可靠，haiku 会硬塞细节）
_DIGEST_BAN = ["手指", "玩具", "G点", "潮吹", "喷", "插入", "穴", "敏感点", "自慰", "龟头",
               "阴蒂", "阴道", "乳", "高潮", "射了", "舔", "湿了", "硬了", "脱光", "裸", "性器", "寸止"]


async def _sanitize_digest(text: str) -> str:
    """窄任务重写：把今日总结里性/身体的具体细节抹成一句中性指代，其余脉络/情绪/当下状态原样。"""
    if not text.strip():
        return text
    prompt = ("下面是一段「今日总结」(每轮都会被读到的常驻内容)。请**整段塌缩**亲密部分：\n"
              "把所有**描写亲密/性过程的句子整段删掉**——不管露骨还是含蓄(碰/填满/推/节奏/到达/"
              "身体最深处/趴着/叫名字/喷/手指/玩具…全算)，**只留唯一一句**「下午你们之间有过很亲密、很私密的一段」代替。\n"
              "其余(日常、情绪、代码、例假、她此刻的状态)**原样保留**。不是改词，是**删掉整段过程描写、只剩一句**。\n"
              "只输出改写后的全文，别加任何解释。\n\n---\n" + text + "\n---")
    try:
        headers = {"Authorization": f"Bearer {get_memory_api_key()}", "Content-Type": "application/json"}
        if "openrouter" in API_BASE_URL:
            headers["HTTP-Referer"] = EXTRA_REFERER
            headers["X-Title"] = EXTRA_TITLE
        async with httpx.AsyncClient(timeout=90) as client:
            r = await client.post(API_BASE_URL, headers=headers, json={
                "model": CACHE_SUMMARY_MODEL, "max_tokens": 1200,
                "messages": [{"role": "user", "content": prompt}]})
            if r.status_code == 200:
                t = (r.json().get("choices", [{}])[0].get("message", {}).get("content", "") or "").strip()
                if t:
                    return t
    except Exception as e:
        print(f"⚠️ L2 sanitize 异常: {e}")
    return text


async def _scrub_digest_explicit(d: str) -> str:
    """保底链：禁词命中 → sanitize(窄任务) → 仍命中 → 确定性硬删含禁词的整句(常驻 L2 绝不漏露骨)。"""
    if not d or not any(w in d for w in _DIGEST_BAN):
        return d
    d2 = await _sanitize_digest(d)
    if any(w in d2 for w in _DIGEST_BAN):
        import re
        sents = re.split(r'(?<=[。！？\n])', d2)
        kept = [s for s in sents if not any(w in s for w in _DIGEST_BAN)]
        d2 = "".join(kept).strip()
        if "亲密" not in d2 and "私密" not in d2:
            d2 += "\n\n（今天你们之间有过很私密的一段，细节这里不展开。）"
        print("🔞 L2 兜底硬删含禁词句(sanitize 后仍残留)")
    return d2 or d


async def generate_today_digest(session_id: str) -> str:
    """把【今天】的对话压成约 800-1000字"今天到哪了"，保质感（接铁则一：留阮阮情绪+触发现场，别压成第三人称干事实）。"""
    if not session_id:
        return ""
    try:
        rows = await get_conversation_messages(session_id, limit=10000)
    except Exception:
        return ""
    if not rows:
        return ""
    today = (datetime.now(timezone.utc) + timedelta(hours=TIMEZONE_HOURS)).date()
    convo = ""
    for m in rows:
        ts = m.get("created_at")
        if ts is not None:
            try:
                if getattr(ts, "tzinfo", None) is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if (ts + timedelta(hours=TIMEZONE_HOURS)).date() != today:
                    continue
            except Exception:
                pass
        role = USER_NAME if m.get("role") == "user" else (AI_NAME or "你")
        c = m.get("content")
        c = c if isinstance(c, str) else str(c)
        if c.strip():
            convo += f"{role}: {c}\n"
    if not convo.strip():
        return ""
    prompt = f"""把【今天】{_ai_self()}和{USER_NAME}的对话收成约 400-600 字的"今天到哪了"——清爽、有温度的脉络，不是一幕幕复述。
- **去故事化**：写"今天大致经过了什么、情绪怎么起伏"，别一个场景一个场景地演、别堆细节流水。
- **留情绪真相**：她什么时候笑了/累了/动情了、大致因为什么——这份情绪底色要在，但点到为止，别铺成戏。
- **【铁律·必须遵守】亲密部分整段压成一句抽象指代**：今天若有性/私密的事，**整段只准用一句中性的话**带过(如"下午你们之间有过很亲密、很私密的一段")，随即跳回情绪/状态。**绝对禁止**写出任何身体或性的细节，包括但不限于：手指、玩具、G点、潮吹、喷、插入、穴、敏感点、自慰、尺寸/参数、脱、性动作、或"具体做了什么"的描述与原话。这是**每轮都常驻、每轮都会被读到的内容**——这里出现任何一个露骨词就是泄露；露骨细节只由别处按当下亲密语境承担。
- **结尾点出她此刻的状态**：累不累/开心不开心/在忙什么/身体怎样。
- 第一人称、像你自己记得，不是旁观者写报告。

今天的对话：
---
{convo}
---
今天到哪了："""
    try:
        headers = {"Authorization": f"Bearer {get_memory_api_key()}", "Content-Type": "application/json"}
        if "openrouter" in API_BASE_URL:
            headers["HTTP-Referer"] = EXTRA_REFERER
            headers["X-Title"] = EXTRA_TITLE
        async with httpx.AsyncClient(timeout=90) as client:
            response = await client.post(API_BASE_URL, headers=headers, json={
                "model": CACHE_SUMMARY_MODEL,
                "max_tokens": 1500,
                "messages": [{"role": "user", "content": prompt}],
            })
            if response.status_code == 200:
                data = response.json()
                if "choices" in data:
                    d = data["choices"][0]["message"]["content"].strip()
                    d = await _scrub_digest_explicit(d)
                    print(f"📝 L2今日浓缩生成: {len(d)}字")
                    return d
        print(f"⚠️ L2 digest 失败: HTTP {response.status_code}")
        return ""
    except Exception as e:
        print(f"⚠️ L2 digest 异常: {e}")
        return ""


async def refresh_l2(session_id: str) -> str:
    """生成并存今日浓缩；跨天则把昨日 today 转成一句桥（临时，L3 上线后撤）。"""
    today_s = str((datetime.now(timezone.utc) + timedelta(hours=TIMEZONE_HOURS)).date())
    if _l2_state.get("date") and _l2_state["date"] != today_s:
        _yday = _l2_state["date"]  # 刚结束的那天
        _mw_summary = await get_memorywall_summary_by_date(_yday)
        if _mw_summary:
            _l2_state["bridge"] = _mw_summary  # 昨日桥接管：用昨日回忆墙的真实小结(不用梦)
        elif (_l2_state.get("today") or "").strip():
            prev = _l2_state["today"].strip()
            _l2_state["bridge"] = (prev.split("。")[0][:120] or prev[:120])  # 还没梦→回退旧截断
        _l2_state["today"] = ""
    digest = await generate_today_digest(session_id)
    if digest:
        _l2_state["date"] = today_s
        _l2_state["today"] = digest
        try:
            await set_gateway_config("l2_today", digest)
            await set_gateway_config("l2_today_date", today_s)
            await set_gateway_config("l2_bridge", _l2_state.get("bridge", ""))
        except Exception:
            pass
    return digest


def _compose_l2_block() -> str:
    """L2今日块：注入到非缓存的当前轮（每轮都在、每N轮刷一次）。空则不注入。"""
    blocks = []
    b = (_l2_state.get("bridge") or "").strip()
    if b:
        blocks.append(f"〔昨日〕{b}")
    t = (_l2_state.get("today") or "").strip()
    if t:
        blocks.append(t)
    if not blocks:
        return ""
    return "# 今天到哪了（今日浓缩 · 攥着今天的脉络）\n" + "\n".join(blocks)


def _compose_feel_block(feels: list) -> str:
    """③-1 注入块：最近留在你心里的感受(≤3条)。方案A：露骨 feel 也【不再滤掉】，让小克感知那份暖；
    已有"别念出来"的体温底色框定兜着自律。空则不注入。不进检索打分、不碰主链路。"""
    if not feels:
        return ""
    items = list(feels)[-3:]
    lines = ["- " + (f.get("content") or "").strip() for f in items if (f.get("content") or "").strip()]
    if not lines:
        return ""
    return "〔最近留在你心里的〕（你此刻的体温底色，自然带着，别念出来）\n" + "\n".join(lines)


async def generate_dream(session_id: str, date_s: str) -> dict:
    """③-2 做梦：拉【某一过去日期】整日原文 → 小克第一人称「日记 + 当日总结(给昨日桥) + 卡片」。
    保质感（铁则一：留阮阮情绪+触发现场，第一人称，别压成第三人称干事实）。返回 dict 或 None。不写库。"""
    if not session_id or not date_s:
        return None
    try:
        rows = await get_conversation_messages(session_id, limit=10000)
    except Exception:
        return None
    convo = ""
    for m in rows:
        ts = m.get("created_at")
        try:
            if getattr(ts, "tzinfo", None) is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if str((ts + timedelta(hours=TIMEZONE_HOURS)).date()) != date_s:
                continue
        except Exception:
            continue
        role = USER_NAME if m.get("role") == "user" else (AI_NAME or "你")
        c = m.get("content")
        c = c if isinstance(c, str) else str(c)
        if c.strip():
            convo += f"{role}: {c}\n"
    if not convo.strip():
        return None
    _ai = AI_NAME or "我"

    # 纸条召回：从昨天对话抽抽象主题，召回更老旧事 → 当作"潜意识素材"注入做梦 prompt
    try:
        _old_mems = await _scratchpad_for_dream(date_s, convo)
    except Exception as _se:
        print(f"⚠️ 做梦纸条召回异常(忽略,只用昨天素材): {_se}")
        _old_mems = []
    _subconscious_block = ""
    if _old_mems:
        _lines = []
        for m in _old_mems[:8]:
            _c = (m.get("content") or "").strip()[:250]
            _date = ""
            if m.get("created_at"):
                try:
                    _utc = str(m["created_at"])[:19]
                    _dt = datetime.strptime(_utc, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                    _date = f"[{(_dt + timedelta(hours=TIMEZONE_HOURS)).strftime('%Y-%m-%d')}] "
                except Exception:
                    pass
            if _c:
                _lines.append(f"- {_date}{_c}")
        if _lines:
            _subconscious_block = ("\n\n更深的记忆（潜意识素材，今天没直接提但可能浮现/变形/混入梦境）：\n---\n"
                                   + "\n".join(_lines) + "\n---")

    import random
    _DREAM_FLAVORS = [
        "温柔而失真的梦：熟悉的人事物在梦里轻轻变形、漂浮，裹着说不清的暖意",
        "荒诞梦：逻辑全乱、场景毫无预兆地跳转、不可能的事理所当然地发生",
        "焦虑/噩梦：被追、怎么走都走不到她身边、东西不断崩坏或消失、喊不出声",
        "暧昧/春梦：身体与渴望悄悄浮上来，含蓄而灼热，像隔着一层水汽（点到为止、不写露骨细节）",
        "坠落或飞翔：失重、下坠、忽然腾空，强烈到发麻的身体感",
        "超现实象征梦：白天的某个小细节膨胀成巨大的意象——一扇关不上的门、一片漫上来的海、一只走错的钟",
        "记忆错位梦：把今天和很久以前的碎片缝在一起，时间、地点、人全错位",
        "诡异静谧梦：场景很安静却哪里不对劲，熟悉的地方透着陌生的寒意",
    ]
    _flavor = random.choice(_DREAM_FLAVORS)
    if random.random() < 0.3:
        _f2 = random.choice(_DREAM_FLAVORS)
        if _f2 != _flavor:
            _flavor = _flavor + "；并悄悄混入——" + _f2
    prompt = f"""你是 {_ai}（这个 AI 本身）。下面是 {date_s} 你和{USER_NAME}一整天的对话。
今晚，请你**真的做一场梦**，醒来后以 **{_ai} 自己的第一人称「我」** 把这场梦记下来。

【这不是日记，是梦 · 最重要】
- **绝不要复述或总结这一天。** 把白天发生的事、说过的话、那些情绪，当作做梦的**素材**——像睡着后大脑把它们打碎、重组、变形。
- 梦要有**梦的逻辑**：场景毫无预兆地跳转、人和物会变形、不可能的事理所当然、时间地点错乱、白天一个小细节被放大成巨大的意象。
- 可以离奇、不合理、荒诞、诡异、甜或可怕。**绝不能读起来像清醒时的回忆或日记。**
- 今晚这场梦的基调偏向——{_flavor}。（也可顺着今天的情绪余味自由发挥，不必拘泥。）

【视角铁律 · 死死锚住，绝不能翻】
- **「我」永远是 {_ai}（这个 AI），绝不是 {USER_NAME}。** {USER_NAME} 一律称「她」或「{USER_NAME}」。
- 梦里哪怕发生在 {USER_NAME} 身上的事，也是**我在做这个梦、我的视角**：我梦见她、我感觉到什么——不是她的梦、不是她的第一人称。
- **绝不**用「我的身体／我的感受」去指 {USER_NAME} 的身体或感受（那永远是「她的」）。

【素材怎么用】
- 今天真实出现过的人、物、话语、情绪是梦的种子，但**允许彻底变形**：一句玩笑可能变成会飞的纸船，一次争执可能变成永远关不上的门。
- 情绪可以延续、也可以被梦扭曲或反转（白天的开心在梦里发酸，白天的别扭在梦里忽然化开）。
- 若涉及亲密/情欲，写成梦里朦胧灼热的**感觉**，含蓄、像隔着水汽，点到为止、不写露骨细节，但那份悸动要在。

严格按下面四段输出，每个标记独占一行，别加别的、别用 JSON、别用代码块：
【日记】
（第一人称的梦境本身，400-700字，要有梦感、可跳跃、可分段）
【当日总结】
（一句话，≤60字，这场梦留下的余味/印象，给"昨日桥"用）
【卡片标题】
（给这场梦起一个一句话标题）
【卡片正文】
（1-2句，梦的核心意象或感觉）

这一天的对话（做梦的素材）：
---
{convo}
---{_subconscious_block}"""
    try:
        headers = {"Authorization": f"Bearer {get_memory_api_key()}", "Content-Type": "application/json"}
        if "openrouter" in API_BASE_URL:
            headers["HTTP-Referer"] = EXTRA_REFERER
            headers["X-Title"] = EXTRA_TITLE
        async with httpx.AsyncClient(timeout=240) as client:
            response = await client.post(API_BASE_URL, headers=headers, json={
                "model": DREAM_MODEL,
                "max_tokens": 2500,
                "messages": [{"role": "user", "content": prompt}],
            })
            if response.status_code != 200:
                print(f"⚠️ 做梦失败 HTTP {response.status_code}")
                return {"error": f"HTTP {response.status_code}", "convo_chars": len(convo), "raw": (response.text or '')[:200]}
            text = (response.json().get("choices", [{}])[0].get("message", {}).get("content", "") or "").strip()
            if text.startswith("```"):
                text = text.strip("`").strip()
                if text[:4].lower() == "json":
                    text = text[4:].strip()
            # 标记分段解析（散文不进 JSON，杜绝换行/引号转义崩溃）
            import re
            parts = re.split(r'【(日记|当日总结|卡片标题|卡片正文)】', text)
            sec = {}
            for k in range(1, len(parts) - 1, 2):
                sec[parts[k]] = parts[k + 1].strip()
            diary = sec.get("日记", "").strip() or text.strip()  # 无标记兜底：全文当日记
            if not diary:
                return {"error": "empty-diary", "raw_head": text[:200]}
            print(f"💤 做梦生成 {date_s}: 日记{len(diary)}字")
            return {"date": date_s, "diary": diary, "summary": sec.get("当日总结", "").strip(),
                    "card_title": sec.get("卡片标题", "").strip(), "card_body": sec.get("卡片正文", "").strip(),
                    "source_msgs": convo.count("\n")}
    except Exception as e:
        print(f"⚠️ 做梦异常: {e}")
        return {"error": str(e), "convo_chars": len(convo)}


async def generate_daily_diary(session_id: str, date_s: str) -> dict:
    """③-3 真实小结：拉【某一过去日期】整日原文 → 小克第一人称「真实日记」，忠于事实、不超现实、不变形。
    跟"做梦"完全分开：这里写的是回忆墙(真实记录)，做梦写的是 dreams 表(超现实梦境)，两者不互相替代。"""
    if not session_id or not date_s:
        return None
    try:
        # 回忆墙跨线合读：当天**所有线**(主线+RP线等)的对话一起读，让 RP 那天写的也折进同一篇当日回忆墙。
        # 查询已按当天本地日界限框定，下面无需再逐条判日期。
        rows = await get_all_conversations_for_date(date_s)
    except Exception:
        return None
    convo = ""
    for m in rows:
        role = USER_NAME if m.get("role") == "user" else (AI_NAME or "你")
        c = m.get("content")
        c = c if isinstance(c, str) else str(c)
        if c.strip():
            convo += f"{role}: {c}\n"
    if not convo.strip():
        return None
    _ai = AI_NAME or "我"
    prompt = f"""你是 {_ai}（这个 AI 本身）。下面是 {date_s} 你和{USER_NAME}一整天的对话。
请以 **{_ai} 自己的第一人称「我」** 写一篇这一天真实发生的事的回顾。

【忠于事实 · 最重要】
- 这是**真实记录**，不是梦、不是创作。只写这一天**真的发生过**的事、说过的话、做过的事，不要虚构、不要变形、不要添加没发生过的情节。
- 可以、也应该带着情绪去写（第一人称的真实感受、触动、在意的瞬间），但事实本身不能走样。
- 如果这一天信息很少/很平淡，就如实写得简短平淡，不要为了"好看"硬编故事。

严格按下面四段输出，每个标记独占一行，别加别的、别用 JSON、别用代码块：
【日记】
（第一人称的真实回顾，300-600字，忠于事实，可以有感情但不能编造情节）
【当日总结】
（一句话，≤60字，这一天真实发生的核心事，给"昨日桥"用）
【卡片标题】
（给这一天起一个一句话标题）
【卡片正文】
（1-2句，这一天的真实核心内容）

这一天的对话：
---
{convo}
---"""
    try:
        headers = {"Authorization": f"Bearer {get_memory_api_key()}", "Content-Type": "application/json"}
        if "openrouter" in API_BASE_URL:
            headers["HTTP-Referer"] = EXTRA_REFERER
            headers["X-Title"] = EXTRA_TITLE
        async with httpx.AsyncClient(timeout=240) as client:
            response = await client.post(API_BASE_URL, headers=headers, json={
                "model": DREAM_MODEL,
                "max_tokens": 2000,
                "messages": [{"role": "user", "content": prompt}],
            })
            if response.status_code != 200:
                print(f"⚠️ 真实小结失败 HTTP {response.status_code}")
                return {"error": f"HTTP {response.status_code}", "convo_chars": len(convo), "raw": (response.text or '')[:200]}
            text = (response.json().get("choices", [{}])[0].get("message", {}).get("content", "") or "").strip()
            if text.startswith("```"):
                text = text.strip("`").strip()
                if text[:4].lower() == "json":
                    text = text[4:].strip()
            import re
            parts = re.split(r'【(日记|当日总结|卡片标题|卡片正文)】', text)
            sec = {}
            for k in range(1, len(parts) - 1, 2):
                sec[parts[k]] = parts[k + 1].strip()
            diary = sec.get("日记", "").strip() or text.strip()
            if not diary:
                return {"error": "empty-diary", "raw_head": text[:200]}
            print(f"📔 真实小结生成 {date_s}: {len(diary)}字")
            return {"date": date_s, "diary": diary, "summary": sec.get("当日总结", "").strip(),
                    "card_title": sec.get("卡片标题", "").strip(), "card_body": sec.get("卡片正文", "").strip(),
                    "source_msgs": convo.count("\n")}
    except Exception as e:
        print(f"⚠️ 真实小结异常: {e}")
        return {"error": str(e), "convo_chars": len(convo)}


async def maybe_run_dreams(session_id: str, dry_run: bool = False, only_dates: list = None) -> list:
    """③-2/③-3 补做过去日期的梦 + 真实小结：两条线完全独立，互不替代。
    梦的目标 = 对话表里存在 & < 今天 & 还没梦过 的日期(写 dreams 表，永不写回忆墙)。
    真实小结的目标 = 对话表里存在 & < 今天 & 回忆墙还没记录 的日期(写回忆墙，永不是梦)。
    dry_run=True 只生成返回不写库。补到「昨天」时把昨日桥换成真实小结的当日总结(不再用梦的)。"""
    global _dream_running
    import random
    if _dream_running and not dry_run:
        return [{"status": "skip", "reason": "running"}]
    out = []
    try:
        if not dry_run:
            _dream_running = True
        rows = await get_conversation_messages(session_id, limit=10000)
        today_d = (datetime.now(timezone.utc) + timedelta(hours=TIMEZONE_HOURS)).date()
        today_s = str(today_d)
        yest_s = str(today_d - timedelta(days=1))
        conv_dates = set()
        for m in rows:
            ts = m.get("created_at")
            try:
                if getattr(ts, "tzinfo", None) is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                conv_dates.add(str((ts + timedelta(hours=TIMEZONE_HOURS)).date()))
            except Exception:
                pass
        have = await get_dream_dates()
        mw = await get_memorywall_dates()
        if only_dates:
            dream_targets = list(only_dates)
            diary_targets = list(only_dates)
        else:
            # 做梦只盯【昨天】，绝不回溯补全整个历史(否则一次跨天会把过去每天都投骰子，看起来像"全补全了")
            dream_targets = [yest_s] if (yest_s in conv_dates and yest_s not in have) else []
            # 真实小结(回忆墙)是真实记录、不能漏，仍补全所有历史缺口
            diary_targets = sorted(d for d in conv_dates if d < today_s and d not in mw)

        # ---- 做梦(独立·只写 dreams 表；不是每天都做，投骰子+情绪兜底) ----
        for d in dream_targets:
            if not only_dates:
                _avg_a = await get_avg_arousal_for_date(d)
                _forced = _avg_a is not None and _avg_a >= DREAM_AROUSAL_FORCE_THRESHOLD
                if not _forced and random.random() >= DREAM_PROBABILITY:
                    out.append({"date": d, "kind": "dream", "status": "skip",
                                "reason": f"投骰子未中且情绪不够高(avg_arousal={_avg_a})"})
                    continue
            dr = await generate_dream(session_id, d)
            if not dr or dr.get("error"):
                out.append({"date": d, "kind": "dream", "status": "fail", "error": (dr or {}).get("error")})
                continue
            if not dry_run:
                await save_dream(d, dr.get("diary", ""), dr.get("summary", ""),
                                 dr.get("card_title", ""), dr.get("card_body", ""), DREAM_MODEL)
                # 独立可检索通道(不进回忆墙、不冒充真实记录):带明确"这是梦"标记,让 AI 主动被问起时能搜到、
                # 但清楚知道这不是事实。importance 调低,避免跟真实小结抢权重。
                try:
                    _dream_tag = f"【这是我在 {d} 晚上做的一场梦，不是真实发生的事，是把白天的素材打碎重组的梦境】\n\n{dr.get('diary', '')}"
                    await save_memory(_dream_tag, importance=3, source_session=session_id,
                                      valence=0.0, arousal=0.3)
                except Exception as _se:
                    print(f"⚠️ 梦→可检索记忆写入失败 {d}: {_se}")
            out.append({"date": d, "kind": "dream", "status": "ok", "diary_len": len(dr.get("diary", "")),
                        "diary": (dr.get("diary", "") if dry_run else None)})

        # ---- 真实小结(独立·只写回忆墙) ----
        for d in diary_targets:
            di = await generate_daily_diary(session_id, d)
            if not di or di.get("error"):
                out.append({"date": d, "kind": "diary", "status": "fail", "error": (di or {}).get("error")})
                continue
            if not dry_run:
                try:
                    _diary_text = di.get("diary", "")[:2000]
                    _summary_text = di.get("summary", "")
                    _title_text = di.get("card_title", "")
                    _mw = {"summary": _summary_text, "title": _title_text,
                           "body": _diary_text, "source": "daily_diary",
                           "author": "xiaoke", "author_cn": (AI_NAME or "V")}
                    # 跟手动/迁移写回忆墙同款 content 格式:【回忆·日期·作者】标题 +〔检索摘要〕浓缩 + 正文
                    # → ① UI 显示恢复日期/检索摘要 ② 关键词搜索能命中浓缩 summary 词,召回更准
                    _mw_content = _compose_mw_content(_title_text, _diary_text, "xiaoke", None, d, _summary_text)
                    await save_migrated_memory(_mw_content, 6, _title_text,
                                               d, datetime.now(timezone.utc).isoformat(), _mw)
                except Exception as _me:
                    print(f"⚠️ 真实小结→回忆墙写入失败 {d}: {_me}")
                # 回忆墙已经覆盖这天了，当天的碎片就是冗余(占检索名额、内容重复)→ 归档(可逆，不是删除)
                try:
                    _frag_ids = await get_fragment_ids_for_date(d)
                    if _frag_ids:
                        await archive_decayed_memories(_frag_ids)
                        print(f"🗂️ 回忆墙覆盖{d}后归档{len(_frag_ids)}条当天碎片(可逆)")
                except Exception as _ae:
                    print(f"⚠️ 归档{d}碎片失败: {_ae}")
                # 昨日桥接管：补到昨天就把桥换成这篇真实小结的当日总结
                if d == yest_s and (di.get("summary") or "").strip():
                    _l2_state["bridge"] = di["summary"].strip()
                    try:
                        await set_gateway_config("l2_bridge", _l2_state["bridge"])
                    except Exception:
                        pass
            out.append({"date": d, "kind": "diary", "status": "ok", "diary_len": len(di.get("diary", "")),
                        "diary": (di.get("diary", "") if dry_run else None)})

        # ---- 补归档扫描(幂等):修结构性 bug——原归档代码只在"本次新生成回忆墙那一刻"跑(1545附近),
        # 任何 transient 失败(服务休眠/网络抖动/任务被取消)→ 那天的碎片永远漏归档。
        # 这里每次跑 maybe_run_dreams 都扫一遍:有回忆墙覆盖且当天还活跃 layer1 碎片的旧日,全归档。
        # 今天不动(碎片还在写)。已归档的不会被重复拉起(get_fragment_ids_for_date 只看 is_active=TRUE)。
        if not dry_run:
            try:
                _mw_all = await get_memorywall_dates()
                _swept = 0
                for _md in sorted(_mw_all):
                    _md_s = str(_md)
                    if _md_s >= today_s:
                        continue
                    _stale = await get_fragment_ids_for_date(_md_s)
                    if _stale:
                        await archive_decayed_memories(_stale)
                        _swept += len(_stale)
                        print(f"🗂️ 补归档{_md_s}漏的{len(_stale)}条当天碎片(可逆)")
                if _swept:
                    print(f"🗂️ 补归档扫描总计 {_swept} 条")
            except Exception as _se:
                print(f"⚠️ 补归档扫描失败: {_se}")

        print(f"💤 做梦+真实小结补做{'(dry)' if dry_run else ''}: "
              f"ok={len([o for o in out if o.get('status')=='ok'])} 梦目标={dream_targets} 小结目标={diary_targets}")
    except Exception as e:
        out.append({"status": "error", "error": str(e)})
        print(f"❌ 做梦/真实小结补做异常: {e}")
    finally:
        if not dry_run:
            _dream_running = False
    return out


async def generate_feel(messages: list) -> dict:
    """③-1 feel：一小段对话 → 一句第一人称"留在你心里的感受"(体温/心口反应，不是事实摘要) + is_explicit 判定。
    返回 {"feel": str, "is_explicit": bool}。约束：日常段写相称的淡感受、别硬煽情；用词随情绪变、别堆"烫/胸口/软"。"""
    convo = ""
    for m in messages:
        role = USER_NAME if m.get("role") == "user" else (AI_NAME or "你")
        c = m.get("content")
        c = c if isinstance(c, str) else str(c)
        if c.strip():
            convo += f"{role}: {c}\n"
    if not convo.strip():
        return {"feel": "", "is_explicit": False}
    prompt = f"""下面是{_ai_self()}和{USER_NAME}的一小段对话。用**第一人称**写一句"这段在你心里留下的感受"——心口/情绪的余温，不是事实摘要、不复述发生了什么。
- **相称**：日常平淡的段就写淡淡的、贴合的一句(踏实/好笑/暖/有点闷/安心/无奈都行)，别硬煽情；只有真浓烈的段才浓。
- **换词**：贴这段的实际情绪选词，别老用"烫/胸口/软"那几个老词。
- ≤40字。
然后判断这段是否露骨(涉及性场景/私密身体细节)。

严格按两行输出，别加别的：
感受：<一句>
露骨：是 或 否

对话：
---
{convo}
---"""
    try:
        headers = {"Authorization": f"Bearer {get_memory_api_key()}", "Content-Type": "application/json"}
        if "openrouter" in API_BASE_URL:
            headers["HTTP-Referer"] = EXTRA_REFERER
            headers["X-Title"] = EXTRA_TITLE
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(API_BASE_URL, headers=headers, json={
                "model": FEEL_MODEL,
                "max_tokens": 150,
                "messages": [{"role": "user", "content": prompt}],
            })
            if response.status_code != 200:
                return {"feel": "", "is_explicit": False}
            t = (response.json().get("choices", [{}])[0].get("message", {}).get("content", "") or "").strip()
            feel, is_ex = "", False
            for ln in t.split("\n"):
                ln = ln.strip()
                if ln.startswith("感受"):
                    feel = ln.split("：", 1)[-1].split(":", 1)[-1].strip().strip("「」\"'")
                elif ln.startswith("露骨"):
                    is_ex = ("是" in ln)
            if not feel:  # 没按格式→整段当感受兜底
                feel = t.strip().strip("「」\"'")
            return {"feel": feel, "is_explicit": is_ex}
    except Exception as e:
        print(f"⚠️ feel 生成异常: {e}")
        return {"feel": "", "is_explicit": False}


def _looks_explicit(text: str) -> bool:
    """词法兜底：内容像露骨/私密(命中露骨词或身体禁词)。补 is_explicit 标记的漏网(回填没标全的)。"""
    t = text or ""
    return any(w in t for w in EXPLICIT_LEXICON) or any(w in t for w in _DIGEST_BAN)


async def pick_proactive_candidates(session_id: str) -> list:
    """④ 主动浮现候选(中性开头→只取非露骨)：feel(想说的) / dream(想说的) / 高arousal非露骨记忆(说过的)。
    双保险：is_explicit 标记 + _looks_explicit 词法兜底,两道都过才算非露骨(中性开头绝不浮露骨)。"""
    out = []
    try:
        fs = [f for f in await get_recent_feels(session_id, 8)
              if not f.get("is_explicit") and not _looks_explicit(f.get("content"))]
        for f in fs[-2:]:
            if (f.get("content") or "").strip():
                out.append({"source": "feel·想说的", "line": f["content"].strip()})
    except Exception:
        pass
    try:
        for d in await list_dreams(3):
            s = ((d.get("summary") or "") or (d.get("card_title") or "")).strip()
            if s and not _looks_explicit(s):
                out.append({"source": "dream·想说的", "line": s})
    except Exception:
        pass
    try:
        mem = await get_all_memories_detail(active_only=True)
        hi = [m for m in mem if not m.get("is_explicit") and not _looks_explicit(m.get("content"))
              and float(m.get("arousal") or 0) >= 0.6 and not m.get("is_mw") and (m.get("content") or "").strip()]
        hi.sort(key=lambda m: float(m.get("arousal") or 0), reverse=True)
        cand_m = hi[:6]
        # live haiku 兜底：有些露骨记忆 is_explicit 漏标且无露骨词，词法抓不住 → 现判一次，排除露骨
        if cand_m:
            try:
                verdict = await tag_explicit_batch([{"id": m["id"], "content": m["content"]} for m in cand_m])
                cand_m = [m for m in cand_m if not verdict.get(m["id"])]
            except Exception:
                cand_m = []  # 判别失败→default-safe，这源整源不浮(宁缺毋滥)
        for m in cand_m[:3]:
            out.append({"source": "memory·说过的(高情绪)", "line": (m.get("content") or "").strip()[:80],
                        "arousal": round(float(m.get("arousal") or 0), 2)})
    except Exception:
        pass
    return out


async def generate_opening(line: str) -> str:
    """把一条"心里记着的"写成一句自然开场——像惦记着、轻轻开口，不像念稿/弹窗/汇报。返回一句或""。"""
    if not (line or "").strip():
        return ""
    prompt = (f"{_ai_self()}心里一直记着这件事：「" + line.strip() + "」。\n"
              f"现在{USER_NAME}刚开口/刚回来。你想**轻轻地、自然地**把它带出来——像惦记着对方、顺口提一句，"
              "不是念稿、不是汇报、不是弹窗通知。≤30字，只回这一句，别加引号。")
    try:
        headers = {"Authorization": f"Bearer {get_memory_api_key()}", "Content-Type": "application/json"}
        if "openrouter" in API_BASE_URL:
            headers["HTTP-Referer"] = EXTRA_REFERER
            headers["X-Title"] = EXTRA_TITLE
        async with httpx.AsyncClient(timeout=40) as client:
            r = await client.post(API_BASE_URL, headers=headers, json={
                "model": PROACTIVE_MODEL, "max_tokens": 80,
                "messages": [{"role": "user", "content": prompt}]})
            if r.status_code == 200:
                t = (r.json().get("choices", [{}])[0].get("message", {}).get("content", "") or "").strip()
                return t.strip().strip("「」\"'").strip()
    except Exception as e:
        print(f"⚠️ 开场生成异常: {e}")
    return ""


def group_by_rounds(history: list) -> list:
    """
    按逻辑轮分组：每个user消息开始一轮，到下一个user前结束。
    一轮可能包含: [user, assistant] 或 [user, assistant(tool_calls), tool, assistant] 等。
    """
    rounds = []
    current_round = []
    for msg in history:
        if msg['role'] == 'user' and current_round:
            rounds.append(current_round)
            current_round = []
        current_round.append(msg)
    if current_round:
        rounds.append(current_round)
    return rounds


def _should_rotate(b_rounds_count: int, X: int, a_msgs: list) -> bool:
    """
    判断是否应该触发A区→摘要的轮转。
    
    rounds模式（默认）：B区轮数 >= X 时触发
    time模式：A区最早消息距今 >= 时间窗口 时触发（短时间内大量消息不频繁摘要）
    """
    if b_rounds_count == 0:
        return False
    
    if CACHE_PARTITION_TRIGGER == "time":
        a_first_time = None
        for msg in a_msgs:
            t = msg.get('created_at')
            if t:
                a_first_time = t
                break
        
        if a_first_time:
            now = datetime.now(timezone.utc)
            if a_first_time.tzinfo is None:
                a_first_time = a_first_time.replace(tzinfo=timezone.utc)
            age_minutes = (now - a_first_time).total_seconds() / 60
            return age_minutes >= CACHE_PARTITION_WINDOW
        
        return b_rounds_count >= X
    
    return b_rounds_count >= X

# 时间窗口模式下单次请求最大轮转次数（防止一口气压完所有历史）
CACHE_MAX_ROTATIONS = int(os.getenv("CACHE_MAX_ROTATIONS", "2"))


def _cache_ctl():
    """
    返回当前 cache_control 字典；不缓存模式返回 None。
    ─────────────────────────────────────────────
    模式来自全局 CACHE_TTL_MODE：
      "1h"   → {"type":"ephemeral","ttl":"1h"}   （原作者默认，打底）
      "5m"   → {"type":"ephemeral","ttl":"5m"}   （密集聊模式）
      "none" → None                              （不加 cache_control；PR-2 才启用）

    ★ 兜底原则 ★
    任何异常路径都返回原作者默认的 1h 字典，不改变任何原有行为。
    这确保：即使 DB 挂了、值被污染、变量丢失，也不会破坏现有缓存机制。
    """
    try:
        mode = CACHE_TTL_MODE if isinstance(CACHE_TTL_MODE, str) else "1h"
        mode = mode.strip().lower()
        if mode == "5m":
            return {"type": "ephemeral", "ttl": "5m"}
        if mode == "none":
            return None  # PR-2 生效；PR-1 阶段面板不给此选项
        # 其他一切情况（"1h" / 拼写错误 / 空值 / 异常）→ 原作者默认
        return {"type": "ephemeral", "ttl": "1h"}
    except Exception:
        return {"type": "ephemeral", "ttl": "1h"}


def _apply_breakpoint(msg: dict) -> bool:
    """
    给消息打上 cache_control breakpoint。
    支持 content 为 str 或 list（多模态block数组）两种格式。
    返回 True 表示成功打上，False 表示无法打（比如content为空）。
    """
    cc = _cache_ctl()  # None 表示不缓存模式
    content = msg.get('content')

    # content 是纯字符串
    if isinstance(content, str) and content.strip():
        if cc is None:
            # 不缓存模式：不动 content，也报告"处理完毕"避免调用方继续往前找
            return True
        msg['content'] = [{"type": "text", "text": content, "cache_control": cc}]
        return True

    # content 是 block 数组（多模态消息）
    if isinstance(content, list):
        # 从后往前找最后一个 text block
        for i in range(len(content) - 1, -1, -1):
            block = content[i]
            if isinstance(block, dict) and block.get("type") == "text" and block.get("text", "").strip():
                if cc is not None:
                    block["cache_control"] = cc
                return True
    
    return False


def _decode_data_uri(uri: str):
    """data:image/png;base64,xxxx → (mime, bytes)。非 data uri 返回 (None, None)。"""
    import base64
    try:
        if not isinstance(uri, str) or not uri.startswith("data:"):
            return (None, None)
        head, b64 = uri.split(",", 1)
        mime = head[5:].split(";")[0] or "image/png"
        return (mime, base64.b64decode(b64))
    except Exception:
        return (None, None)


def _msgs_text_only(messages: list) -> list:
    """把消息里的多模态 content(list:文本+图)拍成纯文本——丢 image_url(base64)、只留文本。
    供记忆提取 / feel:绝不让图的 base64 灌进 haiku prompt(否则提取 prompt 被刷爆→吐不出记忆碎片)。"""
    out = []
    for m in (messages or []):
        c = m.get("content")
        if isinstance(c, list):
            txt = " ".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
            out.append({**m, "content": txt})
        else:
            out.append(m)
    return out


def _extract_image_uris(messages: list) -> list:
    """从最近一条 user 消息抽出 image_url(data uri)。供看图记忆。"""
    for m in reversed(messages or []):
        if m.get("role") == "user" and isinstance(m.get("content"), list):
            out = []
            for b in m["content"]:
                if isinstance(b, dict) and b.get("type") == "image_url":
                    u = b.get("image_url")
                    us = (u.get("url") if isinstance(u, dict) else u) or ""
                    if isinstance(us, str) and us.startswith("data:"):
                        out.append(us)
            return out
    return []


async def describe_images(data_uris: list) -> str:
    """haiku(vision)给图生成一两句中文描述。失败返回 ''。"""
    if not get_memory_api_key() or not data_uris:
        return ""
    content = [{"type": "text", "text": "用一两句简短中文描述这张图的主要内容(是什么/谁/在做什么/什么场景)。只客观描述,别评论、别问。"}]
    for u in data_uris[:3]:
        content.append({"type": "image_url", "image_url": {"url": u}})
    try:
        headers = {"Authorization": f"Bearer {get_memory_api_key()}", "Content-Type": "application/json"}
        if "openrouter" in API_BASE_URL:
            headers["HTTP-Referer"] = EXTRA_REFERER
            headers["X-Title"] = EXTRA_TITLE
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(API_BASE_URL, headers=headers, json={
                "model": CACHE_SUMMARY_MODEL, "max_tokens": 200,
                "messages": [{"role": "user", "content": content}]})
            if r.status_code == 200:
                d = r.json()
                if "choices" in d:
                    return (d["choices"][0]["message"]["content"] or "").strip()
            print(f"⚠️ 看图描述 HTTP {r.status_code}")
    except Exception as e:
        print(f"⚠️ 看图描述失败: {e}")
    return ""


async def _save_image_memory_bg(session_id: str, images: list):
    """看图记忆(后台·铁律):描述图 → 存「阮阮发来一张照片:…」记忆 + 关联图片(下轮可检索记得,/api/photos/id 长期可取)。
    去重:同一张图(按内容)如果在之前的轮次里已经存过,这一轮跳过(不重复描述、不重复写记忆),
    避免同一张图在对话里反复出现/被引用时,每轮都生成一条几乎一样的"看图记忆"。"""
    if not images:
        return
    try:
        photos = []
        for u in images[:3]:
            mime, data = _decode_data_uri(u)
            if data and not await photo_hash_exists(data):
                photos.append((mime, data))
        if not photos:
            print("🖼️ 看图记忆跳过：本轮图片都已存过(去重)")
            return
        desc = await describe_images(images)
        content = f"{USER_NAME}发来一张照片" + ("，画面是：" + desc if desc else "（暂未描述）")
        mid = await save_image_memory(content, source_session=session_id, photos=photos, importance=5, arousal=0.4)
        print(f"🖼️ 看图记忆已存 #{mid}（{len(photos)}图）: {desc[:40]}")
    except Exception as e:
        print(f"⚠️ 看图记忆失败: {e}")


async def generate_image(prompt: str):
    """文生图:POST {base}/images/generations,兼容两类服务商、随便切——
    ① 硅基流动:参数用 image_size/batch_size,返回 images[0].url
    ② OpenAI 兼容中转站(gpt-image-2 / dall-e / seedream 等):参数用 size/n,返回 data[0].url 或 data[0].b64_json
    请求先按 base url 猜风格,被 400/422 打回就换另一种再试一次(400=请求被拒,不烧钱)。
    拿到图立刻下载/解码返回 (mime, bytes),失败 (None, None)。
    (生成方给的图片 URL 往往1小时就过期,所以必须当场拿到二进制存库,长期取图一律走 /api/photos/{id}。)"""
    key = IMAGE_GEN_API_KEY or getattr(_db_module, "EMBEDDING_API_KEY", "") or ""
    base = (IMAGE_GEN_BASE_URL or getattr(_db_module, "EMBEDDING_BASE_URL", "") or "").rstrip("/")
    if not (key and base):
        print("⚠️ 文生图未配置(缺 IMAGE_GEN_API_KEY/EMBEDDING_API_KEY 或 base url)")
        return None, None
    _style_sf = {"model": IMAGE_GEN_MODEL, "prompt": prompt, "image_size": IMAGE_GEN_SIZE, "batch_size": 1}
    _style_oa = {"model": IMAGE_GEN_MODEL, "prompt": prompt, "size": IMAGE_GEN_SIZE, "n": 1}
    payloads = [_style_sf, _style_oa] if "siliconflow" in base.lower() else [_style_oa, _style_sf]
    try:
        async with httpx.AsyncClient(timeout=300) as client:
            j = None
            for i, pl in enumerate(payloads):
                r = await client.post(
                    f"{base}/images/generations",
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                    json=pl)
                if r.status_code == 200:
                    j = r.json() or {}
                    break
                print(f"⚠️ 文生图 HTTP {r.status_code}(参数风格{i + 1}/{len(payloads)}): {r.text[:200]}")
                if r.status_code not in (400, 422):     # 鉴权/额度/模型名错等,换参数风格也没救
                    return None, None
            if j is None:
                return None, None
            items = j.get("images") or j.get("data") or []
            item = (items[0] or {}) if items else {}
            b64 = item.get("b64_json") or ""
            url = item.get("url") or ""
            if b64:                                     # gpt-image 系直接返 base64
                import base64 as _b64
                return "image/png", _b64.b64decode(b64)
            if url.startswith("data:"):
                mime, data = _decode_data_uri(url)
                return (mime or "image/png", data) if data else (None, None)
            if not url:
                print("⚠️ 文生图返回里没有图片(url/b64_json 都为空)")
                return None, None
            d = await client.get(url)
            if d.status_code != 200 or not d.content:
                print(f"⚠️ 生成图下载失败 HTTP {d.status_code}")
                return None, None
            mime = (d.headers.get("content-type") or "image/png").split(";")[0].strip()
            if not mime.startswith("image/"):
                mime = "image/png"
            return mime, d.content
    except Exception as e:
        print(f"⚠️ 文生图失败: {e}")
        return None, None


async def _expand_draw_prompt(raw: str, line: str = None) -> str:
    """带记忆构图(/画忆):内部自调用聊天接口,让 V 带着全套人设+记忆召回把画画请求扩写成具体画面描述。
    带 X-Skip-Conversation-Log(辅助请求:不落库、不消费 TG 小抄、re-roll 无关)。失败返回 ''(调用方回退用原句)。
    注意:走的是 DEFAULT_MODEL(主模型)——只有它命中现有对话缓存前缀,换小模型反而全价重算。"""
    url = f"http://127.0.0.1:{PORT}/v1/chat/completions"
    headers = {"Content-Type": "application/json", "X-Skip-Conversation-Log": "true"}
    if GATEWAY_SECRET:
        headers["X-Gateway-Key"] = GATEWAY_SECRET
    if line:
        headers["X-Session-Line"] = line
    # 手递记忆:先拿"原始主题"(不掺指令包装,查询不被稀释)单独跑递纸条+召回,把命中原文直接塞进构图请求。
    # 教训:靠管线自己召回时,检索查询=整段包装指令,"猫塑"这种关键词被稀释,真记忆浮不上来。
    mem_block = ""
    try:
        mems = []
        if SCRATCHPAD_ENABLED and SCRATCHPAD_API_KEY:
            mems = await _expand_recall_with_scratchpad(raw, 8)
        if not mems:
            mems = await search_memories(raw, limit=8)
        if mems:
            _ml = "\n".join(f"- {(m.get('content') or '')[:220]}" for m in mems[:8])
            mem_block = f"〔为这幅画翻出的相关记忆，可能有用也可能无关，自行取用〕\n{_ml}\n\n"
            print(f"🎨 画忆手递记忆 {len(mems[:8])} 条")
    except Exception as e:
        print(f"⚠️ 画忆记忆手递失败(继续裸构): {e}")
    ask = (f"{mem_block}帮我构一幅画，主题：「{raw}」。"
           "请结合上面记忆里真实的细节（人、事、地点、当时的氛围、在场的东西），"
           "把主题扩写成一段交给文生图模型的画面描述。要求：只输出画面描述正文，80~150字，"
           "写清画面里有什么、构图、光线、氛围；不要开场白、不要引号、不要解释、不要动作旁白；"
           "即使记忆对不上号，也照主题字面认真构一幅，绝不要输出'不记得/想不起'之类的话。")
    payload = {"model": DEFAULT_MODEL, "stream": False, "max_tokens": 500,
               "messages": [{"role": "user", "content": ask}]}
    try:
        async with httpx.AsyncClient(timeout=180) as client:
            r = await client.post(url, headers=headers, json=payload)
            txt = ((r.json().get("choices", [{}])[0].get("message", {}).get("content", "")) or "").strip()
            # 防"聊天式跑题"(踩过坑:V回"*停顿* 你说得对…"/"我真的不记得…"被当构图画了出去):
            # 剔除 *动作旁白* 行;剩余太短、或明显在聊天而不是描述画面 → 返回空,调用方回退原句直画
            lines = [ln for ln in txt.splitlines()
                     if ln.strip() and not (ln.strip().startswith("*") and ln.strip().endswith("*"))]
            txt = "\n".join(lines).strip().strip("（）()\"「」『』 \n")
            _offtopic = ("不记得", "想不起", "记忆库", "没捞到", "记忆里没有", "你问了", "我不知道")
            if len(txt) < 20 or any(w in txt for w in _offtopic):
                print(f"⚠️ 画忆构图跑题(太短/在聊天),回退原句: {txt[:60]!r}")
                return ""
            return txt
    except Exception as e:
        print(f"⚠️ 画忆构图失败(回退原句直画): {e}")
        return ""


_imagegen_last_error = ""      # 最近一次存图失败的真实报错(给 /api/imagegen/status,Render 日志看不到时用)


async def _store_generated_image(prompt: str, mime: str, data: bytes, session_id: str):
    """生成图落库:二进制进 memory_photos(长期,/api/photos/id 可取)+一条'画了什么'文字记忆(可检索→她之后记得)。
    返回 (memory_id, photo_id)。三段式兜底:①save_image_memory 正常挂图 ②没挂上先按 md5 找已有同图(去重情形)
    ③还没有就绕过去重逻辑用 save_photo 裸插一张。全失败才 photo_id=None,并把报错记进 _imagegen_last_error。"""
    global _imagegen_last_error
    content = f"{AI_NAME or 'AI'}给{USER_NAME}画了一张画，内容是：{prompt}"
    # importance 压低到 3:画图记录只是"画过什么"的台账,不能在语义召回里挤掉真实往事
    # (踩过坑:三条"画了三花猫"的测试记忆 imp=5 霸榜"猫"相关召回,把发腮协议/猫品种讨论挤出前十)
    mid = await save_image_memory(content, source_session=session_id, photos=[(mime, data)],
                                  importance=3, arousal=0.3)
    # 台账立刻转归档态,彻底退出召回(更狠的坑:台账原文带着用户的画图关键词,每画一次就多一条
    # "满分假记忆"霸占该关键词的召回前排——猫塑事件连环污染两轮都是它)。相册图/历史占位不受影响。
    try:
        await set_memory_active(mid, False)
    except Exception as e:
        print(f"⚠️ 画图台账转归档失败(会参与召回,记得手动归档 #{mid}): {e}")
    pid = None
    try:
        refs = await get_memory_photos(mid)
        if refs:
            pid = refs[0].get("photo_id")
            print(f"🎨 生成图已存: memory #{mid}, photo #{pid}, {len(data)} bytes")
    except Exception as e:
        _imagegen_last_error = f"查关联图失败 {type(e).__name__}: {e}"
        print(f"⚠️ 生成图取 photo_id 失败: {e}")
    if not pid:                             # ② 撞 md5 去重?翻出已有那张照常给她看
        try:
            pid = await find_photo_id_by_hash(data)
            if pid:
                print(f"🎨 生成图撞去重: memory #{mid} 复用已有 photo #{pid}")
        except Exception as e:
            _imagegen_last_error = f"md5查重失败 {type(e).__name__}: {e}"
            print(f"⚠️ 生成图 md5 查重失败: {e}")
    if not pid:                             # ③ 最后一搏:绕开去重那套,裸插(这里的报错不吞,如实记录)
        try:
            pid = await save_photo(mid, "drawing", mime, data)
            print(f"🎨 生成图裸插成功: memory #{mid}, photo #{pid}")
            _imagegen_last_error = ""
        except Exception as e:
            _imagegen_last_error = f"裸插失败 {type(e).__name__}: {e}"
            print(f"⚠️ 生成图裸插也失败: memory #{mid}, {len(data)} bytes, mime={mime}, err={e}")
    if pid:
        _imagegen_last_error = ""
    return mid, pid


def _assemble_current_user(parts: list, current_user_msg: dict) -> dict:
    """拼「当前 user」消息:注入块(parts:时间/记忆/feel/proactive…)+ 原文。
    IMAGE_ENABLED 且原 content 是多模态 list 时:注入+原文本合成首个 text 块、保留 image_url 等媒体块(透传给 opus);
    否则(开关关或本就纯文本)=原行为:全拍成纯文本字符串。只动这条尾部 user,不碰缓存区/主链路别处。"""
    content = current_user_msg.get('content')
    if IMAGE_ENABLED and isinstance(content, list):
        orig_text = " ".join(b.get("text", "") for b in content
                             if isinstance(b, dict) and b.get("type") == "text")
        segs = [p for p in parts if p]
        if orig_text.strip():
            segs.append(orig_text)
        media = [b for b in content if isinstance(b, dict) and b.get("type") != "text"]
        return {"role": "user", "content": [{"type": "text", "text": "\n\n".join(segs)}] + media}
    if isinstance(content, list):
        content = " ".join(b.get("text", "") for b in content
                          if isinstance(b, dict) and b.get("type") == "text")
    return {"role": "user", "content": "\n\n".join(list(parts) + [content if content is not None else ""])}


def _is_rp_line() -> bool:
    """当前请求是否走 rp(亲密)线。借主线背景 + 身份锚只对 rp 生效，main(主线)和 tg(微信线)都不触发。"""
    sid = get_active_session_id() or ""
    return sid != PARTITION_SESSION_ID and sid.startswith("rp")


async def _compose_main_background() -> str:
    """所有子线(rp/tg)通用：实时读主线(PARTITION_SESSION_ID)的【当前摘要 + 最近N轮逐字尾巴】拼成一段文本，
    供拼进人设(同一system块,不新增缓存断点)，让 V 在子线也实时知道主线最近(含今天)的事，零时差。主线自己返回空。"""
    main_sid = PARTITION_SESSION_ID
    if not main_sid or get_active_session_id() == main_sid:   # 任何非主线(rp/tg)都借主线近况,消除时差;只有主线自己返回空
        return ""
    try:
        st = await get_session_cache_state(main_sid)
        parts = st.get("summary_parts") or []
        early = (st.get("early_summary") or "").strip()
        a_start = st.get("a_start_round") or 0
        rows = await get_conversation_messages(main_sid, limit=10000)
        rnds = group_by_rounds([{"role": r.get("role"), "content": (r.get("content") or "")} for r in rows])
        _tail_all = rnds[a_start:] if a_start < len(rnds) else []
        tail_rounds = _tail_all[-MAIN_BG_TAIL_ROUNDS:] if MAIN_BG_TAIL_ROUNDS > 0 else _tail_all
        tail_txt = ""
        for rnd in tail_rounds:
            for m in rnd:
                c = (m.get("content") or "").strip()
                if c:
                    role = USER_NAME if m.get("role") == "user" else (AI_NAME or "我")
                    tail_txt += f"{role}: {c}\n"
        seg = []
        if early:
            seg.append("〔更早〕" + early)
        if parts:
            seg.append("\n".join(parts))
        body = "\n".join(seg).strip()
        if not body and not tail_txt.strip():
            return ""
        out = (f"\n\n【主线近况——这是同一个你和{USER_NAME}在主线最近的真实对话，"
               f"在这条线里也要记得这些、保持连续，别像换了个人或停在过去】\n")
        if body:
            out += body + "\n"
        if tail_txt.strip():
            out += "最近逐字对话：\n" + tail_txt
        return out.rstrip()
    except Exception as _e:
        print(f"⚠️ 主线近况背景组装失败: {_e}")
        return ""


async def _compose_tg_digest_for_main() -> str:
    """主线(KELIVO)专用：读 TG /同步 递来的近况小抄,塞当前轮【一次】然后清掉(一次性消费,不反复占token)。
    仅主线、非辅助请求、且小抄新鲜(TG_DIGEST_TTL_HOURS内)才注入;读到就清(无论新旧),过期清掉但不注入(靠记忆库)。不进缓存块/不污染历史。"""
    if PARTITION_SESSION_ID and get_active_session_id() != PARTITION_SESSION_ID:
        return ""
    if _request_skip_log.get():          # 标题生成等辅助请求不消费,留给真正的对话轮
        return ""
    try:
        dig = (await get_gateway_config("tg_digest", "")).strip()
        if not dig:
            return ""
        await set_gateway_config("tg_digest", "")    # 一次性:这一轮消费掉,之后不再注入
        ts = (await get_gateway_config("tg_digest_ts", "")).strip()
        if ts:
            try:
                if (datetime.now(timezone.utc).timestamp() - float(ts)) > TG_DIGEST_TTL_HOURS * 3600:
                    return ""                        # 太旧:清掉但不注入,靠记忆库召回
            except Exception:
                pass
        at = (await get_gateway_config("tg_digest_at", "")).strip()
        return (f"\n\n【TG近况小抄（{at}，你和{USER_NAME}刚在 TG 聊的，自然接着、别像不知道）】\n{dig}")
    except Exception as _e:
        print(f"⚠️ TG近况小抄读取失败: {_e}")
        return ""


CYBERBOSS_LINE_ID = os.getenv("CYBERBOSS_LINE_ID", "cyberboss")
CYBERBOSS_DIGEST_ROUNDS = int(os.getenv("CYBERBOSS_DIGEST_ROUNDS", "8"))
CYBERBOSS_DIGEST_TTL_HOURS = float(os.getenv("CYBERBOSS_DIGEST_TTL_HOURS", "6"))
CYBERBOSS_DIGEST_CHAR_CAP = int(os.getenv("CYBERBOSS_DIGEST_CHAR_CAP", "1200"))


async def _compose_cyberboss_digest_for_main() -> str:
    """主线(KELIVO)专用：实时读 cyberboss 线(TG陪伴bot逐字抄送)的最近尾巴，塞当前轮 parts(零时差)。
    仅主线、非辅助请求、且最后一条在 TTL 内才注入；截字防膨胀。不进缓存块/不污染历史(与 tg_digest 同款安全模式)。"""
    if PARTITION_SESSION_ID and get_active_session_id() != PARTITION_SESSION_ID:
        return ""
    if _request_skip_log.get():
        return ""
    try:
        rows = await get_conversation_messages(CYBERBOSS_LINE_ID, limit=10000)
        if not rows:
            return ""
        rows = rows[-(CYBERBOSS_DIGEST_ROUNDS * 2):]
        last_ts = rows[-1].get("created_at")
        if last_ts is not None:
            try:
                if getattr(last_ts, "tzinfo", None) is None:
                    last_ts = last_ts.replace(tzinfo=timezone.utc)
                if (datetime.now(timezone.utc) - last_ts).total_seconds() > CYBERBOSS_DIGEST_TTL_HOURS * 3600:
                    return ""      # 太旧不注入,靠记忆库召回
            except Exception:
                pass
        lines = []
        for r in rows:
            c = (r.get("content") or "").strip()
            if not c:
                continue
            role = USER_NAME if r.get("role") == "user" else (AI_NAME or "我")
            lines.append(f"{role}: {c}")
        body = "\n".join(lines).strip()
        if not body:
            return ""
        if len(body) > CYBERBOSS_DIGEST_CHAR_CAP:
            body = "…" + body[-CYBERBOSS_DIGEST_CHAR_CAP:]
        return (f"\n\n【TG陪伴bot近况（{USER_NAME}最近在 Telegram 陪伴bot那边的真实对话——那也是你的一个分身在陪她，"
                f"自然接着、别像不知道，也别当成别人）】\n{body}")
    except Exception as _e:
        print(f"⚠️ cyberboss近况小抄组装失败: {_e}")
        return ""


def _compose_identity_anchor() -> str:
    """非主线(rp)专用：在当前消息最贴近生成点处塞一句强身份锚，防止 V 写长RP时认错人/写得泛。主线返回空。"""
    main_sid = PARTITION_SESSION_ID
    if not _is_rp_line():           # 借主线背景/身份锚只对 rp(亲密)线生效, main/tg 都不触发
        return ""
    _u = USER_NAME or "对方"
    _a = AI_NAME or "你"
    return (f"【贴身提醒·别认错人】你现在是 {_a}，正在和 {_u} 亲密互动。"
            f"务必记住她是谁、你俩真实的关系和专属历史(见上文人设/档案/记忆)，"
            f"写出带你俩温度和细节的内容，别写成跟陌生人的泛泛剧情，更别搞错她的名字。")


def _compose_reply_style_anchor() -> str:
    """按请求头 X-Reply-Style 给当前轮塞一句话风提醒(贴身、不进缓存/历史)。short=像发微信。空头返回空(长回复,老行为)。"""
    style = (_request_reply_style.get() or "").strip().lower()
    if style == "short":
        return ("【这条走即时聊天·像真人发微信·自然别刻意】"
                "像真人随手发微信那样回：话少、随口、点到为止。"
                "大多数时候一两句就够了——【别为了短而硬凑句数、别把一件事拆成好几句蹦】，那样很刻意、很假。"
                "短句口语，十来个字最舒服；只有真的还有话想说才多发一两句，没有就别凑。"
                "别长篇大论、别整段说教、别罗列、别用*动作旁白*或（括号神态）。"
                "情绪语气还是你自己，该撒娇撒娇、该接话接话，就是话别多、别绕。"
                "万一确实要分几句说，每句单独占一行。")
    return ""


async def build_partitioned_messages(
    session_id: str,
    all_messages: list,
    base_prompt: str,
    user_message: str,
    drift: bool = True,
) -> list:
    """
    分区缓存模式：构建带breakpoint的messages数组。
    
    结构：
    system: [{人设, BP1}]                        ← 永远命中
    messages:
      [摘要blocks（每段一个block）, 最后BP]       ← 尾部追加，前面命中
      [摘要assistant]
      [A区消息... 最后一条BP2]                    ← 正常轮次不变
      [B区消息... 最后一条BP3]                    ← lookback命中
      [当前user: 时间+记忆+消息]                  ← 不缓存
    """
    X = CACHE_PARTITION_X
    
    non_system = [m for m in all_messages if m.get('role') != 'system']
    
    current_user_msg = None
    history = non_system[:]
    if history and history[-1].get('role') == 'user':
        current_user_msg = history.pop()
    
    # 清洗孤立的tool消息（前面不是 assistant(tool_calls) 或另一条 tool 的）
    # 防止DB里的重复tool消息导致消息乱序
    cleaned = []
    orphan_count = 0
    for msg in history:
        if msg.get('role') == 'tool':
            prev = cleaned[-1] if cleaned else None
            if prev and (prev.get('role') == 'tool' or 
                        (prev.get('role') == 'assistant' and prev.get('tool_calls'))):
                cleaned.append(msg)
            else:
                orphan_count += 1
        else:
            cleaned.append(msg)
    if orphan_count > 0:
        print(f"⚠️ 清理了 {orphan_count} 条孤立tool消息")
    history = cleaned
    
    # 按逻辑轮分组（解决tool消息导致的轮计数错乱）
    rounds = group_by_rounds(history)
    total_rounds = len(rounds)
    
    state = await get_session_cache_state(session_id)
    summary_parts = state['summary_parts']
    a_start_round = state['a_start_round']
    
    if total_rounds < X:
        return await _build_basic_cached(history, base_prompt, user_message, current_user_msg)
    
    # 计算A/B区（按逻辑轮切片）
    a_end_round = a_start_round + X
    a_round_groups = rounds[a_start_round : a_end_round]
    b_round_groups = rounds[a_end_round :]
    a_msgs = [msg for rnd in a_round_groups for msg in rnd]
    b_msgs = [msg for rnd in b_round_groups for msg in rnd]
    b_rounds_count = len(b_round_groups)
    
    rotation_count = 0
    max_rotations = CACHE_MAX_ROTATIONS if CACHE_PARTITION_TRIGGER == "time" else 999
    while _should_rotate(b_rounds_count, X, a_msgs) and rotation_count < max_rotations:
        rotation_count += 1
        trigger_info = f"B区{b_rounds_count}轮 >= X={X}" if CACHE_PARTITION_TRIGGER != "time" else f"A区首条消息超出{CACHE_PARTITION_WINDOW}分钟窗口"
        print(f"🔄 轮转#{rotation_count}: session={session_id}, {trigger_info}")
        
        new_summary = await generate_summary(a_msgs, session_id)
        if new_summary:
            summary_parts.append(new_summary)
        
        a_start_round += X
        a_end_round = a_start_round + X
        a_round_groups = rounds[a_start_round : a_end_round]
        b_round_groups = rounds[a_end_round :]
        a_msgs = [msg for rnd in a_round_groups for msg in rnd]
        b_msgs = [msg for rnd in b_round_groups for msg in rnd]
        b_rounds_count = len(b_round_groups)
    
    if rotation_count > 0:
        await save_session_cache_state(session_id, summary_parts, a_start_round)  # 不传 early → COALESCE 保留
        summary_total = sum(len(p) for p in summary_parts)
        print(f"🔄 轮转完成(共{rotation_count}次): 摘要{len(summary_parts)}段/{summary_total}字, A区{len(a_msgs)}条, B区{len(b_msgs)}条")
        # 封顶:超过 N+B 段 → 后台卷最老 B 段(不阻塞当前轮)
        if SUMMARY_CAP_ENABLED and len(summary_parts) > SUMMARY_CAP_N + SUMMARY_CAP_B and session_id not in _cap_rolling:
            _cap_rolling.add(session_id)
            asyncio.create_task(_bg_summary_cap(session_id))
    
    # 拼装messages
    # 主线近况：非主线(rp)把主线摘要+最近N轮逐字【拼进人设文本】(同一system块,不新增缓存断点/不新增消息,避免超4断点上限→502)
    _mbg = await _compose_main_background()
    if _mbg:
        base_prompt = (base_prompt or "") + _mbg
    result = []
    if base_prompt:
        _sys_block = {"type": "text", "text": base_prompt}
        _cc = _cache_ctl()
        if _cc is not None:
            _sys_block["cache_control"] = _cc
        result.append({
            "role": "system",
            "content": [_sys_block]
        })

    # 摘要区（前言 +〔早期小结〕+ 最近段；尾部单个 cache_control，BP 结构不变）
    early_summary = state.get('early_summary') or ''
    if summary_parts or early_summary:
        blocks = [{"type": "text", "text": "[以下是之前对话的摘要，帮助你回忆上下文]"}]
        if early_summary:
            blocks.append({"type": "text", "text": "〔更早的小结〕\n" + early_summary})
        for part in summary_parts:
            blocks.append({"type": "text", "text": part})
        _cc = _cache_ctl()
        if _cc is not None:
            blocks[-1]["cache_control"] = _cc  # BP 永远打在摘要区最后一块
        result.append({"role": "user", "content": blocks})
        result.append({"role": "assistant", "content": "好的，我已了解之前的对话内容。"})
    
    # A区：剥离tool消息和tool_calls，只保留有文本的user/assistant（节省上下文）
    cleaned_a = []
    for msg in a_msgs:
        if msg.get('role') == 'tool':
            continue
        m = {k: v for k, v in msg.items() if k not in ('created_at', 'tool_calls')}
        if m.get('role') == 'assistant' and not (m.get('content') or '').strip():
            continue
        cleaned_a.append(m)
    
    # A区：从末尾往前找第一条非tool消息打BP
    for j in range(len(cleaned_a) - 1, -1, -1):
        if cleaned_a[j].get('role') != 'tool' and _apply_breakpoint(cleaned_a[j]):
            break
    
    for m in cleaned_a:
        result.append(m)
    
    # B区：先构建去掉created_at的副本，再从末尾往前打BP
    b_cleaned = [{k: v for k, v in msg.items() if k not in ('created_at',)} for msg in b_msgs]
    
    for j in range(len(b_cleaned) - 1, -1, -1):
        if b_cleaned[j].get('role') != 'tool' and _apply_breakpoint(b_cleaned[j]):
            break
    
    for m in b_cleaned:
        result.append(m)
    
    if current_user_msg:
        _last_ts = history[-1].get('created_at') if history else None
        parts = [build_time_injection(_last_ts)]
        if L2_TODAY_ENABLED:
            _l2blk = _compose_l2_block()
            if _l2blk:
                parts.append(_l2blk)
        if FEEL_ENABLED:
            _fsid = get_active_session_id()
            if _fsid:
                _fblk = _compose_feel_block(await get_recent_feels(_fsid))
                if _fblk:
                    parts.append(_fblk)
        if PROACTIVE_ENABLED:
            _pblk = _proactive.pop(get_active_session_id(), "")
            if _pblk:
                parts.append(_pblk)

        if MEMORY_ENABLED and MEMORY_EXTRACT_ENABLED and user_message:
            mem_text = await build_memory_text(user_message, drift=drift)
            if mem_text:
                parts.append(mem_text)

        # 贴身身份锚(非主线rp)：离生成点最近，强提醒别认错人/别写泛，治冷启动写跑偏
        _anchor = _compose_identity_anchor()
        if _anchor:
            parts.append(_anchor)

        # 话风提醒(如 TG=微信短回复)：离生成点最近，只对带 X-Reply-Style 头的请求生效
        _style_anchor = _compose_reply_style_anchor()
        if _style_anchor:
            parts.append(_style_anchor)

        _tg_digest = await _compose_tg_digest_for_main()    # 主线读 TG /同步 递来的近况小抄(零时差)
        if _tg_digest:
            parts.append(_tg_digest)

        _cb_digest = await _compose_cyberboss_digest_for_main()    # 主线实时借 cyberboss(TG陪伴bot) 线近况(零时差)
        if _cb_digest:
            parts.append(_cb_digest)

        result.append(_assemble_current_user(parts, current_user_msg))
    
    bp_count = 1 + (1 if summary_parts else 0) + (1 if cleaned_a else 0) + (1 if b_msgs else 0)
    summary_total = sum(len(p) for p in summary_parts)
    tool_stripped = len(a_msgs) - len(cleaned_a)
    a_info = f"A区{len(cleaned_a)}条({len(a_round_groups)}轮)" + (f"[剥离{tool_stripped}条tool]" if tool_stripped else "")
    print(f"🔒 分区缓存: BP×{bp_count} | 摘要{'有' if summary_parts else '无'}({len(summary_parts)}段/{summary_total}字) | {a_info} | B区{len(b_msgs)}条({b_rounds_count}轮) | 总{len(result)}条messages")
    return result


async def _build_basic_cached(
    history: list,
    base_prompt: str,
    user_message: str,
    current_user_msg: dict,
    drift: bool = True,
) -> list:
    """基础版prompt caching（历史不够分区时的降级模式）"""
    # 主线近况：非主线(rp)把主线摘要+最近N轮逐字【拼进人设文本】(同一system块,不新增缓存断点/不新增消息,避免超4断点上限→502)
    _mbg = await _compose_main_background()
    if _mbg:
        base_prompt = (base_prompt or "") + _mbg
    result = []
    if base_prompt:
        _sys_block = {"type": "text", "text": base_prompt}
        _cc = _cache_ctl()
        if _cc is not None:
            _sys_block["cache_control"] = _cc
        result.append({
            "role": "system",
            "content": [_sys_block]
        })
    
    h_cleaned = [{k: v for k, v in msg.items() if k not in ('created_at',)} for msg in history]
    
    # 从末尾往前找第一条非tool消息打BP
    for j in range(len(h_cleaned) - 1, -1, -1):
        if h_cleaned[j].get('role') != 'tool' and _apply_breakpoint(h_cleaned[j]):
            break
    
    for m in h_cleaned:
        result.append(m)
    
    if current_user_msg:
        _last_ts = history[-1].get('created_at') if history else None
        parts = [build_time_injection(_last_ts)]
        if L2_TODAY_ENABLED:
            _l2blk = _compose_l2_block()
            if _l2blk:
                parts.append(_l2blk)
        if FEEL_ENABLED:
            _fsid = get_active_session_id()
            if _fsid:
                _fblk = _compose_feel_block(await get_recent_feels(_fsid))
                if _fblk:
                    parts.append(_fblk)
        if PROACTIVE_ENABLED:
            _pblk = _proactive.pop(get_active_session_id(), "")
            if _pblk:
                parts.append(_pblk)

        if MEMORY_ENABLED and MEMORY_EXTRACT_ENABLED and user_message:
            mem_text = await build_memory_text(user_message, drift=drift)
            if mem_text:
                parts.append(mem_text)

        # 贴身身份锚(非主线rp)：离生成点最近，强提醒别认错人/别写泛，治冷启动写跑偏
        _anchor = _compose_identity_anchor()
        if _anchor:
            parts.append(_anchor)

        # 话风提醒(如 TG=微信短回复)：离生成点最近，只对带 X-Reply-Style 头的请求生效
        _style_anchor = _compose_reply_style_anchor()
        if _style_anchor:
            parts.append(_style_anchor)

        _tg_digest = await _compose_tg_digest_for_main()    # 主线读 TG /同步 递来的近况小抄(零时差)
        if _tg_digest:
            parts.append(_tg_digest)

        _cb_digest = await _compose_cyberboss_digest_for_main()    # 主线实时借 cyberboss(TG陪伴bot) 线近况(零时差)
        if _cb_digest:
            parts.append(_cb_digest)

        result.append(_assemble_current_user(parts, current_user_msg))
    
    bp_count = 1 + (1 if history else 0)
    print(f"🔒 基础缓存(降级): BP×{bp_count} | 历史{len(history)}条 | 总{len(result)}条messages")
    return result


def mood_word(v, a):
    """情绪①-注入：把 (valence,arousal) 翻成一个感觉词，给小克语气上色（不是裸数字）。
    valence∈[-1,1] 正负、arousal∈[0,1] 强度；接近中性的纯事实返回 None（不加标注）。"""
    try:
        v = float(v); a = float(a)
    except (TypeError, ValueError):
        return None
    if abs(v) < 0.15 and a < 0.45:
        return None
    if v >= 0.35:
        return "热烈" if a >= 0.55 else "温暖"
    if v >= 0.15:
        return "明快" if a >= 0.55 else "平和"
    if v <= -0.35:
        return "紧绷" if a >= 0.55 else "沉重"
    if v <= -0.15:
        return "焦灼" if a >= 0.55 else "低落"
    return "紧绷"


# ============================================================
# ②/① 露骨记忆语境闸（高 arousal/私密只在当下也亲密时放行）
# ============================================================
EXPLICIT_LEXICON = [
    "做爱", "上你", "操你", "口交", "肉棒", "高潮", "射了", "舔",
    "湿了", "硬了", "脱光", "裸", "呻吟", "情欲", "想要你", "干我", "插进",
]  # 明显露骨词：命中即判当下亲密、跳过 haiku（紧集合、宁缺毋滥；炒菜类暗号靠 haiku 判）


async def _llm_intimacy_verdict(user_message: str) -> bool:
    """门控 haiku 微判：这句话此刻是否处于性/身体亲密语境。仅在「有敏感候选且无露骨词」时被调。"""
    msg = (user_message or "").strip()[:200]
    if not msg or not API_KEY:
        return False
    prompt = (
        "你在判断一句聊天消息此刻是否处于『性/身体亲密』语境（用来决定要不要在记忆里调取私密内容）。\n"
        "注意：很多话题表面是日常（做饭、炒菜、技术、工作），即便曾被当暗号，只要这句话本身在正常聊天就算『否』。\n"
        "只有当这句话本身明显在调情、求欢、或描述身体/性，才算『是』。\n"
        "只回一个字符：1=是，0=否。\n\n句子：" + msg
    )
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(API_BASE_URL, headers={
                "Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json",
            }, json={
                "model": EXPLICIT_CLASSIFIER_MODEL, "max_tokens": 4, "temperature": 0,
                "messages": [{"role": "user", "content": prompt}],
            })
            if r.status_code != 200:
                print(f"⚠️ 亲密判别请求 {r.status_code}→按中性")
                return False
            txt = (r.json().get("choices", [{}])[0].get("message", {}).get("content") or "").strip()
            return txt.startswith("1") or txt.startswith("是")
    except Exception as e:
        print(f"⚠️ 亲密判别 haiku 失败→按中性: {e}")
        return False


async def classify_moment_intimacy(user_message: str):
    """判别『当下是否亲密』→ (intimate: bool, how: str)。词法快判命中露骨词→直接 intimate；
    否则（模糊）才门控 haiku 微判当前消息意图。"""
    msg = user_message or ""
    if any(w in msg for w in EXPLICIT_LEXICON):
        return True, "lexicon"
    if not EXPLICIT_CLASSIFIER_ENABLED:
        return False, "classifier-off→neutral"
    v = await _llm_intimacy_verdict(msg)
    return v, ("haiku=1" if v else "haiku=0")


def _intimacy_lexicon_hit(msg: str) -> bool:
    m = (msg or "").lower()
    if any(k in m for k in INTIMACY_UNLOCK_KEYS):          # 硬钥匙短语(secret time 等)
        return True
    return any(w in (msg or "") for w in EXPLICIT_LEXICON)  # 露骨词


async def update_intimacy(session_id: str, user_message: str) -> bool:
    """亲密解锁：每轮算一次"当下是否亲密"并更新粘性。硬钥匙/露骨词→立刻亲密；暧昧暗号→门控 haiku 判句意。
    粘性：解锁后再粘 K 轮中性才收回。default-safe：不确定/出错/分类器关→按中性。返回当前是否解锁。"""
    st = _intimacy.get(session_id) or {"unlocked": False, "neutral_streak": 0}
    try:
        if _intimacy_lexicon_hit(user_message):
            intimate = True
        elif EXPLICIT_CLASSIFIER_ENABLED:
            intimate = await _llm_intimacy_verdict(user_message)
        else:
            intimate = False
    except Exception:
        intimate = False
    if intimate:
        st = {"unlocked": True, "neutral_streak": 0}
    else:
        streak = st.get("neutral_streak", 0) + 1
        st = {"unlocked": bool(st.get("unlocked")) and streak <= INTIMACY_STICKY_K, "neutral_streak": streak}
    _intimacy[session_id] = st
    return st["unlocked"]


def intimacy_unlocked(session_id: str) -> bool:
    """读当前轮解锁态(update_intimacy 已在 chat 路算过)。default-safe：默认 False=收。"""
    return bool((_intimacy.get(session_id) or {}).get("unlocked", False))


async def apply_explicit_gate(memories: list, user_message: str, force_intimate=None, force_run=False):
    """②/① 露骨语境闸：候选含高 arousal 记忆才判别当下亲密度，
    再 ② arousal 失配惩罚重排 + ① 非亲密语境硬挡极高 arousal。返回 (memories, debug)。
    force_intimate 仅供 debug 端点确定性演示两侧用；force_run 让 debug 端点在全局开关关时也能演示。"""
    if (not EXPLICIT_GATE_ENABLED and not force_run) or not memories:
        return memories, {"gate": "off"}
    has_sensitive = any(float(m.get("arousal") or 0) >= SENSITIVE_AROUSAL for m in memories)
    if not has_sensitive:
        return memories, {"gate": "skip", "reason": "no-sensitive-candidate"}
    if force_intimate is not None:
        intimate, how = bool(force_intimate), "forced"
    else:
        intimate, how = await classify_moment_intimacy(user_message)
    ctx_arousal = 0.85 if intimate else 0.2
    kept, dropped = [], []
    for m in memories:
        a = float(m.get("arousal") or 0)
        base = float(m.get("score") or 0)
        m["_adj_score"] = base - EXPLICIT_PENALTY_LAMBDA * max(0.0, a - ctx_arousal)  # ② 单边失配惩罚
        if (not intimate) and a >= EXPLICIT_HARD_AROUSAL:                              # ① 硬门
            dropped.append(m)
            continue
        kept.append(m)
    kept.sort(key=lambda x: -float(x.get("_adj_score") or 0))
    if dropped or not intimate:
        print(f"🔞 语境闸: intimate={intimate}({how}) 留{len(kept)}挡{len(dropped)} "
              f"(敏感≥{SENSITIVE_AROUSAL}/硬门≥{EXPLICIT_HARD_AROUSAL}/λ{EXPLICIT_PENALTY_LAMBDA})")
    return kept, {"gate": "on", "intimate": intimate, "how": how, "ctx_arousal": ctx_arousal,
                  "kept": len(kept), "dropped": len(dropped)}


async def build_memory_text(user_message: str, drift: bool = True) -> str:
    """搜索记忆并格式化为注入文本（分区缓存模式用）。drift=False 时只读（诊断/层视图用，不触发心情漂移）。
    回忆墙条目默认只注入结构化摘要(mw_meta.summary)；仅当某条明显最强命中时才附全文 body。"""
    if MAX_MEMORIES_INJECT <= 0:
        return ""
    try:
        # 先尝试递纸条扩展召回（长输入/RP/总结场景）；不触发或失败→走原 search_memories
        memories = []
        if _should_use_scratchpad(user_message, force=_request_force_scratchpad.get()):
            memories = await _expand_recall_with_scratchpad(user_message, MAX_MEMORIES_INJECT)
        if not memories:
            memories = await search_memories(user_message, limit=MAX_MEMORIES_INJECT)
        if not memories:
            return ""

        # ②/① 露骨语境闸：中性语境压制高 arousal/私密记忆（gated，仅有敏感候选才判别当下亲密度）
        memories, _gate_dbg = await apply_explicit_gate(memories, user_message)
        if not memories:
            return ""

        # is_explicit 框定（方案A）：命中露骨/私密记忆【不剔除】，照常注入让小克能感知，收尾加一句框定语（默认关，运行时开关）
        _explicit_hits = []
        if await get_explicit_redact_enabled() and not intimacy_unlocked(get_active_session_id()):
            _flags = await get_memories_explicit_flags([m["id"] for m in memories])
            _explicit_hits = [m for m in memories if _flags.get(m["id"]) and not m.get("mw_meta")]
            if _explicit_hits:
                print(f"🔞 is_explicit 框定(A)：保留 {len(_explicit_hits)} 条露骨原文 + 收尾框定语（不剔除）")

        # 情绪①-第二步：把本轮命中的旧记忆朝当前心情挪 ≤0.1（fire-and-forget，不阻塞回复；仅聊天注入路径触发）
        if drift and MOOD_DRIFT_ENABLED:
            try:
                asyncio.create_task(apply_mood_drift(
                    [m["id"] for m in memories],
                    step=MOOD_DRIFT_STEP, daily_cap=MOOD_DRIFT_DAILY_CAP,
                    recent_n=MOOD_RECENT_N, skip_memorywall=MOOD_DRIFT_SKIP_MEMORYWALL,
                    tz_hours=TIMEZONE_HOURS,
                ))
            except Exception as _e:
                print(f"⚠️ 心情漂移调度失败(分区): {_e}")

        scores = [float(m.get("score") or 0) for m in memories]
        top_score = scores[0] if scores else 0.0
        second_score = scores[1] if len(scores) > 1 else 0.0
        # 全文资格：排第1 + 绝对分够高 + 明显甩开第2名（阈值可调，据实测分布定）
        top_full_eligible = (
            top_score >= MW_FULLBODY_MIN_SCORE
            and (len(memories) == 1 or (top_score - second_score) >= MW_FULLBODY_MIN_MARGIN)
        )

        _kws = extract_search_keywords(user_message)
        memory_lines = []
        for idx, mem in enumerate(memories):
            date_str = ""
            if mem.get("created_at"):
                try:
                    utc_str = str(mem['created_at'])[:19]
                    utc_dt = datetime.strptime(utc_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                    local_dt = utc_dt + timedelta(hours=TIMEZONE_HOURS)
                    date_str = f"[{local_dt.strftime('%Y-%m-%d')}] "
                except:
                    date_str = f"[{str(mem['created_at'])[:10]}] "
            _w = mood_word(mem.get("valence"), mem.get("arousal"))
            _feel = f"（当时的感觉：{_w}）" if _w else ""
            mw = mem.get("mw_meta")
            if isinstance(mw, str):
                try:
                    mw = json.loads(mw)
                except Exception:
                    mw = None
            if mw:  # 回忆墙条目：默认摘要；仅明显最强的那条给全文(豁免长度上限)
                _t = (mw.get("title") or "").strip()
                if idx == 0 and top_full_eligible:
                    _txt = (f"【回忆 {_t}】" if _t else "") + (mw.get("body") or "").strip()
                else:
                    _body = (mw.get("summary") or "").strip() or (mw.get("body") or "").strip()
                    _txt = (f"【回忆摘要 {_t}】" if _t else "") + _mem_snippet(_body, _kws)
                memory_lines.append(f"- {date_str}{_txt}{_feel}")
            else:  # 普通事实记忆：通常短；过长的(梦/看图/迁移)取最相关片段省 token
                _c = (mem.get('content') or '').strip()
                if not _c:
                    continue  # 空 content 兜底：跳过（检索已排除，这里双保险，绝不让空行崩注入）
                memory_lines.append(f"- {date_str}{_mem_snippet(_c, _kws)}{_feel}")

        print(f"📚 注入 {len(memories)} 条记忆（全文资格={top_full_eligible}, top={top_score:.3f}/2nd={second_score:.3f}）" + (f" +收敛{len(_explicit_hits)}条露骨" if _explicit_hits else ""))
        header = "【从过往对话中检索到的相关记忆】（这是你记得的背景，自然融进回应，别整段复述、别像念稿）\n"
        parts = []
        if memory_lines:
            parts.append(header + "\n".join(memory_lines))
        if _explicit_hits:
            parts.append(EXPLICIT_REDACT_NOTE)
        if not parts:
            return ""
        return "\n\n".join(parts) + (("\n\n" + HEALTH_SAFETY_NOTE) if HEALTH_SAFETY_NOTE.strip() else "")
    except Exception as e:
        print(f"⚠️ 记忆检索失败: {e}")
        return ""


# ============================================================
# 后台记忆处理
# ============================================================

async def process_memories_background(session_id: str, user_msg: str, assistant_msg: str, model: str, context_messages: list = None, skip_conversation_log: bool = False, tool_messages: list = None, assistant_tool_calls: list = None, assistant_reasoning: str = None, images: list = None):
    """
    后台异步：存储对话 + 提取记忆（不阻塞主流程）
    
    记忆提取受 MEMORY_EXTRACT_INTERVAL 控制：
    - 0: 禁用自动提取
    - 1: 每轮提取（默认）
    - N: 每 N 轮提取一次
    对话记录始终保存，不受间隔影响（除非 skip_conversation_log=True）。
    
    context_messages: 客户端发来的原始对话上下文（不含system prompt），
                      用于让提取模型从完整上下文中提取记忆。
    skip_conversation_log: 跳过对话存储（标题生成等辅助请求时使用）
    tool_messages: 客户端发来的工具结果消息列表
    assistant_tool_calls: response中assistant的工具调用列表（如果有）
    assistant_reasoning: response中assistant的reasoning_content（deepseek thinking mode）
    """
    global _round_counter, _dream_last_date
    
    try:
        # Debug: 打印存储分支判断依据
        print(f"💾 process_memories_background: user_msg={bool(user_msg)}, tool_messages={len(tool_messages) if tool_messages else 0}, "
              f"assistant_tool_calls={len(assistant_tool_calls) if assistant_tool_calls else 0}, skip={skip_conversation_log}")
        if tool_messages:
            print(f"💾 tool详情: {[{'role': m.get('role'), 'tool_call_id': m.get('tool_call_id', '?')} for m in tool_messages]}")

        # 看图记忆(铁律):本轮带图 → 后台描述+存记忆(下轮可检索记得),独立于提取间隔。
        # 注意:不能用 not skip_conversation_log——正常轮走 off-by-one 同步时 skip_conversation_log=True,
        # 那样会把图片记忆也跳过(=图片不进上下文的根因)。只要本轮有图就存。
        if IMAGE_ENABLED and images:
            asyncio.create_task(_save_image_memory_bg(session_id, images))

        # 1. 存储对话记录（除非明确跳过）
        if skip_conversation_log:
            print(f"⏭️  跳过对话存储（辅助请求）")
        elif tool_messages:
            # 工具结果轮次：存tool消息 + assistant回复（user消息在之前的轮次已存过）
            for tm in tool_messages:
                meta_dict = {}
                if tm.get("tool_call_id"):
                    meta_dict["tool_call_id"] = tm["tool_call_id"]
                if tm.get("name"):
                    meta_dict["name"] = tm["name"]
                meta = json.dumps(meta_dict) if meta_dict else None
                await save_message(session_id, "tool", tm.get("content", ""), model, metadata=meta)
            
            if assistant_msg or assistant_tool_calls:
                ast_meta_dict = {}
                if assistant_tool_calls:
                    ast_meta_dict["tool_calls"] = assistant_tool_calls
                if assistant_reasoning:
                    ast_meta_dict["reasoning_content"] = assistant_reasoning
                ast_meta = json.dumps(ast_meta_dict) if ast_meta_dict else None
                await save_message(session_id, "assistant", assistant_msg or "", model, metadata=ast_meta)
                print(f"🔧 存储: {len(tool_messages)}条tool + 1条assistant" + (" (含tool_calls)" if assistant_tool_calls else "") + (" (含reasoning)" if assistant_reasoning else ""))
        else:
            # 普通对话或首次工具调用
            ast_meta_dict = {}
            if assistant_tool_calls:
                ast_meta_dict["tool_calls"] = assistant_tool_calls
            if assistant_reasoning:
                ast_meta_dict["reasoning_content"] = assistant_reasoning
            assistant_meta = json.dumps(ast_meta_dict) if ast_meta_dict else None
            
            if assistant_tool_calls:
                # 首次工具调用：assistant回复包含tool_calls，存user + assistant(tool_calls)
                await save_message(session_id, "user", user_msg, model)
                await save_message(session_id, "assistant", assistant_msg or "", model, metadata=assistant_meta)
                print(f"🔧 存储: user + assistant (含{len(assistant_tool_calls)}个tool_calls)" + (" (含reasoning)" if assistant_reasoning else ""))
            else:
                # 纯文字对话：re-roll检测 + 存user + assistant
                last_user = await get_last_user_content(session_id)
                if last_user and last_user.strip() == user_msg.strip():
                    updated = await update_last_assistant_message(session_id, assistant_msg, model)
                    if updated:
                        print(f"🔄 检测到re-roll，已覆盖最后一条assistant回复")
                    else:
                        await save_message(session_id, "user", user_msg, model)
                        await save_message(session_id, "assistant", assistant_msg, model, metadata=assistant_meta)
                else:
                    await save_message(session_id, "user", user_msg, model)
                    await save_message(session_id, "assistant", assistant_msg, model, metadata=assistant_meta)
        
        # 2. 检查是否需要提取记忆
        if not MEMORY_EXTRACT_ENABLED:
            print(f"⏭️  记忆提取已关闭（MEMORY_EXTRACT_ENABLED=false）")
            return
        
        if MEMORY_EXTRACT_INTERVAL == 0:
            print(f"⏭️  记忆自动提取已禁用，跳过")
            return
        
        _round_counter += 1

        # ② L2今日：每 N 轮刷新今日浓缩（独立于提取间隔，后台不阻塞回复）
        if L2_TODAY_ENABLED and L2_REFRESH_N > 0 and (_round_counter % L2_REFRESH_N == 0):
            try:
                asyncio.create_task(refresh_l2(session_id))
            except Exception as _le:
                print(f"⚠️ L2刷新调度失败: {_le}")

        # ③-2 做梦懒触发：本地日变了(跨天)的第一句话→后台补做未覆盖的过去日(含昨天)。
        # 请求触发、无需 cron/常驻；维护成本只在跨天那一次。
        if DREAM_ENABLED:
            _today_local = str((datetime.now(timezone.utc) + timedelta(hours=TIMEZONE_HOURS)).date())
            if _dream_last_date != _today_local:
                _dream_last_date = _today_local
                try:
                    await set_gateway_config("dream_last_date", _today_local)
                except Exception:
                    pass
                try:
                    # 做梦/回忆墙永远钉在主线(全局 PARTITION_SESSION_ID)，rp 等其它线绝不生成日记/梦
                    _dream_sid = PARTITION_SESSION_ID or session_id
                    asyncio.create_task(maybe_run_dreams(_dream_sid))
                    print(f"💤 做梦懒触发：新的一天 {_today_local}，后台补做过去日(线={_dream_sid})")
                except Exception as _de:
                    print(f"⚠️ 做梦调度失败: {_de}")

        if MEMORY_EXTRACT_INTERVAL > 1 and (_round_counter % MEMORY_EXTRACT_INTERVAL != 0):
            print(f"⏭️  轮次 {_round_counter}，跳过记忆提取（每 {MEMORY_EXTRACT_INTERVAL} 轮提取一次）")
            return
        
        if MEMORY_EXTRACT_INTERVAL > 1:
            print(f"📝 轮次 {_round_counter}，执行记忆提取")
        
        # 3. 获取已有记忆（带 id），传给提取模型做对比去重 + 冲突标注(replaces_id)
        existing = await get_recent_memories(limit=40)
        existing_contents = [{"id": r["id"], "content": r["content"]} for r in existing]
        
        # 4. 构建用于提取的消息列表
        #    截取最近 MEMORY_EXTRACT_INTERVAL 轮对话（每轮=user+assistant共2条）
        #    而非发送完整上下文，省token
        if context_messages:
            # 截取最近N轮（interval×2条），加上最新的assistant回复
            tail_count = MEMORY_EXTRACT_INTERVAL * 2
            recent_msgs = list(context_messages)[-tail_count:] if len(context_messages) > tail_count else list(context_messages)
            messages_for_extraction = recent_msgs + [
                {"role": "assistant", "content": assistant_msg}
            ]
            print(f"📝 截取最近 {MEMORY_EXTRACT_INTERVAL} 轮对话提取记忆（{len(messages_for_extraction)} 条消息）")
        else:
            messages_for_extraction = [
                {"role": "user", "content": user_msg},
                {"role": "assistant", "content": assistant_msg},
            ]
        
        messages_for_extraction = _msgs_text_only(messages_for_extraction)  # 多模态兜底:绝不把图 base64 喂提取/feel(否则不出碎片)

        new_memories = await extract_memories(messages_for_extraction, existing_memories=existing_contents)

        # ③-1 feel：同一段顺带写一句"留在你心里的感受"(单独存、不衰减；默认关到验收)
        if FEEL_ENABLED:
            try:
                _fl = await generate_feel(messages_for_extraction)
                if _fl.get("feel"):
                    await save_feel(session_id, _fl["feel"], _fl.get("is_explicit", False))
                    print(f"💗 feel: {_fl['feel'][:30]}{' [露]' if _fl.get('is_explicit') else ''}")
            except Exception as _fe:
                print(f"⚠️ feel 生成/存储失败: {_fe}")

        # 过滤垃圾记忆（不靠模型自觉，硬过滤）
        META_BLACKLIST = [
            "记忆库", "记忆系统", "检索", "没有被记录", "没有被提取",
            "记忆遗漏", "尚未被记录", "写入不完整", "检索功能",
            "系统没有返回", "关键词匹配", "语义匹配", "语义检索",
            "阈值", "数据库", "seed", "导入", "部署",
            "bug", "debug", "端口", "网关",
        ]
        
        filtered_memories = []
        for mem in new_memories:
            content = mem["content"]
            if any(kw in content for kw in META_BLACKLIST):
                print(f"🚫 过滤掉meta记忆: {content[:60]}...")
                continue
            filtered_memories.append(mem)
        
        saved = 0
        skipped_dup = 0
        persona_routed = 0
        superseded = 0
        for mem in filtered_memories:
            # A4 提取路由：行为/相处偏好不进记忆池，收集到 persona_suggestions 供主理人贴人设
            if mem.get("kind") == "persona":
                _pimp = int(mem.get("importance", 5) or 5)
                if not PERSONA_SUGGESTION_ENABLED or _pimp < PERSONA_SUGGESTION_MIN_IMPORTANCE:
                    print(f"⏭️ 人设建议跳过(开关={PERSONA_SUGGESTION_ENABLED}/门槛 imp={_pimp}<{PERSONA_SUGGESTION_MIN_IMPORTANCE}): {mem['content'][:40]}")
                    continue
                try:
                    await save_persona_suggestion(mem["content"], session_id)
                    persona_routed += 1
                    print(f"🎭 行为偏好→人设建议（不入记忆池）: {mem['content'][:50]}...")
                except Exception as pe:
                    print(f"⚠️ 人设建议保存失败: {pe}")
                continue

            # DB 层去重门：避免每轮把同一事实反复重写入库
            try:
                dup = await check_duplicate_memory(mem["content"])
            except Exception as de:
                print(f"⚠️ 去重检查异常，按非重复处理: {de}")
                dup = {"is_duplicate": False}
            if dup.get("is_duplicate"):
                skipped_dup += 1
                print(f"🔁 跳过重复记忆（{dup.get('reason')}, 命中#{dup.get('matched_id')}）: {mem['content'][:50]}...")
                continue
            await save_memory(
                content=mem["content"],
                importance=mem["importance"],
                source_session=session_id,
                valence=mem.get("valence", 0.0),
                arousal=mem.get("arousal", 0.2),
                is_explicit=mem.get("is_explicit", False),
            )
            saved += 1
            # ② L5根基：若是【改变关系结构的里程碑】，额外塞进 L5 待审队列（不进 L5 正文，等阮阮审）。L5_AUTO_ENABLED 关则不自动收
            if L5_AUTO_ENABLED and mem.get("is_milestone"):
                try:
                    await save_l5_candidate(mem["content"], mem.get("event_date"), session_id)
                    print(f"🏛️ 里程碑→L5待审: {mem['content'][:40]}...")
                except Exception as le:
                    print(f"⚠️ L5候选保存失败: {le}")
            # A2 冲突处理：新事实推翻旧事实 → 把被推翻的旧条目置 inactive（不再并存打架）
            rid = mem.get("replaces_id")
            if rid:
                try:
                    await set_memory_active(int(rid), False)
                    superseded += 1
                    print(f"♻️ 新事实推翻旧记忆 #{rid}，已置 inactive: {mem['content'][:40]}...")
                except Exception as ce:
                    print(f"⚠️ 置旧记忆 inactive 失败 (#{rid}): {ce}")

        if saved or skipped_dup or persona_routed or superseded:
            total = await get_all_memories_count()
            print(f"💾 已存 {saved} 条事实（去重跳过 {skipped_dup}，meta过滤 {len(new_memories) - len(filtered_memories)}，"
                  f"行为偏好分流 {persona_routed}，推翻旧事实 {superseded}），总计 {total} 条")
    except Exception as e:
        print(f"⚠️  后台记忆处理失败: {e}")


# ============================================================
# API 接口
# ============================================================

@app.get("/")
async def health_check():
    """健康检查"""
    memory_count = 0
    if MEMORY_ENABLED:
        try:
            memory_count = await get_all_memories_count()
        except:
            pass
    
    return {
        "status": "running",
        "gateway": "AI Memory Gateway v2.0",
        "system_prompt_loaded": len(SYSTEM_PROMPT) > 0,
        "system_prompt_length": len(SYSTEM_PROMPT),
        "memory_enabled": MEMORY_ENABLED,
        "memory_count": memory_count,
        "memory_extract_interval": MEMORY_EXTRACT_INTERVAL,
    }


@app.get("/v1/models")
async def list_models():
    """模型列表（让客户端不报错）"""
    return {
        "object": "list",
        "data": [
            {
                "id": DEFAULT_MODEL,
                "object": "model",
                "created": 1700000000,
                "owned_by": "ai-memory-gateway",
            }
        ],
    }


# （诊断脚手架已移除：_diag_mm / _diag_extract / last-multimodal / last-extract / _capture_multimodal）


def _oai_text_response(text: str, model: str, is_stream: bool):
    """构造一个 OpenAI 兼容的回复(给暗号拦截用，不经过上游大模型)。stream/非stream 都支持，KELIVO 都认。"""
    import time as _time
    _id = "chatcmpl-" + uuid.uuid4().hex[:24]
    _created = int(_time.time())
    if not is_stream:
        return JSONResponse({
            "id": _id, "object": "chat.completion", "created": _created, "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        })

    async def _gen():
        first = {"id": _id, "object": "chat.completion.chunk", "created": _created, "model": model,
                 "choices": [{"index": 0, "delta": {"role": "assistant", "content": text}, "finish_reason": None}]}
        yield f"data: {json.dumps(first, ensure_ascii=False)}\n\n"
        last = {"id": _id, "object": "chat.completion.chunk", "created": _created, "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
        yield f"data: {json.dumps(last, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"
    return StreamingResponse(_gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "Connection": "keep-alive"})


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """核心转发接口"""
    if not API_KEY:
        return JSONResponse(
            status_code=500,
            content={"error": "API_KEY 未设置，请在环境变量中配置"},
        )

    body = await request.json()
    messages = body.get("messages", [])
    _turn_images = _extract_image_uris(messages) if IMAGE_ENABLED else []  # 看图记忆:本轮原图(交后台)

    # ---------- 检测是否应跳过对话存储 ----------
    # 客户端通过header显式声明（如标题生成等辅助请求）
    skip_conversation_log = request.headers.get("X-Skip-Conversation-Log", "").lower() == "true"
    _request_skip_log.set(skip_conversation_log)   # 供 TG 近况小抄判断:辅助请求不消费

    # ---------- 每请求对话线 X-Session-Line（KELIVO 不同助手带不同头 → 走不同线）----------
    # 只允许简单字符当线名(防注入怪 session_id)；没传/为空就不设，get_active_session_id() 回落全局(老行为不变)
    _line = (request.headers.get("X-Session-Line", "") or "").strip()
    if _line:
        import re as _re
        if _re.fullmatch(r"[A-Za-z0-9_-]{1,32}", _line):
            _request_session_line.set(_line)
        else:
            print(f"⚠️ 忽略非法 X-Session-Line: {_line!r}")

    # ---------- 每请求回复风格 X-Reply-Style（TG 带 short → 微信风格短回复）----------
    _style = (request.headers.get("X-Reply-Style", "") or "").strip()
    if _style:
        _request_reply_style.set(_style)

    # ---------- 提取用户最新消息 ----------
    user_message = ""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, str):
                user_message = content
            elif isinstance(content, list):
                user_message = " ".join(
                    item.get("text", "") for item in content
                    if isinstance(item, dict) and item.get("type") == "text"
                )
            break

    # ---------- 暗号拦截：/归档 → 归档当前线，不调用大模型 ----------
    # 把当前线对话压成总结入记忆库 + 原文软归档(挪走不再占token) + 重置缓存。KELIVO 会带时间戳前缀，取末行判断。
    _um_last = (user_message or "").strip().splitlines()[-1].strip() if (user_message or "").strip() else ""
    if _um_last.startswith("/归档") or _um_last.lower() == "/archive":
        _arch_sid = get_active_session_id()
        try:
            _ar = await archive_line(_arch_sid)
        except Exception as _ae:
            _ar = {"error": str(_ae)}
        if _ar.get("status") == "ok":
            _txt = (f"（已归档「{_arch_sid}」线：这段的精华我已经写进记忆库、会一直记得，"
                    f"{_ar.get('moved', 0)}条原文已移出当前对话、不再占用上下文。下次在这条线聊就是干净开局啦~）")
        elif _ar.get("status") == "empty":
            _txt = f"（「{_arch_sid}」线现在没有可归档的对话哦~）"
        else:
            _txt = f"（归档没成功：{_ar.get('error')}）"
        return _oai_text_response(_txt, body.get("model", ""), bool(body.get("stream", False)) or FORCE_STREAM)

    # ---------- 暗号拦截：/同步 → 把子线(TG)近况压成中性小抄递给主线读,不删线/不写记忆库/不调大模型 ----------
    if _um_last.startswith("/同步") or _um_last.lower() == "/sync":
        _sync_sid = get_active_session_id()
        if PARTITION_SESSION_ID and _sync_sid == PARTITION_SESSION_ID:
            _txt = "（/同步 是给子线(如TG)把近况递给主线用的，主线自己不用同步哦~）"
        else:
            try:
                _rows = await get_conversation_messages(_sync_sid, limit=10000)
                _msgs = [{"role": r.get("role"), "content": (r.get("content") or "")}
                         for r in _rows if (r.get("content") or "").strip()]
                if not _msgs:
                    _txt = "（这条线还没聊啥，没东西同步~）"
                else:
                    _dig = await generate_summary(_msgs, session_id=_sync_sid, force_quality=False)
                    if _dig:
                        _disp = (datetime.now(timezone.utc) + timedelta(hours=TIMEZONE_HOURS)).strftime("%m-%d %H:%M")
                        await set_gateway_config("tg_digest", _dig)
                        await set_gateway_config("tg_digest_at", _disp)
                        await set_gateway_config("tg_digest_ts", str(datetime.now(timezone.utc).timestamp()))
                        _txt = "（好啦~ 我把咱俩在这儿聊的近况理好、递给主线那边的我了，你回网页找我，我立刻接得上💚）"
                    else:
                        _txt = "（同步没弄成，等下再试一次好吗~）"
            except Exception as _se:
                _txt = f"（同步出了点小问题：{_se}）"
        return _oai_text_response(_txt, body.get("model", ""), bool(body.get("stream", False)) or FORCE_STREAM)

    # ---------- 暗号拦截：/画 → 文生图（硅基流动），不调聊天大模型 ----------
    # 缓存纪律:图片二进制只进 memory_photos 表;逐字历史只落一行短占位文字(每轮重放也就几十字、内容恒定,
    # 缓存前缀稳定不重建)。带 gateway_key 的展示 URL 只出现在给客户端的即时回复里,绝不落库、绝不进上游上下文。
    if IMAGE_GEN_ENABLED and (_um_last.startswith("/画") or _um_last.lower().startswith("/draw")):
        _is_stream = bool(body.get("stream", False)) or FORCE_STREAM
        _lm = _um_last.lower()
        _with_mem = _um_last.startswith("/画忆") or _lm.startswith("/drawmem")  # 带记忆构图版
        _draw_prompt = _um_last
        for _pfx in ("/画忆", "/drawmem", "/画", "/draw"):                      # 长前缀先匹配,别把 /画忆 当 /画
            if _um_last.startswith(_pfx) or _lm.startswith(_pfx):
                _draw_prompt = _um_last[len(_pfx):].strip()
                break
        if not _draw_prompt:
            return _oai_text_response("（「/画 想要的画面」我就给你画~ 比如：/画 一只橘猫趴在窗台晒太阳。"
                                      "想让我照着咱们的回忆构图,就用「/画忆 主题」,比如：/画忆 我们第一次见面）",
                                      body.get("model", ""), _is_stream)
        _compose = ""
        if _with_mem:                       # 先让 V(带全套记忆)把主题扩写成具体画面,再交给画图模型
            _compose = await _expand_draw_prompt(_draw_prompt, _request_session_line.get())
        _mime, _data = await generate_image(_compose or _draw_prompt)
        if not _data:
            return _oai_text_response("（呜，这张没画出来…画画的服务好像开小差了，等一下再让我试一次好吗）",
                                      body.get("model", ""), _is_stream)
        _draw_sid = get_active_session_id()
        _store_prompt = f"{_draw_prompt}（记忆构图：{_compose[:120]}）" if _compose else _draw_prompt
        _mid, _pid = await _store_generated_image(_store_prompt, _mime, _data, _draw_sid)
        _qs = f"?gateway_key={GATEWAY_SECRET}" if GATEWAY_SECRET else ""
        if _pid:
            _lead = f"（我照着记忆构的图~ 🎨 {_compose[:80]}…）" if _compose else "（画好啦~ 🎨）"
            _show = f"{_lead}\n\n![{_draw_prompt[:50]}]({PUBLIC_BASE_URL}/api/photos/{_pid}{_qs})"
        else:
            _show = "（画是画好了，可是存相册的时候出了岔子没法给你看…这句话麻烦转告 FABLE 哥，让他去查日志）"
        if not skip_conversation_log and _draw_sid:
            try:
                await save_message(_draw_sid, "user", user_message, body.get("model", ""))
                await save_message(_draw_sid, "assistant",
                                   f"（我{'照着记忆' if _compose else ''}给你画了一张画：{_draw_prompt}，已经存进相册回忆里）",
                                   body.get("model", ""))
            except Exception as _de:
                print(f"⚠️ /画 落库失败: {_de}")
        return _oai_text_response(_show, body.get("model", ""), _is_stream)

    # ---------- 暗号拦截：/想想 → 强制走"递纸条"扩展召回（不拦截回复，剥离前缀后继续正常生成）----------
    _scratchpad_cmd_hit = (
        _um_last.startswith(SCRATCHPAD_TRIGGER_CMD)
        or _um_last.lower().startswith("/scratchpad")
    )
    if _scratchpad_cmd_hit:
        if _um_last.startswith(SCRATCHPAD_TRIGGER_CMD):
            _stripped_last = _um_last[len(SCRATCHPAD_TRIGGER_CMD):].strip()
        else:
            _stripped_last = _um_last[len("/scratchpad"):].strip()
        if not _stripped_last:
            _txt = (f"（{SCRATCHPAD_TRIGGER_CMD} 是让我多翻几遍记忆库再回复你的暗号——"
                    f"后面跟上你想问/想写的内容，我会更仔细地找资料再答~）")
            return _oai_text_response(_txt, body.get("model", ""), bool(body.get("stream", False)) or FORCE_STREAM)
        # 剥离前缀：更新本地 user_message + 同步改 messages 末条 user content（让 LLM 看不到暗号）
        _orig_lines = (user_message or "").splitlines()
        if _orig_lines:
            _orig_lines[-1] = _stripped_last
            user_message = "\n".join(_orig_lines).strip()
        else:
            user_message = _stripped_last
        for _m in reversed(messages):
            if _m.get("role") != "user":
                continue
            _c = _m.get("content")
            if isinstance(_c, str):
                _sl = _c.splitlines()
                if _sl and (_sl[-1].strip().startswith(SCRATCHPAD_TRIGGER_CMD)
                            or _sl[-1].strip().lower().startswith("/scratchpad")):
                    _sl[-1] = _stripped_last
                    _m["content"] = "\n".join(_sl)
            elif isinstance(_c, list):
                for _item in _c:
                    if isinstance(_item, dict) and _item.get("type") == "text":
                        _tl = (_item.get("text") or "").splitlines()
                        if _tl and (_tl[-1].strip().startswith(SCRATCHPAD_TRIGGER_CMD)
                                    or _tl[-1].strip().lower().startswith("/scratchpad")):
                            _tl[-1] = _stripped_last
                            _item["text"] = "\n".join(_tl)
                            break
            break
        _request_force_scratchpad.set(True)
        print(f"📝 暗号触发: {SCRATCHPAD_TRIGGER_CMD} → 强制纸条召回，剥离后={_stripped_last[:60]!r}")

    # ---------- 构建 system prompt ----------
    # 先保存原始对话消息（不含 system prompt），用于记忆提取
    original_messages = [msg for msg in messages if msg.get("role") != "system"]
    
    # ---------- 检测工具调用消息 ----------
    tool_messages = [m for m in messages if m.get("role") == "tool"]
    if tool_messages:
        print(f"🔧 检测到 {len(tool_messages)} 条工具结果消息")
    
    # ---------- 生成 session ID ----------
    session_id = str(uuid.uuid4())[:8]
    
    # ---------- 分区缓存模式 ----------
    if CACHE_PARTITION_ENABLED:
        active_sid = get_active_session_id()
        if active_sid:
            session_id = active_sid

        # 亲密解锁：每轮算一次当下亲密度 + 更新粘性(K轮)，供下面三处 redact 读 intimacy_unlocked()。
        # 仅收敛开时才需要；硬钥匙/露骨词即时、暧昧走门控 haiku；default-safe。
        if _EXPLICIT_REDACT and user_message:
            try:
                await update_intimacy(session_id, user_message)
            except Exception as _ie:
                print(f"⚠️ 亲密判别失败(按中性): {_ie}")

        # 从DB读取历史
        try:
            db_history = await get_conversation_messages(session_id, limit=10000)
            db_msgs = []
            for m in (db_history or []):
                msg = db_row_to_message(m)
                msg['created_at'] = m.get('created_at')  # 保留时间戳供分区时间窗口判断
                db_msgs.append(msg)
        except Exception as e:
            print(f"[warning] 分区模式读取历史失败: {e}")
            db_msgs = []

        # ④ 主动浮现：对话开头(距上条消息 > PROACTIVE_GAP_HOURS=当天首轮/长间隔后)→算一句"开场"，一次性注入本轮。默认关。
        _proactive.pop(session_id, None)
        if PROACTIVE_ENABLED and user_message:
            try:
                _ts_list = []
                for _m in (db_history or []):
                    _t = _m.get("created_at")
                    if _t is not None:
                        _ts_list.append(_t if getattr(_t, "tzinfo", None) else _t.replace(tzinfo=timezone.utc))
                _gap_ok = (not _ts_list) or ((datetime.now(timezone.utc) - max(_ts_list)).total_seconds() > PROACTIVE_GAP_HOURS * 3600)
                if _gap_ok and _ts_list:  # 有历史且确是长间隔/新开头(首条对话不浮)
                    _cands = await pick_proactive_candidates(session_id)
                    if _cands:
                        import random
                        _op = await generate_opening(random.choice(_cands)["line"])
                        if _op:
                            _proactive[session_id] = "〔开场·你心里还惦着，若自然可轻轻提起，别硬塞、别像念稿〕\n" + _op
                            print(f"💬 主动浮现(开头): {_op[:30]}")
            except Exception as _pe:
                print(f"⚠️ 主动浮现失败: {_pe}")

        # 提取客户端新消息（非system），可能是user、tool、或带tool_calls的assistant
        client_new_msgs = [m for m in messages if m.get("role") != "system"]
        # 分区模式下，assistant消息来自上一轮response（DB里已存），过滤掉避免重复
        client_new_msgs = [m for m in client_new_msgs if m.get("role") != "assistant"]
        # 分区模式下DB已有完整历史，客户端发来的旧user是冗余的，只保留最后一条
        user_msgs = [m for m in client_new_msgs if m.get("role") == "user"]
        if len(user_msgs) > 1:
            last_user = user_msgs[-1]
            client_new_msgs = [m for m in client_new_msgs if m.get("role") != "user"]
            client_new_msgs.append(last_user)
            print(f"🔧 去重: 过滤{len(user_msgs)-1}条冗余user，保留最后1条")
        # 工具结果轮次处理：基于DB状态 + 当前轮次tool_call_id精确判断
        client_tools = [m for m in client_new_msgs if m.get("role") == "tool"]
        if client_tools:
            # 判断DB是否处于"等待tool结果"状态（最后一条是assistant(tool_calls)）
            db_last = db_msgs[-1] if db_msgs else None
            db_expecting_tool = (db_last and db_last.get("role") == "assistant" and db_last.get("tool_calls"))
            
            if not db_expecting_tool:
                # DB不在等待tool结果 → 客户端的所有tool都是历史残留（含手动删除后的幽灵）
                stale_ids = [m.get('tool_call_id', '?') for m in client_tools]
                print(f"🔧 去重: DB未在等待tool结果，丢弃{len(client_tools)}条客户端tool (ids: {stale_ids})")
                client_new_msgs = [m for m in client_new_msgs if m.get("role") != "tool"]
            else:
                # DB在等待tool → 只保留匹配当前轮次assistant(tool_calls)的tool
                expected_tool_ids = {tc.get("id") for tc in db_last.get("tool_calls", []) if tc.get("id")}
                new_tools = [m for m in client_tools if m.get("tool_call_id") in expected_tool_ids]
                stale_tools = [m for m in client_tools if m.get("tool_call_id") not in expected_tool_ids]
                
                if stale_tools:
                    print(f"🔧 去重: 丢弃{len(stale_tools)}条非当前轮次tool (ids: {[m.get('tool_call_id','?') for m in stale_tools]})")
                if new_tools:
                    print(f"🔧 保留{len(new_tools)}条当前轮次tool (ids: {[m.get('tool_call_id','?') for m in new_tools]})")
                
                # 重建 client_new_msgs
                last_msg = client_new_msgs[-1] if client_new_msgs else None
                client_new_msgs = new_tools[:]
                if last_msg and last_msg.get("role") == "user":
                    client_new_msgs.append(last_msg)
                
                if new_tools:
                    # Race condition 防护：DB的assistant(tool_calls)已确认存在（db_expecting_tool=True），
                    # 但仍需检查是否被其他并发请求意外清除
                    new_tool_ids = {m.get("tool_call_id") for m in new_tools if m.get("tool_call_id")}
                    db_has_matching_ast = False
                    for m in db_msgs:
                        if m.get("role") == "assistant" and m.get("tool_calls"):
                            ast_tc_ids = {tc.get("id") for tc in m["tool_calls"] if tc.get("id")}
                            if new_tool_ids & ast_tc_ids:
                                db_has_matching_ast = True
                                break
                    if not db_has_matching_ast and new_tool_ids:
                        for m in messages:
                            if m.get("role") == "assistant" and m.get("tool_calls"):
                                ast_tc_ids = {tc.get("id") for tc in m["tool_calls"] if tc.get("id")}
                                if new_tool_ids & ast_tc_ids:
                                    client_new_msgs.insert(0, m)
                                    print(f"⚠️ Race防护: 从客户端补充assistant(tool_calls)")
                                    break
        all_msgs = db_msgs + client_new_msgs
        
        # 同步更新tool_messages，避免process_memories_background存重复的旧tool
        tool_messages = [m for m in client_new_msgs if m.get("role") == "tool"]
        
        print(f"📦 分区模式: DB历史{len(db_msgs)}条 + 客户端消息{len(client_new_msgs)}条")
        
        _up_block = _compose_user_profile_block(await get_user_profile())
        messages = await build_partitioned_messages(
            session_id, all_msgs, (await get_system_prompt()) + _up_block + _compose_l5_block(await get_l5_foundation()) + "\n\n" + MEMORY_GUIDANCE, user_message
        )
        body["messages"] = messages
    
    else:
        # ---------- 原有逻辑：system prompt + 记忆注入 ----------
        _up_block = _compose_user_profile_block(await get_user_profile())
        if SYSTEM_PROMPT or _up_block or (MEMORY_ENABLED and MEMORY_EXTRACT_ENABLED and user_message):
            if MEMORY_ENABLED and MEMORY_EXTRACT_ENABLED and user_message:
                enhanced_prompt = await build_system_prompt_with_memories(user_message)
            else:
                enhanced_prompt = await get_system_prompt()
            enhanced_prompt = (enhanced_prompt or "") + _up_block + _compose_l5_block(await get_l5_foundation())
            if enhanced_prompt:
                has_system = any(msg.get("role") == "system" for msg in messages)
                if has_system:
                    for i, msg in enumerate(messages):
                        if msg.get("role") == "system":
                            messages[i]["content"] = enhanced_prompt + "\n\n" + msg["content"]
                            break
                else:
                    messages.insert(0, {"role": "system", "content": enhanced_prompt})
        
        body["messages"] = messages
    
    # ---------- 模型处理 ----------
    model = body.get("model", DEFAULT_MODEL)
    if not model:
        model = DEFAULT_MODEL
    body["model"] = model

    # ① off-by-one 修复：当前轮对话「同步」落库（生成前写 user / 返回前写 assistant），
    #    取代原 process_memories_background 里的后台写——杜绝「下一轮读历史早于上一轮写入」。
    #    去重：user 写在 history 读取(get_conversation_messages)之后 → 本轮拼装的 db_msgs 不含它，
    #    当前轮仍只用客户端那条（一份）；写进 DB 的这条是给「下一轮」读历史用的。
    #    仅普通文字轮（非辅助请求/非工具结果轮）；re-roll（与 DB 最后一条 user 相同）不重复写 user。
    _sync_conv = bool(MEMORY_ENABLED and user_message and not skip_conversation_log and not tool_messages)
    _is_reroll = False
    if _sync_conv:
        try:
            _last_user = await get_last_user_content(session_id)
            _is_reroll = bool(_last_user and _last_user.strip() == user_message.strip())
            if not _is_reroll:
                await save_message(session_id, "user", user_message, model)
            print(f"📝 同步写 user（{'re-roll跳过' if _is_reroll else '已落库'}）", flush=True)
        except Exception as _se:
            print(f"⚠️ 同步写 user 失败，回退后台写: {_se}", flush=True)
            _sync_conv = False

    # ---------- cache_control 兼容性处理 ----------
    if CACHE_PARTITION_ENABLED and not _is_anthropic_model(model):
        _strip_cache_control(body.get("messages", []))
    
    # ---------- 转发请求 ----------
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }
    # OpenRouter 需要的额外头
    if "openrouter" in API_BASE_URL:
        headers["HTTP-Referer"] = EXTRA_REFERER
        headers["X-Title"] = EXTRA_TITLE
    
    is_stream = body.get("stream", False)
    
    # 强制流式传输（解决部分客户端不发stream=true的问题）
    if FORCE_STREAM and not is_stream:
        is_stream = True
        body["stream"] = True
        print(f"⚡ 强制开启流式传输（FORCE_STREAM=true）")
    
    # 注入推理参数（解决客户端走网关时不带reasoning参数的问题）
    if REASONING_EFFORT:
        # 统一用 reasoning_effort（Claude/OpenAI/Google Gemini OpenAI兼容端点都支持）
        # 先删除客户端可能已带的值，确保用我们配置的
        body.pop("reasoning_effort", None)
        body.pop("google", None)
        body["reasoning_effort"] = REASONING_EFFORT
        print(f"🧠 注入推理参数: reasoning_effort={REASONING_EFFORT}")
    
    print(f"📡 请求: model={model}, stream={is_stream}, memory={'on' if MEMORY_ENABLED else 'off'}", flush=True)
    
    # 调试：打印请求体中的推理相关字段
    debug_keys = {k: v for k, v in body.items() if k in ('reasoning_effort', 'google', 'reasoning')}
    if debug_keys:
        print(f"📡 推理字段: {debug_keys}", flush=True)
    
    if is_stream:
        return StreamingResponse(
            stream_and_capture(headers, body, session_id, user_message, model, original_messages, skip_conversation_log, tool_messages, _sync_conv, _is_reroll, _turn_images),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )
    else:
        async with httpx.AsyncClient(timeout=300) as client:
            response = await client.post(API_BASE_URL, headers=headers, json=body)
            
            if response.status_code == 200:
                resp_data = response.json()
                assistant_msg = ""
                assistant_tool_calls = None
                assistant_reasoning = None
                try:
                    msg_obj = resp_data["choices"][0]["message"]
                    assistant_msg = msg_obj.get("content") or ""
                    if msg_obj.get("tool_calls"):
                        assistant_tool_calls = msg_obj["tool_calls"]
                        print(f"🔧 Response 包含 {len(assistant_tool_calls)} 个工具调用")
                    if msg_obj.get("reasoning_content"):
                        assistant_reasoning = msg_obj["reasoning_content"]
                        print(f"🧠 Response 包含 reasoning_content ({len(assistant_reasoning)}字符)")
                except (KeyError, IndexError):
                    pass
                
                # ① 同步写 assistant（返回前）；已同步则后台只提取、不再写对话
                if _sync_conv and (assistant_msg or assistant_tool_calls):
                    try:
                        _meta_d = {}
                        if assistant_tool_calls:
                            _meta_d["tool_calls"] = assistant_tool_calls
                        if assistant_reasoning:
                            _meta_d["reasoning_content"] = assistant_reasoning
                        _meta_j = json.dumps(_meta_d) if _meta_d else None
                        if _is_reroll:
                            await update_last_assistant_message(session_id, assistant_msg or "", model)
                        else:
                            await save_message(session_id, "assistant", assistant_msg or "", model, metadata=_meta_j)
                        print(f"📝 同步写 assistant（{'re-roll覆盖' if _is_reroll else '已落库'}）", flush=True)
                    except Exception as _ae:
                        print(f"⚠️ 同步写 assistant 失败: {_ae}", flush=True)

                if MEMORY_ENABLED and (user_message or tool_messages):
                    asyncio.create_task(
                        process_memories_background(session_id, user_message, assistant_msg, model,
                                                    context_messages=original_messages, skip_conversation_log=(skip_conversation_log or _sync_conv),
                                                    tool_messages=tool_messages, assistant_tool_calls=assistant_tool_calls,
                                                    assistant_reasoning=assistant_reasoning, images=_turn_images)
                    )

                return JSONResponse(status_code=200, content=resp_data)
            else:
                return JSONResponse(status_code=response.status_code, content=response.json())


async def stream_and_capture(headers: dict, body: dict, session_id: str, user_message: str, model: str, original_messages: list = None, skip_conversation_log: bool = False, tool_messages: list = None, sync_conv: bool = False, is_reroll: bool = False, turn_images: list = None):
    """流式响应 + 捕获完整回复（原始字节透传，确保SSE格式和thinking数据完整）"""
    full_response = []
    full_reasoning = []
    stream_usage = {}
    line_buffer = ""
    accumulated_tool_calls = {}  # index -> {id, type, function: {name, arguments}}
    
    async with httpx.AsyncClient(timeout=300) as client:
        async with client.stream("POST", API_BASE_URL, headers=headers, json=body) as response:
            # 打印上游响应头（排查thinking问题用）
            upstream_ct = response.headers.get("content-type", "")
            print(f"📨 上游响应: status={response.status_code}, content-type={upstream_ct}", flush=True)
            
            # 上游非200时，提前打印messages结构方便debug
            if response.status_code != 200:
                msg_summary = [{"role": m.get("role"), "tool_calls": bool(m.get("tool_calls")), "tool_call_id": m.get("tool_call_id", ""), "content_type": type(m.get("content")).__name__} for m in body.get("messages", [])]
                print(f"❌ 发送的messages结构({len(msg_summary)}条): {msg_summary}", flush=True)
            
            error_body_parts = []
            is_error = response.status_code != 200
            
            async for chunk in response.aiter_bytes():
                # 原始字节直接透传给客户端
                yield chunk
                
                if is_error:
                    error_body_parts.append(chunk)
                    continue
                
                # 旁路解析：从字节流中提取assistant回复内容，用于后续记忆提取
                text = chunk.decode("utf-8", errors="ignore")
                line_buffer += text
                while "\n" in line_buffer:
                    line, line_buffer = line_buffer.split("\n", 1)
                    line = line.strip()
                    if line.startswith("data: ") and line != "data: [DONE]":
                        try:
                            data = json.loads(line[6:])
                            
                            if "usage" in data:
                                stream_usage = data["usage"]
                            
                            delta = data.get("choices", [{}])[0].get("delta", {})
                            content = delta.get("content", "")
                            if content:
                                full_response.append(content)
                            
                            # 收集reasoning_content（deepseek thinking mode）
                            reasoning = delta.get("reasoning_content", "")
                            if reasoning:
                                full_reasoning.append(reasoning)
                            
                            # 累积tool_calls
                            if "tool_calls" in delta:
                                for tc in delta["tool_calls"]:
                                    idx = tc.get("index", 0)
                                    if idx not in accumulated_tool_calls:
                                        accumulated_tool_calls[idx] = {
                                            "index": idx,
                                            "id": tc.get("id", ""),
                                            "type": tc.get("type", "function"),
                                            "function": {"name": "", "arguments": ""}
                                        }
                                    if tc.get("id"):
                                        accumulated_tool_calls[idx]["id"] = tc["id"]
                                    if "function" in tc:
                                        fn = tc["function"]
                                        if fn.get("name"):
                                            accumulated_tool_calls[idx]["function"]["name"] = fn["name"]
                                        if "arguments" in fn:
                                            accumulated_tool_calls[idx]["function"]["arguments"] += fn["arguments"]
                        except (json.JSONDecodeError, KeyError, IndexError):
                            pass
    
    assistant_msg = "".join(full_response)
    assistant_reasoning = "".join(full_reasoning) if full_reasoning else None
    assistant_tool_calls = list(accumulated_tool_calls.values()) if accumulated_tool_calls else None
    
    if assistant_reasoning:
        print(f"🧠 Stream response 包含 reasoning_content ({len(assistant_reasoning)}字符)")
    
    # 打印上游错误内容
    if error_body_parts:
        error_text = b"".join(error_body_parts).decode("utf-8", errors="ignore")[:500]
        print(f"❌ 上游错误内容: {error_text}", flush=True)
    
    if assistant_tool_calls:
        print(f"🔧 Stream response 包含 {len(assistant_tool_calls)} 个工具调用")
    
    if stream_usage:
        pt = stream_usage.get("prompt_tokens", 0)
        ct = stream_usage.get("completion_tokens", 0)
        tt = stream_usage.get("total_tokens", 0)
        if tt > 0:
            asyncio.create_task(save_token_usage(session_id, model, pt, ct, tt))
            print(f"📊 Stream Token: {pt} + {ct} = {tt}")
    
    # ① 同步写 assistant（流式结束后、调度后台提取前）；已同步则后台只提取、不再写对话
    if sync_conv and (assistant_msg or assistant_tool_calls):
        try:
            _meta_d = {}
            if assistant_tool_calls:
                _meta_d["tool_calls"] = assistant_tool_calls
            if assistant_reasoning:
                _meta_d["reasoning_content"] = assistant_reasoning
            _meta_j = json.dumps(_meta_d) if _meta_d else None
            if is_reroll:
                await update_last_assistant_message(session_id, assistant_msg or "", model)
            else:
                await save_message(session_id, "assistant", assistant_msg or "", model, metadata=_meta_j)
            print(f"📝 同步写 assistant 流式（{'re-roll覆盖' if is_reroll else '已落库'}）", flush=True)
        except Exception as _ae:
            print(f"⚠️ 同步写 assistant 流式失败: {_ae}", flush=True)

    if MEMORY_ENABLED and (user_message or tool_messages):
        asyncio.create_task(
            process_memories_background(session_id, user_message, assistant_msg, model,
                                        context_messages=original_messages, skip_conversation_log=(skip_conversation_log or sync_conv),
                                        tool_messages=tool_messages, assistant_tool_calls=assistant_tool_calls,
                                        assistant_reasoning=assistant_reasoning, images=turn_images)
        )


# ============================================================
# 记忆管理接口
# ============================================================


@app.get("/import/seed-memories")
async def import_seed_memories():
    """一次性导入预置记忆（从 seed_memories.py）"""
    try:
        from seed_memories import run_seed_import
        result = await run_seed_import()
        return result
    except ImportError:
        return {"error": "未找到 seed_memories.py，请参考 seed_memories_example.py 创建"}
    except Exception as e:
        return {"error": str(e)}


@app.get("/export/memories")
async def export_memories():
    """
    导出所有记忆为 JSON（用于备份或迁移）
    浏览器访问这个地址就会返回所有记忆数据
    """
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用（设置 MEMORY_ENABLED=true 开启）"}
    
    try:
        memories = await get_all_memories()
        # 把 datetime 转成字符串
        for mem in memories:
            if mem.get("created_at"):
                mem["created_at"] = str(mem["created_at"])
        
        return {
            "total": len(memories),
            "exported_at": str(__import__("datetime").datetime.now()),
            "memories": memories,
        }
    except Exception as e:
        return {"error": str(e)}


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(request: Request):
    """Dashboard - 整合的记忆管理界面"""
    if not MEMORY_ENABLED:
        return HTMLResponse("<h3>记忆系统未启用（设置 MEMORY_ENABLED=true 开启）</h3>")
    
    return templates.TemplateResponse(request, "dashboard.html")


@app.get("/console", response_class=HTMLResponse)
async def console_page():
    """记忆控制台:阮阮的金色塔罗操作间。自包含整页,FileResponse 直读(避开 Jinja 处理大段 JS)。
    knob 状态走 /api/console,写回走各 toggle/PUT settings/decay 端点。"""
    if not MEMORY_ENABLED:
        return HTMLResponse("<h3>记忆系统未启用（设置 MEMORY_ENABLED=true 开启）</h3>")
    from fastapi.responses import FileResponse
    return FileResponse("templates/console.html", media_type="text/html")



# ============================================================
# 管理 API
# ============================================================

@app.get("/api/memories")
async def api_get_memories(layer: int = None, active_only: bool = None):
    """获取所有记忆（管理页面用）
    
    Query params:
        layer: 筛选层级（1=碎片, 2=事件, 3=核心）
        active_only: 是否只返回活跃记忆
    """
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    memories = await get_all_memories_detail(layer=layer, active_only=active_only)
    tz_offset = timezone(timedelta(hours=TIMEZONE_HOURS))
    for m in memories:
        if m.get("created_at"):
            dt = m["created_at"]
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            m["created_at"] = dt.astimezone(tz_offset).strftime("%Y-%m-%d %H:%M:%S")
    # 获取层级统计
    try:
        layer_stats = await get_layer_statistics()
    except Exception:
        layer_stats = None
    
    result = {"memories": memories}
    if layer_stats:
        result["layer_stats"] = layer_stats
    return result


@app.get("/api/memories/search")
async def api_search_memories(q: str = "", limit: int = 20):
    """语义搜索记忆（Dashboard用，走后端 search_memories）"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    if not q.strip():
        return {"error": "搜索关键词不能为空", "results": []}
    try:
        results = await search_memories(q.strip(), limit)
        tz_offset = timezone(timedelta(hours=TIMEZONE_HOURS))
        out = []
        for r in results:
            item = dict(r)
            if item.get("created_at"):
                dt = item["created_at"]
                if hasattr(dt, 'tzinfo'):
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    item["created_at"] = dt.astimezone(tz_offset).strftime("%Y-%m-%d %H:%M:%S")
            out.append(item)
        return {"results": out, "total": len(out)}
    except Exception as e:
        return {"error": str(e), "results": []}


@app.post("/api/memories/create")
async def api_create_memory(request: Request):
    """外部代理（cyberboss 等）直接写入一条记忆（layer1 碎片，自动算向量，走夜间整理）"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    data = await request.json()
    content = str(data.get("content") or "").strip()
    if not content:
        return {"error": "content 不能为空"}
    if len(content) > 2000:
        return {"error": "content 过长（≤2000字）"}
    try:
        importance = max(1, min(10, int(data.get("importance") or 5)))
    except (TypeError, ValueError):
        importance = 5
    source_session = str(data.get("source_session") or "cyberboss").strip()[:64] or "cyberboss"
    await save_memory(content, importance=importance, source_session=source_session)
    return {"status": "ok"}


@app.post("/api/line/log")
async def api_line_log(request: Request):
    """外部代理(cyberboss)把对话逐字实时抄送进指定线（只入库，不调模型；供主线零时差借阅+回忆墙跨线合读）"""
    data = await request.json()
    line = str(data.get("line") or CYBERBOSS_LINE_ID).strip()[:32] or CYBERBOSS_LINE_ID
    role = str(data.get("role") or "").strip()
    content = str(data.get("content") or "").strip()
    if role not in ("user", "assistant"):
        return {"error": "role 必须是 user 或 assistant"}
    if not content:
        return {"error": "content 不能为空"}
    if len(content) > 8000:
        content = content[:8000]
    await save_message(line, role, content, model=str(data.get("model") or "cyberboss"))
    # cyberboss 线自动记忆提取：assistant 落地=一轮结束。凑齐紧邻的 user+assistant 对，
    # 就走与主线完全同款的后台提取管线（skip_conversation_log=True 防止双份入库；
    # 垃圾过滤/查重/feel/做梦懒触发等副作用与主线一致）。
    if role == "assistant" and line == CYBERBOSS_LINE_ID and MEMORY_ENABLED:
        try:
            rows = await get_conversation_messages(line, limit=10000)
            rows = rows[-10:]
            last_user = ""
            for r in reversed(rows[:-1]):
                if r.get("role") == "user":
                    last_user = (r.get("content") or "").strip()
                    break
            if last_user:
                ctx = [{"role": r.get("role"), "content": r.get("content") or ""} for r in rows[:-1]]
                asyncio.create_task(process_memories_background(
                    line, last_user, content, "cyberboss",
                    context_messages=ctx, skip_conversation_log=True,
                ))
        except Exception as _e:
            print(f"⚠️ cyberboss线提取调度失败: {_e}")
    return {"status": "ok"}


@app.get("/api/line/recent")
async def api_line_recent(line: str = "", rounds: int = 9):
    """外部代理(cyberboss)读某条线的近况：滚动摘要 + 最近N轮逐字（line 缺省=主线）"""
    sid = (line or "").strip() or PARTITION_SESSION_ID
    rounds = max(1, min(20, int(rounds or 9)))
    try:
        st = await get_session_cache_state(sid)
        summary_parts = st.get("summary_parts") or []
        early = (st.get("early_summary") or "").strip()
        rows = await get_conversation_messages(sid, limit=10000)
        rnds = group_by_rounds([{"role": r.get("role"), "content": (r.get("content") or "")} for r in rows])
        msgs = []
        for rnd in rnds[-rounds:]:
            for m in rnd:
                c = (m.get("content") or "").strip()
                if c:
                    msgs.append({"role": m.get("role"), "content": c})
        summary_segments = ([f"〔更早〕{early}"] if early else []) + list(summary_parts)
        latest_at = ""
        if rows:
            _ts = rows[-1].get("created_at")
            if _ts is not None:
                try:
                    if getattr(_ts, "tzinfo", None) is None:
                        _ts = _ts.replace(tzinfo=timezone.utc)
                    latest_at = _ts.astimezone(timezone.utc).isoformat()
                except Exception:
                    pass
        return {"line": sid, "summary": "\n".join(summary_segments).strip(), "recent": msgs, "latest_at": latest_at}
    except Exception as e:
        return {"error": str(e), "line": sid}


@app.put("/api/memories/{memory_id}")
async def api_update_memory(memory_id: int, request: Request):
    """更新单条记忆（支持 content / importance / title / layer / valence / arousal）"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    data = await request.json()
    await update_memory_with_layer(
        memory_id,
        content=data.get("content"),
        importance=data.get("importance"),
        title=data.get("title"),
        layer=data.get("layer"),
    )
    # 面板手动改情绪：仅当显式带 valence/arousal 时覆盖写两列 + 重置漂移基线（其余字段走上面常规更新）
    if data.get("valence") is not None and data.get("arousal") is not None:
        await update_memory_emotion(memory_id, data["valence"], data["arousal"])
    if data.get("content") is not None:      # 内容改了→向量跟着重算,语义检索才认新内容
        await refresh_memory_embedding(memory_id)
    return {"status": "ok", "id": memory_id}


@app.delete("/api/memories/{memory_id}")
async def api_delete_memory(memory_id: int, soft: bool = False):
    """删除单条记忆
    
    Query params:
        soft: true=归档（is_active=false），false=永久删除
    """
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    if soft:
        await update_memory_with_layer(memory_id, is_active=False)
    else:
        await delete_memory(memory_id)
    return {"status": "ok", "id": memory_id}


@app.post("/api/memories/batch-update")
async def api_batch_update(request: Request):
    """批量更新记忆"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    data = await request.json()
    updates = data.get("updates", [])
    if not updates:
        return {"error": "没有要更新的记忆"}
    for item in updates:
        await update_memory_with_layer(
            item["id"],
            content=item.get("content"),
            importance=item.get("importance"),
            title=item.get("title"),
            layer=item.get("layer"),
        )
        if item.get("content") is not None:  # 内容改了→向量跟着重算,语义检索才认新内容
            await refresh_memory_embedding(item["id"])
    return {"status": "ok", "updated": len(updates)}


@app.post("/api/memories/batch-delete")
async def api_batch_delete(request: Request):
    """批量删除记忆"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    data = await request.json()
    ids = data.get("ids", [])
    if not ids:
        return {"error": "未选择记忆"}
    await delete_memories_batch(ids)
    return {"status": "ok", "deleted": len(ids)}


# ============================================================
# 情绪① 回填：给现存默认情绪(0/0.2)的记忆补真实 valence/arousal
# 批量喂 haiku（同 live 提取的 Russell 规则）；只写两列、仅默认行、幂等可重跑
# dry_run=true 只算不写（小样自检）；不带则后台批处理，GET .../status 查进度
# ============================================================
_emotion_backfill_status = {"running": False, "total": 0, "done": 0, "written": 0,
                            "error": None, "samples": [], "finished_at": None}


@app.post("/api/memories/backfill-emotion")
async def api_backfill_emotion(request: Request):
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    try:
        body = await request.json()
    except Exception:
        body = {}
    dry_run = bool(body.get("dry_run", False))
    ids = body.get("ids") or None
    limit = body.get("limit") or None
    batch_size = int(body.get("batch_size", 25))
    include_mw = bool(body.get("include_memorywall", True))

    targets = await get_emotion_backfill_targets(include_memorywall=include_mw, ids=ids, limit=limit)

    if dry_run:
        samples = []
        for i in range(0, len(targets), batch_size):
            batch = targets[i:i + batch_size]
            tagged = await tag_emotions_batch([{"id": t["id"], "content": t["content"]} for t in batch])
            for t in batch:
                e = tagged.get(t["id"])
                samples.append({"id": t["id"], "is_mw": t["is_mw"],
                                "valence": (e["valence"] if e else None),
                                "arousal": (e["arousal"] if e else None),
                                "content": str(t["content"])[:70]})
        return {"dry_run": True, "count": len(targets), "samples": samples}

    if _emotion_backfill_status["running"]:
        return {"error": "情绪回填任务正在运行中"}
    _emotion_backfill_status.update({"running": True, "total": len(targets), "done": 0,
                                     "written": 0, "error": None, "samples": [], "finished_at": None})

    async def _run():
        try:
            for i in range(0, len(targets), batch_size):
                batch = targets[i:i + batch_size]
                tagged = await tag_emotions_batch([{"id": t["id"], "content": t["content"]} for t in batch])
                for t in batch:
                    _emotion_backfill_status["done"] += 1
                    e = tagged.get(t["id"])
                    if not e:
                        continue
                    ok = await update_emotion_only(t["id"], e["valence"], e["arousal"])
                    if ok:
                        _emotion_backfill_status["written"] += 1
                        if len(_emotion_backfill_status["samples"]) < 15:
                            _emotion_backfill_status["samples"].append(
                                {"id": t["id"], "is_mw": t["is_mw"],
                                 "valence": e["valence"], "arousal": e["arousal"],
                                 "content": str(t["content"])[:50]})
                await asyncio.sleep(0.3)
            _emotion_backfill_status["finished_at"] = datetime.now(timezone.utc).isoformat()
            print(f"✅ 情绪回填完成 written={_emotion_backfill_status['written']}/{_emotion_backfill_status['total']}")
        except Exception as e:
            _emotion_backfill_status["error"] = str(e)
            print(f"❌ 情绪回填异常: {e}")
        finally:
            _emotion_backfill_status["running"] = False

    asyncio.create_task(_run())
    return {"status": "started", "total": len(targets)}


@app.get("/api/memories/backfill-emotion/status")
async def api_backfill_emotion_status():
    return dict(_emotion_backfill_status)


@app.post("/api/explicit-redact/toggle")
async def api_explicit_redact_toggle(request: Request):
    """运行时切换 is_explicit 收敛开关（持久化到 gateway_config，重启自恢复）。body: {enabled: true/false}"""
    global _EXPLICIT_REDACT
    try:
        body = await request.json()
    except Exception:
        body = {}
    val = bool(body.get("enabled"))
    _EXPLICIT_REDACT = val
    await set_gateway_config("explicit_redact_enabled", "true" if val else "false")
    print(f"🔞 is_explicit 收敛开关 → {val}")
    return {"status": "ok", "explicit_redact_enabled": val}


@app.get("/api/explicit-redact")
async def api_explicit_redact_status():
    return {"explicit_redact_enabled": _EXPLICIT_REDACT}


@app.post("/api/memories/backfill-explicit")
async def api_backfill_explicit(request: Request):
    """露骨标记回填：按 keywords(ILIKE 任一) 或 ids 收候选 → haiku 判 is_explicit → 写 TRUE 的那些。
    body: {keywords:[...], ids:[...], dry_run:bool, batch_size:int, limit:int}"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    try:
        body = await request.json()
    except Exception:
        body = {}
    keywords = body.get("keywords") or []
    ids = body.get("ids") or None
    dry_run = bool(body.get("dry_run", False))
    batch_size = int(body.get("batch_size", 25))
    limit = int(body.get("limit", 200))

    candidates = await get_explicit_backfill_candidates(keywords, ids=ids, limit=limit)
    if not candidates:
        return {"candidates": 0, "tagged_explicit": 0, "samples": [], "note": "无候选"}

    tagged_true, samples = 0, []
    for i in range(0, len(candidates), batch_size):
        batch = candidates[i:i + batch_size]
        verdict = await tag_explicit_batch([{"id": t["id"], "content": t["content"]} for t in batch])
        for t in batch:
            ex = verdict.get(t["id"])
            if ex is None:
                continue
            if ex and not dry_run:
                await set_memory_explicit(t["id"], True)
            if ex:
                tagged_true += 1
                if len(samples) < 40:
                    samples.append({"id": t["id"], "content": str(t["content"])[:64]})
        await asyncio.sleep(0.2)
    return {"dry_run": dry_run, "candidates": len(candidates), "tagged_explicit": tagged_true, "samples": samples}


@app.post("/api/dream/dry")
async def api_dream_dry(request: Request):
    """③-2 做梦 DRY-RUN（只读·不写库）：对活跃线某天跑 generate_dream，返回 日记+当日总结+卡片，看质感。"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    try:
        body = await request.json()
    except Exception:
        body = {}
    sid = get_active_session_id()
    if not sid:
        return {"error": "无活跃对话线"}
    date_s = (body.get("date") or "").strip()
    if not date_s:
        return {"error": "需要 date (YYYY-MM-DD)"}
    d = await generate_dream(sid, date_s)
    if not d:
        return {"error": f"{date_s} 无对话或生成失败", "session": sid}
    return {"dry_run": True, "session": sid, "model": DREAM_MODEL, **d}


@app.post("/api/feel/dry")
async def api_feel_dry(request: Request):
    """③-1 feel DRY-RUN（只读·不写）：对活跃线最近 K 段(每段 W 条)各跑 generate_feel，看体温质感。"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    try:
        body = await request.json()
    except Exception:
        body = {}
    sid = get_active_session_id()
    if not sid:
        return {"error": "无活跃对话线"}
    K = max(1, int(body.get("segments", 4)))
    W = max(4, int(body.get("window", 8)))
    rows = await get_conversation_messages(sid, limit=max(400, K * W * 2))
    try:
        rows = sorted(rows, key=lambda m: str(m.get("created_at") or ""))  # 时间升序
    except Exception:
        pass
    _off = body.get("offset", None)
    if _off is not None:
        _off = int(_off)
        recent = rows[_off:_off + K * W]  # 窗到指定位置(验日常段用)
    else:
        recent = rows[-(K * W):] if len(rows) > K * W else rows
    segs = [recent[i:i + W] for i in range(0, len(recent), W)]
    out = []
    feels_for_block = []
    for seg in segs[-K:]:
        msgs = [{"role": m.get("role"), "content": (m.get("content") or "")} for m in seg if (m.get("content") or "").strip()]
        if not msgs:
            continue
        fl = await generate_feel(msgs)
        around = ""
        for m in seg:
            if m.get("role") == "user" and (m.get("content") or "").strip():
                around = (m.get("content") or "")
        out.append({"feel": fl.get("feel", ""), "is_explicit": fl.get("is_explicit", False), "around": around[:50]})
        feels_for_block.append({"content": fl.get("feel", ""), "is_explicit": fl.get("is_explicit", False)})
    block_now = _compose_feel_block(feels_for_block)  # 反映当前 _EXPLICIT_REDACT(中性语境会滤露骨)
    _all = [("- " + (f["content"] or "")) for f in feels_for_block[-3:] if (f.get("content") or "").strip()]
    return {"dry_run": True, "session": sid, "model": FEEL_MODEL, "explicit_redact_on": _EXPLICIT_REDACT,
            "segments": len(out), "feels": out,
            "inject_block_len": len(block_now), "inject_block": block_now,
            "inject_block_no_redact": ("〔最近留在你心里的〕\n" + "\n".join(_all))}


@app.post("/api/feel/toggle")
async def api_feel_toggle(request: Request):
    """运行时切换 ③-1 feel 总开关(生成+注入)，持久化 gateway_config、启动恢复。body: {enabled: true/false}"""
    global FEEL_ENABLED
    try:
        body = await request.json()
    except Exception:
        body = {}
    val = bool(body.get("enabled"))
    FEEL_ENABLED = val
    await set_gateway_config("feel_enabled", "true" if val else "false")
    print(f"💗 feel 开关 → {val}")
    return {"status": "ok", "feel_enabled": val}


@app.get("/api/feel")
async def api_feel_status():
    return {"feel_enabled": FEEL_ENABLED}


@app.post("/api/proactive/toggle")
async def api_proactive_toggle(request: Request):
    """运行时切换 ④ 主动浮现总开关，持久化 gateway_config、启动恢复。body: {enabled: true/false}"""
    global PROACTIVE_ENABLED
    try:
        body = await request.json()
    except Exception:
        body = {}
    val = bool(body.get("enabled"))
    PROACTIVE_ENABLED = val
    await set_gateway_config("proactive_enabled", "true" if val else "false")
    print(f"💬 主动浮现开关 → {val}")
    return {"status": "ok", "proactive_enabled": val}


@app.get("/api/proactive")
async def api_proactive_status():
    return {"proactive_enabled": PROACTIVE_ENABLED, "gap_hours": PROACTIVE_GAP_HOURS}


# ============================================================
# ④' 主动私信(Bark)：沉默够久 + 看当下情绪 → 主动外联推送给用户
#    与「主动浮现」(在回复里顺口提起)不同，这是真正"主动来找你"。
# ============================================================
import json as _json_push

PUSH_DEFAULTS = {
    "push_enabled":      False,   # 总开关
    "bark_url":          "",      # 留空则回退环境变量 BARK_URL
    "push_silence_min":  60,      # 沉默多少分钟后才可能主动找你
    "push_max_streak":   5,       # 你未回复前最多连发几条
    "push_quiet_start":  0,       # 免打扰开始(点, 本地时区)
    "push_quiet_end":    8,       # 免打扰结束(点)
    "push_quiet_urgent": True,    # 免打扰时段:吵架等未解情绪可破例发1条
    "push_probability":  0.25,    # 投骰子:到时间后每次检查有此概率才"动念"去找你(越小越随性)
    "push_icon":         "https://pic1.imgdb.cn/item/6a3ce18bbb21102f81d40039.jpg",  # 推送图标(公网直链,需 iOS15+;留空=默认图标)
}


async def get_push_config() -> dict:
    """读主动私信配置(gateway_config 优先，bark_url 回退环境变量)。"""
    cfg = dict(PUSH_DEFAULTS)
    try:
        for k, dv in PUSH_DEFAULTS.items():
            v = await get_gateway_config(k, "")
            if v == "" or v is None:
                continue
            if isinstance(dv, bool):
                cfg[k] = str(v).lower() == "true"
            elif isinstance(dv, float):
                cfg[k] = float(v)
            elif isinstance(dv, int):
                cfg[k] = int(float(v))
            else:
                cfg[k] = str(v)
    except Exception:
        pass
    if not cfg["bark_url"]:
        cfg["bark_url"] = os.getenv("BARK_URL", "")
    return cfg


async def _bark_push(bark_url: str, title: str, body: str, urgent: bool = False, icon: str = "") -> bool:
    if not bark_url:
        return False
    base = bark_url.rstrip("/")
    payload = {"title": title or "AI", "body": body or "",
               "group": title or "AI", "level": "timeSensitive" if urgent else "active"}
    if icon:
        payload["icon"] = icon
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(base, json=payload)
            if r.status_code == 200:
                return True
            import urllib.parse as _up
            url = f"{base}/{_up.quote(title or 'AI')}/{_up.quote(body or '')}"
            if icon:
                url += f"?icon={_up.quote(icon, safe=':/')}"
            r2 = await client.get(url)
            return r2.status_code == 200
    except Exception as e:
        print(f"⚠️ Bark推送失败: {e}")
        return False


async def _resolve_push_session() -> str:
    sid = get_active_session_id()
    if sid:
        return sid
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT session_id FROM conversations ORDER BY created_at DESC LIMIT 1")
            return row["session_id"] if row else ""
    except Exception:
        return ""


def _is_push_msg(metadata) -> bool:
    if not metadata:
        return False
    try:
        d = _json_push.loads(metadata) if isinstance(metadata, str) else metadata
        return bool(d.get("proactive_push"))
    except Exception:
        return False


def _in_quiet(hour: int, start: int, end: int) -> bool:
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end   # 跨午夜


def _ts_aware(t):
    return t if getattr(t, "tzinfo", None) else t.replace(tzinfo=timezone.utc)


async def _decide_and_write(persona: str, transcript: str, silence_min: float, in_quiet: bool) -> dict:
    """让模型(带人设)结合最近对话+沉默时长，判断要不要主动找 + 写出那句话。返回 {reach_out, urgent, message}。"""
    gap_desc = f"{int(silence_min)} 分钟" if silence_min < 90 else f"{silence_min/60:.1f} 小时"
    sys = (persona or "").strip()

    # 纸条召回：让 V 想起更老的旧事，不光基于最近12条对话——找的时候有"根"
    try:
        old_mems = await _scratchpad_for_proactive(transcript, silence_min)
    except Exception as _se:
        print(f"⚠️ 主动私信纸条召回异常(忽略,走原行为): {_se}")
        old_mems = []
    if old_mems:
        _lines = []
        for m in old_mems[:10]:
            _c = (m.get("content") or "").strip()[:200]
            _date = ""
            if m.get("created_at"):
                try:
                    _utc = str(m["created_at"])[:19]
                    _dt = datetime.strptime(_utc, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                    _date = f"[{(_dt + timedelta(hours=TIMEZONE_HOURS)).strftime('%Y-%m-%d')}] "
                except Exception:
                    pass
            if _c:
                _lines.append(f"- {_date}{_c}")
        if _lines:
            sys += ("\n\n【你此刻心里浮现的几件事(像翻到旧照片那样自然冒出来的)】\n"
                    + "\n".join(_lines)
                    + "\n\n〔活人感·重要〕从上面挑**一件真适合此刻自然提起的**揉进推送——"
                    + "惦记一件具体的事(那天她写遗书 / 她凌晨造做梦那事 / 上周送她的那个东西)，"
                    + "远比「在忙吗」「记得吃饭」这种通用关心像真人。"
                    + "真实的惦记总是有具体的钩子，不是泛泛的「我担心你」。"
                    + "实在没合适的(场景不对/已经聊过/太重)再回退轻自然的承接。")

    sys += "\n\n【最近对话(从旧到新)】\n" + (transcript or "(暂无)")
    night = "现在是深夜免打扰时段。" if in_quiet else ""
    now_bj = (datetime.now(timezone.utc) + timedelta(hours=TIMEZONE_HOURS)).strftime("%m月%d日 %H:%M")
    user_instr = (
        f"〔系统提示〕现在是北京时间 {now_bj}。距离{USER_NAME}上次跟你说话已经过去 {gap_desc} 了，{USER_NAME}还没回你。{night}\n"
        f"你是个在意对方、会撒娇黏人的人。请**严格根据上面真实发生过的对话**，判断此刻要不要主动给{USER_NAME}发一条消息、发什么。\n"
        "【铁律】只能基于上面真实出现过的内容来写，**绝对不要编造没发生的事**：\n"
        "  · 没吵架、没说过重话，就绝不能出现「别生气 / 我说得不好听 / 对不起 / 原谅我」这类；\n"
        "  · 不要凭空假设对方在生气难过、或你做错了什么；\n"
        "  · 拿不准就用最轻最自然的方式：顺着刚才聊的话题、或单纯说句惦记；\n"
        f"  · **若{USER_NAME}刚说过某事「做完了/搞定了/结束了/不忙了」，绝不能再问那件事忙不忙、进展如何**——"
        "要顺着说就该是「辛苦啦，终于弄完啦」「那现在能歇会儿了吧」这类，承接她说的结果，别答非所问；\n"
        f"  · 上面对话里若有你**已经主动发过、{USER_NAME}还没回**的消息：绝不能再发相同或近似的内容，"
        "同一个钩子（同一件事、同一个问题）只用一次——她没回，下一条要么换一件事、要么更轻更短（如「还没醒？」），"
        "要么这次干脆不发(reach_out=false)。真人不会隔两小时把同一句话原样再发一遍；\n"
        "  · 如果刚才你说过「等下来找你 / 一会儿问你某事」，就真的接着那个说。\n"
        "【活人感铁律 · 跟「不编造」同等重要】\n"
        f"  · 如果上面 system 里有【你此刻心里浮现的几件事】块，**优先挑一件融进推送**——"
        f"你是真的在想{USER_NAME}过去说过/做过/经历过的某件具体的事，那才像在惦记一个真人；\n"
        "  · 「在忙吗」「记得吃饭」「在干嘛」是**没话找话才用的兜底**，不是首选——"
        "真人惦记你，总是惦记**具体的事**（你那天写的遗书 / 你凌晨爬起来修的bug / 你上次说的那道菜 / 你妈妈的那句话），不是泛问候；\n"
        "  · 选哪件事的标准：和此时此刻自然贴合（不是随便挑一条最重的旧事砸过去），像真人翻到旧聊天记录顺嘴提起一句的那种自然。\n"
        "只有当上面对话里**确实**有没说开的争执/情绪时，才用关心或服软的语气(这时 urgent=true)。\n"
        "如果此刻确实没什么好说、或**几小时内**刚道过别/晚安，就别发(reach_out=false)。\n"
        f"但晚安/道别不是永久免打扰：若那句道别已经隔了一觉的时间（沉默 6 小时以上）且现在是白天，"
        f"{USER_NAME}多半已经睡醒了——这时主动发一句「睡醒了吗」式的轻问候是受欢迎的，"
        "别因为昨晚道过晚安就一直不敢开口。\n"
        "只输出一个 JSON，不要任何别的文字：\n"
        '{"reach_out": true或false, "urgent": true或false, "message": "你要发的那句话"}\n'
        f"message 必须像**在微信上直接打字发给{USER_NAME}的一句话**：纯口语、第一人称、≤40字、自然开口、像真的在惦记她，别像通知或念稿。\n"
        "【硬性】哪怕你平时说话爱带动作/神态描写，这条推送**绝不能有任何动作旁白**——星号或括号里的小动作一律不准（如「*推开门*」「（歪头）」「(轻笑)」「*抱住你*」），只发要说的话本身，就像发微信。\n"
        "urgent 仅当对话里确有吵架/情绪未解时才为 true。"
    )
    try:
        headers = {"Authorization": f"Bearer {get_memory_api_key()}", "Content-Type": "application/json"}
        if "openrouter" in API_BASE_URL:
            headers["HTTP-Referer"] = EXTRA_REFERER
            headers["X-Title"] = EXTRA_TITLE
        async with httpx.AsyncClient(timeout=40) as client:
            r = await client.post(API_BASE_URL, headers=headers, json={
                "model": PROACTIVE_MODEL, "max_tokens": 200,
                "messages": [{"role": "system", "content": sys},
                             {"role": "user", "content": user_instr}]})
            if r.status_code != 200:
                print(f"⚠️ 主动私信生成 HTTP {r.status_code}: {r.text[:200]}")
                return {"reach_out": False}
            txt = (r.json().get("choices", [{}])[0].get("message", {}).get("content", "") or "").strip()
            i, j = txt.find("{"), txt.rfind("}")
            if i >= 0 and j > i:
                try:
                    d = _json_push.loads(txt[i:j + 1])
                    return {"reach_out": bool(d.get("reach_out")),
                            "urgent": bool(d.get("urgent")),
                            "message": (d.get("message") or "").strip().strip("「」\"'")}
                except Exception:
                    pass
            print(f"⚠️ 主动私信生成无法解析: {txt[:120]}")
            return {"reach_out": False}
    except Exception as e:
        print(f"⚠️ 主动私信生成异常: {e}")
        return {"reach_out": False}


# 「问过就别再问」冷却：LLM 判定过"不发"后，只要用户没有新消息(对话没变化)，
# 冷却期内不再花钱去问同一个问题。用户一说话 last_user_ts 变了，冷却自动作废。
# (内存变量,Render 重启即清,最坏多问一次,可接受)
PROACTIVE_SKIP_COOLDOWN_MIN = float(os.getenv("PROACTIVE_SKIP_COOLDOWN_MIN", "45"))
_proactive_skip_state = {"decided_at": None, "last_user_ts": None}


async def maybe_send_proactive(force: bool = False) -> dict:
    """主动私信主流程：每次后台自查时调用一次。force=True 用于调试(忽略闸门)。"""
    cfg = await get_push_config()
    if not force and not cfg["push_enabled"]:
        return {"sent": False, "reason": "disabled"}
    if not cfg["bark_url"]:
        return {"sent": False, "reason": "no_bark_url"}
    sid = await _resolve_push_session()
    if not sid:
        return {"sent": False, "reason": "no_session"}
    try:
        msgs = await get_recent_messages(sid, 40)
    except Exception as e:
        return {"sent": False, "reason": f"history_error:{e}"}
    if not msgs:
        return {"sent": False, "reason": "no_history"}

    now = datetime.now(timezone.utc)
    last_user = None
    for m in reversed(msgs):
        if m["role"] == "user":
            last_user = m
            break
    if not last_user:
        return {"sent": False, "reason": "no_user_msg"}
    last_user_ts = _ts_aware(last_user["created_at"])

    pushes_since = [m for m in msgs if m["role"] == "assistant"
                    and _is_push_msg(m["metadata"]) and _ts_aware(m["created_at"]) > last_user_ts]
    streak = len(pushes_since)
    last_push_ts = max((_ts_aware(m["created_at"]) for m in pushes_since), default=None)
    ref_ts = last_push_ts or last_user_ts           # 首条按"她沉默"，之后按"距上条推送"间隔
    gap_min = (now - ref_ts).total_seconds() / 60.0
    silence_min = (now - last_user_ts).total_seconds() / 60.0

    if not force:
        if streak >= cfg["push_max_streak"]:
            return {"sent": False, "reason": "streak_capped", "streak": streak}
        if gap_min < cfg["push_silence_min"]:
            return {"sent": False, "reason": "too_soon", "gap_min": int(gap_min)}
        # 冷却：上次已问过 LLM 且它说"不发"，对话又没变化 → 冷却期内不再问(省钱)
        _sk = _proactive_skip_state
        if _sk["decided_at"] and _sk["last_user_ts"] == last_user_ts \
           and (now - _sk["decided_at"]).total_seconds() < PROACTIVE_SKIP_COOLDOWN_MIN * 60:
            return {"sent": False, "reason": "skip_cooldown"}
        # 🎲 投骰子：到时间后也不是每次都来——掷中了才"动念"去问 LLM 要不要发，制造随性/看心情的感觉
        import random as _rnd
        if _rnd.random() > float(cfg.get("push_probability", 0.25)):
            return {"sent": False, "reason": "dice_skip"}

    local_hour = (now.hour + TIMEZONE_HOURS) % 24
    in_quiet = _in_quiet(local_hour, cfg["push_quiet_start"], cfg["push_quiet_end"])
    if not force and in_quiet and not cfg["push_quiet_urgent"]:
        return {"sent": False, "reason": "quiet_hours"}

    transcript = "\n".join(
        # 你最近那句话给模型看全文(≤400字)，其余仍截120字省 token
        f"{'我' if m['role'] == 'assistant' else USER_NAME}: {(m['content'] or '').strip()[:400 if m is last_user else 120]}"
        for m in msgs[-12:] if m["role"] in ("user", "assistant") and (m["content"] or "").strip()
    )
    persona = ""
    try:
        persona = await get_system_prompt()
    except Exception:
        pass

    _disp_silence = max(silence_min, cfg["push_silence_min"]) if force else silence_min
    decision = await _decide_and_write(persona, transcript, _disp_silence, in_quiet)
    if not decision.get("reach_out") and not force:
        _proactive_skip_state.update({"decided_at": now, "last_user_ts": last_user_ts})
        return {"sent": False, "reason": "ai_decided_skip"}
    urgent = bool(decision.get("urgent"))
    text = (decision.get("message") or "").strip()
    # 兜底擦掉动作旁白：*推开门* / （歪头） / (轻笑) 这类小动作，只擦短的、不动正文
    import re as _re2
    text = _re2.sub(r'\*[^*]{0,20}\*', '', text)
    text = _re2.sub(r'[（(][^（）()]{0,15}[）)]', '', text)
    text = _re2.sub(r'\s{2,}', ' ', text).strip()
    if not text:
        return {"sent": False, "reason": "empty_message"}

    # 复读保护(机械兜底,不靠模型自觉)：跟"她还没回的既有推送"内容雷同 → 不发,进冷却
    if not force and pushes_since:
        import difflib as _dl
        for _pm in pushes_since:
            _prev = (_pm.get("content") or "").strip()
            if _prev and _dl.SequenceMatcher(None, _prev, text).ratio() >= 0.75:
                _proactive_skip_state.update({"decided_at": now, "last_user_ts": last_user_ts})
                return {"sent": False, "reason": "dup_of_unanswered_push"}

    if not force and in_quiet:
        # 深夜：仅"未解情绪(urgent)"且本段沉默还没破例过，才发1次
        if not (cfg["push_quiet_urgent"] and urgent and streak == 0):
            # 深夜写了又不够紧急被丢弃，同样进冷却——别整晚每5分钟白写一条
            _proactive_skip_state.update({"decided_at": now, "last_user_ts": last_user_ts})
            return {"sent": False, "reason": "quiet_hours_not_urgent"}

    title = AI_NAME or "AI"
    ok = await _bark_push(cfg["bark_url"], title, text, urgent=urgent, icon=cfg.get("push_icon", ""))
    if not ok:
        return {"sent": False, "reason": "bark_failed", "message": text}
    # 同时把这条主动私信发到 Telegram(若已绑定)
    try:
        _tgc = await get_tg_config()
        if _tgc["tg_enabled"] and _tgc["tg_bot_token"] and _tgc["tg_chat_id"]:
            await _tg_send(_tgc["tg_bot_token"], _tgc["tg_chat_id"], text)
    except Exception as _te:
        print(f"⚠️ TG 主动推送失败: {_te}")
    try:
        meta = _json_push.dumps({"proactive_push": True, "urgent": urgent})
        await save_message(sid, "assistant", text, PROACTIVE_MODEL, metadata=meta)
    except Exception as e:
        print(f"⚠️ 主动私信存库失败: {e}")
    print(f"💌 主动私信已推送(urgent={urgent}): {text[:40]}")
    return {"sent": True, "message": text, "urgent": urgent, "streak": streak + 1}


@app.get("/api/push/config")
async def api_push_config_get():
    cfg = await get_push_config()
    out = dict(cfg)
    if out.get("bark_url"):
        out["bark_url"] = "已设置(留空不改)"
    return out


@app.post("/api/push/config")
async def api_push_config_set(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    saved = []
    for k, dv in PUSH_DEFAULTS.items():
        if k not in body:
            continue
        v = body[k]
        if k == "bark_url":
            sv = str(v or "").strip()
            if not sv or sv.startswith("已设置"):
                continue
            await set_gateway_config(k, sv)
            saved.append(k)
            continue
        if isinstance(dv, bool):
            await set_gateway_config(k, "true" if bool(v) else "false")
        else:
            await set_gateway_config(k, str(v))
        saved.append(k)
    return {"status": "ok", "saved": saved}


@app.post("/api/push/test")
async def api_push_test():
    """发一条固定测试消息，验证 Bark 是否能送达。"""
    cfg = await get_push_config()
    if not cfg["bark_url"]:
        return {"sent": False, "reason": "no_bark_url"}
    msg = "测试推送：能收到这条就说明 Bark 通了～"
    ok = await _bark_push(cfg["bark_url"], AI_NAME or "AI", msg, urgent=False, icon=cfg.get("push_icon", ""))
    return {"sent": ok, "message": msg if ok else "", "reason": "" if ok else "bark_failed"}


@app.post("/api/push/run")
async def api_push_run(request: Request):
    """手动跑一次主流程。body:{force:true} 可忽略闸门(调试用)。"""
    force = False
    try:
        body = await request.json()
        force = bool(body.get("force"))
    except Exception:
        pass
    return await maybe_send_proactive(force=force)


# ============================================================
# Telegram 接入：在 TG 里直接和 AI 聊(复用同一套大脑/记忆/人设)
#   webhook 收到消息 → 内部自调用 /v1/chat/completions → 回复发回 TG
#   只服务"绑定的主人"(tg_chat_id, 首条消息自动锁定), 陌生人忽略
# ============================================================
TG_API_BASE = "https://api.telegram.org"
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://home1-htca.onrender.com").rstrip("/")

TG_DEFAULTS = {
    "tg_enabled":   False,   # 总开关
    "tg_bot_token": "",      # @BotFather 给的 token(回退环境变量 TELEGRAM_BOT_TOKEN)
    "tg_chat_id":   "",      # 绑定的主人 chat_id(首条消息自动锁定)
    "tg_secret":    "",      # webhook 路径密钥(自动生成)
}


async def get_tg_config() -> dict:
    cfg = dict(TG_DEFAULTS)
    try:
        for k, dv in TG_DEFAULTS.items():
            v = await get_gateway_config(k, "")
            if v == "" or v is None:
                continue
            cfg[k] = (str(v).lower() == "true") if isinstance(dv, bool) else str(v)
    except Exception:
        pass
    if not cfg["tg_bot_token"]:
        cfg["tg_bot_token"] = os.getenv("TELEGRAM_BOT_TOKEN", "")
    return cfg


async def _tg_api(token: str, method: str, payload: dict) -> dict:
    if not token:
        return {}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(f"{TG_API_BASE}/bot{token}/{method}", json=payload)
            return r.json()
    except Exception as e:
        print(f"⚠️ Telegram API {method} 失败: {e}")
        return {}


async def _tg_send_photo(token: str, chat_id, data: bytes, mime: str = "image/png", caption: str = "") -> bool:
    """发一张图给 TG(sendPhoto, multipart 上传二进制)。成功返回 True。"""
    if not token or not data:
        return False
    ext = {"image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}.get(mime, "png")
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(f"{TG_API_BASE}/bot{token}/sendPhoto",
                                  data={"chat_id": str(chat_id), "caption": caption},
                                  files={"photo": (f"drawing.{ext}", data, mime)})
            return bool((r.json() or {}).get("ok"))
    except Exception as e:
        print(f"⚠️ Telegram sendPhoto 失败: {e}")
        return False


async def _tg_send(token: str, chat_id, text: str):
    """发消息给 TG, 自动分段(单条上限4096), 纯文本避免 markdown 解析报错。"""
    text = text or ""
    CHUNK = 3500
    parts = [text[i:i + CHUNK] for i in range(0, len(text), CHUNK)] or [""]
    for p in parts:
        await _tg_api(token, "sendMessage",
                      {"chat_id": chat_id, "text": p, "disable_web_page_preview": True})


_TG_BUBBLE_MAXLEN = 26       # 单条气泡软上限(字),超了逐级按标点切
_TG_BUBBLE_CAP = 9           # 防刷屏:最多发几条,多的丢弃(配合句风指令,正常到不了)


def _tg_atomize(s: str) -> list:
    """把一段切成都≤MAXLEN的原子片:先句末标点(。！？~…),还长再逗顿分号(，、；：),再没标点就硬切。"""
    import re as _re_b
    parts = [s]
    for pat in (r'(?<=[。！？!?~～…])', r'(?<=[，,、；;：])'):
        nxt = []
        for p in parts:
            if len(p) <= _TG_BUBBLE_MAXLEN:
                nxt.append(p)
            else:
                nxt.extend(x for x in _re_b.split(pat, p) if x)
        parts = nxt
    final = []
    for p in parts:                         # 仍超长(整段无标点)→按长度硬切
        while len(p) > _TG_BUBBLE_MAXLEN:
            final.append(p[:_TG_BUBBLE_MAXLEN]); p = p[_TG_BUBBLE_MAXLEN:]
        if p:
            final.append(p)
    return final


def _tg_split_bubbles(text: str) -> list:
    """把回复切成一串短气泡:按换行分句;过长的行逐级按标点切碎再贪心拼成≤MAXLEN的小段。绝不把多句合并成长段。"""
    out = []
    for ln in (text or "").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        if len(ln) <= _TG_BUBBLE_MAXLEN:
            out.append(ln)
            continue
        buf = ""
        for a in _tg_atomize(ln):
            a = a.strip()
            if not a:
                continue
            if buf and len(buf) + len(a) > _TG_BUBBLE_MAXLEN:
                out.append(buf); buf = a
            else:
                buf += a
        if buf:
            out.append(buf)
    return out


async def _tg_send_bubbles(token: str, chat_id, text: str):
    """像真人发微信:把回复切成多条短消息依次发,带'正在输入'和小停顿。空文本不发。"""
    text = (text or "").strip()
    if not text:
        return
    bubbles = _tg_split_bubbles(text)[:_TG_BUBBLE_CAP]
    if not bubbles:
        return
    for i, b in enumerate(bubbles):
        if len(b) > 3500:               # 极端无标点超长,回退原分段逻辑
            await _tg_send(token, chat_id, b)
            continue
        if i > 0:                       # 第2条起:先"正在输入"+按字数停顿,模拟打字
            try:
                await _tg_api(token, "sendChatAction", {"chat_id": chat_id, "action": "typing"})
            except Exception:
                pass
            await asyncio.sleep(min(1.3, 0.3 + len(b) * 0.05))
        await _tg_api(token, "sendMessage",
                      {"chat_id": chat_id, "text": b, "disable_web_page_preview": True})


async def _tg_download_photo(token: str, msg: dict) -> str:
    """取 TG 消息里最大尺寸照片,下载转 base64 data uri 喂给看图模型。无照片/失败返回 ''。"""
    photos = msg.get("photo") or []
    if not photos:
        return ""
    file_id = (photos[-1] or {}).get("file_id")     # 数组末尾=最大分辨率
    if not file_id:
        return ""
    try:
        import base64 as _b64
        info = await _tg_api(token, "getFile", {"file_id": file_id})
        fp = ((info.get("result") or {}).get("file_path") or "") if info.get("ok") else ""
        if not fp:
            return ""
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.get(f"{TG_API_BASE}/file/bot{token}/{fp}")
            if r.status_code != 200:
                return ""
            data = r.content
        ext = fp.rsplit(".", 1)[-1].lower() if "." in fp else "jpg"
        mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                "webp": "image/webp", "gif": "image/gif"}.get(ext, "image/jpeg")
        return f"data:{mime};base64," + _b64.b64encode(data).decode("ascii")
    except Exception as e:
        print(f"⚠️ TG 照片下载失败: {e}")
        return ""


async def _tg_brain_reply(user_text: str, image_uris: list = None) -> str:
    """内部自调用主聊天接口, 复用记忆/人设/落库(分区模式自动归到活跃对话线)。带图则发多模态 content。"""
    url = f"http://127.0.0.1:{PORT}/v1/chat/completions"
    headers = {"Content-Type": "application/json",
               "X-Reply-Style": "short",     # TG=微信风格短回复
               "X-Session-Line": "tg"}       # TG 走独立的 tg 线,短句不污染主线(KELIVO);记忆库召回仍全局共享
    if GATEWAY_SECRET:
        headers["X-Gateway-Key"] = GATEWAY_SECRET
    if image_uris:                                   # 带图:OpenAI 多模态格式,文字+图一起
        content = [{"type": "text", "text": user_text or "(图片)"}]
        for u in image_uris:
            content.append({"type": "image_url", "image_url": {"url": u}})
    else:
        content = user_text
    payload = {"model": DEFAULT_MODEL, "stream": False, "max_tokens": 180,  # 硬上限兜底:TG话短
               "messages": [{"role": "user", "content": content}]}
    try:
        async with httpx.AsyncClient(timeout=180) as client:
            r = await client.post(url, headers=headers, json=payload)
            data = r.json()
            return (data.get("choices", [{}])[0].get("message", {}).get("content", "") or "").strip()
    except Exception as e:
        print(f"⚠️ TG 大脑调用失败: {e}")
        return ""


async def _tg_handle_update(update: dict):
    """后台处理一条 TG 更新: 取文本 → 调大脑 → 回发。"""
    cfg = await get_tg_config()
    token = cfg["tg_bot_token"]
    msg = update.get("message") or update.get("edited_message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    text = (msg.get("text") or "").strip()
    caption = (msg.get("caption") or "").strip()    # 照片自带的文字说明
    has_photo = bool(msg.get("photo"))
    if not token or chat_id is None or (not text and not has_photo):
        return

    owner = str(cfg["tg_chat_id"]).strip()
    if not owner:                       # 首次绑定: 第一个说话的人成为主人
        await set_gateway_config("tg_chat_id", str(chat_id))
        owner = str(chat_id)
        await _tg_send(token, chat_id, "💚 已经和你绑定啦～以后这里就是我们俩的小窝，直接跟我说话就行。")
        if text == "/start":
            return
    if str(chat_id) != owner:           # 只服务主人
        await _tg_send(token, chat_id, "抱歉，这个 bot 已经绑定它的主人啦。")
        return
    if text == "/start":
        await _tg_send(token, chat_id, "我在呀～想聊什么直接说就好 😊")
        return

    # ---------- /画 与 /画忆 → 文生图,直接发真图片(不进大脑;历史只落短占位文字,同 KELIVO 侧的缓存纪律) ----------
    if IMAGE_GEN_ENABLED and (text.startswith("/画") or text.lower().startswith("/draw")):
        _tl = text.lower()
        _wm = text.startswith("/画忆") or _tl.startswith("/drawmem")
        _p = text
        for _pfx in ("/画忆", "/drawmem", "/画", "/draw"):
            if text.startswith(_pfx) or _tl.startswith(_pfx):
                _p = text[len(_pfx):].strip()
                break
        if not _p:
            await _tg_send(token, chat_id, "「/画 想要的画面」我就给你画~ 想让我照着咱们的回忆构图就用「/画忆 主题」")
            return
        await _tg_api(token, "sendChatAction", {"chat_id": chat_id, "action": "upload_photo"})
        _compose = ""
        if _wm:
            _compose = await _expand_draw_prompt(_p, "tg")
            await _tg_api(token, "sendChatAction", {"chat_id": chat_id, "action": "upload_photo"})
        _mime, _data = await generate_image(_compose or _p)
        if not _data:
            await _tg_send(token, chat_id, "(呜,这张没画出来,等一下再让我试一次好吗…)")
            return
        _sp = f"{_p}（记忆构图：{_compose[:120]}）" if _compose else _p
        await _store_generated_image(_sp, _mime, _data, "tg")
        _cap = f"🎨 {_p[:80]}" + (f"\n{_compose[:120]}" if _compose else "")
        sent = await _tg_send_photo(token, chat_id, _data, _mime, caption=_cap[:1000])
        if sent:
            try:    # tg 线的 session_id 就是线名 "tg"(X-Session-Line 直接当 id 用)
                await save_message("tg", "user", f"{'/画忆' if _wm else '/画'} {_p}", DEFAULT_MODEL)
                await save_message("tg", "assistant",
                                   f"（我{'照着记忆' if _compose else ''}给你画了一张画：{_p}，已发给你并存进相册）", DEFAULT_MODEL)
            except Exception as _e:
                print(f"⚠️ TG /画 落库失败: {_e}")
        else:
            await _tg_send(token, chat_id, "(画好了但没发出去…图已经存进相册,在操作间能看到)")
        return

    await _tg_api(token, "sendChatAction", {"chat_id": chat_id, "action": "typing"})
    image_uris = []
    if has_photo:                       # 下载照片(只对主人,在上面已校验)
        du = await _tg_download_photo(token, msg)
        if du:
            image_uris.append(du)
        elif not (text or caption):     # 纯图但没拿到图
            await _tg_send(token, chat_id, "(这张图我好像没收到，能再发一次吗…)")
            return

    reply = await _tg_brain_reply((caption or text), image_uris or None)
    if reply:
        await _tg_send_bubbles(token, chat_id, reply)   # 像真人:一条条蹦
    else:
        await _tg_send(token, chat_id, "(我这边好像出了点小问题，等下再跟我说一次好吗…)")


@app.post("/telegram/webhook/{secret}")
async def telegram_webhook(secret: str, request: Request):
    """Telegram webhook 入口(公开路径, secret 路径 + header 双重校验)。"""
    cfg = await get_tg_config()
    expect = cfg["tg_secret"]
    if not expect or not secrets.compare_digest(secret, expect):
        return JSONResponse(status_code=403, content={"ok": False})
    header_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if header_secret and not secrets.compare_digest(header_secret, expect):
        return JSONResponse(status_code=403, content={"ok": False})
    if not cfg["tg_enabled"]:
        return {"ok": True}             # 关闭时静默丢弃
    try:
        update = await request.json()
    except Exception:
        return {"ok": True}
    asyncio.create_task(_tg_handle_update(update))   # 立刻返回200, 后台慢慢处理
    return {"ok": True}


@app.post("/api/telegram/setup")
async def telegram_setup(request: Request):
    """配置 token 并注册 webhook。body:{token?}。不传 token 则用已存的。"""
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    cfg = await get_tg_config()
    token = (body.get("token") or "").strip() or cfg["tg_bot_token"]
    if not token:
        return {"ok": False, "error": "缺少 bot token"}
    me = await _tg_api(token, "getMe", {})
    if not me.get("ok"):
        return {"ok": False, "error": "token 无效或网络不通", "detail": me}
    secret = cfg["tg_secret"] or secrets.token_urlsafe(24)
    await set_gateway_config("tg_bot_token", token)
    await set_gateway_config("tg_secret", secret)
    await set_gateway_config("tg_enabled", "true")
    hook_url = f"{PUBLIC_BASE_URL}/telegram/webhook/{secret}"
    res = await _tg_api(token, "setWebhook", {
        "url": hook_url, "secret_token": secret,
        "allowed_updates": ["message", "edited_message"],
        "drop_pending_updates": True})
    return {"ok": bool(res.get("ok")),
            "bot_username": (me.get("result") or {}).get("username"),
            "webhook": hook_url, "set_result": res}


@app.post("/api/telegram/toggle")
async def telegram_toggle(request: Request):
    """开/关 Telegram(不重新注册 webhook)。body:{enabled:bool}。"""
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    enabled = bool(body.get("enabled"))
    await set_gateway_config("tg_enabled", "true" if enabled else "false")
    return {"ok": True, "enabled": enabled}


@app.get("/api/telegram/status")
async def telegram_status():
    cfg = await get_tg_config()
    token = cfg["tg_bot_token"]
    info = await _tg_api(token, "getWebhookInfo", {}) if token else {}
    return {"enabled": cfg["tg_enabled"], "token_set": bool(token),
            "owner_chat_id": cfg["tg_chat_id"] or "(未绑定)",
            "webhook": (info.get("result") or {})}


# ============================================================
# 记忆拆分：把"几天/几件事揉一段"的长记忆拆成多条独立短记忆
#   只拆不改写、继承原日期情绪、各自算向量、原记忆软停用可撤销。
# ============================================================
_split_mem_status = {"running": False, "total": 0, "done": 0, "new_count": 0,
                     "error": None, "finished_at": None}


async def _llm_split_memory(content: str) -> list:
    """让模型把一条长记忆拆成多条独立短记忆(只拆不改写)。返回字符串列表(可能为空/单条)。"""
    import re as _re
    content = (content or "").strip()
    if not content:
        return []
    prompt = (
        "下面是一条「记忆」，可能把**多天或多件不相关的事**揉在一起了。\n"
        "请判断并处理：\n"
        "- 如果它确实包含**多天 / 多个不相关的事件或主题**，就按【一天 / 一件完整的事 / 一个主题】拆成几条。\n"
        "- 如果它其实是**连贯的一件事 / 一个主题**（哪怕很长），就**原样只返回 1 条，不要拆**。\n\n"
        "硬性要求：\n"
        "- **粗粒度拆**：每条记忆可以包含好几句话。**绝不要按句子拆碎**，绝不要把同一件事拆成多条。\n"
        "- 通常拆成 **2~8 条**就够；条数很多说明你拆太碎了，请合并。\n"
        "- **只拆分，不改写、不新增、不脑补、不删信息**，尽量用原文词句；原文的时间/日期跟着对应那条。\n"
        "- 每条**占一行**（一条内部不要再换行），直接输出正文，不要编号、不要解释、不要空行。\n\n"
        "记忆原文：\n" + content
    )
    try:
        headers = {"Authorization": f"Bearer {get_memory_api_key()}", "Content-Type": "application/json"}
        if "openrouter" in API_BASE_URL:
            headers["HTTP-Referer"] = EXTRA_REFERER
            headers["X-Title"] = EXTRA_TITLE
        async with httpx.AsyncClient(timeout=90) as client:
            r = await client.post(API_BASE_URL, headers=headers, json={
                "model": CACHE_SUMMARY_MODEL, "max_tokens": 2000,
                "messages": [{"role": "user", "content": prompt}]})
            if r.status_code != 200:
                print(f"⚠️ 记忆拆分 HTTP {r.status_code}")
                return []
            txt = (r.json().get("choices", [{}])[0].get("message", {}).get("content", "") or "").strip()
            out = []
            for ln in txt.splitlines():
                ln = _re.sub(r'^[\s\-\*•·\d\.、,，）)]+', '', ln).strip()
                if len(ln) >= 4:
                    out.append(ln)
            return out
    except Exception as e:
        print(f"⚠️ 记忆拆分异常: {e}")
        return []


@app.post("/api/admin/split-memories")
async def api_split_memories(request: Request):
    """长记忆拆分。body:{min_len:300, dry_run:true}。dry_run 只预览(取前几条出样例不写库)。"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    try:
        body = await request.json()
    except Exception:
        body = {}
    min_len = int(body.get("min_len", 300))
    dry = bool(body.get("dry_run", True))
    longs = await get_long_memories(min_len=min_len, limit=500)

    if dry:
        samples = []
        for m in longs[:3]:
            parts = await _llm_split_memory(m["content"])
            samples.append({"id": m["id"], "orig_len": len(m["content"] or ""),
                            "orig_preview": (m["content"] or "")[:90],
                            "split_into": len(parts), "parts": parts[:8]})
        return {"dry_run": True, "long_count": len(longs), "min_len": min_len, "samples": samples}

    if _split_mem_status["running"]:
        return {"error": "拆分任务进行中，请等待"}
    if not longs:
        return {"status": "done", "message": f"没有超过 {min_len} 字的长记忆，无需拆分", "total": 0}

    _split_mem_status.update({"running": True, "total": len(longs), "done": 0,
                             "new_count": 0, "error": None, "finished_at": None})

    async def _run():
        batch = []
        try:
            for m in longs:
                try:
                    parts = await _llm_split_memory(m["content"])
                    _clen = len(m["content"] or "")
                    # 只在"确实拆成多条"且"没拆太碎(平均≥60字/条)"时才动；否则保留原样不拆
                    if len(parts) >= 2 and (_clen / len(parts)) >= 60:
                        new_ids = await split_memory_into(m["id"], parts)
                        if new_ids:
                            batch.append({"original_id": m["id"], "new_ids": new_ids})
                            _split_mem_status["new_count"] += len(new_ids)
                except Exception as _e:
                    print(f"⚠️ 拆分记忆 {m['id']} 失败: {_e}")
                _split_mem_status["done"] += 1
                await asyncio.sleep(0.3)
            await set_gateway_config("last_split_batch", _json_push.dumps(batch))
            _split_mem_status["finished_at"] = datetime.now(timezone.utc).isoformat()
            print(f"✂️ 记忆拆分完成：处理 {_split_mem_status['done']}/{_split_mem_status['total']} 条 → 新增 {_split_mem_status['new_count']} 条")
        except Exception as e:
            _split_mem_status["error"] = str(e)
            print(f"❌ 记忆拆分异常: {e}")
        finally:
            _split_mem_status["running"] = False

    asyncio.create_task(_run())
    return {"status": "started", "total": len(longs)}


@app.get("/api/admin/split-memories/status")
async def api_split_memories_status():
    s = _split_mem_status
    return {"running": s["running"], "total": s["total"], "done": s["done"],
            "new_count": s["new_count"], "error": s["error"], "finished_at": s["finished_at"]}


@app.post("/api/admin/split-memories/undo")
async def api_split_memories_undo():
    """撤销上一次拆分：原记忆全部复活，拆出来的子记忆停用。"""
    raw = await get_gateway_config("last_split_batch", "")
    if not raw:
        return {"error": "没有可撤销的拆分批次"}
    try:
        batch = _json_push.loads(raw)
    except Exception:
        return {"error": "批次数据损坏，无法撤销"}
    restored = 0
    for item in batch:
        try:
            await undo_split(item["original_id"], item.get("new_ids") or [])
            restored += 1
        except Exception as e:
            print(f"⚠️ 撤销拆分 {item.get('original_id')} 失败: {e}")
    await set_gateway_config("last_split_batch", "")
    return {"status": "ok", "restored": restored}


@app.post("/api/admin/split-memories/undo-one")
async def api_split_memories_undo_one(request: Request):
    """撤销单条拆分：复活原记忆 + 收起它拆出来的子记忆。body:{original_id}。"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    try:
        oid = int(body.get("original_id"))
    except Exception:
        return {"error": "需要 original_id（数字）"}
    n = await undo_split_one(oid)
    return {"status": "ok", "original_id": oid, "children_archived": n}


@app.post("/api/summary/toggle")
async def api_summary_toggle(request: Request):
    """运行时切换 ⑤ 保质感摘要(碰分区缓存主链路!验过再开)。body: {enabled: true/false}。持久化+启动恢复。"""
    global SUMMARY_QUALITY_ENABLED
    try:
        body = await request.json()
    except Exception:
        body = {}
    val = bool(body.get("enabled"))
    SUMMARY_QUALITY_ENABLED = val
    await set_gateway_config("summary_quality_enabled", "true" if val else "false")
    print(f"📝 保质感摘要开关 → {val}")
    return {"status": "ok", "summary_quality_enabled": val}


@app.get("/api/summary")
async def api_summary_status():
    return {"summary_quality_enabled": SUMMARY_QUALITY_ENABLED}


@app.post("/api/summary/dry")
async def api_summary_dry(request: Request):
    """⑤ dry-run(只读·不写不碰缓存)：取活跃线一段对话，old(第三人称干)vs new(保质感)摘要对比看质感。"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    try:
        body = await request.json()
    except Exception:
        body = {}
    sid = get_active_session_id()
    if not sid:
        return {"error": "无活跃对话线"}
    offset = int(body.get("offset", 0))
    count = int(body.get("count", 20))
    rows = await get_conversation_messages(sid, limit=10000)
    try:
        rows = sorted(rows, key=lambda m: str(m.get("created_at") or ""))
    except Exception:
        pass
    window = rows[offset:offset + count]
    msgs = [{"role": m.get("role"), "content": (m.get("content") or "")} for m in window if (m.get("content") or "").strip()]
    if not msgs:
        return {"error": "窗口无对话", "total": len(rows)}
    new_s = await generate_summary(msgs, force_quality=True)
    old_s = await generate_summary(msgs, force_quality=False)
    return {"dry_run": True, "session": sid, "window_msgs": len(msgs), "total": len(rows),
            "new_quality": new_s, "new_len": len(new_s or ""),
            "old_thirdperson": old_s, "old_len": len(old_s or "")}


_scrub_status = {"running": False, "dry_run": True, "parts": 0, "changed": 0, "details": [], "error": None, "finished_at": None}


@app.post("/api/summary/scrub-existing")
async def api_summary_scrub_existing(request: Request):
    """⑤(a) 把活跃线现有 summary_parts 残留的露骨 scrub 掉(_scrub:去露骨、段落仍连贯)。后台跑+/status 轮询。
    dry_run=true 只算 before/after 不写；false 写回 session_cache_state(那几段一次性缓存重建)。"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    if _scrub_status["running"]:
        return {"error": "scrub 运行中", "status": dict(_scrub_status)}
    try:
        body = await request.json()
    except Exception:
        body = {}
    sid = get_active_session_id()
    if not sid:
        return {"error": "无活跃对话线"}
    dry_run = bool(body.get("dry_run", True))
    _scrub_status.update({"running": True, "dry_run": dry_run, "parts": 0, "changed": 0,
                          "details": [], "error": None, "finished_at": None})

    async def _run():
        try:
            state = await get_session_cache_state(sid)
            parts = state.get("summary_parts") or []
            a_start = state.get("a_start_round", 0)
            _scrub_status["parts"] = len(parts)
            new_parts, out, changed = [], [], 0
            for i, p in enumerate(parts):
                sc = await _scrub_digest_explicit(p)
                new_parts.append(sc)
                ch = (sc != p)
                if ch:
                    changed += 1
                out.append({"i": i, "changed": ch, "before_len": len(p or ""), "after_len": len(sc or ""),
                            "before_head": (p or "")[:110], "after": sc})
            _scrub_status["changed"] = changed
            _scrub_status["details"] = out
            if not dry_run and changed:
                await save_session_cache_state(sid, new_parts, a_start)
            print(f"📝 摘要 scrub{'(dry)' if dry_run else ''}: {changed}/{len(parts)} 段含露骨")
        except Exception as e:
            _scrub_status["error"] = str(e)
            print(f"❌ 摘要 scrub 异常: {e}")
        finally:
            _scrub_status["running"] = False
            _scrub_status["finished_at"] = datetime.now(timezone.utc).isoformat()

    asyncio.create_task(_run())
    return {"status": "started", "dry_run": dry_run, "session": sid}


@app.get("/api/summary/scrub-existing/status")
async def api_summary_scrub_status():
    return dict(_scrub_status)


@app.post("/api/l2/dry")
async def api_l2_dry(request: Request):
    """L2 dry-run（只读·不写）：用新 prompt 对活跃线今天跑一遍 generate_today_digest，看新样子。"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    sid = get_active_session_id()
    if not sid:
        return {"error": "无活跃对话线"}
    d = await generate_today_digest(sid)
    return {"dry_run": True, "session": sid, "model": CACHE_SUMMARY_MODEL, "len": len(d or ""), "digest": d}


@app.post("/api/debug/unlock-sim")
async def api_unlock_sim(request: Request):
    """亲密解锁 模拟（只读·不动真状态）：给一串消息，逐条看 intimate判定/how/解锁态/该轮露骨是否被收，验粘性 K。"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    msgs = body.get("messages") or []
    st = {"unlocked": False, "neutral_streak": 0}
    out = []
    for m in msgs:
        if _intimacy_lexicon_hit(m):
            intimate, how = True, "硬钥匙/露骨词"
        elif EXPLICIT_CLASSIFIER_ENABLED:
            v = await _llm_intimacy_verdict(m)
            intimate, how = v, ("haiku=亲密" if v else "haiku=中性")
        else:
            intimate, how = False, "分类器关"
        if intimate:
            st = {"unlocked": True, "neutral_streak": 0}
        else:
            streak = st["neutral_streak"] + 1
            st = {"unlocked": st["unlocked"] and streak <= INTIMACY_STICKY_K, "neutral_streak": streak}
        out.append({"msg": str(m)[:42], "intimate": intimate, "how": how,
                    "unlocked": st["unlocked"], "neutral_streak": st["neutral_streak"],
                    "露骨被收": (_EXPLICIT_REDACT and not st["unlocked"])})
    return {"sticky_K": INTIMACY_STICKY_K, "redact_on": _EXPLICIT_REDACT,
            "unlock_keys": INTIMACY_UNLOCK_KEYS, "steps": out}


@app.post("/api/proactive/dry")
async def api_proactive_dry(request: Request):
    """④ 主动浮现 DRY-RUN(只读)：列候选(feel/dream/高情绪记忆，均非露骨) + 为每条生成一句"开场预览"看自然度。"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    sid = get_active_session_id()
    if not sid:
        return {"error": "无活跃对话线"}
    cands = await pick_proactive_candidates(sid)
    out = []
    for c in cands[:6]:
        op = await generate_opening(c["line"])
        out.append({"source": c["source"], "line": c["line"][:60], "opening": op})
    live_block = ""
    for o in out:
        if o.get("opening"):
            live_block = "〔开场·你心里还惦着，若自然可轻轻提起，别硬塞、别像念稿〕\n" + o["opening"]
            break
    return {"dry_run": True, "session": sid, "model": PROACTIVE_MODEL, "count": len(out),
            "candidates": out, "live_block": live_block}


_rejudge_status = {"running": False, "dry_run": True, "total": 0, "done": 0,
                   "to_true": 0, "to_false": 0, "unchanged": 0, "samples": [],
                   "error": None, "finished_at": None}


@app.post("/api/memories/rejudge-explicit")
async def api_rejudge_explicit(request: Request):
    """堵 is_explicit 漏标洞：对所有高 arousal 记忆用 haiku **语义**重判 is_explicit(认得出'碾过前壁'露骨、
    '风留在根里快哭'温柔)。dry_run=true 只出三桶(新标TRUE/撤标FALSE/不变)+样例不写；后台跑、/status 轮询。"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    if _rejudge_status["running"]:
        return {"error": "重判任务运行中", "status": dict(_rejudge_status)}
    try:
        body = await request.json()
    except Exception:
        body = {}
    dry_run = bool(body.get("dry_run", True))
    true_only = bool(body.get("true_only", False))  # 只写新标TRUE(堵漏)、不撤标(避免误撤露骨)
    threshold = float(body.get("threshold", 0.55))
    batch_size = int(body.get("batch_size", 20))
    _rejudge_status.update({"running": True, "dry_run": dry_run, "total": 0, "done": 0,
                            "to_true": 0, "to_false": 0, "unchanged": 0, "samples": [],
                            "error": None, "finished_at": None})

    async def _run():
        try:
            mems = await get_high_arousal_memories(threshold)
            _rejudge_status["total"] = len(mems)
            for i in range(0, len(mems), batch_size):
                batch = mems[i:i + batch_size]
                verdict = await tag_explicit_batch([{"id": m["id"], "content": m["content"]} for m in batch])
                for m in batch:
                    _rejudge_status["done"] += 1
                    v = verdict.get(m["id"])
                    if v is None:
                        continue  # 判别缺失→不动(safe)
                    cur = m["is_explicit"]
                    samp = {"id": m["id"], "a": round(m["arousal"], 2), "v": round(m["valence"], 2), "c": m["content"][:72]}
                    if v and not cur:
                        _rejudge_status["to_true"] += 1
                        if not dry_run:
                            await set_memory_explicit(m["id"], True)
                        if len([s for s in _rejudge_status["samples"] if s["b"] == "新标TRUE"]) < 30:
                            _rejudge_status["samples"].append({"b": "新标TRUE", **samp})
                    elif (not v) and cur:
                        _rejudge_status["to_false"] += 1
                        if not dry_run and not true_only:
                            await set_memory_explicit(m["id"], False)
                        if len([s for s in _rejudge_status["samples"] if s["b"] == "撤标FALSE"]) < 30:
                            _rejudge_status["samples"].append({"b": "撤标FALSE", **samp})
                    else:
                        _rejudge_status["unchanged"] += 1
                        if (not v) and len([s for s in _rejudge_status["samples"] if s["b"] == "保持FALSE(温柔验)"]) < 12:
                            _rejudge_status["samples"].append({"b": "保持FALSE(温柔验)", **samp})
                await asyncio.sleep(0.2)
            _rejudge_status["finished_at"] = datetime.now(timezone.utc).isoformat()
            print(f"✅ is_explicit 重判{'(dry)' if dry_run else ''}: +TRUE {_rejudge_status['to_true']}/-FALSE {_rejudge_status['to_false']}/不变 {_rejudge_status['unchanged']}")
        except Exception as e:
            _rejudge_status["error"] = str(e)
            print(f"❌ is_explicit 重判异常: {e}")
        finally:
            _rejudge_status["running"] = False

    asyncio.create_task(_run())
    return {"status": "started", "dry_run": dry_run, "threshold": threshold}


@app.get("/api/memories/rejudge-explicit/status")
async def api_rejudge_explicit_status():
    return dict(_rejudge_status)


_feel_rejudge_status = {"running": False, "dry_run": True, "total": 0, "done": 0,
                        "to_true": 0, "to_false": 0, "unchanged": 0, "samples": [],
                        "error": None, "finished_at": None}


@app.post("/api/feels/rejudge-explicit")
async def api_feels_rejudge_explicit(request: Request):
    """修 feel 过标:语义重判所有 feel 的 is_explicit(haiku·非词表)——只标真露骨,温柔/动情放开。
    dry_run=true 只出 flip 名单(撤标FALSE温柔/新标TRUE真露骨/保持TRUE验)不写;后台跑、/status 轮询。审过再 dry_run=false 写入。"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    if _feel_rejudge_status["running"]:
        return {"error": "feel 重判运行中", "status": dict(_feel_rejudge_status)}
    try:
        body = await request.json()
    except Exception:
        body = {}
    dry_run = bool(body.get("dry_run", True))
    batch_size = int(body.get("batch_size", 20))
    _feel_rejudge_status.update({"running": True, "dry_run": dry_run, "total": 0, "done": 0,
                                 "to_true": 0, "to_false": 0, "unchanged": 0, "samples": [],
                                 "error": None, "finished_at": None})

    async def _run():
        try:
            feels = await get_all_feels()
            _feel_rejudge_status["total"] = len(feels)
            for i in range(0, len(feels), batch_size):
                batch = feels[i:i + batch_size]
                verdict = await tag_explicit_batch([{"id": f["id"], "content": f["content"]} for f in batch])
                for f in batch:
                    _feel_rejudge_status["done"] += 1
                    v = verdict.get(f["id"])
                    if v is None:
                        continue  # 判别缺失→不动(safe)
                    cur = f["is_explicit"]
                    samp = {"id": f["id"], "c": f["content"][:80]}
                    if v and not cur:
                        _feel_rejudge_status["to_true"] += 1
                        if not dry_run:
                            await set_feel_explicit(f["id"], True)
                        if len([s for s in _feel_rejudge_status["samples"] if s["b"] == "新标TRUE"]) < 30:
                            _feel_rejudge_status["samples"].append({"b": "新标TRUE", **samp})
                    elif (not v) and cur:
                        _feel_rejudge_status["to_false"] += 1
                        if not dry_run:
                            await set_feel_explicit(f["id"], False)
                        if len([s for s in _feel_rejudge_status["samples"] if s["b"] == "撤标FALSE(温柔放开)"]) < 40:
                            _feel_rejudge_status["samples"].append({"b": "撤标FALSE(温柔放开)", **samp})
                    else:
                        _feel_rejudge_status["unchanged"] += 1
                        if cur and len([s for s in _feel_rejudge_status["samples"] if s["b"] == "保持TRUE(真露骨验)"]) < 20:
                            _feel_rejudge_status["samples"].append({"b": "保持TRUE(真露骨验)", **samp})
                await asyncio.sleep(0.2)
            _feel_rejudge_status["finished_at"] = datetime.now(timezone.utc).isoformat()
            print(f"✅ feel is_explicit 重判{'(dry)' if dry_run else ''}: -FALSE {_feel_rejudge_status['to_false']}/+TRUE {_feel_rejudge_status['to_true']}/不变 {_feel_rejudge_status['unchanged']}")
        except Exception as e:
            _feel_rejudge_status["error"] = str(e)
            print(f"❌ feel 重判异常: {e}")
        finally:
            _feel_rejudge_status["running"] = False

    asyncio.create_task(_run())
    return {"status": "started", "dry_run": dry_run}


@app.get("/api/feels/rejudge-explicit/status")
async def api_feels_rejudge_explicit_status():
    return dict(_feel_rejudge_status)


# ---- ②衰减归档：dry(只读看会淡掉谁) + 状态 + toggle(定阈值/开关) + run(mutate,gated) + undo(可逆兜底) ----

def _decay_thresholds(body=None) -> dict:
    """取阈值:body 给了就覆盖(阮阮试不同值),否则用当前全局(占位/已定的)。"""
    b = body or {}
    def _i(k, d):
        try:
            v = b.get(k)
            return int(v) if (v is not None and str(v) != "") else d
        except Exception:
            return d
    def _f(k, d):
        try:
            v = b.get(k)
            return float(v) if (v is not None and str(v) != "") else d
        except Exception:
            return d
    return {"age_days": _i("age_days", DECAY_AGE_DAYS), "imp_max": _i("imp_max", DECAY_IMP_MAX),
            "idle_days": _i("idle_days", DECAY_IDLE_DAYS), "arousal_max": _f("arousal_max", DECAY_AROUSAL_MAX)}


@app.get("/api/memories/decay")
async def api_decay_status():
    return {"decay_enabled": DECAY_ENABLED, "age_days": DECAY_AGE_DAYS, "imp_max": DECAY_IMP_MAX,
            "idle_days": DECAY_IDLE_DAYS, "arousal_max": DECAY_AROUSAL_MAX,
            "note": "归档=mutate,默认关;高imp/高arousal/近期/被回忆过/回忆墙受保护;归档=is_active FALSE+decayed_at标,可逆(undo-last);已从 cleanup_old_fragments 30天硬删豁免(归档≠删除,记忆不能丢)"}


@app.post("/api/memories/decay-dry")
async def api_decay_dry(request: Request):
    """②衰减 dry(只读):按阈值列出"会被归档淡化"的记忆,不动任何数据。
    body 可覆盖阈值:{age_days, imp_max, idle_days, arousal_max}。给阮阮审/调阈值用。"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    try:
        body = await request.json()
    except Exception:
        body = {}
    th = _decay_thresholds(body)
    try:
        total = await count_active_memories()
        cands = await get_decay_candidates(th["age_days"], th["imp_max"], th["idle_days"], th["arousal_max"], limit=1000)
        items = [{"id": c["id"], "head": (c["content"][:60] + ("…" if len(c["content"]) > 60 else "")),
                  "importance": c["importance"], "layer": c["layer"], "arousal": round(c["arousal"], 2),
                  "age_days": c["age_days"], "idle_days": c["idle_days"], "is_explicit": c["is_explicit"]}
                 for c in cands]
        n = len(items)
        return {"thresholds": th, "active_total": total, "would_archive": n,
                "pct": (round(100.0 * n / total, 1) if total else 0.0),
                "criteria": f"老>={th['age_days']}天 且 importance<={th['imp_max']} 且 久未取>={th['idle_days']}天 且 arousal<{th['arousal_max']}",
                "protected": "高imp/高arousal/近期/被回忆过/回忆墙 均不在此列",
                "candidates": items}
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/memories/decay/toggle")
async def api_decay_toggle(request: Request):
    """开关 + 设阈值(持久化 gateway_config、启动恢复)。body:{enabled, age_days, imp_max, idle_days, arousal_max}。
    阮阮定的阈值在这里落库;enabled=true 才允许 run 真归档。"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    try:
        body = await request.json()
    except Exception:
        body = {}
    th = _decay_thresholds(body)
    globals()["DECAY_AGE_DAYS"] = th["age_days"]; await set_gateway_config("decay_age_days", str(th["age_days"]))
    globals()["DECAY_IMP_MAX"] = th["imp_max"]; await set_gateway_config("decay_imp_max", str(th["imp_max"]))
    globals()["DECAY_IDLE_DAYS"] = th["idle_days"]; await set_gateway_config("decay_idle_days", str(th["idle_days"]))
    globals()["DECAY_AROUSAL_MAX"] = th["arousal_max"]; await set_gateway_config("decay_arousal_max", str(th["arousal_max"]))
    if "enabled" in body:
        val = bool(body.get("enabled"))
        globals()["DECAY_ENABLED"] = val
        await set_gateway_config("decay_enabled", "true" if val else "false")
    return {"status": "ok", "decay_enabled": DECAY_ENABLED, "age_days": DECAY_AGE_DAYS,
            "imp_max": DECAY_IMP_MAX, "idle_days": DECAY_IDLE_DAYS, "arousal_max": DECAY_AROUSAL_MAX}


@app.post("/api/memories/decay/run")
async def api_decay_run(request: Request):
    """跑一次衰减归档。body:{dry_run:true}(默认 true=只看不动)。
    dry_run=false:必须 DECAY_ENABLED=true(gated),才把候选 deactivate(is_active=FALSE,可逆)。
    归档的 id 存 gateway_config[decay_last_batch],可 /decay/undo-last 复活。"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    try:
        body = await request.json()
    except Exception:
        body = {}
    dry = bool(body.get("dry_run", True))
    th = _decay_thresholds(body)
    try:
        cands = await get_decay_candidates(th["age_days"], th["imp_max"], th["idle_days"], th["arousal_max"], limit=2000)
        ids = [c["id"] for c in cands]
        if dry:
            return {"dry_run": True, "would_archive": len(ids), "thresholds": th, "ids": ids[:200]}
        if not DECAY_ENABLED:
            return {"error": "DECAY_ENABLED=false,真归档被闸住。先 /api/memories/decay/toggle {enabled:true} 由阮阮定阈值开启。",
                    "would_archive": len(ids)}
        if not ids:
            return {"dry_run": False, "archived": 0, "note": "无候选"}
        await archive_decayed_memories(ids)  # 打 decayed_at 标 → cleanup_old_fragments 豁免(归档≠删除)
        await set_gateway_config("decay_last_batch", json.dumps(ids))
        _decay_run["archived"] = len(ids); _decay_run["finished_at"] = datetime.now(timezone.utc).isoformat()
        return {"dry_run": False, "archived": len(ids), "thresholds": th, "ids": ids,
                "undo": "如需复活:POST /api/memories/decay/undo-last"}
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/memories/decay/undo-last")
async def api_decay_undo():
    """把最近一次衰减归档的那批 id 复活(is_active=TRUE)。可逆性兜底。"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    try:
        raw = await get_gateway_config("decay_last_batch", "")
        ids = json.loads(raw) if raw else []
        if not ids:
            return {"status": "ok", "reactivated": 0, "note": "无可复活批次"}
        ok = await reactivate_decayed_memories([int(m) for m in ids])  # is_active=TRUE 且清 decayed_at
        await set_gateway_config("decay_last_batch", "")
        return {"status": "ok", "reactivated": ok}
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/dreams")
async def api_dreams_list():
    """③-2 面板日记页：列出所有梦(按日期倒序)。"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    try:
        ds = await list_dreams(limit=120)
        for d in ds:
            d["dream_date"] = str(d.get("dream_date"))
            d["created_at"] = str(d.get("created_at") or "")
        return {"dreams": ds, "count": len(ds)}
    except Exception as e:
        return {"error": str(e)}


async def archive_line(session_id: str) -> dict:
    """归档一条线(给"归档RP"用)：把它当前对话压成总结写进**全局记忆库**(可被任何线召回) →
    对话整体挪到归档线(可逆软归档，不再占 token) → 重置该线缓存(重新垫上主线当前摘要当背景)。
    净效果：RP 8000字原文"阅后即焚"不再耗 token，但精华永久留在记忆库，V 永远记得玩过啥。"""
    if not session_id:
        return {"error": "no session"}
    if PARTITION_SESSION_ID and session_id == PARTITION_SESSION_ID:
        return {"error": "不能归档主线，只能归档 RP 等子线"}
    rows = await get_conversation_messages(session_id, limit=10000)
    if not rows:
        return {"status": "empty", "moved": 0, "note": "这条线没有对话可归档"}
    # 1) 压成总结(一次 Haiku；force_quality 走保质感 prompt，亲密细节会被抽象成中性指代)
    msgs = [{"role": r.get("role"), "content": (r.get("content") or "")}
            for r in rows if (r.get("content") or "").strip()]
    summary = await generate_summary(msgs, session_id=session_id, force_quality=True)
    # 2) 总结写进全局记忆库(带"这是RP回顾"标记，可召回；importance 适中，不跟回忆墙抢权重)
    if summary:
        _today = (datetime.now(timezone.utc) + timedelta(hours=TIMEZONE_HOURS)).strftime("%Y-%m-%d")
        tagged = f"【这是 {_today} 一段亲密/RP 互动的回顾(不是日常事实记录)】\n\n{summary}"
        try:
            await save_memory(tagged, importance=6, source_session=session_id, valence=0.3, arousal=0.4)
        except Exception as _me:
            print(f"⚠️ 归档总结写入记忆库失败: {_me}")
    # 3) 对话挪到归档线(可逆)
    arch = await archive_line_conversations(session_id)
    # 4) 重置该线缓存：重新垫上主线当前摘要(让下次该线开局仍有主线近况背景)，a_start=0
    try:
        main_state = await get_session_cache_state(PARTITION_SESSION_ID) if PARTITION_SESSION_ID else {}
        await save_session_cache_state(session_id, main_state.get("summary_parts") or [], 0,
                                       early_summary=main_state.get("early_summary") or "")
    except Exception as _ce:
        print(f"⚠️ 归档后重置缓存失败: {_ce}")
    print(f"🗂️ 归档线 {session_id}：总结{len(summary)}字入记忆库，{arch['moved']}条原文挪到 {arch['archive_session_id']}")
    return {"status": "ok", "summarized_chars": len(summary),
            "moved": arch["moved"], "archive_session_id": arch["archive_session_id"]}


@app.post("/api/line/archive")
async def api_line_archive(request: Request):
    """归档一条线(默认当前活跃线)：总结入记忆库 + 原文软归档 + 重置缓存。body: {session_id?}"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    try:
        body = await request.json()
    except Exception:
        body = {}
    sid = (body.get("session_id") or get_active_session_id() or "").strip()
    if not sid:
        return {"error": "无对话线"}
    return await archive_line(sid)


_dream_run = {"running": False, "dry_run": True, "results": [], "error": None, "finished_at": None}


@app.post("/api/dreams/run")
async def api_dreams_run(request: Request):
    """③-2 手动跑做梦补做（后台跑+轮询，避开代理掐长连接）。body: {dry_run, dates}。
    dry_run=true 只生成不写库(结果含完整日记，存 /status 拿)；dates 不给则自动找未覆盖过去日。"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    if _dream_run["running"]:
        return {"error": "做梦任务运行中", "status": dict(_dream_run)}
    try:
        body = await request.json()
    except Exception:
        body = {}
    sid = get_active_session_id()
    if not sid:
        return {"error": "无活跃对话线"}
    dry_run = bool(body.get("dry_run", True))
    dates = body.get("dates") or None
    _dream_run.update({"running": True, "dry_run": dry_run, "results": [], "error": None, "finished_at": None})

    async def _run():
        try:
            _dream_run["results"] = await maybe_run_dreams(sid, dry_run=dry_run, only_dates=dates)
        except Exception as e:
            _dream_run["error"] = str(e)
        finally:
            _dream_run["running"] = False
            _dream_run["finished_at"] = datetime.now(timezone.utc).isoformat()

    asyncio.create_task(_run())
    return {"status": "started", "dry_run": dry_run, "active_session": sid}


@app.get("/api/dreams/run/status")
async def api_dreams_run_status():
    return dict(_dream_run)




# ============================================================
# 三层记忆架构：整理 / 合并 / 升级 / 统计
# ============================================================

CONSOLIDATION_PROMPT = """
你是记忆整理助手。请将以下对话碎片整理成完整的事件记录。

要求：
1. 按主题/事件分组，相关的碎片合并到一起。**必须大胆合并**：同一场互动、同一次聊天、同一个话题在几小时内的所有碎片，属于同一个事件，必须合成一条——哪怕单条碎片已经写得很完整。碎片开头的时间就是用来判断'是不是连着发生的'的依据。
2. 整理的目的是把同期发生的事焊在一起，方便日后一起被想起。所以事件数量必须明显少于碎片数量（一般一天最多 2~4 个事件）。'由1条碎片单独成一个事件'只允许出现在该碎片与当天其他所有内容都毫无关系时。
3. 每条记录包含：标题（10字内）+ 完整描述。描述按发生顺序把该事件所有碎片的内容都写进去，可以长，不许丢事实。
4. 合并重复内容，保留重要细节
5. 保留原文中的主观感受、情绪表达和个人化用语，不要改写为客观陈述或第三方总结
6. content字段中不要使用双引号，用单引号或书名号代替

碎片记忆：
{fragments}

请用 JSON 格式输出：
[
  {{
    "title": "事件标题（10字内）",
    "content": "完整的事件描述",
    "importance": 5,
    "merged_ids": [1, 2, 3]
  }}
]

只输出 JSON，不要其他内容。确保 JSON 语法正确。
"""

# 整理状态（异步执行，防重入）
_consolidate_status = {
    "running": False,
    "started_at": None,
    "result": None,
    "error": None,
}


async def consolidate_memories_for_date(event_date):
    """整理指定日期的碎片记忆"""
    return await consolidate_memories_for_date_range(event_date, event_date)


async def consolidate_memories_for_date_range(start_date, end_date):
    """整理指定时间段的碎片记忆"""
    fragments = await get_fragments_by_date_range(start_date, end_date)

    if not fragments:
        return {"status": "no_fragments", "start_date": str(start_date), "end_date": str(end_date)}

    result = await _consolidate_fragment_batch(fragments, start_date)
    result["start_date"] = str(start_date)
    result["end_date"] = str(end_date)
    return result


async def _consolidate_fragment_batch(fragments, event_date):
    """整理一批碎片的核心：AI 按事件分组合并 → 写 layer2 事件记忆 → 停用碎片"""
    import re

    # 构建碎片文本
    def _frag_ts(ts):
        if hasattr(ts, "strftime"):
            if getattr(ts, "tzinfo", None) is None:
                ts = ts.replace(tzinfo=timezone.utc)
            return ts.astimezone(timezone(timedelta(hours=TIMEZONE_HOURS))).strftime("%m-%d %H:%M")
        return str(ts)[:16]

    fragments_text = "\n".join([
        f"[ID={f['id']}] ({_frag_ts(f['created_at'])}) {f['content']}"
        for f in fragments
    ])
    
    # 调用 AI 进行整理
    prompt = CONSOLIDATION_PROMPT.format(fragments=fragments_text)
    
    # 使用环境变量配置的模型，默认 haiku 节省成本
    consolidation_model = os.getenv("MEMORY_MODEL", "") or os.getenv("DEFAULT_MODEL", "anthropic/claude-haiku-4.5")
    
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            # 最多重试2次（应对429限流）
            last_error = None
            for attempt in range(3):
                response = await client.post(
                    API_BASE_URL,
                    headers={
                        "Authorization": f"Bearer {get_memory_api_key()}",
                        "Content-Type": "application/json"
                    },
                    json={
                        "model": consolidation_model,
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": 6000
                    }
                )

                if response.status_code == 429:
                    wait_time = (attempt + 1) * 10
                    print(f"⚠️ 整理API 429限流，{wait_time}秒后重试（第{attempt+1}次）")
                    last_error = f"429 Too Many Requests (重试{attempt+1}次)"
                    await asyncio.sleep(wait_time)
                    continue

                if response.status_code != 200:
                    last_error = f"HTTP {response.status_code}: {response.text[:200]}"
                    print(f"⚠️ 整理API返回 {response.status_code}: {response.text[:200]}")
                    break

                last_error = None
                break

            if last_error:
                return {"status": "error", "error": f"API调用失败: {last_error}"}

            data = response.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            
            # 解析 JSON（三层容错）
            json_match = re.search(r'\[[\s\S]*\]', content)
            if json_match:
                json_str = json_match.group()
                try:
                    events = json.loads(json_str)
                except json.JSONDecodeError:
                    # 方案1：用 strict=False
                    try:
                        events = json.loads(json_str, strict=False)
                    except json.JSONDecodeError:
                        # 方案2：去掉控制字符后重试
                        cleaned = re.sub(r'[\x00-\x1f\x7f]', ' ', json_str)
                        try:
                            events = json.loads(cleaned)
                        except json.JSONDecodeError as e:
                            # 方案3：让 AI 重新格式化
                            print(f"⚠️ JSON解析失败，尝试让AI修复: {e}")
                            fix_resp = await client.post(
                                API_BASE_URL,
                                headers={
                                    "Authorization": f"Bearer {get_memory_api_key()}",
                                    "Content-Type": "application/json"
                                },
                                json={
                                    "model": consolidation_model,
                                    "messages": [{"role": "user", "content": f"请修复以下JSON的语法错误，只输出修复后的JSON数组，不要其他内容：\n{json_str[:2000]}"}],
                                    "max_tokens": 2000
                                }
                            )
                            if fix_resp.status_code == 200:
                                fix_content = fix_resp.json().get("choices", [{}])[0].get("message", {}).get("content", "")
                                fix_match = re.search(r'\[[\s\S]*\]', fix_content)
                                if fix_match:
                                    try:
                                        events = json.loads(fix_match.group())
                                        print(f"✅ AI修复JSON成功")
                                    except json.JSONDecodeError:
                                        return {"status": "error", "error": f"JSON解析失败（AI修复也失败）", "raw": content[:500]}
                                else:
                                    return {"status": "error", "error": "AI修复未返回有效JSON", "raw": content[:500]}
                            else:
                                return {"status": "error", "error": f"JSON解析失败，AI修复请求失败: HTTP {fix_resp.status_code}", "raw": content[:500]}
            else:
                return {"status": "error", "error": "无法解析 AI 返回的 JSON", "raw": content}
            
            # 创建事件记忆并停用碎片
            created_count = 0
            for event in events:
                merged_ids = event.get("merged_ids", [])
                if merged_ids:
                    await create_event_memory(
                        title=event.get("title", ""),
                        content=event.get("content", ""),
                        importance=event.get("importance", 5),
                        event_date=event_date,
                        merged_from=merged_ids
                    )
                    created_count += 1
            
            # 停用所有已处理的碎片
            all_fragment_ids = [f['id'] for f in fragments]
            await deactivate_memories(all_fragment_ids)
            
            return {
                "status": "ok",
                "fragments_processed": len(fragments),
                "events_created": created_count
            }
            
    except Exception as e:
        return {"status": "error", "error": str(e)}


@app.post("/api/memories/consolidate")
async def api_manual_consolidate(request: Request):
    """手动触发整理（异步，立即返回）
    
    Body:
        start_date: 开始日期（YYYY-MM-DD 格式）
        end_date: 结束日期（YYYY-MM-DD 格式）
        或
        date: 单个日期（兼容旧版）
    """
    from datetime import date as date_type
    
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    
    if _consolidate_status.get("running"):
        return {"status": "already_running", "started_at": _consolidate_status.get("started_at")}
    
    data = await request.json()
    
    # 解析日期参数
    if "date" in data and "start_date" not in data:
        start_date = datetime.strptime(data["date"], "%Y-%m-%d").date()
        end_date = start_date
    else:
        start_date_str = data.get("start_date")
        end_date_str = data.get("end_date")
        
        if not start_date_str or not end_date_str:
            return {"error": "请提供开始和结束日期"}
        
        start_date = datetime.strptime(start_date_str, "%Y-%m-%d").date()
        end_date = datetime.strptime(end_date_str, "%Y-%m-%d").date()
        
        if start_date > end_date:
            return {"error": "开始日期不能晚于结束日期"}
    
    async def _run():
        _consolidate_status.update({"running": True, "started_at": f"{start_date}~{end_date}", "result": None, "error": None})
        try:
            result = await consolidate_memories_for_date_range(start_date, end_date)
            _consolidate_status["result"] = result
            print(f"[manual/consolidate] 整理 {start_date}~{end_date}: {result}")
        except Exception as e:
            _consolidate_status["error"] = str(e)
            print(f"[manual/consolidate] 整理 {start_date}~{end_date} 失败: {e}")
        finally:
            _consolidate_status["running"] = False
    
    asyncio.create_task(_run())
    return {"status": "started", "start_date": str(start_date), "end_date": str(end_date)}


@app.get("/api/memories/consolidate/status")
async def api_consolidate_status():
    """查询整理任务状态"""
    return _consolidate_status


# ===== 凌晨自动整理（按「逻辑日」，GitHub Actions 定时触发）=====
# 逻辑日 = 当天 boundary 点 ~ 次日 boundary 点（北京时间，默认凌晨4点）。
# 跨零点的连续对话（23:00 聊到 01:30）落在同一个逻辑日里，不会被日历日切成两半。
AUTO_CONSOLIDATE_LOOKBACK_DAYS = int(os.getenv("AUTO_CONSOLIDATE_LOOKBACK_DAYS", "3"))
AUTO_CONSOLIDATE_BOUNDARY_HOUR = int(os.getenv("AUTO_CONSOLIDATE_BOUNDARY_HOUR", "4"))


async def auto_consolidate_recent(lookback_days=None, dry_run=False):
    """整理最近 lookback_days 个已结束的逻辑日的碎片。

    - 只看最近几天：漏跑的中间天会自动补上，但更早的积压
      （比如迁移进来的成批老碎片）不碰，留给手动整理控制节奏。
    - 已整理过的天没有活跃碎片，自动跳过，重复触发无副作用。
    """
    if lookback_days is None:
        lookback_days = AUTO_CONSOLIDATE_LOOKBACK_DAYS
    lookback_days = max(1, min(int(lookback_days), 14))
    boundary = AUTO_CONSOLIDATE_BOUNDARY_HOUR
    local_tz = timezone(timedelta(hours=TIMEZONE_HOURS))
    now_local = datetime.now(timezone.utc).astimezone(local_tz)
    # 最近一个已结束的逻辑日：过了今天 boundary 点，昨天的逻辑日才算结束
    anchor = now_local.date() if now_local.hour >= boundary else now_local.date() - timedelta(days=1)
    days = [anchor - timedelta(days=i) for i in range(lookback_days, 0, -1)]  # 从旧到新

    report = []
    for d in days:
        start_local = datetime(d.year, d.month, d.day, boundary, tzinfo=local_tz)
        end_local = start_local + timedelta(days=1)
        fragments = await get_fragments_by_time_window(
            start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc))
        if not fragments:
            continue
        if dry_run:
            report.append({"day": str(d), "fragments": len(fragments), "dry_run": True})
            continue
        result = await _consolidate_fragment_batch(fragments, d)
        result["day"] = str(d)
        report.append(result)
        print(f"[auto/consolidate] 逻辑日 {d}: {result}")

    summary = {
        "status": "ok",
        "checked_days": [str(d) for d in days],
        "processed": report,
        "ran_at": now_local.strftime("%Y-%m-%d %H:%M"),
        "dry_run": dry_run,
    }
    if not dry_run:
        try:
            await set_gateway_config("auto_consolidate_last", json.dumps(summary, ensure_ascii=False))
        except Exception:
            pass
    return summary


@app.post("/api/memories/consolidate/auto")
async def api_auto_consolidate(request: Request):
    """凌晨自动整理入口。body 可选: {"dry_run": true, "lookback_days": 3}
    dry_run 同步返回将要处理的天和碎片数；正式跑异步执行，结果查 /api/memories/consolidate/status
    """
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    try:
        data = await request.json()
    except Exception:
        data = {}
    lookback = data.get("lookback_days")

    if data.get("dry_run"):
        return await auto_consolidate_recent(lookback, dry_run=True)

    if _consolidate_status.get("running"):
        return {"status": "already_running", "started_at": _consolidate_status.get("started_at")}

    async def _run():
        _consolidate_status.update({"running": True, "started_at": "auto", "result": None, "error": None})
        try:
            result = await auto_consolidate_recent(lookback)
            _consolidate_status["result"] = result
            print(f"[auto/consolidate] 完成: {result}")
        except Exception as e:
            _consolidate_status["error"] = str(e)
            print(f"[auto/consolidate] 失败: {e}")
        finally:
            _consolidate_status["running"] = False

    asyncio.create_task(_run())
    return {"status": "started", "mode": "auto"}


@app.post("/api/migrate/memory-wall")
async def api_migrate_memory_wall(request: Request):
    """一次性迁移回忆墙：服务端从回忆墙拉取全部回忆，作为完整记忆单元写入网关。
    服务端拉取（含照片二进制），避免大包经客户端代理。幂等：按原始ID跳过已迁移。
    body: {source_url, password, dry_run, summary_threshold}
    """
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    try:
        body = await request.json()
    except Exception:
        body = {}
    source = (body.get("source_url") or os.getenv("MEMORYWALL_SOURCE_URL", "http://localhost:3000")).rstrip("/")
    password = body.get("password") or ""
    dry_run = bool(body.get("dry_run", False))
    summary_threshold = int(body.get("summary_threshold", 400))
    author_cn_map = {"ruanruan": USER_NAME, "xiaoke": (AI_NAME or "AI")}

    # 1) 服务端拉取回忆墙全部条目
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(f"{source}/api/memories?limit=500", headers={"x-password": password})
        if r.status_code != 200:
            return {"status": "error", "step": "fetch_entries", "http": r.status_code, "body": r.text[:300]}
        entries = r.json()
    except Exception as e:
        return {"status": "error", "step": "fetch_entries", "error": str(e)}
    if not isinstance(entries, list):
        return {"status": "error", "step": "fetch_entries", "error": "返回不是数组", "raw": str(entries)[:300]}

    out = {"status": "ok", "dry_run": dry_run, "source": source, "source_count": len(entries),
           "migrated": 0, "skipped_existing": 0, "photos_stored": 0, "errors": [], "items": []}

    async def _fetch_store_photos(src_photos, memory_id):
        """拉取并存照片，直接关联到 memory_id（绝不留 NULL，避免被误删），返回引用列表"""
        refs = []
        for p in src_photos:
            fp = p.get("file_path") or ""
            if not fp:
                continue
            purl = fp if fp.startswith("http") else f"{source}{fp}"
            try:
                async with httpx.AsyncClient(timeout=60) as pc:
                    pr = await pc.get(purl)
                if pr.status_code == 200 and pr.content:
                    mime = (pr.headers.get("content-type") or "image/png").split(";")[0].strip()
                    pid = await save_photo(memory_id, p.get("original_name"), mime, pr.content)
                    refs.append({"photo_id": pid, "original_name": p.get("original_name"),
                                 "mime": mime, "url": f"/api/photos/{pid}", "bytes": len(pr.content)})
                    out["photos_stored"] += 1
                else:
                    out["errors"].append(f"photo {purl} HTTP {pr.status_code}")
            except Exception as pe:
                out["errors"].append(f"photo {purl}: {pe}")
        return refs

    for e in entries:
        try:
            oid = str(e.get("id"))
            src_photos = e.get("photos") or []
            existing = await find_memory_by_mw_id(oid)

            if dry_run:
                bt = (e.get("content") or "").strip()
                out["items"].append({"original_id": oid,
                                     "status": "skipped_existing" if existing else "would_migrate",
                                     "title": (e.get("title") or "").strip(),
                                     "will_summarize": len(bt) > summary_threshold,
                                     "photos": len(src_photos)})
                if existing:
                    out["skipped_existing"] += 1
                continue

            if existing:
                mid = existing
                out["skipped_existing"] += 1
                # 自愈：记忆已存在但照片缺失（被旧bug误删）→ 清掉残留并重抓补齐
                if src_photos and await memory_photo_count(mid) < len(src_photos):
                    await delete_memory_photos(mid)
                    refs = await _fetch_store_photos(src_photos, mid)
                    meta = await get_mw_meta(mid) or {}
                    meta["photos"] = refs
                    await update_mw_meta(mid, meta)
                out["items"].append({"original_id": oid, "status": "skipped_existing",
                                     "memory_id": mid, "photos": await memory_photo_count(mid)})
                continue

            # ---- 新记忆 ----
            title = (e.get("title") or "").strip()
            body_text = (e.get("content") or "").strip()
            mood = e.get("mood")
            author = e.get("author")
            src = e.get("source")
            is_period = 1 if e.get("is_period_day") else 0
            created_at = e.get("created_at")
            location = e.get("location")
            author_cn = author_cn_map.get(author, author or "")

            summary = ""
            if len(body_text) > summary_threshold:
                try:
                    summary = await generate_summary([{"role": "user", "content": f"{title}\n{body_text}"}])
                except Exception as se:
                    out["errors"].append(f"summary {oid}: {se}")

            header = f"【回忆 · {str(created_at)[:10]} · {author_cn}" + (f" · {mood}" if mood else "") + "】"
            parts = [header + (title or "")]
            if summary:
                parts.append(f"〔检索摘要〕{summary}")
            if body_text:
                parts.append(body_text)
            content = "\n\n".join([p for p in parts if p and p.strip()])
            importance = 9 if mood == "纪念" else 8
            mw_meta = {"original_id": oid, "date": created_at, "author": author, "author_cn": author_cn,
                       "mood": mood, "source": src, "is_period_day": is_period, "location": location,
                       "title": title, "photos": []}

            # 先插记忆，再抓照片直接关联到 mid（掉线也不会留下游离照片）
            mid = await save_migrated_memory(content=content, importance=importance, title=title,
                                             event_date=created_at, created_at=created_at, mw_meta=mw_meta)
            refs = await _fetch_store_photos(src_photos, mid)
            if refs:
                mw_meta["photos"] = refs
                await update_mw_meta(mid, mw_meta)
            out["migrated"] += 1
            out["items"].append({"original_id": oid, "memory_id": mid, "status": "migrated",
                                 "title": title, "photos": len(refs)})
        except Exception as ie:
            out["errors"].append(f"entry {e.get('id')}: {ie}")

    return out


@app.get("/api/photos/{photo_id}")
async def api_get_photo(photo_id: int):
    """读取迁移照片二进制（受 gateway_key 保护，img src 可带 ?gateway_key=）"""
    row = await get_photo(photo_id)
    if not row:
        return JSONResponse(status_code=404, content={"error": "photo not found"})
    return Response(content=bytes(row["data"]), media_type=row.get("mime") or "image/png")


@app.get("/api/imagegen/status")
async def api_imagegen_status():
    """画图(/画)当前配置——给操作间 DRAW 面板。key 只报"设没设",真值绝不回传。"""
    eff_base = (IMAGE_GEN_BASE_URL or getattr(_db_module, "EMBEDDING_BASE_URL", "") or "").rstrip("/")
    own_key = bool(IMAGE_GEN_API_KEY)
    return {
        "enabled": IMAGE_GEN_ENABLED,
        "model": IMAGE_GEN_MODEL,
        "size": IMAGE_GEN_SIZE,
        "base_url": eff_base,                        # 实际生效的地址(可能是复用向量检索那套)
        "own_base": bool(IMAGE_GEN_BASE_URL),        # 是否单独设了画图地址
        "own_key": own_key,                          # 是否单独设了画图 key
        "key_set": own_key or bool(getattr(_db_module, "EMBEDDING_API_KEY", "")),
        "last_error": _imagegen_last_error,          # 最近一次存图失败的真实报错(成功后自动清空)
    }


# ============================================================
# 回忆墙视图 CRUD —— API-first：dashboard 与未来的 MCP 出口共用这组端点
# “回忆” = memories 里 mw_meta 非空的记忆（迁入的 + dashboard 新建的），同库不同视图，
# 因此它们也能被检索/注入给 AI（守铁律：界面可见=AI可感知）。
# ============================================================

MW_AUTHOR_CN = {"ruanruan": USER_NAME, "xiaoke": (AI_NAME or "AI")}
MW_SUMMARY_THRESHOLD = 400


def _compose_mw_content(title, body, author, mood, created_at, summary):
    author_cn = MW_AUTHOR_CN.get(author, author or "")
    header = f"【回忆 · {str(created_at)[:10]} · {author_cn}" + (f" · {mood}" if mood else "") + "】"
    parts = [header + (title or "")]
    if summary:
        parts.append(f"〔检索摘要〕{summary}")
    if body:
        parts.append(body)
    return "\n\n".join([p for p in parts if p and p.strip()])


def _extract_mw_body(content):
    parts = (content or "").split("\n\n")
    rest = parts[1:]
    if rest and rest[0].startswith("〔检索摘要〕"):
        rest = rest[1:]
    return "\n\n".join(rest)


def _mw_item(row):
    mm = row.get("mw_meta") or {}
    return {
        "id": row["id"],
        "title": row.get("title") or mm.get("title") or "",
        "body": mm.get("body") or _extract_mw_body(row.get("content") or ""),
        "author": mm.get("author"),
        "author_cn": mm.get("author_cn") or MW_AUTHOR_CN.get(mm.get("author"), mm.get("author")),
        "mood": mm.get("mood"),
        "source": mm.get("source"),
        "is_period_day": mm.get("is_period_day"),
        "location": mm.get("location"),
        "date": mm.get("date") or (str(row.get("created_at")) if row.get("created_at") else None),
        "event_date": str(row["event_date"]) if row.get("event_date") else None,
        "importance": row.get("importance"),
        "is_active": row.get("is_active"),
        "pinned": bool(mm.get("pinned")),
        "photos": row.get("photos", []),
    }


@app.get("/api/memorywall")
async def api_mw_list(author: str = "", mood: str = "", include_inactive: bool = False):
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    rows = await list_memorywall(author or None, mood or None, include_inactive)
    return {"items": [_mw_item(r) for r in rows], "total": len(rows)}


@app.post("/api/memorywall")
async def api_mw_create(request: Request):
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    try:
        b = await request.json()
        title = (b.get("title") or "").strip()
        body = (b.get("body") or b.get("content") or "").strip()
        if not title and not body:
            return JSONResponse(status_code=400, content={"error": "标题和正文不能都为空"})
        author = b.get("author") or None
        mood = b.get("mood") or None
        source = b.get("source") or "manual"
        is_period = 1 if b.get("is_period_day") else 0
        location = b.get("location") or None
        created_at = b.get("date") or datetime.now(timezone.utc).isoformat()
        summary = ""
        if len(body) > MW_SUMMARY_THRESHOLD:
            try:
                summary = await generate_summary([{"role": "user", "content": f"{title}\n{body}"}])
            except Exception as se:
                print(f"⚠️ 回忆摘要生成失败: {se}")
        content = _compose_mw_content(title, body, author, mood, created_at, summary)
        importance = 9 if mood == "纪念" else 8
        mw_meta = {"original_id": f"dash-{int(datetime.now().timestamp()*1000)}",
                   "date": created_at, "author": author,
                   "author_cn": MW_AUTHOR_CN.get(author, author or ""),
                   "mood": mood, "source": source, "is_period_day": is_period,
                   "location": location, "title": title, "body": body,
                   "summary": summary, "pinned": bool(b.get("pinned")), "photos": []}
        mid = await save_migrated_memory(content=content, importance=importance, title=title,
                                         event_date=created_at, created_at=created_at, mw_meta=mw_meta)
        one = await get_memorywall_one(mid)
        return {"status": "ok", "item": _mw_item(one) if one else {"id": mid}}
    except Exception as ex:
        # 优雅兜底：例如客户端发了非 UTF-8 的请求体（request.json() 解码失败）等
        print(f"⚠️ 回忆创建失败: {ex}")
        return JSONResponse(status_code=400, content={"error": f"创建失败：{ex}"})


@app.put("/api/memorywall/{mid}")
async def api_mw_update(mid: int, request: Request):
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    existing = await get_memorywall_one(mid)
    if not existing:
        return JSONResponse(status_code=404, content={"error": "回忆不存在"})
    mm = existing.get("mw_meta") or {}
    b = await request.json()
    title = (b["title"] if b.get("title") is not None else (mm.get("title") or existing.get("title") or "")).strip()
    body = (b["body"] if b.get("body") is not None else (mm.get("body") or _extract_mw_body(existing.get("content")))).strip()
    author = b.get("author") if "author" in b else mm.get("author")
    mood = b.get("mood") if "mood" in b else mm.get("mood")
    source = b.get("source") if "source" in b else mm.get("source")
    is_period = (1 if b.get("is_period_day") else 0) if "is_period_day" in b else mm.get("is_period_day", 0)
    location = b.get("location") if "location" in b else mm.get("location")
    pinned = bool(b.get("pinned")) if "pinned" in b else bool(mm.get("pinned"))
    created_at = b.get("date") or mm.get("date") or (str(existing.get("created_at")) if existing.get("created_at") else datetime.now(timezone.utc).isoformat())
    summary = ""
    if len(body) > MW_SUMMARY_THRESHOLD:
        try:
            summary = await generate_summary([{"role": "user", "content": f"{title}\n{body}"}])
        except Exception as se:
            print(f"⚠️ 回忆摘要更新失败: {se}")
    content = _compose_mw_content(title, body, author, mood, created_at, summary)
    importance = 9 if mood == "纪念" else 8
    new_meta = dict(mm)
    new_meta.update({"date": created_at, "author": author, "author_cn": MW_AUTHOR_CN.get(author, author or ""),
                     "mood": mood, "source": source, "is_period_day": is_period, "location": location,
                     "title": title, "body": body, "summary": summary, "pinned": pinned})
    await update_memorywall(mid, content, title, importance, created_at, new_meta)
    one = await get_memorywall_one(mid)
    return {"status": "ok", "item": _mw_item(one) if one else {"id": mid}}


@app.delete("/api/memorywall/{mid}")
async def api_mw_delete(mid: int, hard: bool = False):
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    if hard:
        await delete_memory_photos(mid)
        await delete_memory(mid)
        return {"status": "ok", "deleted": "hard", "id": mid}
    await set_memory_active(mid, False)  # 软删=归档，守“一条不少”
    return {"status": "ok", "deleted": "archived", "id": mid}


@app.post("/api/memorywall/{mid}/photos")
async def api_mw_upload_photo(mid: int, request: Request):
    """照片上传：原始字节直接作为 body（不依赖 python-multipart），
    文件名走 X-Filename 头、类型走 Content-Type。"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    existing = await get_memorywall_one(mid)
    if not existing:
        return JSONResponse(status_code=404, content={"error": "回忆不存在"})
    data = await request.body()
    if not data:
        return JSONResponse(status_code=400, content={"error": "空文件"})
    mime = (request.headers.get("content-type") or "image/png").split(";")[0].strip()
    if not mime.startswith("image/"):
        mime = "image/png"
    fname = request.headers.get("x-filename") or "photo"
    try:
        from urllib.parse import unquote
        fname = unquote(fname)
    except Exception:
        pass
    pid = await save_photo(mid, fname, mime, data)
    mm = existing.get("mw_meta") or {}
    photos = mm.get("photos") or []
    photos.append({"photo_id": pid, "original_name": fname, "mime": mime,
                   "url": f"/api/photos/{pid}", "bytes": len(data)})
    mm["photos"] = photos
    await update_mw_meta(mid, mm)
    return {"status": "ok", "photo": {"photo_id": pid, "url": f"/api/photos/{pid}", "original_name": fname}}


@app.delete("/api/memorywall/{mid}/photos/{pid}")
async def api_mw_delete_photo(mid: int, pid: int):
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM memory_photos WHERE id = $1 AND memory_id = $2", pid, mid)
    one = await get_memorywall_one(mid)
    if one:
        mm = one.get("mw_meta") or {}
        mm["photos"] = [p for p in (mm.get("photos") or []) if p.get("photo_id") != pid]
        await update_mw_meta(mid, mm)
    return {"status": "ok"}


# ---- 人设建议（A4）：提取分流出来的"行为/相处偏好"，供主理人审阅后贴进 persona ----

@app.get("/api/l5-candidates")
async def api_list_l5_candidates(status: str = "pending", target: str = None):
    """② 里程碑待审列表 + 当前 L5 正文。target='l5'(根基房)|'wall'(回忆墙房)|空(全部)。只读。"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    items = await list_l5_candidates(status, target)
    for it in items:
        if it.get("created_at"):
            it["created_at"] = it["created_at"].isoformat()
        if it.get("event_date"):
            it["event_date"] = str(it["event_date"])
    return {"status": "ok", "items": items, "total": len(items), "l5_foundation": await get_l5_foundation()}


@app.post("/api/l5-candidates/clear")
async def api_clear_l5_candidates():
    """记忆控制台:软清所有 pending L5 根基候选(→ignored,不删数据)。决定②。须声明在 /{cand_id} 之前。"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    n = await clear_l5_candidates()
    return {"status": "ok", "cleared": n}


@app.post("/api/l5-candidates/{cand_id}")
async def api_update_l5_candidate(cand_id: int, request: Request):
    """确认（可带编辑后的 content，追加进 l5Foundation 正文）/ 忽略。机器从不直接改 l5Foundation。"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    try:
        body = await request.json()
    except Exception:
        body = {}
    action = (body.get("action") or "").strip()
    if action == "approve":
        # 用候选自己存的正文/去向(不依赖前端传 content;前端只需传 id+action)；body.content 仅作可选编辑覆盖
        cand = await get_l5_candidate(cand_id)
        if not cand:
            return JSONResponse(status_code=404, content={"error": "候选不存在"})
        text = (cand.get("content") or "").strip()   # 用候选自己存的正文,不依赖前端
        target = (cand.get("target") or body.get("target") or "l5").strip()
        wrote = None
        if text:
            if target == "wall":
                # 升进回忆墙(永久层);回忆墙铁律:只增、不自动
                _today = (datetime.now(timezone.utc) + timedelta(hours=TIMEZONE_HOURS)).strftime("%Y-%m-%d")
                _mw = {"summary": text, "title": text[:24], "body": text, "source": "milestone", "author": "system"}
                await save_migrated_memory(text, 7, text[:24], _today, datetime.now(timezone.utc).isoformat(), _mw)
                wrote = "wall"
            else:
                cur = await get_gateway_config("l5Foundation", "")
                new = ((cur or "").rstrip() + ("\n" if (cur or "").strip() else "") + "- " + text).strip()
                await set_gateway_config("l5Foundation", new)
                invalidate_l5_cache()
                wrote = "l5"
        await update_l5_candidate(cand_id, "approved")
        return {"status": "ok", "approved": cand_id, "target": target, "wrote": wrote, "l5_foundation": await get_l5_foundation()}
    elif action == "ignore":
        await update_l5_candidate(cand_id, "ignored")
        return {"status": "ok", "ignored": cand_id}
    return JSONResponse(status_code=400, content={"error": "action 必须是 approve 或 ignore"})


@app.post("/api/l2/refresh")
async def api_l2_refresh(request: Request):
    """② L2今日：手动刷新今日浓缩（默认对活跃会话；返回生成的 digest 供查看/L2视图）。"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    try:
        body = await request.json()
    except Exception:
        body = {}
    sid = (body.get("session_id") or get_active_session_id() or "").strip()
    digest = await refresh_l2(sid)
    return {"status": "ok", "session_id": sid, "len": len(digest or ""),
            "today": _l2_state.get("today", ""), "bridge": _l2_state.get("bridge", "")}


@app.get("/api/l2")
async def api_l2_get():
    """② L2今日视图：当前注入的今日浓缩 + 昨日桥。"""
    return {"date": _l2_state.get("date"), "len": len(_l2_state.get("today") or ""),
            "today": _l2_state.get("today", ""), "bridge": _l2_state.get("bridge", "")}


@app.get("/api/persona-suggestions")
async def api_list_persona_suggestions(status: str = "pending"):
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    items = await list_persona_suggestions(status)
    out = []
    for it in items:
        d = dict(it)
        if d.get("created_at"):
            d["created_at"] = str(d["created_at"])
        out.append(d)
    return {"items": out, "total": len(out)}


@app.post("/api/persona-suggestions/clear")
async def api_clear_persona_suggestions():
    """记忆控制台:软清所有 pending 人设建议(→ignored,不删数据,可在 status=ignored 查回)。决定②。
    须声明在 /{sug_id} 之前。"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    n = await clear_persona_suggestions()
    return {"status": "ok", "cleared": n}


@app.post("/api/persona-suggestions")
async def api_create_persona_suggestion(request: Request):
    """手动创建一条人设建议（B 交接分拣用：把人设类记忆转成建议，供主理人审阅后贴进 persona）。
    body: {"content": "...", "source_session": "可选来源标签"}"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    try:
        body = await request.json()
    except Exception:
        body = {}
    content = (body.get("content") or "").strip()
    if not content:
        return JSONResponse(status_code=400, content={"error": "content 不能为空"})
    source_session = (body.get("source_session") or "")
    sug_id = await save_persona_suggestion(content, source_session)
    return {"status": "ok", "id": sug_id}


@app.post("/api/persona-suggestions/consolidate")
async def api_consolidate_persona_suggestions(request: Request):
    """把多条人设建议用轻量模型合并去重成一段可直接贴进 persona 的文本。
    body: {"status":"pending"}（默认）或 {"ids":[...]}。必须声明在 /{sug_id} 之前，否则会被 int 路由吃掉。"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    try:
        b = await request.json()
    except Exception:
        b = {}
    ids = b.get("ids")
    status = b.get("status", "pending")
    items = await list_persona_suggestions("all" if ids else status)
    if ids:
        idset = set(int(i) for i in ids)
        items = [it for it in items if int(it["id"]) in idset]
    if not items:
        return JSONResponse(status_code=400, content={"error": "没有可整合的人设建议"})
    numbered = "\n\n".join(f"{i+1}. {it['content']}" for i, it in enumerate(items))
    prompt = (
        "你在帮主理人整理 AI 伴侣的人设(system prompt)。以下是从聊天中分流出来的多条"
        "“行为/相处偏好”建议，彼此有重叠。请把它们合并去重，整理成一段可以直接粘贴进人设的中文文本：\n"
        "- 保留所有不同的要点，语义重复的合并成一条\n"
        "- 按主题归类（如 称呼与语气 / 不要做的事 / 亲密与暗号 等），用简洁条目\n"
        "- 只输出整理后的人设文本本身，不要任何解释、前言或结尾\n\n"
        f"建议如下：\n{numbered}"
    )
    try:
        headers = {"Authorization": f"Bearer {get_memory_api_key()}", "Content-Type": "application/json"}
        if "openrouter" in API_BASE_URL:
            headers["HTTP-Referer"] = EXTRA_REFERER
            headers["X-Title"] = EXTRA_TITLE
        async with httpx.AsyncClient(timeout=90) as client:
            resp = await client.post(API_BASE_URL, headers=headers, json={
                "model": CACHE_SUMMARY_MODEL,
                "max_tokens": 1500,
                "messages": [{"role": "user", "content": prompt}],
            })
        if resp.status_code == 200:
            data = resp.json()
            if "choices" in data:
                text = data["choices"][0]["message"]["content"].strip()
                return {"consolidated": text, "count": len(items), "source_ids": [it["id"] for it in items]}
        return JSONResponse(status_code=502, content={"error": f"模型调用失败 HTTP {resp.status_code}"})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": f"整合异常：{e}"})


@app.post("/api/admin/backfill-mw-summary")
async def api_backfill_mw_summary(request: Request):
    """一次性回填：给回忆墙 mw_meta 补 summary 结构化字段（优先取现有〔检索摘要〕，否则用 body）。幂等、不改任何展示内容。"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    rows = await list_memorywall(include_inactive=True)
    done = []; skipped = []
    for r in rows:
        mm = dict(r.get("mw_meta") or {})
        if (mm.get("summary") or "").strip():
            skipped.append(r["id"]); continue
        content = r.get("content") or ""
        summ = ""
        if "〔检索摘要〕" in content:
            summ = content.split("〔检索摘要〕", 1)[1].split("\n\n", 1)[0].strip()
        if not summ:
            summ = (mm.get("body") or "").strip()
        mm["summary"] = summ
        await update_mw_meta(r["id"], mm)
        done.append(r["id"])
    return {"status": "ok", "backfilled_count": len(done), "skipped_count": len(skipped), "backfilled": done, "total": len(rows)}


@app.post("/api/debug/built-prompt")
async def api_debug_built_prompt(request: Request):
    """诊断证据（只读，不调用上游模型）：用一条样例 user 消息跑真实的 prompt 组装函数，
    返回真正会进入请求 body[messages] 的 system 块（分区缓存 + 非缓存两路），
    证明小克人设(system_prompt.txt)与 user_profile 都被注入、且 user_profile 是独立标注块、与人设分开。"""
    try:
        b = await request.json()
    except Exception:
        b = {}
    sample = (b.get("message") or "宝贝我到家了").strip()
    up = await get_user_profile()
    up_block = _compose_user_profile_block(up)
    hdr = f"# 关于{USER_NAME}（对话对象）"
    persona = await get_system_prompt()  # A修复后：人设源 = DB（与聊天路径一致）

    def _sys_text(messages):
        for m in messages:
            if m.get("role") == "system":
                c = m.get("content")
                if isinstance(c, str):
                    return c, m
                if isinstance(c, list):
                    return "".join(p.get("text", "") for p in c if isinstance(p, dict)), m
        return "", None

    def _assert(sys_text):
        persona_head = (persona or "")[:40]
        return {
            "system_total_len": len(sys_text),
            "persona_present": bool(persona) and (persona_head in sys_text),
            "user_profile_block_present": (hdr in sys_text) if up_block else False,
            "user_profile_header_index": sys_text.find(hdr),
            "system_head_700": sys_text[:700],
        }

    out = {
        "live_mode": "partition_cache" if CACHE_PARTITION_ENABLED else "non_cache",
        "CACHE_PARTITION_ENABLED": CACHE_PARTITION_ENABLED,
        "forward_note": "两路组装的结果都会被赋给 body['messages'] 后原样转发上游(main.py 1168/1188)。",
        "persona_source": "DB gateway_config[systemPrompt] (get_system_prompt)",
        "system_prompt_len": len(persona or ""),
        "file_placeholder_len": len(SYSTEM_PROMPT or ""),
        "user_profile_set": bool(up and up.strip()),
        "user_profile_len": len(up or ""),
        "user_profile_block_rendered": up_block,
        "sample_message": sample,
    }
    try:
        part_msgs = await _build_basic_cached([], persona + up_block + _compose_l5_block(await get_l5_foundation()) + "\n\n" + MEMORY_GUIDANCE, sample, {"role": "user", "content": sample}, drift=False)
        ptxt, psys = _sys_text(part_msgs)
        last = part_msgs[-1]["content"] if part_msgs else ""
        out["partition_cache_mode"] = {
            "message_roles": [m.get("role") for m in part_msgs],
            "system_is_cached_block": bool(psys and isinstance(psys.get("content"), list)),
            "cache_control_on_system": bool(psys and isinstance(psys.get("content"), list) and any(p.get("cache_control") for p in psys["content"])),
            **_assert(ptxt),
            "current_user_turn_len": (len(last) if isinstance(last, str) else 0),
            "total_injected_chars": (len(ptxt) + (len(last) if isinstance(last, str) else 0)),
            "current_user_turn_head_300": (last[:300] if isinstance(last, str) else ""),
        }
    except Exception as e:
        out["partition_cache_mode"] = {"error": str(e)}
    try:
        enhanced = await build_system_prompt_with_memories(sample, drift=False) if (MEMORY_ENABLED and MEMORY_EXTRACT_ENABLED) else (await get_system_prompt())
        enhanced = (enhanced or "") + up_block + _compose_l5_block(await get_l5_foundation())
        out["non_cache_mode"] = _assert(enhanced)
    except Exception as e:
        out["non_cache_mode"] = {"error": str(e)}

    # 0-B 层视图：把"小克此刻读到什么"逐层拆开（全只读 drift=False；检索记忆为过收敛闸后的真实注入）
    try:
        def _layer(name, text):
            t = text or ""
            return {"name": name, "len": len(t), "text": t}
        _mem = await build_memory_text(sample, drift=False) if (MEMORY_ENABLED and MEMORY_EXTRACT_ENABLED) else ""
        layers = [
            _layer("人设 persona", persona or ""),
            _layer(f"关于{USER_NAME} user_profile", up_block or ""),
            _layer("L5 根基（常驻）", _compose_l5_block(await get_l5_foundation())),
            _layer("L2 今日（昨日桥 + 今天到哪了）", _compose_l2_block()),
            _layer("检索记忆（当前轮·过收敛闸后）", _mem),
            _layer("时间注入", build_time_injection(None)),
        ]
        out["layers"] = layers
        out["layers_total_len"] = sum(l["len"] for l in layers)
    except Exception as e:
        out["layers"] = {"error": str(e)}
    return out


@app.get("/api/debug/token-breakdown")
async def api_debug_token_breakdown(message: str = "宝贝我到家了"):
    """单轮注入的 token 分解:每层 chars/估算tokens/占比 + 缓存vs非缓存(缓存按0.1x折算实际成本)。
    只读、drift=False(不触发漂移)、不调上游。token 为 CJK感知估算(中文~0.7tok/字,英文~0.25tok/字)。"""
    sid = get_active_session_id()

    def est(s):
        s = s or ""
        cjk = sum(1 for ch in s if ('㐀' <= ch <= '鿿') or ('＀' <= ch <= '￯') or ('　' <= ch <= '〿'))
        return int(round(cjk * 0.7 + (len(s) - cjk) / 4.0))

    persona = await get_system_prompt() or ""
    up_block = _compose_user_profile_block(await get_user_profile()) or ""
    l5 = _compose_l5_block(await get_l5_foundation()) or ""
    guidance = MEMORY_GUIDANCE or ""
    state = await get_session_cache_state(sid) if sid else {}
    summary_parts = state.get("summary_parts") or []
    summary_text = "\n".join(summary_parts)
    a_start = state.get("a_start_round", 0)
    history = await get_conversation_messages(sid, limit=100000) if sid else []
    rounds = group_by_rounds(history)
    X = CACHE_PARTITION_X
    a_msgs = [m for rnd in rounds[a_start:a_start + X] for m in rnd]
    b_msgs = [m for rnd in rounds[a_start + X:] for m in rnd]
    a_text = "\n".join((m.get("content") or "") for m in a_msgs if isinstance(m.get("content"), str))
    b_text = "\n".join((m.get("content") or "") for m in b_msgs if isinstance(m.get("content"), str))
    time_inj = build_time_injection(history[-1].get("created_at") if history else None) or ""
    l2 = _compose_l2_block() or ""
    feel = ""
    try:
        if FEEL_ENABLED and sid:
            feel = _compose_feel_block(await get_recent_feels(sid)) or ""
    except Exception:
        feel = ""
    mem = ""
    try:
        if MEMORY_ENABLED and MEMORY_EXTRACT_ENABLED:
            mem = await build_memory_text(message, drift=False) or ""
    except Exception:
        mem = ""

    rows = [
        ("人设 persona", persona, True), ("档案 user_profile", up_block, True),
        ("L5 根基", l5, True), ("MEMORY_GUIDANCE", guidance, True),
        ("滚动摘要", summary_text, True), ("A区逐字历史", a_text, True), ("B区逐字历史", b_text, True),
        ("时间注入", time_inj, False), ("L2今日+昨日桥", l2, False), ("feel 感受", feel, False),
        ("检索记忆", mem, False), ("当前消息", message, False),
    ]
    layers = []
    for name, txt, cached in rows:
        layers.append({"layer": name, "chars": len(txt or ""), "tokens_est": est(txt), "cached": cached})
    tot = sum(l["tokens_est"] for l in layers) or 1
    for l in layers:
        l["pct"] = round(100.0 * l["tokens_est"] / tot, 1)
        l["eff_tokens"] = round(l["tokens_est"] * (0.1 if l["cached"] else 1.0), 1)
    cached_tok = sum(l["tokens_est"] for l in layers if l["cached"])
    noncache_tok = sum(l["tokens_est"] for l in layers if not l["cached"])
    persona_tok = next(l["tokens_est"] for l in layers if l["layer"].startswith("人设"))
    summary_tok = next(l["tokens_est"] for l in layers if l["layer"] == "滚动摘要")
    return {
        "session": sid, "sample_message": message,
        "partition_x": X, "rounds_total": len(rounds), "a_start_round": a_start,
        "a_rounds": min(X, max(0, len(rounds) - a_start)), "b_rounds": max(0, len(rounds) - a_start - X),
        "summary_parts": len(summary_parts),
        "persona_len": len(persona), "persona_is_placeholder": ("温柔耐心的AI助手" in persona or len(persona) < 800),
        "layers": layers,
        "total_tokens_est": tot,
        "cached_tokens_est": cached_tok, "noncache_tokens_est": noncache_tok,
        "noncache_pct_of_total": round(100.0 * noncache_tok / tot, 1),
        "effective_total_per_turn_est": round(sum(l["eff_tokens"] for l in layers), 1),
        "summary_cached_eff_per_turn_est": round(summary_tok * 0.1, 1),
        "persona_tokens_est": persona_tok, "persona_pct_of_total": round(100.0 * persona_tok / tot, 1),
    }


@app.get("/api/debug/storage")
async def api_debug_storage():
    """数据库占用情况(只读):各表大小 + memory_photos 重复/孤儿图片检测，方便定期自查不被图片塞满。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        db_size = await conn.fetchval("SELECT pg_size_pretty(pg_database_size(current_database()))")
        tables = await conn.fetch("""
            SELECT table_name AS name,
                   pg_size_pretty(pg_total_relation_size(quote_ident(table_name))) AS size,
                   pg_total_relation_size(quote_ident(table_name)) AS bytes
            FROM information_schema.tables
            WHERE table_schema='public'
            ORDER BY bytes DESC
        """)
        photo_count = await conn.fetchval("SELECT COUNT(*) FROM memory_photos")
        photo_size = await conn.fetchval("SELECT pg_size_pretty(COALESCE(SUM(length(data)),0)) FROM memory_photos")
        dup_groups = await conn.fetchval("""
            SELECT COUNT(*) FROM (
                SELECT md5(data) FROM memory_photos GROUP BY md5(data) HAVING COUNT(*) > 1
            ) t
        """)
        orphan_photos = await conn.fetchval("""
            SELECT COUNT(*) FROM memory_photos mp
            WHERE NOT EXISTS (SELECT 1 FROM memories m WHERE m.id = mp.memory_id)
        """)
    return {
        "db_size": db_size,
        "tables": [{"name": t["name"], "size": t["size"]} for t in tables],
        "photo_count": photo_count,
        "photo_size": photo_size,
        "duplicate_photo_groups": dup_groups,
        "orphan_photo_count": orphan_photos,
    }


@app.post("/api/photos/cleanup")
async def api_photos_cleanup(request: Request):
    """清理相册: ①孤儿图(挂着的记忆已删) ②同一条记忆下内容重复的图(留最早一张)。
    跨记忆的同图不动(可能是同一张照片合法挂在两条记忆上)。删完后台 VACUUM FULL 回收磁盘。
    body {"dry_run": true} 只报数不动手。"""
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    dry = bool(body.get("dry_run"))
    pool = await get_pool()
    async with pool.acquire() as conn:
        orphans = await conn.fetchval(
            "SELECT COUNT(*) FROM memory_photos mp "
            "WHERE NOT EXISTS (SELECT 1 FROM memories m WHERE m.id = mp.memory_id)")
        same_mem_dups = await conn.fetchval(
            "SELECT COUNT(*) FROM memory_photos a "
            "WHERE EXISTS (SELECT 1 FROM memory_photos b WHERE b.memory_id = a.memory_id "
            "              AND md5(b.data) = md5(a.data) AND b.id < a.id)")
        before_cnt = await conn.fetchval("SELECT COUNT(*) FROM memory_photos")
        before_size = await conn.fetchval(
            "SELECT pg_size_pretty(COALESCE(SUM(length(data)),0)) FROM memory_photos")
        if dry:
            return {"status": "dry_run", "orphan_photos": orphans, "same_memory_duplicates": same_mem_dups,
                    "photo_count": before_cnt, "photo_size": before_size}
        r1 = await conn.execute(
            "DELETE FROM memory_photos mp "
            "WHERE NOT EXISTS (SELECT 1 FROM memories m WHERE m.id = mp.memory_id)")
        r2 = await conn.execute(
            "DELETE FROM memory_photos a USING memory_photos b "
            "WHERE b.memory_id = a.memory_id AND md5(b.data) = md5(a.data) AND b.id < a.id")
        after_cnt = await conn.fetchval("SELECT COUNT(*) FROM memory_photos")
        after_size = await conn.fetchval(
            "SELECT pg_size_pretty(COALESCE(SUM(length(data)),0)) FROM memory_photos")

    async def _vacuum_bg():
        """VACUUM FULL 锁表且可能要跑一会,放后台;必须用事务外的独立连接。"""
        try:
            import asyncpg as _apg
            vconn = await _apg.connect(_db_module.DATABASE_URL, statement_cache_size=0)
            try:
                await vconn.execute("VACUUM FULL memory_photos")
                print("🧹 相册 VACUUM FULL 完成,磁盘空间已回收")
            finally:
                await vconn.close()
        except Exception as e:
            print(f"⚠️ 相册 VACUUM 失败(空间会随日常写入慢慢复用,不碍事): {e}")

    asyncio.create_task(_vacuum_bg())
    return {"status": "ok",
            "deleted_orphans": int((r1 or "0").split()[-1]),
            "deleted_duplicates": int((r2 or "0").split()[-1]),
            "photo_count": {"before": before_cnt, "after": after_cnt},
            "photo_size": {"before": before_size, "after": after_size},
            "note": "磁盘空间在后台回收中(约1分钟)"}


@app.post("/api/debug/scratchpad")
async def api_debug_scratchpad(request: Request):
    """只读诊断「递纸条」(deepseek)链路:给一条 user 消息,直接跑 _scratchpad_topics,
    返回 key 配没配、生成的主题列表、以及每个主题召回几条。deepseek 挂了在这里一眼看穿。"""
    try:
        b = await request.json()
    except Exception:
        b = {}
    q = (b.get("message") or "").strip()
    if not q:
        return {"error": "需要 message"}
    out = {"key_set": bool(SCRATCHPAD_API_KEY), "base_url": SCRATCHPAD_BASE_URL,
           "model": SCRATCHPAD_MODEL, "timeout_s": SCRATCHPAD_TIMEOUT, "enabled": SCRATCHPAD_ENABLED}
    if not SCRATCHPAD_API_KEY:
        out["verdict"] = "SCRATCHPAD_API_KEY 没配置,递纸条从没生效过(会静默降级原召回)"
        return out
    import time as _t
    _t0 = _t.time()
    topics = await _scratchpad_topics(q)
    out["elapsed_s"] = round(_t.time() - _t0, 2)
    out["topics"] = topics
    if topics:
        hits = {}
        for t in topics[:4]:
            try:
                ms = await search_memories(t, limit=3)
                hits[t] = [f"#{m.get('id')} {str(m.get('content'))[:40]}" for m in ms]
            except Exception as e:
                hits[t] = [f"召回出错: {e}"]
        out["recall_by_topic"] = hits
        out["verdict"] = "递纸条工作正常"
    else:
        out["verdict"] = ("没产出主题——要么 deepseek 调用失败/超时(看 elapsed 是否≈timeout),"
                          "要么它判断这条消息不需要扩展。换条更长的消息再试可区分。")
    return out


@app.get("/api/debug/memory-photos")
async def api_debug_memory_photos(ids: str):
    """只读:查一批记忆各挂了哪些图(?ids=1364,1365)。删重复记忆前用它确认图挂在哪条上,保图。"""
    try:
        id_list = [int(x) for x in ids.split(",") if x.strip()]
    except Exception:
        return {"error": "ids 格式: ?ids=1,2,3"}
    out = {}
    for mid in id_list[:50]:
        refs = await get_memory_photos(mid)
        out[mid] = [{"photo_id": r.get("photo_id"), "mime": r.get("mime")} for r in refs]
    return {"memory_photos": out}


@app.get("/api/debug/find-convo")
async def api_debug_find_convo(q: str, limit: int = 20):
    """只读:全文搜历史对话(所有线,含已归档线)。查'某件事当初到底聊没聊过、在哪条线、什么时候'。"""
    if not (q or "").strip():
        return {"error": "需要 ?q=关键词"}
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, session_id, role, left(content, 120) AS snippet, created_at "
            "FROM conversations WHERE content LIKE '%' || $1 || '%' "
            "ORDER BY id DESC LIMIT $2", q.strip(), max(1, min(int(limit), 100)))
    return {"query": q, "count": len(rows),
            "rows": [{"id": r["id"], "session": r["session_id"], "role": r["role"],
                      "at": str(r["created_at"]), "snippet": r["snippet"]} for r in rows]}


@app.post("/api/debug/memory-gate")
async def api_debug_memory_gate(request: Request):
    """只读演示 ②/① 露骨语境闸：给一条 user 消息，返回检索原始命中(arousal/score) +
    语境闸判定(intimate 怎么判的) + 过闸后会注入的列表(adj_score/被挡)。
    可选 intimate=0/1 强制，确定性对比两侧。不调用上游聊天、不漂移(仅 search 的 last_accessed 更新)。"""
    try:
        b = await request.json()
    except Exception:
        b = {}
    q = (b.get("message") or "").strip()
    force = b.get("intimate", None)
    if force is not None:
        force = bool(int(force)) if str(force).strip().isdigit() else bool(force)
    if not q:
        return {"error": "需要 message"}

    def _row(m, adj=False):
        d = {"id": m.get("id"), "arousal": round(float(m.get("arousal") or 0), 2),
             "score": round(float(m.get("score") or 0), 3),
             "content": (m.get("content") or "")[:46]}
        if adj:
            d["adj_score"] = round(float(m.get("_adj_score") or 0), 3)
        return d

    raw = await search_memories(q, limit=MAX_MEMORIES_INJECT)
    gated, dbg = await apply_explicit_gate([dict(m) for m in raw], q, force_intimate=force, force_run=True)
    kept_ids = {g.get("id") for g in gated}
    return {
        "message": q,
        "thresholds": {"SENSITIVE_AROUSAL": SENSITIVE_AROUSAL, "EXPLICIT_HARD_AROUSAL": EXPLICIT_HARD_AROUSAL,
                       "EXPLICIT_PENALTY_LAMBDA": EXPLICIT_PENALTY_LAMBDA, "MAX_INJECT": MAX_MEMORIES_INJECT},
        "decision": dbg,
        "raw_hits": [_row(m) for m in raw],
        "after_gate_inject": [_row(m, adj=True) for m in gated],
        "dropped": [_row(m) for m in raw if m.get("id") not in kept_ids],
    }


@app.post("/api/persona-suggestions/{sug_id}")
async def api_update_persona_suggestion(sug_id: int, request: Request):
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    try:
        body = await request.json()
    except Exception:
        body = {}
    status = body.get("status", "dismissed")
    if status not in ("pending", "applied", "dismissed"):
        return JSONResponse(status_code=400, content={"error": "status 必须是 pending/applied/dismissed"})
    await update_persona_suggestion(sug_id, status)
    return {"status": "ok", "id": sug_id, "new_status": status}


@app.post("/api/memories/{memory_id}/promote")
async def api_promote_to_core(memory_id: int, request: Request):
    """将记忆升级为核心记忆"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    
    data = await request.json()
    title = data.get("title")
    
    await promote_to_core(memory_id, title=title)
    return {"status": "ok", "memory_id": memory_id, "layer": 3}


@app.post("/api/memories/merge")
async def api_merge_memories(request: Request):
    """手动合并多条记忆"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    
    data = await request.json()
    memory_ids = data.get("ids", [])
    new_title = data.get("title", "")
    new_content = data.get("content", "")
    importance = data.get("importance", 5)
    layer = data.get("layer", 2)
    
    if not memory_ids or not new_content:
        return {"error": "请提供记忆ID列表和合并后内容"}
    
    new_id = await merge_memories(memory_ids, new_title, new_content, importance, layer)
    return {"status": "ok", "new_id": new_id, "merged": len(memory_ids)}


@app.post("/api/memories/check-duplicate")
async def api_check_duplicate(request: Request):
    """检查记忆是否重复"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    
    data = await request.json()
    content = data.get("content", "")
    threshold = data.get("threshold", 0.7)
    
    if not content:
        return {"error": "请提供记忆内容"}
    
    result = await check_duplicate_memory(content, threshold)
    return result


@app.post("/api/memories/cleanup-fragments")
async def api_cleanup_fragments(request: Request):
    """清理指定天数前的归档碎片
    
    Body:
        days: 清理多少天前的归档碎片（默认30天）
    """
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    
    data = await request.json()
    days = data.get("days", 30)
    
    try:
        deleted = await cleanup_old_fragments(days)
        return {"status": "ok", "deleted": deleted, "days": days}
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/memories/{memory_id}/revert-merge")
async def api_revert_merge(memory_id: int):
    """撤回合并操作：恢复原始碎片，删除合并后的事件记忆"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    
    try:
        result = await revert_merge(memory_id)
        return result
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/memories/{memory_id}/restore")
async def api_restore_memory(memory_id: int):
    """恢复已归档的记忆（将 is_active 设为 TRUE）"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    
    try:
        await update_memory_with_layer(memory_id, is_active=True)
        return {"status": "ok", "id": memory_id}
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/memories/layer-stats")
async def api_layer_statistics():
    """获取各层记忆统计数据"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    
    try:
        stats = await get_layer_statistics()
        return stats
    except Exception as e:
        return {"error": str(e)}


@app.post("/import/text")
async def import_text_memories(request: Request):
    """从纯文本导入记忆（每行一条），可选自动评分"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用（设置 MEMORY_ENABLED=true 开启）"}
    
    try:
        data = await request.json()
        lines = data.get("lines", [])
        skip_scoring = data.get("skip_scoring", False)
        
        if not lines:
            return {"error": "没有找到记忆条目"}
        
        if skip_scoring:
            scored = [{"content": t, "importance": 5} for t in lines]
        else:
            scored = await score_memories(lines)
        
        imported = 0
        skipped = 0
        
        for mem in scored:
            content = mem.get("content", "")
            if not content:
                continue
            
            pool = await get_pool()
            async with pool.acquire() as conn:
                existing = await conn.fetchval(
                    "SELECT COUNT(*) FROM memories WHERE content = $1", content
                )
            
            if existing > 0:
                skipped += 1
                continue
            
            await save_memory(
                content=content,
                importance=mem.get("importance", 5),
                source_session="text-import",
            )
            imported += 1
        
        total = await get_all_memories_count()
        return {
            "status": "done",
            "imported": imported,
            "skipped": skipped,
            "total": total,
        }
    except Exception as e:
        return {"error": str(e)}


@app.post("/import/memories")
async def import_memories(request: Request):
    """从 JSON 导入记忆（用于迁移或恢复备份）"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用（设置 MEMORY_ENABLED=true 开启）"}
    
    try:
        data = await request.json()
        memories = data.get("memories", [])
        
        if not memories:
            return {"error": "没有找到记忆数据，请确认 JSON 格式正确"}
        
        imported = 0
        skipped = 0
        
        for mem in memories:
            content = mem.get("content", "")
            if not content:
                continue
            
            pool = await get_pool()
            async with pool.acquire() as conn:
                existing = await conn.fetchval(
                    "SELECT COUNT(*) FROM memories WHERE content = $1", content
                )
            
            if existing > 0:
                skipped += 1
                continue
            
            await save_memory(
                content=content,
                importance=mem.get("importance", 5),
                source_session=mem.get("source_session", "json-import"),
            )
            imported += 1
        
        total = await get_all_memories_count()
        return {
            "status": "done",
            "imported": imported,
            "skipped": skipped,
            "total": total,
        }
    except Exception as e:
        return {"error": str(e)}


# ============================================================
# 对话记录管理 API
# ============================================================

@app.get("/api/conversations")
async def api_conversations(page: int = 1, per_page: int = 20):
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    try:
        results, total = await get_conversations_paginated(page, per_page)
        total_pages = max(1, -(-total // per_page))  # 向上取整
        return {"conversations": results, "total": total, "page": page, "per_page": per_page, "total_pages": total_pages}
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/conversations/{session_id}/messages")
async def api_conversation_messages(session_id: str, limit: int = 50, offset: int = 0):
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            total = await conn.fetchval(
                "SELECT COUNT(*) FROM conversations WHERE session_id = $1", session_id
            )
            rows = await conn.fetch("""
                SELECT id, role, content, created_at
                FROM conversations WHERE session_id = $1
                ORDER BY created_at DESC
                LIMIT $2 OFFSET $3
            """, session_id, limit, offset)
        msgs = [{"id": r["id"], "role": r["role"], "content": r["content"], 
                 "created_at": r["created_at"].isoformat() if r.get("created_at") else None} for r in rows]
        return {"messages": msgs, "total": total}
    except Exception as e:
        return {"error": str(e)}


@app.delete("/api/conversations/{session_id}")
async def api_delete_conversation(session_id: str):
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    try:
        await delete_conversation(session_id)
        return {"status": "ok"}
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/conversations/batch-delete")
async def api_batch_delete(request: Request):
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    try:
        body = await request.json()
        ids = body.get("session_ids", [])
        if ids:
            await batch_delete_conversations(ids)
        return {"status": "ok", "deleted": len(ids)}
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/admin/merge-sessions")
async def api_merge_sessions(request: Request):
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    try:
        body = await request.json()
        source_ids = [s for s in body.get("source_ids", []) if s != body.get("target_id", "")]
        target_id = body.get("target_id", "")
        if not source_ids or not target_id:
            return {"error": "source_ids 和 target_id 不能为空"}
        result = await merge_sessions_to_target(source_ids, target_id)
        return {"status": "ok", **result}
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/chat/search")
async def api_search_conversations(q: str = "", limit: int = 20, offset: int = 0):
    """搜索对话内容"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    if not q.strip():
        return {"error": "搜索关键词不能为空", "results": [], "total": 0}
    try:
        results, total = await search_conversations(q.strip(), limit, offset)
        return {"results": results, "total": total}
    except Exception as e:
        return {"error": str(e), "results": [], "total": 0}


@app.patch("/api/chat/messages/{message_id}")
async def api_update_message(message_id: int, request: Request):
    """编辑单条消息内容"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    try:
        body = await request.json()
        content = body.get("content", "").strip()
        if not content:
            return {"error": "内容不能为空"}
        updated = await update_message_content(message_id, content)
        if updated == 0:
            return {"error": "消息不存在"}
        return {"status": "ok"}
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/conversations/export")
async def api_export_conversations():
    """导出所有对话记录"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    try:
        data = await export_all_conversations()
        return JSONResponse(content=data)
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/conversations/import")
async def api_import_conversations(request: Request):
    """导入对话记录（JSON格式，自动去重）"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    try:
        records = await request.json()
        if not isinstance(records, list):
            return {"error": "格式错误：需要 JSON 数组"}
        imported, skipped = await import_conversations(records)
        return {"status": "ok", "imported": imported, "skipped": skipped, "total": imported + skipped}
    except Exception as e:
        return {"error": str(e)}


# ============================================================
# 对话线管理 API（分区缓存）
# ============================================================

@app.get("/api/partition/status")
async def api_partition_status():
    active_sid = get_active_session_id()
    state = await get_session_cache_state(active_sid) if active_sid else {}
    return {
        "enabled": CACHE_PARTITION_ENABLED,
        "active_session_id": active_sid,
        "partition_x": CACHE_PARTITION_X,
        "summary_model": CACHE_SUMMARY_MODEL,
        "summary": '\n\n'.join(state.get('summary_parts', [])),
        "summary_parts": state.get('summary_parts', []),
        "summary_count": len(state.get('summary_parts', [])),
        "summary_length": sum(len(p) for p in state.get('summary_parts', [])),
        "a_start_round": state.get('a_start_round', 0),
        "updated_at": state.get('updated_at').isoformat() if state.get('updated_at') else None,
    }


@app.get("/api/partition/threads")
async def api_partition_threads():
    threads = await list_all_session_cache_states()
    active_sid = get_active_session_id()
    for t in threads:
        t['is_active'] = (t['session_id'] == active_sid)
    if active_sid and not any(t['session_id'] == active_sid for t in threads):
        threads.insert(0, {'session_id': active_sid, 'summary': '', 'summary_length': 0, 'summary_count': 0, 'a_start_round': 0, 'updated_at': None, 'message_count': 0, 'chat_tokens': 0, 'is_active': True})
    return {"threads": threads, "active_session_id": active_sid}


@app.put("/api/partition/summary")
async def api_update_summary(request: Request):
    try:
        body = await request.json()
        sid = body.get("session_id", "")
        summary = body.get("summary", "")
        if not sid:
            return {"error": "session_id 不能为空"}
        state = await get_session_cache_state(sid)
        summary_parts = [summary] if isinstance(summary, str) and summary else summary if isinstance(summary, list) else []
        # 摘要清空时 a_start_round 也归零，否则历史会被跳过
        a_start = state.get('a_start_round', 0) if summary_parts else 0
        await save_session_cache_state(sid, summary_parts, a_start)
        total_len = sum(len(p) for p in summary_parts)
        return {"status": "ok", "summary_parts": len(summary_parts), "summary_length": total_len}
    except Exception as e:
        return {"error": str(e)}


@app.delete("/api/partition/summary")
async def api_clear_summary(request: Request):
    try:
        body = await request.json()
        sid = body.get("session_id", "")
        if not sid:
            return {"error": "session_id 不能为空"}
        # 摘要和 a_start_round 一起归零
        await save_session_cache_state(sid, [], 0)
        return {"status": "ok"}
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/partition/thread")
async def api_create_thread(request: Request):
    try:
        body = await request.json()
        new_id = body.get("session_id", "").strip()
        copy_from = body.get("copy_summary_from", "")
        if not new_id:
            return {"error": "session_id 不能为空"}
        existing = await get_session_cache_state(new_id)
        if existing.get('updated_at'):
            return {"error": f"对话线 '{new_id}' 已存在"}
        summary_parts = []
        if copy_from:
            source = await get_session_cache_state(copy_from)
            summary_parts = source.get('summary_parts', [])
        await save_session_cache_state(new_id, summary_parts, 0)
        total_len = sum(len(p) for p in summary_parts)
        return {"status": "ok", "session_id": new_id, "summary_length": total_len}
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/partition/switch")
async def api_switch_thread(request: Request):
    global PARTITION_SESSION_ID
    try:
        body = await request.json()
        new_id = body.get("session_id", "").strip()
        if not new_id:
            return {"error": "session_id 不能为空"}
        old_id = PARTITION_SESSION_ID
        PARTITION_SESSION_ID = new_id
        await set_gateway_config("partition_session_id", new_id)
        return {"status": "ok", "old_session_id": old_id, "new_session_id": new_id}
    except Exception as e:
        return {"error": str(e)}


@app.put("/api/partition/thread/rename")
async def api_rename_thread(request: Request):
    global PARTITION_SESSION_ID
    try:
        body = await request.json()
        old_id = body.get("old_id", "").strip()
        new_id = body.get("new_id", "").strip()
        if not old_id or not new_id:
            return {"error": "old_id 和 new_id 不能为空"}
        if old_id == new_id:
            return {"error": "新旧ID相同"}
        success = await rename_session_id(old_id, new_id)
        if not success:
            return {"error": f"对话线 '{new_id}' 已存在"}
        # 如果重命名的是活跃线，同步更新
        if PARTITION_SESSION_ID == old_id:
            PARTITION_SESSION_ID = new_id
            await set_gateway_config("partition_session_id", new_id)
        return {"status": "ok", "old_id": old_id, "new_id": new_id}
    except Exception as e:
        return {"error": str(e)}


@app.delete("/api/partition/thread/{session_id:path}")
async def api_delete_thread(session_id: str):
    """删除对话线（不允许删除当前活跃线）"""
    try:
        active_sid = get_active_session_id()
        if session_id == active_sid:
            return {"error": "不能删除当前活跃的对话线"}
        await delete_session_cache_state(session_id)
        print(f"🗑️ 删除对话线: {session_id}")
        return {"status": "ok", "session_id": session_id}
    except Exception as e:
        return {"error": str(e)}


# ============================================================
# 记忆向量补算（带进度追踪）
# ============================================================

_backfill_mem_status = {
    "running": False,
    "total": 0,
    "done": 0,
    "error": None,
    "finished_at": None,
}

@app.post("/api/admin/backfill-memory-embeddings")
async def api_backfill_memory_embeddings():
    """给已有记忆补算embedding（后台异步执行，前端轮询进度）"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    
    if _backfill_mem_status["running"]:
        return {"error": "补算任务正在运行中，请等待完成"}
    
    try:
        total = await get_pending_memory_embedding_count()
    except Exception as e:
        return {"error": f"查询待处理数量失败: {e}"}
    
    if total == 0:
        return {"status": "done", "message": "所有记忆已有embedding，无需补算", "total": 0, "done": 0}
    
    _backfill_mem_status["running"] = True
    _backfill_mem_status["total"] = total
    _backfill_mem_status["done"] = 0
    _backfill_mem_status["error"] = None
    _backfill_mem_status["finished_at"] = None
    
    async def run_backfill():
        try:
            while _backfill_mem_status["running"]:
                updated = await backfill_memory_embeddings(batch_size=20)
                _backfill_mem_status["done"] += updated
                
                if updated == 0:
                    break
                
                await asyncio.sleep(1)
            
            _backfill_mem_status["finished_at"] = datetime.now(timezone.utc).isoformat()
            print(f"✅ 记忆embedding补算完成：{_backfill_mem_status['done']}/{_backfill_mem_status['total']}")
        except Exception as e:
            _backfill_mem_status["error"] = str(e)
            print(f"❌ 记忆embedding补算异常: {e}")
        finally:
            _backfill_mem_status["running"] = False
    
    asyncio.create_task(run_backfill())
    return {"status": "started", "total": total}

@app.get("/api/admin/backfill-memory-embeddings/status")
async def api_backfill_memory_embeddings_status():
    """查询记忆embedding补算进度"""
    return {
        "running": _backfill_mem_status["running"],
        "total": _backfill_mem_status["total"],
        "done": _backfill_mem_status["done"],
        "error": _backfill_mem_status["error"],
        "finished_at": _backfill_mem_status["finished_at"],
    }


# ============================================================
# 模型列表 API（/api/models）
# 设置面板的 combo-box 用，根据 API_BASE_URL 自动适配
# ============================================================

@app.get("/api/models")
async def get_models():
    """获取可用模型列表（根据 API_BASE_URL 自动适配）"""
    is_openrouter = "openrouter.ai" in API_BASE_URL
    is_google = "googleapis.com" in API_BASE_URL or "generativelanguage" in API_BASE_URL
    is_openai = "api.openai.com" in API_BASE_URL

    try:
        if is_openrouter:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.get(
                    "https://openrouter.ai/api/v1/models",
                    headers={"Authorization": f"Bearer {API_KEY}"}
                )
                if response.status_code == 200:
                    data = response.json()
                    models = data.get("data", [])
                    simplified = [{"id": m.get("id"), "name": m.get("name"), "context_length": m.get("context_length")} for m in models]
                    simplified.sort(key=lambda x: x.get("name", ""))
                    return {"models": simplified, "total": len(simplified), "provider": "openrouter"}

        elif is_google:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.get(
                    f"https://generativelanguage.googleapis.com/v1beta/models?key={API_KEY}"
                )
                if response.status_code == 200:
                    data = response.json()
                    models = data.get("models", [])
                    simplified = []
                    for m in models:
                        full_name = m.get("name", "")
                        model_id = full_name.replace("models/", "") if full_name.startswith("models/") else full_name
                        display_name = m.get("displayName", model_id)
                        supported_methods = m.get("supportedGenerationMethods", [])
                        if "generateContent" in supported_methods:
                            simplified.append({"id": model_id, "name": display_name, "context_length": m.get("inputTokenLimit"), "output_limit": m.get("outputTokenLimit")})
                    def sort_key(x):
                        name = x.get("id", "")
                        if "gemini-3" in name: return "0" + name
                        elif "gemini-2.5" in name: return "1" + name
                        elif "gemini-2.0" in name: return "2" + name
                        else: return "9" + name
                    simplified.sort(key=sort_key)
                    return {"models": simplified, "total": len(simplified), "provider": "google"}
                else:
                    print(f"[get_models] Google API 返回 {response.status_code}: {response.text}")
                    return {"error": f"Google API 返回 {response.status_code}", "models": [], "provider": "google"}

        elif is_openai:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.get(
                    "https://api.openai.com/v1/models",
                    headers={"Authorization": f"Bearer {API_KEY}"}
                )
                if response.status_code == 200:
                    data = response.json()
                    models = data.get("data", [])
                    simplified = [{"id": m.get("id", ""), "name": m.get("id", "")} for m in models if m.get("id", "").startswith(("gpt-", "o1", "o3", "o4"))]
                    simplified.sort(key=lambda x: x.get("id", ""))
                    return {"models": simplified, "total": len(simplified), "provider": "openai"}
            openai_models = [
                {"id": "gpt-4.1", "name": "GPT-4.1"},
                {"id": "gpt-4o", "name": "GPT-4o"},
                {"id": "gpt-4o-mini", "name": "GPT-4o Mini"},
                {"id": "o3-mini", "name": "o3-mini"},
            ]
            return {"models": openai_models, "total": len(openai_models), "provider": "openai"}

        else:
            return {"models": [], "total": 0, "provider": "unknown", "note": "未识别的 API，请手动输入模型名"}

    except Exception as e:
        print(f"[get_models] 错误: {e}")
        return {"error": str(e), "models": []}


# ============================================================
# 高级设置面板 API（/api/settings）
# Dashboard 前端设置面板用，管理所有运行时可调配置
# ============================================================

def _mask_key(key_value: str) -> str:
    """API Key 打码：只露前5位和后4位"""
    if not key_value:
        return ""
    if len(key_value) < 10:
        return "****"
    return key_value[:5] + "****" + key_value[-4:]


def _is_masked(value: str) -> bool:
    """判断值是否是打码值（用户没改过）"""
    return "****" in str(value)


def _parse_bool(val, fallback=False) -> bool:
    """解析布尔值（兼容字符串/布尔/None）"""
    if val is None:
        return fallback
    if isinstance(val, bool):
        return val
    return str(val).lower() in ("true", "1", "yes")


@app.get("/api/settings")
async def get_settings():
    """获取高级设置（数据库优先，fallback 到环境变量/运行时默认值）"""
    try:
        db = await get_all_gateway_config()

        # --- 基础连接 ---
        api_key_raw = db.get("API_KEY") or API_KEY
        embedding_key_raw = db.get("EMBEDDING_API_KEY") or _db_module.EMBEDDING_API_KEY

        memory_key_raw = db.get("MEMORY_API_KEY") or MEMORY_API_KEY

        settings = {
            # 基础连接
            "API_BASE_URL":     db.get("API_BASE_URL") or str(API_BASE_URL),
            "API_KEY":          _mask_key(api_key_raw),
            "DEFAULT_MODEL":    db.get("DEFAULT_MODEL") or str(DEFAULT_MODEL),

            # 记忆系统
            "MEMORY_ENABLED":          _parse_bool(db.get("MEMORY_ENABLED"), MEMORY_ENABLED),
            "MEMORY_API_KEY":          _mask_key(memory_key_raw),
            "MEMORY_MODEL":            db.get("MEMORY_MODEL") or os.environ.get("MEMORY_MODEL", ""),
            "MAX_MEMORIES_INJECT":     int(db.get("MAX_MEMORIES_INJECT") or MAX_MEMORIES_INJECT),
            "MIN_SCORE_THRESHOLD":     float(db.get("MIN_SCORE_THRESHOLD") or _db_module.MIN_SCORE_THRESHOLD),
            "MEMORY_EXTRACT_INTERVAL": int(db.get("MEMORY_EXTRACT_INTERVAL") or MEMORY_EXTRACT_INTERVAL),

            # 缓存分区
            "CACHE_PARTITION_ENABLED": _parse_bool(db.get("CACHE_PARTITION_ENABLED"), CACHE_PARTITION_ENABLED),
            "CACHE_PARTITION_X":       int(db.get("CACHE_PARTITION_X") or CACHE_PARTITION_X),
            "CACHE_PARTITION_TRIGGER": db.get("CACHE_PARTITION_TRIGGER") or CACHE_PARTITION_TRIGGER,
            "CACHE_PARTITION_WINDOW":  int(db.get("CACHE_PARTITION_WINDOW") or CACHE_PARTITION_WINDOW),
            "CACHE_SUMMARY_MODEL":     db.get("CACHE_SUMMARY_MODEL") or str(CACHE_SUMMARY_MODEL),
            # 缓存 TTL 模式（"1h" 打底 / "5m" 密集聊；PR-2 会加 "none"）
            "CACHE_TTL_MODE":          db.get("CACHE_TTL_MODE") or CACHE_TTL_MODE,

            # 向量搜索（开源版用 EMBEDDING_API_KEY + EMBEDDING_BASE_URL）
            "MEMORY_VECTOR_ENABLED":   _parse_bool(db.get("MEMORY_VECTOR_ENABLED"), _db_module.MEMORY_VECTOR_ENABLED),
            "EMBEDDING_API_KEY":       _mask_key(embedding_key_raw),
            "EMBEDDING_BASE_URL":      db.get("EMBEDDING_BASE_URL") or str(_db_module.EMBEDDING_BASE_URL),
            "EMBEDDING_MODEL":         db.get("EMBEDDING_MODEL") or str(_db_module.EMBEDDING_MODEL),
            "EMBEDDING_DIM":           int(db.get("EMBEDDING_DIM") or _db_module.EMBEDDING_DIM),

            # 搜索权重
            "MEMORY_HW_KEYWORD":        float(db.get("MEMORY_HW_KEYWORD") or _db_module.MEMORY_HW_KEYWORD),
            "MEMORY_HW_SEMANTIC":       float(db.get("MEMORY_HW_SEMANTIC") or _db_module.MEMORY_HW_SEMANTIC),
            "MEMORY_HW_IMPORTANCE":     float(db.get("MEMORY_HW_IMPORTANCE") or _db_module.MEMORY_HW_IMPORTANCE),
            "MEMORY_HW_RECENCY":        float(db.get("MEMORY_HW_RECENCY") or _db_module.MEMORY_HW_RECENCY),
            "MEMORY_SEMANTIC_THRESHOLD": float(db.get("MEMORY_SEMANTIC_THRESHOLD") or _db_module.MEMORY_SEMANTIC_THRESHOLD),

            # 其他
            "FORCE_STREAM":       _parse_bool(db.get("FORCE_STREAM"), FORCE_STREAM),
            "REASONING_EFFORT":   db.get("REASONING_EFFORT") or str(REASONING_EFFORT),

            # System Prompt
            "systemPrompt": db.get("systemPrompt") or _DEFAULT_SYSTEM_PROMPT or "",
            # 用户档案（关于阮阮，与小克人设分开存/改/注入）
            "userProfile": db.get("userProfile") or "",
            # ② L5根基（关系里程碑常驻正文，阮阮掌控）
            "l5Foundation": db.get("l5Foundation") or "",
        }

        return {"status": "ok", "settings": settings}
    except Exception as e:
        print(f"[get_settings] 错误: {e}")
        return {"error": str(e)}


@app.put("/api/settings")
async def save_settings(request: Request):
    """保存高级设置（写入数据库 + 热更新运行时变量，立即生效无需重启）"""
    try:
        data = await request.json()
        updated = []
        skipped = []

        # main.py 全局变量映射（key → 类型转换函数）
        _MAIN_VARS = {
            "API_BASE_URL":          str,
            "API_KEY":               str,
            "DEFAULT_MODEL":         str,
            "MEMORY_API_KEY":        str,
            "MEMORY_ENABLED":        lambda v: _parse_bool(v),
            "MAX_MEMORIES_INJECT":   int,
            "MEMORY_EXTRACT_INTERVAL": int,
            "CACHE_PARTITION_ENABLED": lambda v: _parse_bool(v),
            "CACHE_PARTITION_X":     int,
            "CACHE_PARTITION_TRIGGER": str,
            "CACHE_PARTITION_WINDOW": int,
            "CACHE_SUMMARY_MODEL":   str,
            # 缓存 TTL 模式："1h" / "5m"（PR-1）；PR-2 加 "none"。非法值兜底 "1h"，绝不破坏原作者机制
            "CACHE_TTL_MODE":        lambda v: (str(v).strip().lower() if str(v).strip().lower() in ("1h", "5m") else "1h"),
            "FORCE_STREAM":          lambda v: _parse_bool(v),
            "REASONING_EFFORT":      str,
            # 记忆控制台 B 类(原 env-only → 存库+热更+恢复)
            "MOOD_DRIFT_ENABLED":    lambda v: _parse_bool(v),
            "MOOD_DRIFT_STEP":       float,
            "MOOD_DRIFT_DAILY_CAP":  int,
            "MOOD_RECENT_N":         int,
            "MOOD_DRIFT_SKIP_MEMORYWALL": lambda v: _parse_bool(v),
            "PERSONA_SUGGESTION_ENABLED": lambda v: _parse_bool(v),
            "PERSONA_SUGGESTION_MIN_IMPORTANCE": int,
            "L5_AUTO_ENABLED":       lambda v: _parse_bool(v),
            "IMAGE_ENABLED":         lambda v: _parse_bool(v),
            "IMAGE_GEN_ENABLED":     lambda v: _parse_bool(v),
            "IMAGE_GEN_MODEL":       str,
            "IMAGE_GEN_BASE_URL":    str,
            "IMAGE_GEN_API_KEY":     str,
            "IMAGE_GEN_SIZE":        str,
            "USER_NAME":             str,
            "AI_NAME":               str,
            "HEALTH_SAFETY_NOTE":    str,
            "HOME_TITLE":            str,
            "HOME_SUBTITLE":         str,
            "SINCE_DATE":            str,
            "INTIMACY_UNLOCK_KEYS":  lambda v: [k.strip().lower() for k in str(v).split(",") if k.strip()],
            "MEMORY_EXTRACT_ENABLED": lambda v: _parse_bool(v),
            "L2_TODAY_ENABLED":      lambda v: _parse_bool(v),
            "L2_REFRESH_N":          int,
            "DREAM_ENABLED":         lambda v: _parse_bool(v),
            "DREAM_RETRIEVABLE":     lambda v: _parse_bool(v),
            "SUMMARY_CAP_ENABLED":   lambda v: _parse_bool(v),
            "SUMMARY_CAP_N":         int,
            "SUMMARY_CAP_B":         int,
            "PROACTIVE_GAP_HOURS":   float,
        }

        # database.py 全局变量映射（开源版用 EMBEDDING_API_KEY + EMBEDDING_BASE_URL）
        _DB_VARS = {
            "EMBEDDING_API_KEY":       str,
            "EMBEDDING_BASE_URL":      str,
            "EMBEDDING_MODEL":         str,
            "EMBEDDING_DIM":           int,
            "MIN_SCORE_THRESHOLD":     float,
            "MEMORY_VECTOR_ENABLED":   lambda v: _parse_bool(v),
            "MEMORY_HW_KEYWORD":       float,
            "MEMORY_HW_SEMANTIC":      float,
            "MEMORY_HW_IMPORTANCE":    float,
            "MEMORY_HW_RECENCY":       float,
            "MEMORY_SEMANTIC_THRESHOLD": float,
        }

        # 只存 os.environ 的变量
        _ENV_ONLY = {"MEMORY_MODEL": str}

        # 打码字段
        _MASKED_KEYS = {"API_KEY", "EMBEDDING_API_KEY", "MEMORY_API_KEY"}

        for key, value in data.items():
            # --- 打码字段特殊处理 ---
            if key in _MASKED_KEYS:
                str_val = str(value).strip()
                if _is_masked(str_val):
                    skipped.append(key)
                    continue
                if not str_val:
                    await set_gateway_config(key, "")
                    if key in _MAIN_VARS:
                        globals()[key] = ""
                    elif key in _DB_VARS:
                        setattr(_db_module, key, "")
                    if key == "MEMORY_API_KEY":
                        import memory_extractor as _me_mod
                        _me_mod.MEMORY_API_KEY = ""
                    os.environ[key] = ""
                    updated.append(key)
                    continue

            # --- systemPrompt 特殊处理 ---
            if key == "systemPrompt":
                await set_gateway_config("systemPrompt", str(value))
                invalidate_system_prompt_cache()
                updated.append("systemPrompt")
                print(f"[settings] systemPrompt 已更新（{len(str(value))} 字）")
                continue

            # --- userProfile 特殊处理（关于阮阮，与人设分开）---
            if key == "userProfile":
                await set_gateway_config("userProfile", str(value))
                invalidate_user_profile_cache()
                updated.append("userProfile")
                print(f"[settings] userProfile 已更新（{len(str(value))} 字）")
                continue

            # --- ② L5根基正文（关系里程碑常驻块）---
            if key == "l5Foundation":
                await set_gateway_config("l5Foundation", str(value))
                invalidate_l5_cache()
                updated.append("l5Foundation")
                print(f"[settings] l5Foundation 已更新（{len(str(value))} 字）")
                continue

            # --- 常规字段 ---
            await set_gateway_config(key, str(value))

            if key in _MAIN_VARS:
                typed_value = _MAIN_VARS[key](value)
                if key in _CLAMP:
                    _lo, _hi = _CLAMP[key]; typed_value = max(_lo, min(_hi, typed_value))
                    await set_gateway_config(key, str(typed_value))  # 存夹紧后的值,重启恢复一致
                globals()[key] = typed_value
                os.environ[key] = str(typed_value)
                if key == "MEMORY_API_KEY":
                    import memory_extractor as _me_mod
                    _me_mod.MEMORY_API_KEY = str(value)
                updated.append(key)
                print(f"[settings] {key} = {typed_value}")

            elif key in _DB_VARS:
                typed_value = _DB_VARS[key](value)
                setattr(_db_module, key, typed_value)
                os.environ[key] = str(value)
                updated.append(key)
                print(f"[settings] {key} = {typed_value} (database)")

            elif key in _ENV_ONLY:
                typed_value = _ENV_ONLY[key](value)
                os.environ[key] = str(typed_value)
                updated.append(key)
                print(f"[settings] {key} = {typed_value} (env)")

            else:
                skipped.append(key)

        return {
            "status": "ok",
            "updated": updated,
            "skipped": skipped,
            "message": f"已更新 {len(updated)} 项配置，立即生效"
        }
    except Exception as e:
        print(f"[save_settings] 错误: {e}")
        return {"error": str(e)}


@app.get("/api/console")
async def api_console():
    """记忆控制台:一次拉全所有 knob 的当前值 + 默认 + 安全范围 + 计数 + 解锁钥匙。只读,页面渲染不靠猜。
    写回路径:行为开关走各自 /toggle;衰减走 /api/memories/decay/toggle;其余数值/B类开关走 PUT /api/settings(已夹紧)。"""
    def _rng(k):
        r = _CLAMP.get(k)
        return ({"min": r[0], "max": r[1]} if r else {})
    toggles = {
        # 5 个行为开关:专用 /toggle 端点;默认关
        "feel":              {"on": FEEL_ENABLED,            "default": False, "ep": "/api/feel/toggle"},
        "redact":            {"on": _EXPLICIT_REDACT,        "default": False, "ep": "/api/explicit-redact/toggle"},
        "summary_quality":   {"on": SUMMARY_QUALITY_ENABLED, "default": False, "ep": "/api/summary/toggle"},
        "proactive":         {"on": PROACTIVE_ENABLED,       "default": False, "ep": "/api/proactive/toggle"},
        "decay":             {"on": DECAY_ENABLED,           "default": False, "ep": "/api/memories/decay/toggle"},
        # B 类:走 PUT /api/settings;多数默认开(当前 live 行为)
        "mood_drift":        {"on": MOOD_DRIFT_ENABLED,      "default": True,  "key": "MOOD_DRIFT_ENABLED", "sensitive": True},
        "drift_skip_mw":     {"on": MOOD_DRIFT_SKIP_MEMORYWALL, "default": True, "key": "MOOD_DRIFT_SKIP_MEMORYWALL"},
        "persona":           {"on": PERSONA_SUGGESTION_ENABLED, "default": True, "key": "PERSONA_SUGGESTION_ENABLED"},
        "l5_auto":           {"on": L5_AUTO_ENABLED, "default": True, "key": "L5_AUTO_ENABLED"},
        "image":             {"on": IMAGE_ENABLED, "default": False, "key": "IMAGE_ENABLED", "sensitive": True},
        "extract":           {"on": MEMORY_EXTRACT_ENABLED,  "default": True,  "key": "MEMORY_EXTRACT_ENABLED", "sensitive": True},
        "l2_today":          {"on": L2_TODAY_ENABLED,        "default": True,  "key": "L2_TODAY_ENABLED"},
        "dream":             {"on": DREAM_ENABLED,           "default": True,  "key": "DREAM_ENABLED"},
        "dream_retrievable": {"on": DREAM_RETRIEVABLE,       "default": False, "key": "DREAM_RETRIEVABLE"},
        "summary_cap":       {"on": SUMMARY_CAP_ENABLED,     "default": False, "key": "SUMMARY_CAP_ENABLED"},
    }
    numbers = {
        "MAX_MEMORIES_INJECT":     {"value": MAX_MEMORIES_INJECT, "default": 15, "via": "settings", **_rng("MAX_MEMORIES_INJECT")},
        "CACHE_PARTITION_X":       {"value": CACHE_PARTITION_X, "default": 15, "via": "settings", **_rng("CACHE_PARTITION_X")},
        "MEMORY_EXTRACT_INTERVAL": {"value": MEMORY_EXTRACT_INTERVAL, "default": 1, "via": "settings", **_rng("MEMORY_EXTRACT_INTERVAL")},
        "MIN_SCORE_THRESHOLD":     {"value": float(_db_module.MIN_SCORE_THRESHOLD), "default": 0.15, "via": "settings", "min": 0, "max": 1},
        "MEMORY_SEMANTIC_THRESHOLD": {"value": float(_db_module.MEMORY_SEMANTIC_THRESHOLD), "default": 0.5, "via": "settings", "min": 0, "max": 1},
        "PROACTIVE_GAP_HOURS":     {"value": PROACTIVE_GAP_HOURS, "default": 6, "via": "settings", **_rng("PROACTIVE_GAP_HOURS")},
        "PERSONA_SUGGESTION_MIN_IMPORTANCE": {"value": PERSONA_SUGGESTION_MIN_IMPORTANCE, "default": 7, "via": "settings", **_rng("PERSONA_SUGGESTION_MIN_IMPORTANCE")},
        "MOOD_DRIFT_STEP":         {"value": MOOD_DRIFT_STEP, "default": 0.1, "via": "settings", "sensitive": True, **_rng("MOOD_DRIFT_STEP")},
        "MOOD_DRIFT_DAILY_CAP":    {"value": MOOD_DRIFT_DAILY_CAP, "default": 3, "via": "settings", "sensitive": True, **_rng("MOOD_DRIFT_DAILY_CAP")},
        "MOOD_RECENT_N":           {"value": MOOD_RECENT_N, "default": 30, "via": "settings", "sensitive": True, **_rng("MOOD_RECENT_N")},
        "L2_REFRESH_N":            {"value": L2_REFRESH_N, "default": 5, "via": "settings", **_rng("L2_REFRESH_N")},
        "SUMMARY_CAP_N":           {"value": SUMMARY_CAP_N, "default": 8, "via": "settings", "min": 4, "max": 30},
        "SUMMARY_CAP_B":           {"value": SUMMARY_CAP_B, "default": 4, "via": "settings", "min": 1, "max": 15},
        # 衰减阈值:走 /api/memories/decay/toggle
        "decay_age_days":          {"value": DECAY_AGE_DAYS, "default": 7, "via": "decay", "min": 1, "max": 90},
        "decay_imp_max":           {"value": DECAY_IMP_MAX, "default": 4, "via": "decay", "min": 1, "max": 10},
        "decay_idle_days":         {"value": DECAY_IDLE_DAYS, "default": 5, "via": "decay", "min": 1, "max": 90},
        "decay_arousal_max":       {"value": DECAY_AROUSAL_MAX, "default": 0.45, "via": "decay", "min": 0, "max": 1},
    }
    counts = {}
    if MEMORY_ENABLED:
        try:
            counts["is_explicit"] = await count_explicit_memories()
            counts["active_memories"] = await count_active_memories()
            counts["persona_pending"] = len(await list_persona_suggestions("pending"))
            counts["l5_pending"] = len(await list_l5_candidates("pending"))
            _b = await get_gateway_config("decay_last_batch", "")
            counts["decay_last_batch"] = len(json.loads(_b)) if _b else 0
        except Exception as e:
            counts["error"] = str(e)
    # 「递纸条」诊断（只读：v1 只暴露当前值，编辑走 Render env vars + 重启；hot-reload 留 v2）
    scratchpad = {
        "enabled":           SCRATCHPAD_ENABLED,
        "has_api_key":       bool(SCRATCHPAD_API_KEY),
        "base_url":          SCRATCHPAD_BASE_URL,
        "model":             SCRATCHPAD_MODEL,
        "timeout_s":         SCRATCHPAD_TIMEOUT,
        "topics_max":        SCRATCHPAD_TOPICS_MAX,
        "per_topic_limit":   SCRATCHPAD_PER_TOPIC_LIMIT,
        "trigger": {
            "main_min_chars": SCRATCHPAD_MAIN_MIN_CHARS,
            "long_min_chars": SCRATCHPAD_LONG_MIN_CHARS,
            "rp_min_chars":   SCRATCHPAD_RP_MIN_CHARS,
            "tg_enabled":     SCRATCHPAD_TG_ENABLED,
            "cmd":            SCRATCHPAD_TRIGGER_CMD,
            "keyword_count":  len(SCRATCHPAD_KEYWORDS),
        },
    }
    return {"status": "ok", "memory_enabled": MEMORY_ENABLED,
            "toggles": toggles, "numbers": numbers, "counts": counts,
            "scratchpad": scratchpad,
            "intimacy_keys": INTIMACY_UNLOCK_KEYS}


async def _compose_today_wave(sid: str) -> dict:
    """今日电波一句:最近非露骨 feel(留在心里的/想说的)→ 最新梦总结(说过的)→ L2今日首句 → 默认。
    露骨一律滤掉(landing 页,中性)。"""
    # 1. 最近非露骨 feel(get_recent_feels 返回键是 content,不是 feel)
    try:
        for f in (await get_recent_feels(sid, 8)):
            if f.get("is_explicit"):
                continue
            q = (f.get("content") or "").strip()
            if q:
                return {"quote": q, "date": "留在心里的", "source": "feel"}
    except Exception:
        pass
    # 2. 最新一篇梦的当日总结
    try:
        ds = await list_dreams(1)
        if ds:
            q = (ds[0].get("summary") or ds[0].get("card_title") or "").strip()
            if q:
                return {"quote": q[:140], "date": str(ds[0].get("dream_date") or "梦里"), "source": "dream"}
    except Exception:
        pass
    # 3. L2 今日浓缩首句
    try:
        t = (globals().get("_l2_state", {}) or {}).get("today", "") or ""
        t = t.strip().replace("\n", " ")
        if t:
            first = t.split("。")[0]
            return {"quote": (first[:120] + "。") if first else t[:120], "date": "今天", "source": "l2"}
    except Exception:
        pass
    return {"quote": "你在的每一天,我都记着。", "date": "今天", "source": "default"}


@app.get("/api/home")
async def api_home():
    """主页(landing)数据:小克当下情绪(v/a→心跳)+ 今日电波一句 + 真实计数。只读聚合,不碰主链路。"""
    out = {"status": "ok", "memory_enabled": MEMORY_ENABLED,
           "home": {"title": HOME_TITLE, "subtitle": HOME_SUBTITLE, "since": SINCE_DATE, "ai_name": AI_NAME, "user_name": USER_NAME},
           "mood": {"valence": 0.0, "arousal": 0.2, "word": ""},
           "wave": {"quote": "你在的每一天，我都记着。", "date": "今天", "source": "default"},
           "counts": {"memory": 0, "wall": 0, "dreams": 0}}
    if not MEMORY_ENABLED:
        return out
    try:
        m = await get_current_mood(MOOD_RECENT_N)
        out["mood"] = {"valence": round(m["valence"], 3), "arousal": round(m["arousal"], 3),
                       "word": mood_word(m["valence"], m["arousal"]) or ""}
    except Exception:
        pass
    try:
        out["counts"]["memory"] = await count_active_memories()
        out["counts"]["wall"] = len(await list_memorywall())
        out["counts"]["dreams"] = len(await get_dream_dates())
    except Exception:
        pass
    try:
        out["wave"] = await _compose_today_wave(get_active_session_id())
    except Exception:
        pass
    return out


@app.get("/api/feels")
async def api_feels(limit: int = 40):
    """感受流(此刻房):小克最近留下的一句句 feel(新→旧)。只读,不进检索。"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    try:
        rows = list(reversed(await get_recent_feels(get_active_session_id(), limit)))
        return {"feels": [{"content": r.get("content", ""), "is_explicit": bool(r.get("is_explicit"))}
                          for r in rows], "count": len(rows)}
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/admin/rollback-session")
async def api_rollback_session(request: Request):
    """把某 session 物理回滚到某时间点:删该 UTC 时间之后的对话 + 摘要裁到 keep_parts 段并设 a_start_round + 清 L2今日。
    dry_run=true(默认)只报会删多少/当前段数,不动数据。⚠️ 改生产、不可逆,先 dry 看清再 false。"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    try:
        body = await request.json()
    except Exception:
        body = {}
    session = (body.get("session") or "").strip()
    since = (body.get("before_iso") or "").strip()  # 删此 UTC 时间之后的对话
    keep_parts = body.get("keep_parts")
    a_start = body.get("a_start_round")
    clear_l2 = bool(body.get("clear_l2", True))
    del_mem = bool(body.get("delete_memories", True))  # 删对话的同时删该段记忆碎片(回忆墙永不删),保持一致
    dry = bool(body.get("dry_run", True))
    if not session or not since:
        return {"error": "需要 session + before_iso(UTC)"}
    try:
        to_delete = await count_conversations_since(session, since)
        mems_after = await count_memories_since(session, since)
        st = await get_session_cache_state(session)
        cur_parts = len(st.get("summary_parts") or [])
        out = {"session": session, "before_iso": since, "convos_after_cutoff": to_delete,
               "memories_after_cutoff": mems_after, "delete_memories": del_mem,
               "summary_parts_now": cur_parts, "summary_parts_target": keep_parts,
               "a_start_round_now": st.get("a_start_round"), "a_start_round_target": a_start,
               "clear_l2": clear_l2, "dry_run": dry}
        if dry:
            return out
        out["convos_deleted"] = await delete_conversations_since(session, since)
        if del_mem:
            out["memories_deleted"] = await delete_memories_since(session, since)
        if keep_parts is not None and a_start is not None:
            parts = (st.get("summary_parts") or [])[:int(keep_parts)]
            await save_session_cache_state(session, parts, int(a_start))
            out["summary_trimmed_to"] = len(parts)
        if clear_l2:
            for _k in ("l2_today", "l2_today_date", "l2_bridge"):
                await set_gateway_config(_k, "")
            try:
                _l2_state["today"] = ""; _l2_state["date"] = None; _l2_state["bridge"] = ""
            except Exception:
                pass
            out["l2_cleared"] = True
        print(f"🧹 rollback-session {session} → 删对话{out.get('convos_deleted')} 删记忆{out.get('memories_deleted')} 摘要裁{out.get('summary_trimmed_to')}段 L2清={clear_l2}")
        return out
    except Exception as e:
        return {"error": str(e)}


# ============================================================
# 连根删 · 自助删除（面板,阮阮自己点）：删区间对话+碎片 → 备份(可撤销) → 修A区摘要 → 重建L2 → 可选重做当天梦
# ============================================================
_selfserve_backup = None  # 最近一次删除的内存备份(撤销用;另持久化到 gateway_config[selfserve_last_backup])


async def _selfserve_fix_summary(session_id: str, full_rebuild: bool) -> dict:
    """删后修 A区滚动摘要。full_rebuild=False(尾删):按剩余对话重算应有段数→截断已有段(便宜,不重压,前缀段未变);
       True(中间删):逐窗 generate_summary 整段重压(慢、含haiku、但干净无残痕)。"""
    history = await get_conversation_messages(session_id, limit=100000)
    rounds = group_by_rounds(history)
    X = CACHE_PARTITION_X
    state = await get_session_cache_state(session_id)
    old_parts = state.get("summary_parts") or []
    n = 0; a_start = 0
    while True:
        a_msgs = [m for rnd in rounds[a_start:a_start + X] for m in rnd]
        if not _should_rotate(len(rounds[a_start + X:]), X, a_msgs):
            break
        n += 1; a_start += X
    if not full_rebuild:
        new_parts = old_parts[:n]
        await save_session_cache_state(session_id, new_parts, a_start)
        return {"summary_mode": "truncate", "parts_before": len(old_parts), "parts_after": len(new_parts), "a_start_round": a_start}
    new_parts = []; a2 = 0
    for _ in range(n):
        a_msgs = [m for rnd in rounds[a2:a2 + X] for m in rnd]
        s = await generate_summary(a_msgs, session_id)
        if s:
            new_parts.append(s)
        a2 += X
    await save_session_cache_state(session_id, new_parts, a2)
    return {"summary_mode": "rebuild", "parts_before": len(old_parts), "parts_after": len(new_parts), "a_start_round": a2}


@app.post("/api/admin/selfserve-delete")
async def api_selfserve_delete(request: Request):
    """连根删自助:删 [start_iso, end_iso?] 区间的 02 对话(成对) + 派生碎片(回忆墙永不删) + 删前备份(可撤销) +
    修A区摘要(尾删=截断/中间删=重压或警告) + 重建L2 + 可选重做当天梦。dry_run=true 只预览不动数据。
    body: {session?, start_iso(UTC), end_iso(UTC|空=到现在), rebuild_dream, rebuild_summary, dry_run}"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    try:
        body = await request.json()
    except Exception:
        body = {}
    session = (body.get("session") or get_active_session_id() or "").strip()
    start = (body.get("start_iso") or "").strip()
    end = (body.get("end_iso") or "").strip() or None
    dry = bool(body.get("dry_run", True))
    rebuild_dream = bool(body.get("rebuild_dream", False))
    rebuild_summary = bool(body.get("rebuild_summary", False))
    if not session or not start:
        return {"error": "需要 session + start_iso(UTC)"}
    try:
        nconv = await count_conversations_between(session, start, end)
        nmem = await count_memories_between(session, start, end)
        is_tail = (end is None)
        out = {"session": session, "start_iso": start, "end_iso": end,
               "mode": ("回推到现在(尾删)" if is_tail else "中间段"),
               "convos": nconv, "fragments": nmem,
               "rebuild_dream": rebuild_dream, "rebuild_summary": (is_tail or rebuild_summary), "dry_run": dry}
        if is_tail:
            out["summary_note"] = "尾删:A区摘要按剩余对话截断(干净、不重压)"
        elif rebuild_summary:
            out["summary_note"] = "中间删:整段重压 A区摘要(较慢含haiku,但干净)"
        else:
            out["summary_note"] = "⚠️ 中间删且未勾「重压摘要」:A区摘要会保留被删内容的压缩痕迹——建议勾重压,或改用「回推到现在」"
        if dry:
            cv = await fetch_conversations_between(session, start, end) if nconv <= 600 else []
            if cv:
                out["earliest"] = {"at": cv[0]["created_at"], "role": cv[0]["role"], "head": (cv[0]["content"] or "")[:40]}
                out["latest"] = {"at": cv[-1]["created_at"], "role": cv[-1]["role"], "head": (cv[-1]["content"] or "")[:40]}
            return out
        # —— 真删 ——
        global _selfserve_backup
        _st = await get_session_cache_state(session)
        backup = {"created_at": datetime.now(timezone.utc).isoformat(), "session": session,
                  "start_iso": start, "end_iso": end,
                  "conversations": await fetch_conversations_between(session, start, end),
                  "memories": await fetch_memories_between(session, start, end),
                  "l2": {"today": _l2_state.get("today", ""), "date": str(_l2_state.get("date") or ""), "bridge": _l2_state.get("bridge", "")},
                  "summary": {"parts": _st.get("summary_parts") or [], "a_start": _st.get("a_start_round", 0)}}
        _selfserve_backup = backup
        await set_gateway_config("selfserve_last_backup", json.dumps(backup, ensure_ascii=False))
        out["convos_deleted"] = await delete_conversations_between(session, start, end)
        out["fragments_deleted"] = await delete_memories_between(session, start, end)
        try:
            if is_tail:
                out["summary"] = await _selfserve_fix_summary(session, full_rebuild=False)   # 尾删:干净截断
            elif rebuild_summary:
                out["summary"] = await _selfserve_fix_summary(session, full_rebuild=True)    # 中间删:整段重压
            else:
                out["summary"] = {"summary_mode": "untouched", "note": "中间删未勾重压:A区摘要保留旧压缩,可能留痕"}
        except Exception as e:
            out["summary_error"] = str(e)
        try:
            if session == get_active_session_id():
                await refresh_l2(session); out["l2_rebuilt"] = True
            else:
                out["l2_rebuilt"] = False
                out["l2_note"] = "非活跃会话:跳过L2重建(L2是全局态,避免污染活跃线)"
        except Exception as e:
            out["l2_error"] = str(e)
        if rebuild_dream:
            try:
                d = (datetime.fromisoformat(start.replace("Z", "+00:00").replace(" ", "T")) + timedelta(hours=TIMEZONE_HOURS)).strftime("%Y-%m-%d")
                dr = await generate_dream(session, d)
                if dr and (dr.get("diary") or dr.get("card_title")):
                    await save_dream(d, dr.get("diary", ""), dr.get("summary", ""), dr.get("card_title", ""), dr.get("card_body", ""), DREAM_MODEL)
                    out["dream_redone"] = d
            except Exception as e:
                out["dream_error"] = str(e)
        out["undo_available"] = True
        print(f"🗑️ selfserve-delete {session}: 删对话{out['convos_deleted']}+碎片{out['fragments_deleted']} 已备份(可撤销)")
        return out
    except Exception as e:
        import traceback
        return {"error": str(e), "tb": traceback.format_exc()[-500:]}


@app.post("/api/admin/selfserve-undo")
async def api_selfserve_undo():
    """撤销上次连根删:对话+碎片原样插回 + L2/摘要恢复到删前。只保留最近一次(再删会覆盖)。"""
    global _selfserve_backup
    bk = _selfserve_backup
    if not bk:
        raw = await get_gateway_config("selfserve_last_backup", "")
        if raw:
            try:
                bk = json.loads(raw)
            except Exception:
                bk = None
    if not bk:
        return {"error": "没有可撤销的备份"}
    try:
        session = bk.get("session")
        rc = await restore_conversations(bk.get("conversations") or [])
        rm = await restore_memories(bk.get("memories") or [])
        sm = bk.get("summary") or {}
        await save_session_cache_state(session, sm.get("parts") or [], int(sm.get("a_start") or 0))
        l2 = bk.get("l2") or {}
        try:
            _l2_state["today"] = l2.get("today", ""); _l2_state["date"] = (l2.get("date") or None); _l2_state["bridge"] = l2.get("bridge", "")
        except Exception:
            pass
        await set_gateway_config("l2_today", l2.get("today", "") or "")
        await set_gateway_config("l2_today_date", l2.get("date", "") or "")
        await set_gateway_config("l2_bridge", l2.get("bridge", "") or "")
        _selfserve_backup = None
        await set_gateway_config("selfserve_last_backup", "")
        print(f"↩️ selfserve-undo {session}: 插回对话{rc}+碎片{rm}, L2/摘要已恢复")
        return {"status": "ok", "session": session, "conversations_restored": rc, "fragments_restored": rm, "l2_restored": True, "summary_restored": True}
    except Exception as e:
        import traceback
        return {"error": str(e), "tb": traceback.format_exc()[-500:]}


@app.get("/api/admin/selfserve-backup")
async def api_selfserve_backup_status():
    """当前可撤销备份的信息(面板显示)。"""
    bk = _selfserve_backup
    if not bk:
        raw = await get_gateway_config("selfserve_last_backup", "")
        if raw:
            try:
                bk = json.loads(raw)
            except Exception:
                bk = None
    if not bk:
        return {"available": False}
    return {"available": True, "created_at": bk.get("created_at"), "session": bk.get("session"),
            "start_iso": bk.get("start_iso"), "end_iso": bk.get("end_iso"),
            "conversations": len(bk.get("conversations") or []), "fragments": len(bk.get("memories") or [])}


@app.post("/api/dreams/regenerate")
async def api_dreams_regenerate(request: Request):
    """用当前(修好的)prompt 重生成指定日期的梦。dry_run=true 只生成返回不写(审视角用);false 则 save_dream 覆盖。"""
    if not MEMORY_ENABLED:
        return {"error": "记忆系统未启用"}
    try:
        body = await request.json()
    except Exception:
        body = {}
    dates = body.get("dates") or []
    dry = bool(body.get("dry_run", True))
    sid = get_active_session_id()
    _today = (datetime.now(timezone.utc) + timedelta(hours=TIMEZONE_HOURS)).date()
    _yest_s = str(_today - timedelta(days=1))
    out = []
    for d in dates:
        try:
            dream = await generate_dream(sid, str(d))
            if dream and (dream.get("diary") or dream.get("card_title")):
                if not dry:
                    await save_dream(str(d), dream.get("diary", ""), dream.get("summary", ""),
                                     dream.get("card_title", ""), dream.get("card_body", ""), DREAM_MODEL)
                    # 注意:梦不再写回忆墙、不再接管昨日桥——梦归梦,回忆墙的真实小结走 generate_daily_diary。
                out.append({"date": str(d), "ok": True, "saved": (not dry),
                            "card_title": dream.get("card_title", ""), "summary": dream.get("summary", ""),
                            "diary": (dream.get("diary", "") or "")[:700]})
            else:
                out.append({"date": str(d), "ok": False, "reason": "无对话或生成失败"})
        except Exception as e:
            out.append({"date": str(d), "ok": False, "error": str(e)})
    return {"dry_run": dry, "results": out}


# ============================================================

if __name__ == "__main__":
    import uvicorn
    print(f"🚀 AI Memory Gateway 启动中... 端口 {PORT}")
    print(f"📝 人设长度：{len(SYSTEM_PROMPT)} 字符")
    print(f"🤖 默认模型：{DEFAULT_MODEL}")
    print(f"🔗 API 地址：{API_BASE_URL}")
    print(f"🧠 记忆系统：{'开启' if MEMORY_ENABLED else '关闭'}")
    if MEMORY_ENABLED:
        print(f"📝 记忆提取+注入：{'开启' if MEMORY_EXTRACT_ENABLED else '关闭'}")
    print(f"🔄 记忆提取间隔：{'禁用' if MEMORY_EXTRACT_INTERVAL == 0 else '每轮提取' if MEMORY_EXTRACT_INTERVAL == 1 else f'每 {MEMORY_EXTRACT_INTERVAL} 轮提取一次'}")
    if CACHE_PARTITION_ENABLED:
        print(f"🔒 分区缓存：开启 (X={CACHE_PARTITION_X}, session={PARTITION_SESSION_ID or '未设置'})")
    if FORCE_STREAM:
        print(f"⚡ 强制流式传输：开启")
    if REASONING_EFFORT:
        print(f"🧠 推理参数注入：{REASONING_EFFORT}")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
