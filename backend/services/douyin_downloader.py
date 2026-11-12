"""
抖音视频下载器
支持：
- 分享链接解析（含短链 iesdouyin / v.douyin.com）
- 无水印视频下载（Playwright 浏览器拦截 + 反检测 + 登录态持久化）
- 自动提取视频标题/作者
"""
import re
import httpx
import asyncio
from typing import Optional
from pathlib import Path
from urllib.parse import urlparse

from config import settings

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://www.douyin.com/",
}

# 抖音登录态持久化
DOUYIN_STORAGE_STATE_PATH = Path(settings.UPLOAD_DIR) / "douyin_storage_state.json"

# 反检测脚本：隐藏 webdriver 标识
STEALTH_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });
window.chrome = { runtime: {} };
const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) => (
    parameters.name === 'notifications'
        ? Promise.resolve({ state: Notification.permission })
        : originalQuery(parameters)
);
"""


async def resolve_douyin_url(share_url: str) -> dict:
    """
    解析抖音分享链接，返回视频信息：
    {
        "video_id": str,
        "aweme_url": str,      # 可访问的详情页URL
        "title": str,          # 视频标题
        "author": str,         # 作者昵称
        "desc": str,           # 描述文案
    }
    """
    share_url = share_url.strip()
    if not share_url:
        raise ValueError("抖音链接为空")

    # 提取链接（用户可能粘贴整段分享文案）
    # 排除反引号 ` 和引号 等包裹字符，避免被当作 URL 的一部分
    url_match = re.search(r'https?://[^\s，。`\'"<>]+', share_url)
    if not url_match:
        raise ValueError("未在输入中找到抖音链接")
    raw_url = url_match.group(0).rstrip('/`\'"<>')

    # 直接从URL里找 video_id（适合用户粘贴完整链接 https://www.douyin.com/video/123456）
    direct_vid = re.search(r'/video/(\d+)', raw_url) or re.search(r'modal_id=(\d+)', raw_url)

    async with httpx.AsyncClient(
        follow_redirects=True,
        headers=HEADERS,
        timeout=15.0,
    ) as client:
        video_id = None
        title = ""
        author = ""
        desc = ""

        # 方案A：直接已包含 video_id 的完整链接
        if direct_vid:
            video_id = direct_vid.group(1)

        # 方案B：短链跳转解析
        if not video_id:
            try:
                resp = await client.get(raw_url)
                final_url = str(resp.url)
                vid_match = (
                    re.search(r'/video/(\d+)', final_url)
                    or re.search(r'modal_id=(\d+)', final_url)
                )
                if vid_match:
                    video_id = vid_match.group(1)
                if not video_id:
                    text_vid = re.search(r'"aweme_id"\s*:\s*"?(\d{15,})"?', resp.text)
                    if text_vid:
                        video_id = text_vid.group(1)
            except Exception:
                pass

        # 方案C：iesdouyin share 接口
        if not video_id:
            try:
                sid_match = re.search(r'v\.douyin\.com/([^/?\s]+)', raw_url)
                if sid_match:
                    share_id = sid_match.group(1).rstrip('/')
                    ies_url = f"https://www.iesdouyin.com/share/video/{share_id}/"
                    resp2 = await client.get(ies_url)
                    text_vid = re.search(r"aweme_id\s*[=:]\s*['\"]?(\d{15,})['\"]?", resp2.text)
                    if text_vid:
                        video_id = text_vid.group(1)
            except Exception:
                pass

        if not video_id:
            raise RuntimeError(
                "无法解析抖音链接：可能是无效链接、已删除视频，或需要 cookie。"
                f" 原始URL: {raw_url}"
            )

        # 通过 iesdouyin iteminfo 接口获取详情
        try:
            detail_url = f"https://www.iesdouyin.com/web/api/v2/aweme/iteminfo/?item_ids={video_id}"
            detail_resp = await client.get(detail_url, headers={**HEADERS, "Cookie": ""})
            if detail_resp.status_code == 200:
                items = detail_resp.json().get("item_list", [])
                if items:
                    title = items[0].get("desc", "") or items[0].get("item_title", "")
                    author = items[0].get("author", {}).get("nickname", "")
                    desc = items[0].get("desc", "")
        except Exception:
            pass

        return {
            "video_id": video_id,
            "aweme_url": f"https://www.douyin.com/video/{video_id}",
            "title": title or f"抖音视频_{video_id}",
            "author": author,
            "desc": desc,
        }


async def ensure_douyin_login(
    status_callback=None,
    force_relogin: bool = False,
) -> Optional[str]:
    """
    确保已登录抖音（扫码登录，持久化 storage_state）
    返回 storage_state 路径，未登录返回 None
    """
    from playwright.async_api import async_playwright

    if not force_relogin and DOUYIN_STORAGE_STATE_PATH.exists():
        if status_callback:
            await status_callback("使用已保存的抖音登录状态")
        return str(DOUYIN_STORAGE_STATE_PATH)

    if status_callback:
        await status_callback("请扫码登录抖音（3分钟超时）...")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(
            user_agent=HEADERS["User-Agent"],
            viewport={"width": 1280, "height": 800},
            locale="zh-CN",
        )
        await context.add_init_script(STEALTH_SCRIPT)
        page = await context.new_page()
        await page.goto("https://www.douyin.com/login")

        # 等待登录成功（URL变化或登录元素消失）
        try:
            # 登录成功后会跳转回首页或显示用户头像
            await page.wait_for_url("https://www.douyin.com/", timeout=180_000)
            await page.wait_for_timeout(3000)  # 等 cookie 写入
        except Exception:
            # 也可能用户已经在首页点了登录弹窗
            try:
                await page.wait_for_selector('img[data-e2e="user-avatar"]', timeout=10_000)
            except Exception:
                raise RuntimeError("抖音扫码登录超时或失败")

        await context.storage_state(path=str(DOUYIN_STORAGE_STATE_PATH))
        await browser.close()

    if status_callback:
        await status_callback("抖音登录成功，已保存登录状态")
    return str(DOUYIN_STORAGE_STATE_PATH)


async def _download_with_ytdlp(
    share_url: str,
    video_id: str,
    output_path: Path,
    status_callback=None,
) -> bool:
    """
    使用 yt-dlp 下载抖音视频（社区维护的提取器，自动跟随反爬升级）
    从 Playwright 保存的 storage_state 提取 cookies 转 Netscape 格式，规避 fresh cookies 要求。
    """
    import yt_dlp

    if status_callback:
        await status_callback("使用 yt-dlp 下载视频...")

    # yt-dlp 是同步库，放到线程池执行
    outtmpl = str(output_path.with_suffix(".%(ext)s"))

    ydl_opts = {
        "outtmpl": outtmpl,
        # 优先下载带音频的格式，避免只有视频流的片段
        "format": "bestvideo+bestaudio/best/bestvideo+bestaudio",
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "concurrent_fragment_downloads": 5,
        # 模拟正常浏览器
        "http_headers": {
            "User-Agent": HEADERS["User-Agent"],
            "Referer": "https://www.douyin.com/",
            "Accept-Language": "zh-CN,zh;q=0.9",
        },
        # 不下载字幕/缩略图等
        "writesubtitles": False,
        "writeautomaticsub": False,
        "writethumbnail": False,
        "skip_download": False,
    }

    # 从 Playwright storage_state 提取 cookies → Netscape cookiefile（规避 fresh cookies 要求）
    cookiefile_path: str | None = None
    if DOUYIN_STORAGE_STATE_PATH.exists():
        try:
            import json as _json
            import tempfile
            data = _json.loads(DOUYIN_STORAGE_STATE_PATH.read_text(encoding="utf-8"))
            cookies = data.get("cookies", [])
            # Netscape cookie 格式：domain  include_subdomain  path  secure  expiration  name  value
            lines = ["# Netscape HTTP Cookie File"]
            for c in cookies:
                domain = c.get("domain", "")
                if not domain:
                    continue
                include_sub = "TRUE" if domain.startswith(".") else "FALSE"
                path = c.get("path", "/")
                secure = "TRUE" if c.get("secure") else "FALSE"
                expires = int(c.get("expires", 0))
                if expires <= 0:
                    expires = 0
                name = c.get("name", "")
                value = c.get("value", "")
                lines.append(f"{domain}\t{include_sub}\t{path}\t{secure}\t{expires}\t{name}\t{value}")
            tmp = tempfile.NamedTemporaryFile(
                mode="w", suffix=".txt", delete=False, encoding="utf-8"
            )
            tmp.write("\n".join(lines))
            tmp.close()
            cookiefile_path = tmp.name
            ydl_opts["cookiefile"] = cookiefile_path
            if status_callback:
                await status_callback(f"yt-dlp: 已注入 {len(cookies)} 个抖音 cookies")
        except Exception as e:
            if status_callback:
                await status_callback(f"yt-dlp: cookies 提取失败({e})，尝试无 cookie 下载")

    def _sync_download():
        # 优先用原始分享链接（yt-dlp 自己解析短链）
        url_to_use = share_url
        # 如果是纯文本里提取的链接，直接用
        url_match = re.search(r'https?://[^\s，。]+', share_url)
        if url_match:
            url_to_use = url_match.group(0)
        # 如果是完整视频页URL，直接用
        if f"/video/{video_id}" in url_to_use:
            url_to_use = f"https://www.douyin.com/video/{video_id}"

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url_to_use, download=True)
            return info

    loop = asyncio.get_event_loop()
    try:
        info = await loop.run_in_executor(None, _sync_download)
    except Exception as e:
        if status_callback:
            await status_callback(f"yt-dlp 下载失败: {e}")
        return False

    # yt-dlp 下载的文件可能扩展名不同，找出来重命名
    downloaded = None
    if output_path.exists():
        downloaded = output_path
    else:
        # 查找同前缀的文件
        parent = output_path.parent
        stem = output_path.stem
        for f in parent.iterdir():
            if f.name.startswith(stem):
                downloaded = f
                break

    if downloaded and downloaded.exists() and downloaded.stat().st_size > 400_000:
        # 重命名为目标路径
        if downloaded != output_path:
            downloaded.rename(output_path)
        if status_callback:
            await status_callback(f"视频下载完成（{output_path.stat().st_size // 1024}KB）")
        result = True
    else:
        result = False

    # 清理临时 cookie 文件
    if cookiefile_path:
        try:
            import os as _os
            _os.unlink(cookiefile_path)
        except Exception:
            pass
    return result


async def _download_with_playwright(
    video_id: str,
    output_path: Path,
    status_callback=None,
) -> bool:
    """
    用 Playwright 打开抖音视频页，提取视频URL并下载。
    - 使用非 headless 模式（headless 会被抖音反爬检测）
    - 注入反检测脚本
    - 复用登录态（如有）
    - 双重方案：网络拦截 + 页面JS数据提取
    """
    from playwright.async_api import async_playwright

    if status_callback:
        await status_callback("启动浏览器获取视频...")

    async with async_playwright() as p:
        # 关键：headless=False 避免被反爬识别
        browser = await p.chromium.launch(
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        )

        # 复用登录态（如果有）
        context_kwargs = {
            "user_agent": HEADERS["User-Agent"],
            "viewport": {"width": 1280, "height": 800},
            "locale": "zh-CN",
        }
        if DOUYIN_STORAGE_STATE_PATH.exists():
            context_kwargs["storage_state"] = str(DOUYIN_STORAGE_STATE_PATH)
            if status_callback:
                await status_callback("使用已保存的抖音登录态")

        context = await browser.new_context(**context_kwargs)
        await context.add_init_script(STEALTH_SCRIPT)
        page = await context.new_page()

        captured_streams = []  # 收集所有视频流分片，最后取最大的完整流

        async def on_response(response):
            try:
                url = response.url
                # 抖音视频流URL特征（含 douyinvod.com CDN）
                if (
                    "video/tos" in url
                    or "video_mp4" in url
                    or "douyinvod.com" in url
                    or ".mp4" in url
                ) and response.status in (200, 206):
                    try:
                        ct = response.headers.get("content-type", "")
                        if "video" in ct or ".mp4" in url or "video/tos" in url:
                            body = await response.body()
                            if len(body) > 100_000:  # 过滤小预览图
                                captured_streams.append((len(body), url, body))
                    except Exception:
                        pass
            except Exception:
                pass

        page.on("response", on_response)

        if status_callback:
            await status_callback("打开视频页面...")

        try:
            await page.goto(
                f"https://www.douyin.com/video/{video_id}",
                wait_until="domcontentloaded",
                timeout=30_000,
            )

            # 方案A：从页面 SSR 数据提取视频URL（最可靠）
            await page.wait_for_timeout(3000)  # 等 SSR 数据写入
            if status_callback:
                await status_callback("提取视频地址...")

            try:
                play_url = await page.evaluate("""() => {
                    // 抖音SSR数据：window._ROUTER_DATA
                    const routerData = window._ROUTER_DATA || {};
                    const state = routerData.loaderData || {};
                    // 视频详情通常在 (video_)id 页面节点
                    for (const key of Object.keys(state)) {
                        const node = state[key];
                        if (node && node.videoInfo) {
                            const video = node.videoInfo.video || node.videoInfo;
                            const urlList = video.playApi || video.play_addr?.url_list || video.download_addr?.url_list;
                            if (urlList) {
                                return Array.isArray(urlList) ? urlList[0] : urlList;
                            }
                        }
                        if (node && node.awemeDetail) {
                            const video = node.awemeDetail.video || {};
                            const urlList = video.playApi || video.play_addr?.url_list || video.download_addr?.url_list;
                            if (urlList) {
                                return Array.isArray(urlList) ? urlList[0] : urlList;
                            }
                        }
                    }
                    // 备选：__INITIAL_STATE__
                    if (window.__INITIAL_STATE__) {
                        const s = window.__INITIAL_STATE__;
                        const detail = s.videoDetail || s.awemeDetail || (s.item && s.item.list && s.item.list[0]);
                        if (detail && detail.video) {
                            const urlList = detail.video.playApi || detail.video.play_addr?.url_list;
                            if (urlList) {
                                return Array.isArray(urlList) ? urlList[0] : urlList;
                            }
                        }
                    }
                    // 最后兜底：video 标签的 src
                    const videoEl = document.querySelector('video');
                    if (videoEl && videoEl.src) {
                        return videoEl.src;
                    }
                    return null;
                }""")

                if play_url:
                    # playApi 可能是相对路径，需要拼接域名
                    if play_url.startswith("/"):
                        play_url = "https://www.douyin.com" + play_url
                    # 替换为无水印地址
                    if "playwm" in play_url:
                        play_url = play_url.replace("playwm", "play")

                    if status_callback:
                        await status_callback(f"已提取视频地址，下载中...")

                    # 用浏览器的 cookie 上下文下载（避免反爬）
                    # APIResponse 没有 iter_chunks，改用 body() 一次性读取
                    try:
                        video_resp = await context.request.get(
                            play_url,
                            headers={"Referer": "https://www.douyin.com/"},
                            timeout=180_000,
                        )
                        if video_resp.ok:
                            expected_len = video_resp.headers.get("content-length")
                            expected_len = int(expected_len) if expected_len else None
                            tmp_path = output_path.with_suffix(".mp4.tmp")
                            body = await video_resp.body()
                            written = len(body)
                            if expected_len and written != expected_len:
                                if status_callback:
                                    await status_callback(f"下载不完整：预期 {expected_len} 字节，实际 {written} 字节，尝试 httpx...")
                            elif written > 400_000:
                                tmp_path.write_bytes(body)
                                tmp_path.replace(output_path)
                                if status_callback:
                                    await status_callback(f"视频下载完成（{written // 1024}KB）")
                                await browser.close()
                                return True
                            else:
                                if status_callback:
                                    await status_callback(f"下载内容过小（{written // 1024}KB < 400KB），可能是预览流，等待网络拦截...")
                    except Exception as e:
                        if status_callback:
                            await status_callback(f"浏览器下载失败: {e}，尝试 httpx...")

                    # 浏览器下载失败，用 httpx 带 Referer 流式下载
                    try:
                        async with httpx.AsyncClient(
                            follow_redirects=True,
                            headers={**HEADERS, "Referer": "https://www.douyin.com/"},
                            timeout=httpx.Timeout(connect=30, read=300, write=60, pool=30),
                        ) as client:
                            async with client.stream("GET", play_url) as resp:
                                resp.raise_for_status()
                                expected_len = resp.headers.get("content-length")
                                expected_len = int(expected_len) if expected_len else None
                                tmp_path = output_path.with_suffix(".mp4.tmp")
                                written = 0
                                with open(tmp_path, "wb") as f:
                                    async for chunk in resp.aiter_bytes(chunk_size=65536):
                                        f.write(chunk)
                                        written += len(chunk)
                                if expected_len and written != expected_len:
                                    if status_callback:
                                        await status_callback(f"httpx 下载不完整：预期 {expected_len}，实际 {written}")
                                elif written > 400_000:
                                    tmp_path.replace(output_path)
                                    if status_callback:
                                        await status_callback(f"视频下载完成（{written // 1024}KB）")
                                    await browser.close()
                                    return True
                                else:
                                    if status_callback:
                                        await status_callback(f"httpx 下载内容过小（{written} 字节），等待网络拦截...")
                    except Exception as e:
                        if status_callback:
                            await status_callback(f"httpx 下载失败: {e}")
            except Exception as e:
                if status_callback:
                    await status_callback(f"提取视频地址失败: {e}")

            # 方案B：等待网络拦截捕获视频流（不论 captured_streams 是否已有，都尝试拿完整流）
            # 因为方案A拿到的可能是预览流，真正的视频流要等播放器加载
            if status_callback:
                await status_callback("等待视频流加载（最多 60s）...")
            # 先尝试点击播放器触发播放
            try:
                # 等待 video 元素出现
                await page.wait_for_selector("video", timeout=10_000)
                # 多次点击触发播放（抖音播放器有遮罩）
                for click_sel in [".video-player", "video", ".xgplayer-play", "[data-e2e='video-player']"]:
                    try:
                        el = page.locator(click_sel).first
                        if await el.count() > 0:
                            await el.click(timeout=2000)
                            break
                    except Exception:
                        continue
                # 取消静音（某些情况下静音不会触发完整流加载）
                try:
                    await page.evaluate("""() => {
                        const v = document.querySelector('video');
                        if (v) { v.muted = false; v.volume = 0.01; v.play().catch(()=>{}); }
                    }""")
                except Exception:
                    pass
            except Exception as e:
                if status_callback:
                    await status_callback(f"触发播放失败: {e}")

            # 等待网络拦截捕获到大于 400KB 的视频流（完整流）
            best_stream = None
            for wait_round in range(12):  # 最多等60秒
                # 优先找大于 400KB 的（完整视频）
                big_streams = [s for s in captured_streams if s[0] > 400_000]
                if big_streams:
                    best_stream = max(big_streams, key=lambda x: x[0])
                    break
                await page.wait_for_timeout(5000)
                # 每轮再尝试点一次
                if wait_round % 3 == 2:
                    try:
                        for click_sel in ["video", ".video-player", "[data-e2e='video-player']"]:
                            el = page.locator(click_sel).first
                            if await el.count() > 0:
                                await el.click(timeout=1000)
                                break
                    except Exception:
                        pass

        except Exception as e:
            if status_callback:
                await status_callback(f"页面加载异常: {e}")

        # 更新登录态（cookie可能变化）
        if DOUYIN_STORAGE_STATE_PATH.exists():
            try:
                await context.storage_state(path=str(DOUYIN_STORAGE_STATE_PATH))
            except Exception:
                pass

        await browser.close()

    # 从所有捕获的视频流中取最大的一条（完整视频而非预览分片）
    if best_stream:
        best_size, best_url, best_body = best_stream
        output_path.write_bytes(best_body)
        if status_callback:
            await status_callback(f"视频流拦截下载完成（{best_size // 1024}KB）")
        return True
    if captured_streams:
        captured_streams.sort(key=lambda x: x[0], reverse=True)
        best_size, best_url, best_body = captured_streams[0]
        # 如果最大的也 < 400KB，判定为预览流，不下发
        if best_size < 400 * 1024:
            if status_callback:
                await status_callback(f"仅捕获到预览流（{best_size // 1024}KB < 400KB），判定失败")
            return False
        output_path.write_bytes(best_body)
        if status_callback:
            await status_callback(f"视频下载完成（{best_size // 1024}KB）")
        return True

    return False


async def _download_via_ssr(
    video_id: str,
    output_path: Path,
    status_callback=None,
) -> tuple[bool, dict]:
    """
    方案0（首选）：iesdouyin 分享页 SSR 数据直接解析无水印 play_url。
    无需 Cookie，使用移动端 UA 访问 https://www.iesdouyin.com/share/video/{id}/
    返回 (success, info_dict)，info_dict 包含 title/author/duration_ms。
    """
    import json as _json
    mobile_headers = {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
                      "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    share_url = f"https://www.iesdouyin.com/share/video/{video_id}/"
    if status_callback:
        await status_callback("方案0：获取分享页 SSR 数据...")

    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            headers=mobile_headers,
            timeout=15.0,
        ) as client:
            resp = await client.get(share_url)
            if resp.status_code != 200:
                if status_callback:
                    await status_callback(f"方案0：分享页请求失败 status={resp.status_code}")
                return False, {}

            # 提取 window._ROUTER_DATA
            m = re.search(r'window\._ROUTER_DATA\s*=\s*(.*?)</script>', resp.text, re.DOTALL)
            if not m:
                if status_callback:
                    await status_callback("方案0：未找到 SSR 数据")
                return False, {}

            try:
                data = _json.loads(m.group(1).strip())
            except _json.JSONDecodeError as e:
                if status_callback:
                    await status_callback(f"方案0：SSR JSON 解析失败: {e}")
                return False, {}

            loader = data.get("loaderData", {})
            # 键名可能是 video_(id)/page 或其他变体
            page_data = loader.get("video_(id)/page") or loader.get("video_(id)/page/page") or {}
            if not page_data:
                # 兜底找第一个含 videoInfoRes 的节点
                for k, v in loader.items():
                    if isinstance(v, dict) and v.get("videoInfoRes"):
                        page_data = v
                        break

            vir = page_data.get("videoInfoRes", {})
            items = vir.get("item_list", [])
            if not items:
                if status_callback:
                    await status_callback(f"方案0：item_list 为空（status_code={vir.get('status_code')}）")
                return False, {}

            item = items[0]
            title = item.get("desc", "") or f"抖音视频_{video_id}"
            author = item.get("author", {}).get("nickname", "")
            duration_ms = item.get("video", {}).get("duration", 0)

            # 提取无水印 play_url（把 playwm 替换为 play）
            video_data = item.get("video", {})
            play_addr = video_data.get("play_addr", {})
            url_list = play_addr.get("url_list", [])
            if not url_list:
                if status_callback:
                    await status_callback("方案0：play_addr.url_list 为空")
                return False, {}

            play_url_wm = url_list[0]
            play_url = play_url_wm.replace("playwm", "play")

            if status_callback:
                dur_s = duration_ms / 1000 if duration_ms else 0
                await status_callback(f"方案0：已提取无水印地址（时长 {dur_s:.1f}s），下载中...")

            # 流式下载
            tmp_path = output_path.with_suffix(".mp4.tmp")
            async with client.stream(
                "GET", play_url, headers={**mobile_headers, "Referer": "https://www.iesdouyin.com/"}
            ) as dl_resp:
                dl_resp.raise_for_status()
                expected_len = dl_resp.headers.get("content-length")
                expected_len = int(expected_len) if expected_len else None
                written = 0
                with open(tmp_path, "wb") as f:
                    async for chunk in dl_resp.aiter_bytes(chunk_size=65536):
                        f.write(chunk)
                        written += len(chunk)
                if expected_len and written != expected_len:
                    if status_callback:
                        await status_callback(f"方案0：下载不完整 预期 {expected_len} 实际 {written}")
                    return False, {}
                if written < 200_000:
                    if status_callback:
                        await status_callback(f"方案0：下载内容过小（{written} 字节）")
                    return False, {}
                tmp_path.replace(output_path)
                if status_callback:
                    await status_callback(f"方案0：下载完成（{written // 1024}KB）")
                return True, {
                    "title": title,
                    "author": author,
                    "duration_ms": duration_ms,
                }
    except Exception as e:
        if status_callback:
            await status_callback(f"方案0：异常 {e}")
        return False, {}


async def download_douyin_video(share_url: str, output_dir: str, status_callback=None) -> dict:
    """
    完整流程：解析 → 下载视频
    """
    if status_callback:
        await status_callback("解析抖音链接...")

    info = await resolve_douyin_url(share_url)
    video_id = info["video_id"]

    if status_callback:
        await status_callback(f"已解析：{info['title'][:30]}... 下载视频...")

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    safe_title = re.sub(r'[\\/:*?"<>|\n\r\t]', '_', info["title"])[:40]
    filename = f"douyin_{video_id}_{safe_title}.mp4"
    output_path = output_dir / filename

    # 方案0（首选）：iesdouyin SSR 直解（无需 Cookie，最稳）
    if status_callback:
        await status_callback("尝试方案0：iesdouyin SSR...")
    ok, ssr_info = await _download_via_ssr(video_id, output_path, status_callback)
    if ok:
        # 用 SSR 数据补全 title/author（resolve_douyin_url 通常拿不到）
        if ssr_info.get("title"):
            info["title"] = ssr_info["title"]
        if ssr_info.get("author"):
            info["author"] = ssr_info["author"]
        # 质量校验：不达标则删除文件降级到方案1/2/3，不直接 raise
        if not output_path.exists() or output_path.stat().st_size < 400 * 1024:
            size_kb = output_path.stat().st_size / 1024 if output_path.exists() else 0
            if status_callback:
                await status_callback(f"方案0：文件不达标（{size_kb:.0f}KB < 400KB），降级到 yt-dlp...")
            try:
                output_path.unlink()
            except Exception:
                pass
            ok = False  # 降级
        else:
            try:
                try:
                    from moviepy.editor import VideoFileClip  # moviepy v1.x
                except ImportError:
                    from moviepy import VideoFileClip  # moviepy v2.x
                with VideoFileClip(str(output_path)) as clip:
                    dur = clip.duration
                if dur < 5.0:
                    if status_callback:
                        await status_callback(f"方案0：时长不达标（{dur:.2f}s < 5s），降级到 yt-dlp...")
                    try:
                        output_path.unlink()
                    except Exception:
                        pass
                    ok = False  # 降级
                else:
                    quality = f"{dur:.2f}s / {output_path.stat().st_size/1024/1024:.2f}MB"
            except ImportError:
                quality = f"{output_path.stat().st_size/1024/1024:.2f}MB (moviepy 未安装)"
            except Exception as e:
                quality = f"{output_path.stat().st_size/1024/1024:.2f}MB (时长解析失败: {e})"

    if ok:
        if status_callback:
            await status_callback(f"下载完成: {filename}（{quality}）")
        return {
            "video_path": str(output_path),
            "video_id": video_id,
            "title": info["title"],
            "author": info["author"],
            "desc": info["desc"],
            "quality": quality,
        }

    # 方案1：yt-dlp（社区维护，自动跟随反爬升级）
    if status_callback:
        await status_callback("尝试 yt-dlp 方案...")
    success = await _download_with_ytdlp(share_url, video_id, output_path, status_callback)
    if success:
        if status_callback:
            await status_callback(f"下载完成: {filename}")
        return {
            "video_path": str(output_path),
            "video_id": video_id,
            "title": info["title"],
            "author": info["author"],
            "desc": info["desc"],
        }

    # 方案2：httpx iesdouyin 接口（多数已失效，作为快速尝试）
    if status_callback:
        await status_callback("yt-dlp 失败，尝试 httpx iesdouyin...")
    no_wm_url = None
    async with httpx.AsyncClient(
        follow_redirects=True,
        headers={**HEADERS, "Cookie": ""},
        timeout=15.0,
    ) as client:
        try:
            detail_url = f"https://www.iesdouyin.com/web/api/v2/aweme/iteminfo/?item_ids={video_id}"
            detail_resp = await client.get(detail_url)
            if detail_resp.status_code == 200:
                items = detail_resp.json().get("item_list", [])
                if items:
                    play_url = items[0].get("video", {}).get("play_addr", {}).get("url_list", [])
                    if play_url:
                        no_wm_url = play_url[0].replace("playwm", "play")
        except Exception:
            pass

    if no_wm_url:
        if status_callback:
            await status_callback("正在下载视频（httpx直链）...")
        try:
            async with httpx.AsyncClient(
                follow_redirects=True, headers=HEADERS,
                timeout=httpx.Timeout(connect=30, read=300, write=60, pool=30),
            ) as client:
                async with client.stream("GET", no_wm_url) as resp:
                    resp.raise_for_status()
                    expected_len = resp.headers.get("content-length")
                    expected_len = int(expected_len) if expected_len else None
                    tmp_path = output_path.with_suffix(".mp4.tmp")
                    written = 0
                    with open(tmp_path, "wb") as f:
                        async for chunk in resp.aiter_bytes(chunk_size=65536):
                            f.write(chunk)
                            written += len(chunk)
                if expected_len and written != expected_len:
                    if status_callback:
                        await status_callback(f"httpx 直链下载不完整：预期 {expected_len}，实际 {written}，切换浏览器方案")
                elif written > 100_000:
                    tmp_path.replace(output_path)
                    if status_callback:
                        await status_callback("视频下载完成")
                    return {
                        "video_path": str(output_path),
                        "video_id": video_id,
                        "title": info["title"],
                        "author": info["author"],
                        "desc": info["desc"],
                    }
                else:
                    if status_callback:
                        await status_callback(f"httpx 直链下载内容过小（{written} 字节），切换浏览器方案")
        except Exception as e:
            if status_callback:
                await status_callback(f"httpx 下载失败，切换浏览器方案: {e}")

    # 方案3：Playwright 浏览器拦截（兜底方案）
    if status_callback:
        await status_callback("尝试 Playwright 浏览器方案...")
    success = await _download_with_playwright(video_id, output_path, status_callback)
    if not success:
        raise RuntimeError(
            f"视频下载失败：yt-dlp、httpx、Playwright 三种方案均失败。"
            f" video_id={video_id}，可能视频已删除或地区受限。"
            f" 可尝试先扫码登录抖音后再试。"
        )

    # 质量校验：大小 ≥ 400KB 且 时长 ≥ 5s，否则视为下载失败
    if not output_path.exists() or output_path.stat().st_size < 400 * 1024:
        size_kb = output_path.stat().st_size / 1024 if output_path.exists() else 0
        raise RuntimeError(
            f"视频下载不达标：大小 {size_kb:.0f}KB < 400KB（可能是预览流或地区限制）。"
            f" video_id={video_id}"
        )
    try:
        try:
            from moviepy.editor import VideoFileClip  # moviepy v1.x
        except ImportError:
            from moviepy import VideoFileClip  # moviepy v2.x
        with VideoFileClip(str(output_path)) as clip:
            dur = clip.duration
        if dur < 5.0:
            raise RuntimeError(
                f"视频下载不达标：时长 {dur:.2f}s < 5s（可能是预览片段）。"
                f" video_id={video_id}"
            )
        quality = f"{dur:.2f}s / {output_path.stat().st_size/1024/1024:.2f}MB"
    except ImportError:
        quality = f"{output_path.stat().st_size/1024/1024:.2f}MB (moviepy 未安装，跳过时长校验)"
    except RuntimeError:
        raise
    except Exception as e:
        quality = f"{output_path.stat().st_size/1024/1024:.2f}MB (时长解析失败: {e})"

    if status_callback:
        await status_callback(f"下载完成: {filename}（{quality}）")

    return {
        "video_path": str(output_path),
        "video_id": video_id,
        "title": info["title"],
        "author": info["author"],
        "desc": info["desc"],
        "quality": quality,
    }


async def douyin_logout():
    """删除抖音登录态"""
    if DOUYIN_STORAGE_STATE_PATH.exists():
        DOUYIN_STORAGE_STATE_PATH.unlink()


if __name__ == "__main__":
    import sys
    test_url = sys.argv[1] if len(sys.argv) > 1 else "https://v.douyin.com/xxxxx"
    result = asyncio.run(download_douyin_video(test_url, "outputs/toutiao_test"))
    print(result)
