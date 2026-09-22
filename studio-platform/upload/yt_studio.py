#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""yt_studio — state-driven слой над YouTube Studio.

Принцип: читаем РЕАЛЬНОЕ состояние Polymer-компонентов Studio вместо угадывания
по тексту страницы. Studio отдаёт машинные константы (VIDEO_PRIVACY_PUBLIC,
DRAFT_STATUS_NONE, workflowStep=REVIEW), одинаковые на всех языках интерфейса —
поэтому здесь нет ни одного regex по словам вроде "Опубликовано|Published|Veřejné".

Карта DOM снята живьём с studio.youtube.com — см. research/dom/SELECTOR_MAP.md.
Каждый селектор ниже подтверждён дампом, а не памятью. При изменении Studio
запускать tools/verify_selectors.py — он скажет, что именно отвалилось.

Все действия ВЕРИФИЦИРУЮТСЯ чтением состояния после клика: функция возвращает
False, если состояние не изменилось. Молчаливых успехов тут нет.
"""
import re
import sys
import time
import logging
import unicodedata
from datetime import datetime

logger = logging.getLogger("yt_studio")

# ---------------------------------------------------------------------------
# Селекторы. Единственное место, где они живут. Дата = когда подтверждено дампом.
# ---------------------------------------------------------------------------
# Переключатель Google-аккаунтов профиля.                   [дамп 2026-08-07]
# Снято живьём (research/dom/account_switcher_2026-08-07.json): в меню аватара пункт
# «Switch account» опознаётся НЕ текстом, а машинно — data.icon.iconType == SWITCH_ACCOUNTS;
# сам список аккаунтов — ytd-account-item-renderer, и в его .data лежит
# serviceEndpoint.selectActiveIdentityEndpoint с authuser=N. Отсюда номер аккаунта
# ЧИТАЕТСЯ, а не подбирается перебором 0..3 (перебор ломался: у профиля бывает 6 аккаунтов).
SEL_ACCOUNTS = {
    "avatar_btn":       "button#avatar-btn",
    "menu_item":        "ytd-compact-link-renderer",
    "switch_icon":      "SWITCH_ACCOUNTS",          # data.icon.iconType
    "account_item":     "ytd-account-item-renderer",
}

# Страница ролика: меню «Options» и удаление.               [дамп 2026-08-07]
# Снято живьём на /video/{id}/edit: пункты меню несут АТРИБУТ test-id (VIDEO_DELETE,
# VIDEO_PROMOTE, …) — машинный признак, не зависящий от языка интерфейса. В диалоге
# удаления кнопка #confirm-button держит aria-disabled=true, пока не отмечен
# #confirm-checkbox (проверено: true → false после клика по чекбоксу).
SEL_DELETE = {
    "options_menu":     "ytcp-icon-button#overflow-menu-button",
    "menu_delete":      'tp-yt-paper-item[test-id="VIDEO_DELETE"]',
    "confirm_checkbox": "ytcp-checkbox-lit#confirm-checkbox",
    "confirm_btn":      "ytcp-button#confirm-button",
    "cancel_btn":       "ytcp-button#cancel-button",
}

SEL = {
    # список видео канала                                    [дамп 2026-08-03]
    "video_row":        "ytcp-video-row",
    # страница видео → кнопка открытия мастера черновика      [дамп 2026-08-03]
    "edit_draft_btn":   "ytcp-button#action-1",
    # мастер загрузки                                         [дамп 2026-08-03]
    "wizard":           "ytcp-uploads-dialog",
    # файловый инпут мастера                                 [дамп 2026-08-07]
    # путь из дампа: input < div < ytcp-uploads-file-picker < div < tp-yt-paper-dialog
    # < ytcp-uploads-dialog, и он в light DOM — достаётся обычным querySelector.
    # Важно брать именно ЭТОТ инпут: на странице канала есть и другие input[type=file]
    # (обложка, баннер), и файл уходил не туда.
    "file_input":       'ytcp-uploads-dialog input[type="file"]',
    "step_badge":       "[id^=step-badge-]",
    "next_button":      "ytcp-button#next-button",
    "done_button":      "ytcp-button#done-button",
    "close_button":     "ytcp-button#close-button",
    # шаг Details                                             [дамп 2026-08-03]
    "textbox":          "#textbox[aria-label]",
    "kids_no":          'tp-yt-paper-radio-button[name="VIDEO_MADE_FOR_KIDS_NOT_MFK"]',
    "altered_yes":      'tp-yt-paper-radio-button[name="VIDEO_HAS_ALTERED_CONTENT_YES"]',
    # шаг Visibility (внутреннее имя шага = REVIEW)           [дамп 2026-08-03]
    "visibility_host":  "ytcp-video-visibility-select",
    "privacy_radios":   "tp-yt-paper-radio-group#privacy-radios",
    "radio_public":     'tp-yt-paper-radio-button[name="PUBLIC"]',
    "radio_private":    'tp-yt-paper-radio-button[name="PRIVATE"]',
    "radio_unlisted":   'tp-yt-paper-radio-button[name="UNLISTED"]',
    # отложка. Радио name=SCHEDULE НЕ СУЩЕСТВУЕТ — это отдельная
    # раскрывающаяся секция + datetime-picker.                [дамп 2026-08-03]
    "schedule_expand":  "#second-container-expand-button",
    "datetime_picker":  "ytcp-datetime-picker",
    "date_trigger":     "#datepicker-trigger",
    "time_input":       "ytcp-datetime-picker #time-of-day-container input",
    # диалог календаря живёт на уровне <body>, НЕ внутри пикера [дамп 2026-08-03]
    "date_dialog":      "ytcp-date-picker tp-yt-paper-dialog#dialog",
    "date_input":       "ytcp-date-picker tp-yt-paper-dialog#dialog input",
}

# Машинные константы Studio (не зависят от языка интерфейса)
DRAFT_NONE = "DRAFT_STATUS_NONE"
VIS_DRAFT = "VIDEO_VISIBILITY_STATUS_DRAFT"
VIS_PROCESSING = "VIDEO_VISIBILITY_STATUS_UPLOADING_OR_PROCESSING"
STEP_REVIEW = "REVIEW"          # последний шаг мастера («Visibility» в UI)

# ---------------------------------------------------------------------------
# JS-фундамент: обход shadow DOM. Половина Studio живёт в shadowRoot и обычным
# document.querySelector не достаётся — это причина «селектор есть, но не найден».
# ---------------------------------------------------------------------------
_JS_BASE = r"""
function deepAll(root){
  var out=[], els=root.querySelectorAll('*');
  for(var i=0;i<els.length;i++){
    out.push(els[i]);
    if(els[i].shadowRoot) out=out.concat(deepAll(els[i].shadowRoot));
  }
  return out;
}
function deepFind(sel, visibleOnly){
  var all=deepAll(document);
  for(var i=0;i<all.length;i++){
    try{
      if(!all[i].matches || !all[i].matches(sel)) continue;
      if(visibleOnly){
        var r=all[i].getBoundingClientRect();
        if(!(r.width>0 && r.height>0)) continue;
      }
      return all[i];
    }catch(e){}
  }
  return null;
}
function realClick(el){
  // Polymer часто игнорирует el.click() (overlay перехватывает) — шлём настоящую
  // последовательность событий мыши по центру элемента.
  if(!el) return false;
  try{ el.scrollIntoView({block:'center'}); }catch(e){}
  var r=el.getBoundingClientRect();
  var o={bubbles:true,cancelable:true,view:window,
         clientX:r.left+r.width/2, clientY:r.top+r.height/2, button:0};
  el.dispatchEvent(new MouseEvent('mousedown',o));
  el.dispatchEvent(new MouseEvent('mouseup',o));
  el.dispatchEvent(new MouseEvent('click',o));
  return true;
}
function setNativeValue(input, value){
  // React/Polymer слушают события, а не присваивание .value — ставим через
  // нативный сеттер и шлём input+change, иначе изменение молча теряется.
  var proto=Object.getPrototypeOf(input);
  var desc=Object.getOwnPropertyDescriptor(proto,'value');
  if(desc && desc.set) desc.set.call(input, value); else input.value=value;
  input.dispatchEvent(new Event('input',{bubbles:true}));
  input.dispatchEvent(new Event('change',{bubbles:true}));
}
"""


def _js(driver, body, *args):
    return driver.execute_script(_JS_BASE + body, *args)


def _norm(s: str) -> str:
    """NFC + lower + схлопнутые пробелы.

    Studio отдаёт диакритику в NFD ('journée' = e + U+0301), наши строки в NFC →
    прямое сравнение даёт промах на всех языках с диакритикой. Эта грабля уже
    ломала dedup (см. CLAUDE.md, tools/finalize_ro_draft.py).
    """
    s = unicodedata.normalize("NFC", s or "")
    return re.sub(r"\s+", " ", s).strip().lower()


# ---------------------------------------------------------------------------
# Чтение состояния
# ---------------------------------------------------------------------------
_READ_VIDEOS_JS = r"""
var rows = deepAll(document).filter(function(e){
  return (e.tagName||'').toLowerCase()==='ytcp-video-row';
});
var out=[];
for(var i=0;i<rows.length;i++){
  var v=null;
  try{ v=rows[i].video; }catch(e){}
  if(!v) continue;
  var vis=v.visibility||{};
  // запланированная публикация: privacy остаётся PRIVATE, а timePublishedSeconds = 0,
  // поэтому время берём из scheduledPublishingDetails (снято дампом 2026-08-03)
  var schedTime='0';
  try{
    var sp=(v.scheduledPublishingDetails||{}).scheduledPublishings||[];
    if(sp.length && sp[0].scheduledTimeSeconds) schedTime=String(sp[0].scheduledTimeSeconds);
  }catch(e){}
  out.push({
    videoId: v.videoId||'',
    title: v.title||'',
    draftStatus: v.draftStatus||'',
    privacy: v.privacy||'',
    status: v.status||'',
    effectiveStatus: vis.effectiveStatus||'',
    userSetVisibility: vis.userSetVisibility||'',
    scheduledTime: schedTime,
    timePublished: v.timePublishedSeconds||'0',
    lengthSeconds: v.lengthSeconds||'',
    // коллабораторы: поле allCollaborators появляется ТОЛЬКО после реально созданного
    // приглашения. serializedShareEntity есть всегда (это id владельца) — по нему судить нельзя.
    collaborators: (function(){
      try{
        var c=(v.collaboration||{}).allCollaborators||[];
        return c.map(function(x){ return x.externalChannelId||''; }).filter(Boolean);
      }catch(e){ return []; }
    })(),
    prechecksDone: !!(v.videoPrechecks && v.videoPrechecks.copyrightPrechecksDone
                      && v.videoPrechecks.brandSafetyPrechecksDone),
    alteredDisclosed: !!(v.alteredContentSettings &&
        v.alteredContentSettings.creatorDisclosedHasAlteredContent==='VIDEO_HAS_ALTERED_CONTENT_YES')
  });
}
return out;
"""


_ACCOUNTS_JS = r"""
// Список Google-аккаунтов профиля из МАШИННЫХ данных меню, а не из текста.
// [дамп 2026-08-07, research/dom/account_switcher_2026-08-07.json]
var out = {menuOpen:false, switchFound:false, accounts:[]};
var items = deepAll(document).filter(function(e){
  return (e.tagName||'').toLowerCase() === 'ytd-account-item-renderer';
});
out.menuOpen = !!deepFind('ytd-multi-page-menu-renderer', false);
for(var i=0;i<items.length;i++){
  var d = items[i].data || {};
  var s = '';
  try { s = JSON.stringify(d.serviceEndpoint||{}); } catch(e) { s = ''; }
  var au = s.match(/authuser=(\d)/);
  var name = '';
  try { name = (d.accountName && (d.accountName.simpleText ||
        (d.accountName.runs && d.accountName.runs[0] && d.accountName.runs[0].text))) || ''; } catch(e) {}
  out.accounts.push({
    name: name,
    handle: (d.channelHandle && d.channelHandle.simpleText) || '',
    hasChannel: !!d.hasChannel,
    selected: !!d.isSelected,
    authuser: au ? au[1] : '',
    pageId: (s.match(/"pageId":"([^"]+)"/)||['',''])[1]
  });
}
return out;
"""


def read_accounts(driver):
    """Аккаунты профиля с их номерами authuser — из данных меню, не из текста.

    Возвращает {'menuOpen': bool, 'accounts': [{name, handle, authuser, selected, ...}]}.
    Пустой список — это «меню не открыто», а не «аккаунтов нет».
    """
    try:
        return _js(driver, _ACCOUNTS_JS) or {"accounts": []}
    except Exception as e:
        logger.warning("read_accounts: %s", e)
        return {"accounts": [], "error": str(e)}


def open_account_menu(driver, wait=8):
    """Открыть меню аватара и раскрыть список аккаунтов.

    Пункт «Switch account» ищется по data.icon.iconType == SWITCH_ACCOUNTS — это
    машинный признак, одинаковый на любом языке интерфейса (раньше пункт искали по
    надписи, и на локализованном Studio переключатель просто не находился).
    """
    _js(driver, """
        var av = document.querySelector(arguments[0]);
        if(av) realClick(av);
        return !!av;
    """, SEL_ACCOUNTS["avatar_btn"])
    time.sleep(2)
    # ИСПРАВЛЕНО 19.09.2026 (правка production от 14.09, срез live_2026-09-18):
    # `arguments` внутри вложенной функции — это аргументы САМОЙ вложенной функции, а не
    # скрипта. Сравнение `e.tagName === arguments[0]` брало первый аргумент колбэка
    # (элемент), поэтому пункт «Switch account» не находился никогда: переключение
    # канала и возврат вышедшего аккаунта молча не работали. Параметры скрипта теперь
    # сохраняются в переменные сразу — это правило гайда «обязан сохранить».
    _js(driver, """
        var menuTag = arguments[0], switchIcon = arguments[1];
        var sw = deepAll(document).filter(function(e){
          try {
            return (e.tagName||'').toLowerCase() === menuTag &&
                   e.data && e.data.icon && e.data.icon.iconType === switchIcon;
          } catch(err) { return false; }
        })[0];
        if(sw) realClick(sw);
        return !!sw;
    """, SEL_ACCOUNTS["menu_item"], SEL_ACCOUNTS["switch_icon"])
    end = time.time() + wait
    while time.time() < end:
        st = read_accounts(driver)
        if st.get("accounts"):
            return st
        time.sleep(1)
    return read_accounts(driver)


def authuser_for_channel(driver, channel_name: str):
    """Номер authuser нужного канала — прочитанный, а не подобранный.

    Возвращает строку с номером или None, если канала нет среди аккаунтов профиля
    (это честный ответ «профиль каналом не владеет», а не повод перебирать 0..3).
    """
    st = open_account_menu(driver)
    acc = st.get("accounts") or []
    if not acc:
        logger.warning("authuser_for_channel: список аккаунтов не прочитан")
        return None
    target = _norm(channel_name)
    for a in acc:
        if _norm(a.get("name", "")) == target:
            return a.get("authuser") or None
    for a in acc:
        if _ascii_key(a.get("name", "")) == _ascii_key(channel_name):
            return a.get("authuser") or None
    logger.info("authuser_for_channel: %r нет среди %d аккаунтов профиля (%s)",
                channel_name, len(acc), ", ".join((a.get("name") or "?")[:18] for a in acc))
    return None


def read_videos(driver):
    """Список видео канала со ЗНАЧЕНИЯМИ ИЗ КОМПОНЕНТА, а не из текста строки.

    Раньше статус определялся regex'ом по row.textContent, куда попадает скрытый
    дропдаун видимости со словами Public/Private/Unlisted/Scheduled — они там
    ЕСТЬ ВСЕГДА. Из-за этого черновик не распознавался и ролик заливался заново.
    """
    try:
        return _js(driver, _READ_VIDEOS_JS) or []
    except Exception as e:
        logger.warning("read_videos: %s", e)
        return []


def classify(v: dict) -> str:
    """DRAFT | PROCESSING | SCHEDULED | PUBLIC | UNLISTED | PRIVATE | UNKNOWN."""
    if not v:
        return "UNKNOWN"
    if v.get("effectiveStatus") == VIS_DRAFT or v.get("draftStatus", "") not in (DRAFT_NONE, ""):
        return "DRAFT"
    # Запланированное видео Studio помечает ИМЕННО так: privacy остаётся
    # VIDEO_PRIVACY_PRIVATE, timePublishedSeconds = 0, а признак — userSetVisibility
    # (+ scheduledPublishingDetails). Проверять privacy/timePublished бесполезно.
    if v.get("userSetVisibility") == "VIDEO_USER_SET_VISIBILITY_SCHEDULED":
        return "SCHEDULED"
    try:
        st = int(v.get("scheduledTime") or 0)
    except (TypeError, ValueError):
        st = 0
    if st > time.time():
        return "SCHEDULED"
    priv = v.get("privacy", "")
    if v.get("effectiveStatus") == VIS_PROCESSING:
        return "PROCESSING"
    if priv == "VIDEO_PRIVACY_PUBLIC":
        return "PUBLIC"
    if priv == "VIDEO_PRIVACY_UNLISTED":
        return "UNLISTED"
    if priv == "VIDEO_PRIVACY_PRIVATE":
        return "PRIVATE"
    return "UNKNOWN"


def set_video_meta(driver, video_id, title=None, description=None, wait=12):
    """Поправить название/описание УЖЕ залитого ролика на странице /edit.

    Нужно, когда мастер заливки не смог заполнить поля («Название НЕ применилось,
    readback='no visible box'») и ролик остался с сервисным именем файла. Перезаливать
    в такой ситуации нельзя — на канале уже лежит копия, повтор даст дубль.

    Ввод — тем же единственным работающим способом, что и таймкоды рекламы: настоящий
    клик мышью через CDP, выделение всего и `Input.insertText`; Polymer-полям
    JS-присваивание не помогает. Возвращает (ok, описание).
    """
    driver.get(f"https://studio.youtube.com/video/{video_id}/edit")
    time.sleep(wait)

    _field = _JS_BASE + """
    var re = new RegExp(arguments[0], 'i');
    var e = deepAll(document).filter(function(x){
      if((x.tagName||'').toLowerCase() !== 'div') return false;
      if((x.id||'') !== 'textbox') return false;
      var al = (x.getAttribute && x.getAttribute('aria-label')) || '';
      if(!re.test(al)) return false;
      var r = x.getBoundingClientRect();
      return r.width > 20 && r.height > 10;
    })[0];
    if(!e) return null;
    var r = e.getBoundingClientRect();
    return {x: r.x + r.width/2, y: r.y + r.height/2, val: (e.innerText||'').slice(0, 80)};
    """

    def _type(where, text, what):
        pos = driver.execute_script(_field, where)
        if not pos:
            return False, f"поле «{what}» не найдено"
        driver.execute_cdp_cmd("Input.dispatchMouseEvent",
                               {"type": "mouseMoved", "x": pos["x"], "y": pos["y"]})
        time.sleep(0.3)
        for ev in ("mousePressed", "mouseReleased"):
            driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
                "type": ev, "x": pos["x"], "y": pos["y"], "button": "left", "clickCount": 1})
        time.sleep(0.6)
        driver.execute_script("document.execCommand('selectAll', false, null);")
        time.sleep(0.3)
        driver.execute_cdp_cmd("Input.insertText", {"text": text})
        time.sleep(1.5)
        got = driver.execute_script(_field, where) or {}
        head = text.strip()[:25]
        return (head[:20] in (got.get("val") or "")), f"{what}: «{(got.get('val') or '')[:40]}»"

    notes = []
    if title:
        ok, why = _type("Add a title|название", title, "название")
        notes.append(why)
        if not ok:
            return False, "; ".join(notes)
    if description:
        ok, why = _type("Tell viewers|описание", description, "описание")
        notes.append(why)
        if not ok:
            return False, "; ".join(notes)

    # Save появляется только после правки — искать его заранее бесполезно
    save = driver.execute_script(_JS_BASE + """
    var b = deepAll(document).filter(function(e){
      if((e.tagName||'').toLowerCase() !== 'button') return false;
      var r = e.getBoundingClientRect();
      if(r.width < 20 || e.offsetParent === null) return false;
      var tx = ((e.innerText||'') + ' ' +
                ((e.getAttribute && e.getAttribute('aria-label'))||'')).trim();
      return /^save|сохранить/i.test(tx) &&
             !(e.disabled || e.getAttribute('aria-disabled') === 'true');
    })[0];
    if(!b) return null;
    var r = b.getBoundingClientRect();
    return {x: r.x + r.width/2, y: r.y + r.height/2};
    """)
    if not save:
        return False, "; ".join(notes) + " · кнопка Save не активна (правки не приняты)"
    driver.execute_cdp_cmd("Input.dispatchMouseEvent",
                           {"type": "mouseMoved", "x": save["x"], "y": save["y"]})
    time.sleep(0.3)
    for ev in ("mousePressed", "mouseReleased"):
        driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
            "type": ev, "x": save["x"], "y": save["y"], "button": "left", "clickCount": 1})
    time.sleep(6)
    return True, "; ".join(notes) + " · сохранено"


def delete_video(driver, video_id, confirm=True):
    """Удалить ролик навсегда. Возвращает (ok, описание).

    Путь по дампу 2026-08-07 (research/dom): страница ролика → `#overflow-menu-button`
    → пункт с АТРИБУТОМ `test-id="VIDEO_DELETE"` → чекбокс `#confirm-checkbox` →
    `#confirm-button` (до галки он `aria-disabled=true` — это и есть проверка, что
    согласие принято). Ни одного слова из интерфейса: прежний путь искал «Options» по
    aria-label и «Delete forever» по тексту, и на локализованном Studio не работал,
    а меню строки списка в текущем DOM вообще не рендерится (проверено живьём).
    """
    try:
        driver.get(f"https://studio.youtube.com/video/{video_id}/edit")
        wait_until(driver, lambda d: _js(d, "return !!deepFind(arguments[0], true);",
                                        SEL_DELETE["options_menu"]),
                   timeout=40, poll=2, desc="меню Options на странице ролика")
        click(driver, SEL_DELETE["options_menu"])
        time.sleep(2)
        if not click(driver, SEL_DELETE["menu_delete"]):
            return False, "пункт удаления (test-id=VIDEO_DELETE) не найден"
        time.sleep(3)
        if confirm:
            click(driver, SEL_DELETE["confirm_checkbox"])
            ok = wait_until(
                driver,
                lambda d: _js(d, """
                    var b = deepFind(arguments[0], true);
                    return b ? b.getAttribute('aria-disabled') !== 'true' : false;
                """, SEL_DELETE["confirm_btn"]),
                timeout=15, poll=1, desc="кнопка удаления стала активной")
            if not ok:
                return False, "согласие не принято: #confirm-button остался aria-disabled"
            click(driver, SEL_DELETE["confirm_btn"])
        time.sleep(5)
        driver.get(driver.current_url.split("/video/")[0] + "/videos/upload"
                   if "/video/" in driver.current_url else driver.current_url)
        time.sleep(4)
        rows = read_videos(driver) or []
        gone = not any(v.get("videoId") == video_id for v in rows)
        if rows and gone:
            return True, "удалён"
        if not rows:
            return True, "удалён (список канала не перечитан — проверить отдельно)"
        return False, "после подтверждения строка ещё на месте"
    except Exception as e:
        logger.warning("delete_video (путь по дампу) не сработал: %s — пробую старый путь", e)

    return _delete_video_legacy(driver, video_id, confirm)


def _delete_video_legacy(driver, video_id, confirm=True):
    """Старый путь удаления через меню строки списка (наведение мышью + текст).

    Оставлен ЯВНЫМ фолбэком: в текущем DOM меню у строки не рендерится, но на других
    версиях Studio путь может ещё работать. Срабатывание пишется в лог.

    Способ снят дампом 05.08.2026. Прежний селектор «Delete forever» не срабатывал,
    потому что путь другой:
      1. кнопка меню появляется у строки ТОЛЬКО при наведении мыши (hover);
      2. на странице `/video/{id}/edit` кнопка Options отдаёт размеры 0×0 в фоновом
         окне Chrome — работать надо со списка `/videos/upload`;
      3. клики — настоящей мышью через CDP по координатам, JS-клик Polymer глотает.
    Пункт «Delete forever» — последний в меню, после него диалог с чекбоксом и Delete.
    """
    _row = _JS_BASE + """
    var want = arguments[0];
    var rows = deepAll(document).filter(function(e){return (e.tagName||'').toLowerCase()==='ytcp-video-row';});
    for(var i=0;i<rows.length;i++){
      var v=null; try{ v=rows[i].video; }catch(e){}
      if(v && v.videoId===want){ var r=rows[i].getBoundingClientRect();
        return {hx: Math.round(r.x + r.width - 80), hy: Math.round(r.y + r.height/2)}; }
    }
    return null;
    """
    _btn = _JS_BASE + """
    var want = arguments[0];
    var rows = deepAll(document).filter(function(e){return (e.tagName||'').toLowerCase()==='ytcp-video-row';});
    for(var i=0;i<rows.length;i++){
      var v=null; try{ v=rows[i].video; }catch(e){}
      if(v && v.videoId===want){
        var b = deepAll(rows[i]).filter(function(e){
          return /ytcp-icon-button|button/.test((e.tagName||'').toLowerCase()) &&
                 /option|more/i.test((e.getAttribute&&e.getAttribute('aria-label'))||'');})[0];
        if(!b) return null;
        var r=b.getBoundingClientRect();
        return {x: r.x + r.width/2, y: r.y + r.height/2, w: r.width};
      }
    }
    return null;
    """
    _item = _JS_BASE + """
    var re = new RegExp(arguments[0], 'i');
    var it = deepAll(document).filter(function(e){
      return /paper-item|menu-item/.test((e.tagName||'').toLowerCase()) &&
             e.offsetParent !== null && re.test((e.innerText||'').trim());})[0];
    if(!it) return null;
    var r = it.getBoundingClientRect();
    return {x: r.x + r.width/2, y: r.y + r.height/2};
    """

    def _click_xy(pos):
        # mouseMoved обязателен: без наведения ровно на кнопку Studio не считает клик
        # (проверено — «Delete video» молча не открывает диалог, если курсор пришёл
        # из другой точки), настоящая мышь всегда сначала доводится до цели.
        driver.execute_cdp_cmd("Input.dispatchMouseEvent",
                               {"type": "mouseMoved", "x": pos["x"], "y": pos["y"]})
        time.sleep(0.4)
        for ev in ("mousePressed", "mouseReleased"):
            driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
                "type": ev, "x": pos["x"], "y": pos["y"], "button": "left", "clickCount": 1})
        time.sleep(1.5)

    row = driver.execute_script(_row, video_id)
    if not row:
        return False, "строка ролика не найдена (не тот канал/authuser?)"
    driver.execute_cdp_cmd("Input.dispatchMouseEvent",
                           {"type": "mouseMoved", "x": row["hx"], "y": row["hy"]})
    time.sleep(1.2)

    # У прерванной загрузки строка выглядит иначе: меню Options нет вообще, зато есть
    # прямая кнопка «Delete video» (`delete-draft-button`). Дамп 05.08.2026.
    direct = driver.execute_script(_JS_BASE + """
    var want = arguments[0];
    var rows = deepAll(document).filter(function(e){return (e.tagName||'').toLowerCase()==='ytcp-video-row';});
    for(var i=0;i<rows.length;i++){
      var v=null; try{ v=rows[i].video; }catch(e){}
      if(v && v.videoId===want){
        var b = deepAll(rows[i]).filter(function(e){
          var c = ((e.className&&e.className.baseVal!==undefined?e.className.baseVal:e.className)||'').toString();
          return /delete-draft-button/.test(c) && e.getBoundingClientRect().width > 0;})[0];
        if(!b) return null;
        // Строка шире окна: у части профилей кнопка стоит на x≈1307 при ширине 1199,
        // и клик по координате уходит мимо экрана. Подтягиваем её в видимую область
        // и только потом снимаем координаты.
        b.scrollIntoView({block: 'center', inline: 'center'});
        var r = b.getBoundingClientRect();
        if(r.x + r.width/2 > window.innerWidth || r.x < 0) return {off: true};
        return {x: r.x + r.width/2, y: r.y + r.height/2};
      }
    }
    return null;
    """, video_id)

    if direct and direct.get("off"):
        return False, "кнопка удаления вне видимой области даже после прокрутки"
    if direct:
        time.sleep(0.8)                     # дать доскроллиться
        _click_xy(direct)
    else:
        btn = driver.execute_script(_btn, video_id)
        if not btn or not btn.get("w"):
            return False, "ни меню строки, ни прямой кнопки удаления"
        _click_xy(btn)
        item = driver.execute_script(_item, "delete forever|удалить навсегда")
        if not item:
            return False, "пункт «Delete forever» не найден"
        _click_xy(item)
    if not confirm:
        return True, "диалог удаления открыт (без подтверждения)"

    # Диалог «Permanently delete this draft video?». Искать элементы по всей странице
    # нельзя: первый видимый чекбокс — это чекбокс ВЫДЕЛЕНИЯ СТРОКИ в таблице, а текст
    # «Delete video» носят и кнопки в строках. Поэтому сначала находим сам диалог —
    # по характерной фразе — и ищем строго внутри него.
    _dlg = _JS_BASE + """
    var kind = arguments[0];
    var vis = function(e){ var r=e.getBoundingClientRect();
                           return r.width>0 && r.height>0 && e.offsetParent!==null; };
    // самый глубокий узел, содержащий фразу подтверждения — это тело диалога
    var host = null;
    deepAll(document).forEach(function(e){
      var t = (e.innerText||'');
      if(!/cannot be undone|permanently delete|навсегда/i.test(t)) return;
      if(t.length > 600) return;                       // отсекаем body и обёртки
      if(!host || t.length < (host.innerText||'').length) host = e;
    });
    if(!host) return null;
    var scope = host;
    for(var i=0; i<6 && scope.parentElement; i++) scope = scope.parentElement;  // до控
    var inside = deepAll(scope);
    if(kind === 'check'){
      // Согласие ставится кликом по ПОДПИСИ, а не по чекбоксу: сам `ytcp-checkbox-lit`
      // клик мышью игнорирует, и его свойство `checked` остаётся false даже когда
      // согласие принято. Признак успеха — кнопка удаления перестала быть disabled.
      var lab = inside.filter(function(e){
        return (e.tagName||'').toLowerCase() === 'label' && vis(e) &&
               /I understand|permanent|навсегда|понимаю/i.test(e.innerText||''); })[0];
      if(!lab) return null;
      var lr = lab.getBoundingClientRect();
      return {x: lr.x + lr.width/2, y: lr.y + lr.height/2};
    }
    var re = new RegExp(arguments[1], 'i');
    var b = inside.filter(function(e){
      var t = (e.tagName||'').toLowerCase();
      if(t !== 'button') return false;
      if(!vis(e)) return false;
      return re.test(((e.innerText||'') + ' ' +
                      ((e.getAttribute && e.getAttribute('aria-label'))||'')).trim());
    })[0];
    if(!b) return null;
    var rr = b.getBoundingClientRect();
    return {x: rr.x + rr.width/2, y: rr.y + rr.height/2,
            dis: !!b.disabled || b.getAttribute('aria-disabled')==='true'};
    """
    time.sleep(2)
    ack = driver.execute_script(_dlg, "check", "")
    if ack is None:
        return False, "диалог подтверждения не открылся"
    _click_xy(ack)
    _q = "delete draft video|delete forever|удалить навсегда"
    ok_btn = driver.execute_script(_dlg, "button", _q)
    if not ok_btn:
        return False, "кнопка подтверждения в диалоге не найдена"
    if ok_btn.get("dis"):
        _click_xy(ack)                       # согласие не приняли с первого раза
        ok_btn = driver.execute_script(_dlg, "button", _q)
    if not ok_btn or ok_btn.get("dis"):
        return False, "кнопка подтверждения осталась неактивной"
    _click_xy(ok_btn)
    time.sleep(5)
    driver.refresh()
    time.sleep(6)
    gone = not driver.execute_script(_row, video_id)
    return gone, ("удалён" if gone else "после подтверждения строка ещё на месте")


def _ascii_key(s: str) -> str:
    """Заголовок без диакритики и пунктуации — устойчивая игла.

    Studio отдаёт диакритику в NFD, наши строки в NFC; вдобавок в заголовке живут
    типографские кавычки и тире, которые Studio иногда подменяет. Поэтому кроме
    NFC-сравнения держим запасной ключ из голых букв и цифр.
    """
    s = unicodedata.normalize("NFD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def find_video(driver, title: str, videos=None):
    """Найти видео по заголовку. Сначала строго, потом по ASCII-игле.

    Возвращает (запись, статус) или (None, None). Обратное вхождение (`t in needle`)
    убрано намеренно: короткий чужой заголовок так матчился с нашим и выдавал чужой
    ролик за наш.
    """
    needle = _norm(title)
    if not needle:
        return None, None
    rows = videos if videos is not None else read_videos(driver)
    # Точное совпадение проверяем ОТДЕЛЬНЫМ проходом и раньше префиксного: в серии
    # заголовки различаются хвостом («…, część 2»), и при поиске в одном проходе
    # первой матчилась соседняя серия — возвращался чужой videoId.
    for v in rows:
        if _norm(v.get("title", "")) == needle:
            return v, classify(v)
    for v in rows:
        t = _norm(v.get("title", ""))
        if t and (t.startswith(needle[:60]) or needle.startswith(t[:60])):
            return v, classify(v)
    a_needle = _ascii_key(title)
    if len(a_needle) >= 12:
        for v in rows:
            a = _ascii_key(v.get("title", ""))
            if a and (a[:60] == a_needle[:60] or a.startswith(a_needle[:40])):
                logger.info("find_video: совпало по ASCII-игле (диакритика/кавычки разошлись)")
                return v, classify(v)
    return None, None


def wait_videos(driver, timeout=90, poll=3.0):
    """Дождаться, пока список видео канала действительно прочитается.

    Пустой ответ read_videos — это НЕ «на канале нет роликов», а «строки ещё не
    отрисованы». Раньше пустой список принимался за факт, и заливка либо считала
    ролик потерянным, либо (в дедупе) лила второй раз.
    """
    end = time.time() + timeout
    while True:
        rows = read_videos(driver) or []
        if rows:
            return rows
        if time.time() >= end:
            logger.warning("wait_videos: список видео канала пуст за %dс — считаю НЕ прочитанным", timeout)
            return []
        time.sleep(poll)


_READ_WIZARD_JS = r"""
var d = deepFind('ytcp-uploads-dialog', false);
if(!d) return {present:false};
function g(k){ try{ var v=d[k]; return (v===undefined)?null:v; }catch(e){ return null; } }
var host = deepFind('ytcp-video-visibility-select', false);
var avail = null;
try{ if(host) avail = host.availabilityRadioSelection; }catch(e){}
var rect = d.getBoundingClientRect();
// видимые радио видимости — признак, что шаг реально отрисован
var radios = deepAll(document).filter(function(e){
  var n=(e.getAttribute&&e.getAttribute('name'))||'';
  if(['PUBLIC','PRIVATE','UNLISTED'].indexOf(n)<0) return false;
  var r=e.getBoundingClientRect(); return r.width>0 && r.height>0;
});
var checked = radios.filter(function(e){ return e.getAttribute('aria-checked')==='true'; })
                    .map(function(e){ return e.getAttribute('name'); });
