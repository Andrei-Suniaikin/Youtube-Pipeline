"""Actual private45 observation plus editor ownership and one-Next progression."""
import copy
import json
import os
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
import yt_studio as ys
from test_private_stress import ScopedDOMDriver

EVIDENCE = ROOT / 'research/ww2_upload_speed_20260914/private45_probe_run3/result.json'


def observed_editor():
    evidence = json.loads(EVIDENCE.read_text())
    state = evidence['privateSaveSnapshot']
    owner = evidence['owner_after']
    state.update(appChannelPresent=False, videoModelId=owner['wizardVideo']['videoId'],
                 videoChannelUC=owner['wizardVideo']['channelId'],
                 currentEntityId=owner['currentEntity']['id'], currentEntityType=owner['currentEntity']['type'])
    return state, evidence['video_id'], evidence['channel_uc']


class PrivateEditorIntentTest(unittest.TestCase):
    def test_live_review_editor_is_accepted_with_own_video_and_entity(self):
        state, video, channel = observed_editor()
        self.assertEqual(state['appUC'], '')
        self.assertTrue(ys.private_save_allowed(state, video, channel, state['sourceUrl']))
        self.assertTrue(ys.private_save_allowed(dict(state, appChannelPresent=True, appUC=channel),
                                               video, channel, state['sourceUrl']))

    def test_editor_cannot_borrow_owner_from_other_video_or_channel(self):
        state, video, channel = observed_editor()
        for mutation in ({'videoModelId': 'other___123'}, {'videoChannelUC': 'UC' + 'x' * 22},
                         {'currentEntityId': 'other___123'}, {'currentEntityType': 'CHANNEL'},
                         {'appUC': 'UC' + 'x' * 22}, {'appChannelPresent': True},
                         {'appChannelPresent': None}, {'videoChannelUC': ''},
                         {'privacySelected': 'PUBLIC'}, {'secondContainerStyle': 'selected'},
                         {'nativeHit': False}, {'nativeEnabled': False}, {'isVideoUploaded': False}):
            with self.subTest(mutation=mutation):
                self.assertFalse(ys.private_save_allowed(dict(state, **mutation), video, channel, state['sourceUrl']))

    def test_channel_route_still_requires_app_channel(self):
        state, video, channel = observed_editor()
        url = f'https://studio.youtube.com/channel/{channel}/videos/upload'
        state['sourceUrl'] = url
        self.assertFalse(ys.private_save_allowed(state, video, channel, url))
        self.assertTrue(ys.private_save_allowed(dict(state, appUC=channel), video, channel, url))

    def test_real_snapshot_js_extracts_editor_owner_without_app_channel(self):
        driver = ScopedDOMDriver(positive=True)
        video = 'abcDefG_123'
        script = ("delete app.channel;app.currentEntity={id:arguments[0],type:'VIDEO'};"
                  "w.video={videoId:arguments[0],channelId:arguments[1]};"
                  "location.href='https://studio.youtube.com/video/'+arguments[0]+'/edit';")
        state = driver.execute_script(script + ys._JS_BASE + ys._PRIVATE_SAVE_JS +
                                      'return privateSaveSnapshot().state;', video, driver.uc)
        self.assertIs(state['appChannelPresent'], False)
        self.assertEqual(state['videoChannelUC'], driver.uc)
        self.assertTrue(ys.private_save_allowed(state, video, driver.uc, state['sourceUrl']))
        self.assertEqual(driver.actions, [])

    def test_rejection_log_has_reason_and_no_arbitrary_payload(self):
        state, video, channel = observed_editor()
        state.update(videoChannelUC='', sourceUrl=state['sourceUrl'] + '?token=TOP_SECRET',
                     nativeText='TOP_SECRET', nativeLabel='TOP_SECRET', password='TOP_SECRET')
        class Driver:
            current_url = state['sourceUrl']
            def execute_script(self, *_): return copy.deepcopy(state)
        with self.assertLogs('yt_studio', level='WARNING') as logs:
            with self.assertRaises(ys.PrivateIntentError):
                ys.require_private_save(Driver(), video, channel)
        output = '\n'.join(logs.output)
        self.assertIn('editor_owner_mismatch', output)
        self.assertIn('snapshot=', output)
        self.assertNotIn('TOP_SECRET', output)
        self.assertNotIn('password', output)


