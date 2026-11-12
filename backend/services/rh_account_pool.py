"""RunningHub 账号池管理

设计：
- 账号库：rh_accounts.json（手动维护，包含 phone/password/profile_dir/enabled）
- 运行时状态：rh_account_state.json（自动维护，保存 status/last_login/last_used/in_use 等）
- 每账号独立 profile 目录：d:\\nzdh\\rh_profiles\\rh_<phone>
- 调度：串行队列（_global_rpa_lock 保证同时只跑 1 个 Step5 RPA 任务）
- 换号触发：
  1. 检测到未登录/登录失效（open_workflow 看到"登 录"按钮）
  2. RunningHub 报风控/限流
  3. 该账号已在跑（in_use=True）
  换号策略：调用 next_available(current_phone) 取下一个 ready 且非 in_use 的账号
"""
import asyncio
import base64
import json
import os
import time
from dataclasses import dataclass, field, asdict, fields
from pathlib import Path
from typing import Optional

import httpx

BASE_DIR = Path(__file__).resolve().parent.parent.parent
ACCOUNTS_FILE = BASE_DIR / "rh_accounts.json"
STATE_FILE = BASE_DIR / "rh_account_state.json"
DEFAULT_PROFILE_ROOT = BASE_DIR / "rh_profiles"

# 状态枚举
STATUS_PENDING = "pending"        # 待首次登录
STATUS_READY = "ready"            # 已登录，可用
STATUS_IN_USE = "in_use"          # 正在使用
STATUS_LOGIN_FAILED = "login_failed"  # 登录失败（账号或密码错误）
STATUS_LOGIN_LOCKED = "login_locked"  # 密码登录被平台锁定
STATUS_LIMITED = "limited"        # 被风控/限流
STATUS_DISABLED = "disabled"      # 用户禁用


@dataclass
class RHAccount:
    """账号信息（静态配置，从 rh_accounts.json 加载）"""
    phone: str
    password: str
    note: str = ""
    enabled: bool = True
    profile_dir: str = ""  # 空字符串=用默认目录 DEFAULT_PROFILE_ROOT/rh_<phone>


@dataclass
class RHAccountState:
    """账号运行时状态（从 rh_account_state.json 加载/保存）"""
    phone: str
    status: str = STATUS_PENDING
    last_login_ts: int = 0
    last_login_ok: bool = False
    last_used_ts: int = 0
    in_use: bool = False
    fail_count: int = 0
    last_error: str = ""
    # 运行时实时状态（由 SDK 调用 accountStatus + getJsonApiFormat 校准）
    wf_exists: bool = False           # 本账号是否已有私有副本（runtime_wf_id 且 owner 对得上）
    runtime_wf_id: str = ""           # 账号私有副本工作流 ID（copy_workflow 后保存）
    runtime_wf_name: str = ""         # 账号私有副本名称
    runtime_wf_ts: int = 0            # 副本复制/校验时间戳
    api_key_cached: str = ""          # 缓存的真 API Key（避免每次登录）
    api_key_ts: int = 0               # API Key 缓存时间戳
    runtime_in_use: bool = False      # 平台视角：是否有正在运行的任务
    runtime_balance: str = ""         # 平台视角：钱包余额
    runtime_coins: str = ""           # 平台视角：RH 币余额
    runtime_concurrent_limit: int = 0  # 并发上限
    runtime_task_counts: int = 0       # 当前运行中任务数
    runtime_ts: int = 0                # 上次实时校准时间戳


# ───── 全局单例 ─────
_accounts: list[RHAccount] = []
_states: dict[str, RHAccountState] = {}
_global_rpa_lock: Optional[asyncio.Lock] = None


def _resolve_profile_dir(acc: RHAccount) -> str:
    """返回账号实际 profile 目录（空则用默认目录）"""
    if acc.profile_dir:
        return acc.profile_dir
    p = DEFAULT_PROFILE_ROOT / f"rh_{acc.phone}"
    p.mkdir(parents=True, exist_ok=True)
    return str(p)


def load_accounts() -> list[RHAccount]:
    """从 rh_accounts.json 加载所有启用的账号"""
    global _accounts
    if not ACCOUNTS_FILE.exists():
        print(f"[RHAccountPool] 账号库不存在: {ACCOUNTS_FILE}")
        _accounts = []
        return _accounts
    try:
        with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        accs = []
        for item in data.get("accounts", []):
            if not item.get("enabled", True):
                continue
            accs.append(RHAccount(
                phone=item["phone"],
                password=item["password"],
                note=item.get("note", ""),
                enabled=item.get("enabled", True),
                profile_dir=item.get("profile_dir", ""),
            ))
        _accounts = accs
        print(f"[RHAccountPool] 加载 {len(_accounts)} 个账号: {[a.phone for a in _accounts]}")
    except Exception as e:
        print(f"[RHAccountPool] 加载账号库失败: {e}")
        _accounts = []
    return _accounts


