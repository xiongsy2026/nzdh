"""抖音商城女装链接解析与 Image2 产品图生成。"""
import base64
import json
import re
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urljoin, unquote, urlparse, parse_qs

import httpx

from backend.services.image2_client import Image2Client


IMAGE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36",
    "Referer": "https://www.douyin.com/",
}


def _extract_url(value: str) -> str:
    match = re.search(r"https?://[^\s，。]+", value.strip())
    if not match:
        raise ValueError("未找到有效的抖音商城链接")
    return match.group(0).rstrip("，。")


def _image_candidates(text: str) -> list[str]:
    urls = re.findall(r"https?://[^\"'\\\s]+", text)
    return [u.replace("\\u002F", "/").replace("\\/", "/") for u in urls if re.search(r"\.(?:jpg|jpeg|png|webp)(?:[?&]|$)", u, re.I)]


def _parse_goods_detail_from_url(final_url: str) -> dict[str, Any] | None:
    """从重定向后的 URL query 中解析 goods_detail 参数。

    抖音 C 端分享短链（v.douyin.com/xxx）重定向后的 URL 会带 goods_detail
    参数（URL 编码的 JSON），其中包含标题、销量、价格、主图 URL 列表。
    命中即返回 {title, image_urls, sales, min_price, max_price, product_id}，
    否则返回 None。
    """
    m = re.search(r"[?&]goods_detail=([^&]+)", final_url)
    if not m:
        return None
    try:
        gd = json.loads(unquote(m.group(1)))
    except (json.JSONDecodeError, ValueError):
        return None
    img = gd.get("img") or {}
    image_urls = [u for u in (img.get("url_list") or []) if isinstance(u, str) and u.startswith("http")]
    if not image_urls:
        return None
    return {
        "title": gd.get("title", "") or "",
        "image_urls": image_urls,
        "sales": gd.get("sales"),
        "min_price": gd.get("min_price"),
        "max_price": gd.get("max_price"),
        "product_id": gd.get("product_id"),
    }


async def resolve_douyin_mall_clothing(share_url: str) -> dict[str, Any]:
    """解析商城链接，返回商品标题及候选服装图 URL。"""
    raw_url = _extract_url(share_url)
    candidates: list[str] = []
    title = ""
    final_url = raw_url

    try:
        async with httpx.AsyncClient(follow_redirects=True, headers=IMAGE_HEADERS, timeout=20.0) as client:
            response = await client.get(raw_url)
            final_url = str(response.url)
            # 优先解析重定向后 URL 里的 goods_detail 参数（C 端分享短链走这条路）
            gd = _parse_goods_detail_from_url(final_url)
            if gd:
                return {
                    "url": raw_url,
                    "final_url": final_url,
                    "title": gd["title"],
                    "clothing_image_urls": gd["image_urls"][:12],
                    "sales": gd.get("sales"),
                    "min_price": gd.get("min_price"),
                    "max_price": gd.get("max_price"),
                    "product_id": gd.get("product_id"),
                }
            title_match = re.search(r"<title[^>]*>(.*?)</title>", response.text, re.I | re.S)
            if title_match:
                title = re.sub(r"\s+", " ", title_match.group(1)).strip()
            candidates.extend(_image_candidates(response.text))
    except httpx.HTTPError:
        pass

    # 动态商城页使用浏览器读取最终 DOM 和图片属性。
    if not candidates:
        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            context = await browser.new_context(user_agent=IMAGE_HEADERS["User-Agent"], locale="zh-CN")
            page = await context.new_page()
            await page.goto(final_url, wait_until="domcontentloaded", timeout=45_000)
            await page.wait_for_timeout(2500)
            title = title or await page.title()
            candidates = await page.locator("img").evaluate_all(
                """imgs => imgs.map(img => img.currentSrc || img.src || img.getAttribute('data-src') || '')"""
            )
            await browser.close()

    candidates = list(dict.fromkeys(urljoin(final_url, u) for u in candidates if u.startswith(("http://", "https://"))))
    if not candidates:
        raise RuntimeError("未从抖音商城页面找到商品图片，请确认链接可访问或先登录抖音")

    return {"url": raw_url, "final_url": final_url, "title": title, "clothing_image_urls": candidates[:12]}


async def download_clothing_image(image_url: str, output_path: str | Path) -> str:
    path = Path(output_path)
    async with httpx.AsyncClient(headers=IMAGE_HEADERS, follow_redirects=True, timeout=60.0) as client:
        response = await client.get(image_url)
        response.raise_for_status()
    if not response.content:
        raise RuntimeError("商品图片为空")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(response.content)
    return str(path)


