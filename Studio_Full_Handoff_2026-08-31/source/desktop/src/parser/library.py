"""Библиотека материалов: SQLite-индекс (WAL) + файлы по контент-хэшу. Этап 2 плана.

Всё скачанное парсером падает сюда; поиск новых сцен идёт СНАЧАЛА локально
(косинус по CLIP-эмбеддингам, numpy), внешние API — только для дыр. Вердикты
судьи кэшируются per (материал, контекст) — один файл судим один раз.

Структура: <root>/index.sqlite + <root>/files/ab/<id16>.jpg,
id16 = sha256(байты тумбы)[:16] — контент-хэш, естественный дедуп по байтам.
Дедуп «почти-дублей» — по perceptual hash (imagehash, мягко: нет пакета — пропуск).

Потокобезопасность (router многопоточный): короткое соединение на каждый вызов,
WAL + busy_timeout; каждый метод — одна транзакция.
"""
import hashlib
import io
import json
import os
import re
import sqlite3
import threading
import time
import unicodedata
import uuid
from contextlib import contextmanager

import numpy as np

SCHEMA_VERSION = "2"

# C3: золотой фонд — буст к скору вручную помеченных юзером материалов
# (extra_json: {"gold": true} через merge_extra). Применяется ТОЧНО в двух
# точках скоринга: ранжирование кандидатов _cluster_broll_fill (router)
# и CLIP-ранг pick_from_pool (broll). В _pick скоринга нет — там не трогать.
GOLD_BOOST = 0.05

_SCHEMA = """
CREATE TABLE IF NOT EXISTS materials(
  id TEXT PRIMARY KEY,
  niche TEXT, source TEXT, page_url TEXT, full_url TEXT, title TEXT,
  license TEXT, license_raw TEXT, author TEXT, date_text TEXT, query TEXT,
  media_type TEXT DEFAULT 'image', mime_type TEXT, width INT, height INT,
  duration REAL, file_size INT, fps REAL, extra_json TEXT,
  file_rel TEXT, phash TEXT, emb BLOB, added_at REAL);
CREATE INDEX IF NOT EXISTS idx_materials_niche ON materials(niche);
CREATE INDEX IF NOT EXISTS idx_materials_phash ON materials(phash);
CREATE TABLE IF NOT EXISTS usage(
  material_id TEXT, project TEXT, scene INT, position TEXT, used_at REAL);
CREATE INDEX IF NOT EXISTS idx_usage_material ON usage(material_id);
CREATE TABLE IF NOT EXISTS verdicts(
  material_id TEXT, ctx_hash TEXT, verdict TEXT, reason TEXT, judged_at REAL,
  PRIMARY KEY(material_id, ctx_hash));
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
"""

# порядок колонок materials — единый для INSERT и SELECT
_COLS = ("id", "niche", "source", "page_url", "full_url", "title", "license",
         "license_raw", "author", "date_text", "query", "media_type", "mime_type",
         "width", "height", "duration", "file_size", "fps", "extra_json",
         "file_rel", "phash", "emb", "added_at")
_CAND_FIELDS = ("source", "page_url", "full_url", "title", "license",
                "license_raw", "author", "date_text", "query", "media_type",
                "mime_type", "width", "height", "duration", "file_size", "fps")


def make_ctx_hash(subject: str, query: str) -> str:
    """Хэш контекста судейства: пара (сюжет сцены, запрос) = один вердикт."""
    raw = f"{subject or ''}|{query or ''}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _cand_dict(cand) -> dict:
    """Candidate-датакласс или dict -> плоский dict нужных полей."""
    if isinstance(cand, dict):
        out = {k: cand.get(k) for k in _CAND_FIELDS}
        extra = cand.get("extra") or {}
    else:
        out = {k: getattr(cand, k, None) for k in _CAND_FIELDS}
        extra = getattr(cand, "extra", {}) or {}
    for key in ("source", "page_url", "full_url", "title", "license",
                "license_raw", "author", "date_text", "query", "mime_type"):
        out[key] = str(out.get(key) or "")
    out["media_type"] = str(out.get("media_type") or "image")
    out["width"] = int(out.get("width") or 0)
    out["height"] = int(out.get("height") or 0)
    out["duration"] = float(out.get("duration") or 0.0)
    out["file_size"] = int(out.get("file_size") or 0)
    out["fps"] = float(out.get("fps") or 0.0)
    out["extra_json"] = json.dumps(extra, ensure_ascii=False, sort_keys=True)
    return out


