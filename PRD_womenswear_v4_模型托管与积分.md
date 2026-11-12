# 女装带货 · 模型配置托管 + 客户充值积分 PRD（v4.0）

> **版本**：v4.0（方案稿，待评审）
> **创建日期**：2026-10-30
> **上游文档**：`PRD_womenswear.md`（v1）、`PRD_womenswear_v2_模式精简.md`（v2 输入模式）、`PRD_womenswear_v3_批量动作迁移.md`（v3 批量三步）
> **本系统指代**：`D:\nzdh` = 打包成 exe/zip 交付给客户的「动作迁移工作台」。若你其实指的是 8099 工坊或 DSH 对客面，说一句我换宿主 —— §3 之后的形状不变。
> **本文档要解决的是一句话**：**客户不应该配置模型。他把票填进来、有积分，就能出片。**

---

## 0. 一句话结论

**盘上已经有一套八成成熟的客户积分账本（`D:\dsh-tk\runners\rhclient.py`：租户/余额/预扣/结算/退款/价版本/对客话术屏蔽），缺的不是账本，是三样：① 客户能自己把积分充进来的口（今天只有运营手工加分，租户 token 打那个口必 401）；② 「模型配置」从客户面前整块搬走；③ 公网接口的鉴权收紧（现在裸 TCP 直达控制面，这是硬前置）。**

---

## 1. 背景：客户今天被迫要配的四样

| # | 客户要做的事 | 代码位置 | 为什么他不适合 |
|---|---|---|---|
| 1 | 用我们的邀请码**注册/登录 RunningHub**（真人手机号+验证码，600 秒超时被动抓 Cookie） | `womenswear/router.py:1037-1064` → `rh_auto_login.py:429-516`；弹窗判据 `router.py:1021-1034` | 电商老板不是来注册 AI 平台的。这一步今天就有人卡死 |
| 2 | 自己去 `platform.agnes-ai.cn` 注册、**粘 API Key** | 引导 Step2 `frontend/womenswear_studio.html:2640-2654`，热改 9 个键 `router.py:1138-1188` | 🔴 引导**只验输入框非空、不验 Key 真伪** ⇒ 坏 Key 能过引导，跑到生图一半才炸（`image2_client.py:142` 裸 `raise_for_status`） |
| 3 | 自备 64 位 Python 3.10–3.14 + 跑 `playwright install chromium` | `PACK_README.md`（生成源 `pack_full.py:218-272,253-257`） | exe 路线本来为解决它，**但最近一次构建 10-02 在 csc 步骤失败**（`dist\exe-build\step.log`），现在还在用 zip |
| 4 | **号里的 RH 币用完要自己充值**（隐性，屏上只写「请给账号 {phone} 的 API 钱包充值」） | `runninghub_api_client.py:165-169`；余额字段 `rh_account_pool.py:69-70` | 我们自己的运营文案都还在说「给账号充值」，客户看了只会懵 |

**计费痕迹核查**：nzdh 全仓搜 `积分/credit/余额/recharge/充值/套餐` —— **客户积分/充值/套餐，盘上没有，连半成品都没有**。命中的只有 RH 平台侧余额（那是号主的钱）、Agnes 额度提示、网关调用 jsonl（无金额）。唯一谈到"卖积分"的是我自己上一份文档：`PRD_womenswear_v3_批量动作迁移.md:246` 的 OQ1，当时明确写「不记租户积分」。

**这份 PRD 就是来回答那个 OQ1 的，并把 v3 的相关决定改掉（见 §9）。**

---

## 2. 目标与非目标

**目标**

1. 客户面上**不再出现任何 base_url / api_key / 模型名**：模型清单由我们侧下发，客户只看见「能做什么、多少钱一次」。
2. 客户拿到包后只需要一样输入：**一张票**（租户 token）。注册第三方 AI 平台这件事从流程里消失。
3. 客户能把积分充进来：**卡密即时到账**为主路，**充值申请单 + 运营确认**为兜底。
4. 每一分钱可核：`Σ(充值+卡密+退款) − Σ(实扣) == 当前余额`，前端自己算，对不上就大声报警（这套对账 `docs/client-ui/index.html:649-676` 已经实现，直接复用）。
5. 交付包里**结构上没有凭据**：不是"打包时打码"，而是代码里就不存 key（v3 的 FR-9 一并解决）。

**非目标（本期不做）**

