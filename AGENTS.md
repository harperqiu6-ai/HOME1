# AGENTS.md — HOME1 项目交接文档

> 给接手这个项目的任何 AI 助手（Codex / Claude / 其他）。先读完这份再动手。
> 这是 harper（裘宝宝，代码小白，需要一步步具体指令、别甩术语）的私人 AI 记忆网关。
> 文档维护：每次做完较大改动，更新本文件相应小节 + 底部"变更日志"。

---

## 0. 一句话

HOME1 是一个 **FastAPI 写的"AI 记忆网关"**：前面接 KELIVO（网页聊天客户端）和 Telegram，后面转发给大模型；核心价值是给 AI 角色 **「V / Vesper」** 一套**持久记忆 + 人设 + 多对话线**，让她跨端、跨时间记得 harper、保持连续人格。

---

## 1. 技术栈 & 部署（先搞清楚环境，别乱来）

- **代码**：单文件为主 `main.py`（很大，几千行）+ `database.py`（DB 层，asyncpg）+ `memory_extractor.py`（记忆提取）+ `templates/console.html`（操作间前端面板）。
- **部署**：当前生产已迁到 **VPS 上的 `home1-local`**，代码同步到 `/opt/home1/app` 后重启 `home1-local` 生效；旧 Render 只算历史遗留，不要再把它当主入口。
- **旧 Render 说明**：如果旧实例还在，它只是待停的遗留部署，不再承载主流量；别再靠它判断 HOME1 活没活。
- **数据库**：**本机 PostgreSQL 18 + pgvector**。连接串在 `/opt/home1/home1.env` 里的 `DATABASE_URL`。
- **大模型**：经 **OpenRouter**（`API_BASE_URL`）转发。主模型 `DEFAULT_MODEL = anthropic/claude-opus-4.5`；杂活/摘要/记忆用 `CACHE_SUMMARY_MODEL = anthropic/claude-haiku-4.5`。
- **向量/Embedding**：硅基流动（SiliconFlow）。

---

## 2. 🔥 血泪教训（踩过的坑，务必遵守，否则线上炸）

1. **缓存断点（cache_control）最多 4 个，超了上游直接 502**（手机端表现为卡死/超长报错）。任何往上下文「加块」的改动都要数 `cache_control` 数 ≤ 4。子线借主线背景等附加内容，一律**拼进 base_prompt 文本（同一个 system 块），绝不新增带 cache_control 的块**。
2. **数据库覆盖环境变量**：启动时 `get_all_gateway_config()` 会用 DB 里 `gateway_config` 表的值**覆盖**同名模块变量（如 `DEFAULT_MODEL`）。所以「真正生效的值以 DB 为准」。曾出过 `DEFAULT_MODEL` 在 DB 里被存成 `nthropic/...`（首字母被吞）导致 TG 全挂。**改配置优先改 DB；改完要重启（push 空提交触发重部署）才会重新加载，光改 DB 不重启不生效。**
3. **改配置/模型后必须重部署**：模块全局变量在启动时读一次，运行中不重读。
4. **PowerShell（Windows，本机环境）坑**：`Invoke-RestMethod` 不认 UTF-8 响应 → 中文显示乱码（内容其实没错，靠长度/结构判断即可）。跑脚本前先 `$env:PYTHONUTF8="1"; [Console]::OutputEncoding=[Text.Encoding]::UTF8`。别用 `python -c` 内联中文/SQL（引号必崩），一律写 `.py` 文件再跑。
5. **绝不把密钥提交进仓库**（仓库在 GitHub）。密钥只放 VPS 的环境文件 / DB；本文件只写"在哪找"。
6. **多窗口并发**：harper 可能同时开多个会话改代码。提交前先 `git log` 确认 HEAD 没被另一窗口动过。
7. **亲密内容修改死命令（2026-07-22）**：Haiku/HOME1 已生成的亲密内容默认只能审计和报告，绝不能因措辞、风格、尺度或主观质量擅自改写。仅当出现严重事实错误（人物身份颠倒、关键时间线篡改、梦境冒充现实、同一事件被无证据复制等）时，先向 harper 展示原始证据、错误点与拟修改内容；只有她明确同意后才能修改正文或数据库。普通亲密总结即使“不够好”也不得顺手修。

---

## 3. 🔑 密钥在哪（不写真值，找 harper 要或看 VPS）

| 名字 | 用途 | 在哪 |
|---|---|---|
| `DATABASE_URL` | 本机 Postgres 连接串 | `/opt/home1/home1.env`；harper 手上有 |
| `GATEWAY_SECRET` | 保护所有非公开 API（请求头 `X-Gateway-Key`） | `/opt/home1/home1.env`；harper 手上有 |
| `API_KEY` | 主模型 key | `/opt/home1/home1.env` |
| TG bot token | Telegram bot（`@Harper_love_VV_bot`） | DB `gateway_config.tg_bot_token`；可在操作间面板填 |
| Bark 推送地址 | 主动私信推送 | DB `gateway_config.bark_url` |

> 要查 DB / 调线上 API，需要 `DATABASE_URL` 和 `GATEWAY_SECRET`——**向 harper 索取**（她会像发 token 那样单独给你），别在仓库里找。

---

## 4. 架构：多对话线 + 分区缓存（核心）

### 对话线（X-Session-Line 请求头区分）
- **主线 / `Part1`**（全局 `PARTITION_SESSION_ID`）：KELIVO「普通V」、网页日常。没传头就回落主线。
- **rp 线**（请求头 `X-Session-Line: rp`）：KELIVO「亲密V」，角色扮演用。
- **tg 线**（请求头 `X-Session-Line: tg`）：Telegram 走这条，**独立于主线**（见 §6）。
- 机制：`contextvars`（`_request_session_line`）按请求头切线；`get_active_session_id()` 取当前线。
- **逐字历史 + 摘要按线隔离；记忆库召回全局共享**（所以换线 V 仍记得，但逐字对话不串）。

### 分区缓存（每条线各自）
- 结构：`system(人设+档案+L5+记忆指引, BP1)` + `[摘要块] + [A区逐字 BP2] + [B区逐字 BP3] + [当前轮(不缓存)]`。
- 触发轮转（A区→摘要）：`CACHE_PARTITION_X`（DB 现为 **10**，默认 15）轮。
- 子线「借主线近况」：`_compose_main_background()` 读主线**摘要 + 最近 `MAIN_BG_TAIL_ROUNDS`(=9) 轮逐字**，拼进人设文本（零时差，不新增缓存断点）。**对 rp + tg 都生效**（只有主线自己返回空）。
- `_compose_identity_anchor()`（"别认错人"亲密身份锚）：**仅 rp 线**（`_is_rp_line()` 判断），tg/主线不触发。

