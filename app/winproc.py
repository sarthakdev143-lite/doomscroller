"""Windows-friendly background process flags, CPU priority, and working-set helpers."""
from __future__ import annotations

import gc
import os
import subprocess
from functools import lru_cache

IS_WINDOWS = os.name == "nt"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
BELOW_NORMAL_PRIORITY_CLASS = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0x00004000)


@lru_cache(maxsize=1)
def _kernel32():
    """Bind Win32 kernel32 signatures once with 64-bit pointer-safe HANDLE types."""
    import ctypes

    k32 = ctypes.windll.kernel32
    k32.GetCurrentProcess.argtypes = []
    k32.GetCurrentProcess.restype = ctypes.c_void_p
    k32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    k32.SetPriorityClass.restype = ctypes.c_int
    k32.K32EmptyWorkingSet.argtypes = [ctypes.c_void_p]
    k32.K32EmptyWorkingSet.restype = ctypes.c_int
    k32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    k32.OpenProcess.restype = ctypes.c_void_p
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    k32.CloseHandle.restype = ctypes.c_int
    return k32


def background_subprocess_kwargs(*, low_priority: bool = True) -> dict[str, int]:
    """Subprocess creation kwargs that hide console popups and yield foreground CPU on Windows."""
    if not IS_WINDOWS:
        return {}
    flags = CREATE_NO_WINDOW
    if low_priority:
        flags |= BELOW_NORMAL_PRIORITY_CLASS
    return {"creationflags": flags}


def lower_current_process_priority() -> bool:
    """Drop the current process to BelowNormal CPU priority while keeping normal disk I/O."""
    if IS_WINDOWS:
        try:
            k32 = _kernel32()
            return bool(k32.SetPriorityClass(k32.GetCurrentProcess(), BELOW_NORMAL_PRIORITY_CLASS))
        except Exception:
            return False
    try:
        os.nice(10)
        return True
    except (AttributeError, OSError):
        return False


def trim_working_set(pid: int | None = None) -> bool:
    """Ask Windows to page out cold initialization/post-job pages from physical RAM."""
    if not IS_WINDOWS:
        return False
    try:
        k32 = _kernel32()
        if pid is None:
            return bool(k32.K32EmptyWorkingSet(k32.GetCurrentProcess()))
        # PROCESS_SET_QUOTA (0x0100) | PROCESS_QUERY_LIMITED_INFORMATION (0x1000)
        handle = k32.OpenProcess(0x1100, 0, int(pid))
        if not handle:
            return False
        try:
            return bool(k32.K32EmptyWorkingSet(handle))
        finally:
            k32.CloseHandle(handle)
    except Exception:
        return False


def reclaim_memory() -> bool:
    """Collect cyclic Python garbage and release cold pages from the Windows working set."""
    gc.collect()
    return trim_working_set()
