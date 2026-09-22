/**
 * ВВ2 · меню «ВВ2» в Google-таблице.
 * Установка: Расширения → Apps Script → удалить всё в Code.gs → вставить этот файл → Сохранить → обновить таблицу.
 * Первый запуск любого пункта попросит разрешение Google — это доступ скрипта к этой же таблице.
 *
 * Логика совпадает с сервером (~/ops/ww2_sheet.py): коды К-NNN, лист канала — копия скрытого «Шаблон канала».
 * Колонки ищутся по заголовкам, поэтому новые колонки в конце листов меню не ломают.
 */
var CH = 'Каналы', TPL = 'Шаблон канала', QUEUE = 'Очередь', MAILS = 'Почты', PROFILES = 'Профили';
var LANGS = ['Польский', 'Чешский', 'Румынский', 'Венгерский', 'Хорватский', 'Греческий', 'Украинский', 'Испанский', 'Португальский'];
var SERIES = ['Письма с фронта', 'Сталь и пепел', 'Неизвестная война', 'Последнее убежище', 'Невероятные истории', 'Жёсткая сторона', 'Дневники и письма',
              'Великие битвы', 'Генералы и приказы', 'Разведка и диверсии', 'Техника войны', 'После войны'];
var MY_STATUS = ['Куплен', 'Льётся', 'Пауза', 'На монетизации', 'Монетизирован', 'Демонетизирован', 'Выведен', 'Запланирован'];
var MACHINES = ['Mac', 'Минский бокс', 'Lenovo'];
var QUEUE_STATUS = ['Запланировано', 'Текст готов', 'Рендер', 'Готово к заливке', 'Залито', 'Отменено'];

function onOpen() {
  SpreadsheetApp.getUi().createMenu('ВВ2')
    .addItem('Новый канал…', 'showNewChannel')
    .addItem('Заливка в очередь…', 'showNewUpload')
    .addSeparator()
    .addItem('Открыть лист канала из выделенной строки', 'openSelectedChannel')
    .addItem('Создать недостающие листы сейчас', 'createMissingSheets')
    .addToUi();
}

