"""女装带货 HTTP 路由。页面 + studio API + 上传 + 抖音发布。"""
from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

from config import BASE_DIR, settings
from womenswear.models import (
    PublishCopyRequest,
    PublishRequest,
    PublishSessionRequest,
    WomenswearStep5RpaRequest,
    WomenswearStudioRequest,
)
from womenswear.pipeline import (
    hydrate_task,
    output_url,
    run_womenswear_pipeline,
    set_step_rpa,
    task_logs,
)
from womenswear.state import motion_tasks, publish_tasks

router = APIRouter()
UPLOAD_DIR = Path(settings.UPLOAD_DIR)
OUTPUT_DIR = Path(settings.OUTPUT_DIR)
FRONTEND_DIR = Path(settings.FRONTEND_DIR)


def _page() -> FileResponse:
    html_path = FRONTEND_DIR / "womenswear_studio.html"
    if not html_path.exists():
        raise HTTPException(404, "女装带货工作台页面未找到")
    return FileResponse(
        path=str(html_path),
        media_type="text/html; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/")
async def home():
    return _page()


@router.get("/womenswear")
async def womenswear_page():
    return _page()


def _start_task(req: WomenswearStudioRequest) -> str:
    source_mode = (req.source_mode or "product").lower()
    # FR-3 旧值兼容映射：link→product，upload/beauty→creator
    source_mode = {"link": "product", "upload": "creator", "beauty": "creator"}.get(source_mode, source_mode)
    if source_mode not in ("product", "creator"):
        source_mode = "product"

    if source_mode == "product":
        if not req.product_url:
            raise HTTPException(status_code=400, detail="product 模式必须提供 product_url")
        if not req.video_url:
            raise HTTPException(status_code=400, detail="product 模式必须提供 video_url")
        # 模特图必填：Agnes 融合生成动作迁移参考图的人物真值
        if not req.model_image_path:
            raise HTTPException(status_code=400, detail="product 模式必须提供模特图")
        if not Path(req.model_image_path).exists():
            raise HTTPException(status_code=400, detail=f"模特图不存在: {req.model_image_path}")
    else:  # creator
        if not req.raw_product_path:
            raise HTTPException(status_code=400, detail="creator 模式必须提供 raw_product_path（模特图）")
        if not req.source_video_path:
            raise HTTPException(status_code=400, detail="creator 模式必须提供 source_video_path")
    # scene_image_path 可选（product/creator 统一）：有则融合人物场景；无则抽视频关键帧作背景
    if req.scene_image_path and not Path(req.scene_image_path).exists():
        raise HTTPException(status_code=400, detail=f"背景图不存在: {req.scene_image_path}")

    task_id = uuid.uuid4().hex[:12]
    motion_tasks[task_id] = {
        "status": "INIT",
        "progress": 0,
        "outputs": [],
        "error": None,
        "rh_task_id": None,
        "params": {
            "product_url": req.product_url,
            "video_url": req.video_url,
            "raw_product_path": req.raw_product_path,
            "source_video_path": req.source_video_path,
            "model_image_path": req.model_image_path,
            "scene_image_path": req.scene_image_path,
            "source_mode": source_mode,
            "engine": req.engine,
        },
        "stage": "启动中",
        "intermediate": {},
        "extra": {
            "current_step": "init",
            "step_list": ["step1", "step2", "step3", "step4", "step5"],
            "source_mode": source_mode,
            "engine": req.engine,
            # 会话主人：右栏「动作迁移」台的历史按它过滤（不照搬全局历史，谁点的谁看自己的）
            "session": (req.session or "").strip(),
        },
    }
    try:
        from backend.services import task_store
        task_store.create(
            task_id,
            "womenswear",
            status="INIT",
            progress=0,
            stage="启动中",
            params=motion_tasks[task_id]["params"],
            intermediate={},
            extra=motion_tasks[task_id]["extra"],
        )
    except Exception:
        pass
    return task_id


@router.post("/api/womenswear-studio/run")
async def womenswear_studio_run(req: WomenswearStudioRequest):
    # 花钱批准不在这一端问：8091 是人自己点的独立工作台（agent 轨的条子仍在 gateway.invoke() 里消费；判据见 C:\dsh\scripts\check-motion-rpa-lane.py F 组）
    task_id = _start_task(req)
    source_mode = motion_tasks[task_id]["params"]["source_mode"]

    async def _run():
        try:
            await run_womenswear_pipeline(
                task_id,
                model_image_path=req.model_image_path,
                source_mode=source_mode,
                raw_product_path=req.raw_product_path,
                source_video_path=req.source_video_path,
                engine=req.engine,
                scene_image_path=req.scene_image_path,
            )
        except Exception as e:
            if not motion_tasks[task_id].get("error"):
                motion_tasks[task_id].update({"status": "FAILED", "error": repr(e), "stage": "失败"})

    asyncio.create_task(_run())
    return {"success": True, "task_id": task_id}