def _load_states() -> None:
    """从 rh_account_state.json 加载运行时状态（含校准字段，避免重启后丢失）"""
    global _states
    _states = {}
    known = {f.name for f in fields(RHAccountState)}
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            for phone, s in data.items():
                if not isinstance(s, dict):
                    continue
                kwargs = {k: s[k] for k in known if k in s}
                kwargs["phone"] = phone
                _states[phone] = RHAccountState(**kwargs)
        except Exception as e:
            print(f"[RHAccountPool] 加载状态失败: {e}")

    # 确保所有已加载账号都有对应 state
    for acc in _accounts:
        if acc.phone not in _states:
            _states[acc.phone] = RHAccountState(phone=acc.phone)


def _save_states() -> None:
    """保存运行时状态"""
    try:
        data = {phone: asdict(s) for phone, s in _states.items()}
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[RHAccountPool] 保存状态失败: {e}")


def init_pool() -> None:
    """初始化账号池：加载账号库 + 状态文件"""
    load_accounts()
    _load_states()
    # 启动时清掉所有 in_use 标记（避免上次崩溃残留）
    for s in _states.values():
        if s.in_use:
            s.in_use = False
            print(f"[RHAccountPool] 清除 {s.phone} 的 in_use 残留标记")
    _seed_known_copies()
    for s in _states.values():
        if not s.runtime_wf_id:
            s.wf_exists = False
    _save_states()


def get_account_by_phone(phone: str) -> Optional[RHAccount]:
    """按手机号获取账号"""
    for acc in _accounts:
        if acc.phone == phone:
            return acc
    return None


def upsert_account(
    phone: str,
    password: str,
    note: str = "",
    enabled: bool = True,
    profile_dir: str = "",
    cookie_only: bool = False,
) -> dict:
    """写入 rh_accounts.json。已存在则更新密码，返回 created=是否新号。

    cookie_only=True：Cookie 登录账号（首次引导注册，无密码，靠浏览器会话登录），password 可为空。
    """
    phone = (phone or "").strip()
    password = (password or "").strip()
    if not phone or (not password and not cookie_only):
        return {"ok": False, "created": False, "error": "phone 和 password 必填", "phone": phone}

    if ACCOUNTS_FILE.exists():
        with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = {"accounts": []}

    created = True
    for item in data.setdefault("accounts", []):
        if item.get("phone") == phone:
            item["password"] = password or item.get("password", "")
            if note:
                item["note"] = note
            item["enabled"] = enabled
            if profile_dir:
                item["profile_dir"] = profile_dir
            created = False
            break
    else:
        data["accounts"].append({
            "phone": phone,
            "password": password,
            "note": note,
            "enabled": enabled,
            "profile_dir": profile_dir,
        })

    with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    load_accounts()
    get_state(phone)
    acc = get_account_by_phone(phone)
    if acc:
        _resolve_profile_dir(acc)
    _save_states()
    return {"ok": True, "created": created, "error": "", "phone": phone}


def get_state(phone: str) -> RHAccountState:
    """获取账号状态（不存在则创建默认 pending）"""
    if phone not in _states:
        _states[phone] = RHAccountState(phone=phone)
    return _states[phone]


def get_profile_dir(phone: str) -> str:
    """获取账号的 profile 目录路径"""
    acc = get_account_by_phone(phone)
    if not acc:
        raise ValueError(f"账号不存在: {phone}")
    return _resolve_profile_dir(acc)


def _balance_value(st: RHAccountState) -> Optional[float]:
    """解析平台余额；解析不了返回 None（视为未知，不降权）"""
    try:
        return float(st.runtime_balance)
    except (TypeError, ValueError):
        return None


def get_available_account(exclude_phones: list[str] = None) -> Optional[RHAccount]:
    """获取一个可用账号（status=ready 且非 in_use）
    被占用（平台侧有正在跑任务）的账号排后，优先把任务派给空闲账号；
    全部被占用时才回退选占用中的账号（平台状态可能过期）。
    排序：正余额/余额未知优先，其次 last_used_ts 最早的（最久没用的先用）
    exclude_phones: 要排除的账号（已经失败过的）
    """
    exclude_phones = exclude_phones or []
    idle, busy = [], []
    for acc in _accounts:
        if not acc.enabled:
            continue
        if acc.phone in exclude_phones:
            continue
        st = get_state(acc.phone)
        if st.status != STATUS_READY or st.in_use:
            continue
        (busy if st.runtime_in_use else idle).append((acc, st))
    candidates = idle or busy
    if not candidates:
        return None
    # 负余额（欠费）账号排后，最后再用；同档按 last_used_ts 升序
    def _sort_key(x):
        bal = _balance_value(x[1])
        neg_rank = 1 if (bal is not None and bal < 0) else 0
        return (neg_rank, x[1].last_used_ts)
    candidates.sort(key=_sort_key)
    return candidates[0][0]