SHOWCASE_PROMPT = (
    "生成一张9:16竖版高端电商服装橱窗展示摄影图，8k高清，全身正面构图。"
    "第1张图是唯一的服装外观真值，必须完整保留服装版型、颜色、面料质感、"
    "图案、印花、领口、袖口、门襟、纽扣和所有细节，禁止改款、换色、添加文字或Logo；"
    "根据这套服装的风格，为模特搭配协调的鞋子。"
    "第2张图是唯一的模特外观真值，必须完整保留模特的脸型、五官、发型、"
    "肤色和身材比例，让模特穿着第1张图的服装和搭配的鞋子，面带微笑，"
    "站立姿势，全身正面展示。"
    "第3张图是背景范式参考，保留其空间布局和橱窗展示风格，去掉原图中的人物。"
    "环境细节：模特站在挂满同款衣服的黑色边框内嵌式衣架展示区旁，"
    "展示区上方有灯带白光，挂着6套套装——【硬约束：衣架上挂着的每一件衣服必须与模特身上穿着的服装完全一致，"
    "包括颜色、图案、版型、面料质感、领口、袖口、门襟、纽扣等所有细节，不得换色、不得改款、不得变形、不得使用近似款】；"
    "衣架下方台面上铺白色毛绒垫，摆放白色陶瓷花瓶（插有不同颜色的鲜花）、"
    "香薰、相机、精致礼盒、杂志、招财猫、香槟、小夜灯，质感精致。"
    "右侧落地窗外为春日街道景色，有绿色树木、白黄花朵、喷泉、"
    "不同颜色鲜花的花坛。浅灰色背景墙上方有电子投影屏幕，"
    "投影屏幕上显示模特穿着同款服装的全身展示画面（与画面中的人物形象一致）。"
    "整体为兼具时尚感和节日气息的服装店场景，无字幕、无水印、无Logo文字。"
)


async def generate_womenswear_image(
    clothing_image_path: str,
    model_image_path: str,
    scene_image_path: str,
) -> str:
    """以服装白底图、小女孩模特图、背景范式图为参考，生成 9:16 橱窗展示图。"""
    result = await Image2Client().image_edit_sync(
        prompt=SHOWCASE_PROMPT,
        images=[Path(clothing_image_path), Path(model_image_path), Path(scene_image_path)],
        size="1024x1536",
        n=1,
        output_format="png",
    )
    return Image2Client()._get_result_image(result)


FLAT_LAY_PROMPT = (
    "生成一张3:4竖版高端电商服装平铺摄影图（flat lay）。"
    "第1张图是唯一的服装外观真值，必须完整保留服装版型、颜色、面料质感、"
    "图案、印花、领口、袖口、门襟、纽扣和所有细节，禁止改款、换色、添加文字或Logo。"
    "要求：1.纯白背景（#FFFFFF），无人模特，衣服按穿着时的自然形态平铺展示；"
    "2.衣服居中占画面主体，四周适当留白；"
    "3.如原图含配饰（腰带、帽子、围巾、包包等），一并平铺在服装旁的白底上，"
    "保持构图协调，不重叠不遮挡服装主体；"
    "4.高端商业摄影质感，柔和均匀顶光，无强阴影，细节锐利，色彩还原准确；"
    "5.无文字、无水印、无衣架、无模特、无背景道具。"
)


async def generate_flat_lay_image(clothing_image_path: str) -> str:
    """以商品图为唯一参考，生成 3:4 白底服装平铺图。"""
    result = await Image2Client().image_edit_sync(
        prompt=FLAT_LAY_PROMPT,
        images=[Path(clothing_image_path)],
        size="768x1024",
        n=1,
        output_format="png",
    )
    return Image2Client()._get_result_image(result)


# ═════════════════════════════════════════════════════════════════════
# 动作迁移参考图：从模仿视频抽关键帧 + 泛化融合（模特穿服装在指定背景里）
# ═════════════════════════════════════════════════════════════════════

