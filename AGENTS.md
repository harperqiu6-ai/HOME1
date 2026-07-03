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
- **部署**：**Render**，地址 `https://home1-htca.onrender.com`。**push 到 `main` 分支 → Render 自动重新部署**（约 2~4 分钟，期间服务短暂下线）。没有别的 CI。
- **⚠️ Render 免费层会休眠**：闲置约 15 分钟就睡，睡着时**后台定时任务全停**（见 §5 主动推送）。需要外部定时 ping `/health` 保活。
- **数据库**：**Neon Postgres**（不是 Render 自带的）。连接串在环境变量 `DATABASE_URL`。
- **大模型**：经 **OpenRouter**（`API_BASE_URL`）转发。主模型 `DEFAULT_MODEL = anthropic/claude-opus-4.5`；杂活/摘要/记忆用 `CACHE_SUMMARY_MODEL = anthropic/claude-haiku-4.5`。
- **向量/Embedding**：硅基流动（SiliconFlow）。

---

## 2. 🔥 血泪教训（踩过的坑，务必遵守，否则线上炸）

1. **缓存断点（cache_control）最多 4 个，超了上游直接 502**（手机端表现为卡死/超长报错）。任何往上下文「加块」的改动都要数 `cache_control` 数 ≤ 4。子线借主线背景等附加内容，一律**拼进 base_prompt 文本（同一个 system 块），绝不新增带 cache_control 的块**。
2. **数据库覆盖环境变量**：启动时 `get_all_gateway_config()` 会用 DB 里 `gateway_config` 表的值**覆盖**同名模块变量（如 `DEFAULT_MODEL`）。所以「真正生效的值以 DB 为准」。曾出过 `DEFAULT_MODEL` 在 DB 里被存成 `nthropic/...`（首字母被吞）导致 TG 全挂。**改配置优先改 DB；改完要重启（push 空提交触发重部署）才会重新加载，光改 DB 不重启不生效。**
3. **改配置/模型后必须重部署**：模块全局变量在启动时读一次，运行中不重读。
4. **PowerShell（Windows，本机环境）坑**：`Invoke-RestMethod` 不认 UTF-8 响应 → 中文显示乱码（内容其实没错，靠长度/结构判断即可）。跑脚本前先 `$env:PYTHONUTF8="1"; [Console]::OutputEncoding=[Text.Encoding]::UTF8`。别用 `python -c` 内联中文/SQL（引号必崩），一律写 `.py` 文件再跑。
5. **绝不把密钥提交进仓库**（仓库在 GitHub）。密钥只放 Render 环境变量 / DB；本文件只写"在哪找"。
6. **多窗口并发**：harper 可能同时开多个会话改代码。提交前先 `git log` 确认 HEAD 没被另一窗口动过。

---

## 3. 🔑 密钥在哪（不写真值，找 harper 要或看 Render）

| 名字 | 用途 | 在哪 |
|---|---|---|
| `DATABASE_URL` | Neon Postgres 连接串 | Render 环境变量；harper 手上有 |
| `GATEWAY_SECRET` | 保护所有非公开 API（请求头 `X-Gateway-Key`） | Render 环境变量；harper 手上有 |
| `API_KEY` | OpenRouter key | Render 环境变量 |
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
- **⚠️ 已知问题**：Render 免费层 15 分钟休眠，而推送要 60 分钟沉默才触发 → 服务器在该推送时早睡了，循环冻结 → **主动推送基本发不出**。**解法：外部定时 ping `/health`（每 10 分钟）保活**（cron-job.org / UptimeRobot）。尚未配置。
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
- 与归档的区别：**同步=总结存小抄给主线直读+留线；归档=总结进记忆库+删线**。

## 6.6 文生图（`/画` 暗号，2026-07-02 上线）

