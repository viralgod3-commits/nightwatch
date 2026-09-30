
from __future__ import annotations

"""Primary Nightwatch Futures launcher."""

import ctypes
import multiprocessing
import os
import sys

# Apply before importing NumPy/Qt, including in Windows spawned workers.
for _name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
    os.environ[_name] = os.environ.get("NIGHTWATCH_NUMERIC_THREADS", "1")


def _set_windows_priority() -> None:
    if sys.platform != "win32" or os.environ.get("NIGHTWATCH_HIGH_PRIORITY") != "1":
        return

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    kernel32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel32.SetPriorityClass.restype = ctypes.c_int
    kernel32.GetPriorityClass.argtypes = [ctypes.c_void_p]
    kernel32.GetPriorityClass.restype = ctypes.c_uint32

    HIGH_PRIORITY_CLASS = 0x00000080

    process = kernel32.GetCurrentProcess()

    if not kernel32.SetPriorityClass(process, HIGH_PRIORITY_CLASS):
        raise ctypes.WinError(ctypes.get_last_error())

    actual = kernel32.GetPriorityClass(process)
    if actual != HIGH_PRIORITY_CLASS:
        raise RuntimeError(
            f"Failed to retain HIGH priority: expected 0x80, got {actual:#x}"
        )


from nightwatch.entrypoint import main


if __name__ == "__main__":
    multiprocessing.freeze_support()
    _set_windows_priority()
    raise SystemExit(main())
