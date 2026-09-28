"""Public updater regressions using complete ZIP fixtures and real disk writes."""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import py_compile
import struct
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock
import zipfile


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('open_update_under_test', ROOT / 'src/Bake_Groups/bg_update.py')
update = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(update)


def native(marker=b'original'):
    data = bytearray(512)
    data[:2] = b'MZ'
    struct.pack_into('<I', data, 0x3c, 128)
    data[128:132] = b'PE\0\0'
    struct.pack_into('<H', data, 132, 0x8664)
    return bytes(data) + marker


def package(version='1.0.2', overrides=None, extra=None):
    files = {'Bake_Groups/' + name: b'# fixture source\n' for name in update.REQUIRED}
    files['Bake_Groups/bg_version.py'] = ('__version__ = "{}"\n'.format(version)).encode('ascii')
    files['Bake_Groups/bg_main_window.py'] = b'VALUE = "new"\n'
    files.update({'Bake_Groups/' + name: native() for name in update.NATIVE_PATHS})
    files['README.md'] = b'complete public release fixture\n'
    # This is deliberately not valid Python: the updater must not execute it.
    files['安装到Maya.py'] = b'installer must never execute !!!!\n'
    files.update(overrides or {})
    files.update(extra or {})
    manifest = ''.join('{}  {}\n'.format(hashlib.sha256(content).hexdigest(), name)
                       for name, content in sorted(files.items())).encode('utf-8')
    files['SHA256.txt'] = manifest
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


class OpenUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='bake-open-update-')
        self.addCleanup(self.temporary.cleanup)
        self.runtime = Path(self.temporary.name) / 'Bake_Groups'
        self.runtime.mkdir()
        for name in update.REQUIRED:
            (self.runtime / name).write_bytes(b'# fixture source\n')
        (self.runtime / 'bg_version.py').write_text('__version__ = "1.0.1"\n', encoding='utf-8')
        (self.runtime / 'bg_main_window.py').write_bytes(b'VALUE = "old"\n')
        for name in update.NATIVE_PATHS:
            path = self.runtime / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(native())
        self.release = update._release({'version': '1.0.2'})
        self.cache = self.runtime.parent / 'Bake_Groups.update'

    def network(self, archive, wrong_digest=False):
        digest = '0' * 64 if wrong_digest else hashlib.sha256(archive).hexdigest()
        checksum = '{}  {}\n'.format(digest, self.release['archive_name']).encode('ascii')

        def download(url, destination=None, limit=update.MAX_ARCHIVE, cancelled=None):
            update._cancel(cancelled)
            if url == self.release['checksum_url']:
                return checksum
            self.assertEqual(url, self.release['archive_url'])
            Path(destination).write_bytes(archive)
            return len(archive)
        return mock.patch.object(update, '_download', side_effect=download)

    def stage(self, archive=None):
        with self.network(archive if archive is not None else package()):
            return update.download_and_stage(self.release, str(self.runtime))

    def snapshot(self):
        return {str(path.relative_to(self.runtime)): path.read_bytes()
                for path in self.runtime.rglob('*') if path.is_file()}

    def test_stable_newer_release_requires_both_exact_assets(self):
        data = {'tag_name': 'v1.0.2', 'draft': False, 'prerelease': False,
                'assets': [{'name': self.release['archive_name'],
                            'browser_download_url': self.release['archive_url']},
                           {'name': self.release['archive_name'] + '.sha256',
                            'browser_download_url': self.release['checksum_url']}]}
        with mock.patch.object(update, '_download', return_value=json.dumps(data).encode('utf-8')):
            self.assertEqual(update.check_for_update('1.0.1'), self.release)
            self.assertIsNone(update.check_for_update('1.0.2'))
        for mutation in ({'prerelease': True}, {'draft': True}, {'tag_name': 'v1.0.3-beta.1'}):
            changed = dict(data, **mutation)
            with mock.patch.object(update, '_download', return_value=json.dumps(changed).encode('utf-8')):
                self.assertIsNone(update.check_for_update('1.0.1'))
        data['assets'].pop()
        with mock.patch.object(update, '_download', return_value=json.dumps(data).encode('utf-8')):
            with self.assertRaises(update.UpdateError):
                update.check_for_update('1.0.1')

    def test_stage_is_complete_verified_and_does_not_change_runtime(self):
        before = self.snapshot()
        pending = self.stage()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(pending['version'], '1.0.2')
        self.assertEqual(pending['status'], 'pending')
        self.assertFalse(pending['requires_restart'])
        self.assertEqual(set(pending['files']), set(update.REQUIRED + update.NATIVE_PATHS))
        self.assertFalse((Path(pending['stage_dir']) / '安装到Maya.py').exists())

    def test_checksum_syntax_version_and_incomplete_archive_refused(self):
        with self.network(package(), wrong_digest=True):
            with self.assertRaisesRegex(update.UpdateError, 'archive checksum failed'):
                update.download_and_stage(self.release, str(self.runtime))
        malformed = (
            package(version='1.0.3'),
            package(overrides={'Bake_Groups/bg_core.py': b'if syntax broken !!!'}),
            package(overrides={'Bake_Groups/bin/2027/bg_math_core.pyd': b'not a PE'}),
            package(overrides={'Bake_Groups/bin/2027/bg_math_core.pyd': native(b'require_authorized')}),
        )
        for archive in malformed:
            with self.subTest(size=len(archive)), self.network(archive):
                with self.assertRaises(update.UpdateError):
                    update.download_and_stage(self.release, str(self.runtime))
        self.assertIsNone(update.pending_update(str(self.runtime)))
        self.assertEqual(list(self.cache.iterdir()), [])

    def test_unsafe_zip_paths_and_symlinks_refused(self):
        for name in ('../escape.py', 'Bake_Groups/../escape.py', 'C:/escape.py',
                     'Bake_Groups/NUL.py', 'Bake_Groups/file.py:stream', 'Bake_Groups/source.ma'):
            with self.subTest(name=name), self.network(package(extra={name: b'bad'})):
                with self.assertRaises(update.UpdateError):
                    update.download_and_stage(self.release, str(self.runtime))
        archive = io.BytesIO(package())
        with zipfile.ZipFile(archive, 'a') as zipped:
            info = zipfile.ZipInfo('linked.py')
            info.create_system = 3
            info.external_attr = (0o120777 << 16)
            zipped.writestr(info, 'outside')
        with self.network(archive.getvalue()):
            with self.assertRaisesRegex(update.UpdateError, 'Links'):
                update.download_and_stage(self.release, str(self.runtime))

    def test_cancelled_download_preserves_old_pending(self):
        old = self.stage()
        newer = update._release({'version': '1.0.3'})
        with self.assertRaises(update.UpdateCancelled):
            update.download_and_stage(newer, str(self.runtime), cancelled=lambda: True)
        self.assertEqual(update.pending_update(str(self.runtime))['stage'], old['stage'])

    def test_source_checkout_refused_without_network(self):
        runtime = self.runtime.parent / 'src' / 'Bake_Groups'
        (runtime / 'bin').mkdir(parents=True)
        with mock.patch.object(update, '_download') as network:
            with self.assertRaisesRegex(update.UpdateError, 'Source checkouts'):
                update.download_and_stage(self.release, str(runtime))
            self.assertIsNone(update.pending_update(str(runtime)))
        network.assert_not_called()

    def test_apply_preserves_unknown_files_and_unchanged_loaded_native(self):
        (self.runtime / 'artist-notes.txt').write_text('keep this', encoding='utf-8')
        self.stage()
        binary = self.runtime / 'bin/2027/bg_math_core.pyd'
        before = binary.stat().st_mtime_ns
        loaded = {'bg_math_core': types.SimpleNamespace(__file__=str(binary))}
        self.assertFalse(update.requires_restart(str(self.runtime), loaded))
        with mock.patch.object(update, '_atomic_copy', wraps=update._atomic_copy) as copied:
            result = update.apply_pending(str(self.runtime), loaded)
        self.assertEqual(result['status'], 'applied')
        self.assertFalse(any(str(args[0][1]).endswith('.pyd') for args in copied.call_args_list))
        self.assertEqual(binary.stat().st_mtime_ns, before)
        self.assertEqual((self.runtime / 'artist-notes.txt').read_text(), 'keep this')
        self.assertIsNone(update.pending_update(str(self.runtime)))
        self.assertFalse((self.cache / 'transaction.json').exists())

    def test_changed_loaded_native_defers_every_file_until_restart(self):
        self.stage(package(overrides={'Bake_Groups/bin/2027/bg_math_core.pyd': native(b'new')}))
        loaded = {'bg_math_core': types.SimpleNamespace(__file__=str(self.runtime / 'bin/2027/bg_math_core.pyd'))}
        before = self.snapshot()
        self.assertTrue(update.requires_restart(str(self.runtime), loaded))
        self.assertEqual(update.apply_pending(str(self.runtime), loaded)['status'], 'restart_required')
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(update.apply_pending(str(self.runtime), {})['status'], 'applied')

    def test_change_to_other_unloaded_maya_binary_can_apply(self):
        self.stage(package(overrides={'Bake_Groups/bin/2022/bg_math_core.pyd': native(b'new')}))
        loaded = {'bg_math_core': types.SimpleNamespace(__file__=str(self.runtime / 'bin/2027/bg_math_core.pyd'))}
        self.assertFalse(update.requires_restart(str(self.runtime), loaded))
        self.assertEqual(update.apply_pending(str(self.runtime), loaded)['status'], 'applied')

    def test_partial_apply_failure_rolls_back_all_changes(self):
        self.stage()
        before = self.snapshot()
        original, count = update._atomic_copy, [0]

        def fail_once(source, destination):
            count[0] += 1
            if count[0] == 2:
                raise OSError('fixture locked destination')
            return original(source, destination)

        with mock.patch.object(update, '_atomic_copy', side_effect=fail_once):
            with self.assertRaisesRegex(update.UpdateError, 'previous files were restored'):
                update.apply_pending(str(self.runtime), {})
        self.assertEqual(self.snapshot(), before)
        self.assertIsNotNone(update.pending_update(str(self.runtime)))
        self.assertFalse((self.cache / 'transaction.json').exists())

    def test_interrupted_transaction_is_recovered_before_retry(self):
        self.stage()
        original, count = update._atomic_copy, [0]

        def interrupt_once(source, destination):
            original(source, destination)
            count[0] += 1
            if count[0] == 1:
                raise KeyboardInterrupt('simulate process exit after an atomic replacement')

        with mock.patch.object(update, '_atomic_copy', side_effect=interrupt_once):
            with self.assertRaises(KeyboardInterrupt):
                update.apply_pending(str(self.runtime), {})
        pending = update.pending_update(str(self.runtime))
        self.assertEqual(pending['recovery_state'], 'applying')
        self.assertEqual(update.apply_pending(str(self.runtime), {})['status'], 'applied')
        self.assertIsNone(update.pending_update(str(self.runtime)))

    def test_process_death_releases_locks_and_recovers_durable_journal(self):
        self.stage()
        code = (
            'import importlib.util, os\n'
            'spec = importlib.util.spec_from_file_location("child_update", {module!r})\n'
            'mod = importlib.util.module_from_spec(spec)\n'
            'spec.loader.exec_module(mod)\n'
            'original = mod._atomic_copy\n'
            'def crash(source, destination):\n'
            '    original(source, destination)\n'
            '    os._exit(37)\n'
            'mod._atomic_copy = crash\n'
            'mod.apply_pending({runtime!r}, {{}})\n'
        ).format(module=str(ROOT / 'src/Bake_Groups/bg_update.py'), runtime=str(self.runtime))
        child = subprocess.run([sys.executable, '-c', code], stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, timeout=30)
        self.assertEqual(child.returncode, 37, child.stderr.decode('utf-8', errors='replace'))
        self.assertEqual(update.pending_update(str(self.runtime))['recovery_state'], 'applying')
        self.assertEqual(update.apply_pending(str(self.runtime), {})['status'], 'applied')
        self.assertIsNone(update.pending_update(str(self.runtime)))

    def test_damaged_recovery_backup_blocks_importable_success(self):
        self.stage()
        original = update._atomic_copy

        def stop_after_copy(source, destination):
            original(source, destination)
            raise KeyboardInterrupt('stop')

        with mock.patch.object(update, '_atomic_copy', side_effect=stop_after_copy):
            with self.assertRaises(KeyboardInterrupt):
                update.apply_pending(str(self.runtime), {})
        journal = update._journal(str(self.runtime), str(self.cache))
        backup = self.cache / journal['backup'] / 'bg_main_window.py'
        backup.write_bytes(b'broken backup')
        with self.assertRaises(update.UpdateRecoveryError):
            update.apply_pending(str(self.runtime), {})
        self.assertEqual(update.pending_update(str(self.runtime))['recovery_state'], 'applying')

    def test_committed_journal_without_pending_still_finishes_cleanup(self):
        self.stage()
        original = update._recover

        def block_cleanup(runtime, root, loaded):
            journal = update._journal(runtime, root)
            if journal and journal['state'] == 'committed':
                raise OSError('fixture cleanup interrupted')
            return original(runtime, root, loaded)

        with mock.patch.object(update, '_recover', side_effect=block_cleanup):
            self.assertIn('warning', update.apply_pending(str(self.runtime), {}))
        (self.cache / 'pending.json').unlink()
        self.assertEqual(update.pending_update(str(self.runtime))['recovery_state'], 'committed')
        self.assertEqual(update.apply_pending(str(self.runtime), {})['status'], 'applied')
        self.assertIsNone(update.pending_update(str(self.runtime)))

    def test_modified_staged_file_and_scene_in_runtime_prevent_writes(self):
        pending = self.stage()
        path = Path(pending['stage_dir']) / 'payload/bg_core.py'
        path.write_text('tampered = True\n', encoding='utf-8')
        before = self.snapshot()
        with self.assertRaisesRegex(update.UpdateError, 'checksum failed'):
            update.apply_pending(str(self.runtime), {})
        self.assertEqual(self.snapshot(), before)
        path.write_bytes(b'# fixture source\n')
        (self.runtime / 'artist_scene.ma').write_bytes(b'artist scene')
        with self.assertRaisesRegex(update.UpdateError, 'Maya scene files'):
            update.apply_pending(str(self.runtime), {})
        self.assertEqual((self.runtime / 'artist_scene.ma').read_bytes(), b'artist scene')

    def test_same_size_same_mtime_source_invalidates_existing_bytecode(self):
        path = self.runtime / 'bg_main_window.py'
        py_compile.compile(str(path), doraise=True)
        old_mtime = path.stat().st_mtime
        pending = self.stage()
        staged = Path(pending['stage_dir']) / 'payload/bg_main_window.py'
        self.assertEqual(path.stat().st_size, staged.stat().st_size)
        os.utime(str(staged), (old_mtime, old_mtime))
        update.apply_pending(str(self.runtime), {})
        spec = importlib.util.spec_from_file_location('updated_fixture', str(path))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(module.VALUE, 'new')


if __name__ == '__main__':
    unittest.main(verbosity=2)
