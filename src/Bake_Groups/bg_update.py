"""Public GitHub release updates, independent of Maya and Qt (Python 3.7+).

Downloads are staged beside the installation. Only the launcher may apply them,
after shutting down the UI. No downloaded installer or Python source is executed.
SHA256 protects download integrity; release publishing trusts the GitHub account.
"""
import ast
import contextlib
import hashlib
import json
import os
import re
import shutil
import stat
import struct
import sys
import tempfile
import time
import uuid
import zipfile
from urllib import error, parse, request


REPOSITORY = 'huang200201-dotcom/Bake-Groups-Tool'
API_URL = 'https://api.github.com/repos/' + REPOSITORY + '/releases/latest'
NETWORK_TIMEOUT = 15
DOWNLOAD_DEADLINE = 180
MAX_ARCHIVE = 64 * 1024 * 1024
MAX_EXPANDED = 256 * 1024 * 1024
MAX_FILE = 32 * 1024 * 1024
MAX_FILES = 2048
YEARS = ('2022', '2023', '2024', '2025', '2026', '2027')
REQUIRED = ('__init__.py', 'launcher.py', 'bg_version.py', 'bg_main_window.py',
            'bg_core.py', 'bg_mixins.py', 'bg_final_export.py', 'bg_final_groups.py',
            'bg_scene_state.py', 'bg_worker_hp.py', 'bg_worker_lp.py', 'bg_cage.py',
            'bg_localization.py', 'bg_ui_widgets.py', 'bg_update.py')
NATIVE_PATHS = tuple('bin/' + year + '/bg_math_core.pyd' for year in YEARS)
_VERSION = re.compile(r'^v?((?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*))$')
_HASH = re.compile(r'^[0-9a-f]{64}$')
_STAGE = re.compile(r'^stage-[0-9a-f]{32}$')
_BACKUP = re.compile(r'^backup-[0-9a-f]{32}$')


class UpdateError(RuntimeError):
    pass


class UpdateCancelled(UpdateError):
    pass


class UpdateRecoveryError(UpdateError):
    """The installation must not be imported until journal recovery succeeds."""
    pass


def _cancel(cancelled):
    if cancelled is not None and cancelled():
        raise UpdateCancelled('Update cancelled')


def _version(value):
    match = _VERSION.match(str(value))
    if match is None:
        raise UpdateError('Expected a stable numeric release version')
    return tuple(int(part) for part in match.group(1).split('.'))


def _trusted_url(url):
    parsed = parse.urlsplit(url)
    try:
        valid_port = parsed.port in (None, 443)
    except ValueError:
        valid_port = False
    if (parsed.scheme != 'https' or not valid_port or parsed.username or parsed.password
            or parsed.fragment):
        raise UpdateError('Untrusted update URL')
    host = (parsed.hostname or '').lower()
    if host == 'api.github.com':
        valid = parsed.path == '/repos/' + REPOSITORY + '/releases/latest'
    elif host == 'github.com':
        valid = parsed.path.startswith('/' + REPOSITORY + '/releases/download/')
    else:
        valid = host in ('release-assets.githubusercontent.com', 'objects.githubusercontent.com')
    if not valid:
        raise UpdateError('Untrusted update download host or path')
    return url


class _SafeRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _trusted_url(newurl)
        return super(_SafeRedirect, self).redirect_request(req, fp, code, msg, headers, newurl)


def _download(url, destination=None, limit=MAX_ARCHIVE, cancelled=None):
    _trusted_url(url)
    _cancel(cancelled)
    deadline = time.monotonic() + DOWNLOAD_DEADLINE
    opener = request.build_opener(_SafeRedirect())
    req = request.Request(url, headers={'User-Agent': 'Bake-Master-Open-Updater',
                                        'Accept': 'application/vnd.github+json' if url == API_URL else '*/*'})
    chunks, total = [], 0
    try:
        with opener.open(req, timeout=NETWORK_TIMEOUT) as response:
            _trusted_url(response.geturl())
            length = response.headers.get('Content-Length')
            if length is not None and int(length) > limit:
                raise UpdateError('Update download exceeds its size limit')
            stream = open(destination, 'wb') if destination else None
            try:
                while True:
                    _cancel(cancelled)
                    if time.monotonic() > deadline:
                        raise UpdateError('Update download timed out')
                    block = response.read(128 * 1024)
                    if not block:
                        break
                    total += len(block)
                    if total > limit:
                        raise UpdateError('Update download exceeds its size limit')
                    if stream is None:
                        chunks.append(block)
                    else:
                        stream.write(block)
            finally:
                if stream is not None:
                    stream.close()
    except error.HTTPError:
        raise
    except UpdateError:
        raise
    except Exception as exc:
        raise UpdateError('Update download failed: ' + type(exc).__name__)
    _cancel(cancelled)
    return b''.join(chunks) if destination is None else total