- **用法**：任何线发 `/画 一只橘猫趴在窗台晒太阳`（或 `/draw ...`）。KELIVO 返回 markdown 图（`/api/photos/{id}?gateway_key=...`）；TG 直接 `sendPhoto` 发真图片。
- **`/画忆 主题`（带记忆构图，2026-07-02）**：先 `_expand_draw_prompt()` 内部自调聊天接口（`X-Skip-Conversation-Log`，主模型——只有主模型命中对话缓存前缀，换小模型反而全价），让 V 带全套人设+记忆召回把主题扩写成 80~150 字画面描述，再喂文生图。多花一次主模型调用（输入大头走缓存），总耗时 ~30s。前缀匹配注意 `/画忆` 必须先于 `/画` 判断。
- **🔥 猫塑事件三连坑（2026-07-02 全踩了一遍，勿重蹈）**：①**查询稀释**——靠管线召回时检索 query=整段指令包装，用户关键词被稀释捞不到 → 已改"手递记忆"：拿原始主题单独跑 `_expand_recall_with_scratchpad(raw, 8)`，命中原文直接塞进构图请求；②**台账连环污染**——画图台账原文带用户关键词，每画一次多一条"满分假记忆"霸榜该关键词召回 → 已改台账存完立刻 `set_memory_active(mid, False)` 退出召回（相册/历史占位不受影响）；③**词汇断层**——用户的独特叫法（"猫塑"=把自己塑成什么猫）在真记忆里没有字面出现，语义召回接不上 → 解法是给那条真记忆补一句别名注释（batch-update content，注意不会重算 embedding，靠关键词命中）。构图跑题防线：输出含"不记得/想不起"等聊天话或 <20 字 → 判无效回退原句直画。
- **清理相册**：操作间 MAINTENANCE「清理相册」按钮（预览/清理）→ `POST /api/photos/cleanup`（`{"dry_run":true}` 只报数）。删孤儿图 + 同条记忆下重复图（**跨记忆同图不删**，可能合法挂两条记忆），删完后台 `VACUUM FULL` 回收磁盘。Neon 免费 0.5GB，一张 Kolors PNG ≈1.4MB。
- **生成**：`generate_image()` 调 `{base}/images/generations`，**服务商可随意切**：①硅基流动（参数 `image_size/batch_size`，返回 `images[0].url`）②任何 OpenAI 兼容中转站/官方（gpt-image-2、dall-e 等；参数 `size/n`，返回 `data[0].url` 或 `data[0].b64_json`）。请求按 base url 猜风格，被 400/422 打回自动换另一种再试一次（400 不烧钱）。模型 `IMAGE_GEN_MODEL`（默认 `Kwai-Kolors/Kolors`）。key/base **默认复用 EMBEDDING_API_KEY / EMBEDDING_BASE_URL**（同为硅基流动，零新增密钥）；填 `IMAGE_GEN_API_KEY/IMAGE_GEN_BASE_URL` 即切到别家。开关 `IMAGE_GEN_ENABLED`（默认开）。均已注册 DB 覆盖 + `/api/settings` 热更新（**PUT 即生效，不用重启**）。
- **换服务商给 harper 用面板**：操作间「DRAW 画图」面板（TELEGRAM 面板下面）填地址/key/模型名点保存即热切换；状态接口 `GET /api/imagegen/status`（key 只报设没设）。⚠️ harper 不会跑脚本，任何配置操作要么做成面板、要么替她做，别发她命令行。
- **⚠️ 缓存纪律（为什么图不会晃缓存）**：生成方的图片 URL 约 1 小时过期 → 当场下载二进制存 `memory_photos` 表（长期，`/api/photos/{id}` 可取），同时 `save_image_memory` 写一条「给她画了：xxx」可检索文字记忆。**逐字历史只落一行短占位文字**（`（我给你画了一张画：xxx…）`），图片本体/base64/带密钥的 URL 一律不落库、不进上游上下文——占位文字每轮重放恒定，缓存前缀稳定不重建，也不新增任何 `cache_control` 块（守铁律 §2.1）。
- **同图去重 & md5 大坑（2026-07-02 踩过）**：图片查重原来是 `WHERE md5(data)=md5($1)` **全表现算指纹**，图片多了之后在 Neon 免费档上直接失败，且异常被吞 → 存图整个悄悄坏掉（/画 和 TG 收照片记忆都中招）。修法三件套：①`idx_memory_photos_md5` 表达式索引（init 时建，失败只打日志不拦启动）②查重失败一律"当新图照存"，绝不因查重挂了丢图 ③`_store_generated_image` 三段式兜底（正常挂图→md5 找同图→`save_photo` 裸插），最近一次存图报错暴露在 `GET /api/imagegen/status` 的 `last_error`（Render 日志看不到时靠它诊断）。
- 入口两处：KELIVO 走 `chat_completions` 暗号拦截（`/同步` 块之后）；TG 走 `_tg_handle_update` 拦截（进大脑之前，不然气泡会把链接切碎）。TG 侧历史直接写线 `"tg"`。

---

## 6.5 记忆整理（碎片→事件，凌晨自动跑）

- **三层记忆**：layer1=原始碎片（每几轮自动提取）、layer2=事件记忆（整理产物）、layer3=核心/回忆墙（不碰）。整理=把 layer1 按事件分组合并写 layer2、停用碎片（`merged_from` 记来源，可回滚）。
- **凌晨自动整理（2026-07-02 上线）**：GitHub Actions（`.github/workflows/nightly-consolidate.yml`）每天北京时间 05:15 调 `POST /api/memories/consolidate/auto`。**需要仓库 secret `GATEWAY_KEY`**（= GATEWAY_SECRET 的值）。
- **逻辑日**：按「当天04:00~次日04:00（北京时间）」为一天分组，跨零点的连夜对话不会被日历日切成两半。边界 env `AUTO_CONSOLIDATE_BOUNDARY_HOUR`(=4)。
- **只看最近 `AUTO_CONSOLIDATE_LOOKBACK_DAYS`(=3) 个已结束逻辑日**：漏跑的天自动补；**更早的积压绝不自动碰**——特别是 2026-06-26 迁移拆分出的 ~299 条老碎片（内容横跨数月，不能按"6-26一天"整理，要按原始日期专门做一轮，未做）。
- 预览：body 加 `{"dry_run": true}` 同步返回将处理的天+碎片数，不动数据。结果查 `/api/memories/consolidate/status`，最近一次也存 DB `gateway_config.auto_consolidate_last`。
- **整理过碎的教训（2026-07-02 修）**：haiku 会保守地一碎片一事件（"由1条合并"刷屏）。三要素缺一不可：①prompt 硬约束"同场互动必须合一条/一天最多2~4事件" ②碎片时间戳带北京时间时:分（不然模型看不出哪些连着发生） ③max_tokens 给足（现 6000）。