def next_available(current_phone: str, tried_phones: list[str] = None) -> Optional[RHAccount]:
    """当前账号失败时换下一个。

    tried_phones: 本次任务已尝试过的全部账号（必须全部排除）。
    只排除 current_phone 会导致 A↔B 互相换号死循环。
    """
    exclude = set(tried_phones or [])
    exclude.add(current_phone)
    return get_available_account(exclude_phones=list(exclude))


def mark_in_use(phone: str) -> None:
    """标记账号正在使用"""
    st = get_state(phone)
    st.in_use = True
    _save_states()


def mark_free(phone: str) -> None:
    """标记账号空闲"""
    st = get_state(phone)
    st.in_use = False
    # 本账号的平台任务已结束，清掉平台侧占用缓存，避免后续派发长期跳过该账号
    st.runtime_in_use = False
    st.runtime_task_counts = 0
    st.last_used_ts = int(time.time())
    _save_states()


def mark_ready(phone: str, login_ok: bool = True) -> None:
    """标记账号登录成功/可用"""
    st = get_state(phone)
    st.status = STATUS_READY if login_ok else STATUS_LOGIN_FAILED
    st.last_login_ts = int(time.time())
    st.last_login_ok = login_ok
    st.fail_count = 0 if login_ok else st.fail_count + 1
    _save_states()


def mark_failed(phone: str, reason: str, kind: str = "limited") -> None:
    """标记账号失败（被限流/风控等）"""
    st = get_state(phone)
    st.status = kind  # limited / login_failed / disabled
    st.last_error = reason
    st.fail_count += 1
    st.in_use = False
    _save_states()


def is_account_login_valid(phone: str) -> bool:
    """快速判断账号是否已登录（基于 state，不做实际验证）"""
    st = get_state(phone)
    return st.status in (STATUS_READY, STATUS_IN_USE) and st.last_login_ok


def get_global_rpa_lock() -> asyncio.Lock:
    """获取全局串行锁（保证同时只有 1 个 RPA 任务在跑）"""
    global _global_rpa_lock
    if _global_rpa_lock is None:
        _global_rpa_lock = asyncio.Lock()
    return _global_rpa_lock


def list_all_status() -> list[dict]:
    """列出所有账号的状态（用于前端展示/调试）"""
    result = []
    for acc in _accounts:
        st = get_state(acc.phone)
        # 综合判断当前显示状态（前端 UI 用）：
        # 平台在跑(runtime_in_use) 或 本系统占用(in_use) → 使用中
        # 工作流不存在 → 未就绪
        # 否则 → 空闲
        if st.status in (STATUS_LOGIN_FAILED, STATUS_LOGIN_LOCKED):
            ui_status = st.status
        elif st.runtime_in_use or st.in_use:
            ui_status = "in_use"
        elif not st.wf_exists and st.runtime_ts > 0:
            ui_status = "no_workflow"
        elif st.status in (STATUS_READY,):
            ui_status = "ready"
        else:
            ui_status = st.status
        result.append({
            "phone": acc.phone,
            "note": acc.note,
            "enabled": acc.enabled,
            "status": st.status,
            "ui_status": ui_status,  # 综合展示状态：ready/in_use/no_workflow/pending/...
            "in_use": st.in_use,
            "last_login_ts": st.last_login_ts,
            "last_login_ok": st.last_login_ok,
            "last_used_ts": st.last_used_ts,
            "fail_count": st.fail_count,
            "last_error": st.last_error,
            "profile_dir": _resolve_profile_dir(acc),
            # 运行时实时状态
            "wf_exists": st.wf_exists,
            "runtime_wf_id": st.runtime_wf_id,
            "runtime_wf_name": st.runtime_wf_name,
            "runtime_in_use": st.runtime_in_use,
            "runtime_task_counts": st.runtime_task_counts,
            "runtime_balance": st.runtime_balance,
            "runtime_coins": st.runtime_coins,
            "runtime_concurrent_limit": st.runtime_concurrent_limit,
            "runtime_ts": st.runtime_ts,
        })
    return result


def init_on_startup() -> None:
    """app.py 启动时调用：加载账号池"""
    init_pool()
    print(f"[RHAccountPool] 初始化完成：{len(_accounts)} 个账号")


# ───── 实时状态校准（通过 RunningHub SDK）─────
WORKFLOW_ID = "2106377827540103169"  # 公共坏节点动作迁移工作流（get_workflow_json 对谁都能读，不能当归属）
WORKFLOW_NAME = "动作迁移成功了！"
_API_KEY_CACHE_TTL = 86400  # API Key 缓存 24 小时
# 诊断脚本已 copy 过的私有副本，避免再次复制
_KNOWN_COPIES = {
    "18900279795": ("2103289823258828801", WORKFLOW_NAME),
    "18159181026": ("2103289828979863553", WORKFLOW_NAME),
}


