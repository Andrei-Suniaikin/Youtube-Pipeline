"""B1: кластеры b-roll — «похожие кадры» вместо заглушек.

Два маппинга:
- scene_cluster(cls) — детерминированный кластер сцены по контракту классификатора
  (type + ключевые слова show/queries/event; язык EN, т.к. контракт EN);
- material_clusters(row) — кластеры кадра библиотеки: regex по title/query
  + CLIP-близость эмбеддинга кадра к текстовым центроидам кластеров.
Двойное назначение материала разрешено (кадр может быть и combat, и winter).

Центроиды считаются ОДИН раз (embed_text) и кэшируются в
<library.root>/cluster_centroids.json (ключ включает модель CLIP —
смена модели инвалидирует кэш). В library.add кэш только читается
(allow_compute=False): не грузим CLIP-веса из горячего пути добавления.
"""
import json
import os
import re

import numpy as np

CLUSTERS = {"combat", "city_ruins", "vehicles_armor", "aviation", "navy_sea",
            "people_daily", "leaders_officials", "documents_print", "winter",
            "crowd_parade"}

# Порядок важен: для сцены выигрывает ПЕРВЫЙ матч (узкие кластеры выше общих).
_RULES = [
    ("winter", r"\b(winter|snow|snowy|frost|frozen|blizzard|ice|sub-?zero)\b"),
    ("aviation", r"\b(aircraft|airplane|plane[s]?|bomber|fighter|luftwaffe|air raid|"
                 r"airfield|pilot[s]?|aviation|aerial|bombing run|stuka)\b"),
    ("navy_sea", r"\b(navy|naval|warship|battleship|cruiser|destroyer|fleet|submarine|"
                 r"u-?boat|harbou?r|seaport|torpedo|sailors?)\b"),
    ("documents_print", r"\b(documents?|letters?|decree|directive|newspaper|leaflet|"
                        r"maps?|telegram|typewritten|printed|archive file|order form|"
                        r"protocols?|reports?)\b"),
    ("leaders_officials", r"\b(hitler|stalin|churchill|roosevelt|mussolini|f(?:ü|u)hrer|"
                          r"generals?|marshals?|field marshal|commanders?|ministers?|"
                          r"headquarters|high command|leadership|officials?|diplomats?)\b"),
    ("crowd_parade", r"\b(parades?|rall(?:y|ies)|crowds?|celebrations?|demonstrations?|"
                     r"ceremon(?:y|ies)|marching column|liberation celebration)\b"),
    ("vehicles_armor", r"\b(tanks?|panzers?|armou?r(?:ed)?|convoys?|trucks?|vehicles?|"
                       r"half-?tracks?|motorcycles?|jeeps?|artillery tractor)\b"),
    ("city_ruins", r"\b(ruins?|ruined|destroyed|rubble|burn(?:ed|ing|t)|devastat\w*|"
                   r"bombed(?:-| )?out|shattered|wreckage|demolished)\b"),
    ("combat", r"\b(battles?|combat|fighting|assaults?|offensives?|attacks?|artillery|"
               r"infantry|front ?line|trench(?:es)?|shelling|machine ?guns?|sieges?|"
               r"defen[cs]e|counterattack|firefight|soldiers? firing)\b"),
    ("people_daily", r"\b(civilians?|refugees?|women|children|daily life|markets?|"
                     r"queues?|workers?|farmers?|famil(?:y|ies)|street scene|"
                     r"evacuations?|villagers?)\b"),
]

# Текстовые центроиды кластеров (EN) — embed_text по ним, кэш в JSON.
_CENTROID_PROMPTS = {
    "combat": "World War II combat photograph: soldiers firing weapons, artillery "
              "barrage, infantry assault on the front line",
    "city_ruins": "ruined city after bombing, destroyed buildings, rubble in the "
                  "streets, World War II devastation",
    "vehicles_armor": "World War II tanks and armored vehicles, military truck "
                      "convoy on a road",
    "aviation": "World War II military aircraft: bombers and fighter planes, "
                "airfield, air raid",
    "navy_sea": "World War II navy warships at sea, naval fleet, submarine, harbor",
    "people_daily": "wartime civilians daily life: refugees, women and children on "
                    "the street, market queue, World War II",
    "leaders_officials": "World War II leaders and officials: generals, ministers, "
                         "military headquarters meeting",
    "documents_print": "historical wartime document, typewritten letter, newspaper "
                       "page, printed map",
    "winter": "winter warfare World War II: snow-covered battlefield, soldiers in "
              "winter uniforms, frozen landscape",
    "crowd_parade": "large crowd at a wartime parade or rally, marching columns, "
                    "public ceremony, World War II",
}

CENTROIDS_FILENAME = "cluster_centroids.json"


def _cluster_text(cls: dict) -> str:
    """Текст контракта сцены для regex-таблицы (show + queries + event)."""
    parts = [str(cls.get("show") or ""), str(cls.get("event") or "")]
    parts += [str(q) for q in (cls.get("queries") or [])]
    return " ".join(parts).casefold()


