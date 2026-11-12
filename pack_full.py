"""把女装带货工作流打成可离线安装的全量包：源码 + wheel。
产物 dist/nzdh-full.zip，目标机解压后双击「一键安装.bat」自动装离线依赖、装浏览器并启动。

打包口径（商业化分发）：
- 历史记录：只保留「一键示例任务」（data/tasks.db 过滤 + outputs 只留示例素材），其他历史不打包
- 不打包：用户上传素材、抖音登录态（douyin_storage_state.json）、运行时状态（rh_account_state.json）、日志、缓存
- 账号池/登录态一律不随包分发（本机保留不动）：rh_accounts.json、rh_profiles/*（含 token）
  首次启动由三页引导弹窗带客户注册 RunningHub（记录 Cookie）并注入动作迁移工作流
- Agnes API Key 不随包分发（本机保留不动）：STAGE 副本 .env 三变量置空 + config.py 默认值抹掉，
  由引导第 2 页让客户自行注册填写
- vendor：按 64 位 Python 3.10~3.14 多目标下载 wheel（pip --python-version），
  客户机 Python 版本不同也能离线装全
"""
from __future__ import annotations

import re
import shutil
import sqlite3
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST_DIR = ROOT / "dist"
STAGE = DIST_DIR / "nzdh-full"
ZIP_PATH = DIST_DIR / "nzdh-full.zip"

# 一键示例任务 ID（前端 loadExampleTask 引用其素材；历史记录只保留它）
EXAMPLE_TASK_ID = "3174cfdb6365"

SKIP_DIR_NAMES = {
    "__pycache__",
    ".git",
    "dist",
    "logs",
    "Cache",
    "Code Cache",
    "GPUCache",
    "GrShaderCache",
    "ShaderCache",
    "DawnGraphiteCache",
    "DawnWebGPUCache",
    "GPUPersistentCache",
    "Crashpad",
    "BrowserMetrics",
    "Safe Browsing",
    "component_crx_cache",
    "extensions_crx_cache",
}
SKIP_FILE_SUFFIXES = {".pyc", ".pyo", ".pma"}
# 不随包分发的文件：任务历史库（另行生成过滤版）、运行时状态、浏览器登录态、账号池
SKIP_FILE_NAMES = {
    "tasks.db", "tasks.db-shm", "tasks.db-wal",
    "rh_account_state.json",
    "rh_accounts.json",
    "douyin_storage_state.json", "douyin_nickname.txt",
}
# uploads 只保留默认素材（兜底模特图/橱窗背景），用户上传的一律不打包
KEEP_UPLOAD_FILES = {"default_girl_model.jpg", "default_showcase_bg.jpg"}


def _should_skip(path: Path) -> bool:
    rel = path.relative_to(ROOT)
    parts = rel.parts
    if set(parts) & SKIP_DIR_NAMES:
        return True
    if path.suffix.lower() in SKIP_FILE_SUFFIXES:
        return True
    if path.name in SKIP_FILE_NAMES:
        return True
    # outputs：只保留一键示例任务素材，其他历史产物不打包
    if "outputs" in parts and path.is_file():
        i = parts.index("outputs")
        tail = parts[i + 1:]
        if not (len(tail) == 3 and tail[0] == "womenswear_studio" and tail[1] == EXAMPLE_TASK_ID):
            return True
    # uploads：只保留默认素材
    if parts and parts[0] == "uploads" and path.is_file():
        return path.name not in KEEP_UPLOAD_FILES
    # rh_profiles：登录态/浏览器画像一律不打包（本机保留不动），
    # 客户首次启动由引导弹窗自行注册 RunningHub 并记录 Cookie
    if parts and parts[0] == "rh_profiles" and len(parts) > 1:
        return True
    return False


def copy_tree() -> None:
    if STAGE.exists():
        shutil.rmtree(STAGE)
    STAGE.mkdir(parents=True)

    for src in ROOT.rglob("*"):
        if src.resolve() == Path(__file__).resolve():
            pass
        try:
            src.relative_to(ROOT)
        except ValueError:
            continue
        if src == DIST_DIR or DIST_DIR in src.parents:
            continue
        if _should_skip(src):
            continue
        dest = STAGE / src.relative_to(ROOT)
        if src.is_dir():
            dest.mkdir(parents=True, exist_ok=True)
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(src, dest)
            except (PermissionError, OSError) as e:
                # 运行中的后端/RPA 可能占用个别文件，跳过不中断打包
                print(f"跳过被占用文件: {src.relative_to(ROOT)} ({e})")

    (STAGE / "outputs" / "womenswear_studio").mkdir(parents=True, exist_ok=True)
    (STAGE / "uploads").mkdir(parents=True, exist_ok=True)
    (STAGE / "rh_profiles").mkdir(parents=True, exist_ok=True)
    (STAGE / "data").mkdir(parents=True, exist_ok=True)


