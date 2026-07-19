import base64
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


try:
    from unittest import mock
except ImportError:  # pragma: no cover
    import mock


PLUGIN_ROOT = os.path.join(os.path.dirname(__file__), "plugin", "Bake_Groups")


def runtime_under_test():
    active_path = os.path.join(PLUGIN_ROOT, "active_version.json")
    with io.open(active_path, "r", encoding="utf-8-sig") as stream:
        active_version = str(json.load(stream).get("active_version") or "")
    for version in ("1.3.14", active_version):
        candidate = os.path.join(PLUGIN_ROOT, "versions", version)
        if version and os.path.isfile(os.path.join(candidate, "bg_license.py")):
            return candidate
    raise RuntimeError("No Bake Master runtime is available for license tests")


RUNTIME = runtime_under_test()
sys.path.insert(0, RUNTIME)
import bg_license

if os.path.normcase(os.path.dirname(os.path.realpath(bg_license.__file__))) != os.path.normcase(
    os.path.realpath(RUNTIME)
):
    raise RuntimeError("License tests imported the wrong Bake Master runtime")


def read_bytes(path):
    with open(path, "rb") as stream:
        return stream.read()


class FakeResponse(io.BytesIO):
    pass


class LicenseTests(unittest.TestCase):
    def setUp(self):
        self.private = Ed25519PrivateKey.generate()
        self.public = self.private.public_key().public_bytes_raw()
        self.old_key = bg_license.PUBLIC_KEY_B64
        self.old_config = bg_license._config
        self.old_session = bg_license._SESSION
        bg_license._config = lambda: {}
        bg_license.PUBLIC_KEY_B64 = base64.b64encode(self.public).decode("ascii")
        bg_license._SESSION = None
        self.tempdir = tempfile.mkdtemp(prefix="bg-license-test-")

    def tearDown(self):
        bg_license.PUBLIC_KEY_B64 = self.old_key
        bg_license._config = self.old_config
        bg_license._SESSION = self.old_session
        shutil.rmtree(self.tempdir)

    def license_data(self, license_id="TEST"):
        unsigned = {
            "schema_version": 1,
            "product_id": "bake-master",
            "product": "Bake Master",
            "license_id": license_id,
            "machine_hash": bg_license.machine_hash(),
            "issued_at": 1,
            "expires_at": None,
            "offline_days": 14,
            "native_proof": "a" * 64,
        }
        signed = dict(unsigned)
        signed["signature"] = base64.b64encode(
            self.private.sign(bg_license._canonical_json(unsigned))
        ).decode("ascii")
        return signed

    def test_signature_and_machine_binding(self):
        data = self.license_data()
        self.assertTrue(bg_license.verify_license_payload(data))
        data["machine_hash"] = "0" * 64
        with self.assertRaises(bg_license.LicenseError):
            bg_license.verify_license_payload(data)

    def test_dpapi_save_load_and_online_success(self):
        path = os.path.join(self.tempdir, "license.dat")
        data = self.license_data()
        bg_license.save_license(data, path)
        original_fetch = bg_license._fetch_status
        try:
            bg_license._fetch_status = lambda timeout: {"online": True, "revoked_ids": []}
            result = bg_license.validate(path)
        finally:
            bg_license._fetch_status = original_fetch
        self.assertTrue(result["online"])

    def test_revocation_blocks_online_start(self):
        path = os.path.join(self.tempdir, "license.dat")
        data = self.license_data("REVOKED")
        bg_license.save_license(data, path)
        original_fetch = bg_license._fetch_status
        try:
            bg_license._fetch_status = lambda timeout: {"online": True, "revoked_ids": ["REVOKED"]}
            with self.assertRaises(bg_license.LicenseError):
                bg_license.validate(path)
        finally:
            bg_license._fetch_status = original_fetch

    def test_native_handshake_payload(self):
        captured = {}
        class NativeStub(object):
            def activate_signed_license(self, product_id, license_id, machine, expires_at, proof):
                captured.update({"product_id": product_id, "license_id": license_id, "machine": machine, "expires_at": expires_at, "proof": proof})
                return True
        old_import = __import__("builtins").__import__
        def fake_import(name, *args, **kwargs):
            if name == "bg_math_core":
                return NativeStub()
            return old_import(name, *args, **kwargs)
        __import__("builtins").__import__ = fake_import
        try:
            data = self.license_data("HANDSHAKE")
            bg_license._activate_session(data)
        finally:
            __import__("builtins").__import__ = old_import
        self.assertEqual(captured, {
            "product_id": "bake-master",
            "license_id": "HANDSHAKE",
            "machine": bg_license.machine_hash(),
            "expires_at": 0,
            "proof": "a" * 64,
        })

    def test_status_response_is_always_closed(self):
        unsigned_status = {
            "schema_version": 1,
            "products": {"bake-master": {"revoked": []}},
        }
        signed_status = dict(unsigned_status)
        signed_status["signature"] = base64.b64encode(
            self.private.sign(bg_license._canonical_json(unsigned_status))
        ).decode("ascii")
        response = FakeResponse(json.dumps(signed_status).encode("utf-8"))
        with mock.patch.object(bg_license, "_status_url", return_value="https://example.invalid/status"):
            with mock.patch.object(bg_license._urlrequest, "urlopen", return_value=response):
                result = bg_license._fetch_status(1)
        self.assertTrue(result["online"])
        self.assertTrue(response.closed)

    def test_failed_atomic_save_preserves_existing_license(self):
        path = os.path.join(self.tempdir, "license.dat")
        bg_license.save_license(self.license_data("ORIGINAL"), path)
        original = read_bytes(path)
        with mock.patch.object(bg_license.os, "replace", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                bg_license.save_license(self.license_data("REPLACEMENT"), path)
        self.assertEqual(original, read_bytes(path))
        self.assertFalse(any(".tmp-" in name for name in os.listdir(self.tempdir)))

    def test_failed_online_timestamp_write_preserves_license(self):
        path = os.path.join(self.tempdir, "license.dat")
        bg_license.save_license(self.license_data(), path)
        original = read_bytes(path)
        with mock.patch.object(
            bg_license, "_fetch_status", return_value={"online": True, "revoked_ids": []}
        ):
            with mock.patch.object(bg_license.os, "replace", side_effect=OSError("disk failure")):
                with self.assertRaises(OSError):
                    bg_license.validate(path)
        self.assertEqual(original, read_bytes(path))
        self.assertFalse(any(".tmp-" in name for name in os.listdir(self.tempdir)))

    def test_native_activation_failure_does_not_leave_python_session(self):
        class NativeStub(object):
            def activate_signed_license(self, *unused_args):
                return False

        old_import = __import__("builtins").__import__

        def fake_import(name, *args, **kwargs):
            if name == "bg_math_core":
                return NativeStub()
            return old_import(name, *args, **kwargs)

        __import__("builtins").__import__ = fake_import
        try:
            with self.assertRaises(bg_license.LicenseRuntimeError):
                bg_license._activate_session(self.license_data("NATIVE-FAIL"))
        finally:
            __import__("builtins").__import__ = old_import
        self.assertIsNone(bg_license._SESSION)


if __name__ == "__main__":
    unittest.main()
