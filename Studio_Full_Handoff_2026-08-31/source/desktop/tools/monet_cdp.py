#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Включение монетизации видео через CDP-клики (новый лэйаут «Ways to earn», 07.2026).
Флоу: переключить канал → /video/{vid}/monetization → клик триггера → радио On →
Next/Done (+mid-roll чекбокс) → Save → verify перечитыванием.
  python3 monet_cdp.py <profile> <UC> <video_id>
"""
import sys, time, json
sys.path.insert(0, "/Users/owner/VideoStudio/src")
import requests
import upload_worker as uw

PIERCE = r"function* walk(root){const ns=root.querySelectorAll('*');for(const n of ns){yield n;if(n.shadowRoot)yield* walk(n.shadowRoot);}}window.__all=()=>[...walk(document)];"
VIS = "function vis(e){const r=e.getBoundingClientRect();return r.width>0&&r.height>0;}"


def _shot(d, name):
    """Скриншот в scratchpad, если он существует; иначе no-op. Раньше был хардкод-путь
    ЧУЖОЙ сессии — save_screenshot бросал исключение и прерывал монетизацию при реюзе."""
    import os
    for base in ("/tmp", os.path.expanduser("~/ww2_renders/boost_shots")):
        try:
            if os.path.isdir(base):
                d.save_screenshot(os.path.join(base, name))
                return
        except Exception:
            pass


def run(d, uc, vid):
    """Включает монетизацию в УЖЕ ОТКРЫТОМ driver (сессия переиспользуется вызывающим).
    НЕ стартует AdsPower-профиль и НЕ закрывает driver/вкладку — это забота вызывающего.
    Идемпотентна (уже On → True). Возвращает bool (подтверждено перечитыванием)."""
    def cdp_click(x, y):
        for t in ("mousePressed", "mouseReleased"):
            d.execute_cdp_cmd("Input.dispatchMouseEvent", {"type": t, "x": x, "y": y, "button": "left", "clickCount": 1})

    def jsq(js, *args):
        d.execute_script(PIERCE)
        return d.execute_script(js, *args)

    def read_state():
        return jsq("for(const e of window.__all()){if(e.tagName==='YTCP-VIDEO-MONETIZATION')return (e.innerText||'').trim().replace(/\\s+/g,' ').slice(0,40);}return '';")

    ok = False
    ucid, err = uw._navigate_to_channel_studio(d, f"https://studio.youtube.com/channel/{uc}")
    if err:
        print("SWITCH FAIL:", err); return False
    for attempt in (1, 2):
        d.get(f"https://studio.youtube.com/video/{vid}/monetization")
        comp = False
        for _ in range(15):
            time.sleep(3)
            comp = jsq("for(const e of window.__all()){if(e.tagName==='YTCP-VIDEO-MONETIZATION')return true;}return false;")
            if comp:
                break
        if comp:
            break
        print(f"компонент не отрисовался (попытка {attempt}) — refresh")
    if not comp:
        print("NO COMPONENT (не YPP или не грузится)"); return False

    cur = read_state()
    print("state before:", cur)
    if "On" in cur.split():
        print("уже On ✅"); return True

    trg = jsq(r"""
    for(const e of window.__all()){
      if(e.tagName==='YTCP-VIDEO-MONETIZATION'){
        const c=e.querySelector('#container')||(e.shadowRoot&&e.shadowRoot.querySelector('#container'))||e;
        c.scrollIntoView({block:'center'});
        const r=c.getBoundingClientRect(); return {x:r.left+r.width/2, y:r.top+r.height/2};
      }} return null;""")
    if not trg:
        print("NO TRIGGER"); return False
    cdp_click(trg["x"], trg["y"]); time.sleep(1.5)

    on = jsq(VIS + r"""
    for(const e of window.__all()){
      const role=e.getAttribute?e.getAttribute('role'):'';
      if(vis(e)&&role==='radio'&&(e.innerText||'').trim()==='On'){
        const r=e.getBoundingClientRect(); return {x:r.left+r.width/2,y:r.top+r.height/2};}
    } return null;""")
    if not on:
        print("NO RADIO PANEL"); return False
    cdp_click(on["x"], on["y"]); time.sleep(1.5)
    checked = jsq(VIS + r"""
    for(const e of window.__all()){
      const role=e.getAttribute?e.getAttribute('role'):'';
      if(vis(e)&&role==='radio'&&(e.innerText||'').trim()==='On') return e.getAttribute('aria-checked');
    } return null;""")
    print("radio On aria-checked:", checked)
    if checked != "true":
        print("RADIO NOT SET"); return False

    # Next → опросник самосертификации «Tell us what's in your video»
    nxt = jsq(VIS + r"""
    for(const e of window.__all()){
      if(vis(e)&&e.tagName==='YTCP-BUTTON'&&(e.innerText||'').trim()==='Next'){
        const r=e.getBoundingClientRect(); return {x:r.left+r.width/2,y:r.top+r.height/2};}
    } return null;""")
    if not nxt:
        print("NO NEXT BTN"); return False
    cdp_click(nxt["x"], nxt["y"]); time.sleep(2.5)

    # выбор юзера: «None of the above» (найти и кликнуть, может быть ниже — скроллим к нему)
    noa = jsq(VIS + r"""
    for(const e of window.__all()){
      const role=e.getAttribute?e.getAttribute('role'):'';
      const t=(e.innerText||'').trim();
      if((role==='checkbox'||role==='radio'||e.tagName==='YTCP-CHECKBOX-LITE')&&/none of the above/i.test(t||((e.parentElement&&e.parentElement.innerText)||''))){
        e.scrollIntoView({block:'center'});
        const r=e.getBoundingClientRect(); return {x:r.left+r.width/2,y:r.top+r.height/2,found:'ctl'};
      }
    }
    // fallback: любой элемент с этим текстом (label)
    for(const e of window.__all()){
      const t=(e.innerText||'').trim();
      if(t.toLowerCase()==='none of the above'){
        e.scrollIntoView({block:'center'});
        const r=e.getBoundingClientRect(); return {x:r.left+r.width/2,y:r.top+r.height/2,found:'label'};
      }
    }
    return null;""")
    print("None-of-the-above:", noa)
    if not noa:
        _shot(d, "selfcert_missing.png")
        print("NO NOA CONTROL — скрин сохранён"); return False
    cdp_click(noa["x"], noa["y"]); time.sleep(1.5)

    sub = jsq(VIS + r"""
    for(const e of window.__all()){
      if(vis(e)&&e.tagName==='YTCP-BUTTON'&&e.id==='submit-button'&&!e.hasAttribute('disabled')&&e.getAttribute('aria-disabled')!=='true'){
        const r=e.getBoundingClientRect(); return {x:r.left+r.width/2,y:r.top+r.height/2};}
    } return null;""")
    print("Submit:", bool(sub))
    if not sub:
        _shot(d, "selfcert_submit_disabled.png")
        print("SUBMIT DISABLED — скрин сохранён"); return False
    cdp_click(sub["x"], sub["y"]); time.sleep(3)
    _shot(d, "after_submit.png")

    # после Submit может быть ещё панель (midroll) — включить непроставленные чекбоксы и Done
    for _ in range(3):
        box = jsq(VIS + r"""
        for(const e of window.__all()){
          if(vis(e)&&(e.tagName==='YTCP-CHECKBOX-LITE'||(e.getAttribute&&e.getAttribute('role')==='checkbox'))&&e.getAttribute('aria-checked')!=='true'){
            const r=e.getBoundingClientRect(); return {x:r.left+r.width/2,y:r.top+r.height/2};}
        } return null;""")
        if box:
            cdp_click(box["x"], box["y"]); time.sleep(1)
            print("checkbox (midroll) checked")
        btn = jsq(VIS + r"""
        for(const e of window.__all()){
          if(vis(e)&&e.tagName==='YTCP-BUTTON'&&['Done','Next'].includes((e.innerText||'').trim())){
            const r=e.getBoundingClientRect(); return {x:r.left+r.width/2,y:r.top+r.height/2,t:(e.innerText||'').trim()};}
        } return null;""")
        if not btn:
            break
        print("panel btn:", btn["t"])
        cdp_click(btn["x"], btn["y"]); time.sleep(2)
        if btn["t"] == "Done":
            break

    save = jsq(VIS + r"""
    for(const e of window.__all()){
      if(vis(e)&&e.tagName==='YTCP-BUTTON'&&e.id==='save'&&!e.hasAttribute('disabled')&&e.getAttribute('aria-disabled')!=='true'){
        const r=e.getBoundingClientRect(); return {x:r.left+r.width/2,y:r.top+r.height/2};}
    } return null;""")
    print("Save btn:", bool(save))
    if save:
        cdp_click(save["x"], save["y"]); time.sleep(4)
    # verify
    d.get(f"https://studio.youtube.com/video/{vid}/monetization")
    st = ""
    for _ in range(15):
        time.sleep(3)
        st = read_state()
        if st:
            break
    print("VERIFY:", st)
    ok = "On" in (st or "").split()
    print("RESULT:", "✅ On подтверждено" if ok else "❌ не подтверждено")
    return ok


def main(profile, uc, vid):
    r = requests.get("http://localhost:50325/api/v1/browser/start",
                     params={"serial_number": profile, "open_tabs": 1, "ip_tab": 0}, timeout=120).json()
    if r.get("code") != 0:
        print("PROFILE START FAIL:", r.get("msg")); return False
    addr = r["data"]["ws"]["selenium"]; drv = r["data"].get("webdriver", "")
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    opts = Options(); opts.add_experimental_option("debuggerAddress", addr)
    d = webdriver.Chrome(service=Service(drv), options=opts) if drv else webdriver.Chrome(options=opts)
    d.set_page_load_timeout(75)
    d.switch_to.new_window("tab")
    try:
        return run(d, uc, vid)
    finally:
        try:
            d.close()
        except Exception:
            pass


if __name__ == "__main__":
    p, uc, vid = sys.argv[1], sys.argv[2], sys.argv[3]
    sys.exit(0 if main(p, uc, vid) else 1)
