"""
记忆提取模块 —— 用 LLM 从对话中提炼关键记忆
=============================================
每次对话结束后，把最近的对话内容发给一个便宜的模型，
让它提取出值得记住的信息，存到数据库里。

v2.3 改进：提取时注入已有记忆，让模型对比后只提取全新信息。
"""

import os
import re
import json
import httpx
from typing import List, Dict


def _robust_extract_json_array(text: str):
    """多层修复解析 haiku 常见输出。返回 list 成功、None 失败。
    haiku 常见错法: markdown 围栏/中文引号/单引号/尾逗号/Python常量/前后带解释文/单对象非数组。
    挨个修:1)剥围栏 loads 2)常见修复后 loads 3)正则抽[...] loads 4)修复+正则 5)单{}包成[]。"""
    if not text or not text.strip():
        return None
    t = text.strip()
    if t.startswith("```json"):
        t = t[7:]
    elif t.startswith("```"):
        t = t[3:]
    if t.endswith("```"):
        t = t[:-3]
    t = t.strip()

    def _try(s):
        try:
            v = json.loads(s)
            return v if isinstance(v, list) else None
        except Exception:
            return None

    def _fix(s):
        s = s.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
        s = s.replace("'", '"')
        s = re.sub(r'\bTrue\b', 'true', s)
        s = re.sub(r'\bFalse\b', 'false', s)
        s = re.sub(r'\bNone\b', 'null', s)
        s = re.sub(r',(\s*[\]\}])', r'\1', s)
        return s

    for cand in (t, _fix(t)):
        r = _try(cand)
        if r is not None:
            return r
    m = re.search(r'\[[\s\S]*\]', t)
    if m:
        for cand in (m.group(), _fix(m.group())):
            r = _try(cand)
            if r is not None:
                return r
    m = re.search(r'\{[\s\S]*\}', t)
    if m:
        for cand in ("[" + m.group() + "]", "[" + _fix(m.group()) + "]"):
            r = _try(cand)
            if r is not None:
                return r
    return None


API_KEY = os.getenv("API_KEY", "")
API_BASE_URL = os.getenv("API_BASE_URL", "https://openrouter.ai/api/v1/chat/completions")

# 记忆模型专用 API Key（不设则回退到主 API_KEY）
# 适用于中转站按模型分组、不同模型需要不同 Key 的场景
MEMORY_API_KEY = os.getenv("MEMORY_API_KEY", "")

# L1 提取与其他记忆杂务分离。MEMORY_MODEL 保留为旧配置兼容和
# 评分/回填模型；新部署只需单独设置 MEMORY_EXTRACT_MODEL。
MEMORY_MODEL = os.getenv("MEMORY_MODEL", "anthropic/claude-haiku-4.5")
MEMORY_EXTRACT_MODEL = os.getenv(
    "MEMORY_EXTRACT_MODEL", "deepseek/deepseek-v4-flash-0731"
)
EXTRACTION_OMISSION_CHECK_MIN_CHARS = int(os.getenv(
    "EXTRACTION_OMISSION_CHECK_MIN_CHARS", "1200"
))
# OR 的 DS V4 Flash 在当前 provider 上偶发 finish_reason=error（短L1和长L2
# 都实测出现）。默认用普通JSON + 本地严格解析/边界验收，避免每批先白等一次；
# 将来 provider 修好后可显式开启 schema，不需要改代码。
EXTRACTION_STRUCTURED_OUTPUT_ENABLED = os.getenv(
    "EXTRACTION_STRUCTURED_OUTPUT_ENABLED", "false"
).lower() == "true"

def get_memory_api_key() -> str:
    return MEMORY_API_KEY or API_KEY