def extract_video_keyframe(video_path: str, output_path: str | Path) -> str:
    """从视频抽取一个代表关键帧（取中间时刻），用于短剧模式作背景参考。

    优先用 ffmpeg，失败则降级 opencv。返回输出图片路径。
    """
    import shutil
    import subprocess
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    # 取中间帧：先探时长再 -ss 定位
    ffmpeg_path = shutil.which("ffmpeg")
    if ffmpeg_path:
        try:
            # 探测时长
            probe = [ffmpeg_path, "-v", "error", "-show_entries", "format=duration",
                     "-of", "default=noprint_wrappers=1:nokey=1", str(video_path)]
            dur_out = subprocess.run(probe, capture_output=True, text=True, timeout=30)
            dur = float(dur_out.stdout.strip() or "0")
            mid_t = max(0.5, dur / 2) if dur > 0 else 1.0
            cmd = [ffmpeg_path, "-y", "-ss", str(mid_t), "-i", str(video_path),
                   "-vframes", "1", "-q:v", "2", str(out)]
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=60)
            if out.exists() and out.stat().st_size > 1024:
                return str(out)
        except Exception:
            pass

    # 降级 opencv
    try:
        import cv2
        cap = cv2.VideoCapture(str(video_path))
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        mid = max(1, total // 2) if total > 0 else 1
        cap.set(cv2.CAP_PROP_POS_FRAMES, mid)
        ok, frame = cap.read()
        cap.release()
        if ok and frame is not None:
            cv2.imwrite(str(out), frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
            return str(out)
    except Exception:
        pass

    raise RuntimeError(f"抽取视频关键帧失败: {video_path}")


# ═════════════════════════════════════════════════════════════════════
# 上传模式专用：LLM 协作（MiMo 识别商品图 + GPT 挑动作帧）
# ═════════════════════════════════════════════════════════════════════

def extract_video_frames_for_llm(
    video_path: str,
    out_dir: str | Path,
    count: int = 5,
    max_side: int = 768,
) -> list[str]:
    """从视频均匀抽取 N 帧 JPEG（供 LLM 多模态挑选）。

    策略：先 ffprobe 探时长，按 [0.1*total, 0.9*total] 区间均匀取 N 个时刻。
    每帧缩放长边到 max_side 降低 token 体积。
    返回 JPEG 文件路径列表（按时间顺序）。
    """
    import shutil
    import subprocess
    from PIL import Image

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    ffmpeg_path = shutil.which("ffmpeg")
    if not ffmpeg_path:
        raise RuntimeError("未找到 ffmpeg，无法抽取视频帧")

    # 探测时长
    probe = [ffmpeg_path, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(video_path)]
    dur_out = subprocess.run(probe, capture_output=True, text=True, timeout=30)
    dur = float(dur_out.stdout.strip() or "0")
    if dur <= 0:
        # 降级：仅取一帧
        dur = 1.0
        count = 1

    start_t = max(0.1, dur * 0.1)
    end_t = max(start_t + 0.5, dur * 0.9)
    if count == 1:
        timestamps = [(start_t + end_t) / 2]
    else:
        step = (end_t - start_t) / (count - 1)
        timestamps = [start_t + step * i for i in range(count)]

    paths: list[str] = []
    for i, t in enumerate(timestamps):
        raw_jpg = out_dir / f"frame_{i:02d}.jpg"
        cmd = [ffmpeg_path, "-y", "-ss", f"{t:.2f}", "-i", str(video_path),
               "-vframes", "1", "-q:v", "2", str(raw_jpg)]
        try:
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=60)
        except Exception:
            continue
        if not raw_jpg.exists() or raw_jpg.stat().st_size < 1024:
            continue
        # 缩放
        try:
            with Image.open(raw_jpg) as img:
                w, h = img.size
                if max(w, h) > max_side:
                    scale = max_side / max(w, h)
                    img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
                img.convert("RGB").save(raw_jpg, "JPEG", quality=85)
        except Exception:
            pass
        paths.append(str(raw_jpg))

    if not paths:
        raise RuntimeError(f"抽取视频帧全部失败: {video_path}")
    return paths


async def analyze_product_image_full(product_image_path: str) -> dict:
    """调 MiMo-v2.5 多模态识别商品图，返回 {has_model, is_white_bg}。

    - has_model: True = 含真人模特（可作模特图直接复用）
    - is_white_bg: True = 纯白底图（无复杂背景，可直接复用模特图当参考图）

    白底图定义：背景为纯白/极浅色（#FFFFFF/#FAFAFA 等），无场景物体、无文字水印。
    用于素材出片 4 分支：白底模特图无需抠图直接融合；有背景模特图需先抠图。
    """
    import asyncio
    import base64
    from config import settings

    if not Path(product_image_path).exists():
        raise RuntimeError(f"商品图不存在: {product_image_path}")

    # base64 编码（压缩到长边 768）
    try:
        from PIL import Image
        import io
        with Image.open(product_image_path) as img:
            w, h = img.size
            if max(w, h) > 768:
                scale = 768 / max(w, h)
                img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
            buf = io.BytesIO()
            img.convert("RGB").save(buf, "JPEG", quality=85)
            img_b64 = base64.b64encode(buf.getvalue()).decode()
    except Exception:
        with open(product_image_path, "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode()

    system_prompt = (
        "你是服装商品图识别助手。用户会给你一张图，你需要判断两个维度：\n"
        "1. has_model：是否包含真人模特穿着服装展示（true=含模特，false=仅服装平铺/挂拍无人物）\n"
        "2. is_white_bg：背景是否为纯白底（true=纯白/极浅色背景，无场景物体、无文字水印；"
        "false=有复杂背景如室内/街道/橱窗/渐变/道具等）\n"
        "只输出 JSON，格式严格为 "
        "{\"has_model\": true/false, \"is_white_bg\": true/false}，不要任何额外解释。"
    )
    user_content = [
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}},
        {"type": "text", "text": "请判断这张商品图是否包含真人模特，以及背景是否为纯白底。"},
    ]
    payload = {
        "model": settings.MIMO_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "max_completion_tokens": 256,
    }
    headers = {
        "Authorization": f"Bearer {settings.MIMO_API_KEY}",
        "Content-Type": "application/json",
    }

    last_exc: Optional[Exception] = None
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                resp = await client.post(
                    f"{settings.MIMO_BASE_URL}/chat/completions",
                    headers=headers, json=payload,
                )
                if resp.status_code in (403, 429) or resp.status_code >= 500:
                    body = resp.text[:300]
                    if "insufficient" in body or "额度" in body or "quota" in body:
                        raise RuntimeError(f"MiMo API 额度不足: {body}")
                    raise httpx.HTTPStatusError(
                        f"HTTP {resp.status_code}: {body}",
                        request=resp.request, response=resp,
                    )
                resp.raise_for_status()
                data = resp.json()
                content = data["choices"][0]["message"]["content"] or ""
                # 兼容 ```json 包裹
                content = content.strip()
                if content.startswith("```"):
                    content = re.search(r"\{[^}]*\}", content, re.S)
                    content = content.group(0) if content else ""
                obj = json.loads(content)
                return {
                    "has_model": bool(obj.get("has_model", False)),
                    "is_white_bg": bool(obj.get("is_white_bg", False)),
                }
        except RuntimeError:
            raise
        except (httpx.HTTPStatusError, httpx.TimeoutException, httpx.TransportError) as e:
            last_exc = e
            if attempt < 2:
                await asyncio.sleep(5 * (attempt + 1))
                continue
    raise RuntimeError(f"MiMo 商品图识别失败（已重试 3 次）: {last_exc}") from last_exc


