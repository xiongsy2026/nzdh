"""女装带货 5 步流水线。从 video-skill `_womenswear_run` 单独抽出。"""
from __future__ import annotations

import asyncio
import shutil
import time
from pathlib import Path

from config import settings
from womenswear.state import motion_tasks


def output_url(path: Path) -> str:
    try:
        return f"/outputs/{path.relative_to(Path(settings.OUTPUT_DIR)).as_posix()}"
    except Exception:
        return ""


def hydrate_task(task_id: str) -> dict | None:
    """内存丢失时从 task_store 恢复。"""
    if task_id in motion_tasks:
        return motion_tasks[task_id]
    from backend.services import task_store
    t_db = task_store.get(task_id)
    if not t_db or t_db.get("task_type") != "womenswear":
        return None
    motion_tasks[task_id] = {
        "status": t_db.get("status", "UNKNOWN"),
        "progress": t_db.get("progress", 0),
        "outputs": t_db.get("outputs", []),
        "error": t_db.get("error"),
        "params": t_db.get("params", {}),
        "stage": t_db.get("stage", ""),
        "rh_task_id": t_db.get("rh_task_id", ""),
        "intermediate": t_db.get("intermediate", {}),
        "extra": t_db.get("extra", {}),
        "product_title": t_db.get("extra", {}).get("product_title", ""),
        "raw_product_url": t_db.get("extra", {}).get("raw_product_url", ""),
        "flat_lay_url": t_db.get("extra", {}).get("flat_lay_url", ""),
        "showcase_url": t_db.get("extra", {}).get("showcase_url", ""),
        "source_video_url": t_db.get("extra", {}).get("source_video_url", ""),
    }
    return motion_tasks[task_id]


def task_logs(task_id: str) -> list:
    try:
        from backend.services import task_store
        logs = task_store.get_logs(task_id, limit=100)
        return [
            {
                "message": l.get("msg", ""),
                "level": l.get("level", "info"),
                "ts": l.get("ts", 0),
                "ts_str": time.strftime("%H:%M:%S", time.localtime(l.get("ts", 0))) if l.get("ts") else "",
            }
            for l in logs
        ]
    except Exception:
        return []


def set_step_rpa(task_id: str, stage_text: str, progress: int, log: bool = True) -> None:
    from backend.services import task_store as _ts
    motion_tasks[task_id].update({"stage": stage_text, "progress": progress})
    try:
        _ts.update(
            task_id,
            stage=stage_text,
            progress=progress,
            extra={**motion_tasks[task_id].get("extra", {}), "current_step": "step5", "link_type": "rpa"},
        )
        if log:
            _ts.add_log(task_id, stage_text)
    except Exception:
        pass


def _validate_source_video(p: Path) -> tuple[bool, str]:
    if not p.exists() or p.stat().st_size < 1024:
        return False, "文件不存在或过小"
    if p.stat().st_size < 400 * 1024:
        return False, f"文件大小 {p.stat().st_size/1024:.0f}KB < 400KB（疑似预览流）"
    try:
        try:
            from moviepy.editor import VideoFileClip
        except ImportError:
            from moviepy import VideoFileClip
        with VideoFileClip(str(p)) as clip:
            dur = clip.duration
        if dur < 5.0:
            return False, f"时长 {dur:.2f}s < 5s"
        return True, f"{dur:.2f}s / {p.stat().st_size/1024/1024:.2f}MB"
    except Exception as e:
        return True, f"{p.stat().st_size/1024/1024:.2f}MB (时长解析失败: {e})"


