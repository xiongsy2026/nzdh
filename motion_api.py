"""动作迁移对外 API：给别人调用的纯 HTTP 接口，全程无浏览器。

给外部系统用最简三件套：
  POST /api/motion/generate     提交生成（multipart: image + video，或 JSON 带 URL）
  GET  /api/motion/task/{id}    查任务状态 / 结果 URL
  GET  /api/motion/task/{id}/video  下载成片

鉴权：X-API-Key 头，Key 配在 config.MOTION_API_KEYS（逗号分隔）。
🔒 这条轨会花真钱（跑 RunningHub 任务）：不带 Key 一律 401；本服务默认仍只绑 127.0.0.1，
   要给外网用请自行套反代 + HTTPS，并按需限流。
"""
from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

from fastapi import APIRouter, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse

from config import settings
from backend.services import task_store

router = APIRouter(prefix="/api/motion", tags=["motion-api"])

TASK_TYPE = "motion_api"
_OUTPUT_DIR = Path(settings.OUTPUT_DIR) / "motion_api"

# 上传素材限额（防止有人拿接口当网盘）
_MAX_IMAGE_MB = 30
_MAX_VIDEO_MB = 200
_IMG_EXT = {".png", ".jpg", ".jpeg", ".webp"}
_VID_EXT = {".mp4", ".mov", ".webm", ".m4v"}


def _check_key(x_api_key: str) -> None:
    keys = [k.strip() for k in (settings.MOTION_API_KEYS or "").split(",") if k.strip()]
    if not keys:
        raise HTTPException(503, "服务端未配置 MOTION_API_KEYS，API 未开放")
    if x_api_key not in keys:
        raise HTTPException(401, "无效的 X-API-Key")


def _task_view(t: dict) -> dict:
    outputs = t.get("outputs") or []
    return {
        "task_id": t.get("task_id"),
        "status": t.get("status"),
        "progress": t.get("progress"),
        "stage": t.get("stage"),
        "error": t.get("error") or "",
        "outputs": outputs,
        "video_url": (outputs[0].get("fileUrl") if outputs else ""),
        "created_at": t.get("created_at"),
        "updated_at": t.get("updated_at"),
    }


def _new_task_id() -> str:
    """task_id = [worker前缀-]随机段。池模式下网关按前缀把后续请求路由回本机。"""
    raw = uuid.uuid4().hex[:12]
    wid = (settings.MOTION_WORKER_ID or "").strip()
    return f"{wid}-{raw}" if wid else raw


@router.post("/generate")
async def motion_generate(
    image: UploadFile = File(..., description="参考图（showcase/模特图）"),
    video: UploadFile = File(..., description="模仿视频"),
    engine: str = Form("plus"),
    timeout: int = Form(3600),
    fix_bad_node: bool = Form(True),
    link: str = Form("auto", description="出片链路：auto=API优先失败降级RPA / api=纯HTTP / rpa=本机RPA"),
    x_api_key: str = Header(default=""),
):
    """提交一次动作迁移生成任务。返回 task_id，用 /task/{id} 轮询。"""
    _check_key(x_api_key)
    if link not in ("auto", "api", "rpa"):
        raise HTTPException(400, f"link 不合法: {link}（可用 auto/api/rpa）")
    task_id = _new_task_id()
    work_dir = _OUTPUT_DIR / task_id
    work_dir.mkdir(parents=True, exist_ok=True)

    img_name = Path(image.filename or "image.png").name
    vid_name = Path(video.filename or "video.mp4").name
    img_ext, vid_ext = Path(img_name).suffix.lower(), Path(vid_name).suffix.lower()
    if img_ext not in _IMG_EXT:
        raise HTTPException(400, f"参考图格式不支持: {img_ext}（可用 {sorted(_IMG_EXT)}）")
    if vid_ext not in _VID_EXT:
        raise HTTPException(400, f"模仿视频格式不支持: {vid_ext}（可用 {sorted(_VID_EXT)}）")

    img_path = work_dir / f"source_image{img_ext}"
    vid_path = work_dir / f"source_video{vid_ext}"
    result_path = work_dir / "final.mp4"
    await _save_upload(image, img_path, _MAX_IMAGE_MB)
    await _save_upload(video, vid_path, _MAX_VIDEO_MB)

    task_store.create(
        task_id,
        TASK_TYPE,
        status="QUEUED",
        progress=0,
        stage="排队中",
        params={"engine": engine, "timeout": timeout, "fix_bad_node": fix_bad_node,
                "link": link, "image": img_name, "video": vid_name},
        work_dir=str(work_dir),
    )

    asyncio.create_task(_run_generation(task_id, img_path, vid_path, result_path,
                                        engine, timeout, fix_bad_node, link))
    return {"task_id": task_id, "poll_url": f"/api/motion/task/{task_id}", "link": link}