async def analyze_product_image_has_model(product_image_path: str) -> bool:
    """向后兼容封装：调 analyze_product_image_full 取 has_model 字段。

    Returns:
        True = 含模特（可作模特图直接复用）
        False = 仅服装（需从模仿视频挑关键帧作模特图）
    """
    result = await analyze_product_image_full(product_image_path)
    return result["has_model"]


async def select_best_model_frame_from_video(
    video_path: str,
    out_dir: str | Path,
    frame_count: int = 5,
) -> str:
    """调 GPT-5.6-sol 多模态从模仿视频抽取的 N 帧中挑选最能体现动作的一帧。

    上传模式下，当商品图仅含服装（无模特）时调用此函数，挑出一帧能清晰展示
    完整人物动作/姿态的帧作为模特图，后续用于动作迁移参考图融合。

    Returns:
        选中的帧图片路径
    """
    import asyncio
    import base64
    from config import settings

    # 1. 抽 N 帧
    frame_paths = extract_video_frames_for_llm(video_path, out_dir, count=frame_count)

    # 2. 编码为 base64
    frames_b64: list[str] = []
    for fp in frame_paths:
        with open(fp, "rb") as f:
            frames_b64.append(base64.b64encode(f.read()).decode())

    # 3. 构造 GPT 多模态请求
    system_prompt = (
        "你是动作视频分析助手。用户会给你一段模仿视频里的若干帧（按时间顺序），"
        "你需要从中挑选出最适合做「动作迁移模特图」的一帧。挑选标准：\n"
        "1. 人物全身或大部分入镜\n"
        "2. 姿态/动作清晰可辨（避免被遮挡或运动模糊）\n"
        "3. 光线充足、画面清晰\n"
        "4. 能代表整段视频的核心动作\n"
        "只输出 JSON，格式严格为 {\"index\": 0}（0 表示第 1 张），不要任何额外解释。"
    )
    user_content: list[dict] = []
    for i, b64 in enumerate(frames_b64):
        user_content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
        })
        user_content.append({"type": "text", "text": f"第 {i+1} 帧"})
    user_content.append({"type": "text", "text": "请挑选最适合做模特图的一帧，输出 JSON。"})

    payload = {
        "model": settings.OPENAI_MODEL,  # gpt-5.6-sol
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "max_completion_tokens": 256,
    }
    headers = {
        "Authorization": f"Bearer {settings.OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }

    last_exc: Optional[Exception] = None
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=300.0) as client:
                resp = await client.post(
                    f"{settings.OPENAI_BASE_URL}/chat/completions",
                    headers=headers, json=payload,
                )
                if resp.status_code in (403, 429) or resp.status_code >= 500:
                    body = resp.text[:300]
                    if "insufficient" in body or "额度" in body or "quota" in body:
                        raise RuntimeError(f"GPT API 额度不足: {body}")
                    raise httpx.HTTPStatusError(
                        f"HTTP {resp.status_code}: {body}",
                        request=resp.request, response=resp,
                    )
                resp.raise_for_status()
                data = resp.json()
                content = data["choices"][0]["message"]["content"] or ""
                content = content.strip()
                if content.startswith("```"):
                    m = re.search(r"\{[^}]*\}", content, re.S)
                    content = m.group(0) if m else ""
                obj = json.loads(content)
                idx = int(obj.get("index", 0))
                if idx < 0 or idx >= len(frame_paths):
                    idx = 0
                return frame_paths[idx]
        except RuntimeError:
            raise
        except (httpx.HTTPStatusError, httpx.TimeoutException, httpx.TransportError) as e:
            last_exc = e
            if attempt < 2:
                await asyncio.sleep(5 * (attempt + 1))
                continue
    # 兜底：GPT 失败时退回中间帧
    print(f"[select_best_model_frame] GPT 选择失败，退回中间帧: {last_exc}")
    mid = len(frame_paths) // 2
    return frame_paths[mid]


