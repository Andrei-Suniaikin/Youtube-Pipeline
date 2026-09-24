# -*- coding: utf-8 -*-
"""Серверные ключи владельца — правка из студии, без SSH.

Решение владельца 24.09.2026: «сделаем так чтобы серверные ключи админ тоже мог менять
из интерфейса». До этого ключ FastGen, озвучки или обложек менялся только правкой файла
на боевой машине. У ключа FastGen срок истекает 3 октября, и менять его будет заказчик,
у которого SSH нет и быть не должно.

Где живут. Файл `VS_SERVER_KEYS_PATH` (в бою `/srv/studio/keys/…`, права 0600). Сервер
перечитывает его сам, как только меняется размер или время правки (`main.keys()`), и пул
раздачи спрашивает значения на каждой выдаче. Поэтому перезапуск не нужен: новый ключ
работает со следующей задачи.

Три правила, которые здесь соблюдаются жёстко:

1. **Значение наружу не отдаётся никогда** — ни в ответе, ни в журнале. Видно только
   «задан / не задан» и хвост из четырёх знаков, как у ключей людей.
2. **Чужое в файле не трогаем.** Рядом с ключами лежат заметки, лимиты и правила выбора
   канала промптов (`_prompt_policy`, `_concurrency`). Запись идёт поверх разобранного
   файла, а не новым файлом из известных полей: иначе первая же правка снесла бы всё,
   о чём этот модуль не знает.
3. **Пишем атомарно и закрыто**: временный файл 0600 рядом, затем подмена. Оборванная
   на полпути запись не должна оставить конвейер без ключей.
"""
from __future__ import annotations

import json
import os
import stat
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple

# Семейства, которые можно менять со страницы. Имя → как назвать человеку.
# Список намеренно закрытый: файл ключей — не свалка, и запись в произвольное поле
# позволила бы через форму подменить, например, правила выбора канала промптов.
EDITABLE = (
    ("googler", "FastGen — картинки и видео"),
    ("openai", "OpenAI — картинки gpt-image"),
    ("elevenlabs", "Lumean (бывш. ElevenLabs) — озвучка"),
    ("lumean", "Lumean · HeyGen — озвучка"),
    ("voicer", "Voicer — озвучка"),
    ("voicegencore", "VoiceGenCore — озвучка"),
    ("veo", "Veo — клипы"),
    ("alt_llm", "Сторонний Claude API — обложки и промпты"),
)
EDITABLE_KEYS = tuple(name for name, _ in EDITABLE)
# Пул того же семейства: если он есть, ключей несколько и работают они по кругу.
POOL_SUFFIX = "_pool"
MIN_LEN = 8
MAX_LEN = 4096


class KeyError_(ValueError):
    """Отказ, который можно показать человеку целиком: значения ключа внутри нет."""


def path() -> str:
    return (os.environ.get("VS_SERVER_KEYS_PATH")
            or os.path.expanduser("~/video_studio_api_keys.json"))


def _load(p: Optional[str] = None) -> Dict[str, Any]:
    try:
        with open(p or path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except ValueError as exc:
        # Испорченный файл не перезаписываем вслепую: молча затереть чужие ключи хуже,
        # чем отказать.
        raise KeyError_(f"файл ключей испорчен и не читается: {exc}")
    return data if isinstance(data, dict) else {}


def _write(data: Dict[str, Any]) -> None:
    target = path()
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".keys-", dir=os.path.dirname(target) or ".")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
        os.chmod(target, 0o600)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def tail(value: str) -> str:
    """Хвост ключа — чтобы отличить один от другого, не показывая сам ключ."""
    value = str(value or "")
    return f"…{value[-4:]}" if len(value) > 4 else "…"


def _values(data: Dict[str, Any], family: str) -> List[str]:
    out: List[str] = []
    one = data.get(family)
    if isinstance(one, str) and one.strip():
        out.append(one.strip())
    elif isinstance(one, list):
        out += [str(v).strip() for v in one if str(v or "").strip()]
    pool = data.get(family + POOL_SUFFIX)
    if isinstance(pool, list):
        out += [str(v).strip() for v in pool if str(v or "").strip()]
    return list(dict.fromkeys(out))


def listing() -> List[Dict[str, Any]]:
    """Что показать на экране: семейство, задан ли ключ, хвост, сколько их в пуле."""
    data = _load()
    out = []
    for family, title in EDITABLE:
        vals = _values(data, family)
        out.append({
            "family": family,
            "title": title,
            "set": bool(vals),
            "tail": tail(vals[0]) if vals else "",
            "count": len(vals),
            # Пул раздаётся по кругу; отдельной правки пула здесь нет — только
            # показываем, что ключей больше одного, чтобы «Заменить» не выглядело
            # как «поменять их все».
            "pooled": len(vals) > 1,
        })
    return out


def _check(value: str) -> str:
    value = "".join(str(value or "").split())
    if not value:
        raise KeyError_("ключ пустой")
    if len(value) < MIN_LEN:
        raise KeyError_(f"это не похоже на ключ: короче {MIN_LEN} знаков")
    if len(value) > MAX_LEN:
        raise KeyError_("слишком длинно для ключа")
    return value


def set_value(family: str, value: str) -> Dict[str, Any]:
    """Заменить ключ семейства. Всё остальное в файле остаётся как было."""
    if family not in EDITABLE_KEYS:
        raise KeyError_("такой ключ отсюда не меняется")
    value = _check(value)
    data = _load()
    previous = _values(data, family)
    if value in previous:
        raise KeyError_("этот ключ уже задан — менять нечего")
    # Прежнее значение не выбрасываем молча: кладём рядом с пометкой даты, как это
    # делалось руками (`_googler_expired_20260831`). Так видно, что ключ меняли, и
    # старый можно вернуть, если новый окажется чужим.
    old = data.get(family)
    if isinstance(old, str) and old.strip():
        data[f"_{family}_prev_{time.strftime('%Y%m%d')}"] = old
    data[family] = value
    _write(data)
    return {"family": family, "set": True, "tail": tail(value)}


def drop(family: str) -> Dict[str, Any]:
    """Убрать ключ семейства. Пул того же семейства не трогаем: он отдельный список."""
    if family not in EDITABLE_KEYS:
        raise KeyError_("такой ключ отсюда не меняется")
    data = _load()
    if not _values(data, family):
        raise KeyError_("этого ключа и так нет")
    old = data.get(family)
    if isinstance(old, str) and old.strip():
        data[f"_{family}_removed_{time.strftime('%Y%m%d')}"] = old
    data.pop(family, None)
    _write(data)
    return {"family": family, "set": bool(_values(_load(), family))}


def file_state() -> Tuple[bool, str]:
    """Есть ли файл и закрыт ли он как следует — это видно человеку на экране."""
    p = path()
    try:
        info = os.stat(p)
    except FileNotFoundError:
        return False, "файла ключей нет — он появится при первой записи"
    if stat.S_IMODE(info.st_mode) & 0o077:
        return True, "внимание: файл ключей открыт не только владельцу"
    return True, ""