def _release(data):
    version = '.'.join(str(part) for part in _version(data['version']))
    tag = data.get('tag_name', 'v' + version)
    if _version(tag) != _version(version):
        raise UpdateError('Release tag and version disagree')
    name = 'Bake_Master_{}_Windows_x64.zip'.format(version)
    base = 'https://github.com/' + REPOSITORY + '/releases/download/' + tag + '/'
    expected = {'version': version, 'tag_name': tag,
                'html_url': 'https://github.com/' + REPOSITORY + '/releases/tag/' + tag,
                'archive_name': name, 'archive_url': base + name,
                'checksum_url': base + name + '.sha256'}
    for key in ('archive_name', 'archive_url', 'checksum_url'):
        if key in data and data[key] != expected[key]:
            raise UpdateError('Release asset does not match the public repository')
    return expected


def check_for_update(current_version, cancelled=None):
    """Return a newer stable, complete Windows release, or None. Network only."""
    current = _version(current_version)
    try:
        data = json.loads(_download(API_URL, limit=2 * 1024 * 1024, cancelled=cancelled).decode('utf-8'))
    except error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise UpdateError('GitHub release check failed (HTTP {})'.format(exc.code))
    except (ValueError, UnicodeError):
        raise UpdateError('Invalid GitHub release response')
    if not isinstance(data, dict) or data.get('draft') or data.get('prerelease'):
        return None
    try:
        latest = _version(data.get('tag_name', ''))
    except UpdateError:
        return None
    if latest <= current:
        return None
    release = _release({'version': '.'.join(map(str, latest)), 'tag_name': data['tag_name']})
    assets = data.get('assets', [])
    for field, name in (('archive_url', release['archive_name']),
                        ('checksum_url', release['archive_name'] + '.sha256')):
        matches = [asset for asset in assets if isinstance(asset, dict) and asset.get('name') == name]
        if len(matches) != 1 or matches[0].get('browser_download_url') != release[field]:
            raise UpdateError('Release does not contain the complete Windows package and checksum')
    return release


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _reject_links(path):
    cursor = os.path.abspath(path)
    while True:
        if os.path.lexists(cursor):
            info = os.lstat(cursor)
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise UpdateError('Update paths cannot contain links or junctions')
        parent = os.path.dirname(cursor)
        if parent == cursor:
            break
        cursor = parent


def _relative(value):
    if not isinstance(value, str) or not value or '\\' in value or ':' in value or value.startswith('/'):
        raise UpdateError('Unsafe update path')
    reserved = {'CON', 'PRN', 'AUX', 'NUL'} | {'COM' + str(n) for n in range(1, 10)} | {'LPT' + str(n) for n in range(1, 10)}
    for part in value.split('/'):
        if (part in ('', '.', '..') or part.rstrip(' .') != part or
                any(ord(char) < 32 or char in '<>"|?*' for char in part) or
                part.split('.', 1)[0].upper() in reserved):
            raise UpdateError('Unsafe update path')
    return value.split('/')


def _payload_path(value):
    _relative(value)
    if (value.split('/')[0].lower() == 'versions' or
            os.path.basename(value).lower() in ('bg_license.py', 'bg_credentials.py',
                                               'license_config.json', 'update_config.json', 'active_version.json')):
        raise UpdateError('Legacy authorization or versioned package is not supported')
    if value in NATIVE_PATHS:
        return
    if value.split('/')[0].lower() == 'bin' or os.path.splitext(value)[1].lower() not in (
            '.py', '.json', '.png', '.svg', '.ico', '.qss', '.txt', '.md', '.ttf', '.woff2'):
        raise UpdateError('Unexpected file in runtime payload: ' + value)


