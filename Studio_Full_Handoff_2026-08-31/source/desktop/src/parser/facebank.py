"""D1/D2: face-банк персон ниши — распознавание «чужого лица» до LLM.

Библиотека: insightface (buffalo_l, onnxruntime CPU) — ставится pip, работает
на Mac без GPU. ВСЕ импорты insightface ленивые: без пакета банк молча
выключается (лог-warning один раз), поведение = сегодняшнее (решает LLM).

Банк: <library.path>/facebank.sqlite, таблица faces(person, emb BLOB, src_url).
Эмбеддинги — normed_embedding buffalo_l (512 float32). Сборка банка —
tools/build_facebank.py (Commons-портреты по faces.persons из профиля ниши).

Контракт гейта (используется judge.final_check, D2):
  gate(path, person, niche) ->
    {"status": "wrong", "person": "<X>"} — узнаваемое лицо ДРУГОЙ персоны
        банка с отрывом >= faces.margin → reject ДО LLM-вызова (экономия);
    {"status": "match"}                 — лицо совпало с named-персоной →
        судья получает строку-подсказку в prompt;
    None — лиц нет / не уверен / банк или insightface недоступны → LLM
        решает как сейчас (гейт никогда не блокирует пайплайн).
"""
import os
import sqlite3
import threading

import numpy as np

# ленивый singleton insightface: одна попытка инициализации на процесс
_APP = None
_APP_FAILED = False
_APP_LOCK = threading.Lock()

# кэш банков по пути: гейт зовётся на каждый final_check
_BANKS = {}
_BANKS_LOCK = threading.Lock()


def faces_cfg(niche: dict) -> dict:
    return (niche or {}).get("faces", {}) or {}


def bank_path(niche: dict) -> str:
    """facebank.sqlite живёт рядом с библиотекой материалов."""
    root = os.path.expanduser(
        ((niche or {}).get("library", {}) or {}).get(
            "path", "~/VideoStudio/parser_library"))
    return os.path.join(root, "facebank.sqlite")


def ensure_schema(path: str):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    con = sqlite3.connect(path)
    try:
        con.execute("CREATE TABLE IF NOT EXISTS faces("
                    "person TEXT NOT NULL, emb BLOB NOT NULL, "
                    "src_url TEXT DEFAULT '')")
        con.commit()
    finally:
        con.close()


def add_face(path: str, person: str, emb, src_url: str = ""):
    """Одно лицо в банк (сборка — tools/build_facebank.py)."""
    ensure_schema(path)
    blob = np.asarray(emb, dtype=np.float32).tobytes()
    con = sqlite3.connect(path)
    try:
        con.execute("INSERT INTO faces VALUES(?,?,?)",
                    (str(person), blob, str(src_url)))
        con.commit()
    finally:
        con.close()


def clear_person(path: str, person: str):
    """Идемпотентная пересборка: старые эмбеддинги персоны — прочь."""
    if not os.path.exists(path):
        return
    con = sqlite3.connect(path)
    try:
        con.execute("DELETE FROM faces WHERE person=?", (str(person),))
        con.commit()
    finally:
        con.close()


class FaceBank:
    """Эмбеддинги персон банка в памяти; сравнение — косинус по max."""

    def __init__(self, path: str):
        self.path = path
        self._by_person = {}   # canonical person -> np.ndarray (N, dim)
        self._names = {}       # casefold -> canonical
        self._load()

    def _load(self):
        if not os.path.exists(self.path):
            return
        con = sqlite3.connect(self.path)
        try:
            rows = con.execute("SELECT person, emb FROM faces").fetchall()
        except sqlite3.Error:
            rows = []
        finally:
            con.close()
        grouped = {}
        for person, blob in rows:
            emb = np.frombuffer(blob, dtype=np.float32).copy()
            norm = float(np.linalg.norm(emb))
            if not norm:
                continue
            grouped.setdefault(str(person), []).append(emb / norm)
        self._by_person = {p: np.stack(v) for p, v in grouped.items()}
        self._names = {p.casefold(): p for p in self._by_person}

    def canonical(self, person: str):
        return self._names.get(str(person or "").strip().casefold())

    def has_person(self, person: str) -> bool:
        return self.canonical(person) is not None

    def persons(self) -> list:
        return sorted(self._by_person)

    def scores(self, emb) -> dict:
        """{person: max cosine} по всем эталонам персоны."""
        v = np.asarray(emb, dtype=np.float32)
        norm = float(np.linalg.norm(v))
        if not norm:
            return {}
        v = v / norm
        return {p: float(np.max(embs @ v))
                for p, embs in self._by_person.items()}