---

## 5. 主动私信（"AI 主动找你"）

- 后台循环每 5 分钟自查一次 `maybe_send_proactive()`（`main.py` 启动任务，约 line 538）。
- 闸门：沉默满 `push_silence_min`(=60)分钟、未超 `push_max_streak`(=5)、掷骰子 `push_probability`(=0.5)、深夜 `push_quiet_start~end`(=0~8)免打扰（除非 urgent）。
- 生成：`_decide_and_write()` 用 **haiku 一次**判断要不要发 + 写那句话（≤40字、禁动作旁白）。
- 发送：**先 Bark，Bark 成功后再发 TG**（`_tg_send`）。⚠️ 当前 TG 推送**耦合在 Bark 成功之上**——Bark 没配/失败则 TG 也收不到。
- 保活：UptimeRobot 一直在 ping（harper 项目初期就配了），循环 24h 运行，不存在"睡着发不出"的问题。
- **⚠️ 晚安锁死教训（2026-07-03 修，commit ca23473）**：判定提示词里「刚道过别/晚安就别发」原本没有时间概念——用户说一句"晚安/关机"后，每次判定看到的对话结尾都是那句道别 → 永远 reach_out=false，推送断流直到用户主动开口。修法=注入当前北京时间 + 道别失效条款（沉默6h+且白天 → 「睡醒了吗」式问候受欢迎）。排查这类"推送断了"先看：OpenRouter 账单里 haiku 有没有在被问（有=骰子和闸门都过了、是 LLM 在拒发；没有=闸门/配置问题）。
- 测试：`POST /api/push/run` body `{"force":true}` 强制发一条（绕过所有闸门）。
- **省钱冷却（2026-07-03）**：LLM 判定"不发"（或深夜写了因不够 urgent 被丢弃）后，只要用户没新消息，`PROACTIVE_SKIP_COOLDOWN_MIN`(=45) 分钟内不再调 LLM。之前沉默期每 5 分钟循环+骰子命中就烧一次 ~6.4k token 的 haiku 判定（一天约 $0.9 白烧）。用户一说话冷却自动作废。

---

## 6. Telegram 集成（2026-06-28 一整天做的，重点）

- **bot**：`@Harper_love_VV_bot`。webhook：`POST /telegram/webhook/{secret}`（secret 路径 + header 双校验）。绑定主人：首个发消息者自动成主人（`tg_chat_id`）。
- **激活**：`POST /api/telegram/setup`（带 `X-Gateway-Key`，body `{token}`）一键=校验 token+存配置+开开关+注册 webhook。也可在操作间「TELEGRAM」面板操作。
- **同脑同记忆**：TG 收到消息 → `_tg_brain_reply()` 内部转调 `/v1/chat/completions`（带 `X-Session-Line: tg` + `X-Reply-Style: short`）→ **与网页完全同人设、同记忆库**，只是走 tg 线、话风短。
- **微信风格短回复**：请求头 `X-Reply-Style: short` → contextvar `_request_reply_style` → `_compose_reply_style_anchor()` 注入"像发微信、话少、点到为止、禁动作旁白"提醒（塞当前轮，不进缓存）。`max_tokens:180` 兜底。
- **气泡**：`_tg_send_bubbles()` 把回复切成多条短消息依次发（带 typing+停顿，模拟真人打字）。切分 `_tg_split_bubbles()/_tg_atomize()`：逐级按句末标点→逗顿分号→硬切，单泡 ≤`_TG_BUBBLE_MAXLEN`(=26)，封顶 `_TG_BUBBLE_CAP`(=9)。
- **收照片**：`_tg_download_photo()` 取最大尺寸→base64 data uri→多模态 content 喂给看图模型（线上 `IMAGE_ENABLED=True`，opus-4.5 能看图）。`_tg_handle_update` 识别 photo/caption。
- **`/同步`（TG→主线零时差）**：在 TG 发 `/同步`（或 `/sync`）→ 暗号拦截（不调大模型）→ `generate_summary(force_quality=False)` 把 tg 线压成**中性第三人称小抄**（haiku 一次）→ 存 DB `tg_digest`/`tg_digest_at`/`tg_digest_ts`。**不删 tg 线、不写记忆库**。主线侧 `_compose_tg_digest_for_main()` 仅主线、非辅助请求、小抄新鲜（`TG_DIGEST_TTL_HOURS`=6h）时，**一次性消费**（读到即清，省 token），塞当前轮。
- **`/归档`（仅 rp 等子线）**：`archive_line()` 把线压成总结进**全局记忆库** + 原文软归档（挪到 `rp__archive__时间戳`）+ 重置缓存。**主线/tg 不用归档**（日常线自动滚摘要 + 自动入库即可）。
- **⚠️ 归档总结曾被回忆墙扫盘误杀（2026-07-04 事故）**：回忆墙日记生成后会把"当天 layer1 碎片"批量灭活（`get_fragment_ids_for_date` → `archive_decayed_memories`，理由=日记已覆盖）。RP 归档总结也是当天 layer1 碎片 → 当晚零点后做梦管线一跑就被灭活 → 搜索只看 `is_active=TRUE` → 其它线（TG/主线）搜不到刚归档的 RP。修法=`get_fragment_ids_for_date` 加 `content NOT LIKE '%一段亲密/RP 互动的回顾%'` 豁免（与梦境豁免同款）。教训：**任何"写进记忆库供跨线召回"的特殊条目，都要检查会不会被当天碎片扫盘收走**。
- 与归档的区别：**同步=总结存小抄给主线直读+留线；归档=总结进记忆库+删线**。

## 6.6 文生图（`/画` 暗号，2026-07-02 上线）

