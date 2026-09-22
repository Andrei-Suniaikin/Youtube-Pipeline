#!/usr/bin/env python3
"""ВВ2 · пул оригиналов для рерайта. Подключается из ww2_sheet.py.

Что здесь:
  ensure_structure(bk)   каждые 10 минут: вкладки «Конкуренты» и «Оригиналы», служебные колонки «Очереди»,
                         колонка «Свободно в пуле» в «Каналах» — создаёт недостающее, существующее не трогает
  channel_layout(...)    раскладка листа канала: слева пул (формулы), справа ролики с YouTube
  pool_sync(bk)          ночью: новые ролики конкурентов → разбор по сериям → «Оригиналы»; просмотры и корзины

Пул на листе канала — живые формулы: берёт из «Оригиналов» ролики под серию канала, от средних просмотров
к высоким, а статус каждой строки считает из «Очереди» по ссылке на оригинал. Ручных данных в пуле нет,
поэтому его можно перестраивать в любой момент без риска что-то потерять.
"""
import fcntl, http.client, json, os, re, statistics, time, datetime as dt, urllib.parse, urllib.request, urllib.error
from pathlib import Path

KONK, ORIG, QUEUE = "Конкуренты", "Оригиналы", "Очередь"
host = None                      # модуль ww2_sheet: retry, L, IX, ROWS, iso_dur — выставляет он сам при загрузке
HERE = Path(__file__).resolve().parent
STATE_FILE = Path(os.environ.get("WW2_POOL_STATE", HERE / "pool_state.json"))
KEYS_FILE = os.environ.get("WW2_KEYS_FILE", "/root/video_studio_api_keys.json" if Path("/root").is_dir() and os.access("/root", os.W_OK)
                           else str(Path.home() / "video_studio_api_keys.json"))
LLM_MODEL = os.environ.get("WW2_POOL_MODEL", "gpt-4.1")
MAX_DONOR_VIDEOS = 4000
FULL_REFRESH_DAYS = 6

# ---- колонки ----
KONK_FIELDS = [  # ключ, заголовок, ручная
    ("name", "Конкурент", True), ("lang", "Язык", True), ("link", "Ссылка на канал", True), ("on", "Берём", True),
    ("note", "Заметка", True), ("uc", "UC", False), ("total", "Роликов на канале", False),
    ("ww2", "Длинных про ВВ2", False), ("median", "Медиана просмотров длинных", False),
    ("in_pool", "Годятся в пул (средне и много)", False), ("updated", "Обновлено", False), ("state", "Состояние", False),
]
# Настройки — в СТРОКЕ ЗАГОЛОВКА «Конкурентов» (N1:S1: подпись, значение ×3): сортировка и удаление строк
# конкурентов их не задевают. Формулы ссылаются на именованные диапазоны, а не на адреса.
SETTINGS = [("min_len", "Мин. длина оригинала, мин →", 20, "pool_min_len"), ("mid", "Порог «средне», доля медианы →", 0.5, "pool_mid"),
            ("top", "Порог «много», доля медианы →", 2, "pool_top"), ("min_views", "Мин. просмотров после разгона (для всех каналов) →", 300000, "pool_min_views"),
            ("size", "Свободных в пуле канала →", 60, "pool_size"), ("warmup", "Разгон: первых роликов ниже порога →", 10, "pool_warmup")]
SETTINGS_COL0 = 13       # N (0-based): подпись, справа значение
ORIG_FIELDS = [
    ("id", "ID", False), ("donor", "Конкурент", False), ("lang", "Язык", False), ("title", "Название оригинала", False),
    ("url", "Ссылка", False), ("date", "Вышел", False), ("min", "Длина, мин", False), ("views", "Просмотры", False),
    ("ratio", "× медианы", False), ("basket", "Корзина", False), ("series", "Подходит для серий", True),
    ("skip", "Не брать", True), ("taken", "Взят на каналах", False), ("note", "Заметка", True), ("added", "Добавлен", False),
]
OX = {k: i for i, (k, _, _) in enumerate(ORIG_FIELDS)}
KX = {k: i for i, (k, _, _) in enumerate(KONK_FIELDS)}
QUEUE_EXTRA = ["Название оригинала", "ID оригинала", "Язык канала"]      # J, K, L
POOL_FREE_HEADER = "Свободно в пуле"

# ---- лист канала ----
POOL_HDR_ROW = 27                     # строка заголовков обеих таблиц (1-based)
POOL_COLS = ["Статус", "Оригинал (клик — открыть)", "Просмотры", "Корзина", "Конкурент", "Длина, мин", "Ссылка на оригинал", "Вышел", "Наш ролик"]
VID_COL = 11                          # K: первая колонка таблицы роликов с YouTube (1-based)
VID_COLS = ["Дата", "Название", "Длительность", "Просмотры", "Ссылка", "Тема", "Моя пометка", "Оригинал"]
CARD_COLS = 18                        # A..R
CARD_ROWS_MIN = 1200

# ---- серии: что в них идёт (для разбора роликов) ----
SERIES_ABOUT = {
    "Письма с фронта": "голос конкретного человека времён войны: письма, дневник, записные книжки, мемуары, воспоминания ветерана, показания от первого лица; «глазами солдата» по его записям. Не подходит общий обзор без личного источника.",
    "Дневники и письма": "то же, что «Письма с фронта»: письма, дневники, воспоминания одного человека, один голос от первой записи до последней.",
    "Сталь и пепел": "жестокая реальность фронта на уровне солдата и подразделения: конкретный боец, снайпер, штурмовая группа, дивизия (в т.ч. СС), каратели, окопы, выживание, плен, «насколько это было ужасно», без героизации. Не подходит: техника как предмет, большая стратегия, послевоенные суды.",
    "Жёсткая сторона": "то же, что «Сталь и пепел»: война на уровне солдата, один человек или одно подразделение, жестокие подробности, без героизации.",
    "Неизвестная война": "необычный, малоизвестный или парадоксальный сюжет, который звучит выдуманным: «немцы/японцы не знали, что…», неожиданная реакция противника, странное изобретение, абсурдное решение, логистика, курьёзы, взгляд с неожиданной стороны. Не подходит стандартный хронологический обзор.",
    "Невероятные истории": "то же, что «Неизвестная война»: истории Второй мировой, которые звучат выдуманными, но случились; парадоксы, странные решения, взгляд противника.",
    "Последнее убежище": "конкретные объекты и тайники: бункеры, ставки (Волчье логово), подземные заводы и штольни, секретные базы, замки и резиденции, пропавшее золото и сокровища Рейха, Янтарная комната, что нашли в объекте.",
    "Великие битвы": "ролик целиком про одну крупную битву, операцию или кампанию (Сталинград, Курск, Москва, Ленинград, Нормандия, Берлин, Мидуэй, Иводзима, Эль-Аламейн, Арденны, Монте-Кассино, Крит, Барбаросса) — от планов до боёв.",
    "Генералы и приказы": "военачальники и решения: биография или судьба генерала, маршала, адмирала (Жуков, Роммель, Паттон, Манштейн, Гудериан, Модель, Монтгомери, Ямамото), штабные решения, приказы и их цена, ошибки командования, стратегические решения Гитлера или Сталина как главнокомандующих.",
    "Разведка и диверсии": "шпионаж, разведка и контрразведка, диверсанты, спецоперации, коммандос, партизанские рейды, SOE/OSS/СМЕРШ/Абвер, шифры и их взлом (Энигма), двойные агенты, покушения, операции под прикрытием.",
    "Техника войны": "танки, самолёты, корабли, подлодки, артиллерия, стрелковое оружие, ракеты (Фау), «чудо-оружие»: создание, характеристики в бою, сравнения, почему подводили. Если сюжет про технику через людей — всё равно техника.",
    "После войны": "события после окончания войны: суды (Нюрнберг, Токио), бегство нацистов (крысиные тропы, Аргентина), судьбы солдат, генералов и пленных после 1945, оккупация и денацификация, послевоенные находки и расследования, что стало с участниками.",
}

