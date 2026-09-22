"""PRIVATE contract/guard tests; browser transport is the only fake boundary."""
import ast
import copy
import shutil
from datetime import datetime
import importlib.util
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
import yt_studio as ys
sys.path.insert(0,str(ROOT/'research/ww2_upload_stress_20260914'))
import private_stress as private
import parallel_upload as parallel
import upload_manager as manager

UC = 'UC' + 'a' * 22
ID = 'abcDefG_123'


def private_wizard():
    state={key:1 for key in ('schema','masterCount','dialogCount','visibilityHostCount','privacyGroupCount',
                            'schedulerCount','doneHostCount','nativeButtonCount')}
    state.update({key:True for key in ('isVideoUploaded','visibilityHostVisible','privacyGroupVisible',
        'schedulersHidden','doneHostVisible','doneHostEnabled','nativeVisible','nativeEnabled','nativeHit')})
    state.update(sourceUrl=f'https://studio.youtube.com/channel/{UC}/videos/upload',appUC=UC,
        videoId=ID,workflowStep='REVIEW',isCurrentStepInvalid=False,errorSectionStatus='SUCCESS',
        foreignDialogs=0,visibleErrors=0,firstContainerStyle='selected',secondContainerStyle='',
        privacySelected='PRIVATE',nativeLabel='Save',nativeText='Save',nativeX=100,nativeY=100,
        radios=[{'name':n,'checked':'true' if n=='PRIVATE' else 'false','visible':True,'enabled':True}
                for n in ('PRIVATE','PUBLIC','UNLISTED')])
    return state


class Driver:
    current_url = f'https://studio.youtube.com/channel/{UC}/videos/upload'
    def __init__(self, state=None, final=None):
        self.state = private_wizard() if state is None else state
        self.final = final or {}
        self.actions = []
    def execute_script(self, script, *args):
        if 'function privateSaveSnapshot' in script:
            if 'var current=privateSaveSnapshot' in script:
                if self.state!=args[0]:return False
                self.actions.append(ys.SEL['done_button']);return True
            return copy.deepcopy(self.state)
        if 'out.publishAnyway' in script:
            return copy.deepcopy(self.final if self.actions else {})
        if 'realClick(el);' in script:
            self.actions.append(args[0])
            return 'clicked'
        raise AssertionError('Unexpected browser operation')


