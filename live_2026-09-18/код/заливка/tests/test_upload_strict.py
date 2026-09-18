"""Offline regression checks for the opt-in nightly upload safety gates."""

from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "strict_upload_worker", ROOT / "src" / "upload_worker.py"
)
worker = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(worker)
VIDEO_ID = "aBcDeFg_123"


class ThumbnailPreflightTest(unittest.TestCase):
    def test_nonempty_readable_file_passes(self):
        with tempfile.TemporaryDirectory() as directory:
            thumbnail = Path(directory) / "cover.jpg"
            thumbnail.write_bytes(b"cover")
            worker._strict_thumbnail_preflight(str(thumbnail))

    def test_missing_directory_and_empty_file_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            empty = Path(directory) / "empty.jpg"
            empty.touch()
            cases = [
                ("", "strict_thumbnail_missing"),
                (str(Path(directory) / "missing.jpg"), "strict_thumbnail_unreadable"),
                (directory, "strict_thumbnail_unreadable"),
                (str(empty), "strict_thumbnail_empty"),
            ]
            for path, message in cases:
                with self.subTest(path=path), self.assertRaisesRegex(ValueError, message):
                    worker._strict_thumbnail_preflight(path)

    def test_unreadable_file_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            thumbnail = Path(directory) / "cover.jpg"
            thumbnail.write_bytes(b"cover")
            with mock.patch("builtins.open", side_effect=PermissionError("denied")):
                with self.assertRaisesRegex(ValueError, "strict_thumbnail_unreadable"):
                    worker._strict_thumbnail_preflight(str(thumbnail))

    def test_reconcile_can_omit_thumbnail_but_not_pass_a_missing_file(self):
        worker._strict_thumbnail_preflight("", required=False)
        with self.assertRaisesRegex(ValueError, "strict_thumbnail_unreadable"):
            worker._strict_thumbnail_preflight("/missing-night-cover.jpg", required=False)

    def test_invalid_thumbnail_fails_before_any_adspower_call(self):
        with mock.patch.dict(worker.os.environ, {"VIDEO_UPLOAD_STRICT": "1"}):
            with mock.patch.object(worker.requests, "get") as request:
                with redirect_stdout(io.StringIO()):
                    result = worker.upload_to_youtube(
                        "/missing-night-video.mp4", "Exact title", "Description", "55",
                        thumbnail_path="/missing-night-cover.jpg",
                    )
        request.assert_not_called()
        self.assertFalse(result["success"])
        self.assertEqual(result["phase"], "preflight")
        self.assertFalse(result["do_not_reupload"])
        self.assertIn("strict_thumbnail_unreadable", result["error"])

    def test_legacy_mode_does_not_require_thumbnail(self):
        with mock.patch.dict(worker.os.environ, {"VIDEO_UPLOAD_STRICT": "0"}):
            with redirect_stdout(io.StringIO()):
                result = worker.upload_to_youtube(
                    "/missing-night-video.mp4", "Exact title", "Description", "55"
                )
        self.assertIn("Видео не найдено", result["error"])
        self.assertNotIn("strict_thumbnail", result["error"])

