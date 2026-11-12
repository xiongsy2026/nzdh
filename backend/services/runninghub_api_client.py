"""RunningHub 纯 HTTP 动作迁移客户端：参考图 + 模仿视频 → 成片视频，全程不打开浏览器。

与 runninghub_rpa_client（Playwright RPA 链路）能力等价，但没有任何浏览器依赖：

1. 账号池拿 API Key（登录 token 复用 rh_profiles 的缓存，不用浏览器导出）
2. /openapi/v2/media/upload/binary 上传参考图、模仿视频
3. /task/openapi/create 提交任务：nodeInfoList 改 299（参考图）、275（模仿视频）两个输入节点；
   默认同时修掉 600 坏节点（FailedTrigger：pix_fmt="None" 故意把任务标 FAILED 的那个），
   让任务能正常 COMPLETED，走官方 outputs 接口正大光明取片
4. /openapi/v2/query 轮询状态
5. /task/openapi/outputs 取成片 URL → 下载

RPA 链路为什么非开浏览器不可：工作流带 600 坏节点故意把任务标 FAILED
（据称是为了绕过平台的视频时长/质量校验），官方 outputs 接口对 FAILED 任务不给 URL，
RPA 只能从 ComfyUI 画布 widget 直读 cos_url。本模块的对策是「修掉坏节点让任务正常成功」，
若平台校验仍把它打回 FAILED，则走兜底链从历史/实例接口捞成片：

  兜底① /openapi/v2/query 的 results / failedReason.currentOutputs 里扫 mp4 URL
  兜底② /api/output/v2/history（hasOutput=true）按 taskId 找 fileUrl
  兜底③ ComfyUI 实例 /history/{taskId}（用 netWssUrl 推实例地址 + /api/instance/access/auth
        的 accessKey）拿 285 节点产物，构造 /view 下载地址
"""
from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

import httpx

from runninghub_sdk import RunningHubClient
from runninghub_sdk.typedefs import NodeInput, V2QueryResult

# 与 runninghub_rpa_client 一致的节点约定（动作迁移weeksir_带坏节点 工作流）
NODE_IMAGE = "299"        # LoadImage：动作迁移参考图（showcase）
NODE_VIDEO = "275"        # VHS_LoadVideo：模仿视频
NODE_FULL_VIDEO = "285"   # VHS_VideoCombine save_output=True：完整成片输出节点
NODE_BAD_TRIGGER = "600"  # FailedTrigger：故意把任务标 FAILED 的坏节点

# 前端 engine 值 → RunningHub 平台实例档位（RPA 侧 plus=中间按钮，lite=左侧按钮，ultra=最右）
ENGINE_TO_INSTANCE_TYPE = {"plus": "plus", "lite": "lite", "standard": "ultra", "ultra": "ultra"}

_POLL_INTERVAL = 10.0
_URL_RE = re.compile(r'https?://[^\s"\'\\]+', re.I)


class MotionApiError(RuntimeError):
    """动作迁移 API 链路失败（str(e) 可直接回给调用方）"""


def _report(cb: Optional[Callable[[str, int], None]], stage: str, pct: int) -> None:
    print(f"[MotionAPI] {pct}% {stage}")
    if not cb:
        return
    try:
        cb(stage, int(pct))
    except Exception as e:
        print(f"[MotionAPI] on_progress 失败: {e}")


# ───────────────────────────── 账号 / 客户端 ─────────────────────────────

def _pick_account(phone: str):
    """取 RH 账号：指定 phone 优先，否则从账号池拿一个可用的。"""
    from backend.services import rh_account_pool as pool

    if not pool._accounts:
        pool.init_pool()
    if phone:
        acc = pool.get_account_by_phone(phone)
        if not acc:
            raise MotionApiError(f"账号不存在: {phone}")
        return acc
    acc = pool.get_available_account()
    if not acc:
        raise MotionApiError("账号池无可用账号（全部被限流/未登录/禁用）")
    return acc


def _build_clients(phone: str):
    """建 openapi 客户端（API Key）+ 登录态（token），并返回该账号的工作流 ID。

    Returns:
        (api_client, access_token, workflow_id)
    """
    from backend.services import rh_account_pool as pool

    st = pool.get_state(phone)
    api_key = pool._get_cached_api_key(phone)
    if not api_key:
        login_client = pool._get_login_client(phone)
        api_keys = login_client.get_user_api_key(user_id="")
        api_key = api_keys.shared_api.api_key or api_keys.normal_api_key
        st.api_key_cached = api_key
        st.api_key_ts = int(time.time())
        pool._save_states()
    access_token = pool._read_access_token(phone) or ""
    workflow_id = st.runtime_wf_id or pool.WORKFLOW_ID
    return RunningHubClient(api_key=api_key), access_token, workflow_id


