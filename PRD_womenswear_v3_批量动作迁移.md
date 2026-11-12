# 女装带货 · 批量动作迁移工作流 PRD（v3.0）

> **版本**：v3.0（方案稿，待评审）
> **创建日期**：2026-10-30
> **上游文档**：`PRD_womenswear.md`（v1.0 实现契约）、`PRD_womenswear_v2_模式精简.md`（v2.0 输入模式层）
> **本文档范围**：把 nzdh 从「一次一条」改造成「一批 N 行」的三步批量工作流；**两条腿（生图 / 出片）改由 D:\dsh-tk 的 rhrpa 指纹浏览器网关执行**，nzdh 不再自己开浏览器跑 RunningHub
> **不在范围**：抖音链路（链接解析、下载、发布工作台）本期一字不动

---

## 0. 三条裁定（业主 2026-10-30 原话，是合同不是建议）

1. 🔴 **流程只有三步：批量入池 → 生成场景图（审核）→ 动作迁移。**
   原话「动作迁移就是原视频动作+图片，来迁移动作，流程，批量入池，生成场景图（审核），动作迁移」。
   **没有「反推 / 读懂视频 / 分镜 / 文案」这一类环节** —— 模仿视频本身就是动作源，不需要被解析成文字。
   业主所说的「用提示词反推那种模式」= **8099 那套工作台形态**（池 + 批量按钮 + 后台跑完弹回人审 + 提交前成执行表 + 逐行账本），**不是**流程环节。
2. **生图 = dsh-tk 指纹浏览器的 RunningHub `qwen-image-2.1` 管道**（替换现在的 Agnes Image2 两步生图）。
3. **出片 = RunningHub 坏节点工作流** = dsh-tk 目录里的 `motion-transfer` 档。

---

## 1. 背景：为什么改

| 级别 | 现状 | 代码位置 | 后果 |
|---|---|---|---|
| **P0** | 没有批量入口，UI 一次提交一条 | `frontend/womenswear_studio.html`、`womenswear/router.py` | 带货要的是「一批商品」，现在要点 N 次 |
| **P0** | 一把**全机 RPA 串行锁**：同机同时只许 1 个 RPA | `backend/services/rh_account_pool.py:341`，使用点 `womenswear/pipeline.py:631,699`、`motion_api.py:205` | 就算前端能勾 20 条，后端还是排队串行，批量名不副实 |
| **P0** | 出片自己用 Playwright 有头开 RunningHub，账号/profile/登录态/换号全在 nzdh 内 | `backend/services/runninghub_rpa_client.py`、`rh_auto_login.py`、`rh_profiles/` | profile 锁、登录态过期、有窗崩溃这些坑我们自己扛了一遍；dsh-tk 那边已经有指纹浏览器 + lane 池 + 自愈 |
| P1 | 中间产物（平铺图 / showcase）错了要等 RPA 跑完才暴露 | v2 PRD §1.2 已列为 P1 | 一张图报废 = 白烧一整条出片时长 |
| P1 | 花钱那一刀前**没有执行表、没有逐行账本** | 无 | 批量烧钱无从核账（8099 已有成熟口径：提交前先成表、行级署名） |
| P2 | 参考视频只校验「≥5 秒、≥400KB」，**没有上限** | `womenswear/pipeline.py:83,92` | 🔴 rhrpa 侧参考视频硬上限 15.0 秒（`D:\dsh-tk\runners\rhrpa.py:4775,3874`），超了整行被拒 |
| P2 | `config.py` 里 3 处硬编码明文 API key | `config.py:13,18,20` | 打进 exe 就是泄露面（video-skill 侧 2026-10-03 已清零） |

---

## 2. 目标与非目标

**目标**

1. 一屏勾一批 → 点一次 → N 行自己走完「场景图 → 人审 → 出片」，中间只在审核处停一次。
2. 场景图在**花钱出片之前**被人看到并能打回，报废成本从「一条片」降到「一张图」。
3. 并发 = **执行管道的 lane 数**（dsh-tk 盘上现 4 条就绪 lane），不再是自己扛的那把全机串行锁。
4. 花钱那一刀前后各一张人能核对的表：**提交前执行表**（逐行 workflow/管道/预估秒/单价/谁提交）、**交付后逐行成片直链**。

