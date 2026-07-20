"""Machine-bound license verification for Bake Master.

The runtime only contains the Ed25519 public key. License issuance and key
management belong in tools/license_admin.py and must never ship with a private
key. The local license envelope is protected with Windows DPAPI.
"""
from __future__ import absolute_import, division, print_function

import base64
import binascii
import hashlib
import hmac
import io
import json
import os
import platform
import time
import uuid

try:
    import urllib.request as _urlrequest
    import urllib.error as _urlerror
except ImportError:  # pragma: no cover
    import urllib2 as _urlrequest
    import urllib2 as _urlerror

import bg_credentials


LICENSE_SCHEMA = 1
STORAGE_SCHEMA = 2
DEFAULT_OFFLINE_DAYS = 14
DEFAULT_TIMEOUT_SECONDS = 5
CLOCK_SKEW_SECONDS = 300
LICENSE_FILE = "bake-master-license.dat"

# Replaced by the release builder when a production key is generated. This
# development key is intentionally invalid until the admin tool creates one.
PUBLIC_KEY_B64 = "OGJmrHn2lTdC+7gFuZiaRz0nq406dBnT+s20gYMF6d0="
STATUS_URL = ""
PRODUCT_ID = "bake-master"
PRODUCT = "Bake Master"
_SESSION = None