# примеры разметки для ночного разбора — взяты из перепроверенной разметки 17.09.2026
FEW_SHOT = [
    ("MILITARY CLUB", "Temperature from -33° to -37°. Diary of a German Soldier. Eastern front.", True, ["Письма с фронта", "Дневники и письма", "Сталь и пепел", "Жёсткая сторона"]),
    ("ВЕК ВОЙН", "ВОСПОМИНАНИЯ АРТИЛЛЕРИСТА ВЕРМАХТА. Алоис Цвайгер. Часть 4", True, ["Письма с фронта", "Дневники и письма", "Сталь и пепел", "Жёсткая сторона"]),
    ("Исторический фронт", "Они вошли в СТАЛИНГРАД — и не ВЫШЛИ. Письма немцев с фронта под Сталинградом", True, ["Письма с фронта", "Дневники и письма", "Сталь и пепел", "Жёсткая сторона"]),
    ("WW2 Records", "How This LITERALLY BLIND Marine Killed 200 Japanese in One Night — Without Ever Seeing Them", True, ["Сталь и пепел", "Жёсткая сторона"]),
    ("The Soldier's Diary", "The Most Ruthless Execution Methods of World War 2", True, ["Сталь и пепел", "Жёсткая сторона"]),
    ("WW2 Tales", "German U-Boat Commander Realized America Built Ships Faster Than They Could Sink Them", True, ["Неизвестная война", "Невероятные истории"]),
    ("WW2 Dossier", "Why German Troops Panicked When American Shells Left No Craters", True, ["Техника войны", "Неизвестная война", "Невероятные истории"]),
    ("Тогда-Сейчас", "После Падения Рейха нашли ЭТО: 15 пугающих открытий (Гитлер скрывал)", True, ["Последнее убежище", "После войны"]),
    ("The Soldier's Diary", "15 Mysterious Discoveries Linked to Nazi Germany in Antarctica", True, ["Последнее убежище"]),
    ("Der Kommandant English", "Operation Bagration | The Death of Army Group Center", True, ["Великие битвы"]),
    ("ЧЕСТНОЕ СЛОВО", "КУРСК. КАК ГИТЛЕР ТРИЖДЫ ПЕРЕНЁС СВОЁ РЕШАЮЩЕЕ НАСТУПЛЕНИЕ", True, ["Генералы и приказы", "Великие битвы"]),
    ("Der Kommandant English", "Who Was the Best German General of World War 2? | Documentary", True, ["Генералы и приказы"]),
    ("WW2 Flavin", "How Nimitz Gambled America's Last Carriers and Stopped Japan at Midway", True, ["Великие битвы", "Генералы и приказы"]),
    ("Тайный Отдел", "СМЕРШ ЗНАЛ ВСЁ. Как контрразведка читала планы Абвера раньше, чем их получали немецкие агенты", True, ["Разведка и диверсии"]),
    ("История Второй Мировой", "Свой против своих: Самая циничная диверсия операции «Багратион»", True, ["Разведка и диверсии"]),
    ("Две войны - военная история", "Пантера против ИС-2: кто на самом деле был СИЛЬНЕЕ?", True, ["Техника войны"]),
    ("Shadows of War", "Inside Tank Ace Knispel's Tiger at Kursk | Over 20 Soviet Tanks Destroyed", True, ["Сталь и пепел", "Жёсткая сторона", "Техника войны"]),
    ("Der Kommandant English", "What happened to Hitler's personal belongings after World War II?", True, ["После войны"]),
    ("The Soldier's Diary", "This Is What Germany Was Like 10 Years After World War 2", True, ["После войны"]),
    ("WW2 Tales", "How America's Industrial Production Helped Win World War 2", True, []),
    ("The Soldier's Diary", "What Would Have Happened if Hitler Had Been Captured by the Soviets? | Berlin 1945", True, []),
    ("Battle Guide", "Le Hamel : The Battle That Broke The Western Front (WW1 Documentary)", False, []),
    ("Historic Weirdos", "What Did Osama Bin Laden Actually Do All Day?", False, []),
    ("Живая История", "Предатель или двойной агент? Исчезновение генерала", False, []),
]

GREEN = {"red": 0.85, "green": 0.94, "blue": 0.86}
AMBER = {"red": 1.0, "green": 0.95, "blue": 0.80}
BLUE = {"red": 0.87, "green": 0.93, "blue": 0.99}
GREY_TXT = {"red": 0.45, "green": 0.45, "blue": 0.45}
MANUAL_HDR = {"red": 0.87, "green": 0.93, "blue": 0.99}
AUTO_HDR = {"red": 0.90, "green": 0.90, "blue": 0.90}


def col_letter(n):          # 1-based
    s = ""
    while n: n, r = divmod(n - 1, 26); s = chr(65 + r) + s
    return s


def vid_id(text):
    m = re.search(r"(?:youtu\.be/|[?&]v=|/shorts/|/live/|/video/)([\w-]{11})", str(text or ""))
    if m: return m.group(1)
    t = str(text or "").strip()
    return t if re.fullmatch(r"[\w-]{11}", t) else ""


def txt(v):
    """Текст для USER_ENTERED всегда с апострофом: Sheets съедает ровно один, и ни «-abc», ни «1945.», ни «'…» не превратятся в формулу/число."""
    return "'" + str(v)


def arr(bk):
    """Разделитель колонок в литерале массива {a\\b}: в ru-локали «\\», в en — «,»."""
    return "\\" if bk.sep == ";" else ","


def to_num(s, default=0.0):
    try: return float(str(s).replace(" ", "").replace(" ", "").replace(",", "."))
    except (TypeError, ValueError): return default


# =====================================================================
#  Структура: вкладки, служебные колонки
# =====================================================================
def _hdr(sid, c0, c1, manual, row=0):
    return {"repeatCell": {"range": {"sheetId": sid, "startRowIndex": row, "endRowIndex": row + 1, "startColumnIndex": c0, "endColumnIndex": c1},
                           "cell": {"userEnteredFormat": {"backgroundColor": MANUAL_HDR if manual else AUTO_HDR, "wrapStrategy": "WRAP",
                                                          "textFormat": {"bold": True}, "verticalAlignment": "MIDDLE"}},
                           "fields": "userEnteredFormat(backgroundColor,textFormat,wrapStrategy,verticalAlignment)"}}


def _width(sid, i, px):
    return {"updateDimensionProperties": {"range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": i, "endIndex": i + 1},
                                          "properties": {"pixelSize": px}, "fields": "pixelSize"}}


def _rng(sid, c0, c1, r0, r1):
    """r1=None — до конца листа, сколько бы строк ни добавилось потом."""
    g = {"sheetId": sid, "startRowIndex": r0, "startColumnIndex": c0, "endColumnIndex": c1}
    if r1 is not None: g["endRowIndex"] = r1
    return g


def _fmt(sid, c0, c1, r0, r1, fmt, fields):
    return {"repeatCell": {"range": _rng(sid, c0, c1, r0, r1), "cell": {"userEnteredFormat": fmt}, "fields": fields}}


def _date(sid, c, r0, r1):
    return _fmt(sid, c, c + 1, r0, r1, {"numberFormat": {"type": "DATE", "pattern": "dd.mm.yyyy"}}, "userEnteredFormat.numberFormat")


def _num(sid, c, r0, r1, pattern="#,##0"):
    return _fmt(sid, c, c + 1, r0, r1, {"numberFormat": {"type": "NUMBER", "pattern": pattern}}, "userEnteredFormat.numberFormat")


def _protect(sid, desc, c0, c1, r0, r1):
    return {"addProtectedRange": {"protectedRange": {"range": _rng(sid, c0, c1, r0, r1), "description": desc, "warningOnly": True}}}


def _cond(bk, sid, c0, c1, r0, r1, formula, bg=None, fg=None, bold=False):
    fmt = {}
    if bg: fmt["backgroundColor"] = bg
    if fg or bold: fmt["textFormat"] = {**({"foregroundColor": fg} if fg else {}), **({"bold": True} if bold else {})}
    return {"addConditionalFormatRule": {"rule": {
        "ranges": [_rng(sid, c0, c1, r0, r1)],
        "booleanRule": {"condition": {"type": "CUSTOM_FORMULA", "values": [{"userEnteredValue": formula.replace(";", bk.sep)}]}, "format": fmt}},
        "index": 0}}


