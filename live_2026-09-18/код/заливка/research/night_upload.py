#!/usr/bin/env python3
"""Sequential, opt-in Minsk upload runner for the frozen 30-channel WW2 batch.

Default: offline asset preflight only. --start invokes the existing upload
manager protocol; a successful worker result is still awaiting live readback.
No profile or upload action is performed when this module is imported.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import csv
from datetime import datetime, timezone
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import sys
import time
from types import SimpleNamespace
import unicodedata
import uuid
from urllib.parse import parse_qs, urlparse


BASE = Path('/home/vova/ww2_night_20260914')
SOURCE_ROOT = Path('/home/vova/VideoStudio')
PYTHON = SOURCE_ROOT / '.venv-upload/bin/python'
ADS_API = 'http://localhost:50325'
ADS_HOSTS = 'minsk=http://localhost:50325,mac=http://100.100.244.89:50325'
MINSK_HOSTNAME = 'vova-System-Product-Name'
LEASE_COORDINATOR = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', '--',
                     'video-ops-profile-leases-vps', '/opt/video-ops/upload_profile_lease.py']
UC_RE = re.compile(r'UC[A-Za-z0-9_-]{22}')
PID_RE = re.compile(r'p_[a-f0-9]{10}')
HASH_RE = re.compile(r'[a-f0-9]{64}')
VIDEO_ID_RE = re.compile(r'[A-Za-z0-9_-]{11}')
# The reviewed recovery is one specific incomplete upload, not a general bypass.
CANCELLATION_SCOPE = {
    'project_id': 'p_24f361ac3d', 'channel_uc': 'UCN9rwIpHw-UxhRBYBFdOfOw',
    'profile_id': '54', 'old_video_id': '4gZgVK3fFnM',
    'video_sha256': '34194df9fbe62cbe021645026b55eb8d763d52c85298a65336a895dd1a2eb1e7',
}


class Blocked(RuntimeError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def title_hash(title):
    value = re.sub(r'\s+', ' ', unicodedata.normalize('NFC', title)).strip().casefold()
    return hashlib.sha256(value.encode()).hexdigest()


def require_finished_attempt(record, attempt):
    """Bind readback/confirmation to one terminal worker result, never running."""
    if (record.get('status') not in ('needs_readback', 'confirmed_public')
            or attempt.get('status') != 'needs_readback' or not attempt.get('result_json')
            or record.get('phase') != 'video_id_known' or attempt.get('phase') != 'video_id_known'
            or record.get('attempt_id') != attempt.get('attempt_id')
            or any(record.get(key) != attempt.get(key) for key in ('channel_uc', 'project_id', 'video_id'))):
        raise Blocked('Readback requires the current finished attempt; running/unfinished target is blocked')
    try:
        result = json.loads(attempt['result_json'])
    except (TypeError, ValueError) as exc:
        raise Blocked('Current attempt terminal result is unreadable') from exc
    if (not isinstance(result, dict) or result.get('phase') != 'video_id_known'
            or result.get('video_id') != record.get('video_id')
            or result.get('do_not_reupload') is not True or type(result.get('success')) is not bool):
        raise Blocked('Current attempt lacks an exact terminal worker result')
    return result


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path):
    with Path(path).open(encoding='utf-8') as handle:
        return json.load(handle)


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise Blocked(f'Cannot load {path}')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_manifest(path, expected_count=30):
    raw = Path(path).read_bytes()
    value = json.loads(raw)
    rows = value.get('candidates') if isinstance(value, dict) else value
    if not isinstance(rows, list) or len(rows) != expected_count:
        raise Blocked(f'Manifest must contain exactly {expected_count} candidate rows')
    seen_uc, seen_pid = set(), set()
    for row in rows:
        uc, pid = row.get('channel_uc', ''), row.get('project_id', '')
        if not UC_RE.fullmatch(uc) or not PID_RE.fullmatch(pid):
            raise Blocked('Invalid UC or project ID')
        if uc in seen_uc or pid in seen_pid:
            raise Blocked('Manifest repeats a channel or a project')
        if not str(row.get('profile_id', '')).isdigit():
            raise Blocked(f'{pid}: explicit numeric profile_id required')
        title = row.get('publication_title')
        if not isinstance(title, str) or not title.strip() or len(title) > 100:
            raise Blocked(f'{pid}: a nonempty publication title of <=100 characters is required')
        seen_uc.add(uc)
        seen_pid.add(pid)
    return rows, hashlib.sha256(raw).hexdigest()


def file_in_project(path, project):
    resolved = Path(path).resolve()
    if not resolved.is_relative_to(project.resolve()) or not resolved.is_file():
        raise Blocked(f'Asset must be a regular file inside {project}')
    return resolved


def verify_assets(base, row):
    pid, uc = row['project_id'], row['channel_uc']
    project = base / pid
    media = read_json(project / 'media_verified.json')
    approval = read_json(project / 'cover_approval.json')
    for record in (media, approval):
        if record.get('project_id') != pid or record.get('channel_uc') != uc:
            raise Blocked(f'{pid}: asset approval identity mismatch')
    if media.get('verified') is not True:
        raise Blocked(f'{pid}: media is not verified')
    if any(approval.get(key) is not True for key in ('approved', 'has_headline', 'readable')):
        raise Blocked(f'{pid}: headline/legibility/cover approval missing')
    video = file_in_project(media.get('path', ''), project)
    cover = file_in_project(approval.get('path', ''), project)
    if video.suffix.lower() != '.mp4' or cover.suffix.lower() not in ('.jpg', '.jpeg', '.png'):
        raise Blocked(f'{pid}: unexpected media extension')
    if video == cover or video.stat().st_size <= 0 or cover.stat().st_size <= 0:
        raise Blocked(f'{pid}: empty or identical media files')
    if media.get('bytes') != video.stat().st_size:
        raise Blocked(f'{pid}: video byte count changed')
    for path, record in ((video, media), (cover, approval)):
        expected = record.get('sha256', '')
        if not HASH_RE.fullmatch(expected) or sha256(path) != expected:
            raise Blocked(f'{pid}: asset SHA256 mismatch: {path.name}')
    probe = media.get('media') or {}
    stream_types = {stream.get('codec_type') for stream in probe.get('streams', [])}
    if not {'audio', 'video'} <= stream_types or float((probe.get('format') or {}).get('duration', 0)) <= 0:
        raise Blocked(f'{pid}: media probe does not prove audio/video/duration')
    if not isinstance(row.get('description'), str) or not row['description'].strip():
        raise Blocked(f'{pid}: prepared description required before manifest freeze')
    return {'video': video, 'cover': cover, 'video_sha256': media['sha256'],
            'cover_sha256': approval['sha256']}


def normalize_cancellation_raw(kind, raw, row, record):
    """Derive receipt fields from typed, unchanged DOM-reader output.

    Envelope v1: schema_version, kind, observed_at (UTC ISO), profile_id,
    video_id, channel_uc, snapshot. `snapshot` is the direct JSON result of
    the observed READ_EXACT_WIZARD_JS (before/transfer) or READ_CANCEL_ROW_JS
    (content). It must not contain generated success/absence summary flags.

    No observed contract yet proves Content completeness, cancellation
    confirmation, direct-ID not-found or absence of every transfer. Those
    conclusions remain UNKNOWN/BLOCKED, without a CLI/env enable switch.
    """
    envelope = {'schema_version', 'kind', 'observed_at', 'profile_id', 'video_id', 'channel_uc', 'snapshot'}
    if (not isinstance(raw, dict) or set(raw) != envelope or type(raw.get('schema_version')) is not int
            or raw['schema_version'] != 1 or raw['kind'] != kind
            or raw['profile_id'] != str(row['profile_id']) or raw['video_id'] != record['video_id']
            or raw['channel_uc'] != row['channel_uc'] or not isinstance(raw['snapshot'], dict)):
        raise Blocked('Unknown or mismatched typed raw cancellation evidence')
    try:
        captured = datetime.fromisoformat(str(raw['observed_at']).replace('Z', '+00:00'))
    except ValueError as exc:
        raise Blocked('Raw cancellation timestamp is invalid') from exc
    if captured.tzinfo is None or captured > datetime.now(timezone.utc):
        raise Blocked('Raw cancellation timestamp is naive or in the future')
    snapshot = raw['snapshot']
    observed = snapshot.get('observed_at')
    if (isinstance(observed, bool) or not isinstance(observed, (int, float))
            or not abs(captured.timestamp() - observed) <= 1):
        raise Blocked('Raw DOM timestamp does not match the evidence envelope')
    route = urlparse(str(snapshot.get('url', '')))
    if route.scheme != 'https' or route.hostname != 'studio.youtube.com':
        raise Blocked('Raw cancellation evidence is outside Studio or at authentication')

    if kind in ('before', 'transfer'):
        allowed = {'present', 'url', 'observed_at', 'videoId', 'uploadMode', 'isVideoUploaded',
                   'hasError', 'errorShortMessage', 'video', 'entries'}
        if set(snapshot) - allowed:
            raise Blocked('Unknown fields in raw wizard evidence; no summary flags accepted')
        if route.path != f'/video/{record["video_id"]}/edit':
            raise Blocked('Raw wizard URL does not match the cancelled exact ID')
        if (snapshot.get('present') is not True or snapshot.get('videoId') != record['video_id']
                or snapshot.get('uploadMode') != 'EXISTING_DRAFT'
                or snapshot.get('hasError') is not False or snapshot.get('errorShortMessage') != ''):
            raise Blocked('Raw wizard is missing, ambiguous or reports an error')
        video, entries = snapshot.get('video'), snapshot.get('entries')
        if not isinstance(video, dict) or not isinstance(entries, list):
            raise Blocked('Raw wizard video/transfer data missing')
        if (set(video) - {'videoId', 'channelId', 'title', 'privacy', 'draftStatus', 'effectiveStatus', 'userSetVisibility'}
                or any(not isinstance(e, dict) or set(e) - {'videoId', 'local', 'status', 'fraction', 'abandoned',
                                                               'filename', 'durationMs', 'width', 'height'} for e in entries)):
            raise Blocked('Unknown fields in raw video/transfer data')
        if (video.get('videoId') != record['video_id'] or video.get('channelId') != row['channel_uc']
                or title_hash(str(video.get('title', ''))) != record['title_sha256']):
            raise Blocked('Raw wizard video identity/title mismatch')
        if kind == 'transfer':
            # A pending exact entry proves a surviving transfer. An absent entry
            # does NOT prove that every server/browser upload session is gone.
            pending = any(isinstance(e, dict) and e.get('videoId') == record['video_id']
                          and e.get('status') == 'PENDING' for e in entries)
            return {'active': True if pending else None, 'read_complete': None}
        if (snapshot.get('isVideoUploaded') is not False
                or video.get('privacy') != 'VIDEO_PRIVACY_PRIVATE'
                or video.get('draftStatus') not in ('DRAFT_STATUS_PUBLIC', 'DRAFT_STATUS_PRIVATE')
                or video.get('effectiveStatus') != 'VIDEO_VISIBILITY_STATUS_DRAFT'
                or video.get('userSetVisibility') != 'VIDEO_USER_SET_VISIBILITY_DRAFT'):
            raise Blocked('Raw before-state is not an incomplete private draft')
        if len(entries) != 1 or not isinstance(entries[0], dict):
            raise Blocked('Raw before-state has no unique transfer entry')
        entry = entries[0]
        fraction = entry.get('fraction')
        if (entry.get('videoId') != record['video_id'] or entry.get('local') is not False
                or entry.get('status') != 'PENDING' or entry.get('filename') != 'video.mp4'
                or isinstance(fraction, bool) or not isinstance(fraction, (int, float))
                or not 0 <= fraction < 1):
            raise Blocked('Raw before-transfer is foreign, completed or unknown')
        return {'observed_at': raw['observed_at'], 'video_id': video['videoId'],
                'channel_uc': video['channelId'], 'title_sha256': title_hash(video['title']),
                'privacy': video['privacy'], 'effectiveStatus': video['effectiveStatus'],
                'isVideoUploaded': snapshot['isVideoUploaded']}
    if kind == 'content':
        if set(snapshot) != {'url', 'observed_at', 'all_video_ids', 'rows'}:
            raise Blocked('Unknown fields in raw Content evidence; no summary flags accepted')
        if route.path != f'/channel/{row["channel_uc"]}/videos/upload':
            raise Blocked('Raw Content URL is not the exact channel')
        ids, rows = snapshot['all_video_ids'], snapshot['rows']
        if (not isinstance(ids, list) or any(not isinstance(v, str) or not VIDEO_ID_RE.fullmatch(v) for v in ids)
                or not isinstance(rows, list) or any(not isinstance(r, dict) or not isinstance(r.get('video'), dict) for r in rows)):
            raise Blocked('Raw Content rows are incomplete or malformed')
        present = record['video_id'] in ids or any((r.get('video') or {}).get('videoId') == record['video_id'] for r in rows)
        return {'exact_uc': row['channel_uc'], 'unfiltered': parse_qs(route.query).get('filter') == ['[]'],
                'old_id_absent': False if present else None,
                'complete': None, 'auth_gate': None}
    # These DOM contracts must be obtained from a real cancellation probe.
    # Do not interpret a manually written not_found/acknowledged boolean.
    raise Blocked(f'Raw {kind} cancellation DOM contract is unobserved; import/retry disarmed')


def validate_cancellation_proof(row, record, assets, proof_path, expected_hash, project):
    """Accept only the reviewed terminal cancellation receipt, never an intent."""
    path = file_in_project(proof_path, project)
    raw = path.read_bytes()
    if not HASH_RE.fullmatch(expected_hash or '') or hashlib.sha256(raw).hexdigest() != expected_hash:
        raise Blocked('Cancellation proof SHA256 mismatch')
    proof = json.loads(raw)
    if (not isinstance(proof, dict) or type(proof.get('schema_version')) is not int
            or proof.get('schema_version') != 1 or proof.get('event') != 'incomplete_upload_cancelled'):
        raise Blocked('Terminal incomplete cancellation proof required; intent is insufficient')
    try:
        uuid.UUID(proof['cancellation_id'])
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        raise Blocked('Cancellation operation ID is invalid') from exc
    expected = {'project_id': row['project_id'], 'channel_uc': row['channel_uc'],
                'profile_id': str(row['profile_id']), 'old_video_id': record['video_id'],
                'prior_attempt_id': record['attempt_id'], 'manifest_sha256': record['manifest_sha256'],
                'video_sha256': record['video_sha256'], 'cover_sha256': record['cover_sha256']}
    if (any(proof.get(k) != v for k, v in expected.items())
            or any(proof.get(k) != v for k, v in CANCELLATION_SCOPE.items())
            or any(assets[k] != record[k] for k in ('video_sha256', 'cover_sha256'))
            or record['profile_id'] != str(row['profile_id'])
            or record['title_sha256'] != title_hash(row['publication_title'])):
        raise Blocked('Cancellation proof exact identity/frozen assets mismatch')
    if record['status'] == 'confirmed_public' or record['confirmed_proof']:
        raise Blocked('Confirmed PUBLIC can never authorize cancellation replacement')
    if record['phase'] != 'video_id_known' or not record['attempt_id']:
        raise Blocked('Cancellation requires a durable exact ID and prior attempt')

    def evidence(link):
        if not isinstance(link, dict) or not HASH_RE.fullmatch(str(link.get('sha256', ''))):
            raise Blocked('Cancellation evidence link/hash missing')
        target = file_in_project(link.get('path', ''), project)
        data = target.read_bytes()
        if not data or hashlib.sha256(data).hexdigest() != link['sha256']:
            raise Blocked('Cancellation evidence SHA256 mismatch')
        return data

    before, confirmation, after = (proof.get(k) or {} for k in ('before', 'confirmation', 'after'))
    if any(not isinstance(part, dict) for part in (before, confirmation, after)):
        raise Blocked('Cancellation observations must be structured evidence objects')
    if (before.get('video_id') != record['video_id'] or before.get('channel_uc') != row['channel_uc']
            or before.get('title_sha256') != record['title_sha256']
            or before.get('privacy') != 'VIDEO_PRIVACY_PRIVATE'
            or before.get('effectiveStatus') != 'VIDEO_VISIBILITY_STATUS_DRAFT'
            or before.get('isVideoUploaded') is not False):
        raise Blocked('Cancellation before-state is not this exact incomplete private draft')
    if confirmation.get('acknowledged') is not True:
        raise Blocked('Observed cancellation confirmation is required')
    content, direct, transfer = (after.get(k) or {} for k in ('content', 'direct', 'transfer'))
    if any(not isinstance(part, dict) for part in (content, direct, transfer)):
        raise Blocked('Cancellation readbacks must be structured evidence objects')
    if (content.get('exact_uc') != row['channel_uc']
            or any(content.get(k) is not True for k in ('complete', 'unfiltered', 'old_id_absent'))
            or content.get('auth_gate') is not False
            or direct.get('requested_video_id') != record['video_id']
            or direct.get('not_found') is not True
            or direct.get('auth_gate') is not False or direct.get('error') is not False
            or transfer.get('active') is not False or transfer.get('read_complete') is not True):
        raise Blocked('Cancellation absence/complete Content/direct-ID/transfer proof is incomplete')
    intent = json.loads(evidence(proof.get('intent')))
    if (not isinstance(intent, dict) or intent.get('event') != 'cancel_intent_prepared'
            or intent.get('cancellation_id') != proof['cancellation_id']
            or any(intent.get(k) != v for k, v in expected.items())
            or intent.get('title_sha256') != record['title_sha256']):
        raise Blocked('Cancellation intent is not bound to this operation and frozen upload')
    for kind, part in (('before', before), ('content', content), ('transfer', transfer),
                       ('direct', direct), ('confirmation', confirmation)):
        try:
            raw_observation = json.loads(evidence(part.get('evidence')))
        except (ValueError, UnicodeDecodeError) as exc:
            raise Blocked('Raw cancellation evidence is not valid JSON') from exc
        derived = normalize_cancellation_raw(kind, raw_observation, row, record)
        if any(value is None or part.get(key) != value for key, value in derived.items()):
            raise Blocked(f'Raw {kind} contradicts receipt or cannot establish the required conclusion')
    try:
        times = [datetime.fromisoformat(str(item.get('observed_at', '')).replace('Z', '+00:00'))
                 for item in (before, intent, confirmation, after, proof)]
    except ValueError as exc:
        raise Blocked('Cancellation evidence timestamp missing or invalid') from exc
    if any(t.tzinfo is None for t in times) or times != sorted(times) or times[-1] > datetime.now(timezone.utc):
        raise Blocked('Cancellation evidence timestamps are out of order or in the future')
    return proof, path


@contextmanager
def exclusive_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Blocked(f'Another runner/profile operation holds {path}') from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class Ledger:
    """SQLite FULL commits finish before the manager is allowed to ACK a phase."""
    def __init__(self, path, contract):
        self.path, self.contract = Path(path), contract
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=DELETE')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS uploads (
            channel_uc TEXT NOT NULL UNIQUE, project_id TEXT NOT NULL UNIQUE,
            profile_id TEXT NOT NULL, manifest_sha256 TEXT NOT NULL,
            title_sha256 TEXT NOT NULL, video_sha256 TEXT NOT NULL DEFAULT '',
            cover_sha256 TEXT NOT NULL DEFAULT '', phase TEXT NOT NULL DEFAULT 'preflight',
            video_id TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'ready',
            last_error TEXT NOT NULL DEFAULT '', attempt_id TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL, confirmed_proof TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(channel_uc, project_id));
          CREATE TABLE IF NOT EXISTS attempts (
            attempt_id TEXT PRIMARY KEY, channel_uc TEXT NOT NULL, project_id TEXT NOT NULL,
            mode TEXT NOT NULL, started_at TEXT NOT NULL, phase TEXT NOT NULL,
            video_id TEXT NOT NULL, status TEXT NOT NULL, result_json TEXT NOT NULL DEFAULT '',
            log_path TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS cancellations (
            cancellation_id TEXT PRIMARY KEY, channel_uc TEXT NOT NULL UNIQUE,
            project_id TEXT NOT NULL UNIQUE, old_video_id TEXT NOT NULL UNIQUE,
            prior_attempt_id TEXT NOT NULL, proof_sha256 TEXT NOT NULL UNIQUE,
            proof_path TEXT NOT NULL, proof_json TEXT NOT NULL,
            prior_uploads_json TEXT NOT NULL, accepted_at TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS cancellation_consumptions (
            cancellation_id TEXT PRIMARY KEY REFERENCES cancellations(cancellation_id),
            replacement_attempt_id TEXT NOT NULL UNIQUE REFERENCES attempts(attempt_id),
            consumed_at TEXT NOT NULL);
          CREATE TRIGGER IF NOT EXISTS cancellations_no_update
            BEFORE UPDATE ON cancellations BEGIN SELECT RAISE(ABORT,'Cancellation history is append-only'); END;
          CREATE TRIGGER IF NOT EXISTS cancellations_no_delete
            BEFORE DELETE ON cancellations BEGIN SELECT RAISE(ABORT,'Cancellation history is append-only'); END;
          CREATE TRIGGER IF NOT EXISTS consumption_no_update
            BEFORE UPDATE ON cancellation_consumptions BEGIN SELECT RAISE(ABORT,'Cancellation consumption is append-only'); END;
          CREATE TRIGGER IF NOT EXISTS consumption_no_delete
            BEFORE DELETE ON cancellation_consumptions BEGIN SELECT RAISE(ABORT,'Cancellation consumption is append-only'); END;
          CREATE TRIGGER IF NOT EXISTS cancelled_attempt_no_update BEFORE UPDATE ON attempts
            WHEN EXISTS (SELECT 1 FROM cancellations c WHERE c.channel_uc=OLD.channel_uc
              AND c.project_id=OLD.project_id AND (c.old_video_id=OLD.video_id OR c.prior_attempt_id=OLD.attempt_id))
            BEGIN SELECT RAISE(ABORT,'Cancelled attempt history is immutable'); END;
          CREATE TRIGGER IF NOT EXISTS cancelled_attempt_no_delete BEFORE DELETE ON attempts
            WHEN EXISTS (SELECT 1 FROM cancellations c WHERE c.channel_uc=OLD.channel_uc
              AND c.project_id=OLD.project_id AND (c.old_video_id=OLD.video_id OR c.prior_attempt_id=OLD.attempt_id))
            BEGIN SELECT RAISE(ABORT,'Cancelled attempt history is immutable'); END;
        ''')
        self.sync()

    def close(self):
        self.db.close()

    def sync(self):
        self.db.commit()
        with self.path.open('rb') as handle:
            os.fsync(handle.fileno())
        fd = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def freeze(self, rows, manifest_hash):
        previous = self.db.execute("SELECT value FROM meta WHERE key='manifest_sha256'").fetchone()
        if previous and previous[0] != manifest_hash:
            raise Blocked('Frozen manifest changed; existing ledger cannot be reused')
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO meta VALUES ('manifest_sha256',?)", (manifest_hash,))
            for row in rows:
                self.db.execute('''INSERT OR IGNORE INTO uploads
                    (channel_uc,project_id,profile_id,manifest_sha256,title_sha256,updated_at)
                    VALUES (?,?,?,?,?,?)''', (row['channel_uc'], row['project_id'], str(row['profile_id']),
                                             manifest_hash, title_hash(row['publication_title']), now()))
        self.sync()

    def get(self, row):
        record = self.db.execute('SELECT * FROM uploads WHERE channel_uc=? AND project_id=?',
                                 (row['channel_uc'], row['project_id'])).fetchone()
        if record is None:
            raise Blocked('Row is absent from the frozen ledger')
        return dict(record)

    def cancellation(self, row):
        record = self.db.execute('SELECT * FROM cancellations WHERE channel_uc=? AND project_id=?',
                                 (row['channel_uc'], row['project_id'])).fetchone()
        return dict(record) if record is not None else None

    def bind_assets(self, row, assets):
        record = self.get(row)
        for key in ('video_sha256', 'cover_sha256'):
            if record[key] and record[key] != assets[key]:
                raise Blocked(f'{row["project_id"]}: frozen {key} changed')
        self.db.execute('UPDATE uploads SET video_sha256=?,cover_sha256=? WHERE channel_uc=? AND project_id=?',
                        (assets['video_sha256'], assets['cover_sha256'], row['channel_uc'], row['project_id']))
        self.sync()

    def import_canary(self, row, event, result=None):
        event = self.contract.validate_phase_event(event)
        if result is not None:
            result = self.contract.validate_result(result)
            event = self.contract.advance_phase_event(event, result['phase'], result['video_id'])
        old = self.get(row)
        merged = self.contract.advance_phase_event(
            {'upload_phase_schema_version': 1, 'phase': old['phase'], 'video_id': old['video_id']},
            event['phase'], event['video_id'])
        self.db.execute('''UPDATE uploads SET phase=?,video_id=?,status=?,last_error=?,updated_at=?
                           WHERE channel_uc=? AND project_id=?''',
                        (merged['phase'], merged['video_id'], 'needs_readback',
                         'Imported diagnostic canary; never proof of PUBLIC', now(),
                         row['channel_uc'], row['project_id']))
        self.sync()

    def _cancellation_prior(self, row):
        record = self.get(row)
        prior = self.db.execute('SELECT * FROM attempts WHERE attempt_id=?', (record['attempt_id'],)).fetchone()
        if (prior is None or prior['channel_uc'] != row['channel_uc']
                or prior['project_id'] != row['project_id'] or prior['video_id'] != record['video_id']):
            raise Blocked('Cancellation has no matching prior attempt and exact video ID')
        for attempt in self.db.execute('SELECT result_json FROM attempts WHERE channel_uc=? AND project_id=? AND video_id=?',
                                       (row['channel_uc'], row['project_id'], record['video_id'])):
            result = json.loads(attempt['result_json'] or '{}')
            if result.get('success') is True or result.get('visibility') in ('PUBLIC', 'PUBLIC_PENDING', 'SCHEDULED'):
                raise Blocked('Completed/published prior upload cannot authorize incomplete cancellation')
        return record, dict(prior)

    def register_cancellation(self, row, assets, proof_path, proof_sha256, project):
        # A terminal receipt adds history only; the current uploads row and all
        # prior attempts remain byte-for-byte unchanged until explicit consume.
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            if self.cancellation(row):
                raise Blocked('Cancellation already recorded; receipts cannot be replaced or reused')
            record, prior = self._cancellation_prior(row)
            proof, path = validate_cancellation_proof(row, record, assets, proof_path, proof_sha256, project)
            before_time = datetime.fromisoformat(proof['before']['observed_at'].replace('Z', '+00:00'))
            if before_time < datetime.fromisoformat(prior['started_at']):
                raise Blocked('Cancellation observation predates the prior attempt')
            self.db.execute('INSERT INTO cancellations VALUES (?,?,?,?,?,?,?,?,?,?)',
                            (proof['cancellation_id'], row['channel_uc'], row['project_id'], record['video_id'],
                             record['attempt_id'], proof_sha256, str(path),
                             json.dumps(proof, ensure_ascii=False, sort_keys=True),
                             json.dumps(record, ensure_ascii=False, sort_keys=True), now()))
        self.sync()
        return proof['cancellation_id']

    def replacement_permission(self, row, assets, old_id, proof_sha256):
        cancelled = self.cancellation(row)
        if (not cancelled or old_id != cancelled['old_video_id']
                or proof_sha256 != cancelled['proof_sha256']):
            raise Blocked('Exact old ID and registered cancellation proof SHA256 required')
        if self.db.execute('SELECT 1 FROM cancellation_consumptions WHERE cancellation_id=?',
                           (cancelled['cancellation_id'],)).fetchone():
            raise Blocked('NO REUPLOAD: cancellation replacement permission already consumed')
        record, _ = self._cancellation_prior(row)
        prior = json.loads(cancelled['prior_uploads_json'])
        for key in ('channel_uc', 'project_id', 'profile_id', 'manifest_sha256', 'title_sha256',
                    'video_sha256', 'cover_sha256', 'phase', 'video_id', 'attempt_id'):
            if record[key] != prior[key]:
                raise Blocked('Upload identity changed after cancellation was recorded')
        proof, _ = validate_cancellation_proof(row, record, assets, cancelled['proof_path'],
                                               proof_sha256, self.path.parent / row['project_id'])
        if proof != json.loads(cancelled['proof_json']):
            raise Blocked('Stored immutable cancellation proof differs from the receipt')
        return cancelled

    def begin_replacement(self, row, assets, log_path, old_id, proof_sha256):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            cancelled = self.replacement_permission(row, assets, old_id, proof_sha256)
            attempt = self._insert_replacement(row, log_path, cancelled)
        self.sync()  # return/worker invocation only after durable consumption
        return attempt

    def _insert_replacement(self, row, log_path, cancelled):
        """Storage only, called after proof validation in the same transaction.

        Kept separate so atomic-history tests do not pretend that synthetic
        cancellation DOM is an accepted real terminal proof.
        """
        if not self.db.in_transaction:
            raise Blocked('Replacement storage requires the surrounding validation transaction')
        registered = self.cancellation(row)
        record = self.get(row)
        if (registered is None or registered != cancelled or record['status'] == 'confirmed_public'
                or record['confirmed_proof'] or record['video_id'] != registered['old_video_id']
                or record['attempt_id'] != registered['prior_attempt_id']):
            raise Blocked('Replacement storage identity/history changed after validation')
        attempt, started = uuid.uuid4().hex, now()
        self.db.execute('INSERT INTO attempts VALUES (?,?,?,?,?,?,?,?,?,?)',
                        (attempt, row['channel_uc'], row['project_id'], 'replacement_cancelled_incomplete',
                         started, 'preflight', '', 'running', '', str(log_path)))
        self.db.execute('INSERT INTO cancellation_consumptions VALUES (?,?,?)',
                        (cancelled['cancellation_id'], attempt, started))
        # Current pointer only: immutable cancellations/attempts retain old ID.
        self.db.execute('''UPDATE uploads SET phase='preflight',video_id='',status='running',
                           last_error='',attempt_id=?,updated_at=? WHERE channel_uc=? AND project_id=?''',
                        (attempt, started, row['channel_uc'], row['project_id']))
        return attempt

    def begin(self, row, assets, log_path, resume_id='', cancelled_id='', cancellation_proof_sha256=''):
        record = self.get(row)
        if record['status'] == 'confirmed_public':
            raise Blocked('Already confirmed PUBLIC; no upload action allowed')
        if cancelled_id or cancellation_proof_sha256:
            if resume_id:
                raise Blocked('Cancellation replacement and same-ID finalize are separate operations')
            return self.begin_replacement(row, assets, log_path, cancelled_id, cancellation_proof_sha256)
        cancelled = self.cancellation(row)
        if cancelled and (not resume_id or resume_id == cancelled['old_video_id']):
            raise Blocked('NO REUPLOAD: cancelled identity requires its unconsumed explicit replacement permission')
        unsafe = self.contract.phase_requires_no_reupload(record['phase']) or bool(record['video_id'])
        if resume_id:
            if record['video_id'] != resume_id or record['phase'] != 'video_id_known':
                raise Blocked('Explicit finalize ID must equal the durable known video ID')
        elif unsafe:
            raise Blocked('NO REUPLOAD: upload_started/known ID is durable; reconcile this same video')
        self.bind_assets(row, assets)
        attempt = uuid.uuid4().hex
        phase, video_id = ('video_id_known', resume_id) if resume_id else ('preflight', '')
        with self.db:
            self.db.execute('INSERT INTO attempts VALUES (?,?,?,?,?,?,?,?,?,?)',
                            (attempt, row['channel_uc'], row['project_id'],
                             'finalize_known_id' if resume_id else 'fresh', now(), phase, video_id,
                             'running', '', str(log_path)))
            self.db.execute('''UPDATE uploads SET phase=?,video_id=?,status='running',last_error='',
                               attempt_id=?,updated_at=? WHERE channel_uc=? AND project_id=?''',
                            (phase, video_id, attempt, now(), row['channel_uc'], row['project_id']))
        self.sync()
        return attempt

    def phase(self, attempt, event):
        event = self.contract.validate_phase_event(event)
        current = self.db.execute('SELECT * FROM attempts WHERE attempt_id=?', (attempt,)).fetchone()
        if current is None:
            raise Blocked('Unknown attempt; cannot ACK')
        if current['mode'] == 'replacement_cancelled_incomplete' and event['video_id']:
            cancelled = self.cancellation(dict(current))
            if cancelled and event['video_id'] == cancelled['old_video_id']:
                raise Blocked('Replacement returned the cancelled old video ID; reconcile, never retry')
        # A finalize worker resets its own process state before announcing the
        # requested ID. The ledger already knows that ID and must not regress.
        if current['mode'] == 'finalize_known_id' and event['phase'] == 'preflight':
            self.sync()
            return
        merged = self.contract.advance_phase_event(
            {'upload_phase_schema_version': 1, 'phase': current['phase'], 'video_id': current['video_id']},
            event['phase'], event['video_id'])
        with self.db:
            self.db.execute('UPDATE attempts SET phase=?,video_id=? WHERE attempt_id=?',
                            (merged['phase'], merged['video_id'], attempt))
            self.db.execute('UPDATE uploads SET phase=?,video_id=?,updated_at=? WHERE attempt_id=?',
                            (merged['phase'], merged['video_id'], now(), attempt))
        self.sync()

    def finish(self, attempt, result):
        result = self.contract.validate_result(result)
        self.phase(attempt, {'upload_phase_schema_version': 1,
                             'phase': result['phase'], 'video_id': result['video_id']})
        status = 'needs_readback' if result['do_not_reupload'] else 'failed_safe_to_retry'
        with self.db:
            self.db.execute('UPDATE attempts SET status=?,result_json=? WHERE attempt_id=?',
                            (status, json.dumps(result, ensure_ascii=False), attempt))
            self.db.execute('UPDATE uploads SET status=?,last_error=?,updated_at=? WHERE attempt_id=?',
                            (status, result['error'], now(), attempt))
        self.sync()

    def record_error(self, row, error):
        # Never erase the phase/ID, even when a guard/log/proof operation fails.
        self.db.execute('UPDATE uploads SET last_error=?,updated_at=? WHERE channel_uc=? AND project_id=?',
                        (str(error), now(), row['channel_uc'], row['project_id']))
        self.sync()

    def confirm(self, row, proof, project):
        record = self.get(row)
        if not VIDEO_ID_RE.fullmatch(record['video_id']):
            raise Blocked('PUBLIC proof has no durable video ID to bind to')
        expected = {'project_id': row['project_id'], 'channel_uc': row['channel_uc'],
                    'video_id': record['video_id'], 'publication_title': row['publication_title'],
                    'visibility': 'PUBLIC', 'thumbnail_sha256': record['cover_sha256'],
                    'video_sha256': record['video_sha256'], 'manifest_sha256': record['manifest_sha256'],
                    'attempt_id': record['attempt_id']}
        if any(proof.get(key) != value for key, value in expected.items()):
            raise Blocked('PUBLIC proof current attempt/UC/ID/title/visibility/asset binding mismatch')
        attempt = self.db.execute('SELECT * FROM attempts WHERE attempt_id=?', (record['attempt_id'],)).fetchone()
        if attempt is None:
            raise Blocked('PUBLIC proof current attempt is absent')
        attempt = dict(attempt)
        require_finished_attempt(record, attempt)
        assets = verify_assets(project.parent, row)
        if any(assets[key] != record[key] for key in ('video_sha256', 'cover_sha256')):
            raise Blocked('PUBLIC proof frozen assets changed before confirmation')
        if proof.get('verified') is not True or proof.get('thumbnail_verified') is not True:
            raise Blocked('PUBLIC proof lacks explicit live and thumbnail verification')
        observed = datetime.fromisoformat(str(proof.get('verified_at', '')).replace('Z', '+00:00'))
        if observed.tzinfo is None or observed > datetime.now(timezone.utc):
            raise Blocked('PUBLIC proof timestamp is invalid')
        if observed < datetime.fromisoformat(attempt['started_at']):
            raise Blocked('PUBLIC proof predates this upload attempt')
        evidence = file_in_project(proof.get('evidence_path', ''), project)
        if evidence.stat().st_size <= 0:
            raise Blocked('PUBLIC evidence file is empty')
        stored = dict(proof, evidence_sha256=sha256(evidence), accepted_at=now())
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            current = self.get(row)
            current_attempt = self.db.execute('SELECT * FROM attempts WHERE attempt_id=?', (current['attempt_id'],)).fetchone()
            if current_attempt is None or any(current[key] != record[key] for key in (
                    'attempt_id', 'phase', 'video_id', 'manifest_sha256', 'video_sha256', 'cover_sha256', 'title_sha256')):
                raise Blocked('PUBLIC proof attempt/assets changed during confirmation')
            require_finished_attempt(current, dict(current_attempt))
            self.db.execute('''UPDATE uploads SET status='confirmed_public',confirmed_proof=?,last_error='',
                               updated_at=? WHERE channel_uc=? AND project_id=?''',
                            (json.dumps(stored, ensure_ascii=False), now(), row['channel_uc'], row['project_id']))
        self.sync()

    def export_csv(self, path, rows):
        temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
        columns = ['channel_uc', 'project_id', 'profile_id', 'channel_name', 'publication_title',
                   'status', 'phase', 'video_id', 'youtube_url', 'last_error', 'updated_at']
        with temp.open('x', encoding='utf-8-sig', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            for row in rows:
                record = self.get(row)
                record.update(channel_name=row.get('channel_name', ''), publication_title=row['publication_title'],
                              youtube_url=f'https://youtu.be/{record["video_id"]}' if record['video_id'] else '')
                writer.writerow({key: record.get(key, '') for key in columns})
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists():
            shutil.copy2(path, path.with_name(path.name + '.bak.' + uuid.uuid4().hex))
        os.replace(temp, path)


def profile_index_with_rate_limit_retry(collector, pause=time.sleep):
    # check() may have just resolved busy profile names through user/list.
    # Retry only AdsPower's explicit rate limit, never network/auth failures.
    for attempt in range(3):
        try:
            return collector.profile_index(ADS_API)
        except RuntimeError as exc:
            if 'too many request per second' not in str(exc).lower() or attempt == 2:
                raise
            pause(2 * (attempt + 1))


def profile_lease_context():
    return {'schema_version': 1, 'coordinator_command': list(LEASE_COORDINATOR),
            'holder': 'minsk', 'ttl_seconds': 3600,
            'renew_interval_seconds': 60, 'command_timeout_seconds': 30}


def profile_gate(collector, profile_id):
    collector.check(SimpleNamespace(hosts=ADS_HOSTS, api=ADS_API))
    hosts = collector.hosts_from(ADS_HOSTS)
    if set(hosts) != {'minsk', 'mac'}:
        raise Blocked('Both Minsk and Mac are required')
    try:
        busy, dead = collector.busy_everywhere(hosts)  # default is fail-closed
    except SystemExit as exc:
        raise Blocked(str(exc)) from exc
    if dead:
        raise Blocked('A profile host is unreachable')
    uid = collector.uid_of(str(profile_id), profile_index_with_rate_limit_retry(collector))
    if not uid:
        raise Blocked('Profile serial cannot be resolved to a user ID')
    if busy.get(uid):
        raise Blocked(f'Profile {profile_id} is open on {busy[uid]}')
    processes = collector.local_upload_procs()
    if str(profile_id) in processes or uid in processes:
        raise Blocked(f'Another local upload owns profile {profile_id}')
    return uid


def wait_profile_closed(collector, uid, timeout=120):
    deadline = time.monotonic() + timeout
    while True:
        try:
            busy, dead = collector.busy_everywhere(collector.hosts_from(ADS_HOSTS))
        except SystemExit as exc:
            raise Blocked(str(exc)) from exc
        if dead:
            raise Blocked('Cannot verify profile closure on both hosts')
        if not busy.get(uid):
            return
        if time.monotonic() >= deadline:
            raise Blocked('Profile remained active after worker exit')
        time.sleep(3)


def worker_argv(row, assets, source_root, python, resume_id=''):
    argv = [str(python), str(source_root / 'src/upload_worker.py'),
            '--video', str(assets['video']), '--thumbnail', str(assets['cover']),
            '--profile', str(row['profile_id']), '--channel-url',
            f'https://studio.youtube.com/channel/{row["channel_uc"]}/',
            '--title', row['publication_title'], '--description', row['description'], '--visibility', 'public']
    if resume_id:
        if not VIDEO_ID_RE.fullmatch(resume_id):
            raise Blocked('Invalid exact finalize video ID')
        if '--finalize-video-id' not in (source_root / 'src/upload_worker.py').read_text():
            raise Blocked('Worker does not support exact-ID finalize; no profile opened')
        argv += ['--finalize-draft', '--finalize-video-id', resume_id]
    return argv


def run_row(base, row, ledger, manager, collector, source_root, python, resume_id='',
            cancelled_id='', cancellation_proof_sha256=''):
    assets = verify_assets(base, row)
    argv = worker_argv(row, assets, source_root, python, resume_id)
    record = ledger.get(row)
    if record['status'] == 'confirmed_public':
        if cancelled_id:
            raise Blocked('Already confirmed PUBLIC; no cancellation replacement allowed')
        print(f'{row["project_id"]}: confirmed PUBLIC; no action', flush=True)
        return
    if cancelled_id or cancellation_proof_sha256:
        if resume_id:
            raise Blocked('Cancellation replacement and same-ID finalize are separate operations')
        ledger.replacement_permission(row, assets, cancelled_id, cancellation_proof_sha256)
    elif not resume_id and (record['video_id'] or ledger.cancellation(row)
                           or manager.upload_contract.phase_requires_no_reupload(record['phase'])):
        raise Blocked('NO REUPLOAD: durable upload phase; use exact-ID finalize or readback')
    log_dir = base / row['project_id'] / 'attempts'
    log_dir.mkdir(exist_ok=True)
    log_path = log_dir / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex + '.log')
    with exclusive_lock(base / '.locks' / f'profile_{row["profile_id"]}.lock'):
        with manager.upload_coordination.profile_lease(
            str(row['profile_id']), profile_lease_context()
        ) as lease:
            if lease is None:
                raise Blocked('Shared profile lease is required')
            uid = profile_gate(collector, row['profile_id'])
            lease.assert_held()
            invoked = False
            try:
                with log_path.open('x', encoding='utf-8') as handle:
                    attempt = ledger.begin(row, assets, log_path, resume_id, cancelled_id, cancellation_proof_sha256)
                    def log(line):
                        handle.write(str(line).rstrip('\r\n') + '\n')
                        handle.flush()
                        print(str(line), flush=True)
                    def phase(event):
                        handle.flush()
                        os.fsync(handle.fileno())
                        ledger.phase(attempt, event)  # fsync before manager ACK
                    env = dict(os.environ, ADSPOWER_API=ADS_API, VIDEO_UPLOAD_STRICT='1',
                               VIDEO_UPLOAD_THUMBNAIL_PROOF_PATH=str(log_path.with_suffix('.thumbnail.json')))
                    for key in list(env):
                        if key.startswith('VIDEO_OPS_RECEIPT_'):
                            env.pop(key)
                    invoked = True
                    result = manager._invoke_worker(argv, env, log, lease=lease, phase_callback=phase)
                    handle.flush()
                    os.fsync(handle.fileno())
                    ledger.finish(attempt, result)
            finally:
                if invoked:
                    try:
                        wait_profile_closed(collector, uid)
                    except Exception as exc:
                        lease.retain_until_expiry(str(exc))
                        raise
        if lease.release_error:
            raise Blocked('Shared profile lease release unconfirmed; TTL retained: ' + lease.release_error)
        if not result['success']:
            raise Blocked('Worker failed: ' + (result.get('error') or 'unsuccessful worker result'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', type=Path, default=BASE)
    parser.add_argument('--source-root', type=Path, default=SOURCE_ROOT)
    parser.add_argument('--only', action='append', default=[], metavar='PROJECT_ID')
    parser.add_argument('--start', action='store_true')
    parser.add_argument('--confirm-only', action='store_true')
    parser.add_argument('--resume-draft-known-id', default='', metavar='VIDEO_ID')
    parser.add_argument('--import-canary-phase', type=Path)
    parser.add_argument('--import-canary-result', type=Path)
    parser.add_argument('--import-cancellation-proof', type=Path)
    parser.add_argument('--cancellation-proof-sha256', default='')
    parser.add_argument('--retry-cancelled-video-id', default='', metavar='OLD_VIDEO_ID')
    args = parser.parse_args()
    if args.start and args.confirm_only:
        parser.error('--start and --confirm-only are separate operations')
    if args.resume_draft_known_id and (not args.start or len(args.only) != 1):
        parser.error('--resume-draft-known-id requires --start and exactly one --only')
    if args.import_canary_phase and len(args.only) != 1:
        parser.error('Canary import requires exactly one --only project')
    if args.import_canary_result and not args.import_canary_phase:
        parser.error('--import-canary-result requires --import-canary-phase')
    cancellation_action = bool(args.import_cancellation_proof or args.retry_cancelled_video_id)
    if cancellation_action:
        if len(args.only) != 1 or not HASH_RE.fullmatch(args.cancellation_proof_sha256):
            parser.error('Cancellation operation requires exactly one --only and --cancellation-proof-sha256')
        if args.resume_draft_known_id or args.confirm_only or args.import_canary_phase:
            parser.error('Cancellation operations cannot be combined with finalize, confirm or canary import')
        if args.import_cancellation_proof and (args.start or args.retry_cancelled_video_id):
            parser.error('Terminal cancellation import is separate from replacement upload')
        if args.retry_cancelled_video_id and not args.start:
            parser.error('--retry-cancelled-video-id requires explicit --start')
    elif args.cancellation_proof_sha256:
        parser.error('--cancellation-proof-sha256 requires a cancellation operation')
    rows, digest = load_manifest(args.base / 'candidates.json')
    unknown = set(args.only) - {row['project_id'] for row in rows}
    if unknown:
        raise Blocked(f'Unknown projects: {sorted(unknown)}')
    selected = [row for row in rows if not args.only or row['project_id'] in args.only]
    if not (args.start or args.confirm_only or args.import_canary_phase or args.import_cancellation_proof):
        errors = 0
        for row in selected:
            try:
                verify_assets(args.base, row)
                print(f'READY {row["project_id"]} → {row["channel_uc"]} (offline only)')
            except Exception as exc:
                errors += 1
                print(f'BLOCK {row["project_id"]}: {exc}')
        print(f'Dry-run only; no ledger freeze, profile or upload. Manifest SHA256 {digest}')
        return 1 if errors else 0
    if (args.start or args.import_cancellation_proof) and (socket.gethostname() != MINSK_HOSTNAME or sys.platform != 'linux'
                       or args.base.resolve() != BASE or args.source_root.resolve() != SOURCE_ROOT
                       or Path(sys.executable).absolute() != PYTHON):
        raise Blocked('Real upload requires the fixed Minsk host, base, source root and upload venv')
    manager = load_module('night_upload_manager', args.source_root / 'src/upload_manager.py')
    collector = load_module('night_upload_collector', args.source_root / 'tools/collect_channel_emails.py')
    failures = 0
    with exclusive_lock(args.base / '.night_upload.lock'):
        ledger = Ledger(args.base / 'upload_ledger.sqlite3', manager.upload_contract)
        try:
            # Metadata must be complete for every row before freezing the batch;
            # unavailable cover/media files may be approved later per row.
            if any(not isinstance(row.get('description'), str) or not row['description'].strip() for row in rows):
                raise Blocked('All 30 descriptions must be ready before freezing candidates.json')
            ledger.freeze(rows, digest)
            if args.import_cancellation_proof:
                row = selected[0]
                assets = verify_assets(args.base, row)
                cancellation_id = ledger.register_cancellation(
                    row, assets, args.import_cancellation_proof, args.cancellation_proof_sha256,
                    args.base / row['project_id'])
                print(f'CANCELLATION RECORDED {row["project_id"]}: {cancellation_id}; no upload performed', flush=True)
            if args.import_canary_phase:
                assets = verify_assets(args.base, selected[0])
                ledger.bind_assets(selected[0], assets)
                ledger.import_canary(selected[0], read_json(args.import_canary_phase),
                                     read_json(args.import_canary_result) if args.import_canary_result else None)
            if args.start or args.confirm_only:
                for row in selected:
                    try:
                        if args.confirm_only:
                            ledger.confirm(row, read_json(args.base / row['project_id'] / 'public_readback.json'),
                                           args.base / row['project_id'])
                        else:
                            run_row(args.base, row, ledger, manager, collector, args.source_root,
                                    PYTHON, args.resume_draft_known_id, args.retry_cancelled_video_id,
                                    args.cancellation_proof_sha256)
                    except Exception as exc:
                        failures += 1
                        ledger.record_error(row, str(exc))
                        print(f'BLOCK {row["project_id"]}: {exc}', flush=True)
                    finally:
                        ledger.export_csv(args.base / 'upload_status.csv', rows)
            ledger.export_csv(args.base / 'upload_status.csv', rows)
        finally:
            ledger.close()
    return 1 if failures else 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (Blocked, OSError, ValueError) as exc:
        print(f'BLOCKED: {exc}', file=sys.stderr)
        raise SystemExit(2)