def _token_cache_path(phone: str) -> str:
    return str(BASE_DIR / "rh_profiles" / f"rh_{phone}_token.json")


def _seed_known_copies() -> None:
    """把已确认的私有副本 ID 写进 state，刷新时再按 owner 校验。"""
    for phone, (wf_id, name) in _KNOWN_COPIES.items():
        st = _states.get(phone)
        if not st or st.runtime_wf_id:
            continue
        st.runtime_wf_id = wf_id
        st.runtime_wf_name = name
        st.wf_exists = True
        st.runtime_wf_ts = st.runtime_wf_ts or int(time.time())
        print(f"[RHAccountPool] 写入已知副本 {phone} -> {wf_id}")


def _get_cached_api_key(phone: str) -> Optional[str]:
    """从内存 state 拿缓存的 API Key（未过期则用）"""
    st = get_state(phone)
    if st.api_key_cached and (int(time.time()) - st.api_key_ts) < _API_KEY_CACHE_TTL:
        return st.api_key_cached
    return None


def _is_token_invalid(exc: Exception) -> bool:
    code = getattr(exc, "code", None)
    if code in (401, 412):
        return True
    raw = str(getattr(exc, "message", None) or exc)
    upper = raw.upper()
    if "TOKEN_INVALID" in upper or "TOKEN_EXPIRED" in upper or "[412]" in raw or "[401]" in raw:
        return True
    # 平台对过期登录态也可能直接回中文提示（如“token已过期”）
    return ("TOKEN" in upper or "登录" in raw) and ("过期" in raw or "失效" in raw)


def _invalidate_token_cache(phone: str) -> None:
    """丢掉被平台作废的登录态，下次走密码登录。"""
    p = Path(_token_cache_path(phone))
    try:
        if p.exists():
            p.unlink()
            print(f"[RHAccountPool] 已删除失效 token {phone}")
    except Exception as e:
        print(f"[RHAccountPool] 删除 token 失败 {phone}: {e}")
    st = get_state(phone)
    st.api_key_cached = ""
    st.api_key_ts = 0


def _get_login_client(phone: str, force_relogin: bool = False):
    """用登录 token 建客户端（copy_workflow 必须走 token，不能走 API Key）。"""
    try:
        from runninghub_sdk import RunningHubClient
    except Exception as e:
        # 没装 / 装坏了 / SDK 内部导入失败，把真实原因报出来，别让客户猜
        raise RuntimeError(f"runninghub-sdk 不可用（{e}），请重新双击「一键安装.bat」补装依赖") from e

    acc = get_account_by_phone(phone)
    if not acc:
        raise ValueError(f"账号不存在: {phone}")
    cache = _token_cache_path(phone)
    Path(cache).parent.mkdir(parents=True, exist_ok=True)
    if force_relogin:
        _invalidate_token_cache(phone)
        if not acc.password:
            raise RuntimeError(
                f"账号 {phone} 登录态已失效（Cookie 登录账号无密码可自动重登）；请刷新网页重新完成引导登录"
            )
        return RunningHubClient.from_login(acc.phone, acc.password, token_cache=cache)
    if Path(cache).exists():
        try:
            return RunningHubClient.from_token_cache(cache, password=acc.password)
        except Exception as e:
            print(f"[RHAccountPool] token 缓存不可用，重新登录 {phone}: {e}")
    if not acc.password:
        raise RuntimeError(
            f"账号 {phone} 缺少登录态（Cookie 登录账号无密码可自动重登）；请刷新网页重新完成引导登录"
        )
    return RunningHubClient.from_login(acc.phone, acc.password, token_cache=cache)


def _classify_login_error(exc: Exception) -> tuple[str, Optional[int], str]:
    """区分锁定 / 账号密码错误 / 其它登录失败。"""
    code = getattr(exc, "code", None)
    raw = str(getattr(exc, "message", None) or exc)
    blob = f"{code} {raw}".upper()
    if code == 126 or "USER_PWD_LOGIN_LOCKED" in blob or "LOGIN_LOCKED" in blob:
        return (
            "locked",
            code or 126,
            "密码登录被锁定（平台未判定密码错误）。等待 15–30 分钟后点「完成初始化」，或先在 runninghub.cn 用短信登录解锁。",
        )
    if code == 412 or "TOKEN_INVALID" in blob:
        return "token_invalid", code or 412, "登录态已失效，将重新校验账号密码"
    if any(k in raw for k in ("密码错误", "账号或密码", "用户名或密码", "密码不正确")):
        return "bad_credentials", code, f"账号或密码错误: {raw}"
    if any(k in blob for k in ("USER_PWD_ERROR", "USER_NOT_EXIST", "PASSWORD_ERROR", "BAD_CREDENTIAL")):
        return "bad_credentials", code, f"账号或密码错误: {raw}"
    return "other", code, raw[:200]


