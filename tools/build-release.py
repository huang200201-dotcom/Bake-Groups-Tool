#!/usr/bin/env python3
"""Build an unsigned, complete open-source Windows Maya release ZIP.

Run after tools/build-native.* has produced all six native binaries. The
release checksum detects corruption; no activation/signing service is used.
"""
import argparse
import ast
import hashlib
import importlib.util
import os
from pathlib import Path
import re
import shutil
import struct
import tempfile
import zipfile


SUPPORTED_MAYA = ('2022', '2023', '2024', '2025', '2026', '2027')
NATIVE_NAME = 'bg_math_core.pyd'
REQUIRED_SOURCE = (
    '__init__.py', 'launcher.py', 'bg_version.py', 'bg_main_window.py',
    'bg_core.py', 'bg_mixins.py', 'bg_final_export.py', 'bg_final_groups.py',
    'bg_scene_state.py', 'bg_worker_hp.py', 'bg_worker_lp.py',
    'bg_cage.py', 'bg_localization.py', 'bg_ui_widgets.py',
)
LEGACY_NAMES = {'bg_license.py', 'bg_credentials.py', 'bg_update.py',
                'active_version.json', 'license_config.json', 'update_config.json'}
OLD_NATIVE_APIS = (b'activate_signed_license', b'deactivate_license', b'require_authorized')
REPOSITORY_URL = 'https://github.com/huang200201-dotcom/Bake-Groups-Tool'


def sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _load_installer(repo):
    path = repo / 'installer' / '安装到Maya.py'
    spec = importlib.util.spec_from_file_location('_bake_release_installer', str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _version(path):
    tree = ast.parse(path.read_text(encoding='utf-8-sig'))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == '__version__' for target in node.targets):
            value = ast.literal_eval(node.value)
            if isinstance(value, str) and re.match(r'^\d+\.\d+\.\d+$', value):
                return value
    raise ValueError('bg_version.py must declare a numeric __version__')


def _validate_native(path):
    """Reject missing/empty/non-Windows modules and accidental licensed builds."""
    if not path.is_file():
        raise ValueError('Missing native binary: ' + str(path))
    content = path.read_bytes()
    if len(content) < 256 or content[:2] != b'MZ':
        raise ValueError('Not a Windows native binary: ' + str(path))
    offset = struct.unpack_from('<I', content, 0x3C)[0]
    if (offset > len(content) - 24 or content[offset:offset + 4] != b'PE\0\0' or
            struct.unpack_from('<H', content, offset + 4)[0] != 0x8664):
        raise ValueError('Native binary must target Windows x64: ' + str(path))
    if any(symbol in content for symbol in OLD_NATIVE_APIS):
        raise ValueError('Old licensed native binary found: ' + str(path))


def _copy(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(source), str(destination))


