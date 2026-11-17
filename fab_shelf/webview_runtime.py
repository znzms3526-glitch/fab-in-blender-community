"""Windows Evergreen setup. Called only for the Windows gallery."""
import hashlib
import os
from pathlib import Path
import subprocess
import time


BOOTSTRAPPER = Path(__file__).parent / "runtime" / "MicrosoftEdgeWebview2Setup.exe"
BOOTSTRAPPER_SHA256 = "cf2f2445fef0c5c14e372045f84998771eeb8bb01d59be4afd00692fa160082e"
CLIENT_KEY = r"SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"


def installed():
    import winreg
    for hive, view in ((winreg.HKEY_CURRENT_USER, 0), (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY)):
        try:
            with winreg.OpenKey(hive, CLIENT_KEY, 0, winreg.KEY_READ | view) as key:
                version = winreg.QueryValueEx(key, "pv")[0]
            parts = str(version).split(".")
            if len(parts) == 4 and all(s.isdecimal() for s in parts) and any(int(s) for s in parts):
                return True
        except FileNotFoundError:
            continue
    return False


def install_if_missing():
    import msvcrt
    if installed():
        return
    directory = Path(os.environ["LOCALAPPDATA"]) / "FabShelf"
    directory.mkdir(parents=True, exist_ok=True)
    # Separate Blender processes can open the gallery at the same time.
    with (directory / "webview-setup.lock").open("a+b") as lock:
        lock.seek(0)
        deadline = time.monotonic() + 900
        while True:
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError as e:
                if e.errno != 13 or time.monotonic() >= deadline:
                    raise OSError("无法等待其他 WebView2 安装任务，请稍后重试") from e
                time.sleep(.5)
                if installed():
                    return
        if installed():
            return
        if not BOOTSTRAPPER.is_file() or hashlib.sha256(BOOTSTRAPPER.read_bytes()).hexdigest() != BOOTSTRAPPER_SHA256:
            raise ValueError("WebView2 安装文件缺失或损坏，请重新安装插件")
        try:
            result = subprocess.run([str(BOOTSTRAPPER), "/silent", "/install"],
                                    creationflags=subprocess.CREATE_NO_WINDOW, timeout=900)
        except subprocess.TimeoutExpired as e:
            raise OSError("WebView2 安装超时，请检查网络后重试") from e
        if not installed():
            raise OSError(f"WebView2 安装未完成（0x{result.returncode & 0xffffffff:08X}），请检查网络或系统策略")
