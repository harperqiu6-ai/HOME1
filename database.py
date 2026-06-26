"""
数据库模块 —— 负责所有跟 PostgreSQL 打交道的事情
==============================================
包括：
- 创建表结构
- 存储对话记录
- 存储/检索记忆（带中文分词和加权排序）
"""

import os
import re
import json
from typing import Optional, List
from datetime import datetime, timedelta, timezone as dt_timezone

import asyncpg

# 时区偏移（和 main.py 保持一致）
TIMEZONE_HOURS = int(os.getenv("TIMEZONE_HOURS", "8"))

DATABASE_URL = os.getenv("DATABASE_URL", "")

HAS_PGVECTOR = False  # 在init_tables时检测

# Embedding 配置（向量搜索用）
EMBEDDING_API_KEY = os.getenv("EMBEDDING_API_KEY", "")
EMBEDDING_BASE_URL = os.getenv("EMBEDDING_BASE_URL", "https://api.openai.com/v1")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", "256"))

# 记忆向量搜索开关（需要同时设置 EMBEDDING_API_KEY）
MEMORY_VECTOR_ENABLED = os.getenv("MEMORY_VECTOR_ENABLED", "false").lower() == "true"

# 记忆搜索权重（纯关键词模式）
WEIGHT_KEYWORD = float(os.getenv("WEIGHT_KEYWORD", "0.5"))
WEIGHT_IMPORTANCE = float(os.getenv("WEIGHT_IMPORTANCE", "0.3"))
WEIGHT_RECENCY = float(os.getenv("WEIGHT_RECENCY", "0.2"))
MIN_SCORE_THRESHOLD = float(os.getenv("MIN_SCORE_THRESHOLD", "0.15"))
SEARCH_COMMON_FRAC = float(os.getenv("SEARCH_COMMON_FRAC", "0.10"))  # 关键词 df 超过语料这一占比=高频噪声→从匹配集剔除（IDF 降噪，自适应，不维护停用词表）

# A: 时间指示词（闭集，不会长大）——不参与字面 token 匹配，时间召回交给 recency 逻辑。
# 否则"今天"会精准撞上"阮阮今天…"那批当天记忆；且 IDF 因其语料稀有反而把它当宝贝保留。
TIME_DEIXIS = {
    "今天", "昨天", "明天", "后天", "前天", "大前天", "大后天",
    "今早", "今晚", "今夜", "昨晚", "昨夜", "明早", "明晚", "昨儿", "明儿",
    "现在", "此刻", "当下", "眼下", "刚才", "方才", "这会儿", "待会儿", "一会儿",
    "早上", "早晨", "清晨", "上午", "中午", "晌午", "下午", "傍晚", "晚上", "夜里", "夜晚", "半夜", "凌晨", "白天",
    "今", "昨", "晨", "凌",  # 纯时间指示的单字（IDF 会误当稀有词保留）
}

# 记忆混合搜索权重（MEMORY_VECTOR_ENABLED=true 时生效）
MEMORY_HW_KEYWORD = float(os.getenv("MEMORY_HW_KEYWORD", "0.35"))
MEMORY_HW_SEMANTIC = float(os.getenv("MEMORY_HW_SEMANTIC", "0.35"))
MEMORY_HW_IMPORTANCE = float(os.getenv("MEMORY_HW_IMPORTANCE", "0.15"))
MEMORY_HW_RECENCY = float(os.getenv("MEMORY_HW_RECENCY", "0.15"))
MEMORY_SEMANTIC_THRESHOLD = float(os.getenv("MEMORY_SEMANTIC_THRESHOLD", "0.5"))


# ============================================================
# 连接池管理
# ============================================================

_pool: Optional[asyncpg.Pool] = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL 未设置！")
        _pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5, statement_cache_size=0)
        print("✅ 数据库连接池已创建")
    return _pool


async def close_pool():
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
        print("✅ 数据库连接池已关闭")


# ============================================================
# 表结构初始化
# ============================================================