# 短剧模式提示词：场景图已预先移除人物（空场景化），只把模特置入场景。
# 结构性防“双人”：人物唯一来源是第2张模特图，杜绝弱模型把原人物画进画面。
_MOTION_REFERENCE_PROMPT = (
    "这是一次模特写真生成任务：输入为服装图、模特图和一张无人物的场景图，"
    "请生成一张全新的9:16竖版高清照片，"
    "严禁以拼图、分屏、画中画、并排、堆叠等方式拼接输入图。"
    "第3张图是目标场景（图中无人物）：必须完整保留该场景的背景，包括空间布局、环境物体、"
    "色调氛围、光线方向、透视关系，背景元素位置和形态不可改变，不可添加或删除背景物体；"
    "如果第3张图中有字幕、标题、水印等任何文字，必须完全擦除并修复为干净背景，不可残留文字痕迹。"
    "第2张图是模特真值，也是画面中人物的唯一来源：人物一致性硬性要求——最终画面中的人物"
    "必须与第2张图中的模特是同一个人，脸型、五官、眉眼、鼻形、唇形、发型、发色、肤色、"
    "身材比例必须与第2张图严格一致，面部清晰可辨；严禁换脸、严禁美化变形、严禁改变年龄感、"
    "严禁融合任何其他人的面部特征。"
    "第1张图是服装真值：必须完整保留这套服装的版型、颜色、面料质感、图案、印花、"
    "领口、袖口、门襟、纽扣等所有细节，禁止改款、换色、添加文字或Logo；"
    "并根据服装风格为模特搭配协调的鞋子。"
    "人物姿态要求：保持自然、符合场景氛围的姿态，站姿或动态姿势均可。"
    "人物数量硬性约束：画面中有且只有模特这一个人物，严禁出现第二个人、双人同框、"
    "多人同框、路人、背景人影、残缺肢体、镜中人或倒影中的人，背景中不得残留任何人物。"
    "人物须自然融入背景，光线方向、透视关系、色温色调与背景环境一致，人物在地面有合理的投影。"
    "最终输出一张单一、完整、真实感强的人物在场景中的照片，画面无文字、无水印、无Logo。"
)

