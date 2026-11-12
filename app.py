"""女装带货独立服务。只挂载 womenswear 工作流，不包含其它 video-skill 模块。"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import traceback
import uuid
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path
from time import time
from typing import Awaitable, Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from config import settings
from womenswear.router import router
from motion_api import router as motion_api_router

# ── 结构化日志 ──────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("nzdh")

UPLOAD_DIR = Path(settings.UPLOAD_DIR)
OUTPUT_DIR = Path(settings.OUTPUT_DIR)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


# ── 启动时配置校验（警告不拒绝，客户填 Key 就能跑） ──────────────
def _validate_config() -> None:
    missing = []
    if not settings.IMAGE2_API_KEY:
        missing.append("IMAGE2_API_KEY（平铺图/橱窗参考图）")
    if not settings.MIMO_API_KEY:
        missing.append("MIMO_API_KEY（模特识别/发布文案）")
    if not settings.OPENAI_API_KEY:
        missing.append("OPENAI_API_KEY（挑帧）")
    if missing:
        log.warning("═══ .env 配置缺失，首次引导会提示填写 ═══")
        for m in missing:
            log.warning("  - %s", m)


def _check_port(port: int) -> bool:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _wait_port_free(port: int, max_wait: float = 20.0) -> None:
    deadline = time() + max_wait
    while time() < deadline:
        if not _check_port(port):
            return
        time.sleep(0.5)


# ── 全局中间件：trace_id + 请求日志 + 异常兜底 ──────────────────
def _build_middlewares(app: FastAPI) -> None:
    @app.middleware("http")
    async def request_log(
        req: Request,
        call_next: Callable[[Request], Awaitable],
    ):
        tid = req.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
        start = time()
        quiet = req.url.path == "/health"

        try:
            resp = await call_next(req)
        except Exception as exc:
            cost = (time() - start) * 1000
            tb = traceback.format_exc()
            log.error(
                "[%s] %s %s → 500 (%.0fms)\n%s",
                tid, req.method, req.url.path, cost, tb,
            )
            return JSONResponse(
                status_code=500,
                content={
                    "detail": "内部错误",
                    "trace_id": tid,
                    "hint": "把 trace_id 发给技术支持排查",
                },
            )

        resp.headers["X-Request-ID"] = tid
        cost = (time() - start) * 1000
        if not quiet or cost > 500:
            log.info(
                "[%s] %s %s → %d (%.0fms)",
                tid, req.method, req.url.path, resp.status_code, cost,
            )
        return resp


# ── 生命周期 ──────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    print("=" * 60)
    print("女装带货 · 独立封装")
    print(f"页面: http://127.0.0.1:{settings.PORT}/womenswear")
    print(f"上传目录: {UPLOAD_DIR}")
    print(f"输出目录: {OUTPUT_DIR}")
    print(f"RunningHub profile: {settings.RUNNINGHUB_PROFILE_DIR}")

    _validate_config()

    # task_store + 重启恢复
    try:
        from backend.services import task_store
        task_store.init_db()
        n = task_store.recover_running()
        if n > 0:
            log.warning("重启恢复：%d 个 RUNNING 任务已标记 FAILED（可重试）", n)
    except Exception as e:
        log.error("task_store 初始化失败: %s", e)

    # RH 账号池 + 后台自动登录
    try:
        from backend.services.rh_account_pool import (
            init_on_startup,
            _accounts,
            _states,
            get_profile_dir,
            mark_ready,
            STATUS_PENDING,
            _token_cache_path,
        )
        init_on_startup()

        async def _login_pending_accounts():
            from backend.services.rh_auto_login import auto_login
            for acc in _accounts:
                st = _states.get(acc.phone)
                if not st or st.status == STATUS_PENDING:
                    log.info("[RH] 首次登录: %s", acc.phone)
                    try:
                        ok = await auto_login(
                            acc.phone, acc.password, get_profile_dir(acc.phone),
                            headless=settings.RPA_HEADLESS,
                            token_cache=_token_cache_path(acc.phone),
                        )
                        mark_ready(acc.phone, login_ok=ok)
                        log.info("[RH] %s → %s", acc.phone, "OK" if ok else "FAIL")
                    except Exception as e:
                        log.error("[RH] %s 登录异常: %s", acc.phone, e)
                        mark_ready(acc.phone, login_ok=False)

        asyncio.create_task(_login_pending_accounts())
    except Exception as e:
        log.warning("账号池初始化跳过: %s", e)

    print("=" * 60)

    # 自动开浏览器
    async def _open_browser():
        await asyncio.sleep(0.8)
        if os.environ.get("OPEN_BROWSER", "1").strip().lower() in ("0", "false", "no"):
            return
        url = f"http://127.0.0.1:{settings.PORT}/womenswear"
        try:
            webbrowser.open(url)
        except Exception as e:
            log.warning("开浏览器失败: %s", e)

    asyncio.create_task(_open_browser())

    yield

    # ── shutdown：标记所有 RUNNING 任务 ──
    try:
        from backend.services import task_store
        n = 0
        for t in task_store.list_running():
            task_store.update(t["task_id"], status="FAILED", error="服务关闭中断")
            task_store.add_log(t["task_id"], "⛔ 服务关闭中断", level="error")
            n += 1
        if n:
            log.info("关闭：%d 个 running 任务已标记 FAILED", n)
    except Exception as e:
        log.warning("关闭时标记任务失败: %s", e)
    log.info("服务已完全关闭")


app = FastAPI(title="女装带货 · 独立封装", lifespan=lifespan)
_build_middlewares(app)

app.mount("/outputs", StaticFiles(directory=str(OUTPUT_DIR)), name="outputs")
app.mount("/uploads", StaticFiles(directory=str(UPLOAD_DIR)), name="uploads")
app.include_router(router)
app.include_router(motion_api_router)


@app.get("/health")
async def health():
    return {"ok": True, "service": "womenswear"}


# ── 进程入口 ──────────────────────────────────────────────────
if __name__ == "__main__":
    root = Path(__file__).resolve().parent
    os.chdir(root)
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    url = f"http://127.0.0.1:{settings.PORT}/womenswear"
    if _check_port(settings.PORT):
        log.warning("port %d 已被占用，等它释放...", settings.PORT)
        _wait_port_free(settings.PORT, max_wait=15)

    if _check_port(settings.PORT):
        log.info("port 仍被占用，打开已有实例页面")
        try:
            webbrowser.open(url)
        except Exception:
            pass
        sys.exit(0)

    import uvicorn
    uvicorn.run(
        "app:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=False,
        timeout_keep_alive=75,
        limit_concurrency=40,
        log_level="info",
    )