var done = deepFind('ytcp-button#done-button', false);
var next = deepFind('ytcp-button#next-button', false);
function btn(b){
  if(!b) return null;
  var r=b.getBoundingClientRect();
  return {text:(b.innerText||'').trim().slice(0,40),
          disabled: b.getAttribute('aria-disabled')==='true',
          visible: r.width>0 && r.height>0};
}
// videoId мастера [дамп 2026-08-07]: заполняется через ~8с после отправки файла,
// на шаге DETAILS, ЗАДОЛГО до конца загрузки. Это позволяет вести ролик по id с самого
// начала вместо поиска по заголовку — из-за которого 07.08 восемнадцать успешных
// заливок были объявлены провальными («видео по заголовку не найдено»).
var vid = g('videoId') || '';
if(!vid){
  try{
    var det = d.uploadDetailsByFrontendId || {};
    for(var k in det){ if(det[k] && det[k].videoId){ vid = det[k].videoId; break; } }
  }catch(e){}
}
return {
  present: true,
  open: d.offsetParent !== null || (rect.width>0 && rect.height>0),
  videoId: vid,
  workflowStep: g('workflowStep'),
  uploadMode: g('uploadMode'),
  isVideoUploaded: g('isVideoUploaded'),
  isCurrentStepInvalid: g('isCurrentStepInvalid'),
  isPrechecksLoading: g('isPrechecksLoading'),
  shouldShowMonetizationStep: g('shouldShowMonetizationStep'),
  errorSectionStatus: g('errorSectionStatus'),
  hasError: g('hasError'),
  errorShortMessage: g('errorShortMessage'),
  isErrorRetryable: g('isErrorRetryable'),
  saveDraftBadgeLabel: g('saveDraftBadgeLabel'),
  availability: avail,
  visibilityRadiosVisible: radios.length,
  visibilityChecked: checked,
  doneButton: btn(done),
  nextButton: btn(next)
};
"""


def read_wizard(driver):
    """Состояние мастера загрузки. Ключ ко всему: workflowStep, isVideoUploaded,
    isCurrentStepInvalid — вместо regex по «upload complete|загрузка завершена|…»."""
    try:
        return _js(driver, _READ_WIZARD_JS) or {"present": False}
    except Exception as e:
        logger.debug("read_wizard: %s", e)
        return {"present": False, "error": str(e)}


_UPLOAD_PROGRESS_JS = r"""
var d = deepFind('ytcp-uploads-dialog', false);
if(!d) return null;
var best = null;
try{
  var m = d.uploadDetailsByFrontendId || {};
  for(var k in m){
    var u = m[k];
    if(!u) continue;
    var pct = (u.progress!==undefined && u.progress!==null) ? u.progress
            : (u.uploadProgress!==undefined ? u.uploadProgress : null);
    var rec = {id:k, pct:pct, status:u.status||u.uploadStatus||null};
    if(!best || (pct!==null && best.pct!==null && pct>best.pct)) best=rec;
    if(!best) best=rec;
  }
}catch(e){ return {error:String(e)}; }
return best;
"""


def read_upload_progress(driver):
    """Прогресс заливки из uploadManager, а не из текста «57 %» на странице."""
    try:
        return _js(driver, _UPLOAD_PROGRESS_JS)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Ожидание условия — вместо time.sleep(6) «на всякий случай»
# ---------------------------------------------------------------------------
def wait_until(driver, check, timeout=60, poll=2.0, desc="условие", on_tick=None):
    """Ждать, пока check(driver) вернёт истину. Возвращает результат или None.

    check получает driver и возвращает любое значение; истинное = успех.
    on_tick(elapsed, last) вызывается раз в poll — для логов прогресса.
    """
    start = time.time()
    last = None
    while time.time() - start < timeout:
        try:
            last = check(driver)
            if last:
                return last
        except Exception as e:
            logger.debug("wait_until(%s): %s", desc, e)
        if on_tick:
            try:
                on_tick(time.time() - start, last)
            except Exception:
                pass
        time.sleep(poll)
    logger.warning("wait_until: не дождался «%s» за %dс (последнее: %r)",
                   desc, timeout, str(last)[:120])
    return None


def wait_wizard_ready(driver, timeout=90):
    """Дождаться, пока мастер РЕАЛЬНО прогрузился.

    Сразу после открытия он уже присутствует в DOM, но отдаёт workflowStep=DETAILS,
    errorSectionStatus=LOADING и isVideoUploaded=null — действовать в этот момент
    бесполезно: клики уходят в ещё не отрисованный шаг. Признак готовности —
    LOADING ушёл и появилось поле заголовка.
    """
    def _check(d):
        w = read_wizard(d)
        if not w.get("present"):
            return None
        if w.get("errorSectionStatus") == "LOADING":
            return None
        if w.get("isVideoUploaded") is None and not w.get("workflowStep"):
            return None
        return w

    return wait_until(driver, _check, timeout=timeout, poll=2.0, desc="готовность мастера")


def wait_wizard_step(driver, step, timeout=120):
    """Дождаться конкретного шага мастера (напр. REVIEW)."""
    return wait_until(
        driver,
        lambda d: read_wizard(d).get("workflowStep") == step,
        timeout=timeout, desc=f"шаг мастера {step}",
    )


def goto_step_and_wait(driver, step=STEP_REVIEW, timeout=90, attempts=3):
    """Перейти на шаг и ДОЖДАТЬСЯ, что мастер реально там.

    Один клик по бейджу не гарантирует переход: пока идут проверки YouTube,
    Polymer возвращает мастер обратно. Поэтому кликаем и проверяем состояние,
    а не «кликнули → значит перешли».
    """
    for i in range(1, attempts + 1):
        w = read_wizard(driver)
        if w.get("workflowStep") == step:
            return w
        goto_last_step(driver)
        got = wait_wizard_step(driver, step, timeout=timeout // attempts or 20)
        if got:
            return read_wizard(driver)
        logger.info("Переход на шаг %s не удался (попытка %d/%d)", step, i, attempts)
    return None


def wait_visibility_ready(driver, timeout=1800, log_every=30):
    """Шаг Visibility готов = мастер на REVIEW и видимы радио PUBLIC/PRIVATE/UNLISTED.

    Раньше гейтом был текст body («upload complete»), который матчился РАНЬШЕ,
    чем отрисовывались радио → клик уходил в пустоту и видео оставалось Draft.
    """
    state = {"logged": 0}

    def _tick(elapsed, last):
        if elapsed - state["logged"] >= log_every:
            state["logged"] = elapsed
            w = last if isinstance(last, dict) else read_wizard(driver)
            logger.info("...жду Visibility (%dс): шаг=%s uploaded=%s радио=%s",
                        int(elapsed), w.get("workflowStep"), w.get("isVideoUploaded"),
                        w.get("visibilityRadiosVisible"))

    def _check(d):
        w = read_wizard(d)
        if w.get("workflowStep") == STEP_REVIEW and (w.get("visibilityRadiosVisible") or 0) >= 2:
            return w
        return None

    return wait_until(driver, _check, timeout=timeout, poll=3.0,
                      desc="шаг Visibility", on_tick=_tick)


# ---------------------------------------------------------------------------
# Действия. Каждое верифицирует результат чтением состояния.
# ---------------------------------------------------------------------------
_CLICK_JS = r"""
var el = deepFind(arguments[0], arguments[1]);
if(!el) return 'not-found';
// overlay ytcp-uploads-review перехватывает клики — прячем на время клика
var ov = deepFind('ytcp-uploads-review', false), od = null;
if(ov){ od = ov.style.display; ov.style.display='none'; }
realClick(el);
if(ov) ov.style.display = od || '';
return 'clicked';
"""


def click(driver, selector, visible_only=True):
    """Клик настоящими MouseEvent'ами сквозь shadow DOM, с временным снятием overlay."""
    try:
        return _js(driver, _CLICK_JS, selector, visible_only) == "clicked"
    except Exception as e:
        logger.debug("click(%s): %s", selector, e)
        return False