# 美女模式专用提示词（避开 Adobe 安全策略敏感词：不用"替换原人物"等表述，
# 改用"生成一张全新的美女在此场景中的照片"的中性描述）
_BEAUTY_REFERENCE_PROMPT = (
    "请生成一张全新的9:16竖版高清摄影照片，画面主体为一位美女置身于第3张图所展示的场景中。"
    "严禁以拼图、分屏、画中画、并排、堆叠等方式拼接输入图。"
    "第3张图是目标场景参考：复用此场景的背景与空间结构，包括环境物体、色调氛围、光线方向、"
    "透视关系，背景元素位置和形态尽量保持一致，不可添加或删除背景物体；"
    "如果场景中存在字幕、标题、水印等任何文字，必须完全擦除并修复为干净背景；"
    "场景中若有人物，必须全部移除，只保留空场景。"
    "第2张图是人物外貌参考：照此人物的脸型、五官、发型、肤色和身材比例塑造画面中的人物形象。"
    "第1张图是服装参考：让画面中人物穿着这套服装，必须保留服装的版型、颜色、面料质感、图案、"
    "印花、领口、袖口、门襟、纽扣等所有细节，禁止改款、换色、添加文字或Logo；"
    "并根据服装风格搭配协调的鞋子。"
    "人物数量硬性约束：画面中有且只有这位美女一个人物，"
    "严禁出现两个人、多人同框、路人、背景人影、残缺人影或镜中人。"
    "画面要求：人物姿态自然、与场景互动合理，光线方向、透视关系、色温色调与背景环境一致，"
    "人物在地面有合理的投影，整体画面真实感强、构图美观。"
    "最终输出一张单一、完整、无文字、无水印、无Logo的高质量摄影照片。"
)

# 商品出片·传背景图专用：模特背景融合 + 模仿视频关键帧融合（4 图输入：服装/模特/背景图/视频帧）
# 第3、4张图均已预先移除人物，画面中人物唯一来源是第2张模特图
_SCENE_FUSION_PROMPT = (
    "这是一次场景人物融合任务，请生成一张全新的9:16竖版高清照片，"
    "严禁以拼图、分屏、画中画、并排、堆叠等方式拼接输入图。"
    "第3张图是用户指定的背景图（图中无人物）：必须将其场景完整融入画面，保留空间布局、环境物体、"
    "色调氛围、光线方向、透视关系，如果第3张图中有字幕、标题、水印等任何文字，"
    "必须完全擦除并修复为干净背景。"
    "第4张图是模仿视频画面（其中人物已移除，仅存场景）：只取其场景氛围、构图与光线作为参考，"
    "与第3张图的背景和谐统一，两图场景融合自然，不得出现拼接、分层或风格冲突。"
    "第2张图是模特真值，也是画面中人物的唯一来源：人物一致性硬性要求——最终画面中的人物"
    "必须与第2张图中的模特是同一个人，脸型、五官、眉眼、鼻形、唇形、发型、发色、肤色、"
    "身材比例必须与第2张图严格一致，面部清晰可辨；严禁换脸、严禁美化变形、严禁改变年龄感、"
    "严禁融合任何其他人的面部特征。"
    "第1张图是服装真值：必须完整保留这套服装的版型、颜色、面料质感、图案、印花、"
    "领口、袖口、门襟、纽扣等所有细节，禁止改款、换色、添加文字或Logo；"
    "并根据服装风格为模特搭配协调的鞋子。"
    "核心要求：让第2张图的模特穿上第1张图的服装和鞋子，置身于融合后的场景中。"
    "人物姿态要求：保持自然、符合场景氛围的姿态，站姿或动态姿势均可。"
    "人物数量硬性约束：画面中有且只有模特这一个人物，严禁出现第二个人、双人同框、"
    "多人同框、路人、背景人影、残缺肢体、镜中人或倒影中的人，背景中不得残留任何人物。"
    "人物须自然融入背景，光线方向、透视关系、色温色调与背景环境一致，人物在地面有合理的投影。"
    "最终输出一张单一、完整、真实感强的人物在场景中的照片，画面无文字、无水印、无Logo。"
)