class PrivateGuardTest(unittest.TestCase):
    def test_exact_private_state_only(self):
        base = private_wizard()
        self.assertTrue(ys.private_save_allowed(base, ID, UC, Driver.current_url))
        for key,value in [('masterCount',0),('masterCount',2),('masterCount',True),('videoId','wrongId_123'),
                          ('workflowStep','DETAILS'),('isVideoUploaded',False),('visibleErrors',1),
                          ('privacySelected','PUBLIC'),('privacySelected','DRAFT'),('privacySelected',None),
                          ('radios',[]),('firstContainerStyle',''),('secondContainerStyle','selected'),
                          ('schedulersHidden',False),('nativeEnabled',False),('nativeHit',False),
                          ('foreignDialogs',1),('appUC','UCforeign')]:
            with self.subTest(key=key,value=value):
                self.assertFalse(ys.private_save_allowed(dict(base, **{key:value}),ID,UC,Driver.current_url))
        for url in ('https://accounts.google.com/', 'https://studio.youtube.com/channel/UCforeign/videos',
                    'https://studio.youtube.com/video/otherId_123/edit','http://studio.youtube.com/video/'+ID+'/edit'):
            self.assertFalse(ys.private_save_allowed(base,ID,UC,url))

    def test_unknown_blocks_before_any_save(self):
        driver=Driver({})
        with self.assertRaises(ys.PrivateIntentError):
            ys.finalize(driver,timeout=1,private_video_id=ID,private_channel_uc=UC)
        self.assertEqual(driver.actions,[])

    def test_publish_anyway_cannot_be_clicked_for_private(self):
        driver=Driver(final={'publishAnyway':True})
        with patch.object(ys.time,'sleep',lambda _:None), self.assertRaises(ys.PrivateIntentError):
            ys.finalize(driver,timeout=1,private_video_id=ID,private_channel_uc=UC)
        self.assertEqual(driver.actions,[ys.SEL['done_button']])

    def test_private_unknown_primary_confirmation_is_never_clicked(self):
        driver=Driver(final={'gotIt':True})
        with patch.object(ys.time,'sleep',lambda _:None),self.assertRaises(ys.PrivateIntentError):
            ys.finalize(driver,timeout=1,private_video_id=ID,private_channel_uc=UC)
        self.assertEqual(driver.actions,[ys.SEL['done_button']])

    def test_saved_private_closes_confirmation_without_second_save(self):
        driver=Driver(final={'finalDialog':'Saved fixture'})
        with patch.object(ys.time,'sleep',lambda _:None):
            result=ys.finalize(driver,timeout=1,private_video_id=ID,private_channel_uc=UC)
        self.assertTrue(result[0]);self.assertEqual(driver.actions.count(ys.SEL['done_button']),1)

    def test_repeated_save_rechecks_intent(self):
        driver=Driver(final={'gotIt':True})
        original=driver.execute_script
        def transport(script,*args):
            if 'function privateSaveSnapshot' in script and driver.actions:
                return dict(private_wizard(),privacySelected='PUBLIC')
            return original(script,*args)
        driver.execute_script=transport
        with patch.object(ys.time,'sleep',lambda _:None),self.assertRaises(ys.PrivateIntentError):
            ys.finalize(driver,timeout=1,private_video_id=ID,private_channel_uc=UC)
        self.assertEqual(driver.actions.count(ys.SEL['done_button']),1)

    def test_public_default_keeps_existing_finalization(self):
        driver=Driver(state={},final={'finalDialog':'Published fixture'})
        with patch.object(ys.time,'sleep',lambda _:None):
            self.assertTrue(ys.finalize(driver,timeout=1)[0])
        self.assertIn(ys.SEL['done_button'],driver.actions)

    def test_worker_schedule_private_stops_before_browser(self):
        spec=importlib.util.spec_from_file_location('private_test_worker',ROOT/'src/upload_worker.py')
        worker=importlib.util.module_from_spec(spec);spec.loader.exec_module(worker)
        with patch.dict('os.environ',{'VIDEO_UPLOAD_STRICT':'1'}),patch.object(worker,'VISIBILITY','PRIVATE'):
            result=worker.upload_to_youtube('missing.mp4','title','description',profile_id='40',schedule_date=datetime(2026,9,20))
        self.assertFalse(result['success']);self.assertEqual(result['phase'],'preflight')
        self.assertIn('strict_private_schedule_prohibited',result['error'])

    def test_worker_private_guard_covers_finalization_and_both_fallbacks(self):
        source=(ROOT/'src/upload_worker.py').read_text()
        tree=ast.parse(source)
        function=next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=='upload_to_youtube')
        saves=[n for n in ast.walk(function) if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute)
               and n.func.attr=='click_private_save']
        self.assertEqual(len(saves),2)
        self.assertIn('if not schedule_date and not _strict_private:',source)
        self.assertIn('private_video_id=_current_upload_phase().get("video_id") or ""',source)
        self.assertIn('if _strict_private:\n                    raise\n',source)
        self.assertIn('_do_click = not (_strict_private and _fin_ok)',source)


