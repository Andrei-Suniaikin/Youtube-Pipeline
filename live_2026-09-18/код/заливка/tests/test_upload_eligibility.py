"""Real detector JS and strict phase/drain regressions; no live browser access."""

from contextlib import redirect_stdout
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("eligibility_upload_worker", ROOT / "src/upload_worker.py")
worker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(worker)
FIXTURE = ROOT / "research/ww2_night_20260914/evidence/long_video_eligibility_descendants_0bc6c88b0faa4f51800596428d833446.json"
OBSERVED = json.loads(FIXTURE.read_text())["state"]["hosts"][0]
VIDEO_ID = "aBcDeFg_123"
CODE = "PHONE_VERIFICATION_REQUIRED_LONG_VIDEO"

# The element properties and descendant order come from the saved live DOM.
# querySelectorAll deliberately excludes shadow descendants, like the browser.
HARNESS = r"""
const packet=JSON.parse(require('fs').readFileSync(0,'utf8'));
function el(p={}) {
  const e={tagName:p.tag||'DIV',children:[],parentElement:null,shadowRoot:null,
    hidden:p.hidden||false,opened:p.opened,innerText:p.text||'',
    rect:{width:p.width||0,height:p.height||0},
    style:{display:p.display||'block',visibility:p.visibility||'visible'}};
  e.querySelectorAll=()=>e.children.flatMap(c=>[c,...c.querySelectorAll('*')]);
  e.getBoundingClientRect=()=>e.rect;e.getRootNode=()=>({host:null});return e;
}
function append(a,b){a.children.push(b);b.parentElement=a;return b;}
global.getComputedStyle=e=>e.style;
global.document=el({tag:'DOCUMENT'});
const body=append(document,el({tag:'BODY',text:packet.bodyText||''}));
if(packet.observed){
  const host=append(body,el(packet.observed.host));let parent=host;
  if(packet.shadow){host.shadowRoot=el({tag:'SHADOWROOT'});parent=host.shadowRoot;}
  for(const desc of packet.observed.descendants){
    parent=append(packet.outside?body:parent,el(desc));
  }
}
if(packet.google){
  const auth=append(body,el({tag:'YTCP-AUTH-CONFIRMATION-DIALOG'}));
  append(auth,el({tag:'TP-YT-PAPER-DIALOG',opened:true,width:576,height:200}));
}
process.stdout.write(JSON.stringify(new Function(packet.script)()));
"""


def run_js(script, observed=OBSERVED, **options):
    process = subprocess.run(
        [shutil.which("node"), "-e", HARNESS],
        input=json.dumps(dict(script=script, observed=observed, **options)),
        text=True, capture_output=True, check=True, timeout=5,
    )
    return json.loads(process.stdout)


class Driver:
    """Only the browser I/O boundary is replaced; production guards run intact."""
    def __init__(self, *, observed=OBSERVED, google=False, state=None, unreadable=False):
        self.observed, self.google, self.state = observed, google, state
        self.unreadable = unreadable
        self.calls = []
        self.stopped = 0
        self.service = self

    def execute_script(self, script):
        if self.unreadable:
            raise RuntimeError("CDP disconnected")
        if "YTCP-AUTH-CONFIRMATION-DIALOG" in script:
            self.calls.append("gate")
            return run_js(script, self.observed, google=self.google)
        if "saveDraftBadgeLabel" in script and self.state is not None:
            self.calls.append("wizard")
            return dict(self.state)
        raise AssertionError("Unexpected browser read or mutation")

    def stop(self):
        self.stopped += 1

    def quit(self):
        raise AssertionError("Browser must remain open")


@unittest.skipUnless(shutil.which("node"), "Node required to execute actual production JS")
class ActualEligibilityJsTest(unittest.TestCase):
    def detect(self, observed=OBSERVED, **options):
        return run_js("return " + worker._LONG_VIDEO_PHONE_GATE_JS, observed, **options)

    def test_live_zero_height_host_and_visible_open_paper_are_detected(self):
        self.assertEqual(OBSERVED["host"]["height"], 0)
        self.assertEqual(OBSERVED["descendants"][1]["height"], 313)
        self.assertIs(self.detect(), True)

    def test_shadow_root_and_normalized_whitespace_are_detected(self):
        observed = copy.deepcopy(OBSERVED)
        observed["descendants"][1]["text"] = observed["descendants"][1]["text"].replace(" ", " \n\t")
        self.assertIs(self.detect(observed, shadow=True), True)

    def test_hidden_closed_zero_sized_or_wrong_paper_is_rejected(self):
        cases = {"hidden": True, "opened": False, "width": 0, "height": 0,
                 "display": "none", "visibility": "hidden", "tag": "YTCP-DIALOG"}
        for key, value in cases.items():
            with self.subTest(key=key):
                observed = copy.deepcopy(OBSERVED)
                observed["descendants"][1][key] = value
                self.assertIs(self.detect(observed), False)

    def test_hidden_or_wrong_host_is_rejected(self):
        for key, value in {"hidden": True, "display": "none", "visibility": "hidden", "tag": "YTCP-AUTH-CONFIRMATION-DIALOG"}.items():
            with self.subTest(key=key):
                observed = copy.deepcopy(OBSERVED)
                observed["host"][key] = value
                self.assertIs(self.detect(observed), False)

    def test_unrelated_body_host_or_outside_dialog_text_is_not_evidence(self):
        self.assertIs(self.detect(None, bodyText=OBSERVED["host"]["text"]), False)
        self.assertIs(self.detect(outside=True), False)
        observed = copy.deepcopy(OBSERVED)
        observed["descendants"][1]["text"] = "Verify it's you"
        self.assertIs(self.detect(observed), False)
        observed["descendants"] = []
        self.assertIs(self.detect(observed), False)

    def test_changed_requirement_phrase_is_not_misclassified(self):
        observed = copy.deepcopy(OBSERVED)
        observed["descendants"][1]["text"] = observed["descendants"][1]["text"].replace("15 minutes", "5 minutes")
        self.assertIs(self.detect(observed), False)

    def test_google_verify_is_separate_from_phone_eligibility(self):
        self.assertIs(self.detect(None, google=True), False)
        driver = Driver(observed=None, google=True)
        self.assertIs(worker.auth_gate_present(driver, check_upload_eligibility=True), True)
        self.assertEqual(driver.calls, ["gate"])

    def test_optional_legacy_call_does_not_gain_new_exception(self):
        driver = Driver()
        self.assertIs(worker.auth_gate_present(driver), False)
        self.assertEqual(driver.calls, ["gate"])

    def test_typed_exception_is_not_swallowed_by_default_error_policy(self):
        driver = Driver()
        with self.assertRaisesRegex(worker.StrictUploadEligibilityError, CODE):
            worker.auth_gate_present(driver, check_upload_eligibility=True)
        self.assertEqual(driver.calls, ["gate"])


