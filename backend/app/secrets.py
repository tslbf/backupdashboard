"""Secret indirection for settings values.

Any secret-bearing setting accepts three forms:

  plaintext          used as-is (fine for lab/testing)
  file:<path>        read the secret from a file (protect it with NTFS ACLs)
  dpapi:<base64>     Windows DPAPI ciphertext — decryptable only by the same
                     user on the same machine (or any user on the machine if
                     created with --machine). Generate with:
                         python -m app.cli protect

DPAPI is the recommended form on Windows: no key file to guard, and the
token is useless if .env leaks off the box.
"""
from __future__ import annotations

import base64
import sys
from pathlib import Path

DPAPI_PREFIX = "dpapi:"
FILE_PREFIX = "file:"

# CryptProtectData flags
_CRYPTPROTECT_UI_FORBIDDEN = 0x1
_CRYPTPROTECT_LOCAL_MACHINE = 0x4


def _dpapi_call(data: bytes, encrypt: bool, machine_scope: bool = False) -> bytes:
    if sys.platform != "win32":
        raise RuntimeError(
            "dpapi: secrets can only be used on Windows (this value was protected with Windows DPAPI)"
        )
    import ctypes
    import ctypes.wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [
            ("cbData", ctypes.wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_char)),
        ]

    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()
    flags = _CRYPTPROTECT_UI_FORBIDDEN | (_CRYPTPROTECT_LOCAL_MACHINE if machine_scope else 0)

    if encrypt:
        ok = ctypes.windll.crypt32.CryptProtectData(
            ctypes.byref(blob_in), None, None, None, None, flags, ctypes.byref(blob_out)
        )
    else:
        ok = ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, None, None, None, flags, ctypes.byref(blob_out)
        )
    if not ok:
        action = "protect" if encrypt else "unprotect"
        raise OSError(
            f"DPAPI could not {action} the secret — it was likely protected by a different "
            "user or on a different machine. Re-run 'python -m app.cli protect' as the "
            "account that runs the app."
        )
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def protect(secret: str, machine_scope: bool = False) -> str:
    """Encrypt a secret and return the dpapi:<base64> token for .env."""
    token = _dpapi_call(secret.encode("utf-8"), encrypt=True, machine_scope=machine_scope)
    return DPAPI_PREFIX + base64.b64encode(token).decode("ascii")


def resolve_secret(value: str) -> str:
    """Turn a settings value into the actual secret."""
    if not value:
        return value
    if value.startswith(DPAPI_PREFIX):
        raw = base64.b64decode(value[len(DPAPI_PREFIX):])
        return _dpapi_call(raw, encrypt=False).decode("utf-8")
    if value.startswith(FILE_PREFIX):
        return Path(value[len(FILE_PREFIX):]).read_text(encoding="utf-8-sig").strip()
    return value