_Q = 2 ** 255 - 19
_L = 2 ** 252 + 27742317777372353535851937790883648493
_D = (-121665 * pow(121666, _Q - 2, _Q)) % _Q
_I = pow(2, (_Q - 1) // 4, _Q)
_BY = (4 * pow(5, _Q - 2, _Q)) % _Q


class LicenseError(RuntimeError):
    pass


class LicenseNetworkError(LicenseError):
    pass


class LicenseRuntimeError(LicenseError):
    """The signed license is valid, but the protected runtime cannot start."""

    pass


def _inv(value):
    return pow(value, _Q - 2, _Q)


def _xrecover(y):
    xx = (y * y - 1) * _inv(_D * y * y + 1) % _Q
    x = pow(xx, (_Q + 3) // 8, _Q)
    if (x * x - xx) % _Q != 0:
        x = (x * _I) % _Q
    if (x * x - xx) % _Q != 0:
        raise ValueError("Invalid Ed25519 point")
    if x & 1:
        x = _Q - x
    return x


_B = (_xrecover(_BY), _BY)


def _edwards_add(point_a, point_b):
    x1, y1 = point_a
    x2, y2 = point_b
    denominator_x = _inv(1 + _D * x1 * x2 * y1 * y2)
    denominator_y = _inv(1 - _D * x1 * x2 * y1 * y2)
    return (
        (x1 * y2 + x2 * y1) * denominator_x % _Q,
        (y1 * y2 + x1 * x2) * denominator_y % _Q,
    )


def _scalarmult(point, scalar):
    result = (0, 1)
    addend = point
    while scalar:
        if scalar & 1:
            result = _edwards_add(result, addend)
        addend = _edwards_add(addend, addend)
        scalar >>= 1
    return result


def _decode_point(encoded):
    if len(encoded) != 32:
        raise ValueError("Invalid Ed25519 point length")
    value = int.from_bytes(encoded, "little")
    sign = value >> 255
    y = value & ((1 << 255) - 1)
    if y >= _Q:
        raise ValueError("Invalid Ed25519 point")
    x = _xrecover(y)
    if x == 0 and sign:
        raise ValueError("Non-canonical Ed25519 point")
    if (x & 1) != sign:
        x = _Q - x
    point = (x, y)
    if _scalarmult(point, 8) == (0, 1):
        raise ValueError("Small-order Ed25519 point")
    return point


def ed25519_verify(public_key, signature, message):
    """Verify an Ed25519 signature without third-party runtime packages."""
    if not isinstance(public_key, bytes) or len(public_key) != 32:
        return False
    if not isinstance(signature, bytes) or len(signature) != 64:
        return False
    if not isinstance(message, bytes):
        return False
    try:
        point_a = _decode_point(public_key)
        point_r = _decode_point(signature[:32])
        scalar_s = int.from_bytes(signature[32:], "little")
        if scalar_s >= _L:
            return False
        challenge = int.from_bytes(
            hashlib.sha512(signature[:32] + public_key + message).digest(),
            "little",
        ) % _L
        return _scalarmult(_B, scalar_s) == _edwards_add(
            point_r, _scalarmult(point_a, challenge)
        )
    except (TypeError, ValueError, OverflowError):
        return False


def _canonical_json(value):
    return (json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _b64decode(value, label):
    try:
        return base64.b64decode(str(value).encode("ascii"), validate=True)
    except (TypeError, ValueError, binascii.Error) as exc:
        raise LicenseError("Invalid {}: {}".format(label, exc))


def _config():
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "license_config.json")
    if not os.path.isfile(path):
        return {}
    try:
        with io.open(path, "r", encoding="utf-8-sig") as stream:
            value = json.load(stream)
        return value if isinstance(value, dict) else {}
    except Exception as exc:
        raise LicenseError("License configuration is invalid: {}".format(exc))


def license_path(path=None):
    if path:
        return os.path.abspath(path)
    local_app_data = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
    return os.path.join(local_app_data, "BakeGroups", LICENSE_FILE)


def _machine_material():
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography")
        value = str(winreg.QueryValueEx(key, "MachineGuid")[0]).strip().lower()
        winreg.CloseKey(key)
    except Exception as exc:
        raise LicenseError("Cannot read this computer's Windows identity: {}".format(exc))
    if not value:
        raise LicenseError("Cannot determine this computer's identity")
    return value


def machine_hash():
    try:
        import bg_math_core
        native_machine_hash = getattr(bg_math_core, "machine_hash", None)
        if native_machine_hash is not None:
            value = str(native_machine_hash()).strip().lower()
            if len(value) == 64:
                return value
    except ImportError:
        pass
    material = "BakeGroups:Machine:v2:" + _machine_material()
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def machine_code():
    return machine_hash().upper()


def _license_message(license_data):
    signed = dict(license_data)
    signed.pop("signature", None)
    return _canonical_json(signed)


def _public_key():
    configured = _config().get("public_key_b64") or PUBLIC_KEY_B64
    key = _b64decode(configured, "public key")
    if len(key) != 32:
        raise LicenseError("License public key must be 32 bytes")
    return key


def verify_license_payload(license_data):
    if not isinstance(license_data, dict) or license_data.get("schema_version") != LICENSE_SCHEMA:
        raise LicenseError("License format is unsupported")
    payload_product_id = license_data.get("product_id") or str(
        license_data.get("product", "")
    ).strip().lower().replace(" ", "-")
    if payload_product_id != PRODUCT_ID:
        raise LicenseError("License belongs to another product")
    if not license_data.get("license_id") or not license_data.get("machine_hash"):
        raise LicenseError("License is missing its identity fields")
    native_proof = str(license_data.get("native_proof", "")).strip().lower()
    if len(native_proof) != 64 or any(char not in "0123456789abcdef" for char in native_proof):
        raise LicenseError("License does not contain a valid native authorization proof")
    if not hmac.compare_digest(str(license_data["machine_hash"]).lower(), machine_hash()):
        raise LicenseError("This license is bound to another computer")
    signature = _b64decode(license_data.get("signature", ""), "license signature")
    if not ed25519_verify(_public_key(), signature, _license_message(license_data)):
        raise LicenseError("License signature is invalid")
    expires_at = license_data.get("expires_at")
    if expires_at and int(expires_at) < int(time.time()):
        raise LicenseError("License has expired")
    return True


def _atomic_write_json(path, value):
    parent = os.path.dirname(os.path.abspath(path))
    if not os.path.isdir(parent):
        os.makedirs(parent)
    payload = (
        json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    temporary = os.path.join(
        parent,
        ".{}.tmp-{}".format(os.path.basename(path), uuid.uuid4().hex),
    )
    descriptor = None
    try:
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            if os.path.exists(temporary):
                os.remove(temporary)
        except OSError:
            pass


def _license_hash(license_data):
    return hashlib.sha256(_canonical_json(license_data)).hexdigest()


def _bounded_offline_days(license_data, config=None):
    config = config if isinstance(config, dict) else _config()
    limits = [DEFAULT_OFFLINE_DAYS]
    configured = config.get("offline_days", DEFAULT_OFFLINE_DAYS)
    payload_value = license_data.get("offline_days")
    for label, value in (("configured", configured), ("licensed", payload_value)):
        if value is None:
            continue
        try:
            parsed = int(value)
        except (TypeError, ValueError, OverflowError):
            raise LicenseError("The {} offline grace period is invalid".format(label))
        limits.append(max(0, parsed))
    return min(limits)


def _write_license_record(path, license_data, last_online_at):
    try:
        timestamp = int(last_online_at or 0)
    except (TypeError, ValueError, OverflowError):
        raise LicenseError("Stored license state has an invalid online timestamp")
    if timestamp < 0:
        raise LicenseError("Stored license state has an invalid online timestamp")
    digest = _license_hash(license_data)
    record = {
        "storage_schema_version": STORAGE_SCHEMA,
        "license": license_data,
        "state": {
            "last_online_at": timestamp,
            "license_sha256": digest,
        },
    }
    protected = bg_credentials._protect(_canonical_json(record))
    envelope = {
        "schema_version": STORAGE_SCHEMA,
        "protection": "windows_dpapi_current_user",
        "payload": base64.b64encode(protected).decode("ascii"),
    }
    _atomic_write_json(path, envelope)


def _legacy_last_online(state, offline_days, now=None):
    now = int(time.time()) if now is None else int(now)
    try:
        timestamp = int(state.get("last_online_at", 0) or 0)
        file_mtime = int(state.get("legacy_file_mtime", 0) or 0)
    except (TypeError, ValueError, OverflowError):
        return 0
    if timestamp <= 0:
        return 0
    if timestamp > now + CLOCK_SKEW_SECONDS:
        return 0
    if file_mtime > 0 and timestamp > file_mtime + CLOCK_SKEW_SECONDS:
        return 0
    if now - timestamp > (max(0, int(offline_days)) * 86400 + CLOCK_SKEW_SECONDS):
        return 0
    return min(timestamp, now)


def save_license(license_data, path=None):
    verify_license_payload(license_data)
    target = license_path(path)
    last_online_at = 0
    if os.path.isfile(target):
        try:
            stored_license, state = load_license(target)
            same_license = hmac.compare_digest(
                _license_hash(stored_license), _license_hash(license_data)
            )
            if same_license:
                if state.get("legacy"):
                    offline_days = _bounded_offline_days(license_data)
                    last_online_at = _legacy_last_online(state, offline_days)
                else:
                    last_online_at = int(state.get("last_online_at", 0) or 0)
        except Exception:
            # A valid pasted license must still be able to replace a damaged file.
            last_online_at = 0
    _write_license_record(target, license_data, last_online_at)
    return target


def load_license(path=None):
    target = license_path(path)
    if not os.path.isfile(target):
        return None, None
    try:
        with io.open(target, "r", encoding="utf-8-sig") as stream:
            envelope = json.load(stream)
        if not isinstance(envelope, dict):
            raise LicenseError("Stored license envelope is invalid")
        protected = _b64decode(envelope.get("payload", ""), "license payload")
        clear_value = json.loads(bg_credentials._unprotect(protected).decode("utf-8"))
        if not isinstance(clear_value, dict):
            raise LicenseError("Stored license payload is invalid")

        if clear_value.get("storage_schema_version") == STORAGE_SCHEMA:
            if envelope.get("schema_version") != STORAGE_SCHEMA:
                raise LicenseError("Stored license envelope is invalid")
            license_data = clear_value.get("license")
            state = clear_value.get("state")
            if not isinstance(license_data, dict) or not isinstance(state, dict):
                raise LicenseError("Stored license state is invalid")
            digest = _license_hash(license_data)
            stored_digest = str(state.get("license_sha256", "")).strip().lower()
            if not hmac.compare_digest(digest, stored_digest):
                raise LicenseError("Stored license state does not match its license")
            try:
                last_online_at = int(state.get("last_online_at", 0) or 0)
            except (TypeError, ValueError, OverflowError):
                raise LicenseError("Stored license state has an invalid online timestamp")
            if last_online_at < 0:
                raise LicenseError("Stored license state has an invalid online timestamp")
            return license_data, {
                "legacy": False,
                "last_online_at": last_online_at,
                "license_sha256": digest,
            }

        if envelope.get("schema_version") != LICENSE_SCHEMA:
            raise LicenseError("Stored license format is unsupported")
        try:
            file_mtime = int(os.path.getmtime(target))
        except OSError:
            file_mtime = 0
        return clear_value, {
            "legacy": True,
            "last_online_at": envelope.get("last_online_at", 0),
            "legacy_file_mtime": file_mtime,
            "license_sha256": _license_hash(clear_value),
        }
    except LicenseError:
        raise
    except Exception as exc:
        raise LicenseError("Stored license is invalid: {}".format(exc))


def _status_url():
    return _config().get("status_url") or STATUS_URL


def _fetch_status(timeout):
    url = _status_url()
    if not url:
        return {"online": False, "revoked": False}
    request = _urlrequest.Request(url, headers={"Accept": "application/json", "User-Agent": "Bake-Master-License/2"})
    response = None
    try:
        response = _urlrequest.urlopen(request, timeout=timeout)
        raw = response.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("response exceeds the size limit")
        status = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise LicenseNetworkError("License status check failed: {}".format(exc))
    finally:
        if response is not None:
            try:
                response.close()
            except Exception:
                pass
    if not isinstance(status, dict):
        raise LicenseNetworkError("License status response is invalid")
    signed = dict(status)
    signature = _b64decode(signed.pop("signature", ""), "status signature")
    if not ed25519_verify(_public_key(), signature, _canonical_json(signed)):
        raise LicenseNetworkError("License status signature is invalid")
    products = status.get("products")
    if isinstance(products, dict):
        product_state = products.get(PRODUCT_ID, {})
        revoked = product_state.get("revoked", []) if isinstance(product_state, dict) else []
    else:
        revoked = status.get("revoked", [])
    return {"online": True, "revoked_ids": [str(item) for item in revoked]}


def _invalidate_session():
    global _SESSION
    _SESSION = None
    try:
        import bg_math_core
    except Exception:
        return False
    deactivate = getattr(bg_math_core, "deactivate_license", None)
    if callable(deactivate):
        try:
            deactivate()
            return True
        except Exception:
            pass
    # This also invalidates the previous native implementation during an
    # in-place upgrade, before install_native_guard rejects that old binary.
    activate = getattr(bg_math_core, "activate_signed_license", None)
    if callable(activate):
        try:
            activate("", "", "", 0, "")
            return True
        except Exception:
            pass
    return False


def validate(path=None, timeout=None):
    _invalidate_session()
    try:
        return _validate(path, timeout)
    except Exception:
        _invalidate_session()
        raise


def _validate(path=None, timeout=None):
    license_data, state = load_license(path)
    target = license_path(path)
    if not license_data or not state:
        raise LicenseError("This installation has not been activated")
    verify_license_payload(license_data)
    config = _config()
    offline_days = _bounded_offline_days(license_data, config)
    timeout = int(timeout or config.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS))
    if state.get("legacy"):
        migrated_last_online = _legacy_last_online(state, offline_days)
        _write_license_record(target, license_data, migrated_last_online)
        state = {"legacy": False, "last_online_at": migrated_last_online}
    try:
        status = _fetch_status(timeout)
        if status.get("online"):
            if str(license_data.get("license_id")) in status.get("revoked_ids", []):
                raise LicenseError("This license has been revoked")
            _write_license_record(target, license_data, int(time.time()))
            _activate_session(license_data)
            return {"online": True, "offline_remaining_days": offline_days}
    except LicenseNetworkError:
        pass
    last_online = int(state.get("last_online_at", 0) or 0)
    now = int(time.time())
    if last_online <= 0 or now < last_online - CLOCK_SKEW_SECONDS:
        raise LicenseError("Cannot verify license online and its offline grace period is unavailable")
    remaining = offline_days * 86400 - (now - last_online)
    if remaining < 0:
        raise LicenseError("Offline grace period has expired; connect to the internet")
    _activate_session(license_data)
    return {"online": False, "offline_remaining_days": int(remaining / 86400)}


def _activate_session(license_data):
    global _SESSION
    _invalidate_session()
    expires_at = int(license_data.get("expires_at") or 0)
    session = {
        "license_id": str(license_data.get("license_id")),
        "machine_hash": machine_hash(),
        "pid": os.getpid(),
        "activated_at": int(time.time()),
        "expires_at": expires_at,
    }
    try:
        import bg_math_core
        activate = getattr(bg_math_core, "activate_signed_license", None)
        if activate is not None:
            if not activate(
                str(license_data.get("product_id") or PRODUCT_ID),
                str(license_data.get("license_id")),
                str(license_data.get("machine_hash", "")).lower(),
                expires_at,
                str(license_data.get("native_proof", "")).lower(),
            ):
                raise LicenseRuntimeError("Native license session could not be activated")
    except ImportError:
        # Older development binaries do not yet expose the native gate.
        pass
    except LicenseRuntimeError:
        _invalidate_session()
        raise
    except Exception as exc:
        _invalidate_session()
        raise LicenseRuntimeError("Native license session failed: {}".format(exc))
    _SESSION = session


def require_session(recheck_machine=False):
    if not _SESSION or _SESSION.get("pid") != os.getpid():
        _invalidate_session()
        raise LicenseError("License session is not active")
    expires_at = int(_SESSION.get("expires_at", 0) or 0)
    if expires_at > 0 and expires_at <= int(time.time()):
        _invalidate_session()
        raise LicenseError("License has expired")
    if recheck_machine and not hmac.compare_digest(_SESSION.get("machine_hash", ""), machine_hash()):
        _invalidate_session()
        raise LicenseError("License machine identity changed")
    return True


def require_authorized_action():
    """Require both the signed Python session and the native process gate."""
    require_session(recheck_machine=True)
    try:
        import bg_math_core
        native_require = getattr(bg_math_core, "require_authorized", None)
        if native_require is None:
            _invalidate_session()
            raise LicenseError("Native authorization gate is unavailable")
        native_require()
    except LicenseError:
        raise
    except Exception as exc:
        _invalidate_session()
        raise LicenseError("Native authorization gate rejected this action: {}".format(exc))
    return True


def install_native_guard():
    """Verify that every protected native entry point and process gate exists."""
    try:
        import bg_math_core
    except Exception as exc:
        raise LicenseRuntimeError(
            "Native Bake Master math module is unavailable: {}".format(exc)
        )
    native_activate = getattr(bg_math_core, "activate_signed_license", None)
    native_deactivate = getattr(bg_math_core, "deactivate_license", None)
    native_require = getattr(bg_math_core, "require_authorized", None)
    if not all(callable(item) for item in (native_activate, native_deactivate, native_require)):
        raise LicenseRuntimeError("This Maya runtime does not contain the required native license guard")
    function_names = (
        "calculate_bidirectional_avg_distance",
        "calculate_avg_distance",
        "calculate_coverage_stats",
        "calculate_min_distance",
        "check_mesh_collision",
        "are_symmetric",
        "resolve_hp_collision",
        "calculate_vertex_owner_scores",
        "analyze_mesh_shape",
        "generate_fingerprint_data",
        "PointCloudIndex",
    )
    missing = [name for name in function_names if not callable(getattr(bg_math_core, name, None))]
    if missing:
        raise LicenseRuntimeError(
            "Native authorization entry points are missing: {}".format(", ".join(missing))
        )
    try:
        native_require()
    except Exception as exc:
        raise LicenseRuntimeError("Native license guard is not active: {}".format(exc))
    return True


def clear_license(path=None):
    _invalidate_session()
    target = license_path(path)
    if os.path.isfile(target):
        os.remove(target)
        return True
    return False