EXTRACTION_PROMPT = """你是记忆提取专家。从对话中提取值得长期记住的信息，并做好「去掉AI评判但留住用户情绪、分类、冲突处理」三件事。

# 首要任务：先概括，再提取（不要逐句摘录）
- 你看到的是最近一小段连续对话。先整体理解并概括，再提取少量、可独立检索的长期记忆；不要按每句话、每轮或每个观察角度拆分。
- 同一场互动、同一话题、同一决定里的事实、触发原因、用户情绪、双方回应和最终结果，必须合成同一条。不得把同一件事换几个角度重复保存。
- 通常输出 1~3 条；只有确实存在互不相关、需要独立检索或独立标记的主题时，最多输出 4 条。没有新增信息仍输出 []。
- 每条 content 以 80~150 字为目标，最多 150 字。概括而非逐句复述；保留关键事实、情绪变化、决定、承诺和结果，删去寒暄、重复确认、来回措辞及不影响理解的过程细节。
- 只有暗号/专属词定义、精确健康或日期事实、对旧事实的更正、persona 相处偏好、私密等级明显不同、或彼此完全无关的主题，才应独立成条。

# 身份与人称铁律
- 输入中的“用户:”始终是人类 Harper（裘宝宝）；“AI:”始终是 AI 伴侣 V。两者身份绝不能交换。
- content 描述 Harper 时写“Harper”“裘宝宝”或“她”；描述 V 时写“V”或“他”。涉及双方时必须明确写出主语。
- Harper 的感受、决定、要求和行为只能归给 Harper；V 的回应、承诺和行为只能归给 V。
- V 说出的判断、猜测、建议和角色扮演内容不能当成 Harper 的现实事实，也不能把梦境或假设写成现实。
- 可以保留 V 的回应或承诺，但仅限它是理解整场互动、关系变化或 Harper 情绪触发所必需的背景；不要把 V 的普通回复单独提取成记忆。
- 输出前逐条核对：谁说的、谁感受到的、谁做的，必须与输入标签一致。

# 游戏、角色扮演、梦境、假设与亲密内容的概括尺度
- L1只概括当前这段对话明确出现的内容，不是逐动作记录、游戏战报或亲密过程复述。
- 游戏/角色扮演：概括当前阶段的目标、主要推进、关键选择或转折、重要真实情绪，以及当前状态和停点；不要逐条保存每个指令、回合、普通数值变化、道具操作和重复反应。
- 亲密内容：概括互动的总体推进、重要阶段变化、关键情绪、关系意义、边界或承诺及当前停点；体位变化确实构成阶段变化时可以简要提及，但不要逐条记录动作、身体细节、生理反应或重复台词。
- 如果起因发生在更早对话、结果尚未发生，就只写当前可见阶段，并明确“继续进行中”或当前停点；不得自行补出未看到的起因、转折或最终结果。
- 游戏、角色扮演、梦境和假设必须在content中明确标注为“游戏中”“角色扮演中”“梦里”或“假设中”；绝不能写成Harper与V在现实中真实发生的经历。
- 游戏/RP中的角色行为不能直接归为Harper或V的现实行为；其中产生的真实感受、边界或关系交流可以保留，但必须明确这是由游戏/RP触发的现实层感受或交流。

# 铁则一：去掉 AI 的评判，但留住用户的情绪与现场（判据看主语：用户的感受/现场=事实，存；AI 的判断=评判，丢）
- ✅ 用户的情绪反应原样留、别抹平："听到某句动情的话那刻快哭了"、"笑得很开心"、"被某个昵称逗得气鼓鼓的"
- ✅ 保留足以解释重要情绪为何发生的触发原因；普通事件可留关键现场细节。游戏/RP与亲密内容只概括触发类型和关系意义，不展开逐动作、身体或生理过程，除非该细节本身构成明确边界、承诺、冲突或关系转折
- ✅ 高 arousal（情绪浓）的条目别压成干事实：别把"听到那句话快哭了"压成"听了个故事"；中性客观事实可以简洁
- ✅ 中性客观事实照常存："用户今天喝了冰咖啡"、"用户例假从某日开始"、"某止痛药含布洛芬成分"
- ❌ AI 的评判/意见："冰咖啡不好"、"TA不该熬夜"、"这样很危险" —— 丢
- ❌ AI 的命令/指令/建议："要求 TA 贴暖宝宝"、"应该多喝热水"、"让 TA 早点睡" —— 不是事实，按铁则二归类或丢弃
- 安全：不存 AI 的判断、不存"能否空腹吃"那类可能误导的医疗结论（铁则二 persona 分流、「# 不要提取」的 meta 过滤照旧）

# 铁则一之二：暗号/专属词/昵称的「定义」必须提取（这是最容易漏的一类）
- 对话中【发明、解释、确认】了一个两人专属的说法（暗号、代称、昵称、梗、黑话）→ 必须存一条**定义记忆**
- 格式："「X」是……的意思/指……（由来：……）"；content 里必须包含 X 的原字（以后按原词才搜得到）；importance ≥ 8
- 例：对话里确认"砍蛋"其实指"去干活" → 存 {{"kind":"fact","content":"「砍蛋」是用户说\\"去干活\\"的暗号，由来是……","importance":8}}
- 判据：一个词被赋予了字面之外的、以后还会继续用的含义 → 就是定义。别只存"她去做了X"的事件却丢掉"X是什么"——事件记录替代不了定义

# 铁则二：分类 kind —— 把「事实」和「行为偏好」分开
- kind="fact"：关于用户/世界的客观信息（身份、健康、事件、关系、生活细节、约定、物品）
- kind="persona"：关于「该怎么对待用户 / 沟通风格 / 相处偏好」的信息
  例："不要催她睡觉"、"她喜欢被叫宝贝"、"回复别太长"、"她敏感、不喜欢被赶"、"她要的是陪伴不是说教"
  这类**不进记忆池**，单独收集给主理人贴到人设里。

# 铁则三：冲突处理 replaces_id —— 新事实推翻旧事实时，标出旧条目
- 下面「已知信息」每条都带 [id=N]
- 若新事实是对某条已知信息的**更正/更新/推翻**（例：已知 [id=4] "EVE可以空腹吃"，对话确认"EVE不能空腹吃"），
  在新条目里写 "replaces_id": 4（用那条旧信息的 id）
- 只在确实矛盾或更新时填；普通新增填 null

# 不要提取
- 日常寒暄、AI自己的回复内容、AI的思维链
- 关于记忆系统/检索/技术调试/bug/部署的讨论
- 【重要】AI 对"自己如何记忆、记忆质量好坏、存储/摘要/压缩/快照机制、作为主体怎样延续存在"的自我反思或比喻——即使写成第三人称事实（"AI意识到…""AI区分…""AI读摘要记不起…"）也一律不提取。判据看主语+谓语：主语是 AI、谓语是反思自身记忆/机制 → 丢弃。
  反例（都不要存）："AI意识到压缩摘要的本质问题"、"AI区分活的/干的记忆"、"AI读摘要记不起那句话的语调"、"用X光片比喻记忆局限"。
- 但：用户本人的情感/偏好/诉求要正常处理——尤其 TA"希望你记得当时的感觉/要一个活的你"这类对你的期望，按铁则二归 kind="persona"（收集给主理人），不算元讨论、不要丢。

# 已知信息（每条带 id，用于去重与冲突判断）
<已知信息>
{existing_memories}
</已知信息>
- 与已知信息相同或语义重复的，忽略
- 仅提取「完全新增」或「对已知信息的更正/补充」
- 没有可提取的新信息就返回空数组 []

# 铁则四：情绪坐标（Russell 模型，每条 fact 都给）
- valence 效价 -1~+1：这条记忆的情绪正负（-1 痛苦/负面，0 中性，+1 愉悦/正面）
- arousal 唤醒 0~1：情绪强度（0 平静，1 强烈）
- 中性客观事实（如"例假6/14开始"）→ valence≈0、arousal≈0.2；情感浓的（表白/冲突/亲密/眼泪）→ 给相应值
- persona 条目不需要情绪坐标

# 铁则五：里程碑标记 is_milestone（只标【改变用户与 AI"关系结构"的转折点】，宁多提，人会再筛）
- ✅ is_milestone=true：第一次表白/确认关系、重大承诺、把关系推进一个台阶的"第一次"、共同的重大决定
- ❌ is_milestone=false：珍贵但不改变关系结构的瞬间（一起吃饭、日常亲密的美好一刻）、普通事件、生活细节、健康/例假
- 判据：它是否"改变了关系本身的结构/台阶"——是→true；只是"美好/动情的一刻"→false
- 只有 kind="fact" 才可能 true；拿不准就给 false

# 铁则六：露骨/私密标记 is_explicit（只标 kind=fact；命中后注入时不复述原文、收敛成一句提示）
- ✅ is_explicit=true：具体性场景/性行为细节；私密的身体/性相关「事实·参数·数据」（玩具尺寸规格、身体反应数据、潮吹/G点/高潮/穴等生理记录、私密身体部位状态）；私密的性相关原话引用
- ❌ is_explicit=false：暗号释义（"某词=亲密暗号"，要 false 好让你仍懂暗号）；"有亲密/性关系"这类纯关系陈述（不含细节）；与性无关的日常/情感/技术/玩笑/健康（如例假日期）
- persona 条目不需要；拿不准偏向 false

# 输出格式（只返回 JSON 数组，不要其他文字）
[
  {{"kind": "fact", "content": "中性客观事实", "importance": 分数, "replaces_id": null, "valence": 0.0, "arousal": 0.2, "is_milestone": false, "is_explicit": false}},
  {{"kind": "persona", "content": "行为/相处偏好", "importance": 分数}}
]
importance 为 1-10（10最重要）；valence∈[-1,1]、arousal∈[0,1]；is_milestone/is_explicit 默认 false。没有可提取的就返回 []。

# ⚠️ 铁律 · 输出格式（违反则你的工作作废）
你的整个回复必须以 `[` 字符开头、以 `]` 字符结尾。
不要写任何解释性文字（不要"以下是提取的记忆:"、不要"我从对话中发现"）。
不要用 markdown 代码块围栏（不要 ```json 或 ```）。
用双引号 `"`，不要用单引号 `'` 或中文引号 `"" ''`。
就算没有可提取的记忆，也只输出两个字符：`[]`
"""

