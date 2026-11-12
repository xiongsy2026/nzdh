"""女装带货独立封装配置。路径相对本文件，任意目录启动均可。"""
import os
from pathlib import Path

from pydantic_settings import BaseSettings

BASE_DIR = Path(__file__).resolve().parent


class Settings(BaseSettings):
    # 大图模型：Step2 平铺图、Step3 橱窗/参考图
    IMAGE2_BASE_URL: str = "https://api.agnes-ai.cn/v1"
    IMAGE2_API_KEY: str = ""
    IMAGE2_MODEL: str = "agnes-image-2.5-flash"

    # upload 识别是否含模特、发布文案
    MIMO_BASE_URL: str = "https://api.agnes-ai.cn/v1"
    MIMO_API_KEY: str = ""
    MIMO_MODEL: str = "agnes-3.0-flash"
    # upload 从模仿视频挑模特帧
    OPENAI_API_KEY: str = ""
    OPENAI_BASE_URL: str = "https://api.agnes-ai.cn/v1"
    OPENAI_MODEL: str = "agnes-3.0-flash"

    RUNNINGHUB_PROFILE_DIR: str = str(BASE_DIR / "runninghub_profile")
    RUNNINGHUB_WORKFLOW_URL: str = (
        "https://www.runninghub.cn/workflow/2106377827540103169?source=workspace"
    )

    UPLOAD_DIR: str = str(BASE_DIR / "uploads")
    OUTPUT_DIR: str = str(BASE_DIR / "outputs")
    FRONTEND_DIR: str = str(BASE_DIR / "frontend")

    # 🔴 只绑环回（2026-10-06）：这个服务能起浏览器 RPA 花真钱、能登录抖音发布、还管着一批 RH 账号池，
    # 同一局域网里的任何设备都不该点得动它。要用局域网访问请自己显式改 HOST，别默认敞开。
    HOST: str = "127.0.0.1"
    PORT: int = 8091
    RPA_HEADLESS: bool = False
    # RPA 实时画面（右栏那一格"工作流页面现在在干什么"）的截图间隔秒；0 = 关掉截图。
    # 0.5 秒以下别设：Playwright 截图会挤进动作时序里。
    RPA_LIVE_INTERVAL: float = 3.0

    # 动作迁移对外 API（motion_api.py）的调用方 Key，逗号分隔多个；留空 = API 关闭（503）。
    # 🔴 这条轨会花真钱（跑 RunningHub 任务），Key 别外传；换 Key 直接改这里重启即可。
    MOTION_API_KEYS: str = ""

    # 调用池里本机的 worker 名：生成的 task_id 会带 "{MOTION_WORKER_ID}-" 前缀。
    # 多机池建议每台起个好认的名字（如 ws1/ws2/bj1）；单机直连可留空。
    MOTION_WORKER_ID: str = ""

    # 调用池网关（motion_gateway.py，独立进程）：对外 Key + 监听地址 + worker 清单文件。
    # 网关默认也只绑环回，对外走前面的反代（Caddy/nginx）做 HTTPS。
    GATEWAY_API_KEYS: str = ""
    GATEWAY_HOST: str = "127.0.0.1"
    GATEWAY_PORT: int = 8092
    GATEWAY_WORKERS_FILE: str = str(BASE_DIR / "motion_gateway_workers.json")
    # 对外路径前缀（反代子目录场景用）：对外是 https://域名/motion/*，
    # 网关回给调用方的 poll_url / video_url 用它拼，保证调用方拿的地址能直接访问。
    GATEWAY_PUBLIC_PREFIX: str = "/api/motion"

    class Config:
        env_file = str(BASE_DIR / ".env")
        env_file_encoding = "utf-8"
        case_sensitive = True
        extra = "ignore"


settings = Settings()
os.makedirs(settings.UPLOAD_DIR, exist_ok=True)
os.makedirs(settings.OUTPUT_DIR, exist_ok=True)
