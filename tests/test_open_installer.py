"""Filesystem-only installer tests; never use an actual Maya scripts folder."""
import hashlib
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import types
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('open_installer', ROOT / 'installer' / '安装到Maya.py')
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


def write_manifest(package):
    lines = []
    for path in sorted(package.rglob('*')):
        if path.is_file() and path.name != 'SHA256.txt':
            lines.append('{}  {}'.format(hashlib.sha256(path.read_bytes()).hexdigest(), path.relative_to(package).as_posix()))
    (package / 'SHA256.txt').write_text('\n'.join(lines) + '\n', encoding='utf-8')


def make_package(package):
    payload = package / 'Bake_Groups'
    payload.mkdir(parents=True)
    for name in installer.REQUIRED_FILES:
        (payload / name).write_text('__version__ = "1.0.0"\n' if name == 'bg_version.py' else '# open fixture\n', encoding='utf-8')
    for year in installer.SUPPORTED_MAYA:
        native = payload / 'bin' / year / 'bg_math_core.pyd'
        native.parent.mkdir(parents=True)
        native.write_bytes(b'open native fixture')
    (package / 'README.md').write_text('Open release fixture', encoding='utf-8')
    write_manifest(package)
    return payload


class OpenInstallerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='bake-open-installer-test-')
        self.root = Path(self.temporary.name)
        self.package = self.root / 'package'
        self.payload = make_package(self.package)
        self.scripts = self.root / 'maya' / 'scripts'
        self.scripts.mkdir(parents=True)
        self.target = self.scripts / 'Bake_Groups'

    def tearDown(self):
        self.temporary.cleanup()

    def old_install(self):
        old = self.target / 'versions' / '1.5.2'
        old.mkdir(parents=True)
        (old / 'bg_main_window.py').write_bytes(b'old runtime')
        (self.target / 'active_version.json').write_text('{"active_version":"1.5.2"}', encoding='utf-8')

    def install(self, modules=None):
        return installer.install_package(str(self.package), str(self.scripts), loaded_modules={} if modules is None else modules)

    def assert_no_transaction_files(self):
        self.assertFalse(list(self.scripts.glob('Bake_Groups.install*')))
        self.assertFalse(list(self.scripts.glob('Bake_Groups.backup-*')))

    def test_fresh_install_contains_exact_flat_payload(self):
        result = self.install()
        self.assertEqual(result['target'], str(self.target))
        self.assertEqual(set(installer._files(str(self.payload))), set(installer._files(str(self.target))))
        for name, source in installer._files(str(self.payload)).items():
            self.assertEqual(Path(source).read_bytes(), (self.target / name).read_bytes())
        self.assert_no_transaction_files()

    def test_upgrade_old_higher_version_preserves_neighboring_user_files(self):
        self.old_install()
        scene = self.root / 'projects' / 'artist.ma'
        scene.parent.mkdir()
        scene.write_bytes(b'artist scene fixture')
        license_record = self.root / 'appdata' / 'license.json'
        license_record.parent.mkdir()
        license_record.write_bytes(b'synthetic user record; not an actual license')
        other = self.scripts / 'userSetup.py'
        other.write_bytes(b'# user script')
        self.install()
        self.assertFalse((self.target / 'versions').exists())
        self.assertFalse((self.target / 'active_version.json').exists())
        self.assertIn('1.0.0', (self.target / 'bg_version.py').read_text())
        self.assertEqual(scene.read_bytes(), b'artist scene fixture')
        self.assertEqual(license_record.read_bytes(), b'synthetic user record; not an actual license')
        self.assertEqual(other.read_bytes(), b'# user script')
        self.assert_no_transaction_files()

    def test_failure_replacing_target_restores_entire_old_directory(self):
        self.old_install()
        before = {name: Path(path).read_bytes() for name, path in installer._files(str(self.target)).items()}
        real_replace = installer.os.replace
        def fail_commit(source, target):
            if Path(source).name.startswith('Bake_Groups.install-'):
                raise OSError('simulated commit failure')
            return real_replace(source, target)
        with mock.patch.object(installer.os, 'replace', side_effect=fail_commit):
            with self.assertRaisesRegex(OSError, 'commit failure'):
                self.install()
        after = {name: Path(path).read_bytes() for name, path in installer._files(str(self.target)).items()}
        self.assertEqual(after, before)
        self.assert_no_transaction_files()

    def test_failed_rollback_retains_recoverable_backup(self):
        self.old_install()
        real_replace = installer.os.replace
        def fail_publish_and_restore(source, target):
            if Path(source).name.startswith(('Bake_Groups.install-', 'Bake_Groups.backup-')):
                raise OSError('simulated locked destination')
            return real_replace(source, target)
        with mock.patch.object(installer.os, 'replace', side_effect=fail_publish_and_restore):
            with self.assertRaisesRegex(installer.InstallError, 'restore the previous files'):
                self.install()
        backups = list(self.scripts.glob('Bake_Groups.backup-*'))
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / 'versions/1.5.2/bg_main_window.py').read_bytes(), b'old runtime')

    def test_staging_failure_never_touches_old_install(self):
        self.old_install()
        with mock.patch.object(installer.shutil, 'copy2', side_effect=OSError('copy failed')):
            with self.assertRaisesRegex(OSError, 'copy failed'):
                self.install()
        self.assertEqual((self.target / 'versions/1.5.2/bg_main_window.py').read_bytes(), b'old runtime')
        self.assert_no_transaction_files()

    def test_checksum_corruption_stops_before_creating_target(self):
        (self.payload / 'launcher.py').write_bytes(b'changed after manifest')
        with self.assertRaisesRegex(installer.InstallError, 'checksum failed'):
            self.install()
        self.assertFalse(self.target.exists())
        self.assert_no_transaction_files()

    def test_missing_native_even_with_updated_checksums_is_rejected(self):
        (self.payload / 'bin/2024/bg_math_core.pyd').unlink()
        write_manifest(self.package)
        with self.assertRaisesRegex(installer.InstallError, 'incomplete'):
            self.install()
        self.assertFalse(self.target.exists())

    def test_unsafe_manifest_paths_cannot_reach_outside_package(self):
        sentinel = self.root / 'outside.txt'
        sentinel.write_bytes(b'must remain')
        for bad in ('../outside.txt', '/outside.txt', 'C:/outside.txt',
                    'Bake_Groups/../escape.py', 'Bake_Groups\\escape.py',
                    'Bake_Groups/CON.py', 'Bake_Groups/name. '):
            with self.subTest(path=bad):
                write_manifest(self.package)
                with (self.package / 'SHA256.txt').open('a', encoding='utf-8') as stream:
                    stream.write('0' * 64 + '  ' + bad + '\n')
                with self.assertRaises(installer.InstallError):
                    self.install()
                self.assertEqual(sentinel.read_bytes(), b'must remain')
                self.assertFalse(self.target.exists())

    def test_duplicate_casefold_manifest_path_is_rejected(self):
        with (self.package / 'SHA256.txt').open('a', encoding='utf-8') as stream:
            stream.write('0' * 64 + '  bake_groups/LAUNCHER.py\n')
        with self.assertRaisesRegex(installer.InstallError, 'Duplicate'):
            self.install()

    def test_loaded_native_requires_restart_before_touching_old_directory(self):
        self.old_install()
        loaded = types.SimpleNamespace(__file__=str(self.target / 'versions/1.5.2/bin/2027/bg_math_core.pyd'))
        with self.assertRaisesRegex(installer.InstallError, '重启 Maya'):
            self.install({'bg_math_core': loaded})
        self.assertTrue((self.target / 'active_version.json').is_file())
        self.assert_no_transaction_files()

    def test_artist_scene_inside_install_folder_blocks_replacement(self):
        self.old_install()
        scene = self.target / '__pycache__' / 'artist.ma'
        scene.parent.mkdir()
        scene.write_bytes(b'do not delete')
        with self.assertRaisesRegex(installer.InstallError, 'Maya scenes'):
            self.install()
        self.assertEqual(scene.read_bytes(), b'do not delete')

    def test_target_symlink_or_windows_junction_is_not_followed(self):
        outside = self.root / 'artist-files'
        outside.mkdir()
        (outside / 'keep.txt').write_bytes(b'untouched')
        try:
            self.target.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            if os.name != 'nt':
                self.skipTest('Symlink privilege is unavailable: ' + str(exc))
            environment = dict(os.environ, BAKE_TEST_LINK=str(self.target), BAKE_TEST_TARGET=str(outside))
            subprocess.run(['powershell', '-NoProfile', '-Command',
                            '$ErrorActionPreference = "Stop"; New-Item -ItemType Junction -Path $env:BAKE_TEST_LINK -Target $env:BAKE_TEST_TARGET | Out-Null'],
                           env=environment, check=True, capture_output=True)
        try:
            with self.assertRaisesRegex(installer.InstallError, 'links and junctions'):
                self.install()
            self.assertEqual((outside / 'keep.txt').read_bytes(), b'untouched')
        finally:
            # Remove only the temporary directory link, never its target tree.
            os.rmdir(str(self.target))

    def test_legacy_payload_is_not_mistaken_for_open_package(self):
        (self.payload / 'bg_license.py').write_bytes(b'legacy fixture')
        write_manifest(self.package)
        with self.assertRaisesRegex(installer.InstallError, 'open-source flat'):
            self.install()


if __name__ == '__main__':
    unittest.main()
