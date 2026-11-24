"""Desktop host entry points and nonblocking locks for supported platforms."""
import os
import stat
import sys
from pathlib import Path


def lock_file(file, *, shared=False):
    if os.name=="nt":
        import msvcrt
        if shared:
            import ctypes
            from ctypes import wintypes

            class Overlapped(ctypes.Structure):
                _fields_ = [("internal", ctypes.c_size_t), ("internal_high", ctypes.c_size_t),
                            ("offset", wintypes.DWORD), ("offset_high", wintypes.DWORD),
                            ("event", wintypes.HANDLE)]

            lock = ctypes.WinDLL("kernel32", use_last_error=True).LockFileEx
            lock.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                             wintypes.DWORD, ctypes.POINTER(Overlapped)]
            lock.restype = wintypes.BOOL
            fail_immediately = 1
            if not lock(msvcrt.get_osfhandle(file.fileno()), fail_immediately, 0, 1, 0,
                        ctypes.byref(Overlapped())):
                raise ctypes.WinError(ctypes.get_last_error())
        else:
            msvcrt.locking(file.fileno(),msvcrt.LK_NBLCK,1)
    else:
        import fcntl
        fcntl.flock(file,(fcntl.LOCK_SH if shared else fcntl.LOCK_EX)|fcntl.LOCK_NB)


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