def _layout(runtime_dir, require_installed=False):
    runtime = os.path.abspath(os.fspath(runtime_dir))
    _reject_links(runtime)
    if os.path.basename(runtime).lower() != 'bake_groups':
        raise UpdateError('Update target must be the Bake_Groups installation')
    source = os.path.basename(os.path.dirname(runtime)).lower() == 'src'
    if require_installed and (source or not os.path.isdir(os.path.join(runtime, 'bin'))):
        raise UpdateError('Source checkouts cannot install release updates')
    root = os.path.join(os.path.dirname(runtime), 'Bake_Groups.update')
    _reject_links(root)
    return runtime, root, source


def _files(root):
    _reject_links(root)
    result, folded = {}, set()
    for current, dirs, names in os.walk(root, followlinks=False):
        for name in dirs + names:
            _reject_links(os.path.join(current, name))
        for name in names:
            path = os.path.join(current, name)
            if not stat.S_ISREG(os.stat(path).st_mode):
                raise UpdateError('Unexpected installation filesystem entry')
            relative = os.path.relpath(path, root).replace(os.sep, '/')
            _relative(relative)
            if relative.casefold() in folded:
                raise UpdateError('Case-insensitive installation path collision')
            folded.add(relative.casefold())
            result[relative] = path
    return result


@contextlib.contextmanager
def _lock(root):
    # Keep the tiny lock file: deleting it creates a race with another opener.
    path = root + '.lock'
    _reject_links(path)
    stream = open(path, 'a+b')
    try:
        if os.path.getsize(path) == 0:
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise UpdateError('Another update operation is running')
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    finally:
        stream.close()


@contextlib.contextmanager
def _installation_lock(runtime):
    """Share the drag-and-drop installer's exclusion while touching runtime."""
    # OS locking is released on process exit. A persistent file is not evidence
    # that a writer is still alive, and must not block journal recovery.
    with _lock(os.path.join(os.path.dirname(runtime), 'Bake_Groups.install')):
        yield


def _json_read(path):
    _reject_links(path)
    if os.path.getsize(path) > 2 * 1024 * 1024:
        raise UpdateError('Update metadata is too large')
    try:
        with open(path, 'r', encoding='utf-8') as stream:
            data = json.load(stream)
        if not isinstance(data, dict):
            raise ValueError()
        return data
    except (ValueError, UnicodeError):
        raise UpdateError('Invalid update metadata')


def _atomic_json(path, data):
    descriptor, temporary = tempfile.mkstemp(prefix='.metadata-', dir=os.path.dirname(path))
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


def _remove_owned(root, name, pattern):
    if not pattern.match(name) or os.path.dirname(os.path.abspath(os.path.join(root, name))) != root:
        raise UpdateError('Refusing cleanup outside the update transaction')
    path = os.path.join(root, name)
    if os.path.exists(path):
        _files(path)
        shutil.rmtree(path)


def _source_version(path):
    with open(path, 'r', encoding='utf-8-sig') as stream:
        tree = ast.parse(stream.read(), filename=path)
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == '__version__' for target in node.targets):
            value = ast.literal_eval(node.value)
            return '.'.join(map(str, _version(value)))
    raise UpdateError('Package version declaration is missing')


def _stale(data, runtime):
    return bool(data and _version(data['version']) <= _version(
        _source_version(os.path.join(runtime, 'bg_version.py'))))