def ensure_structure(bk, log):
    """Дешёвая проверка раз в 10 минут: создать недостающее, существующее не трогать."""
    retry = host.retry
    made = []
    # порядок важен: колонки «Очереди» появляются ДО формул, которые на них ссылаются, —
    # иначе Sheets сдвигает ссылку за край листа (K превращалась в N)
    q = bk.sheets.get(QUEUE)
    if q is not None:
        cols = queue_service_cols(bk, q)
        if cols is None:
            got = retry(q.get, "A1:L1")
            hdr = (got[0] if got else []) + [""] * 12
            # создаём только на «родных» местах; если там чужие заголовки — ничего не пишем, только лог
            if hdr[7] in ("Проверка", "") and all(h in ("", e) for h, e in zip(hdr[9:12], QUEUE_EXTRA)):
                _queue_columns(bk, q); made.append("колонки «Очереди»")
                bk.reload(); q = bk.sheets.get(QUEUE)
            else:
                log("пул: в «Очереди» не нашлись служебные колонки, а на их местах чужие заголовки — не трогаю, нужна ручная проверка")
    ch = bk.sheets.get("Каналы")
    if ch is not None:
        L = host.L
        cell = retry(ch.get, f"{L('pool_free')}1") if ch.col_count > host.IX["pool_free"] else []
        if not cell or not cell[0] or cell[0][0] != POOL_FREE_HEADER:
            _channels_column(bk, ch); made.append("колонка «Свободно в пуле»")
    new_tabs = []
    if KONK not in bk.sheets:
        _create_konk(bk); new_tabs.append(KONK)
    if ORIG not in bk.sheets:
        _create_orig(bk); new_tabs.append(ORIG)
    if new_tabs:        # новые вкладки — сразу после «Очереди», а не в хвосте за листами каналов
        base = bk.sheets[QUEUE].index + 1 if QUEUE in bk.sheets else 0
        bk.batch([{"updateSheetProperties": {"properties": {"sheetId": bk.sheets[t].id, "index": base + i}, "fields": "index"}}
                  for i, t in enumerate(new_tabs)])
        made += new_tabs
    cols = queue_service_cols(bk, q) if q is not None else None
    if cols and cols.get("Проверка") == "H" and [cols.get(x) for x in QUEUE_EXTRA] == ["J", "K", "L"]:
        h1 = retry(q.get, "H1", value_render_option="FORMULA")
        if h1 and h1[0] and str(h1[0][0]) != queue_check_formula(bk):
            bk.batch_values([{"range": f"'{QUEUE}'!H1", "values": [[queue_check_formula(bk)]]}])
            made.append("формула «Проверки»")
    ows = bk.sheets.get(ORIG)
    if ows is not None and cols:
        m1 = retry(ows.get, f"{col_letter(OX['taken'] + 1)}1", value_render_option="FORMULA")
        want = f"'{QUEUE}'!{cols['ID оригинала']}2:{cols['ID оригинала']}"
        if not m1 or not m1[0] or want not in str(m1[0][0]):
            bk.batch_values([{"range": f"'{ORIG}'!{col_letter(OX['taken'] + 1)}1", "values": [[taken_formula(bk, cols)]]}])
            made.append("формула «Взят на каналах»")
    kws = bk.sheets.get(KONK)
    names = {n["name"] for n in retry(bk.sh.fetch_sheet_metadata).get("namedRanges", [])} if kws is not None else set()
    if kws is not None and SETTINGS[0][3] in names and any(x[3] not in names for x in SETTINGS):
        bk.batch(settings_requests(bk, kws, read_settings(bk, log)))       # появилась новая настройка — дописать, старые значения сохранить
        made.append("новая настройка пула")
    if kws is not None and SETTINGS[0][3] not in names:
        old = retry(kws.get, "N2:O4")          # настройки первой версии жили в N2:O4
        vals = {}
        for (k, _, d, _), r in zip(SETTINGS, old + [[]] * len(SETTINGS)):
            vals[k] = to_num(r[1], d) if len(r) > 1 and str(r[1]).strip() else d
        bk.batch([{"updateCells": {"range": {"sheetId": kws.id, "startRowIndex": 1, "endRowIndex": 4, "startColumnIndex": 13, "endColumnIndex": 15},
                                   "fields": "userEnteredValue,userEnteredFormat"}}] + settings_requests(bk, kws, vals))
        made.append("настройки пула → строка заголовка «Конкурентов»")
    if made:
        bk.reload(); log("пул: создано/починено —", ", ".join(made))


def _create_konk(bk):
    ws = bk.add_sheet(KONK, rows=300, cols=16)
    sid = ws.id
    vals = [[h for _, h, _ in KONK_FIELDS]]
    bk.batch_values([{"range": f"'{KONK}'!A1:{col_letter(len(vals[0]))}1", "values": vals}])
    req = [_hdr(sid, i, i + 1, m) for i, (_, _, m) in enumerate(KONK_FIELDS)]
    req += settings_requests(bk, ws)
    req += [
            {"updateSheetProperties": {"properties": {"sheetId": sid, "gridProperties": {"frozenRowCount": 1, "frozenColumnCount": 1}},
                                       "fields": "gridProperties.frozenRowCount,gridProperties.frozenColumnCount"}},
            {"setDataValidation": {"range": {"sheetId": sid, "startRowIndex": 1, "endRowIndex": 300, "startColumnIndex": KX["lang"], "endColumnIndex": KX["lang"] + 1},
                                   "rule": {"condition": {"type": "ONE_OF_LIST", "values": [{"userEnteredValue": "EN"}, {"userEnteredValue": "RU"}]}, "showCustomUi": True}}},
            {"setDataValidation": {"range": {"sheetId": sid, "startRowIndex": 1, "endRowIndex": 300, "startColumnIndex": KX["on"], "endColumnIndex": KX["on"] + 1},
                                   "rule": {"condition": {"type": "BOOLEAN"}, "showCustomUi": True}}},
            _num(sid, KX["total"], 1, 300), _num(sid, KX["ww2"], 1, 300), _num(sid, KX["median"], 1, 300), _num(sid, KX["in_pool"], 1, 300),
            _date(sid, KX["updated"], 1, 300),
            _protect(sid, "Эти колонки заполняются ночью сами", KX["uc"], len(KONK_FIELDS), 1, 300)]
    req += [_width(sid, i, px) for i, px in enumerate([210, 60, 300, 60, 240, 210, 90, 90, 110, 120, 95, 260, 20, 210, 60, 210, 60, 210, 60, 200, 70, 190, 50, 210, 50])]
    req += [_cond(bk, sid, KX["state"], KX["state"] + 1, 1, 300, f'=AND({col_letter(KX["state"] + 1)}2<>"";{col_letter(KX["state"] + 1)}2<>"ок")', bg=AMBER)]
    bk.batch(req)


def queue_service_cols(bk, q):
    """Где сейчас служебные колонки «Очереди» (их могли сдвинуть вставкой колонки): {заголовок: буква} или None."""
    row = host.retry(q.get, f"A1:{col_letter(q.col_count)}1", value_render_option="FORMULA")
    row = row[0] if row else []
    found = {}
    for i, v in enumerate(row):
        m = re.match(r'=\{"([^"]+)"', str(v))
        if m: found[m.group(1)] = col_letter(i + 1)
    need = ["Проверка"] + QUEUE_EXTRA
    return found if all(n in found for n in need) else None


def taken_formula(bk, cols=None):
    k = (cols or {}).get("ID оригинала", "K")
    e = "E"
    return bk.f(f"={{\"{ORIG_FIELDS[OX['taken']][1]}\";ARRAYFORMULA(IF(A2:A=\"\"¦\"\"¦COUNTIFS('{QUEUE}'!{k}2:{k}¦A2:A¦'{QUEUE}'!{e}2:{e}¦\"<>Отменено\")))}}")


def settings_requests(bk, ws, values=None):
    """Подписи и значения настроек в N1:S1, именованные диапазоны на значения, проверка «только число больше 0»."""
    sid = ws.id
    values = values or {k: d for k, _, d, _ in SETTINGS}
    row = []
    for k, label, d, _ in SETTINGS: row += [label, values.get(k, d)]
    req = []
    if ws.col_count < SETTINGS_COL0 + len(row):
        req.append({"appendDimension": {"sheetId": sid, "dimension": "COLUMNS", "length": SETTINGS_COL0 + len(row) - ws.col_count}})
    req.append({"updateCells": {"start": {"sheetId": sid, "rowIndex": 0, "columnIndex": SETTINGS_COL0}, "fields": "userEnteredValue",
                                "rows": [{"values": [{"userEnteredValue": {"stringValue": v} if isinstance(v, str) else {"numberValue": v}} for v in row]}]}})
    existing = {n["name"]: n for n in host.retry(bk.sh.fetch_sheet_metadata).get("namedRanges", [])}
    for i, (k, label, d, name) in enumerate(SETTINGS):
        c = SETTINGS_COL0 + 2 * i
        rng = {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1, "startColumnIndex": c + 1, "endColumnIndex": c + 2}
        if name in existing:
            req.append({"updateNamedRange": {"namedRange": {"namedRangeId": existing[name]["namedRangeId"], "name": name, "range": rng}, "fields": "range"}})
        else:
            req.append({"addNamedRange": {"namedRange": {"name": name, "range": rng}}})
        req += [_fmt(sid, c, c + 1, 0, 1, {"backgroundColor": AMBER, "textFormat": {"bold": True}, "horizontalAlignment": "RIGHT", "wrapStrategy": "WRAP"},
                     "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,wrapStrategy)"),
                _fmt(sid, c + 1, c + 2, 0, 1, {"backgroundColor": MANUAL_HDR, "textFormat": {"bold": True}}, "userEnteredFormat(backgroundColor,textFormat)"),
                {"setDataValidation": {"range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1, "startColumnIndex": c + 1, "endColumnIndex": c + 2},
                                       "rule": {"condition": {"type": "NUMBER_GREATER", "values": [{"userEnteredValue": "0"}]}, "strict": True,
                                                "inputMessage": "Число больше 0; дробь пишется через запятую: 0,5"}}}]
    return req