def scene_cluster(cls: dict) -> str:
    """Детерминированный кластер сцены. Сцена получает ровно ОДИН кластер.

    stats-сцены — всегда documents_print (цифры/сводки иллюстрируются печатью);
    без матча — people_daily (самый безопасный общий кластер эпохи)."""
    if not cls:
        return "people_daily"
    if cls.get("type") == "stats":
        return "documents_print"
    text = _cluster_text(cls)
    for name, pattern in _RULES:
        if re.search(pattern, text):
            return name
    return "people_daily"


def _clip_key(niche: dict = None) -> str:
    cfg = (niche or {}).get("clip", {}) or {}
    return (f'{cfg.get("model", "ViT-B-32")}|'
            f'{cfg.get("pretrained", "laion2b_s34b_b79k")}')


def cluster_centroids(library_root: str, niche: dict = None,
                      allow_compute: bool = False, log=None) -> dict:
    """-> {cluster: np.ndarray} — эмбеддинги центроидов из кэша.

    Кэш <root>/cluster_centroids.json считается один раз (reindex-clusters/warm,
    allow_compute=True). Из library.add кэш ТОЛЬКО читается: нет кэша —
    вернём {} (кластеры лягут по regex, добор сделает reindex-clusters)."""
    path = os.path.join(os.path.abspath(os.path.expanduser(library_root)),
                        CENTROIDS_FILENAME)
    key = _clip_key(niche)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if data.get("model") == key:
            return {name: np.asarray(vec, dtype=np.float32)
                    for name, vec in (data.get("clusters") or {}).items()}
    except (OSError, ValueError):
        pass
    if not allow_compute:
        return {}
    from .clip_rank import embed_text
    cents = {}
    for name, prompt in _CENTROID_PROMPTS.items():
        cents[name] = embed_text(prompt, niche)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"model": key,
                   "clusters": {n: [round(float(x), 6) for x in v]
                                for n, v in cents.items()}},
                  f, ensure_ascii=False)
    os.replace(tmp, path)
    if log:
        log(f"[clusters] центроиды посчитаны и закэшированы: {path}")
    return cents


def material_clusters(material_row: dict, emb=None, centroids: dict = None,
                      min_sim: float = 0.23, verdict_text: str = "") -> list:
    """Кластеры кадра библиотеки -> отсортированный list[str] (может быть пустым).

    Regex по title/query (+ текст кэшированного вердикта, если передан) плюс
    CLIP-близость эмбеддинга к центроидам: все кластеры с косинусом >= min_sim;
    если ни regex, ни порог не дали ничего, а эмбеддинг есть — берём argmax
    (у каждого кадра с эмбеддингом гарантированно >=1 кластер)."""
    row = material_row or {}
    text = " ".join(str(row.get(k) or "") for k in ("title", "query"))
    text = f"{text} {verdict_text or ''}".casefold()
    found = {name for name, pattern in _RULES if re.search(pattern, text)}
    if emb is not None and centroids:
        v = np.asarray(emb, dtype=np.float32).ravel()
        n = float(np.linalg.norm(v))
        if n > 0:
            v = v / n
            sims = {}
            for name, cent in centroids.items():
                c = np.asarray(cent, dtype=np.float32).ravel()
                if c.shape != v.shape:
                    continue  # центроиды другой модели/размерности
                cn = float(np.linalg.norm(c))
                if cn == 0:
                    continue
                sims[name] = float(v @ (c / cn))
            found.update(name for name, s in sims.items() if s >= min_sim)
            if not found and sims:
                found.add(max(sims, key=sims.get))
    return sorted(found & CLUSTERS)


def reindex_library(library, niche: dict = None, only_missing: bool = True,
                    log=print) -> tuple:
    """B2: назначить кластеры существующим материалам библиотеки (merge_extra).

    -> (всего материалов, материалов с >=1 кластером). only_missing=False
    пересчитывает и уже размеченные (например, после смены regex-таблицы)."""
    niche_name = (niche or {}).get("name")
    cents = cluster_centroids(library.root, niche=niche, allow_compute=True, log=log)
    rows = library.all_rows(niche=niche_name)
    total = len(rows)
    updated = with_clusters = 0
    for row, emb in rows:
        existing = (row.get("extra") or {}).get("clusters") or []
        if existing and only_missing:
            with_clusters += 1
            continue
        cls_list = material_clusters(row, emb=emb, centroids=cents)
        if cls_list:
            library.merge_extra(row["id"], {"clusters": cls_list})
            updated += 1
            with_clusters += 1
    log(f"[clusters] reindex: материалов {total}, с кластерами {with_clusters} "
        f"({(with_clusters / total * 100) if total else 0:.0f}%), "
        f"обновлено {updated}")
    return total, with_clusters
