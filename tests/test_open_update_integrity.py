"""Independent updater boundary tests using disposable installations and ZIPs."""
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from urllib import request
import zipfile


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('integrity_updater', ROOT / 'src/Bake_Groups/bg_update.py')
updater = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(updater)
INSTALLER_SPEC = importlib.util.spec_from_file_location('integrity_installer', ROOT / 'installer/安装到Maya.py')
installer = importlib.util.module_from_spec(INSTALLER_SPEC)
INSTALLER_SPEC.loader.exec_module(installer)


def native_fixture():
    data = bytearray(512)
    data[:2] = b'MZ'
    struct.pack_into('<I', data, 0x3C, 128)
    data[128:132] = b'PE\0\0'
    struct.pack_into('<H', data, 132, 0x8664)
    return bytes(data)


def runtime_files(version):
    files = {name: b'# Public geometry fixture\n' for name in updater.REQUIRED}
    files['bg_version.py'] = ('__version__ = "' + version + '"\n').encode('ascii')
    files['bg_core.py'] = ('# Runtime ' + version + '\n').encode('ascii')
    files.update({name: native_fixture() for name in updater.NATIVE_PATHS})
    return files


def release_zip(version, corrupt_member=False):
    files = {'Bake_Groups/' + name: data for name, data in runtime_files(version).items()}
    files['README.md'] = b'Public release fixture\n'
    manifest = '\n'.join(hashlib.sha256(data).hexdigest() + '  ' + name
                         for name, data in sorted(files.items())) + '\n'
    if corrupt_member:
        files['Bake_Groups/bg_core.py'] = b'# Changed after manifest creation\n'
    result = io.BytesIO()
    with zipfile.ZipFile(result, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
        archive.writestr('SHA256.txt', manifest.encode('utf-8'))
    return result.getvalue()


class UpdateIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='bake-update-integrity-')
        self.root = Path(self.temporary.name)
        self.runtime = self.root / 'scripts/Bake_Groups'
        for name, data in runtime_files('1.0.1').items():
            target = self.runtime / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        self.cache = self.runtime.parent / 'Bake_Groups.update'

    def tearDown(self):
        self.temporary.cleanup()

    def snapshot(self):
        return {path.relative_to(self.runtime).as_posix(): path.read_bytes()
                for path in self.runtime.rglob('*') if path.is_file()}

    def stage(self, corrupt_member=False):
        content = release_zip('1.0.2', corrupt_member=corrupt_member)
        asset = 'Bake_Master_1.0.2_Windows_x64.zip'
        checksum = (hashlib.sha256(content).hexdigest() + '  ' + asset + '\n').encode('ascii')
        def download(url, destination=None, limit=None, cancelled=None):
            if url.endswith('.sha256'):
                return checksum
            self.assertTrue(url.endswith('/' + asset))
            Path(destination).write_bytes(content)
            return len(content)
        with mock.patch.object(updater, '_download', side_effect=download):
            return updater.download_and_stage({'version': '1.0.2'}, str(self.runtime))

    def test_stale_stage_cannot_downgrade_a_newer_manual_install(self):
        self.stage()
        # Model a completed manual upgrade after this older update was staged.
        for name, data in runtime_files('1.0.3').items():
            (self.runtime / name).write_bytes(data)
        before = self.snapshot()
        updater.apply_pending(str(self.runtime), loaded_modules={})
        self.assertEqual(self.snapshot(), before)
        self.assertIsNone(updater.pending_update(str(self.runtime)))

    def test_apply_respects_the_drag_and_drop_installation_lock(self):
        self.stage()
        before = self.snapshot()
        lock = self.runtime.parent / 'Bake_Groups.install.lock'
        with installer._install_lock(str(self.runtime.parent)):
            with self.assertRaises(updater.UpdateError):
                updater.apply_pending(str(self.runtime), loaded_modules={})
            self.assertEqual(self.snapshot(), before)
            self.assertTrue(lock.is_file())
            self.assertIsNotNone(updater.pending_update(str(self.runtime)))
        self.assertEqual(updater.apply_pending(str(self.runtime), loaded_modules={})['status'], 'applied')

    def test_process_exit_releases_the_shared_lock_for_a_later_update(self):
        self.stage()
        script = (
            'import importlib.util, os, sys\n'
            'spec = importlib.util.spec_from_file_location("lock_fixture", sys.argv[1])\n'
            'installer = importlib.util.module_from_spec(spec)\n'
            'spec.loader.exec_module(installer)\n'
            'with installer._install_lock(sys.argv[2]):\n'
            '    os._exit(0)\n'
        )
        subprocess.run([sys.executable, '-c', script, str(ROOT / 'installer/安装到Maya.py'),
                        str(self.runtime.parent)], check=True, timeout=20,
                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.assertTrue((self.runtime.parent / 'Bake_Groups.install.lock').is_file())
        self.assertEqual(updater.apply_pending(str(self.runtime), loaded_modules={})['status'], 'applied')

    def test_valid_outer_checksum_does_not_override_bad_inner_manifest(self):
        before = self.snapshot()
        with self.assertRaisesRegex(updater.UpdateError, 'checksum failed'):
            self.stage(corrupt_member=True)
        self.assertEqual(self.snapshot(), before)
        self.assertIsNone(updater.pending_update(str(self.runtime)))
        self.assertFalse(list(self.cache.glob('stage-*')))

    def test_runtime_directory_link_is_not_followed_or_replaced(self):
        self.stage()
        outside = self.root / 'artist-files'
        outside.mkdir()
        (outside / 'scene.ma').write_bytes(b'artist fixture')
        link = self.runtime / 'linked-artist-folder'
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            if os.name != 'nt':
                self.skipTest(str(exc))
            environment = dict(os.environ, BAKE_TEST_LINK=str(link), BAKE_TEST_TARGET=str(outside))
            subprocess.run(['powershell', '-NoProfile', '-Command',
                            '$ErrorActionPreference = "Stop"; New-Item -ItemType Junction -Path $env:BAKE_TEST_LINK -Target $env:BAKE_TEST_TARGET | Out-Null'],
                           env=environment, check=True, capture_output=True)
        try:
            with self.assertRaisesRegex(updater.UpdateError, 'links or junctions'):
                updater.apply_pending(str(self.runtime), loaded_modules={})
            self.assertEqual((outside / 'scene.ma').read_bytes(), b'artist fixture')
            self.assertIn(b'1.0.1', (self.runtime / 'bg_version.py').read_bytes())
        finally:
            os.rmdir(str(link))

    def test_redirects_reject_untrusted_hosts_schemes_and_repository_paths(self):
        handler = updater._SafeRedirect()
        source = request.Request(updater.API_URL)
        urls = ('http://github.com/' + updater.REPOSITORY + '/releases/download/v1.0.2/file.zip',
                'https://github.com.evil.invalid/package.zip',
                'https://github.com/another-owner/another-project/releases/download/v1.0.2/file.zip',
                'https://github.com@evil.invalid/package.zip',
                'file:///tmp/package.zip',
                'https://release-assets.githubusercontent.com:8443/file.zip')
        for url in urls:
            with self.subTest(url=url), self.assertRaises(updater.UpdateError):
                handler.redirect_request(source, None, 302, 'Found', {}, url)
        approved = 'https://release-assets.githubusercontent.com/github-production-release-asset/fixture?sig=public-test'
        self.assertEqual(handler.redirect_request(source, None, 302, 'Found', {}, approved).full_url, approved)

    def test_download_rechecks_final_response_url_before_writing(self):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.geturl.return_value = 'https://untrusted.invalid/package.zip'
        opener = mock.Mock()
        opener.open.return_value = response
        destination = self.root / 'should-not-be-written.zip'
        with mock.patch.object(updater.request, 'build_opener', return_value=opener):
            with self.assertRaises(updater.UpdateError):
                updater._download(updater.API_URL, str(destination))
        self.assertFalse(destination.exists())
        response.read.assert_not_called()


if __name__ == '__main__':
    unittest.main()