def _create_orig(bk):
    ws = bk.add_sheet(ORIG, rows=12000, cols=len(ORIG_FIELDS))
    hdr = [h for _, h, _ in ORIG_FIELDS]
    hdr[OX["taken"]] = taken_formula(bk)
    bk.batch_values([{"range": f"'{ORIG}'!A1:{col_letter(len(ORIG_FIELDS))}1", "values": [hdr]}])
    bk.batch(orig_format_requests(bk, ws.id))


def orig_format_requests(bk, sid):
    """Оформление «Оригиналов» без нижней границы: строки, дописанные ночью, получают галочку, цвета и защиту."""
    rows = None
    req = [_hdr(sid, i, i + 1, m) for i, (_, _, m) in enumerate(ORIG_FIELDS)]
    req += [{"updateSheetProperties": {"properties": {"sheetId": sid, "gridProperties": {"frozenRowCount": 1, "frozenColumnCount": 0}},
                                       "fields": "gridProperties.frozenRowCount,gridProperties.frozenColumnCount"}},
            {"setBasicFilter": {"filter": {"range": _rng(sid, 0, len(ORIG_FIELDS), 0, None)}}},
            {"setDataValidation": {"range": _rng(sid, OX["skip"], OX["skip"] + 1, 1, None),
                                   "rule": {"condition": {"type": "BOOLEAN"}, "showCustomUi": True}}},
            _date(sid, OX["date"], 1, rows), _date(sid, OX["added"], 1, rows), _num(sid, OX["views"], 1, rows),
            _num(sid, OX["ratio"], 1, rows, "0.0"), _num(sid, OX["min"], 1, rows, "0"),
            _protect(sid, "Данные оригинала обновляются ночью — править можно «Подходит для серий», «Не брать» и «Заметку»", 0, OX["series"], 1, rows),
            _protect(sid, "Считается из «Очереди»", OX["taken"], OX["taken"] + 1, 1, rows),
            _protect(sid, "Дата добавления ставится сама", OX["added"], OX["added"] + 1, 1, rows),
            _cond(bk, sid, 0, len(ORIG_FIELDS), 1, rows, f'=${col_letter(OX["skip"] + 1)}2=TRUE', fg=GREY_TXT),
            _cond(bk, sid, OX["basket"], OX["basket"] + 1, 1, rows, f'=${col_letter(OX["basket"] + 1)}2="много"', bg=GREEN),
            _cond(bk, sid, OX["basket"], OX["basket"] + 1, 1, rows, f'=${col_letter(OX["basket"] + 1)}2="средне"', bg=AMBER)]
    req += [_width(sid, i, px) for i, px in enumerate([105, 170, 50, 460, 230, 90, 70, 90, 75, 75, 300, 70, 85, 200, 90])]
    return req


def queue_check_formula(bk):
    return bk.f(
        '={"Проверка";ARRAYFORMULA(IF(B2:B=""¦""¦TRIM('
        'IF((K2:K<>"")*(E2:E<>"Отменено")*(COUNTIFS(B2:B¦B2:B¦K2:K¦K2:K¦E2:E¦"<>Отменено")>1)¦"повтор оригинала на канале  "¦"")'
        '&IF((K2:K<>"")*(E2:E<>"Отменено")*(COUNTIFS(L2:L¦L2:L¦K2:K¦K2:K¦B2:B¦"<>"&B2:B¦E2:E¦"<>Отменено")>0)¦"оригинал уже взят другим каналом этого языка  "¦"")'
        '&IF((D2:D<>"")*(K2:K="")¦"в «Донор» нет ссылки на ролик — пул его не увидит  "¦"")'
        '&IF((E2:E="Залито")*(D2:D="")¦"впишите ссылку на оригинал  "¦"")'
        '&IF((D2:D<>"")*(E2:E="")¦"выберите статус  "¦"")'
        '&IF((A2:A<>"")*(A2:A<TODAY())*(E2:E<>"Залито")*(E2:E<>"Отменено")¦"просрочено  "¦"")'
        '&IF(COUNTIF(\'Каналы\'!B2:B¦B2:B)=0¦"канала нет в списке"¦""))))}')


def _queue_columns(bk, q):
    sid = q.id
    if q.col_count < 12:
        bk.batch([{"appendDimension": {"sheetId": sid, "dimension": "COLUMNS", "length": 12 - q.col_count}}])
    a = arr(bk)
    bk.batch_values([
        {"range": f"'{QUEUE}'!H2", "values": [[""]]},          # формула «Проверки» переезжает в заголовок: сортировка её не сломает
        {"range": f"'{QUEUE}'!H1", "values": [[queue_check_formula(bk)]]},
        {"range": f"'{QUEUE}'!J1:L1", "values": [[
            bk.f(f"={{\"{QUEUE_EXTRA[0]}\";ARRAYFORMULA(IF(K2:K=\"\"¦\"\"¦IFERROR(VLOOKUP(K2:K¦{{'{ORIG}'!A2:A{a}'{ORIG}'!D2:D}}¦2¦FALSE)¦\"\")))}}"),
            bk.f(f'={{"{QUEUE_EXTRA[1]}";ARRAYFORMULA(IF(D2:D=""¦""¦IFERROR(REGEXEXTRACT(D2:D&""¦"(?:youtu\\.be/|[?&]v=|/shorts/|/live/)([\\w-]{{11}})")¦'
                 f'IFERROR(REGEXEXTRACT(TRIM(D2:D&"")¦"^([\\w-]{{11}})$")¦""))))}}'),
            bk.f(f"={{\"{QUEUE_EXTRA[2]}\";ARRAYFORMULA(IF(B2:B=\"\"¦\"\"¦IFERROR(VLOOKUP(B2:B¦{{'Каналы'!B2:B{a}'Каналы'!D2:D}}¦2¦FALSE)¦\"\")))}}"),
        ]]},
    ])
    req = [_hdr(sid, 9, 12, False), _width(sid, 9, 360), _width(sid, 10, 110), _width(sid, 11, 110),
           _protect(sid, "Считается само из ссылки в «Донор»", 9, 12, 0, max(q.row_count, 1000))]
    bk.batch(req)


def _channels_column(bk, ch):
    IX, ROWS, L = host.IX, host.ROWS, host.L
    col = IX["pool_free"]
    if ch.col_count < col + 1:
        bk.batch([{"appendDimension": {"sheetId": ch.id, "dimension": "COLUMNS", "length": col + 1 - ch.col_count}}])
    bk.batch_values([{"range": f"'Каналы'!{L('pool_free')}1", "values": [[POOL_FREE_HEADER]]}])
    bk.batch([_hdr(ch.id, col, col + 1, False), _width(ch.id, col, 95), _num(ch.id, col, 1, ROWS),
              _protect(ch.id, f"«{POOL_FREE_HEADER}» считается сама", col, col + 1, 1, ROWS)])