_RADIO_STATE_JS = r"""
var el = deepFind(arguments[0], false);
if(!el) return null;
return el.getAttribute('aria-checked');
"""


def set_radio(driver, selector, desc="радио", attempts=3):
    """Кликнуть радио и ПОДТВЕРДИТЬ по aria-checked. False = не применилось."""
    for i in range(1, attempts + 1):
        if _js(driver, _RADIO_STATE_JS, selector) == "true":
            return True
        if not click(driver, selector):
            logger.debug("%s: элемент не найден (попытка %d)", desc, i)
            time.sleep(1)
            continue
        time.sleep(0.8)
        if _js(driver, _RADIO_STATE_JS, selector) == "true":
            logger.info("%s — отмечено (попытка %d)", desc, i)
            return True
        time.sleep(1)
    logger.warning("%s НЕ отметилось за %d попыток", desc, attempts)
    return False


def set_not_made_for_kids(driver):
    """Ответить «не для детей» и убедиться, что шаг стал валидным.

    [дамп 2026-08-07] Studio рисует ДВЕ видимые группы этого вопроса: на странице
    четыре радио — по паре MFK/NOT_MFK. Прежний set_radio отмечал только первое
    совпадение, вторая группа оставалась пустой, и шаг DETAILS навсегда оставался
    isCurrentStepInvalid=true с заблокированной кнопкой Next — мастер не доходил до
    Visibility. Проверено живьём: клик по второй группе снимает invalid.
    Поэтому жмём ВСЕ неотмеченные и проверяем результат по состоянию мастера.
    """
    clicked = _js(driver, """
        var rs = deepAll(document).filter(function(e){
          return (e.tagName||'').toLowerCase()==='tp-yt-paper-radio-button'
              && e.getAttribute('name')==='VIDEO_MADE_FOR_KIDS_NOT_MFK'
              && e.getBoundingClientRect().width>0;});
        var n=0;
        rs.forEach(function(e){
          if(e.getAttribute('aria-checked')==='true') return;
          var r=e.getBoundingClientRect();
          var o={bubbles:true,cancelable:true,view:window,
                 clientX:r.left+r.width/2, clientY:r.top+r.height/2};
          e.dispatchEvent(new MouseEvent('mousedown',o));
          e.dispatchEvent(new MouseEvent('mouseup',o));
          e.dispatchEvent(new MouseEvent('click',o));
          n++;
        });
        return {всего: rs.length, кликнул: n};
    """) or {}
    time.sleep(2)
    w = read_wizard(driver) or {}
    ok = not w.get("isCurrentStepInvalid")
    logger.info("«Не для детей»: групп %s, кликнул %s → шаг валиден: %s",
                clicked.get("всего"), clicked.get("кликнул"), ok)
    if not ok:
        logger.warning("Шаг DETAILS остался невалидным после ответа «не для детей» "
                       "(next=%s) — дальше мастер не пойдёт", w.get("nextButton"))
    return ok