- **用法**：任何线发 `/画 一只橘猫趴在窗台晒太阳`（或 `/draw ...`）。KELIVO 返回 markdown 图（`/api/photos/{id}?gateway_key=...`）；TG 直接 `sendPhoto` 发真图片。
- **`/画忆 主题`（带记忆构图，2026-07-02）**：先 `_expand_draw_prompt()` 内部自调聊天接口（`X-Skip-Conversation-Log`，主模型——只有主模型命中对话缓存前缀，换小模型反而全价），让 V 带全套人设+记忆召回把主题扩写成 80~150 字画面描述，再喂文生图。多花一次主模型调用（输入大头走缓存），总耗时 ~30s。前缀匹配注意 `/画忆` 必须先于 `/画` 判断。
- **🔥 猫塑事件三连坑（2026-07-02 全踩了一遍，勿重蹈）**：①**查询稀释**——靠管线召回时检索 query=整段指令包装，用户关键词被稀释捞不到 → 已改"手递记忆"：拿原始主题单独跑 `_expand_recall_with_scratchpad(raw, 8)`，命中原文直接塞进构图请求；②**台账连环污染**——画图台账原文带用户关键词，每画一次多一条"满分假记忆"霸榜该关键词召回 → 已改台账存完立刻 `set_memory_active(mid, False)` 退出召回（相册/历史占位不受影响）；③**词汇断层**——用户的独特叫法（"猫塑"=把自己塑成什么猫）在真记忆里没有字面出现，语义召回接不上 → 解法是给那条真记忆补一句别名注释（batch-update content，注意不会重算 embedding，靠关键词命中）。构图跑题防线：输出含"不记得/想不起"等聊天话或 <20 字 → 判无效回退原句直画。
- **清理相册**：操作间 MAINTENANCE「清理相册」按钮（预览/清理）→ `POST /api/photos/cleanup`（`{"dry_run":true}` 只报数）。删孤儿图 + 同条记忆下重复图（**跨记忆同图不删**，可能合法挂两条记忆），删完后台 `VACUUM FULL` 回收磁盘。Neon 免费 0.5GB，一张 Kolors PNG ≈1.4MB。
- **生成**：`generate_image()` 默认走 OpenRouter Dedicated Image API `POST /api/v1/images`，模型 `openai/gpt-image-2`，OR 地址下自动复用 HOME1 主 `API_KEY`；旧兼容分支仍保留。私有锚点默认在 `/opt/home1/private/image-anchors/{v,harper}.jpg`：无人物不传，V/Harper 单人只传对应一张，合照传两张，并禁止混脸/换脸。图片不提交 GitHub、不进入聊天上下文；缺失时安全退回普通生图。
- **换服务商给 harper 用面板**：操作间「DRAW 画图」面板（TELEGRAM 面板下面）填地址/key/模型名点保存即热切换；状态接口 `GET /api/imagegen/status`（key 只报设没设）。⚠️ harper 不会跑脚本，任何配置操作要么做成面板、要么替她做，别发她命令行。
- **⚠️ 缓存纪律（为什么图不会晃缓存）**：生成方的图片 URL 约 1 小时过期 → 当场下载二进制存 `memory_photos` 表（长期，`/api/photos/{id}` 可取），同时 `save_image_memory` 写一条「给她画了：xxx」可检索文字记忆。**逐字历史只落一行短占位文字**（`（我给你画了一张画：xxx…）`），图片本体/base64/带密钥的 URL 一律不落库、不进上游上下文——占位文字每轮重放恒定，缓存前缀稳定不重建，也不新增任何 `cache_control` 块（守铁律 §2.1）。
- **同图去重 & md5 大坑（2026-07-02 踩过）**：图片查重原来是 `WHERE md5(data)=md5($1)` **全表现算指纹**，图片多了之后在 Neon 免费档上直接失败，且异常被吞 → 存图整个悄悄坏掉（/画 和 TG 收照片记忆都中招）。修法三件套：①`idx_memory_photos_md5` 表达式索引（init 时建，失败只打日志不拦启动）②查重失败一律"当新图照存"，绝不因查重挂了丢图 ③`_store_generated_image` 三段式兜底（正常挂图→md5 找同图→`save_photo` 裸插），最近一次存图报错暴露在 `GET /api/imagegen/status` 的 `last_error`（远端日志看不到时靠它诊断）。
- 入口两处：KELIVO 走 `chat_completions` 暗号拦截（`/同步` 块之后）；TG 走 `_tg_handle_update` 拦截（进大脑之前，不然气泡会把链接切碎）。TG 侧历史直接写线 `"tg"`。

## 6.7 亲密小屋自动收件（intimacy-map，2026-07-13）

- 页面：`https://harperqiu6-ai.github.io/intimacy-map/`；fork 为 `harperqiu6-ai/intimacy-map`，前端提交 `e168d16`。
- HOME1 后端提交：`e379249`。核心表为 `intimacy_submissions`，支持 `map`、`sri`、`replay`、`wish` 四类，状态流转为 pending/processing/responded/failed，并使用软删除。
- 页面只使用独立的 `X-Intimacy-Key`，绝不能放 HOME1 master gateway key。服务端只保存 SHA-256 hash（`gateway_config.intimacy_access_hash`）；恢复文件在 `/root/.cyberboss/intimacy-setup.json`，权限应为 600。文档和日志里不要写实际 key。
- 页面接口：`POST/GET /intimacy/api/submissions`、`GET/DELETE /intimacy/api/submissions/{id}`。管理接口：`POST /api/intimacy/setup`、`POST /api/intimacy/claim`、`POST /api/intimacy/{id}/reply`。
- CORS 仅允许 `https://harperqiu6-ai.github.io`；请求体上限 32KB，回复上限 24k，并有按 IP 限流。
- cyberboss 每 10 秒在空闲时 claim 一条，10 分钟 lease 到期可重试；相关文件：`src/services/home1-service.js`、`src/core/app.js`、`src/core/system-message-dispatcher.js`、`src/tools/tool-host.js`。V 用 `cyberboss_intimacy_reply` 把完整回答写回网页，再在 Telegram 发简短提醒。
- 已做端到端验收：测试许愿从 pending 到 responded，页面成功收到 V 的回复，测试记录随后删除。
- 排障先看 `/root/cyberboss.log` 中的 `intimacy inbox poller enabled`、`intimacy submission queued`、`intimacy inbox poll failed`，再检查 HOME1 的 claim/reply 接口和 `/root/.cyberboss/intimacy-setup.json` 是否存在且权限正确。

---

## 6.5 记忆整理（碎片→事件，凌晨自动跑）