def _validate_payload(payload, expected, version):
    actual = _files(payload)
    if set(actual) != set(expected) or not set(REQUIRED + NATIVE_PATHS).issubset(actual):
        raise UpdateError('Staged runtime is incomplete or has extra files')
    for relative, path in actual.items():
        _payload_path(relative)
        if not _HASH.match(str(expected[relative])) or _sha256(path) != expected[relative]:
            raise UpdateError('Staged file checksum failed: ' + relative)
        if relative.endswith('.py'):
            with open(path, 'rb') as stream:
                compile(stream.read(), relative, 'exec')
        if relative in NATIVE_PATHS:
            with open(path, 'rb') as stream:
                content = stream.read()
            if len(content) < 256 or content[:2] != b'MZ':
                raise UpdateError('Native module is not a Windows binary')
            offset = struct.unpack_from('<I', content, 0x3C)[0]
            if (offset > len(content) - 24 or content[offset:offset + 4] != b'PE\0\0' or
                    struct.unpack_from('<H', content, offset + 4)[0] != 0x8664):
                raise UpdateError('Native module does not target Windows x64')
            if any(symbol in content for symbol in (b'activate_signed_license', b'deactivate_license', b'require_authorized')):
                raise UpdateError('Legacy licensed native module is not supported')
    if _source_version(os.path.join(payload, 'bg_version.py')) != version:
        raise UpdateError('Package version does not match the GitHub release')


def _unpack(archive, payload, version, cancelled):
    with zipfile.ZipFile(archive) as zipped:
        infos = zipped.infolist()
        if len(infos) > MAX_FILES:
            raise UpdateError('Too many files in update archive')
        files, folded, expanded = {}, set(), 0
        for info in infos:
            relative = info.filename[:-1] if info.is_dir() else info.filename
            _relative(relative)
            mode = info.external_attr >> 16
            file_type = stat.S_IFMT(mode)
            if (file_type not in (0, stat.S_IFDIR, stat.S_IFREG) or info.flag_bits & 1
                    or (info.external_attr & 0x400)):
                raise UpdateError('Links or encrypted entries in update archive')
            if relative.casefold() in folded:
                raise UpdateError('Duplicate or case-colliding ZIP path')
            folded.add(relative.casefold())
            if info.is_dir():
                continue
            expanded += info.file_size
            if info.file_size > MAX_FILE or expanded > MAX_EXPANDED:
                raise UpdateError('Expanded update archive exceeds size limits')
            files[relative] = info
        manifest = files.pop('SHA256.txt', None)
        if manifest is None or manifest.file_size > 2 * 1024 * 1024:
            raise UpdateError('Update archive checksum manifest is missing or too large')
        expected = {}
        for line in zipped.read(manifest).decode('utf-8-sig').splitlines():
            if not line:
                continue
            match = re.match(r'^([0-9a-fA-F]{64})  (.+)$', line)
            if match is None:
                raise UpdateError('Invalid archive checksum manifest')
            digest, relative = match.groups()
            _relative(relative)
            if relative.casefold() in {name.casefold() for name in expected}:
                raise UpdateError('Duplicate archive checksum path')
            expected[relative] = digest.lower()
        if set(expected) != set(files):
            raise UpdateError('Archive files do not match its checksum manifest')
        payload_hashes = {}
        for name, info in files.items():
            _cancel(cancelled)
            target = None
            if name.startswith('Bake_Groups/'):
                relative = name[len('Bake_Groups/'):]
                _payload_path(relative)
                target = os.path.join(payload, *_relative(relative))
                os.makedirs(os.path.dirname(target), exist_ok=True)
                payload_hashes[relative] = expected[name]
            digest = hashlib.sha256()
            with zipped.open(info) as source:
                output = open(target, 'wb') if target else None
                try:
                    for block in iter(lambda: source.read(128 * 1024), b''):
                        _cancel(cancelled)
                        digest.update(block)
                        if output is not None:
                            output.write(block)
                finally:
                    if output is not None:
                        output.close()
            if digest.hexdigest() != expected[name]:
                raise UpdateError('Archive file checksum failed: ' + name)
    _validate_payload(payload, payload_hashes, version)
    return payload_hashes


def _metadata(runtime, root):
    path = os.path.join(root, 'pending.json')
    if not os.path.isfile(path):
        return None
    data = _json_read(path)
    stage = data.get('stage', '')
    if (data.get('schema') != 1 or data.get('runtime_dir') != runtime or
            not _STAGE.match(stage) or not isinstance(data.get('files'), dict) or
            not data['files'] or len(data['files']) > MAX_FILES):
        raise UpdateError('Invalid pending update metadata')
    _version(data.get('version'))
    for name, digest in data['files'].items():
        _payload_path(name)
        if not _HASH.match(str(digest)):
            raise UpdateError('Invalid pending file digest')
    data.update(stage_dir=os.path.join(root, stage), status='pending')
    return data


