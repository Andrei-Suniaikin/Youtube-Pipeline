#!/usr/bin/env python3
"""ВВ2 · рабочая Google-таблица. Таблица — главный источник, скрипт её ДОПОЛНЯЕТ и не пересоздаёт.

Режимы:
  --rebuild   один раз с Mac: новая структура; ручные данные прежних «Каналов» сохраняются,
              «Почты» и «Профили» засеваются из ~/ops/registry.db
  --sync      раз в сутки: цифры и ролики с YouTube (только новые страницы), история + всё из --light
  --light     каждые 10 минут: коды и листы для новых строк, переименование вкладок,
              «Статус (авто)», «Внимание», дни без заливки

Переменные окружения (для VPS): WW2_SA, WW2_YT_KEY или WW2_YT_KEY_FILE, WW2_REG, WW2_NICHES.
"""
import json, os, re, sqlite3, sys, time, datetime as dt, urllib.request, urllib.parse, urllib.error
from pathlib import Path
import gspread
import ww2_pool as pool

SHEET_ID = os.environ.get("WW2_SHEET_ID", "<WW2_SHEET_ID>")
SA = os.environ.get("WW2_SA", "/Users/vova/organizm_agent/credentials/google_service_account.json")
REG = os.environ.get("WW2_REG", "/Users/vova/ops/registry.db")
NICHES = Path(os.environ.get("WW2_NICHES", "/Users/vova/niches"))
KEY_FILE = os.environ.get("WW2_YT_KEY_FILE", "/Users/vova/.claude/projects/-Users-vova-niches/memory/ww2-credentials.md")
TZ = dt.timezone(dt.timedelta(hours=7))
NOW = dt.datetime.now(TZ)
TODAY = NOW.date()
WW2_START = dt.date(2026, 8, 1)      # раньше — ролики «частоты души» на перекрашенных каналах
MAX_VIDEOS = 200
ROWS = 1000                           # строк под каналы / очередь / почты

LANGS = ["Польский", "Чешский", "Румынский", "Венгерский", "Хорватский", "Греческий", "Украинский", "Испанский", "Португальский"]
LANG_CODE = {"pl": "Польский", "cs": "Чешский", "ro": "Румынский", "hu": "Венгерский", "hr": "Хорватский",
             "el": "Греческий", "uk": "Украинский", "es": "Испанский", "pt": "Португальский"}
SERIES = ["Письма с фронта", "Сталь и пепел", "Неизвестная война", "Последнее убежище",
          "Невероятные истории", "Жёсткая сторона", "Дневники и письма",
          "Великие битвы", "Генералы и приказы", "Разведка и диверсии", "Техника войны", "После войны"]
MY_STATUS = ["Запланирован", "Куплен", "Льётся", "Пауза", "На монетизации", "Монетизирован", "Демонетизирован", "Выведен"]
AUTO_STATUS = ["Нет на YouTube", "Пустой", "Грязный", "Льётся", "Встал"]
MACHINES = ["Mac", "Минский бокс", "Lenovo"]
MONET = ["Нет", "Подана", "Одобрена", "Отклонена", "Демонетизирован"]
QUEUE_STATUS = ["Запланировано", "Текст готов", "Рендер", "Готово к заливке", "Залито", "Отменено"]
NO_SHEET_STATUS = {"", "Запланирован"}      # для таких строк лист канала не создаётся

# «Каналы»: ключ, заголовок, ручная ли колонка
FIELDS = [
    ("code", "Код", False), ("name", "Название канала", True), ("sheet", "Лист", False),
    ("lang", "Язык", True), ("series", "Серия", True), ("started", "Когда заведён", True),
    ("status_my", "Статус (мой)", True), ("status_auto", "Статус (авто)", False), ("alert", "Внимание", False),
    ("email", "Почта", True), ("profile", "Профиль", True), ("machine", "Машина", True),
    ("monet", "Монетизация", True), ("note", "Заметка", True),
    ("yt", "YouTube", False), ("studio", "Студия", False), ("handle", "Хендл", False), ("created", "Создан на YouTube", False),
    ("videos", "Роликов ВВ2", False), ("last", "Последняя заливка", False), ("idle", "Дней без заливки", False),
    ("week", "Роликов за 7 дн", False), ("subs", "Подписчиков", False), ("subs7", "Прирост подписчиков за 7 дн", False),
    ("views", "Просмотров", False), ("best_v", "Лучший ролик, просм.", False), ("best", "Лучший ролик", False),
    ("foreign", "Чужих роликов", False), ("uc", "UC канала", True), ("pool_free", "Свободно в пуле", False),
]
KEYS = [f[0] for f in FIELDS]
IX = {k: i for i, k in enumerate(KEYS)}


def L(key):
    n = IX[key] + 1; s = ""
    while n: n, r = divmod(n - 1, 26); s = chr(65 + r) + s
    return s


def letter(n):
    s = ""
    while n: n, r = divmod(n - 1, 26); s = chr(65 + r) + s
    return s


CARD_ROW_VIDEOS = 27           # строка заголовка таблицы роликов на листе канала (и пула — он слева, ролики справа)
VC0 = pool.VID_COL             # K: первая колонка таблицы роликов с YouTube
VCOL = lambda i: letter(VC0 + i)   # i = 0..7 → K..R
TEMPLATE = "Шаблон канала"      # скрытый лист: из него копируют и сервер, и кнопка меню в таблице
MODE = "--rebuild" if "--rebuild" in sys.argv else "--sync" if "--sync" in sys.argv else "--pool" if "--pool" in sys.argv else "--light"


def log(*a):
    print(f"[{dt.datetime.now(TZ):%Y-%m-%d %H:%M:%S}] {MODE[2:]}:", *a, flush=True)


def retry(fn, *a, tries=6, **k):
    for i in range(tries):
        try:
            return fn(*a, **k)
        except (gspread.exceptions.APIError, ConnectionError, OSError, urllib.error.URLError) as e:
            if isinstance(e, urllib.error.HTTPError) and e.code < 500 and e.code != 429: raise   # 404 пустого канала — не ошибка сети
            if i == tries - 1: raise
            wait = 5 * (i + 1)
            log(f"повтор через {wait} с: {str(e)[:120]}")
            time.sleep(wait)


# ---------------- YouTube ----------------
def api_key():
    if os.environ.get("WW2_YT_KEY"): return os.environ["WW2_YT_KEY"]
    m = re.search(r"key\s+(AIza[A-Za-z0-9_\-]+)", Path(KEY_FILE).read_text())
    if not m: raise SystemExit("нет ключа YouTube")
    return m.group(1)


def yt(endpoint, **params):
    params["key"] = api_key()
    url = f"https://www.googleapis.com/youtube/v3/{endpoint}?" + urllib.parse.urlencode(params)
    def go():
        with urllib.request.urlopen(url, timeout=60) as r:
            return json.load(r)
    return retry(go)


def iso_dur(s):
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", s or "")
    h, mi, se = (int(x or 0) for x in m.groups()) if m else (0, 0, 0)
    return f"{h}:{mi:02d}:{se:02d}"


def parse_date(s):
    s = (s or "").strip()
    for f in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y"):
        try: return dt.datetime.strptime(s, f).date()
        except ValueError: pass
    return None


def to_int(s):
    try: return int(str(s).replace(" ", "").replace(" ", "").replace(",", ""))
    except (ValueError, TypeError): return 0


