"""RunningHub 无头浏览器登录

用 Playwright persistent context 把登录态写进每账号独立的 profile 目录。
登录成功后从页面/接口导出 JWT，写入 rh_<phone>_token.json，给复制工作流用。

下次同一 profile 仍有效时，不再填密码。
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Optional

from playwright.async_api import async_playwright

WF_URL = "https://www.runninghub.cn/workflow/2106377827540103169?source=workspace"
# 官方邀请注册链接（首次引导第 1 页必须用它打开，注册自动带上邀请码）
INVITE_URL = "https://www.runninghub.cn?inviteCode=jgzvdb2g"
BASE_DIR = Path(__file__).resolve().parent.parent.parent

_browser_lock: Optional[asyncio.Lock] = None


def _get_browser_lock() -> asyncio.Lock:
    global _browser_lock
    if _browser_lock is None:
        _browser_lock = asyncio.Lock()
    return _browser_lock


def _default_token_cache(phone: str) -> str:
    return str(BASE_DIR / "rh_profiles" / f"rh_{phone}_token.json")


def _is_jwt(value: str) -> bool:
    s = (value or "").strip()
    if s.lower().startswith("bearer "):
        s = s[7:].strip()
    return s.startswith("eyJ") and s.count(".") >= 2


def _as_jwt(value: str) -> str:
    s = (value or "").strip()
    if s.lower().startswith("bearer "):
        s = s[7:].strip()
    return s if _is_jwt(s) else ""


def _pick_jwt(obj) -> str:
    if isinstance(obj, str):
        found = _as_jwt(obj)
        if found:
            return found
        try:
            return _pick_jwt(json.loads(obj))
        except Exception:
            return ""
    if isinstance(obj, dict):
        for key in ("access_token", "accessToken", "token", "Authorization", "authorization"):
            found = _as_jwt(str(obj.get(key) or ""))
            if found:
                return found
        for val in obj.values():
            found = _pick_jwt(val)
            if found:
                return found
    if isinstance(obj, list):
        for val in obj:
            found = _pick_jwt(val)
            if found:
                return found
    return ""


def _jwt_exp_ms(access_token: str) -> int:
    try:
        payload = access_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        import base64
        data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
        exp = int(data.get("exp") or 0)
        if exp > 10_000_000_000:
            return exp
        if exp > 0:
            return exp * 1000
    except Exception:
        pass
    return int(time.time() * 1000) + 2592000000


def save_token_cache(
    phone: str,
    access_token: str,
    refresh_token: str = "",
    expire_in: int = 2592000000,
    identify: str = "",
    token_cache: str = "",
) -> str:
    access_token = _as_jwt(access_token)
    if not access_token:
        return ""
    path = Path(token_cache or _default_token_cache(phone))
    path.parent.mkdir(parents=True, exist_ok=True)
    exp_ms = _jwt_exp_ms(access_token)
    now_ms = int(time.time() * 1000)
    data = {
        "username": phone,
        "token": {
            "access_token": access_token,
            "refresh_token": refresh_token or access_token,
            "expire_in": int(expire_in or max(exp_ms - now_ms, 0) or 2592000000),
            "expires_at_ms": exp_ms,
            "identify": identify,
            "first_login": False,
        },
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[AutoLogin] 已保存 token: {path}")
    return str(path)


def _token_from_login_body(body: dict) -> dict:
    if not isinstance(body, dict):
        return {}
    data = body.get("data") if isinstance(body.get("data"), dict) else body
    access = _as_jwt(str(data.get("access_token") or data.get("accessToken") or ""))
    if not access:
        access = _pick_jwt(body)
    if not access:
        return {}
    return {
        "access_token": access,
        "refresh_token": str(data.get("refresh_token") or data.get("refreshToken") or ""),
        "expire_in": int(data.get("expire_in") or data.get("expireIn") or 2592000000),
        "identify": str(data.get("identify") or ""),
    }


async def _dump_web_storage(page) -> dict:
    try:
        return await page.evaluate(
            """() => {
                const dump = (store) => {
                    const out = {};
                    try {
                        for (let i = 0; i < store.length; i++) {
                            const k = store.key(i);
                            out[k] = store.getItem(k);
                        }
                    } catch (e) {}
                    return out;
                };
                return { localStorage: dump(localStorage), sessionStorage: dump(sessionStorage) };
            }"""
        )
    except Exception:
        return {}


async def _extract_token_from_page(page, captured_auth: list, login_body: dict, cookies: Optional[list] = None) -> dict:
    found = _token_from_login_body(login_body)
    if found:
        return found
    for raw in reversed(captured_auth):
        jwt = _as_jwt(raw)
        if jwt:
            return {"access_token": jwt, "refresh_token": "", "expire_in": 2592000000, "identify": ""}
    for cookie in cookies or []:
        jwt = _as_jwt(str(cookie.get("value") or ""))
        if jwt:
            return {"access_token": jwt, "refresh_token": "", "expire_in": 2592000000, "identify": ""}
    storage = await _dump_web_storage(page)
    jwt = _pick_jwt(storage)
    if jwt:
        return {"access_token": jwt, "refresh_token": "", "expire_in": 2592000000, "identify": ""}
    return {}


async def browser_verify(
    phone: str,
    password: str,
    profile_dir: str,
    headless: bool = False,
    timeout_seconds: int = 60,
    token_cache: Optional[str] = None,
    force: bool = False,
) -> dict:
    """无头浏览器校验。profile 已登录则直接导出 token，否则填密码登录并保存会话。

    force=True：跳过“会话看起来仍有效”的复用分支——旧会话可能已被平台作废
    （页面不显示登录按钮，但接口报 token 已过期），先清掉旧登录态强制走密码登录。
    """
    result = {
        "ok": False,
        "reused_session": False,
        "token_saved": False,
        "error": "",
        "kind": "",
    }
    token_cache = token_cache or _default_token_cache(phone)
    Path(profile_dir).mkdir(parents=True, exist_ok=True)
    print(f"[AutoLogin] 无头浏览器校验 {phone}（profile: {profile_dir}）")

    async with _get_browser_lock():
        pw = await async_playwright().start()
        ctx = None
        try:
            ctx = await pw.chromium.launch_persistent_context(
                user_data_dir=profile_dir,
                headless=headless,
                viewport={"width": 1440, "height": 1000},
                locale="zh-CN",
                args=["--disable-blink-features=AutomationControlled"],
            )
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()

            captured_auth: list[str] = []
            login_body: dict = {}

            def _on_request(request):
                auth = request.headers.get("authorization") or request.headers.get("Authorization") or ""
                if _as_jwt(auth):
                    captured_auth.append(auth)

            async def _on_response(response):
                url = response.url or ""
                if "pwdLogin" not in url and "smsLogin" not in url:
                    return
                try:
                    body = await response.json()
                except Exception:
                    return
                if isinstance(body, dict):
                    login_body.update(body)

            page.on("request", _on_request)
            page.on("response", _on_response)

            print(f"[AutoLogin] 打开工作流: {WF_URL}")
            try:
                await page.goto(WF_URL, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                print(f"  [warn] 打开页面超时: {str(e)[:80]}")
            await page.wait_for_timeout(4000)

            if force:
                # 强制重登：清掉可能已失效的旧登录态（cookie + localStorage），
                # 否则页面“看起来已登录”会误复用过期会话
                print("[AutoLogin] 强制重新登录：清除旧登录态")
                try:
                    await ctx.clear_cookies()
                    await page.evaluate(
                        "() => { try { localStorage.clear(); sessionStorage.clear(); } catch (e) {} }"
                    )
                except Exception as e:
                    print(f"  [warn] 清除旧登录态异常: {e}")
                try:
                    await page.goto(WF_URL, wait_until="domcontentloaded", timeout=30000)
                except Exception as e:
                    print(f"  [warn] 打开页面超时: {str(e)[:80]}")
                await page.wait_for_timeout(3000)

            login_btn = page.locator("button:has-text('登 录'), button:has-text('登录')").first
            try:
                need_login = await login_btn.is_visible(timeout=2500)
            except Exception:
                need_login = False

            if not need_login:
                print(f"[AutoLogin] {phone} 浏览器会话仍有效，无需重新登录")
                await page.wait_for_timeout(2000)
                tok = await _extract_token_from_page(
                    page, captured_auth, login_body, await ctx.cookies()
                )
                if tok.get("access_token"):
                    save_token_cache(phone, token_cache=token_cache, **tok)
                    result["token_saved"] = True
                result["reused_session"] = True
                result["ok"] = bool(result["token_saved"])
                if not result["token_saved"]:
                    result["error"] = "浏览器已登录，但未能导出 token"
                    result["kind"] = "other"
                    print(f"[AutoLogin] {result['error']}")
                return result

            print("[AutoLogin] 点登录按钮...")
            await login_btn.click()
            await page.wait_for_timeout(1500)

            # RunningHub V3 登录弹窗默认微信扫码登录，账密表单需点「密码登录」切换后才出现
            account_input = page.locator("#form_item_account")
            try:
                await account_input.wait_for(state="visible", timeout=6000)
            except Exception:
                pwd_switch = page.locator("div.action-btn", has_text="密码登录").first
                try:
                    if await pwd_switch.is_visible(timeout=3000):
                        print("[AutoLogin] 切换到密码登录...")
                        await pwd_switch.click()
                        await page.wait_for_timeout(1000)
                except Exception:
                    pass
            try:
                await account_input.wait_for(state="visible", timeout=15000)
            except Exception as e:
                result["error"] = f"登录表单未出现: {e}"
                result["kind"] = "other"
                print(f"  [error] {result['error']}")
                return result

            await page.locator("#form_item_account").fill(phone)
            await page.wait_for_timeout(300)
            await page.locator("#form_item_password").fill(password)
            await page.wait_for_timeout(300)

            checkbox = page.locator("input[type='checkbox']").first
            try:
                if await checkbox.is_visible(timeout=2000) and not await checkbox.is_checked():
                    await checkbox.check()
            except Exception:
                pass

            print("[AutoLogin] 点立即登录...")
            await page.locator("button:has-text('立即登录')").first.click()

            login_ok = False
            for i in range(timeout_seconds):
                await page.wait_for_timeout(1000)
                form_count = await page.locator("#form_item_account").count()
                if form_count == 0:
                    print(f"  [{i+1}s] 登录表单已关闭")
                    login_ok = True
                    break
                err_els = await page.evaluate(
                    """() => {
                        const errs = document.querySelectorAll('.ant-message-error, [class*="error-msg"], .ant-form-item-explain-error');
                        return Array.from(errs).map(e => (e.textContent || '').trim()).filter(t => t);
                    }"""
                )
                api_msg = str(login_body.get("msg") or login_body.get("message") or "")
                api_code = login_body.get("code")
                blob = " ".join(err_els + [api_msg, str(api_code)])
                if "USER_PWD_LOGIN_LOCKED" in blob.upper() or "锁定" in blob:
                    result["error"] = "密码登录被锁定（浏览器）。请等待后重试，或用短信登录一次解锁。"
                    result["kind"] = "locked"
                    print(f"[AutoLogin] {result['error']}")
                    return result
                for err in err_els:
                    print(f"  [{i+1}s] 错误: {err}")
                    if any(k in err for k in ("密码错误", "账号或密码", "不存在", "密码不正确")):
                        result["error"] = f"账号或密码错误: {err}"
                        result["kind"] = "bad_credentials"
                        return result
                if api_code not in (None, 0) and api_code != "0":
                    if api_code == 126 or "LOCKED" in api_msg.upper():
                        result["error"] = f"[{api_code}] {api_msg}"
                        result["kind"] = "locked"
                        return result
                    if "密码" in api_msg or "USER_PWD" in api_msg.upper():
                        result["error"] = f"账号或密码错误: {api_msg}"
                        result["kind"] = "bad_credentials"
                        return result

            if not login_ok:
                result["error"] = f"登录超时（{timeout_seconds}s）"
                result["kind"] = "other"
                return result

            await page.goto(WF_URL, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(4000)
            try:
                still_visible = await page.locator("button:has-text('登 录')").first.is_visible(timeout=3000)
            except Exception:
                still_visible = False
            if still_visible:
                result["error"] = "验证失败：登录按钮仍可见"
                result["kind"] = "other"
                return result

            tok = await _extract_token_from_page(
                page, captured_auth, login_body, await ctx.cookies()
            )
            if tok.get("access_token"):
                save_token_cache(phone, token_cache=token_cache, **tok)
                result["token_saved"] = True
            result["ok"] = True
            print(f"[AutoLogin] {phone} 登录成功（token_saved={result['token_saved']}）")
            if not result["token_saved"]:
                result["error"] = "浏览器登录成功，但未能导出 token"
            return result
        except Exception as e:
            result["error"] = str(e)[:200]
            result["kind"] = "other"
            print(f"[AutoLogin] 异常: {e}")
            return result
        finally:
            if ctx:
                try:
                    await ctx.close()
                except Exception:
                    pass
            await pw.stop()


async def auto_login(
    phone: str,
    password: str,
    profile_dir: str,
    headless: bool = False,
    timeout_seconds: int = 60,
    token_cache: Optional[str] = None,
    force: bool = False,
) -> bool:
    """兼容旧调用：返回是否登录成功。默认无头，并把 JWT 写入 token 缓存。"""
    result = await browser_verify(
        phone,
        password,
        profile_dir,
        headless=headless,
        timeout_seconds=timeout_seconds,
        token_cache=token_cache,
        force=force,
    )
    return bool(result.get("ok"))


async def cookie_login(
    phone: str,
    profile_dir: str,
    token_cache: Optional[str] = None,
    start_url: str = INVITE_URL,
    timeout_seconds: int = 600,
    headless: bool = False,
    force: bool = False,
) -> dict:
    """Cookie 记录式登录：打开浏览器让客户自己注册/登录，脚本只负责记录登录 Cookie。

    - 会话仍有效：页面加载即产生带 JWT 的请求，几秒内自动判定成功，无需人工操作
    - 未登录/会话失效：浏览器停在页面上等待客户注册登录，登录成功后自动记录并返回
    - force=True：先清掉旧登录态再等待（旧会话可能已被平台作废，避免误复用过期 token）
    登录态保存两份：浏览器持久 profile（cookie）+ rh_<phone>_token.json（JWT）。
    """
    result = {"ok": False, "token_saved": False, "error": "", "reused_session": False}
    token_cache = token_cache or _default_token_cache(phone)
    Path(profile_dir).mkdir(parents=True, exist_ok=True)
    print(f"[CookieLogin] 打开浏览器等待登录 {phone}: {start_url}")

    async with _get_browser_lock():
        pw = await async_playwright().start()
        ctx = None
        try:
            ctx = await pw.chromium.launch_persistent_context(
                user_data_dir=profile_dir,
                headless=headless,
                viewport={"width": 1440, "height": 1000},
                locale="zh-CN",
                args=["--disable-blink-features=AutomationControlled"],
            )
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            captured_auth: list[str] = []

            def _on_request(request):
                auth = request.headers.get("authorization") or request.headers.get("Authorization") or ""
                if _as_jwt(auth):
                    captured_auth.append(auth)

            page.on("request", _on_request)

            try:
                await page.goto(start_url, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                print(f"  [warn] 打开页面超时: {str(e)[:80]}")
            await page.wait_for_timeout(3000)

            if force:
                # 强制重登：清掉可能已失效的旧登录态，否则页面/存储里残留的旧 JWT 会被误当成已登录
                print("[CookieLogin] 清除旧登录态后等待重新登录")
                try:
                    await ctx.clear_cookies()
                    await page.evaluate(
                        "() => { try { localStorage.clear(); sessionStorage.clear(); } catch (e) {} }"
                    )
                except Exception as e:
                    print(f"  [warn] 清除旧登录态异常: {e}")
                captured_auth.clear()
                try:
                    await page.goto(start_url, wait_until="domcontentloaded", timeout=30000)
                except Exception as e:
                    print(f"  [warn] 打开页面超时: {str(e)[:80]}")
                await page.wait_for_timeout(2000)

            for i in range(timeout_seconds):
                tok = await _extract_token_from_page(page, captured_auth, {}, await ctx.cookies())
                if tok.get("access_token"):
                    save_token_cache(phone, token_cache=token_cache, **tok)
                    result["ok"] = True
                    result["token_saved"] = True
                    print(f"[CookieLogin] {phone} 登录成功，已记录 Cookie/token（{i}s）")
                    return result
                await page.wait_for_timeout(1000)
            result["error"] = f"等待登录超时（{timeout_seconds}s），未检测到登录 Cookie"
            print(f"[CookieLogin] {result['error']}")
            return result
        except Exception as e:
            result["error"] = str(e)[:200]
            print(f"[CookieLogin] 异常: {e}")
            return result
        finally:
            if ctx:
                try:
                    await ctx.close()
                except Exception:
                    pass
            await pw.stop()


def _chromium_time_us() -> int:
    """Chromium cookies.expires_utc：自 1601-01-01 起的微秒数"""
    return int((time.time() + 11644473600) * 1_000_000)


def _profile_cookie_alive(profile_dir: Path) -> bool:
    """profile 的 Cookies 库里是否还有未过期的 runninghub.cn Cookie（只读，不需要解密值）。"""
    for cand in ("Default/Network/Cookies", "Default/Cookies", "Network/Cookies", "Cookies"):
        db = profile_dir / cand
        if not db.is_file():
            continue
        try:
            import sqlite3
            conn = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True)
            try:
                row = conn.execute(
                    "SELECT COUNT(*) FROM cookies WHERE host_key LIKE '%runninghub.cn'"
                    " AND (expires_utc = 0 OR expires_utc > ?)",
                    (_chromium_time_us(),),
                ).fetchone()
            finally:
                conn.close()
            if row and row[0] > 0:
                return True
        except Exception:
            continue
    return False


def cookies_alive(phone: str = "") -> bool:
    """RunningHub 登录 Cookie 是否还在（首次引导弹窗的出现标准：没了就弹）。

    任一账号的浏览器 profile 里有未过期 runninghub.cn Cookie，或 cookie 登录时
    导出的 token 缓存未过期 → 视为登录态还在。
    """
    root = BASE_DIR / "rh_profiles"
    if not root.exists():
        return False
    if phone:
        dirs = [root / f"rh_{phone}"]
        token_files = list(root.glob(f"rh_{phone}_token.json"))
    else:
        dirs = [p for p in root.glob("rh_*") if p.is_dir()]
        token_files = list(root.glob("rh_*_token.json"))
    for d in dirs:
        if _profile_cookie_alive(d):
            return True
    for p in token_files:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            token = data.get("token") or {}
            if not token.get("access_token"):
                continue
            exp = int(token.get("expires_at_ms") or 0)
            if not exp or exp > int(time.time() * 1000) + 60_000:
                return True
        except Exception:
            continue
    return False