# ───────────────────────────── 上传 / 提交 / 轮询 ─────────────────────────────

def _upload_inputs(api_client: RunningHubClient, image_path: str, video_path: str,
                   cb: Optional[Callable[[str, int], None]]) -> tuple[str, str]:
    """上传参考图和模仿视频，返回（image fileName, video fileName）。"""
    img_p, vid_p = Path(image_path), Path(video_path)
    for p in (img_p, vid_p):
        if not p.is_file() or p.stat().st_size == 0:
            raise MotionApiError(f"输入文件不存在或为空: {p}")

    _report(cb, f"上传参考图 {img_p.name}", 10)
    img_up = api_client.upload_file(str(img_p))
    if not img_up.file_name:
        raise MotionApiError(f"参考图上传响应无 fileName: {img_up}")
    _report(cb, f"上传模仿视频 {vid_p.name}", 18)
    vid_up = api_client.upload_file(str(vid_p))
    if not vid_up.file_name:
        raise MotionApiError(f"模仿视频上传响应无 fileName: {vid_up}")
    return img_up.file_name, vid_up.file_name


def _submit_task(api_client: RunningHubClient, workflow_id: str,
                 image_name: str, video_name: str, engine: str,
                 fix_bad_node: bool, phone: str = "") -> dict:
    """提交任务。fix_bad_node=True 时把 600 坏节点修好，任务才能正常 COMPLETED。"""
    from backend.services import rh_account_pool as pool

    node_info_list = [
        NodeInput(node_id=NODE_IMAGE, field_name="image", field_value=image_name),
        NodeInput(node_id=NODE_VIDEO, field_name="video", field_value=video_name),
    ]
    if fix_bad_node:
        # 600 的 pix_fmt="None" 会让 VHS_VideoCombine 抛异常（设计内的假失败）。
        # 改成 yuv420p 后它只是 285 的一个无害重复编码，任务可正常成功。
        node_info_list.append(
            NodeInput(node_id=NODE_BAD_TRIGGER, field_name="pix_fmt", field_value="yuv420p")
        )
    instance_type = ENGINE_TO_INSTANCE_TYPE.get((engine or "plus").lower(), "plus")

    def _create(wf_id: str):
        return api_client.run(
            wf_id,
            node_info_list=node_info_list,
            instance_type=instance_type,
        )

    print(f"[MotionAPI] 提交任务 workflow={workflow_id} engine={engine}({instance_type}) fix_bad_node={fix_bad_node}")
    try:
        resp = _create(workflow_id)
    except Exception as e:
        # 私有副本不在/不可用 → 补一份拷贝再试一次
        print(f"[MotionAPI] 提交失败（{str(e)[:120]}），尝试补工作流副本后重试")
        if phone:
            try:
                up = pool.upload_workflow_to_account(phone)
                if up.get("success") and up.get("runtime_wf_id"):
                    resp = _create(str(up["runtime_wf_id"]))
                    return {"task_id": resp.task_id, "net_wss_url": getattr(resp, "net_wss_url", None) or ""}
            except Exception as e2:
                print(f"[MotionAPI] 补副本重试仍失败: {str(e2)[:120]}")
        if "NOT_ENOUGH_BALANCE" in str(e).upper():
            raise MotionApiError(
                f"提交任务失败: {e}（RunningHub openapi 按 CNY 钱包计费，约 2.5 元/次；"
                f"请给账号 {phone} 的 API 钱包充值后重试）"
            ) from e
        raise MotionApiError(f"提交任务失败: {e}") from e
    return {"task_id": resp.task_id, "net_wss_url": getattr(resp, "net_wss_url", None) or ""}


def _poll(api_client: RunningHubClient, task_id: str, timeout: float,
          cb: Optional[Callable[[str, int], None]], start_pct: int = 25) -> V2QueryResult:
    """轮询任务状态直到出结果（SUCCESS/FAILED）或超时。"""
    start = time.time()
    last_report = 0
    while True:
        elapsed = time.time() - start
        if elapsed > timeout:
            raise MotionApiError(f"等待出片超时（{int(timeout)}s）")
        v2 = api_client.query_v2(task_id)
        status = (v2.status.value if v2.status else "").upper()
        if status in ("SUCCESS", "FAILED"):
            return v2
        if status in ("CANCELED", "CANCELLED"):
            raise MotionApiError("任务被取消")
        if elapsed - last_report >= 30:
            pct = min(start_pct + 60, start_pct + int(elapsed / max(timeout, 1) * 60))
            _report(cb, f"出片中 · {status or 'QUEUED'} · 已等待 {int(elapsed) // 60}分{int(elapsed) % 60}秒", pct)
            last_report = elapsed
        time.sleep(_POLL_INTERVAL)