def _token_cache_valid(phone: str) -> bool:
    p = Path(_token_cache_path(phone))
    if not p.exists():
        return False
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        token = data.get("token") or {}
        if not token.get("access_token"):
            return False
        exp = int(token.get("expires_at_ms") or 0)
        if exp:
            return exp > int(time.time() * 1000) + 60_000
        return True
    except Exception:
        return False


async def verify_account_login(phone: str, force_relogin: bool = False) -> dict:
    """第一步：校验账号密码。优先复用 token / 无头浏览器会话，失效才重新登录。"""
    acc = get_account_by_phone(phone)
    st = get_state(phone)
    result = {
        "success": False,
        "error": "",
        "kind": "",
        "code": None,
        "reused_token": False,
        "via_browser": False,
    }
    if not acc:
        result["error"] = "账号不存在"
        result["kind"] = "missing"
        return result

    cache = _token_cache_path(phone)
    Path(cache).parent.mkdir(parents=True, exist_ok=True)

    try:
        stale_session = False
        if not force_relogin and _token_cache_valid(phone):
            try:
                token = _read_access_token(phone)
                _probe_login_token(token)
                result["reused_token"] = True
            except Exception as probe_err:
                if not _is_token_invalid(probe_err):
                    raise
                print(f"[RHAccountPool] {phone} 缓存 token 已失效，改走浏览器重新登录")
                stale_session = True
                _invalidate_token_cache(phone)
        # token 复用只覆盖 API 登录态；RPA 跑任务用的是浏览器会话（另一份独立登录态）。
        # 安装机的浏览器 profile 是全新的（Chromium 画像不打包），只验 token 不做浏览器登录，
        # 网页会报 token 失效（403 TOKEN_MISSION）。因此无论 token 是否复用，都要过一次
        # 浏览器登录：有会话秒过（复用），没会话就真实登录一次。
        if acc.password:
            from backend.services.rh_auto_login import browser_verify
            from config import settings as _cfg
            br = await browser_verify(
                acc.phone,
                acc.password,
                get_profile_dir(phone),
                headless=_cfg.RPA_HEADLESS,
                token_cache=cache,
                # token 被平台作废 / 强制重登时，浏览器里“看起来已登录”的旧会话同样已失效，
                # 必须清掉强制走密码登录，否则会误复用过期会话
                force=force_relogin or stale_session,
            )
        else:
            # Cookie 登录账号（首次引导注册，无密码）：打开浏览器让客户手动登录/续登并记录 Cookie，
            # 会话仍有效时几秒内自动通过
            from backend.services.rh_auto_login import cookie_login
            br = await cookie_login(
                acc.phone,
                get_profile_dir(phone),
                token_cache=cache,
                force=force_relogin or stale_session,
                timeout_seconds=300,
            )
        result["via_browser"] = True
        if br.get("reused_session"):
            result["reused_token"] = True
        if not br.get("ok"):
            kind = br.get("kind") or "other"
            msg = br.get("error") or "浏览器登录失败"
            st.last_login_ok = False
            st.last_error = msg
            st.status = STATUS_LOGIN_LOCKED if kind == "locked" else STATUS_LOGIN_FAILED
            if kind != "locked":
                st.fail_count += 1
            _save_states()
            result.update({"error": msg, "kind": kind})
            print(f"[RHAccountPool] 校验登录失败 {phone}: kind={kind} {msg}")
            return result
        token = _read_access_token(phone)
        if not token:
            raise RuntimeError(br.get("error") or "浏览器已登录，但未能导出 token")
        _probe_login_token(token)

        st.last_login_ok = True
        st.last_login_ts = int(time.time())
        st.last_error = ""
        st.fail_count = 0
        if st.status in (STATUS_PENDING, STATUS_LOGIN_FAILED, STATUS_LOGIN_LOCKED):
            st.status = STATUS_PENDING
        _save_states()
        result["success"] = True
        return result
    except Exception as e:
        kind, code, msg = _classify_login_error(e)
        st.last_login_ok = False
        st.last_error = msg
        st.status = STATUS_LOGIN_LOCKED if kind == "locked" else STATUS_LOGIN_FAILED
        if kind != "locked":
            st.fail_count += 1
        _save_states()
        result.update({"error": msg, "kind": kind, "code": code})
        print(f"[RHAccountPool] 校验登录失败 {phone}: kind={kind} {msg}")
        return result