@router.get("/task/{task_id}")
async def motion_task_status(task_id: str, x_api_key: str = Header(default="")):
    """查询任务状态与结果。"""
    _check_key(x_api_key)
    t = task_store.get(task_id)
    if not t or t.get("task_type") != TASK_TYPE:
        raise HTTPException(404, f"任务不存在: {task_id}")
    return _task_view(t)


@router.get("/task/{task_id}/video")
async def motion_task_video(task_id: str, x_api_key: str = Header(default="")):
    """下载成片。"""
    _check_key(x_api_key)
    t = task_store.get(task_id)
    if not t or t.get("task_type") != TASK_TYPE:
        raise HTTPException(404, f"任务不存在: {task_id}")
    video_path = Path(t.get("work_dir") or "") / "final.mp4"
    if t.get("status") != "SUCCESS" or not video_path.is_file():
        raise HTTPException(409, f"成片未就绪（status={t.get('status')}）")
    return FileResponse(str(video_path), media_type="video/mp4", filename=f"{task_id}.mp4")


@router.get("/tasks")
async def motion_task_list(limit: int = 20, x_api_key: str = Header(default="")):
    """最近任务列表。"""
    _check_key(x_api_key)
    return [_task_view(t) for t in task_store.list_by_type(TASK_TYPE, limit=min(limit, 100))]


@router.get("/health")
async def motion_health(x_api_key: str = Header(default="")):
    """本 worker 健康度：网关选机 + 运维看池都用这个。不触发任何花钱操作。"""
    _check_key(x_api_key)
    from backend.services import rh_account_pool as pool

    if not pool._accounts:
        pool.init_pool()
    accounts = []
    for acc in pool._accounts:
        st = pool.get_state(acc.phone)
        accounts.append({
            "phone": acc.phone,
            "enabled": acc.enabled,
            "status": st.status,
            "in_use": bool(st.in_use),
            "balance": getattr(st, "runtime_balance", "") or "",
        })
    running = sum(1 for t in task_store.list_by_type(TASK_TYPE, limit=50)
                  if t.get("status") in ("QUEUED", "RUNNING"))
    return {
        "worker_id": (settings.MOTION_WORKER_ID or "").strip(),
        "ok": True,
        "running_tasks": running,
        "accounts": accounts,
        "accounts_available": sum(1 for a in accounts if a["enabled"] and a["status"] == "ready" and not a["in_use"]),
    }


# ───────────────────────────── 内部 ─────────────────────────────


async def _save_upload(upload: UploadFile, dest: Path, max_mb: int) -> None:
    limit = max_mb * 1024 * 1024
    size = 0
    with dest.open("wb") as f:
        while chunk := await upload.read(1024 * 1024):
            size += len(chunk)
            if size > limit:
                f.close()
                dest.unlink(missing_ok=True)
                raise HTTPException(413, f"{upload.filename} 超过 {max_mb}MB 限制")
            f.write(chunk)
    if size == 0:
        raise HTTPException(400, f"{upload.filename} 为空文件")