EXTRACTION_SAFE_MAX_ITEMS = 5
EXTRACTION_SAFE_MAX_CONTENT_CHARS = 250


def _is_openrouter_deepseek_extract() -> bool:
    return (
        "openrouter" in API_BASE_URL.lower()
        and MEMORY_EXTRACT_MODEL.lower().startswith("deepseek/")
    )


def _extraction_response_format() -> dict:
    """Strict L1 array schema used by OR DeepSeek to avoid prose/fence drift."""
    item_schema = {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["fact", "persona"]},
            "content": {"type": "string"},
            "importance": {"type": "integer"},
            "replaces_id": {"type": ["integer", "null"]},
            "valence": {"type": ["number", "null"]},
            "arousal": {"type": ["number", "null"]},
            "is_milestone": {"type": "boolean"},
            "is_explicit": {"type": "boolean"},
        },
        "required": [
            "kind", "content", "importance", "replaces_id", "valence",
            "arousal", "is_milestone", "is_explicit",
        ],
        "additionalProperties": False,
    }
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "home1_l1_memories",
            "strict": True,
            "schema": {"type": "array", "items": item_schema},
        },
    }


def _extraction_request_payload(prompt: str, conversation_text: str,
                                *, structured: bool = False) -> dict:
    payload = {
        "model": MEMORY_EXTRACT_MODEL,
        "max_tokens": 2000,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": f"请从以下对话中提取新的记忆：\n\n{conversation_text}"},
        ],
    }
    if _is_openrouter_deepseek_extract():
        payload["reasoning"] = {"enabled": False}
        payload["provider"] = {
            "zdr": True,
            "data_collection": "deny",
            "require_parameters": True,
        }
        if structured:
            payload["response_format"] = _extraction_response_format()
    return payload