class StrictExistingRowTest(unittest.TestCase):
    def setUp(self):
        worker._reset_upload_phase()
        self.row = {"videoId": VIDEO_ID, "title": "Żołnierz: Exact title"}

    def test_exact_nfc_whitespace_case_matches(self):
        row = dict(self.row, title="  Z\u0307ołnierz:  EXACT\n title ")
        self.assertEqual(worker._strict_existing_blocker(
            row, self.row["title"], "PUBLIC", "PUBLIC"
        ), "")

    def test_prefix_candidate_is_rejected(self):
        row = dict(self.row, title=self.row["title"] + " — another story")
        self.assertIn("strict_existing_title_mismatch", worker._strict_existing_blocker(
            row, self.row["title"], "PUBLIC", "PUBLIC"
        ))

    def test_each_other_visibility_is_rejected_for_public(self):
        for state in ("PRIVATE", "UNLISTED", "SCHEDULED", "DRAFT", "PROCESSING", "UNKNOWN"):
            with self.subTest(state=state):
                self.assertIn("strict_existing_visibility_mismatch", worker._strict_existing_blocker(
                    self.row, self.row["title"], state, "PUBLIC"
                ))

    def test_public_is_not_an_unlisted_or_scheduled_match(self):
        for requested in ("PRIVATE", "UNLISTED", "SCHEDULED"):
            with self.subTest(requested=requested):
                self.assertIn("strict_existing_visibility_mismatch", worker._strict_existing_blocker(
                    self.row, self.row["title"], "PUBLIC", requested
                ))

    def test_invalid_video_id_is_rejected(self):
        row = dict(self.row, videoId="not-an-id")
        self.assertIn("strict_existing_id_invalid", worker._strict_existing_blocker(
            row, self.row["title"], "PUBLIC", "PUBLIC"
        ))

    def test_rejected_candidate_id_survives_structured_failure(self):
        with redirect_stdout(io.StringIO()):
            result = worker._strict_existing_failure("strict_existing_title_mismatch", self.row)
        worker.upload_contract.validate_result(result)
        self.assertFalse(result["success"])
        self.assertEqual(result["video_id"], VIDEO_ID)
        self.assertEqual(result["phase"], "video_id_known")
        self.assertTrue(result["do_not_reupload"])
        self.assertNotEqual(result["visibility"], "ALREADY_EXISTS")

    def test_interrupted_upload_without_id_is_not_retry_safe(self):
        with redirect_stdout(io.StringIO()):
            result = worker._strict_existing_failure("strict_resume_requires_reconcile")
        worker.upload_contract.validate_result(result)
        self.assertFalse(result["success"])
        self.assertEqual(result["phase"], "upload_started")
        self.assertTrue(result["do_not_reupload"])


class KnownDraftFinalizeTest(unittest.TestCase):
    def setUp(self):
        worker._reset_upload_phase()
        self.row = {"videoId": VIDEO_ID, "title": "Exact title",
                    "draftStatus": "DRAFT_STATUS_SAVED"}

    def tearDown(self):
        worker._reset_upload_phase()

    def test_selects_only_exact_known_id_and_title(self):
        other = dict(self.row, videoId="zYxWvUt_987")
        self.assertEqual(worker._strict_finalize_row(
            [other, self.row], VIDEO_ID, "Exact title"
        ), self.row)

    def test_absent_duplicate_or_wrong_title_stops(self):
        for rows, title in [([], "Exact title"), ([self.row, self.row], "Exact title"),
                            ([self.row], "Exact title another story")]:
            with self.subTest(rows=rows, title=title), self.assertRaises(ValueError):
                worker._strict_finalize_row(rows, VIDEO_ID, title)

    def test_non_draft_does_not_enter_finalize(self):
        public = dict(self.row, draftStatus="DRAFT_STATUS_NONE",
                      privacy="VIDEO_PRIVACY_PUBLIC")
        with self.assertRaisesRegex(ValueError, "strict_existing_visibility_mismatch"):
            worker._strict_finalize_row([public], VIDEO_ID, "Exact title")

    def test_finalize_requires_strict_and_preserves_id_before_browser(self):
        with mock.patch.dict(worker.os.environ, {"VIDEO_UPLOAD_STRICT": "0"}):
            with mock.patch.object(worker.requests, "get") as request:
                with redirect_stdout(io.StringIO()):
                    result = worker.upload_to_youtube(
                        "unused.mp4", "Exact title", "Description", "55",
                        finalize_draft=True, finalize_video_id=VIDEO_ID,
                    )
        request.assert_not_called()
        self.assertFalse(result["success"])
        self.assertEqual(result["video_id"], VIDEO_ID)
        self.assertTrue(result["do_not_reupload"])

    def test_finalize_cannot_send_video_even_without_ack_environment(self):
        worker._FINALIZE_ONLY_VIDEO_ID = VIDEO_ID
        file_input = mock.Mock()
        with self.assertRaisesRegex(RuntimeError, "forbids every video file handoff"):
            worker._send_video_file_at_upload_boundary(file_input, "video.mp4")
        file_input.send_keys.assert_not_called()


