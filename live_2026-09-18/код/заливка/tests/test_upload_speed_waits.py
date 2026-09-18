"""S1 readiness/reuse regressions. Real reader JS; only browser and clock I/O fake.

The small DOM below is synthetic and is not evidence of a live upload. Selectors
and component fields are the existing production readers' recorded selectors.
"""
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
import yt_studio as ys

UC = "UClpooibglsUsvd3zjuYi4cg"
OTHER = "UCEfrjn4_NPXW6wRe5H_8reA"
NODE = shutil.which("node")
HARNESS = r"""
const p=JSON.parse(require('fs').readFileSync(0,'utf8')),s=p.state;
global.window={};global.location=new URL(s.url);global.getComputedStyle=e=>e.style;
global.performance={timeOrigin:s.time_origin_ms||2000,getEntriesByType:type=>type==='navigation'?
 (s.navigation===null?[]:[s.navigation||{name:s.url,responseStatus:200,responseEnd:20}]):
 (s.resources||[])};
global.MouseEvent=class{constructor(type){this.type=type;}};
const events=[];
function el(tag,props={}){
 const e={tagName:tag,hidden:false,disabled:false,style:{display:'block',visibility:'visible'},
  rect:{width:100,height:30},...props};
 e.getBoundingClientRect=()=>e.rect;e.getAttribute=k=>(e.attrs||{})[k]??null;
 e.scrollIntoView=()=>{};e.querySelectorAll=()=>[];
 e.matches=selector=>selector===tag.toLowerCase();
 e.dispatchEvent=event=>{if(event.type==='click'){
   events.push(tag);if(tag==='BUTTON')s.stage='avatar';
   if(tag==='YTD-COMPACT-LINK-RENDERER')s.stage='accounts';
 }};return e;
}
const app=el('YTCP-APP',{channel:{channelId:s.app_uc},currentEntity:{id:s.entity_id,type:s.entity_type}});
const create=el('YTCP-ICON-BUTTON',s.create||{});
const avatar=el('BUTTON',s.avatar||{});
const sw=el('YTD-COMPACT-LINK-RENDERER',{data:{icon:{iconType:'SWITCH_ACCOUNTS'}},...(s.switch||{})});
const account=el('YTD-ACCOUNT-ITEM-RENDERER',{data:{accountName:{simpleText:'Channel'},hasChannel:true,
 channelHandle:{simpleText:'@channel'},serviceEndpoint:{url:'?authuser=0'}}});
const opened=el('TP-YT-PAPER-DIALOG',{opened:s.dialog_opened!==false});
const wizard=el('YTCP-UPLOADS-DIALOG',{videoId:'',workflowStep:'SELECT_FILES',uploadMode:'NEW_UPLOAD',
 isVideoUploaded:false,uploadDetailsByFrontendId:{},contains:()=>false});
wizard.querySelectorAll=selector=>selector==='tp-yt-paper-dialog#dialog'?[opened]:[];
function all(){return [app,create,avatar,
 ...Array(s.wizard_count||0).fill(wizard),
 ...(s.stage==='avatar'&& !s.no_switch?[sw,el('YTD-MULTI-PAGE-MENU-RENDERER')]:[]),
 ...(s.stage==='accounts'?[account,el('YTD-MULTI-PAGE-MENU-RENDERER')]:[])];}
global.document={readyState:s.ready||'complete',
 querySelector:selector=>selector==='ytcp-app'?(s.app_present===false?null:app):selector==='button#avatar-btn'?avatar:null,
 querySelectorAll:selector=>selector==='*'?all():selector.includes('#upload-icon')?(s.no_create?[]:[create]):[]};
const result=new Function(p.script)(...p.args);
process.stdout.write(JSON.stringify({result:result===undefined?null:result,state:s,events}));
"""


def state(**changes):
    return dict(url=f"https://studio.youtube.com/channel/{UC}/videos/upload", app_uc=UC,
                entity_id=UC, entity_type="CHANNEL", stage="initial", **changes)


class Browser:
    def __init__(self, data=None):
        self.state = data or state()
        self.navigations, self.events = [], []
        self.url_after_read = None

    @property
    def current_url(self):
        return self.url_after_read or self.state["url"]

    def get(self, url):
        self.navigations.append(url)
        self.state["url"] = (self.state.get("root_redirect", url)
                             if url == "https://studio.youtube.com" else url)

    def execute_script(self, script, *args):
        result = subprocess.run([NODE, "-e", HARNESS], input=json.dumps(
            dict(script=script, args=args, state=self.state)), text=True,
            capture_output=True, timeout=5, check=True)
        data = json.loads(result.stdout)
        self.state = data["state"]
        self.events.extend(data["events"])
        return data["result"]


class Clock:
    def __init__(self):
        self.now, self.sleeps = 100.0, []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