class SeleniumOrderedTransport(ScopedDOMDriver):
    """ChromeDriver/W3C object mapping sorts keys, including nested radio keys."""
    def __init__(self, *, mutation=None, argument_mutation=None):
        super().__init__(positive=True, mutation=mutation)
        self.argument_mutation = argument_mutation

    def execute_script(self, script, *args):
        if 'var current=privateSaveSnapshot' in script and self.argument_mutation:
            expected = copy.deepcopy(args[0])
            self.argument_mutation(expected)
            args = (expected,) + args[1:]
        value = super().execute_script(script, *args)
        return json.loads(json.dumps(value, sort_keys=True))


class PrivateSaveTransportTest(unittest.TestCase):
    def test_actual_function_accepts_sorted_selenium_transport_once(self):
        driver = SeleniumOrderedTransport()
        ys.click_private_save(driver, 'abcDefG_123', driver.uc)
        self.assertEqual(driver.actions, ['mousedown', 'mouseup', 'click'])

    def test_sorted_transport_still_blocks_actual_late_ui_drift(self):
        driver = SeleniumOrderedTransport(mutation='state_drift')
        with self.assertRaises(ys.PrivateIntentError):
            ys.click_private_save(driver, 'abcDefG_123', driver.uc)
        self.assertEqual(driver.actions, [])

    def test_exact_recursive_compare_rejects_every_field_shape_or_type_drift(self):
        mutations = [lambda s: s.update(extra=True), lambda s: s.pop('nativeHit'),
                     lambda s: s.update(nativeX=str(s['nativeX'])),
                     lambda s: s['radios'][0].update(enabled=False),
                     lambda s: s['radios'].reverse(),
                     lambda s: s.update(radios={str(i): r for i, r in enumerate(s['radios'])})]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                driver = SeleniumOrderedTransport(argument_mutation=mutation)
                with self.assertRaises(ys.PrivateIntentError):
                    ys.click_private_save(driver, 'abcDefG_123', driver.uc)
                self.assertEqual(driver.actions, [])


class WizardTransport:
    """Only Selenium transport is replaced; production reads/waits/clicks run."""
    def __init__(self, steps, *, stuck=False, disabled=False, delay=0, changed_video=False):
        self.steps, self.index = steps, 0
        self.actions = []
        self.stuck, self.disabled, self.delay = stuck, disabled, delay
        self.changed_video, self.pending = changed_video, None

    def execute_script(self, script, *args):
        if 'visibilityRadiosVisible: radios.length' in script:
            if self.pending is not None and time.monotonic() >= self.pending:
                self.index += 1; self.pending = None
            return {'present': True, 'videoId': 'other___123' if self.changed_video and self.actions else 'abcDefG_123',
                    'workflowStep': self.steps[self.index], 'isCurrentStepInvalid': False,
                    'nextButton': {'visible': True, 'disabled': self.disabled}}
        if 'return \'clicked\';' in script:
            self.actions.append(args[0])
            if not self.stuck and self.index < len(self.steps) - 1:
                self.pending = time.monotonic() + self.delay
            return 'clicked'
        raise AssertionError('Unexpected browser operation')