class ScopedDOMDriver:
    """Execute the production JS against saved DOM attributes/geometry, no UI."""
    def __init__(self,positive=False,mutation=None):
        self.raw=json.loads((ROOT/'research/ww2_upload_stress_20260914/es_visibility_scoped.json').read_text())['state']
        self.current_url=self.raw['origin']+self.raw['path'];self.uc=self.raw['app_uc']
        self.positive=positive;self.mutation=mutation;self.actions=[]
    def execute_script(self,script,*args):
        if 'out.publishAnyway' in script:return {}
        atomic='var current=privateSaveSnapshot' in script
        setup=r'''
const raw=RAW, positive=POSITIVE, mutation=MUTATION, atomic=ATOMIC;
const events=[];
class Element {
 constructor(o={}){this.o=o;this.id=o.id||'';this.attrs={...(o.attrs||{})};this.hidden=o.hidden===true;
  this.disabled=o.disabled;this.opened=o.opened;this.selected=o.selected;this.innerText=o.text||'';this.map={};}
 getAttribute(n){return Object.hasOwn(this.attrs,n)?this.attrs[n]:null}
 hasAttribute(n){return Object.hasOwn(this.attrs,n)}
 getBoundingClientRect(){return this.o.rect||{x:0,y:0,width:0,height:0}}
 querySelectorAll(s){return this.map[s]||[]}
 contains(e){return e===this||Object.values(this.map).flat().some(c=>c.contains(e))}
 scrollIntoView(){}
 dispatchEvent(e){events.push(e.type);return true}
}
const w=new Element(raw.master_geometry),inner=new Element(raw.inner_dialogs[0]);
w.videoId=raw.video_id;w.workflowStep=raw.step;w.isVideoUploaded=raw.props.isVideoUploaded;
w.isCurrentStepInvalid=raw.props.isCurrentStepInvalid;w.errorSectionStatus=raw.props.errorSectionStatus;
const source=raw.scoped_visibility[0],host=new Element(source.host);
for(const name of ['first-container-style','second-container-style']){
 const match=raw.visibility[0].html.match(new RegExp(name+'="([^"]*)"'));host.attrs[name]=match?match[1]:null;
}
const group=new Element(source.elements.find(e=>e.id==='privacy-radios'));
const radios=source.elements.filter(e=>e.tag==='TP-YT-PAPER-RADIO-BUTTON').map(e=>new Element(e));
const scheduler=new Element(source.elements.find(e=>e.tag==='YTCP-VISIBILITY-SCHEDULER'));
const dh=new Element(raw.scoped_done[0].host),button=new Element(raw.scoped_done[0].buttons[0]);
button.attrs['aria-label']='Save';button.innerText='Save';
group.map['tp-yt-paper-radio-button']=radios;
host.map['tp-yt-paper-radio-group#privacy-radios']=[group];host.map['ytcp-visibility-scheduler']=[scheduler];
dh.map['button']=[button];w.map['tp-yt-paper-dialog#dialog']=[inner];
w.map['ytcp-video-visibility-select']=[host];w.map['ytcp-button#done-button']=[dh];
const document=new Element();document.map['ytcp-uploads-dialog']=[w];document.map['tp-yt-paper-dialog']=[inner];
const app={channel:{channelId:raw.app_uc}};document.querySelector=s=>s==='ytcp-app'?app:null;
document.elementFromPoint=()=>button;
const location={href:raw.origin+raw.path},innerWidth=1556,innerHeight=900,window={};
const getComputedStyle=e=>({display:e.o.display||'block',visibility:e.o.visibility||'visible'});
class MouseEvent{constructor(type){this.type=type}}
if(positive){w.videoId='abcDefG_123';group.selected='PRIVATE';
 for(const r of radios)r.attrs['aria-checked']=r.attrs.name==='PRIVATE'?'true':'false';
 for(const e of [dh,button]){e.disabled=false;delete e.attrs.disabled;e.attrs['aria-disabled']='false';}}
if(mutation==='foreign_wizard')document.map['ytcp-uploads-dialog'].push(w);
if(mutation==='duplicate_radios')group.map['tp-yt-paper-radio-button'].push(radios[0]);
if(mutation==='unknown_scheduler')host.map['ytcp-visibility-scheduler'].push(new Element({rect:{x:20,y:20,width:30,height:30}}));
if(mutation==='schedule'){host.attrs['second-container-style']='selected';host.attrs['first-container-style']='';}
if(mutation==='overlay')document.elementFromPoint=()=>new Element();
if(mutation==='foreign_uc')app.channel.channelId='UCforeign';
if(mutation==='state_drift'&&atomic){group.selected='PUBLIC';radios[0].attrs['aria-checked']='false';radios[2].attrs['aria-checked']='true';}
'''
        setup=(setup.replace('RAW',json.dumps(self.raw)).replace('POSITIVE',json.dumps(self.positive))
               .replace('MUTATION',json.dumps(self.mutation)).replace('ATOMIC',json.dumps(atomic)))
        text=setup+'const value=(function(){'+script+'}).apply(null,'+json.dumps(args)+');console.log(JSON.stringify({value,events}));'
        result=subprocess.run(['node','-e',text],capture_output=True,text=True,check=True)
        output=json.loads(result.stdout);self.actions.extend(output['events']);return output['value']