- 不做在线支付（微信/支付宝/订单/发票）。客户是小老板，走「他转账 → 发卡密」最快，接支付是另一个月的活。
- 不做改密/找回/多端共存（`rhclient.py:1156` 明说没有，规划文档也列为不做）。
- 不在 nzdh 建第二套账本（见 §3 定案）。
- 不替客户配"模型选型"这种专业动作：客户不选模型，只选 `workflow`（做哪一种能力）。

---

## 3. 定案：四层各归一处（这是本 PRD 的核心，评审先拍这一节）

| 层 | 定案 | 为什么 |
|---|---|---|
| **身份** | 客户 = `tenants` 表一行；凭 = **一把租户 token**（`secrets.token_urlsafe(24)`，盘上只存 `sha256`，只回显一次：`rhclient.py:336-357,836-850,1207-1210`） | 已有，别再造。「客户不适合配模型」的正解是「客户只持一张低权限票」 |
| **账本** | **唯一源 = `D:\dsh-tk\runners\rhclient.py` 的 `fleet.db`**（tenants/credits/ledger/jobs/rate_history/media，`rhclient.py:172-192`）。nzdh 不建自己的积分表 | 见下方 🔴 冲突说明 |
| **关口** | 提交 = `POST /api/jobs`：**先算 estimate → 预扣 hold → 入队**；余额不足 **423 且不建单不入队不写流水**（`rhclient.py:636-645`）；失败/被拒/撤回**全额退**，真实时长比预估长则**补扣到负余额**（`settle():296-333`） | 这套状态机盘上成熟度最高，且幂等（`on_station_terminal():372-373`） |
| **挂载** | 真 key、真 RH 号、真 lane **全部留我们侧**（rhclient → rhrpa → OpenBrowser 指纹窗）。nzdh 只发 `workflow` key + 素材 + 提示词 | 与 `gateway.resolve()` 同构：调用方只见「能力名 + purpose」，凭证从 `.env` 间接取（`gateway.py:3-4,291-325`） |

🔴 **有一处既有裁定会跟这节打架，必须当场说破，不能我自己悄悄选边**：
`C:\dsh\docs\PRD-weeksir-platform.md:276,288` 与 `ADR-model-gateway.md:705` 写的是「**DSH Agent 不自建用户表，身份/密码/积分/订单以 weeksir.com 客户中心为唯一账本**」「不在 DSH 里另开充值」。
而 weeksir.com 那套后端**不在这台机器上**，且对接缺三块（token 获取、POST 体、素材上传接口 —— `ADR-input-form-single-port.md:149`；`ADR-model-gateway.md:848` 自己列为未决勾）。

⇒ 本 PRD 的立场：**rhclient 账本是"weeksir.com 账本的本地适配层"，接口形状按 weeksir 对齐，不新造第三套**。将来切 weeksir 时，nzdh 客户端**一字不改**（换的是票的发放方与扣费后端）。

✅ **这一条不用你拍了 —— 10-30 你已经答了**：「我要实现商业化，要客户登录，充值积分，要卖钱」= **不等 weeksir API，现在就建**，账本先挂 rhclient。我把 §11 的 OQ1 标成已定，只在文档里留一句：weeksir 那三个缺口（token 获取 / POST 体 / 素材上传）是**迁移日**的活，不是今天的前置。

---

## 4. 形状 A：客户面上「模型」变成什么

### 4.1 删掉（客户再也看不到）

`config.py:12-22` 里 9 个客户可见字段：`IMAGE2_BASE_URL/IMAGE2_API_KEY/IMAGE2_MODEL`、`MIMO_*`、`OPENAI_*` ⇒ **整组删除**（不是"留默认值"，是删字段 + 删引导 Step2 + 删 `/api/settings/agnes` 的写 key 分支 `router.py:1160-1165`）。

同时删掉**客户侧的 RunningHub 账号体系**（这是本 PRD 最大的产品收益）：`rh_accounts.json`/`rh_account_state.json`/`rh_profiles/`/`rh_auto_login.py`/引导 Step1 与"注入工作流"那一步（`router.py:1067-1092`、`rh_account_pool.py:881-938`）。执行账号由我们的 lane 池承担（v3 §3）。

### 4.2 新增（客户只看这三样）