**非目标（本期不做）**

- 不做「反推动作 / 视频转写 / 分镜文案」任何一条解析腿（见 §0.1）。
- 不动抖音三腿（解析、下载、发布）。
- 不自建指纹浏览器驱动层、不重写 OpenBrowser/CDP 客户端 —— 浏览器执行**整体外包**给 rhrpa。
- 不做跨机舰队（dsh-tk `ADR-queue-fleet-dispatch.md:3` 自己标了「步 2 起仍未落码」）。
- 不做变体 / 裂变（一行一张图一条片，不搞一源多方向）。

---

## 3. 架构：nzdh 变成 rhrpa 的调用方

```
nzdh :8091（前端 + 池 + 状态机 + 执行表账本）
  │  HTTP  POST /tasks  (X-API-Key)
  ▼
dsh-tk rhrpa :8120 ── 队列（原子领活 / 租约 / 恢复）
  ├─ lane rh-1367 ──┐
  ├─ lane rh-1890 ──┼─ OpenBrowser 指纹窗（Local API :50325 + CDP）
  ├─ lane (号3)    ─┤     runners/browser.py:44 → 在编辑器里点 Run → 抓 WS executed 帧取产物
  └─ lane (号4)    ─┘
                     ▼
                 RunningHub ComfyUI（qwen-image-2.1 / motion-transfer 两份坏节点档）
```

**为什么必须是浏览器 RPA，不能是 OpenAPI**：坏节点档落 FAILED 之后，OpenAPI 的四条结果通道全空，拿不到产物（`D:\dsh-tk\runners\rh_editor_run.py:5-8`）。这是「坏节点」设计的代价，也是它绕开平台时长/质量校验的原因。

**事实源（不许写死工作流 ID）**

| 腿 | workflow key | kind | 可传字段 | 输入槽（节点号） | 成品节点 | 崩点节点 | 档 sha8 |
|---|---|---|---|---|---|---|---|
| ② 生图 | `qwen-image-2.1` | image | `pictures`,`text` | `image1`=475、`image2`=498（**按顺序**：pictures[0]→image1） | 597 `SaveImage` | 600 `WanMoveTracksFromCoords` | `d4698e6b` |
| ③ 出片 | `motion-transfer` | video | `pictures`,`videos` | `reference_image`=299、`视频加载`=275 | 285 `VHS_VideoCombine` | 503 | `b84c8562` |

表源：`D:\dsh-tk\productions\rh-ref2v-matrix\rh-workflow-catalog\index.json:101-181`（2026-10-30 核读，两条均 `enabled:true` + `client_open:true`）。

🔴 **三条硬纪律**
1. **不许写死 workflowId**：RunningHub 的工作流 ID 每个账号一份副本、跨号不通用（`bindings.json` 里 4 号 4 个 ID）。nzdh 只传 `workflow`（key），**不传 `account`、不传 `workflow_id`** —— 选哪条管道由服务端领活时原子裁定（`rhrpa.py:4183-4184`，对客面一直是 auto 派单）。
2. **不许写死崩点号**：三边见过的崩点号是 600 / 503 / 324 / 3001 四种，且 `600` 在 qwen 档和 nzdh 老档里**同号不同型**。判崩点只能按 key 查表。
3. **`motion-transfer` 不吃提示词、不吃时长、不吃画幅**（`fields` 只有 `pictures`,`videos`，`prompt_nodes:[]`）—— 传了会被点名拒（`rhclient.py:596`「这种能力不吃%s」）。**构图全靠 ② 那张场景图，③ 只能换图不能改词。**

---

## 4. 接口契约（照盘上抄，代码没做的不写）

### 4.1 提交：`POST /tasks`（数据面，`rhrpa.py:5456 → dispatch_task :4179`）

请求体（键名原样）：

