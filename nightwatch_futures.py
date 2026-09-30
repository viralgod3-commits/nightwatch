
from __future__ import annotations

"""Primary Nightwatch Futures launcher."""

import ctypes
import sys


def _set_windows_priority() -> None:
    if sys.platform != "win32":
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


_set_windows_priority()

from nightwatch.entrypoint import main


if __name__ == "__main__":
    raise SystemExit(main())