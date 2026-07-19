"""Transactional, Maya-independent update core for Bake Master.

Updates are downloaded from the public release endpoint. No GitHub token is
requested, embedded, or persisted by the plugin.
"""

from __future__ import absolute_import, division, print_function

import base64
import binascii
import hashlib
import hmac
import json
import os
import posixpath
import re
import shutil
import stat
import tempfile
import time
import uuid
import zipfile

try:
    from urllib.error import HTTPError, URLError
    from urllib.parse import quote, urlencode, urlparse
    from urllib.request import HTTPRedirectHandler, Request, build_opener
except ImportError:  # pragma: no cover - Python 3 is required by supported Maya.
    raise


DEFAULT_MAYA_VERSIONS = ("2022", "2023", "2024", "2025", "2026", "2027")
DEFAULT_MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
DEFAULT_MAX_EXTRACTED_BYTES = 1024 * 1024 * 1024
DEFAULT_MAX_FILE_BYTES = 512 * 1024 * 1024
DEFAULT_MAX_ENTRIES = 8192
DEFAULT_MAX_API_BYTES = 4 * 1024 * 1024

REQUIRED_RUNTIME_FILES = (
    "__init__.py",
    "Bake_Group.png",
    "Cheking_Icon.png",
    "Combine.png",
    "Find_ZBRUSH.png",
    "Look_Icon_Button.png",
    "Separate.png",
    "Unlook_Icon_Button.png",
    "bg_core.py",
    "bg_credentials.py",
    "bg_license.py",
    "bg_final_export.py",
    "bg_gt_matcher.py",
    "bg_localization.py",
    "bg_main_window.py",
    "bg_mixins.py",
    "bg_ui_widgets.py",
    "bg_update.py",
    "bg_version.py",
    "bg_worker_hp.py",
    "bg_worker_lp.py",
    "close_eye.png",
    "install_shelf.py",
    "launcher.py",
    "open_eye.png",
    "localization/languages.json",
    "localization/en.json",
    "localization/zh-CN.json",
)

_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_SAFE_REPO_PART_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_WINDOWS_RESERVED = set(
    ["CON", "PRN", "AUX", "NUL"]
    + ["COM%d" % value for value in range(1, 10)]
    + ["LPT%d" % value for value in range(1, 10)]
)
_GITHUB_HOSTS = frozenset(
    (
        "api.github.com",
        "github.com",
        "objects.githubusercontent.com",
        "release-assets.githubusercontent.com",
        "raw.githubusercontent.com",
    )
)


class UpdateError(Exception):
    pass


class IntegrityError(UpdateError):
    pass


class UnsafeArchiveError(UpdateError):
    pass


class PayloadValidationError(UpdateError):
    pass


class VersionConflictError(UpdateError):
    pass


class UpdateInProgressError(UpdateError):
    pass


class GithubApiError(UpdateError):
    pass


class UpdateCancelled(UpdateError):
    """Raised when the user cancels a check or download."""

    pass


class RollbackError(UpdateError):
    def __init__(self, original_error, rollback_errors):
        self.original_error = original_error
        self.rollback_errors = tuple(rollback_errors)
        message = "Update failed and rollback was incomplete: %s" % "; ".join(
            str(item) for item in self.rollback_errors
        )
        UpdateError.__init__(self, message)


class UpdateSpec(object):
    """Validated update metadata, normally decoded from a GitHub manifest."""

    __slots__ = (
        "version",
        "runtime_version",
        "package_version",
        "archive_sha256",
        "maya_versions",
        "tag",
        "asset_name",
    )

    def __init__(
        self,
        version,
        archive_sha256,
        package_version=None,
        maya_versions=None,
        tag=None,
        asset_name=None,
        runtime_version=None,
    ):
        self.package_version = _validate_package_version(package_version or version)
        self.version = self.package_version
        self.runtime_version = _validate_version(runtime_version or version)
        self.archive_sha256 = _normalize_sha256(archive_sha256)
        versions = DEFAULT_MAYA_VERSIONS if maya_versions is None else tuple(maya_versions)
        if not versions or any(not re.match(r"^20[0-9]{2}$", str(item)) for item in versions):
            raise ValueError("maya_versions must contain four-digit Maya release years")
        self.maya_versions = tuple(str(item) for item in versions)
        self.tag = _optional_text(tag, "tag")
        self.asset_name = _optional_text(asset_name, "asset_name")

    @classmethod
    def from_mapping(cls, mapping):
        if not isinstance(mapping, dict):
            raise ValueError("Update manifest must be a JSON object")
        # The stable channel uses asset_sha256; older local manifests used
        # archive_sha256/sha256. Accept all three while normalizing internally.
        checksum = mapping.get(
            "asset_sha256",
            mapping.get("archive_sha256", mapping.get("sha256")),
        )
        package_version = mapping.get("package_version", mapping.get("version"))
        runtime_version = mapping.get("runtime_version", mapping.get("version"))
        return cls(
            version=package_version,
            package_version=package_version,
            runtime_version=runtime_version,
            archive_sha256=checksum,
            maya_versions=mapping.get("maya_versions"),
            tag=mapping.get("tag"),
            asset_name=mapping.get("asset_name"),
        )