HOWTO = [
    ["Как пользоваться", ""],
    ["Кому что", "Владелец смотрит «Сводку». Оператор работает с «Сегодня», «Очередью» и пулом оригиналов на листе канала. Остальное — справочники."],
    ["Цвет заголовков", "Голубой заголовок — колонку заполняете руками. Серый — заполняется сама; при попытке правки таблица предупредит."],
    ["Завести новый канал", "1) «Каналы», первая пустая строка: название, язык, серия, когда заведён, «Статус (мой)» = Куплен, почта, профиль, машина. "
                            "2) В течение 10 минут появятся код и ссылка «открыть» на лист канала; пул оригиналов под его серию появится там сам. "
                            "3) Когда канал создан на YouTube, вставьте в колонку «UC канала» UC из адреса канала (youtube.com/channel/UC…). Ролики и цифры подтянутся ночью."],
    ["Когда лист не создаётся", "Лист не заводится для строк со статусом «Запланирован» без UC — это места под будущие каналы. Смените статус на «Куплен» или впишите UC."],
    ["Переименовать канал", "Поменяйте название в «Каналах». Вкладка переименуется сама. Строки «Очереди» со старым названием поменяйте тоже — иначе пул их не увидит («Проверка» напишет «канала нет в списке»)."],
    ["Убрать канал", "Поставьте статус «Выведен». Строку не удаляйте — пропадут лист и история."],
    ["Пул оригиналов", "На листе канала слева — оригиналы для рерайта. Сверху разгон: пока у канала меньше 10 роликов в «Очереди», идут сильнейшие ролики с просмотрами НИЖЕ 300 тыс. "
                       "(от меньших к большим). Дальше — 60 свободных от 300 тыс. просмотров, от самого сильного вниз: сначала своя серия, не хватит — сильнейшие из других серий "
                       "(в «Корзине» «из другой серии»). Внизу — уже взятые каналом. Взяли ролик — на его место встаёт следующий. Ролики, взятые другим каналом того же языка, не показываются. "
                       "Голубая строка — следующий. Числа 10, 60 и порог 300 тыс. меняются в «Конкурентах», в строке заголовка справа."],
    ["Взять оригинал в работу", "Скопируйте «Ссылку на оригинал» из пула и заведите строку в «Очереди»: дата, канал, тема, ссылка в «Донор», статус. Пул перекрасится сразу. "
                                "Один оригинал на разных языках — нормально; на двух каналах одного языка «Проверка» предупредит."],
    ["Запланировать заливку", "«Очередь»: дата, канал из списка, тема, ссылка на оригинал в «Донор», статус. Название оригинала подставится само. «Проверка» напишет: повтор оригинала на канале, "
                              "оригинал уже взят другим каналом этого языка, в «Донор» нет ссылки на ролик, просрочено, канала нет в списке."],
    ["Залили ролик", "В «Очереди»: статус «Залито» и ссылка на наш ролик в «Готовое видео». Строка пула станет зелёной со ссылкой на наш ролик, а в таблице роликов канала (справа) появится название оригинала."],
    ["Работа оператора за день", "Открыть «Сегодня». Слева — заливки на сегодня и просроченные, справа — каналы с проблемами. Сделал — поменяй статус в «Очереди» на «Залито»."],
    ["Конкуренты", "Список каналов-доноров. Добавить: новая строка — название, язык (EN/RU), ссылка на канал, галочка «Берём». Ночью его ролики снимутся, разложатся по сериям и лягут в «Оригиналы». "
                   "Снять галочку — его ролики уйдут из пулов (из «Оригиналов» не удаляются). В строке заголовка справа — настройки пула: минимальная длина, пороги «средне» и «много», минимум просмотров. Меняются там же, пул пересчитывается сразу."],
    ["Оригиналы", "Все длинные ролики конкурентов про Вторую мировую с цифрами. Руками можно: поправить «Подходит для серий» (названия серий через запятую, как в «Каналах»), "
                  "поставить «Не брать» (уйдёт из всех пулов), написать заметку. Просмотры и корзины обновляются ночью, новые ролики добавляются в конец."],
    ["Корзина", "Считается от медианы просмотров длинных роликов своего конкурента: мало — меньше 0,5 медианы (в пул не идёт), средне — от 0,5 до 2, много — от 2. Пороги меняются в «Конкурентах», в строке заголовка справа."],
    ["Почта сгорела", "«Почты» → галочка «Сгорела». Статус почты станет «сгорела», свободной она больше не считается."],
    ["Прокси умер", "«Профили» → галочка «Прокси мёртв». Все каналы на этом профиле получат пометку «мёртвый прокси» в колонке «Внимание»."],
    ["Статус (мой) и (авто)", "«Мой» ставите вы: куплен, льётся, пауза, монетизация. «Авто» — что видно на YouTube: нет на YouTube, пустой, грязный, льётся, встал. Если они расходятся, это попадёт во «Внимание»."],
    ["Что значит «Внимание»", "Нет UC, почты или профиля; мёртвый прокси; больше 7 дней без заливки; на канале чужие ролики; на одной почте больше 3 каналов или на профиле больше 3 почт."],
    ["Когда обновляется", "Каждые 10 минут: коды, листы, «Статус (авто)», «Внимание», дни без заливки. Раз в сутки около 06:00 по Вьетнаму: ролики, подписчики, просмотры с YouTube, строка в «Истории», "
                          "новые ролики конкурентов и их просмотры. Пул и статусы в нём — сразу: это формулы."],
    ["Лист канала", "Сверху — карточка из «Каналов» (править там) и сводка пула. Ниже слева — пул оригиналов, справа (с колонки K) — ролики канала с YouTube. "
                    "Руками на листе: диктор, о чём канал, описание и «Моя пометка» у роликов — она сохраняется при обновлении."],
    ["История", "Одна строка на канал в день: подписчики, просмотры, ролики. Из неё считается «+подписчиков за 7 дней»."],
    ["Монетизация", "YouTube не отдаёт её без входа в аккаунт — колонку «Монетизация» ведёте руками."],
    ["Старые каналы ядра", "12 каналов старой сетки (Минский бокс) в список не входят. Их почты и профили видны как «занята другой нишей», чтобы не взять их повторно."],
    ["Меню «ВВ2»", "Вверху таблицы, рядом со «Справкой». «Новый канал…» — форма со свободными почтами и профилями: создаёт строку, код и лист сразу. «Заливка в очередь…» — форма для «Очереди», "
                   "сразу показывает результат проверки. «Открыть лист канала» — выделите строку в «Каналах» или «Очереди». «Создать недостающие листы сейчас» — не ждать 10 минут."],
    ["Если меню нет", "Обновите страницу. Код меню хранится в файле ~/ops/ww2_sheet_menu.gs и ставится через Расширения → Apps Script."],
]

# ---------------- помощники таблицы ----------------
class Book:
    def __init__(self):
        self.gc = gspread.service_account(filename=SA, http_client=gspread.BackOffHTTPClient)
        self.sh = retry(self.gc.open_by_key, SHEET_ID)
        meta = retry(self.sh.fetch_sheet_metadata)
        loc = meta.get("properties", {}).get("locale", "en_US").lower()
        self.sep = ";" if loc.startswith(("ru", "de", "fr", "pl", "cs", "es", "it", "pt", "uk")) else ","
        self.reload()

    def reload(self):
        self.sheets = {ws.title: ws for ws in retry(self.sh.worksheets)}
        self.by_id = {ws.id: ws for ws in self.sheets.values()}

    def f(self, formula):            # ¦ — разделитель аргументов
        return formula.replace("¦", self.sep)

    def batch_values(self, data):
        for i in range(0, len(data), 200):
            retry(self.sh.values_batch_update, {"valueInputOption": "USER_ENTERED", "data": data[i:i + 200]})

    def batch(self, requests):
        for i in range(0, len(requests), 300):
            retry(self.sh.batch_update, {"requests": requests[i:i + 300]})

    def add_sheet(self, title, rows, cols, index=None):
        ws = retry(self.sh.add_worksheet, title=title, rows=rows, cols=cols, index=index)
        self.sheets[title] = ws; self.by_id[ws.id] = ws
        return ws


def safe_title(name, taken):
    t = re.sub(r"[\[\]\*\?/\\:']", " ", name).strip()[:90] or "Канал"
    base, k = t, 2
    while t in taken: t = f"{base} {k}"; k += 1
    return t


def header_fmt(sheet_id, col_from, col_to, manual, row=0):
    color = {"red": 0.87, "green": 0.93, "blue": 0.99} if manual else {"red": 0.90, "green": 0.90, "blue": 0.90}
    return {"repeatCell": {"range": {"sheetId": sheet_id, "startRowIndex": row, "endRowIndex": row + 1,
                                     "startColumnIndex": col_from, "endColumnIndex": col_to},
                           "cell": {"userEnteredFormat": {"backgroundColor": color, "wrapStrategy": "WRAP",
                                                          "textFormat": {"bold": True}, "verticalAlignment": "MIDDLE"}},
                           "fields": "userEnteredFormat(backgroundColor,textFormat,wrapStrategy,verticalAlignment)"}}


def widths(sheet_id, px):
    return [{"updateDimensionProperties": {"range": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": i, "endIndex": i + 1},
                                           "properties": {"pixelSize": w}, "fields": "pixelSize"}} for i, w in enumerate(px)]


def freeze(sheet_id, rows, cols=0):
    return {"updateSheetProperties": {"properties": {"sheetId": sheet_id, "gridProperties": {"frozenRowCount": rows, "frozenColumnCount": cols}},
                                      "fields": "gridProperties.frozenRowCount,gridProperties.frozenColumnCount"}}


def dropdown(sheet_id, col, values, r0=1, r1=ROWS, strict=False):
    return {"setDataValidation": {"range": {"sheetId": sheet_id, "startRowIndex": r0, "endRowIndex": r1, "startColumnIndex": col, "endColumnIndex": col + 1},
                                  "rule": {"condition": {"type": "ONE_OF_LIST", "values": [{"userEnteredValue": v} for v in values]},
                                           "showCustomUi": True, "strict": strict}}}