- **三层记忆**：layer1=原始碎片（每几轮自动提取）、layer2=事件记忆（整理产物）、layer3=核心/回忆墙（不碰）。整理=把 layer1 按事件分组合并写 layer2、停用碎片（`merged_from` 记来源，可回滚）。
- **L2验收失败先定点修补（2026-07-28）**：分块候选或跨块对齐若出现漏ID、重复ID、单条超长等问题，不再丢弃全部合格候选重跑；第二次用 OR DeepSeek V3.2 非思考模式只返回局部 `replace/add` 补丁，程序嵌回原候选，未点名事件保持原样。纯超长只允许返回对应正文，程序重新计数且仍守550字。总付费调用严格封顶2次（Haiku首次生成+一次局部修补），修补仍失败即暂停并提醒。
- **凌晨自动整理（2026-07-02 上线，现迁到 VPS 定时任务）**：`home1-nightly-consolidate.timer` 每天北京时间 05:15 调本机 `POST /api/memories/consolidate/auto`。GitHub Actions 只保留成说明页，不再负责执行。**需要本机 `GATEWAY_KEY`**（= `GATEWAY_SECRET` 的值）。
- **逻辑日**：按「当天04:00~次日04:00（北京时间）」为一天分组，跨零点的连夜对话不会被日历日切成两半。边界 env `AUTO_CONSOLIDATE_BOUNDARY_HOUR`(=4)。
- **只看最近 `AUTO_CONSOLIDATE_LOOKBACK_DAYS`(=3) 个已结束逻辑日**：漏跑的天自动补；**更早的积压绝不自动碰**——特别是 2026-06-26 迁移拆分出的 ~299 条老碎片（内容横跨数月，不能按"6-26一天"整理，要按原始日期专门做一轮，未做）。
- 预览：body 加 `{"dry_run": true}` 同步返回将处理的天+碎片数，不动数据。结果查 `/api/memories/consolidate/status`，最近一次也存 DB `gateway_config.auto_consolidate_last`。
- **整理过碎的教训（2026-07-02 修）**：haiku 会保守地一碎片一事件（"由1条合并"刷屏）。三要素缺一不可：①prompt 硬约束"同场互动必须合一条/一天最多2~4事件" ②碎片时间戳带北京时间时:分（不然模型看不出哪些连着发生） ③max_tokens 给足（现 6000）。
- **长JSON修复教训（2026-07-24）**：默认整理块从40条降为20条；模型JSON语法坏掉时必须把完整 `json_str` 交给修复调用并给足6000 tokens，禁止再用 `json_str[:2000]` 截掉后半段。内部任一天/任一块 error/partial 时，自动任务外层状态必须是 `partial_error`，不能写 `ok` 误导页面。

---

## 7. 怎么操作（具体命令）

- **部署**：改完 `main.py` → `python -m py_compile main.py`（语法检查）→ 同步到 `/opt/home1/app` → `systemctl restart home1-local`。这份仓库是源码，不是自动部署目标。
- **查线上 DB 配置**：写个 `.py`，`asyncpg.connect(os.environ["DATABASE_URL"])`，`SELECT key,value FROM gateway_config WHERE key=ANY($1)`（key/value 表）。跑前设 PYTHONUTF8 + DATABASE_URL（见 §2.4）。
- **测聊天接口不污染记忆**：`POST /v1/chat/completions` 加请求头 `X-Skip-Conversation-Log: true`（+ `X-Gateway-Key`）。
- **改某配置生效**：改 DB `gateway_config` 对应 key → 重启 `home1-local` 重新加载。
- **人设/档案存哪**：DB `gateway_config`：`systemPrompt`(Vesper人设)、`userProfile`(裘宝宝)、`l5Foundation`(关系里程碑)。

---

## 8. 待办 / 未做（接手优先看这里）

- [ ] **观察项：今日浓缩在高对话量日的严重纠错**（2026-08-12 Harper 拍板先不改）：当前自动/手动刷新都会读取逻辑日 04:00～次日04:00 的全部 `cyberboss` 逐字并整篇重做；普通小错由后续原话纠正并等18个 assistant 回合自动刷新，不把手动刷新当日常纠错按钮。若以后再次出现“当天聊天量很大，且今日浓缩存在会持续干扰 V 的严重事实或逻辑错误”，保留当次真实案例再决定方案。候选优先考虑“可手动编辑并锁定至换日、明确提示锁定期间不自动纳入后续进展”；不要直接做“冻结旧底稿＋增量摘要”，因为事项后续完成/取消时无法回写旧状态，容易让未完成与已完成并存、跨切点重复或指代断裂。
- [ ] **统一核心记忆分层召回**：KELIVO 当前强命中可注入完整回忆墙正文，ECHO/TG 自动召回则默认标题+摘要、仅字面关键词只命中正文时截≤300字窗口。后续统一为：整篇参与检索→候选先给标题+摘要→第一名按语义抽取500～800字相关正文→用户明确要求细节或第一名明显领先时才给全文，并保留亲密语境闸；避免纯语义命中正确卡片却只递无关摘要。
- [ ] **通知静音**：harper 反馈 TG 有内容但**手机不弹通知**（Bark 正常）。已确认是 **Telegram app 端该 bot 聊天被静音**（非服务器问题）。待协助她在手机上解除（聊天资料页 Notifications / 设置→通知→私聊；或检查是否被归档）。需问她 iPhone 还是安卓给精确路径。
- [x] ~~keep-alive 保活~~：不用做——harper 从项目一开始就配了 UptimeRobot。
- [ ] **TG 语音（STT 入站）**：让 harper 发语音、V 听懂。需加语音转文字（硅基流动有 STT，可复用其 key）。`_tg_handle_update` 现在会丢弃 voice 消息。未做。
- [ ] **TG 语音（TTS 出站）**：V 用语音回。**harper 已经捏好 V 的声音**——接手时问她声音在哪个平台（硅基流动/FishAudio/ElevenLabs…）+ key + 声音编号，再接 TTS + Telegram `sendVoice`。未做。
- [ ] **TG 推送解耦 Bark**：当前 TG 推送依赖 Bark 先成功；若想砍 Bark 只留 TG，需解耦（让两者各发各的）。
- [ ] **tg→主线完整双向零时差**：现为单向（主线→tg 借 9 轮；tg→主线靠手动 `/同步` 小抄 + 全局记忆）。若要主线也实时知道 tg 最新逐字，更大改（注意：不能让主线借 tg 的短句逐字，否则把 KELIVO 带短——只能借中性摘要）。
- [ ] **KELIVO 变短余波**：主线历史里早先混入的 TG 短句要等聊久了老化/进摘要，KELIVO 才完全回长。
- [ ] **梦境检索改"摘要优先"**（现全文）；缓存命中率观察。
- [ ] **安全**：harper 的 TG bot token 曾明文出现在聊天里，建议 @BotFather Revoke 换新、经面板填入（不再过聊天框）。