def set_altered_content_yes(driver):
    """AI-дисклоуз. Секция бывает свёрнута под «Показать больше» (#toggle-button)."""
    if _js(driver, _RADIO_STATE_JS, SEL["altered_yes"]) is None:
        click(driver, "ytcp-button#toggle-button")
        time.sleep(1.0)
    return set_radio(driver, SEL["altered_yes"], "AI-дисклоуз (altered content)")


def set_visibility(driver, value="PUBLIC", attempts=3):
    """PUBLIC / PRIVATE / UNLISTED с проверкой по состоянию компонента."""
    key = {"PUBLIC": "radio_public", "PRIVATE": "radio_private",
           "UNLISTED": "radio_unlisted"}.get((value or "").upper())
    if not key:
        logger.error("set_visibility: неизвестное значение %r", value)
        return False
    ok = set_radio(driver, SEL[key], f"видимость {value}", attempts=attempts)
    if ok:
        w = read_wizard(driver)
        if value.upper() not in (w.get("visibilityChecked") or []):
            logger.warning("Видимость %s: aria-checked есть, но компонент показывает %s",
                           value, w.get("visibilityChecked"))
    return ok


# --- отложка ---------------------------------------------------------------
_SCHEDULE_EXPAND_JS = r"""
var pick = deepFind('ytcp-datetime-picker', true);
if(pick) return 'already-open';
var btn = deepFind('#second-container-expand-button', true);
if(!btn) return 'no-expand-button';
realClick(btn);
return 'expanded';
"""

_SET_TIME_JS = r"""
var inp = deepFind('ytcp-datetime-picker #time-of-day-container input', true);
if(!inp) return 'no-time-input';
inp.focus();
setNativeValue(inp, arguments[0]);
inp.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}));
inp.blur();
return String(inp.value||'');
"""

def _set_date_native(driver, date_str):
    """Ввести дату в диалог календаря НАСТОЯЩИМИ нажатиями клавиш.

    Календарь открывается отдельным диалогом на уровне <body>. Ввод через JS
    (setNativeValue + события) поле меняет, но модель пикера НЕ обновляет —
    триггер продолжает показывать старую дату. Нативные события клавиатуры
    проходят. Кликать по input нельзя: tp-yt-iron-overlay-backdrop перехватывает
    клик, поэтому фокус ставим через JS, а печатаем в активный элемент.

    Кликать по числу в календаре — плохой путь: ячейки это просто <span> с
    числом, без атрибута даты, а сетка охватывает несколько месяцев.
    """
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.common.action_chains import ActionChains
    try:
        inp = driver.find_element(By.CSS_SELECTOR, SEL["date_input"])
    except Exception:
        return "no-date-input"
    try:
        driver.execute_script("arguments[0].focus(); arguments[0].select();", inp)
        time.sleep(0.4)
        mod = Keys.COMMAND if sys.platform == "darwin" else Keys.CONTROL
        (ActionChains(driver)
            .key_down(mod).send_keys("a").key_up(mod)
            .send_keys(Keys.DELETE)
            .send_keys(date_str)
            .send_keys(Keys.ENTER)
            .perform())
        time.sleep(2)
        return str(inp.get_attribute("value") or "")
    except Exception as e:
        return f"err:{e}"

_READ_SCHEDULE_JS = r"""
var trig = deepFind('#datepicker-trigger', false);
var tinp = deepFind('ytcp-datetime-picker #time-of-day-container input', false);
var host = deepFind('ytcp-video-visibility-select', false);
var avail = null;
try{ if(host) avail = host.availabilityRadioSelection; }catch(e){}
return {
  date: trig ? (trig.innerText||'').trim() : null,
  time: tinp ? String(tinp.value||'') : null,
  availability: avail
};
"""

_DATE_FORMATS = ("%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%d %B %Y",
                 "%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%m/%d/%Y")


def _parse_shown_date(s: str):
    s = (s or "").strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _shown_time_matches(value: str, when: datetime) -> bool:
    """Проверить локализованный read-back времени против нужного часа.

    Studio использует и 12-часовые поля (``6:00 PM``), и 24-часовые
    (``18:00``). В 24-часовом поле строка ``6:00 PM`` раньше молча
    превращалась в ``06:00`` — валидное значение, но уже прошедшее время.
    """
    shown = unicodedata.normalize("NFKC", str(value or "")).upper()
    shown = re.sub(r"(?<=\d)\.(?=\d{2}(?:\D|$))", ":", shown)
    match = re.search(r"(?<!\d)(\d{1,2}):(\d{2})(?:\s*([AP])\.?\s*M\.?)?", shown)
    if not match:
        return False
    hour, minute = int(match.group(1)), int(match.group(2))
    meridiem = match.group(3)
    if meridiem:
        if not 1 <= hour <= 12:
            return False
        hour %= 12
        if meridiem == "P":
            hour += 12
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        return False
    return (hour, minute) == (when.hour, when.minute)


def set_schedule(driver, when: datetime, attempts=3):
    """Запланировать публикацию. Возвращает True только при ПОДТВЕРЖДЁННОЙ дате.

    Отложка в Studio — это НЕ радио name=SCHEDULE (такого элемента не существует),
    а раскрывающаяся секция Schedule с datetime-picker. Старый код кликал по
    заголовку секции и попадал случайно.
    """
    date_str = when.strftime("%b %-d, %Y") if hasattr(when, "strftime") else str(when)
    time_str = when.strftime("%-I:%M %p")
    time_str_24 = when.strftime("%H:%M")

    for att in range(1, attempts + 1):
        res = ""            # read-back поля ввода даты (см. верификацию ниже)
        exp = _js(driver, _SCHEDULE_EXPAND_JS)
        logger.info("Отложка: секция = %s (попытка %d)", exp, att)
        if exp == "no-expand-button":
            time.sleep(2)
            continue
        time.sleep(2)

        # ДАТА первой: смена даты сбрасывает поле времени на 12:00 AM,
        # поэтому время выставляем ПОСЛЕ неё.
        if click(driver, SEL["date_trigger"]):
            time.sleep(2)
            res = _set_date_native(driver, date_str)
            logger.info("Отложка: дата введена → %r (просили %s)", res, date_str)
            time.sleep(1.5)
            try:  # закрыть календарь, если остался открытым
                driver.execute_script(
                    "var d=document.querySelector('%s'); if(d && d.close) d.close();" % SEL["date_dialog"])
            except Exception:
                pass
            time.sleep(1)
        else:
            logger.warning("Отложка: триггер даты не найден (попытка %d)", att)

        # время (JS-путь тут работает: поле времени слушает input/change)
        res_t = _js(driver, _SET_TIME_JS, time_str)
        logger.info("Отложка: время → %r (просили %s)", res_t, time_str)
        if not _shown_time_matches(res_t, when):
            res_t_24 = _js(driver, _SET_TIME_JS, time_str_24)
            logger.info("Отложка: 24ч-фолбэк времени → %r (просили %s)",
                        res_t_24, time_str_24)
            res_t = res_t_24
        time.sleep(1.5)

        # ВЕРИФИКАЦИЯ по состоянию компонента + read-back даты
        st = _js(driver, _READ_SCHEDULE_JS) or {}
        shown = _parse_shown_date(st.get("date"))
        want = when.date() if hasattr(when, "date") else None
        if st.get("availability") != "SCHEDULE":
            logger.warning("Отложка: availability=%r (ждали SCHEDULE) — повтор",
                           st.get("availability"))
            time.sleep(2)
            continue
        if not _shown_time_matches(st.get("time") or res_t, when):
            logger.error("Отложка: read-back времени %r не равен %s — повтор",
                         st.get("time") or res_t, time_str_24)
            time.sleep(2)
            continue
        if shown and want and shown != want:
            logger.error("Отложка: пикер показывает %s, просили %s — НЕ планирую "
                         "(эфир в неверный день хуже, чем провал)", shown, want)
            return False
        if not shown:
            # availability=SCHEDULE доказывает только «отложка включена», но НЕ «на нужный
            # день», поэтому раньше здесь была молчаливая деградация: функция возвращала
            # True, ничего про дату не проверив. Локализованный триггер («7 sie 2026»,
            # «7. 8. 2026») ни один из _DATE_FORMATS не берёт, так что на не-английской
            # Studio это была ШТАТНАЯ ветка. Остаётся один честный read-back: значение,
            # которое поле ввода календаря (SEL["date_input"], дамп 2026-08-03) удержало
            # после нашего нативного ввода — это НАША строка, а не текст интерфейса.
            if _norm(res) != _norm(date_str):
                logger.error("Отложка: дату пикера %r не распарсил и read-back поля ввода "
                             "%r ≠ %r — считаю НЕ подтверждённой (нужен дамп свойств "
                             "ytcp-datetime-picker для машинного чтения даты)",
                             st.get("date"), res, date_str)
                time.sleep(2)
                continue
            logger.warning("Отложка: текст триггера %r не распарсен, дата подтверждена "
                           "read-back поля ввода (%r); итоговая сверка — по "
                           "scheduledPublishingDetails строки ролика", st.get("date"), res)
        logger.info("Отложка ПОДТВЕРЖДЕНА: %s %s (availability=SCHEDULE)",
                    st.get("date"), st.get("time"))
        return True

    logger.error("Отложка не применилась за %d попыток", attempts)
    return False


# --- шаг Monetization (только на YPP-каналах, uploadMode=NEW_UPLOAD) --------
_MONETIZATION_OPEN_JS = r"""
// Контрол монетизации — НЕ дропдаун «Select»: радио ON/OFF спрятаны за
// icon-button ВНУТРИ ytcp-video-monetization. Точно такая же кнопка
// («Edit video monetization status») есть на фоновой странице списка видео,
// поэтому ищем строго внутри компонента, иначе клик открывает меню Studio.
var host = deepFind('ytcp-video-monetization', true);
if(!host) return 'no-component';
if(deepFind('#radio-on', true)) return 'already-open';
var inner = deepAll(host.shadowRoot || host);
for(var i=0;i<inner.length;i++){
  var e = inner[i];
  if((e.tagName||'').toLowerCase() !== 'ytcp-icon-button') continue;
  var r = e.getBoundingClientRect();
  if(r.width>0 && r.height>0){ realClick(e); return 'opened'; }
}
return 'no-icon-button';
"""


def set_monetization(driver, on=True, attempts=3):
    """Шаг Monetization: ON/OFF + обязательное подтверждение «Done».

    Без `#save-button` шаг остаётся невалидным (`isCurrentStepInvalid=true`) и
    Next заблокирован — мастер не доходит до Visibility, ролик остаётся черновиком.
    """
    w = read_wizard(driver)
    if w.get("present") and w.get("shouldShowMonetizationStep") is False:
        logger.info("Монетизация: шаг не требуется на этом канале")
        return True

    for att in range(1, attempts + 1):
        opened = _js(driver, _MONETIZATION_OPEN_JS)
        if opened == "no-component":
            logger.info("Монетизация: компонента нет (шаг не показан)")
            return True
        logger.info("Монетизация: контрол = %s (попытка %d)", opened, att)
        time.sleep(2)

        target = "#radio-on" if on else "#radio-off"
        if not set_radio(driver, target, f"монетизация {'ON' if on else 'OFF'}"):
            time.sleep(1.5)
            continue

        if not click(driver, "ytcp-button#save-button"):
            logger.warning("Монетизация: кнопка «Done» (#save-button) не найдена")
            time.sleep(1.5)
            continue
        time.sleep(2.5)

        w = read_wizard(driver)
        if w.get("isCurrentStepInvalid") is False:
            logger.info("Монетизация применена и подтверждена (шаг валиден)")
            return True
        logger.warning("Монетизация: после «Done» шаг всё ещё невалиден — повтор")
        time.sleep(2)

    logger.error("Монетизация не применилась за %d попыток", attempts)
    return False


