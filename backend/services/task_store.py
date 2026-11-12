"""统一任务持久化存储（SQLite）。

替代 app.py 中 6 个内存字典（toutiao_tasks / replica_phased_tasks / motion_tasks /
publish_tasks / h3_studio_tasks / vt_tasks），所有任务状态落盘到 data/tasks.db。

设计要点：
- 单表 tasks 存所有工作流的元信息，task_type 区分类型
- 日志单独表 task_logs，避免单行过大
- 同步接口（同步 sqlite3 性能足够，更新频率低）
- 启动时调用 recover_running() 可扫出未完成任务
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

# 全局锁（sqlite 同进程内并发写需要串行化）
_lock = threading.Lock()

# 数据库路径（懒初始化，避免导入时副作用）
_DB_PATH: Optional[Path] = None


def _db_path() -> Path:
    global _DB_PATH
    if _DB_PATH is None:
        from config import settings
        data_dir = Path(settings.OUTPUT_DIR).parent / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        _DB_PATH = data_dir / "tasks.db"
    return _DB_PATH


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db_path()), timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")  # 写入不阻塞读
    conn.execute("PRAGMA synchronous=NORMAL")  # 平衡性能与安全
    return conn


def init_db() -> None:
    """初始化数据库表。可重复调用。"""
    with _lock, _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                task_id      TEXT PRIMARY KEY,
                task_type    TEXT NOT NULL,
                status       TEXT NOT NULL DEFAULT 'INIT',
                progress     INTEGER DEFAULT 0,
                stage        TEXT DEFAULT '',
                params       TEXT DEFAULT '{}',
                outputs      TEXT DEFAULT '[]',
                intermediate TEXT DEFAULT '{}',
                rh_task_id   TEXT DEFAULT '',
                error        TEXT DEFAULT '',
                work_dir     TEXT DEFAULT '',
                extra        TEXT DEFAULT '{}',
                created_at   INTEGER DEFAULT 0,
                updated_at   INTEGER DEFAULT 0
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_tasks_type ON tasks(task_type)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_tasks_updated ON tasks(updated_at DESC)")
        c.execute("""
            CREATE TABLE IF NOT EXISTS task_logs (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id    TEXT NOT NULL,
                ts         INTEGER NOT NULL,
                level      TEXT DEFAULT 'info',
                msg        TEXT NOT NULL,
                FOREIGN KEY(task_id) REFERENCES tasks(task_id)
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_logs_task ON task_logs(task_id, id)")


def create(task_id: str, task_type: str, **fields: Any) -> dict:
    """创建任务记录。已存在则覆盖。"""
    init_db()
    now = int(time.time())
    defaults = {
        "task_type": task_type,
        "status": "INIT",
        "progress": 0,
        "stage": "",
        "params": "{}",
        "outputs": "[]",
        "intermediate": "{}",
        "rh_task_id": "",
        "error": "",
        "work_dir": "",
        "extra": "{}",
        "created_at": now,
        "updated_at": now,
    }
    for k, v in fields.items():
        if k in ("params", "outputs", "intermediate", "extra"):
            defaults[k] = json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v
        else:
            defaults[k] = v
    defaults["task_id"] = task_id

    cols = ",".join(defaults.keys())
    placeholders = ",".join(["?"] * len(defaults))
    with _lock, _conn() as c:
        c.execute(f"DELETE FROM tasks WHERE task_id=?", (task_id,))
        c.execute(f"INSERT INTO tasks ({cols}) VALUES ({placeholders})", tuple(defaults.values()))
    return get(task_id)


def update(task_id: str, **fields: Any) -> Optional[dict]:
    """更新任务字段。自动更新 updated_at。"""
    if not fields:
        return get(task_id)
    init_db()
    sets = []
    vals = []
    for k, v in fields.items():
        if k in ("params", "outputs", "intermediate", "extra"):
            sets.append(f"{k}=?")
            vals.append(json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v)
        else:
            sets.append(f"{k}=?")
            vals.append(v)
    sets.append("updated_at=?")
    vals.append(int(time.time()))
    vals.append(task_id)
    with _lock, _conn() as c:
        c.execute(f"UPDATE tasks SET {','.join(sets)} WHERE task_id=?", vals)
    return get(task_id)


def get(task_id: str) -> Optional[dict]:
    """读取任务。返回扁平字典（JSON 字段已解析）。不存在返回 None。"""
    init_db()
    with _lock, _conn() as c:
        row = c.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    for k in ("params", "outputs", "intermediate", "extra"):
        try:
            d[k] = json.loads(d.get(k) or ("{}" if k != "outputs" else "[]"))
        except Exception:
            d[k] = {} if k != "outputs" else []
    return d


def list_by_type(task_type: str, limit: int = 50) -> list:
    """按类型列出任务，最新优先。"""
    init_db()
    with _lock, _conn() as c:
        rows = c.execute(
            "SELECT * FROM tasks WHERE task_type=? ORDER BY updated_at DESC LIMIT ?",
            (task_type, limit)
        ).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        for k in ("params", "outputs", "intermediate", "extra"):
            try:
                d[k] = json.loads(d.get(k) or ("{}" if k != "outputs" else "[]"))
            except Exception:
                d[k] = {} if k != "outputs" else []
        out.append(d)
    return out


def list_all(limit: int = 100) -> list:
    """列出所有任务，最新优先。"""
    init_db()
    with _lock, _conn() as c:
        rows = c.execute("SELECT * FROM tasks ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        for k in ("params", "outputs", "intermediate", "extra"):
            try:
                d[k] = json.loads(d.get(k) or ("{}" if k != "outputs" else "[]"))
            except Exception:
                d[k] = {} if k != "outputs" else []
        out.append(d)
    return out


def list_running() -> list:
    """列出所有未完成任务（status 不是 SUCCESS/FAILED）。"""
    init_db()
    with _lock, _conn() as c:
        rows = c.execute(
            "SELECT * FROM tasks WHERE status NOT IN ('SUCCESS','FAILED') ORDER BY created_at",
        ).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        for k in ("params", "outputs", "intermediate", "extra"):
            try:
                d[k] = json.loads(d.get(k) or ("{}" if k != "outputs" else "[]"))
            except Exception:
                d[k] = {} if k != "outputs" else []
        out.append(d)
    return out


def recover_running() -> int:
    """启动时调用：把所有 INIT/RUNNING 任务标记为 FAILED，补 failed_step。

    后端重启后异步任务已死，INIT/RUNNING 状态的任务永远卡住。
    根据 extra.current_step 推断失败步骤，写入 extra.failed_step。
    返回恢复的任务数。
    """
    init_db()
    running = list_running()
    count = 0
    for t in running:
        tid = t["task_id"]
        extra = t.get("extra", {}) or {}
        current_step = extra.get("current_step", "") or ""
        # current_step 可能是 "step1".."step5" 或 "done"
        failed_step = current_step if current_step.startswith("step") else "step1"
        extra["failed_step"] = failed_step
        stage = t.get("stage", "") or ""
        err = f"服务重启中断（卡在 {failed_step}）"
        update(
            tid,
            status="FAILED",
            error=err,
            stage=f"❌ {stage}（重启中断）" if stage else "❌ 重启中断",
            extra=extra,
        )
        add_log(tid, f"⚠ 服务重启，任务标记为失败（failed_step={failed_step}），可重试", level="error")
        count += 1
    return count


def add_log(task_id: str, msg: str, level: str = "info") -> None:
    """追加一条日志。"""
    init_db()
    with _lock, _conn() as c:
        c.execute(
            "INSERT INTO task_logs(task_id, ts, level, msg) VALUES(?,?,?,?)",
            (task_id, int(time.time()), level, msg)
        )


def get_logs(task_id: str, limit: int = 200) -> list:
    """读取日志（按时间正序，最多 limit 条）。"""
    init_db()
    with _lock, _conn() as c:
        rows = c.execute(
            "SELECT ts, level, msg FROM task_logs WHERE task_id=? ORDER BY id DESC LIMIT ?",
            (task_id, limit)
        ).fetchall()
    return [{"ts": r["ts"], "level": r["level"], "msg": r["msg"]} for r in reversed(rows)]


def delete(task_id: str) -> bool:
    """删除任务（含日志）。"""
    init_db()
    with _lock, _conn() as c:
        c.execute("DELETE FROM task_logs WHERE task_id=?", (task_id,))
        cur = c.execute("DELETE FROM tasks WHERE task_id=?", (task_id,))
        return cur.rowcount > 0