def dropdown_range(sheet_id, col, source, r0=1, r1=ROWS):
    return {"setDataValidation": {"range": {"sheetId": sheet_id, "startRowIndex": r0, "endRowIndex": r1, "startColumnIndex": col, "endColumnIndex": col + 1},
                                  "rule": {"condition": {"type": "ONE_OF_RANGE", "values": [{"userEnteredValue": source}]},
                                           "showCustomUi": True, "strict": False}}}


def checkbox(sheet_id, col, r0=1, r1=ROWS):
    return {"setDataValidation": {"range": {"sheetId": sheet_id, "startRowIndex": r0, "endRowIndex": r1, "startColumnIndex": col, "endColumnIndex": col + 1},
                                  "rule": {"condition": {"type": "BOOLEAN"}, "showCustomUi": True}}}


def wrap(sheet_id, col, r0=1, r1=ROWS):
    return {"repeatCell": {"range": {"sheetId": sheet_id, "startRowIndex": r0, "endRowIndex": r1, "startColumnIndex": col, "endColumnIndex": col + 1},
                           "cell": {"userEnteredFormat": {"wrapStrategy": "WRAP", "verticalAlignment": "TOP"}},
                           "fields": "userEnteredFormat(wrapStrategy,verticalAlignment)"}}


def date_fmt(sheet_id, col, r0=1, r1=ROWS):
    return {"repeatCell": {"range": {"sheetId": sheet_id, "startRowIndex": r0, "endRowIndex": r1, "startColumnIndex": col, "endColumnIndex": col + 1},
                           "cell": {"userEnteredFormat": {"numberFormat": {"type": "DATE", "pattern": "dd.mm.yyyy"}}},
                           "fields": "userEnteredFormat.numberFormat"}}


def num_fmt(sheet_id, col, r0=1, r1=ROWS):
    return {"repeatCell": {"range": {"sheetId": sheet_id, "startRowIndex": r0, "endRowIndex": r1, "startColumnIndex": col, "endColumnIndex": col + 1},
                           "cell": {"userEnteredFormat": {"numberFormat": {"type": "NUMBER", "pattern": "#,##0"}}},
                           "fields": "userEnteredFormat.numberFormat"}}


def cond(sheet_id, col_from, col_to, formula, bg=None, fg=None, bold=False, r0=1, r1=ROWS):
    fmt = {}
    if bg: fmt["backgroundColor"] = bg
    if fg or bold: fmt["textFormat"] = {**({"foregroundColor": fg} if fg else {}), **({"bold": True} if bold else {})}
    return {"addConditionalFormatRule": {"rule": {
        "ranges": [{"sheetId": sheet_id, "startRowIndex": r0, "endRowIndex": r1, "startColumnIndex": col_from, "endColumnIndex": col_to}],
        "booleanRule": {"condition": {"type": "CUSTOM_FORMULA", "values": [{"userEnteredValue": formula}]}, "format": fmt}}, "index": 0}}


def protect_warn(sheet_id, desc, c0=None, c1=None, r0=None, r1=None):
    rng = {"sheetId": sheet_id}
    if c0 is not None: rng.update({"startColumnIndex": c0, "endColumnIndex": c1})
    if r0 is not None: rng.update({"startRowIndex": r0, "endRowIndex": r1})
    return {"addProtectedRange": {"protectedRange": {"range": rng, "description": desc, "warningOnly": True}}}


RED = {"red": 0.99, "green": 0.87, "blue": 0.86}
GREEN = {"red": 0.85, "green": 0.94, "blue": 0.86}
AMBER = {"red": 1.0, "green": 0.95, "blue": 0.80}
GREY_TXT = {"red": 0.45, "green": 0.45, "blue": 0.45}


# ---------------- «Каналы»: чтение ----------------
def read_channels(bk):
    ws = bk.sheets["Каналы"]
    vals = retry(ws.get_all_values)
    forms = retry(ws.get, f"C1:C{max(len(vals), 2)}", value_render_option="FORMULA")
    hdr = vals[0] if vals else []
    rows = []
    for i, r in enumerate(vals[1:], start=2):
        r = r + [""] * (len(KEYS) - len(r))
        d = {k: r[IX[k]].strip() for k in KEYS}
        d["_row"] = i
        fc = forms[i - 1][0] if i - 1 < len(forms) and forms[i - 1] else ""
        m = re.search(r"#gid=(\d+)", fc or "")
        d["_gid"] = int(m.group(1)) if m else None
        if d["name"] or d["uc"]: rows.append(d)
    return ws, hdr, rows


# ---------------- лист канала ----------------
def card_values(bk, code):
    P = lambda key: bk.f(f"=IFERROR(INDEX('Каналы'!{L(key)}:{L(key)}¦MATCH($B$2¦'Каналы'!A:A¦0))¦\"\")")
    uc = f"INDEX('Каналы'!{L('uc')}:{L('uc')}¦MATCH($B$2¦'Каналы'!A:A¦0))"
    return [
        [bk.f("=IFERROR(INDEX('Каналы'!B:B¦MATCH($B$2¦'Каналы'!A:A¦0))¦\"канал удалён из списка\")"), "",
         bk.f("=HYPERLINK(\"#gid=" + str(bk.sheets["Каналы"].id) + "\"¦\"← все каналы\")")],
        ["Код", code],
        ["Язык", P("lang")], ["Серия", P("series")], ["Когда заведён", P("started")],
        ["Статус (мой)", P("status_my")], ["Статус (авто)", P("status_auto")], ["Внимание", P("alert")],
        ["Почта", P("email")], ["Профиль AdsPower", P("profile")], ["Машина", P("machine")], ["Монетизация", P("monet")],
        ["Канал на YouTube", bk.f(f"=IFERROR(IF({uc}=\"\"¦\"UC ещё не вписан\"¦HYPERLINK(\"https://www.youtube.com/channel/\"&{uc}¦\"открыть канал\"))¦\"\")")],
        ["Студия", bk.f(f"=IFERROR(IF({uc}=\"\"¦\"\"¦HYPERLINK(\"https://studio.youtube.com/channel/\"&{uc}¦\"открыть студию\"))¦\"\")")],
        ["Хендл", P("handle")], ["Создан на YouTube", P("created")], ["Роликов ВВ2", P("videos")],
        ["Последняя заливка", P("last")], ["Подписчиков", P("subs")], ["Прирост подписчиков за 7 дн", P("subs7")],
        ["Просмотров", P("views")], ["Заметка (из «Каналов»)", P("note")],
    ]


def card_manual_labels():
    return [["Диктор", ""], ["О чём канал", ""], ["Описание на языке канала", ""]]


def card_protections(sid, rows=pool.CARD_ROWS_MIN):
    return [protect_warn(sid, "Карточка подтягивается из «Каналов» — меняйте там", 1, 2, 1, 22)] + pool.channel_protections(sid, rows)


def card_format(bk, ws, rows=None):
    sid = ws.id
    rows = rows or max(ws.row_count, pool.CARD_ROWS_MIN)
    req = [
        {"repeatCell": {"range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": 2},
                        "cell": {"userEnteredFormat": {"textFormat": {"bold": True, "fontSize": 15}}}, "fields": "userEnteredFormat.textFormat"}},
        {"repeatCell": {"range": {"sheetId": sid, "startRowIndex": 1, "endRowIndex": 26, "startColumnIndex": 0, "endColumnIndex": 1},
                        "cell": {"userEnteredFormat": {"textFormat": {"bold": True}, "backgroundColor": {"red": 0.95, "green": 0.95, "blue": 0.95}}},
                        "fields": "userEnteredFormat(textFormat,backgroundColor)"}},
        {"repeatCell": {"range": {"sheetId": sid, "startRowIndex": 22, "endRowIndex": 25, "startColumnIndex": 1, "endColumnIndex": 2},
                        "cell": {"userEnteredFormat": {"backgroundColor": {"red": 0.93, "green": 0.96, "blue": 1.0}, "wrapStrategy": "WRAP"}},
                        "fields": "userEnteredFormat(backgroundColor,wrapStrategy)"}},
        cond(sid, 1, 2, '=AND(ROW()=8;B8<>"")', bg=RED, bold=True, r0=7, r1=8),
        date_fmt(sid, 1, 4, 5), date_fmt(sid, 1, 15, 16), date_fmt(sid, 1, 17, 18),
        num_fmt(sid, 1, 16, 17), num_fmt(sid, 1, 18, 21),
    ]
    # формулы условного формата в ru-локали принимают «;» — подставляем фактический разделитель
    for q in req:
        if "addConditionalFormatRule" in q:
            v = q["addConditionalFormatRule"]["rule"]["booleanRule"]["condition"]["values"][0]
            v["userEnteredValue"] = v["userEnteredValue"].replace(";", bk.sep)
    return req + pool.channel_layout_requests(bk, sid, rows)


def card_layout_values(bk, title):
    """Заголовки обеих таблиц, формулы пула и сводки — то, что есть на каждом листе канала."""
    return [{"range": f"'{title}'!{d['range']}", "values": d["values"]} for d in pool.channel_layout_values(bk)]