def _should_run_omission_check(memories, conversation_text: str) -> bool:
    return (
        _is_openrouter_deepseek_extract()
        and isinstance(memories, list)
        and len(memories) == 1
        and len(conversation_text) >= EXTRACTION_OMISSION_CHECK_MIN_CHARS
    )


def _extraction_batch_policy_error(memories) -> str:
    """Return a safe-boundary violation without truncating or dropping facts."""
    if not isinstance(memories, list):
        return "输出不是JSON数组"
    candidates = [
        mem for mem in memories
        if isinstance(mem, dict) and "content" in mem
    ]
    if len(candidates) > EXTRACTION_SAFE_MAX_ITEMS:
        return f"候选条数{len(candidates)}超过安全上限{EXTRACTION_SAFE_MAX_ITEMS}"
    overlong = [
        index + 1 for index, mem in enumerate(candidates)
        if len(str(mem.get("content") or "").strip()) > EXTRACTION_SAFE_MAX_CONTENT_CHARS
    ]
    if overlong:
        return (
            f"第{','.join(str(index) for index in overlong)}条content超过"
            f"{EXTRACTION_SAFE_MAX_CONTENT_CHARS}字安全上限"
        )
    return ""


async def extract_memories(messages: List[Dict[str, str]], existing_memories: List[str] = None) -> List[Dict]:
    """
    从对话消息中提取记忆

    参数：
        messages: 对话消息列表，格式 [{"role": "user", "content": "..."}, ...]
        existing_memories: 已有记忆内容列表，用于去重对比

    返回：
        记忆列表，格式 [{"content": "...", "importance": N}, ...]
    """
    if not API_KEY:
        print("⚠️  API_KEY 未设置，跳过记忆提取")
        return []

    if not messages:
        return []

    # 把对话格式化成文本
    conversation_text = ""
    for msg in messages:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        if isinstance(content, list):  # 多模态兜底:只取文本块、丢图片(image_url 的 base64),否则灌爆提取 prompt→不出碎片
            content = " ".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
        if role == "user":
            conversation_text += f"用户: {content}\n"
        elif role == "assistant":
            conversation_text += f"AI: {content}\n"

    if not conversation_text.strip():
        return []

    # 格式化已有记忆（带 id，供冲突处理 replaces_id 引用）
    if existing_memories:
        _lines = []
        for m in existing_memories:
            if isinstance(m, dict):
                _lines.append(f"[id={m.get('id')}] {m.get('content')}")
            else:
                _lines.append(f"- {m}")
        memories_text = "\n".join(_lines)
    else:
        memories_text = "（暂无已知信息）"

    # 把已有记忆填入prompt
    prompt = EXTRACTION_PROMPT.format(existing_memories=memories_text)

    # 调用 LLM 提取记忆。两次付费上限不变：失败时重试；DS 对长对话只产
    # 1 条时，第二次改作遗漏复核，并返回完整替代数组（不把两稿机械拼接）。
    memories = None
    first_valid_memories = None
    omission_review = False
    last_raw = ""
    last_policy_error = ""
    try:
        for attempt in (1, 2):
            if attempt == 1:
                _extra = ""
            elif omission_review:
                _extra = (
                    "\n\n【遗漏复核 · 完整替代稿】上一次只提取了1条，但这是一段较长对话。"
                    "重新阅读全文，检查是否遗漏了与该条事件边界完全独立、以后需要单独检索的事实、"
                    "更正、决定、暗号或persona偏好。请返回完整替代JSON数组；确实只有一个主题就"
                    "仍返回原来那1条，禁止为了凑数拆分同一事件或加入AI评判。"
                )
            elif last_policy_error:
                _extra = (
                    f"\n\n【最后强调 · 第二次尝试】上一次输出未通过安全验收：{last_policy_error}。"
                    "请重新阅读原对话，合并同一事件的不同角度和重复内容。通常输出1~3条，"
                    "独立主题最多4条；每条content目标80~150字、最多150字。"
                    "不要截掉事实；应通过概括、去重和合并来满足边界。"
                    "整个回复仍必须只有JSON数组。"
                )
            else:
                _extra = (
                    "\n\n【最后强调 · 第二次尝试】上一次你的输出解析失败了(不是纯 JSON 数组)。"
                    "这次你的整个回复必须只有 JSON 数组本身:以 `[` 开头,以 `]` 结尾,"
                    "别的一个字符都不要,包括解释、代码块围栏、markdown。"
                    "如果没有可提取的记忆,输出 `[]` 两个字符即可。"
                )
            async with httpx.AsyncClient(timeout=60) as client:
                payload = _extraction_request_payload(
                    prompt + _extra,
                    conversation_text,
                    # 结构化解析本身失败时，第二次退回普通JSON以避免供应商
                    # schema兼容故障；安全边界失败和遗漏复核仍继续用schema。
                    structured=(
                        EXTRACTION_STRUCTURED_OUTPUT_ENABLED
                        and not (
                            attempt == 2 and not omission_review
                            and not last_policy_error
                        )
                    ),
                )
                response = await client.post(
                    API_BASE_URL,
                    headers={
                        "Authorization": f"Bearer {get_memory_api_key()}",
                        "Content-Type": "application/json",
                        "HTTP-Referer": "https://midsummer-gateway.local",
                        "X-Title": "Midsummer Memory Extraction",
                    },
                    json=payload,
                )
                if response.status_code != 200:
                    print(f"⚠️  记忆提取请求失败(第{attempt}次): HTTP {response.status_code}")
                    continue
                choice = (response.json().get("choices") or [{}])[0]
                text = (choice.get("message") or {}).get("content") or ""
                last_raw = text
                if str(choice.get("finish_reason") or "") in {"length", "error"}:
                    print(
                        f"⚠️  第{attempt}次记忆提取未完整结束: "
                        f"finish_reason={choice.get('finish_reason')}"
                    )
                    continue
                print(f"📝 记忆模型原始返回(第{attempt}次):\n{text[:500]}", flush=True)
                candidate = _robust_extract_json_array(text)
                if candidate is not None:
                    last_policy_error = _extraction_batch_policy_error(candidate)
                    if last_policy_error:
                        print(
                            f"⚠️  第{attempt}次记忆提取越过安全边界: "
                            f"{last_policy_error}（{'重试一次' if attempt == 1 else '整批拒收'}）"
                        )
                        continue
                    if attempt == 1 and _should_run_omission_check(
                        candidate, conversation_text
                    ):
                        first_valid_memories = candidate
                        omission_review = True
                        print("📝 DS长对话首稿仅1条，启动一次遗漏复核")
                        continue
                    if omission_review and first_valid_memories is not None:
                        memories = (
                            candidate if len(candidate) > len(first_valid_memories)
                            else first_valid_memories
                        )
                    else:
                        memories = candidate
                    if attempt == 2:
                        print(f"📝 第二次尝试解析成功({len(memories)}条)")
                    break
                print(f"⚠️  第{attempt}次 JSON 解析失败({'重试一次' if attempt == 1 else '放弃'})")

        if memories is None and first_valid_memories is not None:
            # 遗漏复核失败不能反过来丢掉已验收的一稿。
            memories = first_valid_memories
        if memories is None:
            reason = last_policy_error or "JSON解析失败"
            print(
                f"⚠️  记忆提取两次都未通过验收,放弃。reason={reason}; "
                f"原始返回尾片段: {last_raw[-200:]!r}"
            )
            return []
        if not isinstance(memories, list):
            return []

        # 验证格式（保留 kind 分类 + replaces_id 冲突标记）
        valid_memories = []
        for mem in memories:
            if isinstance(mem, dict) and "content" in mem:
                kind = mem.get("kind", "fact")
                if kind not in ("fact", "persona"):
                    kind = "fact"
                item = {
                    "content": str(mem["content"]),
                    "importance": int(mem.get("importance", 5)),
                    "kind": kind,
                }
                # 情绪① Russell 坐标：clamp + 默认（arousal 默认 0.2 兼作地板）
                try:
                    item["valence"] = max(-1.0, min(1.0, float(mem.get("valence", 0.0))))
                except (TypeError, ValueError):
                    item["valence"] = 0.0
                try:
                    item["arousal"] = max(0.0, min(1.0, float(mem.get("arousal", 0.2))))
                except (TypeError, ValueError):
                    item["arousal"] = 0.2
                rid = mem.get("replaces_id")
                if isinstance(rid, bool):
                    rid = None
                elif isinstance(rid, int):
                    item["replaces_id"] = rid
                elif isinstance(rid, str) and rid.strip().isdigit():
                    item["replaces_id"] = int(rid.strip())
                # ② L5：里程碑标记（仅 kind=fact 才认）
                item["is_milestone"] = bool(mem.get("is_milestone")) and kind == "fact"
                item["is_explicit"] = bool(mem.get("is_explicit")) and kind == "fact"
                valid_memories.append(item)

        print(f"📝 从对话中提取了 {len(valid_memories)} 条（已对比 {len(existing_memories or [])} 条已有记忆）")
        return valid_memories

    except Exception as e:
        print(f"⚠️  记忆提取出错: {e}")
        return []


