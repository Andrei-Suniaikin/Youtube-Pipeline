"""Execute the observation JS on DOM fixtures; no upload gates are mocked."""
import contextlib
import copy
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
import upload_worker as worker
import yt_studio as ys

UC, VID = 'UC' + 'a' * 22, 'abcDefG_123'
NODE = shutil.which('node') or '/Users/vova/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node'


def fixture():
    return {'origin': 'https://studio.youtube.com', 'path': '/channel/' + UC + '/videos/upload',
            'uc': UC, 'wizard_id': VID, 'entry_id': VID, 'draft_id': '', 'frontend': 'innertube_studio:test:0',
            'wizard_count': 1, 'entry_count': 1, 'dialog_count': 1, 'opened': True, 'fraction': 0.4}


def execute(state, uc=UC, video_id=VID):
    preamble = r"""
const s=STATE, app={channel:{channelId:s.uc}};
const dialog={opened:s.opened,getBoundingClientRect:()=>({width:900,height:700})};
const entry={videoId:s.entry_id,draftVideoId:s.draft_id,local:s.local!==false,transferProgress:{transferredFraction:s.fraction}};
const wizard={tagName:'YTCP-UPLOADS-DIALOG',videoId:s.wizard_id,
 uploadDetailsByFrontendId:Object.fromEntries(Array.from({length:s.entry_count},(_,i)=>[s.frontend+(i?'other':''),entry])),
 querySelectorAll:()=>Array(s.dialog_count).fill(dialog)};
if(s.history)wizard.uploadDetailsByFrontendId['old-frontend']={videoId:'oldVIDEO123',local:false};
const deepAll=()=>Array(s.wizard_count).fill(wizard),deepFind=()=>app;
const location={origin:s.origin,pathname:s.path},document={},getComputedStyle=()=>({display:'block',visibility:'visible'});
const reader=new Function('deepAll','deepFind','location','document','getComputedStyle','return function(){'+SOURCE+'}');
console.log(JSON.stringify(reader(deepAll,deepFind,location,document,getComputedStyle)(UC,VID)));
"""
    code = preamble.replace('STATE', json.dumps(state)).replace('SOURCE', json.dumps(ys._BOUND_UPLOAD_PROGRESS_JS))
    code = code.replace('(UC,VID)', '(' + json.dumps(uc) + ',' + json.dumps(video_id) + ')')
    result = subprocess.run([NODE, '-e', code], capture_output=True, text=True, check=True, timeout=5)
    return json.loads(result.stdout)


class Driver:
    def __init__(self, state):
        self.state, self.reads = state, 0

    def execute_script(self, script, *args):
        assert ys._BOUND_UPLOAD_PROGRESS_JS in script
        self.reads += 1
        return execute(self.state, *args)


class ProgressTests(unittest.TestCase):
    def test_real_fraction_field_and_timestamp(self):
        actual = execute(fixture())
        self.assertEqual(actual['fraction'], 0.4)
        self.assertEqual(actual['video_id'], VID)
        self.assertTrue(actual['observed_at'].endswith('Z'))

    def test_wrong_identity_or_ambiguous_dom_produces_no_observation(self):
        cases = [('origin', 'https://accounts.google.com'), ('path', '/channel/foreign/videos/upload'),
                 ('uc', 'UC' + 'b' * 22), ('wizard_id', 'otherID1234'), ('entry_id', 'otherID1234'),
                 ('draft_id', 'otherID1234'), ('wizard_count', 2), ('entry_count', 2),
                 ('dialog_count', 0), ('dialog_count', 2), ('opened', False), ('frontend', ''), ('local', False)]
        for key, value in cases:
            with self.subTest(key=key, value=value):
                state = fixture(); state[key] = value
                self.assertIsNone(execute(state))

    def test_old_nonlocal_history_does_not_hide_exact_local_transfer(self):
        state=fixture();state['history']=True
        sample=execute(state)
        self.assertEqual(sample['video_id'],VID)
        self.assertEqual(sample['frontend_id'],state['frontend'])
        self.assertEqual(sample['fraction'],.4)

    def test_invalid_fraction_is_not_zero_progress(self):
        for value in (None, True, '0.5', -0.1, 1.1, float('nan'), float('inf')):
            with self.subTest(value=value):
                state = fixture(); state['fraction'] = value
                self.assertIsNone(execute(state))

    def test_disabled_does_not_read_or_change_phase(self):
        worker._reset_upload_phase()
        driver = Driver(fixture())
        before = copy.deepcopy(worker._current_upload_phase())
        with patch.dict(os.environ, {'VIDEO_UPLOAD_PROGRESS_OBSERVATIONS': '0'}):
            worker._observe_upload_progress(driver, VID)
        self.assertEqual(driver.reads, 0)
        self.assertEqual(worker._current_upload_phase(), before)

    def test_accepted_sample_pins_frontend_and_preserves_phase(self):
        worker._reset_upload_phase()
        worker._UPLOAD_AUTH_CONTEXT = {'channel_uc': UC}
        driver, output = Driver(fixture()), io.StringIO()
        before = copy.deepcopy(worker._current_upload_phase())
        with patch.dict(os.environ, {'VIDEO_UPLOAD_PROGRESS_OBSERVATIONS': '1'}), patch.object(worker.time, 'monotonic', side_effect=[1, 4, 7]), contextlib.redirect_stdout(output):
            worker._observe_upload_progress(driver, VID)
            driver.state['fraction'] = 0.5
            worker._observe_upload_progress(driver, VID)
            driver.state['frontend'] = 'other-frontend'
            worker._observe_upload_progress(driver, VID)
        events = [json.loads(line.split('|', 1)[1]) for line in output.getvalue().splitlines()]
        self.assertEqual([e['fraction'] for e in events], [0.4, 0.5])
        self.assertEqual(worker._current_upload_phase(), before)
        self.assertTrue(all(e['read_started_at'] <= e['read_finished_at'] for e in events))

    def test_invalid_ids_do_not_call_browser(self):
        driver = Driver(fixture())
        self.assertIsNone(ys.read_bound_upload_progress(driver, UC, ''))
        self.assertEqual(driver.reads, 0)

    def test_clock_reversal_and_stale_or_future_samples_are_rejected(self):
        start = datetime(2026, 9, 14, 9, 0, 1, tzinfo=timezone.utc)
        for end, sample in ((start - timedelta(seconds=.5), start - timedelta(seconds=.5)),
                            (start + timedelta(seconds=.5), start - timedelta(seconds=2)),
                            (start + timedelta(seconds=.5), start + timedelta(seconds=2))):
            with self.subTest(end=end, sample=sample):
                values = iter((start, end))
                class Clock(datetime):
                    @classmethod
                    def now(cls, tz=None):
                        return next(values)
                class ClockDriver(Driver):
                    def execute_script(self, script, *args):
                        state = super().execute_script(script, *args)
                        state['observed_at'] = sample.isoformat()
                        return state
                worker._reset_upload_phase()
                worker._UPLOAD_AUTH_CONTEXT = {'channel_uc': UC}
                output = io.StringIO()
                with patch.dict(os.environ, {'VIDEO_UPLOAD_PROGRESS_OBSERVATIONS': '1'}), patch.object(worker, 'datetime', Clock), contextlib.redirect_stdout(output):
                    worker._observe_upload_progress(ClockDriver(fixture()), VID)
                self.assertEqual(output.getvalue(), '')
                self.assertIsNone(worker._UPLOAD_PROGRESS_FRONTEND_ID)


if __name__ == '__main__':
    unittest.main()