def build_tasks_db() -> None:
    """生成过滤版 data/tasks.db：只保留一键示例任务的历史记录，其他任务/日志删除。

    用 SQLite backup API 拷贝，避免后端正在读写 tasks.db 时 shutil.copy2 被文件锁拒绝。
    """
    src = ROOT / "data" / "tasks.db"
    dst = STAGE / "data" / "tasks.db"
    if not src.exists():
        return
    try:
        src_conn = sqlite3.connect(str(src))
        dst_conn = sqlite3.connect(str(dst))
        try:
            src_conn.backup(dst_conn)
        finally:
            src_conn.close()
            dst_conn.close()
    except sqlite3.Error as e:
        print(f"tasks.db 拷贝失败，跳过: {e}")
        return
    conn = sqlite3.connect(str(dst))
    try:
        conn.execute("DELETE FROM task_logs WHERE task_id != ?", (EXAMPLE_TASK_ID,))
        conn.execute("DELETE FROM tasks WHERE task_id != ?", (EXAMPLE_TASK_ID,))
        conn.commit()
        conn.execute("VACUUM")
    finally:
        conn.close()
    print(f"tasks.db 过滤完成：只保留示例任务 {EXAMPLE_TASK_ID}")


AGNES_KEY_VARS = ("IMAGE2_API_KEY", "MIMO_API_KEY", "OPENAI_API_KEY")


def strip_agnes_keys() -> None:
    """抹掉 STAGE 副本里的 Agnes Key（.env 三变量置空 + config.py 默认值抹掉）。

    只动打包暂存区，本机 .env / config.py 一律不碰；
    客户首次启动由引导第 2 页自行注册 Agnes Key。
    """
    env = STAGE / ".env"
    if env.is_file():
        lines = []
        for line in env.read_text(encoding="utf-8").splitlines():
            key = line.split("=", 1)[0].strip()
            if key in AGNES_KEY_VARS:
                lines.append(f"{key}=")
            else:
                lines.append(line)
        env.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print("已清空 STAGE/.env 的 Agnes Key（本机 .env 未动）")

    cfg = STAGE / "config.py"
    if cfg.is_file():
        text = cfg.read_text(encoding="utf-8")
        # 把三个密钥字段的默认值里的 sk-xxx 剔除（不管原来填的是什么 key）
        new = re.sub(
            r'((?:IMAGE2_API_KEY|MIMO_API_KEY|OPENAI_API_KEY)\s*:\s*str\s*=\s*)"[^"]*"',
            r'\1""',
            text,
        )
        if new != text:
            cfg.write_text(new, encoding="utf-8")
            print("已抹掉 STAGE/config.py 的 Agnes Key 默认值（本机 config.py 未动）")


# 离线 wheel 覆盖的客户 Python 版本（64 位 Windows）；每个版本各解析下载一套，
# 同一 vendor 目录累加共存（如 pillow 11.3.0 各 cp3xx wheel、numpy 2.2/2.4/2.5）
WHEEL_PY_VERSIONS = ("3.10", "3.11", "3.12", "3.13", "3.14")


def download_wheels() -> None:
    vendor = STAGE / "vendor"
    vendor.mkdir(parents=True, exist_ok=True)
    for pyver in WHEEL_PY_VERSIONS:
        cmd = [
            sys.executable,
            "-m",
            "pip",
            "download",
            "-r",
            str(ROOT / "requirements.txt"),
            "-d",
            str(vendor),
            "--only-binary",
            ":all:",
            "--platform",
            "win_amd64",
            "--python-version",
            pyver,
        ]
        ok = False
        for attempt in range(3):
            print(f"pip download -> {vendor} (Python {pyver})  attempt {attempt+1}/3")
            try:
                subprocess.check_call(cmd)
                ok = True
                break
            except subprocess.CalledProcessError as e:
                print(f"  pip download failed (attempt {attempt+1}): {e}")
                import time; time.sleep(2)
        if not ok:
            print(f"  ⚠ Python {pyver} wheel 下载全部失败，跳过该版本（vendor 会缺这一组）")


