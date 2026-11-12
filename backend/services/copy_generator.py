"""
抖音发布文案生成器

输入：视频路径（可选产品信息）
流程：
  1. ffmpeg 从视频中抽取一帧关键画面
  2. 调用 MiMo 多模态模型识别视频中服装类型
  3. 基于服装类型 + 抖音爆款文案套路，生成：
     - 20 字以内有噱头的标题（拉停留）
     - 5 个相关标签

输出：{"title": str, "tags": [str]}
"""
import io
import os
import re
import json
import base64
import asyncio
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Optional, Dict, Any

import httpx
from PIL import Image

from config import settings


# ─────────────────────────────────────────────────────────
# 内置提示词（服装识别 + 抖音爆款文案）
# ─────────────────────────────────────────────────────────
SYSTEM_PROMPT = """你是抖音女装带货爆款文案专家，擅长用「钩子+反差+痛点+悬念」拉停留。

任务流程：
1. 仔细看抽取的关键帧，识别视频中模特穿着的服装类型，包括：
   - 服装品类（连衣裙/卫衣/西装/牛仔裤/羽绒服/衬衫/T恤/半身裙/套装 等）
   - 风格标签（韩系/日系/法式/复古/街头/通勤/甜美/极简 等）
   - 主色与配色
   - 关键设计点（泡泡袖/开衩/收腰/落肩/拼接/印花 等）
   - 适合人群与场景（小个子/微胖/梨形/职场/约会/日常 等）
2. 基于识别出的服装特征，生成一条抖音带货短视频的标题/描述：
   - ≤ 20 个汉字
   - 必须有噱头（数字/反差/悬念/痛点任选其一）
   - 直接拉停留，禁止「点击查看」之类废话
   - 体现服装最大卖点（如显瘦/遮肉/显高/百搭/显瘦10斤）
3. 生成 5 个相关标签（不带 #，2-6 字短词），覆盖品类+风格+人群+场景+卖点。

输出严格 JSON（不要任何额外文字、不要 Markdown 代码块）：
{
  "clothing_type": "识别出的服装类型一句话描述",
  "title": "20字以内标题",
  "tags": ["标签1", "标签2", "标签3", "标签4", "标签5"]
}
"""


# ─────────────────────────────────────────────────────────
# ffmpeg 抽帧
# ─────────────────────────────────────────────────────────
def _extract_midframe(video_path: str, out_path: str) -> bool:
    """从视频中抽取中间时刻的一帧作为关键画面。

    使用 ffmpeg：先 probe 时长，再 -ss 跳到中点抽一帧。
    """
    if not os.path.exists(video_path):
        return False

    # probe 时长
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", video_path],
            capture_output=True, text=True, timeout=15,
        )
        dur = float(probe.stdout.strip() or "0")
    except Exception:
        dur = 0.0
    mid = dur / 2 if dur > 0 else 0.5

    try:
        subprocess.run(
            ["ffmpeg", "-y", "-ss", str(mid), "-i", video_path,
             "-frames:v", "1", "-q:v", "2", out_path],
            capture_output=True, timeout=30,
        )
        return os.path.exists(out_path) and os.path.getsize(out_path) > 0
    except Exception:
        return False


def _file_to_b64(file_path: str) -> str:
    """读取图片返回 base64 字符串（不带 data: 前缀）。"""
    with open(file_path, "rb") as f:
        return base64.b64encode(f.read()).decode()


def _compress_frame_b64(img_path: str, max_side: int = 1024, quality: int = 85) -> Optional[str]:
    """把抽帧图片缩放到长边 max_side 并转 JPEG base64，降低请求体积。"""
    try:
        with Image.open(img_path) as im:
            im = im.convert("RGB")
            w, h = im.size
            if max(w, h) > max_side:
                scale = max_side / max(w, h)
                im = im.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=quality, optimize=True)
            return base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return None