async def run_womenswear_pipeline(
    task_id: str,
    model_image_path: str = "",
    source_mode: str = "",
    raw_product_path: str = "",
    source_video_path: str = "",
    engine: str = "plus",
    scene_image_path: str = "",
) -> None:
    """5 步流水线。扫描 work_dir 已有产物，自动跳过已完成步骤。"""
    from backend.services.douyin_mall_clothing import (
        resolve_douyin_mall_clothing,
        download_clothing_image,
        generate_flat_lay_image,
        generate_motion_reference_image,
        extract_video_keyframe,
        save_image_result,
        analyze_product_image_full,
        analyze_product_image_has_model,
        remove_scene_people,
        describe_person_pose,
        _BEAUTY_REFERENCE_PROMPT,
        _SCENE_FUSION_PROMPT,
        _MODEL_BG_REPLACE_PROMPT,
    )
    from backend.services.douyin_downloader import download_douyin_video
    from backend.services.runninghub_rpa_client import run_motion_transfer_rpa
    from backend.services import task_store

    def _set_step(step_key: str, stage_text: str, progress: int, **extra):
        motion_tasks[task_id].update({
            "stage": stage_text,
            "progress": progress,
            "current_step": step_key,
            **extra,
        })
        task_store.update(
            task_id,
            stage=stage_text,
            progress=progress,
            extra={**motion_tasks[task_id].get("extra", {}), "current_step": step_key, **extra},
        )
        task_store.add_log(task_id, stage_text)

    def _save_intermediate(step_key: str, input_data: dict, output_data: dict):
        inter = motion_tasks[task_id].get("intermediate", {})
        inter[step_key] = {"input": input_data, "output": output_data, "ts": int(time.time())}
        motion_tasks[task_id]["intermediate"] = inter
        task_store.update(task_id, intermediate=inter)

    def _fail(step_key: str, err) -> None:
        label = {
            "step1": "Step1 失败",
            "step2": "Step2 失败",
            "step3": "Step3 失败",
            "step4": "Step4 失败",
            "step5": "Step5 失败",
        }.get(step_key, f"{step_key} 失败")
        text = str(err)
        if not text.startswith("Step"):
            text = f"{label}: {err}"
        motion_tasks[task_id].update({
            "status": "FAILED",
            "error": text,
            "stage": label,
            "failed_step": step_key,
        })
        task_store.update(
            task_id,
            status="FAILED",
            error=text,
            stage=label,
            extra={**motion_tasks[task_id].get("extra", {}), "failed_step": step_key},
        )
        task_store.add_log(task_id, f"❌ {text}", level="error")

    work_dir = Path(settings.OUTPUT_DIR) / "womenswear_studio" / task_id
    work_dir.mkdir(parents=True, exist_ok=True)
    motion_tasks[task_id].update({"status": "RUNNING", "error": None})

    upload_dir = Path(settings.UPLOAD_DIR)
    params = motion_tasks[task_id].get("params", {})
    source_mode = (source_mode or params.get("source_mode") or "product").lower()
    # FR-3 旧值兼容映射：link→product，upload/beauty→creator
    source_mode = {"link": "product", "upload": "creator", "beauty": "creator"}.get(source_mode, source_mode)
    if source_mode not in ("product", "creator"):
        source_mode = "product"
    raw_product_path = raw_product_path or params.get("raw_product_path", "")
    source_video_path = source_video_path or params.get("source_video_path", "")
    model_path = model_image_path or params.get("model_image_path") or ""
    # 背景来源已统一（与素材出片一致）：scene_image_path 有则融合场景，无则抽视频关键帧
    # mode/background_image_path 已废弃，仅保留旧任务元数据回读
    mode = (params.get("mode") or "").lower()
    engine = "plus"  # 前端已移除引擎选项，固定走 Plus（质量优先）
    scene_image_path = scene_image_path or params.get("scene_image_path") or ""
    default_model_path = str(upload_dir / "default_girl_model.jpg")

    raw_img_path = work_dir / "raw_product.jpg"
    flat_path = work_dir / "flat_lay.png"
    show_path = work_dir / "showcase.png"
    keyframe_path = work_dir / "keyframe_bg.jpg"
    video_path = work_dir / "source_video.mp4"
    result_path = work_dir / "final.mp4"

    if result_path.exists():
        motion_tasks[task_id].update({
            "status": "SUCCESS",
            "progress": 100,
            "outputs": [{"fileUrl": output_url(result_path), "fileType": "mp4"}],
            "stage": "完成（断点恢复）",
            "current_step": "done",
        })
        task_store.update(
            task_id,
            status="SUCCESS",
            progress=100,
            stage="完成（断点恢复）",
            outputs=[{"fileUrl": output_url(result_path), "fileType": "mp4"}],
            extra={**motion_tasks[task_id].get("extra", {}), "current_step": "done"},
        )
        return

    # ───── Step1 ─────
    if source_mode == "creator":
        try:
            if not raw_product_path or not Path(raw_product_path).exists():
                raise RuntimeError(f"上传的模特图不存在: {raw_product_path}")
            shutil.copyfile(raw_product_path, raw_img_path)
            _set_step("step1", "Step1: 使用上传的模特图（跳过链接解析）", 15, raw_product_url=output_url(raw_img_path))
            _save_intermediate(
                "step1",
                {"source_mode": source_mode, "raw_product_path": raw_product_path},
                {"raw_product": output_url(raw_img_path), "skipped": True, "source_mode": source_mode},
            )
        except Exception as e:
            _fail("step1", f"Step1 失败: {e}")
            raise
    elif raw_img_path.exists():
        _set_step("step1", "Step1: 商品图已下载（跳过）", 15, raw_product_url=output_url(raw_img_path))
        _save_intermediate(
            "step1",
            {"product_url": params.get("product_url", "")},
            {"raw_product": output_url(raw_img_path), "skipped": True},
        )
    else:
        try:
            _set_step("step1", "Step1: 解析商品链接", 5)
            info = await resolve_douyin_mall_clothing(params.get("product_url", ""))
            img_urls = info.get("clothing_image_urls") or info.get("image_urls") or []
            if not img_urls:
                raise RuntimeError("商品图解析失败")
            _set_step("step1", "Step1: 下载商品原图", 10)
            await download_clothing_image(img_urls[0], raw_img_path)
            _set_step(
                "step1",
                "Step1: 商品图已下载",
                15,
                raw_product_url=output_url(raw_img_path),
                product_title=info.get("title", ""),
            )
            _save_intermediate(
                "step1",
                {"product_url": params.get("product_url", ""), "image_urls": img_urls[:3]},
                {"raw_product": output_url(raw_img_path), "title": info.get("title", ""), "author": info.get("author", "")},
            )
        except Exception as e:
            _fail("step1", f"Step1 失败: {e}")
            raise

    # ───── Step2 ─────
    if source_mode == "creator":
        _set_step("step2", "Step2: 素材出片跳过白底平铺图生成", 40, flat_lay_url="")
        _save_intermediate(
            "step2",
            {"source_mode": source_mode, "raw_product": output_url(raw_img_path)},
            {"flat_lay": "", "skipped": True, "source_mode": source_mode},
        )
        task_store.add_log(task_id, "⏭️ Step2 素材出片跳过（模特图直接作为服装真值）")
    elif flat_path.exists():
        _set_step("step2", "Step2: 白底平铺图已生成（跳过）", 40, flat_lay_url=output_url(flat_path))
        _save_intermediate("step2", {"raw_product": output_url(raw_img_path)}, {"flat_lay": output_url(flat_path), "skipped": True})
    else:
        try:
            _set_step("step2", "Step2: Agnes 生图 生成白底平铺图", 25)
            flat_result = await generate_flat_lay_image(str(raw_img_path))
            await save_image_result(flat_result, flat_path)
            _set_step("step2", "Step2: 白底平铺图已生成", 40, flat_lay_url=output_url(flat_path))
            _save_intermediate("step2", {"raw_product": output_url(raw_img_path)}, {"flat_lay": output_url(flat_path)})
        except Exception as e:
            _fail("step2", f"Step2 失败: {e}")
            raise

    async def _download_source_video(step_key: str = "step4", stage_prefix: str = "Step4", base_progress: int = 70) -> dict:
        if video_path.exists() and video_path.stat().st_size > 1024:
            ok, reason = await asyncio.to_thread(_validate_source_video, video_path)
            if ok:
                task_store.add_log(task_id, f"✅ 模仿视频已就绪: {reason}")
                return {"quality": reason, "title": "", "author": ""}
            try:
                video_path.unlink()
            except Exception:
                pass
            _set_step(step_key, f"{stage_prefix}: 已有视频不达标({reason})，重新下载", base_progress)
        _set_step(step_key, f"{stage_prefix}: 下载抖音模仿视频", base_progress)
        vid_info = await download_douyin_video(params.get("video_url", ""), str(work_dir))
        src = vid_info.get("video_path") or vid_info.get("file_path")
        if not src or not Path(src).exists():
            raise RuntimeError("模仿视频下载失败")
        if Path(src).resolve() != video_path.resolve():
            shutil.copyfile(src, video_path)
        ok, reason = await asyncio.to_thread(_validate_source_video, video_path)
        if not ok:
            raise RuntimeError(f"模仿视频不达标: {reason}")
        task_store.add_log(task_id, f"✅ 模仿视频质量: {reason}")
        return {"quality": reason, "title": vid_info.get("title", ""), "author": vid_info.get("author", "")}

    # ───── 素材出片模特图决策（FR-2 模特来源统一：用户模特图 > 模特图含模特 > 默认模特） ─────
    # 4 分支判断维度：is_white_bg（模特图是否白底）× has_scene（是否传了背景图）
    #   分支1：白底 + 无背景图 → 抽关键帧当背景融合
    #   分支2：白底 + 有背景图 → 用户背景图融合
    #   分支3：非白底 + 无背景图 → 复制模特图当 showcase（不融合）
    #   分支4：非白底 + 有背景图 → 一步融合（提示词删原背景+放新背景）
    is_white_bg = False  # 默认非白底（兜底/未识别时按非白底处理）
    use_4branch = False  # 是否走 4 分支（仅主图含模特且未上传独立模特图时为 True）
    if source_mode == "creator":
        if model_path and Path(model_path).exists():
            task_store.add_log(task_id, "✅ 使用用户上传的模特图")
            _save_intermediate(
                "step3_pre",
                {"source": "user_model", "has_model": None, "is_white_bg": None},
                {"model_path": model_path, "decision": "use_user_model"},
            )
        else:
            try:
                _set_step("step3", "Step3-素材出片: LLM 识别模特图（Agnes）", 48, llm_phase="mimo_analyze")
                analysis = await analyze_product_image_full(str(raw_img_path))
                has_model = analysis["has_model"]
                is_white_bg = analysis["is_white_bg"]
                if has_model:
                    model_path = str(raw_img_path)
                    use_4branch = True
                    task_store.add_log(task_id, f"✅ Agnes 识别模特图含模特（白底={is_white_bg}），直接复用作模特图")
                    _save_intermediate(
                        "step3_pre",
                        {"source": "mimo_analyze", "has_model": True, "is_white_bg": is_white_bg},
                        {"model_path": model_path, "decision": "use_product_as_model"},
                    )
                else:
                    model_path = default_model_path
                    task_store.add_log(task_id, "✅ Agnes 识别模特图无模特，使用默认模特兜底生成参考图")
                    _save_intermediate(
                        "step3_pre",
                        {"source": "mimo_analyze", "has_model": False, "is_white_bg": is_white_bg},
                        {"model_path": model_path, "decision": "use_default_model"},
                    )
            except Exception as e:
                _fail("step3", f"Step3-素材出片 模特图决策失败: {e}")
                raise

    # ───── Step3 ─────
    if source_mode == "creator":
        # FR-1：素材出片统一生成融合参考图，RPA 输入 = showcase.png（杜绝货不对板）
        # 服装真值 = 模特图；模特 = 模特图（含模特时）或用户模特图/默认模特
        step3_label = "素材出片"
        clothing_img = str(raw_img_path)
        model_img = clothing_img if model_path == clothing_img else model_path
        # 模特图兼服装真值 → 美女专用提示词（人物+服装同图）；服装/模特分离 → 默认提示词
        fusion_prompt = _BEAUTY_REFERENCE_PROMPT if model_img == clothing_img else None
        has_scene = bool(scene_image_path and Path(scene_image_path).exists())
        try:
            if use_4branch:
                if is_white_bg and not has_scene:
                    # 分支1：白底模特 + 无背景图 → 抽模仿视频关键帧当背景融合
                    _set_step("step3", f"Step3-{step3_label}: 抽取模仿视频关键帧作背景（分支1·白底模特）", 45)
                    if not video_path.exists() or video_path.stat().st_size <= 1024:
                        if not source_video_path or not Path(source_video_path).exists():
                            raise RuntimeError(f"上传的模仿视频不存在: {source_video_path}")
                        shutil.copyfile(source_video_path, video_path)
                        ok, reason = await asyncio.to_thread(_validate_source_video, video_path)
                        if not ok:
                            raise RuntimeError(f"上传的模仿视频不达标: {reason}")
                    _set_step("step3", f"Step3-{step3_label}: 关键帧已抽取，Agnes 融合参考图（分支1）", 55, source_video_url=output_url(video_path))
                    await asyncio.to_thread(extract_video_keyframe, str(video_path), str(keyframe_path))
                    scene_bg = str(keyframe_path)
                    show_result = await generate_motion_reference_image(clothing_img, model_img, scene_bg, prompt=fusion_prompt)
                    await save_image_result(show_result, show_path)
                    _set_step("step3", f"Step3-{step3_label}: 参考图已生成（分支1·关键帧背景）", 65, showcase_url=output_url(show_path), keyframe_url=output_url(keyframe_path))
                    _save_intermediate(
                        "step3",
                        {"source_mode": source_mode, "clothing": clothing_img, "model": model_img, "scene_bg": scene_bg, "mode": "branch1_keyframe", "is_white_bg": True},
                        {"showcase": output_url(show_path), "keyframe": output_url(keyframe_path)},
                    )
                elif is_white_bg and has_scene:
                    # 分支2：白底模特 + 有背景图 → 用户背景图融合
                    _set_step("step3", f"Step3-{step3_label}: Agnes 用模特图+用户场景图融合生成参考图（分支2·白底模特）", 50)
                    scene_bg = scene_image_path
                    show_result = await generate_motion_reference_image(clothing_img, model_img, scene_bg, prompt=fusion_prompt)
                    await save_image_result(show_result, show_path)
                    _set_step("step3", f"Step3-{step3_label}: 参考图已生成（分支2·用户场景图）", 65, showcase_url=output_url(show_path))
                    _save_intermediate(
                        "step3",
                        {"source_mode": source_mode, "clothing": clothing_img, "model": model_img, "scene_bg": scene_bg, "mode": "branch2_scene_upload", "is_white_bg": True},
                        {"showcase": output_url(show_path), "scene_image": output_url(Path(scene_bg))},
                    )
                elif not is_white_bg and not has_scene:
                    # 分支3：非白底模特 + 无背景图 → 复制模特图当 showcase（不融合）
                    _set_step("step3", f"Step3-{step3_label}: 模特图有背景未传背景图，直接复用作参考图（分支3）", 50)
                    shutil.copyfile(clothing_img, show_path)
                    _set_step("step3", f"Step3-{step3_label}: 参考图已就绪（分支3·直接复用模特图）", 65, showcase_url=output_url(show_path))
                    _save_intermediate(
                        "step3",
                        {"source_mode": source_mode, "clothing": clothing_img, "model": model_img, "mode": "branch3_direct", "is_white_bg": False},
                        {"showcase": output_url(show_path), "skipped_fusion": True},
                    )
                else:
                    # 分支4：非白底模特 + 有背景图 → 一步融合（提示词删原背景+放新背景）
                    _set_step("step3", f"Step3-{step3_label}: Agnes 模特换背景融合（分支4·有背景模特+用户背景图）", 50)
                    scene_bg = scene_image_path
                    show_result = await generate_motion_reference_image(clothing_img, model_img, scene_bg, prompt=_MODEL_BG_REPLACE_PROMPT)
                    await save_image_result(show_result, show_path)
                    _set_step("step3", f"Step3-{step3_label}: 参考图已生成（分支4·模特换背景）", 65, showcase_url=output_url(show_path))
                    _save_intermediate(
                        "step3",
                        {"source_mode": source_mode, "clothing": clothing_img, "model": model_img, "scene_bg": scene_bg, "mode": "branch4_bg_replace", "is_white_bg": False},
                        {"showcase": output_url(show_path), "scene_image": output_url(Path(scene_bg))},
                    )
            else:
                # 兜底：用户上传独立模特图 或 主图不含模特（默认模特）→ 走原融合逻辑
                if has_scene:
                    _set_step("step3", f"Step3-{step3_label}: Agnes 用模特图+用户场景图融合生成参考图", 50)
                    scene_bg = scene_image_path
                    show_result = await generate_motion_reference_image(clothing_img, model_img, scene_bg, prompt=fusion_prompt)
                    await save_image_result(show_result, show_path)
                    _set_step("step3", f"Step3-{step3_label}: 参考图已生成（用户场景图）", 65, showcase_url=output_url(show_path))
                    _save_intermediate(
                        "step3",
                        {"source_mode": source_mode, "clothing": clothing_img, "model": model_img, "scene_bg": scene_bg, "mode": "scene_upload"},
                        {"showcase": output_url(show_path), "scene_image": output_url(Path(scene_bg))},
                    )
                else:
                    _set_step("step3", f"Step3-{step3_label}: 抽取模仿视频关键帧作背景", 45)
                    if not video_path.exists() or video_path.stat().st_size <= 1024:
                        if not source_video_path or not Path(source_video_path).exists():
                            raise RuntimeError(f"上传的模仿视频不存在: {source_video_path}")
                        shutil.copyfile(source_video_path, video_path)
                        ok, reason = await asyncio.to_thread(_validate_source_video, video_path)
                        if not ok:
                            raise RuntimeError(f"上传的模仿视频不达标: {reason}")
                    _set_step("step3", f"Step3-{step3_label}: 关键帧已抽取，Agnes 融合参考图（去字幕+替换人物）", 55, source_video_url=output_url(video_path))
                    await asyncio.to_thread(extract_video_keyframe, str(video_path), str(keyframe_path))
                    scene_bg = str(keyframe_path)
                    show_result = await generate_motion_reference_image(clothing_img, model_img, scene_bg, prompt=fusion_prompt)
                    await save_image_result(show_result, show_path)
                    _set_step("step3", f"Step3-{step3_label}: 参考图已生成（视频关键帧背景）", 65, showcase_url=output_url(show_path), keyframe_url=output_url(keyframe_path))
                    _save_intermediate(
                        "step3",
                        {"source_mode": source_mode, "clothing": clothing_img, "model": model_img, "scene_bg": scene_bg, "mode": "keyframe"},
                        {"showcase": output_url(show_path), "keyframe": output_url(keyframe_path)},
                    )
        except Exception as e:
            _fail("step3", f"Step3-{step3_label}失败: {e}")
            raise
    elif show_path.exists():
        # 断点恢复：按背景来源推断 mode（传背景图=scene_upload 融合，否则 keyframe 抽帧），旧任务回退 mode
        skip_mode = "scene_upload" if scene_image_path and Path(scene_image_path).exists() else (
            "keyframe" if keyframe_path.exists() else mode
        )
        _set_step("step3", "Step3: 参考图已生成（跳过）", 65, showcase_url=output_url(show_path))
        _save_intermediate(
            "step3",
            {"source_mode": source_mode, "flat_lay": output_url(flat_path), "model": model_path, "keyframe": output_url(keyframe_path), "mode": skip_mode},
            {"showcase": output_url(show_path), "keyframe": output_url(keyframe_path) if keyframe_path.exists() else "", "skipped": True},
        )
    else:
        # 商品出片：模仿视频必下载（抽关键帧 + Step5 动作迁移都要用）
        # 模特图背景决定融合方式：
        #   - 有背景模特图 → 保留模特图原背景，只换衣服（不融合视频背景）
        #   - 白底模特图/默认模特 → 走原逻辑（融合视频关键帧背景 或 用户背景图）
        try:
            # 识别模特图是否白底（用户传了独立模特图才识别，默认模特假设白底）
            product_is_white_bg = True
            if model_path and Path(model_path).exists() and model_path != default_model_path:
                try:
                    _set_step("step3", "Step3: 识别模特图背景（Agnes）", 41, llm_phase="mimo_analyze")
                    product_analysis = await analyze_product_image_full(model_path)
                    product_is_white_bg = product_analysis["is_white_bg"]
                    task_store.add_log(task_id, f"✅ Agnes 识别模特图白底={product_is_white_bg}")
                    _save_intermediate(
                        "step3_pre",
                        {"source": "mimo_analyze", "is_white_bg": product_is_white_bg, "mode": "product"},
                        {"model_path": model_path, "decision": "use_user_model"},
                    )
                except Exception as e:
                    task_store.add_log(task_id, f"⚠️ 模特图背景识别失败，按白底处理: {e}")
            else:
                if not model_path or not Path(model_path).exists():
                    model_path = default_model_path
                    task_store.add_log(task_id, "模特图未提供，使用默认模特")
            # 两个分支都先下载模仿视频（Step5 RPA 动作迁移也要用）
            _set_step("step3", "Step3: 下载模仿视频", 42)
            vinfo = await _download_source_video(step_key="step3", stage_prefix="Step3", base_progress=42)
            _set_step(
                "step3",
                "Step3: 抽取模仿视频关键帧",
                50,
                source_video_url=output_url(video_path),
                video_quality=vinfo["quality"],
            )
            await asyncio.to_thread(extract_video_keyframe, str(video_path), str(keyframe_path))
            if not product_is_white_bg:
                # 有背景模特图 → 保留模特图原背景，只换衣服（不融合视频关键帧背景）
                # 结构性防“双人”：先把模特图人物擦成空场景（保留背景布局），融合输入只剩模特一张带人图
                _set_step("step3", "Step3: 模特图背景清理（保留背景布局，擦除人物）", 52, keyframe_url=output_url(keyframe_path))
                model_bg_clean = await remove_scene_people(model_path, work_dir / "model_bg_clean.png")
                _set_step("step3", "Step3: Agnes 融合（模特换衣服·保留原背景）", 58, keyframe_url=output_url(keyframe_path), scene_clean_url=output_url(Path(model_bg_clean)))
                show_result = await generate_motion_reference_image(str(flat_path), model_path, model_bg_clean)
                await save_image_result(show_result, show_path)
                _set_step("step3", "Step3: 动作迁移参考图已生成（模特换衣服·保留原背景）", 65, showcase_url=output_url(show_path), keyframe_url=output_url(keyframe_path))
                _save_intermediate(
                    "step3",
                    {"source_mode": source_mode, "flat_lay": output_url(flat_path), "model": model_path, "scene_bg": str(model_bg_clean), "keyframe": output_url(keyframe_path), "mode": "model_bg_keep", "is_white_bg": False},
                    {"showcase": output_url(show_path), "keyframe": output_url(keyframe_path), "model_bg_clean": output_url(Path(model_bg_clean))},
                )
            elif scene_image_path and Path(scene_image_path).exists():
                # 白底模特 + 传背景图 → 模特背景融合 + 下载视频关键帧融合（4 图融合）
                # 结构性防“双人”：背景图与关键帧都先移除人物，融合输入只剩模特一张带人图
                scene_bg = scene_image_path
                _set_step("step3", "Step3: 识别关键帧人物姿势（文字化保留）", 52, keyframe_url=output_url(keyframe_path))
                pose_hint = await describe_person_pose(str(keyframe_path))
                _set_step("step3", "Step3: 背景图+关键帧移除原人物（空场景化）", 54)
                scene_bg_clean = await remove_scene_people(scene_image_path, work_dir / "scene_bg_clean.png")
                keyframe_clean = await remove_scene_people(str(keyframe_path), work_dir / "keyframe_clean.png")
                _set_step("step3", "Step3: 模特背景融合+下载视频 Agnes 融合生成动作迁移参考图", 58)
                show_result = await generate_motion_reference_image(
                    str(flat_path), model_path, scene_bg_clean,
                    prompt=_SCENE_FUSION_PROMPT,
                    extra_scene_image_path=keyframe_clean,
                    pose_hint=pose_hint,
                )
                await save_image_result(show_result, show_path)
                _set_step("step3", "Step3: 动作迁移参考图已生成（模特背景融合+下载视频融合）", 65, showcase_url=output_url(show_path), keyframe_url=output_url(keyframe_path))
                _save_intermediate(
                    "step3",
                    {"source_mode": source_mode, "flat_lay": output_url(flat_path), "model": model_path, "scene_bg": scene_bg, "keyframe": output_url(keyframe_path), "mode": "scene_upload", "pose_hint": pose_hint, "is_white_bg": True},
                    {"showcase": output_url(show_path), "scene_image": output_url(Path(scene_bg)), "keyframe": output_url(keyframe_path), "keyframe_clean": output_url(Path(keyframe_clean))},
                )
            else:
                # 白底模特/默认模特 + 不传背景图 → 下载视频截取一帧作背景，替换模特人物，做成场景人物图
                # 结构性防“双人”：先把关键帧人物擦成空场景、姿势文字化，
                # 融合输入只剩模特一张带人图，杜绝弱模型把原人物画进画面
                _set_step("step3", "Step3: 识别关键帧人物姿势（文字化保留）", 52, keyframe_url=output_url(keyframe_path))
                pose_hint = await describe_person_pose(str(keyframe_path))
                _set_step("step3", "Step3: 关键帧移除原人物（空场景化）", 54, keyframe_url=output_url(keyframe_path))
                scene_bg = await remove_scene_people(str(keyframe_path), work_dir / "keyframe_clean.png")
                _set_step("step3", "Step3: Agnes 融合生成参考图（模特置入场景）", 58, scene_clean_url=output_url(Path(scene_bg)))
                show_result = await generate_motion_reference_image(str(flat_path), model_path, scene_bg, pose_hint=pose_hint)
                await save_image_result(show_result, show_path)
                _set_step("step3", "Step3: 动作迁移参考图已生成（视频关键帧背景）", 65, showcase_url=output_url(show_path), keyframe_url=output_url(keyframe_path))
                _save_intermediate(
                    "step3",
                    {"source_mode": source_mode, "flat_lay": output_url(flat_path), "model": model_path, "scene_bg": scene_bg, "keyframe": output_url(keyframe_path), "mode": "keyframe", "pose_hint": pose_hint, "is_white_bg": True},
                    {"showcase": output_url(show_path), "keyframe": output_url(keyframe_path), "keyframe_clean": output_url(Path(scene_bg))},
                )
        except Exception as e:
            _fail("step3", f"Step3 失败: {e}")
            raise

    # ───── Step4 ─────
    if source_mode == "creator":
        try:
            if not source_video_path or not Path(source_video_path).exists():
                raise RuntimeError(f"上传的模仿视频不存在: {source_video_path}")
            # Step3 关键帧分支可能已经把视频复制到 video_path 了，校验通过则跳过复制
            need_copy = True
            if video_path.exists() and video_path.stat().st_size > 1024:
                ok, reason = await asyncio.to_thread(_validate_source_video, video_path)
                if ok:
                    need_copy = False
                else:
                    try:
                        video_path.unlink()
                    except Exception:
                        pass
            if need_copy:
                shutil.copyfile(source_video_path, video_path)
                ok, reason = await asyncio.to_thread(_validate_source_video, video_path)
                if not ok:
                    raise RuntimeError(f"上传的模仿视频不达标: {reason}")
            label = "素材出片"
            skip_tag = "已在Step3就绪" if not need_copy else f"使用上传的模仿视频"
            _set_step("step4", f"Step4: {skip_tag}({reason}) [{label}]", 75, source_video_url=output_url(video_path))
            _save_intermediate(
                "step4",
                {"source_mode": source_mode, "source_video_path": source_video_path},
                {"source_video": output_url(video_path), "quality": reason, "skipped": not need_copy, "source_mode": source_mode},
            )
        except Exception as e:
            _fail("step4", f"Step4 失败: {e}")
            raise
    else:
        # 商品出片统一：视频已在 Step3（下载+抽帧）就绪则复用，否则在此下载
        ok, reason = await asyncio.to_thread(_validate_source_video, video_path)
        if not ok:
            vinfo2 = await _download_source_video(step_key="step4", stage_prefix="Step4", base_progress=70)
            reason = vinfo2["quality"]
            _set_step("step4", f"Step4: 模仿视频已下载({reason})", 75, source_video_url=output_url(video_path))
            _save_intermediate(
                "step4",
                {"video_url": params.get("video_url", "")},
                {"source_video": output_url(video_path), "quality": reason, "title": vinfo2["title"], "author": vinfo2["author"]},
            )
        else:
            _set_step("step4", f"Step4: 模仿视频已在Step3就绪({reason})", 75, source_video_url=output_url(video_path))
            _save_intermediate(
                "step4",
                {"video_url": params.get("video_url", "")},
                {"source_video": output_url(video_path), "quality": reason, "skipped": True},
            )

    # ───── Step5 RPA ─────
    # FR-1：所有模式统一用 Step3 融合参考图做 RPA 输入（不再直接用模特图，杜绝货不对板）
    if not show_path.exists():
        _fail("step5", f"Step3 产物缺失: {show_path}")
        raise RuntimeError(f"Step3 产物缺失: {show_path}")
    rpa_image_path = str(show_path)
    rpa_image_label = "动作迁移参考图(showcase)"

    # 账号池 + 串行锁：从池里取一个 ready 账号；同时只允许 1 个 RPA 任务运行
    from backend.services.rh_account_pool import (
        get_available_account,
        next_available,
        get_profile_dir,
        get_global_rpa_lock,
        mark_in_use,
        mark_free,
        mark_failed,
        mark_ready,
        STATUS_PENDING,
    )

    async def _run_rpa_with_account(acc_phone: str, acc_password: str):
        """用指定账号跑一次 RPA"""
        mark_in_use(acc_phone)
        try:
            def _on_rpa_progress(stage: str, pct: int):
                set_step_rpa(task_id, f"Step5-RPA: {stage}", pct, log=(pct % 2 == 0))

            await run_motion_transfer_rpa(
                image_path=rpa_image_path,
                video_path=str(video_path),
                output_path=str(result_path),
                timeout=3600,
                engine=engine,
                phone=acc_phone,
                password=acc_password,
                profile_dir=get_profile_dir(acc_phone),
                on_progress=_on_rpa_progress,
            )
            # RPA 返回≠成片到手：必须确认 final.mp4 真实落盘且非空，否则按失败换号
            if not result_path.exists() or result_path.stat().st_size == 0:
                raise RuntimeError(f"RPA 完成但成片缺失或为空: {result_path}")
        except Exception as e:
            # 判定失败类型
            err_text = str(e)
            err_up = err_text.upper()
            is_login_err = (
                any(kw in err_text for kw in ["未登录", "登录失败", "登 录", "未授权", "登录态", "重新登录"])
                or "TOKEN_" in err_up
                or (("TOKEN" in err_up or "IDENTIFY" in err_up) and any(k in err_text for k in ("过期", "失效", "无效")))
            )
            # 被占用/并发中：账号本身没问题，只是正在跑别的任务 → 释放后换空闲账号，不拉黑
            is_busy_err = any(kw in err_text for kw in ["被占用", "占用", "并发", "正在使用", "正在运行", "排队", "同时"])
            is_limit_err = any(kw in err_text for kw in ["限流", "风控", "频控", "限制"])
            is_balance_err = any(kw in err_text for kw in ["余额不足", "余额为负", "欠费", "算力不足", "星力不足", "insufficient", "quota"])
            if is_login_err:
                mark_failed(acc_phone, f"登录失效: {err_text}", kind="login_failed")
            elif is_busy_err:
                mark_free(acc_phone)
            elif is_limit_err:
                mark_failed(acc_phone, f"限流/风控: {err_text}", kind="limited")
            elif is_balance_err:
                mark_failed(acc_phone, f"余额不足: {err_text}", kind="limited")
            else:
                # 普通失败也释放账号
                mark_free(acc_phone)
            raise
        else:
            mark_free(acc_phone)

    try:
        btn_label = "中间按钮(beveled-btn-left)" if engine == "plus" else (
            "左侧按钮(Lite/Standard)" if engine == "lite" else f"engine={engine} 按钮"
        )
        _set_step("step5", f"Step5-RPA: 启动 Playwright + ComfyUI 工作流（引擎: {engine.upper()} → 点击{btn_label}）", 80)
        _set_step("step5", f"Step5-RPA: 上传{rpa_image_label}(节点299) + 上传视频(节点275) + 服务端校验 hash 一致", 85)
        _set_step("step5", "Step5-RPA: 等待节点 285 生成完整视频（最长 30 分钟；禁止读取节点 502 的 1 秒预览）", 90)
        if motion_tasks[task_id].get("abort_requested"):
            raise RuntimeError("任务被用户中止")

        # 串行锁：等待全局 RPA 锁（同时只允许 1 个 RPA 任务运行）
        rpa_lock = get_global_rpa_lock()
        _set_step("step5", "Step5-RPA: 等待 RPA 串行锁（其他任务运行中）", 88)
        async with rpa_lock:
            # 从账号池取一个可用账号
            acc = get_available_account()
            if not acc:
                # 没有可用账号：检查是否有 pending 账号（未做首次登录）
                from backend.services.rh_account_pool import _states, _accounts
                pending_phones = [
                    a.phone for a in _accounts
                    if _states.get(a.phone) and _states[a.phone].status == STATUS_PENDING
                ]
                if pending_phones:
                    raise RuntimeError(
                        f"无可用账号；账号 {pending_phones} 待首次登录，请在 app 启动时自动登录，或手动调用 /api/rh-accounts/init-login"
                    )
                raise RuntimeError("账号池无可用账号（全部被限流/未登录/禁用）")

            _set_step("step5", f"Step5-RPA: 使用账号 {acc.phone}（{acc.note}）", 89)
            task_store.add_log(task_id, f"Step5-RPA: 选中账号 {acc.phone}", level="info")

            tried_phones = []
            last_error = None
            while True:
                tried_phones.append(acc.phone)
                try:
                    await _run_rpa_with_account(acc.phone, acc.password)
                    break  # 成功跳出循环
                except Exception as e:
                    last_error = e
                    task_store.add_log(
                        task_id,
                        f"Step5-RPA: 账号 {acc.phone} 失败: {str(e)[:120]}",
                        level="warn",
                    )
                    # 换下一个账号（排除本次已试过的全部账号，避免 A↔B 互换死循环）
                    next_acc = next_available(acc.phone, tried_phones=tried_phones)
                    if not next_acc:
                        # 所有账号都试过了
                        raise RuntimeError(
                            f"所有账号都失败，已尝试: {tried_phones}；最后一次错误: {last_error}"
                        )
                    acc = next_acc
                    _set_step("step5", f"Step5-RPA: 换号重试 → {acc.phone}", 89)
                    task_store.add_log(
                        task_id,
                        f"Step5-RPA: 换号重试 → {acc.phone}",
                        level="info",
                    )

        # 最终校验：成片必须真实存在才标 SUCCESS（防止“完成”状态但没有视频）
        if not result_path.exists() or result_path.stat().st_size == 0:
            raise RuntimeError(f"RPA 链路结束但成片缺失: {result_path}")

        motion_tasks[task_id].update({
            "status": "SUCCESS",
            "progress": 100,
            "outputs": [{"fileUrl": output_url(result_path), "fileType": "mp4"}],
            "stage": f"完成（RPA 链路，账号 {acc.phone}）",
            "current_step": "done",
        })
        task_store.update(
            task_id,
            status="SUCCESS",
            progress=100,
            stage=f"完成（RPA 链路，账号 {acc.phone}）",
            outputs=[{"fileUrl": output_url(result_path), "fileType": "mp4"}],
            extra={**motion_tasks[task_id].get("extra", {}), "current_step": "done", "rh_account": acc.phone},
        )
        _save_intermediate(
            "step5",
            {
                "source_video": output_url(video_path),
                "rpa_image": rpa_image_path,
                "rpa_image_label": rpa_image_label,
                "source_mode": source_mode,
                "rh_account": acc.phone,
            },
            {"final_video": output_url(result_path)},
        )
        task_store.add_log(task_id, f"✅ Step5-RPA 完成：预览视频已下载到本地（账号: {acc.phone}）", level="done")
    except Exception as e:
        _fail("step5", f"Step5 失败: {e}")
        raise