| 客户看到的 | 来源（**盘上已实现，直接接**） | 字段 |
|---|---|---|
| 一张票输入框 + 「激活」 | 校验 = 打 `GET /api/me`（`rhclient.py:952-971`） | `tenant`、`balance`、`price{display,per_second,per_image,tiers,version}`、`ratios`、`seconds_min/max`、`video_max_s`、`megapixels_min/max`、`signup` |
| 「能做什么」列表（不是模型名，是能力标题） | `GET /api/workflows`（`972-991`，只列 `enabled AND client_open`；`available = ready AND sell`，两个闸不许合成一条 `447-453`） | `key`、`title`、`kind`、`fields`、`slots`、`bill_unit` |
| 剩余积分 + 一次大概多少分 | 同 `/api/me` + `GET /api/capacity`（`992-1004`，只给号池**数量**，不给号名/机名） | `balance`、`price.*` |

屏上纪律：**只显示积分，不显示钱**（沿用 `docs/PRD-client-credits-ui.md:75` 的裁定）；`lane/station/号池/副本/熔断/租约/死信` 这类内部词一律被客户面拦掉 —— 拦法是**白名单拦词**而不是列几条已知句去翻译（`CLIENT_ERR_INTERNAL:458-463` + `client_err():476-484`，这条形状是对的，照抄）。

### 4.3 两条腿的调用改形（覆盖 v3 的 §4.1）

| 腿 | v3 的决定 | v4 改成 | 请求体（键名原样，`rhclient.py:524-581`） |
|---|---|---|---|
| ② 场景图 | `POST /tasks` 数据面 `params.pictures/text` | **`POST /api/jobs`** `{workflow:"qwen-image-2.1", media_ids:{pictures:[mid,mid]}, prompt}` | `prompt ≥10 字`（`546-548`，🔴 盘上**没有**上限判据，不要臆造）；`media_ids` 必须是 dict，值来自 `PUT /api/media`（`1297-1368`，回 `media_id/bytes/sha8/kind`） |
| ③ 动作迁移 | `POST /tasks` 数据面 `params.pictures/videos` | **`POST /api/jobs`** `{workflow:"motion-transfer", media_ids:{pictures:[mid],videos:[mid]}}` | 该档 `fields` 只有 `pictures,videos` ⇒ 传 `prompt/ratio/seconds` 会被点名拒（`596`「这种能力不吃%s」） |

客户**禁止**传的字段（照抄，别自己发明）：`account/station/lane/priority/workflow_id/wf_key/task_key/tenant_id/rate/per_second/price/discount/credits/tier`（`CLIENT_DENY_FIELDS:50-51`，传了就 400）—— 用哪个号、哪台机，由中控定。

⚠️ **对客口有两个码要分开处理**：能力开了销售口但没有就绪管道 ⇒ **503**「暂时没有可用管道，稍后再试（预扣没有发生）」（`539`）；没开销售口 ⇒ **503**「暂未开放下单」（`540`）；key 写错/下架 ⇒ **400**（`535`）。都是**没扣钱**的，界面不许显示成失败订单。

### 4.4 v3 那张执行表怎么办

保留，但**数据来源换成对客口的 `estimate`**：`POST /api/jobs` 的 202 就回 `estimate/balance/unit_price/rate_version`（`686-692`）。🔴 注意语义：**那一刻预扣已经发生**（`651-659`），所以「提交前先看表」这件事在 v4 仍然只能由 nzdh 侧自己算一份**计划表**（行 × 预估秒 × 当前价），再一发点击提交；「先报价后扣」这个口**盘上不存在**（`grep quote|/api/estimate|dry_run` 两服务零命中），别对客户承诺。

---

## 5. 充值：新建的就这两块（+ 必须修的 4 个洞）

### 5.1 卡密（主路，即时到账）

```
redeem_codes(code_sha, amount, batch, created_by, created_at, used_by_tenant, used_at, note)
POST /api/redeem {code}  →  move(con, tid, +amount, "recharge:redeem", "tenant")
```
- 复用 `move()` 这个**唯一余额写原语**（`rhclient.py:282-293`：余额与流水同函数落，不可能改余额不留痕）；`LEDGER_REASONS:466` 扩一类（现在只有 `recharge/hold/ok/refund`）。
- 卡密**只存 sha256**（同 token 的做法 `349,1210`），批量发卡由运营侧生成（`POST /ops/api/redeem/batch`，`_ops()` 守）。
- 防重放：`used_by_tenant` 非空即废；同一码并发必须**行级抢占**（见 §5.3 第 1 条，同一个洞）。
- 失败文案：`"卡密无效或已使用"` —— 不透露"存在但已用"与"不存在"的差别（避免枚举）。

### 5.2 充值申请单（兜底，人工确认）

