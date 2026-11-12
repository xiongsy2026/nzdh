# 女装带货 · 一键生成 PRD

> **版本**：v1.0  
> **创建日期**：2026-09-10  
> **状态**：需求冻结（以参考实现行为为准，待在本仓库落地）  
> **本仓库目标**：在 `D:\nzdh` 复刻并落地女装带货工作流  
> **参考实现**：`D:\video-skill` · 页面 [http://127.0.0.1:8090/womenswear](http://127.0.0.1:8090/womenswear)  
> **参考前端**：`D:\video-skill\frontend\womenswear_studio.html`  
> **参考管线**：`D:\video-skill\app.py` 中 `_womenswear_run` + `/api/womenswear-studio/*`

---

## 0. 文档用途与权威来源

本 PRD 是 **nzdh 项目女装带货工作流的产品与实现契约**。行为以 `video-skill` **当前代码**为准，不以旧文档为准。

以下参考文档已经过时，**禁止照抄**：

| 文档 | 过时点 |
|------|--------|
| `D:\video-skill\PRD.md` 第 1 节 | 写成了旧的 parse/import/RH OpenAPI 链路，不是现行 5 步工作室 |
| `D:\video-skill\女装带货视频工作流.md` | Step5 写成 RH OpenAPI 轮询；质量门槛写成 1MB/7s；产物文件名与现网不一致 |

现行事实（必须写入本仓库实现）：

- 页面入口是 `/womenswear`，实际文件是 `womenswear_studio.html`
- 主链路是 **5 步一体化流水线**，不是旧的 `/api/womenswear/parse` 手工拼图
- Step5 **固定走 RunningHub RPA**（Playwright + ComfyUI iframe），不走 OpenAPI
- 完整视频必须从 **节点 285** 抓取，禁止读节点 502 的 1 秒预览

---

## 1. 产品定位

**一句话**：运营把抖音女装商品和一条要模仿的抖音视频丢进来，系统自动做出「指定模特穿着该服装、按模仿视频做动作」的竖屏带货成片，并可分步发布到抖音创作者中心。

**核心价值**：

1. **链接即可跑**：商品短链 + 模仿视频短链 → 成片，不要求运营会做图
2. **上传也可跑**：本地商品图 + 本地模仿视频，适合无法解析链接的场景
3. **服装一致性**：白底平铺图锁定服装真值，再融合成动作迁移参考图，减少乱改款
4. **动作迁移**：用 RunningHub 带坏节点工作流，把参考图的人/服装迁移到模仿视频动作上
5. **可续跑、可发布**：失败从断点重试；成片后自动拟标题标签，分步发布到抖音

**非目标（本期不做）**：

- 不做爆款 1:1 复刻（那是 replica 管线）
- 不做多 shot 分镜审核
- 不做换人换产品超级复刻
- 不把 Step5 改回 RunningHub OpenAPI（该工作流 API 会 FAILED，拿不到成片）

---

## 2. 用户是谁、要完成什么

| 角色 | 诉求 |
|------|------|
| 女装带货运营 | 每天用商城链接 + 爆款舞蹈/短剧视频，批量出竖屏带货片 |
| 剪辑/投放同学 | 已有商品图和参考视频，不想走链接解析，直接上传生成 |
| 发布同学 | 成片后扫码登录抖音，人工确认每一步再点发布，避免误发 |

成功标准：同一商品、同一模仿视频，在链接模式舞蹈背景下，能稳定产出 ≥ 模仿视频时长的 `final.mp4`，并支持分步发布。

---

## 3. 信息架构与页面

**入口**：`GET /womenswear`  
**布局**：左输入 / 右产出 + 发布，对标参考页。

```
┌─────────────────────────────────────────────────────────────┐
│ 女装带货 · 一键生成                          WOMENSWEAR STUDIO │
├──────────────┬──────────────────────────────────────────────┤
│ 链接 | 上传   │ 状态徽章 / 进度条 / 当前阶段 / 日志            │
│              │ Step1~5 进度条（灰/蓝/绿/红）                  │
│ 商品 / 视频   │ 输入信息（商品链接、模仿视频）                  │
│ 模特 / 背景   │ 预览格：原图 / 平铺 / [关键帧] / 橱窗 / 模仿视频 │
│ Plus | Lite  │ ┌─────────────┐ ┌──────────────────────────┐ │
│ 开始生成      │ │ 最终视频     │ │ 抖音发布工作台            │ │
│ 中止 / 重试   │ │ 播放+下载    │ │ 扫码登录 / 标题 / 标签    │ │
│ 历史任务列表  │ └─────────────┘ │ 分步发布（走一步停一步）  │ │
└──────────────┴──────────────────────────────────────────────┘
```

页面加载必须：

1. 拉历史任务列表（`/api/womenswear-studio/history`，空则回退 `/videos`）
2. 检查抖音登录态（`/api/publish/check-login`）
3. 若 URL 带 `?task_id=`，立即拉状态并在未结束时开始轮询

---

## 4. 输入、模式与校验

### 4.1 来源模式 `source_mode`

| 模式 | 运营操作 | 必填 | 跳过的步骤 |
|------|----------|------|------------|
| `link`（默认） | 贴商品链接 + 模仿视频链接 + 上传模特图 | `product_url`、`video_url`、`model_image_path` | 无（完整 5 步） |
| `upload` | 上传商品图 + 模仿视频，模特图可选 | `raw_product_path`、`source_video_path` | Step2 平铺图、Step3 参考图生成；Step1/4 改为复制本地文件 |

链接支持用户粘贴整段抖音分享文案，正则抽取：

```
https?://[^\s，。`'\"<>]+
```

去掉尾部反引号和引号。

### 4.2 背景模式 `mode`（仅 link 生效）

| 值 | 名称 | 背景来源 | 预览格 |
|----|------|----------|--------|
| `dance`（默认） | 舞蹈模式 | 系统固定背景 `uploads/default_showcase_bg.jpg` | 4 格 |
| `drama` | 短剧模式 | 模仿视频中间关键帧 `keyframe_bg.jpg` | 5 格（多一格「视频关键帧」） |

upload 模式 **强制 `dance`**，忽略用户选的短剧。

### 4.3 Step5 引擎 `engine`

| 值 | 前端文案 | RunningHub 按钮 |
|----|----------|-----------------|
| `plus`（默认） | Plus · 质量优先 | 中间按钮 `beveled-btn-left`，文本「运行」 |
| `lite` | Lite · 速度优先 | 左侧按钮，文本含 Lite/Standard 或 class `beveled-btn-right` |

两者当前都走同一套 RPA + 同一套 plus 工作流，差别只在点哪个运行按钮。  
`engine` 不得被覆盖成 `"rpa"`；链路类型另用 `extra.link_type = "rpa"`。

### 4.4 模特图规则

| 场景 | 模特图来源 |
|------|------------|
| link 模式 | 用户上传，必填；不再使用系统内置默认女模 |
| upload 且用户上传了模特图 | 用用户图 |
| upload 且未上传模特图 | MiMo 判断商品图是否含真人：含模特 → 商品图本身；仅服装 → GPT 从模仿视频抽 5 帧挑动作最清晰的一帧 |

### 4.5 默认素材（必须随仓库提供）

| 文件 | 用途 |
|------|------|
| `uploads/default_girl_model.jpg` | 历史素材，链接模式不再自动使用 |
| `uploads/default_showcase_bg.jpg` | 舞蹈模式背景范式 |

缺失时舞蹈模式不得静默空跑，应报明确错误。

---

## 5. 5 步固定管线

任务 ID：12 位 hex。  
工作目录：`outputs/womenswear_studio/{task_id}/`。

断点原则：**扫描产物文件是否存在且合格，存在则跳过该步**。`final.mp4` 已存在则整任务直接 SUCCESS。

| Step | 进度 | 产物 | link | upload |
|------|------|------|------|--------|
| 1 商品图 | 15% | `raw_product.jpg` | 解析商品链接，下载第一张图 | 复制上传图 |
| 2 白底平铺 | 40% | `flat_lay.png` | Image2 3:4 白底平铺 | **跳过** |
| 3 动作参考图 | 65% | `showcase.png`（drama 另有 `keyframe_bg.jpg`） | Image2 9:16 融合 | **跳过生成**；可能先做 LLM 模特决策 |
| 4 模仿视频 | 75% | `source_video.mp4` | 下载并质量校验；drama 可能已在 Step3 下过 | 复制上传视频并校验 |
| 5 动作迁移 | 100% | `final.mp4` | RPA：参考图 + 视频 | RPA：模特图/商品图 + 视频 |

失败时必须写入：

- `status = FAILED`
- `extra.failed_step = "stepN"`
- `error`、`stage`、日志

### 5.1 Step1 解析商品链接

服务：`backend.services.douyin_mall_clothing.resolve_douyin_mall_clothing`

顺序：

1. 抽取 URL
2. httpx 跟随跳转，优先解析最终 URL 里的 `goods_detail`（C 端短链主路径）
3. 否则从 HTML 抽 title + 图片候选
4. 仍无图则 Playwright 打开页面扫 `<img>`
5. 取 `clothing_image_urls[0]` 下载为 `raw_product.jpg`

中间产物：

```json
{
  "input": { "product_url": "...", "image_urls": ["..."] },
  "output": { "raw_product": "/outputs/womenswear_studio/{id}/raw_product.jpg", "title": "", "author": "" }
}
```

### 5.2 Step2 白底平铺图（仅 link）

- 模型：Image2 `image_edit_sync`
- 尺寸：`768x1024`（3:4），png
- 输入：仅 `raw_product.jpg`
- 提示词要点：纯白底、无人、服装按穿着形态平铺、保留版型颜色面料图案细节、禁止改款换色加字、配饰可并列不遮挡主体

### 5.3 Step3 动作迁移参考图（仅 link）

- 模型：Image2，尺寸 `1024x1536`（约 9:16），png
- 三图顺序（不可乱）：**① 服装真值（平铺图）② 模特真值 ③ 背景**

**舞蹈模式**：背景 = 固定 `default_showcase_bg.jpg`，不碰视频。

**短剧模式**（全部算在 Step3 进度里，不要污染 Step4 状态文案）：

1. 下载模仿视频（与 Step4 同一套下载 + 质量校验）
2. 抽中间关键帧 → `keyframe_bg.jpg`
3. 用「场景人物替换」提示词融合：保留场景、擦字幕、换模特、换衣服、锁原姿态（含手持物品）

upload 模式不生成 `showcase.png`。未上传模特图时，在 Step3 前插入：

1. MiMo-v2.5 判断商品图 `has_model`
2. true → `model_path = raw_product.jpg`
3. false → 抽 5 帧，GPT（`settings.OPENAI_MODEL`，现为 gpt-5.6-sol）选最适合做模特的一帧；GPT 失败则退回中间帧

### 5.4 Step4 模仿视频

下载器：`backend.services.douyin_downloader.download_douyin_video`  
支持整段分享文案；短链 `v.douyin.com` / `iesdouyin`。

**质量校验（现行代码，必须用这套，不要用旧文档的 1MB/7s）**：

| 条件 | 不合格处理 |
|------|------------|
| 文件不存在或 < 1KB | 失败 |
| 大小 < 400KB | 视为预览流，删除重下；仍不合格则 FAILED |
| 时长 < 5s（moviepy） | 失败 |
| moviepy 解析失败但文件够大 | **视为合格**（避免误杀） |

link + drama：视频应已在 Step3 就绪，Step4 只确认；损坏则补下。  
link + dance：本步才下载。

### 5.5 Step5 动作迁移（RPA，硬约束）

**为什么必须 RPA**：工作流含节点 600 FailedTrigger，任务最终状态是 FAILED，这是设计，用来绕过 RunningHub 服务端时长/质量校验。OpenAPI 遇到 FAILED 会丢结果；RPA 从 ComfyUI 画布读完整视频 URL。

工作流：

```
https://www.runninghub.cn/workflow/2087348783733628930?source=workspace
```

浏览器：Playwright persistent context，profile 目录 `runninghub_profile`，需事先登录（`rh_auto_login3.py` 一类脚本）。未登录必须明确报错，禁止假跑。

节点契约：

| 节点 | 作用 | 本管线怎么用 |
|------|------|----------------|
| 299 | 参考图 | 上传 image |
| 275 | 模仿视频 | 上传 video |
| **285** | `VHS_VideoCombine`，`save_output=True`，prefix `h264 yuv420p` | **唯一允许下载的完整视频** |
| 502 | PreviewOnly，`save_output=False` | **禁止下载**（1 秒 / 30 帧） |
| 600 | FailedTrigger | 285 出片后故意把任务标 FAILED，属预期 |

RPA 输入图：

| 模式 | `image_path` |
|------|----------------|
| link | `showcase.png` |
| upload | `model_path`（商品图复用或 GPT 挑帧） |

超时：默认 1800s（30 分钟）。  
成片路径：`final.mp4`。  
用户中止：`abort_requested` 为真则本步抛错退出。

**验收红线**：`final.mp4` 时长必须接近 `source_video.mp4`，不得是 1.0s / 30 帧。出现 1 秒片一律视为读了 502，算实现缺陷。

---

## 6. 任务状态、持久化与续跑

### 6.1 状态机

```
INIT → RUNNING → SUCCESS
               ↘ FAILED  →（retry）RUNNING
用户 abort → FAILED（failed_step = 当前步）
```

内存字典 `motion_tasks` + SQLite `task_store`（`task_type = "womenswear"`）双写。  
查询/重试时内存丢失必须从 `task_store` 恢复。

### 6.2 关键字段

| 字段 | 含义 |
|------|------|
| `task_id` | 12 位 hex |
| `status` | INIT / RUNNING / SUCCESS / FAILED |
| `progress` | 0–100 |
| `stage` | 给人看的当前步骤文案 |
| `params` | 全部入参（url、路径、mode、engine、source_mode） |
| `intermediate` | 每步 `{input, output, ts}` |
| `extra.current_step` | step1…step5 / init / done |
| `extra.failed_step` | 失败步 |
| `extra.engine` | plus / lite |
| `extra.link_type` | rpa |
| `outputs` | `[{fileUrl, fileType: "mp4"}]` |
| `logs` | 带时间与 level |

前端轮询：`GET /api/womenswear-studio/status?task_id=`，间隔约 2.5s。  
`?task_id=` 深链必须能恢复监控。

### 6.3 中止 / 重试 / 导入

| 能力 | 行为 |
|------|------|
| abort | 置 `abort_requested`，标 FAILED，记录 `failed_step` |
| retry | 禁止 RUNNING 时重试；清失败态和 abort 标志；再跑 `_womenswear_run`，靠产物跳过已完成步 |
| import | 把旧任务目录产物拷到新 `task_id` 再续跑（兼容 `douyin_*.mp4` → `source_video.mp4`） |

服务重启：未完成的 INIT/RUNNING 应能被识别为可重试，不得假装还在跑却没有 worker。

---

## 7. 抖音发布工作台（成片后）

与生成管线解耦，但做在同一页右侧。

### 7.1 登录

- 扫码：`POST /api/publish/login` → 打开 `creator.douyin.com`，轮询 `/api/publish/status`
- 检查：`GET /api/publish/check-login`（只看 cookie 文件，不启浏览器）
- 判定登录：cookie 含 `sessionid` / `sessionid_ss` / `sid_tt` 之一
- 持久化：`uploads/douyin_storage_state.json` + `uploads/douyin_nickname.txt`

### 7.2 自动文案

成片或点历史视频后调用 `POST /api/publish/generate-copy`：

1. ffmpeg 抽视频中点帧
2. MiMo-v2.5 识别服装
3. 返回：`clothing_type`、≤20 字标题、5 个标签（无 #）

禁止再用已欠费的 GPT-5.5 做文案。

### 7.3 分步发布（本期主路径）

前端按钮：「启动分步发布（走一步停一步）」。  
每步结束暂停，等用户点「继续下一步」。

| 顺序 | key | 动作 |
|------|-----|------|
| 1 | init | 启动有头浏览器，打开发布页 |
| 2 | upload | 注入视频，等上传完成 |
| 3 | fill_title | 拟人化填写标题 |
| 4 | fill_tags | 填标签（最多 5） |
| 5 | pre_publish | 截屏 + 识别 DOM |
| 6 | locate_publish | 定位发布按钮 |
| 7 | publish | 点击发布并检测成功 |

发布成功检测必须三重，缺一不可：

1. 监听抖音域 publish/upload/commit 响应（`status_code==0` 或 `success` 或返回 `aweme_id`）
2. URL 跳到 `/content/manage`、`/creator-micro/content/manage`、`/creator/home` 任一
3. Semi / Ant / Element 成功 toast，或文案「发布成功 / 视频已发布 / 提交成功」

检测最多约 90s。单次发布间隔建议 ≥30s，防风控。

---

## 8. API 契约（本仓库必须对齐）

### 8.1 工作室主链路

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/womenswear` | 页面 |
| POST | `/api/womenswear-studio/run` | 启动。body 见下 |
| GET | `/api/womenswear-studio/status?task_id=` | 轮询 |
| POST | `/api/womenswear-studio/retry?task_id=` | 断点重试 |
| POST | `/api/womenswear-studio/abort?task_id=` | 中止 |
| GET | `/api/womenswear-studio/history?limit=30` | 历史任务 |
| GET | `/api/womenswear-studio/task/{task_id}` | 详情（intermediate + logs + 各产物 URL） |
| GET | `/api/womenswear-studio/videos` | 扫目录列出 final.mp4（history 为空时的回退） |
| POST | `/api/womenswear-studio/step5-rpa` | 仅重跑 Step5（需已有 showcase + source_video） |

`POST /run` body：

```json
{
  "source_mode": "link | upload",
  "product_url": "",
  "video_url": "",
  "raw_product_path": "",
  "source_video_path": "",
  "model_image_path": "",
  "mode": "dance | drama",
  "background_image_path": "",
  "engine": "plus | lite"
}
```

校验：

- link：缺 `product_url`、`video_url` 或 `model_image_path` → 400
- upload：缺商品图或模仿视频路径 → 400
- dance 且未传背景 → 填默认背景图

### 8.2 上传与发布

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/upload/image` | 返回 `file_path`（绝对路径给管线）+ `path`（`/uploads/...` 给预览） |
| POST | `/api/upload/video` | 同上 |
| POST | `/api/publish/login` | 扫码登录 |
| GET | `/api/publish/check-login` | 登录态 |
| GET | `/api/publish/status?task_id=` | 登录/一键发布进度 |
| POST | `/api/publish/generate-copy` | 文案 |
| POST | `/api/publish/session/start` | 分步发布开会话 |
| POST | `/api/publish/session/{id}/next` | 下一步 |
| POST | `/api/publish/session/{id}/abort` | 放弃 |
| GET | `/api/publish/session/{id}/status` | 查询会话 |

### 8.3 旧接口（兼容即可，不是主路径）

`/api/womenswear/parse`、`import-image`、`flat-lay`、`generate`、`status/{task_id}` 是拆步调试遗留。本期主产品不依赖它们；若实现，不得替代 studio 5 步。

---

## 9. 产物目录

```
outputs/womenswear_studio/{task_id}/
├── raw_product.jpg      # Step1
├── flat_lay.png         # Step2（upload 无此文件）
├── keyframe_bg.jpg      # Step3 drama
├── showcase.png         # Step3 link
├── llm_frames/          # upload 挑模特帧
├── source_video.mp4     # Step4
└── final.mp4            # Step5
```

静态访问：`/outputs/womenswear_studio/{task_id}/...`  
历史列表按目录 mtime 倒序。可附带孤立文件 `_rh_recovered_*.mp4`。

---

## 10. 前端行为清单

必须具备：

1. 链接 / 上传 Tab；切换时改流程说明文案
2. 舞蹈 / 短剧卡片（仅链接模式有意义）
3. Plus / Lite
4. 链接模式模特图为用户上传（必填），禁止内置默认图
5. 上传模式：商品图必填、模仿视频必填、模特图可选；LLM 会识别商品图是否含模特
6. 开始生成、中止、从失败步重试
7. 2.5s 轮询；stage 变化写入日志
8. 5 段 steps-bar：未开始灰、进行中蓝、完成绿、失败红
9. 预览：舞蹈 4 格 / 短剧 5 格；模仿视频角标显示 `✓ {时长} / {大小}` 和作者标题
10. 历史点击回填：入参、预览、日志、失败重试按钮、成片播放
11. 成片后填发布路径并自动生成文案
12. 分步发布面板：步骤列表、截图/DOM、日志、继续/放弃

上传接口必须同时给绝对路径和 URL；只返回 Windows 绝对路径会导致预览 404。

---

## 11. 外部依赖

| 依赖 | 用途 |
|------|------|
| Image2（gpt-image-2） | 平铺图、参考图 |
| MiMo-v2.5 | 商品图是否含模特；发布文案 |
| OpenAI 兼容 GPT（现 gpt-5.6-sol） | upload 模式从视频挑模特帧 |
| Playwright | 商品页兜底、视频下载兜底、RH RPA、抖音登录/发布 |
| RunningHub 已登录 profile | Step5 |
| ffmpeg / ffprobe | 抽帧、文案中点帧 |
| moviepy | 视频时长校验 |
| yt-dlp | 视频下载备选 |
| SQLite task_store | 任务持久化 |

---

## 12. 已知坑（实现时必须避开）

1. **1 秒成片**：读了节点 502 PreviewOnly。只允许节点 285。旧 debug 记录 `debug-womenswear-rpa-one-second.md` 已确认此根因；参考代码头注释与 `wait_for_preview_video(285)` 已按此修复。
2. **节点 600 FAILED 是成功路径**：RPA 必须在 FAILED 后继续从 285 拉片，不能当失败返回。
3. **engine 被写成 rpa**：`extra.link_type` 与 `engine` 必须分开。
4. **质量门槛文档不一致**：以代码为准（400KB / 5s），不要用旧 md 的 1MB / 7s。
5. **短剧模式进度串台**：Step3 下载视频时，`step_key` 必须仍是 step3，不能写成 step4。
6. **GPT 文案欠费**：发布文案走 MiMo，不走 GPT-5.5。
7. **抖音发布成功检测**：必须监听网络响应，不能只靠过时 CSS。
8. **upload 不生成 showcase**：Step5 用模特图/商品图，不要空等 `showcase.png`。
9. **RPA 需有头浏览器 + 已登录 profile**：无登录直接失败并提示先登录 RunningHub。
10. **商品图是服装真值**：平铺与融合提示词禁止改款、换色、加 Logo/文字。

---

## 13. 验收标准

### P0 链接模式 · 舞蹈

1. 输入有效商品短链 + 模仿视频短链，点开始生成
2. 依次出现：原图、白底平铺、橱窗参考图、模仿视频、成片
3. `final.mp4` 可播放，时长与 `source_video.mp4` 同量级（允许编码误差，**禁止 1 秒片**）
4. 刷新页面后历史仍在，点击能回看中间产物和成片
5. 失败后点「从 StepN 重试」不重跑已成功步

### P0 链接模式 · 短剧

1. 预览为 5 格，含视频关键帧
2. 参考图背景来自模仿视频，不是默认橱窗背景
3. 关键帧/参考图中不得残留原视频字幕

### P0 上传模式

1. 只传平铺服装图 + 视频：系统从视频挑模特帧再跑 Step5
2. 只传含模特的商品图 + 视频：直接用商品图做 RPA 图，跳过平铺和橱窗生成
3. 上传不合格短视频（<5s 或 <400KB）必须在 Step4 失败，不得进入 Step5

### P0 Step5

1. Plus / Lite 会点到不同 RunningHub 运行按钮
2. 成片来自节点 285
3. 中止后任务 FAILED，可重试

### P1 发布

1. 扫码登录后刷新仍显示已登录+昵称
2. 成片自动出标题和 5 个标签
3. 分步发布每步可暂停；点发布后能识别成功（含 Semi toast / 新管理页路径 / 发布 API）

### P2 体验

1. `?task_id=` 可恢复
2. 日志按阶段追加，不刷屏重复
3. 上传预览用 `/uploads/...`，不用本地盘符路径

---

## 14. nzdh 落地范围

本期在本仓库实现与参考页 **功能对等** 的女装带货工作室，建议模块切分：

```
nzdh/
├── PRD_womenswear.md          # 本文档
├── app.py                     # 路由：页面 + studio API + publish API
├── frontend/womenswear_studio.html
├── backend/services/
│   ├── douyin_mall_clothing.py
│   ├── douyin_downloader.py
│   ├── douyin_publisher.py
│   ├── copy_generator.py
│   ├── image2_client.py
│   ├── runninghub_rpa_client.py
│   └── task_store.py
├── uploads/
│   ├── default_girl_model.jpg
│   └── default_showcase_bg.jpg
└── outputs/womenswear_studio/
```

实现原则：

1. 先打通 **link + dance + plus** 闭环，再补 drama / upload / lite / 发布
2. Step5 节点号、失败即出片、禁止 502，写成代码注释和测试断言
3. 提示词与参考实现保持一致，改提示词必须先改本 PRD
4. 不要把 replica / 超级复刻 / 一句话生视频卷进本工作流

---

## 15. 决策记录

| 决策 | 选择 | 原因 |
|------|------|------|
| Step5 链路 | 只走 RPA | 带坏节点工作流 API 会 FAILED |
| 完整视频节点 | 285 | 502 是 1 秒预览 |
| 链接模式模特 | 用户上传，必填 | 运营自选模特，不再使用系统内置女模 |
| 舞蹈 vs 短剧 | 背景来源不同，融合提示词同一套泛化替换 | 舞蹈要固定橱窗感，短剧要跟原视频场景和姿态 |
| upload 跳过 Step2/3 | 用户已有成图，直接迁动作 | 避免二次生图改掉服装 |
| 发布主路径 | 分步会话，不是一键黑盒 | 运营要看清每步，降低误发 |
| 文案模型 | MiMo-v2.5 | GPT-5.5 额度问题已在参考实现踩过 |

---

## 16. 变更记录

| 日期 | 说明 |
|------|------|
| 2026-09-10 | 初稿。按 `video-skill` `/womenswear` 现行代码梳理，作为 nzdh 落地契约 |
| 2026-09-10 | 独立封装落地：`womenswear/` 包 + `backend/services/` 仅保留本工作流依赖，入口 `python app.py` |
| 2026-09-25 | 链接模式模特图改为用户上传必填，取消系统内置默认女模 |