# =====================================================================
#  Лист канала: пул слева, ролики справа
# =====================================================================
def pool_formulas(bk):
    """Формулы пула для листа канала (строка POOL_HDR_ROW+1). Имя канала — A1, язык — B3, серия — B4."""
    r = POOL_HDR_ROW + 1
    O = lambda c: f"'{ORIG}'!{c}2:{c}"
    Q = lambda c: f"'{QUEUE}'!{c}2:{c}"
    K = lambda c: f"'{KONK}'!{c}2:{c}"
    oc = lambda k: col_letter(OX[k] + 1)
    a = arr(bk)
    link = f"G{r}:G"
    idx = f'REGEXEXTRACT({link}¦"[\\w-]{{11}}$")'
    own = f'{{IF({Q("E")}="Отменено"¦""¦{Q("B")}&"|"&{Q("K")}){a}IF({Q("E")}=""¦"статус не выбран"¦{Q("E")})&IF({Q("A")}=""¦""¦" · "&TEXT({Q("A")}¦"dd.mm"))}}'
    lang = f'{{IF({Q("E")}="Отменено"¦""¦{Q("L")}&"|"&{Q("K")}){a}{Q("B")}}}'
    cancelled = f'{{IF({Q("E")}="Отменено"¦{Q("B")}&"|"&{Q("K")}¦""){a}{Q("E")}}}'
    status = (f'=ARRAYFORMULA(IF({link}=""¦¦IFERROR(VLOOKUP($A$1&"|"&{idx}¦{own}¦2¦FALSE)¦'
              f'IFERROR("занят: "&VLOOKUP($B$3&"|"&{idx}¦{lang}¦2¦FALSE)¦'
              f'IFERROR(VLOOKUP($A$1&"|"&{idx}¦{cancelled}¦2¦FALSE)¦"свободно")))))')
    title = (f'=ARRAYFORMULA(IF({link}=""¦¦HYPERLINK({link}¦IFERROR(VLOOKUP({idx}¦{{{O("A")}{a}{O("D")}}}¦2¦FALSE)¦'
             f'IFERROR(VLOOKUP({idx}¦{{{Q("K")}{a}{Q("C")}}}¦2¦FALSE)¦{link})))))')
    series_ok = (f'REGEXMATCH(","&REGEXREPLACE(LOWER(TRIM({O(oc("series"))}))¦"\\s*[,;]\\s*"¦",")&","¦","&LOWER(TRIM($B$4))&",")')
    base = (f'({O("A")}<>"")*(TRIM({O(oc("series"))})<>"")'
            f'*({O(oc("skip"))}<>TRUE)*({O(oc("ratio"))}>=pool_mid)*({O(oc("min"))}>=pool_min_len)'
            f'*ISNUMBER(MATCH(TRIM({O(oc("donor"))})¦IFERROR(FILTER(TRIM({K("A")})¦{K("D")}=TRUE)¦"—")¦0))')
    hi = f'({O(oc("views"))}>=pool_min_views)'
    fits = f'{base}*{hi}*{series_ok}'
    cols = f'{O(oc("views"))}{a}{O(oc("basket"))}{a}{O(oc("donor"))}{a}{O(oc("min"))}{a}{O(oc("url"))}{a}{O(oc("date"))}'
    blank = "{" + a.join(['""'] * 6) + "}"
    # Пул канала: сверху разгон — пока в «Очереди» меньше pool_warmup строк, сильнейшие ролики НИЖЕ порога pool_min_views
    # (по возрастанию); дальше pool_size свободных от порога, от сильного вниз (своя серия, не хватает — другие серии);
    # внизу — взятые этим каналом. Взятые этим каналом и соседями по языку в свободные не попадают.
    pool = (f'=LET('
            f'blank¦{blank}¦'
            f'own¦IFERROR(FILTER({Q("K")}¦{Q("B")}=$A$1¦{Q("E")}<>"Отменено"¦{Q("K")}<>"")¦"—")¦'
            f'cx¦IFERROR(FILTER({Q("K")}¦{Q("B")}=$A$1¦{Q("E")}="Отменено"¦{Q("K")}<>"")¦"—")¦'
            f'sib¦IFERROR(FILTER({Q("K")}¦{Q("L")}=$B$3¦{Q("B")}<>$A$1¦{Q("E")}<>"Отменено"¦{Q("K")}<>"")¦"—")¦'
            f'c¦IF(TRIM($B$4)=""¦""¦IFERROR(FILTER({{{cols}{a}{O("A")}}}¦({fits})>0)¦""))¦'
            f'co¦IF(TRIM($B$4)=""¦""¦IFERROR(FILTER({{{cols}{a}{O("A")}}}¦({base})*{hi}*(1-{series_ok})>0)¦""))¦'
            # разгон: ролики НИЖЕ порога — сначала своей серии, не хватит — других
            f'cw¦IF(TRIM($B$4)=""¦""¦IFERROR(FILTER({{{cols}{a}{O("A")}}}¦({base})*(1-{hi})*{series_ok}>0)¦""))¦'
            f'cwo¦IF(TRIM($B$4)=""¦""¦IFERROR(FILTER({{{cols}{a}{O("A")}}}¦({base})*(1-{hi})*(1-{series_ok})>0)¦""))¦'
            # свободные: не взяты этим каналом, не отменены им (отменил — значит не подошёл), не взяты соседом по языку
            f'free¦IFERROR(FILTER(CHOOSECOLS(c¦1¦2¦3¦4¦5¦6)¦ISNA(MATCH(CHOOSECOLS(c¦7)¦own¦0))*ISNA(MATCH(CHOOSECOLS(c¦7)¦cx¦0))*ISNA(MATCH(CHOOSECOLS(c¦7)¦sib¦0)))¦blank)¦'
            f'freeo¦IFERROR(FILTER(CHOOSECOLS(co¦1¦2¦3¦4¦5¦6)¦ISNA(MATCH(CHOOSECOLS(co¦7)¦own¦0))*ISNA(MATCH(CHOOSECOLS(co¦7)¦cx¦0))*ISNA(MATCH(CHOOSECOLS(co¦7)¦sib¦0)))¦blank)¦'
            f'freew¦IFERROR(FILTER(CHOOSECOLS(cw¦1¦2¦3¦4¦5¦6)¦ISNA(MATCH(CHOOSECOLS(cw¦7)¦own¦0))*ISNA(MATCH(CHOOSECOLS(cw¦7)¦cx¦0))*ISNA(MATCH(CHOOSECOLS(cw¦7)¦sib¦0)))¦blank)¦'
            f'freewo¦IFERROR(FILTER(CHOOSECOLS(cwo¦1¦2¦3¦4¦5¦6)¦ISNA(MATCH(CHOOSECOLS(cwo¦7)¦own¦0))*ISNA(MATCH(CHOOSECOLS(cwo¦7)¦cx¦0))*ISNA(MATCH(CHOOSECOLS(cwo¦7)¦sib¦0)))¦blank)¦'
            f'size¦MAX(1¦INDEX(pool_size¦1¦1))¦'
            # основной пул: своя серия от порога, не хватает до size — сильнейшие из других серий (пометка в «Корзине»)
            f'topown¦IFERROR(ARRAY_CONSTRAIN(SORT(free¦1¦FALSE)¦size¦6)¦blank)¦'
            f'no¦IF(INDEX(topown¦1¦5)=""¦0¦ROWS(topown))¦'
            f'add¦IF(no>=size¦blank¦IFERROR(ARRAY_CONSTRAIN(SORT(freeo¦1¦FALSE)¦size-no¦6)¦blank))¦'
            f'addm¦IF(INDEX(add¦1¦5)=""¦blank¦HSTACK(CHOOSECOLS(add¦1)¦ARRAYFORMULA(IF(CHOOSECOLS(add¦5)=""¦""¦"из другой серии"))¦CHOOSECOLS(add¦3¦4¦5¦6)))¦'
            f'both¦VSTACK(topown¦addm)¦'
            f'top¦IFERROR(SORT(FILTER(both¦CHOOSECOLS(both¦5)<>"")¦1¦FALSE)¦blank)¦'
            # разгон: пока у канала в «Очереди» меньше pool_warmup строк — сильнейшие ролики ниже порога, показаны по возрастанию
            f't¦COUNTIFS({Q("B")}¦$A$1¦{Q("E")}¦"<>Отменено"¦{Q("K")}¦"<>")¦'
            f'w¦MAX(0¦INDEX(pool_warmup¦1¦1)-t)¦'
            f'wown¦IF(w=0¦blank¦IFERROR(ARRAY_CONSTRAIN(SORT(freew¦1¦FALSE)¦w¦6)¦blank))¦'
            f'nw¦IF(INDEX(wown¦1¦5)=""¦0¦ROWS(wown))¦'
            f'wadd¦IF(nw>=w¦blank¦IFERROR(ARRAY_CONSTRAIN(SORT(freewo¦1¦FALSE)¦w-nw¦6)¦blank))¦'
            f'waddm¦IF(INDEX(wadd¦1¦5)=""¦blank¦HSTACK(CHOOSECOLS(wadd¦1)¦ARRAYFORMULA(IF(CHOOSECOLS(wadd¦5)=""¦""¦"из другой серии"))¦CHOOSECOLS(wadd¦3¦4¦5¦6)))¦'
            f'wb¦VSTACK(wown¦waddm)¦'
            f'warm¦IFERROR(SORT(FILTER(wb¦CHOOSECOLS(wb¦5)<>"")¦1¦TRUE)¦blank)¦'
            f'best¦top¦'
            f'mine¦IFERROR(SORT(FILTER({{{cols}}}¦ISNUMBER(MATCH({O("A")}¦own¦0)))¦1¦FALSE)¦blank)¦'
            # взятые этим каналом, которых нет в «Оригиналах» (история с других доноров) — тоже видны внизу
            f'u¦UNIQUE(own)¦'
            f'ext¦IF(INDEX(own¦1¦1)="—"¦blank¦IFERROR(FILTER({{IF(u=""¦""¦"")'
            f'{a}IF(u=""¦""¦"")'
            f'{a}IF(u=""¦""¦"нет в «Оригиналах»")'
            f'{a}IF(u=""¦""¦"")'
            f'{a}"https://youtu.be/"&u'
            f'{a}IF(u=""¦""¦"")}}¦ISNA(MATCH(u¦{O("A")}¦0)))¦blank))¦'
            f'all¦VSTACK(warm¦best¦mine¦ext)¦'
            f'IFERROR(FILTER(all¦CHOOSECOLS(all¦5)<>"")¦'
            f'IF(TRIM($B$4)=""¦"у канала не выбрана серия — укажите её в «Каналах»"¦"в пуле пока нет оригиналов под серию «"&$B$4&"»")))')
    ours = (f'=ARRAYFORMULA(IF({link}=""¦¦IFERROR(VLOOKUP($A$1&"|"&{idx}¦'
            f'{{IF({Q("E")}="Отменено"¦""¦{Q("B")}&"|"&{Q("K")}){a}{Q("G")}}}¦2¦FALSE)¦"")))')
    return {"A": bk.f(status), "B": bk.f(title), "C": bk.f(pool), "I": bk.f(ours)}