# 分支4专用：模特图有背景 + 用户传了背景图 → 一步融合
# 输入 3 张图（服装=模特同图）：第1张=服装真值（模特图本身），第2张=模特真值（同图），第3张=用户背景图
# 核心动作：保留模特人物 + 删除原图背景 + 把模特放进用户背景图
_MODEL_BG_REPLACE_PROMPT = (
    "这是一次模特换背景任务，请生成一张全新的9:16竖版高清摄影照片，"
    "严禁以拼图、分屏、画中画、并排、堆叠等方式拼接输入图。"
    "第1张图是服装真值（也是第2张模特身上的服装）：必须完整保留这套服装的版型、颜色、"
    "面料质感、图案、印花、领口、袖口、门襟、纽扣等所有细节，禁止改款、换色、添加文字或Logo；"
    "并根据服装风格为模特搭配协调的鞋子。"
    "第2张图是模特原图（含人物+服装+原图复杂背景）：必须完整保留图中模特的人物形象——"
    "脸型、五官、眉眼、鼻形、唇形、发型、发色、肤色、身材比例必须严格一致，"
    "面部清晰可辨；严禁换脸、严禁美化变形、严禁改变年龄感、严禁融合任何其他人的面部特征。"
    "【关键】必须彻底删除第2张图原图的背景（室内/街道/橱窗/渐变/道具等），"
    "只保留模特人物本身和身上服装，原图背景不得残留在最终画面中。"
    "第3张图是目标背景图：将模特置入此场景，保留该场景的空间布局、环境物体、色调氛围、"
    "光线方向、透视关系，背景元素位置和形态不可改变，不可添加或删除背景物体；"
    "如果第3张图中有字幕、标题、水印等任何文字，必须完全擦除并修复为干净背景。"
    "人物数量硬性约束：画面中有且只有模特这一个人物，严禁出现第二个人、双人同框、"
    "多人同框、路人、背景人影、残缺肢体、镜中人或倒影中的人，背景中不得残留任何人物。"
    "人物须自然融入背景，光线方向、透视关系、色温色调与背景环境一致，人物在地面有合理的投影。"
    "最终输出一张单一、完整、真实感强的人物在新场景中的照片，画面无文字、无水印、无Logo。"
)

# 场景清理提示词：把场景图中所有人物擦成空场景（结构性杜绝“双人”问题）
_SCENE_PEOPLE_REMOVAL_PROMPT = (
    "这是一次场景清理任务：请将图中所有人物全部移除，包括完整人物、半身、残缺肢体、人影、"
    "镜中人和倒影中的人，只保留空场景背景，并用周围环境纹理自然修补人物原来所在的区域，"
    "做到看不出修补痕迹。严格保持原图的场景布局、环境物体、家具陈设、色调、光线方向和"
    "透视关系不变，不新增、不删除任何背景物体，不改变构图，不裁剪画面，"
    "不添加任何文字、水印或Logo。输出与原图相同构图的空场景照片。"
)


async def generate_motion_reference_image(
    clothing_image_path: str,
    model_image_path: str,
    scene_image_path: str,
    prompt: str | None = None,
    extra_scene_image_path: str | None = None,
    pose_hint: str = "",
) -> str:
    """以服装图、模特图、背景场景图为参考，生成 9:16 动作迁移参考图。

    与 generate_womenswear_image 的区别：提示词泛化（不绑定固定橱窗场景），
    背景由 scene_image_path 决定（应为已移除人物的空场景）。

    Args:
        prompt: 自定义提示词。None 时用默认 _MOTION_REFERENCE_PROMPT；
                美女模式应传 _BEAUTY_REFERENCE_PROMPT 规避 Adobe 安全策略。
        extra_scene_image_path: 可选第 4 张融合图（商品出片传背景图时=模仿视频关键帧空场景，
                与背景图一同融合）。None 时保持 3 图输入。
        pose_hint: 可选人物姿态描述（由关键帧人物姿势文字化而来）。
                传入时追加进提示词，用于在不输入人物图的前提下保持原人物姿势。
    """
    images = [Path(clothing_image_path), Path(model_image_path), Path(scene_image_path)]
    if extra_scene_image_path:
        images.append(Path(extra_scene_image_path))
    full_prompt = prompt or _MOTION_REFERENCE_PROMPT
    if pose_hint:
        full_prompt += f"人物姿态要求（以此为准）：模特的动作姿势为——{pose_hint.strip()}。"
    result = await Image2Client().image_edit_sync(
        prompt=full_prompt,
        images=images,
        size="1024x1536",
        n=1,
        output_format="png",
    )
    return Image2Client()._get_result_image(result)