```
recharge_requests(id, tenant_id, amount, note, state=pending|done|rejected, created_at, handled_by, handled_at)
POST /api/recharge-request {amount, note}   → 客户提交，屏上显示"待运营确认"
POST /ops/api/recharge-request/{id}/done     → 运营点确认 = 调 move(..., "recharge:request", "ops")
```
这条把今天「客户面上那句骗人的 hint」变成真的：`rhclient.py:644-645` 现在写着「先充值再提交（我的账 → 充值）」，而**客户面上根本没有充值视图**（`docs/client-ui/index.html:241,479` 只有两句"充值找运营"）。🔴 **要么建视图，要么改文案，不许继续挂着骗人的话**（文档不许写代码没实现的承诺）。

### 5.3 复用前必须修的四个既有洞（都是今天的事实，不是假设）

1. 🔴 **并发超扣**：`create_order` 是「先读 `balance`（`:640`）再无条件 `UPDATE credits SET balance=balance-?`（`:651-652`）」，服务是 `ThreadingHTTPServer`（`rhrpa.py:43,59`），`_db_lock` 只在 `db_init():196` 用过一次，全文件无 `BEGIN IMMEDIATE`/`WHERE balance>=?`/`RETURNING` ⇒ 同租户并发两单能双双过读检、双双扣。
   **修法**：扣减改成带条件的原子写 `UPDATE credits SET balance=balance-? WHERE tenant_id=? AND balance>=?` 并检查 `rowcount`；照 `gateway.py:4036-4037,4051-4052` 的 `WHERE hold_id=? AND status='pending'` 行级栅栏思路补。**卡密核销同一条路**（`WHERE used_by_tenant IS NULL`）。
2. 🔴 **那条回路至今 0 成功样本，而且卡点已经有名有姓**（10-30 17:5x 现读 `fleet.db`）：`tenants` 1 行；`jobs` 6 条**全是 `qwen-image-2.1`**、全 `failed`、每条 `hold=30 / refund=30`（⇒ **预扣与全额退款这套是真在工作**，不是纸面）；`ledger` 里只有 1 条 `recharge`、`actor='cli-agent'`（命令行加的，HTTP 充值口从未被真用过）；`Σactual=0`；**`motion-transfer` 从没走过一次对客口**。6 条死因各不相同，是一条一条往前推进时各撞一次墙：
   `10-29 09:07` 已出号但 23 分钟没终态（image 总预算 20 分钟）→ `10-29 10:38` 上游已落终态 FAILED → `10-29 15:16` 图片档至少要 1 张参考图 → `10-29 18:31` profile 没在跑（`browser.py launch --account 该号`）→ `10-30 00:00` 与 `10-30 16:44` **派单前置不过 · 档级实参对照不过：线上节点集 ≠ 本地基线，`only_online: [475,477,478,479]`**（475 正是 `image1` 那个槽）。
   ⇒ 两条结论：① **M0 拦在生图腿的"档漂移"上**，不在积分系统上；② 🔴 **这条链有另一个会话正在动**（`runners/rhrpa.py` mtime 10-30 15:23、`docs/api-rhrpa/` 10-30 16:42、最后一单 16:44），而 `rhclient.py` 停在 10-29 17:29 ⇒ 充值/卡密若由我落码，**只动 `rhclient.py` 并先比对 mtime**，别和它的在途改动撞车。
3. **自助注册今天是关死的**：`accounts/rhclient.json` 里既无 `signup_code` 也无 `signup_open` ⇒ `signup_state():122-138` 判 `open=False`，唯一开号路 = `POST /ops/api/tenant` 或 CLI。⇒ 本 PRD **不打开自助注册**（要限流才能开，`:255-257` 明写"今天没有这道闸"）；客户由我们建号发票。
4. **死凭据与僵尸配置要清**：`accounts/rhclient.json` 里 `ops_key` 已废但仍配着（len 32）且随 `dist/rhrpa-worker-kit.zip` 分发过（代码不再读它 = 死配置留在盘上）；`C:\frp\frpc-rhclient.toml` 的 `18130 → 127.0.0.1:8130` 是僵尸隧道（8130 已无监听，rhclient 已降级成 :8120 的模块）。

---

## 6. 价：1 积分 = 多少钱（这是报价类，必须业主给数）

盘上现状 —— **没有任何一处把 分↔¥ 写成可用的价**：

