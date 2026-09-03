#!/usr/bin/env python3
"""Пост-проход по видео в YouTube Studio: A/B тест обложек и монетизация.

A/B обложек и монетизация в Studio доступны ПОСЛЕ публикации (A/B гоняет тест на
живом трафике; монетизация — на партнёрском канале). Поэтому это отдельный проход,
не часть заливки: заливщик кладёт видео (public/scheduled), затем этот тул ставит
A/B и/или монетизацию по video_id.

Флоу разведаны вживую 16.07.2026 на двух профилях; их реальные serials санитизированы:
- A/B: #ab-test-button → выбор режима «Thumbnail only» → редактор слотов
  (file-инпуты accept image/jpeg,image/png в контейнерах «Add thumbnail») →
  «Set test» (нужно ≥2 варианта) → закрытие диалога → главный Save страницы.
- Монетизация: /video/{id}/monetization → значение On/Off (YTCP-VIDEO-MONETIZATION) +
  чекбокс «Show mid-roll ads» → YTCP-BUTTON#save. На партнёрском канале новые видео уже On.

КОНТРАКТ A/B (аддитивный, недеструктивный): переданные обложки заливаются в СВОБОДНЫЕ
слоты диалога; текущая обложка видео, если она уже в слоте 1, остаётся вариантом теста.
Итоговый тест = существующие варианты + переданные. Если свободных слотов меньше, чем
файлов — тул НЕ роняет молча файлы, а честно падает с сообщением (передай меньше файлов).

Studio на Polymer → элементы в shadow DOM, ищем пробивающим JS (window.__all()).

Пример:
  python3 tools/studio_ab_monet.py --profile 9003 --video DEMO0000001 \
      --ab a.jpg,b.jpg,c.jpg --monetize on --midroll on
  python3 tools/studio_ab_monet.py --profile 9003 --video DEMO0000001 --dry

⚠️ Set test / Save меняют состояние реального канала. Тул после каждого коммита
перечитывает страницу и возвращает успех ТОЛЬКО при подтверждённом изменении.
"""
import argparse
import os
import sys
import time

import requests

ADS = os.environ.get("ADSPOWER_API", "http://localhost:50325")

# пробивающий shadow DOM обход — регистрируем window.__all()
PIERCE = r"""
function* walk(root){const ns=root.querySelectorAll('*');for(const n of ns){yield n;if(n.shadowRoot)yield* walk(n.shadowRoot);}}
window.__all=()=>[...walk(document)];
"""


def log(m):
    print(m, flush=True)


SHOTS = ""  # каталог для скриншотов ключевых моментов (--shots)


def shot(d, name):
    if SHOTS:
        try:
            d.save_screenshot(os.path.join(SHOTS, f"{name}.png"))
        except Exception:  # noqa: BLE001
            pass


def _pierce(d):
    """(Пере)регистрирует window.__all() — DOM мог перерисоваться после навигации."""
    d.execute_script(PIERCE)


def _wait(d, js, timeout=25, interval=1.0, want=True):
    """Поллит JS-предикат до want или таймаута. Возвращает последнее значение.
    Заменяет слепые sleep: на медленном прокси Polymer рендерится дольше."""
    last = None
    steps = max(1, int(timeout / interval))
    for _ in range(steps):
        try:
            last = d.execute_script(js)
        except Exception:  # noqa: BLE001
            last = None
        if bool(last) == want:
            return last
        time.sleep(interval)
    return last


def _wait_page(d, needle=None, timeout=30):
    """Ждёт готовности документа и (опц.) появления текста-маркера на странице."""
    _wait(d, "return document.readyState==='complete'", timeout=timeout)
    if needle:
        _wait(d, f"return (document.body?document.body.innerText:'').indexOf({needle!r})>=0",
              timeout=timeout)


def _ads_param(pid):
    return {"serial_number": pid} if str(pid).isdigit() else {"user_id": pid}