async def init_tables():
    global HAS_PGVECTOR
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                id              SERIAL PRIMARY KEY,
                session_id      TEXT NOT NULL,
                role            TEXT NOT NULL,
                content         TEXT,
                model           TEXT,
                created_at      TIMESTAMPTZ DEFAULT NOW(),
                metadata        TEXT
            );
        """)
        
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id              SERIAL PRIMARY KEY,
                content         TEXT NOT NULL,
                importance      INTEGER DEFAULT 5,
                source_session  TEXT,
                created_at      TIMESTAMPTZ DEFAULT NOW(),
                last_accessed   TIMESTAMPTZ DEFAULT NOW()
            );
        """)
        
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_memories_fts 
            ON memories 
            USING gin(to_tsvector('simple', content));
        """)
        
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_conversations_session 
            ON conversations (session_id, created_at);
        """)
        
        # 工具调用支持：加 metadata 字段（已有表自动迁移）
        await conn.execute("""
            DO $$ BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'conversations' AND column_name = 'metadata'
                ) THEN
                    ALTER TABLE conversations ADD COLUMN metadata TEXT;
                END IF;
            END $$;
        """)
        
        # content 允许 NULL（工具调用时 assistant 的 content 可能为空）
        await conn.execute("""
            ALTER TABLE conversations ALTER COLUMN content DROP NOT NULL;
        """)
        
        # 网关配置表（存储运行时可变配置）
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS gateway_config (
                key     TEXT PRIMARY KEY,
                value   TEXT DEFAULT ''
            );
        """)
        
        # 分区缓存状态表（存储每个session的轮转状态）
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS session_cache_state (
                session_id      TEXT PRIMARY KEY,
                summary         TEXT DEFAULT '',
                a_start_round   INTEGER DEFAULT 0,
                updated_at      TIMESTAMPTZ DEFAULT NOW()
            );
        """)
        # 滚动摘要封顶：早期小结（卷掉的老段压成一块，进缓存前缀）
        await conn.execute("""
            DO $$ BEGIN
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='session_cache_state' AND column_name='early_summary') THEN
                    ALTER TABLE session_cache_state ADD COLUMN early_summary TEXT DEFAULT '';
                END IF;
            END $$;
        """)
        
        # ---- 三层记忆架构字段（layer / title / is_active / merged_from / event_date）----
        # layer: 1=原始碎片, 2=事件记忆, 3=核心记忆
        await conn.execute("""
            DO $$ BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'memories' AND column_name = 'layer'
                ) THEN
                    ALTER TABLE memories ADD COLUMN layer INTEGER DEFAULT 1;
                END IF;
            END $$;
        """)
        
        # title: 记忆标题（语义锚点，用于搜索加权）
        await conn.execute("""
            DO $$ BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'memories' AND column_name = 'title'
                ) THEN
                    ALTER TABLE memories ADD COLUMN title TEXT DEFAULT NULL;
                END IF;
            END $$;
        """)
        
        # is_active: 是否参与搜索（碎片合并后变为 false）
        await conn.execute("""
            DO $$ BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'memories' AND column_name = 'is_active'
                ) THEN
                    ALTER TABLE memories ADD COLUMN is_active BOOLEAN DEFAULT TRUE;
                END IF;
            END $$;
        """)

        # 情绪① 情感坐标（Russell circumplex）：valence 效价 -1~+1 / arousal 唤醒 0~1
        await conn.execute("""
            DO $$ BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'memories' AND column_name = 'valence'
                ) THEN
                    ALTER TABLE memories ADD COLUMN valence REAL DEFAULT 0;
                END IF;
            END $$;
        """)
        await conn.execute("""
            DO $$ BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'memories' AND column_name = 'arousal'
                ) THEN
                    ALTER TABLE memories ADD COLUMN arousal REAL DEFAULT 0.2;
                END IF;
            END $$;
        """)

        # is_explicit：露骨/私密标记。命中时不注入原文场景，收敛成一句定向指令（默认 FALSE，提取/回填打标）
        await conn.execute("""
            DO $$ BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'memories' AND column_name = 'is_explicit'
                ) THEN
                    ALTER TABLE memories ADD COLUMN is_explicit BOOLEAN DEFAULT FALSE;
                END IF;
            END $$;
        """)

        # ②衰减归档标记：decayed_at 非空=被衰减归档(is_active=FALSE)。用于让 cleanup_old_fragments 豁免它们(归档≠删除,记忆不能丢)。
        await conn.execute("""
            DO $$ BEGIN
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'memories' AND column_name = 'decayed_at') THEN
                    ALTER TABLE memories ADD COLUMN decayed_at TIMESTAMPTZ DEFAULT NULL;
                END IF;
            END $$;
        """)

        # 情绪①-第二步 心情漂移：每条记忆每日漂移次数（防跑飞的「每条每日封顶」用；这两列仅作计数，不碰正文/importance/日期）
        await conn.execute("""
            DO $$ BEGIN
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'memories' AND column_name = 'drift_day') THEN
                    ALTER TABLE memories ADD COLUMN drift_day DATE DEFAULT NULL;
                END IF;
            END $$;
        """)
        await conn.execute("""
            DO $$ BEGIN
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'memories' AND column_name = 'drift_today') THEN
                    ALTER TABLE memories ADD COLUMN drift_today SMALLINT DEFAULT 0;
                END IF;
            END $$;
        """)

        # merged_from: 合并来源的碎片ID列表
        await conn.execute("""
            DO $$ BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'memories' AND column_name = 'merged_from'
                ) THEN
                    ALTER TABLE memories ADD COLUMN merged_from INTEGER[] DEFAULT NULL;
                END IF;
            END $$;
        """)
        
        # event_date: 事件日期（用于按天整理）
        await conn.execute("""
            DO $$ BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'memories' AND column_name = 'event_date'
                ) THEN
                    ALTER TABLE memories ADD COLUMN event_date DATE DEFAULT NULL;
                END IF;
            END $$;
        """)
        
        # 三层记忆索引
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_memories_layer ON memories (layer);
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_memories_active ON memories (is_active);
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_memories_event_date ON memories (event_date);
        """)

        # ---- 回忆墙迁移字段：结构化保存原始字段，不压扁成纯文本 ----
        # mw_meta JSONB: {original_id, date, author, author_cn, mood, source, is_period_day, location, title, photos:[{photo_id,original_name,mime,url}]}
        await conn.execute("""
            DO $$ BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'memories' AND column_name = 'mw_meta'
                ) THEN
                    ALTER TABLE memories ADD COLUMN mw_meta JSONB DEFAULT NULL;
                END IF;
            END $$;
        """)
        # 照片二进制长期存储（随网关 Postgres 一起持久化，独立于回忆墙服务器）
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS memory_photos (
                id            SERIAL PRIMARY KEY,
                memory_id     INTEGER,
                original_name TEXT,
                mime          TEXT DEFAULT 'image/png',
                data          BYTEA NOT NULL,
                created_at    TIMESTAMPTZ DEFAULT NOW()
            );
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_memory_photos_memory ON memory_photos (memory_id);
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_memory_photos_md5 ON memory_photos (md5(data));
        """)

        # 人设建议（A4）：提取识别出的"行为/相处偏好"不进记忆池，收集到这里供主理人贴 persona
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS persona_suggestions (
                id             SERIAL PRIMARY KEY,
                content        TEXT NOT NULL,
                source_session TEXT,
                status         TEXT DEFAULT 'pending',
                created_at     TIMESTAMPTZ DEFAULT NOW()
            );
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_persona_suggestions_status ON persona_suggestions (status, created_at DESC);
        """)

        # ② L5根基候选（里程碑待审队列；机器只增、阮阮审核确认后才进 l5Foundation 正文）
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS l5_candidates (
                id             SERIAL PRIMARY KEY,
                content        TEXT NOT NULL,
                event_date     DATE,
                source_session TEXT,
                status         TEXT DEFAULT 'pending',
                created_at     TIMESTAMPTZ DEFAULT NOW()
            );
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_l5_candidates_status ON l5_candidates (status, created_at DESC);
        """)
        # 里程碑候选去向：'l5'(→根基) 或 'wall'(→回忆墙)。封顶卷制时检出的里程碑入此队列待审。
        await conn.execute("""
            DO $$ BEGIN
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='l5_candidates' AND column_name='target') THEN
                    ALTER TABLE l5_candidates ADD COLUMN target TEXT DEFAULT 'l5';
                END IF;
            END $$;
        """)

        # ③-2 做梦：每个过去日一篇第一人称日记 + 当日总结(给昨日桥) + 卡片。dream_date 唯一(幂等)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS dreams (
                id             SERIAL PRIMARY KEY,
                dream_date     DATE UNIQUE,
                diary          TEXT,
                summary        TEXT,
                card_title     TEXT,
                card_body      TEXT,
                model          TEXT,
                created_at     TIMESTAMPTZ DEFAULT NOW()
            );
        """)

        # ③-1 feel：一句话"留在你心里的感受"。单独存、不衰减(不进 apply_mood_drift)；is_explicit 供注入收敛复用
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS feels (
                id             SERIAL PRIMARY KEY,
                session_id     TEXT,
                content        TEXT,
                is_explicit    BOOLEAN DEFAULT FALSE,
                created_at     TIMESTAMPTZ DEFAULT NOW()
            );
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_feels_session ON feels (session_id, created_at DESC);
        """)

        # 尝试启用pgvector扩展（向量搜索）
        try:
            await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            HAS_PGVECTOR = True
            print("✅ pgvector扩展已启用")
            
            # 对话表向量列
            await conn.execute(f"""
                DO $$ BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name = 'conversations' AND column_name = 'embedding'
                    ) THEN
                        ALTER TABLE conversations ADD COLUMN embedding vector({EMBEDDING_DIM});
                    END IF;
                END $$;
            """)
            
            # 记忆表向量列
            await conn.execute(f"""
                DO $$ BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name = 'memories' AND column_name = 'embedding'
                    ) THEN
                        ALTER TABLE memories ADD COLUMN embedding vector({EMBEDDING_DIM});
                    END IF;
                END $$;
            """)
            try:
                await conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_memories_embedding 
                    ON memories USING ivfflat (embedding vector_cosine_ops)
                    WITH (lists = 10);
                """)
            except Exception:
                pass  # ivfflat需要一定行数才能建索引，初期跳过
        except Exception as e:
            HAS_PGVECTOR = False
            print(f"⚠️ pgvector不可用（{e}），向量搜索将使用Python端计算")
            
            # 回退：用TEXT列存JSON格式的向量
            await conn.execute("""
                DO $$ BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name = 'conversations' AND column_name = 'embedding_json'
                    ) THEN
                        ALTER TABLE conversations ADD COLUMN embedding_json TEXT;
                    END IF;
                END $$;
            """)
            await conn.execute("""
                DO $$ BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name = 'memories' AND column_name = 'embedding_json'
                    ) THEN
                        ALTER TABLE memories ADD COLUMN embedding_json TEXT;
                    END IF;
                END $$;
            """)
    
    print("✅ 数据库表结构已就绪")


# ============================================================
# 中文分词工具（基于 jieba）
# ============================================================

import math
import jieba
import jieba.analyse

# 静默加载词典
jieba.setLogLevel(jieba.logging.INFO)

EN_WORD_PATTERN = re.compile(r'[a-zA-Z][a-zA-Z0-9]*')
NUM_PATTERN = re.compile(r'\d{2,}')
# 单个中文字符（CJK 统一表意文字）
CJK_CHAR_PATTERN = re.compile(r'[一-鿿]')
# 清理查询开头的时间戳（如 "2026-05-02 20:26"）
TIMESTAMP_PATTERN = re.compile(r'^\d{4}[-/.]\d{1,2}[-/.]\d{1,2}\s*\d{1,2}:\d{1,2}\s*')

# 中文停用词（高频但无搜索价值的词）
_STOP_WORDS = frozenset({
    "的", "了", "在", "是", "我", "你", "他", "她", "它", "们",
    "这", "那", "有", "和", "与", "也", "都", "又", "就", "但",
    "而", "或", "到", "被", "把", "让", "从", "对", "为", "以",
    "及", "等", "个", "不", "没", "很", "太", "吗", "呢", "吧",
    "啊", "嗯", "哦", "哈", "呀", "嘛", "么", "啦", "哇", "喔",
    "会", "能", "要", "想", "去", "来", "说", "做", "看", "给",
    "上", "下", "里", "中", "大", "小", "多", "少", "好", "可以",
    "什么", "怎么", "如何", "哪里", "哪个", "为什么", "还是",
    "然后", "因为", "所以", "虽然", "但是", "可以", "已经",
    "一个", "一些", "一下", "一点", "一起", "一样",
    "比较", "应该", "可能", "如果", "这个", "那个",
    "自己", "知道", "觉得", "感觉", "时候", "现在",
})

# jieba 用户词典补充（默认词典缺失的词）
for _w in ["手账", "手帐", "搭子", "种草", "拔草", "安利", "内卷", "摆烂", "emo", "网关"]:
    jieba.add_word(_w)


def extract_search_keywords(query: str) -> List[str]:
    """
    从查询中提取搜索关键词（TF-IDF + 正则）

    1. 去掉开头的时间戳噪音
    2. 用 jieba.analyse.extract_tags (TF-IDF) 提取中文关键词
    3. 正则提取英文单词
    4. 保留4位以上数字（年份等，过滤短数字噪音）

    例如：
    "2026-05-02 20:26 写写手账看看书 放松大脑" → ["手账", "放松", "大脑"]
    "我昨天在手机上部署了Render然后吃了晚饭" → ["手机", "部署", "Render", "晚饭"]
    "春节干了什么" → ["春节"]
    "2026除夕"    → ["2026", "除夕"]
    """
    # 去掉时间戳前缀
    cleaned = TIMESTAMP_PATTERN.sub('', query).strip()
    if not cleaned:
        cleaned = query

    keywords = set()

    # 英文单词（2字符以上）
    for match in EN_WORD_PATTERN.finditer(cleaned):
        word = match.group()
        if len(word) >= 2:
            keywords.add(word)

    # 数字串（只保留4位以上，过滤 "05" "20" 这种时间噪音）
    for match in NUM_PATTERN.finditer(cleaned):
        num = match.group()
        if len(num) >= 4:
            keywords.add(num)

    # TF-IDF 关键词提取（比手动分词+停用词好很多）
    tags = jieba.analyse.extract_tags(cleaned, topK=10)
    for tag in tags:
        # 跳过纯英文/数字（已在上面处理）
        if EN_WORD_PATTERN.fullmatch(tag) or NUM_PATTERN.fullmatch(tag):
            continue
        if tag in _STOP_WORDS:
            continue
        keywords.add(tag)

    # 补救单字召回：jieba 的精确/全模式都会让词典词（如“猫叫”）吞掉其中的单字
    # “猫”，使得 "我家猫叫什么" 根本提取不到 "猫"、检索为空（即使记忆写着“布偶猫”）。
    # 对较短的查询（典型的提问句）直接按字补：把不在停用词表里的单字中文也作为
    # 关键词，确保 "猫"/"狗"/"书" 一定能召回。长文本不做（已有多字词覆盖，避免单字噪声）。
    cjk_singles = [c for c in cleaned if CJK_CHAR_PATTERN.match(c) and c not in _STOP_WORDS]
    if len(cjk_singles) <= 20:
        keywords.update(cjk_singles)

    return list(keywords)


# ============================================================
# 向量搜索（OpenAI 兼容 Embedding API）
# ============================================================

async def compute_embedding(text: str) -> list:
    """调用 OpenAI 兼容的 Embedding API 计算文本向量"""
    if not EMBEDDING_API_KEY:
        return []
    
    try:
        import httpx
        
        if len(text) > 4000:
            text = text[:4000]
        
        body = {
            "model": EMBEDDING_MODEL,
            "input": text,
        }
        if EMBEDDING_DIM > 0:
            body["dimensions"] = EMBEDDING_DIM
        
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{EMBEDDING_BASE_URL}/embeddings",
                headers={
                    "Authorization": f"Bearer {EMBEDDING_API_KEY}",
                    "Content-Type": "application/json",
                },
                json=body,
                timeout=30.0,
            )
            resp.raise_for_status()
            data = resp.json()
            return data["data"][0]["embedding"]
    except Exception as e:
        print(f"⚠️ Embedding计算失败: {e}")
        return []


async def save_memory_embedding(conn, memory_id: int, embedding: list):
    """保存记忆向量到memories表"""
    if not embedding:
        return
    
    if HAS_PGVECTOR:
        vec_str = '[' + ','.join(str(f) for f in embedding) + ']'
        await conn.execute(
            "UPDATE memories SET embedding = $1::vector WHERE id = $2",
            vec_str, memory_id
        )
    else:
        import json
        await conn.execute(
            "UPDATE memories SET embedding_json = $1 WHERE id = $2",
            json.dumps(embedding), memory_id
        )


def _cosine_sim(a, b):
    """余弦相似度（纯Python）"""
    import math
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0
    return dot / (norm_a * norm_b)


def _min_max_normalize(scores: dict) -> dict:
    """min-max归一化到0-1"""
    if not scores:
        return {}
    vals = list(scores.values())
    min_v, max_v = min(vals), max(vals)
    spread = max_v - min_v
    if spread == 0:
        return {k: 1.0 for k in scores}
    return {k: (v - min_v) / spread for k, v in scores.items()}


# ============================================================
# 对话记录操作
# ============================================================

async def save_message(session_id: str, role: str, content: str, model: str = "", metadata: str = None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO conversations (session_id, role, content, model, metadata) VALUES ($1, $2, $3, $4, $5)",
            session_id, role, content, model, metadata,
        )


async def get_last_user_content(session_id: str) -> str:
    """获取指定session最后一条user消息的content"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT content FROM conversations
            WHERE session_id = $1 AND role = 'user'
            ORDER BY created_at DESC
            LIMIT 1
        """, session_id)
        return row['content'] if row else ""


async def update_last_assistant_message(session_id: str, new_content: str, model: str = ""):
    """覆盖指定session最后一条assistant消息的content（用于re-roll去重）"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT id FROM conversations
            WHERE session_id = $1 AND role = 'assistant'
            ORDER BY created_at DESC
            LIMIT 1
        """, session_id)
        if row:
            await conn.execute(
                "UPDATE conversations SET content = $1, model = $2 WHERE id = $3",
                new_content, model, row['id']
            )
            return True
        return False


async def get_recent_messages(session_id: str, limit: int = 20):
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT role, content, metadata, created_at FROM conversations WHERE session_id = $1 ORDER BY created_at DESC LIMIT $2",
            session_id, limit,
        )
        return list(reversed(rows))


async def search_conversations(query: str, limit: int = 20, offset: int = 0):
    """搜索对话内容，返回匹配的session列表"""
    keywords = extract_search_keywords(query)
    if not keywords:
        return [], 0
    
    pool = await get_pool()
    async with pool.acquire() as conn:
        where_parts = []
        params = []
        for i, kw in enumerate(keywords):
            where_parts.append(f"c.content ILIKE '%' || ${i+1} || '%'")
            params.append(kw)
        where_clause = " OR ".join(where_parts)
        
        count_sql = f"""
            SELECT COUNT(DISTINCT c.session_id) as total
            FROM conversations c
            WHERE {where_clause}
        """
        total_row = await conn.fetchrow(count_sql, *params)
        total = total_row['total'] if total_row else 0
        
        if total == 0:
            return [], 0
        
        limit_idx = len(params) + 1
        offset_idx = len(params) + 2
        params.extend([limit, offset])
        
        sql = f"""
            WITH matched_sessions AS (
                SELECT DISTINCT c.session_id
                FROM conversations c
                WHERE {where_clause}
            ),
            session_info AS (
                SELECT 
                    ms.session_id,
                    MIN(c.created_at) as first_time,
                    MAX(c.created_at) as last_time,
                    COUNT(*) as message_count
                FROM matched_sessions ms
                JOIN conversations c ON c.session_id = ms.session_id
                GROUP BY ms.session_id
            )
            SELECT 
                si.session_id,
                si.first_time,
                si.last_time,
                si.message_count
            FROM session_info si
            ORDER BY si.last_time DESC
            LIMIT ${limit_idx} OFFSET ${offset_idx}
        """
        rows = await conn.fetch(sql, *params)
        
        results = []
        for r in rows:
            results.append({
                'session_id': r['session_id'],
                'first_time': r['first_time'].isoformat() if r['first_time'] else None,
                'last_time': r['last_time'].isoformat() if r['last_time'] else None,
                'message_count': r['message_count'],
            })
        
        return results, total


async def update_message_content(message_id: int, new_content: str):
    """更新单条对话消息的内容"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE conversations SET content = $1 WHERE id = $2",
            new_content, message_id,
        )
        return int(result.split()[-1]) if result else 0


# ============================================================
# 记忆操作
# ============================================================

async def save_memory(content: str, importance: int = 5, source_session: str = "", valence: float = 0.0, arousal: float = 0.2, is_explicit: bool = False):
    pool = await get_pool()
    async with pool.acquire() as conn:
        # 情绪① 夹紧到合法范围（arousal 默认 0.2 兼作地板，避免后续衰减乘到 0 退化）
        _val = max(-1.0, min(1.0, float(valence if valence is not None else 0.0)))
        _aro = max(0.0, min(1.0, float(arousal if arousal is not None else 0.2)))
        row = await conn.fetchrow(
            "INSERT INTO memories (content, importance, source_session, valence, arousal, is_explicit) VALUES ($1, $2, $3, $4, $5, $6) RETURNING id",
            content, importance, source_session, _val, _aro, bool(is_explicit),
        )
        
        # MEMORY_VECTOR_ENABLED 时自动计算 embedding
        if MEMORY_VECTOR_ENABLED and row:
            try:
                embedding = await compute_embedding(content)
                if embedding:
                    await save_memory_embedding(conn, row['id'], embedding)
            except Exception as e:
                print(f"⚠️ 记忆 {row['id']} embedding自动计算失败: {e}")


async def get_long_memories(min_len: int = 300, limit: int = 500):
    """取活跃、非回忆墙、超长的普通记忆(供拆分)。按长度降序。返回 [{id, content, importance, valence, arousal, is_explicit, created_at}]"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, content, importance, valence, arousal, is_explicit, created_at
               FROM memories
               WHERE is_active = TRUE AND mw_meta IS NULL
                 AND content IS NOT NULL AND char_length(btrim(content)) > $1
               ORDER BY char_length(content) DESC
               LIMIT $2""",
            min_len, limit)
        return [dict(r) for r in rows]