| 证据 | 内容 |
|---|---|
| `gateway.py:363-366,394,403,409` | 注释反复写「**1 元=几分 未定**」「占位·未实测，报价必须带"占位"两个字」 |
| `docs/PRD-client-credits-ui.md:75` | 「`1 积分=多少钱` 只在后台，沿用 v0.1 §3」—— 🔴 它指向的 v0.1 §3 **已不存在**（同文件 `:3` 说 v0.2 取代 v0.1）⇒ 这个"后台值"无处可查 |
| `ADR-model-gateway.md:738` | **全机唯一的 ¥ 数据点**：zexapi 一条 omni 视频段需预扣 **¥0.7225**，内部记 **40 分** ⇒ 隐含 **1 元 ≈ 55 分**。⚠️ 单次采样、只在 ADR 散文里、代码零常量 |
| `ADR-model-gateway.md:594,697` | 「绝对值别当钱（这台机器的 1 元=几分 仍未定），但相对比例是真的」 |
| `nzdh/runninghub_api_client.py:165-169` | RH openapi 腿「约 **2.5 元/次**（CNY 钱包）」 |
| 页面事实（我方产品对外已标的） | `weeksir.com/video` 的「开始生成」按钮写 **75 积分/条**（Seedance 2.0 档） |
| `rhclient.py:206-207,37` | 现行内部价：视频 **6 分/真实秒 ×档系数**（4-8s×1.0、9-12s×1.06、13-15s×1.22）、图片 **30 分/单**（🔴 `per_image` **不乘图片张数**，一单出一张成品图，`:268-273,306,635`） |

**成本口径也必须一并定**：`runninghub_client.query_task()` 的 `usage` 透传（`taskCostTime/consumeMoney/consumeCoins`）**已经打通**（`ADR-model-gateway.md:697`、`gateway.py:1661-1663`），但结算侧 `settle_credits():480-497` 目前只按 token 结，**没接 `consumeMoney`** ⇒ 现在"我们花了多少真钱"在账上是不存在的。号池成本（RH 币/CNY 钱包）与客户积分必须两张表对一次，否则不知道卖一份亏多少。

> 结论：**OQ2 收缩成一个数 —— 1 元 = 几分**。其余全部由公式导出（下面一屏算式，你填一个数我就把整套价目钉死）。

### 6.1 一屏算式（现行内部价：视频 6 分/真实秒×档系数、图片 30 分/单）

| 对象 | 算式 | 分数 | 若 1 元=55 分 |
|---|---|---|---|
| ② 一张场景图 | `per_image`，🔴 **不乘张数**（`rhclient.py:268-273,635`） | **30 分** | ¥0.55 |
| ③ 一条 6 秒片 | `ceil(6×6×1.0)` | 36 分 | ¥0.65 |
| ③ 一条 10 秒片 | `ceil(10×6×1.06)` | 64 分 | ¥1.16 |
| ③ 一条 15 秒片 | `ceil(15×6×1.22)` | **110 分** | ¥2.00 |
| **一行走完（图 + 15s 片）** | 30 + 110 | **140 分** | ¥2.55 |
| 一批 20 行 | 20 × 140 | 2800 分 | ¥50.9 |

**成本侧必须一起看，否则"55 分/元"是瞎定**：
- 走 **API 腿**：nzdh 自己的文案写着 RH openapi「约 **2.5 元/次**」（`runninghub_api_client.py:165-169`）⇒ 卖 ¥2.55/行 基本白干。
- 走 **坏节点 RPA 腿**：`rh_editor_run.py:8` 实测「任务 FAILED、**账单 coin=0**」而片已在 TOS 上 ⇒ 平台侧近乎不扣费，卖价几乎全利；🔴 **代价从"扣钱"换成"封号/风控"**——号池是我们那 4 条 lane，客户量一大就顶满并发并被平台看见。
- 号池真实消耗还没进账：`consumeMoney` 透传已通（`gateway.py:1661-1663`）但 `settle_credits():480-497` 没用它 ⇒ **"卖一单我们花多少"今天在账上不存在**（FR-8）。

⚠️ 一条会打架的既有事实：`weeksir.com/video` 页面对外**已标「75 积分/条」**（Seedance 2.0 档），而这里的"分"是 rhclient 的内部尺（15 秒片 = 110 分）。🔴 **同一个字不许有两套数** —— 定价前先定"对外那一套分用哪个尺"，否则客户在两个页面看到两种价。

---

## 7. 安全前置（这三条不修，本 PRD 的"客户从外面连我们"不成立）

