"""动作迁移调用池网关：多台 worker 收成一个 API 入口，转发 + 按 task_id 前缀路由。

架构：
    调用方 ──HTTPS + 网关Key──► 网关(8092) ──► worker1/ws1 (8091, 账号池A)
                                  ├──► worker2/ws2 (8091, 账号池B)
                                  └──► worker3/ws3 (8091, 账号池C)

设计（无中心数据库）：
- generate：按「健康 + 在飞任务数」选一台 worker 转发；worker 返回的 task_id 自带
  "{MOTION_WORKER_ID}-" 前缀（worker 端 _new_task_id 生成），网关原样透传
- task/{id} / task/{id}/video：按前缀直达对应 worker——任务状态在各 worker 自己的
  tasks.db 里，前缀路由保证查得到
- 记账：每次 generate 落一行 logs/motion_gateway_usage.jsonl（哪把 Key、哪台机、何时）
- 网关自己不跑生成、不碰 RH 账号——挂了也不影响 worker 独立可用（可直连 worker 应急）

worker 清单：config.GATEWAY_WORKERS_FILE（JSON，改完即生效，不用重启）：
    {"workers": [
        {"name": "ws1", "url": "http://10.0.0.11:8091", "key": "worker1 的 MOTION_API_KEYS", "weight": 1},
        {"name": "ws2", "url": "http://10.0.0.12:8091", "key": "worker2 的 MOTION_API_KEYS"}
    ]}
name 必须与该机 MOTION_WORKER_ID 一致，否则前缀路由对不上。

对外暴露（前面套 Caddy/nginx 做 HTTPS，网关继续只绑环回）：
    POST /api/motion/generate            multipart: image + video（engine/timeout/fix_bad_node 可选）
    GET  /api/motion/task/{id}           状态（轮询）
    GET  /api/motion/task/{id}/video     成片下载
    GET  /api/motion/tasks               最近任务（聚合全池）
    GET  /api/motion/pool                池状态（各 worker 健康/负载/账号）
鉴权：X-API-Key，Key 配 config.GATEWAY_API_KEYS（逗号分隔）。
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path

import httpx
from fastapi import APIRouter, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse

from config import BASE_DIR, settings

router = APIRouter(prefix="/api/motion", tags=["motion-gateway"])

_USAGE_LOG = Path(BASE_DIR) / "logs" / "motion_gateway_usage.jsonl"

# worker 选择的健康探测缓存（秒）：避免每单都全池探活
_HEALTH_TTL = 5.0
_health_cache: dict[str, dict] = {}


class GatewayConfigError(RuntimeError):
    pass


def _check_key(x_api_key: str) -> None:
    keys = [k.strip() for k in (settings.GATEWAY_API_KEYS or "").split(",") if k.strip()]
    if not keys:
        raise HTTPException(503, "网关未配置 GATEWAY_API_KEYS，API 未开放")
    if x_api_key not in keys:
        raise HTTPException(401, "无效的 X-API-Key")


def _load_workers() -> list[dict]:
    """读 worker 清单。文件可热更新（改完即生效）。"""
    path = Path(settings.GATEWAY_WORKERS_FILE)
    if not path.is_file():
        raise GatewayConfigError(
            f"worker 清单不存在: {path}（格式见 motion_gateway.py 模块注释）"
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        workers = data.get("workers") or []
    except Exception as e:
        raise GatewayConfigError(f"worker 清单解析失败: {e}") from e
    out = []
    for w in workers:
        name = str(w.get("name") or "").strip()
        url = str(w.get("url") or "").rstrip("/")
        key = str(w.get("key") or "").strip()
        if not (name and url and key):
            raise GatewayConfigError(f"worker 条目缺 name/url/key: {w}")
        out.append({"name": name, "url": url, "key": key,
                    "weight": max(1, int(w.get("weight") or 1))})
    if not out:
        raise GatewayConfigError("worker 清单为空，至少配一台")
    return out


def _find_worker(workers: list[dict], task_id: str) -> dict:
    """按 task_id 前缀找 worker：name-uuid 形式，name 可含 '-'，取最长匹配。"""
    best = None
    for w in workers:
        prefix = w["name"] + "-"
        if task_id.startswith(prefix) and (best is None or len(w["name"]) > len(best["name"])):
            best = w
    return best


async def _worker_health(client: httpx.AsyncClient, w: dict) -> dict:
    """探 worker 健康（带 TTL 缓存）。返回 {ok, running_tasks, accounts_available, err}。"""
    cached = _health_cache.get(w["name"])
    now = time.time()
    if cached and now - cached["_ts"] < _HEALTH_TTL:
        return cached
    try:
        r = await client.get(f"{w['url']}/api/motion/health",
                             headers={"X-API-Key": w["key"]}, timeout=8)
        if r.status_code == 200:
            d = r.json()
            h = {"ok": bool(d.get("ok")), "running_tasks": int(d.get("running_tasks") or 0),
                 "accounts_available": int(d.get("accounts_available") or 0),
                 "accounts": d.get("accounts") or [], "err": ""}
        else:
            h = {"ok": False, "running_tasks": 0, "accounts_available": 0,
                 "accounts": [], "err": f"HTTP {r.status_code}"}
    except Exception as e:
        h = {"ok": False, "running_tasks": 0, "accounts_available": 0,
             "accounts": [], "err": str(e)[:120]}
    h["_ts"] = now
    _health_cache[w["name"]] = h
    return h


async def _pick_worker(client: httpx.AsyncClient) -> dict:
    """选机：存活 + 还有空闲账号的机器里，在飞任务最少的一台（同分按 weight 反比）。"""
    workers = _load_workers()
    healths = {w["name"]: await _worker_health(client, w) for w in workers}
    candidates = [w for w in workers
                  if healths[w["name"]]["ok"] and healths[w["name"]]["accounts_available"] > 0]
    if not candidates:
        # 全池没空闲账号时退而求其次：只要活着就派（任务会排队等账号）
        candidates = [w for w in workers if healths[w["name"]]["ok"]]
    if not candidates:
        raise HTTPException(503, f"全池 worker 不可用: "
                                 + "; ".join(f"{n}:{h['err']}" for n, h in healths.items()))
    candidates.sort(key=lambda w: (healths[w["name"]]["running_tasks"] / w["weight"]))
    return candidates[0]


def _log_usage(entry: dict) -> None:
    """记账：一行一次调用（调用方 Key 指纹 + 命中 worker + task_id + 耗时）。"""
    try:
        _USAGE_LOG.parent.mkdir(parents=True, exist_ok=True)
        entry = {"ts": int(time.time()), **entry}
        with _USAGE_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[Gateway] 记账写入失败（不影响调用）: {e}")


# ───────────────────────────── 路由 ─────────────────────────────

@router.post("/generate")
async def gw_generate(
    image: UploadFile = File(...),
    video: UploadFile = File(...),
    engine: str = Form("plus"),
    timeout: int = Form(3600),
    fix_bad_node: bool = Form(True),
    link: str = Form("auto", description="出片链路：auto/api/rpa（rpa=worker 本机 RPA）"),
    x_api_key: str = Header(default=""),
):
    """选一台 worker 提交生成。返回的 task_id 自带 worker 前缀，后续按前缀路由。"""
    _check_key(x_api_key)
    t0 = time.time()
    async with httpx.AsyncClient(timeout=httpx.Timeout(300, connect=15)) as client:
        workers = _load_workers()
        try:
            w = await _pick_worker(client)
        except HTTPException:
            _log_usage({"event": "generate", "ok": False, "key_tail": x_api_key[-6:],
                        "error": "no_worker"})
            raise
        try:
            # multipart 流式转发（不整读进内存；图片/视频直接透传）
            r = await client.post(
                f"{w['url']}/api/motion/generate",
                headers={"X-API-Key": w["key"]},
                data={"engine": engine, "timeout": str(timeout),
                      "fix_bad_node": "true" if fix_bad_node else "false",
                      "link": link},
                files={
                    "image": (image.filename, image.file, image.content_type or "application/octet-stream"),
                    "video": (video.filename, video.file, video.content_type or "application/octet-stream"),
                },
            )
        except Exception as e:
            _log_usage({"event": "generate", "ok": False, "worker": w["name"],
                        "key_tail": x_api_key[-6:], "error": str(e)[:200]})
            raise HTTPException(502, f"worker {w['name']} 转发失败: {str(e)[:200]}")
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code >= 400:
            _log_usage({"event": "generate", "ok": False, "worker": w["name"],
                        "key_tail": x_api_key[-6:], "error": str(body)[:200]})
            return JSONResponse(status_code=r.status_code, content=body or {"detail": "worker error"})
        task_id = body.get("task_id", "")
        _log_usage({"event": "generate", "ok": True, "worker": w["name"],
                    "key_tail": x_api_key[-6:], "task_id": task_id,
                    "ms": int((time.time() - t0) * 1000)})
        pub = (settings.GATEWAY_PUBLIC_PREFIX or "/api/motion").rstrip("/")
        return {"task_id": task_id, "worker": w["name"],
                "poll_url": f"{pub}/task/{task_id}"}


@router.get("/task/{task_id}")
async def gw_task_status(task_id: str, x_api_key: str = Header(default="")):
    """按前缀路由到对应 worker 查状态。"""
    _check_key(x_api_key)
    workers = _load_workers()
    w = _find_worker(workers, task_id)
    if not w:
        raise HTTPException(404, f"无法从 task_id 识别 worker: {task_id}")
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            r = await client.get(f"{w['url']}/api/motion/task/{task_id}",
                                 headers={"X-API-Key": w["key"]})
        except Exception as e:
            raise HTTPException(502, f"worker {w['name']} 不可达: {str(e)[:200]}")
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    # video_url 是 worker 本地路径（/outputs/...），对外改写成网关的成片接口
    if isinstance(body, dict) and body.get("video_url"):
        pub = (settings.GATEWAY_PUBLIC_PREFIX or "/api/motion").rstrip("/")
        body["video_url"] = f"{pub}/task/{task_id}/video"
    return JSONResponse(status_code=r.status_code, content=body)


@router.get("/task/{task_id}/video")
async def gw_task_video(task_id: str, x_api_key: str = Header(default="")):
    """成片流式转发（不落盘网关）。"""
    _check_key(x_api_key)
    workers = _load_workers()
    w = _find_worker(workers, task_id)
    if not w:
        raise HTTPException(404, f"无法从 task_id 识别 worker: {task_id}")
    client = httpx.AsyncClient(timeout=httpx.Timeout(300, connect=15))
    try:
        req = client.build_request("GET", f"{w['url']}/api/motion/task/{task_id}/video",
                                   headers={"X-API-Key": w["key"]})
        resp = await client.send(req, stream=True)
    except Exception as e:
        await client.aclose()
        raise HTTPException(502, f"worker {w['name']} 不可达: {str(e)[:200]}")
    if resp.status_code >= 400:
        await resp.aread()
        await client.aclose()
        return JSONResponse(status_code=resp.status_code,
                            content={"detail": f"worker {w['name']}: {resp.text[:200]}"})

    async def _iter():
        try:
            async for chunk in resp.aiter_bytes(64 * 1024):
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    return StreamingResponse(_iter(), media_type=resp.headers.get("content-type", "video/mp4"))


@router.get("/tasks")
async def gw_tasks(limit: int = 20, x_api_key: str = Header(default="")):
    """聚合全池最近任务（并发拉各 worker，按时间倒序）。"""
    _check_key(x_api_key)
    workers = _load_workers()
    limit = max(1, min(limit, 100))

    async def _fetch(client: httpx.AsyncClient, w: dict):
        try:
            r = await client.get(f"{w['url']}/api/motion/tasks",
                                 params={"limit": limit},
                                 headers={"X-API-Key": w["key"]}, timeout=20)
            items = r.json() if r.status_code == 200 else []
            for it in items:
                it["worker"] = w["name"]
            return items
        except Exception:
            return []

    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(*[_fetch(client, w) for w in workers])
    merged = [it for items in results for it in items]
    merged.sort(key=lambda it: it.get("created_at") or 0, reverse=True)
    return merged[:limit]


@router.get("/pool")
async def gw_pool(x_api_key: str = Header(default="")):
    """池状态：各 worker 健康/在飞/账号。运维看板 + 选机依据。"""
    _check_key(x_api_key)
    workers = _load_workers()
    async with httpx.AsyncClient() as client:
        healths = await asyncio.gather(*[_worker_health(client, w) for w in workers])
    return {
        "workers": [
            {"name": w["name"], "url": w["url"], "weight": w["weight"], **{k: v for k, v in h.items() if k != "_ts"}}
            for w, h in zip(workers, healths)
        ],
        "usage_log": str(_USAGE_LOG),
    }


# FastAPI 应用（独立进程：python motion_gateway.py）
app = FastAPI(title="动作迁移调用池网关")
app.include_router(router)


@app.get("/health")
async def gw_health():
    """网关自身存活（给反代/监控探活，不需要 Key）。"""
    return {"ok": True, "role": "motion-gateway"}


# 反代子目录场景：/motion/health 被映射成 /api/motion/health，也挂一份
@router.get("/health")
async def gw_health_alias():
    return {"ok": True, "role": "motion-gateway"}


if __name__ == "__main__":
    import uvicorn

    print("=" * 60)
    print("动作迁移调用池网关")
    print(f"对外: http://{settings.GATEWAY_HOST}:{settings.GATEWAY_PORT}/api/motion/generate")
    print(f"worker 清单: {settings.GATEWAY_WORKERS_FILE}")
    try:
        for w in _load_workers():
            print(f"  - {w['name']}: {w['url']} (weight={w['weight']})")
    except GatewayConfigError as e:
        print(f"  [!] {e}")
    print("=" * 60)
    uvicorn.run(app, host=settings.GATEWAY_HOST, port=settings.GATEWAY_PORT,
                timeout_keep_alive=75, limit_concurrency=40)
