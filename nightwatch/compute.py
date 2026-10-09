"""CPU placement for spawned workers, independent of Qt and trading transport."""
from __future__ import annotations

import ctypes
from dataclasses import dataclass
from functools import lru_cache
import os
from pathlib import Path


@dataclass(frozen=True)
class CpuBudget:
    allowed: tuple[int, ...]
    cores: tuple[tuple[int, ...], ...]
    capacity: int

    @property
    def analysis_workers(self) -> int:
        # GUI, live order-flow analysis and DOM rendering each need CPU time.
        return max(1, min(6, self.capacity - 3))

    @property
    def worker_cpus(self) -> tuple[int, ...]:
        if self.capacity < 4 or not self.cores:
            return self.allowed
        reserved = frozenset(self.cores[0])
        return tuple(cpu for cpu in self.allowed if cpu not in reserved)


def _windows_cpu_layout():
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.argtypes = []
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    handle = kernel.GetCurrentProcess()
    kernel.GetProcessAffinityMask.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(ctypes.c_size_t), ctypes.POINTER(ctypes.c_size_t),
    ]
    kernel.GetProcessAffinityMask.restype = wintypes.BOOL
    allowed_mask, system_mask = ctypes.c_size_t(), ctypes.c_size_t()
    if not kernel.GetProcessAffinityMask(handle, ctypes.byref(allowed_mask), ctypes.byref(system_mask)):
        return None
    if not allowed_mask.value:
        return None
    allowed = tuple(cpu for cpu in range(ctypes.sizeof(ctypes.c_size_t) * 8)
                    if allowed_mask.value & (1 << cpu))

    class InfoData(ctypes.Union):
        _fields_ = [("reserved", ctypes.c_ulonglong * 2)]

    class ProcessorInfo(ctypes.Structure):
        _fields_ = [("mask", ctypes.c_size_t), ("relationship", wintypes.DWORD),
                    ("data", InfoData)]

    query = kernel.GetLogicalProcessorInformation
    query.argtypes = [ctypes.POINTER(ProcessorInfo), ctypes.POINTER(wintypes.DWORD)]
    query.restype = wintypes.BOOL
    size = wintypes.DWORD()
    query(None, ctypes.byref(size))
    if not size.value:
        return allowed, ()
    entries = (ProcessorInfo * (size.value // ctypes.sizeof(ProcessorInfo)))()
    if not query(entries, ctypes.byref(size)):
        return allowed, ()
    cores = tuple(tuple(cpu for cpu in allowed if info.mask & (1 << cpu))
                  for info in entries if info.relationship == 0)
    return allowed, tuple(core for core in cores if core)


def _linux_core_layout(allowed):
    cores = {}
    try:
        for cpu in allowed:
            topology = Path(f"/sys/devices/system/cpu/cpu{cpu}/topology")
            key = (int((topology / "physical_package_id").read_text()),
                   int((topology / "core_id").read_text()))
            if min(key) < 0:
                return ()
            cores.setdefault(key, []).append(cpu)
    except (OSError, ValueError):
        return ()
    return tuple(tuple(cpus) for cpus in cores.values())


def _linux_cpu_quota():
    root = Path("/sys/fs/cgroup")
    roots = (root, root / "cpu", root / "cpu,cpuacct")
    paths = set(roots)
    try:
        groups = Path("/proc/self/cgroup").read_text().splitlines()
    except OSError:
        groups = []
    for group in groups:
        fields = group.split(":", 2)
        if len(fields) != 3:
            continue
        relative = Path(fields[2].lstrip("/"))
        if ".." in relative.parts:
            continue
        controllers = fields[1].split(",")
        mounts = (root,) if not fields[1] else roots[1:] if "cpu" in controllers else ()
        for mount in mounts:
            current = mount / relative
            while current.is_relative_to(mount):
                paths.add(current)
                if current == mount:
                    break
                current = current.parent
    limits = []
    for path in paths:
        try:
            quota, period = (path / "cpu.max").read_text().split()
            if quota != "max" and int(quota) > 0 and int(period) > 0:
                limits.append(max(1, int(quota) // int(period)))
        except (OSError, ValueError):
            pass
        try:
            quota = int((path / "cpu.cfs_quota_us").read_text())
            period = int((path / "cpu.cfs_period_us").read_text())
            if quota > 0 and period > 0:
                limits.append(max(1, quota // period))
        except (OSError, ValueError):
            pass
    return min(limits) if limits else None


@lru_cache(maxsize=1)
def cpu_budget() -> CpuBudget:
    reported = getattr(os, "process_cpu_count", os.cpu_count)() or 1
    allowed = tuple(range(reported))
    cores = ()
    if os.name == "nt":
        try:
            layout = _windows_cpu_layout()
            if layout is not None:
                allowed, cores = layout
        except (AttributeError, OSError, ValueError):
            pass
    elif hasattr(os, "sched_getaffinity"):
        try:
            allowed = tuple(sorted(os.sched_getaffinity(0))) or allowed
        except OSError:
            pass
        cores = _linux_core_layout(allowed)
    capacity = min(reported, len(allowed), len(cores) if cores else len(allowed))
    if os.name == "posix":
        quota = _linux_cpu_quota()
        if quota is not None:
            capacity = min(capacity, quota)
    return CpuBudget(allowed, cores, max(1, capacity))


def configure_worker(*, analysis: bool = False) -> None:
    """Reserve GUI scheduling capacity without changing the GUI process itself.

    Call only inside a freshly spawned child. Live models/renderers keep normal
    priority; analytical workers yield CPU time to them. Restricted platforms
    retain their inherited placement if affinity or priority cannot be changed.
    """
    budget = cpu_budget()
    cpus = budget.worker_cpus
    if os.name == "nt":
        from ctypes import wintypes

        try:
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.GetCurrentProcess.argtypes = []
            kernel.GetCurrentProcess.restype = wintypes.HANDLE
            handle = kernel.GetCurrentProcess()
            if cpus != budget.allowed:
                kernel.SetProcessAffinityMask.argtypes = [wintypes.HANDLE, ctypes.c_size_t]
                kernel.SetProcessAffinityMask.restype = wintypes.BOOL
                kernel.SetProcessAffinityMask(handle, sum(1 << cpu for cpu in cpus))
            if analysis:
                kernel.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
                kernel.SetPriorityClass.restype = wintypes.BOOL
                kernel.SetPriorityClass(handle, 0x00004000)  # BELOW_NORMAL_PRIORITY_CLASS
        except (AttributeError, OSError, ValueError):
            pass
    else:
        if cpus != budget.allowed and hasattr(os, "sched_setaffinity"):
            try:
                os.sched_setaffinity(0, cpus)
            except OSError:
                pass
            # Linux affinity is per thread; native pools may predate the initializer.
            try:
                tasks = tuple(Path("/proc/self/task").iterdir())
            except OSError:
                tasks = ()
            for task in tasks:
                try:
                    os.sched_setaffinity(int(task.name), cpus)
                except (OSError, ValueError):
                    pass
        if analysis and hasattr(os, "nice"):
            try:
                os.nice(5)
            except OSError:
                pass