async def split_memory_into(original_id: int, contents: list) -> list:
    """把 original_id 拆成多条新短记忆：继承原 importance/情绪/**创建日期**，各自算向量，
    并用 merged_from 记录来源；原记忆置 is_active=FALSE(软停用，可撤销)。返回新 id 列表。"""
    pool = await get_pool()
    new_ids = []
    async with pool.acquire() as conn:
        orig = await conn.fetchrow(
            "SELECT importance, source_session, valence, arousal, is_explicit, created_at FROM memories WHERE id=$1",
            original_id)
        if not orig:
            return []
        for c in contents:
            c = (c or "").strip()
            if not c:
                continue
            row = await conn.fetchrow(
                """INSERT INTO memories (content, importance, source_session, valence, arousal, is_explicit, created_at, merged_from)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8) RETURNING id""",
                c, orig["importance"], orig["source_session"], orig["valence"], orig["arousal"],
                bool(orig["is_explicit"]), orig["created_at"], [original_id])
            nid = row["id"]
            new_ids.append(nid)
            if MEMORY_VECTOR_ENABLED:
                try:
                    emb = await compute_embedding(c)
                    if emb:
                        await save_memory_embedding(conn, nid, emb)
                except Exception as e:
                    print(f"⚠️ 拆分子记忆 {nid} 向量失败: {e}")
        await conn.execute("UPDATE memories SET is_active = FALSE WHERE id = $1", original_id)
    return new_ids


async def undo_split(original_id: int, child_ids: list):
    """撤销拆分：原记忆复活(is_active=TRUE)，拆出来的子记忆停用(is_active=FALSE，软删可再复活)。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE memories SET is_active = TRUE WHERE id = $1", original_id)
        if child_ids:
            await conn.execute("UPDATE memories SET is_active = FALSE WHERE id = ANY($1::int[])", list(child_ids))


async def undo_split_one(original_id: int) -> int:
    """撤销单条拆分：原记忆复活，它拆出来的子记忆(merged_from=[original_id])全部停用。返回收起的子记忆数。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE memories SET is_active = TRUE WHERE id = $1", original_id)
        res = await conn.execute(
            "UPDATE memories SET is_active = FALSE WHERE merged_from = $1::int[]", [original_id])
        try:
            return int(str(res).split()[-1])
        except Exception:
            return 0


async def photo_hash_exists(data: bytes) -> bool:
    """检查这张图片(按内容)是否已经存在 memory_photos 里，用于看图记忆去重(避免同一张图反复触发记忆+描述调用)。"""
    if not data:
        return False
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchval("SELECT 1 FROM memory_photos WHERE md5(data) = md5($1) LIMIT 1", data)
        return bool(row)


async def save_image_memory(content: str, source_session: str = "", photos=None,
                            importance: int = 5, valence: float = 0.0, arousal: float = 0.4) -> int:
    """看图记忆:存一条文字描述记忆 + 关联图片(memory_photos,长期可取 /api/photos/id)。返回 memory_id。
    自带 embedding(可检索→下轮记得)。photos=[(mime, bytes), ...]。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        _val = max(-1.0, min(1.0, float(valence or 0.0)))
        _aro = max(0.0, min(1.0, float(arousal if arousal is not None else 0.4)))
        row = await conn.fetchrow(
            "INSERT INTO memories (content, importance, source_session, valence, arousal) "
            "VALUES ($1, $2, $3, $4, $5) RETURNING id",
            content, importance, source_session, _val, _aro)
        mid = row['id']
        for (mime, data) in (photos or []):
            if not data:
                continue
            try:
                dup = await conn.fetchval(
                    "SELECT 1 FROM memory_photos WHERE md5(data) = md5($1) LIMIT 1", data)
                if dup:
                    continue  # 同一张图已经存过，跳过重复存储
                await conn.execute(
                    "INSERT INTO memory_photos (memory_id, original_name, mime, data) VALUES ($1, $2, $3, $4)",
                    mid, "chat_image", (mime or 'image/png'), data)
            except Exception as e:
                print(f"⚠️ 看图记忆 #{mid} 存图失败: {e}")
        if MEMORY_VECTOR_ENABLED:
            try:
                emb = await compute_embedding(content)
                if emb:
                    await save_memory_embedding(conn, mid, emb)
            except Exception:
                pass
        return mid


# ---- is_explicit 露骨标记（注入收敛 + 回填用）----

async def get_memories_explicit_flags(ids: list) -> dict:
    """批量查一组记忆的 is_explicit；返回 {id(int): bool}。注入路径仅在收敛开关开启时调用一次。"""
    if not ids:
        return {}
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, is_explicit FROM memories WHERE id = ANY($1::int[])", list(ids)
        )
        return {r["id"]: bool(r["is_explicit"]) for r in rows}


async def set_memory_explicit(memory_id: int, value: bool) -> bool:
    """置某条记忆的 is_explicit（回填/面板用）。只动这一列，不碰正文/情绪/日期。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        res = await conn.execute(
            "UPDATE memories SET is_explicit = $1 WHERE id = $2", bool(value), memory_id
        )
        return res.endswith("1")


async def get_explicit_backfill_candidates(keywords: list, ids: list = None, limit: int = 200) -> list:
    """收集露骨回填候选：指定 ids，或 content ILIKE 任一 keyword（仅 is_active）。返回 [{id, content}]。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if ids:
            rows = await conn.fetch(
                "SELECT id, content FROM memories WHERE id = ANY($1::int[]) AND content IS NOT NULL", list(ids)
            )
            return [{"id": r["id"], "content": r["content"]} for r in rows]
        if not keywords:
            return []
        where = " OR ".join(f"content ILIKE '%' || ${i+1} || '%'" for i in range(len(keywords)))
        params = list(keywords)
        params.append(limit)
        rows = await conn.fetch(
            f"SELECT id, content FROM memories WHERE is_active = TRUE AND mw_meta IS NULL AND content IS NOT NULL AND ({where}) "
            f"ORDER BY importance DESC, created_at DESC LIMIT ${len(keywords)+1}",
            *params,
        )
        return [{"id": r["id"], "content": r["content"]} for r in rows]


async def get_high_arousal_memories(threshold: float = 0.55) -> list:
    """高 arousal 活跃非回忆墙记忆(含当前 is_explicit/valence/arousal)，供语义重判 is_explicit(堵漏标洞)。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, content, is_explicit, valence, arousal FROM memories "
            "WHERE is_active = TRUE AND mw_meta IS NULL AND content IS NOT NULL AND btrim(content) <> '' "
            "AND arousal >= $1 ORDER BY arousal DESC", threshold)
        return [{"id": r["id"], "content": r["content"], "is_explicit": bool(r["is_explicit"]),
                 "valence": float(r["valence"] or 0), "arousal": float(r["arousal"] or 0)} for r in rows]


async def get_decay_candidates(age_days: int = 7, imp_max: int = 4,
                               idle_days: int = 5, arousal_max: float = 0.45,
                               limit: int = 500) -> list:
    """②衰减归档候选(只读,供 dry 审):同时满足
       老(created_at 在 age_days 天前) + 低重要度(importance<=imp_max) +
       久未取(last_accessed/created_at 在 idle_days 天前) + 低唤起(arousal<arousal_max)
       + 活跃 + 非回忆墙。高imp/高arousal/近期/被回忆过/回忆墙 天然被排除(珍贵记忆受保护)。
       age_days/idle_days 在 SQL 内算(避免 import)。里程碑保护:importance 高的不会进。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, content, COALESCE(importance,5) AS importance, layer, "
            "       COALESCE(arousal,0) AS arousal, COALESCE(valence,0) AS valence, is_explicit, "
            "       FLOOR(EXTRACT(EPOCH FROM (NOW()-created_at))/86400)::int AS age_days, "
            "       FLOOR(EXTRACT(EPOCH FROM (NOW()-COALESCE(last_accessed,created_at)))/86400)::int AS idle_days "
            "FROM memories "
            "WHERE is_active = TRUE AND mw_meta IS NULL AND content IS NOT NULL AND btrim(content) <> '' "
            "  AND COALESCE(importance,5) <= $1 "
            "  AND COALESCE(arousal,0) < $2 "
            "  AND created_at < NOW() - make_interval(days => $3) "
            "  AND COALESCE(last_accessed,created_at) < NOW() - make_interval(days => $4) "
            "ORDER BY COALESCE(importance,5) ASC, age_days DESC LIMIT $5",
            imp_max, arousal_max, age_days, idle_days, limit)
        return [{"id": r["id"], "content": r["content"], "importance": int(r["importance"]),
                 "layer": r["layer"], "arousal": float(r["arousal"]), "valence": float(r["valence"]),
                 "is_explicit": bool(r["is_explicit"]), "age_days": int(r["age_days"]),
                 "idle_days": int(r["idle_days"])} for r in rows]


async def count_active_memories() -> int:
    """活跃非回忆墙记忆总数(衰减 dry 报告占比用)。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        return int(await conn.fetchval(
            "SELECT COUNT(*) FROM memories WHERE is_active = TRUE AND mw_meta IS NULL "
            "AND content IS NOT NULL AND btrim(content) <> ''") or 0)


async def get_current_mood(recent_n: int = 30, skip_memorywall: bool = True) -> dict:
    """小克「当下情绪」:最近 recent_n 条活跃记忆 valence/arousal 均值(=心情漂移 current_mood 的最近窗口基线,
    与 apply_mood_drift 同源 → 主页心跳/电波=小克所见)。只读。"""
    mw = "AND mw_meta IS NULL" if skip_memorywall else ""
    pool = await get_pool()
    async with pool.acquire() as conn:
        r = await conn.fetchrow(
            f"SELECT AVG(valence)::float AS v, AVG(arousal)::float AS a FROM "
            f"(SELECT valence, arousal FROM memories WHERE is_active = TRUE {mw} "
            f"ORDER BY created_at DESC LIMIT $1) t", recent_n)
        return {"valence": float(r["v"]) if r and r["v"] is not None else 0.0,
                "arousal": float(r["a"]) if r and r["a"] is not None else 0.2}


async def archive_decayed_memories(memory_ids: list):
    """②衰减归档(非合并):置 is_active=FALSE 且 decayed_at=NOW()。decayed_at 标记使 cleanup_old_fragments 豁免它——
       归档≠删除,记忆不能丢。可逆(reactivate_decayed_memories 清标复活)。"""
    if not memory_ids:
        return
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE memories SET is_active = FALSE, decayed_at = NOW() WHERE id = ANY($1::int[])",
            memory_ids)


async def reactivate_decayed_memories(memory_ids: list) -> int:
    """衰减归档复活:置 is_active=TRUE 且 decayed_at=NULL(清标)。返回受影响行数。"""
    if not memory_ids:
        return 0
    pool = await get_pool()
    async with pool.acquire() as conn:
        res = await conn.execute(
            "UPDATE memories SET is_active = TRUE, decayed_at = NULL WHERE id = ANY($1::int[])",
            memory_ids)
        try:
            return int(res.split()[-1])
        except Exception:
            return len(memory_ids)


async def count_explicit_memories() -> int:
    """is_explicit=TRUE 的活跃非回忆墙记忆数(记忆控制台「分寸」面板显示)。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        return int(await conn.fetchval(
            "SELECT COUNT(*) FROM memories WHERE is_active = TRUE AND mw_meta IS NULL "
            "AND is_explicit = TRUE AND content IS NOT NULL AND btrim(content) <> ''") or 0)


async def clear_persona_suggestions() -> int:
    """软清:把所有 pending 人设建议置 ignored(不删数据,可在 status=ignored 查回)。返回清理条数。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        res = await conn.execute("UPDATE persona_suggestions SET status = 'ignored' WHERE status = 'pending'")
        try:
            return int(res.split()[-1])
        except Exception:
            return 0


async def clear_l5_candidates() -> int:
    """软清:把所有 pending L5 根基候选置 ignored(不删数据)。返回清理条数。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        res = await conn.execute("UPDATE l5_candidates SET status = 'ignored' WHERE status = 'pending'")
        try:
            return int(res.split()[-1])
        except Exception:
            return 0


# ---- ③-2 做梦 ----

def _to_date(s):
    """'YYYY-MM-DD' 字符串 → datetime.date(asyncpg 的 date 列要 date 对象,不能传 str)。"""
    import datetime as _dt
    if isinstance(s, _dt.date):
        return s
    try:
        return _dt.date.fromisoformat(str(s)[:10])
    except Exception:
        return s


async def save_dream(dream_date: str, diary: str, summary: str = "", card_title: str = "",
                     card_body: str = "", model: str = "") -> bool:
    """写/覆盖某天的梦（dream_date 唯一，幂等 upsert）。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO dreams (dream_date, diary, summary, card_title, card_body, model)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (dream_date) DO UPDATE SET
                diary=EXCLUDED.diary, summary=EXCLUDED.summary,
                card_title=EXCLUDED.card_title, card_body=EXCLUDED.card_body,
                model=EXCLUDED.model, created_at=NOW()
        """, _to_date(dream_date), diary, summary, card_title, card_body, model)
        return True


async def get_dream(dream_date: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT dream_date, diary, summary, card_title, card_body, model, created_at "
            "FROM dreams WHERE dream_date = $1", _to_date(dream_date))
        return dict(row) if row else None


async def list_dreams(limit: int = 60):
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT dream_date, diary, summary, card_title, card_body, model, created_at "
            "FROM dreams ORDER BY dream_date DESC LIMIT $1", limit)
        return [dict(r) for r in rows]


async def get_dream_dates() -> set:
    """已有梦的日期集合（YYYY-MM-DD 字符串），用于幂等跳过。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT dream_date FROM dreams")
        return {str(r["dream_date"]) for r in rows}