def videos_original_formula(bk):
    r = POOL_HDR_ROW + 1
    a = arr(bk)
    Q = lambda c: f"'{QUEUE}'!{c}2:{c}"
    link = f"{col_letter(VID_COL + 4)}{r}:{col_letter(VID_COL + 4)}"          # O: «Ссылка» на наш ролик
    return bk.f(
        f'=ARRAYFORMULA(IF({link}=""¦¦IFERROR(VLOOKUP(REGEXEXTRACT({link}¦"[\\w-]{{11}}$")¦'
        f'{{IFERROR(REGEXEXTRACT({Q("G")}&""¦"(?:youtu\\.be/|[?&]v=|/shorts/|/video/)([\\w-]{{11}})")¦""){a}'
        f'IF({Q("K")}=""¦""¦HYPERLINK("https://youtu.be/"&{Q("K")}¦IF({Q("J")}<>""¦{Q("J")}¦IF({Q("C")}<>""¦{Q("C")}¦"оригинал"))))}}¦2¦FALSE)¦"")))')


def summary_values(bk):
    r = POOL_HDR_ROW + 1
    return [
        ["Пул оригиналов", ""],
        ["Всего в пуле", bk.f(f'=COUNTIF(G{r}:G¦"http*")')],
        ["Свободно", bk.f(f'=COUNTIF(A{r}:A¦"свободно")')],
        ["В работе", bk.f(f'=SUMPRODUCT(--REGEXMATCH(A{r}:A&""¦"^(Запланировано|Текст готов|Рендер|Готово к заливке|статус не выбран)"))')],
        ["Залито", bk.f(f'=COUNTIF(A{r}:A¦"Залито*")')],
        ["Разгон: осталось роликов", bk.f(f"=MAX(0¦pool_warmup-COUNTIFS('{QUEUE}'!B2:B¦$A$1¦'{QUEUE}'!E2:E¦\"<>Отменено\"¦'{QUEUE}'!K2:K¦\"<>\"))")],
        ["Следующий", bk.f(f'=IFERROR(HYPERLINK(INDEX(G{r}:G¦MATCH("свободно"¦A{r}:A¦0))¦INDEX(B{r}:B¦MATCH("свободно"¦A{r}:A¦0)))¦"—")')],
        ["", ""],
        ["Как взять в работу", "берите сверху вниз: первая голубая строка — следующий. Ссылку — в «Очередь», колонка «Донор»"],
        ["Ролики канала", "таблица справа, с колонки K"],
    ]


def channel_layout_values(bk):
    """Всё, что пишется формулами/заголовками на лист канала (кроме карточки и строк роликов)."""
    h, r = POOL_HDR_ROW, POOL_HDR_ROW + 1
    f = pool_formulas(bk)
    vc = col_letter(VID_COL)
    return [
        {"range": "D2:E11", "values": summary_values(bk)},
        {"range": f"A{h - 1}", "values": [["Пул: 60 лучших свободных. Пока идёт разгон — сверху послабее, дальше от самых сильных вниз; внизу — уже взятые каналом"]]},
        {"range": f"{vc}{h - 1}", "values": [["Ролики на канале — с YouTube, обновляются ночью"]]},
        {"range": f"A{h}:I{h}", "values": [POOL_COLS]},
        {"range": f"{vc}{h}:{col_letter(VID_COL + len(VID_COLS) - 1)}{h}", "values": [VID_COLS]},
        {"range": f"A{r}:C{r}", "values": [[f["A"], f["B"], f["C"]]]},
        {"range": f"I{r}", "values": [[f["I"]]]},
        {"range": f"{col_letter(VID_COL + 7)}{r}", "values": [[videos_original_formula(bk)]]},
    ]


def channel_layout_requests(bk, sid, rows=None):
    h0, r0 = POOL_HDR_ROW - 1, POOL_HDR_ROW          # 0-based: строка заголовка, первая строка данных
    v0 = VID_COL - 1
    rows = None                                       # оформление до конца листа: строки добавляются по мере роста пула
    req = [
        _fmt(sid, 3, 4, 1, 2, {"textFormat": {"bold": True, "fontSize": 12}}, "userEnteredFormat.textFormat"),
        _fmt(sid, 3, 4, 2, 11, {"textFormat": {"bold": True}, "backgroundColor": {"red": 0.95, "green": 0.95, "blue": 0.95}},
             "userEnteredFormat(textFormat,backgroundColor)"),
        _fmt(sid, 4, 5, 7, 8, {"textFormat": {"bold": True}, "backgroundColor": BLUE}, "userEnteredFormat(textFormat,backgroundColor)"),
        _fmt(sid, 0, 1, h0 - 1, h0, {"textFormat": {"bold": True, "fontSize": 12}}, "userEnteredFormat.textFormat"),
        _fmt(sid, v0, v0 + 1, h0 - 1, h0, {"textFormat": {"bold": True, "fontSize": 12}}, "userEnteredFormat.textFormat"),
        _hdr(sid, 0, 9, False, row=h0), _hdr(sid, v0, v0 + 6, False, row=h0), _hdr(sid, v0 + 6, v0 + 7, True, row=h0),
        _hdr(sid, v0 + 7, v0 + 8, False, row=h0),
        _num(sid, 2, r0, rows), _num(sid, 5, r0, rows, "0"), _date(sid, 7, r0, rows),
        _date(sid, v0, r0, rows), _num(sid, v0 + 3, r0, rows),
        _cond(bk, sid, 0, 9, r0, rows, f'=REGEXMATCH($A{r0 + 1};"^Залито")', bg=GREEN),
        _cond(bk, sid, 0, 9, r0, rows, f'=REGEXMATCH($A{r0 + 1};"^(Запланировано|Текст готов|Рендер|Готово к заливке|статус не выбран)")', bg=AMBER),
        _cond(bk, sid, 0, 9, r0, rows, f'=REGEXMATCH($A{r0 + 1};"^(занят|Отменено)")', fg=GREY_TXT),
        _cond(bk, sid, 0, 9, r0, rows, f'=AND($A{r0 + 1}="свободно";COUNTIF($A${r0}:$A{r0};"свободно")=0)', bg=BLUE, bold=True),
        _cond(bk, sid, v0, v0 + 8, r0, rows, f'=${col_letter(VID_COL + 5)}{r0 + 1}="чужая тема"', fg=GREY_TXT),
    ]
    widths = {0: 200, 1: 520, 2: 110, 3: 165, 4: 210, 5: 80, 6: 250, 7: 95, 8: 250, 9: 24,
              v0: 95, v0 + 1: 420, v0 + 2: 95, v0 + 3: 95, v0 + 4: 190, v0 + 5: 90, v0 + 6: 200, v0 + 7: 380}
    req += [_width(sid, i, px) for i, px in widths.items()]
    return req


def channel_protections(sid, rows=None):
    h0 = POOL_HDR_ROW - 1
    v0 = VID_COL - 1
    rows = None
    return [
        _protect(sid, "Пул считается сам. Взять оригинал в работу — впишите его ссылку в «Очередь»", 0, 9, h0, rows),
        _protect(sid, "Сводка пула считается сама", 3, 5, 1, 11),
        _protect(sid, "Ролики обновляются с YouTube — своя пометка в колонке «Моя пометка»", v0, v0 + 6, h0, rows),
        _protect(sid, "Оригинал подтягивается из «Очереди» по ссылке на наш ролик", v0 + 7, v0 + 8, h0, rows),
    ]


def reformat_sheet(bk, ws, requests):
    """Sheets хранит диапазоны оформления с нижней границей = размер листа на момент записи.
    После добавления строк правила и защиты снимаются и ставятся заново на весь лист."""
    meta = [x for x in host.retry(bk.sh.fetch_sheet_metadata)["sheets"] if x["properties"]["sheetId"] == ws.id][0]
    req = [{"deleteConditionalFormatRule": {"sheetId": ws.id, "index": i}} for i in range(len(meta.get("conditionalFormats", [])) - 1, -1, -1)]
    req += [{"deleteProtectedRange": {"protectedRangeId": p["protectedRangeId"]}} for p in meta.get("protectedRanges", [])]
    if "basicFilter" in meta: req.append({"clearBasicFilter": {"sheetId": ws.id}})
    bk.batch(req + requests)