async def setup_account(phone: str) -> dict:
    """添加账号后的固定流水线：校验密码 → 上传动作迁移工作流 → 更新状态。"""
    phone = (phone or "").strip()
    steps = {
        "verify": {"success": False, "error": "", "reused_token": False},
        "upload": {"success": False, "error": "", "skipped": False, "runtime_wf_id": ""},
        "status": {"success": False, "error": ""},
    }
    acc = get_account_by_phone(phone)
    if not acc:
        return {
            "success": False,
            "phone": phone,
            "failed_step": "verify",
            "message": "账号不存在",
            "steps": steps,
        }

    force_relogin = "TOKEN_INVALID" in (get_state(phone).last_error or "")
    verify = await verify_account_login(phone, force_relogin=force_relogin)
    steps["verify"] = {
        "success": bool(verify.get("success")),
        "error": verify.get("error") or "",
        "kind": verify.get("kind") or "",
        "reused_token": bool(verify.get("reused_token")),
        "via_browser": bool(verify.get("via_browser")),
    }
    if not verify.get("success"):
        return {
            "success": False,
            "phone": phone,
            "failed_step": "verify",
            "message": f"账号密码校验失败: {verify.get('error')}",
            "steps": steps,
        }

    upload = upload_workflow_to_account(phone)
    steps["upload"] = {
        "success": bool(upload.get("success")),
        "error": upload.get("error") or "",
        "skipped": bool(upload.get("skipped")),
        "runtime_wf_id": upload.get("runtime_wf_id") or "",
        "runtime_wf_name": upload.get("runtime_wf_name") or "",
    }
    if not upload.get("success"):
        return {
            "success": False,
            "phone": phone,
            "failed_step": "upload",
            "message": f"账号密码已通过，动作迁移工作流上传失败: {upload.get('error')}",
            "steps": steps,
        }

    refresh = refresh_account_runtime_status(phone)
    st = get_state(phone)
    if refresh.get("error"):
        steps["status"] = {"success": False, "error": refresh.get("error") or ""}
        st.status = STATUS_READY
        st.last_login_ok = True
        _save_states()
        return {
            "success": True,
            "phone": phone,
            "failed_step": "",
            "message": (
                f"账号密码已通过，工作流 {st.runtime_wf_id or '已上传'}，"
                f"状态刷新失败: {refresh.get('error')}"
            ),
            "steps": steps,
            "runtime_wf_id": st.runtime_wf_id,
        }

    st.status = STATUS_READY
    st.last_login_ok = True
    st.last_error = ""
    _save_states()
    steps["status"] = {
        "success": True,
        "error": "",
        "wf_exists": st.wf_exists,
        "runtime_wf_id": st.runtime_wf_id,
        "runtime_balance": st.runtime_balance,
        "runtime_coins": st.runtime_coins,
    }
    wf_note = "已有副本" if upload.get("skipped") else "已复制"
    return {
        "success": True,
        "phone": phone,
        "failed_step": "",
        "message": f"账号密码已通过，{wf_note}动作迁移工作流 {st.runtime_wf_id}，状态已更新",
        "steps": steps,
        "runtime_wf_id": st.runtime_wf_id,
    }


def _read_access_token(phone: str) -> str:
    p = Path(_token_cache_path(phone))
    if not p.exists():
        return ""
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return str(((data.get("token") or {}).get("access_token")) or "")
    except Exception:
        return ""


def _jwt_sub(access_token: str) -> str:
    try:
        parts = access_token.split(".")
        if len(parts) < 2:
            return ""
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
        return str(data.get("sub") or "")
    except Exception:
        return ""


def _rh_auth_headers(access_token: str) -> dict:
    token = (access_token or "").strip()
    if token.lower().startswith("bearer "):
        auth = token
    else:
        auth = f"Bearer {token}"
    return {
        "Host": "www.runninghub.cn",
        "Content-Type": "application/json",
        "Authorization": auth,
        "Origin": "https://www.runninghub.cn",
        "Referer": "https://www.runninghub.cn/",
        "User-Language": "zh_CN",
    }


def _portal_workflow_detail(access_token: str, workflow_id: str) -> Optional[dict]:
    """公开元数据接口：用 ID 查工作流，可看到 owner。不代表调用方拥有它。"""
    if not access_token or not workflow_id:
        return None
    try:
        with httpx.Client(
            base_url="https://www.runninghub.cn", timeout=20, headers=_rh_auth_headers(access_token)
        ) as client:
            resp = client.post("/api/portal/workflow/detail", json={"workflowId": str(workflow_id)})
            body = resp.json()
    except Exception as e:
        print(f"[RHAccountPool] portal/workflow/detail 失败: {e}")
        return None
    code = body.get("code")
    msg = body.get("msg") or body.get("message") or ""
    if code == 412 or "TOKEN_INVALID" in str(msg).upper():
        raise RuntimeError(f"[{code}] {msg or 'TOKEN_INVALID'}")
    if code != 0:
        return None
    return body.get("data") or None


def _probe_login_token(access_token: str) -> None:
    """真探活：调需要登录态的接口验证 token 是否可用。

    不能用 /api/portal/workflow/detail 探活——它是公开接口，拿假 token 也返回成功，
    会把已失效的登录态误判为“有效”。这里用 /uc/apiKey/get（必须带登录态）验证。
    登录态失效抛 RuntimeError，其余业务错误视为登录态可用。
    """
    if not access_token:
        raise RuntimeError("[412] TOKEN_INVALID")
    with httpx.Client(
        base_url="https://www.runninghub.cn", timeout=20, headers=_rh_auth_headers(access_token)
    ) as client:
        resp = client.post("/uc/apiKey/get", json={"userId": ""})
        body = resp.json()
    code = body.get("code")
    if code not in (None, 0, "0"):
        err = RuntimeError(f"[{code}] {body.get('msg') or body.get('message') or 'probe login token failed'}")
        if _is_token_invalid(err):
            raise err
        print(f"[RHAccountPool] 探活接口返回业务错误（视为登录态可用）: {err}")


