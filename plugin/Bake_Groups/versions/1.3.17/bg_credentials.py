# -*- coding: utf-8 -*-
from __future__ import print_function, division, absolute_import

import base64
import ctypes
from ctypes import wintypes
import io
import json
import os


_SCHEMA_VERSION = 1
_APP_DIR_NAME = "BakeGroups"
_CREDENTIAL_FILE = "github_credentials.dat"
_DESCRIPTION = u"Bake Master GitHub read-only token"
_ENTROPY = b"BakeGroups:GitHubToken:v1"
_CRYPTPROTECT_UI_FORBIDDEN = 0x01


class CredentialError(RuntimeError):
    pass


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def credential_path(path=None):
    if path:
        return os.path.normpath(os.path.abspath(path))
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        local_app_data = os.path.join(os.path.expanduser("~"), "AppData", "Local")
    return os.path.join(local_app_data, _APP_DIR_NAME, _CREDENTIAL_FILE)


def _validated_token(token):
    if token is None:
        raise CredentialError("GitHub token is empty.")
    if not isinstance(token, str):
        token = str(token)
    token = token.strip()
    if len(token) < 20 or len(token) > 512 or any(char.isspace() for char in token):
        raise CredentialError("GitHub token format is invalid.")
    return token


def _blob(data):
    if not isinstance(data, bytes):
        data = bytes(data)
    buffer_value = ctypes.create_string_buffer(data, len(data))
    value = _DataBlob(
        len(data),
        ctypes.cast(buffer_value, ctypes.POINTER(ctypes.c_ubyte)),
    )
    return value, buffer_value


def _windows_crypto():
    if os.name != "nt":
        raise CredentialError("Windows DPAPI is required to store the GitHub token.")
    try:
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        crypt32.CryptProtectData.argtypes = [
            ctypes.POINTER(_DataBlob), wintypes.LPCWSTR, ctypes.POINTER(_DataBlob),
            ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_DataBlob),
        ]
        crypt32.CryptProtectData.restype = wintypes.BOOL
        crypt32.CryptUnprotectData.argtypes = [
            ctypes.POINTER(_DataBlob), ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(_DataBlob),
            ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_DataBlob),
        ]
        crypt32.CryptUnprotectData.restype = wintypes.BOOL
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        return crypt32, kernel32
    except Exception as exc:
        raise CredentialError("Windows DPAPI is unavailable: {}".format(exc))


def _protect(clear_bytes):
    crypt32, kernel32 = _windows_crypto()
    input_blob, input_buffer = _blob(clear_bytes)
    entropy_blob, entropy_buffer = _blob(_ENTROPY)
    output_blob = _DataBlob()
    ok = crypt32.CryptProtectData(
        ctypes.byref(input_blob),
        _DESCRIPTION,
        ctypes.byref(entropy_blob),
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(output_blob),
    )
    if not ok:
        raise CredentialError("Windows could not encrypt the GitHub token: {}".format(ctypes.WinError()))
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        kernel32.LocalFree(output_blob.pbData)


def _unprotect(protected_bytes):
    crypt32, kernel32 = _windows_crypto()
    input_blob, input_buffer = _blob(protected_bytes)
    entropy_blob, entropy_buffer = _blob(_ENTROPY)
    output_blob = _DataBlob()
    description = wintypes.LPWSTR()
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(input_blob),
        ctypes.byref(description),
        ctypes.byref(entropy_blob),
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(output_blob),
    )
    if not ok:
        raise CredentialError("Windows could not decrypt the stored GitHub token.")
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        if description:
            kernel32.LocalFree(description)
        kernel32.LocalFree(output_blob.pbData)


def save_token(token, path=None):
    token = _validated_token(token)
    protected = _protect(token.encode("utf-8"))
    payload = {
        "schema_version": _SCHEMA_VERSION,
        "provider": "github",
        "protection": "windows_dpapi_current_user",
        "payload": base64.b64encode(protected).decode("ascii"),
    }
    target = credential_path(path)
    parent = os.path.dirname(target)
    if not os.path.isdir(parent):
        os.makedirs(parent)
    temporary = target + ".tmp.{}".format(os.getpid())
    try:
        with io.open(temporary, "w", encoding="utf-8", newline="") as handle:
            json.dump(payload, handle, ensure_ascii=True, indent=2, sort_keys=True)
            handle.write(u"\n")
        os.replace(temporary, target)
    finally:
        try:
            if os.path.isfile(temporary):
                os.remove(temporary)
        except Exception:
            pass
    return target


def load_token(path=None):
    target = credential_path(path)
    if not os.path.isfile(target):
        return None
    try:
        with io.open(target, "r", encoding="utf-8-sig") as handle:
            payload = json.load(handle)
        if payload.get("schema_version") != _SCHEMA_VERSION:
            raise CredentialError("Stored GitHub credential has an unsupported format.")
        protected = base64.b64decode(payload.get("payload", "").encode("ascii"), validate=True)
        token = _unprotect(protected).decode("utf-8")
        return _validated_token(token)
    except CredentialError:
        raise
    except Exception as exc:
        raise CredentialError("Stored GitHub credential is invalid: {}".format(exc))


def has_token(path=None):
    try:
        return bool(load_token(path))
    except CredentialError:
        return False


def clear_token(path=None):
    target = credential_path(path)
    if not os.path.isfile(target):
        return False
    try:
        os.remove(target)
    except Exception as exc:
        raise CredentialError("Could not remove the stored GitHub credential: {}".format(exc))
    return True
