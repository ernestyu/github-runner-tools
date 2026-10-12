#!/usr/bin/env python3
"""Strict, read-only fresh installation attempt reader (no repair)."""
import ipaddress
import json
import os
import re
import stat
import sys
import uuid

KEYS = ("schema", "release", "payload_sha256", "attempt_id",
        "runner_user", "runner_uid", "runner_gid", "runner_home",
        "web_bind_address", "stage")
STAGES = ("ATTEMPT_STARTED", "RUNTIME_INSTALLED",
          "AUTHORITY_STATE_INITIALIZED", "ARCHIVE_PLATFORM_INITIALIZED",
          "EXECUTOR_INSTALLED", "WEB_INSTALLED", "SERVICES_STARTED",
          "SMOKE_VERIFIED", "INSTALL_COMPLETE")

def reject():
    raise ValueError("invalid_install_state")

def unique(pairs):
    d = {}
    for k, v in pairs:
        if k in d:
            reject()
        d[k] = v
    return d

def read_state(directory, name):
    # Check both directory and file using file descriptors; no path symlink.
    parent = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        st = os.fstat(parent)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != 0 or st.st_gid != 0 or stat.S_IMODE(st.st_mode) != 0o700:
            reject()
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
        try:
            st = os.fstat(fd)
            if (not stat.S_ISREG(st.st_mode) or st.st_uid != 0 or st.st_gid != 0
                    or stat.S_IMODE(st.st_mode) != 0o600 or st.st_nlink != 1
                    or st.st_size > 4096):
                reject()
            raw = os.read(fd, 4097)
            if len(raw) > 4096:
                reject()
        finally:
            os.close(fd)
    finally:
        os.close(parent)
    obj = json.loads(raw.decode("utf-8", "strict"), object_pairs_hook=unique,
                     parse_constant=lambda x: reject())
    if type(obj) is not dict or set(obj) != set(KEYS):
        reject()
    if type(obj["schema"]) is not int or obj["schema"] != 1:
        reject()
    if type(obj["release"]) is not str or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", obj["release"]):
        reject()
    if type(obj["payload_sha256"]) is not str or not re.fullmatch(r"[0-9a-f]{64}", obj["payload_sha256"]):
        reject()
    if type(obj["attempt_id"]) is not str:
        reject()
    try:
        if str(uuid.UUID(obj["attempt_id"])) != obj["attempt_id"] or uuid.UUID(obj["attempt_id"]).version != 4:
            reject()
    except (ValueError, AttributeError):
        reject()
    if type(obj["runner_user"]) is not str or not re.fullmatch(r"[a-z_][a-z0-9_-]*[$]?", obj["runner_user"]):
        reject()
    for k in ("runner_uid", "runner_gid"):
        if type(obj[k]) is not int or not 0 < obj[k] <= 2**32 - 1:
            reject()
    home = obj["runner_home"]
    if type(home) is not str or not re.fullmatch(r"/[A-Za-z0-9._/-]+", home) or ".." in home.split("/"):
        reject()
    if type(obj["web_bind_address"]) is not str:
        reject()
    try:
        addr = ipaddress.IPv4Address(obj["web_bind_address"])
        if not addr.is_private or not (addr in ipaddress.ip_network("10.0.0.0/8")
           or addr in ipaddress.ip_network("172.16.0.0/12")
           or addr in ipaddress.ip_network("192.168.0.0/16")):
            reject()
    except ipaddress.AddressValueError:
        reject()
    if type(obj["stage"]) is not str or obj["stage"] not in STAGES:
        reject()
    return obj

def main():
    try:
        if len(sys.argv) != 3:
            reject()
        obj = read_state(sys.argv[1], sys.argv[2])
        # Values are validated to be single-line ASCII; fixed order for Bash.
        for key in KEYS:
            print(str(obj[key]))
    except (OSError, ValueError, UnicodeError, TypeError, json.JSONDecodeError):
        print("invalid_install_state", file=sys.stderr)
        return 1
    return 0

if __name__ == "__main__":
    sys.exit(main())