async def get_memorywall_dates() -> set:
    """回忆墙已覆盖的事件日期集合（YYYY-MM-DD），做梦时跳过这些日子。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT DISTINCT event_date FROM memories WHERE mw_meta IS NOT NULL AND event_date IS NOT NULL")
        return {str(r["event_date"]) for r in rows if r["event_date"]}


# ---- ③-1 feel ----

async def save_feel(session_id: str, content: str, is_explicit: bool = False) -> bool:
    if not (content or "").strip():
        return False
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO feels (session_id, content, is_explicit) VALUES ($1, $2, $3)",
            session_id, content.strip(), bool(is_explicit))
        return True


async def get_recent_feels(session_id: str, limit: int = 8) -> list:
    """取最近 N 条 feel(时间升序返回)。注入时只取最近窗、不进检索打分。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT content, is_explicit, created_at FROM feels WHERE session_id = $1 "
            "ORDER BY created_at DESC LIMIT $2", session_id, limit)
        return [{"content": r["content"], "is_explicit": bool(r["is_explicit"])} for r in reversed(rows)]


async def get_all_feels() -> list:
    """所有 feel(id/content/is_explicit),供语义重判 is_explicit(修过标)。只读。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, content, is_explicit FROM feels "
            "WHERE content IS NOT NULL AND btrim(content) <> '' ORDER BY created_at DESC")
        return [{"id": r["id"], "content": r["content"], "is_explicit": bool(r["is_explicit"])} for r in rows]


async def set_feel_explicit(feel_id: int, val: bool):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE feels SET is_explicit = $2 WHERE id = $1", feel_id, bool(val))


# ---- 回忆墙迁移辅助函数 ----

async def find_memory_by_mw_id(original_id: str):
    """按回忆墙原始ID查已迁移记忆（幂等：避免重复迁移）"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id FROM memories WHERE mw_meta->>'original_id' = $1 LIMIT 1",
            str(original_id),
        )
        return row['id'] if row else None


async def save_migrated_memory(content, importance, title, event_date, created_at, mw_meta):
    """插入一条完整的回忆墙记忆（layer=3 核心记忆，结构化字段存 mw_meta，不切碎）"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO memories (content, importance, source_session, layer, is_active, title, event_date, created_at, mw_meta)
            VALUES ($1, $2, 'memory_wall', 3, TRUE, $3, $4::text::date, $5::text::timestamptz, $6::jsonb)
            RETURNING id
            """,
            content, importance, (title or None),
            (str(event_date)[:10] if event_date else None),
            (str(created_at) if created_at else None),
            json.dumps(mw_meta, ensure_ascii=False),
        )
        mid = row['id']
        if MEMORY_VECTOR_ENABLED:
            try:
                embedding = await compute_embedding(content)
                if embedding:
                    await save_memory_embedding(conn, mid, embedding)
            except Exception as e:
                print(f"⚠️ 迁移记忆 {mid} embedding计算失败: {e}")
        return mid


async def save_photo(memory_id, original_name, mime, data: bytes):
    """把照片二进制存进 Postgres，返回 photo id"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO memory_photos (memory_id, original_name, mime, data) VALUES ($1, $2, $3, $4) RETURNING id",
            memory_id, original_name, (mime or 'image/png'), data,
        )
        return row['id']


async def link_photo_to_memory(photo_id: int, memory_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE memory_photos SET memory_id = $2 WHERE id = $1", photo_id, memory_id)


async def get_photo(photo_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, memory_id, original_name, mime, data FROM memory_photos WHERE id = $1",
            photo_id,
        )
        return dict(row) if row else None


async def memory_photo_count(memory_id: int) -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT COUNT(*) AS n FROM memory_photos WHERE memory_id = $1", memory_id)
        return int(row['n']) if row else 0


async def delete_memory_photos(memory_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM memory_photos WHERE memory_id = $1", memory_id)


async def get_mw_meta(memory_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT mw_meta FROM memories WHERE id = $1", memory_id)
        if not row or row['mw_meta'] is None:
            return None
        v = row['mw_meta']
        return v if isinstance(v, dict) else json.loads(v)


async def update_mw_meta(memory_id: int, mw_meta: dict):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE memories SET mw_meta = $2::jsonb WHERE id = $1",
                           memory_id, json.dumps(mw_meta, ensure_ascii=False))


# ---- 回忆墙视图：CRUD（“回忆”= mw_meta 非空的记忆）----

def _row_to_mw(row):
    d = dict(row)
    mm = d.get('mw_meta')
    for _ in range(2):  # 容错:mw_meta 可能是 dict / JSON字符串 / 历史上被双重编码的字符串
        if isinstance(mm, str):
            try:
                mm = json.loads(mm)
            except Exception:
                mm = {}
        else:
            break
    d['mw_meta'] = mm if isinstance(mm, dict) else {}
    return d


async def delete_dream_memories(event_date) -> int:
    """删除某天的「梦→回忆墙」可检索条目(source=dream，含历史上 mw_meta 被双重编码成字符串的坏条目)，
    用于重生成前去重。**只删 dream 条目，绝不碰其它回忆墙。**"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        r = await conn.execute(
            "DELETE FROM memories WHERE source_session='memory_wall' AND event_date = $1::text::date "
            "AND (mw_meta->>'source' = 'dream' OR jsonb_typeof(mw_meta) = 'string')",
            str(event_date)[:10],
        )
    try:
        return int(str(r).split()[-1])
    except Exception:
        return 0


async def get_memory_photos(memory_id: int):
    """某条记忆的照片引用（不含二进制）"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, original_name, mime FROM memory_photos WHERE memory_id = $1 ORDER BY id",
            memory_id,
        )
        return [{"photo_id": r["id"], "original_name": r["original_name"],
                 "mime": r["mime"], "url": f"/api/photos/{r['id']}"} for r in rows]


async def list_memorywall(author: str = None, mood: str = None, include_inactive: bool = False):
    pool = await get_pool()
    async with pool.acquire() as conn:
        sql = "SELECT id, content, title, importance, created_at, event_date, is_active, mw_meta FROM memories WHERE mw_meta IS NOT NULL"
        params = []
        if not include_inactive:
            sql += " AND is_active = TRUE"
        if author:
            params.append(author); sql += f" AND mw_meta->>'author' = ${len(params)}"
        if mood:
            params.append(mood); sql += f" AND mw_meta->>'mood' = ${len(params)}"
        sql += " ORDER BY COALESCE((mw_meta->>'pinned') = 'true', FALSE) DESC, COALESCE(event_date, created_at::date) DESC, created_at DESC"
        rows = await conn.fetch(sql, *params)
    out = []
    for r in rows:
        d = _row_to_mw(r)
        d['photos'] = await get_memory_photos(d['id'])
        out.append(d)
    return out


async def get_memorywall_one(memory_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, content, title, importance, created_at, event_date, is_active, mw_meta FROM memories WHERE id = $1 AND mw_meta IS NOT NULL",
            memory_id,
        )
    if not row:
        return None
    d = _row_to_mw(row)
    d['photos'] = await get_memory_photos(d['id'])
    return d


async def update_memorywall(memory_id: int, content, title, importance, event_date, mw_meta):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE memories
            SET content = $2, title = $3, importance = $4,
                event_date = $5::text::date, mw_meta = $6::jsonb
            WHERE id = $1 AND mw_meta IS NOT NULL
            """,
            memory_id, content, (title or None), importance,
            (str(event_date)[:10] if event_date else None),
            json.dumps(mw_meta, ensure_ascii=False),
        )
    if MEMORY_VECTOR_ENABLED:
        try:
            emb = await compute_embedding(content)
            if emb:
                pool2 = await get_pool()
                async with pool2.acquire() as c2:
                    await save_memory_embedding(c2, memory_id, emb)
        except Exception as e:
            print(f"⚠️ 回忆 {memory_id} embedding更新失败: {e}")


async def set_memory_active(memory_id: int, active: bool):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE memories SET is_active = $2 WHERE id = $1", memory_id, active)


# ---- 人设建议（A4）：行为/相处偏好的收集，供主理人贴 persona ----