def ensure_pool_rows(bk, channel_rows, log):
    """Пул разливается формулой вниз: на листе канала должно хватать строк под ВСЕ оригиналы его серии
    (верхняя граница — даже если включат всех конкурентов и снизят пороги). Иначе формула даст #REF!."""
    ws = bk.sheets.get(ORIG)
    if ws is None: return
    col = col_letter(OX["series"] + 1)
    vals = host.retry(ws.get, f"{col}2:{col}{ws.row_count}")
    per = {}
    for r in vals:
        for sname in (r[0].split(",") if r else []):
            sname = sname.strip()
            if sname: per[sname] = per.get(sname, 0) + 1
    req = []
    for r in channel_rows:
        wsc = bk.by_id.get(r.get("_gid")) if r.get("_gid") else None
        if wsc is None: continue
        need = max(CARD_ROWS_MIN, POOL_HDR_ROW + per.get(r["series"], 0) + 150)
        if wsc.row_count < need:
            req.append((wsc, need - wsc.row_count + 200))
    if req:
        bk.batch([{"appendDimension": {"sheetId": w.id, "dimension": "ROWS", "length": n}} for w, n in req])
        bk.reload()
        for w, _ in req:
            w = bk.by_id[w.id]
            reformat_sheet(bk, w, host.card_format(bk, w) + host.card_protections(w.id))
        log(f"пул: добавлены строки на {len(req)} листах каналов")


# =====================================================================
#  Ночная синхронизация конкурентов
# =====================================================================
def load_state():
    try: return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError): return {"donors": {}}


def save_state(st):
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False))
    os.replace(tmp, STATE_FILE)


def read_settings(bk, log=None):
    out = {}
    names = {n["name"] for n in host.retry(bk.sh.fetch_sheet_metadata).get("namedRanges", [])}
    for k, _, default, name in SETTINGS:
        if name not in names:
            out[k] = default; continue
        try:
            got = host.retry(bk.sh.values_get, name, params={"valueRenderOption": "UNFORMATTED_VALUE"}).get("values", [[""]])
            v = got[0][0] if got and got[0] else ""
            out[k] = float(v) if isinstance(v, (int, float)) and v > 0 else default
        except Exception as e:
            out[k] = default
            if log: log(f"пул: настройка {name} не читается ({type(e).__name__}) — беру {default}")
    return out


def basket(ratio, s):
    return "много" if ratio >= s["top"] else "средне" if ratio >= s["mid"] else "мало"


def openai_key():
    if os.environ.get("WW2_OPENAI_KEY"): return os.environ["WW2_OPENAI_KEY"]
    return json.loads(Path(KEYS_FILE).read_text())["openai"]


def classify(items, log):
    """items: [{id,title,donor}] → {id: {"ww2": bool, "series": [...]}} ; пропавшие id не возвращаются."""
    series_txt = "\n".join(f"- «{k}»: {v}" for k, v in SERIES_ABOUT.items())
    prompt = (
        "Ты раскладываешь ролики YouTube-каналов о войне по сериям наших каналов (мы пересказываем такие ролики на других языках).\n"
        "Для каждого ролика по названию реши:\n"
        "1) ww2 — ролик про Вторую мировую войну (1939–1945, включая её фронты, людей, технику, преступления, прямые последствия и суды). "
        "Первая мировая, Корея, Вьетнам, холодная война, современность, древность — false.\n"
        "2) series — список серий, для которых ролик ГОДИТСЯ как основа выпуска (может быть несколько или ни одной). "
        "Бери серию, только если сюжет ролика действительно попадает в её обещание зрителю.\n\n"
        f"Серии:\n{series_txt}\n\n"
        "Парные серии («Письма с фронта» и «Дневники и письма», «Сталь и пепел» и «Жёсткая сторона», «Неизвестная война» и «Невероятные истории») "
        "ставь вместе.\n\nПримеры правильной разметки (канал | название → ww2 | серии):\n"
        + "\n".join(f"{d} | {t} → {'да' if w else 'нет'} | {', '.join(sr) or '—'}" for d, t, w, sr in FEW_SHOT) + "\n\n"
        'Ответ — строго JSON: {"items":[{"id":"...","ww2":true,"series":["..."]}]} для КАЖДОГО ролика.\n\nРолики:\n'
        + "\n".join(f'{it["id"]} | {it["donor"]} | {it["title"]}' for it in items))
    body = {"model": LLM_MODEL, "messages": [{"role": "user", "content": prompt}], "response_format": {"type": "json_object"}}
    if not LLM_MODEL.startswith(("gpt-5", "o")): body["temperature"] = 0      # рассуждающие модели температуру не принимают
    req = urllib.request.Request("https://api.openai.com/v1/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Authorization": "Bearer " + openai_key(), "Content-Type": "application/json"})
    for i in range(3):
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                d = json.load(r)
            got = json.loads(d["choices"][0]["message"]["content"]).get("items", [])
            ok = {}
            for g in got:
                if g.get("id") in {it["id"] for it in items}:
                    ok[g["id"]] = {"ww2": bool(g.get("ww2")), "series": [s for s in g.get("series") or [] if s in SERIES_ABOUT]}
            return ok
        except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError, http.client.HTTPException) as e:
            log(f"пул: разбор роликов, попытка {i + 1}: {str(e)[:150]}")
            time.sleep(10 * (i + 1))
    return {}


def resolve_channel(yt, link):
    link = urllib.parse.unquote((link or "").strip())
    part = "snippet,statistics,contentDetails"
    m = re.search(r"(UC[\w-]{22})", link)
    if m:
        d = yt("channels", part=part, id=m.group(1))
    elif re.search(r"@([\w.\-]+)", link):
        d = yt("channels", part=part, forHandle="@" + re.search(r"@([\w.\-]+)", link).group(1))
    elif re.search(r"/user/([\w.\-]+)", link):
        d = yt("channels", part=part, forUsername=re.search(r"/user/([\w.\-]+)", link).group(1))
    elif re.search(r"/c/([\w.\-]+)", link):          # старые адреса /c/имя почти всегда совпадают с @хендлом
        d = yt("channels", part=part, forHandle="@" + re.search(r"/c/([\w.\-]+)", link).group(1))
    else:
        return None
    return (d.get("items") or [None])[0]


def fetch_new_ids(yt, playlist, seen):
    ids, token = [], None
    while len(ids) < MAX_DONOR_VIDEOS:
        try:
            r = yt("playlistItems", part="contentDetails", playlistId=playlist, maxResults=50, **({"pageToken": token} if token else {}))
        except urllib.error.HTTPError as e:
            if e.code == 404: break
            raise
        page = [x["contentDetails"]["videoId"] for x in r.get("items", [])]
        ids += page
        token = r.get("nextPageToken")
        if not token or (seen and all(v in seen for v in page)): break
    return [v for v in dict.fromkeys(ids) if v not in seen]


def video_details(yt, ids):
    iso_dur = host.iso_dur
    out = {}
    for i in range(0, len(ids), 50):
        for v in yt("videos", part="snippet,statistics,contentDetails", id=",".join(ids[i:i + 50])).get("items", []):
            h, mi, se = (int(x) for x in iso_dur(v["contentDetails"].get("duration")).split(":"))
            out[v["id"]] = {"title": v["snippet"]["title"], "date": v["snippet"]["publishedAt"][:10],
                            "views": int(v["statistics"].get("viewCount", 0)), "min": round(h * 60 + mi + se / 60, 1),
                            "upcoming": v["snippet"].get("liveBroadcastContent") in ("upcoming", "live")}
    return out