@unittest.skipUnless(shutil.which("node"), "Node required to execute actual production JS")
class EligibilityPhaseAndDrainTest(unittest.TestCase):
    def setUp(self):
        worker._reset_upload_phase()
        self.previous_strict = os.environ.get("VIDEO_UPLOAD_STRICT")
        os.environ["VIDEO_UPLOAD_STRICT"] = "1"

    def tearDown(self):
        worker._reset_upload_phase()
        if self.previous_strict is None:
            os.environ.pop("VIDEO_UPLOAD_STRICT", None)
        else:
            os.environ["VIDEO_UPLOAD_STRICT"] = self.previous_strict

    def test_strict_readiness_stops_before_any_wizard_or_metadata_read(self):
        driver = Driver()
        with self.assertRaisesRegex(worker.StrictUploadEligibilityError, CODE):
            worker._wait_wizard_ready_recording_identity(driver, timeout=90)
        self.assertEqual(driver.calls, ["gate"])
        self.assertEqual(worker._current_upload_phase()["phase"], "preflight")

    def test_non_strict_readiness_preserves_existing_state_contract(self):
        os.environ["VIDEO_UPLOAD_STRICT"] = "0"
        state = {"present": True, "workflowStep": "DETAILS", "errorSectionStatus": "SUCCESS"}
        driver = Driver(state=state)
        self.assertEqual(worker._wait_wizard_ready_recording_identity(driver, timeout=0), state)
        self.assertEqual(driver.calls, ["wizard"])

    def test_before_title_guard_preserves_started_and_known_id(self):
        for video_id in ("", VIDEO_ID):
            with self.subTest(video_id=video_id), redirect_stdout(io.StringIO()):
                worker._reset_upload_phase()
                worker._mark_upload_started()
                if video_id:
                    worker._mark_known_video_id(video_id)
                before = worker._current_upload_phase()
                with self.assertRaises(worker.StrictUploadEligibilityError) as error:
                    worker._require_auth_clear(Driver(), stage="before_title")
                result = worker.upload_contract.failure_result(str(error.exception), worker._current_upload_phase())
                worker.upload_contract.validate_result(result)
                self.assertEqual(worker._current_upload_phase(), before)
                self.assertEqual(result["video_id"], video_id)
                self.assertTrue(result["do_not_reupload"])
                self.assertFalse(result["success"])
                self.assertIn(CODE, result["error"])

    def test_pre_handoff_failure_retains_safe_retry_phase(self):
        with self.assertRaises(worker.StrictUploadEligibilityError) as error:
            worker._require_auth_clear(Driver(), stage="before_file")
        result = worker.upload_contract.failure_result(str(error.exception), worker._current_upload_phase())
        worker.upload_contract.validate_result(result)
        self.assertEqual(result["phase"], "preflight")
        self.assertFalse(result["do_not_reupload"])

    def test_normal_wait_and_failure_drain_both_stop_at_phone_gate(self):
        for check_auth in (True, False):
            with self.subTest(check_auth=check_auth):
                driver = Driver()
                with self.assertRaisesRegex(worker.StrictUploadEligibilityError, CODE):
                    worker._wait_mp4_uploaded(driver, timeout=120, check_auth=check_auth)
                self.assertEqual(driver.calls, ["gate"])

    def test_cleanup_retains_browser_immediately_and_preserves_boundary(self):
        driver = Driver()
        with redirect_stdout(io.StringIO()):
            worker._mark_upload_started()
        before = worker._current_upload_phase()
        self.assertFalse(worker._strict_transfer_may_close(driver, timeout=120))
        self.assertEqual(driver.calls, ["gate"])
        self.assertEqual(driver.stopped, 1)
        self.assertEqual(worker._current_upload_phase(), before)

    def test_failure_drain_can_ignore_google_verify_and_prove_upload_complete(self):
        state = {"present": True, "videoId": VIDEO_ID, "isVideoUploaded": True}
        driver = Driver(observed=None, google=True, state=state)
        with redirect_stdout(io.StringIO()):
            self.assertTrue(worker._strict_transfer_may_close(driver, timeout=0))
        self.assertEqual(driver.calls, ["gate", "wizard"])
        self.assertEqual(driver.stopped, 0)
        self.assertEqual(worker._current_upload_phase()["video_id"], VIDEO_ID)

    def test_unreadable_drain_retains_browser(self):
        driver = Driver(unreadable=True)
        self.assertFalse(worker._strict_transfer_may_close(driver, timeout=120))
        self.assertEqual(driver.stopped, 1)


if __name__ == "__main__":
    unittest.main()
