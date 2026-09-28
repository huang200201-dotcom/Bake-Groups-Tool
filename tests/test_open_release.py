"""Build small fixture releases and verify real ZIP/installer interoperability."""
import hashlib
import importlib.util
from pathlib import Path
import shutil
import struct
import tempfile
import unittest
from unittest import mock
import zipfile


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('open_release', ROOT / 'tools' / 'build-release.py')
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)
VERSION = builder._version(ROOT / 'src/Bake_Groups/bg_version.py')
ASSET_NAME = 'Bake_Master_{}_Windows_x64.zip'.format(VERSION)


def pe_fixture(marker=b''):
    content = bytearray(512)
    content[:2] = b'MZ'
    struct.pack_into('<I', content, 0x3C, 128)
    content[128:132] = b'PE\0\0'
    struct.pack_into('<H', content, 132, 0x8664)
    return bytes(content) + marker


class OpenReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='bake-open-release-test-')
        self.repo = Path(self.temporary.name) / 'repo'
        source = self.repo / 'src/Bake_Groups'
        source.mkdir(parents=True)
        for name in builder.REQUIRED_SOURCE:
            (source / name).write_text('__version__ = "{}"\n'.format(VERSION) if name == 'bg_version.py' else '# fixture source\n', encoding='utf-8')
        for name in ('LICENSE', 'README.md', 'THIRD_PARTY_NOTICES.md', 'CHANGELOG.md'):
            (self.repo / name).write_text('Release document fixture\n', encoding='utf-8')
        (self.repo / 'docs').mkdir()
        for name in ('usage.md', 'building.md'):
            (self.repo / 'docs' / name).write_text('Usage and build fixture\n', encoding='utf-8')
        (self.repo / 'installer').mkdir()
        shutil.copy2(ROOT / 'installer/安装到Maya.py', self.repo / 'installer/安装到Maya.py')
        (self.repo / 'installer/安装说明.txt').write_text('Bake Master {}\n'.format(VERSION), encoding='utf-8')
        (self.repo / 'native').mkdir()
        (self.repo / 'native/bg_math_core.cpp').write_text('// open native fixture\n', encoding='utf-8')
        (self.repo / 'tools').mkdir()
        (self.repo / 'tools/build-native.ps1').write_text('Write-Output "fixture"\n', encoding='ascii')
        for year in builder.SUPPORTED_MAYA:
            path = self.repo / 'build/native' / year / builder.NATIVE_NAME
            path.parent.mkdir(parents=True)
            path.write_bytes(pe_fixture())

    def tearDown(self):
        self.temporary.cleanup()

    def build(self, version=VERSION):
        return builder.build_release(self.repo, version)

    def test_complete_zip_has_six_open_binaries_source_license_and_valid_manifest(self):
        result = self.build()
        archive = Path(result['archive'])
        self.assertEqual(archive.name, ASSET_NAME)
        self.assertEqual(result['sha256'], hashlib.sha256(archive.read_bytes()).hexdigest())
        with zipfile.ZipFile(archive) as package:
            self.assertIsNone(package.testzip())
            names = package.namelist()
            self.assertEqual(len(names), len(set(names)))
            self.assertEqual(len([name for name in names if name.endswith('.pyd')]), 6)
            for name in ('LICENSE', 'THIRD_PARTY_NOTICES.md', 'README.md', 'CHANGELOG.md',
                         'docs/usage.md', 'docs/building.md', 'SHA256.txt', 'Bake_Groups/bg_update.py',
                         '安装到Maya.py', 'source/native/bg_math_core.cpp', 'source/tools/build-native.ps1'):
                self.assertIn(name, names)
            self.assertIn(('/tree/v' + VERSION).encode('ascii'), package.read('SOURCE_CODE.md'))
            self.assertFalse(any('/versions/' in name for name in names))
            extracted = self.repo.parent / 'extracted'
            package.extractall(extracted)
        installer = builder._load_installer(self.repo)
        payload = installer.validate_package(str(extracted))
        self.assertIn('bin/2027/bg_math_core.pyd', payload)
        scripts = self.repo.parent / 'scripts'
        legacy = scripts / 'Bake_Groups/versions/1.5.2'
        legacy.mkdir(parents=True)
        (legacy / 'old.py').write_bytes(b'old')
        installer.install_package(str(extracted), str(scripts), loaded_modules={})
        self.assertTrue((scripts / 'Bake_Groups/launcher.py').is_file())
        self.assertTrue((scripts / 'Bake_Groups/bg_update.py').is_file())
        self.assertFalse((scripts / 'Bake_Groups/versions').exists())

    def test_missing_public_updater_stops_before_publishing(self):
        (self.repo / 'src/Bake_Groups/bg_update.py').unlink()
        with self.assertRaisesRegex(ValueError, 'Missing source files: bg_update.py'):
            self.build()
        self.assertFalse((self.repo / 'dist').exists())

    def test_missing_one_native_stops_before_publishing(self):
        (self.repo / 'build/native/2024/bg_math_core.pyd').unlink()
        with self.assertRaisesRegex(ValueError, 'Missing native'):
            self.build()
        self.assertFalse((self.repo / 'dist').exists())

    def test_licensed_native_is_rejected_without_dumping_binary(self):
        (self.repo / 'build/native/2027/bg_math_core.pyd').write_bytes(pe_fixture(b'activate_signed_license'))
        with self.assertRaisesRegex(ValueError, 'Old licensed native'):
            self.build()

    def test_non_windows_or_wrong_arch_native_is_rejected(self):
        path = self.repo / 'build/native/2027/bg_math_core.pyd'
        for value in (b'not a binary', pe_fixture()[:132] + b'\x4c\x01' + pe_fixture()[134:]):
            with self.subTest(value=value[:2]):
                path.write_bytes(value)
                with self.assertRaisesRegex(ValueError, 'native binary|Windows x64'):
                    self.build()

    def test_version_mismatch_does_not_create_archive(self):
        different_version = VERSION.split('.')
        different_version[-1] = str(int(different_version[-1]) + 1)
        with self.assertRaisesRegex(ValueError, 'must match'):
            self.build('.'.join(different_version))
        self.assertFalse((self.repo / 'dist').exists())

    def test_legacy_authorization_python_must_not_ship(self):
        for name in ('bg_license.py', 'bg_credentials.py'):
            with self.subTest(name=name):
                path = self.repo / 'src/Bake_Groups' / name
                path.write_bytes(b'old fixture')
                with self.assertRaisesRegex(ValueError, 'legacy/build/private'):
                    self.build()
                path.unlink()

    def test_missing_source_or_notice_is_rejected(self):
        (self.repo / 'THIRD_PARTY_NOTICES.md').unlink()
        with self.assertRaisesRegex(ValueError, 'document is missing'):
            self.build()

    def test_failed_zip_write_keeps_preexisting_release(self):
        output = self.repo / 'dist'
        output.mkdir()
        archive = output / ASSET_NAME
        archive.write_bytes(b'previous release')
        with mock.patch.object(builder.zipfile.ZipFile, 'write', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(OSError, 'disk full'):
                self.build()
        self.assertEqual(archive.read_bytes(), b'previous release')
        self.assertFalse(list(output.glob('.bake-release-*')))


if __name__ == '__main__':
    unittest.main()