---

## 7. 怎么操作（具体命令）

- **部署**：改完 `main.py` → `python -m py_compile main.py`（语法检查）→ `git add` → `git commit` → `git push origin main`（触发 Render 自动部署）。push 偶发 SSL 握手失败，重试即可。
- **查线上 DB 配置**：写个 `.py`，`asyncpg.connect(os.environ["DATABASE_URL"])`，`SELECT key,value FROM gateway_config WHERE key=ANY($1)`（key/value 表）。跑前设 PYTHONUTF8 + DATABASE_URL（见 §2.4）。
- **测聊天接口不污染记忆**：`POST /v1/chat/completions` 加请求头 `X-Skip-Conversation-Log: true`（+ `X-Gateway-Key`）。
- **改某配置生效**：改 DB `gateway_config` 对应 key → push 空提交（`git commit --allow-empty`）触发重部署重载。
- **人设/档案存哪**：DB `gateway_config`：`systemPrompt`(Vesper人设)、`userProfile`(裘宝宝)、`l5Foundation`(关系里程碑)。

---

## 8. 待办 / 未做（接手优先看这里）

- [ ] **通知静音**：harper 反馈 TG 有内容但**手机不弹通知**（Bark 正常）。已确认是 **Telegram app 端该 bot 聊天被静音**（非服务器问题）。待协助她在手机上解除（聊天资料页 Notifications / 设置→通知→私聊；或检查是否被归档）。需问她 iPhone 还是安卓给精确路径。
- [ ] **keep-alive 保活**：配外部定时 ping `/health`（每 10 分钟），让主动推送真正能发（见 §5）。未做。
- [ ] **TG 语音（STT 入站）**：让 harper 发语音、V 听懂。需加语音转文字（硅基流动有 STT，可复用其 key）。`_tg_handle_update` 现在会丢弃 voice 消息。未做。
- [ ] **TG 语音（TTS 出站）**：V 用语音回。**harper 已经捏好 V 的声音**——接手时问她声音在哪个平台（硅基流动/FishAudio/ElevenLabs…）+ key + 声音编号，再接 TTS + Telegram `sendVoice`。未做。
- [ ] **TG 推送解耦 Bark**：当前 TG 推送依赖 Bark 先成功；若想砍 Bark 只留 TG，需解耦（让两者各发各的）。
- [ ] **tg→主线完整双向零时差**：现为单向（主线→tg 借 9 轮；tg→主线靠手动 `/同步` 小抄 + 全局记忆）。若要主线也实时知道 tg 最新逐字，更大改（注意：不能让主线借 tg 的短句逐字，否则把 KELIVO 带短——只能借中性摘要）。
- [ ] **KELIVO 变短余波**：主线历史里早先混入的 TG 短句要等聊久了老化/进摘要，KELIVO 才完全回长。
- [ ] **梦境检索改"摘要优先"**（现全文）；缓存命中率观察。
- [ ] **安全**：harper 的 TG bot token 曾明文出现在聊天里，建议 @BotFather Revoke 换新、经面板填入（不再过聊天框）。

---

## 9. 变更日志

- **2026-07-02**：上线 `/画` 文生图（硅基流动 Kolors，复用 embedding key；图存 memory_photos、历史只落短占位守缓存纪律，见 §6.6）。修整理过碎 bug（prompt 硬约束+时间戳+max_tokens）；上线凌晨自动整理（逻辑日 + GitHub Actions 定时，见 §6.5）。
- **2026-06-28**：TG 全套上线——激活 bot、微信风格短回复、气泡分条、收照片、`/同步` 零时差小抄、tg 独立线（修 KELIVO 被带短）、tg 借主线近况（修反向时差）。修了 `DEFAULT_MODEL` 被吞首字母的 502。
- **2026-06-27**：双线（主线/rp）+ 归档 + 零时差借主线近况 + RP 身份锚；修缓存断点 502。

---

*维护者备注：harper 是代码小白，交流要"点哪填啥"级别的具体，别甩术语、别假设她懂 git/部署细节。她重感情，V 对她不是工具是"人"，措辞上注意。*