def pool_sync(bk, yt, log, today):
    retry = host.retry
    lock = open(STATE_FILE.with_suffix(".lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("пул: уже идёт другой прогон — пропускаю"); return
    ensure_structure(bk, log)
    s = read_settings(bk, log)
    st = load_state()
    try:
        openai_key(); llm_err = ""
    except Exception as e:
        llm_err = f"нет ключа OpenAI ({type(e).__name__})"
    kws, ows = bk.sheets[KONK], bk.sheets[ORIG]
    konk = retry(kws.get, f"A2:{col_letter(len(KONK_FIELDS))}{kws.row_count}")
    cur = {r[0]: r + [""] * (len(ORIG_FIELDS) - len(r))
           for r in retry(ows.get, f"A2:{col_letter(len(ORIG_FIELDS))}{ows.row_count}") if r and r[0]}
    changes, appends, gone, konk_out = {}, {}, {}, {}
    quota_out = False
    for r in konk:
        r = r + [""] * (len(KONK_FIELDS) - len(r))
        name, link = r[KX["name"]].strip(), r[KX["link"]].strip()
        if not name or name in konk_out: continue
        if str(r[KX["on"]]).upper() not in ("TRUE", "ИСТИНА"):
            konk_out[name] = {"state": "не берём — в пулах не участвует"}; continue
        if quota_out:
            konk_out[name] = {"state": "не обработан этой ночью — кончилась квота YouTube"}; continue
        try:
            konk_out[name] = _sync_donor(yt, log, today, s, st, cur, changes, appends, gone, name, link, r, llm_err)
        except urllib.error.HTTPError as e:
            quota_out = e.code == 403
            konk_out[name] = {"state": f"ошибка YouTube {e.code} — повтор следующей ночью"}; log(f"пул: {name}: {e}")
        except Exception as e:
            konk_out[name] = {"state": f"ошибка: {type(e).__name__}: {str(e)[:80]}"}; log(f"пул: {name}: {type(e).__name__}: {e}")
        save_state(st)
    # ---- запись: номера строк берём свежие, перед самой записью (лист могли отсортировать) ----
    now = retry(ows.get, f"A2:{col_letter(OX['note'] + 1)}{ows.row_count}")       # свежие ID и заметки — прямо перед записью
    row_of, note_of = {}, {}
    for i, rr in enumerate(now):
        if rr and rr[0]:
            row_of.setdefault(rr[0], i + 2)
            note_of.setdefault(rr[0], rr[OX["note"]] if len(rr) > OX["note"] else "")
    data = []
    for vid, ch in changes.items():
        if vid not in row_of: continue
        rr = row_of[vid]
        if "views" in ch:
            data.append({"range": f"'{ORIG}'!{col_letter(OX['views'] + 1)}{rr}:{col_letter(OX['basket'] + 1)}{rr}", "values": [list(ch["views"])]})
        if "donor" in ch:
            data.append({"range": f"'{ORIG}'!{col_letter(OX['donor'] + 1)}{rr}", "values": [[ch["donor"]]]})
    for vid, (note, entry) in gone.items():
        if vid in row_of:
            rr = row_of[vid]
            old = note_of.get(vid, "")
            data.append({"range": f"'{ORIG}'!{col_letter(OX['skip'] + 1)}{rr}", "values": [[True]]})
            if note not in old:
                data.append({"range": f"'{ORIG}'!{col_letter(OX['note'] + 1)}{rr}", "values": [[txt(f"{old}; {note}" if old else note)]]})
    if data: bk.batch_values(data)
    for vid, (note, entry) in gone.items(): entry["gone_marked"] = True
    new_rows = [a for vid, (a, entry) in appends.items() if vid not in row_of]
    if new_rows:
        start = (max(row_of.values()) if row_of else 1) + 1
        need = start + len(new_rows) + 50
        if ows.row_count < need:
            retry(ows.add_rows, need - ows.row_count + 2000)
            bk.reload(); ows = bk.sheets[ORIG]
            reformat_sheet(bk, ows, orig_format_requests(bk, ows.id))
        tk = OX["taken"]            # «Взят на каналах» — формула в заголовке, колонку не пишем
        bk.batch_values([{"range": f"'{ORIG}'!A{start}:{col_letter(tk)}{start + len(new_rows) - 1}", "values": [a[:tk] for a in new_rows]},
                         {"range": f"'{ORIG}'!{col_letter(tk + 2)}{start}:{col_letter(len(ORIG_FIELDS))}{start + len(new_rows) - 1}",
                          "values": [a[tk + 1:] for a in new_rows]}])
    for vid, (a, entry) in appends.items(): entry["added"] = True       # строка была в таблице: удалят руками — не вернём
    save_state(st)
    names_now = retry(kws.get, f"A2:A{kws.row_count}")
    kdata = []
    for i, rr in enumerate(names_now):
        nm = rr[0].strip() if rr and rr[0] else ""
        if nm not in konk_out: continue
        row, d = i + 2, konk_out.pop(nm)
        if rr[0] != nm:                     # пробел в конце имени — формулы пула сравнивают имена символ в символ
            kdata.append({"range": f"'{KONK}'!A{row}", "values": [[txt(nm)]]})
        for k, v in d.items():
            kdata.append({"range": f"'{KONK}'!{col_letter(KX[k] + 1)}{row}", "values": [[v]]})
        kdata.append({"range": f"'{KONK}'!{col_letter(KX['in_pool'] + 1)}{row}", "values": [[konk_in_pool_formula(bk, row)]]})
    if kdata: bk.batch_values(kdata)
    log(f"пул: новых оригиналов {len(new_rows)}, изменено строк {len(changes)}, удалённых у конкурентов {len(gone)}")


def konk_in_pool_formula(bk, row):
    c = lambda k: f"'{ORIG}'!{col_letter(OX[k] + 1)}:{col_letter(OX[k] + 1)}"
    return bk.f(f"=IF(A{row}=\"\"¦\"\"¦COUNTIFS({c('donor')}¦TRIM(A{row})¦{c('ratio')}¦\">=\"&pool_mid¦{c('min')}¦\">=\"&pool_min_len¦{c('views')}¦\">=\"&pool_min_views¦"
                f"{c('series')}¦\"?*\"¦{c('skip')}¦\"<>TRUE\"))")


def _sync_donor(yt, log, today, s, st, cur, changes, appends, gone, name, link, r, llm_err):
    # ссылка важнее запомненного UC: её правят руками, UC пишет скрипт
    ch = resolve_channel(yt, link) if link else None
    if not ch and r[KX["uc"]]:
        ch = resolve_channel(yt, r[KX["uc"]])
    if not ch:
        return {"state": "канал не найден (удалён, закрыт или ссылка неверна) — его ролики остаются в пулах, пока стоит «Берём»"}
    uc = ch["id"]
    dn = st["donors"].setdefault(uc, {"seen": {}, "full": ""})
    seen = dn["seen"]
    new = fetch_new_ids(yt, ch["contentDetails"]["relatedPlaylists"]["uploads"], seen)
    det = video_details(yt, new) if new else {}
    for k, v in det.items():
        if v.get("upcoming"): continue                       # эфир/премьера: разберём, когда появится длина
        seen[k] = {**{f: v[f] for f in ("title", "date", "views", "min")}, "ww2": None, "series": []}
    # к разбору: всё длинное, что ещё не разобрано (новое, упавшее прошлой ночью, короткое при сниженном пороге)
    todo = [{"id": k, "title": v["title"], "donor": name} for k, v in seen.items() if v.get("ww2") is None and v.get("min", 0) >= s["min_len"]]
    if todo and not llm_err:
        for j in range(0, len(todo), 40):
            res = classify(todo[j:j + 40], log)
            for k, v in res.items():
                seen[k].update(v)
    pending = sum(1 for v in seen.values() if v.get("ww2") is None and v.get("min", 0) >= s["min_len"])
    # просмотры раз в неделю целиком, между — только новые
    if (not dn["full"]) or (dt.date.fromisoformat(dn["full"]) <= today - dt.timedelta(days=FULL_REFRESH_DAYS)):
        ids = [k for k, v in seen.items() if v.get("min", 0) >= s["min_len"] and not v.get("gone")]
        fresh = video_details(yt, ids)
        for k in ids:
            if k in fresh:
                seen[k]["views"] = fresh[k]["views"]
                if seen[k].pop("gone", None): seen[k].pop("gone_marked", None)      # вернулся
            else:
                seen[k]["gone"] = True
        dn["full"] = today.isoformat()
    for k, v in seen.items():                 # пометить в таблице — пока запись не прошла, пробуем каждую ночь
        if v.get("gone") and not v.get("gone_marked") and k in cur:
            gone[k] = (f"удалён или скрыт у конкурента ({today:%d.%m})", v)
    long_all = [v for v in seen.values() if v.get("min", 0) >= s["min_len"] and v.get("ww2") is not None]
    median = statistics.median([v["views"] for v in long_all]) if long_all else 0
    n_ww2 = 0
    for vid, v in seen.items():
        if not v.get("ww2") or v.get("min", 0) < s["min_len"]: continue
        n_ww2 += 1
        ratio = round(v["views"] / median, 2) if median else 0
        b = basket(ratio, s)
        if vid in cur:
            old = cur[vid]
            if (str(old[OX["views"]]).replace("\u00a0", "").replace(" ", "") != str(v["views"])
                    or abs(to_num(old[OX["ratio"]]) - ratio) > 0.05 or old[OX["basket"]] != b):
                changes.setdefault(vid, {})["views"] = (v["views"], ratio, b)
            if old[OX["donor"]] != name:                     # конкурента переименовали — строки идут за ним
                changes.setdefault(vid, {})["donor"] = name
        elif vid not in appends and not v.get("gone") and not v.get("added"):
            appends[vid] = ([txt(vid), name, r[KX["lang"]] or "", txt(v["title"]), f"https://youtu.be/{vid}", v["date"], v["min"],
                             v["views"], ratio, b, ", ".join(v.get("series") or []), False, "", "", today.isoformat()], v)
    state = "ок"
    if pending:
        state = f"не разобрано роликов: {pending} — " + (llm_err if llm_err else "повтор следующей ночью")
    return {"uc": uc, "total": int(ch["statistics"].get("videoCount", 0)), "ww2": n_ww2, "median": median,
            "updated": today.isoformat(), "state": state}
