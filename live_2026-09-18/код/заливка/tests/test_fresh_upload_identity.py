"""Fresh-ID contract on saved 45 history; browser I/O only is simulated.

The post-handoff entries/times below come from the saved live evidence.  The
pre-handoff snapshot is SYNTHETIC: that snapshot was not captured in C3 and these
checks must never be described as a recovery proof for its existing attempt.
"""
import copy
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
import yt_studio as ys
import upload_worker as worker

EVIDENCE = ROOT / 'research/ww2_upload_stress_20260914/c3_identity45_details.json'
SAVED = json.loads(EVIDENCE.read_text())['proof']
UC = SAVED['state']['app_channel']['channelId']
VIDEO = SAVED['state']['wizard']['videoId']
OLD_VIDEO = SAVED['state']['wizard']['entries'][0]['videoId']
NEW_ENTRY = SAVED['state']['wizard']['entries'][-1]
FRONTEND = NEW_ENTRY['map_key']
FILENAME = NEW_ENTRY['filename']
VIDEO_BYTES = 601233497  # frozen fixture data; no invented DOM size property.
NODE = shutil.which('node') or '/Users/vova/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node'


def actual_after():
    wizard = SAVED['state']['wizard']
    entries = []
    for source in wizard['entries']:
        row = {name: source.get(name) for name in (
            'frontendId', 'videoId', 'draftVideoId', 'local', 'status', 'filename',
            'fileSelectedTimeMs', 'transferStartTimeMs')}
        row['key'] = source['map_key']
        row['media'] = copy.deepcopy(source.get('localMediaInfo'))
        entries.append(row)
    return {'observed_ms': int(SAVED['epoch'] * 1000), 'channel_uc': UC, 'route_ok': True,
            'entity_id': UC, 'entity_type': 'CHANNEL', 'wizard_count': 1,
            'opened_count': 1, 'input_in_wizard': False, 'input_file_count': 0,
            'videoId': wizard['videoId'], 'workflowStep': wizard['workflowStep'],
            'uploadMode': wizard['uploadMode'], 'isVideoUploaded': wizard['isVideoUploaded'],
            'entries': entries}


def synthetic_before():
    """Synthetic SELECT_FILES state with only the two recorded history entries."""
    state = actual_after()
    state.update(observed_ms=NEW_ENTRY['fileSelectedTimeMs'] - 1000, videoId='',
                 workflowStep='SELECT_FILES', isVideoUploaded=False, input_in_wizard=True)
    state['entries'] = state['entries'][:-1]
    return state


def binding():
    return ys.fresh_upload_baseline(synthetic_before(), UC, FILENAME, VIDEO_BYTES)


def execute_js(source, state, channel_uc=UC, with_input=False):
    """Execute the production reader, adapting only its DOM/clock transport."""
    program = r"""
const s=STATE, input={type:s.input_type||'file',accept:s.input_accept||'video/*',
 files:Array.from({length:s.input_file_count||0},()=>({}))};
const opened={opened:s.dialog_opened!==false,hidden:!!s.dialog_hidden,
 getBoundingClientRect:()=>({width:s.dialog_width===undefined?960:s.dialog_width,height:773})};
const entries=Object.fromEntries(s.entries.map(e=>[e.key,{...e,localMediaInfo:e.media}]));
const wizard={tagName:'YTCP-UPLOADS-DIALOG',videoId:s.videoId,workflowStep:s.workflowStep,
 uploadMode:s.uploadMode,isVideoUploaded:s.isVideoUploaded,uploadDetailsByFrontendId:entries,
 contains:x=>x===input&&s.input_in_wizard,offsetParent:{},
 getBoundingClientRect:()=>({width:1556,height:0}),
 querySelectorAll:selector=>selector==='tp-yt-paper-dialog#dialog'?Array(s.opened_count).fill(opened):[]};
const app={channel:{channelId:s.channel_uc},currentEntity:{id:s.entity_id,type:s.entity_type}};
const document={},location={origin:s.origin||'https://studio.youtube.com',
 pathname:s.path||'/channel/'+CHANNEL+'/videos/upload'},Date={now:()=>s.observed_ms};
const deepFind=selector=>selector==='ytcp-app'?app:selector==='ytcp-uploads-dialog'?wizard:null;
const deepAll=()=>Array(s.wizard_count).fill(wizard);
const getComputedStyle=()=>({display:s.dialog_display||'block',visibility:s.dialog_visibility||'visible'});
const evaluate=new Function('deepFind','deepAll','document','location','Date','getComputedStyle',
 'return function(){'+SOURCE+'}');
const result=evaluate(deepFind,deepAll,document,location,Date,getComputedStyle)(CHANNEL,WITH_INPUT?input:null);
process.stdout.write(JSON.stringify(result));
"""
    program = (program.replace('CHANNEL', json.dumps(channel_uc)).replace('WITH_INPUT', json.dumps(with_input))
               .replace('STATE', json.dumps(state)).replace('SOURCE', json.dumps(source)))
    run = subprocess.run([NODE, '-e', program], capture_output=True, text=True, timeout=5, check=True)
    return json.loads(run.stdout)


