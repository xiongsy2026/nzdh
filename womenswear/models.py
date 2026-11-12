from pydantic import BaseModel, ConfigDict


class WomenswearStudioRequest(BaseModel):
    model_config = ConfigDict(protected_namespaces=())
    """一体化流水线请求。

    source_mode（FR-3 模式收敛，旧值兼容：link→product，upload/beauty→creator）:
      - product= 商品出片：抖音商品链接 + 模仿视频链接
      - creator= 素材出片：模特图 + 模仿视频（LLM 自动判断模特图是否含模特）
    product_url / video_url: product 模式必填
    raw_product_path / source_video_path: creator 模式必填
    model_image_path: product 模式必填。creator 可缺省（LLM 决策：模特图含模特则复用；仅服装则默认模特兜底）
    scene_image_path: 背景来源（product/creator 统一）。有则融合人物场景；无则抽视频关键帧作背景
    engine: 固定 plus（质量优先）。前端已移除引擎选项，默认走 Plus

    所有模式的 RPA 输入均为 Step3 融合参考图 showcase.png（FR-1，杜绝货不对板）。
    """
    source_mode: str = "product"
    product_url: str = ""
    video_url: str = ""
    raw_product_path: str = ""
    source_video_path: str = ""
    model_image_path: str = ""
    scene_image_path: str = ""
    engine: str = "plus"
    session: str = ""    # DSH 会话 id：右栏「动作迁移」台按它过滤历史，花钱批准条子也按它校验


class WomenswearStep5RpaRequest(BaseModel):
    task_id: str
    timeout: int = 3600
    workflow_url: str = ""
    session: str = ""


class PublishRequest(BaseModel):
    video_path: str
    title: str = ""
    tags: list = []
    schedule_time: str = ""


class PublishCopyRequest(BaseModel):
    video_path: str
    product_info: str = ""


class PublishSessionRequest(BaseModel):
    video_path: str
    title: str = ""
    tags: list = []