class UpdateResult(object):
    __slots__ = ("version", "version_path", "previous_version", "installed_new")

    def __init__(self, version, version_path, previous_version, installed_new):
        self.version = version
        self.version_path = version_path
        self.previous_version = previous_version
        self.installed_new = bool(installed_new)


class _UpdateLock(object):
    def __init__(self, path):
        self.path = path
        self.acquired = False

    def __enter__(self):
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        try:
            descriptor = os.open(self.path, flags, 0o600)
        except OSError as exc:
            if os.path.exists(self.path):
                raise UpdateInProgressError("Another update holds the lock: %s" % self.path)
            raise UpdateError("Cannot create update lock: %s" % exc)
        try:
            payload = json.dumps({"pid": os.getpid(), "created": time.time()}).encode("ascii")
            os.write(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        self.acquired = True
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if self.acquired:
            try:
                os.remove(self.path)
            except OSError:
                if os.path.exists(self.path):
                    raise
            self.acquired = False


class _GithubRedirectHandler(HTTPRedirectHandler):
    """Allow GitHub asset redirects without leaking a private-repo token."""

    def redirect_request(self, request, fp, code, msg, headers, newurl):
        _validate_github_url(newurl)
        redirected = HTTPRedirectHandler.redirect_request(
            self, request, fp, code, msg, headers, newurl
        )
        if redirected is None:
            return None
        old_host = (urlparse(request.full_url).hostname or "").lower()
        new_host = (urlparse(newurl).hostname or "").lower()
        if old_host != new_host:
            for container in (redirected.headers, redirected.unredirected_hdrs):
                for key in list(container.keys()):
                    if key.lower() == "authorization":
                        del container[key]
        return redirected


def _validate_version(value):
    if not isinstance(value, str) or not _VERSION_RE.match(value):
        raise ValueError("Invalid runtime version: %r" % (value,))
    return value


def _validate_package_version(value):
    if not isinstance(value, str) or not value or len(value) > 128:
        raise ValueError("Invalid package version")
    if any(ord(char) < 32 for char in value):
        raise ValueError("Package version contains control characters")
    return value


def _optional_text(value, label):
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > 255:
        raise ValueError("Invalid %s" % label)
    if any(ord(char) < 32 for char in value):
        raise ValueError("%s contains control characters" % label)
    return value


def _normalize_sha256(value):
    if not isinstance(value, str) or not _SHA256_RE.match(value):
        raise ValueError("Expected SHA256 must contain exactly 64 hexadecimal characters")
    return value.lower()


def sha256_file(path, chunk_size=1024 * 1024):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        while True:
            chunk = stream.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def verify_sha256(path, expected_sha256):
    expected = _normalize_sha256(expected_sha256)
    actual = sha256_file(path)
    if not hmac.compare_digest(actual, expected):
        raise IntegrityError("Archive SHA256 mismatch")
    return actual


def _windows_component_is_safe(component):
    if not component or component in (".", ".."):
        return False
    if component.endswith((" ", ".")) or ":" in component:
        return False
    if any(ord(char) < 32 for char in component):
        return False
    stem = component.split(".", 1)[0].upper()
    return stem not in _WINDOWS_RESERVED


def _normalize_zip_name(info):
    raw_name = info.filename
    if not isinstance(raw_name, str) or "\x00" in raw_name:
        raise UnsafeArchiveError("ZIP contains an invalid filename")
    normalized_slashes = raw_name.replace("\\", "/")
    is_directory = normalized_slashes.endswith("/")
    if normalized_slashes.startswith("/") or re.match(r"^[A-Za-z]:", normalized_slashes):
        raise UnsafeArchiveError("ZIP contains an absolute path: %s" % raw_name)
    parts = normalized_slashes.split("/")
    if is_directory:
        parts = parts[:-1]
    if not parts or any(not _windows_component_is_safe(item) for item in parts):
        raise UnsafeArchiveError("ZIP contains an unsafe path: %s" % raw_name)
    normalized = posixpath.join(*parts)
    if len(normalized) > 1024:
        raise UnsafeArchiveError("ZIP member path is too long")
    mode = (info.external_attr >> 16) & 0xFFFF
    file_type = stat.S_IFMT(mode)
    allowed_type = stat.S_IFDIR if is_directory else stat.S_IFREG
    if file_type not in (0, allowed_type):
        raise UnsafeArchiveError("ZIP contains a link or special file: %s" % raw_name)
    if info.flag_bits & 0x1:
        raise UnsafeArchiveError("Encrypted ZIP entries are not supported")
    return normalized, is_directory


def _validated_zip_entries(archive, max_entries, max_extracted_bytes, max_file_bytes):
    infos = archive.infolist()
    if len(infos) > max_entries:
        raise UnsafeArchiveError("ZIP contains too many entries")
    records = []
    paths = {}
    total_size = 0
    for info in infos:
        normalized, is_directory = _normalize_zip_name(info)
        collision_key = normalized.casefold()
        if collision_key in paths:
            raise UnsafeArchiveError("ZIP contains duplicate or case-colliding paths: %s" % normalized)
        paths[collision_key] = is_directory
        if not is_directory:
            if info.file_size > max_file_bytes:
                raise UnsafeArchiveError("ZIP member exceeds the file-size limit: %s" % normalized)
            total_size += info.file_size
            if total_size > max_extracted_bytes:
                raise UnsafeArchiveError("ZIP exceeds the extracted-size limit")
        records.append((info, normalized, is_directory))

    file_paths = set(key for key, is_directory in paths.items() if not is_directory)
    for key in paths:
        pieces = key.split("/")
        for index in range(1, len(pieces)):
            if "/".join(pieces[:index]) in file_paths:
                raise UnsafeArchiveError("ZIP has a file/directory path collision")
    return records


def safe_extract_zip(
    archive_path,
    destination,
    max_entries=DEFAULT_MAX_ENTRIES,
    max_extracted_bytes=DEFAULT_MAX_EXTRACTED_BYTES,
    max_file_bytes=DEFAULT_MAX_FILE_BYTES,
):
    """Extract a ZIP after validating every path and entry before writing."""
    destination = os.path.abspath(destination)
    if os.path.islink(destination):
        raise UnsafeArchiveError("Extraction destination must not be a symbolic link")
    if os.path.exists(destination):
        if not os.path.isdir(destination) or os.listdir(destination):
            raise UnsafeArchiveError("Extraction destination must be an empty directory")
    else:
        os.makedirs(destination)

    try:
        archive = zipfile.ZipFile(archive_path, "r")
    except (OSError, zipfile.BadZipFile) as exc:
        raise UnsafeArchiveError("Cannot open update ZIP: %s" % exc)

    with archive:
        records = _validated_zip_entries(
            archive, max_entries, max_extracted_bytes, max_file_bytes
        )
        extracted_total = 0
        for info, relative_name, is_directory in records:
            target = os.path.abspath(os.path.join(destination, *relative_name.split("/")))
            try:
                common = os.path.commonpath((destination, target))
            except ValueError:
                raise UnsafeArchiveError("ZIP path escapes the extraction directory")
            if os.path.normcase(common) != os.path.normcase(destination):
                raise UnsafeArchiveError("ZIP path escapes the extraction directory")
            if is_directory:
                if not os.path.isdir(target):
                    os.makedirs(target)
                continue
            parent = os.path.dirname(target)
            if not os.path.isdir(parent):
                os.makedirs(parent)
            written = 0
            try:
                source_stream = archive.open(info, "r")
                target_stream = open(target, "xb")
                with source_stream, target_stream:
                    while True:
                        chunk = source_stream.read(1024 * 1024)
                        if not chunk:
                            break
                        written += len(chunk)
                        extracted_total += len(chunk)
                        if written > max_file_bytes or extracted_total > max_extracted_bytes:
                            raise UnsafeArchiveError("ZIP expanded beyond its declared limits")
                        target_stream.write(chunk)
            except (OSError, zipfile.BadZipFile) as exc:
                raise UnsafeArchiveError("Cannot extract ZIP member %s: %s" % (relative_name, exc))
            if written != info.file_size:
                raise UnsafeArchiveError("ZIP member size changed while extracting: %s" % relative_name)
    return destination


def _find_runtime_root(extracted_root):
    marker_files = frozenset(("bg_main_window.py", "bg_core.py", "launcher.py"))
    candidates = []
    for directory, child_dirs, files in os.walk(extracted_root):
        relative = os.path.relpath(directory, extracted_root)
        depth = 0 if relative == "." else len(relative.split(os.sep))
        if depth > 5:
            child_dirs[:] = []
            continue
        if marker_files.issubset(set(files)):
            candidates.append(directory)
    if len(candidates) != 1:
        raise PayloadValidationError(
            "Update ZIP must contain exactly one runtime payload; found %d" % len(candidates)
        )
    return candidates[0]


def validate_runtime_payload(runtime_root, spec):
    missing = []
    empty = []
    required = list(REQUIRED_RUNTIME_FILES)
    required.extend(
        "bin/%s/bg_math_core.pyd" % maya_version for maya_version in spec.maya_versions
    )
    for relative_name in required:
        path = os.path.join(runtime_root, *relative_name.split("/"))
        if not os.path.isfile(path) or os.path.islink(path):
            missing.append(relative_name)
        elif relative_name != "__init__.py" and os.path.getsize(path) <= 0:
            empty.append(relative_name)
    if missing:
        raise PayloadValidationError("Runtime payload is missing: %s" % ", ".join(missing))
    if empty:
        raise PayloadValidationError("Runtime payload has empty files: %s" % ", ".join(empty))

    version_module = os.path.join(runtime_root, "bg_version.py")
    with open(version_module, "rb") as stream:
        version_text = stream.read(64 * 1024).decode("utf-8-sig")
    match = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']\s*$', version_text, re.MULTILINE)
    if not match or match.group(1) != spec.package_version:
        raise PayloadValidationError("bg_version.py does not match package_version")
    return True