1. 🔴 **公网那条隧道直达整套控制面**：`C:\frp\frpc-rhrpa.toml` 把公网 `43.139.42.21:18120` 裸 TCP 直转 `127.0.0.1:8120`，**没有 `allowPorts`、没有 IP 白名单**；18120 上任何 TCP 客户端都能打到「开管道、收手机号+密码的 `/lanes/{k}/login`（`rhrpa.py:6041-6049`）、`/workflows`、`/fleet`」。文档里给的两条收紧路（nginx `auth_basic` / 摘公网前缀）**都只是文档，本机没有 `C:\BtSoft`、盘上未落地**。
   ⇒ 硬前置：**对客口只经 `https://api.qn-einnp.com/rh-client/`（nginx 反代 + `auth_basic` 兜底），并摘掉 18120 的公网 remotePort**；控制面（`/lanes*`、`/workflows`、`/fleet`、`/ui`）永不对公网开。⚠️ 注意 `/rh-client/` 那条 nginx location **今天还没加**（`README.md:433` 自认），现在打它会掉进 `/` 的模型网关（实测 401 `no api key supplied`）。
2. 🔴 **交付包会把凭据送出去**：`build_exe.py:45-59` 的 `build_stage()` **根本没调 `strip_agnes_keys()`** ⇒ 今天打出的 exe 载荷里 `.env` 带着我们真实的 Agnes key + `MOTION_API_KEYS`，且 `config.py:13,18,20` 的**代码默认值本身就是明文 key**（三行同一个值，len 51；这把已于 2026-10-30 进过对话记录 ⇒ **先轮换再谈别的**，见 v3 FR-9）。zip 路线虽有 `strip_agnes_keys()`（`pack_full.py:152-184`），但①置空名单只有那三键 ⇒ **`MOTION_API_KEYS` 原样进包**；②剔除名单不含 `frpc/`（含真 token + 我们服务器 IP）和 `runninghub_profile/`（里面有 RH/抖音登录态）⇒ 下次重新打包会把它们一起送走。
   ⇒ 本 PRD 的解法是**结构性的**：§4.1 把 key 字段从 nzdh 代码里删掉，包里就无凭据可漏，不依赖打码这一步有人记得跑。
3. **票的存活**：`login()` 每次重新生成并覆盖 `token_sha`（`rhclient.py:1246-1247`）⇒ **多端互踢**。对"一台机器长期跑"的 exe 客户，这会让客户端某天突然 401。⇒ 客户端**只用激活时那把票、不重登**；换票走运营动作，并在 nzdh 侧把 401 明确报成「票已更换，找我们重发」而不是原始 HTTP 文本。

---

## 8. 功能需求

| # | 优先级 | 内容 | 落点 |
|---|---|---|---|
| FR-1 | **P0（前置）** | 公网收紧：加 `/rh-client/` 的 nginx location + `auth_basic`；摘 18120 公网映射；清 `ops_key` 与僵尸 18130 隧道 | `C:\frp\*.toml`、宝塔 nginx（**运维动作，改前要备份 + 改后打印 + 给回滚路**） |
| FR-2 | P0 | nzdh 侧凭据清零：删 `config.py:12-22` 九个客户可见字段 + 删引导 Step1/Step2 + 删 `/api/settings/agnes` 的写 key 分支；`build_exe` 与 `pack_full` 两条路线都验"包里 0 凭据" | `config.py`、`router.py:1021-1188`、`build_exe.py:45-59`、`pack_full.py:53-84,152-184` |
| FR-3 | P0 | 激活口：nzdh 新增「填票 → 拉 `/api/me` + `/api/workflows` + `/api/capacity` → 屏上只有能力与余额」；票存本地（加密或至少不写日志），401 有专属文案 | `womenswear/router.py`、前端 |
| FR-4 | P0 | 两条腿改打 `POST /api/jobs`（§4.3），素材先 `PUT /api/media` 拿 `media_id`；`estimate/balance` 落 v3 的 `gen_ledger` | 覆盖 v3 FR-2/FR-3/FR-4 |
| FR-5 | P0 | 🔴 **修并发超扣**：原子扣减 + 行级栅栏；配反向对照（同租户并发两单必须只有一单成） | `rhclient.py:640-659` |
| FR-6 | P0 | 卡密：`redeem_codes` 表 + `POST /api/redeem` + 运营批量发码口 + 客户面输入框 | `rhclient.py`（`move()` 复用、`LEDGER_REASONS` 扩） |
| FR-7 | P0 | 充值申请单：`recharge_requests` + `/api/recharge-request` + 运营确认口；或改 `644-645` 那句骗人 hint | 同上 + `docs/client-ui/index.html` |
| FR-8 | P1 | 价与成本：写入 `1 元=? 分` 的**唯一常量位**（进 `rate_history`/配置而不是散文），`settle_credits` 接 `consumeMoney`，出一张「卖 1 单赚/亏多少」的对账 | `gateway.py:480-497,1661-1663`、`rhclient.py:1253-1295` |
| FR-9 | P1 | 余额不足的客户体验：423 时屏上「还差 N 分」+ 卡密输入框（一次点击，不弹"找运营"） | 前端 + `client_err()` 白名单 |
| FR-10 | P1 | 覆盖闸：`Σ(recharge+redeem+refund) − Σ(actual) == balance` 每租户每天算一遍，对不上报警（前端已有加总自检 `index.html:649-676`，后端也要有一条红的） | `tools/check_*.py` |
| FR-11 | P2 | 能力可见性：`client_open` 是唯一销售闸（`rhclient.py:447-453`）， nzdh 不得硬编码任何 workflow 清单；v3 §9 的闸加一条「nzdh 出现的 key ⊆ `/api/workflows` 返回值」 | 闸 |

