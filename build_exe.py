"""构建单 exe 商业交付包（双击 → 首次自动解压 → 自动启动 → 弹出前端网页）。

流程：
1) stage = dist/nzdh-full（复用 pack_full：源码 + .env + 账号池 + 过滤版 tasks.db）
2) 便携 Python embed 解压进 stage/python，依赖 --target 装进 Lib/site-packages
3) Playwright Chromium 下载进 stage/browsers（RPA 有头浏览器）
4) stage 打成 payload.zip，用 csc 把 zip 作为资源编译进 C# 启动器
5) 产出 dist/动作迁移工作台.exe

二次双击：检测到已安装标记直接启动，不再解压。
用法: python build_exe.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
BUILD = DIST / "exe-build"
STAGE = DIST / "nzdh-full"
PAYLOAD_ZIP = BUILD / "payload.zip"
EXE_OUT = DIST / "动作迁移工作台.exe"

PY_VER = "3.12.9"
PY_EMBED_URL = f"https://www.python.org/ftp/python/{PY_VER}/python-{PY_VER}-embed-amd64.zip"
PY_TAG = "python312"  # embed 包内 ._pth 文件名前缀
MARKER = "payload-v1.done"
CSC = r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
SEVEN_ZIP = r"C:\Program Files\7-Zip\7z.exe"


def sh(args: list[str], cwd: Path | None = None, env: dict | None = None) -> None:
    print("+", " ".join(str(a) for a in args))
    e = os.environ.copy()
    if env:
        e.update(env)
    subprocess.run(args, cwd=str(cwd) if cwd else None, env=e, check=True)


def build_stage() -> None:
    """复用 pack_full 的口径生成 stage（不含 vendor wheel / dist）。"""
    import pack_full

    if STAGE.exists():
        shutil.rmtree(STAGE)
    pack_full.STAGE = STAGE  # type: ignore[attr-defined]
    pack_full.copy_tree()
    pack_full.build_tasks_db()
    pack_full.write_install_readme()
    for fat in ("vendor", "dist"):
        p = STAGE / fat
        if p.exists():
            shutil.rmtree(p)
    print("stage 就绪:", STAGE)


def install_python() -> None:
    pydir = STAGE / "python"
    if pydir.exists():
        shutil.rmtree(pydir)
    pydir.mkdir(parents=True)

    embed_zip = BUILD / "python-embed.zip"
    if not embed_zip.exists():
        print("下载便携 Python", PY_VER)
        urllib.request.urlretrieve(PY_EMBED_URL, embed_zip)
    sh(["powershell", "-NoProfile", "-Command",
        f"Expand-Archive -Path '{embed_zip}' -DestinationPath '{pydir}' -Force"])

    # embed 版默认不带 site-packages，需要在 ._pth 里放行
    pth = pydir / f"{PY_TAG}._pth"
    pth.write_text(
        f"{PY_TAG}.zip\n.\n..\nLib\\site-packages\nimport site\n",
        encoding="utf-8",
    )

    # 用本机同版本 pip 把依赖装进 embed 的 site-packages
    site_pkgs = pydir / "Lib" / "site-packages"
    site_pkgs.mkdir(parents=True)

    pip_ok = False
    for attempt in range(2):
        try:
            print(f"pip install → embed site-packages (attempt {attempt+1})")
            sh([sys.executable, "-m", "pip", "install", "-r", str(ROOT / "requirements.txt"),
                "--target", str(site_pkgs), "-q", "--disable-pip-version-check",
                "--retries", "5", "--timeout", "60"])
            pip_ok = True
            break
        except Exception as e:
            print(f"  pip install 失败: {e}")
    if not pip_ok:
        # fallback：直接复制开发机的 site-packages
        dev_sp = Path(sys.executable).parent / "Lib" / "site-packages"
        if dev_sp.exists():
            print(f"  fallback 复制开发机 site-packages ({dev_sp})")
            for item in dev_sp.iterdir():
                dst = site_pkgs / item.name
                try:
                    if item.is_dir():
                        shutil.copytree(item, dst, dirs_exist_ok=True)
                    else:
                        shutil.copy2(item, dst)
                except PermissionError:
                    pass  # 被占用的文件跳过
        else:
            raise RuntimeError("pip install 失败 + fallback site-packages 不存在，无法继续")
    print("依赖装入 embed Python 完成")


def install_chromium() -> None:
    browsers = STAGE / "browsers"
    browsers.mkdir(exist_ok=True)
    pyexe = STAGE / "python" / "python.exe"
    sh([str(pyexe), "-m", "playwright", "install", "chromium"],
       env={"PLAYWRIGHT_BROWSERS_PATH": str(browsers)})
    print("Chromium 装入 stage/browsers 完成")


def write_start_bat() -> None:
    bat = f"""@echo off
