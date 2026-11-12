"""
Agnes 生图 API 客户端
对接 https://api.agnes-ai.cn/v1（模型 agnes-image-2.5-flash）

路由说明：
- POST /v1/images/generations → 文生图 / 图生图 / 多图合成（统一 JSON 入口）
  - 图生图参考图放 extra_body.image（公共 URL 或 Data URI 数组）
  - response_format 必须放 extra_body（顶层放置会报错）
  - size 用 1K/2K/3K/4K 档位 + ratio 宽高比
"""
import base64
import io
import json
import os
import re
import urllib.request
import httpx
import asyncio
from pathlib import Path
from typing import Optional, Dict, Any, List, Union
from config import settings

from PIL import Image


def _compress_image_bytes(content: bytes, max_side: int = 1024, quality: int = 85, threshold: int = 300_000) -> tuple[bytes, str]:
    """如果图片字节超过 threshold，用 PIL 压缩到最长边 max_side，转 JPEG。

    返回 (content, content_type)。小图原样返回。
    """
    if len(content) <= threshold:
        return content, "image/png"
    try:
        img = Image.open(io.BytesIO(content))
        if img.mode in ("RGBA", "LA", "P"):
            bg = Image.new("RGB", img.size, (255, 255, 255))
            if img.mode == "P":
                img = img.convert("RGBA")
            bg.paste(img, mask=img.split()[-1] if img.mode in ("RGBA", "LA") else None)
            img = bg
        elif img.mode != "RGB":
            img = img.convert("RGB")
        w, h = img.size
        if max(w, h) > max_side:
            ratio = max_side / max(w, h)
            img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality)
        return buf.getvalue(), "image/jpeg"
    except Exception:
        return content, "image/png"


def _extract_image_url_or_b64(data_list: list) -> str:
    """从 data[0] 中提取 url 或 b64_json（url 为空时 fallback 到 b64_json）"""
    if not data_list:
        raise Exception("API 返回 data 为空")
    item = data_list[0]
    url = item.get("url", "")
    if url:
        return url
    b64 = item.get("b64_json", "")
    if b64:
        return b64
    raise Exception(f"API 返回格式异常: {item}")


_AGNES_RATIOS = [
    ("1:1", 1.0), ("3:4", 0.75), ("4:3", 4 / 3), ("16:9", 16 / 9),
    ("9:16", 9 / 16), ("2:3", 2 / 3), ("3:2", 1.5), ("21:9", 21 / 9),
]


def _to_agnes_size(size: str) -> tuple[str, str]:
    """把 WxH 尺寸映射为 Agnes 的 (size 档位, ratio)。

    Agnes 原生输出是 1K/2K/3K/4K 档位 + 固定宽高比，
    精确尺寸会被标准化，因此按宽高比取最近档位请求。
    """
    if size in ("1K", "2K", "3K", "4K"):
        return size, "1:1"
    m = re.match(r"^(\d+)x(\d+)$", size or "")
    if not m:
        return "1K", "1:1"
    w, h = int(m.group(1)), int(m.group(2))
    ratio_value = w / h
    ratio = min(_AGNES_RATIOS, key=lambda kv: abs(kv[1] - ratio_value))[0]
    long_side = max(w, h)
    if long_side <= 1600:
        tier = "1K"
    elif long_side <= 2800:
        tier = "2K"
    elif long_side <= 4200:
        tier = "3K"
    else:
        tier = "4K"
    return tier, ratio