def ensure_template(bk):
    if TEMPLATE in bk.sheets: return bk.sheets[TEMPLATE]
    ws = bk.add_sheet(TEMPLATE, rows=pool.CARD_ROWS_MIN, cols=pool.CARD_COLS)
    bk.batch_values([
        {"range": f"'{TEMPLATE}'!A1:C22", "values": card_values(bk, "")},
        {"range": f"'{TEMPLATE}'!A23:B25", "values": [["Диктор", ""], ["О чём канал", ""], ["Описание на языке канала", ""]]},
        {"range": f"'{TEMPLATE}'!{VCOL(0)}{CARD_ROW_VIDEOS + 1}:{VCOL(1)}{CARD_ROW_VIDEOS + 1}", "values": [["", "роликов пока нет — появятся после ночного обновления"]]},
    ] + card_layout_values(bk, TEMPLATE))
    bk.batch(card_format(bk, ws) + [{"updateSheetProperties": {"properties": {"sheetId": ws.id, "hidden": True}, "fields": "hidden"}}])
    log("создан скрытый «Шаблон канала»")
    return ws


def create_channel_sheet(bk, code, name, manual=None):
    tpl = ensure_template(bk)
    title = safe_title(name, set(bk.sheets))
    res = retry(bk.sh.batch_update, {"requests": [{"duplicateSheet": {"sourceSheetId": tpl.id, "insertSheetIndex": len(bk.sheets), "newSheetName": title}}]})
    sid = res["replies"][0]["duplicateSheet"]["properties"]["sheetId"]
    bk.batch([{"updateSheetProperties": {"properties": {"sheetId": sid, "hidden": False}, "fields": "hidden"}}] + card_protections(sid))
    bk.reload()
    manual = manual or {}
    bk.batch_values([{"range": f"'{title}'!B2", "values": [[code]]},
                     {"range": f"'{title}'!B23:B25", "values": [[manual.get("narrator", "")], [manual.get("desc_ru", "")], [manual.get("desc", "")]]}])
    return bk.by_id[sid]


# ---------------- light: коды, листы, статусы, внимание ----------------
def dead_profiles(bk):
    ws = bk.sheets.get("Профили")
    if not ws: return set()
    vals = retry(ws.get_all_values)
    return {r[0].strip() for r in vals[1:] if len(r) > 3 and r[0].strip() and r[3].strip().upper() in ("TRUE", "ИСТИНА")}


def follow_renames(bk, rows):
    """Переименовали канал в «Каналах» — старое имя в «Очереди» меняем на новое, иначе пул и «занят:» теряют его строки."""
    f = Path(__file__).resolve().parent / "ww2_names.json"
    try: known = json.loads(f.read_text())
    except (OSError, ValueError): known = {}
    renamed = {known[r["code"]]: r["name"] for r in rows if r["code"] and known.get(r["code"]) and known[r["code"]] != r["name"] and r["name"]}
    q = bk.sheets.get("Очередь")
    if renamed and q is not None:
        hdr = retry(q.get, "A1:Z1")[0]
        if "Канал" in hdr:
            c = letter(hdr.index("Канал") + 1)
            vals = retry(q.get, f"{c}2:{c}{q.row_count}")
            data = [{"range": f"'Очередь'!{c}{i + 2}", "values": [[renamed[v[0]]]]} for i, v in enumerate(vals) if v and v[0] in renamed]
            if data: bk.batch_values(data)
            log(f"переименования каналов перенесены в «Очередь»: {renamed}, строк {len(data)}")
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps({r["code"]: r["name"] for r in rows if r["code"] and r["name"]}, ensure_ascii=False))
    os.replace(tmp, f)


def light(bk, youtube_rows=None):
    pool.ensure_structure(bk, log)
    ensure_template(bk)
    ws, hdr, rows = read_channels(bk)
    follow_renames(bk, rows)
    data = []
    # 1. коды
    used = [int(m.group(1)) for r in rows for m in [re.match(r"К-(\d+)", r["code"])] if m]
    nxt = max(used, default=0) + 1
    for r in rows:
        if not r["code"]:
            r["code"] = f"К-{nxt:03d}"; nxt += 1
            data.append({"range": f"'Каналы'!{L('code')}{r['_row']}", "values": [[r["code"]]]})
    if data: bk.batch_values(data); data = []
    # 2. листы: создать недостающие, переименовать вкладку при смене названия
    created = 0
    for r in rows:
        if not r["name"] or (r["status_my"] in NO_SHEET_STATUS and not r["uc"]): continue
        ws_ch = bk.by_id.get(r["_gid"]) if r["_gid"] else None
        if ws_ch is None:
            ws_ch = create_channel_sheet(bk, r["code"], r["name"]); created += 1
        elif ws_ch.title != safe_title(r["name"], set(bk.sheets) - {ws_ch.title}):
            new_t = safe_title(r["name"], set(bk.sheets) - {ws_ch.title})
            old = ws_ch.title
            retry(ws_ch.update_title, new_t); bk.reload(); ws_ch = bk.by_id[ws_ch.id]
            log(f"вкладка переименована: {old} → {new_t}")
        r["_gid"] = ws_ch.id
        data.append({"range": f"'Каналы'!{L('sheet')}{r['_row']}",
                     "values": [[bk.f(f'=HYPERLINK("#gid={ws_ch.id}"¦"открыть")')]]})
        data.append({"range": f"'Каналы'!{L('pool_free')}{r['_row']}", "values": [[f"='{ws_ch.title}'!E4"]]})
    # 3. статус (авто), дни без заливки, внимание
    dead = dead_profiles(bk)
    mail_load, prof_mails = {}, {}
    for r in rows:
        if r["status_my"] in ("Выведен",): continue
        if r["email"]: mail_load[r["email"]] = mail_load.get(r["email"], 0) + 1
        if r["profile"] and r["email"]: prof_mails.setdefault(r["profile"], set()).add(r["email"])
    for r in rows:
        last = parse_date(r["last"]); videos = to_int(r["videos"]); foreign = to_int(r["foreign"])
        if not r["uc"]: auto = "Нет на YouTube"
        elif youtube_rows is None and not r["last"] and not videos and not foreign and not r["created"]: auto = r["status_auto"] or ""
        elif not videos and not foreign: auto = "Пустой"
        elif foreign and videos <= 2: auto = "Грязный"
        elif last and (TODAY - last).days <= 3: auto = "Льётся"
        else: auto = "Встал" if videos else "Пустой"
        idle = (TODAY - last).days if last else ""
        alerts = []
        planned = r["status_my"] in ("Запланирован",)
        resting = r["status_my"] in ("Пауза", "Выведен", "Запланирован")
        if not planned:
            if not r["uc"]: alerts.append("впишите UC канала")
            if not r["email"]: alerts.append("нет почты")
            if not r["profile"]: alerts.append("нет профиля")
        if r["profile"] and r["profile"] in dead: alerts.append("мёртвый прокси")
        if not resting and isinstance(idle, int) and idle > 7: alerts.append(f"{idle} дн без заливки")
        if foreign and videos <= 2: alerts.append("на канале чужие ролики")
        if r["status_my"] == "Льётся" and auto in ("Встал", "Пустой"): alerts.append("статус расходится с YouTube")
        if r["email"] and mail_load.get(r["email"], 0) > 3: alerts.append("на почте больше 3 каналов")
        if r["profile"] and len(prof_mails.get(r["profile"], ())) > 3: alerts.append("на профиле больше 3 почт")
        data.append({"range": f"'Каналы'!{L('status_auto')}{r['_row']}:{L('alert')}{r['_row']}", "values": [[auto, "; ".join(alerts)]]})
        data.append({"range": f"'Каналы'!{L('idle')}{r['_row']}", "values": [[idle]]})
    if data: bk.batch_values(data)
    pool.ensure_pool_rows(bk, rows, log)
    log(f"строк: {len(rows)}, новых листов: {created}")
    return rows


# ---------------- sync: YouTube + история ----------------
def read_card_videos(bk, rows):
    ranges, owners = [], []
    for r in rows:
        ws_ch = bk.by_id.get(r["_gid"]) if r["_gid"] else None
        if ws_ch and r["uc"]:
            ranges.append(f"'{ws_ch.title}'!{VCOL(0)}{CARD_ROW_VIDEOS + 1}:{VCOL(6)}{CARD_ROW_VIDEOS + MAX_VIDEOS + 40}"); owners.append(r["code"])
    got = {}
    for i in range(0, len(ranges), 50):
        res = retry(bk.sh.values_batch_get, ranges[i:i + 50])
        for code, vr in zip(owners[i:i + 50], res.get("valueRanges", [])):
            known = {}
            for row in vr.get("values", []):
                row = row + [""] * (7 - len(row))
                m = re.search(r"youtu\.be/([\w-]{11})", row[4])
                if m: known[m.group(1)] = row[6]
            got[code] = known
    return got