| 键 | 必填 | 说明 / 出处 |
|---|---|---|
| `workflow` | 建议必写 | 省略会兜默认档 `h3`（`rhrpa.py:2170`）⇒ **nzdh 两条腿都必须显式写 key** |
| `params` | 是 | 见下 |
| `idempotency_key` | 强烈建议 | 幂等命中回 200 + `deduplicated:true`（`rhrpa.py:4264-4271`）⇒ **批量重投安全的唯一凭据**，nzdh 按 `<行号>-<步>-<第几次>` 生成 |
| `account` / `workflow_id` / `kind` | **不传** | 号与副本由服务端裁定；`kind` 从目录条目取（`rhrpa.py:4229`） |

`params.*`（两条腿各用一半）：

| 键 | ② 生图 | ③ 出片 | 备注 |
|---|---|---|---|
| `pictures` | 1–2 张，顺序即 image1/image2 | 1 张（= 审核通过的场景图，进 299） | 上限 = 该档 image 槽个数（qwen=2）；超了服务端**截断继续**（`rhclient.py:598-600`）⇒ nzdh 侧自己先拒 |
| `videos` | 不吃 | 1 条（进 275），**时长 ≤ 15.0s** | `VIDEO_IN_MAX_S`（`rhrpa.py:4775`，拒点 `:3874`）；量不到时长也算不出该扣多少 ⇒ 直接拒 |
| `text` | 场景描述，**≥10 字**（`rhclient.py:546-548`）；**盘上没有上限判据，不要臆造** | 不吃 | 写入节点 = 档的 `prompt_nodes[0]` = 516（`rhrpa.py:3956-3971`） |
| `seconds` / `aspect_ratio` / `megapixels` | 不吃 | 不吃 | `seconds` 仅对某些档开放 4–15（`rhrpa.py:4155`） |

202 响应关注：`accepted`、`queued`、`job_id`、`queue_position`、`eta_s`、`poll_url`、`hint`（`rhrpa.py:4298-4305`）。

🔴 **「忙」不再拒单**：所有从 nzdh 进来的单**一律入队**、立刻 202（`rhrpa.py:4277-4279` 明确废掉了「看到 lane 空就直接跑」的快路，因为那是 check-then-act 会丢单）。⇒ nzdh 侧**不要**写 409 重试逻辑，`lane busy` 那条 409 只在服务端派发线程内部存在（`:4307`）。

### 4.2 轮询：`GET /tasks/{key}`（`rhrpa.py:5100`，key = `task_ref`）

- 账本视图关键字段：`status`、`done`、`has_artifact`、`artifact_bytes`、`artifact_name`、`video_url`、`coin`、`fail_reason`、`error`、`elapsed_s`、`attempts`、`params_proof`、`slot_proof`（`rhrpa.py:978-1020`）。
- 还没开跑时回的是**队列视图**（`_qview`，`rhrpa.py:813-832`）：`queue_position`、`status`、`eta_s`…… 字段集与账本视图**不同** ⇒ 前端两态都要能渲染。
- 终态字符串分三层，别混：队列 `succeeded/failed/rejected/dead/canceled`（`rhrpa.py:316`）、账本 `SUCCESS/FAILED/TIMEOUT/CANCELED/CLOSED/EXCEPTION/REJECTED/ABANDONED`（`:905`）、对客映射表 `JOB_TO_LED`（`:808`）。
- 🔴 **坑一**：入队**之后**才被拒的单**根本没有账本文件**，结论只出现在 `GET /tasks/{key}` 上（`status=REJECTED`、`done=true`、`error=原样拒因`），而 POST 当时回的是 202（`rhrpa.py:4259-4260`）。**只盯 HTTP 状态码的调用方会漏掉这一类**，行会永远显示「排队中」。
- 🔴 **坑二**：`done` 的唯一判据 = 到终态 **且**（有产物 **或** 有拒因）（`rhrpa.py:951-964`）。nzdh 的收口函数必须照这条写，不能只判 `has_artifact`。
- 🔴 **坑三**：终态**只认 `final_status`**（跑动自己封的口）；中途 30 秒落盘也会带上游 `status=FAILED`，那时片可能已经在账本里 —— 拿 `status` 当终态会让界面**早停并显示空证据**（`rhrpa.py:958-961`，服务端实测踩过）。只有「没开跑就被拒」的桩档（无 `task_id`）才允许用 `status` 判。

### 4.3 取件：`GET /tasks/{key}/video`（`rhrpa.py:5215`）