class Image2Client:
    """Agnes 生图 API 客户端"""

    def __init__(self):
        self.base_url = settings.IMAGE2_BASE_URL.rstrip("/")
        self.api_key = settings.IMAGE2_API_KEY
        self.default_model = settings.IMAGE2_MODEL or "gpt-image-2"

    # ------------------------------------------------------------------
    # 底层请求方法
    # ------------------------------------------------------------------

    async def text_to_image_sync(
        self,
        prompt: str,
        model: str = "",
        size: str = "1024x1024",
        n: int = 1,
        response_format: str = "url",
        output_format: str = "png",
    ) -> Dict[str, Any]:
        """同步文生图 POST /v1/images/generations"""
        model = model or self.default_model
        tier, ratio = _to_agnes_size(size)
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model,
            "prompt": prompt,
            "size": tier,
            "ratio": ratio,
            "extra_body": {"response_format": response_format},
        }

        async with httpx.AsyncClient(timeout=300.0) as client:
            resp = await client.post(
                f"{self.base_url}/images/generations",
                headers=headers,
                json=payload,
            )
            resp.raise_for_status()
            return resp.json()

    async def text_to_image_async(
        self,
        prompt: str,
        model: str = "",
        size: str = "1024x1024",
        n: int = 1,
        response_format: str = "url",
        output_format: str = "png",
        max_attempts: int = 80,
        interval: float = 5.0,
    ) -> Dict[str, Any]:
        """Agnes 无异步任务端点，委托同步文生图（参数名保持兼容）"""
        return await self.text_to_image_sync(
            prompt=prompt,
            model=model,
            size=size,
            n=n,
            response_format=response_format,
            output_format=output_format,
        )

    async def image_edit_sync(
        self,
        prompt: str,
        images: List[Union[str, bytes, Path]],
        model: str = "",
        size: str = "1024x1024",
        n: int = 1,
        response_format: str = "url",
        output_format: str = "png",
    ) -> Dict[str, Any]:
        """
        同步图生图 POST /v1/images/generations（JSON，参考图放 extra_body.image）
        images: 文件路径列表 / bytes 列表 / base64 字符串列表，混用也可
        """
        model = model or self.default_model
        tier, ratio = _to_agnes_size(size)
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        files_to_close: list = []

        def _prepare_data_uri(value: Union[str, bytes, Path]) -> str:
            """将各种输入统一转为 Data URI 字符串，自动压缩超大图片。"""
            if isinstance(value, Path) or (isinstance(value, str) and not value.startswith("data:")):
                content = Path(value).read_bytes()
            elif isinstance(value, str) and value.startswith("data:"):
                return value
            elif isinstance(value, bytes):
                content = value
            else:
                raise ValueError(f"不支持的图片输入类型: {type(value)}")

            content, content_type = _compress_image_bytes(content)
            files_to_close.append(content)
            return f"data:{content_type};base64,{base64.b64encode(content).decode()}"

        image_uris = [_prepare_data_uri(img) for img in images]
        payload = {
            "model": model,
            "prompt": prompt,
            "size": tier,
            "ratio": ratio,
            "extra_body": {"image": image_uris, "response_format": response_format},
        }

        try:
            async with httpx.AsyncClient(timeout=300.0) as client:
                max_attempts = 6
                for attempt in range(1, max_attempts + 1):
                    # #region debug-point A:request-shape
                    try:
                        _env = os.path.join(".dbg", "image2-url-too-long.env")
                        _cfg = dict(line.strip().split("=", 1) for line in open(_env, encoding="utf-8") if "=" in line)
                        _event = {"sessionId": _cfg.get("DEBUG_SESSION_ID", "image2-url-too-long"), "runId": "post-fix", "hypothesisId": "A", "location": "image2_client.py:image_edit_sync", "msg": "[DEBUG] Image2 request shape", "data": {"attempt": attempt, "image_count": len(image_uris), "prompt_chars": len(prompt), "size": size}}
                        urllib.request.urlopen(urllib.request.Request(_cfg.get("DEBUG_SERVER_URL", "http://127.0.0.1:7777/event"), data=json.dumps(_event).encode(), headers={"Content-Type": "application/json"}), timeout=2).read()
                    except Exception:
                        pass
                    # #endregion
                    try:
                        resp = await client.post(
                            f"{self.base_url}/images/generations",
                            headers=headers,
                            json=payload,
                        )
                        # #region debug-point B:response
                        try:
                            _env = os.path.join(".dbg", "image2-url-too-long.env")
                            _cfg = dict(line.strip().split("=", 1) for line in open(_env, encoding="utf-8") if "=" in line)
                            _event = {"sessionId": _cfg.get("DEBUG_SESSION_ID", "image2-url-too-long"), "runId": "post-fix", "hypothesisId": "B", "location": "image2_client.py:image_edit_sync", "msg": "[DEBUG] Image2 response", "data": {"status": resp.status_code, "response_chars": len(resp.text), "response_prefix": resp.text[:300]}}
                            urllib.request.urlopen(urllib.request.Request(_cfg.get("DEBUG_SERVER_URL", "http://127.0.0.1:7777/event"), data=json.dumps(_event).encode(), headers={"Content-Type": "application/json"}), timeout=2).read()
                        except Exception:
                            pass
                        # #endregion
                    except httpx.HTTPError as exc:
                        if attempt == max_attempts:
                            raise RuntimeError(
                                f"Image2 图生图连续网络异常（{max_attempts}次）: {exc}"
                            ) from exc
                        delay = 5 * (2 ** (attempt - 1))
                        print(f"  ⚠ Image2 网络异常，第{attempt}/{max_attempts}次，{delay}秒后重试: {exc}")
                        await asyncio.sleep(delay)
                        continue

                    if resp.status_code in (502, 503, 504):
                        if attempt == max_attempts:
                            resp.raise_for_status()
                        delay = 5 * (2 ** (attempt - 1))
                        print(
                            f"  ⚠ Image2 HTTP {resp.status_code}，"
                            f"第{attempt}/{max_attempts}次，{delay}秒后重试..."
                        )
                        await asyncio.sleep(delay)
                        continue

                    # 4xx 错误：提取服务端返回的具体错误信息
                    if resp.status_code >= 400:
                        try:
                            err_body = resp.json()
                            err_msg = (
                                err_body.get("error", {}).get("message")
                                or err_body.get("message")
                                or err_body.get("detail")
                                or resp.text[:500]
                            )
                        except Exception:
                            err_msg = resp.text[:500] if resp.text else "无响应体"

                        # 服务端明确建议重试的错误（upstream timeout / try again）→ 重试
                        # 这类通常是上游图片流临时故障，不是请求体过大
                        err_lower = str(err_msg).lower()
                        is_retryable_4xx = any(kw in err_lower for kw in [
                            "timed out", "timeout", "try again", "upstream",
                            "temporary", "temporarily", "service unavailable",
                        ])

                        # Adobe 内容审核拒绝（451 / safety policy）属于永久性错误，不重试
                        # 注意：Adobe 的 "...try again" 是指"调整提示词后再试"，不是"立即重试同一请求"
                        is_content_blocked = (
                            resp.status_code == 451
                            or any(kw in err_lower for kw in [
                                "content blocked", "safety policy",
                                "内容审核", "内容政策", "adobe content",
                            ])
                        )
                        if is_content_blocked:
                            raise RuntimeError(
                                f"Image2 HTTP {resp.status_code} 内容审核拒绝（不可重试）: {err_msg}"
                                f"（提示: 提示词或参考图可能涉及真实人物/名人肖像、暴力、成人内容、"
                                f"受版权保护的角色或品牌等，请调整描述或更换参考图后重试任务）"
                            )

                        if is_retryable_4xx and attempt < max_attempts:
                            delay = 5 * (2 ** (attempt - 1))
                            print(f"  ⚠ Image2 HTTP {resp.status_code}（上游临时故障），第{attempt}/{max_attempts}次，{delay}秒后重试: {err_msg}")
                            await asyncio.sleep(delay)
                            continue

                        # 真正的请求体过长类错误（400/413/414 且非超时）→ 不重试
                        if attempt < max_attempts and resp.status_code in (400, 413, 414):
                            raise RuntimeError(
                                f"Image2 HTTP {resp.status_code}: {err_msg}"
                                f"（提示: 可能是图片过大或多图导致请求超限，建议减少图片数量或降低分辨率）"
                            )
                        if attempt == max_attempts:
                            raise RuntimeError(f"Image2 HTTP {resp.status_code}: {err_msg}")
                        print(f"  ⚠ Image2 HTTP {resp.status_code}: {err_msg}，第{attempt}/{max_attempts}次重试...")
                        await asyncio.sleep(3)
                        continue

                    resp.raise_for_status()
                    return resp.json()
        finally:
            files_to_close.clear()

    # ------------------------------------------------------------------
    # 便捷方法：提取结果
    # ------------------------------------------------------------------

    def _get_result_image(self, result: Dict[str, Any]) -> str:
        """从同步/异步响应中统一提取图片 url 或 b64_json"""
        # 同步响应: {"data": [...]}
        if "data" in result:
            return _extract_image_url_or_b64(result["data"])
        # 异步响应: {"response": {"data": [...]}}
        if "response" in result and "data" in result.get("response", {}):
            return _extract_image_url_or_b64(result["response"]["data"])
        raise Exception(f"无法从响应中提取图片: {list(result.keys())}")

    # ------------------------------------------------------------------
    # 业务方法
    # ------------------------------------------------------------------

    async def generate_product_image(
        self,
        product_image_base64: str,
        product_type: str,
        product_name_hint: Optional[str] = None,
    ) -> str:
        """生成产品标准化图片（图生图）

        Args:
            product_name_hint: 产品名称提示（用于 model_full 时显式告知产品是什么，
                              避免模型把人物手中/身上的产品误判为服装保留下来）
        """
        prompt_map = {
            "product": (
                "请根据上传的产品图片，提取产品本体，生成标准化产品参考图。"
                "严格保留产品真实结构、外形轮廓、比例、颜色、材质纹理、表面工艺、五金、装饰件。"
                "删除人物、手部、服装、桌面、背景、杂物、包装、阴影干扰。"
                "输出为纯白背景产品图。若产品是立体物品，生成正视图、侧视图、背视图三视图。"
                "画面要求：白底干净、光线均匀、细节清晰、无多余道具、无文字、无水印。"
            ),
            "clothing": (
                "请根据上传的服装图片，提取服装本体，生成标准化白底服装平铺图。"
                "严格保留服装的真实版型、颜色、面料质感、纹理、图案、门襟、领口、袖口、下摆。"
                "删除人物身体、手部、头发、背景、衣架、桌面、杂物、阴影干扰。"
                "输出为纯白背景服装平铺图。服装需要自然摊平，正面朝上，衣身完整展开。"
                "画面要求：白底干净、边缘清晰、面料纹理真实、图案完整、不变形。"
            ),
            "model": (
                "请根据上传的人物参考图，提取人物形象，生成标准化人物参考图。"
                "严格保留人物的真实面部特征、五官比例、脸型、发型、发色、肤色、身材比例。"
                "删除原图中的复杂背景、杂物、文字、水印和无关元素，只保留人物主体。"
                "输出为干净的白底或浅灰底人物参考图。人物自然站立，面向镜头，姿态端正。"
                "人物必须呈现真实摄影质感，皮肤需要具备高精度真实皮肤质感。"
                "画面要求：白底或浅灰底，人物完整，光线均匀，肤色自然，边缘干净。"
            ),
            "scene": (
                "请根据上传的场景图片，提取场景环境，生成场景参考图。"
                "严格保留原场景的空间结构、背景布局、墙面、地面、家具位置、灯光方向、色彩氛围。"
                "保留场景中所有静态物品：桌面上的商品、塑料袋、货架、装饰物、道具等全部保留，严禁删除。"
                "仅删除画面中的人物主体、人物手持的产品、手部、临时遮挡物、文字、水印。"
                "输出为无人物的空场景图（保留所有场景物品），保持原图构图和镜头机位。"
                "画面要求：光线一致、透视正确、材质真实、背景清晰、无多余文字、无水印。"
            ),
            "model_full": (
                "请根据上传的人物参考图，提取人物全身形象，生成标准化人物全身参考图。"
                "严格保留人物的真实面部特征、五官比例、脸型、发型、发色、肤色、身材比例。"
                "重点保留人物穿着的完整服装：上衣、裤子/裙子、鞋子、配饰的款式、颜色、图案、面料纹理必须完整呈现。"
                "如果原图中服装被物品遮挡，请根据可见部分合理补全完整服装，确保服装款式连贯一致。"
                "删除原图中的复杂背景、杂物、产品、文字、水印和无关元素，只保留人物主体。"
                "输出为干净的白底或浅灰底人物全身参考图。人物自然站立，面向镜头，姿态端正，全身可见。"
                "人物必须呈现真实摄影质感，皮肤需要具备高精度真实皮肤质感。"
                "画面要求：白底或浅灰底，人物全身完整（头顶到脚底），光线均匀，肤色自然，服装细节清晰，边缘干净。"
            ),
        }

        if product_type not in prompt_map:
            raise ValueError(f"不支持的产品类型: {product_type}")

        prompt = prompt_map[product_type]
        # model_full 场景：显式告知产品名称，避免模型把人物手中/身上的产品（如塑料袋、背心袋）
        # 误判为服装的一部分保留下来
        if product_type == "model_full" and product_name_hint:
            prompt = (
                prompt
                + f"\n\n特别注意：本画面中的产品是「{product_name_hint}」，这是产品不是服装。"
                f"如果人物手中持有、身上缠绕、或怀中抱着「{product_name_hint}」及其类似形态的物品"
                f"（塑料袋/包装袋/背心袋/垃圾袋等），必须从人物身上/手中彻底删除，"
                f"不要将其作为服装、配饰、披风、围裙等任何形式保留下来。"
                f"删除产品后，根据可见部分合理补全被产品遮挡的服装。"
            )
        result = await self.image_edit_sync(
            prompt=prompt,
            images=[f"data:image/jpeg;base64,{product_image_base64}"],
            size="1024x1024",
        )
        return self._get_result_image(result)

    async def replace_shot_elements(
        self,
        original_shot_base64: str,
        product_image_base64: Optional[str] = None,
        model_image_base64: Optional[str] = None,
        shot_description: str = "",
        has_product: bool = True,
        has_person: bool = True,
    ) -> str:
        """替换分镜中的产品和人物（图生图，多参考图）

        has_product=False 时跳过产品参考图（如分镜脚本中"产品"为"无"）
        has_person=False 时跳过人物参考图（如分镜脚本中"人物"为"无"）
        """
        # 构建参考图列表：第1张始终是原图
        images = [f"data:image/jpeg;base64,{original_shot_base64}"]

        # 构建提示词中的图片角色说明
        role_desc = ["第1张图：需要修改的原始分镜画面，必须保持整体构图和结构不变"]

        if has_product and product_image_base64:
            images.append(f"data:image/jpeg;base64,{product_image_base64}")
            role_desc.append("第2张图：产品标准参考图，仅用于替换画面中的产品外观，不要额外添加产品到画面中")

        person_idx = len(images) + 1
        if has_person and model_image_base64:
            images.append(f"data:image/jpeg;base64,{model_image_base64}")
            role_desc.append(f"第{person_idx}张图：人物标准参考图，仅用于替换画面中的人物外观")

        prompt = (
            "对已有分镜结构进行人物与产品替换生成。\n\n"
            + "\n".join(role_desc) + "\n\n"
            "核心目标：以第1张原图为基底，仅替换画面中的人物与产品，不添加原图中不存在的产品或人物。\n\n"
            "强约束：\n"
            "1. 严禁改变分镜结构（Shot数量、顺序、构图、景别、动作、节奏必须完全一致）\n"
            "2. 严禁重构画面（不允许新增/删除动作，不允许改变镜头设计、场景、光线）\n"
            "3. 唯一允许修改：人物和产品的外观\n"
            "4. 如果原图中没有产品，不要添加产品\n"
            "5. 如果原图中没有人物，不要添加人物\n\n"
            "人物替换规则：\n"
            "- 严格参考人物标准图\n"
            "- 脸型、五官比例、发型、发色、肤色、身材比例保持一致\n"
            "- 姿态必须完全继承原分镜动作\n\n"
            "产品替换规则：\n"
            "- 严格参考产品标准图\n"
            "- 保持结构、比例、材质逻辑、五金位置不变\n\n"
            f"分镜描述：{shot_description}\n\n"
            "输出要求：与原图构图完全一致的分镜图，仅视觉替换，不新增任何元素。"
        )

        result = await self.image_edit_sync(
            prompt=prompt,
            images=images,
            size="1024x1024",
        )
        return self._get_result_image(result)

    async def generate_storyboard(
        self,
        shots_images: list,
        script_json: Dict[str, Any],
    ) -> str:
        """生成故事板海报（图生图，多参考图）"""
        import json

        script_str = json.dumps(script_json, ensure_ascii=False, indent=2)
        prompt = (
            "生成一张「电商短视频分镜故事板海报」，采用专业影视分镜 + 带货脚本结构设计。\n\n"
            "整体为一张横向信息海报，风格为「商业短视频分镜分析表 + 电商商品展示脚本 + 声音导演表」。\n\n"
            "画面结构（强制栅格系统）：\n"
            "- 画布比例：16:9 横版\n"
            "- 强制三段式栅格布局：\n"
            "  ① Header 顶部区域（固定高度 18%）：主标题、副标题、参数信息、风格标签\n"
            "  ② Main 中间主体区域（固定高度 64%）：\n"
            "     - 左侧（70%宽度）：分镜网格 Shot Grid\n"
            "     - 右侧（30%宽度）：人物 + 产品 + 声音导演表展示区\n"
            "  ③ Footer 底部区域（固定高度 18%）：视觉设计、拍摄设备、节奏结构、音乐建议、风格标签\n\n"
            f"分镜脚本数据：\n{script_str}\n\n"
            "视觉风格要求：\n"
            "- 商业提案风、电商广告分镜板、轻工业设计感\n"
            "- 信息密度高、视觉干净、理性、清晰\n"
            "- 使用白色/浅灰底色、黑色文字\n"
            "- 使用细线框、表格、标签、模块化卡片\n\n"
            "强制禁止项：\n"
            "- 禁止任何元素跨越 Header/Main/Footer 边界\n"
            "- 禁止文字压图、图片压文字、区域重叠\n"
            "- 禁止删减 JSON 字段、省略「声音语气」\n"
            "- 必须严格栅格系统执行"
        )

        result = await self.image_edit_sync(
            prompt=prompt,
            images=[f"data:image/jpeg;base64,{img}" for img in shots_images],
            size="1536x1024",
        )
        return self._get_result_image(result)

    # ------------------------------------------------------------------
    # 9 宫格带货工作流：基于产品图 i2i 生成 N 段 × 9 张关键帧
    # ------------------------------------------------------------------

    async def _save_image_to_disk(self, result: str, output_path: Path) -> str:
        """保存 Image2 返回的图片到本地磁盘。

        支持四种返回形式：
        1. data:image/...;base64,... → 直接解码
        2. 纯 base64 字符串（>200 字符）→ 直接解码
        3. /v1/images/content/<payload>.<signature> 超长内嵌 URL → Base64URL 解码后取 data 字段
        4. 普通 http(s) URL → HTTP 下载
        """
        import re
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # 形式 1: data: URI
        if result.startswith("data:"):
            b64_data = result.split(",", 1)[1]
            output_path.write_bytes(base64.b64decode(b64_data))
            return str(output_path)

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
            elif isinstance(payload, str) and payload.startswith("data:"):
                embedded_url = payload
            if embedded_url.startswith("data:image/"):
                b64_data = embedded_url.split(",", 1)[1]
                output_path.write_bytes(base64.b64decode(b64_data))
                return str(output_path)

        # 形式 2: 纯 base64
        if re.fullmatch(r"[A-Za-z0-9+/=\r\n]+", result) and len(result) > 200:
            output_path.write_bytes(base64.b64decode(result))
            return str(output_path)

        # 形式 4: 普通 URL
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.get(result)
            response.raise_for_status()
            output_path.write_bytes(response.content)
        return str(output_path)

    async def generate_9grid_keyframes(
        self,
        image_prompts_json: dict,
        product_image_path: str,
        output_dir,
        size: str = "1024x1536",
        max_concurrency: int = 3,
    ) -> dict:
        """基于 9:16 竵屏图片提示词 + 产品参考图，生成 N 段 × 9 张关键帧。

        - 跨段共享：段(i+1) sc1 直接复用段(i) sc9 文件路径，不调用 LLM/image2。
        - 产品一致性：每张图均以原始产品图为参考 i2i 生成。
        - 输出位置：{output_dir}/seg{N}_sc{M}.png

        Args:
            image_prompts_json: 阶段 2 输出的 JSON（含 segments[].first_frame_prompts[]）
            product_image_path: 原始产品图绝对路径
            output_dir: 输出目录（Path 或 str）
            size: 图片尺寸，默认 1024x1536（接近 9:16）
            max_concurrency: 单段内最大并发数（控制 image2 速率）

        Returns:
            {
                "segments": [
                    {
                        "segment_id": int,
                        "images": [
                            {"sc": int, "timestamp_sec": str, "image_path": str, "is_shared": bool, "inherits_from": str?},
                            ...
                        ]
                    },
                    ...
                ]
            }
        """
        import asyncio
        from pathlib import Path as _Path

        output_dir = _Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        if not _Path(product_image_path).exists():
            raise FileNotFoundError(f"产品图不存在: {product_image_path}")

        # 解析输入 JSON
        segments = image_prompts_json.get("segments", [])
        if not segments:
            raise ValueError("image_prompts_json.segments 为空")

        # 段间图像文件路径缓存：{"seg{N}_sc{M}": absolute_path}
        path_cache: dict = {}

        async def _gen_one(segment_id: int, sc: int, prompt: str, timestamp_sec: str) -> dict:
            """生成单张关键帧（i2i：产品图 + prompt）。"""
            filename = f"seg{segment_id}_sc{sc}.png"
            out_path = output_dir / filename
            result = await self.image_edit_sync(
                prompt=prompt,
                images=[_Path(product_image_path)],
                size=size,
                output_format="png",
            )
            img_url_or_b64 = self._get_result_image(result)
            await self._save_image_to_disk(img_url_or_b64, out_path)
            abs_path = str(out_path)
            path_cache[f"segment_{segment_id}_sc{sc}"] = abs_path
            return {
                "sc": sc,
                "timestamp_sec": timestamp_sec,
                "image_path": abs_path,
                "is_shared": False,
            }

        async def _gen_segment(seg: dict) -> dict:
            """生成单段 9 张关键帧（含跨段 sc1 复用判断）。"""
            seg_id = seg.get("segment_id")
            prompts = seg.get("first_frame_prompts", [])
            images_result: list = []
            tasks_for_segment: list = []

            for p in prompts:
                sc = p.get("sc")
                ts = p.get("timestamp_sec", "")
                prompt_text = p.get("prompt", "")
                inherits_from = p.get("inherits_from")  # e.g. "segment_1_sc9"

                # 跨段复用：sc1 = 上段 sc9，物理同一张图
                if inherits_from and inherits_from in path_cache:
                    src_path = path_cache[inherits_from]
                    # 复用同一物理文件（不再调用 image2）
                    images_result.append({
                        "sc": sc,
                        "timestamp_sec": ts,
                        "image_path": src_path,
                        "is_shared": True,
                        "inherits_from": inherits_from,
                    })
                    # 同步登记到 cache，便于后续段继续复用
                    path_cache[f"segment_{seg_id}_sc{sc}"] = src_path
                else:
                    # 异步并发生成（受 max_concurrency 限制）
                    tasks_for_segment.append(_gen_one(seg_id, sc, prompt_text, ts))

            # 限速并发执行
            sem = asyncio.Semaphore(max_concurrency)

            async def _limited(coro):
                async with sem:
                    return await coro

            gathered = await asyncio.gather(*[_limited(t) for t in tasks_for_segment])
            # 把异步结果按 sc 顺序合并到 images_result
            async_by_sc = {g["sc"]: g for g in gathered}
            for p in prompts:
                sc = p.get("sc")
                if not any(r.get("sc") == sc for r in images_result):
                    if sc in async_by_sc:
                        images_result.append(async_by_sc[sc])
            # 按 sc 1..9 排序
            images_result.sort(key=lambda x: x["sc"])
            return {"segment_id": seg_id, "images": images_result}

        # 段间顺序执行（段(i+1) 依赖段(i) sc9 的物理路径）
        segment_results: list = []
        for seg in segments:
            seg_result = await _gen_segment(seg)
            segment_results.append(seg_result)

        return {"segments": segment_results}


# 全局实例
image2_client = Image2Client()