def start_profile(pid, headless=False):
    err = None
    for att in range(1, 6):
        try:
            time.sleep(1.5)  # AdsPower rate-limit ~1 req/s
            r = requests.get(f"{ADS}/api/v1/browser/start",
                             params={**_ads_param(pid), "open_tabs": 1,
                                     "headless": 1 if headless else 0}, timeout=120).json()
            if r.get("code") == 0:
                return r["data"]["ws"]["selenium"], r["data"].get("webdriver", "")
            err = r.get("msg")
        except Exception as e:  # noqa: BLE001
            err = str(e)
        time.sleep(3 * att)
    raise RuntimeError(f"AdsPower start: {err}")


def stop_profile(pid):
    try:
        requests.get(f"{ADS}/api/v1/browser/stop", params=_ads_param(pid), timeout=30)
    except Exception:  # noqa: BLE001
        pass


def attach(addr, drv):
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    o = Options()
    o.add_experimental_option("debuggerAddress", addr)
    d = webdriver.Chrome(service=Service(drv), options=o) if drv else webdriver.Chrome(options=o)
    d.set_page_load_timeout(180)
    d.set_script_timeout(120)
    return d


# ---------------- A/B обложек ----------------

def _ab_editor_open(d):
    """True, если открыт именно слот-редактор A/B (есть метки 'Thumbnail N') —
    в отличие от экрана выбора режима, где кнопка Set test тоже присутствует."""
    return d.execute_script(r"""
    function vis(e){const r=e.getBoundingClientRect?e.getBoundingClientRect():null;return r&&r.width>0&&r.height>0;}
    for(const e of window.__all()){const t=(e.innerText||'').trim();
      if(vis(e)&&t.length<40&&/^thumbnail\s*\d/i.test(t)) return true;}
    return false;""")


def _ab_slots(d):
    """Состояние слот-редактора A/B: {total, empty:[WebElement]}.
    Пустой слот = file-инпут (accept image), у которого БЛИЖАЙШИЙ предок с коротким
    текстом (<80) содержит 'Add thumbnail' — точно по-слотно: у заполнённого слота
    этот текст пропадает. (Проверено вживую; более широкий матч ловил весь редактор.)"""
    empty = d.execute_script(r"""
    function up(n){return n.parentElement || (n.getRootNode&&n.getRootNode().host) || null;}
    const res=[];
    for(const e of window.__all()){
      if(e.tagName==='INPUT'&&e.type==='file'&&/image/i.test(e.getAttribute('accept')||'')){
        let n=e, hops=0;
        while(n&&hops<12){
          const t=(n.innerText||'').trim();
          if(t.length<80&&/add thumbnail/i.test(t)){res.push(e);break;}
          if(t.length>=80)break;
          n=up(n);hops++;
        }
      }
    }
    return res;""")
    total = d.execute_script(r"""
    let total=0;
    for(const e of window.__all()){const t=(e.innerText||'').trim();
      if(t.length<40&&/^thumbnail\s*\d/i.test(t)) total++;}
    return total;""")
    return {"empty": empty, "total": total}


def _ab_test_present(d):
    """Сигналы, что thumbnail-эксперимент реально стоит на видео (после reload /edit).
    Заземлено на живой факт: после сохранения теста в секции обложки появляется
    статус ('Ineligible' для scheduled / 'Active'/'No leader yet' для public) и
    полоска из нескольких обложек."""
    return d.execute_script(r"""
    function vis(e){const r=e.getBoundingClientRect?e.getBoundingClientRect():null;return r&&r.width>0&&r.height>0;}
    let status=null, tiles=0, exp=false;
    for(const e of window.__all()){
      const t=(e.innerText||'').trim();
      if(vis(e)&&t.length<40&&/^(ineligible|active|no leader yet|test in progress|comparing|winner selected|no clear leader)$/i.test(t)) status=t;
      const id=((e.id||'')+' '+((e.getAttribute&&e.getAttribute('test-id'))||'')).toLowerCase();
      if(/thumbnail.*(experiment|test)|ab.?test.*thumbnail/.test(id)) exp=true;
      if(e.tagName==='IMG'&&vis(e)){
        const rr=e.getBoundingClientRect();
        if(rr.width>40&&rr.width<260&&rr.height>24&&rr.height<170){
          const anc=(e.closest&&e.closest('[id]'));
          if(anc&&/thumbnail/i.test(anc.id)) tiles++;
        }
      }
    }
    return {status: status, tiles: tiles, exp: exp};""")