**一条红线**：本 PRD 落完后，nzdh 里不该再有第二处"能改模型/改 key"的地方（`/api/settings/agnes` 整个退役而不是留成兜底）。同一件事不留两个入口。

---

## 9. 对 v3 的覆盖（两份文档不许打架）

| v3 写的 | v4 改成 |
|---|---|
| §4.1 提交走**数据面** `POST /tasks`（不记租户积分） | 改走**对客面** `POST /api/jobs`（`media_ids`/`prompt`/`workflow`，带 hold/settle/refund） |
| §4.4「盘上没有提交前报价 ⇒ 执行表 nzdh 自己算」 | **仍然成立**，保留；但表里多一列 `estimate`（202 之后才有，且那一刻已预扣） |
| §4.2 轮询 `GET /tasks/{key}` + 三个坑 | 对客面换成 `GET /api/jobs/{id}` + `public_view()`（`:720-751`）；🔴 **坑仍要防**：`status` 与 `done` 的语义、`has_file` 现检盘上文件（`:730`）、`err` 已过白名单翻译（原文只在 `full=True`） |
| FR-3「删 nzdh 自己的 RH 执行栈」 | 保留，且**扩大**：连客户侧的 RH 账号/引导 Step1 一起删（§4.1） |
| §7 并发 = lane 数（4） | 不变；🔴 但**多租户之后并发是共享资源** ⇒ 需要每租户上限或排队可见（否则一个大客户吃满 4 条 lane，别人全在等）。列为 OQ4 |
| OQ1「账本走哪张脸（推荐数据面）」 | **本 PRD 就是它的答案：走对客面**。§3 那句立场要业主点头 |

---

## 10. 里程碑

| 阶段 | 内容 | 出门判据 |
|---|---|---|
| **M0** | 🔴 **先把那条回路跑通一次**（零新功能）：运营建号发票 → `/ops/api/credit` 或新卡密加分 → 一张真提交（生图腿）→ 结算落 `ledger` → 前端对账加总 = 余额 | 账上出现**第一条 `reason="ok"` 且有交付件**的流水；`jobs` 不再全 failed |
| M1 | FR-1 公网收紧 + FR-2 凭据清零（含那把 key 已轮换） | 包里逐件核对 0 凭据；公网打 18120 打不通 |
| M2 | FR-3 激活口 + FR-4 两条腿改对客面 + FR-5 原子扣减 | v3 的批量三步跑在积分上，余额逐行对得上 |
| M3 | FR-6/FR-7 卡密与申请单 + FR-9 余额不足体验 | 客户不问人能把积分充进来 |
| M4 | FR-8 价与成本对账 + FR-10/FR-11 两条闸 | 能回答「卖一单赚多少」 |

---

## 11. 开放问题（要业主点头，三个都是钱/账本）

