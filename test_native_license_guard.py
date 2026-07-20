from __future__ import print_function

import hashlib
import hmac
import os
import sys
import time


runtime_bin = os.environ["BG_NATIVE_TEST_BIN"]
key_path = os.environ.get(
    "BG_NATIVE_GUARD_KEY_FILE",
    r"D:\Bake_Groups_License_Keys\native_guard.key",
)
sys.path.insert(0, runtime_bin)

import bg_math_core


def proof(license_id, machine, expires_at):
    with open(key_path, "r") as stream:
        key = bytes.fromhex(stream.read().strip())
    message = "\n".join(
        (
            "BakeMaster:NativeLicense:v1",
            "bake-master",
            license_id,
            machine,
            str(expires_at),
        )
    ).encode("utf-8")
    return hmac.new(key, message, hashlib.sha256).hexdigest()


machine = bg_math_core.machine_hash()
license_id = "BMA-NATIVE-ABI-TEST"
future = int(time.time()) + 3600
assert bg_math_core.activate_signed_license(
    "bake-master", license_id, machine, future, proof(license_id, machine, future)
)
bg_math_core.require_authorized()
bg_math_core.deactivate_license()
assert not bg_math_core.is_authorized()
try:
    bg_math_core.require_authorized()
    raise AssertionError("deactivated native guard accepted an authorized call")
except RuntimeError:
    pass
try:
    bg_math_core.PointCloudIndex([0.0, 0.0, 0.0])
    raise AssertionError("deactivated native guard accepted a point-cloud index")
except RuntimeError:
    pass

short_expiry = int(time.time()) + 1
assert bg_math_core.activate_signed_license(
    "bake-master", license_id, machine, short_expiry,
    proof(license_id, machine, short_expiry),
)
expiring_index = bg_math_core.PointCloudIndex([0.0, 0.0, 0.0])
while int(time.time()) < short_expiry:
    time.sleep(0.02)
try:
    bg_math_core.require_authorized()
    raise AssertionError("expired native guard accepted an authorized call")
except RuntimeError:
    pass
try:
    expiring_index.average_distance([0.0, 0.0, 0.0])
    raise AssertionError("expired native guard accepted an indexed distance query")
except RuntimeError:
    pass
assert not bg_math_core.is_authorized()

assert bg_math_core.activate_signed_license(
    "bake-master", license_id, machine, future, proof(license_id, machine, future)
)
assert not bg_math_core.activate_signed_license(
    "bake-master", license_id, machine, future, "0" * 64
)
assert not bg_math_core.is_authorized()
expired = int(time.time()) - 60
assert not bg_math_core.activate_signed_license(
    "bake-master", license_id, machine, expired, proof(license_id, machine, expired)
)
print("Native guard ABI test passed: {}".format(sys.version.split()[0]))