@router.post("/api/womenswear-studio/step5-rpa")
async def womenswear_studio_step5_rpa(req: WomenswearStep5RpaRequest):
    from backend.services import task_store as _ts
    from backend.services.runninghub_rpa_client import run_motion_transfer_rpa

    tid = req.task_id
    if hydrate_task(tid) is None:
        raise HTTPException(404, f"任务不存在: {tid}")

    work_dir = OUTPUT_DIR / "womenswear_studio" / tid
    showcase_path = work_dir / "showcase.png"
    raw_product_path = work_dir / "raw_product.jpg"
    video_path = work_dir / "source_video.mp4"
    result_path = work_dir / "final.mp4"
    if not video_path.exists():
        raise HTTPException(400, f"Step4 产物缺失: {video_path}（请先完成 step1-4）")

    # RPA 输入 = Step3 融合参考图（FR-1 后所有模式都生成 showcase.png）
    # 旧任务（修复前的 upload 任务）没有 showcase，回退到旧模特图逻辑以便续跑
    params = motion_tasks[tid].get("params", {})
    extra_snap = motion_tasks[tid].get("extra", {})
    source_mode = (params.get("source_mode") or extra_snap.get("source_mode") or "product").lower()
    # FR-3 旧值兼容映射
    source_mode = {"link": "product", "upload": "creator", "beauty": "creator"}.get(source_mode, source_mode)
    rpa_image_path: Path | None = None
    rpa_image_label = ""
    if showcase_path.exists():
        rpa_image_path = showcase_path
        rpa_image_label = "动作迁移参考图(showcase)"
    elif source_mode == "creator":
        # 旧任务回退：从 step3 intermediate 恢复 model_path（pipeline 决策结果）
        inter = motion_tasks[tid].get("intermediate", {}) or {}
        step3 = inter.get("step3", {}) or {}
        model_path_str = (
            (step3.get("output") or {}).get("model_path")
            or (step3.get("input") or {}).get("model_path")
            or (step3.get("output") or {}).get("model")
            or (step3.get("input") or {}).get("model")
            or params.get("model_image_path")
            or ""
        )
        if model_path_str and Path(model_path_str).exists():
            rpa_image_path = Path(model_path_str)
            # 判定标签
            if str(raw_product_path.resolve()) == str(rpa_image_path.resolve()):
                rpa_image_label = "模特图(商品图复用)"
            else:
                rpa_image_label = "模特图(用户上传/默认模特)"
        elif raw_product_path.exists():
            # 兜底：商品图复用
            rpa_image_path = raw_product_path
            rpa_image_label = "模特图(商品图复用-兜底)"
        else:
            # 兜底：默认女模
            rpa_image_path = UPLOAD_DIR / "default_girl_model.jpg"
            rpa_image_label = "模特图(默认女模-兜底)"
    else:
        raise HTTPException(400, f"Step3 产物缺失: {showcase_path}（请先完成 step1-4）")

    motion_tasks[tid].update({
        "status": "RUNNING",
        "progress": 80,
        "stage": "Step5-RPA: 启动 Playwright + ComfyUI 工作流",
        "rh_task_id": None,
        "error": None,
    })
    extra = motion_tasks[tid].get("extra", {})
    extra.update({"current_step": "step5", "link_type": "rpa"})
    # 补登会话主人：只在没有主人的时候补（原有主人不被后来的重跑抢走归属）
    if str(req.session or "").strip() and not str(extra.get("session") or "").strip():
        extra["session"] = str(req.session).strip()
    motion_tasks[tid]["extra"] = extra
    try:
        _ts.update(
            tid,
            status="RUNNING",
            progress=80,
            stage="Step5-RPA: 启动 Playwright + ComfyUI 工作流",
            error="",
            extra={**extra, "current_step": "step5", "link_type": "rpa"},
        )
        _ts.add_log(tid, f"🚀 Step5 RPA 链路启动：用 Playwright 打开工作流页面（输入: {rpa_image_label}）")
    except Exception:
        pass

    async def _run_step5_rpa():
        try:
            _engine = (
                motion_tasks[tid].get("params", {}).get("engine")
                or motion_tasks[tid].get("extra", {}).get("engine")
                or "plus"
            )
            if _engine not in ("plus", "lite", "standard"):
                _engine = "plus"
            def _on_rpa_progress(stage: str, pct: int):
                set_step_rpa(tid, f"Step5-RPA: {stage}", pct, log=(pct % 2 == 0))

            await run_motion_transfer_rpa(
                image_path=str(rpa_image_path),
                video_path=str(video_path),
                output_path=str(result_path),
                timeout=req.timeout,
                workflow_url=req.workflow_url or None,
                engine=_engine,
                on_progress=_on_rpa_progress,
            )
            motion_tasks[tid].update({
                "status": "SUCCESS",
                "progress": 100,
                "outputs": [{"fileUrl": output_url(result_path), "fileType": "mp4", "nodeId": "rpa-285"}],
                "stage": "完成（RPA 链路）",
                "current_step": "done",
            })
            try:
                _ts.update(
                    tid,
                    status="SUCCESS",
                    progress=100,
                    stage="完成（RPA 链路）",
                    outputs=[{"fileUrl": output_url(result_path), "fileType": "mp4", "nodeId": "rpa-285"}],
                    extra={**motion_tasks[tid].get("extra", {}), "current_step": "done"},
                )
                inter = motion_tasks[tid].get("intermediate", {})
                if "step5" in inter:
                    inter["step5"]["output"] = {
                        **inter["step5"].get("output", {}),
                        "final_video": output_url(result_path),
                        "link_type": "rpa",
                    }
                    motion_tasks[tid]["intermediate"] = inter
                    _ts.update(tid, intermediate=inter)
                _ts.add_log(tid, "✅ Step5-RPA 完成：预览视频已下载到本地", level="done")
            except Exception:
                pass
        except Exception as e:
            motion_tasks[tid].update({
                "status": "FAILED",
                "error": f"Step5-RPA 失败: {repr(e)}",
                "stage": "Step5-RPA 失败",
            })
            motion_tasks[tid].setdefault("extra", {})["failed_step"] = "step5"
            try:
                _ts.update(
                    tid,
                    status="FAILED",
                    error=f"Step5-RPA 失败: {repr(e)}",
                    stage="Step5-RPA 失败",
                    extra={**motion_tasks[tid].get("extra", {}), "failed_step": "step5"},
                )
                _ts.add_log(tid, f"❌ Step5-RPA 失败: {e}", level="error")
            except Exception:
                pass

    asyncio.create_task(_run_step5_rpa())
    return {"success": True, "task_id": tid, "link_type": "rpa"}


@router.get("/api/womenswear-studio/videos")
async def womenswear_studio_videos(session: str = ""):
    from backend.services import task_store
    base = OUTPUT_DIR / "womenswear_studio"
    if not base.exists():
        return {"success": True, "videos": []}

    def _owned_by(name: str) -> bool:
        """目录名就是 task_id：带 session 查询时只认这条会话点过的片子（查不到主人 = 不算你的）。"""
        if not session:
            return True
        t = task_store.get(name) or {}
        return (t.get("extra", {}) or {}).get("session") == session

    videos = []
    for d in sorted(base.iterdir(), key=lambda p: p.stat().st_mtime if p.is_dir() else 0, reverse=True):
        if not d.is_dir() or not _owned_by(d.name):
            continue
        f = d / "final.mp4"
        if f.exists() and f.stat().st_size > 1024:
            st = f.stat()
            videos.append({
                "task_id": d.name,
                "url": f"/outputs/womenswear_studio/{d.name}/final.mp4",
                "size_mb": round(st.st_size / 1024 / 1024, 2),
                "created_at": int(st.st_mtime),
                "created_at_str": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)),
                "raw_product_url": f"/outputs/womenswear_studio/{d.name}/raw_product.jpg" if (d / "raw_product.jpg").exists() else "",
                "flat_lay_url": f"/outputs/womenswear_studio/{d.name}/flat_lay.png" if (d / "flat_lay.png").exists() else "",
                "showcase_url": f"/outputs/womenswear_studio/{d.name}/showcase.png" if (d / "showcase.png").exists() else "",
                "source_video_url": f"/outputs/womenswear_studio/{d.name}/source_video.mp4" if (d / "source_video.mp4").exists() and (d / "source_video.mp4").stat().st_size > 1024 else "",
            })
    for f in sorted(base.glob("_rh_recovered_*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True):
        if session:      # 捞回来的孤儿片没有会话主人 ⇒ 带 session 的查询里不列（宁可少列）
            break
        st = f.stat()
        rh_id = f.stem.replace("_rh_recovered_", "")
        videos.append({
            "task_id": f"rh_{rh_id}",
            "url": f"/outputs/womenswear_studio/{f.name}",
            "size_mb": round(st.st_size / 1024 / 1024, 2),
            "created_at": int(st.st_mtime),
            "created_at_str": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)),
            "rh_task_id": rh_id,
        })
    return {"success": True, "videos": videos}


