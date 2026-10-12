#!/usr/bin/env python3
"""Linux capability helpers for the Executor's root-to-runner UID drop."""
from __future__ import annotations

import ctypes
from typing import Any

_LINUX_CAPABILITY_VERSION_3 = 0x20080522
_CAPABILITY_WORDS = 2
_PR_SET_KEEPCAPS = 8
_PR_CAP_AMBIENT = 47
_PR_CAP_AMBIENT_CLEAR_ALL = 4


class _CapHeader(ctypes.Structure):
    _fields_ = [("version", ctypes.c_uint32), ("pid", ctypes.c_int)]


class _CapData(ctypes.Structure):
    _fields_ = [
        ("effective", ctypes.c_uint32),
        ("permitted", ctypes.c_uint32),
        ("inheritable", ctypes.c_uint32),
    ]


def _capget_data() -> tuple[_CapHeader, Any]:
    libc = ctypes.CDLL(None, use_errno=True)
    header = _CapHeader(_LINUX_CAPABILITY_VERSION_3, 0)
    data = (_CapData * _CAPABILITY_WORDS)()
    if libc.capget(ctypes.byref(header), ctypes.byref(data)) != 0:
        raise OSError(ctypes.get_errno(), "capget failed")
    return header, data


def _capset_data(header: _CapHeader, data: Any) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.capset(ctypes.byref(header), ctypes.byref(data)) != 0:
        raise OSError(ctypes.get_errno(), "capset failed")


def clear_capabilities_for_identity_drop() -> None:
    """Clear inheritable/ambient state before the launcher drops uid/gid."""
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_PR_CAP_AMBIENT, _PR_CAP_AMBIENT_CLEAR_ALL, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "PR_CAP_AMBIENT_CLEAR_ALL failed")

    header, data = _capget_data()
    for entry in data:
        entry.inheritable = 0
    _capset_data(header, data)

    if libc.prctl(_PR_SET_KEEPCAPS, 0, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "PR_SET_KEEPCAPS failed")