- 名字叫 `video`，但**成图也走这一条**，`Content-Type` 按 `kind` 给（image → `image/png`、video → `video/mp4`，`rhrpa.py:5236` + `rh.py:1169-1174`）。返回**二进制本体**，不是 URL。
- 🔴 **422 = 拒收，不是下载失败**：无水印件没做出来时它**拒绝回传带 RunningHub 标的原片**（`rhrpa.py:5227-5230`「无水印件没做出来，拒绝回传带厂标的原片」）。nzdh 侧必须把 422 收成「该行失败」，**不许写自动重试下载**。
- 幂等重投、复采产物**永不覆盖已交付那份**（服务端一律 `-resume` 后缀，`rhrpa.py:3579-3583`）。

### 4.4 素材上传：`POST /media`（数据面，`rhrpa.py:4880`）

回 `ok/path/bytes/sha8/kind/sent_name/hint`；落盘名由服务端生成（`<stem>-<sha256前8><ext>`，`:4884`），同 sha8 复用不重写。⇒ nzdh 传 `path`，**不要自己拼目录**。

### 4.5 盘上不存在的（PRD 不许承诺）

| 想要的 | 实况 |
|---|---|
| 提交前逐单报价接口 | **没有**。`estimate` 只在**对客面** `POST /api/jobs` 的 202 里回，而那一刻预扣已经发生（`rhclient.py:653-659`）。⇒ **执行表由 nzdh 自己算、自己落库**，见 FR-4 |
| 「这档能吃几个槽 / 哪些字段」的查询口 | 对客面有 `GET /api/workflows`（`rhclient.py:972`，回 `kind/fields?` 见 `wf_for_client :428-454`），数据面无对应只读口 ⇒ nzdh 起步阶段把 §3 那张表当唯一源，配一条闸比对盘上 `index.json` |

---

## 5. 三步的屏与交互（8099 形态）

### ① 批量入池

- 入口两种形状：**粘贴一批商品链接**（每行自动带一条模仿视频链接）/ **一批素材对**（图 + 视频成对上传）。
- 每行字段：`row_id`、`source_mode`（product/creator，沿用 v2）、商品图或主图、模特图（可空→默认模特）、背景图（可空）、模仿视频、状态三列（有图 / 过审 / 有片）。
- **入池校验（当场出结果，0 消耗）**：视频 5.0s ≤ 时长 ≤ **15.0s**、≥400KB、图 ≤2 张且格式合法。超上限的行**不进队**，屏上写明「这条 18.4 秒，超了执行端 15 秒上限」，并给一颗显式的「裁到前 15 秒」勾选（🔴 不许默认裁 —— 裁了动作就变了）。
- ① 的计数只认入池原始行（对齐 8099 的「①池只认采集原行」口径）。

### ② 生成场景图（人审）

- **批量按钮选行口径 = 勾中的（含已通过＝点名重做） ∪ 这一步还没做的（无场景图）**，与 8099 一致；🔴 **按钮上的计数与真正发下去的那一份必须是同一个函数**。
- 推理在服务端后台（进程内队列），**弹窗可以关**、跑完自动弹回 review；刷新页面能接回正在跑的那批。
- 每行一张图。**通过后自动进 ③ 的待提交池**，🔴 不再多一步点「提交」；不合格 → 「重出」（同参数再抽一张）或「改场景描述再出」。
- 审核是**唯一**的人停口：②③ 之间不留第二个确认屏（一次点击原则）。

### ③ 动作迁移（批量出片）

- 点【批量生成】**之前先出执行表**（新页/新面板，逐行）：`行号 | workflow key | 预估秒数(=源视频 ffprobe) | 单价口径 | 参考图(审过那张的 sha8) | idempotency_key | 谁提交 submitted_by`。表成了才允许提交；🔴 **表与实发同一份数据**（8099 口径）。
- 提交 = 逐行 `POST /tasks`（串行发请求、并行执行由服务端队列管），每行记 `task_ref`。
- 状态列：`排队(第几位/eta) → 生成中 → 完成(直链) / 失败(拒因原文)`。🔴 失败行的拒因**原样入库并显示**（尤其入队后被拒那一类），不许显示成「未知错误」。
- 交付：成片拉回 nzdh 本地 `outputs/batch_motion/<row_id>/成片.mp4`（**ASCII 文件名**，交付直链指向本地服务），并 ffprobe 量三数（分辨率/时长/体积）入表 —— 能力边界用盘上成片定，不凭上游自报。