def pending_update(runtime_dir):
    """Read pending state without networking, hashing binaries, or scene access."""
    runtime, root, source = _layout(runtime_dir)
    if source:
        return None
    data = _metadata(runtime, root)
    journal_path = os.path.join(root, 'transaction.json')
    if os.path.isfile(journal_path):
        journal = _journal(runtime, root)
        if data is None:
            data = {'version': journal['version'], 'runtime_dir': runtime, 'status': 'recovery'}
        data['recovery_required'] = True
        data['recovery_state'] = journal['state']
    elif _stale(data, runtime):
        # A manual installation may have superseded this cached release. Keep
        # this query read-only; stage/apply clean owned cache on their next run.
        return None
    return data


def _loaded_native(loaded_modules):
    result = []
    for name, module in list((sys.modules if loaded_modules is None else loaded_modules).items()):
        if name.rsplit('.', 1)[-1] != 'bg_math_core' or module is None:
            continue
        path = getattr(module, '__file__', None)
        result.append(os.path.normcase(os.path.realpath(path)) if path else None)
    return result


def _change_needs_restart(runtime, files, loaded_modules):
    loaded = _loaded_native(loaded_modules)
    if not loaded:
        return False
    for relative, digest in files.items():
        if not relative.lower().endswith('.pyd'):
            continue
        destination = os.path.join(runtime, *_relative(relative))
        _reject_links(destination)
        unchanged = os.path.isfile(destination) and _sha256(destination) == digest
        if not unchanged and (None in loaded or os.path.normcase(os.path.realpath(destination)) in loaded):
            return True
    # A different installation may already own the process-wide extension name.
    known = {os.path.normcase(os.path.realpath(os.path.join(runtime, *_relative(name)))) for name in NATIVE_PATHS}
    return any(path not in known for path in loaded)


def requires_restart(runtime_dir, loaded_modules=None):
    runtime, root, source = _layout(runtime_dir)
    if source:
        return False
    data = _metadata(runtime, root)
    journal = _journal(runtime, root)
    if not journal and _stale(data, runtime):
        return False
    if journal and journal['state'] == 'applying':
        old = {entry['path']: entry['old_hash'] for entry in journal['entries'] if entry['existed']}
        if _change_needs_restart(runtime, old, loaded_modules):
            return True
    return bool(data and _change_needs_restart(runtime, data['files'], loaded_modules))


def download_and_stage(release, runtime_dir, cancelled=None):
    """Download/verify a complete release. This never changes installed files."""
    release = _release(release)
    runtime, root, unused = _layout(runtime_dir, require_installed=True)
    with _lock(root):
        if _version(release['version']) <= _version(_source_version(os.path.join(runtime, 'bg_version.py'))):
            raise UpdateError('Release is not newer than the installed version')
        if os.path.isfile(os.path.join(root, 'transaction.json')):
            raise UpdateError('Finish the pending update recovery before downloading another release')
        _cancel(cancelled)
        os.makedirs(root, exist_ok=True)
        old = _metadata(runtime, root)
        if old and _version(old['version']) >= _version(release['version']):
            old['requires_restart'] = requires_restart(runtime)
            return old
        stage = 'stage-' + uuid.uuid4().hex
        directory = os.path.join(root, stage)
        os.mkdir(directory)
        published = False
        try:
            checksum = _download(release['checksum_url'], limit=8192, cancelled=cancelled).decode('ascii').strip()
            match = re.match(r'^([0-9a-fA-F]{64})  (.+)$', checksum)
            if match is None or match.group(2) != release['archive_name']:
                raise UpdateError('Invalid release archive checksum')
            archive = os.path.join(directory, 'release.zip')
            _download(release['archive_url'], archive, MAX_ARCHIVE, cancelled)
            if _sha256(archive) != match.group(1).lower():
                raise UpdateError('Release archive checksum failed')
            payload = os.path.join(directory, 'payload')
            os.mkdir(payload)
            hashes = _unpack(archive, payload, release['version'], cancelled)
            _cancel(cancelled)
            os.remove(archive)
            data = {'schema': 1, 'version': release['version'], 'runtime_dir': runtime,
                    'stage': stage, 'files': hashes, 'archive_sha256': match.group(1).lower()}
            _atomic_json(os.path.join(root, 'pending.json'), data)
            published = True
            if old:
                _remove_owned(root, old['stage'], _STAGE)
            data = _metadata(runtime, root)
            data['requires_restart'] = requires_restart(runtime)
            return data
        except error.HTTPError as exc:
            raise UpdateError('GitHub asset download failed (HTTP {})'.format(exc.code))
        except (ValueError, UnicodeError, SyntaxError, zipfile.BadZipFile) as exc:
            raise UpdateError('Invalid update package: ' + str(exc))
        finally:
            if not published:
                _remove_owned(root, stage, _STAGE)