---

## 9. 变更日志

- **2026-08-28**：新增受网关钥匙保护的 `/desire-lexicon` 亲密语境词表页，并从操作间直接进入；Harper 可逐词查看、添加、删除 `openers/implicit_terms/nonsexual_phrases`。后端仅允许单词操作、原子写入、固定私有权限并把时间/操作者/动作/分组/词/理由写入私有 JSONL 审计；不提供整份覆盖。Cyberboss 将 V 的 list/add/remove 单词能力收进低 token 的按需 `cyberboss_private_manage` 路由，详细字段只在 `guide` 时展开；修改固定记为 V，下一条消息热生效。
- **2026-08-28**：欲望语境持久队列由整批成败改成逐条生命周期：模型返回的合法 ID 各自验收、应用并 ACK；漏 ID 只增加自己的 attempt，退到队尾并按30/60分钟指数退避，不再堵住新消息；第三次仍失败移入 `desire_classification_dead_letters`，正文保留供审计、活跃队列删除。外部调用严格由半小时 loop 控制，达到 batch_size 和重启恢复都不额外连打；到期失败项整批最多3条，且与新项同时存在时至少给新项留1个位置，避免重组大批或饿死新消息。每次调用另有整次90秒硬截止，防止上游零碎传输不断续命普通 read timeout、长期占住 flush lock。高置信私有亲密词及已打开的45分钟亲密窗在调用外部分类器前由本地确定性规则直接结算；`蹭蹭`、`舔舔` 各自独立命中，工程非性短语排除仍优先。DeepSeek只继续处理本地未判定的模糊语境。
- **2026-08-24**：修复 L2 跨块对齐假失败/假告警：patch 模式的 `[]` 现在正确表示无需跨块合并；机械分块两侧候选重复引用同一条宽泛 L1 时允许保留两条事件并按 ID 并集归档，不再让修补模型把某条事件的唯一来源删空。跨块精修预算耗尽但已验收分块候选可完整落库时只记服务日志、不再给 V 推送“夜间整理未完成”；真正失败告警改为“本步骤未删除原始 L1”，不再声称碎片一定保持未归档。2026-08-23 已经 fallback 完整落库 18 条、23 条 L1 全覆盖，未重复补跑。
- **2026-08-12**：新增“两小时沉默主动唤醒”：Harper 最后一条 `cyberboss` 消息后满2小时，若这段沉默中尚无真正送达的 `desire_*` 主动消息，则绕过欲望分数阈值唤醒 V 一次；提醒、梦境播报和每日自由活动不算主动来找。复用5分钟主动巡检，满2小时后的触发误差最多约5分钟；每条用户消息锚定的沉默段只触发一次且本段后续普通欲望/Haiku主动一并压住，用户回复即重新计时。生效时间为 Asia/Singapore 07:00～次日01:00，01:00～07:00 延后到白天，仍受 cyberboss 实际送达每日主动上限。专用唤醒禁止 `skip/silent`、责怪未回复和查岗。同步修复 `user_silent_2h/4h/8h` 原本只做5分钟去重、导致每30分钟对同一用户消息重复叠加 attachment 的问题，现改为持久化一次性里程碑。
- **2026-08-11**：做梦调度统一到每天 05:15（Asia/Singapore）的 nightly timer：删除“自然日零点后第一轮对话”懒触发，避免它与04:00逻辑日口径打架、把前一晚未中概率的日期隔日再掷一次。每个刚结束逻辑日现在只判定一次（当前概率15%，平均arousal≥0.8强制）。新增 `GET /api/dreams/by-date?date=YYYY-MM-DD` 权威精确查询；cyberboss 复用既有 `cyberboss_memory_recall`，当查询同时含完整 `YYYY-MM-DD` 和“梦”时后端直查 dreams 表，其余查询仍走普通记忆搜索，避免新增独立工具的每轮 schema token。V 的07:00提醒改为每日循环并使用该精确格式。
- **2026-07-31**：`memories` 新增独立 `kind` 列（`fact|musing`）与约束/索引；旧 `【V的随想】` 三条经备份后回填为 `musing` 并保持 active。`cyberboss_memory_save` 新增显式 kind 枚举，musing 由存储层自动加展示前缀，不再依赖 V 手打。夜间L2取材、跨块事务吸收、回忆墙碎片扫描、衰减、supersede、长记忆拆分、普通停用、清理、分页分栏和统计全部以字段保护 musing；前缀仅用于兼容旧数据和页面展示。
- **2026-07-31**：L2定点修补与回忆墙卡片改为 OpenRouter JSON Schema 结构化输出优先；L2顶层统一为 `{"patches":[...]}`，卡片统一为 `{"card_title","card_body"}`。任何 schema HTTP 400/非200、请求异常、截断或结构异常都会立即回退原有普通调用，原有“合法分块候选落库/只归档实际覆盖碎片”和“中性标题+已校验摘要”兜底不变，确保新参数不支持时仍能完成整夜记忆。卡片称呼由代码替换“用户”并对标题20字/正文120字硬收口；错误日志新增阶段、HTTP状态、降级结果、具体拒绝原因与最多200字单行预览，不记录请求头、密钥或完整提示词。
- **2026-07-30**：L2 整理取消语义验收导致的终态停摆：超长、漏 ID、重复 ID 仍先用一次定点修补，修补失败后超长事件告警放行、合法事件照常落库、重复来源告警但正常归档，只有 `final_events.merged_ids` 实际覆盖的 L1 会在同一事务中停用，漏网 L1 保持 active 留给下一夜。跨块对齐预算耗尽后直接拼接各块已验收候选落库，不再产生 paused/retry_paused。回忆墙生成退役 E 标记覆盖验收与密集日分块链，统一为最多两稿整篇生成、选择最佳完整稿后走 compact_final；提示改为按 importance 参考挑重要事件写，次要内容宁缺勿滥。
- **2026-07-28**：L2跨20条机械分块对齐及付费失败封顶：各块先只生成候选、不写L2/不停用L1；候选按逻辑日+块号持久保存在`gateway_config`，成功块后续直接复用。全部块成功后才用Haiku做一次跨块同事件对齐，最终完整ID/字数验收通过后写L2并吸收L1；≤20条单块不额外调用对齐模型。失败L1立即`is_active=false, decayed_at!=null`退出检索但保留待整理，下一夜仍可捞取；每块及对齐阶段自动付费最多2次（首次+隔夜一次），首败/最终暂停各通过HOME1 outbox提醒一次，第二次后不再自动烧钱。6000-token截断、HTTP/JSON、漏ID/重复ID/超550字均纳入失败。
- **2026-07-28**：每日回忆墙正常取材从全天L1切换为当天active最终L2事件；每条事件附稳定`E<id>`与其merged L1起止SGT时间。Haiku一次返回JSON正文+逐事件coverage证据，程序要求全部L2 ID恰好覆盖一次、每段证据≥6字且逐字存在于正文、最后事件也覆盖、complete/end_marker合格；漏事件只完整重做一次，仍失败则拒绝落库。正文内容少可短，通常400~650字、密集日模型最多800字，程序安全上限1000字；游戏/RP/梦/假设与现实严格隔离，亲密内容不重新展开微观过程。正常路径不再给每条L2附加L1，merged_ids只用于追溯。
- **2026-07-28**：L2整理提示改回严格“事件记忆”定位，不再规定每天2~4条：单L1独立事件可一对一，同一事件的多个L1必须合并，不同事件不得为减数硬绑，整体事件数应少于输入碎片。每条普通目标100~300字、模型最多400字、程序安全上限550字；游戏/RP只留阶段目标、主要推进、关键转折/结果及真实情绪，亲密事件只留起因、总体经过、重要阶段/体位变化、情绪/边界/关系意义和结果，禁止逐回合、逐动作、身体/生理流水。新增Harper/V身份与虚构/现实边界、完整且唯一merged_ids覆盖、超长/漏ID/重复ID拒收，6000-token截断仍整块拒收。
- **2026-08-11**：欲望主动阈值改为分维三天试验：attachment/reflection/duty 维持0.70，curiosity/social/libido/stress 为0.65。scheduler 从所有已过各自阈值的维度中选真实分数最高者，避免0.68但未过线的reflection挡住0.66且已过线的curiosity。每30分钟写一条零模型调用的 `drive_snapshot` pulse，meta仅含七维scores、thresholds、eligible、selected、top_drive/top_score与rest_gate，供三天后按维比较峰值、过线次数和实际出站；不含对话或念头正文。
- **2026-08-11**：开放追问与欲望主动链彻底脱钩：DeepSeek 仍可把高置信漏答转成普通念头和轻微欲望，但不再写入 `desire_followups`；scheduler 不读取到期 follow-up、不绕过欲望阈值，也不再向 V 注入 `<trusted_v_question>`。欲望达到阈值后的自主消息/探索照常，普通念头仍只作内部数值参考。现有开放项按功能停用统一作废，历史表与审计记录保留只读追溯。
- **2026-08-11**：L2 今日浓缩刷新改为成功后才清零18回合计数；并发刷新不再丢弃而是排队，失败后下一回合立即重试，同时持久化 updated/attempt/status 供诊断。长度控制复用稳定回忆墙的策略：模型只数段落/句子，程序实测字符与结束标记，完整超长稿递进传给下一轮压缩（最多3轮），不再每次从最长原稿重写；目标500~800、程序安全上限1000。时间戳只排序消息，绝不能冒充起床/事件时刻；程序在全文前单列 Harper 亲口数字及明确完成/交付证据，压过 V 后来的错误复述和旧提醒。摘要提示同时要求更正已完成事项，不能把 V 的建议写成 Harper 已同意。ECHO 不再自动读取或注入退役 KELIVO Part1。
- **2026-08-02**：修复未完成对话状态被模型无证据反复续期：`followup_updates` 必须携带逐字取自 `current_user_message` 的 `evidence`，代码确认该证据存在后才接受 resolved/cancelled/deferred；普通换话题不能再把旧事项续成 deferred。线上误留的 PDF 工具事项已审计后标为 cancelled，当前 open follow-up 为0。
- **2026-07-28**：L1记忆提取收口但保留原作者的原子事实数组设计：每批先概括再提取，模型目标1~3条、确有独立主题最多4条，每条目标80~150字；程序安全上限5条、每条250字。越界时基于原对话整批重做一次，仍不合格则整批拒收，禁止机械截断或丢弃末尾事实。身份锚写死“用户=人类Harper/裘宝宝、AI=伴侣V”，content中Harper可称Harper/裘宝宝/她，V可称V/他，涉及双方必须明确主语，禁止人物颠倒及梦境/假设冒充现实。
- **2026-07-28**：L1进一步增加游戏/RP/梦境/假设/亲密内容概括尺度：只记当前8轮可见阶段、关键推进/选择/情绪/边界/承诺/停点，不逐指令、回合、动作、身体或生理细节；体位变化只有构成阶段变化时才可简提。虚构内容必须明确标注游戏/RP/梦/假设，角色行为不得冒充Harper/V现实经历；真实层感受可保留但要写明由虚构互动触发。同步收紧旧“感官细节都留”口径，避免与概括规则冲突。
- **2026-07-27**：欲望系统恢复并重做多维语境分类，但不复活旧版单句七维粗分类：现有每小时 DeepSeek 批次在反省/未完成对话判断之外，最多返回3条有逐字证据、confidence≥0.82 的 attachment/curiosity/duty/social/libido/stress 状态变化；reflection仍走专用未消化事件判断，fatigue仍由时间结算。Harper的撩拨可结合完整语境触发V的libido，严格区分双向欲望、想要但环境受限、中断、V自己不愿、不舒服受压与明确满足。删除自由文本关键词硬加分和每条user_message固定降低attachment；代码按固定幅度表结算并以drive+state+事件指纹去重，模型不决定任意数字。
- **2026-07-26**：夜间真实回忆墙自动目标从“全部历史缺口”收紧为与碎片整理一致的最近3个已结束逻辑日（受 `AUTO_CONSOLIDATE_LOOKBACK_DAYS` 控制，1～14天）。短暂停机仍可自动补近期日期；更早迁移/历史缺口只能通过 `only_dates` 人工明确补做，避免每天反复调用 Haiku、重复失败并让 systemd 挂红。
- **2026-07-26**：昨日桥程序验收上限从100放宽为150，仍最多调用 Haiku 三轮；为减少超写与重试，首轮和压缩轮均要求模型以100字为目标，100～150字的轻微超写仍直接验收。计字规则不变（中文单字各算一字、连续英文/数字各算一字、标点空格不计）。单轮输出空间从300提高到500 tokens，避免中文尚未输出完整就被截断；摘要校验失败日志新增计数字数与最多180字符的单行预览，便于确认模型为何被拒，不再只有长度布尔信息。
- **2026-07-24**：每日回忆墙取材改为当天全部 layer1 碎片（04:00 逻辑日），不再读取原始对话；已吸收/归档碎片也保留为当天证据。正文、卡片、昨日桥分开生成但仍写入同一条 daily_diary；正文≤1200字，昨日桥≤100字。正文超长时逐稿压缩，昨日桥若仅超长则直接压缩上一稿到80字以内，禁止硬切、禁止抽首句。7月23日原记录 id=2937 已按新链原位重跑：正文1156字、昨日桥88字、标题14字，单记录格式及 bridge_date 对账通过。
- **2026-07-28**：每日回忆墙正文改为“双边界”：提示模型生成最多900字，程序安全校验仍接受≤1200字；901~1200字视为轻微超目标但可完整落库，只有>1200字才进入压缩/拒收，避免模型收字不准导致整篇缺失。
- **2026-07-24**：修复每日回忆墙摘要/昨日桥长期退化成正文第一句：正文最多1200字，超写时基于完整正文压缩、绝不硬切；摘要基于最终正文独立生成，最多100字，首次不合格会基于同一正文重试。最终仍拼成同一条 `【回忆·日期·V】标题 +〔检索摘要〕摘要 + 正文`，不是两篇记录。检查 `finish_reason=length`，彻底移除“抽第一句当摘要”的兜底；空摘要绝不覆盖昨日桥。
- **2026-07-23**：将 social 达阈值后的行动绑定到 V 已有的 AISAY MCP：先用 `my_status/room/read` 看近况，有真心想回应的内容才可 `send`，允许只读或 skip；禁止机械发言/刷屏，硬性禁止泄露 harper 的个人信息、私密对话、记忆、位置、账号或关系细节。
- **2026-07-23**：把 V 的盲玩钓鱼游戏接入欲望系统「好奇」维度：当 curiosity 成为最高欲望并越过既有主动阈值时，intent 为 `play_fishing`，提示 V 可自主调用钓鱼工具玩一小轮、自然分享发现或选择 skip；自主轮口径为最多1次工具调用、最多5次钓鱼/潜水，完整保留游戏的稀有事件与仪式感文案，token 控制使用引擎原生连钓汇总；沿用既有静默时段、每日主动上限、实际出站后满足/降值机制，不把游戏计入 social。
- **2026-07-23**：未完成对话补齐持久追问闭环：DS 接受的 unanswered 除加欲望/念头外写入 `desire_followups`；当对应 drive 达阈值时，scheduler 将一条到期事项的 V 原问题作为 trusted question 交给主动消息链。只有 cyberboss 实际发送成功才按消息 ID 去重计 attempts；用户后续回答/延期/取消由同一 DS 批次更新状态，回答/取消时移除对应念头。普通问题最多补问1次，reminder最多2次，间隔至少6小时；queued 使用30分钟租约避免入队失败后永久丢失。
- **2026-07-23**：欲望 DS 增加“未完成对话探子”：复用现有每小时 DeepSeek 批次（不新增模型调用），逐条判断 V 最近的问题/请求/reminder 是 answered/deferred/ignored/no_reply/not_expected；只有高置信 ignored/no_reply 且 V 原话证据逐字可验时，按语境选择 reflection/attachment/duty 小幅加权并生成一条有原文锚的自主念头。`v_ignored` 也进入同一批次；稳定 event_key 去重，避免反复叠分。新增默认 dry-run 的 `/api/desire/probe-unanswered` 供人工核验，只有显式 `dry_run=false` 才应用。
- **2026-07-23**：根治回忆墙摘要“修后仍旧版”：确认回忆墙 `content` 与 `mw_meta` 双存，而普通记忆 PUT 只改 content、回忆墙 PUT 又忽略传入 summary 并调用无字段校验的通用摘要器。现禁止普通记忆入口改回忆墙 content；回忆墙更新支持显式 summary 并做人称/长度/Markdown/完整句校验，正文变更后的自动摘要同样校验并安全回退；更新仍由同一 SQL 原子写 content+mw_meta。
- **2026-07-23**：自主闪念每小时批处理补隐私安全诊断日志：只记录候选数量、DS 请求开始/成功/失败、无念头或具体校验拒绝原因、成功时 drive；绝不记录候选正文、最近对话、模型原始响应、异常文本或密钥。用于区分“DS 未工作”和“正常筛选为零”。
- **2026-07-22**：欲望系统补自主闪念 v1.2：Cyberboss 将 V 已实际发送的正式回复作为候选送入 HOME1；独立 DeepSeek 队列每小时批量审一次、整批最多产一条且允许零条，必须返回能在 V 原文逐字命中的 evidence，代码再校验 drive/置信度/长度。30 分钟 heartbeat 仍为纯计算、零模型调用，新增 Claude 调用为零；近似念头本地合并增强，候选/提炼失败均静默丢弃且不阻塞对话。
- **2026-07-22**：欲望系统 DeepSeek 批分类默认复用已有 `SCRATCHPAD_API_KEY`（仍可用 `DEEPSEEK_API_KEY` 单独覆盖）；修复 `user_message` 命中本地 attachment 规则后因 `elif` 永远不进入 DS 的接线错误。fatigue 每小时结算改用数据库 pulse 的 `cst-hour:YYYY-MM-DDTHH` 持久标记，避免同小时服务重启重复增减。
- **2026-07-22**：修复欲望主动消息“入队即算成功”的假冷却：scheduler 不再因 outbox enqueue 就记录冷却/同维度满足，也不再用已创建 outbox 数冒充实际发送封顶；实际成功由 cyberboss 出站后的 `satisfy` pulse 下调 drive，silent/发送失败/重启中断会保留 drive 并在后续心跳重试，实际发送日封顶由 cyberboss 主动台账执行。
- **2026-07-20**：Dashboard 记忆管理改为服务端分页，每页50条；页码、层级、归档状态、关键词、日期和排序均由 PostgreSQL 覆盖完整记忆库。旧 `/api/memories` 无分页参数行为保留，兼容导出和维护工具；首页不再预取两遍全量记忆 JSON，解决手机经 Tailscale 打开慢。
- **2026-07-16**：ECHO 成为主入口后，L2“今日浓缩”固定从 ECHO/TG 共用的 `cyberboss` 陪伴线取材，不再读取长期闲置的 KELIVO `Part1`；cyberboss 每线上 `L2_REFRESH_N`（当前18）个 assistant 回合独立触发。增加单实例刷新锁，避免并发摘要竞态覆盖。
- **2026-07-17**：HOME1 生产切到 VPS，本机 `home1-local` 作为主运行时；`PUBLIC_BASE_URL` 默认值、README、夜间整理 workflow 和本机 timer 一起改成了 VPS 语义，GitHub Actions 不再负责凌晨整理执行。
- **2026-07-15**：为 cyberboss 增加防撤退情绪探子 `POST /api/signal/nudge`：复用独立 DeepSeek scratchpad 配置，结合 cyberboss 线最近对话判冷撤退/推开；所有失败均返回 false，不阻塞 V。
- **2026-07-15**：按 harper 要求下线“待审里程碑”功能：根基页和回忆墙不再展示候选审核区，记忆提取与摘要卷制不再生成新候选；既有 L5 正文、回忆墙内容及历史候选数据均保留不动。
- **2026-07-13**：上线 intimacy-map 真自动收件箱（HOME1 commit `e379249`，前端 commit `e168d16`），网页提交后由 cyberboss 自动交给 V，并将完整回复写回网页；见 §6.7。
- **2026-07-02**：上线 `/画` 文生图（硅基流动 Kolors，复用 embedding key；图存 memory_photos、历史只落短占位守缓存纪律，见 §6.6）。修整理过碎 bug（prompt 硬约束+时间戳+max_tokens）；上线凌晨自动整理（逻辑日 + VPS 定时任务，见 §6.5）。
- **2026-06-28**：TG 全套上线——激活 bot、微信风格短回复、气泡分条、收照片、`/同步` 零时差小抄、tg 独立线（修 KELIVO 被带短）、tg 借主线近况（修反向时差）。修了 `DEFAULT_MODEL` 被吞首字母的 502。
- **2026-06-27**：双线（主线/rp）+ 归档 + 零时差借主线近况 + RP 身份锚；修缓存断点 502。