def set_ab_thumbnails(d, video_id, thumb_paths, dry=False) -> bool:
    """Аддитивно ставит A/B тест обложек: заливает файлы в свободные слоты, жмёт
    Set test и главный Save, затем ПОДТВЕРЖДАЕТ по перезагруженной странице.
    Возвращает True только при подтверждённом тесте. Ничего не удаляет."""
    if not (1 <= len(thumb_paths) <= 3):
        # 18.07: 1 файл валиден — тест «текущая обложка + 1 вариант» (аддитивная семантика)
        log(f"[A/B] передано {len(thumb_paths)} файлов — нужно 1-3")
        return False
    for p in thumb_paths:  # существование проверяем ДО открытия браузера-состояния
        if not os.path.exists(p):
            log(f"[A/B] нет файла: {p}")
            return False

    d.get(f"https://studio.youtube.com/video/{video_id}/edit")
    _wait_page(d, needle="Video details", timeout=40)
    _pierce(d)
    if not _wait(d, "return !!window.__all().find(e=>e.id==='ab-test-button')", timeout=20):
        log("[A/B] кнопка #ab-test-button не найдена (видео не опубликовано / нет фичи)")
        return False

    # 1. открыть A/B
    d.execute_script(r"""for(const e of window.__all()){if(e.id==='ab-test-button'){e.click();return;}}""")
    time.sleep(3)
    _pierce(d)

    # 1b. если на видео УЖЕ есть тест, Studio спрашивает «Run a new test? current test
    # will be deleted» — по умолчанию НЕ удаляем чужой тест, честно прекращаем
    if d.execute_script(r"""
    function vis(e){const r=e.getBoundingClientRect();return r.width>0&&r.height>0;}
    for(const e of window.__all()){const t=(e.innerText||'');
      if(vis(e)&&/current test will be deleted|run a new test\?/i.test(t)) return true;}
    return false;"""):
        log("[A/B] на видео уже стоит A/B тест (Studio предлагает удалить текущий) — "
            "не перезаписываю существующий тест, прекращаю")
        shot(d, "ab_existing_test")
        return False

    # 2. если показан экран выбора режима — выбрать 'Thumbnail only' и ДОЖДАТЬСЯ
    # слот-редактора; кликаем кандидатов без тег-фильтра (клик по реальной опции)
    # с проверкой, что редактор открылся (иначе прошлый тег-фильтр молча промахивался)
    if not _ab_editor_open(d):
        mode_present = d.execute_script(r"""
        function vis(e){const r=e.getBoundingClientRect();return r.width>0&&r.height>0;}
        let a=false,b=false;
        for(const e of window.__all()){const t=(e.innerText||'').trim();
          if(vis(e)&&t.toLowerCase()==='thumbnail only')a=true;
          if(vis(e)&&t.toLowerCase()==='title only')b=true;}
        return a&&b;""")
        if mode_present:
            for attempt in range(4):
                n = d.execute_script(r"""
                function vis(e){const r=e.getBoundingClientRect();return r.width>0&&r.height>0;}
                const idx=arguments[0];
                const cands=window.__all().filter(e=>vis(e)&&(e.innerText||'').trim().toLowerCase()==='thumbnail only');
                if(idx>=cands.length) return cands.length;
                cands[idx].click(); return cands.length;""", attempt)
                time.sleep(2)
                _pierce(d)
                if _ab_editor_open(d):
                    break
                if attempt + 1 >= (n or 0):
                    break
        if not _ab_editor_open(d):
            log("[A/B] не удалось открыть слот-редактор 'Thumbnail only' "
                "(другая локаль Studio / изменившийся UI) — безопасно прекращаю")
            shot(d, "ab_no_editor")
            return False

    # 3. чтение слотов открытого редактора
    slots = _ab_slots(d)
    empty = slots["empty"]
    log(f"[A/B] слотов всего: {slots['total'] or '?'}, свободно: {len(empty)}")

    if dry:
        ok = len(empty) >= len(thumb_paths)
        log(f"[A/B] --dry: свободных слотов {'хватает' if ok else 'НЕ хватает'} "
            f"под {len(thumb_paths)} файлов; Set test НЕ жму")
        return ok

    if len(empty) < len(thumb_paths):
        log(f"[A/B] ❌ свободно {len(empty)} слот(ов), передано {len(thumb_paths)} — "
            f"файлы молча не роняю. Освободи слоты в UI или передай ≤{len(empty)} файлов.")
        shot(d, "ab_not_enough_slots")
        return False

    # 4. заливка по одному в первый свободный слот; DOM перерисовывается — ищем заново
    uploaded = 0
    for p in thumb_paths:
        cur_empty = _ab_slots(d)["empty"]
        if not cur_empty:
            log(f"[A/B] пропал свободный слот для {os.path.basename(p)}")
            shot(d, "ab_slot_lost")
            return False
        before = len(cur_empty)
        inp = cur_empty[0]
        d.execute_script("arguments[0].removeAttribute('hidden');arguments[0].style.display='block';", inp)
        inp.send_keys(os.path.abspath(p))
        # ждём, пока свободных слотов станет меньше (слот реально заполнился)
        ok_fill = False
        for _ in range(25):
            time.sleep(1)
            if len(_ab_slots(d)["empty"]) < before:
                ok_fill = True
                break
        if not ok_fill:
            log(f"[A/B] слот не заполнился: {os.path.basename(p)}")
            shot(d, "ab_upload_fail")
            return False
        uploaded += 1
        log(f"[A/B] залит вариант {uploaded}/{len(thumb_paths)} ← {os.path.basename(p)}")
    shot(d, "ab_before_settest")

    # 5. Set test — видимая и enabled YTCP-BUTTON, кликаем внутренний <button>.
    # Кнопка активируется НЕ мгновенно: детектор заполнения слота выше срабатывает по исчезновению
    # текста «Add thumbnail», а его убирает и спиннер — то есть картинка может ещё догружаться.
    # Поэтому не одна проверка, а поллинг (раньше единственная попытка давала ложное «недоступна»).
    set_ok = False
    for _ in range(12):
        time.sleep(2)
        set_ok = d.execute_script(r"""
        function vis(e){const r=e.getBoundingClientRect();return r.width>0&&r.height>0;}
        for(const e of window.__all()){const t=(e.innerText||'').trim();
          if(e.tagName==='YTCP-BUTTON'&&/^set test$/i.test(t)&&vis(e)
             &&!e.hasAttribute('disabled')&&e.getAttribute('aria-disabled')!=='true'){
            const b=e.querySelector('button'); (b||e).click(); return true;}}
        return false;""")
        if set_ok:
            break
    if not set_ok:
        # «кнопки нет» и «кнопка есть, но disabled» лечатся по-разному — раньше были неразличимы
        census = d.execute_script(r"""
        function vis(e){const r=e.getBoundingClientRect();return r.width>0&&r.height>0;}
        const out=[];
        for(const e of window.__all()){const t=(e.innerText||'').trim();
          if(/set test/i.test(t)&&t.length<40)
            out.push({tag:e.tagName, txt:t.slice(0,30), vis:vis(e),
                      dis:e.hasAttribute('disabled'), aria:e.getAttribute('aria-disabled')});}
        return out.slice(0,6);""")
        log(f"[A/B] ❌ Set test не нажалась. Кандидаты в DOM: {census}")
        shot(d, "ab_settest_disabled")
        return False
    log("[A/B] Set test нажат")

    # 6. диалог должен закрыться (по видимости — Polymer держит его в DOM)
    dialog_open = _wait(d, r"""
    function vis(e){const r=e.getBoundingClientRect();return r.width>0&&r.height>0;}
    return !!window.__all().find(e=>e.tagName==='YTCP-BUTTON'&&/^set test$/i.test((e.innerText||'').trim())&&vis(e));
    """, timeout=12, want=False)
    if dialog_open:
        log("[A/B] ❌ диалог Set test не закрылся — тест НЕ принят")
        shot(d, "ab_dialog_stuck")
        return False

    # 7. тест в черновике страницы → коммит = главный Save страницы edit
    page_saved = d.execute_script(r"""
    function vis(e){const r=e.getBoundingClientRect();return r.width>0&&r.height>0;}
    for(const e of window.__all()){const t=(e.innerText||'').trim();
      if(e.tagName==='YTCP-BUTTON'&&/^save$/i.test(t)&&vis(e)
         &&!e.hasAttribute('disabled')&&e.getAttribute('aria-disabled')!=='true'){
        const b=e.querySelector('button');(b||e).click();return true;}}
    return false;""")
    log(f"[A/B] Save страницы нажат: {page_saved}")
    if not page_saved:
        # Save мог быть неактивен, если Set test сам сохранил — проверим ниже верификацией
        log("[A/B] активный Save не найден — проверяю сохранность теста напрямую")
    time.sleep(4)
    shot(d, "ab_after_save")

    # 8. ВЕРИФИКАЦИЯ (гейт возврата): reload и реальные сигналы теста на странице
    d.get(f"https://studio.youtube.com/video/{video_id}/edit")
    _wait_page(d, needle="Video details", timeout=40)
    _pierce(d)
    time.sleep(2)
    sig = _ab_test_present(d)
    shot(d, "ab_verify")
    present = bool(sig.get("status")) or bool(sig.get("exp")) or sig.get("tiles", 0) >= 2
    log(f"[A/B] верификация: status={sig.get('status')}, полоса-обложек={sig.get('tiles')}, "
        f"exp-контейнер={sig.get('exp')} → {'✅ тест стоит' if present else '❌ теста нет'}")
    return present