class ScopedPrivateJSTest(unittest.TestCase):
    def test_unchanged_actual_draft_no_id_disabled_save_is_blocked(self):
        driver=ScopedDOMDriver()
        state=driver.execute_script(ys._JS_BASE+ys._PRIVATE_SAVE_JS+'return privateSaveSnapshot().state;')
        self.assertEqual(state['masterCount'],1)  # live outer height=0 is valid geometry
        self.assertEqual(state['privacySelected'],'DRAFT');self.assertFalse(state['nativeEnabled'])
        with self.assertRaises(ys.PrivateIntentError):ys.click_private_save(driver,ID,driver.uc)
        self.assertEqual(driver.actions,[])

    def test_synthetic_private_selection_clicks_only_scoped_native_button(self):
        driver=ScopedDOMDriver(positive=True)
        ys.click_private_save(driver,ID,driver.uc)
        self.assertEqual(driver.actions,['mousedown','mouseup','click'])

    def test_actual_js_foreign_scope_schedule_overlay_and_late_drift_block(self):
        for mutation in ('foreign_wizard','duplicate_radios','unknown_scheduler','schedule','overlay','foreign_uc','state_drift'):
            with self.subTest(mutation=mutation):
                driver=ScopedDOMDriver(positive=True,mutation=mutation)
                with self.assertRaises(ys.PrivateIntentError):ys.click_private_save(driver,ID,driver.uc)
                self.assertEqual(driver.actions,[])


def consume_fixture(night_path,base,row,assets,output):
    night=parallel.load_night(night_path)
    ledger=private.open_ledger(night,Path(base),manager.upload_contract)
    try:
        try:
            attempt=private.begin_once(ledger,night,row,assets,Path(base)/'attempt.log')
            result={'consumed':True,'attempt_id':attempt}
        except private.Blocked:
            result={'consumed':False}
        Path(output).write_text(json.dumps(result))
    finally:ledger.close()


class PrivateHarnessTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name)
        self.night_path=ROOT/'research/ww2_night_20260914/night_upload_routing_20260914.py'
        self.night=parallel.load_night(self.night_path)
        self.manifest={'schema_version':1,'run_id':'fixture_20260914','fixture_approved':True,'waves':[]}
        n=0
        for wave in private.WAVES:
            targets=[]
            for index in range(wave):
                targets.append({'project_id':f'p_{n:010x}','channel_uc':'UC'+f'{n:022d}',
                    'profile_id':str(40+index),'approved':True,
                    'publication_title':f'[PRIVATE TEST fixture_20260914] {n}','description':'Technical fixture.'})
                n+=1
            self.manifest['waves'].append({'concurrency':wave,'targets':targets})
        self.path=self.base/'private_manifest.json';self.write_manifest()
        _,self.rows,self.digest=private.load_manifest(self.path)
        private.init_ledger(self.night,self.base,manager.upload_contract,self.rows,self.digest)
        self.ledger=private.open_ledger(self.night,self.base,manager.upload_contract)
        self.assets={'video':self.base/'fixture.mp4','cover':self.base/'cover.jpg',
                     'video_sha256':'a'*64,'cover_sha256':'b'*64}

    def tearDown(self):self.ledger.close();self.temp.cleanup()
    def write_manifest(self):self.path.write_text(json.dumps(self.manifest))

    def test_manifest_fifteen_unique_uc_distinct_profiles_each_wave(self):
        self.assertEqual(len(self.rows),15)
        self.assertEqual([len(w['targets']) for w in self.manifest['waves']],[1,2,4,8])
        for mutation in ('duplicate_uc','duplicate_profile','public_override','approval','bool_wave','bool_schema'):
            with self.subTest(mutation=mutation):
                value=copy.deepcopy(self.manifest)
                if mutation=='duplicate_uc':value['waves'][1]['targets'][0]['channel_uc']=value['waves'][0]['targets'][0]['channel_uc']
                elif mutation=='duplicate_profile':value['waves'][1]['targets'][1]['profile_id']=value['waves'][1]['targets'][0]['profile_id']
                elif mutation=='public_override':value['waves'][0]['targets'][0]['visibility']='public'
                elif mutation=='approval':value['fixture_approved']=False
                elif mutation=='bool_wave':value['waves'][0]['concurrency']=True
                else:value['schema_version']=True
                self.path.write_text(json.dumps(value))
                with self.assertRaises(private.Blocked):private.load_manifest(self.path)

    def test_auth_wave_is_exact_frozen_membership_with_current_attempt(self):
        import hashlib
        wave=8; attempt='f'*32
        result=private.private_auth_wave(self.manifest,self.digest,wave,attempt)
        expected_hash=hashlib.sha256(json.dumps({'manifest_sha256':self.digest,
            'run_id':self.manifest['run_id'],'visibility':'PRIVATE','wave':wave},
            sort_keys=True,separators=(',',':')).encode()).hexdigest()
        self.assertEqual(result['wave_id'],expected_hash)
        self.assertEqual(result['attempt_id'],attempt)
        self.assertEqual(result['visibility'],'PRIVATE')
        self.assertEqual(len(result['members']),8)
        for member,target in zip(result['members'],self.manifest['waves'][3]['targets']):
            self.assertEqual(member,{'profile':target['profile_id'],'channel_uc':target['channel_uc'],
                                     'project_id':target['project_id']})
        changed=private.private_auth_wave(self.manifest,self.digest,wave,'e'*32)
        self.assertEqual(changed['wave_id'],result['wave_id'])
        self.assertNotEqual(changed['attempt_id'],result['attempt_id'])
        self.assertNotEqual(private.private_auth_wave(self.manifest,'0'*64,wave,attempt)['wave_id'],expected_hash)
        for invalid in ('',True,'f'*31):
            with self.subTest(invalid=invalid),self.assertRaises(private.Blocked):
                private.private_auth_wave(self.manifest,self.digest,wave,invalid)

    def test_private_init_rejects_dangling_ledger_symlink(self):
        base=self.base/'fresh';base.mkdir()
        ledger=base/'private_upload_ledger.sqlite3';target=base/'foreign.sqlite3'
        ledger.symlink_to(target)
        with self.assertRaises(private.Blocked):
            private.init_ledger(self.night,base,manager.upload_contract,self.rows,self.digest)
        self.assertFalse(target.exists())

    def test_one_use_is_atomic_between_two_real_processes(self):
        ctx=multiprocessing.get_context('spawn');processes=[]
        for index in range(2):
            p=ctx.Process(target=consume_fixture,args=(str(self.night_path),str(self.base),self.rows[0],
                self.assets,str(self.base/f'consume{index}.json')))
            p.start();processes.append(p)
        for p in processes:p.join(10);self.assertEqual(p.exitcode,0)
        results=[json.loads((self.base/f'consume{i}.json').read_text()) for i in range(2)]
        self.assertEqual(sum(r['consumed'] for r in results),1)
        self.assertEqual(self.ledger.db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0],1)

    def test_preflight_permit_cannot_be_reused_even_without_id(self):
        row=self.rows[0]
        attempt=private.begin_once(self.ledger,self.night,row,self.assets,self.base/'attempt.log')
        self.assertEqual(self.ledger.get(row)['phase'],'preflight')
        with self.assertRaises(private.Blocked):
            private.begin_once(self.ledger,self.night,row,self.assets,self.base/'another.log')
        self.assertEqual(self.ledger.get(row)['attempt_id'],attempt)
        with self.assertRaises(private.Blocked):
            private.init_ledger(self.night,self.base,manager.upload_contract,self.rows,self.digest)

    def test_phase_event_and_row_are_visible_before_ack(self):
        row=self.rows[0]
        attempt=private.begin_once(self.ledger,self.night,row,self.assets,self.base/'attempt.log')
        event=manager.upload_contract.advance_phase_event(manager.upload_contract.initial_phase_event(),'upload_started')
        private.persist_phase(self.ledger,self.night,attempt,event)
        with sqlite3.connect(self.base/'private_upload_ledger.sqlite3') as db:
            self.assertEqual(db.execute('SELECT phase FROM uploads WHERE attempt_id=?',(attempt,)).fetchone()[0],'upload_started')
            self.assertEqual(db.execute('SELECT phase FROM phase_events WHERE attempt_id=?',(attempt,)).fetchone()[0],'upload_started')

    def test_later_wave_is_blocked_by_worker_success_or_pending_without_readback(self):
        row=self.rows[0]
        for status in ('ready','running','needs_readback','needs_private_readback','private_reconcile_required'):
            self.ledger.db.execute('UPDATE uploads SET status=? WHERE project_id=?',(status,row['project_id']));self.ledger.sync()
            with self.assertRaises(private.Blocked):private.prior_waves_ready(self.ledger,self.rows,2)
        self.ledger.db.execute("UPDATE uploads SET status='verified_private' WHERE project_id=?",(row['project_id'],));self.ledger.sync()
        private.prior_waves_ready(self.ledger,self.rows,2)
        with self.assertRaises(private.Blocked):private.prior_waves_ready(self.ledger,self.rows,4)

    def result(self):
        row=self.rows[0]
        evidence={'verified_channel_uc':row['channel_uc'],'row_video_id':ID,'studio_state':'PRIVATE',
             'visibility_user_set_visibility':private.PRIVATE_INTENT,'scheduled_time_seconds':0,'title_matches':True,
             'normalized_title_sha256':self.night.title_hash(row['publication_title']),
             'observed_normalized_title_sha256':self.night.title_hash(row['publication_title'])}
        return {'phase':'video_id_known','do_not_reupload':True,'video_id':ID,'visibility':'PRIVATE','studio_row_evidence':evidence}

    def test_result_public_unknown_foreign_and_schedule_never_private(self):
        row=self.rows[0];result=self.result();self.assertTrue(private.private_result_valid(result,row,self.night))
        for key,value in [('visibility','PUBLIC'),('visibility','PUBLIC_PENDING'),('visibility','DRAFT'),
                          ('video_id','foreign_123'),('do_not_reupload',False)]:
            self.assertFalse(private.private_result_valid(dict(result,**{key:value}),row,self.night))
        for key,value in [('verified_channel_uc','UC'+'z'*22),('visibility_user_set_visibility',''),
                          ('visibility_user_set_visibility','VIDEO_USER_SET_VISIBILITY_SCHEDULED'),
                          ('scheduled_time_seconds',True),('scheduled_time_seconds',123),('title_matches',False)]:
            mutated=copy.deepcopy(result);mutated['studio_row_evidence'][key]=value
            self.assertFalse(private.private_result_valid(mutated,row,self.night))

    def test_saved_editor_private_is_distinct_from_processing_public_or_unsaved(self):
        row=self.rows[0]
        state={'present':True,'source_url':f'https://studio.youtube.com/video/{ID}/edit',
             'video':{'videoId':ID,'channelId':row['channel_uc'],'title':row['publication_title'],
                      'privacy':private.PRIVATE_PRIVACY,'userSetVisibility':private.PRIVATE_INTENT,
                      'draftStatus':'DRAFT_STATUS_NONE','effectiveStatus':'VIDEO_VISIBILITY_STATUS_PRIVATE'},
             'thumbnail_type':'CUSTOM_STILL','original_thumbnail_type':'CUSTOM_STILL',
             'custom_state':'NOT_STARTED','original_custom_state':'NOT_STARTED','uploader_state':'preview',
             'preview_src':f'https://i9.ytimg.com/vi_webp/{ID}/mqdefault.webp',
             'original_preview_src':f'https://i9.ytimg.com/vi_webp/{ID}/mqdefault.webp'}
        state.update({k:True for k in ('type_valid','custom_valid','preview_ready','custom_selected','uploader_selected','no_transfer')})
        self.assertTrue(private.saved_private_valid(state,row,ID,self.night,ys))
        for key,value in [('userSetVisibility',None),('privacy','VIDEO_PRIVACY_PUBLIC'),
                          ('effectiveStatus',ys.VIS_PROCESSING),('channelId','UC'+'z'*22),('draftStatus','DRAFT_STATUS_DRAFT'),('scheduledTime',False),('scheduledTime',None),
                          ('scheduledTime',''),('scheduledTime',123)]:
            bad=copy.deepcopy(state);bad['video'][key]=value
            with self.subTest(key=key),self.assertRaises(private.Blocked):private.saved_private_valid(bad,row,ID,self.night,ys)
        for key,value in [('custom_selected',False),('preview_src',f'https://i9.ytimg.com/vi/otherId_123/a.jpg'),
                          ('original_custom_state','SUCCESS'),('wizard_open',True)]:
            with self.subTest(key=key),self.assertRaises(private.Blocked):
                private.saved_private_valid(dict(state,**{key:value}),row,ID,self.night,ys)

    def test_both_controller_markers_block_private_and_public_admission(self):
        private_base=self.base/'private';private_base.mkdir()
        parallel.require_no_unresolved_controller(self.base,private_base)
        for marker in (self.base/'.parallel_upload.active.json',private_base/'.private_wave.active.json'):
            with self.subTest(marker=marker.name):
                marker.write_text('{}')
                with self.assertRaises(private.Blocked):
                    parallel.require_no_unresolved_controller(self.base,private_base)
                marker.rename(marker.with_suffix('.completed'))
        import ast
        for path in (Path(private.__file__),Path(parallel.__file__)):
            tree=ast.parse(path.read_text())
            calls=[node for node in ast.walk(tree) if isinstance(node,ast.Call) and
                   ((isinstance(node.func,ast.Attribute) and node.func.attr=='require_no_unresolved_controller') or
                    (isinstance(node.func,ast.Name) and node.func.id=='require_no_unresolved_controller'))]
            self.assertEqual(len(calls),1)

    def test_source_pins_include_manager_and_durable_contract(self):
        source=self.base/'srcroot';(source/'src').mkdir(parents=True);(source/'tools').mkdir()
        nightbase=self.base/'night';nightbase.mkdir()
        shutil.copy2(self.night_path,nightbase/'night_upload.py')
        paths={'worker':source/'src/upload_worker.py','studio':source/'src/yt_studio.py',
               'reader':nightbase/'verify_public.py','manager':source/'src/upload_manager.py',
               'contract':source/'src/upload_contract.py','auth':source/'src/upload_auth.py',
               'limiter':source/'src/adspower_rate_limit.py',
               'collector':source/'tools/collect_channel_emails.py'}
        for path in paths.values():path.write_text('# reviewed source')
        expected={key:private.sha(path) for key,path in paths.items()}
        expected['parallel']=private.sha(private.base_parallel_path())
        expected['auth_browser']=private.sha(private.base_parallel_path().parent/'auth_browser.py')
        expected['auth_secrets_helper']=private.sha(private.base_parallel_path().parent/'google_auth_secrets.py')
        for key in ('manager','contract','auth','limiter','collector'):
            paths[key].write_text('# changed source')
            with self.subTest(key=key),self.assertRaisesRegex(private.Blocked,'SHA changed'):
                private.sources({'source_sha256':expected},source,nightbase)
            paths[key].write_text('# reviewed source')
        del expected['manager']
        with self.assertRaisesRegex(private.Blocked,'SHA changed'):
            private.sources({'source_sha256':expected},source,nightbase)

    def test_actual_saved_js_schema_and_content_string_zero(self):
        reader=ROOT/'research/ww2_night_20260914/verify_public_routing_20260914.py'
        tree=ast.parse(reader.read_text())
        javascript=next(ast.literal_eval(n.value) for n in tree.body if isinstance(n,ast.Assign)
            and any(isinstance(t,ast.Name) and t.id=='READ_SAVED_THUMBNAIL_JS' for t in n.targets))
        row=self.rows[0]
        # Browser boundary fixture uses the actual JS, including its omitted
        # scheduledTime field; values are PRIVATE contract data, not a live claim.
        video={'videoId':ID,'channelId':row['channel_uc'],'title':row['publication_title'],
               'privacy':private.PRIVATE_PRIVACY,'draftStatus':'DRAFT_STATUS_NONE',
               'visibility':{'effectiveStatus':'VIDEO_VISIBILITY_STATUS_PRIVATE','userSetVisibility':private.PRIVATE_INTENT}}
        setup="""
        const video=VIDEO_VALUE, src='https://i9.ytimg.com/vi_webp/VIDEO_ID/mqdefault.webp';
        const uploader={selected:true,state:'preview',uploadService:{transfer:null},
            querySelectorAll:()=>[{src,complete:true,naturalWidth:1280,naturalHeight:720}]};
        const custom={selected:true,querySelector:()=>uploader};
        const editor={video,querySelector:()=>custom,thumbnailsEditorForm:{
            thumbnailType:{value:'CUSTOM_STILL',originalValue:'CUSTOM_STILL',isValid:true},
            customStillFormField:{value:{state:'NOT_STARTED',previewDataUri:src},
                originalValue:{state:'NOT_STARTED',previewDataUri:src},isValid:true}}};
        const document={querySelector:(selector)=>selector==='ytcp-uploads-dialog'?null:editor};
        const location={href:'https://studio.youtube.com/video/VIDEO_ID/edit'};
        """.replace('VIDEO_VALUE',json.dumps(video)).replace('VIDEO_ID',ID)
        result=subprocess.run(['node','-e',setup+'console.log(JSON.stringify((function(){'+javascript+'})()))'],
                              capture_output=True,text=True,check=True)
        state=json.loads(result.stdout)
        self.assertNotIn('scheduledTime',state['video'])
        self.assertTrue(private.saved_private_valid(state,row,ID,self.night,ys))
        value=state['video']
        with self.assertRaises(private.Blocked):private.exact_private_row([value],row,ID,self.night,ys)
        for zero in ('0',0):
            self.assertEqual(private.exact_private_row([dict(value,scheduledTime=zero)],row,ID,self.night,ys)['videoId'],ID)
        for invalid in (False,True,None,'',0.0,1,'1'):
            with self.subTest(invalid=invalid),self.assertRaises(private.Blocked):
                private.exact_private_row([dict(value,scheduledTime=invalid)],row,ID,self.night,ys)

    def test_readback_auth_calls_bind_actual_keyword_only_worker_signature(self):
        import inspect
        spec=importlib.util.spec_from_file_location('private_auth_signature_worker',ROOT/'src/upload_worker.py')
        worker=importlib.util.module_from_spec(spec);spec.loader.exec_module(worker)
        signature=inspect.signature(worker._require_auth_clear)
        function=next(n for n in ast.walk(ast.parse(Path(private.__file__).read_text()))
                      if isinstance(n,ast.FunctionDef) and n.name=='verify_target')
        calls=[n for n in ast.walk(function) if isinstance(n,ast.Call) and
               isinstance(n.func,ast.Attribute) and n.func.attr=='_require_auth_clear']
        self.assertEqual(len(calls),2)
        stages=[]
        for call in calls:
            args=[object() if isinstance(arg,ast.Name) else ast.literal_eval(arg) for arg in call.args]
            kwargs={key.arg:ast.literal_eval(key.value) for key in call.keywords}
            bound=signature.bind(*args,**kwargs);stages.append(bound.arguments['stage'])
        self.assertEqual(sorted(stages),['private_readback','private_readback_editor'])
        with self.assertRaises(TypeError):signature.bind(object(),'private_readback')

    def test_live_disarmed_even_when_offline_inputs_valid(self):
        with patch.dict(os.environ,{'PRIVATE_STRESS_REVIEWED_SHA256':''}):
            with self.assertRaises(private.Blocked):private.live_allowed(self.base,ROOT,self.night)


if __name__=='__main__':unittest.main()