def fetch_channel(uc, known_ids):
    info = yt("channels", part="snippet,statistics,contentDetails", id=uc).get("items", [])
    if not info: return None
    it = info[0]; st = it["statistics"]
    out = {"created": it["snippet"]["publishedAt"][:10], "subs": int(st.get("subscriberCount", 0)),
           "views": int(st.get("viewCount", 0)), "handle": it["snippet"].get("customUrl", ""), "items": []}
    ids, token = [], None
    while True:
        try:
            r = yt("playlistItems", part="contentDetails", playlistId=it["contentDetails"]["relatedPlaylists"]["uploads"],
                   maxResults=50, **({"pageToken": token} if token else {}))
        except urllib.error.HTTPError as e:
            if e.code == 404: break
            raise
        page = [x["contentDetails"]["videoId"] for x in r.get("items", [])]
        ids += page
        token = r.get("nextPageToken")
        # новые ролики идут первыми: если на странице уже есть известный — дальше старое
        if not token or len(ids) >= MAX_VIDEOS or any(v in known_ids for v in page): break
    ids = list(dict.fromkeys(ids + list(known_ids)))[:MAX_VIDEOS]
    for i in range(0, len(ids), 50):
        for v in yt("videos", part="snippet,statistics,contentDetails", id=",".join(ids[i:i + 50])).get("items", []):
            out["items"].append({"id": v["id"], "date": v["snippet"]["publishedAt"][:10], "title": v["snippet"]["title"],
                                 "dur": iso_dur(v["contentDetails"].get("duration")), "views": int(v["statistics"].get("viewCount", 0))})
    out["items"].sort(key=lambda x: x["date"], reverse=True)
    return out


def read_history(bk):
    ws = bk.sheets["История"]
    vals = retry(ws.get_all_values)
    hist = {}
    for r in vals[1:]:
        if len(r) < 4: continue
        d = parse_date(r[0])
        if d: hist.setdefault(r[1], []).append((d, to_int(r[3])))
    return ws, vals, hist


def sync(bk):
    rows = light(bk)
    bk.reload()
    known = read_card_videos(bk, rows)
    hws, hvals, hist = read_history(bk)
    today_logged = {r[1] for r in hvals[1:] if len(r) > 1 and parse_date(r[0]) == TODAY}
    data, hist_rows, n_new = [], [], 0
    for r in rows:
        if not r["uc"]: continue
        y = fetch_channel(r["uc"], set(known.get(r["code"], {})))
        if not y: log(f"YouTube не нашёл канал {r['name']} ({r['uc']})"); continue
        items = y["items"]
        ww2 = [v for v in items if dt.date.fromisoformat(v["date"]) >= WW2_START]
        foreign = [v for v in items if dt.date.fromisoformat(v["date"]) < WW2_START]
        best = max(ww2, key=lambda v: v["views"]) if ww2 else None
        week = sum(1 for v in ww2 if (TODAY - dt.date.fromisoformat(v["date"])).days <= 7)
        past = [s for d, s in hist.get(r["code"], []) if 6 <= (TODAY - d).days <= 9]
        subs7 = y["subs"] - past[-1] if past else ""
        n_new += len(set(v["id"] for v in items) - set(known.get(r["code"], {})))
        row = r["_row"]
        auto = {
            "yt": bk.f(f'=HYPERLINK("https://www.youtube.com/channel/{r["uc"]}"¦"открыть")'),
            "studio": bk.f(f'=HYPERLINK("https://studio.youtube.com/channel/{r["uc"]}"¦"студия")'),
            "handle": y["handle"], "created": y["created"], "videos": len(ww2),
            "last": ww2[0]["date"] if ww2 else "", "week": week, "subs": y["subs"], "subs7": subs7, "views": y["views"],
            "best_v": best["views"] if best else "",
            "best": bk.f(f'=HYPERLINK("https://youtu.be/{best["id"]}"¦"{best["title"][:70].replace(chr(34), "")}")') if best else "",
            "foreign": len(foreign),
        }
        for k, v in auto.items():
            data.append({"range": f"'Каналы'!{L(k)}{row}", "values": [[v]]})
        ws_ch = bk.by_id.get(r["_gid"])
        if ws_ch:
            marks = known.get(r["code"], {})
            vrows = [[v["date"], v["title"], v["dur"], v["views"],
                      bk.f(f'=HYPERLINK("https://youtu.be/{v["id"]}"¦"youtu.be/{v["id"]}")'),
                      "ВВ2" if dt.date.fromisoformat(v["date"]) >= WW2_START else "чужая тема", marks.get(v["id"], "")] for v in items]
            blank = [[""] * 7] * max(0, MAX_VIDEOS + 30 - len(vrows))
            data.append({"range": f"'{ws_ch.title}'!{VCOL(0)}{CARD_ROW_VIDEOS + 1}:{VCOL(6)}{CARD_ROW_VIDEOS + len(vrows) + len(blank)}",
                         "values": (vrows or [["", "роликов пока нет", "", "", "", "", ""]]) + blank[: (MAX_VIDEOS + 30 - max(len(vrows), 1))]})
        if r["code"] not in today_logged:
            hist_rows.append([TODAY.isoformat(), r["code"], r["name"], y["subs"], y["views"], len(ww2)])
    bk.batch_values(data)
    if hist_rows:
        start = len(hvals) + 1
        if hws.row_count < start + len(hist_rows):
            retry(hws.add_rows, len(hist_rows) + 500)
        bk.batch_values([{"range": f"'История'!A{start}:F{start + len(hist_rows) - 1}", "values": hist_rows}])
    log(f"YouTube: каналов {sum(1 for r in rows if r['uc'])}, новых роликов {n_new}, в историю {len(hist_rows)}")
    light(bk, youtube_rows=True)          # статусы и внимание уже по свежим цифрам
    retry(bk.sheets["Сводка"].update, range_name="B1", values=[[f"цифры YouTube на {NOW:%d.%m.%Y %H:%M} (UTC+7)"]])


# ---------------- rebuild: новая структура ----------------
def load_concepts():
    out = {}
    def walk(o):
        if isinstance(o, dict):
            nm = o.get("name") or o.get("youtube_name")
            if nm and (o.get("narratorName") or o.get("descriptionRu") or o.get("description")):
                key = re.sub(r"\s*(\((ES|PT|UA|HR|GR|HU)\)|\b(ES|PT|UA|HR|GR|HU))\s*$", "", str(nm)).strip()
                out[key] = {"narrator": o.get("narratorName") or "", "desc_ru": o.get("descriptionRu") or "", "desc": o.get("description") or ""}
            for v in o.values(): walk(v)
        elif isinstance(o, list):
            for v in o: walk(v)
    for f in [NICHES / "греция_2026-08/_концепты.json", *(NICHES / "укр_порт_исп_2026-08").glob("_*_концепты.json")]:
        try: walk(json.load(open(f)))
        except Exception as e: log("концепты:", f.name, e)
    md = NICHES / "ww2/новые_каналы_2026-08-18.md"
    if md.exists():
        for m in re.finditer(r"### \w+ — \*\*(.+?)\*\*[^\n]*\n(?:\*(.+?)\*\n)?((?:>.*\n?)+)", md.read_text()):
            out[m.group(1).strip()] = {"narrator": "", "desc_ru": (m.group(2) or "").strip(),
                                       "desc": " ".join(l.lstrip("> ").strip() for l in m.group(3).splitlines() if l.strip())}
    return out