@unittest.skipUnless(NODE, "Node is needed for production JavaScript evaluation")
class ExactChannelReadyTests(unittest.TestCase):
    def test_exact_live_uc_and_create_are_required(self):
        self.assertTrue(worker._fast_channel_ready(Browser(), UC))
        variations = [
            {"url": f"https://studio.youtube.com/channel/{OTHER}/?wanted={UC}"},
            {"url": f"https://studio.youtube.com/channel/{UC}suffix/"},
            {"url": f"https://accounts.google.com/channel/{UC}/"},
            {"app_uc": OTHER}, {"entity_id": OTHER}, {"entity_type": "VIDEO"},
            {"ready": "loading"}, {"no_create": True},
            {"create": {"hidden": True}}, {"create": {"disabled": True}},
            {"create": {"attrs": {"aria-disabled": "true"}}},
            {"create": {"rect": {"width": 0, "height": 30}}},
            {"create": {"style": {"display": "none", "visibility": "visible"}}},
        ]
        for change in variations:
            with self.subTest(change=change):
                data = state()
                data.update(change)
                self.assertFalse(worker._fast_channel_ready(Browser(data), UC))

    def test_url_drift_between_snapshot_and_return_fails_closed(self):
        browser = Browser()
        browser.url_after_read = f"https://studio.youtube.com/channel/{OTHER}/"
        self.assertFalse(worker._fast_channel_ready(browser, UC))

    def test_reuse_exact_channel_skips_navigation(self):
        browser = Browser()
        with patch.dict(os.environ, {"VIDEO_UPLOAD_FAST_UI": "1"}):
            self.assertEqual(worker._navigate_to_channel_studio(browser, browser.current_url), (UC, None))
            self.assertTrue(worker._studio_ready(browser, UC))
        self.assertEqual(browser.navigations, [])
        self.assertEqual(browser.events, [])

    def test_explicit_authuser_always_navigates_before_acceptance(self):
        browser = Browser()
        with patch.dict(os.environ, {"VIDEO_UPLOAD_FAST_UI": "1"}):
            self.assertTrue(worker._studio_ready(browser, UC, au="2"))
        self.assertEqual(browser.navigations, [f"https://studio.youtube.com/channel/{UC}?authuser=2"])

    def test_loading_wait_is_bounded_without_actions_or_navigation(self):
        data = state(ready="loading")
        browser, clock = Browser(data), Clock()
        with patch.object(ys, "time", clock):
            self.assertFalse(worker._wait_channel_ready(browser, UC, timeout=0.75))
        self.assertEqual(clock.sleeps, [0.25, 0.25, 0.25])
        self.assertEqual(browser.navigations, [])
        self.assertEqual(browser.events, [])

    def test_fast_mode_is_opt_in(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(worker._fast_ui_enabled())
        with patch.dict(os.environ, {"VIDEO_UPLOAD_FAST_UI": "0"}):
            self.assertFalse(worker._fast_ui_enabled())

    def test_upload_dialog_requires_one_open_master_with_exact_uc(self):
        browser, clock = Browser(state(wizard_count=1)), Clock()
        with patch.object(ys, "time", clock):
            self.assertTrue(worker._wait_upload_dialog(browser, UC))
        self.assertEqual(clock.sleeps, [])
        self.assertEqual(browser.events, [])
        for change in ({"wizard_count": 0}, {"wizard_count": 2},
                       {"wizard_count": 1, "dialog_opened": False}, {"app_uc": OTHER}):
            data = state(wizard_count=1)
            data.update(change)
            browser, clock = Browser(data), Clock()
            with self.subTest(change=change), patch.object(ys, "time", clock):
                self.assertFalse(worker._wait_upload_dialog(browser, UC, timeout=0.5))
            self.assertEqual(browser.events, [])
            self.assertEqual(browser.navigations, [])


@unittest.skipUnless(NODE, "Node is needed for production JavaScript evaluation")
class AccountReadinessTests(unittest.TestCase):
    def test_visible_avatar_without_booted_studio_is_not_ready(self):
        browser, clock = Browser(state(app_present=False)), Clock()
        with patch.object(ys, "time", clock):
            self.assertFalse(worker._wait_account_avatar(browser, timeout=0.5))
        self.assertEqual(browser.events, [])
        self.assertEqual(clock.sleeps, [0.25, 0.25])

    def test_root_already_on_target_does_not_open_menu(self):
        browser, clock = Browser(state(root_redirect=f"https://studio.youtube.com/channel/{UC}/")), Clock()
        with patch.dict(os.environ, {"VIDEO_UPLOAD_FAST_UI": "1"}), patch.object(ys, "time", clock):
            self.assertTrue(worker._switch_to_channel(browser, "Channel", UC))
        self.assertEqual(browser.navigations, ["https://studio.youtube.com"])
        self.assertEqual(browser.events, [])
        self.assertEqual(clock.sleeps, [])

    def test_fast_ready_controls_click_once_without_sleep(self):
        browser, clock = Browser(), Clock()
        with patch.dict(os.environ, {"VIDEO_UPLOAD_FAST_UI": "1"}), patch.object(ys, "time", clock):
            result = ys.open_account_menu(browser, wait=1)
        self.assertEqual(result["accounts"][0]["authuser"], "0")
        self.assertEqual(browser.events, ["BUTTON", "YTD-COMPACT-LINK-RENDERER"])
        self.assertEqual(clock.sleeps, [])

    def test_missing_switch_stops_after_one_avatar_click(self):
        browser, clock = Browser(state(no_switch=True)), Clock()
        with patch.dict(os.environ, {"VIDEO_UPLOAD_FAST_UI": "1"}), patch.object(ys, "time", clock):
            result = ys.open_account_menu(browser, wait=0.75)
        self.assertEqual(result["error"], "switch_not_ready")
        self.assertEqual(browser.events, ["BUTTON"])
        self.assertEqual(sum(clock.sleeps), 0.75)

    def test_hidden_avatar_never_clicked(self):
        browser, clock = Browser(state(avatar={"hidden": True})), Clock()
        with patch.dict(os.environ, {"VIDEO_UPLOAD_FAST_UI": "1"}), patch.object(ys, "time", clock):
            result = ys.open_account_menu(browser, wait=0.5)
        self.assertEqual(result["error"], "avatar_not_ready")
        self.assertEqual(browser.events, [])

    def test_default_keeps_legacy_wait(self):
        browser, clock = Browser(), Clock()
        with patch.dict(os.environ, {}, clear=True), patch.object(ys, "time", clock):
            result = ys.open_account_menu(browser, wait=1)
        self.assertEqual(len(result["accounts"]), 1)
        self.assertEqual(clock.sleeps, [2])


@unittest.skipUnless(NODE, "Node is needed for production JavaScript evaluation")
class MainDocumentRejectionTests(unittest.TestCase):
    def rejection_state(self):
        url = state()["url"]
        return dict(url=url, navigation_name=url, time_origin_ms=2000, ready="complete",
                    navigation_count=1, response_status_supported=True, response_status=403,
                    response_end=120, app_present=False, wizard_count=0)

    def test_new_exact_main_403_is_only_an_early_fallback_signal(self):
        s = self.rejection_state()
        self.assertTrue(worker._direct_navigation_rejected(s, UC, 1000, s["url"]))
        variants = [
            {"time_origin_ms": 1000}, {"time_origin_ms": 500}, {"time_origin_ms": float("nan")},
            {"navigation_count": 0}, {"navigation_count": 2}, {"navigation_count": True},
            {"response_status_supported": False}, {"response_status": None}, {"response_status": 0},
            {"response_status": 200}, {"response_status": 429}, {"response_end": 0},
            {"ready": "loading"}, {"app_present": True}, {"wizard_count": 1},
            {"navigation_name": f"https://studio.youtube.com/channel/{OTHER}/videos/upload"},
            {"navigation_name": "https://studio.youtube.com/youtubei/v1/channel/get"},
            {"url": f"https://accounts.google.com/channel/{UC}/videos/upload"},
        ]
        for change in variants:
            with self.subTest(change=change):
                altered = dict(s, **change)
                self.assertFalse(worker._direct_navigation_rejected(altered, UC, 1000, altered["url"]))
        for previous in (None, 0, True, float("nan")):
            self.assertFalse(worker._direct_navigation_rejected(s, UC, previous, s["url"]))
        self.assertFalse(worker._direct_navigation_rejected(s, UC, 1000, s["url"] + "?changed=1"))

    def test_live_shaped_document_403_skips_sleep_without_clicks(self):
        data = state(app_present=False, navigation={"name": state()["url"], "responseStatus": 403, "responseEnd": 120})
        browser, clock = Browser(data), Clock()
        with patch.object(ys, "time", clock):
            self.assertFalse(worker._wait_direct_channel_ready(browser, UC, 1000))
        self.assertEqual(clock.sleeps, [])
        self.assertEqual(browser.events, [])
        self.assertEqual(browser.navigations, [])

    def test_resource_403_unknown_or_stale_main_keeps_readiness_wait(self):
        variations = [
            {"navigation": {"name": state()["url"], "responseStatus": 200, "responseEnd": 120},
             "resources": [{"name": "https://studio.youtube.com/youtubei/v1/channel/get", "responseStatus": 403}]},
            {"navigation": {"name": state()["url"], "responseEnd": 120}},
            {"navigation": {"name": state()["url"], "responseStatus": 403, "responseEnd": 120},
             "time_origin_ms": 1000},
        ]
        for variation in variations:
            browser, clock = Browser(state(app_present=False, **variation)), Clock()
            with self.subTest(variation=variation), patch.object(ys, "time", clock):
                self.assertFalse(worker._wait_direct_channel_ready(browser, UC, 1000, timeout=0.5))
            self.assertEqual(clock.sleeps, [0.25, 0.25])
            self.assertEqual(browser.events, [])


if __name__ == "__main__":
    unittest.main()
