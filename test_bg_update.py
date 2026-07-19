from __future__ import absolute_import, division, print_function

import base64
import hashlib
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
import zipfile

try:
    from unittest import mock
except ImportError:  # pragma: no cover
    import mock


PLUGIN_ROOT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "plugin", "Bake_Groups"
)


def runtime_under_test():
    active_path = os.path.join(PLUGIN_ROOT, "active_version.json")
    with io.open(active_path, "r", encoding="utf-8-sig") as stream:
        active_version = str(json.load(stream).get("active_version") or "")
    # An unreleased development runtime intentionally takes precedence so the
    # tests cannot silently keep exercising the currently published version.
    candidates = ("1.3.14", active_version)
    for version in candidates:
        candidate = os.path.join(PLUGIN_ROOT, "versions", version)
        if version and os.path.isfile(os.path.join(candidate, "bg_update.py")):
            return candidate
    raise RuntimeError("No Bake Master runtime is available for update tests")


HERE = runtime_under_test()
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import bg_update

if os.path.normcase(os.path.dirname(os.path.realpath(bg_update.__file__))) != os.path.normcase(
    os.path.realpath(HERE)
):
    raise RuntimeError("Update tests imported the wrong Bake Master runtime")


def read_bytes(path):
    with open(path, "rb") as stream:
        return stream.read()


class FakeResponse(io.BytesIO):
    def __init__(self, payload, url):
        io.BytesIO.__init__(self, payload)
        self._url = url

    def geturl(self):
        return self._url


class FakeOpener(object):
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("Unexpected request")
        return self.responses.pop(0)


class UpdateCoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.mkdtemp(prefix="bg-update-tests-")
        self.plugin_root = os.path.join(self.temp, "Bake_Groups")
        self.versions_root = os.path.join(self.plugin_root, "versions")
        os.makedirs(os.path.join(self.versions_root, "1.3.7"))
        with open(os.path.join(self.versions_root, "1.3.7", "old.txt"), "wb") as stream:
            stream.write(b"old-version")
        self.active_path = os.path.join(self.plugin_root, "active_version.json")
        self.original_active = (
            b'{\n  "active_version": "1.3.7",\n'
            b'  "package_version": "1.3.7+local.2",\n'
            b'  "path": "C:\\\\old-machine\\\\Bake_Groups\\\\versions\\\\1.3.7"\n}\n'
        )
        with open(self.active_path, "wb") as stream:
            stream.write(self.original_active)
        self.launcher_path = os.path.join(self.plugin_root, "launcher.py")
        self.original_launcher = b"old-bootstrap-launcher\n"
        with open(self.launcher_path, "wb") as stream:
            stream.write(self.original_launcher)

    def tearDown(self):
        shutil.rmtree(self.temp)

    def _create_runtime(self, root, package_version="1.3.8"):
        for relative in bg_update.REQUIRED_RUNTIME_FILES:
            path = os.path.join(root, *relative.split("/"))
            parent = os.path.dirname(path)
            if not os.path.isdir(parent):
                os.makedirs(parent)
            if relative == "__init__.py":
                payload = b""
            elif relative == "bg_version.py":
                payload = ('__version__ = "%s"\n' % package_version).encode("ascii")
            else:
                payload = ("payload:" + relative).encode("utf-8")
            with open(path, "wb") as stream:
                stream.write(payload)
        for maya_version in bg_update.DEFAULT_MAYA_VERSIONS:
            path = os.path.join(root, "bin", maya_version, "bg_math_core.pyd")
            parent = os.path.dirname(path)
            if not os.path.isdir(parent):
                os.makedirs(parent)
            with open(path, "wb") as stream:
                stream.write(("pyd-" + maya_version).encode("ascii"))

    def _create_archive(self, name="update.zip", extra_entries=None):
        build_root = os.path.join(self.temp, "build-" + name.replace(".", "-"))
        runtime_root = os.path.join(build_root, "release", "runtime")
        os.makedirs(runtime_root)
        self._create_runtime(runtime_root)
        archive_path = os.path.join(self.temp, name)
        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for directory, unused_dirs, files in os.walk(build_root):
                for filename in files:
                    path = os.path.join(directory, filename)
                    relative = os.path.relpath(path, build_root).replace(os.sep, "/")
                    archive.write(path, relative)
            for entry_name, payload in extra_entries or ():
                archive.writestr(entry_name, payload)
        checksum = bg_update.sha256_file(archive_path)
        spec = bg_update.UpdateSpec(
            "1.3.8",
            checksum,
            package_version="1.3.8",
            tag="v1.3.8",
            asset_name="Bake_Groups_1.3.8.zip",
        )
        return archive_path, spec

    def _read_active(self):
        with open(self.active_path, "rb") as stream:
            return json.loads(stream.read().decode("utf-8-sig"))

    def test_success_keeps_old_version_and_atomically_activates_new(self):
        archive_path, spec = self._create_archive()
        result = bg_update.install_archive(self.plugin_root, archive_path, spec)

        self.assertTrue(result.installed_new)
        self.assertEqual("1.3.7", result.previous_version)
        self.assertTrue(os.path.isdir(os.path.join(self.versions_root, "1.3.7")))
        new_root = os.path.join(self.versions_root, "1.3.8")
        self.assertTrue(os.path.isdir(new_root))
        self.assertTrue(os.path.isfile(os.path.join(new_root, ".bg_update_receipt.json")))
        active = self._read_active()
        self.assertEqual("1.3.8", active["active_version"])
        self.assertEqual("1.3.8", active["package_version"])
        self.assertNotIn("path", active)
        self.assertEqual(b"payload:launcher.py", read_bytes(self.launcher_path))

    def test_same_verified_version_is_idempotent(self):
        archive_path, spec = self._create_archive()
        first = bg_update.install_archive(self.plugin_root, archive_path, spec)
        with open(self.launcher_path, "wb") as stream:
            stream.write(b"damaged-bootstrap")
        second = bg_update.install_archive(self.plugin_root, archive_path, spec)
        self.assertTrue(first.installed_new)
        self.assertFalse(second.installed_new)
        self.assertEqual(b"payload:launcher.py", read_bytes(self.launcher_path))

    def test_hash_mismatch_changes_nothing(self):
        archive_path, unused_spec = self._create_archive()
        spec = bg_update.UpdateSpec("1.3.8", "0" * 64, package_version="1.3.8")
        with self.assertRaises(bg_update.IntegrityError):
            bg_update.install_archive(self.plugin_root, archive_path, spec)
        self.assertEqual(self.original_active, read_bytes(self.active_path))
        self.assertEqual(self.original_launcher, read_bytes(self.launcher_path))
        self.assertFalse(os.path.exists(os.path.join(self.versions_root, "1.3.8")))

    def test_zip_traversal_is_rejected_before_install(self):
        archive_path, spec = self._create_archive(
            name="traversal.zip", extra_entries=(("../../escaped.txt", b"bad"),)
        )
        with self.assertRaises(bg_update.UnsafeArchiveError):
            bg_update.install_archive(self.plugin_root, archive_path, spec)
        self.assertFalse(os.path.exists(os.path.join(self.versions_root, "1.3.8")))
        self.assertFalse(os.path.exists(os.path.join(self.temp, "escaped.txt")))

    def test_case_colliding_zip_paths_are_rejected(self):
        archive_path, spec = self._create_archive(
            name="collision.zip",
            extra_entries=(("Readme.txt", b"one"), ("README.TXT", b"two")),
        )
        with self.assertRaises(bg_update.UnsafeArchiveError):
            bg_update.install_archive(self.plugin_root, archive_path, spec)

    def test_symlink_zip_entry_is_rejected(self):
        archive_path, spec = self._create_archive(name="symlink-base.zip")
        with zipfile.ZipFile(archive_path, "a") as archive:
            info = zipfile.ZipInfo("release/runtime/link")
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, "../../outside")
        spec = bg_update.UpdateSpec("1.3.8", bg_update.sha256_file(archive_path), "1.3.8")
        with self.assertRaises(bg_update.UnsafeArchiveError):
            bg_update.install_archive(self.plugin_root, archive_path, spec)

    def test_pre_activation_failure_removes_new_version(self):
        archive_path, spec = self._create_archive()

        def fail_validation(unused_path, unused_spec):
            raise RuntimeError("smoke test failed")

        with self.assertRaises(RuntimeError):
            bg_update.install_archive(
                self.plugin_root, archive_path, spec, validate_callback=fail_validation
            )
        self.assertEqual(self.original_active, read_bytes(self.active_path))
        self.assertEqual(self.original_launcher, read_bytes(self.launcher_path))
        self.assertFalse(os.path.exists(os.path.join(self.versions_root, "1.3.8")))

    def test_post_activation_failure_restores_exact_active_file(self):
        archive_path, spec = self._create_archive()

        def fail_after_switch(unused_path, unused_spec):
            raise RuntimeError("launch failed")

        with self.assertRaises(RuntimeError):
            bg_update.install_archive(
                self.plugin_root,
                archive_path,
                spec,
                post_activate_callback=fail_after_switch,
            )
        self.assertEqual(self.original_active, read_bytes(self.active_path))
        self.assertEqual(self.original_launcher, read_bytes(self.launcher_path))
        self.assertFalse(os.path.exists(os.path.join(self.versions_root, "1.3.8")))

    def test_active_replace_failure_rolls_back_new_version(self):
        archive_path, spec = self._create_archive()
        real_replace = bg_update.os.replace
        failed = [False]

        def fail_active_once(source, destination):
            if os.path.normcase(destination) == os.path.normcase(self.active_path) and not failed[0]:
                failed[0] = True
                raise OSError("simulated active switch failure")
            return real_replace(source, destination)

        with mock.patch.object(bg_update.os, "replace", side_effect=fail_active_once):
            with self.assertRaises(OSError):
                bg_update.install_archive(self.plugin_root, archive_path, spec)
        self.assertEqual(self.original_active, read_bytes(self.active_path))
        self.assertEqual(self.original_launcher, read_bytes(self.launcher_path))
        self.assertFalse(os.path.exists(os.path.join(self.versions_root, "1.3.8")))

    def test_unverified_existing_version_is_never_overwritten(self):
        archive_path, spec = self._create_archive()
        target = os.path.join(self.versions_root, "1.3.8")
        os.makedirs(target)
        marker = os.path.join(target, "keep.txt")
        with open(marker, "wb") as stream:
            stream.write(b"user-data")
        with self.assertRaises(bg_update.VersionConflictError):
            bg_update.install_archive(self.plugin_root, archive_path, spec)
        self.assertEqual(b"user-data", read_bytes(marker))

    def test_existing_lock_stops_concurrent_update(self):
        archive_path, spec = self._create_archive()
        lock_path = os.path.join(self.plugin_root, ".bg_update.lock")
        with open(lock_path, "wb") as stream:
            stream.write(b"locked")
        with self.assertRaises(bg_update.UpdateInProgressError):
            bg_update.install_archive(self.plugin_root, archive_path, spec)
        self.assertEqual(b"locked", read_bytes(lock_path))

    def test_live_pid_lock_is_never_reclaimed_even_when_old(self):
        archive_path, spec = self._create_archive()
        lock_path = os.path.join(self.plugin_root, ".bg_update.lock")
        payload = json.dumps(
            {"pid": os.getpid(), "created": 1, "token": "active-owner"},
            sort_keys=True,
        ).encode("ascii")
        with open(lock_path, "wb") as stream:
            stream.write(payload)
        old_time = 1
        os.utime(lock_path, (old_time, old_time))
        with self.assertRaises(bg_update.UpdateInProgressError):
            bg_update.install_archive(self.plugin_root, archive_path, spec)
        self.assertEqual(payload, read_bytes(lock_path))

    def test_dead_pid_lock_is_reclaimed_and_update_completes(self):
        archive_path, spec = self._create_archive()
        lock_path = os.path.join(self.plugin_root, ".bg_update.lock")
        with open(lock_path, "wb") as stream:
            stream.write(
                json.dumps(
                    {"pid": 99999999, "created": 1, "token": "dead-owner"}
                ).encode("ascii")
            )
        with mock.patch.object(bg_update, "_pid_is_running", return_value=False):
            result = bg_update.install_archive(self.plugin_root, archive_path, spec)
        self.assertTrue(result.installed_new)
        self.assertFalse(os.path.exists(lock_path))

    def test_lock_release_does_not_delete_another_owners_lock(self):
        lock_path = os.path.join(self.plugin_root, ".bg_update.lock")
        lock = bg_update._UpdateLock(lock_path)
        lock.__enter__()
        replacement = json.dumps(
            {"pid": os.getpid(), "created": 1, "token": "replacement"},
            sort_keys=True,
        ).encode("ascii")
        with open(lock_path, "wb") as stream:
            stream.write(replacement)
        lock.__exit__(None, None, None)
        self.assertEqual(replacement, read_bytes(lock_path))

    def test_hash_verification_can_be_cancelled(self):
        archive_path, spec = self._create_archive()
        with self.assertRaises(bg_update.UpdateCancelled):
            bg_update.verify_sha256(
                archive_path, spec.archive_sha256, cancelled=lambda: True
            )

    def test_extraction_can_be_cancelled_before_writing(self):
        archive_path, unused_spec = self._create_archive()
        destination = os.path.join(self.temp, "cancelled-extract")
        with self.assertRaises(bg_update.UpdateCancelled):
            bg_update.safe_extract_zip(
                archive_path, destination, cancelled=lambda: True
            )
        self.assertFalse(os.path.exists(destination))

    def test_cancel_after_validation_rolls_back_installed_runtime(self):
        archive_path, spec = self._create_archive()
        state = {"cancelled": False}

        def mark_cancelled(unused_path, unused_spec):
            state["cancelled"] = True

        with self.assertRaises(bg_update.UpdateCancelled):
            bg_update.install_archive(
                self.plugin_root,
                archive_path,
                spec,
                validate_callback=mark_cancelled,
                cancelled=lambda: state["cancelled"],
            )
        self.assertEqual(self.original_active, read_bytes(self.active_path))
        self.assertEqual(self.original_launcher, read_bytes(self.launcher_path))
        self.assertFalse(os.path.exists(os.path.join(self.versions_root, "1.3.8")))

    def test_cancel_after_bootstrap_sync_restores_original_launcher(self):
        archive_path, spec = self._create_archive()
        real_atomic_write = bg_update._atomic_write_bytes
        state = {"cancelled": False}

        def write_then_cancel(path, payload):
            real_atomic_write(path, payload)
            if os.path.normcase(path) == os.path.normcase(self.launcher_path):
                state["cancelled"] = True

        with mock.patch.object(bg_update, "_atomic_write_bytes", side_effect=write_then_cancel):
            with self.assertRaises(bg_update.UpdateCancelled):
                bg_update.install_archive(
                    self.plugin_root,
                    archive_path,
                    spec,
                    cancelled=lambda: state["cancelled"],
                )
        self.assertEqual(self.original_active, read_bytes(self.active_path))
        self.assertEqual(self.original_launcher, read_bytes(self.launcher_path))
        self.assertFalse(os.path.exists(os.path.join(self.versions_root, "1.3.8")))

    def test_invalid_version_cannot_escape_versions_directory(self):
        with self.assertRaises(ValueError):
            bg_update.UpdateSpec("../outside", "0" * 64)


class GithubApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.mkdtemp(prefix="bg-github-tests-")

    def tearDown(self):
        shutil.rmtree(self.temp)

    def test_private_contents_manifest_decodes_base64_and_adds_bearer(self):
        manifest = {
            "version": "1.3.8",
            "package_version": "1.3.8",
            "sha256": "a" * 64,
            "tag": "v1.3.8",
            "asset_name": "update.zip",
        }
        encoded_manifest = base64.b64encode(json.dumps(manifest).encode("utf-8")).decode("ascii")
        encoded_manifest = "\n".join(
            encoded_manifest[index:index + 20] for index in range(0, len(encoded_manifest), 20)
        )
        api_payload = json.dumps(
            {
                "encoding": "base64",
                "content": encoded_manifest,
            }
        ).encode("utf-8")
        opener = FakeOpener(
            [FakeResponse(api_payload, "https://api.github.com/repos/acme/private/contents/update.json")]
        )
        spec = bg_update.load_update_spec_from_github(
            "acme", "private", "updates/update.json", token="secret-token", opener=opener
        )
        self.assertEqual("1.3.8", spec.version)
        self.assertEqual("Bearer secret-token", opener.requests[0].get_header("Authorization"))
        self.assertNotIn("secret-token", repr(spec.__slots__))

    def test_release_asset_is_selected_by_tag_and_exact_name(self):
        payload = b"private release bytes"
        checksum = hashlib.sha256(payload).hexdigest()
        release = {
            "assets": [
                {
                    "name": "update.zip",
                    "url": "https://api.github.com/repos/acme/private/releases/assets/123",
                    "size": len(payload),
                }
            ]
        }
        opener = FakeOpener(
            [
                FakeResponse(
                    json.dumps(release).encode("utf-8"),
                    "https://api.github.com/repos/acme/private/releases/tags/v1.3.8",
                ),
                FakeResponse(payload, "https://release-assets.githubusercontent.com/signed-object"),
            ]
        )
        destination = os.path.join(self.temp, "update.zip")
        bg_update.download_github_release_asset(
            "acme",
            "private",
            "v1.3.8",
            "update.zip",
            destination,
            checksum,
            token="secret-token",
            opener=opener,
        )
        self.assertEqual(payload, read_bytes(destination))
        self.assertEqual("Bearer secret-token", opener.requests[0].get_header("Authorization"))
        self.assertEqual("Bearer secret-token", opener.requests[1].get_header("Authorization"))
        self.assertEqual("application/octet-stream", opener.requests[1].get_header("Accept"))

    def test_cancelled_download_closes_response_and_removes_partial_file(self):
        payload = b"download body"
        checksum = hashlib.sha256(payload).hexdigest()
        release = {
            "assets": [
                {
                    "name": "update.zip",
                    "url": "https://api.github.com/repos/acme/private/releases/assets/123",
                    "size": len(payload),
                }
            ]
        }
        metadata_response = FakeResponse(
            json.dumps(release).encode("utf-8"),
            "https://api.github.com/repos/acme/private/releases/tags/v1.3.8",
        )
        asset_response = FakeResponse(
            payload, "https://release-assets.githubusercontent.com/signed-object"
        )
        opener = FakeOpener([metadata_response, asset_response])
        calls = [0]

        def cancel_after_first_asset_chunk():
            calls[0] += 1
            return calls[0] >= 7

        destination = os.path.join(self.temp, "cancelled.zip")
        with self.assertRaises(bg_update.UpdateCancelled):
            bg_update.download_github_release_asset(
                "acme",
                "private",
                "v1.3.8",
                "update.zip",
                destination,
                checksum,
                opener=opener,
                cancelled=cancel_after_first_asset_chunk,
            )
        self.assertTrue(metadata_response.closed)
        self.assertTrue(asset_response.closed)
        self.assertFalse(os.path.exists(destination))
        self.assertFalse(any(name.startswith("cancelled.zip.part-") for name in os.listdir(self.temp)))

    def test_non_github_or_http_urls_are_rejected(self):
        with self.assertRaises(bg_update.GithubApiError):
            bg_update._validate_github_url("http://api.github.com/example")
        with self.assertRaises(bg_update.GithubApiError):
            bg_update._validate_github_url("https://example.com/update.zip")

    def test_token_with_newline_is_rejected(self):
        with self.assertRaises(ValueError):
            bg_update._github_headers("secret\nInjected: value")


if __name__ == "__main__":
    unittest.main()