_LIVE_PAGE = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>RPA 实时画面</title><style>
html,body{margin:0;height:100%;background:#15171b;color:#e8eaed;font:13px/1.5 system-ui,"Microsoft YaHei",sans-serif}
#bar{display:flex;gap:10px;align-items:center;padding:8px 12px;border-bottom:1px solid #2b2f36}
#bar b{font-weight:600}#bar span{color:#9aa0a6}
#wrap{position:relative;height:calc(100% - 41px);display:flex;align-items:center;justify-content:center;overflow:hidden}
img{max-width:100%;max-height:100%;object-fit:contain}
#idle{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;
gap:8px;text-align:center;padding:24px;color:#9aa0a6}
#idle a{color:#ff8a3c}
</style></head><body>
<div id="bar"><b>RPA 实时画面</b><span id="stage">读取中…</span><span id="age"></span></div>
<div id="wrap">
  <img id="shot" alt="" style="display:none">
  <div id="idle"><div id="why">现在没有动作迁移任务在跑。</div>
    <div>这一格是 8091 自己截的浏览器画面 —— runninghub.cn 的原页带 <code>X-Frame-Options: SAMEORIGIN</code>，
      在别人页面里嵌出来只会是空白；而要做成能嵌，就得开一个转发 RH 登录态的反向代理 ⇒ 那是把账号凭据
      经由本机端口递出去，比空白页严重得多，我们不那么干。</div>
    <div>要看真页：<a id="openrh" href="#" target="_blank" rel="noreferrer">在新窗口打开 RunningHub 工作流</a></div>
  </div>
</div>
<script>
const RH = '%RH_URL%';
document.getElementById('openrh').href = RH;
const shot=document.getElementById('shot'), idle=document.getElementById('idle'),
      stage=document.getElementById('stage'), ageEl=document.getElementById('age');
async function tick(){
  let m={fresh:false};
  try{ m=await (await fetch('/api/womenswear-studio/rpa-live/meta',{cache:'no-store'})).json(); }catch(e){}
  if(m.fresh){
    shot.src='/api/womenswear-studio/rpa-live.png?ts='+Math.floor(m.ts);
    shot.style.display=''; idle.style.display='none';
    stage.textContent=(m.stage||'进行中')+' · '+Math.max(0,Math.min(100,m.progress||0))+'%';
    ageEl.textContent = m.age <= 6 ? ('· ' + m.age + ' 秒前') : ('· 画面停在 ' + m.age + ' 秒前');
  }else{
    shot.style.display='none'; idle.style.display='flex';
    stage.textContent='（无画面）'; ageEl.textContent='';
  }
}
tick(); setInterval(tick, 3000);
</script></body></html>"""


@router.get("/api/womenswear-studio/rpa-live")
async def womenswear_studio_rpa_live():
    """右栏那一格：RPA 正在操作的页面长什么样（由 8091 自己截图，同域可嵌、不带任何凭据）。"""
    from fastapi.responses import HTMLResponse
    return HTMLResponse(_LIVE_PAGE.replace("%RH_URL%", settings.RUNNINGHUB_WORKFLOW_URL),
                        headers={"Cache-Control": "no-store"})


@router.get("/api/womenswear-studio/rpa-live/meta")
async def womenswear_studio_rpa_live_meta():
    from backend.services.runninghub_rpa_client import live_shot_state
    return live_shot_state()


@router.get("/api/womenswear-studio/rpa-live.png")
async def womenswear_studio_rpa_live_png():
    from backend.services.runninghub_rpa_client import LIVE_SHOT
    if not LIVE_SHOT.is_file():
        raise HTTPException(404, "还没有画面")
    return FileResponse(path=str(LIVE_SHOT), media_type="image/png",
                        headers={"Cache-Control": "no-store"})


@router.get("/api/womenswear-studio/history")
async def womenswear_studio_history(limit: int = 30, session: str = ""):
    from backend.services import task_store
    tasks = task_store.list_by_type("womenswear", limit=limit)
    # 右栏「动作迁移」台要的是"这条会话点过的"，不是全局流水 ⇒ 给了 session 就只回它自己的。
    # 没有主人的旧任务（在这次改造之前建的）在带 session 的查询里不出现——那是有意的：
    # 宁可少列，也不把别人几个月前的单子塞进你的历史。
    if session:
        tasks = [t for t in tasks if (t.get("extra", {}) or {}).get("session") == session]
    out = []
    for t in tasks:
        extra = t.get("extra", {})
        out.append({
            "task_id": t["task_id"],
            "status": t["status"],
            "progress": t.get("progress", 0),
            "stage": t.get("stage", ""),
            "error": t.get("error", ""),
            "current_step": extra.get("current_step", ""),
            "failed_step": extra.get("failed_step", ""),
            "session": extra.get("session", ""),
            "product_title": extra.get("product_title", ""),
            "raw_product_url": extra.get("raw_product_url", ""),
            "flat_lay_url": extra.get("flat_lay_url", ""),
            "showcase_url": extra.get("showcase_url", ""),
            "source_video_url": extra.get("source_video_url", ""),
            "rh_task_id": t.get("rh_task_id", ""),
            "final_video_url": (t.get("outputs") or [{}])[0].get("fileUrl", "") if t.get("outputs") else "",
            "intermediate": t.get("intermediate", {}),
            "created_at": t.get("created_at", 0),
            "created_at_str": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t.get("created_at", 0))) if t.get("created_at") else "",
        })
    return {"success": True, "tasks": out}


@router.get("/api/womenswear-studio/task/{task_id}")
async def womenswear_studio_task_detail(task_id: str):
    from backend.services import task_store
    t = task_store.get(task_id)
    if not t or t.get("task_type") != "womenswear":
        raise HTTPException(404, "任务不存在")
    extra = t.get("extra", {})
    inter = t.get("intermediate", {}) or {}
    s1 = (inter.get("step1", {}).get("output") or {})
    s2 = (inter.get("step2", {}).get("output") or {})
    s3 = (inter.get("step3", {}).get("output") or {})
    s4 = (inter.get("step4", {}).get("output") or {})
    return {
        "success": True,
        "task_id": task_id,
        "status": t["status"],
        "progress": t.get("progress", 0),
        "stage": t.get("stage", ""),
        "error": t.get("error", ""),
        "params": t.get("params", {}),
        "outputs": t.get("outputs", []),
        "intermediate": inter,
        "current_step": extra.get("current_step", ""),
        "failed_step": extra.get("failed_step", ""),
        "step_list": extra.get("step_list", ["step1", "step2", "step3", "step4", "step5"]),
        "product_title": extra.get("product_title", ""),
        "raw_product_url": extra.get("raw_product_url", "") or s1.get("raw_product", ""),
        "flat_lay_url": extra.get("flat_lay_url", "") or s2.get("flat_lay", ""),
        "showcase_url": extra.get("showcase_url", "") or s3.get("showcase", ""),
        "source_video_url": extra.get("source_video_url", "") or s4.get("source_video", ""),
        "step4_meta": {
            "title": s4.get("title", ""),
            "author": s4.get("author", ""),
            "quality": s4.get("quality", ""),
        },
        "rh_task_id": t.get("rh_task_id", "") or extra.get("rh_task_id", ""),
        "final_video_url": (t.get("outputs") or [{}])[0].get("fileUrl", "") if t.get("outputs") else "",
        "logs": task_logs(task_id),
        "created_at": t.get("created_at", 0),
        "created_at_str": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t.get("created_at", 0))) if t.get("created_at") else "",
    }


@router.post("/api/womenswear-studio/abort")
async def womenswear_studio_abort(task_id: str):
    if hydrate_task(task_id) is None:
        raise HTTPException(404, "任务不存在")
    motion_tasks[task_id]["abort_requested"] = True
    motion_tasks[task_id]["status"] = "FAILED"
    motion_tasks[task_id]["stage"] = "已中止（用户主动停止）"
    motion_tasks[task_id]["error"] = "任务被用户中止"
    motion_tasks[task_id].setdefault("extra", {})["failed_step"] = motion_tasks[task_id].get("current_step", "step5")
    try:
        from backend.services import task_store
        task_store.update(
            task_id,
            status="FAILED",
            stage="已中止（用户主动停止）",
            error="任务被用户中止",
            extra={**motion_tasks[task_id].get("extra", {}), "failed_step": motion_tasks[task_id].get("current_step", "step5")},
        )
        task_store.add_log(task_id, "⛔ 任务被用户中止", level="error")
    except Exception:
        pass
    return {"success": True, "task_id": task_id, "status": "ABORTED"}


@router.post("/api/womenswear-studio/retry")
async def womenswear_studio_retry(task_id: str):
    t = hydrate_task(task_id)
    if t is None:
        raise HTTPException(404, "任务不存在")
    if t.get("status") == "RUNNING":
        raise HTTPException(400, "任务运行中，无法重试")
    if not t.get("params"):
        raise HTTPException(400, "缺少原始参数，无法重试")

    failed_step = t.get("failed_step") or t.get("extra", {}).get("failed_step", "")
    t["status"] = "RUNNING"
    t["error"] = None
    t["abort_requested"] = False
    t.pop("failed_step", None)
    extra = t.get("extra", {})
    extra.pop("failed_step", None)
    extra["current_step"] = failed_step or "step1"
    t["extra"] = extra
    try:
        from backend.services import task_store
        task_store.update(
            task_id,
            status="RUNNING",
            error="",
            stage=f"重试 {failed_step or 'step1'}",
            extra={**extra, "current_step": failed_step or "step1"},
        )
        task_store.add_log(task_id, f"↻ 从 {failed_step or 'step1'} 重试")
    except Exception:
        pass

    async def retry_pipeline():
        try:
            await run_womenswear_pipeline(task_id)
        except Exception as e:
            motion_tasks[task_id].update({"status": "FAILED", "error": str(e), "stage": "失败"})

    asyncio.create_task(retry_pipeline())
    return {"success": True, "task_id": task_id, "retry_from": failed_step or "step1"}


@router.get("/api/womenswear-studio/status")
async def womenswear_studio_status(task_id: str):
    try:
        t = hydrate_task(task_id)
    except Exception as e:
        return {
            "success": False,
            "status": "ERROR",
            "progress": 0,
            "stage": "",
            "error": f"读取任务失败: {e}",
        }
    if t is None:
        return {
            "success": False,
            "status": "MISSING",
            "progress": 0,
            "stage": "任务不存在",
            "error": f"任务不存在: {task_id}",
        }
    try:
        extra = t.get("extra", {})
        inter = t.get("intermediate", {}) or {}
        s1 = (inter.get("step1", {}).get("output") or {})
        s2 = (inter.get("step2", {}).get("output") or {})
        s3 = (inter.get("step3", {}).get("output") or {})
        s4 = (inter.get("step4", {}).get("output") or {})
        return {
            "success": True,
            "status": t["status"],
            "progress": t.get("progress", 0),
            "stage": t.get("stage", ""),
            "outputs": t.get("outputs", []),
            "error": t.get("error"),
            "product_title": t.get("product_title", "") or extra.get("product_title", ""),
            "raw_product_url": t.get("raw_product_url", "") or extra.get("raw_product_url", "") or s1.get("raw_product", ""),
            "flat_lay_url": t.get("flat_lay_url", "") or extra.get("flat_lay_url", "") or s2.get("flat_lay", ""),
            "showcase_url": t.get("showcase_url", "") or extra.get("showcase_url", "") or s3.get("showcase", ""),
            "source_video_url": t.get("source_video_url", "") or extra.get("source_video_url", "") or s4.get("source_video", ""),
            "params": t.get("params", {}) or extra.get("params", {}),
            "step4_meta": {
                "title": s4.get("title", ""),
                "author": s4.get("author", ""),
                "quality": s4.get("quality", ""),
            },
            "rh_task_id": t.get("rh_task_id", "") or extra.get("rh_task_id", ""),
            "current_step": t.get("current_step", "") or extra.get("current_step", ""),
            "failed_step": t.get("failed_step", "") or extra.get("failed_step", ""),
            "step_list": extra.get("step_list", ["step1", "step2", "step3", "step4", "step5"]),
            "intermediate": inter,
            "logs": task_logs(task_id),
        }
    except Exception as e:
        return {
            "success": False,
            "status": "ERROR",
            "progress": t.get("progress", 0) if isinstance(t, dict) else 0,
            "stage": t.get("stage", "") if isinstance(t, dict) else "",
            "error": f"组装状态失败: {e}",
        }


@router.post("/api/upload/video")
async def upload_video(file: UploadFile = File(...)):
    if not file.filename:
        raise HTTPException(400, "文件名为空")
    ext = Path(file.filename).suffix or ".mp4"
    save_name = f"video_{uuid.uuid4().hex[:12]}{ext}"
    save_path = UPLOAD_DIR / save_name
    content = await file.read()
    save_path.write_bytes(content)
    size_mb = len(content) / 1024 / 1024
    if size_mb > 15:
        save_path.unlink(missing_ok=True)
        raise HTTPException(400, f"视频过大（{size_mb:.1f}MB），请压缩后重试")
    return {
        "success": True,
        "file_path": str(save_path),
        "path": f"/uploads/{save_name}",
        "filename": save_name,
        "size": len(content),
    }


@router.post("/api/upload/image")
async def upload_image(file: UploadFile = File(...)):
    if not file.filename:
        raise HTTPException(400, "文件名为空")
    ext = Path(file.filename).suffix or ".png"
    save_name = f"img_{uuid.uuid4().hex[:12]}{ext}"
    save_path = UPLOAD_DIR / save_name
    content = await file.read()
    save_path.write_bytes(content)
    return {
        "success": True,
        "path": f"/uploads/{save_name}",
        "file_path": str(save_path),
        "filename": save_name,
        "size": len(content),
    }


def _resolve_video_path(vp: str) -> Path:
    if vp.startswith("/outputs/"):
        return OUTPUT_DIR / vp[len("/outputs/"):]
    if vp.startswith("/uploads/"):
        return UPLOAD_DIR / vp[len("/uploads/"):]
    p = Path(vp)
    if p.is_absolute():
        return p
    return Path(settings.OUTPUT_DIR).parent / vp.lstrip("/")


@router.post("/api/publish/submit")
async def publish_submit(req: PublishRequest):
    vp = str(_resolve_video_path(req.video_path))
    if not Path(vp).exists():
        raise HTTPException(400, f"视频文件不存在: {vp}")
    task_id = uuid.uuid4().hex[:12]
    publish_tasks[task_id] = {"status": "INIT", "progress": 0, "stage": "启动中", "url": None, "error": None}

    async def run_publish():
        try:
            from backend.services.douyin_publisher import publish_video

            async def cb(msg):
                publish_tasks[task_id].update({"status": "RUNNING", "stage": msg})

            publish_tasks[task_id].update({"status": "RUNNING", "stage": "开始发布", "progress": 10})
            result = await publish_video(
                video_path=vp,
                title=req.title,
                tags=req.tags,
                status_callback=cb,
                headless=False,
            )
            if result.get("success"):
                publish_tasks[task_id].update({
                    "status": "SUCCESS",
                    "progress": 100,
                    "stage": "发布成功",
                    "url": result.get("url", ""),
                })
            else:
                publish_tasks[task_id].update({
                    "status": "FAILED",
                    "error": result.get("error", "发布失败"),
                    "stage": "失败",
                })
        except Exception as e:
            publish_tasks[task_id].update({"status": "FAILED", "error": str(e), "stage": "异常"})

    asyncio.create_task(run_publish())
    return {"success": True, "task_id": task_id}


@router.get("/api/publish/status")
async def publish_status(task_id: str):
    if task_id not in publish_tasks:
        raise HTTPException(404, "任务不存在")
    t = publish_tasks[task_id]
    return {
        "success": True,
        "status": t["status"],
        "progress": t.get("progress", 0),
        "stage": t.get("stage", ""),
        "url": t.get("url"),
        "error": t.get("error"),
        "nickname": t.get("nickname"),
    }


@router.post("/api/publish/login")
async def publish_login():
    from backend.services.douyin_publisher import ensure_login_for_publish
    task_id = uuid.uuid4().hex[:12]
    publish_tasks[task_id] = {
        "status": "RUNNING",
        "stage": "等待扫码登录创作者中心",
        "progress": 0,
        "url": None,
        "error": None,
        "nickname": None,
    }

    async def run_login():
        try:
            async def cb(msg):
                publish_tasks[task_id].update({"stage": msg})
            info = await ensure_login_for_publish(status_callback=cb, force_relogin=True)
            if info:
                nick = info.get("nickname", "") or ""
                publish_tasks[task_id].update({
                    "status": "SUCCESS",
                    "progress": 100,
                    "stage": f"创作者中心登录成功{('（' + nick + '）') if nick else ''}",
                    "nickname": nick,
                })
            else:
                publish_tasks[task_id].update({"status": "FAILED", "error": "登录失败", "stage": "失败"})
        except Exception as e:
            publish_tasks[task_id].update({"status": "FAILED", "error": str(e), "stage": "异常"})

    asyncio.create_task(run_login())
    return {"success": True, "task_id": task_id}


@router.get("/api/publish/check-login")
async def publish_check_login():
    from backend.services.douyin_publisher import DOUYIN_STORAGE_STATE_PATH
    import json
    if not DOUYIN_STORAGE_STATE_PATH.exists():
        return {"success": True, "logged_in": False, "nickname": None, "message": "未保存登录态"}
    try:
        with open(DOUYIN_STORAGE_STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        cookies = data.get("cookies", [])
        cookie_names = {c.get("name", "") for c in cookies}
        has_session = bool({"sessionid", "sessionid_ss", "sid_tt"} & cookie_names)
        if not has_session:
            try:
                DOUYIN_STORAGE_STATE_PATH.unlink()
            except Exception:
                pass
            try:
                from backend.services.douyin_publisher import DOUYIN_NICKNAME_PATH
                DOUYIN_NICKNAME_PATH.unlink()
            except Exception:
                pass
            return {"success": True, "logged_in": False, "nickname": None, "message": "登录态已失效，请重新扫码"}
        nickname = ""
        try:
            from backend.services.douyin_publisher import DOUYIN_NICKNAME_PATH
            if DOUYIN_NICKNAME_PATH.exists():
                nickname = DOUYIN_NICKNAME_PATH.read_text(encoding="utf-8").strip()
        except Exception:
            pass
        return {
            "success": True,
            "logged_in": True,
            "nickname": nickname,
            "message": "已保存登录态（实际有效性在发布时验证）",
        }
    except Exception as e:
        return {"success": False, "logged_in": False, "nickname": None, "message": str(e)}


# ───── RunningHub 账号池管理 ─────
@router.get("/api/rh-accounts/list")
async def rh_accounts_list():
    """列出所有账号及其状态"""
    try:
        from backend.services.rh_account_pool import list_all_status
        return {"success": True, "accounts": list_all_status()}
    except Exception as e:
        return {"success": False, "error": str(e), "accounts": []}


@router.post("/api/rh-accounts/init-login")
async def rh_accounts_init_login():
    """手动触发所有 pending 账号的首次登录"""
    try:
        from backend.services.rh_account_pool import _accounts, _states, get_profile_dir, mark_ready, STATUS_PENDING
        from backend.services.rh_auto_login import auto_login

        async def _do_login():
            results = []
            for acc in _accounts:
                st = _states.get(acc.phone)
                if not st or st.status == STATUS_PENDING:
                    try:
                        ok = await auto_login(
                            acc.phone, acc.password, get_profile_dir(acc.phone),
                            headless=False,  # 手动触发用非 headless 便于观察
                        )
                        mark_ready(acc.phone, login_ok=ok)
                        results.append({"phone": acc.phone, "ok": ok})
                    except Exception as e:
                        mark_ready(acc.phone, login_ok=False)
                        results.append({"phone": acc.phone, "ok": False, "error": str(e)})
            return results

        results = await _do_login()
        return {"success": True, "results": results}
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.post("/api/rh-accounts/reset")
async def rh_accounts_reset(phone: str = ""):
    """重置账号状态（解除 in_use / limited 标记，重置为 ready）
    phone=空 → 重置所有账号
    """
    try:
        from backend.services.rh_account_pool import _states, _save_states, STATUS_READY
        target_phones = [phone] if phone else list(_states.keys())
        reset_count = 0
        for p in target_phones:
            st = _states.get(p)
            if st and (st.in_use or st.status != STATUS_READY):
                st.in_use = False
                st.status = STATUS_READY
                st.last_error = ""
                reset_count += 1
        _save_states()
        return {"success": True, "reset_count": reset_count}
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.post("/api/rh-accounts/add")
async def rh_accounts_add(req: dict):
    """添加账号后执行：校验账号密码 → 上传动作迁移工作流 → 更新状态。
    已存在的手机号会更新密码并重新跑初始化。
    Body: {phone, password, note?, enabled?, profile_dir?}
    """
    try:
        from backend.services.rh_account_pool import (
            upsert_account, setup_account, list_all_status,
        )

        phone = (req.get("phone") or "").strip()
        password = (req.get("password") or "").strip()
        note = (req.get("note") or "").strip()
        enabled = bool(req.get("enabled", True))
        profile_dir = (req.get("profile_dir") or "").strip()

        saved = upsert_account(
            phone, password, note=note, enabled=enabled, profile_dir=profile_dir,
        )
        if not saved.get("ok"):
            return {"success": False, "error": saved.get("error") or "写入账号失败"}

        setup = await setup_account(phone)
        prefix = "账号已添加。" if saved.get("created") else "账号已在库中，已重新初始化。"
        return {
            "success": True,
            "phone": phone,
            "created": bool(saved.get("created")),
            "setup_ok": bool(setup.get("success")),
            "failed_step": setup.get("failed_step") or "",
            "message": prefix + (setup.get("message") or ""),
            "steps": setup.get("steps") or {},
            "runtime_wf_id": setup.get("runtime_wf_id") or "",
            "accounts": list_all_status(),
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.post("/api/rh-accounts/setup")
async def rh_accounts_setup(phone: str = ""):
    """对已有账号重跑：校验密码 → 上传动作迁移工作流 → 更新状态。"""
    try:
        from backend.services.rh_account_pool import setup_account, list_all_status
        phone = phone.strip()
        if not phone:
            return {"success": False, "error": "phone 必填"}
        setup = await setup_account(phone)
        return {
            "success": bool(setup.get("success")),
            "setup_ok": bool(setup.get("success")),
            "phone": phone,
            "failed_step": setup.get("failed_step") or "",
            "message": setup.get("message") or "",
            "steps": setup.get("steps") or {},
            "runtime_wf_id": setup.get("runtime_wf_id") or "",
            "accounts": list_all_status(),
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.post("/api/rh-accounts/delete")
async def rh_accounts_delete(phone: str = ""):
    """从账号库删除账号（同时移除 state）
    phone 必填
    """
    try:
        from backend.services.rh_account_pool import (
            load_accounts, get_account_by_phone, _states, _save_states, ACCOUNTS_FILE,
        )
        import json as _json

        phone = phone.strip()
        if not phone:
            return {"success": False, "error": "phone 必填"}
        if not get_account_by_phone(phone):
            return {"success": False, "error": f"账号不存在: {phone}"}

        # 从 rh_accounts.json 移除
        with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
            data = _json.load(f)
        data["accounts"] = [a for a in data.get("accounts", []) if a.get("phone") != phone]
        with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
            _json.dump(data, f, ensure_ascii=False, indent=2)

        # 从 state 移除
        if phone in _states:
            del _states[phone]
        _save_states()

        # 重新加载
        load_accounts()

        return {"success": True, "phone": phone, "message": "账号已删除"}
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.post("/api/rh-accounts/upload-workflow")
async def rh_accounts_upload_workflow(phone: str = ""):
    """把公共坏节点工作流 copy 到指定账号。已有且归属正确则跳过。"""
    try:
        from backend.services.rh_account_pool import upload_workflow_to_account, list_all_status
        phone = phone.strip()
        if not phone:
            return {"success": False, "error": "phone 必填"}
        result = upload_workflow_to_account(phone)
        return {"success": bool(result.get("success")), "result": result, "accounts": list_all_status()}
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.post("/api/rh-accounts/refresh-status")
async def rh_accounts_refresh_status(phone: str = ""):
    """实时校准账号状态（通过 RunningHub SDK）
    - from_login + get_user_api_key + accountStatus
    - 私有副本归属用 portal/workflow/detail 的 owner 校验（不用 get_workflow_json）
    - phone=空 → 校准所有账号
    - phone=xxx → 只校准指定账号
    """
    try:
        from backend.services.rh_account_pool import (
            refresh_account_runtime_status, refresh_all_runtime_status,
            _accounts, list_all_status,
        )
        if phone:
            result = refresh_account_runtime_status(phone.strip())
            return {"success": True, "result": result, "accounts": list_all_status()}
        else:
            results = refresh_all_runtime_status()
            return {"success": True, "results": results, "accounts": list_all_status()}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ───── 首次启动引导：注册/登录 RunningHub（Cookie 记录）→ 注入动作迁移工作流 → Agnes Key ─────
# Cookie 登录的本机账号占位名（无密码，靠浏览器会话登录；动作迁移工作流注入到它名下）
ONBOARD_PHONE = "self"
_onboard_login = {"running": False, "ok": False, "error": ""}
_onboard_task = None  # 防止登录任务被 GC


@router.get("/api/onboard/status")
async def onboard_status():
    """引导弹窗出现标准：RunningHub 登录 Cookie 还在不在（没了就弹）。"""
    from backend.services.rh_auto_login import cookies_alive, INVITE_URL
    alive = cookies_alive()
    return {
        "cookie_alive": alive,
        "need_guide": not alive,
        "invite_url": INVITE_URL,
        "login_running": _onboard_login["running"],
        "login_ok": _onboard_login["ok"],
        "login_error": _onboard_login["error"],
        "agnes_key_set": bool(settings.IMAGE2_API_KEY),
    }


@router.post("/api/onboard/rh/login")
async def onboard_rh_login():
    """打开浏览器到邀请注册页，客户注册/登录后自动记录 Cookie（下一步按钮随之点亮）。"""
    global _onboard_task
    if _onboard_login["running"]:
        return {"success": True, "started": False, "message": "登录窗口已打开，请在弹出的浏览器里完成注册/登录"}
    _onboard_login.update(running=True, ok=False, error="")

    async def _run():
        try:
            from backend.services.rh_auto_login import cookie_login, INVITE_URL
            from backend.services.rh_account_pool import DEFAULT_PROFILE_ROOT
            profile = DEFAULT_PROFILE_ROOT / f"rh_{ONBOARD_PHONE}"
            res = await cookie_login(ONBOARD_PHONE, str(profile), start_url=INVITE_URL, timeout_seconds=600)
            _onboard_login["ok"] = bool(res.get("ok"))
            _onboard_login["error"] = "" if res.get("ok") else (res.get("error") or "登录失败")
        except Exception as e:
            _onboard_login["ok"] = False
            _onboard_login["error"] = str(e)[:200]
        finally:
            _onboard_login["running"] = False

    _onboard_task = asyncio.create_task(_run())
    return {
        "success": True,
        "started": True,
        "message": "已打开浏览器，请在弹出的窗口里完成注册/登录（登录成功会自动记录 Cookie）",
    }


@router.post("/api/onboard/rh/inject")
async def onboard_rh_inject():
    """点「下一步」时把公共动作迁移工作流注入（copy）到引导注册的账号名下。"""
    from backend.services.rh_auto_login import cookies_alive
    from backend.services.rh_account_pool import (
        upsert_account, upload_workflow_to_account, refresh_account_runtime_status, mark_ready,
    )
    if not cookies_alive():
        raise HTTPException(400, "未检测到 RunningHub 登录 Cookie，请先完成注册/登录")
    up = upsert_account(ONBOARD_PHONE, "", note="引导注册账号（Cookie 登录）", cookie_only=True)
    if not up.get("ok"):
        raise HTTPException(500, up.get("error") or "账号创建失败")
    upl = upload_workflow_to_account(ONBOARD_PHONE)
    if not upl.get("success"):
        raise HTTPException(500, upl.get("error") or "动作迁移工作流注入失败")
    try:
        refresh_account_runtime_status(ONBOARD_PHONE)
    except Exception as e:
        print(f"[Onboard] 账号状态刷新失败（不影响使用）: {e}")
    mark_ready(ONBOARD_PHONE)
    return {
        "success": True,
        "runtime_wf_id": upl.get("runtime_wf_id") or "",
        "skipped": bool(upl.get("skipped")),
        "message": "动作迁移工作流已存在，跳过注入" if upl.get("skipped") else "动作迁移工作流已注入",
    }


# ───── 设置：Agnes API 配置 ─────
def _mask_key(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 12:
        return "*" * len(key)
    return key[:6] + "..." + key[-4:]


def _update_env_file(updates: dict) -> None:
    """按键更新 .env（保留其他行与注释），不存在的键追加到末尾。"""
    env_path = BASE_DIR / ".env"
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    seen = set()
    out = []
    for line in lines:
        stripped = line.strip()
        if "=" in stripped and not stripped.startswith("#"):
            k = stripped.split("=", 1)[0].strip()
            if k in updates:
                out.append(f"{k}={updates[k]}")
                seen.add(k)
                continue
        out.append(line)
    for k, v in updates.items():
        if k not in seen:
            out.append(f"{k}={v}")
    env_path.write_text("\n".join(out) + "\n", encoding="utf-8")


@router.get("/api/settings/agnes")
async def settings_agnes_get():
    """读取 Agnes API 配置（key 脱敏返回）"""
    return {
        "success": True,
        "base_url": settings.IMAGE2_BASE_URL,
        "api_key_set": bool(settings.IMAGE2_API_KEY),
        "api_key_masked": _mask_key(settings.IMAGE2_API_KEY),
        "image_model": settings.IMAGE2_MODEL,
        "vision_model": settings.MIMO_MODEL,
    }


@router.post("/api/settings/agnes")
async def settings_agnes_save(req: dict):
    """保存 Agnes API 配置：写入 .env 并热更新内存配置（免重启生效）。
    api_key 留空表示不修改现有 key。"""
    base_url = str(req.get("base_url") or "").strip().rstrip("/")
    api_key = str(req.get("api_key") or "").strip()
    image_model = str(req.get("image_model") or "").strip()
    vision_model = str(req.get("vision_model") or "").strip()

    if not base_url.startswith(("http://", "https://")):
        raise HTTPException(400, "API 地址必须以 http:// 或 https:// 开头")
    if not image_model or not vision_model:
        raise HTTPException(400, "模型名不能为空")

    updates = {
        "IMAGE2_BASE_URL": base_url,
        "MIMO_BASE_URL": base_url,
        "OPENAI_BASE_URL": base_url,
        "IMAGE2_MODEL": image_model,
        "MIMO_MODEL": vision_model,
        "OPENAI_MODEL": vision_model,
    }
    if api_key:
        updates.update({
            "IMAGE2_API_KEY": api_key,
            "MIMO_API_KEY": api_key,
            "OPENAI_API_KEY": api_key,
        })
    try:
        _update_env_file(updates)
    except Exception as e:
        raise HTTPException(500, f"写入 .env 失败: {e}")

    for k, v in updates.items():
        setattr(settings, k, v)
    # 生图单例客户端同步刷新（其余调用点每次运行时读 settings，无需处理）
    try:
        from backend.services import image2_client as _i2c
        _i2c.image2_client.base_url = base_url
        _i2c.image2_client.default_model = image_model
        if api_key:
            _i2c.image2_client.api_key = api_key
    except Exception:
        pass

    return {
        "success": True,
        "message": "Agnes 配置已保存并即时生效" if api_key else "Agnes 配置已保存（key 保持不变）",
        "api_key_set": bool(settings.IMAGE2_API_KEY),
        "api_key_masked": _mask_key(settings.IMAGE2_API_KEY),
    }


@router.post("/api/settings/agnes/test")
async def settings_agnes_test(req: dict):
    """测通 Agnes API：用表单里的配置（key 留空则用现有 key）分别测试视觉模型与生图模型。"""
    import time
    import httpx

    base_url = str(req.get("base_url") or "").strip().rstrip("/")
    api_key = str(req.get("api_key") or "").strip() or settings.IMAGE2_API_KEY
    image_model = str(req.get("image_model") or "").strip()
    vision_model = str(req.get("vision_model") or "").strip()

    if not base_url.startswith(("http://", "https://")):
        raise HTTPException(400, "API 地址必须以 http:// 或 https:// 开头")
    if not image_model or not vision_model:
        raise HTTPException(400, "模型名不能为空")
    if not api_key:
        raise HTTPException(400, "API Key 未配置，请先填写 Key")

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    result = {"success": True, "chat": {}, "image": {}}

    # ① 视觉/文本模型：chat/completions
    t0 = time.time()
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{base_url}/chat/completions",
                headers=headers,
                json={"model": vision_model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1},
            )
            ok = resp.status_code == 200
            result["chat"] = {
                "ok": ok,
                "ms": int((time.time() - t0) * 1000),
                "detail": "" if ok else (resp.text or "")[:200],
            }
    except Exception as e:
        result["chat"] = {"ok": False, "ms": int((time.time() - t0) * 1000), "detail": str(e)[:200]}
    if not result["chat"].get("ok"):
        result["success"] = False

    # ② 生图模型：images/generations（1K 小图）
    t0 = time.time()
    try:
        async with httpx.AsyncClient(timeout=180.0) as client:
            resp = await client.post(
                f"{base_url}/images/generations",
                headers=headers,
                json={
                    "model": image_model,
                    "prompt": "a red apple on white background, product photo",
                    "size": "1K",
                    "ratio": "1:1",
                    "extra_body": {"response_format": "url"},
                },
            )
            ok = resp.status_code == 200
            detail = "" if ok else (resp.text or "")[:200]
            if ok:
                try:
                    data = (resp.json() or {}).get("data") or []
                    if not data:
                        ok = False
                        detail = "返回缺少 data（未包含图片结果）"
                except Exception:
                    ok = False
                    detail = "响应不是合法 JSON"
            result["image"] = {"ok": ok, "ms": int((time.time() - t0) * 1000), "detail": detail}
    except Exception as e:
        result["image"] = {"ok": False, "ms": int((time.time() - t0) * 1000), "detail": str(e)[:200]}
    if not result["image"].get("ok"):
        result["success"] = False

    return result


@router.post("/api/publish/session/start")
async def publish_session_start(req: PublishSessionRequest):
    from backend.services.douyin_publisher import PublishSession, publish_sessions
    vp = str(_resolve_video_path(req.video_path))
    if not Path(vp).exists():
        raise HTTPException(400, f"视频文件不存在: {vp}")
    session_id = uuid.uuid4().hex[:12]
    session = PublishSession(video_path=vp, title=req.title, tags=req.tags, headless=False)
    publish_sessions[session_id] = session
    try:
        result = await session.start()
        return {"session_id": session_id, **result}
    except Exception as e:
        publish_sessions.pop(session_id, None)
        return {"session_id": session_id, "success": False, "error": str(e)}


@router.post("/api/publish/session/{session_id}/next")
async def publish_session_next(session_id: str):
    from backend.services.douyin_publisher import publish_sessions
    session = publish_sessions.get(session_id)
    if not session:
        raise HTTPException(404, "会话不存在或已结束")
    try:
        result = await session.run_next()
        if result.get("finished"):
            publish_sessions.pop(session_id, None)
        return {"session_id": session_id, **result}
    except Exception as e:
        return {"session_id": session_id, "success": False, "error": str(e), "logs": list(session._logs)}


@router.post("/api/publish/session/{session_id}/abort")
async def publish_session_abort(session_id: str):
    from backend.services.douyin_publisher import publish_sessions
    session = publish_sessions.pop(session_id, None)
    if not session:
        raise HTTPException(404, "会话不存在或已结束")
    try:
        await session.abort()
        return {"session_id": session_id, "success": True, "aborted": True, "logs": list(session._logs)}
    except Exception as e:
        return {"session_id": session_id, "success": False, "error": str(e)}


@router.get("/api/publish/session/{session_id}/status")
async def publish_session_status(session_id: str):
    from backend.services.douyin_publisher import publish_sessions
    session = publish_sessions.get(session_id)
    if not session:
        raise HTTPException(404, "会话不存在或已结束")
    return {
        "session_id": session_id,
        "success": True,
        "step": session.current_step,
        "finished": session._finished,
        "aborted": session._aborted,
        "next_step": session.STEPS[session._step_index] if session._step_index < len(session.STEPS) and not session._finished else None,
        "logs": list(session._logs),
        "screenshot": session._last_screenshot,
        "dom_info": session._last_dom_info,
    }


@router.post("/api/publish/generate-copy")
async def publish_generate_copy(req: PublishCopyRequest):
    raw_path = (req.video_path or "").strip()
    product_info = (req.product_info or "").strip() or None
    if not raw_path:
        return {"success": False, "detail": "缺少 video_path"}
    p = _resolve_video_path(raw_path)
    if not p.exists():
        return {"success": False, "detail": f"视频不存在: {p}"}
    from backend.services.copy_generator import generate_publish_copy
    try:
        result = await generate_publish_copy(str(p), product_info=product_info)
        return {
            "success": True,
            "clothing_type": result.get("clothing_type", ""),
            "title": result.get("title", ""),
            "tags": result.get("tags", []),
        }
    except Exception as e:
        return {"success": False, "detail": f"文案生成失败: {e}"}