def _copy_workflow(access_token: str, workflow_id: str) -> dict:
    """用登录 JWT 复制工作流。只走 Authorization Bearer，不把 JWT 塞进 apiKey。"""
    if not access_token:
        raise RuntimeError("缺少登录 token，无法复制工作流")
    with httpx.Client(
        base_url="https://www.runninghub.cn", timeout=60, headers=_rh_auth_headers(access_token)
    ) as client:
        resp = client.post(
            "/api/workflow/copy",
            json={"workflowId": str(workflow_id), "copyMode": 1},
        )
        body = resp.json()
    code = body.get("code", 0)
    msg = body.get("msg") or body.get("message") or ""
    if code != 0:
        raise RuntimeError(f"[{code}] {msg or 'copy_workflow 失败'}")
    data = body.get("data") or {}
    if not data.get("id"):
        raise RuntimeError("copy_workflow 未返回新工作流 ID")
    return data


def _owned_by_user(detail: Optional[dict], user_id: str) -> bool:
    if not detail or not user_id:
        return False
    owner = detail.get("owner") or {}
    return str(owner.get("id") or "") == str(user_id)


def _sync_wf_ownership(phone: str, access_token: str) -> None:
    """按 owner 校验私有副本；get_workflow_json 不能当归属。"""
    st = get_state(phone)
    user_id = _jwt_sub(access_token)
    if st.runtime_wf_id:
        detail = _portal_workflow_detail(access_token, st.runtime_wf_id)
        if detail is None:
            print(f"[RHAccountPool] {phone} 无法校验副本 {st.runtime_wf_id}，保留原记录")
            return
        if _owned_by_user(detail, user_id):
            st.wf_exists = True
            st.runtime_wf_name = str(detail.get("name") or st.runtime_wf_name or WORKFLOW_NAME)
            st.runtime_wf_ts = int(time.time())
            return
        print(f"[RHAccountPool] {phone} 记录的副本 {st.runtime_wf_id} 不属于该账号，清除")
        st.runtime_wf_id = ""
        st.runtime_wf_name = ""
        st.runtime_wf_ts = 0

    public = _portal_workflow_detail(access_token, WORKFLOW_ID)
    if _owned_by_user(public, user_id):
        st.runtime_wf_id = WORKFLOW_ID
        st.runtime_wf_name = str((public or {}).get("name") or WORKFLOW_NAME)
        st.runtime_wf_ts = int(time.time())
        st.wf_exists = True
        return

    st.wf_exists = False


def workflow_url_for_phone(phone: str = "") -> str:
    """RPA 打开的工作流 URL：有私有副本用副本，否则用公共 ID。"""
    if phone:
        st = get_state(phone)
        if st.runtime_wf_id:
            return f"https://www.runninghub.cn/workflow/{st.runtime_wf_id}?source=workspace"
    return f"https://www.runninghub.cn/workflow/{WORKFLOW_ID}?source=workspace"


def upload_workflow_to_account(phone: str, _retried: bool = False) -> dict:
    """把公共坏节点工作流 copy 到该账号名下。已有且 owner 对得上则跳过。"""
    acc = get_account_by_phone(phone)
    if not acc:
        return {"phone": phone, "success": False, "error": "账号不存在"}
    st = get_state(phone)
    result = {"phone": phone, "success": False, "error": "", "skipped": False}

    try:
        if _retried:
            _get_login_client(phone, force_relogin=True)
        elif not _read_access_token(phone):
            _get_login_client(phone)
        access_token = _read_access_token(phone)
        if not access_token:
            raise RuntimeError("登录成功但未拿到 token")
        _sync_wf_ownership(phone, access_token)
        if st.wf_exists and st.runtime_wf_id:
            _save_states()
            result.update({
                "success": True,
                "skipped": True,
                "runtime_wf_id": st.runtime_wf_id,
                "runtime_wf_name": st.runtime_wf_name,
                "wf_exists": True,
            })
            return result

        data = _copy_workflow(access_token, WORKFLOW_ID)
        new_id = str(data.get("id") or "")
        if not new_id:
            raise RuntimeError("copy_workflow 未返回新工作流 ID")
        content = data.get("workflowContent") or ""
        if "285" not in content or "600" not in content:
            print(f"[RHAccountPool] 警告: 副本 {new_id} 内容未同时看到 285/600")

        st.runtime_wf_id = new_id
        st.runtime_wf_name = data.get("name") or WORKFLOW_NAME
        st.runtime_wf_ts = int(time.time())
        st.wf_exists = True
        st.last_error = ""
        _save_states()
        result.update({
            "success": True,
            "runtime_wf_id": new_id,
            "runtime_wf_name": st.runtime_wf_name,
            "wf_exists": True,
        })
        return result
    except Exception as e:
        if not _retried and _is_token_invalid(e):
            print(f"[RHAccountPool] {phone} 上传时 token 失效，重新登录再试")
            _invalidate_token_cache(phone)
            return upload_workflow_to_account(phone, _retried=True)
        st.last_error = str(e)[:200]
        _save_states()
        result["error"] = str(e)[:200]
        return result


