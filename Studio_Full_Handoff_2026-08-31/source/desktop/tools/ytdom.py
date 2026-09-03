#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ytdom — живая консоль к странице внутри AdsPower-профиля.

Зачем: писать автоматизацию YouTube Studio по РЕАЛЬНОМУ DOM, а не по памяти.
Браузер остаётся открытым между вызовами → цикл «проверил селектор» = 2 секунды,
а не 20-минутная заливка.

Ключевое отличие от одноразовых dump_*.py: обходит shadow DOM насквозь (в Studio
половина элементов живёт внутри shadowRoot и НЕ находится обычным querySelector),
показывает Polymer-состояние компонентов и сохраняет дампы как артефакты.

Команды
-------
  open  --profile 9005 [--url URL]   старт профиля + attach (браузер НЕ закрывается)
  go    <url>                         навигация в текущей сессии
  js    '<выражение>'                 выполнить JS, вернуть JSON
  dump  [--save ИМЯ] [--grep ТЕКСТ] [--all]
                                      все интерактивные элементы + готовые селекторы
  find  <текст>                       найти элемент по тексту/aria/name, показать всё
  props <селектор>                    Polymer-свойства компонента (реальное состояние)
  state                               краткий снимок: где мы в мастере загрузки
  snap  <ИМЯ>                         скриншот + дамп + текст → research/dom/ИМЯ.*
  close [--keep]                      закрыть профиль (--keep = только забыть сессию)

Пример
------
  python3 tools/ytdom.py open --profile 9005 --url "https://studio.youtube.com/channel/UCFAKE000000000000000001/videos/upload"
  python3 tools/ytdom.py dump --grep radio --save step_visibility
  python3 tools/ytdom.py props ytcp-uploads-dialog
  python3 tools/ytdom.py close