async def save_l5_candidate(content: str, event_date=None, source_session: str = "", target: str = "l5"):
    """② 里程碑候选入待审队列（机器只增；同内容+同去向 pending 去重）。target='l5'(→根基)|'wall'(→回忆墙)。不自动升永久。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        existing = await conn.fetchrow(
            "SELECT id FROM l5_candidates WHERE content = $1 AND status = 'pending' AND target = $2 LIMIT 1", content, target)
        if existing:
            return existing['id']
        row = await conn.fetchrow(
            "INSERT INTO l5_candidates (content, event_date, source_session, target) VALUES ($1, $2::text::date, $3, $4) RETURNING id",
            content, (str(event_date) if event_date else None), source_session, target)
        return row['id']


async def list_l5_candidates(status: str = "pending", target: str = None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        cols = "id, content, event_date, source_session, status, created_at, target"
        where, params = [], []
        if status != "all":
            params.append(status); where.append(f"status = ${len(params)}")
        if target:
            params.append(target); where.append(f"target = ${len(params)}")
        sql = f"SELECT {cols} FROM l5_candidates" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY created_at DESC"
        rows = await conn.fetch(sql, *params)
        return [dict(r) for r in rows]


async def update_l5_candidate(cand_id: int, status: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE l5_candidates SET status = $2 WHERE id = $1", cand_id, status)


async def get_l5_candidate(cand_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, content, event_date, source_session, status, created_at, target FROM l5_candidates WHERE id = $1", cand_id)
        return dict(row) if row else None


async def save_persona_suggestion(content: str, source_session: str = ""):
    pool = await get_pool()
    async with pool.acquire() as conn:
        # 去重：同内容且还在 pending 的不重复收集
        existing = await conn.fetchrow(
            "SELECT id FROM persona_suggestions WHERE content = $1 AND status = 'pending' LIMIT 1",
            content,
        )
        if existing:
            return existing['id']
        row = await conn.fetchrow(
            "INSERT INTO persona_suggestions (content, source_session) VALUES ($1, $2) RETURNING id",
            content, source_session,
        )
        return row['id']


async def list_persona_suggestions(status: str = "pending"):
    pool = await get_pool()
    async with pool.acquire() as conn:
        if status == "all":
            rows = await conn.fetch("SELECT id, content, source_session, status, created_at FROM persona_suggestions ORDER BY created_at DESC")
        else:
            rows = await conn.fetch("SELECT id, content, source_session, status, created_at FROM persona_suggestions WHERE status = $1 ORDER BY created_at DESC", status)
        return [dict(r) for r in rows]


async def update_persona_suggestion(sug_id: int, status: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE persona_suggestions SET status = $2 WHERE id = $1", sug_id, status)


async def search_memories(query: str, limit: int = 10):
    """
    搜索相关记忆
    
    MEMORY_VECTOR_ENABLED=true 时走混合搜索（关键词 + 向量）
    否则走纯关键词搜索
    """
    if MEMORY_VECTOR_ENABLED:
        return await search_memories_hybrid(query, limit)
    
    # ---- 纯关键词搜索 ----
    keywords = extract_search_keywords(query)
    
    if not keywords:
        return []
    
    pool = await get_pool()
    async with pool.acquire() as conn:
        # === IDF 降噪：用语料真实 df 给关键词加权，并把高频词（含"今天/的/我"这类）从匹配集剔除 ===
        # A: 剔除时间指示词（时间召回交给 recency，绝不靠字面 token）
        kw_list = [k for k in keywords if k not in TIME_DEIXIS]
        if not kw_list:
            return []
        N = await conn.fetchval("SELECT COUNT(*) FROM memories WHERE is_active = TRUE") or 1
        df_sel = ", ".join(
            f"SUM(CASE WHEN content ILIKE '%' || ${i+1} || '%' THEN 1 ELSE 0 END) AS df{i}"
            for i in range(len(kw_list))
        )
        df_row = await conn.fetchrow(f"SELECT {df_sel} FROM memories WHERE is_active = TRUE", *kw_list)
        common_cut = max(2, int(N * SEARCH_COMMON_FRAC))
        kept = []        # [(kw, idf)] 参与匹配+加权
        dropped = []     # [(kw, df)] 高频被剔除
        idf_of = {}
        for i, kw in enumerate(kw_list):
            df = df_row[f"df{i}"] or 0
            idf = math.log((N + 1.0) / (df + 1.0)) + 1.0
            idf_of[kw] = (df, idf)
            if df > common_cut:
                dropped.append((kw, df))
            else:
                kept.append((kw, idf))
        if not kept:
            # 整句都是高频词 → 不强行召回噪声，只保留最稀有的一个兜底
            rarest = min(kw_list, key=lambda k: idf_of[k][0])
            kept = [(rarest, idf_of[rarest][1])]
            dropped = [(k, idf_of[k][0]) for k in kw_list if k != rarest]

        params = [kw for kw, _ in kept]
        sum_w = sum(w for _, w in kept) or 1.0
        whit_expr = " + ".join(
            f"{w:.4f}*(CASE WHEN content ILIKE '%' || ${i+1} || '%' THEN 1 ELSE 0 END)"
            for i, (kw, w) in enumerate(kept)
        )
        where_clause = "is_active = TRUE AND (" + " OR ".join(
            f"content ILIKE '%' || ${i+1} || '%'" for i in range(len(kept))
        ) + ")"
        limit_idx = len(kept) + 1
        params.append(limit)

        sql = f"""
            SELECT
                id, content, importance, created_at, mw_meta, valence, arousal,
                ({whit_expr}) AS whit,
                ({WEIGHT_KEYWORD} * ({whit_expr}) / {sum_w:.4f}) AS kw_score,
                (
                    {WEIGHT_KEYWORD} * ({whit_expr}) / {sum_w:.4f} +
                    {WEIGHT_IMPORTANCE} * importance::float / 10.0 +
                    {WEIGHT_RECENCY} * (1.0 / (1.0 + EXTRACT(EPOCH FROM (NOW() - created_at)) / 86400.0))
                ) AS score
            FROM memories
            WHERE {where_clause}
            ORDER BY score DESC, importance DESC, created_at DESC
            LIMIT ${limit_idx}
        """

        results = await conn.fetch(sql, *params)

        # B: 相关性闸门 —— 用「关键词得分」过滤；importance/recency 只在已相关集合里排序，
        #    不能单凭高 imp + 新近就把零关键词相关的记忆顶进来。无关键词相关→根本不注入。
        if MIN_SCORE_THRESHOLD > 0:
            before_count = len(results)
            results = [r for r in results if r['kw_score'] >= MIN_SCORE_THRESHOLD]
            filtered = before_count - len(results)
        else:
            filtered = 0

        _keep_kw = [k for k, _ in kept]
        _drop_kw = [k for k, _ in dropped]
        if results:
            print(f"🔍 '{query}' → 留{_keep_kw[:8]} 丢{_drop_kw[:8]} → 命中 {len(results)} 条" + (f"（过滤 {filtered} 低分）" if filtered else ""))
            for r in results[:3]:
                print(f"   📌 [score={r['score']:.3f}] (imp={r['importance']}) {(r['content'] or '')[:50]}...")
            ids = [r["id"] for r in results]
            await conn.execute(
                "UPDATE memories SET last_accessed = NOW() WHERE id = ANY($1::int[])",
                ids,
            )
        else:
            print(f"🔍 '{query}' → 留{_keep_kw[:8]} 丢{_drop_kw[:8]} → 无结果" + (f"（{filtered} 被阈值过滤）" if filtered else ""))

        return results


async def apply_mood_drift(hit_ids: list, step: float = 0.1, daily_cap: int = 3,
                          recent_n: int = 30, skip_memorywall: bool = True, tz_hours: int = 8):
    """情绪①-第二步 心情漂移：把【本轮命中】的旧记忆的情感坐标朝 current_mood 挪 ≤step 并写回 DB（持久）。
    current_mood = (最近 recent_n 条活跃记忆均值 + 本轮命中均值) / 2（逐轴）。
    只动 valence/arousal（clamp valence[-1,1] / arousal[0,1]），正文/importance/日期一律不碰。
    防跑飞：步长≤step + 命中去重（每条每轮≤1次）+ 每条每日≤daily_cap 次（原子 SQL 守卫）。
    skip_memorywall=True：回忆墙(mw_meta 非空)记忆既不漂移、也不进 mood 基线。
    仅由聊天注入路径 fire-and-forget 调用；dashboard 搜索绝不触发。"""
    from datetime import datetime, timezone, timedelta
    try:
        if not hit_ids:
            return {"drifted": 0}
        ids = list({int(i) for i in hit_ids})  # 去重 → 每条每轮≤1次
        mw_filter = "AND mw_meta IS NULL" if skip_memorywall else ""
        pool = await get_pool()
        async with pool.acquire() as conn:
            hit = await conn.fetchrow(
                f"SELECT AVG(valence)::float AS v, AVG(arousal)::float AS a "
                f"FROM memories WHERE id = ANY($1::int[]) AND is_active = TRUE {mw_filter}",
                ids,
            )
            if not hit or hit["v"] is None:
                return {"drifted": 0}
            rec = await conn.fetchrow(
                f"SELECT AVG(valence)::float AS v, AVG(arousal)::float AS a FROM "
                f"(SELECT valence, arousal FROM memories WHERE is_active = TRUE {mw_filter} "
                f"ORDER BY created_at DESC LIMIT $1) t",
                recent_n,
            )
            rv = rec["v"] if rec and rec["v"] is not None else hit["v"]
            ra = rec["a"] if rec and rec["a"] is not None else hit["a"]
            mood_v = (rv + hit["v"]) / 2.0
            mood_a = (ra + hit["a"]) / 2.0
            today = (datetime.now(timezone.utc) + timedelta(hours=tz_hours)).date()
            rows = await conn.fetch(
                f"SELECT id, valence, arousal FROM memories "
                f"WHERE id = ANY($1::int[]) AND is_active = TRUE {mw_filter}",
                ids,
            )
            drifted = 0
            details = []
            for r in rows:
                cv = float(r["valence"]); ca = float(r["arousal"])
                dv = max(-step, min(step, mood_v - cv))
                da = max(-step, min(step, mood_a - ca))
                if abs(dv) < 1e-4 and abs(da) < 1e-4:
                    continue  # 已在 mood 上 → 不动、也不耗每日额度
                tag = await conn.execute(
                    """
                    UPDATE memories
                    SET valence = GREATEST(-1.0, LEAST(1.0, valence + $2)),
                        arousal = GREATEST(0.0, LEAST(1.0, arousal + $3)),
                        drift_today = CASE WHEN drift_day = $4 THEN drift_today + 1 ELSE 1 END,
                        drift_day = $4
                    WHERE id = $1 AND (drift_day IS DISTINCT FROM $4 OR drift_today < $5)
                    """,
                    r["id"], dv, da, today, daily_cap,
                )
                if tag and tag.rsplit(" ", 1)[-1] != "0":
                    drifted += 1
                    if len(details) < 8:
                        details.append({"id": int(r["id"]), "dv": round(dv, 4), "da": round(da, 4)})
            if drifted:
                print(f"🎭 心情漂移 mood=({mood_v:.2f},{mood_a:.2f}) 漂{drifted}/{len(rows)}条 step≤{step} cap{daily_cap}/日")
            return {"drifted": drifted, "mood": [round(mood_v, 3), round(mood_a, 3)], "details": details}
    except Exception as e:
        print(f"⚠️ 心情漂移失败: {e}")
        return {"drifted": 0, "error": str(e)}


async def search_memories_hybrid(query: str, limit: int = 10):
    """
    记忆混合搜索：关键词 + 向量，归一化后四维加权
    
    权重：MEMORY_HW_KEYWORD + MEMORY_HW_SEMANTIC + MEMORY_HW_IMPORTANCE + MEMORY_HW_RECENCY
    """
    from datetime import datetime, timezone
    
    keywords = extract_search_keywords(query)
    query_embedding = await compute_embedding(query) if EMBEDDING_API_KEY else []
    
    if not keywords and not query_embedding:
        return []
    
    pool = await get_pool()
    async with pool.acquire() as conn:
        candidates = {}  # id -> {content, importance, created_at, kw_score, similarity}
        
        # ---- 关键词路 ----
        if keywords:
            case_parts = []
            params = []
            for i, kw in enumerate(keywords):
                case_parts.append(f"CASE WHEN content ILIKE '%' || ${i+1} || '%' THEN 1 ELSE 0 END")
                params.append(kw)
            
            hit_count_expr = " + ".join(case_parts)
            max_hits = len(keywords)
            where_parts = [f"content ILIKE '%' || ${i+1} || '%'" for i in range(len(keywords))]
            where_clause = f"is_active = TRUE AND ({' OR '.join(where_parts)})"
            
            limit_idx = len(keywords) + 1
            params.append(limit * 3)
            
            kw_sql = f"""
                SELECT id, content, importance, created_at,
                       ({hit_count_expr}) AS hit_count,
                       ({hit_count_expr})::float / {max_hits}.0 AS kw_score
                FROM memories
                WHERE {where_clause}
                ORDER BY kw_score DESC
                LIMIT ${limit_idx}
            """
            kw_rows = await conn.fetch(kw_sql, *params)
            
            for r in kw_rows:
                candidates[r['id']] = {
                    'content': r['content'],
                    'importance': r['importance'],
                    'created_at': r['created_at'],
                    'hit_count': r['hit_count'],
                    'kw_score': float(r['kw_score']),
                    'similarity': 0.0,
                }
        
        # ---- 向量路 ----
        if query_embedding:
            if HAS_PGVECTOR:
                vec_str = '[' + ','.join(str(f) for f in query_embedding) + ']'
                sem_rows = await conn.fetch("""
                    SELECT id, content, importance, created_at,
                           1 - (embedding <=> $1::vector) as similarity
                    FROM memories
                    WHERE embedding IS NOT NULL AND is_active = TRUE
                      AND content IS NOT NULL AND btrim(content) <> ''
                    ORDER BY embedding <=> $1::vector
                    LIMIT $2
                """, vec_str, limit * 3)
            else:
                # Python端计算cosine
                import json
                all_mem = await conn.fetch("""
                    SELECT id, content, importance, created_at, embedding_json
                    FROM memories WHERE embedding_json IS NOT NULL AND is_active = TRUE
                      AND content IS NOT NULL AND btrim(content) <> ''
                """)
                
                scored = []
                for row in all_mem:
                    try:
                        emb = json.loads(row['embedding_json'])
                        sim = _cosine_sim(query_embedding, emb)
                        scored.append({**dict(row), 'similarity': sim})
                    except Exception:
                        continue
                scored.sort(key=lambda x: -x['similarity'])
                sem_rows = scored[:limit * 3]
            
            for r in sem_rows:
                sim = float(r['similarity'])
                if sim < MEMORY_SEMANTIC_THRESHOLD:
                    continue
                mid = r['id']
                if mid in candidates:
                    candidates[mid]['similarity'] = sim
                else:
                    candidates[mid] = {
                        'content': r['content'],
                        'importance': r['importance'],
                        'created_at': r['created_at'],
                        'hit_count': 0,
                        'kw_score': 0.0,
                        'similarity': sim,
                    }
            
            # debug：向量路统计
            sem_total = len(sem_rows)
            sem_passed = sum(1 for r in sem_rows if float(r['similarity']) >= MEMORY_SEMANTIC_THRESHOLD)
            sem_max = max((float(r['similarity']) for r in sem_rows), default=0)
            if sem_total > 0 and sem_passed == 0:
                print(f"   🔢 向量路: {sem_total}条候选全被阈值过滤（最高sim={sem_max:.3f}, 阈值={MEMORY_SEMANTIC_THRESHOLD}）")
            elif sem_total > 0:
                print(f"   🔢 向量路: {sem_passed}/{sem_total}条通过阈值（最高sim={sem_max:.3f}）")
        
        if not candidates:
            print(f"🔍 混合搜索 '{query}' → 两路均无结果")
            return []
        
        # ---- 归一化 + 加权 ----
        kw_norm = _min_max_normalize({mid: v['kw_score'] for mid, v in candidates.items()})
        sem_norm = _min_max_normalize({mid: v['similarity'] for mid, v in candidates.items()})
        
        now = datetime.now(timezone.utc)
        final = []
        for mid, info in candidates.items():
            kw = kw_norm.get(mid, 0.0)
            sem = sem_norm.get(mid, 0.0)
            imp = info['importance'] / 10.0
            days = (now - info['created_at']).total_seconds() / 86400.0
            rec = 1.0 / (1.0 + days)
            
            score = (MEMORY_HW_KEYWORD * kw +
                     MEMORY_HW_SEMANTIC * sem +
                     MEMORY_HW_IMPORTANCE * imp +
                     MEMORY_HW_RECENCY * rec)
            
            final.append({
                'id': mid,
                'content': info['content'],
                'importance': info['importance'],
                'created_at': info['created_at'],
                'hit_count': info['hit_count'],
                'similarity': info['similarity'],
                'score': score,
            })
        
        final.sort(key=lambda x: (-x['score'], -x['importance']))
        
        # 过滤低分
        if MIN_SCORE_THRESHOLD > 0:
            before_count = len(final)
            final = [r for r in final if r['score'] >= MIN_SCORE_THRESHOLD]
            filtered = before_count - len(final)
        else:
            filtered = 0
        
        results = final[:limit]
        
        if results:
            mode_tag = "混合" if query_embedding else "关键词"
            kw_tag = f"关键词 {keywords[:6]}" if keywords else "无关键词"
            print(f"🔍 {mode_tag}搜索 '{query}' → {kw_tag} → 命中 {len(results)} 条" + (f"（过滤 {filtered} 条低分）" if filtered else ""))
            for r in results[:3]:
                print(f"   📌 [score={r['score']:.3f}] (kw={r['hit_count']}, sim={r['similarity']:.2f}, imp={r['importance']}) {(r['content'] or '')[:60]}...")
            
            ids = [r["id"] for r in results]
            await conn.execute(
                "UPDATE memories SET last_accessed = NOW() WHERE id = ANY($1::int[])",
                ids,
            )
        else:
            print(f"🔍 混合搜索 '{query}' → 无结果" + (f"（{filtered} 条被过滤）" if filtered else ""))
        
        return [dict(r) for r in results]


async def get_pending_memory_embedding_count():
    """查询还没有embedding的记忆数量"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if HAS_PGVECTOR:
            return await conn.fetchval(
                "SELECT COUNT(*) FROM memories WHERE embedding IS NULL AND content IS NOT NULL"
            )
        else:
            return await conn.fetchval(
                "SELECT COUNT(*) FROM memories WHERE embedding_json IS NULL AND content IS NOT NULL"
            )