class EarlyAuthGateTest(unittest.TestCase):
    def setUp(self):
        worker._reset_upload_phase()

    def tearDown(self):
        worker._reset_upload_phase()

    def test_visible_gate_stops_before_phase_ack_and_file_handoff(self):
        driver, file_input, ack = mock.Mock(), mock.Mock(), mock.Mock()
        driver.execute_script.return_value = True
        with redirect_stdout(io.StringIO()), mock.patch.dict(worker.os.environ, {
                "VIDEO_UPLOAD_STRICT": "1", "VIDEO_UPLOAD_PHASE_ACK": "1"}):
            worker._advance_upload_phase("profile_opened")
            with self.assertRaisesRegex(worker.StrictAuthGateError, "AUTH_GATE:"):
                worker._send_video_file_at_upload_boundary(file_input, "video.mp4", ack, driver=driver)
        self.assertEqual(worker._current_upload_phase()["phase"], "profile_opened")
        ack.readline.assert_not_called()
        file_input.send_keys.assert_not_called()

    def test_unreadable_gate_is_strict_failure_and_legacy_false(self):
        driver, file_input = mock.Mock(), mock.Mock()
        driver.execute_script.side_effect = RuntimeError("CDP disconnected")
        self.assertFalse(worker.auth_gate_present(driver))
        with mock.patch.dict(worker.os.environ, {"VIDEO_UPLOAD_STRICT": "1"}):
            with self.assertRaisesRegex(worker.StrictAuthGateError, "AUTH_GATE_CHECK_UNREADABLE"):
                worker._send_video_file_at_upload_boundary(file_input, "video.mp4", driver=driver)
        self.assertEqual(worker._current_upload_phase()["phase"], "preflight")
        file_input.send_keys.assert_not_called()

    def test_guard_precedes_ack_and_file_when_auth_is_clear(self):
        events = []
        expected = worker.upload_contract.advance_phase_event(
            worker.upload_contract.initial_phase_event(), "upload_started")

        class Driver:
            def execute_script(self, _script):
                events.append("auth_check")
                return False

        class Ack:
            def readline(self):
                events.append("ack")
                self_phase = worker._current_upload_phase()["phase"]
                if self_phase != "upload_started":
                    raise AssertionError(self_phase)
                return worker.upload_contract.encode_phase_ack(expected) + "\n"

        class Input:
            def send_keys(self, _path):
                events.append("file")

        with redirect_stdout(io.StringIO()), mock.patch.dict(worker.os.environ, {
                "VIDEO_UPLOAD_STRICT": "1", "VIDEO_UPLOAD_PHASE_ACK": "1"}):
            worker._send_video_file_at_upload_boundary(Input(), "video.mp4", Ack(), driver=Driver())
        self.assertEqual(events, ["auth_check", "ack", "file"])

    def test_content_guard_preserves_known_id(self):
        driver = mock.Mock()
        driver.execute_script.return_value = True
        with redirect_stdout(io.StringIO()):
            worker._mark_known_video_id(VIDEO_ID)
        with self.assertRaisesRegex(worker.StrictAuthGateError, "content_before_wait"):
            worker._require_auth_clear(driver, stage="content_before_wait")
        self.assertEqual(worker._current_upload_phase()["video_id"], VIDEO_ID)

    @unittest.skipUnless(shutil.which("node"), "Node required for actual detector JS")
    def test_actual_detector_js_matches_live_descendant_and_rejects_hidden_hosts(self):
        driver = mock.Mock()
        driver.execute_script.return_value = False
        worker.auth_gate_present(driver)
        script = driver.execute_script.call_args.args[0]
        fixture = json.loads((ROOT / "research/ww2_night_20260914/evidence/early_auth_gate_descendants_20260914.json").read_text())
        paper = fixture[0]["dom"]["hosts"][0]["descendants"][-1]
        harness = r'''
const script=process.argv[1], observed=JSON.parse(process.argv[2]);
function el(tag,props={}) { const e={tagName:tag,children:[],parentElement:null,
  shadowRoot:null,hidden:false,opened:false,rect:{width:0,height:0},
  style:{display:'flex',visibility:'visible'},...props};
  e.querySelectorAll=()=>e.children.flatMap(c=>[c,...c.querySelectorAll('*')]);
  e.getBoundingClientRect=()=>e.rect;e.getRootNode=()=>({host:null});return e; }
function append(a,b){a.children.push(b);b.parentElement=a;return b;}
global.getComputedStyle=e=>e.style;
function run(changes={},outside=false,ancestor=false){
  global.document=el('DOCUMENT');const body=append(document,el('BODY'));
  const host=el('YTCP-AUTH-CONFIRMATION-DIALOG');
  const dialog=el(observed.tag,{opened:observed.opened,rect:observed.rect,...changes});
  if(ancestor){append(body,dialog);append(dialog,host);}
  else{append(body,host);append(outside?body:host,dialog);}
  return new Function(script)();
}
process.stdout.write(JSON.stringify([run(),run({hidden:true}),run({opened:false}),
  run({rect:{width:0,height:0}}),run({},true),run({},false,true)]));
'''
        result = subprocess.run([shutil.which("node"), "-e", harness, script, json.dumps(paper)],
                                text=True, capture_output=True, check=True)
        self.assertEqual(json.loads(result.stdout), [True, False, False, False, False, True])