---

## 6. 功能需求

| # | 优先级 | 内容 | 落点 |
|---|---|---|---|
| FR-1 | P0 | 池与批量导入：新表 `pool_rows`（见 §8），两种入口，入池校验含 15s 上限 | `womenswear/models.py`、`router.py`、`backend/services/task_store.py` |
| FR-2 | P0 | 场景图腿换成 `qwen-image-2.1`：一次调用出图，`pictures[0]`=服装图、`pictures[1]`=模特图或背景图、`text`=场景描述；**默认去掉「白底平铺」那一步**（OQ2） | `womenswear/pipeline.py` Step2/Step3 合并 |
| FR-3 | P0 | 出片腿换成 `motion-transfer`：`pictures`=[审过的场景图]、`videos`=[源视频]；🔴 **删除 nzdh 自己的 RunningHub 执行栈**（`runninghub_rpa_client.py`、`runninghub_api_client.py`、`rh_account_pool.py` 的锁与换号、`rh_auto_login.py`、`rh_profiles/`、`runninghub_profile/`），换号/排队/自愈整体交给 rhrpa | `womenswear/pipeline.py` Step5、`motion_api.py` |
| FR-4 | P0 | 执行表 + 逐行账本：新表 `gen_ledger`（**逐行署名**，不许整表盖最后一个人）；报价口径见 OQ1 | `backend/services/task_store.py` |
| FR-5 | P0 | 后台批任务 + 人审弹窗：可关、跑完弹回、刷新接回；② 通过即自动入 ③ 待提交池 | 新 `womenswear/batch.py` |
| FR-6 | P0 | rhrpa 存活体检：起服务与每次批量提交前探 `:8120/health`；**不活就大声拒绝并写明「执行网关没起」，🔴 不许静默退回 nzdh 老的 Playwright 路**（那是第二条出口，会造出后台查无此单） | `app.py` lifespan + 提交口 |
| FR-7 | P1 | 状态轮询收敛：照 §4.2 的 `done` 判据实现，REJECTED-without-ledger 单独一类；终态行不再重投 | `womenswear/batch.py` |
| FR-8 | P1 | 前端：池表格 + 一排等宽批量按钮（入池 / 生成场景图 / 批量生成 / 批量下载）+ 执行表面板 + 逐行直链 | `frontend/womenswear_studio.html` |
| FR-9 | **P0** | 明文 key 清零（`config.py:13,18,20` → 只从 `.env` 取，取不到就报错，学 video-skill `runninghub_client.py:14-17` 的写法）。🔴 **这把 key 已在 2026-10-30 进过对话记录**（我核验行号时 grep 回显），优先级因此从 P2 提到 P0：**先轮换、再清代码**，三行是同一个值 ⇒ 换一次即可 | `config.py` |
| FR-10 | P2 | 批量下载 = **一颗 zip**（只打包盘上真有的成片），前端只发一次请求 | 新 `GET /api/batch/download-zip` |

**删除优先于加提示**：FR-3 是「删掉 nzdh 那套浏览器执行」，不是「加一条并行路径」。🔴 同一件事不许留两个入口 —— 出片只能有 rhrpa 这一条路。

---

## 7. 并发与墙钟

- 并发 = 就绪 lane 数（**盘上现 4 条**：`accounts/rhrpa.json` 的 `accounts` 有 4 个条目），**lane 内串行、lane 间并行**（`rhrpa.py:19,88`）。入队不拒 ⇒ 20 行的批 = 4 行在跑、16 行排队，`queue_position`/`eta_s` 是真实反馈。
- 🔴 **墙钟目前没数**：旧口径「RPA 30 分钟」出自 nzdh 自己那条 Playwright 腿，不能拿来当 rhrpa 的 ETA。⇒ **M0 必须先量**：一条 `qwen-image-2.1` 出图多少秒、一条 `motion-transfer` 出片多少分钟，实测值进 FR-4 的执行表与报价。