# ───────────────────────────── 取片（成功 + 兜底） ─────────────────────────────

def _iter_urls(obj) -> str:
    """递归扫 JSON 里的 URL 字符串（供兜底用）。"""
    if isinstance(obj, str):
        for m in _URL_RE.findall(obj):
            yield m
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _iter_urls(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _iter_urls(v)


def _pick_mp4(urls: list[str]) -> Optional[str]:
    """从候选 URL 里挑成片地址：优先 mp4 且非预览片段。"""
    mp4s = [u for u in urls if ".mp4" in u.lower()]
    for u in mp4s:
        if "preview" not in u.lower():
            return u
    return mp4s[0] if mp4s else None


def _outputs_on_success(api_client: RunningHubClient, task_id: str, v2: V2QueryResult) -> Optional[str]:
    """任务成功后取成片 URL：官方 outputs 优先，其次 v2.results 扫描。"""
    try:
        outputs = api_client.get_outputs(task_id)
    except Exception as e:
        print(f"[MotionAPI] /task/openapi/outputs 异常（改扫 v2.results）: {str(e)[:120]}")
        outputs = []
    # 优先 285 节点（完整成片），其次任意 mp4
    for o in outputs:
        if str(getattr(o, "node_id", "")) == NODE_FULL_VIDEO and o.file_url:
            return o.file_url
    for o in outputs:
        u = getattr(o, "file_url", "") or ""
        if ".mp4" in u.lower():
            return u
    urls = list(_iter_urls(v2.results))
    return _pick_mp4(urls)


def _fallback_on_failed(access_token: str, task_id: str, v2: V2QueryResult,
                        net_wss_url: str) -> Optional[tuple[str, dict]]:
    """任务 FAILED 时的取片兜底链。返回 (url, headers) 或 None。"""
    # 兜底① v2 的 results / failedReason 里扫 mp4 URL
    urls = list(_iter_urls(v2.results)) + list(_iter_urls(
        getattr(v2, "failed_reason", None).__dict__ if getattr(v2, "failed_reason", None) else {}
    ))
    url = _pick_mp4(urls)
    if url:
        print(f"[MotionAPI] 兜底① 从 query_v2 扫到成片: {url[:100]}")
        return url, {}

    # 兜底② /api/output/v2/history 按 taskId 找 fileUrl
    if access_token:
        try:
            with httpx.Client(base_url="https://www.runninghub.cn", timeout=20,
                              headers={"Authorization": f"Bearer {access_token}"}) as c:
                resp = c.post("/api/output/v2/history",
                              json={"size": 30, "current": 1, "hasOutput": True, "reloadData": False})
                data = resp.json().get("data") or []
            for rec in data:
                if str(rec.get("taskId") or "") == str(task_id):
                    fu = rec.get("fileUrl") or ""
                    if ".mp4" in fu.lower():
                        print(f"[MotionAPI] 兜底② 从 output history 捞到成片: {fu[:100]}")
                        return fu, {}
        except Exception as e:
            print(f"[MotionAPI] 兜底② output history 失败: {str(e)[:120]}")

    # 兜底③ ComfyUI 实例 /history/{taskId}：netWssUrl 推实例地址 + accessKey
    if net_wss_url:
        try:
            parsed = urlparse(net_wss_url if "://" in net_wss_url else "wss://" + net_wss_url)
            base = f"https://{parsed.netloc}"
            access_key = ""
            if access_token:
                with httpx.Client(base_url="https://www.runninghub.cn", timeout=20,
                                  headers={"Authorization": f"Bearer {access_token}"}) as c:
                    r = c.post("/api/instance/access/auth", json={})
                    access_key = ((r.json().get("data") or {}).get("accessKey")) or ""
            headers = {}
            if access_key:
                headers = {"Authorization": f"Bearer {access_key}", "Rh-Identify": access_key}
            with httpx.Client(base_url=base, timeout=30, headers=headers) as c:
                r = c.get(f"/history/{task_id}")
                hist = r.json()
            node_out = (hist.get(task_id) or {}).get("outputs", {}).get(NODE_FULL_VIDEO, {})
            url = node_out.get("cos_url") or _pick_mp4(list(_iter_urls(node_out)))
            if not url:
                for f in (node_out.get("gifs") or node_out.get("videos") or []):
                    fn = f.get("filename", "")
                    sub = f.get("subfolder", "")
                    typ = f.get("type", "output")
                    if fn:
                        url = f"{base}/view?filename={fn}&subfolder={sub}&type={typ}"
                        break
            if url:
                print(f"[MotionAPI] 兜底③ 从 ComfyUI 实例 history 捞到成片: {url[:100]}")
                return url, headers
        except Exception as e:
            print(f"[MotionAPI] 兜底③ 实例 history 失败: {str(e)[:120]}")
    return None


def _download(url: str, output_path: str, headers: Optional[dict] = None) -> None:
    print(f"[MotionAPI] 下载成片: {url[:120]}")
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=300, follow_redirects=True, headers=headers or {}) as c:
        resp = c.get(url)
        resp.raise_for_status()
        Path(output_path).write_bytes(resp.content)
    print(f"[MotionAPI] 完成（{len(resp.content) // 1024}KB）→ {output_path}")