class Mp4CompletionGateTest(unittest.TestCase):
    def setUp(self):
        worker._reset_upload_phase()

    def tearDown(self):
        worker._reset_upload_phase()

    @staticmethod
    def driver(states, percent=100):
        sequence = iter(states)
        driver = mock.Mock()
        def execute(script):
            if 'YTCP-AUTH-CONFIRMATION-DIALOG' in script:
                return False
            if 'saveDraftBadgeLabel' in script:
                return next(sequence)
            if 'uploadProgress' in script:
                return {'pct': percent}
            raise AssertionError('Unexpected script')
        driver.execute_script.side_effect = execute
        return driver

    def test_ready_details_or_100_percent_is_not_mp4_completion(self):
        state = {'present': True, 'videoId': VIDEO_ID, 'workflowStep': 'DETAILS',
                 'errorSectionStatus': 'SUCCESS', 'isVideoUploaded': False}
        with redirect_stdout(io.StringIO()):
            self.assertIsNone(worker._wait_mp4_uploaded(self.driver([state]), timeout=0))
        self.assertEqual(worker._current_upload_phase()['video_id'], VIDEO_ID)

    def test_true_uploaded_without_exact_id_is_not_completion(self):
        driver = self.driver([{'present': True, 'videoId': '', 'isVideoUploaded': True}])
        self.assertIsNone(worker._wait_mp4_uploaded(driver, timeout=0))

    def test_late_id_is_saved_while_waiting_for_real_completion(self):
        pending = {'present': True, 'videoId': VIDEO_ID, 'isVideoUploaded': False}
        finished = dict(pending, isVideoUploaded=True)
        driver = self.driver([pending, finished], percent=77)
        with redirect_stdout(io.StringIO()), mock.patch.object(worker.time, 'sleep'):
            state = worker._wait_mp4_uploaded(driver, timeout=10)
        self.assertEqual(state, finished)
        self.assertEqual(worker._current_upload_phase()['video_id'], VIDEO_ID)

    def test_completion_rejects_changed_wizard_id(self):
        with redirect_stdout(io.StringIO()):
            worker._mark_known_video_id(VIDEO_ID)
        driver = self.driver([{'present': True, 'videoId': 'zYxWvUt_987', 'isVideoUploaded': True}])
        with self.assertRaisesRegex(RuntimeError, 'video_id_mismatch'):
            worker._wait_mp4_uploaded(driver, timeout=0)

    def test_failure_drain_retains_browser_if_mp4_remains_unfinished(self):
        state = {'present': True, 'videoId': VIDEO_ID, 'isVideoUploaded': False}
        driver = self.driver([state], percent=77)
        with redirect_stdout(io.StringIO()):
            self.assertFalse(worker._strict_transfer_may_close(driver, timeout=0))
        driver.service.stop.assert_called_once_with()
        driver.quit.assert_not_called()
        result = worker.upload_contract.failure_result('UPLOAD_IN_PROGRESS_PROFILE_RETAINED',
                                                     worker._current_upload_phase())
        worker.upload_contract.validate_result(result)
        self.assertTrue(result['do_not_reupload'])
        self.assertEqual(result['video_id'], VIDEO_ID)

    def test_failure_drain_can_prove_completion_before_closing(self):
        driver = self.driver([{'present': True, 'videoId': VIDEO_ID, 'isVideoUploaded': True}])
        with redirect_stdout(io.StringIO()):
            self.assertTrue(worker._strict_transfer_may_close(driver, timeout=0))
        driver.service.stop.assert_not_called()

    def test_existing_completion_proof_does_not_require_a_wizard_after_publish(self):
        driver = mock.Mock()
        self.assertTrue(worker._strict_transfer_may_close(driver, completion_proven=True))
        driver.execute_script.assert_not_called()
        driver.service.stop.assert_not_called()

    def test_unreadable_failure_drain_does_not_quit_browser(self):
        driver = mock.Mock()
        driver.execute_script.side_effect = RuntimeError('CDP disconnected')
        self.assertFalse(worker._strict_transfer_may_close(driver, timeout=0))
        driver.quit.assert_not_called()
        driver.service.stop.assert_called_once_with()