# ---------------- монетизация ----------------

def _read_monet_state(d):
    """Текущее состояние: ads (On/Off/None — None=не прочитано) и midroll
    (True/False/None — None=чекбокса нет, напр. для коротких видео)."""
    return d.execute_script(r"""
    function vis(e){const r=e.getBoundingClientRect?e.getBoundingClientRect():null;return r&&r.width>0&&r.height>0;}
    let ads=null, mid=null;
    for(const e of window.__all()){
      if(ads===null && e.tagName==='YTCP-VIDEO-MONETIZATION'){
        const m=(e.innerText||'').match(/\b(On|Off)\b/); if(m) ads=m[1];
      }
      if(mid===null && vis(e) && (e.tagName==='YTCP-CHECKBOX-LITE'||(e.getAttribute&&e.getAttribute('role'))==='checkbox')){
        mid = e.getAttribute('aria-checked')==='true';
      }
    }
    return {ads: ads, midroll: mid};""")


def set_monetization(d, video_id, on=True, midroll=True, dry=False) -> bool:
    """Ставит монетизацию видео: Watch Page ads On/Off + мид-роллы. Партнёрский канал.
    Идемпотентно и безопасно: не мутирует вслепую при нечитаемом состоянии,
    проверяет что дропдаун открылся и значение сменилось ДО клика по чекбоксу/Save,
    возвращает успех только при подтверждённом состоянии."""
    d.get(f"https://studio.youtube.com/video/{video_id}/monetization")
    _wait_page(d, timeout=40)
    if "/monetization" not in d.current_url:
        log("[monet] вкладка недоступна — канал не в YPP")
        return False
    _pierce(d)
    # Заголовок «Ways to earn» рендерится РАНЬШЕ контента (лэйаут 07.2026) — текстовый гейт
    # срабатывал до отрисовки компонента и state читался как None. Ждём САМ компонент.
    comp_ready = False
    for _ in range(12):  # до ~36с
        _pierce(d)
        try:
            comp_ready = d.execute_script(
                "for(const e of window.__all()){if(e.tagName==='YTCP-VIDEO-MONETIZATION')return true;}return false;")
        except Exception:  # noqa: BLE001
            comp_ready = False
        if comp_ready:
            break
        time.sleep(3)
    if not comp_ready:
        txt = d.execute_script("return (document.body.innerText||'')")
        if "Ways to earn" not in txt and "Watch Page ads" not in txt:
            log("[monet] страница монетизации не распозналась")
        else:
            log("[monet] компонент YTCP-VIDEO-MONETIZATION не отрисовался за 36с")
        return False

    cur = _read_monet_state(d)
    log(f"[monet] текущее состояние: ads={cur.get('ads')}, midroll={cur.get('midroll')}")
    shot(d, "monet_before")

    # None-guard: состояние не прочиталось → не трогаем реальный канал вслепую
    if cur.get("ads") is None:
        log("[monet] ❌ не смог прочитать значение Watch Page ads — не мутирую вслепую")
        return False

    want = "On" if on else "Off"
    mid_applicable = cur.get("midroll") is not None  # чекбокс есть на странице
    want_mid = bool(midroll) if (on and mid_applicable) else None

    ads_ok = cur.get("ads") == want
    mid_ok = (not on) or (not mid_applicable) or (cur.get("midroll") == want_mid)
    if ads_ok and mid_ok:
        log("[monet] уже в нужном состоянии — сохранять нечего ✅")
        return True
    if dry:
        log(f"[monet] --dry: нужно ads→{want}"
            f"{'' if not on or not mid_applicable else f', midroll→{want_mid}'}; коммит НЕ делаю")
        return True

    # 1. переключить ads, если нужно — с проверкой каждого шага (нет частичных коммитов)
    if not ads_ok:
        d.execute_script(r"""
        for(const e of window.__all()){
          if(e.tagName==='YTCP-VIDEO-MONETIZATION'){
            const c=e.querySelector('#container')||(e.shadowRoot&&e.shadowRoot.querySelector('#container'));
            (c||e).click(); return;}}""")
        opened = _wait(d, r"""
        function vis(e){const r=e.getBoundingClientRect();return r.width>0&&r.height>0;}
        return !!window.__all().find(e=>vis(e)&&/^(On|Off)$/.test((e.innerText||'').trim())
          &&((e.getAttribute&&e.getAttribute('role'))==='option'||e.tagName==='TP-YT-PAPER-ITEM'));
        """, timeout=8)
        if not opened:
            log("[monet] ❌ дропдаун Watch Page ads не открылся — прекращаю без изменений")
            shot(d, "monet_dropdown_stuck")
            return False
        d.execute_script(r"""
        function vis(e){const r=e.getBoundingClientRect();return r.width>0&&r.height>0;}
        const want=arguments[0];
        for(const e of window.__all()){const t=(e.innerText||'').trim();
          if(vis(e)&&t===want&&((e.getAttribute&&e.getAttribute('role'))==='option'||e.tagName==='TP-YT-PAPER-ITEM')){e.click();return;}}""",
                         want)
        # подтвердить, что значение реально сменилось ДО касания чекбокса/Save
        changed = _wait(d, f"return (window.__all().find(e=>e.tagName==='YTCP-VIDEO-MONETIZATION')||{{}}).innerText"
                           f"&& /\\b{want}\\b/.test(window.__all().find(e=>e.tagName==='YTCP-VIDEO-MONETIZATION').innerText)",
                        timeout=8)
        if not changed:
            log(f"[monet] ❌ значение ads не сменилось на {want} — прекращаю без изменений")
            shot(d, "monet_value_stuck")
            return False

    # 2. чекбокс мид-роллов — только если он есть и нужно менять
    if on and mid_applicable and cur.get("midroll") != want_mid:
        d.execute_script(r"""
        function vis(e){const r=e.getBoundingClientRect();return r.width>0&&r.height>0;}
        const want=arguments[0];
        for(const e of window.__all()){
          if(vis(e)&&(e.tagName==='YTCP-CHECKBOX-LITE'||(e.getAttribute&&e.getAttribute('role')==='checkbox'))){
            const checked=e.getAttribute('aria-checked')==='true';
            if(checked!==want)e.click();return;}}""", bool(want_mid))
        time.sleep(1)

    # 3. Save — строго YTCP-BUTTON#save (disabled на хосте И на внутренней <button>)
    saved = d.execute_script(r"""
    function vis(e){const r=e.getBoundingClientRect();return r.width>0&&r.height>0;}
    for(const e of window.__all()){
      if(e.tagName==='YTCP-BUTTON'&&e.id==='save'&&vis(e)
         &&!e.hasAttribute('disabled')&&e.getAttribute('aria-disabled')!=='true'){
        const b=e.querySelector('button');(b||e).click();return true;}}
    return false;""")
    log(f"[monet] Save нажат: {saved}")
    if not saved:
        log("[monet] ❌ Save неактивен — изменение не закоммичено")
        shot(d, "monet_save_fail")
        return False
    time.sleep(4)

    # 4. верификация: reload и перечитать
    d.get(f"https://studio.youtube.com/video/{video_id}/monetization")
    _wait_page(d, timeout=40)
    _pierce(d)
    _wait(d, "return (document.body.innerText||'').indexOf('Watch Page ads')>=0", timeout=20)
    after = _read_monet_state(d)
    shot(d, "monet_verify")
    ads_final = after.get("ads") == want
    mid_final = (not on) or (after.get("midroll") is None) or (after.get("midroll") == want_mid)
    ok = ads_final and mid_final
    log(f"[monet] после Save: ads={after.get('ads')}, midroll={after.get('midroll')} "
        f"→ {'✅ подтверждено' if ok else '❌ НЕ совпало'}")
    return ok


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", required=True, help="AdsPower profile (serial или user_id)")
    ap.add_argument("--video", required=True, help="YouTube video id")
    ap.add_argument("--ab", default="", help="обложки A/B через запятую (2-3 файла)")
    ap.add_argument("--monetize", choices=["on", "off"], default="")
    ap.add_argument("--midroll", choices=["on", "off"], default="on")
    ap.add_argument("--dry", action="store_true", help="открыть/прочитать, но не коммитить")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--keep-open", action="store_true", help="не гасить профиль в конце")
    ap.add_argument("--shots", default="", help="каталог для скриншотов ключевых моментов")
    a = ap.parse_args()
    if a.shots:
        global SHOTS
        SHOTS = a.shots
        os.makedirs(SHOTS, exist_ok=True)

    ab_paths = [p.strip() for p in a.ab.split(",") if p.strip()] if a.ab else []
    if not ab_paths and not a.monetize:
        log("Нечего делать: укажи --ab и/или --monetize")
        sys.exit(2)
    # существование файлов проверяем ДО запуска браузера (в т.ч. в --dry)
    missing = [p for p in ab_paths if not os.path.exists(p)]
    if missing:
        log(f"[A/B] нет файлов: {', '.join(missing)}")
        sys.exit(2)
    if ab_paths and not (2 <= len(ab_paths) <= 3):
        log(f"[A/B] передано {len(ab_paths)} файлов — нужно 2-3")
        sys.exit(2)

    addr, drv = start_profile(a.profile, a.headless)
    d = None
    rc = 0
    try:
        d = attach(addr, drv)  # в try: при падении attach профиль всё равно погасим
        if ab_paths:
            if not set_ab_thumbnails(d, a.video, ab_paths, dry=a.dry):
                rc = 1
        if a.monetize:
            if not set_monetization(d, a.video, on=(a.monetize == "on"),
                                    midroll=(a.midroll == "on"), dry=a.dry):
                rc = 1
    finally:
        if d is not None and not a.keep_open:
            try:
                d.quit()  # освободить chromedriver-процесс локального Service
            except Exception:  # noqa: BLE001
                pass
        if not a.keep_open:
            stop_profile(a.profile)
    sys.exit(rc)


if __name__ == "__main__":
    main()