# ───────────────────────────── 对外入口 ─────────────────────────────

def run_motion_transfer_api_sync(
    image_path: str,
    video_path: str,
    output_path: str,
    timeout: int = 3600,
    engine: str = "plus",
    phone: str = "",
    fix_bad_node: bool = True,
    on_progress: Optional[Callable[[str, int], None]] = None,
) -> str:
    """纯 HTTP 运行动作迁移（同步版）。返回输出视频本地路径。

    Args:
        image_path: 参考图（showcase/模特图）
        video_path: 模仿视频
        output_path: 成片保存路径
        timeout: 出片超时秒数
        engine: plus/lite/standard（对应平台实例档位）
        phone: RH 账号手机号（空=从账号池自动选）
        fix_bad_node: 修掉 600 坏节点让任务正常成功（推荐 True）
        on_progress: 进度回调 (stage, percent)
    """
    from backend.services import rh_account_pool as pool

    acc = _pick_account(phone)
    phone = acc.phone
    pool.mark_in_use(phone)
    try:
        api_client, access_token, workflow_id = _build_clients(phone)

        image_name, video_name = _upload_inputs(api_client, image_path, video_path, on_progress)

        _report(on_progress, "提交任务", 25)
        # 先按 fix_bad_node 提交；失败（如平台不认该覆盖）则退回原样工作流，
        # 靠 FAILED 兜底链取片
        sub = None
        try:
            sub = _submit_task(api_client, workflow_id, image_name, video_name,
                               engine, fix_bad_node=fix_bad_node, phone=phone)
        except MotionApiError:
            if fix_bad_node:
                print("[MotionAPI] 修坏节点提交失败，改用原样工作流重试（走 FAILED 兜底取片）")
                sub = _submit_task(api_client, workflow_id, image_name, video_name,
                                   engine, fix_bad_node=False, phone=phone)
            else:
                raise
        task_id = sub["task_id"]
        print(f"[MotionAPI] 任务已提交: {task_id}")

        v2 = _poll(api_client, task_id, timeout, on_progress)
        status = (v2.status.value if v2.status else "").upper()

        url, headers = None, {}
        if status == "SUCCESS":
            url = _outputs_on_success(api_client, task_id, v2)
        if not url:
            got = _fallback_on_failed(access_token, task_id, v2, sub.get("net_wss_url", ""))
            if got:
                url, headers = got
        if not url:
            raise MotionApiError(
                f"任务 {task_id} 结束（{status}）但未取到成片 URL；"
                f"错误: {v2.error_message or (v2.failed_reason.exception_message if v2.failed_reason else '')}"
            )

        _report(on_progress, "下载成片", 95)
        _download(url, output_path, headers)
        _report(on_progress, "完成", 100)
        return output_path
    finally:
        pool.mark_free(phone)


async def run_motion_transfer_api(
    image_path: str,
    video_path: str,
    output_path: str,
    timeout: int = 3600,
    engine: str = "plus",
    phone: str = "",
    fix_bad_node: bool = True,
    on_progress: Optional[Callable[[str, int], None]] = None,
) -> str:
    """run_motion_transfer_api_sync 的异步包装（跑在线程里，不阻塞事件循环）。"""
    return await asyncio.to_thread(
        run_motion_transfer_api_sync,
        image_path, video_path, output_path,
        timeout=timeout, engine=engine, phone=phone,
        fix_bad_node=fix_bad_node, on_progress=on_progress,
    )


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 3:
        print("用法: python runninghub_api_client.py <image_path> <video_path> [output_path]")
        sys.exit(1)
    img, vid = sys.argv[1], sys.argv[2]
    out = sys.argv[3] if len(sys.argv) > 3 else "outputs/motion_transfer_api.mp4"
    result = run_motion_transfer_api_sync(img, vid, out)
    print(f"\n完成: {result}")