def _journal(runtime, root):
    path = os.path.join(root, 'transaction.json')
    if not os.path.isfile(path):
        return None
    data = _json_read(path)
    if (data.get('schema') != 1 or data.get('runtime_dir') != runtime or
            data.get('state') not in ('applying', 'committed') or
            not _BACKUP.match(data.get('backup', '')) or not _STAGE.match(data.get('stage', '')) or
            not isinstance(data.get('entries'), list) or len(data['entries']) > MAX_FILES):
        raise UpdateError('Invalid update recovery journal')
    _version(data.get('version'))
    seen = set()
    for entry in data['entries']:
        _payload_path(entry['path'])
        if (entry['path'].casefold() in seen or not isinstance(entry.get('existed'), bool) or
                not _HASH.match(str(entry.get('new_hash', ''))) or
                (entry['existed'] and not _HASH.match(str(entry.get('old_hash', ''))))):
            raise UpdateError('Invalid update recovery entry')
        seen.add(entry['path'].casefold())
    return data


def _atomic_copy(source, destination):
    _reject_links(destination)
    parent = os.path.dirname(destination)
    os.makedirs(parent, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.bakemaster-update-', dir=parent)
    os.close(descriptor)
    try:
        shutil.copy2(source, temporary)
        with open(temporary, 'r+b') as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        _invalidate_bytecode(destination)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


def _invalidate_bytecode(source):
    if not source.endswith('.py'):
        return
    parent, filename = os.path.split(source)
    cache = os.path.join(parent, '__pycache__')
    _reject_links(cache)
    candidates = [source + 'c']
    if os.path.isdir(cache):
        stem = os.path.splitext(filename)[0] + '.'
        candidates.extend(os.path.join(cache, name) for name in os.listdir(cache)
                          if name.startswith(stem) and name.endswith('.pyc'))
    for path in candidates:
        _reject_links(path)
        if os.path.isfile(path):
            os.remove(path)


def _recover_transaction(runtime, root, loaded_modules):
    journal = _journal(runtime, root)
    if journal is None:
        return None
    if journal['state'] == 'applying':
        old = {entry['path']: entry['old_hash'] for entry in journal['entries'] if entry['existed']}
        if _change_needs_restart(runtime, old, loaded_modules):
            raise UpdateError('Restart Maya before recovering the native update')
        backup = os.path.join(root, journal['backup'])
        # Validate all backups before restoring even one installed file.
        for entry in journal['entries']:
            if entry['existed']:
                path = os.path.join(backup, *_relative(entry['path']))
                _reject_links(path)
                if not os.path.isfile(path) or _sha256(path) != entry['old_hash']:
                    raise UpdateError('Update recovery backup is missing or damaged')
        for entry in reversed(journal['entries']):
            destination = os.path.join(runtime, *_relative(entry['path']))
            _reject_links(destination)
            if entry['existed']:
                if not os.path.isfile(destination) or _sha256(destination) != entry['old_hash']:
                    _atomic_copy(os.path.join(backup, *_relative(entry['path'])), destination)
            elif os.path.exists(destination):
                if _sha256(destination) != entry['new_hash']:
                    raise UpdateError('New update file was modified; refusing to remove it during recovery')
                os.remove(destination)
                _invalidate_bytecode(destination)
    else:
        pending = _metadata(runtime, root)
        if pending and pending['stage'] == journal['stage']:
            os.remove(os.path.join(root, 'pending.json'))
        _remove_owned(root, journal['stage'], _STAGE)
    os.remove(os.path.join(root, 'transaction.json'))
    _remove_owned(root, journal['backup'], _BACKUP)
    return journal


def _recover(runtime, root, loaded_modules):
    try:
        return _recover_transaction(runtime, root, loaded_modules)
    except Exception as exc:
        journal = _journal(runtime, root)
        if journal and journal['state'] == 'applying':
            raise UpdateRecoveryError('Update recovery remains pending: ' + str(exc))
        raise


def apply_pending(runtime_dir, loaded_modules=None):
    """Apply on the main thread after UI shutdown; roll back any failed write.

    Unchanged files (especially loaded native DLLs) are never replaced. Unknown
    installed files are preserved. A durable journal also handles process exit
    between atomic replacements on the next launcher invocation.
    """
    runtime, root, unused = _layout(runtime_dir, require_installed=True)
    if not any(os.path.isfile(os.path.join(root, name)) for name in ('pending.json', 'transaction.json')):
        return None
    with _lock(root), _installation_lock(runtime):
        data = _metadata(runtime, root)
        if not _journal(runtime, root) and _stale(data, runtime):
            os.remove(os.path.join(root, 'pending.json'))
            _remove_owned(root, data['stage'], _STAGE)
            return None
        if requires_restart(runtime, loaded_modules):
            return {'version': (data or _journal(runtime, root))['version'],
                    'status': 'restart_required', 'requires_restart': True}
        recovered = _recover(runtime, root, loaded_modules)
        data = _metadata(runtime, root)
        if data is None:
            return ({'version': recovered['version'], 'status': 'applied', 'requires_restart': False}
                    if recovered and recovered['state'] == 'committed' else None)
        payload = os.path.join(root, data['stage'], 'payload')
        _validate_payload(payload, data['files'], data['version'])
        actual = _files(runtime)
        if any(name.lower().endswith(('.ma', '.mb')) for name in actual):
            raise UpdateError('Move Maya scene files outside Bake_Groups before updating')
        folded = {name.casefold(): name for name in actual}
        entries = []
        for name, digest in sorted(data['files'].items()):
            if name.casefold() in folded and folded[name.casefold()] != name:
                raise UpdateError('Installed file casing conflicts with update payload')
            previous = actual.get(name)
            old_hash = _sha256(previous) if previous else None
            if old_hash != digest:
                entries.append({'path': name, 'existed': previous is not None,
                                'old_hash': old_hash, 'new_hash': digest})
        backup_name = 'backup-' + uuid.uuid4().hex
        backup = os.path.join(root, backup_name)
        os.mkdir(backup)
        journal = {'schema': 1, 'runtime_dir': runtime, 'version': data['version'],
                   'stage': data['stage'], 'backup': backup_name, 'state': 'applying', 'entries': entries}
        journal_written = False
        try:
            for entry in entries:
                if entry['existed']:
                    path = os.path.join(backup, *_relative(entry['path']))
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    shutil.copy2(actual[entry['path']], path)
                    if _sha256(path) != entry['old_hash']:
                        raise UpdateError('Update backup verification failed')
            _atomic_json(os.path.join(root, 'transaction.json'), journal)
            journal_written = True
            for entry in entries:
                _atomic_copy(os.path.join(payload, *_relative(entry['path'])),
                             os.path.join(runtime, *_relative(entry['path'])))
            journal['state'] = 'committed'
            _atomic_json(os.path.join(root, 'transaction.json'), journal)
        except Exception as exc:
            if journal_written:
                try:
                    _recover(runtime, root, loaded_modules)
                except Exception as recovery:
                    raise UpdateRecoveryError('Update failed; recovery remains pending: ' + str(recovery))
            else:
                _remove_owned(root, backup_name, _BACKUP)
            raise UpdateError('Update failed and the previous files were restored: ' + str(exc))
        result = {'version': data['version'], 'status': 'applied', 'requires_restart': False}
        try:
            _recover(runtime, root, loaded_modules)
        except Exception as exc:
            # All writes committed. Cleanup may safely retry on the next launch.
            result['warning'] = 'Update installed; cache cleanup will retry: ' + str(exc)
        return result
