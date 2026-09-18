"""Lossless journal compaction: only base64 that matches a real output is replaced.

Independent verified blobs survive source-media retention. Existing .result files
remain authoritative; references are read only when the original is absent.
"""
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

SHA = re.compile(r'^[a-f0-9]{64}$')
BASE64 = re.compile(rb'(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{128,}={0,2}(?![A-Za-z0-9+/=])')


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def atomic_new(path, raw):
    """Never overwrite an existing archive/reference; verify content on retry."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix='.cache-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(raw); out.flush(); os.fsync(out.fileno())
        try:
            os.link(temp, path)
        except FileExistsError:
            if path.is_symlink() or path.read_bytes() != raw:
                raise ValueError('archive identity conflict')
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        os.unlink(temp)


def reference_path(path, expected_sha):
    if not SHA.fullmatch(expected_sha):
        raise ValueError('invalid result hash')
    return Path(str(path) + '.' + expected_sha + '.ref')


def read_result(path, expected_sha):
    path = Path(path)
    if path.is_symlink():
        raise ValueError('result symlink refused')
    if path.exists():
        raw = path.read_bytes()
    else:
        ref_path = reference_path(path, expected_sha)
        if ref_path.is_symlink():
            raise ValueError('reference symlink refused')
        ref = json.loads(ref_path.read_bytes())
        if ref.get('version') != 1 or ref.get('sha256') != expected_sha:
            raise ValueError('reference identity conflict')
        parts = []
        for part in ref['parts']:
            if set(part) == {'literal'}:
                parts.append(base64.b64decode(part['literal'], validate=True))
            elif set(part) == {'blob', 'size'} and SHA.fullmatch(part['blob']):
                blob = path.parent / '.result-blobs' / part['blob']
                if blob.is_symlink() or blob.parent.is_symlink():
                    raise ValueError('blob symlink refused')
                binary = blob.read_bytes()
                if len(binary) != part['size'] or digest(binary) != part['blob']:
                    raise ValueError('blob checksum mismatch')
                parts.append(base64.b64encode(binary))
            elif set(part) == {'output', 'sha256', 'size'} and SHA.fullmatch(part['sha256']):
                # Explicit retirement uses an existing verified original. Once
                # source retention expires this MUST fail, never create again.
                project = Path(ref['project_root']).resolve()
                if not path.resolve().is_relative_to(project / '.remote_jobs'):
                    raise ValueError('retired cache project identity mismatch')
                output = project / part['output']
                if output.is_symlink() or not output.resolve().is_relative_to(project):
                    raise ValueError('retired cache output escaped project')
                binary = output.read_bytes()
                if len(binary) != part['size'] or digest(binary) != part['sha256']:
                    raise ValueError('retired cache original missing or changed; no paid retry')
                parts.append(base64.b64encode(binary))
            else:
                raise ValueError('invalid result reference')
        raw = b''.join(parts)
        if len(raw) != ref['bytes']:
            raise ValueError('result size mismatch')
    if digest(raw) != expected_sha:
        raise ValueError('result checksum mismatch')
    return raw


def compact_result(record_path, output_hashes, apply=False, retire_to_project=None):
    """output_hashes maps SHA256 to an existing regular output path; no provider IO."""
    record_path = Path(record_path)
    with open(str(record_path) + '.lock', 'a+b') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        record = json.loads(record_path.read_bytes())
        if record.get('state') != 'done':
            return {'state': 'retained_unsettled', 'saved_bytes': 0}
        path = record_path.with_suffix('.result')
        expected = record['result_sha256']
        if not path.exists():
            previous=reference_path(path,expected)
            if previous.exists() and json.loads(previous.read_bytes()).get('cache_retired') is True:
                return {'state':'already_retired','saved_bytes':0}
        raw = read_result(path, expected)
        if not path.exists():
            return {'state': 'already_compacted', 'saved_bytes': 0}
        before = path.stat()
        parts, binaries, cursor = [], {}, 0
        for match in BASE64.finditer(raw):
            try:
                binary = base64.b64decode(match.group(), validate=True)
            except ValueError:
                continue
            sha = digest(binary)
            output = output_hashes.get(sha)
            if not output or base64.b64encode(binary) != match.group():
                continue
            output = Path(output)
            if output.is_symlink() or not output.is_file() or digest(output.read_bytes()) != sha:
                continue
            parts.append({'literal': base64.b64encode(raw[cursor:match.start()]).decode()})
            if retire_to_project is None:
                parts.append({'blob': sha, 'size': len(binary)})
            else:
                project = Path(retire_to_project).resolve()
                if not output.resolve().is_relative_to(project):
                    raise ValueError('retirement output outside project')
                parts.append({'output': str(output.resolve().relative_to(project)), 'sha256': sha, 'size': len(binary)})
            binaries[sha] = binary
            cursor = match.end()
        if not binaries:
            return {'state': 'retained_no_matching_output', 'saved_bytes': 0}
        parts.append({'literal': base64.b64encode(raw[cursor:]).decode()})
        reference = {'version': 1, 'sha256': expected, 'bytes': len(raw), 'parts': parts}
        if retire_to_project is not None:
            reference.update(cache_retired=True, project_root=str(Path(retire_to_project).resolve()))
        ref = json.dumps(reference, sort_keys=True).encode()
        newly_stored = 0 if retire_to_project is not None else sum(len(raw) for sha, raw in binaries.items() if not (path.parent / '.result-blobs' / sha).exists())
        saved = len(raw) - newly_stored - len(ref)
        if saved <= 0:
            return {'state': 'retained_no_savings', 'saved_bytes': 0}
        if apply:
            if retire_to_project is None:
                for sha, binary in binaries.items():
                    atomic_new(path.parent / '.result-blobs' / sha, binary)
            ref_path = reference_path(path, expected)
            atomic_new(ref_path, ref)
            # Verify the reconstruction before deleting the authoritative raw file.
            reconstructed = []
            for p in parts:
                if 'literal' in p:reconstructed.append(base64.b64decode(p['literal']))
                elif 'output' in p:reconstructed.append(base64.b64encode((Path(retire_to_project)/p['output']).read_bytes()))
                else:reconstructed.append(base64.b64encode((path.parent / '.result-blobs' / p['blob']).read_bytes()))
            if b''.join(reconstructed) != raw:
                raise ValueError('archive restore mismatch')
            after = path.stat()
            if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns) or digest(path.read_bytes()) != expected:
                raise ValueError('result changed during compaction')
            path.unlink()
            fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            if read_result(path, expected) != raw:
                raise ValueError('restored result mismatch')
        return {'state': 'compacted' if apply else 'eligible', 'saved_bytes': saved, 'original_bytes': len(raw), 'blobs': len(binaries)}