def get_bank(niche: dict, log=print):
    """FaceBank по профилю ниши; None если файла нет/пуст."""
    path = bank_path(niche)
    with _BANKS_LOCK:
        bank = _BANKS.get(path)
        if bank is None:
            bank = FaceBank(path)
            _BANKS[path] = bank
    if not bank._by_person:
        return None
    return bank


def _get_app(log=print):
    """Ленивый insightface FaceAnalysis (buffalo_l, CPU). None = недоступен."""
    global _APP, _APP_FAILED
    if _APP is not None or _APP_FAILED:
        return _APP
    with _APP_LOCK:
        if _APP is not None or _APP_FAILED:
            return _APP
        try:
            from insightface.app import FaceAnalysis
            app = FaceAnalysis(name="buffalo_l",
                               providers=["CPUExecutionProvider"])
            app.prepare(ctx_id=-1, det_size=(640, 640))
            _APP = app
        except Exception as e:  # noqa: BLE001 — без пакета гейт молча выключен
            _APP_FAILED = True
            log(f"[facebank] insightface недоступен: {e} — face-гейт выключен")
    return _APP


def _detect_embeddings(image_path: str, log=print) -> list:
    """Лица кадра -> [normed_embedding]; [] при любой проблеме (fail-open)."""
    app = _get_app(log=log)
    if app is None:
        return []
    try:
        from PIL import Image
        img = Image.open(image_path).convert("RGB")
        arr = np.asarray(img)[:, :, ::-1]  # insightface ждёт BGR
        faces = app.get(arr)
        return [f.normed_embedding for f in faces
                if getattr(f, "normed_embedding", None) is not None]
    except Exception as e:  # noqa: BLE001 — битый кадр не роняет контроль
        log(f"[facebank] детект лиц упал ({os.path.basename(image_path)}): {e}")
        return []


def gate(image_path: str, person: str, niche: dict, log=print):
    """D2: face-гейт финального контроля (контракт — в докстринге модуля)."""
    cfg = faces_cfg(niche)
    if not cfg.get("enabled", True):
        return None
    person = str(person or "").strip()
    if not person:
        return None
    bank = get_bank(niche, log=log)
    if bank is None or not bank.has_person(person):
        return None  # персоны нет в банке — гейт не применим
    embs = _detect_embeddings(image_path, log=log)
    if not embs:
        return None  # лиц нет/не уверен — поведение как сейчас (LLM решает)
    threshold = float(cfg.get("threshold", 0.45))
    margin = float(cfg.get("margin", 0.10))
    own_name = bank.canonical(person)
    wrong = None
    for emb in embs:
        scores = bank.scores(emb)
        own = scores.get(own_name, -1.0)
        if own >= threshold:
            # своя персона в кадре — чужие лица рядом легитимны (встреча,
            # группа): подсказка судье, а не reject
            return {"status": "match"}
        others = {p: s for p, s in scores.items() if p != own_name}
        if others:
            other_name = max(others, key=others.get)
            other = others[other_name]
            if other >= threshold and (other - own) >= margin:
                wrong = {"status": "wrong", "person": other_name}
    return wrong
