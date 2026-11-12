"""RunningHub RPA 客户端：通过 Playwright 控制 ComfyUI 操作工作流

核心流程：
1. 打开工作流页面（已登录的 profile）
2. 通过 ComfyUI graph API 上传图片到 299 节点、视频到 275 节点
3. 点击主页面的"运行"按钮
4. 轮询 285 节点的 videopreview.params.cos_url（save_output=True 的完整视频节点；502 是 PreviewOnly 1 秒预览，不可用）
5. 下载视频到本地

节点链路设计（动作迁移weeksir_带坏节点.json）：
- 节点 285：VHS_VideoCombine，filename_prefix='h264 yuv420p'，save_output=True —— 真正的完整视频输出节点
- 节点 502：VHS_VideoCombine，filename_prefix='PreviewOnly'，save_output=False —— 1 秒/30 帧预览，仅供 ComfyUI 画布预览，不可下载
- 节点 600：FailedTrigger —— 在 285 生成完整视频后主动触发任务标 FAILED，绕过 RunningHub 服务端的视频时长/质量校验
- 因此任务状态最终为 FAILED 是设计内的预期行为，调用方必须 try 住 RuntimeError 后继续从 285 抓取视频
- 这正是 RPA 链路存在的意义：API 链路（/openapi/v2）遇 FAILED 直接报错拿不到视频，RPA 链路可以从 ComfyUI 画布的 videopreview widget 直接读 cos_url 抓取完整视频
"""
import asyncio
import json
import os
import time
from pathlib import Path
from typing import Optional, Callable
import httpx
from playwright.async_api import async_playwright, BrowserContext, Page, Frame

from config import settings


WORKFLOW_URL = "https://www.runninghub.cn/workflow/2106377827540103169?source=workspace"
# profile 目录与之前的调试脚本保持一致
_PROFILE_DIR = str(Path(__file__).resolve().parent.parent.parent / "runninghub_profile")
PROFILE_DIR = getattr(settings, "RUNNINGHUB_PROFILE_DIR", _PROFILE_DIR)


# ---- RPA 实时画面：给 DSH 右栏"那一页现在在跑什么"用 ----
# 🔴 为什么不是直接嵌 runninghub.cn 的原页：它回 `X-Frame-Options: SAMEORIGIN`，在别人的页面里永远是
#    空白；要让它"能嵌"就得在本机开一个带着对方登录态的反向代理 —— 那是把 RH 账号凭据经由一个 HTTP 口
#    转发出去，比空白页严重得多，不做。所以这一格放【我们自己截的图】：只有像素，不带 cookie、不带 token，
#    由 8091 自己出图（同域，可嵌）。
#    为什么一块全局缓冲区就够：RPA 由 get_global_rpa_lock() 串行，同一时刻全机只有一条任务在跑。
LIVE_DIR = Path(settings.OUTPUT_DIR) / "_rpa_live"
LIVE_SHOT = LIVE_DIR / "live.png"
LIVE_META = LIVE_DIR / "meta.json"
_live_task = None


def live_shot_state() -> dict:
    """读实时画面的元信息（从没截过 = fresh:False）。调用方拿它决定那一格显示什么。"""
    try:
        with open(LIVE_META, encoding="utf-8") as f:
            st = json.load(f)
    except (OSError, ValueError):
        return {"fresh": False, "ts": 0, "url": "", "stage": "", "progress": 0}
    age = time.time() - float(st.get("ts") or 0)
    st["age"] = round(age)
    # 60 秒没新图 = 那张是上一次任务留下的旧帧，别再当"正在跑"给人看
    st["fresh"] = LIVE_SHOT.is_file() and age < 60
    return st


def _live_shoot_loop(client, interval: float) -> None:
    """定时把 RPA 正在操作的那一页拍下来。🔴 拍不到只是那一格空白，绝不许把正在跑的任务带崩。"""
    async def _go():
        global _live_task
        page_missing = 0
        while True:
            await asyncio.sleep(interval)
            page = getattr(client, "_page", None)
            if page is None:
                page_missing += 1
                if page_missing >= 20:          # 浏览器关了一分多钟 ⇒ 收摊，等下一次任务再起
                    _live_task = None
                    return
                continue
            page_missing = 0
            try:
                LIVE_DIR.mkdir(parents=True, exist_ok=True)
                await page.screenshot(path=str(LIVE_SHOT), timeout=5000)
                try:
                    url = page.url
                except Exception:
                    url = ""
                meta = {"ts": time.time(), "url": url,
                        "stage": str(getattr(client, "_live_stage", "") or ""),
                        "progress": int(getattr(client, "_live_progress", 0) or 0)}
                tmp = LIVE_DIR / "meta.json.tmp"
                tmp.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
                os.replace(tmp, LIVE_META)
            except Exception as e:
                print(f"  [live] 截图失败（不影响 RPA 任务）：{type(e).__name__}: {str(e)[:120]}")

    _live_task = asyncio.create_task(_go())


def _ensure_live_shooter(client) -> None:
    interval = float(getattr(settings, "RPA_LIVE_INTERVAL", 3.0) or 0)
    if interval <= 0 or (_live_task is not None and not _live_task.done()):
        return
    _live_shoot_loop(client, interval)


def _stop_live_shooter() -> None:
    global _live_task
    if _live_task is not None and not _live_task.done():
        _live_task.cancel()
    _live_task = None


class SessionStaleError(RuntimeError):
    """RunningHub 登录态已失效（token 已过期/被作废）：重新登录后可重试"""


def _is_auth_error(status, data) -> bool:
    """判断接口响应是否为登录态失效（token 过期/被作废/缺失/未登录）"""
    blob = data if isinstance(data, str) else str(data)
    # TOKEN_MISSION = 请求缺 token（SSE/EventSource 发不出 Authorization 头时平台的正常应答），
    # 不是登录过期——先排除，否则会把已登录会话误判成登录失效
    if "TOKEN_MISSION" in blob.upper():
        return False
    if status in (401, 403, 412):
        return True
    if isinstance(data, dict):
        try:
            if int(data.get("code")) in (401, 403, 412):
                return True
        except Exception:
            pass
    upper = blob.upper()
    if "TOKEN_INVALID" in upper or "TOKEN_EXPIRED" in upper or "IDENTIFY_INVALID" in upper:
        return True
    # 其余平台 token 类错误码统一带 TOKEN_ 前缀（如 TOKEN_MISSING = 未带 token）
    if "TOKEN_" in upper or "TOKEN MISSING" in upper or "TOKENMISSING" in upper:
        return True
    if ("TOKEN" in upper or "IDENTIFY" in upper) and ("过期" in blob or "失效" in blob or "无效" in blob):
        return True
    if "未登录" in blob or "请重新登录" in blob:
        return True
    return ("登录" in blob or "会话" in blob) and ("过期" in blob or "失效" in blob)


