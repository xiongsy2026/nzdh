"""女装带货独立封装。

对外只暴露流水线、路由和任务状态，不依赖爆款复刻等其它工作流。
"""
from womenswear.pipeline import run_womenswear_pipeline
from womenswear.router import router
from womenswear.state import motion_tasks

__all__ = ["run_womenswear_pipeline", "router", "motion_tasks"]