1. ~~**OQ1｜账本归属**~~ ✅ **已定（10-30 你的原话「我要实现商业化，要客户登录，充值积分，要卖钱」）**：不等 weeksir API，现在就挂 rhclient 账本，接口按 weeksir 形状对齐，将来只换后端不换客户端。
2. **OQ2｜只要一个数：1 元 = 几分**（其余按 §6.1 公式导出）。参考锚点：唯一 ¥ 证据是一次采样「¥0.7225 = 内部 40 分」⇒ 隐含 1 元≈55 分；但那是别人家的 omni 段，不是我们的 RH 坏节点腿。**并且要先回答"对外那个'分'跟 weeksir.com 页面已标的 75 积分/条 是不是同一把尺"**（§6.1 末）。
2b. **OQ2b｜坏节点腿的成本口径**：接受"平台侧 coin=0 ⇒ 卖价≈纯利、风险是号池被封"这个形状吗？还是坚持接 `consumeMoney` 按真钱核算（FR-8）？我推荐**两条都做**：coin=0 记账、号池健康度另设一条红的判据。
3. **OQ3｜卡密谁发**：客户转账后由运营在中控台批量发码（自助注册保持关闭），还是开邀请码自助注册 + 发卡密？（今天自助注册是**关着的**，且**没有登录/注册限流闸**，开了就是给公网送撞库口。）
4. **OQ4｜多租户抢 lane**：一个客户批量 50 行时，要不要给他加每租户在途上限 / 或者按付费分优先级？（并发上限 = 4 条 lane，是共享资源。）
5. **OQ5｜抖音发布那条腿算不算"模型配置"**：`/api/publish/login` 让客户登自己的抖音（§1.1 同类问题）。本期不动，但它是"客户还要配东西"的下一个投诉点 —— 要不要排进 v5。

---

## 12. 风险与未验证（诚实清单）

1. 🔴 **那条回路 0 成功样本**（§5.3.2）。M0 不通过就别开 M2 —— 现在建充值 UI 是在一条没跑过的链子上加第二层。
2. **价目绝对值不可信**：`gateway` 的 PRICE_ROUTES 一堆「占位·未实测」，`ADR-model-gateway.md:594` 原话「绝对值别当钱」。⇒ 上线第一天必须写"内测价"，且 FR-8 的成本对账不能拖到 M4。
3. ⚠️ **别复用 `gateway.py` 的 `Ledger` 当客户账本**：它自称 `STUB`（`:520`）、**首次读余额就自动灌 10 万分**（`:4010 _DEFAULT_BALANCE=100000`）、`member` 列里是 `probe-*/session` 类标识符而不是客户。也**别把 8100 的 `model_admin.py` 挪出环回** —— 它自己写着「无鉴权 · 仅运维本机」，却能改价、能写 `.env` 密钥。
4. ⚠️ **`dist/rhrpa-worker-kit/` 是 `runners/` + `docs/api-rhrpa/` 的整份副本**（含 `rhclient.py`）⇒ 账本改动要改两处，否则发出去的是旧逻辑（同一件事两份真相）。
5. **`openapi.yaml` 的 `/api/jobs` schema 与代码冲突**（缺 `workflow`、多了会被 400 拒的 `priority`、`media_ids` 类型错），`spec-check.py` 不校验字段级一致性 —— 客户端照 spec 写一定踩。README:67 的 `rht_` 前缀、README:171 的 `settle` 流水名**代码里都没有**。
6. **未验的上游障碍**：号池 RH 币的实际消耗速率没量过；`qwen-image-2.1` 腿「就绪但真跑在成品前就崩」（v3 §12.1）还没解 —— 生图腿跑不通，本 PRD 的「按张卖积分」就没有可售标的。
7. ⚠️ 凭据卫生（只报名不改值）：`nzdh/config.py:13,18,20` 明文 key（**先轮换**）、`rh_accounts.json` 三组号明文密码、`WEEKSIR_USERNAME/PASSWORD`（9 位短密码，两处同值、明文在受保护文件里，`ADR-one-shot-inchat-form.md:168` 已列风险）。这三处都是"打包/接客之前必须处理"，但都不该出现在客户面上。
8. 🔴 **撞车风险（10-30 现读）**：对客这条链**另一个会话正在动** —— `runners/rhrpa.py` mtime 10-30 15:23、`docs/api-rhrpa/` 10-30 16:42、`fleet.db` 10-30 16:45（最后一单就是 16:44 那笔 failed+全额退）。而 `rhclient.py` 停在 10-29 17:29。⇒ 我落任何充值/卡密改动前**先比 mtime 和 `git diff --numstat`**，只动自己那几处、只用精确插入式 Edit，不 `git add -A`（同一仓并发，见 [[feedback-concurrent-writers]]）。
9. ⚠️ **"分"这个字现在有两套尺**（rhclient 内部 6 分/秒 vs weeksir.com 页面已对外标 75 积分/条）。上线前必须收成一把，否则客户对不上账、我们自己也核不了。