class RunningHubRPA:
    """RunningHub 网页 RPA 客户端

    支持账号池：__init__ 传入 phone/password/profile_dir，connect 时自动检测未登录并触发登录
    """

    def __init__(
        self,
        headless: Optional[bool] = None,
        phone: str = "",
        password: str = "",
        profile_dir: str = "",
    ):
        """
        Args:
            headless: 是否无头模式（空=用 config.RPA_HEADLESS，默认有头）
            phone: 账号手机号（用于自动登录；空=用默认 PROFILE_DIR）
            password: 账号密码（用于自动登录）
            profile_dir: profile 目录路径（每账号独立）
        """
        self.headless = settings.RPA_HEADLESS if headless is None else headless
        self.phone = phone
        self.password = password
        # profile_dir 优先级：参数 > 账号池 > 默认
        if profile_dir:
            self.profile_dir = profile_dir
        elif phone:
            try:
                from backend.services.rh_account_pool import get_profile_dir
                self.profile_dir = get_profile_dir(phone)
            except Exception:
                self.profile_dir = PROFILE_DIR
        else:
            self.profile_dir = PROFILE_DIR
        self._playwright = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._comfy_frame: Optional[Frame] = None
        self.on_progress: Optional[Callable[[str, int], None]] = None
        # 页面层登录态失效信号（网页自身的接口报 token 错误时记录，供自愈重登）
        self._auth_error_seen = ""

    def _report(self, stage: str, progress: int) -> None:
        print(f"[RPA] {progress}% {stage}")
        # 实时画面：阶段一变就顺手把截图循环拉起来（幂等，已在跑就什么都不做）
        self._live_stage, self._live_progress = str(stage), int(progress)
        try:
            _ensure_live_shooter(self)
        except Exception as e:
            print(f"  [live] 实时画面没拉起来（不影响任务）：{type(e).__name__}: {str(e)[:120]}")
        cb = self.on_progress
        if not cb:
            return
        try:
            cb(stage, int(progress))
        except Exception as e:
            print(f"[RPA] on_progress 失败: {e}")

    def _note_auth_error(self, source: str, status, blob) -> None:
        """记录一次登录态失效信号（页面接口/控制台）"""
        self._auth_error_seen = f"{source} status={status}: {str(blob)[:200]}"
        print(f"  [warn] 检测到登录态失效信号: {self._auth_error_seen}")

    async def _verify_login_alive(self) -> bool:
        """token 失效信号的最终裁决：探测一次需要登录的业务接口，确认登录态是否真的失效。

        页面里 SSE/EventSource 类请求发不出 Authorization 头，平台固定回
        403 TOKEN_MISSION（= 请求缺 token，不是登录过期），且会自动反复重连一直报；
        直接信它会把正常打开的工作流页面误判成登录过期。因此信号只当线索，
        以业务接口探测结果为准：能正常应答 = 登录态还在（误报，忽略信号）。
        """
        if not self._page:
            return False
        # ① 探测需要登录的业务接口（与任务轮询同一个 /api/user/task/list）
        try:
            result = await self._page.evaluate(
                """async () => {
                    try {
                        const r = await fetch('/api/user/task/list', {
                            method: 'POST',
                            headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify({size: 5, current: 1, taskStatus: ['RUNNING', 'QUEUED'], taskType: ['WORKFLOW', 'WEBAPP']})
                        });
                        return {status: r.status, body: (await r.text()).substring(0, 300)};
                    } catch(e) { return {err: e.message}; }
                }"""
            )
            if not result.get("err"):
                if _is_auth_error(result.get("status"), result.get("body") or ""):
                    print(f"  [verify] 业务接口确认登录态失效: {str(result)[:160]}")
                    return False
                print("  [verify] 业务接口正常应答，登录态还在（token 失效信号属误报）")
                return True
        except Exception as e:
            print(f"  [verify] 登录态探测异常: {str(e)[:120]}")
        # ② 兜底：页面出现登录按钮 = 确实未登录
        try:
            login_btn = self._page.locator("button:has-text('登 录'), button:has-text('登录')").first
            if await login_btn.is_visible(timeout=2000):
                print("  [verify] 页面出现登录按钮，确认未登录")
                return False
        except Exception:
            pass
        # 探测不到失效证据 → 按登录态正常处理（宁可漏判，不误伤正常会话）
        return True

    async def _raise_if_session_stale(self) -> None:
        """有登录态失效信号时先真实校验，确认失效才抛 SessionStaleError（触发自愈重登）"""
        if not self._auth_error_seen:
            return
        msg = self._auth_error_seen
        self._auth_error_seen = ""
        if await self._verify_login_alive():
            print(f"  [warn] 忽略误报的登录态失效信号: {msg[:120]}")
            return
        raise SessionStaleError(msg)

    def _attach_auth_watchers(self, page: Page) -> None:
        """监听页面接口响应与控制台，捕获 token 失效信号

        网页自身的接口（页面内容加载、点运行 onClick 等）报 token 错误时
        不会抛进我们的代码（如 403 TOKEN_MISSION 只在页面控制台/弹提示），
        不监听就会漏掉登录态失效 → 误判为普通失败或傻等超时。
        """

        async def _on_response(response):
            url = response.url or ""
            if "/api/" not in url and "/uc/" not in url:
                return
            # SSE/EventSource 请求发不出 Authorization 头，RunningHub 固定回
            # {"code":403,"msg":"TOKEN_MISSION"}（= 缺 token ≠ 登录过期），且失败后
            # 自动反复重连持续上报——这类信号从源头忽略，不作为登录态失效依据
            try:
                if response.request.resource_type == "eventsource":
                    return
            except Exception:
                pass
            status = response.status
            if status not in (200, 401, 403, 412):
                return
            try:
                body = await response.text()
            except Exception:
                return
            # 401/412 本身就是登录态失效；403/200 必须带 token 错误字样才认定
            if status in (401, 412) or _is_auth_error(0, body):
                self._note_auth_error("页面接口", status, body)

        def _on_console(msg):
            try:
                text = msg.text or ""
            except Exception:
                return
            if _is_auth_error(0, text):
                self._note_auth_error("页面控制台", 0, text)

        page.on("response", _on_response)
        page.on("console", _on_console)

    async def connect(self):
        """启动浏览器并打开工作流页面"""
        print(f"[RPA] 启动浏览器 headless={self.headless} profile={self.profile_dir}")
        self._playwright = await async_playwright().start()
        self._context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=self.profile_dir,
            headless=self.headless,
            viewport={"width": 1440, "height": 1000},
            locale="zh-CN",
            args=["--disable-blink-features=AutomationControlled"],
        )
        self._page = self._context.pages[0] if self._context.pages else await self._context.new_page()
        self._auth_error_seen = ""
        self._attach_auth_watchers(self._page)

    async def _try_auto_login_if_needed(self):
        """检测到未登录时，自动调用 rh_auto_login 完成首次登录

        前提：self.phone 和 self.password 不为空
        Returns: True=已登录或登录成功；False=登录失败
        """
        if not self.phone or not self.password:
            return False
        login_btn = self._page.locator("button:has-text('登 录'), button:has-text('登录')").first
        try:
            is_visible = await login_btn.is_visible(timeout=2000)
        except Exception:
            is_visible = False
        if not is_visible:
            return True  # 已登录

        print(f"[RPA] 检测到未登录，自动调用登录流程: {self.phone}")
        # 先关闭当前浏览器（释放 profile 锁），让 auto_login 独占 profile
        try:
            if self._context:
                await self._context.close()
            if self._playwright:
                await self._playwright.stop()
        except Exception as e:
            print(f"  [warn] 关闭浏览器异常: {e}")
        self._context = None
        self._playwright = None
        self._page = None

        # 调用登录
        from backend.services.rh_auto_login import auto_login
        from backend.services.rh_account_pool import mark_ready
        ok = await auto_login(self.phone, self.password, self.profile_dir, headless=self.headless)
        mark_ready(self.phone, login_ok=ok)
        if not ok:
            raise RuntimeError(f"账号 {self.phone} 自动登录失败，请检查密码或风控状态")

        # 重新启动浏览器
        await self.connect()
        # 重新打开工作流
        await self.open_workflow()
        return True

    async def close(self):
        """关闭浏览器"""
        _stop_live_shooter()
        if self._context:
            await self._context.close()
        if self._playwright:
            await self._playwright.stop()

    async def reset_session_and_relogin(self):
        """登录态过期自愈：关浏览器 → 清除旧会话重新登录 → 重开浏览器"""
        if not self.phone:
            raise RuntimeError(
                "RunningHub 登录态已过期，且未配置账号，无法自动重登；请在「ComfyUI 账号池」里重新登录该账号"
            )
        print(f"[RPA] 登录态已过期，清除旧会话并重新登录: {self.phone}")
        try:
            if self._context:
                await self._context.close()
            if self._playwright:
                await self._playwright.stop()
        except Exception as e:
            print(f"  [warn] 关闭浏览器异常: {e}")
        self._context = None
        self._playwright = None
        self._page = None

        from backend.services.rh_account_pool import mark_ready
        if self.password:
            from backend.services.rh_auto_login import auto_login
            ok = await auto_login(self.phone, self.password, self.profile_dir, headless=self.headless, force=True)
        else:
            # Cookie 登录账号（无密码）：弹出浏览器请客户重新登录一次，脚本记录 Cookie
            print(f"[RPA] 请在弹出的浏览器里重新登录 RunningHub: {self.phone}")
            from backend.services.rh_auto_login import cookie_login
            res = await cookie_login(
                self.phone, self.profile_dir, headless=False, timeout_seconds=300, force=True
            )
            ok = bool(res.get("ok"))
        mark_ready(self.phone, login_ok=ok)
        if not ok:
            if self.password:
                raise RuntimeError(
                    f"账号 {self.phone} 登录态过期且自动重新登录失败，请在「ComfyUI 账号池」里重新登录该账号"
                )
            raise RuntimeError(
                f"账号 {self.phone} 登录态过期且重新登录失败，请刷新网页重新完成引导登录"
            )
        await self.connect()

    async def open_workflow(self, workflow_url: str = ""):
        """打开工作流页面并等待加载。有账号私有副本则打开副本。"""
        self._auth_error_seen = ""
        if not workflow_url:
            if self.phone:
                try:
                    from backend.services.rh_account_pool import workflow_url_for_phone
                    workflow_url = workflow_url_for_phone(self.phone)
                except Exception:
                    workflow_url = WORKFLOW_URL
            else:
                workflow_url = WORKFLOW_URL
        print(f"[RPA] 打开工作流: {workflow_url}")
        # RunningHub 页面有持续 polling，networkidle 经常等不到 → try/except 兜底
        # 不用 domcontentloaded - 太早，iframe 还没注入
        # 不用 load 单独 - onload 时 iframe 也可能还没挂载
        # 策略：先 try networkidle 30s（少数情况能等到，iframe 已就绪）；超时就 fallback 到 domcontentloaded + 等待循环
        try:
            await self._page.goto(workflow_url, wait_until="networkidle", timeout=30000)
        except Exception as _e:
            print(f"  [warn] networkidle 30s 超时，回退到 domcontentloaded: {str(_e)[:80]}")
            try:
                await self._page.goto(workflow_url, wait_until="domcontentloaded", timeout=30000)
            except Exception as _e2:
                print(f"  [warn] domcontentloaded 也超时: {str(_e2)[:80]}")
        await self._page.wait_for_timeout(5000)

        # 检查登录：如果未登录且有账号信息 → 自动登录；否则抛错
        login_btn = self._page.locator("button:has-text('登 录'), button:has-text('登录')").first
        try:
            if await login_btn.is_visible(timeout=2000):
                # 未登录：尝试登录（有密码自动登录；Cookie 账号弹浏览器手动登录）
                if self.phone:
                    print(f"[RPA] 检测到未登录，触发登录: {self.phone}")
                    # 关闭当前 context 释放 profile 锁
                    try:
                        if self._context:
                            await self._context.close()
                        if self._playwright:
                            await self._playwright.stop()
                    except Exception as e:
                        print(f"  [warn] 关闭浏览器异常: {e}")
                    self._context = None
                    self._playwright = None
                    self._page = None
                    self._comfy_frame = None

                    # 调用登录
                    from backend.services.rh_account_pool import mark_ready
                    if self.password:
                        from backend.services.rh_auto_login import auto_login
                        ok = await auto_login(self.phone, self.password, self.profile_dir, headless=self.headless)
                    else:
                        print(f"[RPA] 请在弹出的浏览器里登录 RunningHub: {self.phone}")
                        from backend.services.rh_auto_login import cookie_login
                        res = await cookie_login(self.phone, self.profile_dir, headless=False, timeout_seconds=300)
                        ok = bool(res.get("ok"))
                    mark_ready(self.phone, login_ok=ok)
                    if not ok:
                        raise RuntimeError(
                            f"账号 {self.phone} 登录失败，请刷新网页重新完成引导登录"
                        )
                    # 重新连接并打开工作流
                    await self.connect()
                    await self._page.goto(workflow_url, wait_until="domcontentloaded", timeout=30000)
                    await self._page.wait_for_timeout(5000)
                else:
                    raise RuntimeError(
                        "RunningHub 未登录！请刷新网页重新完成引导登录（记录 Cookie）"
                    )
        except RuntimeError:
            raise
        except Exception:
            pass  # 按钮不可见 = 已登录

        # 等 ComfyUI iframe
        print("[RPA] 等待 ComfyUI iframe...")
        last_dbg = -10
        for i in range(30):
            await self._page.wait_for_timeout(2000)
            for f in self._page.frames:
                if "comfyUI" in f.url or "comfyui" in f.url.lower():
                    self._comfy_frame = f
                    break
            if self._comfy_frame:
                print(f"  [{(i+1)*2}s] iframe 就绪")
                break
            # 页面接口报 token 失效（如 403 TOKEN_MISSION）时提前退出触发自愈，不干等
            await self._raise_if_session_stale()
            # 每 10s 打印一次 frame 列表，便于诊断
            if (i+1)*2 - last_dbg >= 10:
                frame_urls = [f.url[:80] for f in self._page.frames if f.url and f.url != 'about:blank']
                print(f"  [{(i+1)*2}s] 暂无 ComfyUI iframe，当前 {len(frame_urls)} 个 frame: {frame_urls}")
                last_dbg = (i+1)*2

        if not self._comfy_frame:
            await self._raise_if_session_stale()
            frame_urls = [f.url[:80] for f in self._page.frames if f.url and f.url != 'about:blank']
            raise RuntimeError(f"未找到 ComfyUI iframe (当前 {len(frame_urls)} 个 frame: {frame_urls})")

        # 等 graph 加载
        print("[RPA] 等待 graph 加载...")
        for i in range(60):
            await self._page.wait_for_timeout(2000)
            count = await self._comfy_frame.evaluate(
                "() => (typeof app !== 'undefined' && app.graph) ? app.graph._nodes.length : -1"
            )
            if count > 0:
                print(f"  [{(i+1)*2}s] graph 加载完成（{count} 个节点）")
                return
            # 页面接口报 token 失效时提前退出触发自愈，不干等
            await self._raise_if_session_stale()
        await self._raise_if_session_stale()
        raise RuntimeError("工作流 graph 加载超时")

    async def upload_to_node(self, node_id: int, file_path: str, widget_name: str) -> str:
        """上传文件到 ComfyUI 节点

        通过 ComfyUI 的 fetchApi 直接调用 /api/upload/image 端点上传文件，
        然后设置节点的 widget 值。

        Args:
            node_id: 节点 ID
            file_path: 本地文件路径
            widget_name: 要设置的 widget 名（如 'image' 或 'video'）

        Returns:
            上传后的文件名
        """
        abs_path = str(Path(file_path).resolve())
        filename_only = Path(abs_path).name
        print(f"[RPA] 上传到节点 {node_id}: {abs_path}")

        # 读取文件转 base64，在 iframe 里用 fetchApi 上传
        import base64
        file_bytes = Path(abs_path).read_bytes()
        file_b64 = base64.b64encode(file_bytes).decode('ascii')
        # 根据扩展名判断 mime type
        ext = Path(abs_path).suffix.lower().lstrip('.')
        mime = {
            'png': 'image/png', 'jpg': 'image/jpeg', 'jpeg': 'image/jpeg',
            'webp': 'image/webp', 'mp4': 'video/mp4', 'webm': 'video/webm',
            'mov': 'video/quicktime',
        }.get(ext, 'application/octet-stream')

        # 在 ComfyUI iframe 内用 api.apiURL + fetchApi 上传
        # api.apiURL('/upload/image') 会自动拼接 Rh-Comfy-Auth 和 Rh-Identify token
        upload_result = await self._comfy_frame.evaluate(
            """async ([b64, filename, mime]) => {
                // base64 → blob
                const byteChars = atob(b64);
                const byteNums = new Array(byteChars.length);
                for (let i = 0; i < byteChars.length; i++) {
                    byteNums[i] = byteChars.charCodeAt(i);
                }
                const byteArray = new Uint8Array(byteNums);
                const blob = new Blob([byteArray], {type: mime});
                const formData = new FormData();
                formData.append('image', blob, filename);
                formData.append('subfolder', '');
                formData.append('type', 'input');

                // 用 api.apiURL 构造带 token 的 URL
                const uploadUrl = api.apiURL('/upload/image');
                const resp = await fetch(uploadUrl, {
                    method: 'POST',
                    body: formData,
                });
                const text = await resp.text();
                let data;
                try { data = JSON.parse(text); } catch(e) { data = {raw: text}; }
                return {status: resp.status, data: data};
            }""",
            [file_b64, filename_only, mime],
        )

        status = upload_result.get('status')
        data = upload_result.get('data')
        if _is_auth_error(status, data):
            raise SessionStaleError(f"上传接口报登录态失效（token 已过期/被作废）: status={status}: {data}")
        if status != 200:
            raise RuntimeError(f"上传失败 status={status}: {data}")
        # 页面层监听器捕获到的 token 失效信号（网页自身接口报错）同样触发自愈
        await self._raise_if_session_stale()

        uploaded = data
        # 打印完整响应，便于排查 RunningHub 服务端问题
        print(f"  [debug] 上传响应 status={upload_result.get('status')} data={uploaded}")

        # RunningHub 会重命名文件为哈希值，响应字段是 "name"
        filename = uploaded.get('name') or uploaded.get('filename')
        if not filename:
            raise RuntimeError(f"上传响应无 filename: {uploaded}")

        # 校验返回的 name 是否和本地 hash 一致（RunningHub 用文件内容 sha256 作文件名）
        local_hash = __import__('hashlib').sha256(file_bytes).hexdigest().upper()
        print(f"  [debug] 本地 sha256={local_hash}")
        print(f"  [debug] RunningHub 返回 name={filename}")
        if local_hash in filename.upper():
            print(f"  [ok] hash 一致，文件内容已正确上传")
        else:
            print(f"  [warn] hash 不一致！RunningHub 可能没收到完整文件内容")

        print(f"  上传成功: {filename}")

        # 设置节点的 widget 值
        await self._comfy_frame.evaluate(
            """([nodeId, widgetName, filename]) => {
                const node = app.graph._nodes_by_id[nodeId];
                if (!node) throw new Error('节点不存在: ' + nodeId);
                const widget = node.widgets.find(w => w.name === widgetName);
                if (!widget) throw new Error('widget 不存在: ' + widgetName);
                widget.value = filename;
                if (widget.callback) widget.callback(filename);
            }""",
            [node_id, widget_name, filename],
        )
        print(f"  节点 {node_id} widget '{widget_name}' = {filename}")

        return filename

    async def _list_run_buttons(self) -> list:
        """识别主页面所有运行按钮，返回其索引/文本/class/位置，便于按 engine 选择

        RunningHub 工作流页面通常有 3 个运行按钮（lite / standard / ultra），
        按钮文本可能为 "运行"、"RUN" 或带前缀如 "Lite 运行"、"Standard 运行"、"Ultra 运行"。
        本方法把它们都抓出来，方便调用方按文本/class 精确选择。
        """
        try:
            buttons_info = await self._page.evaluate("""() => {
                const btns = Array.from(document.querySelectorAll('button.run-btn, button[class*="run-btn"], button[class*="beveled-btn"]'));
                return btns.map((b, i) => {
                    const rect = b.getBoundingClientRect();
                    return {
                        index: i,
                        text: (b.innerText || b.textContent || '').trim().substring(0, 80),
                        className: b.className,
                        x: Math.round(rect.x),
                        y: Math.round(rect.y),
                        width: Math.round(rect.width),
                        height: Math.round(rect.height),
                        visible: rect.width > 0 && rect.height > 0,
                    };
                });
            }""")
            return buttons_info or []
        except Exception as e:
            print(f"  [warn] 识别运行按钮失败: {e}")
            return []

    def _select_button_by_engine(self, buttons: list, engine: str) -> int:
        """根据 engine 在已识别按钮列表中选目标索引

        实测 RunningHub 工作流页面有 3 个运行按钮（按 x 坐标从左到右）：
        - 左侧 idx=0：class='beveled-btn-right'，文本='运行\nLite/Standard'
        - 中间 idx=1：class='beveled-btn-left'，文本='运行'  ← plus 默认选这个
        - 右侧 idx=2：class='beveled-btn-ultra'，文本='运行'  ← Ultra 模式

        用户语义：前端 "plus" = 中间按钮（beveled-btn-left）
                  前端 "lite" = 左侧按钮（文本含 Lite/Standard 或最左侧）

        优先级：
        1. plus → class 含 'beveled-btn-left'；lite → 文本含 'lite' 或 class 含 'beveled-btn-right'
        2. 兜底：lite → 最左；plus → 中间一个；其它 → 最右
        """
        engine = (engine or "plus").lower()
        # 仅看可见按钮
        visible = [b for b in buttons if b.get("visible")]
        if not visible:
            return -1

        # 1) plus 优先匹配 beveled-btn-left；lite 优先匹配文本含 'lite' 或 class 含 'beveled-btn-right'
        if engine == "plus":
            for b in visible:
                if "beveled-btn-left" in (b.get("className") or "").lower():
                    return b.get("index", -1)
        elif engine == "lite":
            # 文本含 lite 的（实测左侧按钮文本是 '运行\nLite/Standard'）
            for b in visible:
                if "lite" in (b.get("text") or "").lower():
                    return b.get("index", -1)
            # 或 class 含 beveled-btn-right
            for b in visible:
                if "beveled-btn-right" in (b.get("className") or "").lower():
                    return b.get("index", -1)

        # 2) 通用关键字匹配（兼容未来 engine 值）
        for b in visible:
            cls = (b.get("className") or "").lower()
            if engine in cls:
                return b.get("index", -1)
        for b in visible:
            txt = (b.get("text") or "").lower()
            if engine in txt:
                return b.get("index", -1)

        # 3) 位置兜底（按 x 坐标排序）
        sorted_btns = sorted(visible, key=lambda b: b.get("x", 0))
        if engine == "lite":
            return sorted_btns[0].get("index", -1)
        if engine == "plus":
            # 中间一个（3 个按钮时 idx=1）
            mid = len(sorted_btns) // 2
            return sorted_btns[mid].get("index", -1) if sorted_btns else -1
        # 其它 → 最右
        return sorted_btns[-1].get("index", -1) if sorted_btns else -1

    async def click_run(self, engine: str = "plus"):
        """点击主页面的运行按钮，按 engine 识别并选择对应按钮

        Args:
            engine: plus/lite/standard —— 对应 RunningHub 主页面的 Ultra/Standard/Lite 按钮
        """
        engine = (engine or "plus").lower()
        print(f"[RPA] 点击运行按钮（engine={engine}）...")
        # 记录运行前的旧视频 filename 和任务列表，用于后续判断新视频生成
        self._old_preview_filename = await self._get_preview_filename(285)
        if self._old_preview_filename:
            print(f"  旧视频: {self._old_preview_filename}")

        # 记录运行前的任务 ID 列表，用于识别新任务
        self._old_task_ids = await self._get_running_task_ids()
        if self._old_task_ids:
            print(f"  旧任务数: {len(self._old_task_ids)}")

        # 先识别页面上所有运行按钮
        buttons = await self._list_run_buttons()
        if buttons:
            print(f"  识别到 {len(buttons)} 个运行按钮:")
            for b in buttons:
                vis = "✓" if b.get("visible") else "✗"
                print(f"    [{vis}] idx={b.get('index')} text={b.get('text')!r} class={b.get('className')!r} pos=({b.get('x')},{b.get('y')})")

        target_idx = self._select_button_by_engine(buttons, engine)
        if target_idx < 0:
            # 兜底：用 has-text('运行') 的最后一个
            run_btns = self._page.locator("button.run-btn:has-text('运行')")
            count = await run_btns.count()
            if count == 0:
                raise RuntimeError(f"未找到运行按钮（识别到 {len(buttons)} 个按钮，无匹配 engine={engine}）")
            run_btn = run_btns.nth(count - 1)
            print(f"  [兜底] engine={engine} 未精确匹配，用第 {count} 个运行按钮")
        else:
            # 通过 index 选择对应按钮
            run_btn = self._page.locator("button.run-btn, button[class*='beveled-btn']").nth(target_idx)
            matched = next((b for b in buttons if b.get("index") == target_idx), None)
            if matched:
                print(f"  匹配 engine={engine}: idx={target_idx} text={matched.get('text')!r} class={matched.get('className')!r}")

        await run_btn.scroll_into_view_if_needed()
        await run_btn.click()
        print(f"[RPA] 已点击运行（engine={engine}）")

        # 等待 8 秒后再次查询任务列表，确认提交是否真的进了队列
        # 如果一直 total=0，说明 click_run 没真正点到运行按钮（按钮被遮挡/不是真正的提交按钮）
        await self._page.wait_for_timeout(8000)
        # 点运行的 onClick 在页面里报 token 失效（如 403 TOKEN_MISSION）时不会抛进我们代码，
        # 不检查就会傻等到超时——这里直接触发自愈重登重试
        await self._raise_if_session_stale()
        try:
            verify = await self._page.evaluate("""async () => {
                try {
                    const r = await fetch('/api/user/task/list', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({size: 50, current: 1, taskStatus: ['RUNNING', 'QUEUED'], taskType: ['WORKFLOW', 'WEBAPP']})
                    });
                    const j = await r.json();
                    const records = j?.data?.records || [];
                    return {total: records.length, sample: records.slice(0, 3).map(r => ({id: r.id, status: r.taskStatus, name: (r.taskName||'').substring(0,40)}))};
                } catch(e) { return {err: e.message}; }
            }""")
            if verify.get('err'):
                print(f"  [verify] 任务列表查询失败: {verify.get('err')}")
            else:
                print(f"  [verify] 提交后任务列表: total={verify.get('total',0)}, sample={verify.get('sample')}")
                if verify.get('total', 0) == 0:
                    print(f"  [warn] ⚠️ 提交后 8 秒任务列表仍为空，可能 click_run 未真正提交工作流")
        except Exception as e:
            print(f"  [verify] 验证提交异常: {e}")

    async def _get_running_task_ids(self) -> set:
        """获取当前运行中/排队中的任务 ID 集合"""
        try:
            result = await self._page.evaluate("""async () => {
                try {
                    const r = await fetch('/api/user/task/list', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({size: 50, current: 1, taskStatus: ['RUNNING', 'QUEUED'], taskType: ['WORKFLOW', 'WEBAPP']})
                    });
                    const j = await r.json();
                    const records = j?.data?.records || [];
                    return records.map(r => String(r.id));
                } catch(e) { return []; }
            }""")
            return set(result or [])
        except Exception:
            return set()

    async def _get_new_task_status(self, old_ids: set) -> dict:
        """获取新提交的任务状态（不在 old_ids 中的任务）"""
        try:
            result = await self._page.evaluate("""async (oldIds) => {
                try {
                    const r = await fetch('/api/user/task/list', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({size: 50, current: 1, taskStatus: ['RUNNING', 'QUEUED', 'COMPLETED', 'FAILED'], taskType: ['WORKFLOW', 'WEBAPP']})
                    });
                    const j = await r.json();
                    const records = j?.data?.records || [];
                    // 找新任务
                    const newTasks = records.filter(r => !oldIds.includes(String(r.id)));
                    return {
                        total: records.length,
                        newTasks: newTasks.map(r => ({
                            id: r.id,
                            status: r.taskStatus,
                            name: (r.taskName || '').substring(0, 50),
                            error: (r.errorMessage || '').substring(0, 300),
                            outputs: r.taskOutputs ? JSON.stringify(r.taskOutputs).substring(0, 500) : '',
                        })),
                    };
                } catch(e) { return {err: e.message}; }
            }""", list(old_ids))
            return result
        except Exception as e:
            return {'err': str(e)}

    async def _get_preview_info(self, node_id: int) -> Optional[dict]:
        """获取节点当前的预览视频信息（filename + cos_url + type）"""
        if not self._comfy_frame:
            return None
        try:
            result = await self._comfy_frame.evaluate(
                """(nodeId) => {
                    const node = app.graph._nodes_by_id[nodeId];
                    if (!node) return null;
                    const widget = node.widgets.find(w => w.name === 'videopreview');
                    if (!widget || !widget.value) return null;
                    const params = widget.value.params || {};
                    return {
                        filename: params.filename || '',
                        cos_url: params.cos_url || '',
                        type: params.type || '',
                    };
                }""",
                node_id,
            )
            return result or None
        except Exception:
            return None

    async def _get_preview_filename(self, node_id: int) -> Optional[str]:
        """获取节点当前的预览视频 filename"""
        info = await self._get_preview_info(node_id)
        return (info or {}).get("filename") or None

    async def _wait_for_new_task_completion(self, timeout: int) -> dict:
        """等待本次点击运行创建的任务完成，避免抓取中间 PreviewOnly 文件。

        设计说明：当前工作流（动作迁移weeksir_带坏节点）包含节点 600 (FailedTrigger)，
        它会在 285 节点生成完整视频后触发任务标 FAILED。这是设计内的预期失败。
        调用方（run_motion_transfer_rpa）必须用 try/except RuntimeError 包住此方法，
        在 FAILED 时继续调用 wait_for_preview_video(285) 抓取完整视频。
        本方法仅负责「等待任务跑完」这一个职责，不负责判断 285 是否有视频。

        增强：除了等任务状态，同时轮询节点 285 的预览视频。若节点 285 出现新视频
        （filename 与运行前不同且 cos_url 非空）并连续稳定 3 次，则视为成片已生成，
        直接把 URL 存入 self._found_video_url 并返回——避免任务列表 API 异常（如
        cookie 失效、分页遗漏）时永远等不到任务完成状态而抓不到已生成的视频。
        """
        old_ids = getattr(self, "_old_task_ids", set())
        old_filename = getattr(self, "_old_preview_filename", "") or ""
        self._found_video_url = None
        stable_filename = None
        stable_count = 0
        start = time.time()
        last_report = 0
        while time.time() - start < timeout:
            # 1) 检查节点 285 是否已出新视频（与任务状态并行，作为兜底）
            try:
                preview = await self._get_preview_info(285)
                if preview and preview.get("cos_url") and preview.get("filename"):
                    cur = preview["filename"]
                    if cur != old_filename:
                        if cur == stable_filename:
                            stable_count += 1
                        else:
                            stable_filename = cur
                            stable_count = 1
                        if stable_count >= 3:
                            self._found_video_url = preview["cos_url"]
                            elapsed = int(time.time() - start)
                            print(f"  [node285 {elapsed}s] 发现新视频且稳定: {cur} → {preview['cos_url']}")
                            self._report(f"节点 285 已生成完整视频（{elapsed}s）", 94)
                            return {"status": "NODE285_READY", "video_url": preview["cos_url"]}
                    else:
                        stable_filename = None
                        stable_count = 0
            except Exception:
                pass

            # 2) 检查任务状态
            task_info = await self._get_new_task_status(old_ids)
            elapsed = int(time.time() - start)
            wait_pct = 90 + min(4, elapsed // 180)
            if task_info.get("err"):
                if elapsed - last_report >= 30:
                    print(f"  [task-wait {elapsed}s] 查询失败: {task_info.get('err')}")
                    self._report(f"出片中 · 已等待 {elapsed // 60}分{elapsed % 60}秒（任务查询失败）", wait_pct)
                    last_report = elapsed
                await self._page.wait_for_timeout(5000)
                continue
            new_tasks = task_info.get("newTasks", [])
            if new_tasks:
                task = new_tasks[0]
                status = str(task.get("status") or "").upper()
                if elapsed - last_report >= 30 or status in ("COMPLETED", "SUCCESS", "FAILED", "CANCELED", "CANCELLED"):
                    print(f"  [task-wait {elapsed}s] total={task_info.get('total',0)} new={len(new_tasks)} status={status} name={task.get('name','')[:40]} err={task.get('error','')[:80]}")
                    self._report(f"出片中 · {status or 'RUNNING'} · 已等待 {elapsed // 60}分{elapsed % 60}秒", wait_pct)
                    last_report = elapsed
                if status in ("COMPLETED", "SUCCESS", "FAILED", "CANCELED", "CANCELLED"):
                    if status not in ("COMPLETED", "SUCCESS"):
                        raise RuntimeError(f"RunningHub RPA 任务失败: {task.get('error') or status}")
                    return task
            else:
                if elapsed - last_report >= 30:
                    print(f"  [task-wait {elapsed}s] 暂无新任务（total={task_info.get('total',0)}, old_ids={len(old_ids)}）")
                    self._report(f"出片中 · 已等待 {elapsed // 60}分{elapsed % 60}秒（等节点 285）", wait_pct)
                    last_report = elapsed
            await self._page.wait_for_timeout(5000)
        raise TimeoutError(f"等待 RunningHub RPA 任务完成超时（{timeout}s）")

    async def wait_for_preview_video(
        self,
        output_node_id: int,
        timeout: int = 3600,
        poll_interval: int = 5,
    ) -> str:
        """等待预览视频生成并返回视频 URL

        通过对比 click_run 前后的 filename 变化判断新视频生成。

        Args:
            output_node_id: 输出节点 ID（如 502）
            timeout: 超时秒数
            poll_interval: 轮询间隔秒数

        Returns:
            视频 URL（cos_url）
        """
        old_filename = getattr(self, '_old_preview_filename', None) or ''
        #region debug-point rpa-preview-observation
        try:
            async with httpx.AsyncClient(timeout=2) as _dbg_client:
                await _dbg_client.post("http://127.0.0.1:7777/event", json={"sessionId":"rpa-video-one-second","runId":"pre","hypothesisId":"H1-H2","event":"wait_start","data":{"node_id":output_node_id,"old_filename":old_filename,"old_task_ids":list(getattr(self, "_old_task_ids", set()))}})
        except Exception:
            pass
        #endregion
        print(f"[RPA] 等待节点 {output_node_id} 生成新视频（旧: {old_filename or '无'}, 超时 {timeout}s）...")
        start = time.time()
        last_report = 0
        last_queue_check = 0
        no_progress_count = 0  # 连续无任务+无队列的次数，用于早期失败检测

        while time.time() - start < timeout:
            preview = await self._get_preview_info(output_node_id)

            if preview and preview.get('cos_url'):
                cur_file = preview.get('filename')
                # 新视频：filename 与旧的不同
                if cur_file and cur_file != old_filename:
                    elapsed = int(time.time() - start)
                    #region debug-point rpa-preview-observation
                    try:
                        async with httpx.AsyncClient(timeout=2) as _dbg_client:
                            await _dbg_client.post("http://127.0.0.1:7777/event", json={"sessionId":"rpa-video-one-second","runId":"pre","hypothesisId":"H1-H2-H4","event":"preview_selected","data":{"filename":cur_file,"cos_url":preview.get("cos_url",""),"type":preview.get("type",""),"elapsed":elapsed}})
                    except Exception:
                        pass
                    #endregion
                    print(f"  [{elapsed}s] 新视频已生成: {cur_file}")
                    print(f"  URL: {preview['cos_url']}")
                    return preview['cos_url']

            # 每 30s 报告一次
            elapsed = int(time.time() - start)
            if elapsed - last_report >= 30:
                print(f"  [{elapsed}s] 等待中...")
                grab_pct = 94 + min(4, elapsed // 180)
                self._report(
                    f"抓取节点 285 视频 · 已等待 {elapsed // 60}分{elapsed % 60}秒",
                    grab_pct,
                )
                last_report = elapsed

            # 每 60s 检查 ComfyUI 队列状态
            if elapsed - last_queue_check >= 60:
                last_queue_check = elapsed
                try:
                    queue_info = await self._comfy_frame.evaluate(
                        """async () => {
                            try {
                                const r = await fetch(api.apiURL('/queue'));
                                const j = await r.json();
                                return {
                                    queue_running: j.queue_running ? j.queue_running.length : 0,
                                    queue_pending: j.queue_pending ? j.queue_pending.length : 0,
                                };
                            } catch(e) { return {err: e.message}; }
                        }"""
                    )
                    if queue_info and not queue_info.get('err'):
                        qr = queue_info.get('queue_running', 0)
                        qp = queue_info.get('queue_pending', 0)
                        print(f"  [{elapsed}s] ComfyUI 队列: running={qr}, pending={qp}")
                        # 早期失败检测：如果 elapsed>120s 且队列空 + 任务列表也空
                        # → 工作流根本没提交成功，再等也是浪费时间
                        if elapsed > 120 and qr == 0 and qp == 0:
                            no_progress_count += 1
                            if no_progress_count >= 3:
                                # 再确认一次任务列表
                                try:
                                    verify = await self._page.evaluate("""async () => {
                                        try {
                                            const r = await fetch('/api/user/task/list', {
                                                method: 'POST', headers: {'Content-Type': 'application/json'},
                                                body: JSON.stringify({size: 50, current: 1, taskStatus: ['RUNNING', 'QUEUED', 'COMPLETED', 'FAILED'], taskType: ['WORKFLOW', 'WEBAPP']})
                                            });
                                            const j = await r.json();
                                            return {total: (j?.data?.records || []).length};
                                        } catch(e) { return {err: e.message}; }
                                    }""")
                                    if not verify.get('err') and verify.get('total', 0) == 0:
                                        # 截图保留证据
                                        try:
                                            await self._page.screenshot(path=f"rpa_no_submit_{int(time.time())}.png", full_page=True)
                                        except Exception:
                                            pass
                                        raise RuntimeError(
                                            f"工作流未提交成功：click_run 后 {elapsed}s 队列和任务列表均为空（可能运行按钮没点中或登录态失效）"
                                        )
                                except RuntimeError:
                                    raise
                                except Exception:
                                    pass
                        else:
                            no_progress_count = 0
                    elif queue_info and queue_info.get('err'):
                        print(f"  [{elapsed}s] 队列查询失败: {queue_info.get('err')}")
                except Exception as e:
                    print(f"  [{elapsed}s] 队列查询异常: {e}")

            await self._page.wait_for_timeout(poll_interval * 1000)

        # 超时前截图，便于诊断
        try:
            screenshot_path = f"rpa_timeout_screenshot_{int(time.time())}.png"
            await self._page.screenshot(path=screenshot_path, full_page=True)
            print(f"  超时截图: {screenshot_path}")
        except Exception:
            pass

        raise TimeoutError(f"等待节点 {output_node_id} 生成视频超时（{timeout}s）")

    async def download_video(self, video_url: str, output_path: str):
        """下载视频到本地"""
        print(f"[RPA] 下载: {video_url}")
        print(f"  → {output_path}")
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        async with httpx.AsyncClient(timeout=300, follow_redirects=True) as client:
            resp = await client.get(video_url)
            resp.raise_for_status()
            Path(output_path).write_bytes(resp.content)
        size_kb = len(resp.content) // 1024
        #region debug-point rpa-video-observation
        try:
            async with httpx.AsyncClient(timeout=2) as _dbg_client:
                await _dbg_client.post("http://127.0.0.1:7777/event", json={"sessionId":"rpa-video-one-second","runId":"pre","hypothesisId":"H1-H2","event":"download_complete","data":{"size_bytes":len(resp.content),"output_path":output_path}})
        except Exception:
            pass
        #endregion
        print(f"  完成（{size_kb}KB）")


async def run_motion_transfer_rpa(
    image_path: str,
    video_path: str,
    output_path: str,
    timeout: int = 3600,
    workflow_url: Optional[str] = None,
    engine: str = "plus",
    phone: str = "",
    password: str = "",
    profile_dir: str = "",
    headless: Optional[bool] = None,
    on_progress: Optional[Callable[[str, int], None]] = None,
) -> str:
    """通过 RPA 运行动作迁移工作流

    Args:
        image_path: 参考图片本地路径
        video_path: 参考视频本地路径
        output_path: 输出视频本地路径
        timeout: 等待超时秒数
        workflow_url: 工作流 URL（可选，默认用本模块顶部 WORKFLOW_URL）
        engine: 运行引擎 plus/lite/standard，对应 RunningHub 主页面的 Ultra/Lite/Standard 按钮
        phone: 账号手机号（用于自动登录，账号池模式必填）
        password: 账号密码（用于自动登录，账号池模式必填）
        profile_dir: profile 目录路径（每账号独立；空=用账号池默认目录）
        headless: 是否无头（空=用 config.RPA_HEADLESS）
        on_progress: 进度回调 (stage, percent)，写入前端 SSE 进度条

    Returns:
        输出视频本地路径
    """
    rpa = RunningHubRPA(
        headless=headless,
        phone=phone,
        password=password,
        profile_dir=profile_dir,
    )
    rpa.on_progress = on_progress
    try:
        rpa._report("启动无头浏览器", 80)
        await rpa.connect()
        rpa._report("打开工作流 / 等待 ComfyUI", 82)
        # 登录态过期自愈：上传接口报 token 失效时，重新登录后整段重试一次
        session_retried = False
        while True:
            try:
                await rpa.open_workflow(workflow_url or "")
                rpa._report("上传参考图 节点299", 85)
                await rpa.upload_to_node(299, image_path, "image")
                rpa._report("上传模仿视频 节点275", 86)
                await rpa.upload_to_node(275, video_path, "video")
                rpa._report("点击运行", 88)
                await rpa.click_run(engine=engine)
                break
            except SessionStaleError as e:
                if session_retried:
                    raise RuntimeError(f"RunningHub 登录态过期，自动重新登录后仍失败: {e}") from e
                session_retried = True
                print(f"[RPA] 登录态已过期（{e}），自动重新登录后重试")
                rpa._report("登录态过期，重新登录", 83)
                await rpa.reset_session_and_relogin()
        rpa._report("等待出片（节点 285）", 90)
        # 等本次 RunningHub 任务完成（避免抓取中间 PreviewOnly 文件）。
        # 注意：该工作流包含节点 600 (FailedTrigger)，任务最终会标 FAILED —— 这是设计内行为。
        # 节点 285 (save_output=True) 在任务报错前已生成完整视频，
        # 因此 FAILED 时不能直接抛错，必须继续从 285 预览抓取视频。
        #
        # 关键：本段必须 catch 所有异常（不只是 RuntimeError），因为：
        # - _wait_for_new_task_completion 超时抛 TimeoutError（不是 RuntimeError 子类）
        # - 任务列表接口 /api/user/task/list 对部分账号可能返回 total=0（cookie 路径问题）
        # - 即使任务状态查询失败/超时，285 节点可能已经生成了完整视频，必须继续抓取
        task_failed_with_err = None
        try:
            await rpa._wait_for_new_task_completion(timeout=timeout)
        except Exception as e:
            # 任何异常（RuntimeError=FAILED 触发 / TimeoutError=查询超时 / 其它）都继续往下抓视频
            task_failed_with_err = str(e)
            print(f"[RPA] 任务状态查询异常（继续从 285 抓视频）: {task_failed_with_err}")
            print(f"[RPA] 继续从节点 285 预览抓取完整视频...")
            rpa._report("任务查询异常，继续抓节点 285", 94)

        # 若在等任务状态时已从节点 285 抓到新视频（任务列表 API 异常兜底），直接用
        video_url = getattr(rpa, "_found_video_url", None)
        if not video_url:
            rpa._report("抓取节点 285 完整视频", 94)
            video_url = await rpa.wait_for_preview_video(285, timeout=timeout)
        else:
            rpa._report("使用节点 285 已发现的视频", 94)
        rpa._report("下载成片", 99)
        await rpa.download_video(video_url, output_path)
        rpa._report("RPA 完成", 100)
        return output_path
    finally:
        try:
            await rpa.close()
        except Exception as close_error:
            print(f"[RPA] 关闭浏览器失败: {close_error!r}")


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 3:
        print("用法: python runninghub_rpa_client.py <image_path> <video_path> [output_path]")
        print()
        print("示例:")
        print("  python runninghub_rpa_client.py uploads/test.png uploads/test.mp4 outputs/motion.mp4")
        sys.exit(1)

    img = sys.argv[1]
    vid = sys.argv[2]
    out = sys.argv[3] if len(sys.argv) > 3 else "outputs/motion_transfer_rpa.mp4"

    result = asyncio.run(run_motion_transfer_rpa(img, vid, out, timeout=3600))
    print(f"\n✓ 完成: {result}")