async def _run_via_rpa(task_id: str, img_path: Path, vid_path: Path, result_path: Path,
                       engine: str, timeout: int,
                       on_progress) -> None:
    """调用本机 RPA 服务出片（runninghub_rpa_client，Playwright 链路）。

    与 womenswear pipeline 的 Step5 同一条 RPA 腿：账号池取号 + 全局串行锁（同机同时只跑一个）。
    走 RH 币计费（网页 RPA 路径），不走 openapi 的 CNY 钱包。
    """
    from backend.services.runninghub_rpa_client import run_motion_transfer_rpa
    from backend.services import rh_account_pool as pool

    if not pool._accounts:
        pool.init_pool()
    # 全局 RPA 串行锁：浏览器 profile 不可共用，同机同时只允许一个 RPA 任务
    async with pool.get_global_rpa_lock():
        acc = pool.get_available_account()
        if not acc:
            raise RuntimeError("账号池无可用 RunningHub 账号（RPA 链路）")
        task_store.add_log(task_id, f"RPA 链路：使用账号 {acc.phone}")
        pool.mark_in_use(acc.phone)
        try:
            await run_motion_transfer_rpa(
                image_path=str(img_path),
                video_path=str(vid_path),
                output_path=str(result_path),
                timeout=timeout,
                engine=engine,
                phone=acc.phone,
                password=acc.password,
                profile_dir=pool.get_profile_dir(acc.phone),
                on_progress=on_progress,
            )
        except Exception as e:
            err = str(e)
            err_up = err.upper()
            is_login_err = (
                any(kw in err for kw in ["未登录", "登录失败", "登 录", "未授权", "登录态", "重新登录"])
                or "TOKEN_" in err_up
                or (("TOKEN" in err_up or "IDENTIFY" in err_up) and any(k in err for k in ("过期", "失效", "无效")))
            )
            if is_login_err:
                pool.mark_failed(acc.phone, f"登录失效: {err}", kind="login_failed")
            else:
                pool.mark_free(acc.phone)
            raise
        else:
            pool.mark_free(acc.phone)


async def _run_generation(task_id: str, img_path: Path, vid_path: Path, result_path: Path,
                          engine: str, timeout: int, fix_bad_node: bool,
                          link: str = "auto") -> None:
    """后台跑生成任务，状态写 task_store。

    link：api=纯 HTTP（openapi，CNY 钱包计费）；rpa=本机 RPA（RH 币计费）；
         auto=先 api，失败降级 rpa。
    """
    from backend.services.runninghub_api_client import (
        run_motion_transfer_api_sync, MotionApiError,
    )

    def _on_progress(stage: str, pct: int) -> None:
        task_store.update(task_id, stage=stage, progress=pct)
        task_store.add_log(task_id, f"{pct}% {stage}")

    task_store.update(task_id, status="RUNNING", progress=5, stage="启动")
    used = ""
    try:
        if link in ("auto", "api"):
            try:
                used = "api"
                await asyncio.to_thread(
                    run_motion_transfer_api_sync,
                    str(img_path), str(vid_path), str(result_path),
                    timeout=timeout, engine=engine,
                    fix_bad_node=fix_bad_node, on_progress=_on_progress,
                )
            except Exception as e:
                if link == "api":
                    raise
                # auto：API 链路失败（如 openapi 钱包欠费）→ 降级本机 RPA
                task_store.add_log(
                    task_id,
                    f"API 链路失败（{str(e)[:150]}），降级本机 RPA 链路",
                    level="warn",
                )
                _on_progress("降级本机 RPA 链路", 30)
                used = "rpa"
                await _run_via_rpa(task_id, img_path, vid_path, result_path,
                                   engine, timeout, _on_progress)
        else:
            used = "rpa"
            await _run_via_rpa(task_id, img_path, vid_path, result_path,
                               engine, timeout, _on_progress)
        if not result_path.is_file() or result_path.stat().st_size == 0:
            raise MotionApiError(f"生成结束但成片缺失或为空（link={used}）")
    except Exception as e:
        err = str(e) if isinstance(e, (MotionApiError, RuntimeError)) else repr(e)
        task_store.update(task_id, status="FAILED", stage="失败", error=err[:500])
        task_store.add_log(task_id, f"失败: {err[:300]}", level="error")
        return

    video_url = f"/outputs/motion_api/{task_id}/final.mp4"
    task_store.update(
        task_id,
        status="SUCCESS",
        progress=100,
        stage=f"完成（{used} 链路）",
        outputs=[{"fileUrl": video_url, "fileType": "mp4"}],
    )
    task_store.add_log(task_id, f"完成（{used} 链路）", level="done")