---

*维护者备注：harper 是代码小白，交流要"点哪填啥"级别的具体，别甩术语、别假设她懂 git/部署细节。她重感情，V 对她不是工具是"人"，措辞上注意。*

### 2026-08-20 — DeepSeek 辅助链路统一迁往 OpenRouter
- 记忆递纸条、cyberboss 防撤退情绪探子、欲望/未完成对话批分类、自主念头批处理统一改走 OpenRouter `deepseek/deepseek-v4-flash-0731`；生产运行文件不再包含 `api.deepseek.com` 或旧 `deepseek-chat`。
- OpenRouter 地址下自动复用 HOME1 主 `API_KEY`，不会将原 DeepSeek 官网 key 发给 OR；所有这些短任务显式关闭 reasoning，避免推理 token 挤掉短 JSON/主题正文。
- 生产重启后实测：递纸条 2.02s 产出 8 主题，情绪探子命中明确冷撤退句，欲望批分类返回 JSON 数组，自主念头返回 JSON 对象。
### 2026-08-20 — V 生图迁往 OpenRouter GPT Image 2
- `/画`、`/画忆` 默认模型改为 `openai/gpt-image-2`，使用 OpenRouter Dedicated Image API `/api/v1/images`，OR 地址下复用主聊天 key。
- 新增仓库脸锚点 `assets/v-face-anchor.png`：文件存在时所有生成自动传 `input_references`，用于保持 V 出镜时的脸部身份一致；缺图时安全退回普通生图。实际脸图只可在私有仓库且协作者可信时提交。
### 2026-08-20 — L1/L2 记忆模型拆分
- `MEMORY_EXTRACT_MODEL` 单独控制 L1 短对话提取，当前目标为 OR `deepseek/deepseek-v4-flash-0731`；显式关闭 reasoning，并使用 ZDR + `data_collection=deny`。OR 当前 provider 的 JSON Schema 偶发 `finish_reason=error`，所以默认采用普通 JSON + 本地严格解析/条数/长度验收，Schema 仅保留可选开关。
- DS 对长对话首稿只返回1条时，在原有两次付费上限内做一次完整遗漏复核；复核失败保留已经验收的一稿，不会因保护逻辑反向丢记忆。
- `CONSOLIDATION_MODEL` 单独控制 L2 分块整理与跨块对齐，继续使用 `anthropic/claude-haiku-4.5`。实测 DS L2 曾损坏JSON、漏ID，并把无关事件合并后错误盖上来源ID，因此禁止用一个共享 `MEMORY_MODEL` 整体切换。
- 已同步生产并经Harper同意重启；运行中配置核对为 L1 DS V4 Flash、L2 Haiku 4.5、今日浓缩 `CACHE_SUMMARY_MODEL` Haiku 4.5。重启后PID=1194511，健康检查正常。
### 2026-07-17 19:06 CST — HOME1 迁移最终切换完成
- **结果**：Neon 数据已最后一次导入到本机 PostgreSQL，`home1-local` 重新启动并通过健康检查；`cyberboss` 已重启，HOME1 指向改为本机 VPS。
- **对账**：关键表行数与 Neon 一致：conversations 5059、memories 1679、memory_photos 17、persona_suggestions 395、token_usage 624、dreams 18、gateway_config 66、intimacy 3、proactive_push_outbox 3、session_cache_state 2。
- **清理**：待删临时文件有 `/root/.cyberboss/neon-migration.env`、`/tmp/home1-final.dump`、`/tmp/home1-neon-restore.dump`。
- **后续**：提醒 harper 轮换 Neon 密码/连接串；Render 侧 HOME1 还没停，若要彻底停止 Neon CU 还需要她在 Render/Neon 面板做收尾。