class FastNextTest(unittest.TestCase):
    def test_ready_steps_advance_without_fixed_five_second_pauses(self):
        driver = WizardTransport(['DETAILS', 'VIDEO_ELEMENTS', 'CHECKS', 'REVIEW'])
        with patch.dict(os.environ, {'VIDEO_UPLOAD_FAST_UI': '1'}):
            started = time.monotonic()
            result, reason = ys.advance_to_review(driver, monetize=False, timeout=2)
        self.assertEqual(reason, 'ok'); self.assertEqual(result['workflowStep'], 'REVIEW')
        self.assertEqual(driver.actions, [ys.SEL['next_button']] * 3)
        self.assertLess(time.monotonic() - started, 1)

    def test_stuck_step_clicks_next_once_and_stops(self):
        driver = WizardTransport(['CHECKS', 'REVIEW'], stuck=True)
        with patch.dict(os.environ, {'VIDEO_UPLOAD_FAST_UI': '1'}):
            result, reason = ys.advance_to_review(driver, timeout=0.1)
        self.assertIsNone(result); self.assertIn('повтор не выполняется', reason)
        self.assertEqual(driver.actions, [ys.SEL['next_button']])

    def test_disabled_next_is_never_clicked(self):
        driver = WizardTransport(['CHECKS', 'REVIEW'], disabled=True)
        with patch.dict(os.environ, {'VIDEO_UPLOAD_FAST_UI': '1'}):
            result, _ = ys.advance_to_review(driver, timeout=0.1)
        self.assertIsNone(result); self.assertEqual(driver.actions, [])

    def test_delayed_transition_does_not_repeat_next(self):
        driver = WizardTransport(['CHECKS', 'REVIEW'], delay=0.4)
        with patch.dict(os.environ, {'VIDEO_UPLOAD_FAST_UI': '1'}):
            result, _ = ys.advance_to_review(driver, timeout=2)
        self.assertEqual(result['workflowStep'], 'REVIEW')
        self.assertEqual(driver.actions, [ys.SEL['next_button']])

    def test_other_video_cannot_continue_after_next(self):
        driver = WizardTransport(['CHECKS', 'REVIEW'], changed_video=True)
        with patch.dict(os.environ, {'VIDEO_UPLOAD_FAST_UI': '1'}):
            result, _ = ys.advance_to_review(driver, timeout=0.1)
        self.assertIsNone(result); self.assertEqual(driver.actions, [ys.SEL['next_button']])

    def test_identity_is_pinned_across_outer_loop_and_before_review_or_metadata(self):
        for next_step in ('VIDEO_ELEMENTS', 'MONETIZATION', 'CONTENT_RATINGS', 'REVIEW'):
            with self.subTest(next_step=next_step):
                class BetweenIterations(WizardTransport):
                    reads = 0
                    def execute_script(self, script, *args):
                        value = super().execute_script(script, *args)
                        if 'visibilityRadiosVisible: radios.length' in script:
                            self.reads += 1
                            if self.reads >= 4: value['videoId'] = 'other___123'
                        return value
                driver = BetweenIterations(['DETAILS', next_step, 'REVIEW'])
                with patch.dict(os.environ, {'VIDEO_UPLOAD_FAST_UI': '1'}), \
                     patch.object(ys, 'set_monetization') as monetize, \
                     patch.object(ys, 'fill_content_ratings') as ratings:
                    result, reason = ys.advance_to_review(driver, timeout=2)
                self.assertIsNone(result); self.assertIn('ID изменился', reason)
                self.assertEqual(driver.actions, [ys.SEL['next_button']])
                monetize.assert_not_called(); ratings.assert_not_called()

    def test_fast_unknown_initial_video_id_stops_before_any_action(self):
        class UnknownIdentity(WizardTransport):
            def execute_script(self, script, *args):
                value = super().execute_script(script, *args)
                if 'visibilityRadiosVisible: radios.length' in script: value['videoId'] = ''
                return value
        driver = UnknownIdentity(['DETAILS', 'REVIEW'])
        with patch.dict(os.environ, {'VIDEO_UPLOAD_FAST_UI': '1'}):
            result, reason = ys.advance_to_review(driver, timeout=2)
        self.assertIsNone(result); self.assertIn('Исходный video ID', reason)
        self.assertEqual(driver.actions, [])

    def test_monetization_and_rating_helpers_keep_their_semantics(self):
        driver = WizardTransport(['MONETIZATION', 'CONTENT_RATINGS', 'REVIEW'])
        with patch.dict(os.environ, {'VIDEO_UPLOAD_FAST_UI': '1'}), \
             patch.object(ys, 'set_monetization', return_value=True) as monetization, \
             patch.object(ys, 'fill_content_ratings', return_value=True) as ratings:
            result, _ = ys.advance_to_review(driver, monetize=False, timeout=2)
        self.assertEqual(result['workflowStep'], 'REVIEW')
        monetization.assert_called_once_with(driver, on=False)
        ratings.assert_called_once_with(driver)
        self.assertEqual(len(driver.actions), 2)


if __name__ == '__main__':
    unittest.main()