_FILL_RATINGS_JS = r"""
// В каждом вопросе выбираем ПЕРВЫЙ вариант — он всегда «нет / минимально».
// Суффикс у него разный от вопроса к вопросу (NTM, LTM, N, NO, NE), поэтому
// ориентируемся на порядок, а не на имя: PY_NTM, SC_NTM, VG_NTM, SK_LTM, HD_N,
// DG_N, DB_NTM, HH_NO, FM_NE, SE_NO, CI_LTM (снято дампом 2026-08-03).
// Радио лежат в свёрнутых вопросах (rect 0×0), поэтому здесь именно .click(),
// а не realClick по координатам.
var qs = deepAll(document).filter(function(e){
  return (e.tagName||'').toLowerCase()==='ytpp-self-certification-question';
});
var picked = [];
for(var i=0;i<qs.length;i++){
  var inner = deepAll(qs[i].shadowRoot || qs[i]);
  for(var j=0;j<inner.length;j++){
    var e = inner[j], id = e.id || '';
    if(id.indexOf('VIDEO_SELF_CERTIFICATION_ANSWER_') !== 0) continue;
    if(e.getAttribute('aria-checked') !== 'true'){ try{ e.click(); }catch(x){} }
    picked.push({id: id.replace('VIDEO_SELF_CERTIFICATION_ANSWER_',''),
                 ok: e.getAttribute('aria-checked')==='true'});
    break;   // только первый вариант вопроса
  }
}
return picked;
"""


def fill_content_ratings(driver, attempts=2):
    """Самосертификация: «нет» на все вопросы + Submit rating.

    ⚠️ Это ДЕКЛАРАЦИЯ О СОДЕРЖАНИИ ролика от имени владельца канала. Включено по
    прямому решению владельца (03.08.2026): заливка должна идти сквозняком, как
    руками. Если контент канала не соответствует «нет на всё» — это его риск по
    монетизации, не техническая деталь.

    Без пройденной анкеты Next заблокирован навсегда и мастер НИКОГДА не доходит
    до шага Visibility → ролик остаётся черновиком.
    """
    for att in range(1, attempts + 1):
        st = read_content_ratings(driver)
        if st.get("status") and st["status"] != "VIDEO_SELF_CERTIFICATION_STATUS_NOT_CERTIFIED":
            logger.info("Самосертификация уже пройдена (%s)", st["status"])
            return True

        picked = _js(driver, _FILL_RATINGS_JS) or []
        bad = [p["id"] for p in picked if not p.get("ok")]
        logger.info("Самосертификация: отвечено «нет» на %d вопросов%s",
                    len(picked), f", не проставились: {bad}" if bad else "")
        if not picked:
            logger.warning("Самосертификация: вопросы не найдены (попытка %d)", att)
            time.sleep(2)
            continue
        time.sleep(1.5)

        if not click(driver, "ytcp-button#submit-questionnaire-button"):
            logger.warning("Самосертификация: кнопка «Submit rating» не найдена")
            time.sleep(2)
            continue

        # статус проставляется бэкендом с задержкой — ждём его, а не смотрим разово
        st = wait_until(
            driver,
            lambda d: (read_content_ratings(d)
                       if (read_content_ratings(d).get("status")
                           and read_content_ratings(d)["status"]
                           != "VIDEO_SELF_CERTIFICATION_STATUS_NOT_CERTIFIED") else None),
            timeout=20, poll=2.5, desc="подтверждение самосертификации") or read_content_ratings(driver)
        w = read_wizard(driver)
        if st.get("status") and st["status"] != "VIDEO_SELF_CERTIFICATION_STATUS_NOT_CERTIFIED":
            logger.info("Самосертификация отправлена (%s), шаг валиден=%s",
                        st["status"], w.get("isCurrentStepInvalid") is False)
            return True
        logger.warning("Самосертификация: после Submit статус %s — повтор", st.get("status"))
        time.sleep(2)

    logger.error("Самосертификация не прошла за %d попыток", attempts)
    return False


def read_content_ratings(driver):
    """Состояние шага самосертификации (анкета из ~11 вопросов)."""
    try:
        return _js(driver, r"""
            var d = deepFind('ytcp-uploads-dialog', false);
            var qs = deepAll(document).filter(function(e){
                return (e.tagName||'').toLowerCase()==='ytpp-self-certification-question';
            });
            var submit = deepFind('ytcp-button#submit-questionnaire-button', true);
            return {
                required: d ? !!d.shouldShowContentRatingsStep : null,
                status: d ? d.selfCertificationStatus : null,
                questions: qs.length,
                submitVisible: !!submit
            };
        """) or {}
    except Exception as e:
        logger.debug("read_content_ratings: %s", e)
        return {}


def advance_to_review(driver, monetize=True, timeout=180, midroll_plan=None):
    """Провести мастер до шага Visibility (REVIEW) через Next.

    Для свежей заливки прыжок по step-badge не работает — шаги проходятся строго
    по порядку. Перед каждым Next чиним то, что делает шаг невалидным.
    Возвращает (состояние|None, причина_остановки).
    """
    start = time.time()
    placed = 0                      # сколько слотов реально встало на шаге монетизации
    while time.time() - start < timeout:
        w = read_wizard(driver)
        if not w.get("present"):
            return None, "мастер закрыт"
        step = w.get("workflowStep")
        if step == STEP_REVIEW:
            # отдаём наверх факт простановки: иначе воркер после публикации полезет
            # ставить паузы второй раз и потратит минуту на заведомо лишний заход
            w["midrollsPlaced"] = placed
            return w, "ok"

        # Обрабатываем ПО ТИПУ шага, а не по isCurrentStepInvalid: на шаге
        # самосертификации флаг бывает false и до отправки анкеты, а пройти шаг
        # без неё всё равно нельзя.
        if step == "MONETIZATION":
            # ПОРЯДОК ВАЖЕН: сначала монетизация On, только потом мидроллы —
            # кнопка «Review mid-roll placement» активна лишь при включённой рекламе.
            mon_ok = set_monetization(driver, on=monetize)
            if midroll_plan:
                if not mon_ok:
                    logger.warning("Мидроллы пропущены: монетизация не включилась")
                    midroll_plan = None
                else:
                    time.sleep(2)
                    ok_m, msg_m = place_midrolls_in_wizard(driver, midroll_plan)
                    logger.info("Мидроллы в мастере: %s · %s", ok_m, str(msg_m)[:90])
                    if ok_m:
                        placed = len(midroll_plan)
                    midroll_plan = None      # ставим один раз
        elif step == "CONTENT_RATINGS":
            if not fill_content_ratings(driver):
                cr = read_content_ratings(driver)
                return None, (f"самосертификация не прошла: {cr.get('questions')} вопросов, "
                              f"статус {cr.get('status')}")
        elif step == "DETAILS":
            if w.get("isCurrentStepInvalid"):
                set_not_made_for_kids(driver)
        elif w.get("isCurrentStepInvalid"):
            logger.warning("Шаг %s невалиден, а чинить нечем", step)

        time.sleep(2)
        click(driver, SEL["next_button"])
        time.sleep(3)

    return None, f"не дошёл до {STEP_REVIEW} за {timeout}с"


# --- финализация -----------------------------------------------------------
def click_done(driver):
    """Нажать финальную кнопку мастера (Save/Publish/Schedule)."""
    return click(driver, SEL["done_button"], visible_only=False)


_FINAL_STATE_JS = r"""
// Итог мастера. ВАЖНО: сам ytcp-uploads-dialog после успеха остаётся в DOM
// открытым — «мастер закрылся» НЕ является признаком успеха. Признак — финальный
// диалог («Video scheduled» / «Video published» / ссылка на видео).
var out = {gotIt:false, publishAnyway:false, finalDialog:'', videoUrl:'', doneText:''};
// [дамп 2026-08-08] Диалог «We're still checking your content» приходит в ДВУХ видах,
// и id кнопок в них означают ПРОТИВОПОЛОЖНОЕ:
//   вид А:  #primary-action-button = «Got it»        → закрыть предупреждение
//   вид Б:  #primary-action-button = «Go back»  (!)  → ОТМЕНА публикации
//           #secondary-action-button = «Publish anyway» → опубликовать не дожидаясь проверок
// Раньше код жал #primary-action-button вслепую и на виде Б сам отменял публикацию,
// после чего честно докладывал, что ролика на канале нет.
var sec = deepFind('ytcp-button#secondary-action-button', true);
out.publishAnyway = !!sec;
var b = deepFind('ytcp-button#primary-action-button', true);
out.gotIt = !!b && !sec;               // «Got it» — только когда рядом нет «Publish anyway»
var done = deepFind('ytcp-button#done-button', false);
if(done) out.doneText = (done.innerText||'').trim().slice(0,30);
// Финальный диалог опознаём ПО ДАННЫМ, а не по словам: прежний признак — английские
// слова scheduled|published|is live|processing — молча промахивался на локализованном
// Studio («Zaplanowano», «Naplánováno», «Programat»): 07.08 из-за этого 13 успешных
// заливок были объявлены провальными.
// [дамп 2026-08-07, research/dom/finalize_dialogs_2026-08-07.json] САМ МАСТЕР — это тоже
// tp-yt-paper-dialog#dialog, причём с классом ytcp-uploads-dialog и со ссылкой youtu.be
// внутри. Поэтому «есть ссылка» без этого исключения давало бы ложный успех ещё до
// финализации: диалог мастера надо отбрасывать по классу.
var all = deepAll(document);
for(var i=0;i<all.length;i++){
  var e = all[i], tag = (e.tagName||'').toLowerCase();
  if(tag !== 'tp-yt-paper-dialog') continue;
  var cls = (e.className||'').toString();
  if(cls.indexOf('ytcp-uploads-dialog') >= 0) continue;   // это мастер, а не подтверждение
  var r = e.getBoundingClientRect();
  if(!(r.width>0 && r.height>0)) continue;
  var t = (e.innerText||'').trim();
  var href = '';
  var links = e.querySelectorAll ? e.querySelectorAll('a[href]') : [];
  for(var j=0;j<links.length;j++){
    var h = links[j].getAttribute('href')||'';
    if(/youtu\.be\/[\w-]{11}/.test(h) || /watch\?v=[\w-]{11}/.test(h)){ href = h; break; }
  }
  var m = t.match(/https?:\/\/youtu\.be\/([\w-]{11})/);
  // Слова оставлены ТОЛЬКО как слабый сигнал для англоязычного интерфейса и никогда
  // не являются единственным основанием вердикта: настоящий признак — состояние строки
  // видео (visibility.userSetVisibility), см. research/dom/SELECTOR_MAP.md §«Признак успеха».
  var byWords = /scheduled|published|is live|processing/i.test(t.slice(0,60));
  if(href || m || byWords){
    out.finalDialog = t.replace(/\n/g,' | ').slice(0,200);
    out.finalBy = href ? 'ссылка' : (m ? 'ссылка-в-тексте' : 'текст');
    if(m) out.videoUrl = m[0];
    else if(href){
      var mm = href.match(/(?:youtu\.be\/|watch\?v=)([\w-]{11})/);
      if(mm) out.videoUrl = 'https://youtu.be/' + mm[1];
    }
  }
}
return out;
"""


def read_final_state(driver):
    """Итог мастера как ДАННЫЕ: финальный диалог, ссылка на ролик, признак «Got it».

    Публичная обёртка над _FINAL_STATE_JS. Нужна вызывающему (upload_worker), чтобы
    после добивки судить по подтверждению Studio, а не по исчезновению
    ytcp-uploads-dialog: мастер после успеха остаётся открытым (SELECTOR_MAP §2c),
    поэтому его закрытость не значит ни успеха, ни провала.
    """
    try:
        return _js(driver, _FINAL_STATE_JS) or {}
    except Exception as e:
        logger.debug("read_final_state: %s", e)
        return {}


def finalize(driver, timeout=120):
    """Нажать Schedule/Publish и дождаться подтверждения от Studio.

    Разбирает два препятствия, на которых заливка молча оставалась приватной:
      • диалог «We're still checking your content» с кнопкой #primary-action-button
        («Got it») — перекрывает мастер, и повторные клики по done уходят в него;
      • ожидание закрытия ytcp-uploads-dialog — он НЕ закрывается после успеха,
        поэтому ждём финальный диалог «Video scheduled / published».

    Возвращает (успех, текст_подтверждения).
    """
    click_done(driver)
    start = time.time()
    warned = False
    got_it_clicks = 0
    while time.time() - start < timeout:
        time.sleep(3)
        st = read_final_state(driver)
        if st.get("finalDialog"):
            logger.info("Финализация подтверждена Studio: %s", st["finalDialog"][:110])
            click(driver, SEL["close_button"])
            return True, st["finalDialog"]
        if st.get("publishAnyway"):
            # [дамп 2026-08-08] Вид Б диалога проверок: публикуем не дожидаясь окончания
            # проверок — заказчик так и просит («processing ждать не обязательно»).
            # ⚠️ Жать здесь #primary-action-button нельзя: это «Go back», то есть отмена.
            logger.info("Диалог проверок: жму «Publish anyway» (#secondary-action-button)")
            click(driver, "ytcp-button#secondary-action-button")
            time.sleep(3)
            continue
        if st.get("gotIt"):
            # #primary-action-button дампом подтверждён ТОЛЬКО у диалога «We're still
            # checking your content» («Got it»). Поверх мастера всплывает и другой диалог
            # («Publish anyway / Go back»), и если это он — клики уходят не туда. Раз кнопка
            # не убирает диалог за несколько попыток, перестаём по ней стучать (живьём цикл
            # молотил её ~50 раз подряд впустую) и просто ждём подтверждения до таймаута;
            # вердикт всё равно вынесет состояние строки видео у вызывающего.
            if got_it_clicks >= 3:
                if got_it_clicks == 3:
                    got_it_clicks += 1      # предупреждаем один раз, дальше молча ждём
                    logger.warning("Диалог поверх мастера не закрывается кнопкой "
                                   "#primary-action-button — перестаю по ней кликать "
                                   "(нужен дамп этого диалога: tools/ytdom.py dump)")
                continue
            if not warned:
                logger.info("Диалог «ещё идут проверки» — закрываю и повторяю финализацию")
                warned = True
            got_it_clicks += 1
            click(driver, "ytcp-button#primary-action-button")
            time.sleep(2)
            click_done(driver)
            continue
        if wizard_closed(driver):
            # Закрытие мастера — побочный эффект, а не состояние публикации: он и после
            # успеха остаётся открытым. Поэтому это не «успех», а «дальше решает состояние
            # строки видео» — вызывающий обязан проверить по read_videos/classify.
            logger.info("Мастер закрылся без финального диалога — вердикт по состоянию канала")
            return None, "wizard closed"
    # НЕ «провал»: мы лишь не увидели подтверждения. Настоящий вердикт даёт строка видео
    # на канале (userSetVisibility). 07.08 текстовое ожидание объявило провальными 13 заливок,
    # у которых файл был долит, а отложка выставлена и подтверждена.
    # Таймер — только верхняя граница ожидания, а не приговор. Чтобы разбор шёл по
    # фактам, а не по молчанию, дописываем СОСТОЯНИЕ мастера: по нему видно, застрял
    # ли шаг (isCurrentStepInvalid) или Studio просто не показала диалог.
    w = read_wizard(driver)
    logger.warning("Финализация: подтверждения не дождался за %dс — вердикт по состоянию канала "
                   "(мастер: present=%s step=%s invalid=%s errorSection=%s uploaded=%s)",
                   timeout, w.get("present"), w.get("workflowStep"),
                   w.get("isCurrentStepInvalid"), w.get("errorSectionStatus"),
                   w.get("isVideoUploaded"))
    return None, "нет подтверждения в диалоге"