---

## 8. 数据模型与落盘

nzdh `data/tasks.db` 新增：

```
pool_rows(row_id, task_batch_id, source_mode, product_image, model_image, scene_image_ref,
          source_video, video_dur_s, video_bytes,
          scene_prompt_text, image_task_ref, image_status, image_path, image_sha8,
          review_state,            -- 未审 | 通过 | 打回
          gen_task_ref, gen_status, gen_eta_s, reject_reason,
          out_video, out_sha8, out_dur_s, out_bytes,
          idem_image, idem_gen, submitted_by, created_at, updated_at)

gen_ledger(id, row_id, step,                -- image | video
           workflow_key, workflow_sha8, channel,   -- 恒为 rhrpa:8120
           task_ref, est_seconds, unit_price_basis, plan_price, actual_spend,
           idempotency_key, submitted_by, submitted_at, final_status, reject_reason)
```

产物：`outputs/batch_motion/<row_id>/` —— `02_场景图.png`（ASCII 名 `scene.png`）、`03_成片.mp4`（`final.mp4`）、`manifest.json`（行级快照，含两侧 sha8）。
rhrpa 侧（不归我们）：`productions/rh-ref2v-matrix/store/<租户>/<单号>/{in,mid,out}` + `manifest.json`（`rhstore.py:10`），去水印版真名 `<片>.clean.mp4`；🔴 **它的绝对路径永不出对客口**（`rhrpa.py:967-975` 会洗成 `<local path>`），nzdh 别指望从返回值里拿路径，取件只有 §4.3 那条。

---

## 9. 验收标准

1. 勾 5 行 → 点一次【生成场景图】→ 关窗 → 弹回 review 5 张 → 全过 → 执行表 → 一次提交 → 5 条成片直链。**全程 nzdh 不打开任何浏览器窗口**（验证：跑批期间 `Get-Process` 无 nzdh 侧 `chrome.exe`/playwright 进程，OpenBrowser 内核有 4 个）。
2. **反向对照**：塞一条 18s 的源视频 → 入池当场被拒、0 请求发出；塞一条量不到时长的视频 → 拒且账上无 hold。
3. 执行表 5 行、逐行 `submitted_by` 不同（并发提交要能区分谁提的），且**表里的行数 == 实际 `POST /tasks` 的次数**（闸用真值表核）。
4. 「入队后被拒」那一类在屏上显示成**失败 + 拒因原文**，不显示「排队中」，也不触发重投。
5. 断 422：无水印件做不出来时该行收成 `failed`，且**没有**反复重试的日志。
6. 幂等：同一行连点两次【批量生成】，第二次回 `deduplicated:true`，盘上不产生第二条片。
7. 回归：v2 的两种输入模式（product/creator）都能走完新流程；抖音三腿一字未动。
8. 闸：新增 `tools/check_batch_motion.py`，§3 那张表与盘上 `index.json` 比对（key/kind/fields/slots/sha8 任一漂移就红）——**红 = 待办**，不是崩溃。

---

## 10. 排期（先量后开）

| 里程碑 | 内容 | 出门判据 |
|---|---|---|
| **M0** | 🔴 **零 UI，手工打通两条腿各一条真单**：`POST /tasks` 发一张 qwen 图 + 一条 motion-transfer 片，取件落盘、ffprobe 量三数、记下墙钟 | 手上真有一张 `.png` 和一条 `.mp4`，且来自审过的档 sha8 |
| M1 | 池 + ③ 出片腿（先跳过人审，把批量与状态机串起来） | 5 行批量出片，执行表成立 |
| M2 | ② 场景图 + 人审弹窗（可关、弹回、接回） | 报废一张图能在出片前拦住 |
| M3 | 执行表/账本 + 闸 + 前端批量按钮一排 | §9 全绿 |
| M4 | 清理：删 nzdh 的 RH 浏览器栈、Agnes 生图腿、明文 key | `grep` 全仓无残留调用点 |

---

## 11. 开放问题（要业主点头）