cd /d "%~dp0"
set PLAYWRIGHT_BROWSERS_PATH=%~dp0browsers
:loop
"%~dp0python\\python.exe" app.py
echo 服务退出，3 秒后自动重启...
timeout /t 3 /nobreak >nul
goto loop
"""
    (STAGE / "start.bat").write_text(bat, encoding="gbk")


def make_payload_zip() -> None:
    BUILD.mkdir(parents=True, exist_ok=True)
    if PAYLOAD_ZIP.exists():
        PAYLOAD_ZIP.unlink()

    # 优先用 7z（快），找不到 fallback PowerShell Compress-Archive
    if Path(SEVEN_ZIP).exists():
        try:
            cwd_prev = os.getcwd()
            os.chdir(str(STAGE))
            sh([SEVEN_ZIP, "a", "-tzip", "-mx=5", "-bsp0", "-bso0",
                str(PAYLOAD_ZIP), ".\\*"])
            os.chdir(cwd_prev)
        except Exception as e:
            print(f"  7z 失败 ({e})，fallback Compress-Archive")
            _ps_zip()
    else:
        print("7-Zip 未安装，用 PowerShell Compress-Archive")
        _ps_zip()

    mb = PAYLOAD_ZIP.stat().st_size / 1024 / 1024
    print(f"payload.zip: {mb:.1f} MB")


def _ps_zip() -> None:
    """PowerShell Compress-Archive fallback。"""
    import subprocess as sp
    cmd = [
        "powershell", "-NoProfile", "-Command",
        f"Compress-Archive -Path '{STAGE}\\*' -DestinationPath '{PAYLOAD_ZIP}' -CompressionLevel Optimal",
    ]
    sp.run(cmd, check=True)


LAUNCHER_CS = r'''
using System;
using System.Diagnostics;
using System.IO;
using System.IO.Compression;
using System.Reflection;
using System.Windows.Forms;

class Launcher : Form
{
    Label lbl;
    ProgressBar pb;
    readonly string installDir = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
        "nzdh-workbench");
    const string URL = "http://127.0.0.1:8091/womenswear";

    [STAThread]
    static void Main()
    {
        Application.EnableVisualStyles();
        Application.Run(new Launcher());
    }

    public Launcher()
    {
        Text = "动作迁移工作台";
        Width = 440; Height = 150;
        FormBorderStyle = FormBorderStyle.FixedDialog;
        StartPosition = FormStartPosition.CenterScreen;
        MaximizeBox = false; MinimizeBox = false;
        lbl = new Label { Left = 14, Top = 14, Width = 400, Height = 22,
                          Text = "正在准备运行环境，请稍候..." };
        pb = new ProgressBar { Left = 14, Top = 44, Width = 400, Height = 24,
                               Style = ProgressBarStyle.Marquee };
        Controls.Add(lbl); Controls.Add(pb);
        Shown += (s, e) => System.Threading.Tasks.Task.Run((Action)Boot);
    }

    void Boot()
    {
        try
        {
            string marker = Path.Combine(installDir, "payload-v1.done");
            if (!File.Exists(marker))
            {
                SetText("首次运行，正在解压运行环境（约 1-2 分钟）...");
                Directory.CreateDirectory(installDir);
                var asm = Assembly.GetExecutingAssembly();
                using (var st = asm.GetManifestResourceStream("payload.zip"))
                {
                    if (st == null) throw new Exception("payload.zip 资源缺失");
                    using (var za = new ZipArchive(st))
                        za.ExtractToDirectory(installDir);
                }
                File.WriteAllText(marker, DateTime.Now.ToString());
            }
            SetText("正在启动服务...");
            Process.Start(new ProcessStartInfo
            {
                FileName = Path.Combine(installDir, "start.bat"),
                WorkingDirectory = installDir,
                UseShellExecute = true,
                WindowStyle = ProcessWindowStyle.Minimized
            });
            System.Threading.Thread.Sleep(4000);
            Process.Start(new ProcessStartInfo(URL) { UseShellExecute = true });
            System.Threading.Thread.Sleep(500);
            SafeClose();
        }
        catch (Exception ex)
        {
            MessageBox.Show("启动失败: " + ex.Message, "动作迁移工作台",
                MessageBoxButtons.OK, MessageBoxIcon.Error);
            SafeClose();
        }
    }

    void SetText(string t)
    {
        try { if (IsHandleCreated) BeginInvoke((MethodInvoker)(() => lbl.Text = t)); } catch { }
    }

    void SafeClose()
    {
        try { if (IsHandleCreated) BeginInvoke((MethodInvoker)Close); } catch { }
    }
}
'''


def build_launcher() -> None:
    cs = BUILD / "Launcher.cs"
    cs.write_text(LAUNCHER_CS, encoding="utf-8")
    if EXE_OUT.exists():
        EXE_OUT.unlink()
    sh([CSC, "/nologo", "/target:winexe",
        f"/out:{EXE_OUT}",
        f"/resource:{PAYLOAD_ZIP},payload.zip",
        "/r:System.IO.Compression.dll",
        "/r:System.IO.Compression.FileSystem.dll",
        "/r:System.Windows.Forms.dll",
        "/r:System.Drawing.dll",
        str(cs)])
    mb = EXE_OUT.stat().st_size / 1024 / 1024
    print(f"产出: {EXE_OUT}  ({mb:.1f} MB)")


def main() -> None:
    log = DIST / "exe-build" / "step.log"

    def note(msg: str) -> None:
        print(msg, flush=True)
        with open(log, "a", encoding="utf-8") as f:
            f.write(msg + "\n")

    try:
        note("== build_stage ==")
        build_stage()
        note("== install_python ==")
        install_python()
        note("== install_chromium ==")
        install_chromium()
        note("== write_start_bat ==")
        write_start_bat()
        note("== make_payload_zip ==")
        make_payload_zip()
        note("== build_launcher ==")
        build_launcher()
        note("全部完成。交付物: " + str(EXE_OUT))
    except Exception:
        import traceback
        tb = traceback.format_exc()
        note("构建失败:\n" + tb)
        raise


if __name__ == "__main__":
    main()