def wizard_closed(driver):
    w = read_wizard(driver)
    return not w.get("present") or not w.get("open")


def goto_last_step(driver):
    """Перейти на последний шаг мастера по бейджу (стабильные id step-badge-N)."""
    try:
        return _js(driver, r"""
            var bs = deepAll(document).filter(function(e){
                return (e.id||'').indexOf('step-badge-')===0;
            });
            if(!bs.length) return 'no-badges';
            realClick(bs[bs.length-1]);
            return 'clicked:'+bs[bs.length-1].id;
        """)
    except Exception as e:
        return f"err:{e}"


# --- коллаборации ----------------------------------------------------------
# Живут НЕ в мастере заливки, а на странице видео /video/{id}/edit, в блоке
# расширенных настроек («Show more» → «Paid promotion, collaboration, and more»).
# Снято дампом 2026-08-03.
SEL_COLLAB = {
    "show_more":   "ytcp-button#toggle-button",
    "open_dialog": "ytcp-button#collaboration-button",     # «Invite a collaborator»
    "dialog":      "ytcp-video-collaborators-dialog",
    "search":      "ytcp-video-collaborators-dialog input#search-input",
    "result_item": "ytcp-channel-picker-list-item",        # «Название @handle • N subscribers»
    "create_link": "ytcp-button#create-link-button",       # ⚠️ обязательный шаг: без него Save заблокирован
    "save":        "ytcp-button#save-button",              # в диалоге (активен только после create-link)
    "cancel":      "ytcp-button#cancel-button",
    "page_save":   "ytcp-button#save",                     # «Save» страницы видео
}


def _norm_handle(h: str) -> str:
    """Handle к сравнимому виду: без диакритики, регистра и разделителей.

    YouTube показывает в поиске коллабораторов ОТОБРАЖАЕМЫЙ handle с диакритикой
    (`@CZ_StínŘíše`), а API отдаёт транслитерированный `customUrl` (`cz_stinrise`).
    Строгое сравнение строк рвётся именно здесь, и приглашение молча не уходит.
    Нормализуем обе стороны — строгость (совпадение целиком, а не «похоже») остаётся.
    """
    h = unicodedata.normalize("NFKD", (h or "").lstrip("@").strip().lower())
    h = "".join(c for c in h if not unicodedata.combining(c))
    # буквы ЛЮБОГО алфавита, не только латиница: у румынского Dosarele handle
    # кириллический (`@свекла-п8о`), и вырезание не-ASCII схлопнуло бы его в «8» —
    # такой «handle» совпал бы с любым каналом, где есть восьмёрка.
    return re.sub(r"[\W_]+", "", h, flags=re.UNICODE)


def _collab_results(driver):
    return _js(driver, r"""
        var out=[];
        var all=deepAll(document);
        for(var i=0;i<all.length;i++){
          var e=all[i];
          if((e.tagName||'').toLowerCase()!=='ytcp-channel-picker-list-item') continue;
          var r=e.getBoundingClientRect();
          if(!(r.width>0&&r.height>0)) continue;
          out.push({text:(e.innerText||'').trim().replace(/\s+/g,' ').slice(0,80), y:Math.round(r.top)});
        }
        return out;
    """) or []


def _click_collab_result(driver, index):
    return _js(driver, r"""
        var idx=arguments[0], n=0;
        var all=deepAll(document);
        for(var i=0;i<all.length;i++){
          var e=all[i];
          if((e.tagName||'').toLowerCase()!=='ytcp-channel-picker-list-item') continue;
          var r=e.getBoundingClientRect();
          if(!(r.width>0&&r.height>0)) continue;
          if(n++===idx){ realClick(e); return 'clicked'; }
        }
        return 'not-found';
    """, index)


def set_collaborator(driver, video_id, channel_query, wait=10, save=True, authuser=None):
    """Пригласить соавтора на видео.

    channel_query — @handle или название канала. Выбирается результат с ТОЧНЫМ
    совпадением handle (или первый, если handle не задан).

    ⚠️ Это приглашение РЕАЛЬНОМУ каналу — внешнее и необратимое действие.
    save=False прогоняет всё до сохранения, ничего не отправляя.

    ⚠️⚠️ Канал-соавтор брать ТОЛЬКО из явного указания владельца. Список в
    переключателе аккаунтов Studio НЕ означает, что канал ваш: на профиле 28
    он показывал 5 каналов, а доступ (владение) был лишь к 2 — приглашение по
    такому списку ушло постороннему каналу (инцидент 04.08.2026).
    Проверка владения: Studio канала (`/channel/{UC}`) не отдаёт
    «you don't have permission to view this page».

    Возвращает (успех, описание).
    """
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.action_chains import ActionChains

    if not channel_query:
        return False, "пустой запрос канала"

    def _collab_btn_visible(drv):
        return _js(drv, "return !!deepFind('%s', true);" % SEL_COLLAB["open_dialog"])

    def _show_more_visible(drv):
        return _js(drv, "return !!deepFind('%s', true);" % SEL_COLLAB["show_more"])

    # ⚠️ Страницу видео открываем С authuser: в одном AdsPower-профиле несколько
    # Google-аккаунтов, и без нужного номера Studio отдаёт страницу БЕЗ элементов
    # управления (ни «Show more», ни блока коллабораций) — молча, без ошибки.
    opened = False
    for au in ([authuser] if authuser is not None else (1, 0, 2, 3)):
        driver.get(f"https://studio.youtube.com/video/{video_id}/edit?authuser={au}")
        time.sleep(wait)
        if _collab_btn_visible(driver) or _show_more_visible(driver):
            opened = True
            break
    if not opened:
        return False, "страница видео не открылась ни под одним authuser (нет «Show more»)"

    # Блок коллабораций спрятан под «Show more»; раскрытие не мгновенное.
    if not _collab_btn_visible(driver):
        if not wait_until(driver, _show_more_visible, timeout=30, poll=2,
                          desc="кнопка «Show more»"):
            return False, "страница видео не догрузилась (нет «Show more»)"
        # ⚠️ Кнопка ПЕРЕКЛЮЧАЕТ блок: повторный клик сворачивает его обратно.
        # Поэтому кликаем один раз и ждём, а не «кликаем в цикле».
        click(driver, SEL_COLLAB["show_more"])
        wait_until(driver, _collab_btn_visible, timeout=25, poll=2, desc="блок коллабораций")
    if not _collab_btn_visible(driver):
        return False, "кнопка «Invite a collaborator» не найдена (блок не раскрылся)"

    # YouTube может ЗАПРЕТИТЬ коллаборацию на конкретном ролике — кнопка остаётся
    # видимой, но с aria-disabled=true. Клик тогда молча не открывает диалог, и без
    # этой проверки причина выглядит как «диалог не открылся» (ложный след).
    if _js(driver, """
        var b = deepFind('%s', true);
        return !!(b && b.getAttribute('aria-disabled') === 'true');
    """ % SEL_COLLAB["open_dialog"]):
        return False, "YouTube запрещает коллаборацию на этом ролике (кнопка disabled)"

    if not click(driver, SEL_COLLAB["open_dialog"]):
        return False, "кнопка «Invite a collaborator» не нажалась"
    # диалог тоже открывается не мгновенно
    # диалог иногда не успевает за 15с; при неудаче пробуем открыть повторно
    if not wait_until(driver,
                      lambda d: _js(d, "return !!deepFind('%s', true);" % SEL_COLLAB["search"]),
                      timeout=25, poll=2, desc="диалог коллабораций"):
        click(driver, SEL_COLLAB["open_dialog"])
        if not wait_until(driver,
                          lambda d: _js(d, "return !!deepFind('%s', true);" % SEL_COLLAB["search"]),
                          timeout=25, poll=2, desc="диалог коллабораций (повтор)"):
            return False, "диалог коллабораций не открылся"

    try:
        inp = driver.find_element(By.CSS_SELECTOR, SEL_COLLAB["search"])
    except Exception:
        return False, "поле поиска канала не найдено"

    # нативный ввод: у Polymer-полей JS-присваивание не запускает поиск
    driver.execute_script("arguments[0].focus();", inp)
    time.sleep(0.4)
    ActionChains(driver).send_keys(channel_query).perform()

    results = wait_until(driver, lambda d: _collab_results(d) or None,
                         timeout=25, poll=2, desc="результаты поиска канала") or []
    if not results:
        click(driver, SEL_COLLAB["cancel"])
        return False, f"канал {channel_query!r} не найден в поиске"

    # ВЫБОР ТОЛЬКО ПО ТОЧНОМУ HANDLE. Поиск по названию возвращает чужие каналы с
    # похожим именем: запрос «DARK Film Studio» первым отдал «Dark Film Studio
    # @frahanayan97 • 0 subscribers» — совершенно посторонний канал. Приглашение
    # необратимо уходит наружу, поэтому «взять первый похожий» тут недопустимо.
    want = _norm_handle(channel_query)
    idx = None
    for i, r in enumerate(results):
        # из строки результата вытаскиваем сам handle и сравниваем нормализованно
        for cand in re.findall(r"@[^\s•·]+", r.get("text") or ""):
            if _norm_handle(cand) == want:
                idx = i
                break
        if idx is not None:
            break
    if idx is None:
        click(driver, SEL_COLLAB["cancel"])
        variants = " · ".join(r.get("text", "")[:45] for r in results[:5])
        return False, (f"точного совпадения по handle @{want} нет — НЕ приглашаю "
                       f"(нашлось: {variants})")

    chosen = results[idx].get("text", "")
    logger.info("Коллаборация: найдено %d, точное совпадение %r", len(results), chosen[:60])

    if not save:
        click(driver, SEL_COLLAB["cancel"])
        return True, f"(dry-run) выбран бы: {chosen}"

    if _click_collab_result(driver, idx) != "clicked":
        click(driver, SEL_COLLAB["cancel"])
        return False, "не удалось выбрать канал из списка"
    time.sleep(3)

    # ОБЯЗАТЕЛЬНЫЙ шаг: коллаборация оформляется ссылкой-приглашением. Пока её не
    # создали, #save-button остаётся aria-disabled=true, и клик по нему уходит
    # в никуда — именно так «сохранение» молча не срабатывало.
    if not click(driver, SEL_COLLAB["create_link"]):
        click(driver, SEL_COLLAB["cancel"])
        return False, "кнопка «Create link» не найдена"

    active = wait_until(
        driver,
        lambda d: _js(d, """
            var b = deepFind('%s', true);
            return !!(b && b.getAttribute('aria-disabled') !== 'true');
        """ % SEL_COLLAB["save"]),
        timeout=20, poll=2, desc="разблокировка Save в диалоге")
    if not active:
        click(driver, SEL_COLLAB["cancel"])
        return False, "после «Create link» кнопка Save осталась заблокированной"

    if not click(driver, SEL_COLLAB["save"]):
        click(driver, SEL_COLLAB["cancel"])
        return False, "кнопка сохранения в диалоге не найдена"
    time.sleep(4)

    # изменения страницы видео сохраняются отдельной кнопкой (если она активна)
    if _js(driver, """
        var b = deepFind('%s', true);
        return !!(b && b.getAttribute('aria-disabled') !== 'true');
    """ % SEL_COLLAB["page_save"]):
        click(driver, SEL_COLLAB["page_save"])
        time.sleep(4)

    # ВЕРИФИКАЦИЯ: блок меняется с «Invite a collaborator» на «Manage collaborators»
    state = _js(driver, r"""
        var txt = '';
        deepAll(document).forEach(function(e){
          var r=e.getBoundingClientRect();
          if(!(r.width>0&&r.height>0)) return;
          if(e.children && e.children.length) return;
          var t=(e.innerText||'').trim();
          if(/manage collaborator|copy and share your invite/i.test(t)) txt += t + ' | ';
        });
        return txt.slice(0, 140);
    """) or ""
    if not state:
        return False, f"выбран {chosen}, но подтверждения («Manage collaborators») не видно"
    return True, f"приглашение создано для {chosen} · {state.strip(' |')}"


def collab_invite_url(guest_uc: str, inviter_uc: str) -> str:
    """Ссылка-приглашение. Строится из двух ID — буфер обмена не нужен.

    Формат снят с кнопки «Copy invite link» (дамп 2026-08-04):
    studio.youtube.com/channel/{UC_приглашённого}/collaboration/{UC_пригласившего}
    """
    return f"https://studio.youtube.com/channel/{guest_uc}/collaboration/{inviter_uc}"


# Диалог приглашения (дамп 2026-08-04): «You're invited to collaborate»
SEL_ACCEPT = {
    "accept":  "ytcp-button#confirm-button",   # «Accept»
    "decline": "ytcp-button#deny-button",      # «Decline»
    "close":   "ytcp-icon-button#close-button",
}

_INVITE_DIALOG_JS = r"""
// Заголовок локализован, поэтому опознаём диалог по паре кнопок Accept/Decline.
var found = false;
deepAll(document).forEach(function(e){
  if(found) return;
  if(e.id !== 'confirm-button') return;
  var r = e.getBoundingClientRect();
  if(r.width>0 && r.height>0) found = true;
});
return found;
"""


def accept_collaboration(driver, guest_uc, inviter_uc, wait=14):
    """Принять приглашение на коллаборацию (сторона соавтора).

    ⚠️ ГЛАВНОЕ: перед вызовом активным каналом сессии должен быть КАНАЛ-СОАВТОР,
    и переключать его нужно ЧЕРЕЗ МЕНЮ Studio (upload_worker._switch_to_channel),
    а не заходом по URL `/channel/{UC}`. Заход по URL открывает Studio нужного
    канала, но активный канал сессии не меняет — и страница приглашения тогда
    молча редиректит на /videos/upload (потеряно ~час на этом).

    Проверено живьём 04.08.2026: приглашение принято, у отправителя статус
    сменился с «Pending acceptance» на «Active collaborator».

    Возвращает (успех, описание).
    """
    driver.get(collab_invite_url(guest_uc, inviter_uc))
    time.sleep(wait)

    if not _js(driver, _INVITE_DIALOG_JS):
        return False, (f"диалог приглашения не появился (URL: "
                       f"{(driver.current_url or '')[:70]}) — обычно значит, что активным "
                       f"каналом сессии стоит не соавтор; переключите канал через меню")

    if not click(driver, SEL_ACCEPT["accept"]):
        return False, "кнопка Accept не нажалась"
    time.sleep(6)

    if _js(driver, _INVITE_DIALOG_JS):
        return False, "после клика диалог всё ещё открыт — приглашение не принято"
    logger.info("Коллаборация принята (диалог закрылся)")
    return True, "принято"