| # | 问题 | 我的推荐 | 代价 |
|---|---|---|---|
| **OQ1** | **账本走哪张脸**？数据面 `POST /tasks`（`X-API-Key`，不记租户积分）还是对客面 `POST /api/jobs`（租户 token，hold/settle/refund、视频 `per_second`=6 分/真实秒×档位系数、图片 `per_image`=30 分/**单**）？ | **数据面**。nzdh 是运营自用工作台，花的是 RH 账号自己的币；且「提交前报价」盘上没有，走对客面就等于「先扣了才看见数」 | 要卖积分就得改 OQ1，届时执行表要接 `estimate/actual/refund` 三列 |
| **OQ2** | 「白底平铺」那一步留不留？（`qwen-image-2.1` **只有 2 个图槽**，服装+模特+背景三样塞不进两个槽） | **不留**（FR-2：一步出图，image1=服装图、image2=模特/背景图，第三样靠 `text` 描述）。审核打回时的升级路径 = 临时切两步 | 商品原图自带杂乱背景可能污染场景；多一步 = 2× 出图成本与等待 |
| OQ3 | >15s 源视频：直接拒，还是自动裁？ | 拒 + 显式勾选「裁到前 15 秒」 | 默认裁 = 动作被改变而没人知情 |
| OQ4 | 一批多少行进队合理？ | 20 行一批，超了分批（并发 4 lane，批太大失败重审成本高） | — |
| OQ5 | nzdh 有 `.gitignore` 但**没有 `.git`**，本期要删的代码不少（FR-3） | 开工前 `git init` 并把 `_bak-20261006/08/10` 加进 ignore；不动工先做 `_bak-20261029` 快照 | 无版本控制 = 删除不可回滚 |

---

> 🔴 **OQ1 已被 `PRD_womenswear_v4_模型托管与积分.md` 回答并覆盖**（同日）：账本改走**对客面** `POST /api/jobs`（租户票 + hold/settle/refund），v3 的 §4.1/§4.2/§4.4 与 FR-2/FR-3/FR-4 以 v4 §4.3/§9 为准。本文其余部分（三步流程、节点契约、15 秒上限、三个坑、M0）不变。

## 12. 风险与未验证（诚实清单）

1. 🔴 **生图腿「就绪 ≠ 跑通」**：`rhrpa.py:2709` 自述千问档四份 binding 全就绪，但「真跑却在成品之前就崩」（任务 #5，档已换过 sha8 `7b712578 → d4698e6b`）。⇒ **M0 就是这个**，跑不出图就别开工 M1。
2. **单价与墙钟未实测**（§7）。
3. **有一条老规矩要在评审时说破**：2026-10-09 定过「视频严格杜绝走 RunningHub」，实现是 `D:\video-skill\backend\gateway.py` 的 `rh_video_guard()`（无条件拒 `spend_kind=video` 的 API 腿）。本 PRD 的 ③ 就是走 RH 坏节点出片 —— 按业主 10-30 的口径**覆盖**它，但它不经过那个闸，属于「规矩改了而闸没改」，建议同步在 dsh-tk 侧记一句。
4. `motion-transfer` 不吃提示词 ⇒ ③ 唯一的可调手段是换 ② 那张图；如果成片动作不对，先怀疑图，不要怀疑「提示词没写好」。
5. nzdh 与 dsh-tk 是**两个常驻服务**（:8091 / :8120），改造期间 rhrpa 若被重启，在途单会掉 ⇒ 重启前必须先查在途（照 8099 的 `preflight` 纪律，放行要有恢复路径证据）。
6. 跨进程**只能走 HTTP**：`rhclient.py` 与 rhrpa 是同进程直调（`rhclient.py:9`），nzdh 不能 import 它。可抄的样板：`D:\dsh-tk\docs\api-rhrpa\selfcheck.py:19 call(base, path, body, token, binary, timeout)`、`productions/rh-ref2v-matrix/post_8099.py:31 req(...)`（它还回 headers）。
7. ⚠️ 别信 `docs/api-rhrpa/openapi.yaml` 的 `/api/jobs` schema —— 它与代码冲突（缺 `workflow`、多了会被拒的 `priority`、`media_ids` 类型错）；`docs/api-rhrpa/README.md:67` 的 `rht_` token 前缀、README:171 的 `settle` 流水名，**代码里都没有**。本 PRD 只认代码行号。