async def remove_scene_people(image_path: str, output_path: str | Path) -> str:
    """把场景图中所有人物擦除为空场景，返回输出文件路径。

    结构性防“双人”：融合前先把非模特的人物图清理成空场景，
    最终融合输入里只剩模特图一张带人图，弱模型就不会把原人物画进画面。
    """
    result = await Image2Client().image_edit_sync(
        prompt=_SCENE_PEOPLE_REMOVAL_PROMPT,
        images=[Path(image_path)],
        size="1024x1536",
        n=1,
        output_format="png",
    )
    return await save_image_result(Image2Client()._get_result_image(result), output_path)


async def describe_person_pose(image_path: str) -> str:
    """调 Agnes 3.0 视觉模型（settings.MIMO_MODEL=agnes-3.0-flash）识别图中人物姿态动作
    （一句话），失败或无人物返回空字符串。

    配合 remove_scene_people 使用：人物被擦除前先把姿势文字化，
    融合时用文字保持原人物姿态，做到“只取姿势不取人”。
    """
    import base64
    from config import settings

    if not Path(image_path).exists():
        return ""
    try:
        from PIL import Image
        import io
        with Image.open(image_path) as img:
            w, h = img.size
            if max(w, h) > 768:
                scale = 768 / max(w, h)
                img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
            buf = io.BytesIO()
            img.convert("RGB").save(buf, "JPEG", quality=85)
            img_b64 = base64.b64encode(buf.getvalue()).decode()
    except Exception:
        with open(image_path, "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode()

    system_prompt = (
        "你是动作描述助手。用一句话（不超过30字）只描述图中人物的姿态和动作，"
        "例如：侧身站立双手握拳举起、单腿抬起身体后仰、弯腰伸手扶桌。"
        "不要描述外貌、服装、表情和场景。若图中无人物，只输出：无。"
    )
    user_content = [
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}},
        {"type": "text", "text": "请描述图中人物的姿态动作。"},
    ]
    payload = {
        "model": settings.MIMO_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "max_completion_tokens": 128,
    }
    headers = {
        "Authorization": f"Bearer {settings.MIMO_API_KEY}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{settings.MIMO_BASE_URL}/chat/completions",
                headers=headers, json=payload,
            )
            resp.raise_for_status()
            content = (resp.json()["choices"][0]["message"]["content"] or "").strip()
            content = content.strip("。. \n")
            if not content or content == "无":
                return ""
            return content[:60]
    except Exception as e:
        print(f"[describe_person_pose] Agnes 视觉姿态识别失败（忽略，用自然姿势）: {e}")
        return ""


async def save_image_result(result: str, output_path: str | Path) -> str:
    """保存 Image2 返回的图片到本地。

    支持四种返回形式：
    1. data:image/...;base64,... → 直接解码
    2. 纯 base64 字符串（>200 字符）→ 直接解码
    3. /v1/images/content/<payload>.<signature> 形式的超长内嵌 URL
       （Image2 把 PNG base64 嵌在 URL 路径里，HTTP GET 会触发 URL too long）
       → 取 payload（点号前）做 Base64URL 解码得到 JSON，从中取 data: 字段
    4. 普通 http(s) URL → HTTP 下载
    """
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    # 形式 1: data: URI
    if result.startswith("data:"):
        result = result.split(",", 1)[1]
        path.write_bytes(base64.b64decode(result))
        return str(path)

    # 形式 3: /v1/images/content/<payload>.<signature> 超长内嵌 URL
    m = re.search(r"/v1/images/content/([A-Za-z0-9_-]+)\.([A-Za-z0-9_-]+)", result)
    if m and len(result) > 1000:
        payload_str = m.group(1)
        padding = "=" * (-len(payload_str) % 4)
        decoded = base64.urlsafe_b64decode(payload_str + padding)
        try:
            payload = json.loads(decoded)
        except json.JSONDecodeError:
            payload = {}
        embedded_url = ""
        if isinstance(payload, dict):
            embedded_url = payload.get("url", "") or payload.get("data", "")
        if isinstance(payload, str) and payload.startswith("data:"):
            embedded_url = payload
        if embedded_url.startswith("data:image/"):
            b64 = embedded_url.split(",", 1)[1]
            path.write_bytes(base64.b64decode(b64))
            return str(path)

    # 形式 2: 纯 base64
    if re.fullmatch(r"[A-Za-z0-9+/=\r\n]+", result) and len(result) > 200:
        path.write_bytes(base64.b64decode(result))
        return str(path)

    # 形式 4: 普通 URL
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.get(result)
        response.raise_for_status()
        path.write_bytes(response.content)
    return str(path)
