"""S5 resume guard: real production JS and branch, synthetic DOM/browser I/O.

The fixture is not live UI evidence. It exercises visibility across shadow hosts,
including the zero-height host / visible paper-dialog shape seen in the S5 dump.
"""
import ast
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import upload_worker as worker

UC = "UClpooibglsUsvd3zjuYi4cg"
OTHER = "UCEfrjn4_NPXW6wRe5H_8reA"
NODE = shutil.which("node")
HARNESS = r"""
const p=JSON.parse(require('fs').readFileSync(0,'utf8')),s=p.state;
global.location=new URL(s.url);global.getComputedStyle=e=>e.style;
function descendants(e){return (e.children||[]).flatMap(c=>[c,...descendants(c)]);}
function rendered(e){if(e.hidden||e.style.display==='none'||e.style.visibility==='hidden'||
 e.style.visibility==='collapse'||['SCRIPT','STYLE'].includes(e.tagName))return '';
 return (e.text||'')+(e.children||[]).map(rendered).join('');}
function element(spec,parent=null,host=null){
 const e={tagName:spec.tag||'DIV',text:spec.text||'',hidden:false,
  style:{display:'block',visibility:'visible',opacity:'1',...(spec.style||{})},
  rect:{width:100,height:30,...(spec.rect||{})},parentElement:parent,
  getRootNode:()=>host?{host}:document,...spec};
 e.getBoundingClientRect=()=>e.rect;e.querySelectorAll=()=>descendants(e);
 e.click=()=>{throw Error('Unexpected UI mutation');};
 e.children=(spec.children||[]).map(c=>element(c,e,host));
 if(spec.shadow)e.shadowRoot={querySelectorAll:()=>e.shadowChildren};
 e.shadowChildren=(spec.shadow||[]).map(c=>element(c,null,e));
 Object.defineProperty(e,'innerText',{get:()=>rendered(e)});
 Object.defineProperty(e,'textContent',{get:()=>(e.text||'')+descendants(e).map(c=>c.text||'').join('')});
 return e;
}
global.document={readyState:s.ready||'complete'};
document.body=element({tag:'BODY',...(s.body||{}),children:s.elements||[]});
const app={channel:{channelId:s.app_uc},currentEntity:{id:s.entity_id,type:s.entity_type}};
document.querySelector=selector=>selector==='ytcp-app'?(s.app_present===false?null:app):null;
document.querySelectorAll=()=>[document.body,...descendants(document.body)];
const result=new Function(p.script)();
process.stdout.write(JSON.stringify(result));
"""


def state(**changes):
    result = dict(url=f"https://studio.youtube.com/channel/{UC}/videos/upload", app_uc=UC,
                  entity_id=UC, entity_type="CHANNEL", elements=[])
    result.update(changes)
    return result


def dialog(text="Resume upload", **changes):
    return dict(tag="TP-YT-PAPER-DIALOG", text=text, **changes)


class Browser:
    def __init__(self, data=None, later_states=()):
        self.state = data or state()
        self.later_states = iter(later_states)
        self.url_after_read = None
        self.legacy_reads = 0
        self.read_error = False
        self.reads = 0

    @property
    def current_url(self):
        return self.url_after_read or self.state["url"]

    def execute_script(self, script, *args):
        if self.reads:
            self.state = next(self.later_states, self.state)
        self.reads += 1
        if self.read_error:
            raise TimeoutError("Synthetic renderer read failure")
        if "var CANC=" in script:
            self.legacy_reads += 1
            return "none"  # Browser boundary: old branch remains selected, no actual UI.
        result = subprocess.run([NODE, "-e", HARNESS], input=json.dumps(
            dict(script=script, state=self.state)), text=True,
            capture_output=True, timeout=5, check=True)
        return json.loads(result.stdout)

    def get(self, *args):
        raise AssertionError("Resume guard must not navigate")

    def find_element(self, *args):
        raise AssertionError("Resume guard must not press Escape or click")


class Clock:
    def __init__(self):
        self.now, self.sleeps = 0.0, []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def production_resume_branch():
    """Compile the actual enclosing resume try block, with ordinary I/O fakes."""
    tree = ast.parse((ROOT / "src/upload_worker.py").read_text())
    matches = [node for node in ast.walk(tree) if isinstance(node, ast.Try)
               and any(isinstance(child, ast.Assign)
                       and any(isinstance(t, ast.Name) and t.id == "_fast_resume_guard"
                               for t in child.targets) for child in node.body)]
    assert len(matches) == 1
    wrapper = ast.parse("def branch(driver, uc_id, _strict, finalize_draft):\n    pass\n")
    wrapper.body[0].body = [copy.deepcopy(matches[0]), ast.Return(ast.Constant("continued"))]
    namespace = dict(worker.__dict__)
    exec(compile(ast.fix_missing_locations(wrapper), "<production-resume-branch>", "exec"), namespace)
    return namespace["branch"]


