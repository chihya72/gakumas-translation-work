"""Keep each foreground pipeline process tree in its own Windows job."""
from __future__ import annotations

import ctypes
import os
import signal
import subprocess
from pathlib import Path


class BasicLimits(ctypes.Structure):
    _fields_ = [("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
                ("flags", ctypes.c_ulong), ("minimum_working_set", ctypes.c_size_t),
                ("maximum_working_set", ctypes.c_size_t), ("active_processes", ctypes.c_ulong),
                ("affinity", ctypes.c_size_t), ("priority", ctypes.c_ulong), ("scheduling", ctypes.c_ulong)]


class ExtendedLimits(ctypes.Structure):
    _fields_ = [("basic", BasicLimits), ("io_counters", ctypes.c_ulonglong * 6),
                ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t)]


kernel = ctypes.WinDLL("kernel32", use_last_error=True)
kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
kernel.CreateJobObjectW.restype = ctypes.c_void_p
kernel.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong]
kernel.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
kernel.CloseHandle.argtypes = [ctypes.c_void_p]
kernel.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint]


def close_job(process):
    handle = getattr(process, "_home_voice_job", None)
    if handle:
        if not kernel.CloseHandle(handle):
            raise ctypes.WinError(ctypes.get_last_error())
        process._home_voice_job = None


def start_child(arguments: list[str], cwd: Path):
    handle = kernel.CreateJobObjectW(None, None)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    limits = ExtendedLimits()
    limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
        error = ctypes.WinError(ctypes.get_last_error())
        kernel.CloseHandle(handle)
        raise error
    environment = os.environ.copy()
    environment["PYTHONUTF8"] = "1"
    try:
        process = subprocess.Popen(arguments, cwd=str(cwd), env=environment,
                                   creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    except BaseException:
        kernel.CloseHandle(handle)
        raise
    process._home_voice_job = handle
    if not kernel.AssignProcessToJobObject(handle, int(process._handle)):
        error = ctypes.WinError(ctypes.get_last_error())
        process.terminate()
        process.wait()
        close_job(process)
        raise error
    return process


def stop_child(process) -> None:
    try:
        if process.poll() is None:
            try:
                process.send_signal(signal.CTRL_BREAK_EVENT)
                process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                handle = getattr(process, "_home_voice_job", None)
                if handle and not kernel.TerminateJobObject(handle, 1):
                    raise ctypes.WinError(ctypes.get_last_error())
    finally:
        # Close even after the launcher has exited, to end any surviving child.
        close_job(process)
    process.wait(timeout=10)


def run_child(arguments: list[str], cwd: Path, asr=False) -> None:
    if asr:
        print("启动 OpenMOSS 识别子进程；Ctrl+C 可中断，已完成批次保留。", flush=True)
    process = start_child(arguments, cwd)
    try:
        returncode = process.wait()
    finally:
        stop_child(process)
        if asr:
            print("OpenMOSS 识别子进程已结束，模型资源随进程释放。", flush=True)
    if returncode:
        raise subprocess.CalledProcessError(returncode, arguments)
