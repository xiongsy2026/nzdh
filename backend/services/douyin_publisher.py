"""抖音视频发布 RPA 客户端。

依赖 douyin_downloader 保存的 storage_state.json 登录态。
通过 Playwright 自动化 creator.douyin.com 上传视频。

设计要点（防失效）：
1. 慢速打字（80-150ms 抖动），模拟真人输入
2. 步骤间随机停顿 1-3s
3. 标签真实化：逐个输入 #标签 → 等联想弹窗 → 回车确认，避免死文本
4. cookie 持久化到 douyin_storage_state.json
"""
import asyncio
import random
from pathlib import Path
from typing import Optional

from config import settings

UPLOAD_DIR = Path(settings.UPLOAD_DIR)
DOUYIN_STORAGE_STATE_PATH = UPLOAD_DIR / "douyin_storage_state.json"
DOUYIN_NICKNAME_PATH = UPLOAD_DIR / "douyin_nickname.txt"

CREATOR_URL = "https://creator.douyin.com/creator-micro/home"
UPLOAD_URL = "https://creator.douyin.com/creator-micro/content/upload"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36",
}

STEALTH_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh'] });
Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
"""


async def _human_pause(min_s: float = 1.0, max_s: float = 3.0) -> None:
    """拟人停顿：随机等待 min_s ~ max_s 秒"""
    await asyncio.sleep(random.uniform(min_s, max_s))


async def _human_type(page, text: str, min_delay: int = 80, max_delay: int = 150) -> None:
    """拟人逐字输入：每个字符 80-150ms 随机延迟。

    增强拟人：
    - 每隔 5-12 个字符有 0.4-1.2s 的「思考停顿」
    - 2% 概率打错字，立即退格修正（模拟手抖）
    - 标点符号后停顿更久
    """
    since_last_pause = 0
    for ch in text:
        # 2% 概率打错字再退格
        if random.random() < 0.02 and ch not in ' \n':
            wrong = chr(ord(ch) + random.randint(1, 3))
            await page.keyboard.type(wrong)
            await asyncio.sleep(random.uniform(120, 220) / 1000)
            await page.keyboard.press("Backspace")
            await asyncio.sleep(random.uniform(100, 200) / 1000)
        await page.keyboard.type(ch)
        # 标点后停顿更久
        if ch in "，。！？,.!?;:":
            await asyncio.sleep(random.uniform(200, 450) / 1000)
            since_last_pause = 0
        else:
            await asyncio.sleep(random.uniform(min_delay, max_delay) / 1000)
            since_last_pause += 1
        # 每隔 5-12 字符来一次思考停顿
        if since_last_pause >= random.randint(5, 12):
            await asyncio.sleep(random.uniform(400, 1200) / 1000)
            since_last_pause = 0


async def ensure_login_for_publish(status_callback=None, force_relogin: bool = False) -> dict:
    """确保抖音创作者中心登录态可用。

    返回 {"storage_state": str, "nickname": str} 或 None。
    登录在 https://creator.douyin.com/ 直接扫码（不走 www.douyin.com 消费端登录）。

    优化：发布时跳过 headless 验证（耗时长），直接复用已保存的 cookie。
    真正的登录态检查在发布流程中通过页面跳转判断，失效再返回错误。
    """
    if force_relogin:
        # 强制重新登录
        return await _creator_login(status_callback=status_callback)

    if DOUYIN_STORAGE_STATE_PATH.exists():
        # 直接复用已保存的 cookie，不预先验证（发布时会自然检测）
        nickname = ""
        try:
            if DOUYIN_NICKNAME_PATH.exists():
                nickname = DOUYIN_NICKNAME_PATH.read_text(encoding="utf-8").strip()
        except Exception:
            pass
        if status_callback:
            await status_callback(f"使用已保存登录态{('（' + nickname + '）') if nickname else ''}，开始发布")
        return {"storage_state": str(DOUYIN_STORAGE_STATE_PATH), "nickname": nickname}

    # 没有保存过登录态，需要扫码
    if status_callback:
        await status_callback("未检测到登录态，需要扫码登录")
    return await _creator_login(status_callback=status_callback)


async def _verify_creator_login(storage_state_path: Path) -> Optional[dict]:
    """验证已保存的 cookie 是否能访问创作者中心。返回 {"nickname": str} 或 None。"""
    from playwright.async_api import async_playwright
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
            )
            context = await browser.new_context(
                user_agent=HEADERS["User-Agent"],
                viewport={"width": 1440, "height": 900},
                locale="zh-CN",
                storage_state=str(storage_state_path),
            )
            page = await context.new_page()
            try:
                await page.goto(CREATOR_URL, wait_until="domcontentloaded", timeout=20000)
                # 等页面完全渲染（创作者中心是 SPA）
                try:
                    await page.wait_for_load_state("networkidle", timeout=10000)
                except Exception:
                    pass
                await page.wait_for_timeout(2500)
                url = page.url
                # 综合判定登录态：
                # 1. URL 含 /login 或 passport -> 未登录
                # 2. 页面文本含「扫码登录」「我是创作者」「我是MCN机构」等登录引导词 -> 未登录
                # 3. cookie 中无 sessionid/sessionid_ss -> 未登录
                body_text = await page.evaluate("() => document.body ? document.body.innerText.slice(0, 3000) : ''")
                is_login_page = "/login" in url or "passport" in url
                has_login_text = any(kw in body_text for kw in ["扫码登录", "我是创作者", "我是MCN机构", "密码登录", "获取验证码登录"])
                # cookie 检查
                cookies = await context.cookies()
                cookie_names = {c.get("name", "") for c in cookies}
                has_session = bool({"sessionid", "sessionid_ss", "sid_tt"} & cookie_names)
                if is_login_page or has_login_text or not has_session:
                    await browser.close()
                    return None
                # 已登录，提取昵称
                nickname = await _extract_nickname(page)
                await browser.close()
                return {"nickname": nickname or ""}
            except Exception:
                await browser.close()
                return None
    except Exception:
        return None


async def _extract_nickname(page) -> str:
    """从创作者中心页面提取登录用户的抖音昵称。"""
    try:
        result = await page.evaluate("""() => {
            // 多种可能的昵称元素选择器（按优先级）
            const selectors = [
                // 创作者中心常见的用户信息容器
                '[data-e2e="user-info"] [class*="name"]',
                '[data-e2e="user-info"] [class*="nick"]',
                '[data-e2e="user-name"]',
                '[data-e2e="user-avatar"]',
                '.user-info [class*="name"]',
                '.user-info [class*="nick"]',
                // 抖音通用昵称类名
                '.nickname',
                '.user-nickname',
                'span[class*="nickname"]',
                'div[class*="nickname"]',
                'div[class*="user-name"]',
                'div[class*="userName"]',
                'div[class*="account-name"]',
                'div[class*="accountName"]',
                // header/侧边栏
                'header [class*="name"]',
                '.side-bar [class*="name"]',
                '.sidebar [class*="name"]',
                '[class*="account-info"] [class*="name"]',
                '[class*="user-card"] [class*="name"]',
                '[class*="userCard"] [class*="name"]',
                '[class*="header-user"]',
                '[class*="headerUser"]',
                '[class*="avatar-wrapper"]',
                '[class*="avatarWrapper"]',
                // 头像元素（title/alt 通常含昵称）
                'img[data-e2e="user-avatar"]',
                'img[class*="avatar"]',
                'img[src*="avatar"]',
            ];
            let found = '';
            const matched = [];
            for (const sel of selectors) {
                const els = document.querySelectorAll(sel);
                els.forEach(el => {
                    // 优先取 title/alt/data 属性
                    const attr = el.getAttribute('title') || el.getAttribute('alt') || el.getAttribute('data-nickname') || el.getAttribute('data-name') || '';
                    if (attr && attr.length > 0 && attr.length < 60 && !/^(avatar|头像|user)/i.test(attr)) {
                        matched.push({sel, type: 'attr', value: attr});
                    }
                    // 再取文本
                    const txt = (el.innerText || el.textContent || '').trim();
                    if (txt && txt.length > 0 && txt.length < 60 && !/^(登录|扫码登录|首页|创作|消息|通知)$/.test(txt)) {
                        matched.push({sel, type: 'text', value: txt});
                    }
                });
                if (matched.length > 0) {
                    found = matched[0].value;
                    break;
                }
            }
            // 兜底1：从 body 文本匹配多种昵称格式
            if (!found) {
                const bodyText = document.body ? document.body.innerText.slice(0, 5000) : '';
                // 「你好，XXX」「XXX的创作者中心」
                const m1 = bodyText.match(/你好[，,]\\s*([^\\s，,。\\n]{1,30})/);
                if (m1) found = m1[1];
                if (!found) {
                    const m2 = bodyText.match(/([^\\s，,。\\n]{2,30})的创作者中心/);
                    if (m2) found = m2[1];
                }
                // 抖音号：xxx → 抖音号上面那行通常是昵称
                // 实际页面结构：第一行昵称、第二行「抖音号：xxx」、第三行描述
                if (!found) {
                    const lines = bodyText.split(/\\n/).map(s => s.trim()).filter(s => s);
                    for (let i = 0; i < lines.length; i++) {
                        if (/^抖音号[：:]/.test(lines[i]) && i > 0) {
                            // 上一行是昵称（避免取到「网址」「抖音」等导航词）
                            const cand = lines[i - 1];
                            if (cand && cand.length >= 2 && cand.length <= 30 &&
                                !/^(首页|活动|内容|互动|数据|变现|创作|通知|网址|抖音|发布|管理|中心|服务|平台)$/.test(cand)) {
                                found = cand;
                                break;
                            }
                        }
                    }
                }
                if (!found) {
                    // 兜底2：body 文本前 500 字（用于调试）
                    return JSON.stringify({
                        found: '',
                        debug: {
                            url: location.href,
                            title: document.title,
                            bodyHead: bodyText.slice(0, 500),
                            matchedCount: 0,
                        }
                    });
                }
            }
            return JSON.stringify({found: found, matchedCount: matched.length});
        }""")
        # result 是 JSON 字符串
        import json as _json
        try:
            data = _json.loads(result)
            nick = data.get("found", "") or ""
            if nick:
                return nick.strip()
            # 没找到，打印调试信息
            debug = data.get("debug", {})
            if debug:
                print(f"[nickname-debug] url={debug.get('url')}, title={debug.get('title')}, bodyHead={debug.get('bodyHead', '')[:200]}")
            return ""
        except Exception:
            return (result or "").strip()
    except Exception as e:
        print(f"[nickname-debug] extract error: {e}")
        return ""


async def _creator_login(status_callback=None) -> Optional[dict]:
    """打开 https://creator.douyin.com/ 扫码登录，保存 cookie。返回 {"storage_state": str, "nickname": str} 或 None。"""
    from playwright.async_api import async_playwright

    if status_callback:
        await status_callback("请扫码登录抖音创作者中心（3分钟超时）...")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        context = await browser.new_context(
            user_agent=HEADERS["User-Agent"],
            viewport={"width": 1440, "height": 900},
            locale="zh-CN",
        )
        await context.add_init_script(STEALTH_SCRIPT)
        page = await context.new_page()
        await page.goto(CREATOR_URL, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(2000)

        # 检测登录成功：QR 码弹窗消失 + 页面出现登录后才有的内容
        # creator.douyin.com 登录前会显示 QR 码浮层/登录引导，登录后浮层消失并显示创作者中心内容
        logged_in = False
        try:
            for i in range(36):  # 最多等 3 分钟
                await page.wait_for_timeout(5000)
                try:
                    # 检测当前页面状态：是否有 QR 登录弹窗、是否有登录后才有的元素
                    state = await page.evaluate("""() => {
                        // QR 登录弹窗的多种可能选择器
                        const qrSelectors = [
                            '[class*="qrcode"]', '[class*="qr-code"]', '[class*="login-qrcode"]',
                            '[class*="loginQr"]', '[class*="qr_login"]', '[class*="web-login"]',
                            'div[class*="login-panel"]', 'div[class*="loginPanel"]',
                            'canvas', // 抖音有时用 canvas 画二维码
                        ];
                        let hasQr = false;
                        for (const sel of qrSelectors) {
                            const el = document.querySelector(sel);
                            if (el && el.offsetWidth > 80) { hasQr = true; break; }
                        }
                        // 登录成功后才有的元素：用户头像、数据概览、发布入口、侧边栏菜单等
                        const authedSelectors = [
                            '[data-e2e="user-avatar"]', '[data-e2e="user-info"]',
                            'img[src*="avatar"]', 'img[src*="aweme/aweme"]',
                            '.avatar', '.user-avatar',
                            'a[href*="/creator-micro/content/upload"]',
                            'a[href*="/creator-micro/content/manage"]',
                            'div[class*="data-overview"]', 'div[class*="dataOverview"]',
                            'div[class*="creator-home"]', 'div[class*="creatorHome"]',
                            'div[class*="side-bar"]', 'div[class*="sidebar"]',
                            'span[class*="nickname"]', 'div[class*="nickname"]',
                        ];
                        let hasAuthed = false;
                        for (const sel of authedSelectors) {
                            if (document.querySelector(sel)) { hasAuthed = true; break; }
                        }
                        // body 文本特征：登录后会有「创作服务平台」「数据概览」「内容管理」等
                        const bodyText = document.body ? document.body.innerText.slice(0, 3000) : '';
                        const hasAuthedText = /内容管理|数据概览|创作服务平台|发布视频|创作者中心|我的作品/.test(bodyText);
                        // 登录前文本：会包含「扫码登录」「登录抖音」
                        const hasLoginText = /扫码登录|登录抖音|请使用抖音/.test(bodyText);

                        return {
                            hasQr: hasQr,
                            hasAuthed: hasAuthed,
                            hasAuthedText: hasAuthedText,
                            hasLoginText: hasLoginText,
                            url: location.href,
                            title: document.title,
                        };
                    }""")

                    # 登录成功的判定（任一命中即认为登录成功）：
                    # A. QR 弹窗消失 + 有登录后元素/文本
                    # B. cookie 中出现登录态字段（最可靠）
                    if state and not state.get("hasQr"):
                        authed = state.get("hasAuthed") or (state.get("hasAuthedText") and not state.get("hasLoginText"))
                        if authed:
                            logged_in = True
                            if status_callback:
                                await status_callback(f"检测到登录成功（{state.get('title', '')}）")
                            break
                    # 兜底：直接查 cookie（严格判定，passport_csrf_token 未登录也有）
                    try:
                        cookies = await context.cookies()
                        cookie_names = {c.get("name", "") for c in cookies}
                        # 必须是 sessionid / sessionid_ss / sid_tt 之一（passport_csrf_token 未登录也下发）
                        if {"sessionid", "sessionid_ss", "sid_tt"} & cookie_names:
                            logged_in = True
                            if status_callback:
                                await status_callback("检测到登录 session cookie，登录成功")
                            break
                    except Exception:
                        pass
                    if status_callback and i % 4 == 0:
                        await status_callback(f"等待扫码登录... ({(i+1)*5}s)")
                except Exception:
                    pass
            else:
                await browser.close()
                if status_callback:
                    await status_callback("扫码登录超时")
                return None
        except Exception as e:
            await browser.close()
            if status_callback:
                await status_callback(f"登录异常: {e}")
            return None

        if not logged_in:
            await browser.close()
            if status_callback:
                await status_callback("登录失败：未检测到登录成功标志")
            return None

        # 登录成功后，再等页面完全加载，提取昵称
        try:
            await page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        await page.wait_for_timeout(1500)
        nickname = await _extract_nickname(page)

        # 保存登录态
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        await context.storage_state(path=str(DOUYIN_STORAGE_STATE_PATH))
        # 把昵称独立保存到文件，check-login 时直接读取（避免每次都启 Playwright）
        try:
            DOUYIN_NICKNAME_PATH.write_text(nickname or "", encoding="utf-8")
        except Exception:
            pass
        await browser.close()

    if status_callback:
        nick_disp = f"（{nickname}）" if nickname else ""
        await status_callback(f"创作者中心登录成功{nick_disp}，已保存登录状态")
    return {"storage_state": str(DOUYIN_STORAGE_STATE_PATH), "nickname": nickname or ""}


async def publish_video(
    video_path: str,
    title: str = "",
    tags: list = None,
    status_callback=None,
    headless: bool = False,
) -> dict:
    """通过 RPA 上传并发布视频到抖音创作者中心。

    Args:
        video_path: 本地视频文件路径
        title: 视频标题/描述（会填到发布页面的文本框）
        tags: 标签列表（会被拼到标题后面）
        status_callback: 异步回调 (msg: str) -> None
        headless: 是否无头模式（发布建议 False，便于观察）

    Returns:
        {"success": True, "url": "..."} 或 {"success": False, "error": "..."}
    """
    from playwright.async_api import async_playwright

    p = Path(video_path)
    if not p.exists():
        return {"success": False, "error": f"视频不存在: {video_path}"}

    # 1. 确保登录
    if status_callback:
        await status_callback("检查登录态...")
    login_info = await ensure_login_for_publish(status_callback)
    if not login_info:
        return {"success": False, "error": "未登录抖音，请先扫码登录"}
    storage_state = login_info["storage_state"]

    if status_callback:
        await status_callback("启动浏览器...")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        context = await browser.new_context(
            user_agent=HEADERS["User-Agent"],
            viewport={"width": 1440, "height": 900},
            locale="zh-CN",
            storage_state=storage_state,
        )
        await context.add_init_script(STEALTH_SCRIPT)
        page = await context.new_page()

        # 网络监听：捕获抖音发布 API 的响应，作为发布成功的可靠依据
        publish_api_result = {"done": False, "success": False, "url": ""}
        async def _on_publish_response(response):
            try:
                url = response.url or ""
                # 抖音发布相关 API：upload/commit、publish/send 等
                if ("publish" in url or "/upload/" in url or "commit" in url) and "douyin" in url:
                    if response.status == 200:
                        try:
                            body = await response.json()
                            # 发布 API 成功通常 status_code == 0 或 success == true
                            inner = body.get("data") or body
                            sc = inner.get("status_code") if isinstance(inner, dict) else None
                            if sc == 0 or inner.get("success") is True or "aweme_id" in str(inner):
                                publish_api_result["done"] = True
                                publish_api_result["success"] = True
                                publish_api_result["url"] = url
                        except Exception:
                            pass
                    elif response.status >= 500:
                        publish_api_result["done"] = True
                        publish_api_result["success"] = False
                        publish_api_result["url"] = url
            except Exception:
                pass
        page.on("response", _on_publish_response)

        try:
            # 2. 打开发布页
            if status_callback:
                await status_callback("打开抖音创作者中心...")
            await page.goto(UPLOAD_URL, wait_until="domcontentloaded", timeout=30000)
            await _human_pause(2.0, 3.5)

            # 检查是否被重定向到登录页
            if "/login" in page.url or "passport" in page.url:
                return {"success": False, "error": "登录态已失效，请重新扫码登录"}

            # 3. 上传视频文件
            if status_callback:
                await status_callback("上传视频文件...")
            # 诊断：打印当前页面状态
            try:
                diag = await page.evaluate("""() => {
                    const inputs = document.querySelectorAll('input[type="file"]');
                    return {
                        url: location.href,
                        title: document.title,
                        fileInputCount: inputs.length,
                        bodyTextHead: (document.body ? document.body.innerText : '').slice(0, 300),
                    };
                }""")
                if status_callback:
                    await status_callback(f"页面诊断: url={diag.get('url')}, title={diag.get('title')}, fileInputs={diag.get('fileInputCount')}")
            except Exception:
                pass
            # 抖音上传页的 input[type=file] 可能延迟挂载，多策略尝试
            file_input = page.locator('input[type="file"]').first
            uploaded = False
            # 策略 A：等 input 出现并直接 set_input_files
            try:
                await file_input.wait_for(state="attached", timeout=20000)
                await file_input.set_input_files(str(p))
                uploaded = True
            except Exception as e_a:
                if status_callback:
                    await status_callback(f"input 直接注入失败，尝试点击上传区... ({e_a.__class__.__name__})")
            # 策略 B：点击页面的「点击上传」区域，触发文件选择器，再用FileChooser接收
            if not uploaded:
                try:
                    # 抖音上传区常见选择器
                    upload_area_selectors = [
                        'div[class*="upload"]:not([class*="uploading"]):not([class*="uploaded"])',
                        'div[class*="upload-container"]',
                        'div[class*="upload-card"]',
                        'div[class*="container"] >> text=/点击上传|拖拽|上传视频/',
                        'button:has-text("上传")',
                        'div[role="button"]:has-text("上传")',
                    ]
                    # 找到可点击的上传区
                    target_loc = None
                    for sel in upload_area_selectors:
                        try:
                            loc = page.locator(sel).first
                            if await loc.count() > 0:
                                target_loc = loc
                                break
                        except Exception:
                            continue
                    if target_loc is None:
                        if status_callback:
                            await status_callback("未找到上传区域元素")
                    else:
                        # expect_file_chooser 必须在 click 之前进入上下文
                        async with page.expect_file_chooser(timeout=15000) as fc_info:
                            await target_loc.click(timeout=5000)
                        file_chooser = await fc_info.value
                        await file_chooser.set_files(str(p))
                        uploaded = True
                except Exception as e_b:
                    if status_callback:
                        await status_callback(f"点击上传区也失败：{e_b.__class__.__name__}: {e_b}")
            if not uploaded:
                return {"success": False, "error": "无法找到视频上传入口（input[type=file] 不可用且上传区点击失败）"}
            await _human_pause(2.0, 3.0)

            # 4. 等待视频上传完成（发布按钮可点击）
            if status_callback:
                await status_callback("等待视频上传完成（可能需要1-5分钟）...")
            upload_done = False
            for i in range(90):  # 最多等 7.5 分钟
                await page.wait_for_timeout(5000)
                try:
                    publish_btn = page.locator('button:has-text("发布"), button.publish-btn, .publish-content button').first
                    if await publish_btn.count() > 0:
                        is_disabled = await publish_btn.get_attribute("disabled")
                        if is_disabled is None:
                            if status_callback:
                                await status_callback(f"视频上传完成（{(i+1)*5}s）")
                            upload_done = True
                            break
                except Exception:
                    pass
                if status_callback and i % 6 == 0:
                    await status_callback(f"上传中... ({(i+1)*5}s)")

            if not upload_done:
                return {"success": False, "error": "视频上传超时"}

            await _human_pause(1.5, 2.5)

            # 5. 填写标题/描述（慢速逐字输入）
            if status_callback:
                await status_callback("填写标题和描述（慢速输入中）...")
            try:
                editor = page.locator('.ql-editor, [contenteditable="true"]').first
                if await editor.count() > 0:
                    await editor.click()
                    await _human_pause(0.8, 1.5)
                    # 仅输入标题文字（不含标签，标签单独走联想流程）
                    title_text = (title or "").strip()
                    if title_text:
                        await _human_type(page, title_text)
                    await _human_pause(1.0, 2.0)
            except Exception as e:
                if status_callback:
                    await status_callback(f"填写标题警告: {e}")

            # 6. 输入标签（触发抖音联想弹窗，逐个确认）
            if tags:
                if status_callback:
                    await status_callback(f"输入 {len(tags)} 个标签（触发联想确认）...")
                try:
                    editor = page.locator('.ql-editor, [contenteditable="true"]').first
                    if await editor.count() > 0:
                        await editor.click()
                        await _human_pause(0.5, 1.0)
                        for idx, tag in enumerate(tags[:5]):  # 最多5个标签
                            tag = tag.strip().lstrip("#")
                            if not tag:
                                continue
                            # 输入 # + 标签名
                            await page.keyboard.type("#", delay=80)
                            await _human_type(page, tag)
                            # 等待联想弹窗出现
                            await _human_pause(0.8, 1.5)
                            # 按回车确认选中第一个联想项
                            await page.keyboard.press("Enter")
                            await _human_pause(0.8, 1.5)
                            # 标签之间加一个空格分隔
                            if idx < min(len(tags), 5) - 1:
                                await page.keyboard.type(" ", delay=50)
                                await _human_pause(0.3, 0.6)
                except Exception as e:
                    if status_callback:
                        await status_callback(f"标签输入警告: {e}")

            await _human_pause(1.5, 2.5)

            # 7. 点击发布
            if status_callback:
                await status_callback("点击发布按钮...")
            try:
                publish_btn = page.locator('button:has-text("发布"), button.publish-btn, .publish-content button').first
                await publish_btn.wait_for(state="visible", timeout=10000)
                # 高亮发布按钮 + 较长停顿，便于用户观察（有头模式）
                try:
                    await publish_btn.evaluate("el => { el.style.outline='4px solid #ef4444'; el.style.outlineOffset='2px'; el.scrollIntoView({block:'center'}); }")
                except Exception:
                    pass

                # 截屏：点击前对页面进行识别，保存到 uploads 目录便于查看/调试
                try:
                    pre_click_shot = UPLOAD_DIR / f"douyin_pre_publish_{int(asyncio.get_event_loop().time())}.png"
                    await page.screenshot(path=str(pre_click_shot), full_page=True)
                    # 页面元素识别：提取按钮/输入框/可见文本，便于诊断
                    page_dom_info = await page.evaluate("""() => {
                        const buttons = Array.from(document.querySelectorAll('button')).slice(0, 20).map(b => ({
                            text: (b.innerText || '').trim().slice(0, 40),
                            disabled: b.disabled,
                            visible: b.offsetParent !== null,
                        })).filter(b => b.visible && b.text);
                        const editors = Array.from(document.querySelectorAll('.ql-editor, [contenteditable="true"]')).map(e => ({
                            text: (e.innerText || '').trim().slice(0, 200),
                            length: (e.innerText || '').length,
                        }));
                        return { url: location.href, title: document.title, buttons, editors };
                    }""")
                    if status_callback:
                        btn_texts = [b["text"] for b in page_dom_info.get("buttons", [])]
                        editor_lens = [e["length"] for e in page_dom_info.get("editors", [])]
                        await status_callback(f"📋 发布前截屏已保存: {pre_click_shot.name}")
                        await status_callback(f"🔍 页面识别: 按钮={btn_texts}, 编辑器内容长度={editor_lens}")
                except Exception as e_shot:
                    if status_callback:
                        await status_callback(f"截屏/识别警告: {e_shot}")

                await _human_pause(3.0, 4.5)  # 停 3-4.5 秒让用户看清就要点发布
                await publish_btn.click()
                await _human_pause(2.0, 3.0)  # 点击后再停 2-3 秒让用户看到点击效果
            except Exception as e:
                return {"success": False, "error": f"找不到/无法点击发布按钮: {e}"}

            # 8. 等待发布完成（网络 API 响应优先，URL 变化/成功提示兜底）
            if status_callback:
                await status_callback("等待发布完成...")
            success = False
            final_url = page.url
            deadline = asyncio.get_event_loop().time() + 90  # 最多等 90 秒
            while asyncio.get_event_loop().time() < deadline:
                # ① 优先：网络监听捕获到发布 API 成功响应
                if publish_api_result["done"]:
                    success = publish_api_result["success"]
                    if success:
                        final_url = publish_api_result["url"]
                    break

                cur_url = page.url
                # ② URL 跳转到内容管理页（抖音多种路径）
                if any(seg in cur_url for seg in ("/content/manage", "/creator-micro/content/manage", "/creator/home")):
                    success = True
                    final_url = cur_url
                    break

                # ③ 页面出现成功提示（toast/文字/弹窗，放宽选择器）
                try:
                    found = await page.evaluate("""() => {
                        // 成功 toast / 弹窗
                        const sels = ['.success', '.toast-success', '.ant-message-success',
                                      '.el-message--success', '[role="alert"].success',
                                      '.semi-toast-success', '.semi-notification-success'];
                        for (const s of sels) {
                            if (document.querySelector(s)) return true;
                        }
                        // 页面文字"发布成功"（抖音常见）
                        const txt = document.body.innerText || '';
                        if (txt.includes('发布成功') || txt.includes('视频已发布') || txt.includes('提交成功')) return true;
                        return false;
                    }""")
                    if found:
                        success = True
                        final_url = cur_url
                        break
                except Exception:
                    pass

                await asyncio.sleep(1.5)

            if success:
                if status_callback:
                    await status_callback("发布成功！")
                return {"success": True, "url": final_url}
            return {"success": False, "error": "发布后未检测到成功提示，请手动确认", "url": final_url}
        finally:
            # 保存最新登录态
            try:
                await context.storage_state(path=str(DOUYIN_STORAGE_STATE_PATH))
            except Exception:
                pass
            await browser.close()


# ============================================================================
# 分步式 RPA 发布会话（走一步停一步，等用户指令继续）
# ============================================================================
# 节点顺序: init -> upload -> fill_title -> fill_tags -> pre_publish -> publish
# 每个节点执行后返回 paused 状态 + 截屏 + 识别结果，保持浏览器不关闭
# 用户调用 next() 才执行下一步；abort() 关闭浏览器并清理


class PublishSession:
    """有状态的分步发布执行器。

    用法:
        session = PublishSession(video_path, title, tags)
        await session.start()           # 启动浏览器 + 打开发布页
        result = await session.run_next()  # 执行下一步，返回 {paused, step, ...}
        ...
        await session.abort()           # 放弃，关闭浏览器
    """

    STEPS = ["init", "upload", "fill_title", "fill_tags", "pre_publish", "locate_publish", "publish"]

    def __init__(self, video_path: str, title: str = "", tags: list = None, headless: bool = False):
        self.video_path = video_path
        self.title = (title or "").strip()
        self.tags = tags or []
        self.headless = headless

        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._publish_api_result = {"done": False, "success": False, "url": ""}

        self._step_index = 0
        self._started = False
        self._finished = False
        self._aborted = False
        self._last_screenshot = None  # 最近一次截屏路径
        self._last_dom_info = None    # 最近一次页面识别结果
        self._logs: list = []         # 累积日志

    @property
    def current_step(self):
        if self._finished:
            return "done"
        if self._aborted:
            return "aborted"
        if self._step_index >= len(self.STEPS):
            return "done"
        return self.STEPS[self._step_index]

    def _log(self, msg: str):
        self._logs.append(msg)

    def _bring_browser_to_foreground(self):
        """把 Chromium 浏览器窗口置前（Windows API），方便用户检查。"""
        if self.headless:
            return
        try:
            import ctypes
            user32 = ctypes.windll.user32
            EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int))
            found_hwnd = None
            def _enum_callback(hwnd, lParam):
                nonlocal found_hwnd
                if user32.IsWindowVisible(hwnd):
                    class_name = ctypes.create_unicode_buffer(256)
                    user32.GetClassNameW(hwnd, class_name, 256)
                    if class_name.value == "Chrome_WidgetWin_1":
                        found_hwnd = hwnd
                        return False
                return True
            user32.EnumWindows(EnumWindowsProc(_enum_callback), 0)
            if found_hwnd:
                SW_RESTORE = 9
                user32.ShowWindow(found_hwnd, SW_RESTORE)
                user32.SetForegroundWindow(found_hwnd)
        except Exception:
            pass

    async def _take_snapshot(self, label: str = ""):
        """截屏 + DOM 识别，保存到 uploads/，返回 (screenshot_path, dom_info)"""
        if self._page is None:
            return None, None
        try:
            ts = int(asyncio.get_event_loop().time())
            shot_name = f"douyin_step_{label or self.current_step}_{ts}.png"
            shot_path = UPLOAD_DIR / shot_name
            await self._page.screenshot(path=str(shot_path), full_page=True)
            self._last_screenshot = str(shot_path)
        except Exception as e:
            self._log(f"截屏失败: {e}")
            shot_path = None
        try:
            dom = await self._page.evaluate("""() => {
                const buttons = Array.from(document.querySelectorAll('button')).slice(0, 20).map(b => ({
                    text: (b.innerText || '').trim().slice(0, 40),
                    disabled: b.disabled,
                    visible: b.offsetParent !== null,
                })).filter(b => b.visible && b.text);
                const editors = Array.from(document.querySelectorAll('.ql-editor, [contenteditable="true"]')).map(e => ({
                    text: (e.innerText || '').trim().slice(0, 200),
                    length: (e.innerText || '').length,
                }));
                const fileInputs = document.querySelectorAll('input[type="file"]').length;
                return { url: location.href, title: document.title, buttons, editors, fileInputs };
            }""")
            self._last_dom_info = dom
        except Exception as e:
            self._log(f"DOM 识别失败: {e}")
            dom = None
        return shot_path, dom

    async def start(self) -> dict:
        """启动会话：检查登录 → 启动浏览器 → 打开发布页 → 暂停"""
        if self._started:
            return {"success": False, "error": "会话已启动"}
        self._started = True

        from playwright.async_api import async_playwright

        p = Path(self.video_path)
        if not p.exists():
            return {"success": False, "error": f"视频不存在: {self.video_path}"}

        # 1. 确保登录
        self._log("检查登录态...")
        async def _status_cb(msg):
            self._log(msg)
        login_info = await ensure_login_for_publish(status_callback=_status_cb)
        if not login_info:
            return {"success": False, "error": "未登录抖音，请先扫码登录"}
        storage_state = login_info["storage_state"]

        self._log("启动浏览器（有头模式，窗口置前）...")
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self.headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--start-maximized",        # 窗口最大化
                "--foreground",             # 窗口置前
                "--disable-popup-blocking", # 允许弹窗
            ],
        )
        self._context = await self._browser.new_context(
            user_agent=HEADERS["User-Agent"],
            viewport={"width": 1440, "height": 900},
            locale="zh-CN",
            storage_state=storage_state,
        )
        await self._context.add_init_script(STEALTH_SCRIPT)
        self._page = await self._context.new_page()
        # 有头模式：把浏览器窗口置前
        if not self.headless:
            self._bring_browser_to_foreground()
            self._log("✅ 浏览器窗口已置前")

        # 网络监听
        async def _on_publish_response(response):
            try:
                url = response.url or ""
                if ("publish" in url or "/upload/" in url or "commit" in url) and "douyin" in url:
                    if response.status == 200:
                        try:
                            body = await response.json()
                            inner = body.get("data") or body
                            sc = inner.get("status_code") if isinstance(inner, dict) else None
                            if sc == 0 or inner.get("success") is True or "aweme_id" in str(inner):
                                self._publish_api_result.update({"done": True, "success": True, "url": url})
                        except Exception:
                            pass
                    elif response.status >= 500:
                        self._publish_api_result.update({"done": True, "success": False, "url": url})
            except Exception:
                pass
        self._page.on("response", _on_publish_response)

        # 打开发布页
        self._log("打开抖音创作者中心发布页...")
        await self._page.goto(UPLOAD_URL, wait_until="domcontentloaded", timeout=30000)
        await _human_pause(2.0, 3.5)

        if "/login" in self._page.url or "passport" in self._page.url:
            await self._cleanup()
            return {"success": False, "error": "登录态已失效，请重新扫码登录"}

        await self._take_snapshot("init")
        self._log("✅ 已打开发布页，等待指令继续（下一步：上传视频）")
        # 停在 init 步，等用户调用 next
        self._step_index = 1  # 下一步是 upload
        return self._build_paused_result()

    async def run_next(self) -> dict:
        """执行下一个节点。返回 paused/done/failed 结果。"""
        if not self._started:
            return {"success": False, "error": "会话未启动"}
        if self._finished:
            return {"success": False, "error": "会话已结束"}
        if self._aborted:
            return {"success": False, "error": "会话已放弃"}
        if self._step_index >= len(self.STEPS):
            return {"success": False, "error": "已无下一步"}

        step = self.STEPS[self._step_index]
        try:
            if step == "upload":
                await self._step_upload()
            elif step == "fill_title":
                await self._step_fill_title()
            elif step == "fill_tags":
                await self._step_fill_tags()
            elif step == "pre_publish":
                await self._step_pre_publish()
            elif step == "locate_publish":
                await self._step_locate_publish()
            elif step == "publish":
                return await self._step_publish()  # 最后一步直接返回最终结果
        except Exception as e:
            self._log(f"❌ 步骤 {step} 失败: {e}")
            await self._take_snapshot(step + "_error")
            return {
                "success": False,
                "finished": False,
                "paused": True,
                "step": step,
                "error": str(e),
                "logs": list(self._logs),
                "screenshot": self._last_screenshot,
                "dom_info": self._last_dom_info,
            }

        self._step_index += 1
        # 如果不是最后一步，暂停等用户继续
        if self._step_index < len(self.STEPS):
            await self._take_snapshot(self.STEPS[self._step_index - 1] + "_done")
            next_step = self.STEPS[self._step_index]
            self._log(f"⏸ 已完成 {step}，等待指令继续（下一步：{next_step}）")
            # 暂停时把浏览器窗口置前，方便用户检查
            self._bring_browser_to_foreground()
            return self._build_paused_result()
        else:
            # 所有步骤完成（不应该走到这里，publish 步会直接返回）
            self._finished = True
            return {"success": True, "finished": True, "step": "done", "logs": list(self._logs)}

    def _build_paused_result(self) -> dict:
        return {
            "success": True,
            "paused": True,
            "finished": False,
            "step": self.STEPS[self._step_index - 1] if self._step_index > 0 else "init",
            "next_step": self.STEPS[self._step_index] if self._step_index < len(self.STEPS) else None,
            "logs": list(self._logs),
            "screenshot": self._last_screenshot,
            "dom_info": self._last_dom_info,
        }

    # ---- 各步骤实现 ----

    async def _step_upload(self):
        """上传视频文件 + 等待上传完成"""
        self._log("上传视频文件...")
        p = Path(self.video_path)
        page = self._page

        # 诊断
        try:
            diag = await page.evaluate("""() => ({
                url: location.href, title: document.title,
                fileInputCount: document.querySelectorAll('input[type="file"]').length,
                bodyTextHead: (document.body ? document.body.innerText : '').slice(0, 300),
            })""")
            self._log(f"页面诊断: url={diag.get('url')}, title={diag.get('title')}, fileInputs={diag.get('fileInputCount')}")
        except Exception:
            pass

        file_input = page.locator('input[type="file"]').first
        uploaded = False
        # 策略 A
        try:
            await file_input.wait_for(state="attached", timeout=20000)
            await file_input.set_input_files(str(p))
            uploaded = True
            self._log("✅ input 直接注入成功")
        except Exception as e_a:
            self._log(f"input 直接注入失败，尝试点击上传区... ({e_a.__class__.__name__})")
        # 策略 B
        if not uploaded:
            try:
                upload_area_selectors = [
                    'div[class*="upload"]:not([class*="uploading"]):not([class*="uploaded"])',
                    'div[class*="upload-container"]',
                    'div[class*="upload-card"]',
                    'div[class*="container"] >> text=/点击上传|拖拽|上传视频/',
                    'button:has-text("上传")',
                    'div[role="button"]:has-text("上传")',
                ]
                target_loc = None
                for sel in upload_area_selectors:
                    try:
                        loc = page.locator(sel).first
                        if await loc.count() > 0:
                            target_loc = loc
                            break
                    except Exception:
                        continue
                if target_loc is None:
                    self._log("未找到上传区域元素")
                else:
                    async with page.expect_file_chooser(timeout=15000) as fc_info:
                        await target_loc.click(timeout=5000)
                    file_chooser = await fc_info.value
                    await file_chooser.set_files(str(p))
                    uploaded = True
                    self._log("✅ 点击上传区成功")
            except Exception as e_b:
                self._log(f"点击上传区也失败：{e_b.__class__.__name__}: {e_b}")
        if not uploaded:
            raise RuntimeError("无法找到视频上传入口（input[type=file] 不可用且上传区点击失败）")

        await _human_pause(2.0, 3.0)
        # 等待上传完成
        self._log("等待视频上传完成（可能需要1-5分钟）...")
        upload_done = False
        for i in range(90):
            await page.wait_for_timeout(5000)
            try:
                publish_btn = page.locator('button:has-text("发布"), button.publish-btn, .publish-content button').first
                if await publish_btn.count() > 0:
                    is_disabled = await publish_btn.get_attribute("disabled")
                    if is_disabled is None:
                        self._log(f"✅ 视频上传完成（{(i+1)*5}s）")
                        upload_done = True
                        break
            except Exception:
                pass
            if i % 6 == 0:
                self._log(f"上传中... ({(i+1)*5}s)")
        if not upload_done:
            raise RuntimeError("视频上传超时")
        await _human_pause(1.5, 2.5)

    async def _step_fill_title(self):
        """填写标题"""
        self._log("填写标题（慢速输入中）...")
        page = self._page
        editor = page.locator('.ql-editor, [contenteditable="true"]').first
        if await editor.count() > 0:
            await editor.click()
            await _human_pause(0.8, 1.5)
            if self.title:
                await _human_type(page, self.title)
                self._log(f"✅ 标题已输入（{len(self.title)} 字）")
            await _human_pause(1.0, 2.0)
        else:
            self._log("⚠ 未找到标题编辑器")

    async def _step_fill_tags(self):
        """填写标签（触发联想弹窗，逐个确认）"""
        if not self.tags:
            self._log("无标签，跳过")
            return
        self._log(f"输入 {len(self.tags)} 个标签（触发联想确认）...")
        page = self._page
        editor = page.locator('.ql-editor, [contenteditable="true"]').first
        if await editor.count() > 0:
            await editor.click()
            await _human_pause(0.5, 1.0)
            for idx, tag in enumerate(self.tags[:5]):
                tag = tag.strip().lstrip("#")
                if not tag:
                    continue
                await page.keyboard.type("#", delay=80)
                await _human_type(page, tag)
                await _human_pause(0.8, 1.5)
                await page.keyboard.press("Enter")
                await _human_pause(0.8, 1.5)
                if idx < min(len(self.tags), 5) - 1:
                    await page.keyboard.type(" ", delay=50)
                    await _human_pause(0.3, 0.6)
            self._log(f"✅ {min(len(self.tags), 5)} 个标签已输入")
        else:
            self._log("⚠ 未找到标题编辑器，无法输入标签")
        await _human_pause(1.5, 2.5)

    async def _step_pre_publish(self):
        """发布前准备：滚动到底部 + 截屏 + 识别 + 高亮发布按钮"""
        self._log("发布前准备：滚动到底部 + 截屏 + 识别页面...")
        page = self._page
        # 先滚动到底部（发布按钮在底部）
        await self._scroll_to_bottom()
        publish_btn = page.locator('button:has-text("发布"), button.publish-btn, .publish-content button').first
        try:
            await publish_btn.wait_for(state="visible", timeout=10000)
            await publish_btn.evaluate("el => { el.style.outline='4px solid #ef4444'; el.style.outlineOffset='2px'; el.scrollIntoView({block:'center'}); }")
        except Exception:
            pass
        await self._take_snapshot("pre_publish")
        # 解析按钮状态，告知用户
        dom = self._last_dom_info or {}
        btns = dom.get("buttons", []) if dom else []
        editors = dom.get("editors", []) if dom else []
        btn_texts = [b["text"] for b in btns]
        editor_lens = [e["length"] for e in editors]
        self._log(f"📋 截屏已保存")
        self._log(f"🔍 页面识别: 按钮={btn_texts}, 编辑器内容长度={editor_lens}")
        # 检查发布按钮是否可点
        publish_btn_info = next((b for b in btns if "发布" in b["text"]), None)
        if publish_btn_info:
            if publish_btn_info.get("disabled"):
                self._log("⚠ 发布按钮当前 disabled，可能上传未完成或内容不满足要求")
            else:
                self._log("✅ 发布按钮可点击")
        await _human_pause(2.0, 3.0)

    async def _scroll_to_bottom(self):
        """滚动到页面最底部（发布按钮在底部，需要先滚下去才能看到/点击）。

        抖音发布页是 SPA，可能有多个滚动容器，所以：
        1. 滚动 window 到底
        2. 遍历所有可滚动元素，把它们也滚到底
        3. 重复几次（SPA 懒加载）
        """
        page = self._page
        try:
            await page.evaluate("""async () => {
                const sleep = (ms) => new Promise(r => setTimeout(r, ms));
                // 滚动 window 到底，分多次
                for (let i = 0; i < 5; i++) {
                    window.scrollTo(0, document.body.scrollHeight);
                    // 遍历所有可滚动容器
                    const all = Array.from(document.querySelectorAll('*'));
                    for (const el of all) {
                        if (el.scrollHeight > el.clientHeight + 50 && el.clientHeight > 100) {
                            try { el.scrollTop = el.scrollHeight; } catch(e) {}
                        }
                    }
                    await sleep(300);
                }
            }""")
            self._log("📜 已滚动到页面底部")
        except Exception as e:
            self._log(f"滚动到底部失败（不影响后续）: {e}")

    async def _locate_publish_button(self):
        """共享方法：先滚动到底部，再定位发布按钮（多选择器 + 遍历兜底，排除 disabled）。返回 Locator 或 None。"""
        page = self._page
        # 先滚动到底部（发布按钮在底部）
        await self._scroll_to_bottom()
        selectors = [
            'button:has-text("发布")',
            'button.publish-btn',
            '.publish-content button',
            'button[data-e2e="publish"]',
            'div[role="button"]:has-text("发布")',
        ]
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0:
                    is_disabled = await loc.get_attribute("disabled")
                    if is_disabled is not None:
                        self._log(f"⚠ 选择器 {sel} 找到按钮但 disabled，继续找...")
                        continue
                    self._log(f"✅ 定位到发布按钮（选择器: {sel}）")
                    return loc
            except Exception:
                continue
        # 遍历兜底
        self._log("常规选择器未找到，尝试遍历所有按钮...")
        btn_info = await page.evaluate("""() => {
            const btns = Array.from(document.querySelectorAll('button, div[role="button"]'));
            for (const b of btns) {
                const txt = (b.innerText || '').trim();
                if (txt === '发布' && !b.disabled && b.offsetParent !== null) {
                    b.setAttribute('data-publish-target', 'true');
                    return true;
                }
            }
            return false;
        }""")
        if btn_info:
            self._log("✅ 通过遍历定位到发布按钮")
            return page.locator('[data-publish-target="true"]').first
        return None

    async def _step_locate_publish(self):
        """专门定位发布按钮：截图 + 详细识别（坐标/尺寸/disabled/文本），暂停等用户确认后再点 publish。

        与 pre_publish 的区别：
        - pre_publish 做整页截屏 + 通用 DOM 识别
        - locate_publish 专门针对发布按钮：获取按钮的 boundingBox（x/y/width/height）、
          滚动到视口、高亮、单独截图按钮区域，让用户看清楚按钮在哪里、长什么样、是否可点
        """
        self._log("🔎 专门定位发布按钮（截图 + 详细识别）...")
        page = self._page

        publish_btn = await self._locate_publish_button()
        if publish_btn is None:
            raise RuntimeError("找不到可点击的发布按钮（所有选择器和遍历均失败）")

        # 滚动到按钮可见
        try:
            await publish_btn.scroll_into_view_if_needed(timeout=5000)
        except Exception:
            pass

        # 获取按钮详细信息：文本/disabled/坐标/尺寸/可见性
        try:
            btn_detail = await publish_btn.evaluate("""el => {
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                return {
                    text: (el.innerText || '').trim(),
                    tagName: el.tagName,
                    disabled: el.disabled,
                    visible: el.offsetParent !== null,
                    x: Math.round(rect.x),
                    y: Math.round(rect.y),
                    width: Math.round(rect.width),
                    height: Math.round(rect.height),
                    centerX: Math.round(rect.x + rect.width / 2),
                    centerY: Math.round(rect.y + rect.height / 2),
                    bgColor: style.backgroundColor,
                    color: style.color,
                    fontSize: style.fontSize,
                    cursor: style.cursor,
                };
            }""")
            self._log(f"📐 发布按钮详情: 文本=\"{btn_detail.get('text')}\" 标签={btn_detail.get('tagName')} disabled={btn_detail.get('disabled')} 可见={btn_detail.get('visible')}")
            self._log(f"   坐标=({btn_detail.get('x')},{btn_detail.get('y')}) 尺寸={btn_detail.get('width')}x{btn_detail.get('height')} 中心=({btn_detail.get('centerX')},{btn_detail.get('centerY')})")
            self._log(f"   背景={btn_detail.get('bgColor')} 文字色={btn_detail.get('color')} 字号={btn_detail.get('fontSize')} 光标={btn_detail.get('cursor')}")

            if btn_detail.get("disabled"):
                self._log("⚠⚠⚠ 发布按钮当前 disabled！点击可能无效。可能原因：上传未完成/内容不满足要求/有必填项未填")
            else:
                self._log("✅ 发布按钮可点击（disabled=false）")
        except Exception as e_detail:
            self._log(f"获取按钮详情失败: {e_detail}")
            btn_detail = None

        # 高亮发布按钮（6px 红色边框 + 滚动到中心）
        try:
            await publish_btn.evaluate("el => { el.style.outline='6px solid #ef4444'; el.style.outlineOffset='3px'; el.scrollIntoView({block:'center', behavior:'smooth'}); }")
        except Exception:
            pass

        # 整页截屏（包含高亮的按钮）
        await self._take_snapshot("locate_publish")

        # 额外：单独截屏按钮区域
        if btn_detail and btn_detail.get("width", 0) > 0:
            try:
                btn_screenshot = UPLOAD_DIR / f"douyin_publish_btn_{int(asyncio.get_event_loop().time())}.png"
                await publish_btn.screenshot(path=str(btn_screenshot))
                self._log(f"📸 发布按钮单独截图已保存: {btn_screenshot.name}")
                # 把按钮特写图存到 last_screenshot，让前端显示按钮特写
                self._last_screenshot = str(btn_screenshot)
            except Exception as e_shot:
                self._log(f"按钮单独截图失败（不影响流程）: {e_shot}")

        # 把按钮详情存到 dom_info，让前端能显示
        if self._last_dom_info is None:
            self._last_dom_info = {}
        self._last_dom_info["publish_button"] = btn_detail

        self._log("⏸ 已定位发布按钮并截图识别，请检查后再点'继续'执行点击")
        await _human_pause(1.0, 1.5)

    async def _step_publish(self) -> dict:
        """点击发布 + 等待发布完成 + 关闭浏览器"""
        self._log("🎯 开始执行发布：点击底部发布按钮...")
        page = self._page
        try:
            # 复用共享定位方法
            publish_btn = await self._locate_publish_button()
            if publish_btn is None:
                await self._cleanup()
                return {"success": False, "finished": True, "step": "publish", "error": "找不到可点击的发布按钮", "logs": list(self._logs)}

            # 滚动到按钮可见 + 高亮
            try:
                await publish_btn.scroll_into_view_if_needed(timeout=5000)
                await publish_btn.evaluate("el => { el.style.outline='6px solid #ef4444'; el.style.outlineOffset='3px'; }")
            except Exception:
                pass
            self._log("👆 已高亮发布按钮（红色边框），3 秒后点击...")
            await _human_pause(3.0, 4.0)

            # 策略 1：Playwright click
            clicked = False
            try:
                await publish_btn.click(timeout=5000)
                clicked = True
                self._log("✅ 已点击发布按钮（Playwright click）")
            except Exception as e_click:
                self._log(f"Playwright click 失败: {e_click.__class__.__name__}，尝试 JS click...")
            # 策略 2：JS click 兜底
            if not clicked:
                try:
                    await publish_btn.evaluate("el => el.click()")
                    clicked = True
                    self._log("✅ 已点击发布按钮（JS click）")
                except Exception as e_js:
                    self._log(f"JS click 失败: {e_js.__class__.__name__}")
            # 策略 3：dispatch event
            if not clicked:
                try:
                    await page.evaluate("""() => {
                        const btn = document.querySelector('[data-publish-target="true"]')
                            || Array.from(document.querySelectorAll('button')).find(b => b.innerText.trim() === '发布' && !b.disabled);
                        if (btn) {
                            btn.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
                        }
                    }""")
                    clicked = True
                    self._log("✅ 已点击发布按钮（dispatch event）")
                except Exception as e_disp:
                    self._log(f"dispatch event 失败: {e_disp.__class__.__name__}")

            if not clicked:
                await self._cleanup()
                return {"success": False, "finished": True, "step": "publish", "error": "所有点击策略均失败", "logs": list(self._logs)}

            await _human_pause(2.0, 3.0)  # 点击后停 2-3 秒让用户看到效果
        except Exception as e:
            await self._cleanup()
            return {"success": False, "finished": True, "step": "publish", "error": f"发布步骤异常: {e}", "logs": list(self._logs)}

        # 等待发布完成
        self._log("等待发布完成...")
        success = False
        final_url = page.url
        deadline = asyncio.get_event_loop().time() + 90
        while asyncio.get_event_loop().time() < deadline:
            if self._publish_api_result["done"]:
                success = self._publish_api_result["success"]
                if success:
                    final_url = self._publish_api_result["url"]
                break
            cur_url = page.url
            if any(seg in cur_url for seg in ("/content/manage", "/creator-micro/content/manage", "/creator/home")):
                success = True
                final_url = cur_url
                break
            try:
                found = await page.evaluate("""() => {
                    const sels = ['.success', '.toast-success', '.ant-message-success',
                                  '.el-message--success', '[role="alert"].success',
                                  '.semi-toast-success', '.semi-notification-success'];
                    for (const s of sels) if (document.querySelector(s)) return true;
                    const txt = document.body.innerText || '';
                    if (txt.includes('发布成功') || txt.includes('视频已发布') || txt.includes('提交成功')) return true;
                    return false;
                }""")
                if found:
                    success = True
                    final_url = cur_url
                    break
            except Exception:
                pass
            await asyncio.sleep(1.5)

        await self._take_snapshot("publish_done")
        self._finished = True
        if success:
            self._log("✅ 发布成功！")
        else:
            self._log("⚠ 发布后未检测到成功提示，请手动确认")
        # 保存登录态 + 关闭浏览器
        await self._cleanup()
        return {
            "success": success,
            "finished": True,
            "paused": False,
            "step": "publish",
            "url": final_url,
            "error": None if success else "发布后未检测到成功提示，请手动确认",
            "logs": list(self._logs),
            "screenshot": self._last_screenshot,
            "dom_info": self._last_dom_info,
        }

    async def _cleanup(self):
        """保存登录态 + 关闭浏览器"""
        try:
            if self._context:
                await self._context.storage_state(path=str(DOUYIN_STORAGE_STATE_PATH))
        except Exception:
            pass
        try:
            if self._browser:
                await self._browser.close()
        except Exception:
            pass
        try:
            if self._playwright:
                await self._playwright.stop()
        except Exception:
            pass
        self._page = None
        self._context = None
        self._browser = None
        self._playwright = None

    async def abort(self):
        """放弃会话，关闭浏览器"""
        if self._aborted:
            return
        self._aborted = True
        self._log("⚠ 用户放弃会话，关闭浏览器")
        await self._cleanup()


# 会话存储（task_id -> PublishSession），由 app.py 管理
publish_sessions: dict = {}