_FIND_INVITE_MAIL_JS = r"""
// Поиск письма-приглашения в открытом Gmail. Тема локализована, поэтому ищем
// по ссылке /collaboration/ в теле письма — она язык-независима.
var out = {links: [], subjects: []};
document.querySelectorAll('a[href]').forEach(function(a){
  var h = a.getAttribute('href') || '';
  if(h.indexOf('/collaboration/') >= 0) out.links.push(h.slice(0, 200));
});
document.querySelectorAll('tr.zA .y6').forEach(function(e){
  out.subjects.push((e.innerText||'').trim().slice(0, 80));
});
return out;
"""


def find_collab_invite_in_gmail(driver, authuser=0, query="from:youtube collaborat"):
    """Найти ссылку-приглашение в Gmail профиля.

    Приглашение приходит письмом на почту Google-аккаунта канала-соавтора,
    поэтому запускать в ЕГО профиле. Ищем по ссылке `/collaboration/` в теле —
    она не зависит от языка письма.
    """
    driver.get(f"https://mail.google.com/mail/u/{authuser}/#search/"
               + query.replace(" ", "+").replace(":", "%3A"))
    time.sleep(12)
    res = _js(driver, _FIND_INVITE_MAIL_JS) or {}
    links = res.get("links") or []
    if links:
        logger.info("Найдена ссылка-приглашение в почте: %s", links[0][:90])
        return links[0]
    logger.info("Письмо-приглашение не найдено (тем в выдаче: %d)",
                len(res.get("subjects") or []))
    return None


def open_draft_wizard(driver, video_id: str, wait=10):
    """Открыть мастер существующего черновика по videoId.

    Прямой путь /video/{id}/edit → кнопка #action-1 («Edit draft»). Надёжнее,
    чем искать строку в списке по тексту заголовка: id мы уже знаем из read_videos,
    а у кнопки стабильный id вместо текста на 9 языках.
    """
    # authuser перебираем на месте: в одном профиле живут каналы РАЗНЫХ Google-аккаунтов,
    # и без верного номера страница отдаёт «Oops, something went wrong» — это читалось
    # как «мастер не открылся», хотя черновик цел и доступен под другим номером.
    opened = False
    for au in ("", "1", "0", "2", "3"):
        suf = f"?authuser={au}" if au else ""
        driver.get(f"https://studio.youtube.com/video/{video_id}/edit{suf}")
        time.sleep(wait)
        try:
            body = (driver.find_element("tag name", "body").text or "")[:120].lower()
        except Exception:                     # noqa: BLE001
            body = ""
        if "oops" in body or "permission" in body:
            continue
        opened = True
        break
    if not opened:
        logger.warning("open_draft_wizard: %s не открылся ни под одним authuser", video_id)
        return False
    if not click(driver, SEL["edit_draft_btn"]):
        return False
    if wait_until(driver, lambda d: read_wizard(d).get("present"),
                  timeout=60, desc="мастер черновика") is None:
        return False
    # мастер в DOM ≠ мастер готов: сразу после открытия он отдаёт LOADING
    return wait_wizard_ready(driver) is not None


# ---------------------------------------------------------------------------
# Мидроллы (ручная расстановка рекламных слотов)
# Страница: /video/{id}/monetization/ads → «Review mid-roll placement»
# Снято дампом 2026-08-04.
# ---------------------------------------------------------------------------
# дамп 2026-08-05 (research/dom/SELECTOR_MAP.md, раздел «Рекламные паузы»):
# панель и поля времени живут в SHADOW DOM, поля отдают время как Ч:ММ:СС:кадры
SEL_ADS = {
    "review_btn":  "ytcp-button#place-manually-button",   # «Review mid-roll placement»
    "editor":      "ytmonet-video-monetization-editor",
    "panel":       "ytve-ad-breaks-editor-options-panel",  # дамп 05.08.2026, внутри shadow
    "row":         "ytve-framestamp-input",                # дамп 05.08.2026, строка слота
    "marker":      "ytve-ad-breaks-marker",               # свойство positionMs
    "insert_btn":  '[aria-label="Insert ad slot"]',       # дамп 05.08.2026, aria-disabled=false
    "back_btn":    "ytcp-button#discard-button",           # выйти без сохранения
    "continue_btn": "ytcp-button#save-button",             # «Continue» из редактора
    "page_save":   "ytcp-button#save",                     # «Save» страницы монетизации
}

AD_FPS = 30          # поля времени в формате MM:SS:FF; FF — кадры
MIDROLL_STEP_SEC = 150   # 2.5 мин — из аналитики владельца (2.5–3 мин безопасно для Ad Spam)
MIDROLL_OFFSET_SEC = 1   # +1 с от начала главы: слот попадает на смену кадра, а не на речь


def fmt_ad_timecode(seconds: float, fps: int = AD_FPS) -> str:
    """Секунды → Ч:ММ:СС:КК, как ждут поля редактора мидроллов.

    Формат снят живым дампом 05.08.2026: поля отдают время ЧЕТЫРЬМЯ частями
    (`0:05:51:13` = 0 ч 5 мин 51 с 13 кадров). Раньше здесь было MM:SS:FF, и на
    часовых роликах минуты уезжали за 60 — слоты вставали не туда либо не вставали.
    """
    total = max(0.0, float(seconds))
    h = int(total // 3600)
    m = int((total % 3600) // 60)
    s = int(total % 60)
    f = int(round((total - int(total)) * fps))
    if f >= fps:
        f = fps - 1
    return f"{h}:{m:02d}:{s:02d}:{f:02d}"


def plan_midrolls(duration_sec, chapters=None, step_sec=MIDROLL_STEP_SEC,
                  offset_sec=MIDROLL_OFFSET_SEC, head_skip=60, tail_skip=60):
    """Список секунд для рекламных слотов.

    chapters — секунды начала глав (из таймкодов). Если заданы, слоты ставятся по
    ним со сдвигом +offset_sec: слот попадает на смену кадра/паузу, а не в середину
    фразы (YouTube помечает такие как High Quality Ad Break).
    Если глав нет — равномерно каждые step_sec.

    head_skip / tail_skip — не ставить в самом начале и в самом конце ролика.
    """
    duration_sec = float(duration_sec or 0)
    if duration_sec <= 0:
        return []
    out = []
    if chapters:
        for c in sorted(float(x) for x in chapters):
            t = c + offset_sec
            if head_skip <= t <= duration_sec - tail_skip:
                out.append(t)
    else:
        t = float(step_sec)
        while t <= duration_sec - tail_skip:
            out.append(t)
            t += step_sec
    # убрать слишком близкие друг к другу (<45 с) — YouTube их всё равно схлопнет
    dedup = []
    for t in out:
        if not dedup or t - dedup[-1] >= 45:
            dedup.append(t)
    return dedup


_READ_SLOTS_JS = r"""
var mk = deepAll(document).filter(function(e){
  return (e.tagName||'').toLowerCase()==='ytve-ad-breaks-marker';
});
return mk.map(function(e){
  var p=null; try{ p=e.positionMs; }catch(x){}
  return p===undefined? null : p;
}).filter(function(x){ return x!==null; });
"""


def open_ad_breaks_editor(driver, video_id, authuser=None, wait=13):
    """Открыть редактор мидроллов ролика. Возвращает (ok, описание)."""
    # ⚠️ Кнопка появляется РАНЬШЕ, чем страница догружает данные монетизации, и в
    # этот момент она ещё aria-disabled=true, а редактор открывается пустым
    # (shouldDisableAdBreaksEditor=true, Insert не работает). Поэтому ждём именно
    # АКТИВНОСТИ кнопки, а не её появления — проверено живьём 04.08.2026.
    def _review_ready(drv):
        return _js(drv, """
            var b = deepFind('%s', true);
            return !!(b && b.getAttribute('aria-disabled') !== 'true');
        """ % SEL_ADS["review_btn"])

    opened = False
    for au in ([authuser] if authuser is not None else (1, 0, 2, 3)):
        driver.get(f"https://studio.youtube.com/video/{video_id}/monetization/ads?authuser={au}")
        time.sleep(wait)
        if not _js(driver, "return !!deepFind('%s', true);" % SEL_ADS["review_btn"]):
            continue
        if wait_until(driver, _review_ready, timeout=45, poll=2,
                      desc="активность «Review mid-roll placement»"):
            opened = True
            break
        return False, ("мидроллы недоступны: кнопка так и осталась disabled "
                       "(нет YPP на канале или реклама выключена для ролика)")
    if not opened:
        # Частая и НЕ ошибочная причина — у канала просто нет YPP: в мастере тогда
        # нет шага MONETIZATION (проход идёт сразу в REVIEW), а страница /monetization/ads
        # не отдаёт кнопку вовсе. Отличаем это от протухшего authuser, чтобы ночные
        # логи не читались как поломка.
        no_ypp = _js(driver, """
            var t = (document.body ? document.body.innerText : '') || '';
            return /not (yet )?eligible|Partner Program|monetization is not|ineligible/i.test(t);
        """)
        if no_ypp:
            return False, "канал без YPP — рекламные паузы недоступны (это не сбой)"
        return False, "страница монетизации не открылась (нет «Review mid-roll placement»)"

    if not click(driver, SEL_ADS["review_btn"]):
        return False, "кнопка «Review mid-roll placement» не нажалась"
    if not wait_until(driver,
                      lambda d: _js(d, "return !!deepFind('%s', false);" % SEL_ADS["panel"]),
                      timeout=25, poll=2, desc="редактор мидроллов"):
        return False, "редактор мидроллов не открылся"
    return True, "редактор открыт"


def read_ad_slots(driver):
    """Слоты, ВИДИМЫЕ В РЕДАКТОРЕ (маркеры на таймлайне), в секундах.

    ⚠️ Пока YouTube генерирует автослоты (AUTO_GEN_MIDROLLS_STATUS_PROCESSING),
    редактор заблокирован и маркеров нет — но сохранённые ручные слоты при этом
    ЕСТЬ. Чтобы проверить, что расстановка применилась, используйте
    read_saved_midrolls(): она читает настройку ролика, а не отрисовку.
    """
    try:
        ms = _js(driver, _READ_SLOTS_JS) or []
        return [round(x / 1000.0, 3) for x in ms]
    except Exception as e:
        logger.debug("read_ad_slots: %s", e)
        return []


_READ_SAVED_MIDROLLS_JS = r"""
var s = deepFind('ytpp-ad-breaks-settings', false);
if(!s) return null;
var ab = null;
try{ ab = s.adBreaks || {}; }catch(e){ return null; }
function times(o){
  try{ return (o && o.placementTimesMillis) ? o.placementTimesMillis.slice() : []; }
  catch(e){ return []; }
}
return {
  manual: times(ab.manualMidrollPlacements),
  auto: times(ab.autoGenMidrollPlacements),
  autoStatus: ab.autoGenMidrollsStatus || null,
  hasMidroll: !!ab.hasMidrollAds
};
"""


def read_saved_midrolls(driver, video_id=None, authuser=None, wait=16):
    """СОХРАНЁННЫЕ слоты ролика: ручные + автоматические (в секундах).

    Источник — настройка ytpp-ad-breaks-settings.adBreaks:
      manualMidrollPlacements.placementTimesMillis  — то, что расставили мы;
      autoGenMidrollPlacements.placementTimesMillis — что расставил YouTube;
      autoGenMidrollsStatus — …_PROCESSING, пока автослоты считаются.

    ⚠️ YouTube может СДВИНУТЬ наши метки к ближайшей естественной точке
    (2:30 → 2:53) — это нормально, сравнивать с планом «в лоб» нельзя.
    """
    if video_id:
        for au in ([authuser] if authuser is not None else (1, 0, 2, 3)):
            driver.get(f"https://studio.youtube.com/video/{video_id}/monetization/ads?authuser={au}")
            time.sleep(wait)
            if _js(driver, "return !!deepFind('ytpp-ad-breaks-settings', false);"):
                break
    try:
        res = _js(driver, _READ_SAVED_MIDROLLS_JS) or {}
    except Exception as e:
        logger.debug("read_saved_midrolls: %s", e)
        return {"manual": [], "auto": [], "autoStatus": None, "hasMidroll": False}
    return {
        "manual": [round(x / 1000.0, 3) for x in (res.get("manual") or [])],
        "auto": [round(x / 1000.0, 3) for x in (res.get("auto") or [])],
        "autoStatus": res.get("autoStatus"),
        "hasMidroll": bool(res.get("hasMidroll")),
    }


_SLOT_INPUTS_JS = r"""
// Поля времени слотов. Панель и поля лежат в shadow DOM
// (ytve-formatted-input < ytve-framestamp-input < … < ytve-ad-breaks-editor-options-panel),
// поэтому обычный find_elements(By.CSS_SELECTOR) их НЕ ВИДИТ — дамп 05.08.2026.
var panel = deepFind('ytve-ad-breaks-editor-options-panel', false);
if(!panel) return [];
return deepAll(panel).filter(function(e){
  return e.tagName === 'INPUT' && e.offsetParent !== null;
});
"""


def _slot_inputs(driver):
    """Видимые поля времени слотов (в порядке отображения)."""
    from selenium.webdriver.common.by import By
    out = []
    try:
        out = _js(driver, _SLOT_INPUTS_JS) or []
        if out:
            return out
    except Exception as e:
        logger.debug("_slot_inputs (deep): %s", e)
    try:
        for host in driver.find_elements(By.CSS_SELECTOR, SEL_ADS["panel"]):
            for inp in host.find_elements(By.CSS_SELECTOR, "input"):
                try:
                    if inp.is_displayed():
                        out.append(inp)
                except Exception:
                    continue
    except Exception:
        pass
    return out


def _type_timecode(driver, inp, timecode):
    """Вписать время в поле слота. Единственный способ, который Studio засчитывает.

    Замер 05.08.2026: поле `ytve-framestamp-input` игнорирует и клавиатуру Selenium
    (ActionChains), и запись в модель панели, и нативный сеттер значения — визуально
    значение меняется, но маркер стоит на месте и кнопка сохранения гаснет.
    Проходит только связка: настоящий клик мышью по координатам поля (CDP) →
    выделение всего содержимого → Input.insertText → Enter.
    """
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", inp)
    time.sleep(0.4)
    box = driver.execute_script(
        "var r=arguments[0].getBoundingClientRect();"
        "return {x:r.x+r.width/2, y:r.y+r.height/2};", inp)
    for ev in ("mousePressed", "mouseReleased"):
        driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
            "type": ev, "x": box["x"], "y": box["y"], "button": "left", "clickCount": 1})
    time.sleep(0.5)
    driver.execute_script(
        "arguments[0].setSelectionRange(0, (arguments[0].value||'').length);", inp)
    time.sleep(0.2)
    driver.execute_cdp_cmd("Input.insertText", {"text": timecode})
    time.sleep(0.6)
    for t in ("keyDown", "keyUp"):
        driver.execute_cdp_cmd("Input.dispatchKeyEvent", {
            "type": t, "key": "Enter", "code": "Enter",
            "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13})
    time.sleep(0.9)
    # Сверять строку в поле нельзя: после Enter Studio переформатирует значение
    # (и элемент может устареть). Судим по факту — встал ли маркер на эту секунду.
    return True


def add_ad_slots(driver, seconds_list, verify=True):
    """Добавить рекламные слоты на заданные секунды.

    «Insert ad slot» создаёт слот на 00:00:00, поэтому время затем вводится в поле
    НАТИВНО (ActionChains): у полей Polymer JS-присваивание не доходит до модели.

    Возвращает (сколько добавлено, описание).
    """
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.common.action_chains import ActionChains

    # open_ad_breaks_editor возвращает True раньше, чем панель отрисует строки слотов:
    # сразу после открытия полей и маркеров НОЛЬ, они появляются через 1–2 с. Клик
    # «Insert ad slot» в этот момент уходит вхолостую — слот не создаётся, а функция
    # потом ищет несуществующее поле (замер 05.08.2026).
    wait_until(driver, lambda d: bool(_slot_inputs(d)) or bool(read_ad_slots(d)),
               timeout=25, poll=0.8, desc="панель слотов готова")

    existing = set(int(x) for x in read_ad_slots(driver))
    todo = [s for s in seconds_list if int(s) not in existing]
    if not todo:
        return 0, "все слоты уже стоят"

    added = 0
    for sec in todo:
        before = len(read_ad_slots(driver))
        # Пока YouTube не закончил обработку, вставка молча не срабатывает —
        # об этом лучше сказать прямо, чем 12 с ждать несуществующий слот.
        if not click(driver, SEL_ADS["insert_btn"]):
            return added, "кнопка «Insert ad slot» не найдена"
        if not wait_until(driver, lambda d: len(read_ad_slots(d)) > before,
                          timeout=12, poll=0.6, desc="новый слот"):
            return added, "слот не добавился"

        # Ждём именно ПОЛЕ, а не маркер: маркер на таймлайне появляется раньше, чем
        # отрисуется строка с полем времени, и фиксированной паузы 0.3 с не хватало —
        # поиск шёл по ещё не существующему полю (замер 05.08.2026).
        def _zero_field(d):
            for _i in _slot_inputs(d):
                try:
                    _v = (_i.get_attribute("value") or "").strip()
                except Exception:
                    continue
                if _v and set(_v) <= {"0", ":"}:
                    return True
            return False

        wait_until(driver, _zero_field, timeout=10, poll=0.5, desc="поле нового слота")

        # новый слот создаётся на нулях — ищем это поле и вписываем время
        target = None
        for inp in _slot_inputs(driver):
            try:
                # новый слот приходит нулями, но частей может быть 3 или 4
                # ('0:00:00:00'), поэтому сверяем цифры, а не строку целиком
                _v = (inp.get_attribute("value") or "").strip()
                if _v and set(_v) <= {"0", ":"}:
                    target = inp
                    break
            except Exception:
                continue
        if target is None:
            return added, "поле нового слота не найдено"

        tc = fmt_ad_timecode(sec)
        try:
            _type_timecode(driver, target, tc)
            # проверяем результат по маркерам таймлайна, а не по тексту поля
            if not wait_until(driver,
                              lambda d: any(abs(float(x) - float(sec)) <= 2.0
                                            for x in read_ad_slots(d)),
                              timeout=8, poll=0.6, desc=f"метка на {int(sec)}с"):
                logger.warning("Слот %ss: метка не встала на место", int(sec))
        except Exception as e:
            return added, f"ввод времени упал: {str(e)[:60]}"
        added += 1

    if verify:
        now = read_ad_slots(driver)
        logger.info("Мидроллы: было %d, стало %d слотов", len(existing), len(now))
    return added, f"добавлено слотов: {added}"


_SET_SLOTS_FAST_JS = r"""
// БЫСТРЫЙ путь: пишем массив слотов прямо в модель панели вместо N кликов
// «Insert ad slot» + N нативных вводов времени (22 слота: ~0.5 с против ~26 с).
// Структура элемента снята дампом 2026-08-04: {placementTimeMs, type:'auto'|'manual'}.
var p = deepFind('ytve-ad-breaks-editor-options-panel', false);
if(!p) return {ok:false, why:'нет панели'};
var times = arguments[0];
var arr = times.map(function(sec){
  return {placementTimeMs: Math.round(sec*1000), type: 'manual'};
});
try{
  if(typeof p.set === 'function') p.set('adBreaks', arr);   // Polymer-путь
  else p.adBreaks = arr;
  if(typeof p.updateAdBreaksRenderData === 'function') p.updateAdBreaksRenderData();
}catch(e){ return {ok:false, why:String(e).slice(0,120)}; }
var got = 0;
try{ got = (p.adBreaks||[]).length; }catch(e){}
return {ok: got === arr.length, applied: got, wanted: arr.length};
"""


def set_ad_slots_fast(driver, seconds_list):
    """Задать ВЕСЬ набор слотов разом через модель панели.

    Возвращает (ok, описание). Панель перерисовывает поля времени сразу; маркеры
    на таймлайне при этом могут остаться от прежней раскладки — это визуализация,
    сохранение идёт из модели adBreaks.
    """
    if not seconds_list:
        return False, "пустой список слотов"
    try:
        res = _js(driver, _SET_SLOTS_FAST_JS, [float(x) for x in seconds_list]) or {}
    except Exception as e:
        return False, f"быстрый путь упал: {str(e)[:70]}"
    if not res.get("ok"):
        return False, res.get("why") or f"применилось {res.get('applied')} из {res.get('wanted')}"

    # ⚠️ Запись в модель Polymer панель ОТРИСОВЫВАЕТ, но YouTube её не сохраняет:
    # кнопка подтверждения остаётся неактивной, а после перезагрузки слотов нет
    # (проверено живьём 04.08.2026). Поэтому успехом считаем только тот случай,
    # когда Studio засчитала изменение и разблокировала сохранение.
    time.sleep(1.5)
    live = _js(driver, """
        var c = deepFind('ytcp-button#save-button', true);
        var p = deepFind('ytcp-button#save', true);
        var ca = c && c.getAttribute('aria-disabled') !== 'true';
        var pa = p && p.getAttribute('aria-disabled') !== 'true';
        return !!(ca || pa);
    """)
    if not live:
        return False, "модель приняла, но Studio не засчитала изменение (нужны клики)"
    return True, f"слотов задано: {res.get('applied')}"


def delete_ad_slots(driver, seconds_list, tol=3.0):
    """Удалить рекламные слоты на указанных секундах. Возвращает (сколько удалено, описание).

    Способ снят дампом 05.08.2026: у маркера нет своей кнопки удаления, а кнопка
    «Remove Ads» на панели — ЧУЖАЯ, её рисует расширение Nexlev (`nexlev-csui`),
    поэтому клики по ней в Studio ничего не меняют. Работает: настоящий клик мышью
    по маркеру (CDP) + клавиша Delete.
    """
    _find = _JS_BASE + """
    var want = arguments[0];
    var m = deepAll(document).filter(function(e){
      return (e.tagName||'').toLowerCase()==='ytve-ad-breaks-marker' &&
             Math.abs((e.positionMs||0)/1000 - want) < arguments[1]; })[0];
    if(!m) return null;
    var r = m.getBoundingClientRect();
    return {x: r.x + r.width/2, y: r.y + r.height/2};
    """
    killed = 0
    for sec in seconds_list:
        try:
            pos = driver.execute_script(_find, float(sec), float(tol))
        except Exception as e:
            logger.debug("delete_ad_slots(%s): %s", sec, e)
            continue
        if not pos:
            continue
        for ev in ("mousePressed", "mouseReleased"):
            driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
                "type": ev, "x": pos["x"], "y": pos["y"], "button": "left", "clickCount": 1})
        time.sleep(1.0)
        for t in ("keyDown", "keyUp"):
            driver.execute_cdp_cmd("Input.dispatchKeyEvent", {
                "type": t, "key": "Delete", "code": "Delete",
                "windowsVirtualKeyCode": 46, "nativeVirtualKeyCode": 46})
        time.sleep(1.2)
        killed += 1
    return killed, f"удалено слотов: {killed}"


