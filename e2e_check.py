"""女装带货 e2e：任意工作目录启动 + 页面/API + 仅保留本工作流密钥。"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ALLOWED_KEY_PREFIXES = (
    "IMAGE2_",
    "MIMO_",
    "OPENAI_",
    "RUNNINGHUB_PROFILE_DIR",
    "RUNNINGHUB_WORKFLOW_URL",
    "UPLOAD_DIR",
    "OUTPUT_DIR",
    "FRONTEND_DIR",
    "HOST",
    "PORT",
    "RPA_HEADLESS",
)
FORBIDDEN_KEY_NAMES = {
    "OMNI_API_KEY",
    "AGNES_API_KEY",
    "SR_GROK_API_KEY",
    "DASHSCOPE_API_KEY",
    "MANXUE_H3_API_KEY",
    "IMGBB_API_KEY",
    "REPLICATE_API_TOKEN",
    "RUNNINGHUB_API_KEY",
    "RUNNINGHUB_ENTERPRISE_API_KEY",
    "RUNNINGHUB_H3_API_KEY",
    "GPT55_API_KEY",
    "MOTION_PRODUCT_API_KEY",
}


class Fail(Exception):
    pass


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _get(url: str, timeout: float = 8.0):
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
        return resp.status, body, dict(resp.headers)


def check_config() -> None:
    sys.path.insert(0, str(ROOT))
    os.chdir(str(Path.cwd()))  # keep caller cwd; config 必须不依赖 cwd
    from config import BASE_DIR, settings

    if BASE_DIR.resolve() != ROOT.resolve():
        raise Fail(f"BASE_DIR 不是包根: {BASE_DIR}")
    if settings.RPA_HEADLESS:
        raise Fail("RPA_HEADLESS 应为有头 False")
    if not settings.IMAGE2_MODEL:
        raise Fail("缺少女装带货大图模型 IMAGE2 配置")
    fields = set(type(settings).model_fields.keys())
    bad = [n for n in fields if n in FORBIDDEN_KEY_NAMES]
    if bad:
        raise Fail(f"配置里仍有无关模型 Key: {bad}")
    extra = [
        n
        for n in fields
        if not any(n == p or n.startswith(p) for p in ALLOWED_KEY_PREFIXES)
    ]
    if extra:
        raise Fail(f"配置含本工作流未使用字段: {extra}")
    for p in (settings.UPLOAD_DIR, settings.OUTPUT_DIR, settings.FRONTEND_DIR):
        if not Path(p).is_absolute():
            raise Fail(f"路径必须是绝对路径: {p}")
        if str(ROOT) not in str(Path(p)):
            raise Fail(f"路径未落在包内: {p}")
    html = Path(settings.FRONTEND_DIR) / "womenswear_studio.html"
    girl = Path(settings.UPLOAD_DIR) / "default_girl_model.jpg"
    bg = Path(settings.UPLOAD_DIR) / "default_showcase_bg.jpg"
    for f in (html, girl, bg, ROOT / "start.bat"):
        if not f.exists():
            raise Fail(f"缺少文件: {f}")
    print("[ok] config 仅保留本工作流密钥，路径相对包根")


def check_image2_key() -> None:
    from config import settings

    if not settings.IMAGE2_API_KEY:
        # 包内不带 Agnes Key（首次启动由引导注册）；Key 未配置时跳过联网探测
        print("[skip] Image2 Key 未配置（引导注册后生效），跳过联网探测")
        return
    url = settings.IMAGE2_BASE_URL.rstrip("/") + "/models"
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {settings.IMAGE2_API_KEY}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            status = resp.status
            raw = resp.read()[:400]
    except urllib.error.HTTPError as e:
        status = e.code
        raw = e.read()[:400] if e.fp else b""
    except Exception as e:
        print(f"[warn] Image2 网络探测跳过: {e}")
        return
    if status in (401, 403):
        raise Fail(f"Image2 Key 被拒绝 HTTP {status} {raw[:120]!r}")
    print(f"[ok] Image2 大图接口可达 HTTP {status}")


def wait_health(base: str, timeout: float = 40.0) -> None:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            status, body, _ = _get(base + "/health", timeout=2)
            if status == 200 and b"womenswear" in body:
                print("[ok] /health", body.decode("utf-8", "replace"))
                return
            last = f"HTTP {status} {body[:120]!r}"
        except Exception as e:
            last = str(e)
        time.sleep(0.4)
    raise Fail(f"服务未起来: {last}")


def check_http(base: str) -> None:
    status, body, _ = _get(base + "/womenswear")
    if status != 200 or "动作迁移工作台".encode("utf-8") not in body:
        raise Fail(f"/womenswear 异常 HTTP {status}")
    print("[ok] /womenswear", len(body), "bytes")

    status, body, _ = _get(base + "/api/womenswear-studio/history?limit=5")
    data = json.loads(body.decode("utf-8"))
    if status != 200 or not data.get("success"):
        raise Fail(f"history 异常 {body[:200]!r}")
    print("[ok] /api/womenswear-studio/history tasks=", len(data.get("tasks") or []))

    status, body, _ = _get(base + "/api/rh-accounts/list")
    data = json.loads(body.decode("utf-8"))
    if status != 200 or not data.get("success"):
        raise Fail(f"rh-accounts 异常 {body[:200]!r}")
    print("[ok] /api/rh-accounts/list accounts=", len(data.get("accounts") or []))

    status, _, _ = _get(base + "/uploads/default_girl_model.jpg")
    if status != 200:
        raise Fail(f"默认模特图 HTTP {status}")
    print("[ok] /uploads/default_girl_model.jpg")


def start_from_foreign_cwd() -> tuple[subprocess.Popen, str]:
    port = _free_port()
    env = os.environ.copy()
    env["PORT"] = str(port)
    env["OPEN_BROWSER"] = "0"
    env["PYTHONPATH"] = str(ROOT)
    foreign = Path(os.environ.get("TEMP") or os.environ.get("TMP") or ".")
    print(f"[e2e] 从 {foreign} 启动 {ROOT / 'app.py'} 端口 {port}")
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "app.py")],
        cwd=str(foreign),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    return proc, f"http://127.0.0.1:{port}"


def main() -> int:
    print("ROOT", ROOT)
    print("caller cwd", Path.cwd())
    failed = 0
    try:
        check_config()
        check_image2_key()
    except Fail as e:
        print("[FAIL]", e)
        return 1

    proc, base = start_from_foreign_cwd()
    try:
        wait_health(base)
        check_http(base)
        print("E2E PASS")
        return 0
    except Fail as e:
        print("[FAIL]", e)
        failed = 1
        return 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
        if failed and proc.stdout:
            out = proc.stdout.read().decode("utf-8", "replace")[-2000:]
            if out:
                print("--- server log ---")
                print(out)


if __name__ == "__main__":
    raise SystemExit(main())