@unittest.skipUnless(NODE, "Node is needed for production JavaScript evaluation")
class VisibleResumeTests(unittest.TestCase):
    def test_hidden_text_does_not_trigger_resume_or_mutation(self):
        cases = [
            [dict(tag="SCRIPT", text="resume upload")],
            [dict(tag="DIV", text="Wrong file — video title")],
            [dialog(style={"display": "none"})],
            [dialog(hidden=True)],
            [dialog(rect={"width": 0, "height": 0})],
            [dialog("Select file", children=[dict(text="resume upload", hidden=True)])],
            [dict(hidden=True, shadow=[dialog()])],
            [dict(style={"display": "none"}, children=[dialog()])],
            [dict(style={"visibility": "hidden"}, shadow=[dialog()])],
            [dict(style={"opacity": "0"}, shadow=[dialog()])],
        ]
        for elements in cases:
            with self.subTest(elements=elements):
                self.assertEqual(worker._visible_resume_disposition(Browser(state(elements=elements)), UC), "none")

    def test_visible_resume_in_open_shadow_dialog_is_busy(self):
        for text in ("Resume upload", "Wrong file", "Возобновить загрузку", "Fortsetzen"):
            elements = [dict(tag="YTCP-UPLOADS-DIALOG", rect={"width": 1556, "height": 0},
                             shadow=[dialog(text, opened=True)])]
            with self.subTest(text=text):
                self.assertEqual(worker._visible_resume_disposition(Browser(state(elements=elements)), UC),
                                 "resume-visible-busy")

    def test_unrelated_visible_dialog_does_not_claim_resume(self):
        self.assertEqual(worker._visible_resume_disposition(
            Browser(state(elements=[dialog("Select files")])) , UC), "none")

    def test_context_must_remain_exact_and_readable(self):
        cases = [{"app_uc": OTHER}, {"entity_id": OTHER}, {"entity_type": "VIDEO"},
                 {"app_present": False}, {"ready": "loading"}, {"body": {"hidden": True}},
                 {"url": f"https://studio.youtube.com/channel/{OTHER}/videos/upload?uc={UC}"},
                 {"url": f"https://accounts.google.com/channel/{UC}/videos/upload"},
                 {"url": f"https://studio.youtube.com/channel/{UC}/"}]
        for change in cases:
            with self.subTest(change=change):
                self.assertEqual(worker._visible_resume_disposition(Browser(state(**change)), UC),
                                 "resume-unreadable")
        browser = Browser()
        browser.url_after_read = f"https://studio.youtube.com/channel/{OTHER}/videos/upload"
        self.assertEqual(worker._visible_resume_disposition(browser, UC), "resume-unreadable")
        browser = Browser()
        browser.read_error = True
        self.assertEqual(worker._visible_resume_disposition(browser, UC), "resume-unreadable")
        self.assertEqual(worker._visible_resume_disposition(Browser(), "invalid"), "resume-unreadable")

    def test_fast_strict_branch_does_not_run_legacy_dismiss(self):
        branch = production_resume_branch()
        for data, expected in ((state(), "continued"),
                               (state(elements=[dialog()]), "BUSY"),
                               (state(ready="loading"), "BUSY")):
            browser = Browser(data)
            with patch.dict(os.environ, {"VIDEO_UPLOAD_FAST_UI": "1"}), patch.object(worker, "time", Clock()):
                result = branch(browser, UC, True, False)
            self.assertEqual(result if isinstance(result, str) else result["visibility"], expected)
            self.assertEqual(browser.legacy_reads, 0)

    def test_delayed_studio_boot_proceeds_without_dismiss_or_reload(self):
        browser = Browser(state(app_present=False), later_states=[state(ready="loading"), state()])
        clock = Clock()
        with patch.object(worker, "time", clock):
            self.assertEqual(worker._wait_visible_resume_disposition(browser, UC), "none")
        self.assertEqual(clock.sleeps, [0.25, 0.25])
        self.assertEqual(browser.reads, 3)
        self.assertEqual(browser.legacy_reads, 0)

    def test_unknown_deadline_is_bounded_and_visible_resume_stops_immediately(self):
        browser, clock = Browser(state(app_present=False)), Clock()
        with patch.object(worker, "time", clock):
            self.assertEqual(worker._wait_visible_resume_disposition(browser, UC, timeout=0.75),
                             "resume-unreadable")
        self.assertEqual(clock.sleeps, [0.25, 0.25, 0.25])
        browser, clock = Browser(state(elements=[dialog()])), Clock()
        with patch.object(worker, "time", clock):
            self.assertEqual(worker._wait_visible_resume_disposition(browser, UC), "resume-visible-busy")
        self.assertEqual(clock.sleeps, [])
        self.assertEqual(browser.reads, 1)

    def test_default_non_strict_and_finalize_keep_previous_routing(self):
        branch = production_resume_branch()
        for enabled, strict, finalize, legacy_reads in (("0", True, False, 1),
                                                       ("1", False, False, 1),
                                                       ("1", True, True, 0)):
            browser = Browser()
            with patch.dict(os.environ, {"VIDEO_UPLOAD_FAST_UI": enabled}):
                self.assertEqual(branch(browser, UC, strict, finalize), "continued")
            self.assertEqual(browser.legacy_reads, legacy_reads)


if __name__ == "__main__":
    unittest.main()