// ---------- общее ----------
function headers_(sh) {
  var h = sh.getRange(1, 1, 1, sh.getLastColumn()).getDisplayValues()[0], m = {};
  h.forEach(function (v, i) { v = String(v).trim(); if (v) m[v] = i + 1; });
  return m;
}
function col_(m, name, sheetName) {
  if (!m[name]) throw new Error('На листе «' + (sheetName || CH) + '» нет колонки «' + name + '»');
  return m[name];
}
function esc_(s) {
  return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
function options_(list, selected) {
  return list.map(function (v) {
    var val = typeof v === 'object' ? v.value : v, label = typeof v === 'object' ? v.label : v;
    return '<option value="' + esc_(val) + '"' + (val === selected ? ' selected' : '') + '>' + esc_(label) + '</option>';
  }).join('');
}
function safeTitle_(ss, name, ignoreId) {
  var t = String(name).replace(/[\[\]\*\?\/\\:']/g, ' ').trim().slice(0, 90) || 'Канал';
  var taken = {};
  ss.getSheets().forEach(function (s) { if (s.getSheetId() !== ignoreId) taken[s.getName()] = true; });
  var base = t, k = 2;
  while (taken[t]) { t = base + ' ' + k; k++; }
  return t;
}
function channelData_(sh) {
  var last = sh.getLastRow();
  if (last < 2) return [];
  return sh.getRange(2, 1, last - 1, sh.getLastColumn()).getDisplayValues();
}
function gidFromFormula_(f) {
  var m = /#gid=(\d+)/.exec(f || '');
  return m ? Number(m[1]) : null;
}
function sheetById_(ss, id) {
  var found = null;
  ss.getSheets().forEach(function (s) { if (s.getSheetId() === id) found = s; });
  return found;
}
function nextCode_(data, cCode) {
  var max = 0;
  data.forEach(function (r) { var mm = /^К-(\d+)/.exec(r[cCode - 1]); if (mm) max = Math.max(max, Number(mm[1])); });
  return 'К-' + ('000' + (max + 1)).slice(-3);
}
function toDateText_(iso) {                       // 2026-09-16 → 16.09.2026 (таблица в ru-локали сама делает дату)
  var p = String(iso || '').split('-');
  return p.length === 3 ? p[2] + '.' + p[1] + '.' + p[0] : '';
}

function makeChannelSheet_(ss, code, name, extra) {
  var tpl = ss.getSheetByName(TPL);
  if (!tpl) throw new Error('Нет скрытого листа «' + TPL + '». Сервер создаёт его сам в течение 10 минут — попробуйте чуть позже.');
  var ws = tpl.copyTo(ss);
  ws.setName(safeTitle_(ss, name, ws.getSheetId()));
  ws.showSheet();
  ws.getRange('B2').setValue(code);
  ws.getRange('B23:B25').setValues([[extra.narrator || ''], [extra.about || ''], [extra.desc || '']]);
  ws.getRange('B2:B22').protect().setDescription('Карточка подтягивается из «Каналов» — меняйте там').setWarningOnly(true);
  // те же защиты, что ставит сервер (ww2_pool.channel_protections): слева пул, справа ролики с YouTube
  ws.getRange('A27:I').protect().setDescription('Пул считается сам. Взять оригинал в работу — впишите его ссылку в «Очередь»').setWarningOnly(true);
  ws.getRange('D2:E11').protect().setDescription('Сводка пула считается сама').setWarningOnly(true);
  ws.getRange('K27:P').protect().setDescription('Ролики обновляются с YouTube — своя пометка в колонке «Моя пометка»').setWarningOnly(true);
  ws.getRange('R27:R').protect().setDescription('Оригинал подтягивается из «Очереди» по ссылке на наш ролик').setWarningOnly(true);
  ss.setActiveSheet(ws);
  ss.moveActiveSheet(ss.getNumSheets());
  return ws;
}

// ---------- Новый канал ----------
function showNewChannel() {
  var ss = SpreadsheetApp.getActive();
  var freeMails = [], freeProfiles = [];
  var ms = ss.getSheetByName(MAILS);
  if (ms && ms.getLastRow() > 1) {
    var mm = headers_(ms);
    ms.getRange(2, 1, ms.getLastRow() - 1, ms.getLastColumn()).getDisplayValues().forEach(function (r) {
      if (r[col_(mm, 'Почта', MAILS) - 1] && r[col_(mm, 'Статус', MAILS) - 1] === 'свободна') freeMails.push(r[col_(mm, 'Почта', MAILS) - 1]);
    });
  }
  var ps = ss.getSheetByName(PROFILES);
  if (ps && ps.getLastRow() > 1) {
    var pm = headers_(ps);
    ps.getRange(2, 1, ps.getLastRow() - 1, ps.getLastColumn()).getDisplayValues().forEach(function (r) {
      var id = r[col_(pm, '№ профиля', PROFILES) - 1], name = r[col_(pm, 'Имя в AdsPower', PROFILES) - 1];
      if (id && r[col_(pm, 'Статус', PROFILES) - 1] === 'свободен') freeProfiles.push({ value: id, label: id + (name ? ' · ' + name : '') });
    });
  }
  var today = Utilities.formatDate(new Date(), ss.getSpreadsheetTimeZone(), 'yyyy-MM-dd');
  var html =
    '<style>body{font:13px Arial;margin:0;padding:12px 16px;color:#222}label{display:block;margin:9px 0 3px;font-weight:bold}' +
    'input,select,textarea{width:100%;box-sizing:border-box;padding:6px;font:13px Arial;border:1px solid #bbb;border-radius:4px}' +
    '.row{display:flex;gap:10px}.row>div{flex:1}.hint{color:#777;font-weight:normal}#err{color:#b00020;margin-top:10px}' +
    'button{margin-top:14px;padding:8px 18px;font:bold 13px Arial;border:0;border-radius:4px;background:#1a73e8;color:#fff;cursor:pointer}' +
    'button[disabled]{background:#9bbcf0}.chk{display:flex;gap:6px;align-items:center;margin-top:10px;font-weight:normal}.chk input{width:auto}</style>' +
    '<label>Название канала *</label><input id="name" autofocus>' +
    '<div class="row"><div><label>Язык *</label><select id="lang">' + options_(LANGS) + '</select></div>' +
    '<div><label>Серия</label><select id="series"><option value=""></option>' + options_(SERIES) + '</select></div></div>' +
    '<div class="row"><div><label>Когда заведён</label><input id="started" type="date" value="' + today + '"></div>' +
    '<div><label>Статус</label><select id="status">' + options_(MY_STATUS, 'Куплен') + '</select></div></div>' +
    '<label>Почта <span class="hint">— свободные из «Почт» или впишите свою</span></label>' +
    '<input id="email" list="mails"><datalist id="mails">' + options_(freeMails) + '</datalist>' +
    '<div class="row"><div><label>Профиль AdsPower <span class="hint">— свободные</span></label>' +
    '<input id="profile" list="profiles"><datalist id="profiles">' + options_(freeProfiles) + '</datalist></div>' +
    '<div><label>Машина</label><select id="machine">' + options_(MACHINES, 'Mac') + '</select></div></div>' +
    '<label>UC канала <span class="hint">— если уже создан на YouTube (youtube.com/channel/UC…)</span></label><input id="uc" placeholder="UC…">' +
    '<label>Диктор</label><input id="narrator">' +
    '<label>О чём канал</label><textarea id="about" rows="2"></textarea>' +
    '<label>Заметка</label><input id="note">' +
    '<label class="chk"><input type="checkbox" id="useSlot" checked> занять запланированное место «(новый) …» этого языка, если оно есть</label>' +
    '<div id="err"></div><button id="go" onclick="go()">Создать канал и лист</button>' +
    '<script>function v(id){return document.getElementById(id).value.trim()}' +
    'function go(){var b=document.getElementById("go");document.getElementById("err").textContent="";' +
    'if(!v("name")){document.getElementById("err").textContent="Впишите название канала";return}' +
    'var u=v("uc");if(u&&!/^UC[\\w-]{22}$/.test(u)){document.getElementById("err").textContent="UC должен выглядеть как UC и ещё 22 символа";return}' +
    'b.disabled=true;b.textContent="Создаю…";' +
    'google.script.run.withSuccessHandler(function(){google.script.host.close()})' +
    '.withFailureHandler(function(e){b.disabled=false;b.textContent="Создать канал и лист";document.getElementById("err").textContent=e.message})' +
    '.createChannel({name:v("name"),lang:v("lang"),series:v("series"),started:v("started"),status:v("status"),email:v("email"),' +
    'profile:v("profile"),machine:v("machine"),uc:u,narrator:v("narrator"),about:v("about"),note:v("note"),useSlot:document.getElementById("useSlot").checked})}</script>';
  SpreadsheetApp.getUi().showModalDialog(HtmlService.createHtmlOutput(html).setWidth(520).setHeight(640), 'Новый канал ВВ2');
}

function createChannel(d) {
  var lock = LockService.getDocumentLock();
  lock.waitLock(30000);
  try {
    var ss = SpreadsheetApp.getActive(), sh = ss.getSheetByName(CH), m = headers_(sh);
    var name = String(d.name || '').trim();
    if (!name) throw new Error('Впишите название канала');
    var cCode = col_(m, 'Код'), cName = col_(m, 'Название канала'), cLang = col_(m, 'Язык'), cStatus = col_(m, 'Статус (мой)'), cUC = col_(m, 'UC канала');
    var data = channelData_(sh);
    data.forEach(function (r) {
      if (r[cName - 1].trim().toLowerCase() === name.toLowerCase()) throw new Error('Канал «' + name + '» уже есть в списке');
      if (d.uc && r[cUC - 1].trim() === d.uc) throw new Error('Этот UC уже записан у канала «' + r[cName - 1] + '»');
    });
    var row = -1, code = '';
    if (d.useSlot) {
      for (var i = 0; i < data.length; i++) {
        if (data[i][cStatus - 1] === 'Запланирован' && data[i][cLang - 1] === d.lang && /^\(новый\)/.test(data[i][cName - 1])) {
          row = i + 2; code = data[i][cCode - 1]; break;
        }
      }
    }
    if (row < 0) {
      var lastUsed = 1;
      data.forEach(function (r, i) { if (r[cName - 1] || r[cCode - 1]) lastUsed = i + 2; });
      row = lastUsed + 1;
    }
    if (!code) code = nextCode_(data, cCode);
    var put = function (h, v) { if (m[h]) sh.getRange(row, m[h]).setValue(v); };
    put('Код', code);
    put('Название канала', name);
    put('Язык', d.lang);
    put('Серия', d.series || '');
    put('Когда заведён', toDateText_(d.started));
    put('Статус (мой)', d.status || 'Куплен');
    put('Почта', d.email || '');
    put('Профиль', /^\d+$/.test(d.profile || '') ? Number(d.profile) : (d.profile || ''));
    put('Машина', d.machine || 'Mac');
    put('Монетизация', 'Нет');
    put('Заметка', d.note || '');
    put('UC канала', d.uc || '');
    var ws = makeChannelSheet_(ss, code, name, d);
    sh.getRange(row, col_(m, 'Лист')).setFormula('=HYPERLINK("#gid=' + ws.getSheetId() + '","открыть")');
    SpreadsheetApp.flush();
    ss.toast('Код ' + code + '. Статус и «Внимание» обновятся в течение 10 минут, цифры с YouTube — ночью.', 'Канал «' + name + '» создан', 8);
  } finally {
    lock.releaseLock();
  }
}

// ---------- Заливка в очередь ----------
function showNewUpload() {
  var ss = SpreadsheetApp.getActive(), sh = ss.getSheetByName(CH), m = headers_(sh);
  var cName = col_(m, 'Название канала'), cStatus = col_(m, 'Статус (мой)');
  var names = channelData_(sh).filter(function (r) {
    return r[cName - 1] && r[cStatus - 1] !== 'Запланирован' && r[cStatus - 1] !== 'Выведен';
  }).map(function (r) { return r[cName - 1]; }).sort();
  var selected = '';
  var active = ss.getActiveSheet();
  if (active.getName() === CH && active.getActiveRange().getRow() > 1) selected = active.getRange(active.getActiveRange().getRow(), cName).getDisplayValue();
  var today = Utilities.formatDate(new Date(), ss.getSpreadsheetTimeZone(), 'yyyy-MM-dd');
  var html =
    '<style>body{font:13px Arial;margin:0;padding:12px 16px;color:#222}label{display:block;margin:9px 0 3px;font-weight:bold}' +
    'input,select,textarea{width:100%;box-sizing:border-box;padding:6px;font:13px Arial;border:1px solid #bbb;border-radius:4px}' +
    '.row{display:flex;gap:10px}.row>div{flex:1}#err{color:#b00020;margin-top:10px}' +
    'button{margin-top:14px;padding:8px 18px;font:bold 13px Arial;border:0;border-radius:4px;background:#1a73e8;color:#fff;cursor:pointer}button[disabled]{background:#9bbcf0}</style>' +
    '<div class="row"><div><label>Дата</label><input id="date" type="date" value="' + today + '"></div>' +
    '<div><label>Статус</label><select id="status">' + options_(QUEUE_STATUS, 'Запланировано') + '</select></div></div>' +
    '<label>Канал *</label><select id="channel"><option value=""></option>' + options_(names, selected) + '</select>' +
    '<label>Тема по-русски</label><input id="topic">' +
    '<label>Донор (ссылка или название)</label><input id="donor">' +
    '<div class="row"><div><label>Кто делает</label><input id="who"></div><div><label>Готовое видео / ссылка</label><input id="link"></div></div>' +
    '<label>Заметка</label><input id="note">' +
    '<div id="err"></div><button id="go" onclick="go()">Добавить в очередь</button>' +
    '<script>function v(id){return document.getElementById(id).value.trim()}' +
    'function go(){var b=document.getElementById("go");if(!v("channel")){document.getElementById("err").textContent="Выберите канал";return}' +
    'b.disabled=true;b.textContent="Добавляю…";google.script.run.withSuccessHandler(function(){google.script.host.close()})' +
    '.withFailureHandler(function(e){b.disabled=false;b.textContent="Добавить в очередь";document.getElementById("err").textContent=e.message})' +
    '.addUpload({date:v("date"),status:v("status"),channel:v("channel"),topic:v("topic"),donor:v("donor"),who:v("who"),link:v("link"),note:v("note")})}</script>';
  SpreadsheetApp.getUi().showModalDialog(HtmlService.createHtmlOutput(html).setWidth(480).setHeight(470), 'Заливка в очередь');
}

function addUpload(d) {
  var lock = LockService.getDocumentLock();
  lock.waitLock(30000);
  try {
    var ss = SpreadsheetApp.getActive(), q = ss.getSheetByName(QUEUE), m = headers_(q);
    var cChan = col_(m, 'Канал', QUEUE), last = q.getLastRow(), row = 2;
    if (last >= 2) {
      var vals = q.getRange(2, cChan, last - 1, 1).getDisplayValues();
      for (var i = vals.length - 1; i >= 0; i--) { if (vals[i][0]) { row = i + 3; break; } }
    }
    var put = function (h, v) { if (m[h]) q.getRange(row, m[h]).setValue(v); };   // «Проверка» не трогаем — там формула
    put('Дата', toDateText_(d.date));
    put('Канал', d.channel);
    put('Тема по-русски', d.topic || '');
    put('Донор (ссылка или название)', d.donor || '');
    put('Статус', d.status || 'Запланировано');
    put('Кто делает', d.who || '');
    put('Готовое видео / ссылка', d.link || '');
    put('Заметка', d.note || '');
    SpreadsheetApp.flush();
    var check = m['Проверка'] ? q.getRange(row, m['Проверка']).getDisplayValue() : '';
    ss.toast(check ? 'Проверка: ' + check : 'Проверка пройдена', 'Добавлено в очередь, строка ' + row, 8);
  } finally {
    lock.releaseLock();
  }
}

// ---------- переходы и досоздание ----------
function openSelectedChannel() {
  var ss = SpreadsheetApp.getActive(), active = ss.getActiveSheet(), row = active.getActiveRange().getRow();
  var sh = ss.getSheetByName(CH), m = headers_(sh), name = '';
  if (active.getName() === CH) {
    if (row < 2) return ss.toast('Выделите строку канала', 'ВВ2', 5);
    name = sh.getRange(row, col_(m, 'Название канала')).getDisplayValue();
  } else if (active.getName() === QUEUE) {
    var qm = headers_(active);
    if (row < 2) return ss.toast('Выделите строку заливки', 'ВВ2', 5);
    name = active.getRange(row, col_(qm, 'Канал', QUEUE)).getDisplayValue();
  } else {
    return ss.toast('Выделите строку на листе «Каналы» или «Очередь»', 'ВВ2', 6);
  }
  var data = channelData_(sh), cName = col_(m, 'Название канала'), cLink = col_(m, 'Лист');
  for (var i = 0; i < data.length; i++) {
    if (data[i][cName - 1] === name) {
      var ws = sheetById_(ss, gidFromFormula_(sh.getRange(i + 2, cLink).getFormula()));
      if (ws) { ss.setActiveSheet(ws); return; }
      return ss.toast('У канала «' + name + '» ещё нет листа. Меню ВВ2 → «Создать недостающие листы сейчас».', 'ВВ2', 8);
    }
  }
  ss.toast('Канал «' + name + '» не найден в «Каналах»', 'ВВ2', 6);
}

function createMissingSheets() {
  var lock = LockService.getDocumentLock();
  lock.waitLock(30000);
  try {
    var ss = SpreadsheetApp.getActive(), sh = ss.getSheetByName(CH), m = headers_(sh);
    var cCode = col_(m, 'Код'), cName = col_(m, 'Название канала'), cStatus = col_(m, 'Статус (мой)'), cUC = col_(m, 'UC канала'), cLink = col_(m, 'Лист');
    var data = channelData_(sh), created = 0, renamed = 0, coded = 0;
    for (var i = 0; i < data.length; i++) {
      var r = data[i], row = i + 2, name = r[cName - 1].trim();
      if (!name && !r[cUC - 1]) continue;
      var code = r[cCode - 1];
      if (!code) { code = nextCode_(data, cCode); data[i][cCode - 1] = code; sh.getRange(row, cCode).setValue(code); coded++; }
      if (!name || (r[cStatus - 1] === 'Запланирован' || r[cStatus - 1] === '') && !r[cUC - 1]) continue;
      var ws = sheetById_(ss, gidFromFormula_(sh.getRange(row, cLink).getFormula()));
      if (!ws) {
        ws = makeChannelSheet_(ss, code, name, {});
        sh.getRange(row, cLink).setFormula('=HYPERLINK("#gid=' + ws.getSheetId() + '","открыть")');
        created++;
      } else if (ws.getName() !== safeTitle_(ss, name, ws.getSheetId())) {
        ws.setName(safeTitle_(ss, name, ws.getSheetId()));
        renamed++;
      }
    }
    ss.setActiveSheet(sh);
    ss.toast('Новых кодов: ' + coded + ', новых листов: ' + created + ', переименовано вкладок: ' + renamed, 'ВВ2', 8);
  } finally {
    lock.releaseLock();
  }
}