# ─────────────────────────────────────────────────────────
# 调用 MiMo 多模态
# ─────────────────────────────────────────────────────────
async def _call_mimo(frame_path: str, product_info: Optional[str]) -> Dict[str, Any]:
    """调用 MiMo-v2.5 多模态 API，返回解析后的 dict。

    用 MiMo 替代 GPT-5.5（GPT55 key 欠费）。MiMo-v2.5 支持 image_url 多模态输入。
    对 403/429/5xx 按指数退避（5/10/15s）重试最多 3 次；并对已知"空 content
    + finish_reason=length"故障做内部重试（最多 2 次）。
    """
    headers = {
        "Authorization": f"Bearer {settings.MIMO_API_KEY}",
        "Content-Type": "application/json",
    }

    content_list = []

    # 1. 关键帧图片（压缩后送 MiMo）
    if frame_path and os.path.exists(frame_path):
        img_b64 = _compress_frame_b64(frame_path)
        if img_b64:
            content_list.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"},
            })

    # 2. 文字指令
    text_parts = ["请仔细看图片，识别服装后生成抖音爆款文案。"]
    if product_info and product_info.strip():
        text_parts.append(f"产品补充信息：{product_info.strip()}")
    text_parts.append("严格按系统提示输出 JSON，不要任何额外解释。")
    content_list.append({"type": "text", "text": "\n".join(text_parts)})

    payload = {
        "model": settings.MIMO_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": content_list},
        ],
        "max_completion_tokens": 16384,
    }

    last_exc: Optional[Exception] = None
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=300.0) as client:
                resp = await client.post(
                    f"{settings.MIMO_BASE_URL}/chat/completions",
                    headers=headers,
                    json=payload,
                )
                if resp.status_code in (403, 429) or resp.status_code >= 500:
                    body = resp.text[:300]
                    if "insufficient" in body or "额度" in body or "quota" in body:
                        raise RuntimeError(
                            f"MiMo API 额度不足，请充值或更换 API Key。"
                            f"服务端返回: {body}"
                        )
                    raise httpx.HTTPStatusError(
                        f"HTTP {resp.status_code}: {body}",
                        request=resp.request, response=resp,
                    )
                resp.raise_for_status()
                data = resp.json()
                content = data["choices"][0]["message"]["content"] or ""
                finish_reason = data["choices"][0].get("finish_reason")
                # MiMo-v2.5 偶发返回空 content + finish_reason=length
                inner_retry = 0
                while not content.strip() and inner_retry < 2:
                    inner_retry += 1
                    print(f"[copy_generator] MiMo 返回空 content (finish_reason={finish_reason})，内部重试第 {inner_retry}/2 次...")
                    async with httpx.AsyncClient(timeout=300.0) as client2:
                        resp2 = await client2.post(
                            f"{settings.MIMO_BASE_URL}/chat/completions",
                            headers=headers,
                            json=payload,
                        )
                        resp2.raise_for_status()
                        data2 = resp2.json()
                        content = data2["choices"][0]["message"]["content"] or ""
                        finish_reason = data2["choices"][0].get("finish_reason")
                if not content.strip():
                    raise RuntimeError(
                        f"MiMo 连续返回空 content（finish_reason={finish_reason}），"
                        f"可能是推理 tokens 耗尽，请重试"
                    )
                return _parse_json(content)
        except RuntimeError:
            raise
        except httpx.HTTPStatusError as e:
            last_exc = e
            if attempt < 2:
                wait = 5 * (attempt + 1)
                print(f"[copy_generator] MiMo 调用 {e.response.status_code}，{wait}s 后重试（第 {attempt+1}/3 次）")
                await asyncio.sleep(wait)
            continue
        except (httpx.TimeoutException, httpx.TransportError) as e:
            last_exc = e
            if attempt < 2:
                wait = 5 * (attempt + 1)
                print(f"[copy_generator] MiMo 调用网络错误 {e}，{wait}s 后重试（第 {attempt+1}/3 次）")
                await asyncio.sleep(wait)
            continue
    raise RuntimeError(f"MiMo 调用失败（已重试 3 次）: {last_exc}") from last_exc


def _parse_json(content: str) -> Dict[str, Any]:
    """从 MiMo 返回中提取 JSON 对象。"""
    if not content:
        raise ValueError("MiMo 返回为空")

    # 优先取 ```json 代码块
    fenced = re.findall(r"```(?:json)?\s*([\s\S]*?)\s*```", content, re.I)
    block = fenced[0].strip() if fenced else None
    if not block:
        # 找最外层 { ... }
        m = re.search(r"\{[\s\S]*\}", content)
        if not m:
            raise ValueError("MiMo 返回中未找到 JSON 对象")
        block = m.group(0)

    try:
        obj = json.loads(block)
    except json.JSONDecodeError:
        # 兜底：第一个 { 到最后一个 }
        s, e = block.find("{"), block.rfind("}")
        if s != -1 and e > s:
            obj = json.loads(block[s:e+1])
        else:
            raise

    if not isinstance(obj, dict):
        raise ValueError(f"期望 JSON 对象，实际 {type(obj).__name__}")
    return obj


# ─────────────────────────────────────────────────────────
# 对外入口
# ─────────────────────────────────────────────────────────
async def generate_publish_copy(
    video_path: str,
    product_info: Optional[str] = None,
) -> Dict[str, Any]:
    """生成抖音发布文案。

    Args:
        video_path: 视频文件绝对路径
        product_info: 可选的产品补充信息

    Returns:
        {"clothing_type": str, "title": str, "tags": [str]}
    """
    if not os.path.exists(video_path):
        raise FileNotFoundError(f"视频不存在: {video_path}")

    # 1. 抽帧（临时文件）
    tmp_dir = Path(tempfile.gettempdir()) / "copy_gen"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    frame_path = str(tmp_dir / f"frame_{uuid.uuid4().hex[:8]}.png")
    has_frame = _extract_midframe(video_path, frame_path)

    if not has_frame:
        raise RuntimeError("从视频抽帧失败，无法生成文案")

    # 2. 调 MiMo
    try:
        result = await _call_mimo(frame_path, product_info)
    finally:
        # 清理临时帧
        try:
            if os.path.exists(frame_path):
                os.remove(frame_path)
        except Exception:
            pass

    # 校验字段
    title = str(result.get("title", "")).strip()
    tags = result.get("tags", [])
    if not isinstance(tags, list):
        tags = []
    tags = [str(t).strip().lstrip("#") for t in tags if str(t).strip()][:5]

    # 标题硬约束 ≤ 20 字
    if len(title) > 20:
        title = title[:20]

    return {
        "clothing_type": str(result.get("clothing_type", "")).strip(),
        "title": title,
        "tags": tags,
    }