def build_release(repo_root, version=None, output_dir=None):
    repo = Path(repo_root).absolute()
    installer = _load_installer(repo)
    source = repo / 'src' / 'Bake_Groups'
    source_files = installer._files(str(source))
    missing = sorted(set(REQUIRED_SOURCE) - set(source_files))
    if missing:
        raise ValueError('Missing source files: ' + ', '.join(missing))
    source_version = _version(source / 'bg_version.py')
    version = version or source_version
    if version != source_version or not re.match(r'^\d+\.\d+\.\d+$', version):
        raise ValueError('Requested version must match src/Bake_Groups/bg_version.py')
    for name in source_files:
        installer._safe_relative(name)
        if (name.casefold() in LEGACY_NAMES or name.split('/')[0].casefold() in ('versions', 'bin') or
                Path(name).suffix.lower() in ('.pyd', '.pyc', '.cpp', '.obj', '.pdb', '.pem', '.key')):
            raise ValueError('Unexpected legacy/build/private file in source payload: ' + name)

    docs = [repo / name for name in ('LICENSE', 'THIRD_PARTY_NOTICES.md', 'README.md', 'CHANGELOG.md')]
    for path in docs:
        installer._reject_links(str(path))
        if not path.is_file() or path.stat().st_size == 0:
            raise ValueError('Required release document is missing: ' + str(path))
    documentation = installer._files(str(repo / 'docs'))
    for name in ('usage.md', 'building.md'):
        if name not in documentation:
            raise ValueError('Required release document is missing: docs/' + name)
    native_source = repo / 'native' / 'bg_math_core.cpp'
    installer._reject_links(str(native_source))
    if not native_source.is_file():
        raise ValueError('Corresponding native source is missing')
    native_build_scripts = sorted(path for path in (repo / 'tools').glob('build-native.*') if path.is_file())
    if not native_build_scripts:
        raise ValueError('Native build script tools/build-native.* is missing')
    binaries = {}
    for year in SUPPORTED_MAYA:
        path = repo / 'build' / 'native' / year / NATIVE_NAME
        installer._reject_links(str(path))
        _validate_native(path)
        binaries[year] = path

    output = Path(output_dir).absolute() if output_dir else repo / 'dist'
    installer._reject_links(str(output))
    output.mkdir(parents=True, exist_ok=True)
    asset_name = 'Bake_Master_{}_Windows_x64.zip'.format(version)
    archive = output / asset_name
    checksum = output / (asset_name + '.sha256')
    for path in (archive, checksum):
        installer._reject_links(str(path))
    with tempfile.TemporaryDirectory(prefix='.bake-release-', dir=str(output)) as temporary:
        staging = Path(temporary) / 'package'
        staging.mkdir()
        for relative, path in sorted(source_files.items()):
            _copy(path, staging / 'Bake_Groups' / relative)
        for year, path in binaries.items():
            _copy(path, staging / 'Bake_Groups' / 'bin' / year / NATIVE_NAME)
        for path in docs:
            _copy(path, staging / path.name)
        for name, path in documentation.items():
            installer._safe_relative(name)
            _copy(path, staging / 'docs' / name)
        installer_files = installer._files(str(repo / 'installer'))
        for name, path in sorted(installer_files.items()):
            if Path(name).suffix.lower() in ('.py', '.txt'):
                installer._safe_relative(name)
                _copy(path, staging / name)
        if not (staging / '安装到Maya.py').is_file():
            raise ValueError('Drag-and-drop installer missing from package')
        for name, path in installer._files(str(repo / 'native')).items():
            if Path(name).suffix.lower() in ('.cpp', '.c', '.h', '.hpp', '.cmake', '.txt'):
                installer._safe_relative(name)
                _copy(path, staging / 'source' / 'native' / name)
        for path in native_build_scripts:
            installer._reject_links(str(path))
            _copy(path, staging / 'source' / 'tools' / path.name)
        cmake = repo / 'CMakeLists.txt'
        if cmake.is_file():
            installer._reject_links(str(cmake))
            _copy(cmake, staging / 'source' / cmake.name)
        source_note = (
            '# Corresponding source for Bake Master {0}\n\n'
            'Release tag: [{1}/tree/v{0}]({1}/tree/v{0})\n\n'
            'Complete source ZIP: [{1}/archive/refs/tags/v{0}.zip]'
            '({1}/archive/refs/tags/v{0}.zip)\n\n'
            'Bake_Groups/ contains the shipped Python source. source/native/ and '
            'source/tools/ contain the corresponding native source and build script. '
            'Use the matching tag for the complete repository layout, build instructions, '
            'and tests. See LICENSE and THIRD_PARTY_NOTICES.md.\n'
        ).format(version, REPOSITORY_URL)
        (staging / 'SOURCE_CODE.md').write_text(source_note, encoding='utf-8')
        payload_files = installer._files(str(staging))
        lines = ['{}  {}'.format(sha256(path), relative) for relative, path in sorted(payload_files.items())]
        (staging / 'SHA256.txt').write_text('\n'.join(lines) + '\n', encoding='utf-8')
        installer.validate_package(str(staging))
        temporary_zip = Path(temporary) / asset_name
        with zipfile.ZipFile(str(temporary_zip), 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as output_zip:
            for relative, path in sorted(installer._files(str(staging)).items()):
                output_zip.write(path, relative)
        digest = sha256(temporary_zip)
        temporary_checksum = Path(temporary) / checksum.name
        temporary_checksum.write_text('{}  {}\n'.format(digest, asset_name), encoding='ascii')
        os.replace(str(temporary_zip), str(archive))
        os.replace(str(temporary_checksum), str(checksum))
    return {'archive': str(archive), 'sha256': digest, 'checksum': str(checksum), 'version': version}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', help='Must match bg_version.py; defaults to that value')
    parser.add_argument('--output', help='Output directory (default: repository/dist)')
    args = parser.parse_args()
    result = build_release(Path(__file__).resolve().parent.parent, args.version, args.output)
    print('Release ZIP: ' + result['archive'])
    print('SHA256: ' + result['sha256'])
    print('Checksum file: ' + result['checksum'])


if __name__ == '__main__':
    main()