async def backfill_memory_embeddings(batch_size: int = 20):
    """给已有记忆补算embedding（没有embedding的记忆）"""
    if not EMBEDDING_API_KEY:
        print("⚠️ EMBEDDING_API_KEY 未设置，无法补算embedding")
        return 0
    
    pool = await get_pool()
    total_updated = 0
    
    async with pool.acquire() as conn:
        if HAS_PGVECTOR:
            rows = await conn.fetch("""
                SELECT id, content FROM memories 
                WHERE embedding IS NULL AND content IS NOT NULL
                ORDER BY id
                LIMIT $1
            """, batch_size)
        else:
            rows = await conn.fetch("""
                SELECT id, content FROM memories 
                WHERE embedding_json IS NULL AND content IS NOT NULL
                ORDER BY id
                LIMIT $1
            """, batch_size)
    
    if not rows:
        print("✅ 所有记忆已有embedding，无需补算")
        return 0
    
    print(f"🔄 开始补算记忆embedding... 本批 {len(rows)} 条")
    
    async with pool.acquire() as conn:
        for row in rows:
            try:
                embedding = await compute_embedding(row['content'] or '')
                if embedding:
                    await save_memory_embedding(conn, row['id'], embedding)
                    total_updated += 1
            except Exception as e:
                print(f"⚠️ 记忆 {row['id']} embedding计算失败: {e}")
    
    # 检查剩余
    async with pool.acquire() as conn:
        if HAS_PGVECTOR:
            remaining = await conn.fetchval("SELECT COUNT(*) FROM memories WHERE embedding IS NULL AND content IS NOT NULL")
        else:
            remaining = await conn.fetchval("SELECT COUNT(*) FROM memories WHERE embedding_json IS NULL AND content IS NOT NULL")
    
    print(f"✅ 本批补算完成：{total_updated}/{len(rows)} 条成功" + (f"，剩余 {remaining} 条待处理" if remaining > 0 else ""))
    return total_updated


async def get_recent_memories(limit: int = 20):
    pool = await get_pool()
    async with pool.acquire() as conn:
        # 只取活跃记忆：避免已归档/去重停用的碎片污染提取去重上下文，并防复发
        return await conn.fetch(
            "SELECT id, content, importance, created_at FROM memories WHERE is_active = TRUE ORDER BY created_at DESC LIMIT $1",
            limit,
        )


async def get_all_memories_count():
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT COUNT(*) as cnt FROM memories")
        return row["cnt"]


async def get_all_memories():
    """导出所有记忆（用于备份）"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT content, importance, source_session, created_at FROM memories ORDER BY id"
        )
        return [dict(r) for r in rows]


async def get_all_memories_detail(limit: int = None, layer: int = None, active_only: bool = None):
    """获取所有记忆（含 id，用于管理页面）
    
    Args:
        limit: 可选，限制返回数量
        layer: 可选，筛选指定层级（1=原始碎片, 2=事件记忆, 3=核心记忆）
        active_only: 可选，是否只返回 is_active=true 的记忆
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        conditions = []
        params = []
        param_idx = 1
        
        if layer is not None:
            conditions.append(f"layer = ${param_idx}")
            params.append(layer)
            param_idx += 1
        
        if active_only is not None:
            conditions.append(f"is_active = ${param_idx}")
            params.append(active_only)
            param_idx += 1
        
        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        
        if limit is not None:
            limit_clause = f"LIMIT ${param_idx}"
            params.append(limit)
        else:
            limit_clause = ""
        
        rows = await conn.fetch(f"""
            SELECT id, content, importance, source_session, created_at,
                   layer, title, is_active, merged_from, event_date, valence, arousal,
                   drift_day, drift_today, is_explicit, (mw_meta IS NOT NULL) AS is_mw
            FROM memories
            {where_clause}
            ORDER BY id
            {limit_clause}
        """, *params)
        return [dict(r) for r in rows]


async def get_emotion_backfill_targets(include_memorywall: bool = True, ids: list = None, limit: int = None):
    """情绪回填：选出 valence/arousal 仍为默认(≈0 / ≈0.2)的活跃记忆。纯只读。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        conds = ["is_active = TRUE",
                 "valence > -0.01 AND valence < 0.01",
                 "arousal > 0.19 AND arousal < 0.21"]
        params = []
        if not include_memorywall:
            conds.append("mw_meta IS NULL")
        if ids:
            params.append([int(i) for i in ids])
            conds.append(f"id = ANY(${len(params)}::int[])")
        where = " AND ".join(conds)
        lim = ""
        if limit:
            params.append(int(limit))
            lim = f"LIMIT ${len(params)}"
        rows = await conn.fetch(
            f"SELECT id, content, (mw_meta IS NOT NULL) AS is_mw FROM memories WHERE {where} ORDER BY id {lim}",
            *params)
        return [dict(r) for r in rows]


async def update_emotion_only(memory_id: int, valence: float, arousal: float) -> bool:
    """情绪回填专用：只写 valence/arousal，且仅当该行仍是默认值(幂等+安全)。
    正文/importance/日期/source/layer 一律不碰。返回是否真的更新了。"""
    v = max(-1.0, min(1.0, float(valence)))
    a = max(0.0, min(1.0, float(arousal)))
    pool = await get_pool()
    async with pool.acquire() as conn:
        tag = await conn.execute(
            """UPDATE memories SET valence = $2, arousal = $3
               WHERE id = $1 AND is_active = TRUE
                 AND valence > -0.01 AND valence < 0.01
                 AND arousal > 0.19 AND arousal < 0.21""",
            memory_id, v, a)
        return bool(tag) and tag.rsplit(" ", 1)[-1] != "0"


async def update_memory_emotion(memory_id: int, valence: float, arousal: float):
    """面板手动改情绪：无条件覆盖写 valence/arousal，并重置漂移基线(drift_day/drift_today 清零)，
    让她的手动值成为新基线、不被已累计的当日漂移立刻吃掉。只动这两列 + 漂移计数，不碰正文/importance/日期。"""
    v = max(-1.0, min(1.0, float(valence)))
    a = max(0.0, min(1.0, float(arousal)))
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE memories SET valence = $2, arousal = $3, drift_day = NULL, drift_today = 0 WHERE id = $1",
            memory_id, v, a)


async def update_memory(memory_id: int, content: str = None, importance: int = None):
    """更新单条记忆"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if content is not None and importance is not None:
            await conn.execute(
                "UPDATE memories SET content = $1, importance = $2 WHERE id = $3",
                content, importance, memory_id
            )
        elif content is not None:
            await conn.execute(
                "UPDATE memories SET content = $1 WHERE id = $2",
                content, memory_id
            )
        elif importance is not None:
            await conn.execute(
                "UPDATE memories SET importance = $1 WHERE id = $2",
                importance, memory_id
            )


async def delete_memory(memory_id: int):
    """删除单条记忆"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM memories WHERE id = $1", memory_id)


async def delete_memories_batch(memory_ids: list):
    """批量删除记忆"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM memories WHERE id = ANY($1::int[])", memory_ids
        )


# ============================================================
# 网关配置
# ============================================================

async def get_gateway_config(key: str, default: str = "") -> str:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT value FROM gateway_config WHERE key = $1", key)
        return row['value'] if row else default


async def set_gateway_config(key: str, value: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO gateway_config (key, value) VALUES ($1, $2)
            ON CONFLICT (key) DO UPDATE SET value = $2
        """, key, value)


async def get_all_gateway_config() -> dict:
    """获取所有配置项"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT key, value FROM gateway_config")
        return {r['key']: r['value'] for r in rows}


# ============================================================
# 对话历史读取（分区缓存用）
# ============================================================

async def get_conversation_messages(session_id: str, limit: int = 100):
    """按时间正序读取session的消息"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT role, content, metadata, created_at
            FROM conversations
            WHERE session_id = $1
            ORDER BY created_at ASC
            LIMIT $2
        """, session_id, limit)
        return [dict(r) for r in rows]


# ============================================================
# 分区缓存状态管理
# ============================================================

async def get_session_cache_state(session_id: str) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT summary, a_start_round, updated_at, early_summary FROM session_cache_state WHERE session_id = $1",
            session_id
        )
        if row:
            raw_summary = row['summary'] or ''
            summary_parts = []
            if raw_summary:
                try:
                    import json
                    parsed = json.loads(raw_summary)
                    if isinstance(parsed, list):
                        summary_parts = parsed
                    else:
                        summary_parts = [raw_summary]
                except (json.JSONDecodeError, ValueError):
                    summary_parts = [raw_summary]
            return {
                'summary_parts': summary_parts,
                'a_start_round': row['a_start_round'] or 0,
                'updated_at': row['updated_at'],
                'early_summary': row['early_summary'] or '',
            }
        return {'summary_parts': [], 'a_start_round': 0, 'updated_at': None, 'early_summary': ''}


