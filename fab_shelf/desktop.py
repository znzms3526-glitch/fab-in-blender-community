"""Desktop host entry points and nonblocking locks for supported platforms."""
import os
import stat
import sys
from pathlib import Path


def lock_file(file):
    if os.name=="nt":
        import msvcrt
        msvcrt.locking(file.fileno(),msvcrt.LK_NBLCK,1)
    else:
        import fcntl
        fcntl.flock(file,fcntl.LOCK_EX|fcntl.LOCK_NB)


def host_command(role,*args):
    runtime=Path(__file__).resolve().parent/"runtime"
    if sys.platform=="darwin":
        binary=runtime/"FabInBlenderHost"
        if not binary.is_file():
            raise OSError("请安装 macOS Apple Silicon 版插件 / Install the macOS Apple Silicon package.")
        if not os.access(binary,os.X_OK):
            binary.chmod(binary.stat().st_mode|stat.S_IXUSR)
        return [str(binary),role,str(runtime),*args]
    if os.name!="nt":raise OSError("Unsupported desktop platform")
    return [str(runtime/{"gallery":"Gallery091.exe","auth":"ShelfAuth093.exe"}[role]),*args]