class DOMDriver:
    def __init__(self, state):
        self.state = state

    def execute_script(self, script, *args):
        if ys._FRESH_UPLOAD_STATE_JS in script:
            return execute_js(ys._FRESH_UPLOAD_STATE_JS, self.state, args[0], bool(args[1]))
        if ys._READ_WIZARD_JS in script:
            return execute_js(ys._READ_WIZARD_JS, self.state)
        raise AssertionError('Unexpected browser I/O')


class FreshIdentityTest(unittest.TestCase):
    def assert_not_bound(self, state, saved=None):
        saved = binding() if saved is None else saved
        try:
            result = ys.fresh_upload_identity(state, saved)
        except ys.FreshUploadIdentityError:
            return
        self.assertIsNone(result, 'Unsafe state produced a durable video identity')

    def test_actual_new_local_entry_wins_over_first_historical_public_id(self):
        state, saved = actual_after(), binding()
        original = copy.deepcopy(saved)
        result = ys.fresh_upload_identity(state, saved)
        self.assertEqual(result, {'videoId': VIDEO, 'frontendId': FRONTEND})
        self.assertNotEqual(result['videoId'], OLD_VIDEO)
        self.assertEqual(saved, original, 'Pure validator must not silently mutate the binding')
        self.assertEqual(saved['video_bytes'], VIDEO_BYTES)
        self.assertEqual(set(saved['preexisting_frontend_keys']), {e['key'] for e in state['entries'][:-1]})
        self.assertIn(OLD_VIDEO, saved['preexisting_video_ids'])

    def test_missing_master_id_never_falls_back_to_any_map_entry(self):
        state = actual_after(); state['videoId'] = ''
        self.assertIsNone(ys.fresh_upload_identity(state, binding()))
        state['videoId'] = OLD_VIDEO
        self.assert_not_bound(state)

    def test_ambiguous_new_entries_or_preexisting_key_are_not_bindable(self):
        state = actual_after()
        duplicate = copy.deepcopy(state['entries'][-1])
        duplicate['key'] += ':second'; duplicate['frontendId'] = duplicate['key']
        state['entries'].append(duplicate)
        self.assert_not_bound(state)
        state = actual_after(); saved = binding()
        saved['preexisting_frontend_keys'].append(FRONTEND)
        self.assert_not_bound(state, saved)

    def test_exact_channel_open_master_and_mode_required(self):
        for key, value in [('channel_uc', 'UC' + 'x' * 22), ('entity_id', 'UC' + 'x' * 22),
                           ('entity_type', 'VIDEO'), ('wizard_count', 0), ('wizard_count', 2),
                           ('wizard_count', True), ('opened_count', 0), ('opened_count', 2),
                           ('uploadMode', 'EDIT')]:
            with self.subTest(key=key, value=value):
                state = actual_after(); state[key] = value
                self.assert_not_bound(state)

    def test_new_record_identity_file_and_timestamps_are_strict(self):
        for key, value in [('local', False), ('local', 1), ('frontendId', 'foreign'),
                           ('videoId', OLD_VIDEO), ('draftVideoId', OLD_VIDEO),
                           ('filename', 'other.mp4'), ('filename', ''),
                           ('fileSelectedTimeMs', None), ('fileSelectedTimeMs', True),
                           ('fileSelectedTimeMs', NEW_ENTRY['fileSelectedTimeMs'] - 2000),
                           ('transferStartTimeMs', NEW_ENTRY['fileSelectedTimeMs'] - 1),
                           ('transferStartTimeMs', int(SAVED['epoch'] * 1000) + 10000)]:
            with self.subTest(key=key, value=value):
                state = actual_after(); state['entries'][-1][key] = value
                self.assert_not_bound(state)

    def test_unknown_or_non_numeric_media_cannot_prove_local_file(self):
        for key, value in [('durationMs', None), ('durationMs', True), ('durationMs', -1),
                           ('width', 0), ('height', '720'), ('width', float('nan')),
                           ('height', float('inf'))]:
            with self.subTest(key=key, value=value):
                state = actual_after(); state['entries'][-1]['media'][key] = value
                self.assert_not_bound(state)

    def test_bad_observation_clock_never_binds(self):
        for value in (None, True, float('nan'), float('inf'), NEW_ENTRY['fileSelectedTimeMs'] - 2000):
            with self.subTest(value=value):
                state = actual_after(); state['observed_ms'] = value
                self.assert_not_bound(state)

    def test_existing_pin_cannot_move_to_new_frontend_or_video(self):
        saved = binding(); saved.update(frontend_id=FRONTEND, video_id=VIDEO)
        self.assertEqual(ys.fresh_upload_identity(actual_after(), saved)['videoId'], VIDEO)
        for mode in ('frontend', 'video'):
            with self.subTest(mode=mode):
                state = actual_after()
                if mode == 'frontend':
                    state['entries'][-1]['key'] += ':other'
                    state['entries'][-1]['frontendId'] = state['entries'][-1]['key']
                else:
                    state['videoId'] = 'abcDefG_123'
                    state['entries'][-1]['videoId'] = state['videoId']
                self.assert_not_bound(state, saved)

    def test_baseline_rejects_nonempty_input_existing_local_upload_and_bad_size(self):
        for key, value in [('input_in_wizard', False), ('input_file_count', 1),
                           ('input_file_count', True), ('videoId', VIDEO), ('wizard_count', 2)]:
            with self.subTest(key=key, value=value):
                state = synthetic_before(); state[key] = value
                with self.assertRaises(ys.FreshUploadIdentityError):
                    ys.fresh_upload_baseline(state, UC, FILENAME, VIDEO_BYTES)
        for local in (True, None, 1, 'false'):
            state = synthetic_before(); state['entries'][0]['local'] = local
            with self.subTest(local=local), self.assertRaises(ys.FreshUploadIdentityError):
                ys.fresh_upload_baseline(state, UC, FILENAME, VIDEO_BYTES)
        for size in (None, True, 0, -1, '601233497'):
            with self.subTest(size=size), self.assertRaises(ys.FreshUploadIdentityError):
                ys.fresh_upload_baseline(synthetic_before(), UC, FILENAME, size)


class ReaderAndPhaseTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        environment = patch.dict(os.environ, {'VIDEO_UPLOAD_THUMBNAIL_PROOF_PATH':
                                             str(Path(self.temp.name) / 'default_cover.json')})
        environment.start()
        self.addCleanup(environment.stop)

    def tearDown(self):
        worker._reset_upload_phase()

    def test_actual_reader_retains_history_and_uses_open_inner_dialog(self):
        raw = ys.read_fresh_upload_state(DOMDriver(actual_after()), UC)
        self.assertEqual(raw['wizard_count'], 1)
        self.assertEqual(raw['opened_count'], 1)  # Outer host height is genuinely zero.
        self.assertEqual(raw['videoId'], VIDEO)
        self.assertEqual(len(raw['entries']), 3)
        self.assertEqual(raw['entries'][0]['videoId'], OLD_VIDEO)
        self.assertEqual(raw['entries'][-1]['fileSelectedTimeMs'], NEW_ENTRY['fileSelectedTimeMs'])
        self.assertNotIn('video_bytes', raw)
        self.assertEqual(ys.fresh_upload_identity(raw, binding()), {'videoId': VIDEO, 'frontendId': FRONTEND})

    def test_actual_js_baseline_rejects_foreign_input_route_and_closed_dialog(self):
        positive = ys.read_fresh_upload_state(DOMDriver(synthetic_before()), UC, object())
        self.assertEqual(ys.fresh_upload_baseline(positive, UC, FILENAME, VIDEO_BYTES)['filename'], FILENAME)
        for key, value in [('origin', 'https://accounts.google.com'), ('path', '/channel/foreign/'),
                           ('input_in_wizard', False), ('input_type', 'text'),
                           ('input_accept', 'image/jpeg'), ('dialog_opened', False),
                           ('dialog_width', 0), ('dialog_display', 'none'),
                           ('dialog_visibility', 'hidden')]:
            with self.subTest(key=key, value=value):
                state = synthetic_before(); state[key] = value
                raw = ys.read_fresh_upload_state(DOMDriver(state), UC, object())
                with self.assertRaises(ys.FreshUploadIdentityError):
                    ys.fresh_upload_baseline(raw, UC, FILENAME, VIDEO_BYTES)

    def test_real_worker_does_not_commit_legacy_fallback_and_pins_new_id(self):
        worker._reset_upload_phase()
        output = io.StringIO()
        with patch.dict(os.environ, {'VIDEO_UPLOAD_PROGRESS_OBSERVATIONS': '0'}), contextlib.redirect_stdout(output):
            worker._mark_upload_started()
            worker._FRESH_UPLOAD_BINDING = binding()
            state = actual_after(); state['videoId'] = ''
            driver = DOMDriver(state)
            self.assertEqual(ys.read_wizard(driver)['videoId'], OLD_VIDEO)  # Reproduce the real old fallback.
            self.assertEqual(worker._read_wizard_recording_identity(driver)['videoId'], '')
            self.assertEqual(worker._current_upload_phase()['phase'], 'upload_started')
            self.assertEqual(worker._current_upload_phase()['video_id'], '')
            driver.state = actual_after()
            self.assertEqual(worker._read_wizard_recording_identity(driver)['videoId'], VIDEO)
            self.assertEqual(worker._FRESH_UPLOAD_BINDING['frontend_id'], FRONTEND)
            self.assertEqual(worker._current_upload_phase()['video_id'], VIDEO)
            driver.state['entries'][-1]['frontendId'] = 'foreign'
            with self.assertRaises(ys.FreshUploadIdentityError):
                worker._read_wizard_recording_identity(driver)
            self.assertEqual(worker._current_upload_phase()['video_id'], VIDEO)
        self.assertNotIn(OLD_VIDEO, output.getvalue())

    def test_direct_legacy_or_auth_adoption_cannot_bypass_fresh_binding(self):
        for candidate in (OLD_VIDEO, VIDEO):
            with self.subTest(candidate=candidate):
                worker._reset_upload_phase()
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    worker._mark_upload_started()
                    worker._FRESH_UPLOAD_BINDING = binding()
                    try:
                        worker._mark_known_video_id(candidate)
                    except RuntimeError:
                        pass
                self.assertEqual(worker._current_upload_phase()['phase'], 'upload_started')
                self.assertEqual(worker._current_upload_phase()['video_id'], '')
                self.assertNotIn(candidate, output.getvalue())

    def test_bound_receipt_exists_before_phase_output_and_is_not_overwritten(self):
        worker._reset_upload_phase()
        with tempfile.TemporaryDirectory() as directory:
            proof = Path(directory) / 'cover.json'
            baseline = Path(directory) / 'cover.fresh_baseline.json'
            bound = Path(directory) / 'cover.fresh_bound.json'
            saved = binding(); saved['video_sha256'] = 'd' * 64  # Fixed state-machine data.
            test = self
            class CheckedOutput(io.StringIO):
                def write(self, value):
                    if value.startswith('__UPLOAD_PHASE__|') and 'video_id_known' in value:
                        test.assertTrue(baseline.exists())
                        record = json.loads(bound.read_text())
                        test.assertEqual(record['binding']['video_id'], VIDEO)
                        test.assertEqual(record['binding']['frontend_id'], FRONTEND)
                        test.assertEqual(record['binding']['video_sha256'], 'd' * 64)
                    return super().write(value)
            with patch.dict(os.environ, {'VIDEO_UPLOAD_THUMBNAIL_PROOF_PATH': str(proof),
                                         'VIDEO_UPLOAD_PROGRESS_OBSERVATIONS': '0'}), \
                    contextlib.redirect_stdout(CheckedOutput()):
                worker._persist_fresh_identity('baseline', saved, synthetic_before())
                baseline_bytes = baseline.read_bytes()
                worker._mark_upload_started()
                worker._FRESH_UPLOAD_BINDING = saved
                worker._read_wizard_recording_identity(DOMDriver(actual_after()))
                bound_bytes = bound.read_bytes()
                worker._read_wizard_recording_identity(DOMDriver(actual_after()))
                self.assertEqual(bound.read_bytes(), bound_bytes)
                self.assertEqual(baseline.read_bytes(), baseline_bytes)

    def test_existing_receipt_blocks_commit_and_preserves_unbound_phase(self):
        worker._reset_upload_phase()
        with tempfile.TemporaryDirectory() as directory:
            proof = Path(directory) / 'cover.json'
            bound = Path(directory) / 'cover.fresh_bound.json'
            bound.write_text('prior immutable receipt')
            with patch.dict(os.environ, {'VIDEO_UPLOAD_THUMBNAIL_PROOF_PATH': str(proof),
                                         'VIDEO_UPLOAD_PROGRESS_OBSERVATIONS': '0'}), \
                    contextlib.redirect_stdout(io.StringIO()):
                worker._mark_upload_started()
                worker._FRESH_UPLOAD_BINDING = binding()
                with self.assertRaises(FileExistsError):
                    worker._read_wizard_recording_identity(DOMDriver(actual_after()))
            self.assertEqual(bound.read_text(), 'prior immutable receipt')
            self.assertEqual(worker._current_upload_phase()['phase'], 'upload_started')
            self.assertEqual(worker._current_upload_phase()['video_id'], '')
            self.assertEqual(worker._FRESH_UPLOAD_BINDING['frontend_id'], '')

    def test_missing_proof_path_blocks_baseline_and_id_commit(self):
        worker._reset_upload_phase()
        with patch.dict(os.environ), contextlib.redirect_stdout(io.StringIO()):
            os.environ.pop('VIDEO_UPLOAD_THUMBNAIL_PROOF_PATH', None)
            with self.assertRaises(RuntimeError):
                worker._persist_fresh_identity('baseline', binding(), synthetic_before())
            self.assertEqual(worker._current_upload_phase()['phase'], 'preflight')
            worker._mark_upload_started()
            worker._FRESH_UPLOAD_BINDING = binding()
            with self.assertRaises(RuntimeError):
                worker._read_wizard_recording_identity(DOMDriver(actual_after()))
        self.assertEqual(worker._current_upload_phase()['phase'], 'upload_started')
        self.assertEqual(worker._current_upload_phase()['video_id'], '')


if __name__ == '__main__':
    unittest.main()