async def save_session_cache_state(session_id: str, summary_parts: list, a_start_round: int, early_summary=None):
    """early_summary=None → 保留原值(COALESCE);传字符串(含'')→覆盖。其余调用方不传=不动早期小结。"""
    import json
    summary_json = json.dumps(summary_parts, ensure_ascii=False)
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO session_cache_state (session_id, summary, a_start_round, early_summary, updated_at)
            VALUES ($1, $2, $3, COALESCE($4, ''), NOW())
            ON CONFLICT (session_id)
            DO UPDATE SET summary = $2, a_start_round = $3,
                early_summary = COALESCE($4, session_cache_state.early_summary),
                updated_at = NOW()
        """, session_id, summary_json, a_start_round, early_summary)


def _parse_utc_dt(s: str):
    """'2026-06-19 15:56:32' / ISO → UTC 感知 datetime(asyncpg timestamptz 参数要 datetime 对象,不能传 str)。"""
    from datetime import datetime, timezone
    s = (s or "").strip().replace("T", " ")
    s = s.split("+")[0].split(".")[0].strip()  # 去时区后缀/小数秒
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


async def count_conversations_since(session_id: str, since_iso: str) -> int:
    """统计某 session 在 since_iso(UTC)之后的对话消息数(回滚 dry 用)。"""
    dt = _parse_utc_dt(since_iso)
    pool = await get_pool()
    async with pool.acquire() as conn:
        return int(await conn.fetchval(
            "SELECT COUNT(*) FROM conversations WHERE session_id = $1 AND created_at > $2",
            session_id, dt) or 0)


async def delete_conversations_since(session_id: str, since_iso: str) -> int:
    """删某 session 在 since_iso(UTC)之后的对话消息,返回删除数(物理回滚到该时间点)。"""
    dt = _parse_utc_dt(since_iso)
    pool = await get_pool()
    async with pool.acquire() as conn:
        res = await conn.execute(
            "DELETE FROM conversations WHERE session_id = $1 AND created_at > $2",
            session_id, dt)
        try:
            return int(res.split()[-1])
        except Exception:
            return 0


async def count_memories_since(session_id: str, since_iso: str) -> int:
    """统计某 session 在 since_iso(UTC)之后提取的记忆碎片数(回忆墙不算)。回滚 dry 用。"""
    dt = _parse_utc_dt(since_iso)
    pool = await get_pool()
    async with pool.acquire() as conn:
        return int(await conn.fetchval(
            "SELECT COUNT(*) FROM memories WHERE source_session = $1 AND created_at > $2 AND mw_meta IS NULL",
            session_id, dt) or 0)


async def delete_memories_since(session_id: str, since_iso: str) -> int:
    """删某 session 在 since_iso(UTC)之后提取的记忆碎片(随对话删除一起回滚)。**回忆墙(mw_meta)永不删**。返回删除数。"""
    dt = _parse_utc_dt(since_iso)
    pool = await get_pool()
    async with pool.acquire() as conn:
        res = await conn.execute(
            "DELETE FROM memories WHERE source_session = $1 AND created_at > $2 AND mw_meta IS NULL",
            session_id, dt)
        try:
            return int(res.split()[-1])
        except Exception:
            return 0


# ============================================================
# 连根删自助:区间 [start, end?] 计数/备份/删除/恢复（end_iso=None=到现在）。回忆墙(mw_meta)永不进删除集。
# ============================================================
def _iso(v):
    try:
        return v.isoformat() if (v is not None and hasattr(v, "isoformat")) else v
    except Exception:
        return v


def _rowcount(res):
    try:
        return int(str(res).split()[-1])
    except Exception:
        return 0


async def count_conversations_between(session_id, start_iso, end_iso=None) -> int:
    s = _parse_utc_dt(start_iso)
    pool = await get_pool()
    async with pool.acquire() as conn:
        if end_iso:
            return int(await conn.fetchval("SELECT COUNT(*) FROM conversations WHERE session_id=$1 AND created_at>$2 AND created_at<=$3", session_id, s, _parse_utc_dt(end_iso)) or 0)
        return int(await conn.fetchval("SELECT COUNT(*) FROM conversations WHERE session_id=$1 AND created_at>$2", session_id, s) or 0)


async def fetch_conversations_between(session_id, start_iso, end_iso=None) -> list:
    s = _parse_utc_dt(start_iso)
    pool = await get_pool()
    async with pool.acquire() as conn:
        if end_iso:
            rows = await conn.fetch("SELECT session_id, role, content, model, created_at, metadata FROM conversations WHERE session_id=$1 AND created_at>$2 AND created_at<=$3 ORDER BY created_at", session_id, s, _parse_utc_dt(end_iso))
        else:
            rows = await conn.fetch("SELECT session_id, role, content, model, created_at, metadata FROM conversations WHERE session_id=$1 AND created_at>$2 ORDER BY created_at", session_id, s)
    return [{"session_id": r["session_id"], "role": r["role"], "content": r["content"],
             "model": r["model"], "created_at": _iso(r["created_at"]), "metadata": r["metadata"]} for r in rows]


async def delete_conversations_between(session_id, start_iso, end_iso=None) -> int:
    s = _parse_utc_dt(start_iso)
    pool = await get_pool()
    async with pool.acquire() as conn:
        if end_iso:
            res = await conn.execute("DELETE FROM conversations WHERE session_id=$1 AND created_at>$2 AND created_at<=$3", session_id, s, _parse_utc_dt(end_iso))
        else:
            res = await conn.execute("DELETE FROM conversations WHERE session_id=$1 AND created_at>$2", session_id, s)
    return _rowcount(res)


async def restore_conversations(rows) -> int:
    if not rows:
        return 0
    pool = await get_pool()
    async with pool.acquire() as conn:
        for r in rows:
            await conn.execute(
                "INSERT INTO conversations (session_id, role, content, model, created_at, metadata) VALUES ($1,$2,$3,$4,$5::text::timestamptz,$6)",
                r.get("session_id"), r.get("role"), r.get("content"), r.get("model"),
                (str(r["created_at"]) if r.get("created_at") else None), r.get("metadata"))
    return len(rows)


async def count_memories_between(session_id, start_iso, end_iso=None) -> int:
    s = _parse_utc_dt(start_iso)
    pool = await get_pool()
    async with pool.acquire() as conn:
        if end_iso:
            return int(await conn.fetchval("SELECT COUNT(*) FROM memories WHERE source_session=$1 AND created_at>$2 AND created_at<=$3 AND mw_meta IS NULL", session_id, s, _parse_utc_dt(end_iso)) or 0)
        return int(await conn.fetchval("SELECT COUNT(*) FROM memories WHERE source_session=$1 AND created_at>$2 AND mw_meta IS NULL", session_id, s) or 0)


async def fetch_memories_between(session_id, start_iso, end_iso=None) -> list:
    s = _parse_utc_dt(start_iso)
    cols = "content, importance, source_session, created_at, layer, title, is_active, valence, arousal, event_date"
    pool = await get_pool()
    async with pool.acquire() as conn:
        if end_iso:
            rows = await conn.fetch(f"SELECT {cols} FROM memories WHERE source_session=$1 AND created_at>$2 AND created_at<=$3 AND mw_meta IS NULL ORDER BY created_at", session_id, s, _parse_utc_dt(end_iso))
        else:
            rows = await conn.fetch(f"SELECT {cols} FROM memories WHERE source_session=$1 AND created_at>$2 AND mw_meta IS NULL ORDER BY created_at", session_id, s)
    return [{"content": r["content"], "importance": r["importance"], "source_session": r["source_session"],
             "created_at": _iso(r["created_at"]), "layer": r["layer"], "title": r["title"],
             "is_active": r["is_active"], "valence": r["valence"], "arousal": r["arousal"],
             "event_date": _iso(r["event_date"])} for r in rows]


async def delete_memories_between(session_id, start_iso, end_iso=None) -> int:
    s = _parse_utc_dt(start_iso)
    pool = await get_pool()
    async with pool.acquire() as conn:
        if end_iso:
            res = await conn.execute("DELETE FROM memories WHERE source_session=$1 AND created_at>$2 AND created_at<=$3 AND mw_meta IS NULL", session_id, s, _parse_utc_dt(end_iso))
        else:
            res = await conn.execute("DELETE FROM memories WHERE source_session=$1 AND created_at>$2 AND mw_meta IS NULL", session_id, s)
    return _rowcount(res)


async def restore_memories(rows) -> int:
    if not rows:
        return 0
    pool = await get_pool()
    async with pool.acquire() as conn:
        for r in rows:
            await conn.execute(
                "INSERT INTO memories (content, importance, source_session, created_at, layer, title, is_active, valence, arousal, event_date) "
                "VALUES ($1,$2,$3,$4::text::timestamptz,$5,$6,$7,$8,$9,$10::text::date)",
                r.get("content"), (r.get("importance") or 5), r.get("source_session"),
                (str(r["created_at"]) if r.get("created_at") else None),
                (r.get("layer") or 1), r.get("title"),
                (r.get("is_active") if r.get("is_active") is not None else True),
                (r.get("valence") or 0), (r.get("arousal") if r.get("arousal") is not None else 0.2),
                (str(r["event_date"]) if r.get("event_date") else None))
    return len(rows)


# ============================================================
# Token 使用记录
# ============================================================

async def ensure_token_usage_table():
    """确保token_usage表存在（在init_tables里调用）"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS token_usage (
                id              SERIAL PRIMARY KEY,
                session_id      TEXT,
                model           TEXT,
                prompt_tokens   INTEGER DEFAULT 0,
                completion_tokens INTEGER DEFAULT 0,
                total_tokens    INTEGER DEFAULT 0,
                usage_type      TEXT DEFAULT 'chat',
                created_at      TIMESTAMPTZ DEFAULT NOW()
            );
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_token_usage_created ON token_usage (created_at DESC);
        """)


async def save_token_usage(session_id: str, model: str, prompt_tokens: int, completion_tokens: int, total_tokens: int, usage_type: str = "chat"):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO token_usage (session_id, model, prompt_tokens, completion_tokens, total_tokens, usage_type)
            VALUES ($1, $2, $3, $4, $5, $6)
        """, session_id, model, prompt_tokens, completion_tokens, total_tokens, usage_type)


# ============================================================
# 对话记录管理
# ============================================================

async def get_conversations_paginated(page: int = 1, per_page: int = 20):
    offset = (page - 1) * per_page
    pool = await get_pool()
    async with pool.acquire() as conn:
        total_row = await conn.fetchrow(
            "SELECT COUNT(DISTINCT session_id) as total FROM conversations"
        )
        total = total_row['total'] if total_row else 0

        rows = await conn.fetch("""
            WITH session_info AS (
                SELECT session_id, MIN(created_at) as first_time, MAX(created_at) as last_time, COUNT(*) as message_count
                FROM conversations GROUP BY session_id ORDER BY last_time DESC LIMIT $1 OFFSET $2
            )
            SELECT si.*,
                   COALESCE(tu.total_all, 0) as total_tokens
            FROM session_info si
            LEFT JOIN (
                SELECT session_id, SUM(total_tokens) as total_all FROM token_usage WHERE usage_type = 'chat' GROUP BY session_id
            ) tu ON si.session_id = tu.session_id
            ORDER BY si.last_time DESC
        """, per_page, offset)
        
        results = []
        for r in rows:
            preview_row = await conn.fetchrow(
                "SELECT content FROM conversations WHERE session_id = $1 AND role = 'user' ORDER BY created_at LIMIT 1",
                r['session_id']
            )
            preview = preview_row['content'][:80] if preview_row else ''
            title = (preview[:30] + '...' if len(preview) > 30 else preview) or r['session_id']
            results.append({
                'session_id': r['session_id'],
                'title': title,
                'first_time': r['first_time'].isoformat() if r['first_time'] else None,
                'last_time': r['last_time'].isoformat() if r['last_time'] else None,
                'message_count': r['message_count'],
                'preview': preview,
                'total_tokens': r['total_tokens'],
            })
        return results, total


async def delete_conversation(session_id: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM conversations WHERE session_id = $1", session_id)
        await conn.execute("DELETE FROM session_cache_state WHERE session_id = $1", session_id)


async def batch_delete_conversations(session_ids: list):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM conversations WHERE session_id = ANY($1)", session_ids)
        await conn.execute("DELETE FROM session_cache_state WHERE session_id = ANY($1)", session_ids)


async def merge_sessions_to_target(source_ids: list, target_id: str) -> dict:
    if not source_ids:
        return {'merged_sessions': 0, 'merged_messages': 0, 'merged_token_records': 0}
    pool = await get_pool()
    async with pool.acquire() as conn:
        msg_count = await conn.fetchval("SELECT COUNT(*) FROM conversations WHERE session_id = ANY($1)", source_ids)
        await conn.execute("UPDATE conversations SET session_id = $1 WHERE session_id = ANY($2)", target_id, source_ids)
        token_count = await conn.fetchval("SELECT COUNT(*) FROM token_usage WHERE session_id = ANY($1)", source_ids)
        await conn.execute("UPDATE token_usage SET session_id = $1 WHERE session_id = ANY($2)", target_id, source_ids)
        await conn.execute("DELETE FROM session_cache_state WHERE session_id = ANY($1)", source_ids)
        return {'merged_sessions': len(source_ids), 'merged_messages': msg_count or 0, 'merged_token_records': token_count or 0}


async def list_all_session_cache_states() -> list:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT scs.session_id, scs.summary, scs.a_start_round, scs.updated_at,
                   COALESCE(c.message_count, 0) as message_count,
                   COALESCE(tu.chat_tokens, 0) as chat_tokens
            FROM session_cache_state scs
            LEFT JOIN (SELECT session_id, COUNT(*) as message_count FROM conversations GROUP BY session_id) c ON scs.session_id = c.session_id
            LEFT JOIN (SELECT session_id, SUM(total_tokens) as chat_tokens FROM token_usage WHERE usage_type = 'chat' GROUP BY session_id) tu ON scs.session_id = tu.session_id
            ORDER BY scs.updated_at DESC
        """)
        results = []
        for r in rows:
            raw_summary = r['summary'] or ''
            try:
                import json
                parsed = json.loads(raw_summary)
                if isinstance(parsed, list):
                    summary_parts = parsed
                else:
                    summary_parts = [raw_summary] if raw_summary else []
            except (json.JSONDecodeError, ValueError):
                summary_parts = [raw_summary] if raw_summary else []
            results.append({
                'session_id': r['session_id'],
                'summary': '\n\n'.join(summary_parts),
                'summary_length': sum(len(p) for p in summary_parts),
                'summary_count': len(summary_parts),
                'a_start_round': r['a_start_round'],
                'updated_at': r['updated_at'].isoformat() if r['updated_at'] else None,
                'message_count': r['message_count'],
                'chat_tokens': r['chat_tokens'],
            })
        return results


async def delete_session_cache_state(session_id: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM session_cache_state WHERE session_id = $1", session_id)


async def rename_session_id(old_id: str, new_id: str) -> bool:
    """重命名对话线ID（事务内同时修改三个表）"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # 检查新ID是否已存在
            exists = await conn.fetchval(
                "SELECT 1 FROM session_cache_state WHERE session_id = $1", new_id
            )
            if exists:
                return False
            # session_cache_state
            await conn.execute(
                "UPDATE session_cache_state SET session_id = $1 WHERE session_id = $2",
                new_id, old_id
            )
            # conversations
            await conn.execute(
                "UPDATE conversations SET session_id = $1 WHERE session_id = $2",
                new_id, old_id
            )
            # token_usage
            await conn.execute(
                "UPDATE token_usage SET session_id = $1 WHERE session_id = $2",
                new_id, old_id
            )
            return True


def db_row_to_message(row: dict) -> dict:
    """
    把DB记录还原成API消息格式。
    
    普通消息: {"role": "user", "content": "你好"} 
    工具调用: {"role": "assistant", "content": null, "tool_calls": [...]}
    工具结果: {"role": "tool", "content": "结果", "tool_call_id": "call_xxx"}
    思维链:   {"role": "assistant", "content": "回答", "reasoning_content": "思维链"}
    """
    import json as _json
    msg = {"role": row["role"], "content": row.get("content") or ""}
    
    meta_str = row.get("metadata")
    if meta_str:
        try:
            meta = _json.loads(meta_str)
            # assistant 带 tool_calls
            if "tool_calls" in meta:
                msg["tool_calls"] = meta["tool_calls"]
                if not row.get("content"):
                    msg["content"] = None
            # assistant 带 reasoning_content（deepseek thinking mode）
            if "reasoning_content" in meta:
                msg["reasoning_content"] = meta["reasoning_content"]
            # tool 消息带 tool_call_id
            if "tool_call_id" in meta:
                msg["tool_call_id"] = meta["tool_call_id"]
            # 其他可能的字段（name 等）
            if "name" in meta:
                msg["name"] = meta["name"]
        except Exception:
            pass
    
    return msg


async def export_all_conversations():
    """导出所有对话记录（用于备份）"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT session_id, role, content, model, created_at
            FROM conversations
            ORDER BY session_id, created_at
        """)
        return [
            {
                'session_id': r['session_id'],
                'role': r['role'],
                'content': r['content'],
                'model': r['model'] or '',
                'created_at': r['created_at'].isoformat() if r['created_at'] else None,
            }
            for r in rows
        ]