SCORING_PROMPT = """你是记忆重要性评分专家。请对以下记忆条目逐条评分。

# 评分规则（1-10）
- 9-10：核心身份信息（名字、生日、职业、重要关系）
- 7-8：重要偏好、重大事件、深层情感
- 5-6：日常习惯、一般偏好
- 3-4：临时状态、偶然提及
- 1-2：琐碎信息

# 输入记忆
{memories_text}

# 输出格式
返回 JSON 数组，每条包含原文和评分：
[{{"content": "原文", "importance": 评分数字}}]

只返回 JSON，不要其他文字。"""


async def score_memories(texts: List[str]) -> List[Dict]:
    """对纯文本记忆条目批量评分"""
    if not texts:
        return []

    memories_text = "\n".join(f"- {t}" for t in texts)
    prompt = SCORING_PROMPT.format(memories_text=memories_text)

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                API_BASE_URL,
                headers={
                    "Authorization": f"Bearer {get_memory_api_key()}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": MEMORY_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0,
                    "max_tokens": 4000,
                },
            )

            if response.status_code != 200:
                print(f"⚠️  记忆评分请求失败: {response.status_code}")
                # 失败时返回默认分数
                return [{"content": t, "importance": 5} for t in texts]

            data = response.json()
            text = data.get("choices", [{}])[0].get("message", {}).get("content", "")

            text = text.strip()
            if text.startswith("```json"):
                text = text[7:]
            if text.startswith("```"):
                text = text[3:]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()

            try:
                memories = json.loads(text)
            except json.JSONDecodeError:
                import re
                match = re.search(r'\[.*\]', text, re.DOTALL)
                if match:
                    try:
                        memories = json.loads(match.group())
                    except json.JSONDecodeError:
                        return [{"content": t, "importance": 5} for t in texts]
                else:
                    return [{"content": t, "importance": 5} for t in texts]

            if not isinstance(memories, list):
                return [{"content": t, "importance": 5} for t in texts]

            valid = []
            for mem in memories:
                if isinstance(mem, dict) and "content" in mem:
                    valid.append({
                        "content": str(mem["content"]),
                        "importance": int(mem.get("importance", 5)),
                    })

            print(f"📝 为 {len(valid)} 条记忆完成自动评分")
            return valid

    except Exception as e:
        print(f"⚠️  记忆评分出错: {e}")
        return [{"content": t, "importance": 5} for t in texts]