def _emb_blob(emb):
    """np.ndarray|None -> нормированный float32 BLOB|None."""
    if emb is None:
        return None
    v = np.asarray(emb, dtype=np.float32).ravel()
    n = float(np.linalg.norm(v))
    if n == 0:
        return None
    return (v / n).tobytes()


def _phash_of(data: bytes):
    """Perceptual hash тумбы; imagehash не установлен / битый файл -> None (мягко)."""
    try:
        import imagehash
        from PIL import Image
        return str(imagehash.phash(Image.open(io.BytesIO(data)).convert("RGB")))
    except Exception:  # noqa: BLE001 — отсутствие пакета или нечитаемый файл
        return None


def _unique_tmp_path(path: str) -> str:
    """A temp sibling safe for concurrent adds in one shared library."""
    return f"{path}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp"


def _norm_words(value: str) -> list:
    text = "".join(ch for ch in unicodedata.normalize("NFKD", str(value or ""))
                   if not unicodedata.combining(ch)).casefold()
    return re.findall(r"[a-z0-9]+", text)


def _prefix_hit(term: str, words: set) -> bool:
    return any(word.startswith(term) or term.startswith(word) for word in words
               if len(word) >= 3)


class Library:
    """Хранилище материалов. root_path приходит из конфига (yaml `library.path`)."""

    def __init__(self, root_path: str):
        self.root = os.path.abspath(os.path.expanduser(root_path))
        self.files_dir = os.path.join(self.root, "files")
        self.db_path = os.path.join(self.root, "index.sqlite")
        os.makedirs(self.files_dir, exist_ok=True)
        with self._conn() as c:
            c.executescript(_SCHEMA)
            self._migrate(c)

    @staticmethod
    def _migrate(c):
        """Additive v1→v2 migration; existing image library remains readable."""
        existing = {row[1] for row in c.execute("PRAGMA table_info(materials)")}
        additions = {
            "media_type": "TEXT DEFAULT 'image'", "mime_type": "TEXT",
            "width": "INT", "height": "INT", "duration": "REAL",
            "file_size": "INT", "fps": "REAL", "extra_json": "TEXT",
        }
        for name, ddl in additions.items():
            if name not in existing:
                c.execute(f"ALTER TABLE materials ADD COLUMN {name} {ddl}")
        c.execute("UPDATE materials SET media_type='image' "
                  "WHERE media_type IS NULL OR media_type='' ")
        c.execute("INSERT OR REPLACE INTO meta(k, v) VALUES('schema_version', ?)",
                  (SCHEMA_VERSION,))

    @contextmanager
    def _conn(self):
        c = sqlite3.connect(self.db_path, timeout=30)
        try:
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA busy_timeout=30000")
            c.execute("PRAGMA synchronous=NORMAL")
            yield c
            c.commit()
        finally:
            c.close()

    # ---------- материалы ----------

    def add(self, cand, thumb_path: str, emb=None, niche: str = "") -> str:
        """Добавить материал (Candidate или dict) + файл тумбы. -> id.

        Дедуп: по id (sha256 байт) и по точному phash. Повторный add существующего
        id возвращает его же (и до-заполняет emb, если раньше его не было).
        """
        d = _cand_dict(cand)
        if isinstance(cand, dict):
            niche = niche or str(cand.get("niche") or "")
        with open(thumb_path, "rb") as f:
            data = f.read()
        mid = hashlib.sha256(data).hexdigest()[:16]
        blob = _emb_blob(emb)
        # pHash может быть сравнительно дорогим; считаем его до захвата
        # сериализующей SQLite writer-lock.
        phash = _phash_of(data)

        with self._conn() as c:
            # Проверка phash + INSERT должны быть одной сериализованной операцией.
            # Обычная DEFERRED-транзакция позволяет двум потокам одновременно
            # увидеть «дубля нет» и записать разные id с одинаковым pHash.
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT emb FROM materials WHERE id=?", (mid,)).fetchone()
            if row is not None:
                if blob is not None and row[0] is None:
                    c.execute("UPDATE materials SET emb=? WHERE id=?", (blob, mid))
                return mid
            if phash:
                dup = c.execute("SELECT id FROM materials WHERE phash=? LIMIT 1",
                                (phash,)).fetchone()
                if dup:
                    return dup[0]
            # B2: кластеры считаются на месте для всех НОВЫХ материалов — иначе
            # добавляемое обычными прогонами навсегда останется без кластеров и
            # кластерный b-roll молча опустеет. Центроиды здесь только из кэша
            # (allow_compute=False): нет кэша — regex-only, добор reindex-clusters.
            try:
                from . import clusters as clusters_mod
                extra = json.loads(d["extra_json"] or "{}")
                if "clusters" not in extra:
                    cents = clusters_mod.cluster_centroids(self.root)
                    emb_arr = (np.frombuffer(blob, dtype=np.float32)
                               if blob else None)
                    cls_list = clusters_mod.material_clusters(
                        {"title": d["title"], "query": d["query"]},
                        emb=emb_arr, centroids=cents)
                    if cls_list:
                        extra["clusters"] = cls_list
                        d["extra_json"] = json.dumps(extra, ensure_ascii=False,
                                                     sort_keys=True)
            except Exception:  # noqa: BLE001 — кластеры = обогащение, add не роняем
                pass
            file_rel = f"files/{mid[:2]}/{mid}.jpg"
            abs_path = os.path.join(self.root, *file_rel.split("/"))
            os.makedirs(os.path.dirname(abs_path), exist_ok=True)
            if not os.path.exists(abs_path):  # атомарная запись; tmp уникален
                tmp = _unique_tmp_path(abs_path)
                with open(tmp, "wb") as f:
                    f.write(data)
                os.replace(tmp, abs_path)
            c.execute(
                f"INSERT OR IGNORE INTO materials({','.join(_COLS)}) "
                f"VALUES({','.join('?' * len(_COLS))})",
                (mid, niche, d["source"], d["page_url"], d["full_url"], d["title"],
                 d["license"], d["license_raw"], d["author"], d["date_text"],
                 d["query"], d["media_type"], d["mime_type"], d["width"], d["height"],
                 d["duration"], d["file_size"], d["fps"], d["extra_json"],
                 file_rel, phash, blob, time.time()))
        return mid

    def get(self, material_id: str):
        """-> dict материала (без blob, с абсолютным path) | None."""
        cols = ",".join(_COLS[:-2])  # без emb/added_at в SELECT ниже добираем added_at
        with self._conn() as c:
            r = c.execute(f"SELECT {cols}, added_at FROM materials WHERE id=?",
                          (material_id,)).fetchone()
        return self._row_dict(r) if r else None

    def search(self, emb, top_k: int = 8, min_score: float = 0.25,
               niche: str = None, media_type: str = None) -> list:
        """Косинус-поиск по эмбеддингам ниши. -> [(material_dict, score)] по убыванию.

        Эмбеддинги хранятся нормированными; запрос нормируем здесь же.
        media_type — отдельная квота (17.07): видео-клипы скорят ниже картинок
        (кадр хроники против текста) и вытеснялись из общего top_k.
        """
        q = np.asarray(emb, dtype=np.float32).ravel()
        n = float(np.linalg.norm(q))
        if n == 0:
            return []
        q = q / n
        cols = ",".join(_COLS[:-2])
        sql = f"SELECT {cols}, added_at, emb FROM materials WHERE emb IS NOT NULL"
        args = []
        if niche:
            sql += " AND niche=?"
            args.append(niche)
        if media_type:
            sql += " AND media_type=?"
            args.append(media_type)
        with self._conn() as c:
            rows = c.execute(sql, args).fetchall()
        scored = []
        for r in rows:
            v = np.frombuffer(r[-1], dtype=np.float32)
            if v.shape != q.shape:
                continue  # эмбеддинг другой модели/размерности
            s = float(v @ q)
            if s >= min_score:
                scored.append((self._row_dict(r[:-1]), s))
        scored.sort(key=lambda t: -t[1])
        return scored[:top_k]

    def search_metadata(self, person: str = "", event: str = "", place: str = "",
                        year=None, top_k: int = 8, niche: str = None) -> list:
        """High-precision title/date recall before CLIP.

        CLIP often buries the exact ``K. H. Frank ... court`` image under a visually
        similar portrait. Full names and initial+surname forms are scored strongly;
        event/place/year only break ties or support concrete non-person scenes.
        """
        pwords, ewords, lwords = _norm_words(person), _norm_words(event), _norm_words(place)
        if not (pwords or (ewords and lwords)):
            return []
        cols = ",".join(_COLS[:-2])
        sql = f"SELECT {cols}, added_at FROM materials"
        args = []
        if niche:
            sql += " WHERE niche=?"
            args.append(niche)
        with self._conn() as c:
            rows = c.execute(sql, args).fetchall()

        event_aliases = {
            "trial": ("trial", "court", "soud", "proces", "process"),
            "execution": ("execution", "execut", "poprav"),
            "prison": ("prison", "veznic", "pankrac"),
            "arrest": ("arrest", "captur", "detain", "detainee", "detention"),
        }
        current_event_groups = {
            key for key, aliases in event_aliases.items()
            if any(_prefix_hit(alias, set(ewords)) for alias in aliases)
        }
        place_aliases = {"prague": ("prague", "praha"), "praha": ("prague", "praha")}
        scored = []
        for raw in rows:
            row = self._row_dict(raw)
            title_words = _norm_words(f"{row.get('title')} {row.get('date_text')}")
            words = set(title_words)
            score, person_match = 0.0, False
            if pwords:
                compact = "".join(title_words)
                if all(word in words for word in pwords) or "".join(pwords) in compact:
                    score += 3.0
                    person_match = True
                elif len(pwords) >= 2 and pwords[-1] in words:
                    initials = [word[0] for word in pwords[:-1]]
                    if all(initial in words for initial in initials):
                        score += 2.8
                        person_match = True
            if person_match:
                neutral_terms = {"portrait", "rf", "ss"}
                if neutral_terms & words or len(title_words) <= len(pwords) + 2:
                    score += 0.6
                if words & {"ausweis", "passport", "document", "letter", "ticket", "id"}:
                    score -= 0.8
            for term in ewords:
                if len(term) >= 4 and _prefix_hit(term, words):
                    score += 0.22
            candidate_event_groups = {
                key for key, aliases in event_aliases.items()
                if any(_prefix_hit(alias, words) for alias in aliases)
            }
            if candidate_event_groups:
                if current_event_groups & candidate_event_groups:
                    score += 0.6
                elif ewords:
                    # A court photo is valuable for a trial scene, but should not be
                    # consumed as the first choice for an arrest/identity cutaway.
                    score -= 0.85
            for term in lwords:
                aliases = place_aliases.get(term, (term,))
                if any(_prefix_hit(alias, words) for alias in aliases):
                    score += 0.7
            if year and str(year) in words:
                score += 0.25
            if score >= 0.65:
                # Keep scores in the same human-readable range as CLIP while retaining order.
                scored.append((row, min(0.99, 0.36 + score * 0.14)))
        scored.sort(key=lambda item: -item[1])
        return scored[:top_k]

    def all_rows(self, niche: str = None) -> list:
        """Все материалы (для reindex-clusters) -> [(dict, emb np.ndarray|None)]."""
        cols = ",".join(_COLS[:-2])
        sql = f"SELECT {cols}, added_at, emb FROM materials"
        args = []
        if niche:
            sql += " WHERE niche=?"
            args.append(niche)
        with self._conn() as c:
            rows = c.execute(sql, args).fetchall()
        out = []
        for r in rows:
            emb = np.frombuffer(r[-1], dtype=np.float32) if r[-1] else None
            out.append((self._row_dict(r[:-1]), emb))
        return out

    def merge_extra(self, material_id: str, patch: dict):
        """B2: дописать ключи в extra_json (dict-мерж, patch поверх старого).

        ⚠ ПЕРЕЗАПИСЬ extra_json запрещена: у movie-клипов там живут
        start_time/end_time/film_uid/local_path — их читает stage_video
        (media_output), потеря = сломанный реюз клипов. Read-modify-write
        под BEGIN IMMEDIATE — конкурентные merge не теряют ключи друг друга."""
        if not patch:
            return
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT extra_json FROM materials WHERE id=?",
                            (material_id,)).fetchone()
            if row is None:
                return
            try:
                extra = json.loads(row[0] or "{}")
            except (TypeError, ValueError):
                extra = {}
            extra.update(patch)
            c.execute("UPDATE materials SET extra_json=? WHERE id=?",
                      (json.dumps(extra, ensure_ascii=False, sort_keys=True),
                       material_id))

    def _row_dict(self, r) -> dict:
        keys = _COLS[:-2] + ("added_at",)
        d = dict(zip(keys, r))
        try:
            d["extra"] = json.loads(d.get("extra_json") or "{}")
        except (TypeError, ValueError):
            d["extra"] = {}
        d["path"] = os.path.join(self.root, *d["file_rel"].split("/"))
        return d

    # ---------- кэш вердиктов ----------

    def get_verdict(self, material_id: str, ctx_hash: str):
        """-> {"verdict","reason","judged_at"} | None."""
        with self._conn() as c:
            r = c.execute("SELECT verdict, reason, judged_at FROM verdicts "
                          "WHERE material_id=? AND ctx_hash=?",
                          (material_id, ctx_hash)).fetchone()
        return {"verdict": r[0], "reason": r[1], "judged_at": r[2]} if r else None

    def cached_verdict_any(self, material_id: str, allowed_ctx_hashes: list):
        """B3: вердикт материала СТРОГО из явного списка допустимых ctx_hash.

        -> {"verdict","reason","ctx_hash","judged_at"} | None. ok приоритетнее
        soft. ⚠ Голый SELECT WHERE verdict IN (ok, soft) запрещён: ctx_hash
        включает JUDGE_VERSION, и без явного списка допустимых ctx мы бы
        воскресили вердикты старых версий судьи."""
        hashes = [h for h in (allowed_ctx_hashes or []) if h]
        if not hashes:
            return None
        marks = ",".join("?" * len(hashes))
        with self._conn() as c:
            rows = c.execute(
                f"SELECT verdict, reason, ctx_hash, judged_at FROM verdicts "
                f"WHERE material_id=? AND ctx_hash IN ({marks})",
                [material_id] + hashes).fetchall()
        if not rows:
            return None
        order = {"ok": 0, "soft": 1}
        rows.sort(key=lambda r: order.get(r[0], 2))
        r = rows[0]
        return {"verdict": r[0], "reason": r[1], "ctx_hash": r[2],
                "judged_at": r[3]}

    def put_verdict(self, material_id: str, ctx_hash: str, verdict: str,
                    reason: str = ""):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO verdicts VALUES(?,?,?,?,?)",
                      (material_id, ctx_hash, verdict, str(reason)[:200], time.time()))

    # ---------- реюз ----------

    def record_usage(self, material_id: str, project: str, scene: int,
                     position: str = ""):
        with self._conn() as c:
            c.execute("INSERT INTO usage VALUES(?,?,?,?,?)",
                      (material_id, str(project), int(scene), position, time.time()))

    def remove_usage(self, material_id: str, project: str, scene: int):
        """Откат usage-записи (финал не сохранился на диск)."""
        with self._conn() as c:
            c.execute("DELETE FROM usage WHERE material_id=? AND project=? AND scene=?",
                      (material_id, str(project), int(scene)))

    def replace_scene_usage(self, material_id_or_none, project: str, scene: int,
                            position: str = "final"):
        """Atomically make a scene point to exactly one material, or to none.

        Older runs could leave several rows for one project/scene.  The DELETE and
        optional INSERT are deliberately one writer transaction so concurrent
        scene workers never expose a half-replaced durable state.
        """
        material_id = str(material_id_or_none or "").strip()
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            c.execute("DELETE FROM usage WHERE project=? AND scene=?",
                      (str(project), int(scene)))
            if material_id:
                c.execute("INSERT INTO usage VALUES(?,?,?,?,?)",
                          (material_id, str(project), int(scene), str(position or ""),
                           time.time()))

    def usage_map(self, project: str) -> dict:
        """{material_id: set(scene)} по проекту — правило «кадр 1 раз на ролик»."""
        with self._conn() as c:
            rows = c.execute("SELECT material_id, scene FROM usage WHERE project=?",
                             (str(project),)).fetchall()
        out = {}
        for mid, sc in rows:
            out.setdefault(mid, set()).add(sc)
        return out

    def usage_count(self, material_id: str, channel_or_project: str = "") -> int:
        """Сколько раз материал использован. Фильтр — подстрока имени проекта:
        канал обычно префикс имени проекта, так что работает и per-канал,
        и per-проект (для будущих кап-правил реюза)."""
        sql = "SELECT COUNT(*) FROM usage WHERE material_id=?"
        args = [material_id]
        if channel_or_project:
            sql += " AND project LIKE ?"
            args.append(f"%{channel_or_project}%")
        with self._conn() as c:
            return int(c.execute(sql, args).fetchone()[0])

    # ---------- сервис ----------

    def stats(self) -> dict:
        with self._conn() as c:
            mats = c.execute("SELECT COUNT(*) FROM materials").fetchone()[0]
            with_emb = c.execute("SELECT COUNT(*) FROM materials "
                                 "WHERE emb IS NOT NULL").fetchone()[0]
            with_phash = c.execute("SELECT COUNT(*) FROM materials "
                                   "WHERE phash IS NOT NULL").fetchone()[0]
            by_niche = dict(c.execute("SELECT niche, COUNT(*) FROM materials "
                                      "GROUP BY niche").fetchall())
            by_source = dict(c.execute("SELECT source, COUNT(*) FROM materials "
                                       "GROUP BY source").fetchall())
            n_usage = c.execute("SELECT COUNT(*) FROM usage").fetchone()[0]
            n_verdicts = c.execute("SELECT COUNT(*) FROM verdicts").fetchone()[0]
            ver = c.execute("SELECT v FROM meta WHERE k='schema_version'").fetchone()
        size = 0
        for dirpath, _dirs, files in os.walk(self.files_dir):
            for fn in files:
                try:
                    size += os.path.getsize(os.path.join(dirpath, fn))
                except OSError:
                    pass
        return {"root": self.root, "schema_version": ver[0] if ver else None,
                "materials": mats, "with_emb": with_emb, "with_phash": with_phash,
                "by_niche": by_niche, "by_source": by_source,
                "usage": n_usage, "verdicts": n_verdicts,
                "files_mb": round(size / 1e6, 2)}

    def cluster_pool_stats(self, niche: str = None) -> dict:
        """G: размер кластерного пула ниши — диагностика «почему мало real».

        Считает материалы с непустым extra.clusters, раздельно фото/клипы.
        Read-only, один вызов на старте run (не в горячем пути)."""
        sql = "SELECT media_type, extra_json FROM materials"
        args = []
        if niche:
            sql += " WHERE niche=?"
            args.append(niche)
        images = videos = 0
        with self._conn() as c:
            rows = c.execute(sql, args).fetchall()
        for mt, extra_json in rows:
            try:
                if json.loads(extra_json or "{}").get("clusters"):
                    if (mt or "image") == "video":
                        videos += 1
                    else:
                        images += 1
            except (ValueError, TypeError):
                continue
        return {"images": images, "videos": videos, "total": images + videos}