def _json_bytes(mapping):
    return (json.dumps(mapping, ensure_ascii=True, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _atomic_write_bytes(path, payload):
    parent = os.path.dirname(os.path.abspath(path))
    temp_path = os.path.join(parent, ".%s.tmp-%s" % (os.path.basename(path), uuid.uuid4().hex))
    descriptor = None
    try:
        descriptor = os.open(temp_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if os.path.exists(temp_path):
            os.remove(temp_path)


def _atomic_write_json(path, mapping):
    _atomic_write_bytes(path, _json_bytes(mapping))


def _read_json_file(path):
    with open(path, "rb") as stream:
        raw = stream.read()
    try:
        value = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise UpdateError("Invalid JSON file %s: %s" % (path, exc))
    if not isinstance(value, dict):
        raise UpdateError("JSON root must be an object: %s" % path)
    return raw, value


def _safe_remove_tree(path, expected_parent):
    path = os.path.abspath(path)
    expected_parent = os.path.abspath(expected_parent)
    if os.path.normcase(os.path.dirname(path)) != os.path.normcase(expected_parent):
        raise UpdateError("Refusing to remove a directory outside the update parent")
    if os.path.islink(path):
        os.remove(path)
    elif os.path.isdir(path):
        shutil.rmtree(path)


def _write_receipt(runtime_root, spec):
    receipt = {
        "schema": 1,
        "runtime_version": spec.runtime_version,
        "package_version": spec.package_version,
        "archive_sha256": spec.archive_sha256,
        "installed_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    _atomic_write_json(os.path.join(runtime_root, ".bg_update_receipt.json"), receipt)


def _existing_version_matches(target, spec):
    receipt_path = os.path.join(target, ".bg_update_receipt.json")
    if not os.path.isfile(receipt_path) or os.path.islink(receipt_path):
        return False
    try:
        unused_raw, receipt = _read_json_file(receipt_path)
    except UpdateError:
        return False
    return (
        receipt.get("runtime_version") == spec.runtime_version
        and receipt.get("package_version") == spec.package_version
        and hmac.compare_digest(str(receipt.get("archive_sha256", "")), spec.archive_sha256)
    )


def install_archive(
    plugin_root,
    archive_path,
    spec,
    validate_callback=None,
    post_activate_callback=None,
):
    """Install beside old versions, then atomically activate the new version.

    Callbacks receive ``(version_path, spec)``. A callback exception triggers
    rollback. ``validate_callback`` runs before activation; ``post_activate``
    exists for a lightweight caller-owned smoke check after pointer switching.
    """
    if not isinstance(spec, UpdateSpec):
        spec = UpdateSpec.from_mapping(spec)
    plugin_root = os.path.realpath(os.path.abspath(plugin_root))
    archive_path = os.path.abspath(archive_path)
    if not os.path.isdir(plugin_root):
        raise UpdateError("Plugin root does not exist: %s" % plugin_root)
    if not os.path.isfile(archive_path):
        raise UpdateError("Update archive does not exist: %s" % archive_path)
    if os.path.getsize(archive_path) > DEFAULT_MAX_ARCHIVE_BYTES:
        raise IntegrityError("Update archive exceeds the size limit")
    verify_sha256(archive_path, spec.archive_sha256)

    active_path = os.path.join(plugin_root, "active_version.json")
    versions_root = os.path.join(plugin_root, "versions")
    if not os.path.isdir(versions_root):
        raise UpdateError("Plugin versions directory does not exist")
    target = os.path.join(versions_root, spec.runtime_version)
    lock_path = os.path.join(plugin_root, ".bg_update.lock")

    with _UpdateLock(lock_path):
        old_active_bytes, old_active = _read_json_file(active_path)
        previous_version = old_active.get("active_version")
        staging_root = None
        installed_new = False
        active_write_attempted = False
        try:
            if os.path.exists(target):
                if not os.path.isdir(target) or not _existing_version_matches(target, spec):
                    raise VersionConflictError(
                        "Version directory already exists and is not this verified update: %s" % target
                    )
                validate_runtime_payload(target, spec)
            else:
                staging_root = tempfile.mkdtemp(prefix=".bg-update-", dir=versions_root)
                extracted_root = os.path.join(staging_root, "extracted")
                safe_extract_zip(archive_path, extracted_root)
                runtime_root = _find_runtime_root(extracted_root)
                validate_runtime_payload(runtime_root, spec)
                _write_receipt(runtime_root, spec)
                os.replace(runtime_root, target)
                installed_new = True

            if validate_callback is not None:
                validate_callback(target, spec)

            next_active = dict(old_active)
            next_active.pop("path", None)
            next_active["active_version"] = spec.runtime_version
            next_active["package_version"] = spec.package_version
            # Keep the previous pointer until the next Maya process proves the
            # new binary can load. The launcher uses this pair for startup
            # rollback after a failed update.
            next_active["previous_version"] = previous_version
            next_active["pending_version"] = (
                spec.runtime_version if previous_version and previous_version != spec.runtime_version else None
            )
            next_active["failed_version"] = None
            next_active.pop("rollback_reason", None)
            active_write_attempted = True
            _atomic_write_json(active_path, next_active)

            if post_activate_callback is not None:
                post_activate_callback(target, spec)

            return UpdateResult(spec.runtime_version, target, previous_version, installed_new)
        except BaseException as original_error:
            rollback_errors = []
            active_restored = True
            if active_write_attempted:
                try:
                    _atomic_write_bytes(active_path, old_active_bytes)
                except BaseException as exc:
                    active_restored = False
                    rollback_errors.append("active_version restore failed: %s" % exc)
            if installed_new and active_restored:
                try:
                    _safe_remove_tree(target, versions_root)
                except BaseException as exc:
                    rollback_errors.append("new version cleanup failed: %s" % exc)
            if rollback_errors:
                raise RollbackError(original_error, rollback_errors) from original_error
            raise
        finally:
            if staging_root and os.path.exists(staging_root):
                _safe_remove_tree(staging_root, versions_root)


def _validate_repo_part(value, label):
    if not isinstance(value, str) or not _SAFE_REPO_PART_RE.match(value):
        raise ValueError("Invalid GitHub %s" % label)
    return value


def _validate_token(token):
    if token is None:
        return None
    if not isinstance(token, str) or not token or any(char in token for char in "\r\n"):
        raise ValueError("Invalid GitHub token")
    return token


def _validate_github_url(url):
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme.lower() != "https" or host not in _GITHUB_HOSTS:
        raise GithubApiError("Refusing a non-GitHub HTTPS URL")
    if parsed.username or parsed.password or parsed.port not in (None, 443):
        raise GithubApiError("GitHub URL contains unsafe authority data")
    return url


def _github_headers(token=None, accept="application/vnd.github+json"):
    headers = {
        "Accept": accept,
        "User-Agent": "Bake-Groups-Updater/1",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = _validate_token(token)
    if token is not None:
        headers["Authorization"] = "Bearer %s" % token
    return headers


def _open_github(request, timeout, opener=None):
    if opener is None:
        opener = build_opener(_GithubRedirectHandler())
    try:
        response = opener.open(request, timeout=timeout)
    except (HTTPError, URLError, OSError) as exc:
        raise GithubApiError("GitHub request failed: %s" % exc)
    try:
        _validate_github_url(response.geturl())
    except BaseException:
        response.close()
        raise
    return response


def _read_limited_response(response, max_bytes):
    chunks = []
    total = 0
    while True:
        chunk = response.read(min(1024 * 1024, max_bytes - total + 1))
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise GithubApiError("GitHub response exceeds the size limit")
        chunks.append(chunk)
    return b"".join(chunks)


def _github_json(url, token=None, timeout=30, opener=None, max_bytes=DEFAULT_MAX_API_BYTES):
    _validate_github_url(url)
    request = Request(url, headers=_github_headers(token))
    response = _open_github(request, timeout, opener)
    try:
        payload = _read_limited_response(response, max_bytes)
    finally:
        response.close()
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise GithubApiError("GitHub returned invalid JSON: %s" % exc)
    return value


def fetch_github_contents_json(
    owner,
    repository,
    manifest_path,
    ref="main",
    token=None,
    timeout=30,
    opener=None,
):
    """Load a JSON manifest through the GitHub Contents API (private repos too)."""
    owner = _validate_repo_part(owner, "owner")
    repository = _validate_repo_part(repository, "repository")
    if not isinstance(manifest_path, str) or not manifest_path or ".." in manifest_path.split("/"):
        raise ValueError("Invalid manifest path")
    encoded_path = "/".join(quote(part, safe="") for part in manifest_path.split("/") if part)
    query = urlencode({"ref": _optional_text(ref, "ref")})
    url = "https://api.github.com/repos/%s/%s/contents/%s?%s" % (
        quote(owner, safe=""),
        quote(repository, safe=""),
        encoded_path,
        query,
    )
    response = _github_json(url, token=token, timeout=timeout, opener=opener)
    if not isinstance(response, dict) or response.get("encoding") != "base64":
        raise GithubApiError("GitHub Contents API did not return base64 file content")
    content = response.get("content")
    if not isinstance(content, str):
        raise GithubApiError("GitHub manifest content is missing")
    try:
        encoded = re.sub(br"\s+", b"", content.encode("ascii"))
        decoded = base64.b64decode(encoded, validate=True)
        manifest = json.loads(decoded.decode("utf-8-sig"))
    except (UnicodeEncodeError, UnicodeDecodeError, binascii.Error, ValueError) as exc:
        raise GithubApiError("Cannot decode GitHub update manifest: %s" % exc)
    if not isinstance(manifest, dict):
        raise GithubApiError("GitHub update manifest must be a JSON object")
    return manifest


def load_update_spec_from_github(
    owner,
    repository,
    manifest_path,
    ref="main",
    token=None,
    timeout=30,
    opener=None,
):
    manifest = fetch_github_contents_json(
        owner, repository, manifest_path, ref, token, timeout, opener
    )
    return UpdateSpec.from_mapping(manifest)


def download_github_release_asset(
    owner,
    repository,
    tag,
    asset_name,
    destination,
    expected_sha256,
    token=None,
    timeout=60,
    opener=None,
    max_bytes=DEFAULT_MAX_ARCHIVE_BYTES,
    progress=None,
    cancelled=None,
):
    """Resolve a release by tag and stream one exact-name asset with SHA256."""
    owner = _validate_repo_part(owner, "owner")
    repository = _validate_repo_part(repository, "repository")
    tag = _optional_text(tag, "tag")
    asset_name = _optional_text(asset_name, "asset_name")
    release_url = "https://api.github.com/repos/%s/%s/releases/tags/%s" % (
        quote(owner, safe=""),
        quote(repository, safe=""),
        quote(tag, safe=""),
    )
    release = _github_json(release_url, token=token, timeout=timeout, opener=opener)
    assets = release.get("assets", []) if isinstance(release, dict) else []
    matches = [item for item in assets if isinstance(item, dict) and item.get("name") == asset_name]
    if len(matches) != 1:
        raise GithubApiError("Release must contain exactly one asset named %s" % asset_name)
    asset = matches[0]
    asset_url = asset.get("url")
    _validate_github_url(asset_url)
    declared_size = asset.get("size")
    if isinstance(declared_size, int) and declared_size > max_bytes:
        raise GithubApiError("Release asset exceeds the download-size limit")

    destination = os.path.abspath(destination)
    parent = os.path.dirname(destination)
    if not os.path.isdir(parent):
        os.makedirs(parent)
    partial = destination + ".part-" + uuid.uuid4().hex
    expected = _normalize_sha256(expected_sha256)
    digest = hashlib.sha256()
    total = 0
    request = Request(
        asset_url,
        headers=_github_headers(token, accept="application/octet-stream"),
    )
    response = _open_github(request, timeout, opener)
    try:
        with open(partial, "xb") as stream:
            while True:
                if cancelled is not None and cancelled():
                    raise UpdateCancelled("Update download cancelled")
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise GithubApiError("Release asset exceeds the download-size limit")
                digest.update(chunk)
                stream.write(chunk)
                if progress is not None:
                    percent = int((total * 100) / declared_size) if declared_size else 0
                    progress(max(0, min(99, percent)), "download")
            stream.flush()
            os.fsync(stream.fileno())
        if not hmac.compare_digest(digest.hexdigest(), expected):
            raise IntegrityError("Downloaded release asset SHA256 mismatch")
        os.replace(partial, destination)
        if progress is not None:
            progress(100, "download_complete")
    finally:
        response.close()
        if os.path.exists(partial):
            os.remove(partial)
    return destination


def update_from_github_release(
    plugin_root,
    owner,
    repository,
    spec,
    token=None,
    timeout=60,
    opener=None,
    validate_callback=None,
    post_activate_callback=None,
    progress=None,
    cancelled=None,
):
    """Download a private/public release asset, install it, and clean the ZIP."""
    if not isinstance(spec, UpdateSpec):
        spec = UpdateSpec.from_mapping(spec)
    if not spec.tag or not spec.asset_name:
        raise ValueError("Update spec requires tag and asset_name")
    plugin_root = os.path.realpath(os.path.abspath(plugin_root))
    download_root = tempfile.mkdtemp(prefix=".bg-download-", dir=plugin_root)
    archive_path = os.path.join(download_root, "update.zip")
    try:
        download_github_release_asset(
            owner,
            repository,
            spec.tag,
            spec.asset_name,
            archive_path,
            spec.archive_sha256,
            token=token,
            timeout=timeout,
            opener=opener,
            progress=progress,
            cancelled=cancelled,
        )
        if cancelled is not None and cancelled():
            raise UpdateCancelled("Update cancelled")
        if progress is not None:
            progress(100, "verify_archive")
        return install_archive(
            plugin_root,
            archive_path,
            spec,
            validate_callback=validate_callback,
            post_activate_callback=post_activate_callback,
        )
    finally:
        if os.path.isdir(download_root):
            shutil.rmtree(download_root)


def _version_key(value):
    """Return a dependency-free comparable key for common version strings."""
    value = str(value or "0")
    parts = re.split(r"[.+-]", value)
    result = []
    for part in parts:
        if part.isdigit():
            result.append((1, int(part)))
        else:
            result.append((0, part.lower()))
    return tuple(result)


def _read_config(plugin_root):
    path = os.path.join(plugin_root, "update_config.json")
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "rb") as stream:
            value = json.loads(stream.read().decode("utf-8-sig"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise UpdateError("Invalid update configuration: %s" % exc)
    if not isinstance(value, dict):
        raise UpdateError("Update configuration must be a JSON object")
    return value


def _repository_parts(config):
    repository = config.get("repository")
    if isinstance(repository, str) and "/" in repository:
        owner, name = repository.split("/", 1)
    else:
        api_url = config.get("repository_api_url", "")
        match = re.match(r"^https://api\.github\.com/repos/([^/]+)/([^/?#]+)", str(api_url))
        if not match:
            raise GithubApiError("GitHub repository is not configured")
        owner, name = match.groups()
    return _validate_repo_part(owner, "owner"), _validate_repo_part(name, "repository")


def _manifest_from_response(response):
    if isinstance(response, dict) and response.get("encoding") == "base64":
        content = response.get("content")
        if not isinstance(content, str):
            raise GithubApiError("GitHub update manifest content is missing")
        try:
            raw = base64.b64decode(
                re.sub(br"\s+", b"", content.encode("ascii")), validate=True
            )
            response = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeEncodeError, UnicodeDecodeError, binascii.Error, ValueError) as exc:
            raise GithubApiError("Cannot decode GitHub update manifest: %s" % exc)
    if not isinstance(response, dict):
        raise GithubApiError("GitHub update manifest must be a JSON object")
    return response


class UpdateClient(object):
    """Small UI-facing client around the transactional private GitHub updater."""

    def __init__(self, token=None, cancelled=None, progress=None, plugin_root=None):
        self.plugin_root = os.path.realpath(
            os.path.abspath(plugin_root or os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
        )
        self.config = _read_config(self.plugin_root)
        # Keep the manifest URL and repository identity consistent. A stale
        # placeholder manifest must fail clearly instead of looking like a
        # valid update channel.
        if self.config.get("private_repository") and "OWNER/REPOSITORY" in str(
            self.config.get("manifest_api_url", "")
        ):
            raise GithubApiError("Private GitHub update repository is not configured")
        self.token = token
        self.cancelled = cancelled
        self.progress = progress

    def _check_cancelled(self):
        if self.cancelled is not None and self.cancelled():
            raise UpdateCancelled("Update check cancelled")

    def _load_manifest(self):
        self._check_cancelled()
        manifest_url = self.config.get("manifest_api_url")
        if manifest_url:
            _validate_github_url(manifest_url)
            response = _manifest_from_response(
                _github_json(
                    manifest_url,
                    token=self.token,
                    timeout=int(self.config.get("request_timeout_seconds", 30)),
                )
            )
        else:
            owner, repository = _repository_parts(self.config)
            response = fetch_github_contents_json(
                owner,
                repository,
                self.config.get("manifest_path", "updates/stable.json"),
                ref=self.config.get("manifest_ref", "main"),
                token=self.token,
                timeout=int(self.config.get("request_timeout_seconds", 30)),
            )
        # Validate the manifest before it reaches the UI. This also normalizes
        # asset_sha256/archive_sha256/sha256 to UpdateSpec.archive_sha256.
        spec = UpdateSpec.from_mapping(response)
        return response, spec

    def check_for_update(self, current_version, maya_version=None):
        manifest, spec = self._load_manifest()
        self._check_cancelled()
        supported = [str(item) for item in spec.maya_versions]
        compatible = not maya_version or str(maya_version) in supported
        available = compatible and _version_key(spec.version) > _version_key(current_version)
        result = dict(manifest)
        result["available"] = bool(available)
        result["compatible"] = bool(compatible)
        result["current_version"] = str(current_version)
        result["version"] = spec.package_version
        result["runtime_version"] = spec.runtime_version
        result["archive_sha256"] = spec.archive_sha256
        return result

    def download_and_install(self, manifest, maya_version=None):
        self._check_cancelled()
        spec = UpdateSpec.from_mapping(manifest)
        if maya_version and str(maya_version) not in spec.maya_versions:
            raise GithubApiError("This update does not support Maya %s" % maya_version)
        owner, repository = _repository_parts(self.config)
        if self.progress is not None:
            self.progress(0, "download")
        result = update_from_github_release(
            self.plugin_root,
            owner,
            repository,
            spec,
            token=self.token,
            timeout=int(self.config.get("request_timeout_seconds", 60)),
            progress=self.progress,
            cancelled=self.cancelled,
        )
        if self.progress is not None:
            self.progress(100, "install_complete")
        return {
            "version": result.version,
            "version_path": result.version_path,
            "previous_version": result.previous_version,
            "installed_new": result.installed_new,
            "requires_restart": bool(result.previous_version and result.previous_version != result.version),
        }