async def import_conversations(records: list):
    """
    导入对话记录（自动去重）
    
    records: [{ session_id, role, content, model?, created_at? }, ...]
    按 session_id + role + created_at 三元组去重，已存在的跳过。
    返回 (导入数量, 跳过数量)
    """
    if not records:
        return 0, 0
    
    pool = await get_pool()
    async with pool.acquire() as conn:
        imported = 0
        skipped = 0
        for r in records:
            session_id = r.get('session_id')
            role = r.get('role')
            content = r.get('content')
            
            if not all([session_id, role, content]):
                continue
            
            model = r.get('model', '')
            created_at = r.get('created_at')
            
            # 解析时间
            from datetime import datetime
            if created_at and isinstance(created_at, str):
                try:
                    created_at = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
                except:
                    created_at = None
            
            # 去重检查
            if created_at:
                existing = await conn.fetchrow("""
                    SELECT id FROM conversations
                    WHERE session_id = $1 AND role = $2 AND created_at = $3
                    LIMIT 1
                """, session_id, role, created_at)
                
                if existing:
                    skipped += 1
                    continue
                
                await conn.execute("""
                    INSERT INTO conversations (session_id, role, content, model, created_at)
                    VALUES ($1, $2, $3, $4, $5)
                """, session_id, role, content, model, created_at)
            else:
                await conn.execute("""
                    INSERT INTO conversations (session_id, role, content, model)
                    VALUES ($1, $2, $3, $4)
                """, session_id, role, content, model)
            
            imported += 1
        
        if skipped:
            print(f"📥 导入对话: {imported} 条新增, {skipped} 条已存在跳过")
        else:
            print(f"📥 导入对话: {imported} 条新增")
        
        return imported, skipped


# ============================================================
# 三层记忆架构（碎片/事件/核心）
# ============================================================

async def get_fragments_by_date(event_date):
    """获取指定日期的原始碎片（用于每日整理）"""
    # 把本地日期转成UTC时间范围，避免DATE()用UTC截断导致日期偏移
    local_tz = dt_timezone(timedelta(hours=TIMEZONE_HOURS))
    start_utc = datetime(event_date.year, event_date.month, event_date.day, tzinfo=local_tz).astimezone(dt_timezone.utc)
    end_utc = start_utc + timedelta(days=1)
    
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, content, importance, created_at
            FROM memories
            WHERE layer = 1 AND is_active = TRUE
            AND created_at >= $1 AND created_at < $2
            ORDER BY created_at
        """, start_utc, end_utc)
        return [dict(r) for r in rows]


async def get_fragments_by_date_range(start_date, end_date):
    """获取指定时间段的原始碎片（用于跨天整理）"""
    # 把本地日期转成UTC时间范围，避免DATE()用UTC截断导致日期偏移
    local_tz = dt_timezone(timedelta(hours=TIMEZONE_HOURS))
    start_utc = datetime(start_date.year, start_date.month, start_date.day, tzinfo=local_tz).astimezone(dt_timezone.utc)
    # end_date 当天结束 = end_date 下一天的 00:00
    end_utc = datetime(end_date.year, end_date.month, end_date.day, tzinfo=local_tz).astimezone(dt_timezone.utc) + timedelta(days=1)
    
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, content, importance, created_at
            FROM memories
            WHERE layer = 1 AND is_active = TRUE
            AND created_at >= $1 AND created_at < $2
            ORDER BY created_at
        """, start_utc, end_utc)
        return [dict(r) for r in rows]


async def create_event_memory(title: str, content: str, importance: int, 
                               event_date, merged_from: list):
    """创建事件记忆（从碎片合并而来）"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO memories (content, importance, layer, title, is_active, merged_from, event_date)
            VALUES ($1, $2, 2, $3, TRUE, $4, $5)
            RETURNING id
        """, content, importance, title, merged_from, event_date)
        
        new_id = row['id'] if row else None
        
        # 向量搜索：计算并保存 embedding
        if MEMORY_VECTOR_ENABLED and new_id:
            try:
                embedding = await compute_embedding(content)
                if embedding:
                    await save_memory_embedding(conn, new_id, embedding)
            except Exception as e:
                print(f"⚠️ 事件记忆embedding计算失败（id={new_id}）: {e}")
        
        return new_id


async def deactivate_memories(memory_ids: list):
    """将记忆标记为不活跃（合并后的碎片）"""
    if not memory_ids:
        return
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE memories SET is_active = FALSE
            WHERE id = ANY($1::int[])
        """, memory_ids)


async def promote_to_core(memory_id: int, title: str = None):
    """将记忆升级为核心记忆"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if title:
            await conn.execute("""
                UPDATE memories SET layer = 3, title = $2
                WHERE id = $1
            """, memory_id, title)
        else:
            await conn.execute("""
                UPDATE memories SET layer = 3
                WHERE id = $1
            """, memory_id)


async def merge_memories(memory_ids: list, new_title: str, new_content: str, 
                         importance: int, layer: int = 2):
    """合并多条记忆为一条新记忆"""
    if not memory_ids:
        return None
    
    pool = await get_pool()
    async with pool.acquire() as conn:
        # 获取原记忆的日期（取最早的）
        rows = await conn.fetch("""
            SELECT MIN(DATE(created_at)) as event_date
            FROM memories WHERE id = ANY($1::int[])
        """, memory_ids)
        event_date = rows[0]['event_date'] if rows else None
        
        # 创建新记忆
        row = await conn.fetchrow("""
            INSERT INTO memories (content, importance, layer, title, is_active, merged_from, event_date)
            VALUES ($1, $2, $3, $4, TRUE, $5, $6)
            RETURNING id
        """, new_content, importance, layer, new_title, memory_ids, event_date)
        
        new_id = row['id'] if row else None
        
        # 向量搜索：计算并保存 embedding
        if MEMORY_VECTOR_ENABLED and new_id:
            try:
                embedding = await compute_embedding(new_content)
                if embedding:
                    await save_memory_embedding(conn, new_id, embedding)
            except Exception as e:
                print(f"⚠️ 合并记忆embedding计算失败（id={new_id}）: {e}")
        
        # 将原记忆标记为不活跃
        if new_id:
            await deactivate_memories(memory_ids)
        
        return new_id


async def check_duplicate_memory(new_content: str, threshold: float = 0.7) -> dict:
    """检查新记忆是否与现有记忆重复
    
    三层去重策略：
    1. 精确匹配：内容完全相同
    2. 包含关系：新内容包含旧内容，或旧内容包含新内容
    3. 关键词重叠度：Jaccard 相似度 > threshold
    
    Returns:
        {
            "is_duplicate": bool,
            "reason": str,  # "exact" / "containment" / "similarity"
            "matched_id": int or None,
            "similarity": float or None
        }
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        # 获取所有活跃记忆
        rows = await conn.fetch("""
            SELECT id, content FROM memories 
            WHERE is_active = TRUE
        """)
        
        new_content_lower = new_content.strip().lower()
        new_keywords = set(extract_search_keywords(new_content))
        
        for row in rows:
            old_content = row['content']
            old_content_lower = old_content.strip().lower()
            
            # 第一层：精确匹配
            if new_content_lower == old_content_lower:
                return {
                    "is_duplicate": True,
                    "reason": "exact",
                    "matched_id": row['id'],
                    "similarity": 1.0
                }
            
            # 第二层：包含关系
            if new_content_lower in old_content_lower:
                return {
                    "is_duplicate": True,
                    "reason": "containment",
                    "matched_id": row['id'],
                    "similarity": len(new_content) / len(old_content)
                }
            if old_content_lower in new_content_lower:
                return {
                    "is_duplicate": True,
                    "reason": "containment_update",
                    "matched_id": row['id'],
                    "similarity": len(old_content) / len(new_content)
                }
            
            # 第三层：关键词重叠度（Jaccard 相似度）
            old_keywords = set(extract_search_keywords(old_content))
            if new_keywords and old_keywords:
                intersection = new_keywords & old_keywords
                union = new_keywords | old_keywords
                similarity = len(intersection) / len(union) if union else 0
                
                if similarity > threshold:
                    return {
                        "is_duplicate": True,
                        "reason": "similarity",
                        "matched_id": row['id'],
                        "similarity": similarity
                    }
        
        return {
            "is_duplicate": False,
            "reason": None,
            "matched_id": None,
            "similarity": None
        }


async def update_memory_with_layer(memory_id: int, content: str = None, 
                                    importance: int = None, title: str = None,
                                    layer: int = None, is_active: bool = None):
    """更新记忆（支持三层架构新字段）"""
    updates = []
    params = []
    param_idx = 2  # $1 给 memory_id
    
    if content is not None:
        updates.append(f"content = ${param_idx}")
        params.append(content)
        param_idx += 1
    
    if importance is not None:
        updates.append(f"importance = ${param_idx}")
        params.append(importance)
        param_idx += 1
    
    if title is not None:
        updates.append(f"title = ${param_idx}")
        params.append(title)
        param_idx += 1
    
    if layer is not None:
        updates.append(f"layer = ${param_idx}")
        params.append(layer)
        param_idx += 1
    
    if is_active is not None:
        updates.append(f"is_active = ${param_idx}")
        params.append(is_active)
        param_idx += 1
    
    if not updates:
        return
    
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            f"UPDATE memories SET {', '.join(updates)} WHERE id = $1",
            memory_id, *params
        )


async def get_layer_statistics():
    """获取各层记忆的统计数据"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT 
                layer,
                COUNT(*) as count,
                COUNT(*) FILTER (WHERE is_active = TRUE) as active_count
            FROM memories
            GROUP BY layer
            ORDER BY layer
        """)
        
        stats = {
            "layer_1": {"total": 0, "active": 0},  # 原始碎片
            "layer_2": {"total": 0, "active": 0},  # 事件记忆
            "layer_3": {"total": 0, "active": 0},  # 核心记忆
        }
        
        for row in rows:
            layer = row['layer'] or 1  # 默认为层级1
            key = f"layer_{layer}"
            if key in stats:
                stats[key] = {
                    "total": row['count'],
                    "active": row['active_count']
                }
        
        return stats


async def cleanup_old_fragments(days: int = 30):
    """清理指定天数前的归档碎片
    
    只清理满足以下条件的记忆：
    - layer = 1（原始碎片）
    - is_active = FALSE（已归档）
    - created_at 在 days 天之前
    - decayed_at IS NULL（②衰减归档的项豁免——归档≠删除,记忆不能丢；这类靠 reactivate 复活,绝不硬删）

    Returns:
        删除的记忆数量
    """
    from datetime import datetime, timedelta

    pool = await get_pool()
    async with pool.acquire() as conn:
        cutoff_date = datetime.now() - timedelta(days=days)

        result = await conn.execute("""
            DELETE FROM memories
            WHERE layer = 1
            AND is_active = FALSE
            AND decayed_at IS NULL
            AND created_at < $1
        """, cutoff_date)
        
        # 解析删除数量，格式如 "DELETE 5"
        deleted = int(result.split()[-1]) if result else 0
        return deleted


async def revert_merge(memory_id: int):
    """撤回合并操作
    
    恢复原始碎片（is_active = TRUE），删除合并后的事件记忆
    
    Args:
        memory_id: 要撤回的事件记忆ID
        
    Returns:
        {"status": "ok", "restored": 恢复的碎片数量}
        或 {"error": "错误信息"}
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        # 获取事件记忆信息
        row = await conn.fetchrow("""
            SELECT id, layer, merged_from FROM memories WHERE id = $1
        """, memory_id)
        
        if not row:
            return {"error": "记忆不存在"}
        
        if row['layer'] != 2:
            return {"error": "只能撤回事件记忆的合并"}
        
        merged_from = row['merged_from']
        if not merged_from or len(merged_from) == 0:
            return {"error": "没有合并来源，无法撤回"}
        
        # 恢复原始碎片
        result = await conn.execute("""
            UPDATE memories SET is_active = TRUE
            WHERE id = ANY($1::int[])
        """, merged_from)
        restored = int(result.split()[-1]) if result else 0
        
        # 删除事件记忆
        await conn.execute("""
            DELETE FROM memories WHERE id = $1
        """, memory_id)
        
        return {"status": "ok", "restored": restored}
