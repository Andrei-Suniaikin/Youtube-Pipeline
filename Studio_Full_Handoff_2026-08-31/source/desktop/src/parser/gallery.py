"""Галерея ревью подбора: parser/report.json → parser/review.html.

Локальная страница (открывается через file:// или `python3 -m http.server`
из папки <project>/parser/): по каждой сцене с судимыми кандидатами — карточка
с выбранным кадром и сеткой альтернатив. Правки (выбрать другой кадр / в AI /
брак) автосохраняются в localStorage, кнопка экспортирует decisions.json
для `parser_worker.py apply --project <dir>`.
"""
import json
import os
import shutil

_PAGE = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<title>Ревью подбора визуала</title>
<style>
 body{font-family:-apple-system,sans-serif;margin:20px;background:#111;color:#eee}
 a{color:#7aa7d6;text-decoration:none}
 #bar{position:sticky;top:0;background:#111;padding:10px 0;z-index:5;
      border-bottom:1px solid #2a2a2c;margin-bottom:14px}
 #bar .row{display:flex;gap:14px;align-items:center;flex-wrap:wrap}
 #exp{border:0;border-radius:6px;background:#1565c0;color:#fff;font-size:14px;
      padding:9px 14px;cursor:pointer}
 .f-btn{border:0;border-radius:6px;background:#2a2a2c;color:#bbb;font-size:13px;
      padding:8px 12px;cursor:pointer}
 .f-btn.sel{background:#f9a825;color:#111}
 #cnt{font-size:15px}
 .stats{color:#999;font-size:13px}
 .hint{color:#777;font-size:12px;margin-top:6px;line-height:1.5}
 .hint code{background:#1c1c1e;padding:1px 5px;border-radius:4px;color:#bbb}
 .scene{background:#1c1c1e;border-radius:10px;padding:12px;margin-bottom:16px;
        display:flex;gap:14px;border-left:4px solid transparent}
 .scene.st-pick{border-left-color:#1565c0}
 .scene.st-ai{border-left-color:#8e24aa}
 .scene.st-trash{border-left-color:#c62828}
 .left{flex:0 0 320px;min-width:0}
 .shead{font-size:13px;color:#bbb;margin-bottom:6px;line-height:1.5}
 .shead b{color:#eee;font-size:15px}
 .badge{display:inline-block;border-radius:4px;padding:1px 6px;font-size:11px;
        margin-left:4px;background:#333;color:#ccc}
 .badge.s-ok{background:#2e7d32;color:#fff}.badge.s-soft{background:#f9a825;color:#111}
 .badge.s-ai{background:#8e24aa;color:#fff}.badge.s-none{background:#555;color:#fff}
 .left img{width:100%;max-height:260px;object-fit:contain;background:#000;border-radius:6px}
 .plate{height:180px;display:flex;align-items:center;justify-content:center;
        background:#0a0a0a;border-radius:6px;color:#888;font-size:15px;
        border:1px dashed #333}
 .meta{font-size:11px;color:#999;margin:6px 0;line-height:1.5;word-break:break-word}
 .btns{display:flex;gap:6px;flex-wrap:wrap;margin-top:4px}
 .btns button{border:0;border-radius:6px;padding:7px 10px;cursor:pointer;
        font-size:13px;background:#333;color:#eee}
 .b-keep.sel{background:#2e7d32}.b-ai.sel{background:#8e24aa}.b-trash.sel{background:#c62828}
 .stline{font-size:12px;color:#8e9bb0;margin-top:6px;min-height:15px}
 .right{flex:1;display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));
        gap:8px;align-content:start}
 .tile{position:relative;background:#141416;border-radius:6px;padding:4px;
       border:2px solid #333;cursor:pointer}
 .tile.v-ok{border-color:#2e7d32}
 .tile.v-soft{border-color:#f9a825}
 .tile.v-reject,.tile.v-error{border-color:#3a3a3c;opacity:.45}
 .tile.picked{outline:3px solid #1e88e5;opacity:1}
 .tile img{width:100%;height:110px;object-fit:cover;border-radius:4px;display:block}
 .tile .noimg{width:100%;height:110px;display:flex;align-items:center;
       justify-content:center;color:#666;font-size:11px;background:#0a0a0a;border-radius:4px}
 .vbadge{position:absolute;top:8px;left:8px;font-size:10px;border-radius:4px;
       padding:1px 5px;background:rgba(0,0,0,.7);color:#eee}
 .tile.v-ok .vbadge{color:#81c784}.tile.v-soft .vbadge{color:#ffd54f}
 .tmeta{font-size:10px;color:#999;margin-top:3px;line-height:1.45;word-break:break-word}
 .b-pick{width:100%;margin-top:4px;border:0;border-radius:5px;padding:5px 0;
       cursor:pointer;font-size:11px;background:#263238;color:#cfd8dc}
 .tile.picked .b-pick{background:#1565c0;color:#fff}
</style></head><body>
<div id="bar">
 <div class="row">
  <b id="cnt"></b>
  <button id="exp">&#11015;&#65038; Экспорт decisions.json</button>
  <span id="filters">
   <button class="f-btn sel" data-f="doubt">Сомнительные</button>
   <button class="f-btn" data-f="all">Все</button>
   <button class="f-btn" data-f="ai">AI-сцены</button>
  </span>
  <span class="stats" id="stats"></span>
 </div>
 <div class="hint">Клики: <b>выбрать этот</b> на альтернативе — заменить кадр сцены ·
  <b>В AI</b> — отдать сцену AI-генерации · <b>Брак</b> — убрать кадр (сцена тоже уйдёт в AI) ·
  <b>Оставить</b> — вернуть как было. Всё сохраняется автоматически (localStorage).<br>
  Применить: экспортируй файл, положи его в <code>&lt;project&gt;/parser/decisions.json</code>
  и запусти <code>python3 parser_worker.py apply --project <span id="pdir"></span></code></div>
</div>
<div id="list"></div>
<script>
const DATA = __DATA__;
const KEY = 'parser_review_v1_' + DATA.project + '_' + (DATA.report_id || '');
let st = {};
try { st = JSON.parse(localStorage.getItem(KEY) || '{}'); } catch (e) { st = {}; }

function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,
 c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function save(){localStorage.setItem(KEY, JSON.stringify(st)); updBar();}
function updBar(){
 const n = Object.keys(st).length;
 document.getElementById('cnt').textContent = `Изменено: ${n} / ${DATA.cards.length}`;
 document.getElementById('exp').style.opacity = n ? 1 : .55;
}
const S = DATA.stats || {};
document.getElementById('stats').textContent =
 `ok=${S.ok||0} soft=${S.soft||0} ai=${S.ai||0} none=${S.none||0} skipped=${S.skipped||0}` +
 ` · сцен в отчёте: ${DATA.total_scenes} · карточек (с кандидатами): ${DATA.cards.length}` +
 ` · ниша: ${DATA.niche||'?'}`;
document.getElementById('pdir').textContent = DATA.project_dir;

const STL = {pick:'выбран другой кадр', ai:'→ AI-генерация', trash:'помечено браком (→ AI)'};
function setAct(scene, act, uid){
 const card = DATA.cards.find(c=>c.scene===scene);
 if (act==='keep' || (act==='pick' && card.chosen && card.chosen.uid===uid)) delete st[scene];
 else st[scene] = act==='pick' ? {action:'pick', uid:uid} : {action:act};
 save(); refresh(scene);
}
function refresh(scene){
 const card = DATA.cards.find(c=>c.scene===scene);
 const el = document.getElementById('sc'+scene);
 const v = st[scene];
 el.classList.remove('st-pick','st-ai','st-trash');
 if (v) el.classList.add('st-'+v.action);
 el.querySelectorAll('.btns button').forEach(b=>b.classList.remove('sel'));
 const selCls = v ? (v.action==='pick' ? null : '.b-'+v.action) : '.b-keep';
 if (selCls) el.querySelector(selCls).classList.add('sel');
 el.querySelectorAll('.tile').forEach(t=>t.classList.toggle('picked',
   !!(v && v.action==='pick' && t.dataset.uid===v.uid)));
 el.querySelector('.stline').textContent = v ? ('✏ ' + STL[v.action] +
   (v.uid ? ' · ' + v.uid : '')) : '';
}

const list = document.getElementById('list');
DATA.cards.forEach(card=>{
 const el = document.createElement('div');
 el.className = 'scene'; el.id = 'sc'+card.scene;
 const ch = card.chosen;
 let leftImg;
 if (ch && ch.img) {
  leftImg = `<a href="${esc(ch.page_url)}" target="_blank"><img src="${esc(ch.img)}" loading="lazy"></a>
   <div class="meta">${esc(ch.title)}<br>${esc(ch.source)} · ${esc(ch.license)} · clip=${ch.clip??'?'}
   · <a href="${esc(ch.page_url)}" target="_blank">источник ↗</a></div>`;
 } else if (ch) {
  leftImg = `<div class="plate">нет тумбы (${esc(ch.uid)})</div>
   <div class="meta">${esc(ch.title)} · <a href="${esc(ch.page_url)}" target="_blank">источник ↗</a></div>`;
 } else {
  leftImg = `<div class="plate">${card.status==='ai' ? '🤖 AI-фолбэк' : '∅ пусто'}</div>`;
 }
 el.innerHTML = `<div class="left">
   <div class="shead"><b>Сцена ${String(card.num).padStart(3,'0')}</b>
    <span class="badge s-${esc(card.status)}">${esc(card.status)}</span>
    <span class="badge">${esc(card.type)}</span>
    <span class="badge">канд. ${card.n}</span><br>
    ${esc(card.subject)}<br>
    <span style="color:#777">запросы: ${esc((card.queries||[]).join(' · '))}</span></div>
   ${leftImg}
   <div class="btns">
    <button class="b-keep">Оставить</button>
    <button class="b-ai">В AI</button>
    <button class="b-trash">Брак</button></div>
   <div class="stline"></div></div>
  <div class="right">` + card.judged.map(j=>`
   <div class="tile v-${esc(j.verdict)}" data-uid="${esc(j.uid)}" title="${esc(j.reason)}">
    <div class="vbadge">${esc(j.verdict)}${j.clip!=null ? ' · '+j.clip : ''}</div>
    ${j.img ? `<img src="${esc(j.img)}" loading="lazy">` : `<div class="noimg">нет тумбы</div>`}
    <div class="tmeta">${esc(j.source)} · ${esc(j.license)}
     · <a href="${esc(j.page_url)}" target="_blank">↗</a><br>${esc(j.reason)}</div>
    <button class="b-pick">выбрать этот</button></div>`).join('') + `</div>`;
 el.querySelector('.b-keep').onclick = ()=>setAct(card.scene,'keep');
 el.querySelector('.b-ai').onclick = ()=>setAct(card.scene,'ai');
 el.querySelector('.b-trash').onclick = ()=>setAct(card.scene,'trash');
 el.querySelectorAll('.tile').forEach(t=>{
  t.onclick = (e)=>{ if (e.target.tagName!=='A') setAct(card.scene,'pick',t.dataset.uid); };
 });
 list.appendChild(el);
 refresh(card.scene);
});
updBar();

// фильтры: «Сомнительные» (дефолт) = soft/staple/cluster b-roll; «AI-сцены» = ушедшие в генерацию
function isDoubt(card){
 const doubtMark = s => ['staple','cluster b-roll'].some(t=>String(s||'').indexOf(t)>=0);
 return card.status==='soft' ||
        (card.chosen && doubtMark(card.chosen.reason)) ||
        (card.judged||[]).some(j=>doubtMark(j.reason) && card.chosen && card.chosen.uid===j.uid);
}
function applyFilter(mode){
 DATA.cards.forEach(card=>{
  const el = document.getElementById('sc'+card.scene);
  let show = true;
  if (mode==='doubt') show = isDoubt(card);
  else if (mode==='ai') show = card.status==='ai' || card.status==='none';
  el.style.display = show ? '' : 'none';
 });
 document.querySelectorAll('.f-btn').forEach(b=>b.classList.toggle('sel', b.dataset.f===mode));
}
document.querySelectorAll('.f-btn').forEach(b=>{ b.onclick = ()=>applyFilter(b.dataset.f); });
applyFilter('doubt');

document.getElementById('exp').onclick = ()=>{
 const out = Object.entries(st).map(([scene,v])=>{
  const d = {scene:+scene, action: v.action==='trash' ? 'ai' : v.action};
  if (v.uid) d.uid = v.uid;
  return d;
 }).sort((a,b)=>a.scene-b.scene);
 const wrapped = {report_id: DATA.report_id || '', decisions: out};
 const blob = new Blob([JSON.stringify(wrapped,null,1)],{type:'application/json'});
 const a = document.createElement('a');
 a.href = URL.createObjectURL(blob); a.download = 'decisions.json'; a.click();
};
</script></body></html>"""


def write_review(project_dir, log=print) -> str:
    """Собирает parser/review.html из parser/report.json. Возвращает путь к html."""
    project_dir = os.path.abspath(project_dir)
    parser_dir = os.path.join(project_dir, "parser")
    rep_path = os.path.join(parser_dir, "report.json")
    report = json.load(open(rep_path, encoding="utf-8"))

    subjects = {}  # scenes.json опционален — только для подписи сцены
    scenes_path = os.path.join(project_dir, "googler", "scenes.json")
    if os.path.exists(scenes_path):
        try:
            for i, s in enumerate(json.load(open(scenes_path, encoding="utf-8"))):
                subjects[i] = (s.get("subject") or "")[:160]
        except Exception as e:  # noqa: BLE001
            log(f"[gallery] scenes.json не прочитан ({e}) — карточки без subject")

    cards = []
    for r in report.get("scenes", []):
        judged = r.get("judged") or []
        if not judged:
            continue  # карточки только по сценам, где судья что-то смотрел
        idx = r["scene"]

        def img_for(path, uid):
            """Относительный URL тумбы: путь из отчёта (candidates/, _broll/) как есть;
            файлы вне parser/ (библиотека) копируются в review_assets/; фолбэк —
            старая реконструкция для отчётов без path."""
            if path and os.path.exists(path):
                ap = os.path.abspath(path)
                if ap.startswith(parser_dir + os.sep):
                    return os.path.relpath(ap, parser_dir).replace(os.sep, "/")
                assets = os.path.join(parser_dir, "review_assets")
                os.makedirs(assets, exist_ok=True)
                dst = os.path.join(assets, uid + ".jpg")
                if not os.path.exists(dst):
                    try:
                        shutil.copy2(ap, dst)
                    except OSError:
                        return None
                return "review_assets/" + uid + ".jpg"
            p = f"candidates/{idx:03d}/{uid}.jpg"
            return p if os.path.exists(os.path.join(parser_dir, p)) else None

        ch = r.get("chosen")
        chosen = None
        if ch:
            chosen = {"uid": ch["uid"], "img": img_for(ch.get("thumb_path"), ch["uid"]),
                      "title": ch.get("title", ""), "page_url": ch.get("page_url", ""),
                      "source": ch.get("source", ""), "license": ch.get("license", ""),
                      "clip": ch.get("clip"),
                      # B3: reason выбранного кадра (staple/cluster b-roll) —
                      # фильтр «Сомнительные» показывает такие сцены на ревью
                      "reason": ch.get("reason", "")}
        cards.append({
            "scene": idx, "num": idx + 1, "status": r.get("status", "?"),
            "type": r.get("type", ""), "queries": r.get("queries") or [],
            "n": r.get("n_candidates", 0), "subject": subjects.get(idx, ""),
            "chosen": chosen,
            "judged": [{"uid": j["uid"], "img": img_for(j.get("path"), j["uid"]),
                        "source": j.get("source", ""), "title": j.get("title", ""),
                        "page_url": j.get("page_url", ""), "license": j.get("license", ""),
                        "clip": j.get("clip"), "verdict": j.get("verdict", "?"),
                        "reason": j.get("reason", "")} for j in judged],
        })

    payload = {"project": os.path.basename(project_dir), "project_dir": project_dir,
               "niche": report.get("niche", ""), "stats": report.get("stats", {}),
               "report_id": report.get("report_id", ""),
               "total_scenes": len(report.get("scenes", [])), "cards": cards}
    # <\/ — чтобы тайтлы с "</..." не разорвали <script>
    data_json = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    html = _PAGE.replace("__DATA__", data_json)

    out_path = os.path.join(parser_dir, "review.html")
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(html)
    os.replace(tmp, out_path)
    log(f"[gallery] карточек: {len(cards)} → {out_path}")
    return out_path
