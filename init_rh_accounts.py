"""安装期自动登录 RunningHub 账号：装完即登录完成，客户不用懂登录态。

由「一键安装.bat」自动调用，也可手动运行：
  python init_rh_accounts.py          自检依赖 + 全部启用账号登录初始化
  python init_rh_accounts.py --check  只自检依赖

每个账号跑 setup_account 流水线：校验账号密码（自动打开浏览器完成真实登录，
登录态存 rh_profiles/*_token.json 和浏览器 profile，两份都要有）→ 上传动作迁移工作流
→ 更新状态。之后启动程序免登录。退出码：0 全部成功；1 有账号失败；2 依赖缺失。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 关键依赖：真导入一次（而不只是找得到）才算过关，装坏了也能查出真实原因
CRITICAL_IMPORTS = (
    ("fastapi", "fastapi"),
    ("uvicorn", "uvicorn"),
    ("httpx", "httpx"),
    ("playwright", "playwright"),
    ("runninghub_sdk", "runninghub-sdk"),
)


def check_deps() -> bool:
    print("依赖自检：")
    ok = True
    for mod, dist in CRITICAL_IMPORTS:
        try:
            __import__(mod)
            print(f"  [OK] {dist}")
        except Exception as e:
            ok = False
            print(f"  [缺失/损坏] {dist}: {e}")
    return ok


def _mask(phone: str) -> str:
    return f"{phone[:3]}****{phone[-4:]}" if len(phone) >= 7 else phone


def _step_line(name: str, step: dict, ok_extra: str) -> None:
    if step.get("success"):
        print(f"  [OK] {name}：{step.get('error') or ok_extra}")
    else:
        print(f"  [X]  {name}：{step.get('error') or '失败'}")


async def _login_all() -> int:
    from backend.services.rh_account_pool import init_pool, load_accounts, setup_account

    init_pool()
    accounts = [a for a in load_accounts() if a.enabled]
    if not accounts:
        print("rh_accounts.json 里没有启用的账号，跳过登录。")
        return 0

    print(f"开始登录 {len(accounts)} 个 RunningHub 账号（自动打开浏览器完成真实登录，无需手动操作；请勿关闭弹出的浏览器窗口）...")
    failed = 0
    for i, acc in enumerate(accounts, 1):
        print(f"\n[{i}/{len(accounts)}] 账号 {_mask(acc.phone)}")
        try:
            r = await setup_account(acc.phone)
        except Exception as e:
            failed += 1
            print(f"  [X]  初始化异常：{e}")
            continue
        steps = r.get("steps") or {}
        verify = steps.get("verify") or {}
        _step_line(
            "校验账号密码",
            verify,
            "通过（复用登录态，无需重新登录）" if verify.get("reused_token") else "通过",
        )
        up = steps.get("upload") or {}
        up_extra = (up.get("runtime_wf_id") or "完成") + ("（已有副本）" if up.get("skipped") else "")
        _step_line("上传动作迁移工作流", up, up_extra)
        _step_line("更新状态", steps.get("status") or {}, "完成")
        if r.get("success"):
            print(f"  结果：{r.get('message')}")
        else:
            failed += 1
            print(f"  结果：未完成（卡在 {r.get('failed_step')}）{r.get('message')}")

    print("\n" + "=" * 44)
    print(f"登录初始化完成：成功 {len(accounts) - failed} / {len(accounts)}")
    if failed:
        print("没登录成功的账号不影响程序启动；")
        print("稍后打开网页，在「账号池」里点「完成初始化」即可重试。")
    else:
        print("登录态已保存到 rh_profiles\\，下次启动免登录。")
    return 1 if failed else 0


def main() -> int:
    if not check_deps():
        print("\n依赖不全或损坏，请重新双击「一键安装.bat」（会用包内 vendor 离线补齐）。")
        return 2
    if "--check" in sys.argv:
        return 0
    return asyncio.run(_login_all())


if __name__ == "__main__":
    sys.exit(main())