class LateWizardIdentityTest(unittest.TestCase):
    def setUp(self):
        worker._reset_upload_phase()

    def tearDown(self):
        worker._reset_upload_phase()

    def test_late_id_is_emitted_while_loading_before_ready(self):
        checks = []
        loading = {"present": True, "videoId": "", "errorSectionStatus": "LOADING",
                   "workflowStep": "DETAILS", "isVideoUploaded": False}
        states = iter([loading, dict(loading, videoId=VIDEO_ID),
                       dict(loading, videoId=VIDEO_ID, errorSectionStatus="SUCCESS")])

        class Driver:
            def execute_script(self, _script):
                checks.append(worker._current_upload_phase())
                return next(states)

        output = io.StringIO()
        with redirect_stdout(output), mock.patch.object(worker.time, "sleep"):
            worker._mark_upload_started()
            ready = worker._wait_wizard_ready_recording_identity(Driver(), timeout=10)
        self.assertEqual(checks[0]["phase"], "upload_started")
        self.assertEqual(checks[1]["phase"], "upload_started")
        self.assertEqual(checks[2]["video_id"], VIDEO_ID)
        self.assertEqual(ready["videoId"], VIDEO_ID)
        self.assertIn(worker.upload_contract.encode_phase_event(
            worker._current_upload_phase()), output.getvalue().splitlines())

    def test_final_read_captures_id_after_readiness_timed_out(self):
        loading = {"present": True, "videoId": "", "errorSectionStatus": "LOADING"}
        driver = mock.Mock()
        driver.execute_script.side_effect = [loading, dict(loading, videoId=VIDEO_ID)]
        with redirect_stdout(io.StringIO()):
            worker._mark_upload_started()
            self.assertIsNone(worker._wait_wizard_ready_recording_identity(driver, timeout=0))
            state = worker._read_wizard_recording_identity(driver, required=True)
        self.assertEqual(state["videoId"], VIDEO_ID)
        self.assertEqual(worker._current_upload_phase()["video_id"], VIDEO_ID)

    def test_final_read_never_substitutes_known_id_for_missing_or_different_wizard(self):
        with redirect_stdout(io.StringIO()):
            worker._mark_known_video_id(VIDEO_ID)
        for observed, error in [("", "video_id_unknown"), ("zYxWvUt_987", "video_id_mismatch")]:
            driver = mock.Mock()
            driver.execute_script.return_value = {"present": True, "videoId": observed}
            with self.subTest(observed=observed), self.assertRaisesRegex(RuntimeError, error):
                worker._read_wizard_recording_identity(driver, required=True)
            self.assertEqual(worker._current_upload_phase()["video_id"], VIDEO_ID)

    def test_wait_does_not_swallow_identity_change(self):
        with redirect_stdout(io.StringIO()):
            worker._mark_known_video_id(VIDEO_ID)
        driver = mock.Mock()
        driver.execute_script.return_value = {
            "present": True, "videoId": "zYxWvUt_987", "errorSectionStatus": "LOADING"
        }
        with self.assertRaisesRegex(RuntimeError, "video_id_mismatch"):
            worker._wait_wizard_ready_recording_identity(driver, timeout=10)


