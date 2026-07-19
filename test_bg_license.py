import json
import os
import tempfile
import unittest

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

RUNTIME = os.path.join(
    os.path.dirname(__file__), "plugin", "Bake_Groups", "versions", "1.3.12"
)
import sys
sys.path.insert(0, RUNTIME)
import bg_license


class LicenseTests(unittest.TestCase):
    def setUp(self):
        self.private = Ed25519PrivateKey.generate()
        self.public = self.private.public_key().public_bytes_raw()
        self.old_key = bg_license.PUBLIC_KEY_B64
        self.old_config = bg_license._config
        bg_license._config = lambda: {}
        bg_license.PUBLIC_KEY_B64 = __import__("base64").b64encode(self.public).decode("ascii")
        self.tempdir = tempfile.mkdtemp(prefix="bg-license-test-")

    def tearDown(self):
        bg_license.PUBLIC_KEY_B64 = self.old_key
        bg_license._config = self.old_config

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
        signed["signature"] = __import__("base64").b64encode(
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


if __name__ == "__main__":
    unittest.main()