def rebuild(bk):
    # 1. прежние «Каналы» → ручные данные по заголовкам
    old = retry(bk.sheets["Каналы"].get_all_values) if "Каналы" in bk.sheets else []
    oh = old[0] if old else []
    def g(r, *names):
        for n in names:
            if n in oh and oh.index(n) < len(r) and r[oh.index(n)].strip(): return r[oh.index(n)].strip()
        return ""
    reg = sqlite3.connect(REG); reg.row_factory = sqlite3.Row
    regch = {r["uc"]: dict(r) for r in reg.execute("SELECT * FROM channels")}
    profiles = [dict(r) for r in reg.execute("SELECT * FROM profiles ORDER BY cast(profile_id as int)")]
    concepts = load_concepts()
    ch_rows = []
    for r in old[1:]:
        name = g(r, "Название канала", "Канал")
        uc = g(r, "UC канала", "UC")
        if not name and not uc: continue
        planned = name.startswith("— новый")
        if planned:
            m = re.match(r"— новый (\S+) (\d+) —", name)
            name = f"(новый) {m.group(1)} {m.group(2)}" if m else name
        started = g(r, "Когда заведён", "Когда заведены"); started = "" if started == "ещё нет" else started
        status_old = g(r, "Статус (мой)", "Статус")
        status_my = "Запланирован" if planned else ("Льётся" if status_old == "Льётся" else "Куплен")
        machine = g(r, "Машина"); machine = "Mac" if machine.lower() == "mac" else machine
        ch_rows.append({"name": name, "lang": g(r, "Язык"), "series": g(r, "Серия"), "started": started,
                        "status_my": status_my, "email": g(r, "Почта"), "profile": g(r, "Профиль"), "machine": machine,
                        "monet": g(r, "Монетизация") or ("" if planned else "Нет"), "note": g(r, "Заметка"), "uc": uc})
    log(f"перенос каналов: {len(ch_rows)}")

    # 2. удалить старые вкладки (кроме тех, что пересоздадим ниже по имени — всё пересоздаётся в последний раз)
    keep_new = ["Сегодня", "Сводка", "Каналы", "Очередь", "Почты", "Профили", "История", "Как пользоваться"]
    tmp = bk.add_sheet(f"_tmp_{int(time.time())}", 1, 1)
    for t, w in list(bk.sheets.items()):
        if w.id != tmp.id and t not in (pool.KONK, pool.ORIG):     # конкурентов и базу оригиналов с ручными пометками не трогаем
            retry(bk.sh.del_worksheet, w); time.sleep(0.3)
    bk.reload()
    S = {}
    sizes = {"Сегодня": (300, 14), "Сводка": (80, 13), "Каналы": (ROWS, len(KEYS)), "Очередь": (ROWS, 12),
             "Почты": (400, 8), "Профили": (200, 9), "История": (3000, 6), "Как пользоваться": (40, 2)}
    for i, t in enumerate(keep_new):
        S[t] = bk.add_sheet(t, *sizes[t], index=i)
    retry(bk.sh.del_worksheet, tmp); bk.reload()
    sid = {t: S[t].id for t in S}

    # 3. «Каналы»
    ch_values = [[f[1] for f in FIELDS]]
    for i, c in enumerate(ch_rows, start=1):
        row = [""] * len(KEYS)
        row[IX["code"]] = f"К-{i:03d}"
        for k in ("name", "lang", "series", "started", "status_my", "email", "profile", "machine", "monet", "note", "uc"):
            row[IX[k]] = c[k]
        ch_values.append(row)
    bk.batch_values([{"range": f"'Каналы'!A1:{letter(len(KEYS))}{len(ch_values)}", "values": ch_values}])

    # 4. «Очередь»
    bk.batch_values([{"range": "'Очередь'!A1:I2", "values": [
        ["Дата", "Канал", "Тема по-русски", "Донор (ссылка или название)", "Статус", "Кто делает", "Готовое видео / ссылка", "Проверка", "Заметка"],
        ["", "", "", "", "", "", "", "", ""]]}])      # «Проверка» и служебные колонки J–L ставит pool.ensure_structure

    # 5. «Почты»: занятость считается формулами из «Каналов»; другие ниши засеяны из реестра
    ww2_ucs = {c["uc"] for c in ch_rows if c["uc"]}
    other_mail, other_prof = {}, {}
    for uc, c in regch.items():
        if uc in ww2_ucs: continue
        label = "ВВ2 старое ядро" if c["niche"] == "ww2" else c["niche"]
        if c.get("email"): other_mail.setdefault(c["email"], []).append(f'{label}: {c["name"]}')
        if c.get("profile_id"): other_prof.setdefault(str(c["profile_id"]), []).append(f'{label}: {c["name"]}')
    free_src = {}
    fm = NICHES / "_свободные_почты_2026-08-18.txt"
    if fm.exists():
        src = ""
        for line in fm.read_text().splitlines():
            if line.startswith("#"): src = line.lstrip("# ").split(" (")[0]
            elif "@" in line: free_src[line.strip()] = src
    mails = sorted({c["email"] for c in ch_rows if c["email"]} | set(other_mail) | set(free_src))
    mv = [["Почта", "Сгорела", "Статус", "Каналов ВВ2", "Каналы ВВ2", "Занята другой нишей", "Откуда", "Заметка"]]
    for i, m in enumerate(mails, start=2):
        mv.append([m, False,
                   bk.f(f'=IF(A{i}=""¦""¦IF(B{i}=TRUE¦"сгорела"¦IF(OR(D{i}>0¦F{i}<>"")¦"занята"¦IF(LEFT(H{i}¦12)="не проверена"¦"не проверена"¦"свободна"))))'),
                   bk.f(f"=IF(A{i}=\"\"¦\"\"¦COUNTIF('Каналы'!$J$2:$J${ROWS}¦A{i}))"),
                   bk.f(f"=IF(OR(A{i}=\"\"¦D{i}=0)¦\"\"¦TEXTJOIN(\", \"¦TRUE¦FILTER('Каналы'!$B$2:$B${ROWS}¦'Каналы'!$J$2:$J${ROWS}=A{i})))"),
                   "; ".join(other_mail.get(m, [])), free_src.get(m, ""), "пароль и 2FA — в файле закупки" if m in free_src else ""])
    for i in range(len(mv) + 1, 301):   # пустые строки с формулами под новые почты
        mv.append(["", False, bk.f(f'=IF(A{i}=""¦""¦IF(B{i}=TRUE¦"сгорела"¦IF(OR(D{i}>0¦F{i}<>"")¦"занята"¦IF(LEFT(H{i}¦12)="не проверена"¦"не проверена"¦"свободна"))))'),
                   bk.f(f"=IF(A{i}=\"\"¦\"\"¦COUNTIF('Каналы'!$J$2:$J${ROWS}¦A{i}))"),
                   bk.f(f"=IF(OR(A{i}=\"\"¦D{i}=0)¦\"\"¦TEXTJOIN(\", \"¦TRUE¦FILTER('Каналы'!$B$2:$B${ROWS}¦'Каналы'!$J$2:$J${ROWS}=A{i})))"), "", "", ""])
    bk.batch_values([{"range": f"'Почты'!A1:H{len(mv)}", "values": mv}])

    # 6. «Профили»
    pv = [["№ профиля", "Имя в AdsPower", "Машина", "Прокси мёртв", "Статус", "Каналов ВВ2", "Каналы ВВ2", "Занят другой нишей", "Заметка / прокси"]]
    for i, p in enumerate(profiles, start=2):
        pid = str(p["profile_id"]); note = p["proxy_note"] or ""
        dead = "МЁРТВ" in note.upper() or "FAIL" in note.upper()
        pv.append([int(pid) if pid.isdigit() else pid, p["name"] or "",
                   {"mac": "Mac", "minsk": "Минский бокс", "lenovo": "Lenovo"}.get(p["device"] or "", ""), dead,
                   bk.f(f'=IF(A{i}=""¦""¦IF(D{i}=TRUE¦"мёртвый прокси"¦IF(OR(F{i}>0¦H{i}<>"")¦"занят"¦"свободен")))'),
                   bk.f(f"=IF(A{i}=\"\"¦\"\"¦SUMPRODUCT(--(TO_TEXT('Каналы'!$K$2:$K${ROWS})=TO_TEXT(A{i}))))"),
                   bk.f(f"=IF(OR(A{i}=\"\"¦F{i}=0)¦\"\"¦TEXTJOIN(\", \"¦TRUE¦FILTER('Каналы'!$B$2:$B${ROWS}¦TO_TEXT('Каналы'!$K$2:$K${ROWS})=TO_TEXT(A{i}))))"),
                   "; ".join(other_prof.get(pid, [])), note])
    for i in range(len(pv) + 1, 151):
        pv.append(["", "", "", False, bk.f(f'=IF(A{i}=""¦""¦IF(D{i}=TRUE¦"мёртвый прокси"¦IF(OR(F{i}>0¦H{i}<>"")¦"занят"¦"свободен")))'),
                   bk.f(f"=IF(A{i}=\"\"¦\"\"¦SUMPRODUCT(--(TO_TEXT('Каналы'!$K$2:$K${ROWS})=TO_TEXT(A{i}))))"),
                   bk.f(f"=IF(OR(A{i}=\"\"¦F{i}=0)¦\"\"¦TEXTJOIN(\", \"¦TRUE¦FILTER('Каналы'!$B$2:$B${ROWS}¦TO_TEXT('Каналы'!$K$2:$K${ROWS})=TO_TEXT(A{i}))))"), "", ""])
    bk.batch_values([{"range": f"'Профили'!A1:I{len(pv)}", "values": pv}])

    # 7. «История»
    bk.batch_values([{"range": "'История'!A1:F1", "values": [["Дата", "Код", "Канал", "Подписчиков", "Просмотров", "Роликов ВВ2"]]}])

    # 8. «Сводка»
    K = lambda key: f"'Каналы'!${L(key)}$2:${L(key)}${ROWS}"
    sv = [["Сводка ВВ2", "цифры YouTube появятся после ночного обновления"], [],
          ["Язык", "Каналов", "Льётся", "Встал", "Роликов ВВ2", "За 7 дн", "Подписчиков", "Прирост за 7 дн", "Просмотров", "Лучший ролик", "Запланировано", "Требуют внимания"]]
    for lang in LANGS:
        sv.append([lang,
                   bk.f(f'=COUNTIFS({K("lang")}¦"{lang}"¦{K("status_my")}¦"<>Запланирован"¦{K("name")}¦"<>")'),
                   bk.f(f'=COUNTIFS({K("lang")}¦"{lang}"¦{K("status_auto")}¦"Льётся")'),
                   bk.f(f'=COUNTIFS({K("lang")}¦"{lang}"¦{K("status_auto")}¦"Встал")'),
                   bk.f(f'=SUMIF({K("lang")}¦"{lang}"¦{K("videos")})'), bk.f(f'=SUMIF({K("lang")}¦"{lang}"¦{K("week")})'),
                   bk.f(f'=SUMIF({K("lang")}¦"{lang}"¦{K("subs")})'), bk.f(f'=SUMIF({K("lang")}¦"{lang}"¦{K("subs7")})'),
                   bk.f(f'=SUMIF({K("lang")}¦"{lang}"¦{K("views")})'), bk.f(f'=MAXIFS({K("best_v")}¦{K("lang")}¦"{lang}")'),
                   bk.f(f'=COUNTIFS({K("lang")}¦"{lang}"¦{K("status_my")}¦"Запланирован")'),
                   bk.f(f'=COUNTIFS({K("lang")}¦"{lang}"¦{K("alert")}¦"?*")')])
    sv[2].append("Свободно в пуле")
    for i, lang in enumerate(LANGS):
        sv[3 + i].append(bk.f(f'=SUMIF({K("lang")}¦"{lang}"¦{K("pool_free")})'))
    n = len(sv)
    sv.append(["Итого"] + [bk.f(f"=SUM({letter(c)}4:{letter(c)}{n})") for c in range(2, 10)] + [bk.f(f"=MAX(J4:J{n})"), bk.f(f"=SUM(K4:K{n})"), bk.f(f"=SUM(L4:L{n})"), bk.f(f"=SUM(M4:M{n})")])
    dates = sorted({parse_date(c["started"]) for c in ch_rows if parse_date(c["started"])})
    sv += [[], ["Когда заведены", "Каналов", "Льётся", "Встал", "Роликов ВВ2", "За 7 дн", "Подписчиков", "Прирост за 7 дн", "Просмотров", "Лучший ролик"]]
    for d in dates:
        crit = f"DATE({d.year}¦{d.month}¦{d.day})"
        sv.append([d.strftime("%d.%m.%Y"),
                   bk.f(f'=COUNTIF({K("started")}¦{crit})'), bk.f(f'=COUNTIFS({K("started")}¦{crit}¦{K("status_auto")}¦"Льётся")'),
                   bk.f(f'=COUNTIFS({K("started")}¦{crit}¦{K("status_auto")}¦"Встал")'),
                   bk.f(f'=SUMIF({K("started")}¦{crit}¦{K("videos")})'), bk.f(f'=SUMIF({K("started")}¦{crit}¦{K("week")})'),
                   bk.f(f'=SUMIF({K("started")}¦{crit}¦{K("subs")})'), bk.f(f'=SUMIF({K("started")}¦{crit}¦{K("subs7")})'),
                   bk.f(f'=SUMIF({K("started")}¦{crit}¦{K("views")})'), bk.f(f'=MAXIFS({K("best_v")}¦{K("started")}¦{crit})')])
    sv += [[], ["Мой статус", "Каналов", "", "Статус с YouTube", "Каналов"]]
    for i in range(max(len(MY_STATUS), len(AUTO_STATUS))):
        a = MY_STATUS[i] if i < len(MY_STATUS) else ""; b = AUTO_STATUS[i] if i < len(AUTO_STATUS) else ""
        sv.append([a, bk.f(f'=COUNTIF({K("status_my")}¦"{a}")') if a else "", "", b, bk.f(f'=COUNTIF({K("status_auto")}¦"{b}")') if b else ""])
    sv += [[], ["Ресурсы и контроль", ""],
           ["Свободных почт", bk.f("=COUNTIF('Почты'!C:C¦\"свободна\")")],
           ["Свободных профилей AdsPower", bk.f("=COUNTIF('Профили'!E:E¦\"свободен\")")],
           ["Профилей с мёртвым прокси", bk.f("=COUNTIF('Профили'!E:E¦\"мёртвый прокси\")")],
           ["Каналов требуют внимания", bk.f(f'=COUNTIF({K("alert")}¦"?*")')],
           ["Заливок просрочено", bk.f("=COUNTIF('Очередь'!H2:H¦\"*просрочено*\")")],
           ["Повторов оригинала на канале", bk.f("=COUNTIF('Очередь'!H2:H¦\"*повтор оригинала*\")")],
           ["Оригинал взят двумя каналами одного языка", bk.f("=COUNTIF('Очередь'!H2:H¦\"*другим каналом этого языка*\")")],
           ["Оригиналов в базе", bk.f("=COUNTA('Оригиналы'!A2:A)")],
           ["Конкурентов в работе", bk.f("=COUNTIF('Конкуренты'!D2:D¦TRUE)")]]
    bk.batch_values([{"range": f"'Сводка'!A1:M{len(sv)}", "values": sv}])

    # 9. «Сегодня»: живые выборки формулами
    today_q = bk.f("=IFERROR(QUERY('Очередь'!A2:I¦\"select A, B, C, E, F, H where B <> '' and E <> 'Залито' and E <> 'Отменено' and A <= date '\"&TEXT(TODAY()¦\"yyyy-mm-dd\")&\"' order by A\"¦0)¦\"на сегодня и просроченных заливок нет\")")
    week_q = bk.f("=IFERROR(QUERY('Очередь'!A2:I¦\"select A, B, C, E, F where B <> '' and E <> 'Залито' and E <> 'Отменено' and A > date '\"&TEXT(TODAY()¦\"yyyy-mm-dd\")&\"' and A <= date '\"&TEXT(TODAY()+7¦\"yyyy-mm-dd\")&\"' order by A\"¦0)¦\"на неделю вперёд ничего не запланировано\")")
    alert_q = bk.f(f"=IFERROR(QUERY('Каналы'!A2:{L('uc')}¦\"select B, D, {L('alert')}, {L('email')}, {L('profile')} where {L('alert')} <> '' order by D, B\"¦0)¦\"все каналы в порядке\")")
    bk.batch_values([{"range": "'Сегодня'!A1:A1", "values": [[bk.f('="Сегодня · "&TEXT(TODAY()¦"dd.mm.yyyy")')]]},
                     {"range": "'Сегодня'!A3:F4", "values": [["Заливки на сегодня и просроченные", "", "", "", "", ""],
                                                              ["Дата", "Канал", "Тема", "Статус", "Кто делает", "Проверка"]]},
                     {"range": "'Сегодня'!A5", "values": [[today_q]]},
                     {"range": "'Сегодня'!H3:L4", "values": [["Каналы, которые требуют внимания", "", "", "", ""],
                                                              ["Канал", "Язык", "Что не так", "Почта", "Профиль"]]},
                     {"range": "'Сегодня'!H5", "values": [[alert_q]]},
                     {"range": "'Сегодня'!A120:E121", "values": [["Запланировано на ближайшие 7 дней", "", "", "", ""],
                                                                  ["Дата", "Канал", "Тема", "Статус", "Кто делает"]]},
                     {"range": "'Сегодня'!A122", "values": [[week_q]]}])

    # 10. «Как пользоваться»
    howto = HOWTO
    bk.batch_values([{"range": f"'Как пользоваться'!A1:B{len(howto)}", "values": howto}])

    # 11. оформление
    req = []
    ch = sid["Каналы"]
    for i, (k, _, manual) in enumerate(FIELDS):
        req.append(header_fmt(ch, i, i + 1, manual))
    req += [freeze(ch, 1, 3), wrap(ch, IX["alert"])] + widths(ch, [60, 230, 70, 100, 150, 105, 125, 120, 260, 200, 70, 105, 115, 220, 70, 70, 190, 105, 80, 105, 80, 80, 90, 95, 100, 100, 260, 80, 210, 95])
    req += [dropdown(ch, IX["lang"], LANGS), dropdown(ch, IX["series"], SERIES), dropdown(ch, IX["status_my"], MY_STATUS, strict=True),
            dropdown(ch, IX["machine"], MACHINES), dropdown(ch, IX["monet"], MONET, strict=True),
            date_fmt(ch, IX["started"]), date_fmt(ch, IX["created"]), date_fmt(ch, IX["last"])]
    for k in ("videos", "idle", "week", "subs", "subs7", "views", "best_v", "foreign"):
        req.append(num_fmt(ch, IX[k]))
    col = lambda k: f"${L(k)}2"
    req += [cond(ch, IX["alert"], IX["alert"] + 1, f'={col("alert")}<>""', bg=RED, bold=True),
            cond(ch, IX["status_auto"], IX["status_auto"] + 1, f'={col("status_auto")}="Льётся"', bg=GREEN),
            cond(ch, IX["status_auto"], IX["status_auto"] + 1, f'=OR({col("status_auto")}="Встал";{col("status_auto")}="Грязный")', bg=AMBER),
            cond(ch, IX["idle"], IX["idle"] + 1, f'=AND(ISNUMBER({col("idle")});{col("idle")}>7)', bg=AMBER, bold=True),
            cond(ch, 0, len(KEYS), f'={col("status_my")}="Выведен"', fg=GREY_TXT),
            cond(ch, IX["subs7"], IX["subs7"] + 1, f'=AND(ISNUMBER({col("subs7")});{col("subs7")}>0)', fg={"red": 0.1, "green": 0.5, "blue": 0.2}, bold=True)]
    for i, (k, _, manual) in enumerate(FIELDS):
        if not manual:
            req.append(protect_warn(ch, f"«{FIELDS[i][1]}» заполняется автоматически", i, i + 1, 1, ROWS))
    q = sid["Очередь"]
    req += [header_fmt(q, 0, 7, True), header_fmt(q, 7, 8, False), header_fmt(q, 8, 9, True), freeze(q, 1, 0)]
    req += widths(q, [95, 230, 320, 260, 150, 130, 220, 260, 220])
    req += [date_fmt(q, 0), dropdown_range(q, 1, f"='Каналы'!$B$2:$B${ROWS}"), dropdown(q, 4, QUEUE_STATUS, strict=True),
            cond(q, 7, 8, '=$H2<>""', bg=RED, bold=True), cond(q, 0, 9, '=$E2="Залито"', bg=GREEN), cond(q, 0, 9, '=$E2="Отменено"', fg=GREY_TXT),
            protect_warn(q, "«Проверка» считается сама", 7, 8, 0, ROWS)]
    p = sid["Почты"]
    req += [header_fmt(p, 0, 2, True), header_fmt(p, 2, 5, False), header_fmt(p, 5, 8, True), freeze(p, 1, 1),
            checkbox(p, 1, 1, 300), cond(p, 2, 3, '=$C2="свободна"', bg=GREEN), cond(p, 2, 3, '=$C2="сгорела"', bg=RED),
            protect_warn(p, "Статус и каналы считаются из «Каналов»", 2, 5, 1, 300)] + widths(p, [240, 80, 95, 95, 360, 330, 130, 230])
    pr = sid["Профили"]
    req += [header_fmt(pr, 0, 4, True), header_fmt(pr, 4, 7, False), header_fmt(pr, 7, 9, True), freeze(pr, 1, 1),
            checkbox(pr, 3, 1, 150), dropdown(pr, 2, MACHINES, 1, 150),
            cond(pr, 4, 5, '=$E2="свободен"', bg=GREEN), cond(pr, 4, 5, '=$E2="мёртвый прокси"', bg=RED),
            protect_warn(pr, "Статус и каналы считаются из «Каналов»", 4, 7, 1, 150)] + widths(pr, [90, 200, 120, 100, 120, 95, 330, 380, 330])
    h = sid["История"]
    req += [header_fmt(h, 0, 6, False), freeze(h, 1, 0), date_fmt(h, 0, 1, 3000), protect_warn(h, "История пишется автоматически")] + widths(h, [100, 70, 260, 110, 110, 100])
    s = sid["Сводка"]
    req += [{"repeatCell": {"range": {"sheetId": s, "startRowIndex": 0, "endRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": 1},
                            "cell": {"userEnteredFormat": {"textFormat": {"bold": True, "fontSize": 16}}}, "fields": "userEnteredFormat.textFormat"}},
            protect_warn(s, "Сводка считается сама")] + widths(s, [230, 90, 80, 130, 100, 80, 110, 90, 110, 110, 115, 130, 110])
    for rr, row in enumerate(sv):
        if row and row[0] in ("Язык", "Когда заведены", "Мой статус", "Ресурсы и контроль"):
            req.append(header_fmt(s, 0, 13, False, row=rr))
        if row and row[0] == "Итого":
            req.append({"repeatCell": {"range": {"sheetId": s, "startRowIndex": rr, "endRowIndex": rr + 1, "startColumnIndex": 0, "endColumnIndex": 12},
                                       "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}}, "fields": "userEnteredFormat.textFormat"}})
    t = sid["Сегодня"]
    req += [{"repeatCell": {"range": {"sheetId": t, "startRowIndex": 0, "endRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": 1},
                            "cell": {"userEnteredFormat": {"textFormat": {"bold": True, "fontSize": 16}}}, "fields": "userEnteredFormat.textFormat"}},
            header_fmt(t, 0, 6, False, row=2), header_fmt(t, 0, 6, False, row=3), header_fmt(t, 7, 12, False, row=2), header_fmt(t, 7, 12, False, row=3),
            header_fmt(t, 0, 5, False, row=119), header_fmt(t, 0, 5, False, row=120), date_fmt(t, 0, 4, 118), date_fmt(t, 0, 121, 300),
            protect_warn(t, "«Сегодня» собирается сама — меняйте «Очередь» и «Каналы»")] + widths(t, [95, 210, 280, 140, 120, 220, 30, 230, 110, 300, 220, 80])
    req += [{"mergeCells": {"range": {"sheetId": t, "startRowIndex": rr, "endRowIndex": rr + 1, "startColumnIndex": c0, "endColumnIndex": c1}, "mergeType": "MERGE_ALL"}}
            for rr, c0, c1 in ((2, 0, 6), (2, 7, 12), (119, 0, 5))]
    req.append(wrap(t, 9, 1, 118))
    hw = sid["Как пользоваться"]
    req += [{"repeatCell": {"range": {"sheetId": hw, "startRowIndex": 0, "endRowIndex": len(howto), "startColumnIndex": 0, "endColumnIndex": 2},
                            "cell": {"userEnteredFormat": {"wrapStrategy": "WRAP", "verticalAlignment": "TOP"}}, "fields": "userEnteredFormat(wrapStrategy,verticalAlignment)"}},
            {"repeatCell": {"range": {"sheetId": hw, "startRowIndex": 0, "endRowIndex": len(howto), "startColumnIndex": 0, "endColumnIndex": 1},
                            "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}}, "fields": "userEnteredFormat.textFormat"}}] + widths(hw, [220, 900])
    for q_ in req:
        if "addConditionalFormatRule" in q_:
            v = q_["addConditionalFormatRule"]["rule"]["booleanRule"]["condition"]["values"][0]
            v["userEnteredValue"] = v["userEnteredValue"].replace(";", bk.sep)
    bk.batch(req)
    bk.reload()

    pool.ensure_structure(bk, log)       # «Конкуренты», «Оригиналы», колонки «Очереди» — до листов каналов: на них ссылаются формулы пула

    # 12. листы каналов (последнее пересоздание) + засев диктора/описаний
    ws_c, hdr, rows = read_channels(bk)
    for r in rows:
        if not r["name"] or (r["status_my"] in NO_SHEET_STATUS and not r["uc"]): continue
        key = re.sub(r"\s*(\((ES|PT|UA|HR|GR|HU)\)|\b(ES|PT|UA|HR|GR|HU))\s*$", "", r["name"]).strip()
        ws_ch = create_channel_sheet(bk, r["code"], r["name"], concepts.get(key, {}))
        bk.batch_values([{"range": f"'Каналы'!{L('sheet')}{r['_row']}", "values": [[bk.f(f'=HYPERLINK("#gid={ws_ch.id}"¦"открыть")')]]}])
        time.sleep(0.5)
    log("структура собрана")


pool.host = sys.modules[__name__]


def run_pool(bk):
    """Конкуренты → «Оригиналы». Падение здесь не должно ронять остальную синхронизацию."""
    try:
        bk.reload()
        pool.pool_sync(bk, yt, log, TODAY)
    except Exception as e:
        log(f"пул: ошибка — {type(e).__name__}: {str(e)[:300]}")


def main():
    bk = Book()
    log("разделитель формул:", bk.sep)
    if MODE == "--rebuild":
        rebuild(bk); bk.reload(); sync(bk)
    elif MODE == "--sync":
        try:
            sync(bk)
        except Exception as e:           # сбой YouTube-части не должен отменять ночной пул
            log(f"sync: ошибка — {type(e).__name__}: {str(e)[:300]}")
        run_pool(bk)
    elif MODE == "--pool":
        run_pool(bk)
    else:
        light(bk)
    log("готово")


if __name__ == "__main__":
    main()