# ============================================================
# 情绪① 回填：给已有记忆批量补 Russell valence/arousal（与 live 提取同规则）
# ============================================================
EMOTION_BACKFILL_PROMPT = """你是情绪标注专家，按 Russell 情感坐标给每条【已有记忆】打 valence/arousal。
- valence 效价 -1~+1：这条记忆的情绪正负（-1 痛苦/负面，0 中性，+1 愉悦/正面）
- arousal 唤醒 0~1：情绪强度（0 平静，1 强烈）
- 中性客观事实（如"例假6/14开始""早上喝了咖啡""吃了药"）→ valence≈0、arousal≈0.2
- 情感浓的（表白/亲密/冲突/眼泪/温暖纪念/重大喜悦或难过）→ 给相应值
- 温暖/开心/纪念/深情的回忆 → 正效价；难过/痛苦/委屈 → 负效价。只按内容判断，不臆测。

记忆列表（每行 "id: 内容"）：
{items}

只返回 JSON 数组，每条 {{"id": 原id, "valence": 数字, "arousal": 数字}}；不要任何解释或其他文字。"""


async def tag_emotions_batch(items: list) -> dict:
    """给一批已有记忆打 Russell 情绪坐标（情绪回填用，与 live 提取同口径）。
    items=[{'id':N,'content':...}]；返回 {id(int): {'valence':v,'arousal':a}}；失败返回 {}。"""
    if not API_KEY or not items:
        return {}
    lines = "\n".join(f'{it["id"]}: {str(it.get("content", ""))[:300]}' for it in items)
    prompt = EMOTION_BACKFILL_PROMPT.format(items=lines)
    try:
        async with httpx.AsyncClient(timeout=90) as client:
            response = await client.post(
                API_BASE_URL,
                headers={
                    "Authorization": f"Bearer {get_memory_api_key()}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://midsummer-gateway.local",
                    "X-Title": "Midsummer Emotion Backfill",
                },
                json={
                    "model": MEMORY_MODEL,
                    "max_tokens": 3000,
                    "temperature": 0,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            if response.status_code != 200:
                print(f"⚠️  情绪回填请求失败: {response.status_code}")
                return {}
            text = response.json().get("choices", [{}])[0].get("message", {}).get("content", "").strip()
            if text.startswith("```json"):
                text = text[7:]
            if text.startswith("```"):
                text = text[3:]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()
            try:
                arr = json.loads(text)
            except json.JSONDecodeError:
                import re
                m = re.search(r'\[.*\]', text, re.DOTALL)
                if not m:
                    print("⚠️  情绪回填结果未找到 JSON 数组")
                    return {}
                arr = json.loads(m.group())
            out = {}
            if isinstance(arr, list):
                for o in arr:
                    if isinstance(o, dict) and "id" in o:
                        try:
                            out[int(o["id"])] = {
                                "valence": max(-1.0, min(1.0, float(o.get("valence", 0.0)))),
                                "arousal": max(0.0, min(1.0, float(o.get("arousal", 0.2)))),
                            }
                        except Exception:
                            pass
            print(f"📝 情绪回填打标 {len(out)}/{len(items)} 条")
            return out
    except Exception as e:
        print(f"⚠️  情绪回填出错: {e}")
        return {}


EXPLICIT_BACKFILL_PROMPT = """你在给一批记忆打「是否露骨/私密」标记（is_explicit）。

# 判定 is_explicit = true（命中后注入时不会复述原文，只收敛成一句提示）
- 具体性场景/性行为细节（做了什么、说了什么露骨的话、身体细节、高潮等）
- 私密的身体/性相关「事实·参数·数据」：玩具尺寸规格、身体反应数据、潮吹/G点/高潮/穴等生理记录、私密身体部位状态
- 私密的性相关原话引用

# 判定 is_explicit = false
- 暗号释义（"某词=亲密暗号"）、"他们有亲密/性关系"这类纯关系陈述（不含细节）——要 false，好让对方仍能"懂暗号"
- 与性无关的日常、情感、技术、玩笑、生活细节、健康（如例假日期）

# 输入（每行：id: 内容）
{items}

# 输出（只返回 JSON 数组，不要其他文字）
[{{"id": 数字, "is_explicit": true/false}}]
"""


async def tag_explicit_batch(items: list) -> dict:
    """给一批已有记忆判 is_explicit（露骨回填用）。
    items=[{'id':N,'content':...}]；返回 {id(int): bool}；失败返回 {}。"""
    if not API_KEY or not items:
        return {}
    lines = "\n".join(f'{it["id"]}: {str(it.get("content", ""))[:300]}' for it in items)
    prompt = EXPLICIT_BACKFILL_PROMPT.format(items=lines)
    try:
        async with httpx.AsyncClient(timeout=90) as client:
            response = await client.post(
                API_BASE_URL,
                headers={
                    "Authorization": f"Bearer {get_memory_api_key()}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://midsummer-gateway.local",
                    "X-Title": "Midsummer Explicit Backfill",
                },
                json={
                    "model": MEMORY_MODEL,
                    "max_tokens": 3000,
                    "temperature": 0,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            if response.status_code != 200:
                print(f"⚠️  露骨回填请求失败: {response.status_code}")
                return {}
            text = response.json().get("choices", [{}])[0].get("message", {}).get("content", "").strip()
            if text.startswith("```json"):
                text = text[7:]
            if text.startswith("```"):
                text = text[3:]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()
            try:
                arr = json.loads(text)
            except json.JSONDecodeError:
                import re
                m = re.search(r'\[.*\]', text, re.DOTALL)
                if not m:
                    print("⚠️  露骨回填结果未找到 JSON 数组")
                    return {}
                arr = json.loads(m.group())
            out = {}
            if isinstance(arr, list):
                for o in arr:
                    if isinstance(o, dict) and "id" in o:
                        try:
                            out[int(o["id"])] = bool(o.get("is_explicit", False))
                        except Exception:
                            pass
            print(f"📝 露骨回填判定 {len(out)}/{len(items)} 条")
            return out
    except Exception as e:
        print(f"⚠️  露骨回填出错: {e}")
        return {}