def save_ad_breaks(driver):
    """Сохранить расстановку: «Continue» в редакторе → «Save» страницы."""
    click(driver, SEL_ADS["continue_btn"])
    time.sleep(3)
    if _js(driver, """
        var b = deepFind('%s', true);
        return !!(b && b.getAttribute('aria-disabled') !== 'true');
    """ % SEL_ADS["page_save"]):
        click(driver, SEL_ADS["page_save"])
        time.sleep(4)
        return True, "сохранено"
    return False, "кнопка Save страницы неактивна (нечего сохранять?)"


# --- мидроллы ПРЯМО В МАСТЕРЕ ЗАЛИВКИ -------------------------------------
# На шаге MONETIZATION мастера есть та же кнопка «Review mid-roll placement»
# и чекбокс «Show mid-roll ads during my video». Ставить слоты можно уже во
# время загрузки файла — не дожидаясь публикации (снято со скриншота+DOM 2026-08-04).
SEL_WIZ_ADS = {
    "midroll_checkbox": "ytcp-checkbox-lit#enable-midroll-checkbox",
    "review_btn":       "ytcp-button#place-manually-button",
}


def place_midrolls_in_wizard(driver, seconds_list, timeout=40):
    """Расставить рекламные слоты на шаге Monetization мастера заливки.

    Возвращает (ok, описание). Не бросает: заливка важнее рекламы.
    """
    if not seconds_list:
        return False, "пустой план слотов"
    try:
        # включить мидроллы, если чекбокс есть и снят
        try:
            st = _js(driver, """
                var c = deepFind('%s', true);
                if(!c) return null;
                return c.getAttribute('aria-checked') || (c.checked ? 'true' : 'false');
            """ % SEL_WIZ_ADS["midroll_checkbox"])
            if st == "false":
                click(driver, SEL_WIZ_ADS["midroll_checkbox"])
                time.sleep(1)
        except Exception:
            pass

        if not wait_until(driver,
                          lambda d: _js(d, "return !!deepFind('%s', true);" % SEL_WIZ_ADS["review_btn"]),
                          timeout=timeout, poll=2, desc="кнопка мидроллов в мастере"):
            return False, "в мастере нет «Review mid-roll placement»"
        if not click(driver, SEL_WIZ_ADS["review_btn"]):
            return False, "кнопка мидроллов не нажалась"
        if not wait_until(driver,
                          lambda d: _js(d, "return !!deepFind('%s', false);" % SEL_ADS["panel"]),
                          timeout=25, poll=2, desc="панель слотов в мастере"):
            return False, "панель слотов не открылась"

        # ⚠️ Только реальные клики: запись массива в модель Polymer панель
        # отрисовывает, но YouTube её НЕ сохраняет (проверено 04.08.2026 —
        # после перезагрузки слотов ноль).
        added, msg = add_ad_slots(driver, seconds_list)
        if not added:
            discard_ad_breaks(driver)
            return False, msg
        # в мастере подтверждение — «Continue», страница не сохраняется отдельно
        click(driver, SEL_ADS["continue_btn"])
        time.sleep(2)
        return True, msg
    except Exception as e:
        return False, f"мидроллы в мастере упали: {str(e)[:80]}"


def discard_ad_breaks(driver):
    """Выйти из редактора мидроллов без сохранения."""
    click(driver, SEL_ADS["back_btn"])
    time.sleep(2)

# Явное пустое состояние списка видео — отличаем «роликов нет» от «не отрисовалось».
# Оба случая дают ноль строк, но у первого Studio показывает свой пустой экран
# с надписью и кнопкой загрузки. Без этого различия первый ролик на НОВЫЙ канал
# залить невозможно: дедуп-гейт справедливо не пускает непрочитанное состояние,
# а состояние у пустого канала не бывает непустым.
_ПУСТО_ТЕКСТ = (
    "no content available", "upload videos",
    "нет доступного контента", "загрузите видео",
    "nema dostupnog sadržaja", "objavite videozapise",          # hr
    "δεν υπάρχει διαθέσιμο περιεχόμενο", "μεταφορτώστε βίντεο",  # el
    "nincs elérhető tartalom", "videók feltöltése",              # hu
    "brak dostępnych treści", "prześlij filmy",                   # pl
    "není k dispozici žádný obsah", "nahrát videa",               # cs
    "nu există conținut disponibil", "încarcă videoclipuri",      # ro
)

_JS_ПУСТО = r"""
function all(root, acc){ acc=acc||[];
  var w=root.querySelectorAll('*');
  for(var i=0;i<w.length;i++){ acc.push(w[i]); if(w[i].shadowRoot) all(w[i].shadowRoot, acc); }
  return acc; }
var узлы = all(document);
var строк = document.querySelectorAll('ytcp-video-row').length;
var пусто_узел = false;
for (var i=0;i<узлы.length;i++){
  var e=узлы[i];
  var id=(e.id||'')+' '+((e.className&&e.className.baseVal!==undefined)?'':(e.className||''));
  if (/empty/i.test(id)) { var r=e.getBoundingClientRect(); if(r.width>0&&r.height>0){ пусто_узел=true; break; } }
}
return {строк: строк, пусто_узел: пусто_узел,
        текст: (document.body.innerText||'').slice(0,4000).toLowerCase()};
"""


def список_пуст_подтверждён(driver):
    """True, только если Studio ЯВНО показывает пустой список видео.

    Требуем совпадения двух признаков сразу: ноль строк-роликов и либо видимый
    узел пустого состояния, либо его надпись на языке интерфейса. Одного нуля мало —
    именно он и означает «ещё не отрисовалось».
    """
    try:
        d = driver.execute_script(_JS_ПУСТО) or {}
    except Exception as e:
        logger.warning("список_пуст_подтверждён: страницу опросить не вышло (%s)", e)
        return False
    if d.get("строк"):
        return False
    т = d.get("текст") or ""
    по_тексту = any(ф in т for ф in _ПУСТО_ТЕКСТ)
    ок = bool(d.get("пусто_узел") or по_тексту)
    logger.info("список_пуст_подтверждён: строк=%s узел=%s текст=%s → %s",
                d.get("строк"), d.get("пусто_узел"), по_тексту, ок)
    return ок
