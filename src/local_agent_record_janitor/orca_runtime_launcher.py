"""Standalone Windows launcher: enter a kill-on-close job before spawning.

Run with Python -I and explicit environment/cwd. This file intentionally has
no package imports, shell dispatch, profile discovery, or credential reads.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from ctypes import wintypes


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _BasicLimit(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD)]


class _ExtendedLimit(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _BasicLimit), ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


def main(arguments: list[str]) -> int:
    if os.name != "nt" or not arguments or not os.path.isabs(arguments[0]):
        raise ValueError("isolated launcher requires a Windows absolute executable")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    inherited = os.environ.pop("LARJ_JOB_HANDLE", None)
    job = wintypes.HANDLE(int(inherited)) if inherited is not None else kernel.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        limits = _ExtendedLimit()
        limits.BasicLimitInformation.LimitFlags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not kernel.AssignProcessToJobObject(job, kernel.GetCurrentProcess()):
            raise ctypes.WinError(ctypes.get_last_error())
        if inherited is not None:
            # The Janitor parent must be the only handle owner. Its crash
            # closes that handle and kills this launcher plus native children.
            if not kernel.CloseHandle(job):
                raise ctypes.WinError(ctypes.get_last_error())
            job = None
        # Descendants inherit this job at CreateProcess. There is no interval
        # in which an uncontained Codex process can spawn its own children.
        process = subprocess.Popen(arguments, stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
        return process.wait()
    finally:
        # A production parent retains its own handle and confirms zero active
        # job members after termination. A standalone lab invocation has only
        # this handle and is itself killed along with its descendants.
        if job is not None:
            if not kernel.CloseHandle(job):
                raise ctypes.WinError(ctypes.get_last_error())


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except (OSError, ValueError):
        sys.stderr.write("isolated Windows runtime could not establish its process job\n")
        sys.exit(1)