def write_install_readme() -> None:
    text = """# 女装带货全量包

RPA 有头。任意路径双击 `start.bat` 即可。

## 首次使用引导（启动后自动弹出，按 3 步走）

本包**不带任何账号 / Key**，第一次打开网页会弹出引导（不可关闭）：

1. **注册 / 登录 RunningHub**（出片算力平台，手机号免费注册，新用户送 RH 币）
   点「打开注册/登录页」会打开官方邀请注册链接；完成注册并登录后工具自动记录登录 Cookie
   （无需再填账号密码），「下一步」按钮亮起；点击下一步自动把出片工作流注入你的账号
2. **注册 Agnes API Key**（生图 / 视频模型，免费注册）
   打开 https://platform.agnes-ai.cn 注册 → 控制台 → API Key 管理 → 创建并复制 Key 填入
   （API 地址 / 模型名默认即可），填好 Key 后「下一步」才会亮起
3. **载入示例任务**一键体验完整流程，或点「取消」自己新建任务

> 之后若 RunningHub 登录 Cookie 过期，刷新网页会重新弹出引导，重新登录一次即可。

## 安装（一键）

双击 `一键安装.bat`，自动完成：

1. 安装离线依赖（用包内 `vendor` 的 wheel，不用联网）
2. 校验依赖是否装全（缺什么会明确报出来，不让客户猜）
3. 安装 Playwright 浏览器（这一步需要联网）
4. 在桌面创建「女装带货工作台」快捷方式（按当前解压路径生成，不写死盘符）并启动

没装 Python 也没关系：脚本会自动打开 Python 官方下载页
（安装时务必勾选 "Add python.exe to PATH"），装完重新双击 `一键安装.bat` 即可。
> 离线依赖支持 **64 位 Python 3.10 ~ 3.14**（推荐 3.12），wheel 已按各版本备齐，安装脚本自动匹配你的 Python。

> 以后若挪动了文件夹，重新双击 `一键安装.bat` 就能重建桌面快捷方式。

手动安装（排错时）：

```bat
python -m pip install --no-index --find-links vendor -r requirements.txt
python -m playwright install chromium
python init_rh_accounts.py --check
```

## 启动

双击桌面「女装带货工作台」快捷方式，或任意目录执行 `start.bat`，启动后自动打开浏览器。

页面：http://127.0.0.1:8091/womenswear

## e2e

```bat
python e2e_check.py
```
"""
    (STAGE / "PACK_README.md").write_text(text, encoding="utf-8")


def write_install_bat() -> None:
    """一键安装脚本：装依赖 + 校验依赖 + 装浏览器 + 建桌面快捷方式 + 启动。

    不做自动登录：包内不带账号，首次启动由网页引导弹窗带客户注册 RunningHub（记录 Cookie）。
    快捷方式用 %~dp0 取「脚本当前所在目录」，因此客户解压到任意盘符/路径都能用。
    """
    text = r"""@echo off
chcp 936 >nul
cd /d "%~dp0"
title 女装带货工作台 - 一键安装
echo.
echo  ==========================================
echo    女装带货工作台 一键安装
echo  ==========================================
echo.

python -c "import sys" >nul 2>&1
if errorlevel 1 goto :nopython

python -c "import struct,sys; sys.exit(0 if ((3, 10) <= sys.version_info[:2] <= (3, 14) and struct.calcsize('P') == 8) else 1)" >nul 2>&1
if errorlevel 1 echo  [提示] 本包离线依赖支持「64 位 Python 3.10~3.14」；你的 Python 不符合，若第 1 步报错请改装 64 位 Python 3.12。

echo  [1/4] 安装离线依赖（用包内 vendor 的 wheel，不用联网）...
python -m pip install --no-index --find-links vendor -r requirements.txt
if errorlevel 1 goto :fail

echo  [2/4] 校验依赖是否装全（防止半截安装）...
python init_rh_accounts.py --check
if errorlevel 1 goto :faildeps

echo  [3/4] 安装 Playwright 浏览器（这一步需要联网）...
python -m playwright install chromium
if errorlevel 1 echo        浏览器下载失败，可稍后手动执行：python -m playwright install chromium

echo  [4/4] 创建桌面快捷方式（按当前解压路径生成，不写死盘符）并启动...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path ([Environment]::GetFolderPath('Desktop')) '女装带货工作台.lnk')); $s.TargetPath='%~dp0start.bat'; $s.WorkingDirectory='%~dp0'; $s.Description='女装带货工作台'; $s.Save()"

echo.
echo  安装完成，正在启动服务，稍后会自动打开网页...
echo  首次打开网页会弹出「使用引导」：按提示注册 RunningHub 账号和 Agnes API Key 即可。
echo.
call "%~dp0start.bat"
goto :eof

:nopython
echo.
echo  [提示] 没检测到 Python，正在打开官方下载页...
echo  安装时请务必勾选 "Add python.exe to PATH"，装完重新双击本文件即可。
start "" https://www.python.org/downloads/
pause
exit /b 1

:fail
echo.
echo  [出错] 依赖安装失败，请把上面的报错截图发给技术支持。
pause
exit /b 1

:faildeps
echo.
echo  [出错] 依赖没装全（常见原因：Python 不是 64 位 3.10~3.14，或上次安装被中断）。
echo  请改装 64 位 Python 3.12（安装时勾选 "Add python.exe to PATH"），
echo  然后重新双击本文件；仍失败请把上面的报错截图发给技术支持。
pause
exit /b 1
"""
    (STAGE / "一键安装.bat").write_text(text, encoding="gbk")


def make_zip() -> None:
    if ZIP_PATH.exists():
        ZIP_PATH.unlink()
    print("zip ->", ZIP_PATH)
    with zipfile.ZipFile(ZIP_PATH, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in STAGE.rglob("*"):
            if f.is_file():
                zf.write(f, f.relative_to(DIST_DIR))


def main() -> None:
    print("stage", STAGE)
    copy_tree()
    strip_agnes_keys()
    build_tasks_db()
    write_install_readme()
    write_install_bat()
    download_wheels()
    make_zip()
    size_mb = ZIP_PATH.stat().st_size / 1024 / 1024
    print(f"done {ZIP_PATH} ({size_mb:.1f} MB)")


if __name__ == "__main__":
    main()