class LiveThumbnailProofTest(unittest.TestCase):
    def setUp(self):
        self.saved = __import__('json').loads((ROOT / 'research/ww2_night_20260914/thumbnail_evidence/strict_reader_live_saved_draft.json').read_text())
        self.kwargs = {'video_id': 'KMOv7Np2zY0', 'channel_uc': 'UCEfrjn4_NPXW6wRe5H_8reA',
                       'filename': 'cover.jpg'}
        # The live startTransfer implementation assigns these fields only after
        # the upload Promise resolves; a saved NOT_STARTED preview is not enough.
        self.success = dict(self.saved, upload_state='SUCCESS', filename='cover.jpg', has_server_resource=True)

    def test_live_saved_custom_thumbnail_is_not_new_upload_proof(self):
        self.assertFalse(worker._strict_thumbnail_ready(self.saved, **self.kwargs))

    def test_completed_transfer_with_exact_identity_passes(self):
        self.assertTrue(worker._strict_thumbnail_ready(self.success, **self.kwargs))

    def test_wrong_identity_filename_or_error_rejected(self):
        for changed in ({'video_id': VIDEO_ID}, {'wizard_video_id': VIDEO_ID},
                        {'channel_uc': 'UC' + 'b' * 22}, {'filename': 'other.jpg'},
                        {'upload_error': 'Upload failed'}):
            with self.subTest(changed=changed), self.assertRaises(RuntimeError):
                worker._strict_thumbnail_ready(dict(self.success, **changed), **self.kwargs)

    def test_missing_resource_invalid_form_or_unloaded_image_not_confirmed(self):
        for changed in ({'has_server_resource': False}, {'custom_valid': False},
                        {'preview_ready': False}, {'thumbnail_type': 'AUTO_STILL'},
                        {'upload_state': 'ONGOING'}, {'custom_selected': False}):
            with self.subTest(changed=changed):
                self.assertFalse(worker._strict_thumbnail_ready(dict(self.success, **changed), **self.kwargs))

    def test_confirmation_persists_bound_proof_and_rejects_changed_file(self):
        with tempfile.TemporaryDirectory() as directory:
            cover = Path(directory) / 'cover.jpg'
            cover.write_bytes(b'approved image bytes')
            digest = worker._thumbnail_sha256(cover)
            proof_path = Path(directory) / 'proof.json'
            driver = mock.Mock()
            driver.execute_script.return_value = self.success
            with mock.patch.dict(worker.os.environ, {'VIDEO_UPLOAD_THUMBNAIL_PROOF_PATH': str(proof_path)}):
                proof = worker._strict_thumbnail_confirmation(
                    driver, str(cover), channel_uc=self.kwargs['channel_uc'],
                    video_id=self.kwargs['video_id'], expected_sha256=digest, timeout=0)
            self.assertEqual(proof['thumbnail_sha256'], digest)
            self.assertTrue(proof_path.is_file())
            cover.write_bytes(b'replaced image bytes')
            with self.assertRaisesRegex(RuntimeError, 'local_file_changed'):
                worker._strict_thumbnail_confirmation(
                    driver, str(cover), channel_uc=self.kwargs['channel_uc'],
                    video_id=self.kwargs['video_id'], expected_sha256=digest, timeout=0)


class ThumbnailInputScopeTest(unittest.TestCase):
    class By:
        CSS_SELECTOR = 'css selector'

    def test_strict_upload_uses_wizard_input_not_first_background_input(self):
        background, wizard = mock.Mock(), mock.Mock()
        background.get_attribute.return_value = 'image/jpeg,image/png'
        wizard.get_attribute.return_value = 'image/jpeg,image/png'
        driver = mock.Mock()
        driver.find_elements.side_effect = lambda by, selector: (
            [wizard] if selector == 'ytcp-uploads-dialog input[type="file"]' else [background, wizard]
        )
        self.assertEqual(worker._thumbnail_upload_inputs(driver, self.By, strict=True), [wizard])
        driver.find_elements.assert_called_once_with('css selector', 'ytcp-uploads-dialog input[type="file"]')
        background.send_keys.assert_not_called()

    def test_missing_or_multiple_wizard_image_inputs_stop_before_file_handoff(self):
        first, second = mock.Mock(), mock.Mock()
        first.get_attribute.return_value = 'image/jpeg'
        second.get_attribute.return_value = 'image/png'
        for inputs in ([], [first, second]):
            driver = mock.Mock()
            driver.find_elements.return_value = inputs
            with self.subTest(count=len(inputs)), self.assertRaisesRegex(RuntimeError, 'input_ambiguous'):
                worker._thumbnail_upload_inputs(driver, self.By, strict=True)
        first.send_keys.assert_not_called()
        second.send_keys.assert_not_called()


if __name__ == "__main__":
    unittest.main()