"""
import os
import sys
import json
import time
import argparse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
sys.path.insert(0, os.path.expanduser("~/VideoStudio/src"))

import requests  # noqa: E402

ADSPOWER_API = os.getenv("ADSPOWER_API", "http://localhost:50325")
SESSION_FILE = os.path.expanduser("~/.ytdom_session.json")
DUMP_DIR = os.path.expanduser("~/VideoStudio/research/dom")


def _ads_param(profile_id) -> dict:
    """Цифра → serial_number, строка → user_id (как в upload_worker)."""
    pid = str(profile_id).strip()
    return {"serial_number": pid} if pid.isdigit() else {"user_id": pid}


# ---------------------------------------------------------------------------
# JS-хелперы. deepAll — единственный способ увидеть Studio целиком: Polymer
# прячет реальные кнопки в shadowRoot, обычный querySelectorAll их не отдаёт.
# ---------------------------------------------------------------------------
_DEEP = r"""
function deepAll(root){
  var out=[], els=root.querySelectorAll('*');
  for(var i=0;i<els.length;i++){
    out.push(els[i]);
    if(els[i].shadowRoot) out=out.concat(deepAll(els[i].shadowRoot));
  }
  return out;
}
function vis(e){
  try{ var r=e.getBoundingClientRect();
       return r.width>0 && r.height>0 && getComputedStyle(e).visibility!=='hidden'; }
  catch(x){ return false; }
}
function txt(e){
  try{ return (e.innerText||e.textContent||'').trim().replace(/\s+/g,' '); }catch(x){ return ''; }
}
function attr(e,n){ try{ return e.getAttribute(n)||''; }catch(x){ return ''; } }
function hostPath(e){
  // цепочка shadow-хостов: подсказывает, достанет ли элемент обычный querySelector
  var p=[], n=e;
  while(n){
    var root=n.getRootNode&&n.getRootNode();
    if(root && root.host){ p.unshift(root.host.tagName.toLowerCase()); n=root.host; }
    else break;
  }
  return p;
}
function bestSel(e){
  // готовый селектор, который можно вставить в код как есть
  var tag=(e.tagName||'').toLowerCase();
  var id=attr(e,'id'), nm=attr(e,'name'), al=attr(e,'aria-label');
  if(id && /^[A-Za-z][\w-]*$/.test(id)) return tag+'#'+id;
  if(nm) return tag+'[name="'+nm+'"]';
  if(al && al.length<60) return tag+'[aria-label="'+al.replace(/"/g,'\\"')+'"]';
  var tid=attr(e,'test-id')||attr(e,'data-testid');
  if(tid) return tag+'[test-id="'+tid+'"]';
  return tag;
}
"""

_DUMP_JS = _DEEP + r"""
var wantAll = arguments[0];
var grep = (arguments[1]||'').toLowerCase();
var all = deepAll(document), out = [];
for(var i=0;i<all.length;i++){
  var e=all[i], tag=(e.tagName||'').toLowerCase();
  var role=attr(e,'role'), nm=attr(e,'name'), id=attr(e,'id'), al=attr(e,'aria-label');
  var interactive = wantAll || tag==='button' || tag==='input' || tag==='select' ||
      tag==='textarea' || tag.indexOf('ytcp-button')>=0 || tag.indexOf('radio')>=0 ||
      tag.indexOf('checkbox')>=0 || tag.indexOf('paper-item')>=0 || tag.indexOf('menu-item')>=0 ||
      tag.indexOf('tab')>=0 || tag.indexOf('dropdown')>=0 || tag.indexOf('picker')>=0 ||
      role==='button' || role==='radio' || role==='checkbox' || role==='menuitem' ||
      role==='tab' || role==='textbox' || attr(e,'contenteditable')==='true' ||
      (id && /button|input|textbox|select|badge|toggle|picker/i.test(id));
  if(!interactive) continue;
  var visible = vis(e);
  if(!wantAll && !visible) continue;
  var rec = {
    tag: tag, id: id, name: nm, role: role, aria: al,
    text: txt(e).slice(0,70),
    checked: attr(e,'aria-checked'),
    disabled: (attr(e,'aria-disabled')||(e.hasAttribute&&e.hasAttribute('disabled')?'true':'')),
    type: attr(e,'type'), accept: attr(e,'accept'),
    visible: visible,
    shadow: hostPath(e),
    sel: bestSel(e)
  };
  if(grep){
    var hay=(rec.tag+' '+rec.id+' '+rec.name+' '+rec.role+' '+rec.aria+' '+rec.text+' '+rec.sel).toLowerCase();
    if(hay.indexOf(grep)<0) continue;
  }
  out.push(rec);
}
return out;
"""

_FIND_JS = _DEEP + r"""
var needle=(arguments[0]||'').toLowerCase();
var all=deepAll(document), out=[];
for(var i=0;i<all.length;i++){
  var e=all[i];
  var hay=((e.tagName||'')+' '+attr(e,'id')+' '+attr(e,'name')+' '+attr(e,'aria-label')+' '+txt(e)).toLowerCase();
  if(hay.indexOf(needle)<0) continue;
  if(e.children && e.children.length>3 && txt(e).length>200) continue;  // отсечь контейнеры
  var r={};
  try{ var b=e.getBoundingClientRect(); r={x:Math.round(b.left),y:Math.round(b.top),w:Math.round(b.width),h:Math.round(b.height)}; }catch(x){}
  out.push({
    tag:(e.tagName||'').toLowerCase(), id:attr(e,'id'), name:attr(e,'name'),
    role:attr(e,'role'), aria:attr(e,'aria-label'), checked:attr(e,'aria-checked'),
    disabled:attr(e,'aria-disabled'), text:txt(e).slice(0,90), rect:r,
    visible:vis(e), shadow:hostPath(e), sel:bestSel(e)
  });
  if(out.length>=40) break;
}
return out;
"""

# Polymer держит состояние компонента в JS-свойствах. Если оно читается —
# можно строить логику на СОСТОЯНИИ, а не на regex по тексту на 9 языках.
_PROPS_JS = _DEEP + r"""
var sel=arguments[0];
var all=deepAll(document), el=null;
for(var i=0;i<all.length;i++){
  var e=all[i];
  try{ if(e.matches && e.matches(sel)){ el=e; break; } }catch(x){}
}
if(!el) return {found:false};
function safe(v,d){
  d=d||0;
  if(v===null||v===undefined) return v;
  var t=typeof v;
  if(t==='string') return v.length>200? v.slice(0,200)+'…' : v;
  if(t==='number'||t==='boolean') return v;
  if(t==='function') return '[fn]';
  if(d>=2) return '[obj]';
  if(Array.isArray(v)) return v.slice(0,8).map(function(x){return safe(x,d+1);});
  if(v instanceof Node) return '[node '+((v.tagName||'').toLowerCase())+']';
  var o={}, n=0;
  for(var k in v){
    if(n++>40) break;
    try{ o[k]=safe(v[k],d+1); }catch(x){ o[k]='[err]'; }
  }
  return o;
}
var out={found:true, tag:(el.tagName||'').toLowerCase(), data:null, own:{}};
try{ if(el.__data) out.data=safe(el.__data,0); }catch(x){ out.data='[err]'; }
try{
  var proto=Object.getPrototypeOf(el);
  var names=Object.getOwnPropertyNames(el).concat(proto?Object.getOwnPropertyNames(proto):[]);
  var seen={};
  for(var i=0;i<names.length;i++){
    var k=names[i];
    if(seen[k]||k.charAt(0)==='_'||/^(constructor|shadowRoot|style|attributes|children)$/.test(k)) continue;
    seen[k]=1;
    try{
      var v=el[k];
      if(typeof v==='function') continue;
      out.own[k]=safe(v,1);
    }catch(x){}
  }
}catch(x){}
return out;
"""

# Краткий снимок: где мы сейчас в мастере. Читает состояние, а не текст.
_STATE_JS = _DEEP + r"""
var all=deepAll(document);
var dlg=null, steps=[], radios=[], boxes=[], fileIn=[], done=null, next=null;
for(var i=0;i<all.length;i++){
  var e=all[i], tag=(e.tagName||'').toLowerCase(), id=attr(e,'id');
  if(tag==='ytcp-uploads-dialog' && !dlg) dlg=e;
  if(id && id.indexOf('step-badge-')===0) steps.push({id:id, text:txt(e).slice(0,40), visible:vis(e), selected:attr(e,'aria-selected')||attr(e,'active')});
  if(tag.indexOf('radio')>=0 || attr(e,'role')==='radio'){
    var nm=attr(e,'name');
    if(nm) radios.push({name:nm, checked:attr(e,'aria-checked'), visible:vis(e), aria:attr(e,'aria-label').slice(0,50)});
  }
  if(id==='textbox' && attr(e,'aria-label')) boxes.push({aria:attr(e,'aria-label').slice(0,40), visible:vis(e), len:txt(e).length, text:txt(e).slice(0,40)});
  if(tag==='input' && attr(e,'type')==='file') fileIn.push({accept:attr(e,'accept').slice(0,40)});
  if(id==='done-button') done={text:txt(e).slice(0,30), disabled:attr(e,'aria-disabled'), visible:vis(e)};
  if(id==='next-button') next={text:txt(e).slice(0,30), disabled:attr(e,'aria-disabled'), visible:vis(e)};
}
var dlgOpen = !!(dlg && dlg.offsetParent!==null);
var body=(document.body.innerText||'').replace(/\s+/g,' ');
return {
  url: location.href,
  dialogOpen: dlgOpen,
  steps: steps, radios: radios, textboxes: boxes,
  fileInputs: fileIn, doneButton: done, nextButton: next,
  bodyHead: body.slice(0,300)
};
"""


# ---------------------------------------------------------------------------
# Сессия
# ---------------------------------------------------------------------------
def _save_session(d):
    with open(SESSION_FILE, "w") as f:
        json.dump(d, f)


def _load_session():
    if not os.path.exists(SESSION_FILE):
        sys.exit("❌ Нет открытой сессии. Сначала: ytdom.py open --profile <N> [--url ...]")
    with open(SESSION_FILE) as f:
        return json.load(f)


def _attach(sess):
    """Присоединиться к уже запущенному браузеру. driver.quit() НЕ вызываем —
    в режиме debuggerAddress он закрывает Chrome вместе с профилем."""
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    opts = Options()
    opts.add_experimental_option("debuggerAddress", sess["addr"])
    cd = sess.get("chromedriver") or ""
    drv = webdriver.Chrome(service=Service(cd), options=opts) if cd else webdriver.Chrome(options=opts)
    try:
        drv.set_page_load_timeout(180)
        drv.set_script_timeout(120)
    except Exception:
        pass
    return drv


def _out(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=1))


# ---------------------------------------------------------------------------
# Команды
# ---------------------------------------------------------------------------
def cmd_open(a):
    try:
        st = requests.get(f"{ADSPOWER_API}/status", timeout=5).json()
        if st.get("code") != 0:
            sys.exit(f"❌ AdsPower не отвечает: {st}")
    except Exception as e:
        sys.exit(f"❌ AdsPower недоступен ({e}) — запусти приложение AdsPower")

    addr = cd = None
    err = None
    for att in range(1, 6):
        time.sleep(1.5)  # AdsPower лимитирует ~1 req/sec
        try:
            r = requests.get(f"{ADSPOWER_API}/api/v1/browser/start", params={
                **_ads_param(a.profile), "open_tabs": 1, "ip_tab": 0,
                "headless": 1 if a.headless else 0,
            }, timeout=120).json()
            if r.get("code") == 0:
                addr = r["data"]["ws"]["selenium"]
                cd = r["data"].get("webdriver", "")
                break
            err = r.get("msg")
        except Exception as e:
            err = str(e)
        print(f"  … попытка {att}: {str(err)[:70]} — жду", file=sys.stderr)
        time.sleep(3 * att)
    if not addr:
        sys.exit(f"❌ browser/start не удался: {err}")

    _save_session({"addr": addr, "chromedriver": cd, "profile": str(a.profile)})
    print(f"✅ профиль {a.profile} открыт (debuggerAddress={addr})")

    if a.url:
        drv = _attach({"addr": addr, "chromedriver": cd})
        drv.get(a.url)
        time.sleep(a.wait)
        print(f"✅ открыт {drv.current_url}")


def cmd_go(a):
    drv = _attach(_load_session())
    drv.get(a.url)
    time.sleep(a.wait)
    print(f"✅ {drv.current_url}")


def cmd_js(a):
    drv = _attach(_load_session())
    expr = a.expr.strip()
    if not expr.startswith("return") and "\n" not in expr and ";" not in expr:
        expr = "return (" + expr + ")"
    _out(drv.execute_script(expr))


def cmd_dump(a):
    drv = _attach(_load_session())
    rows = drv.execute_script(_DUMP_JS, bool(a.all), a.grep or "")
    print(f"# {len(rows)} элементов | url={drv.current_url}\n")
    for r in rows:
        flags = []
        if r.get("checked"):
            flags.append(f"checked={r['checked']}")
        if r.get("disabled") == "true":
            flags.append("DISABLED")
        if not r.get("visible"):
            flags.append("hidden")
        if r.get("shadow"):
            flags.append("shadow:" + ">".join(r["shadow"][-2:]))
        if r.get("accept"):
            flags.append(f"accept={r['accept'][:24]}")
        print(f"  {r['sel']}")
        line = f"      text={r['text']!r}" if r["text"] else "      "
        if r.get("aria"):
            line += f" aria={r['aria'][:40]!r}"
        if flags:
            line += "  [" + " ".join(flags) + "]"
        print(line)
    if a.save:
        os.makedirs(DUMP_DIR, exist_ok=True)
        p = os.path.join(DUMP_DIR, f"{a.save}.json")
        with open(p, "w") as f:
            json.dump({"url": drv.current_url, "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                       "elements": rows}, f, ensure_ascii=False, indent=1)
        print(f"\n💾 {p}")


def cmd_find(a):
    drv = _attach(_load_session())
    _out(drv.execute_script(_FIND_JS, a.text))


def cmd_props(a):
    drv = _attach(_load_session())
    _out(drv.execute_script(_PROPS_JS, a.selector))


def cmd_state(a):
    drv = _attach(_load_session())
    _out(drv.execute_script(_STATE_JS))


def cmd_snap(a):
    drv = _attach(_load_session())
    os.makedirs(DUMP_DIR, exist_ok=True)
    base = os.path.join(DUMP_DIR, a.name)
    drv.save_screenshot(base + ".png")
    rows = drv.execute_script(_DUMP_JS, False, "")
    state = drv.execute_script(_STATE_JS)
    with open(base + ".json", "w") as f:
        json.dump({"url": drv.current_url, "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "state": state, "elements": rows}, f, ensure_ascii=False, indent=1)
    with open(base + ".txt", "w") as f:
        f.write(drv.execute_script("return document.body.innerText"))
    print(f"💾 {base}.png / .json ({len(rows)} эл.) / .txt")


def cmd_close(a):
    sess = _load_session()
    if not a.keep:
        try:
            requests.get(f"{ADSPOWER_API}/api/v1/browser/stop",
                         params=_ads_param(sess["profile"]), timeout=30)
            print("✅ профиль остановлен")
        except Exception as e:
            print(f"⚠️  stop: {e}")
    os.remove(SESSION_FILE)
    print("✅ сессия забыта")


def main():
    p = argparse.ArgumentParser(description="Живая консоль к странице в AdsPower-профиле")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("open");   s.add_argument("--profile", required=True); s.add_argument("--url", default=""); s.add_argument("--headless", action="store_true"); s.add_argument("--wait", type=float, default=8); s.set_defaults(fn=cmd_open)
    s = sub.add_parser("go");     s.add_argument("url"); s.add_argument("--wait", type=float, default=6); s.set_defaults(fn=cmd_go)
    s = sub.add_parser("js");     s.add_argument("expr"); s.set_defaults(fn=cmd_js)
    s = sub.add_parser("dump");   s.add_argument("--save", default=""); s.add_argument("--grep", default=""); s.add_argument("--all", action="store_true"); s.set_defaults(fn=cmd_dump)
    s = sub.add_parser("find");   s.add_argument("text"); s.set_defaults(fn=cmd_find)
    s = sub.add_parser("props");  s.add_argument("selector"); s.set_defaults(fn=cmd_props)
    s = sub.add_parser("state");  s.set_defaults(fn=cmd_state)
    s = sub.add_parser("snap");   s.add_argument("name"); s.set_defaults(fn=cmd_snap)
    s = sub.add_parser("close");  s.add_argument("--keep", action="store_true"); s.set_defaults(fn=cmd_close)

    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