def refresh_account_runtime_status(phone: str, force_refresh_api_key: bool = False, _retried: bool = False) -> dict:
    """实时校准：余额/任务数 + 私有副本归属（不用 get_workflow_json 判断）。"""
    acc = get_account_by_phone(phone)
    if not acc:
        return {"phone": phone, "error": "账号不存在"}

    try:
        from runninghub_sdk import RunningHubClient
    except Exception as e:
        # 没装 / 装坏了 / SDK 内部导入失败，把真实原因报出来，别让客户猜
        return {"phone": phone, "error": f"runninghub-sdk 不可用（{e}），请重新双击「一键安装.bat」补装依赖"}

    token_cache_file = _token_cache_path(phone)
    Path(token_cache_file).parent.mkdir(parents=True, exist_ok=True)

    result = {"phone": phone, "error": ""}
    st = get_state(phone)

    try:
        login_client = _get_login_client(phone, force_relogin=_retried)
        access_token = _read_access_token(phone) or getattr(login_client, "api_key", "")

        real_api_key = None if force_refresh_api_key else _get_cached_api_key(phone)
        if not real_api_key:
            api_keys = login_client.get_user_api_key(user_id="")
            real_api_key = api_keys.shared_api.api_key or api_keys.normal_api_key
            st.api_key_cached = real_api_key
            st.api_key_ts = int(time.time())
            st.runtime_balance = str(api_keys.balance_info.balance or "")
            st.runtime_concurrent_limit = api_keys.shared_api.concurrent_limit or 0
            st.last_login_ok = True
            if st.status == STATUS_PENDING:
                st.status = STATUS_READY
            st.last_login_ts = int(time.time())
            _save_states()

        client2 = RunningHubClient(api_key=real_api_key)
        acc_status = client2.get_account_status()
        count = int(acc_status.current_task_counts or "0")
        st.runtime_task_counts = count
        st.runtime_in_use = count > 0
        st.runtime_coins = str(acc_status.remain_coins or "")
        st.runtime_balance = str(acc_status.remain_money or st.runtime_balance)

        _sync_wf_ownership(phone, access_token)

        st.runtime_ts = int(time.time())
        _save_states()

        result.update({
            "wf_exists": st.wf_exists,
            "runtime_wf_id": st.runtime_wf_id,
            "runtime_wf_name": st.runtime_wf_name,
            "runtime_in_use": st.runtime_in_use,
            "runtime_task_counts": st.runtime_task_counts,
            "runtime_balance": st.runtime_balance,
            "runtime_coins": st.runtime_coins,
            "runtime_concurrent_limit": st.runtime_concurrent_limit,
            "runtime_ts": st.runtime_ts,
        })
        return result

    except Exception as e:
        if not _retried and _is_token_invalid(e):
            print(f"[RHAccountPool] {phone} 校准时 token 失效，重新登录再试")
            _invalidate_token_cache(phone)
            return refresh_account_runtime_status(phone, force_refresh_api_key=True, _retried=True)
        st.last_error = str(e)[:200]
        _save_states()
        result["error"] = str(e)[:200]
        return result


def refresh_all_runtime_status(force_refresh_api_key: bool = False) -> list[dict]:
    """校准所有账号的实时状态"""
    return [
        refresh_account_runtime_status(acc.phone, force_refresh_api_key=force_refresh_api_key)
        for acc in _accounts
        if acc.enabled
    ]


def is_account_truly_available(phone: str) -> bool:
    """可派 RPA：登录 OK + 空闲。公共工作流 URL 也能跑，不强制私有副本。"""
    st = get_state(phone)
    if not st.last_login_ok:
        return False
    if st.runtime_in_use or st.in_use:
        return False
    return True


def get_available_account_truly(exclude_phones: list[str] = None) -> Optional[RHAccount]:
    """真正可用的账号：登录OK + 平台空闲 + 本系统未占用"""
    exclude_phones = exclude_phones or []
    candidates = []
    for acc in _accounts:
        if not acc.enabled or acc.phone in exclude_phones:
            continue
        if is_account_truly_available(acc.phone):
            candidates.append((acc, get_state(acc.phone)))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[1].last_used_ts)
    return candidates[0][0]
