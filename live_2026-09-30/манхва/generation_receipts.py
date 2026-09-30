"""Durable, key-free history of image requests, kept beside the destination file.

A submitted POST without a known generation ID is deliberately not sent twice.
An accepted generation is polled/downloaded again after a worker restart.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import time
from pathlib import Path


class UncertainRequest(RuntimeError):
    pass


class FailedRequest(RuntimeError):
    pass


def fingerprint(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


@contextlib.contextmanager
def destination_lock(image_path):
    if not image_path:
        yield
        return
    p = Path(str(image_path) + ".generation.lock")
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


class Receipt:
    def __init__(self, path):
        self.path = Path(path) if path else None
        self.data = {"version": 1, "attempts": []}
        if self.path and self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(self.data, dict) or self.data.get("version") != 1 or not isinstance(self.data.get("attempts"), list):
                    raise ValueError("invalid receipt structure")
                for attempt in self.data["attempts"]:
                    if not isinstance(attempt, dict) or attempt.get("status") not in {
                            "posting", "uncertain", "accepted", "succeeded", "downloaded", "failed", "rejected",
                            "lost"}:
                        raise ValueError("invalid attempt structure")
                    if not isinstance(attempt.get("request_sha256"), str) or len(attempt["request_sha256"]) != 64:
                        raise ValueError("missing request binding")
                    if attempt["status"] in ("accepted", "succeeded", "downloaded") and not attempt.get("id"):
                        raise ValueError("accepted request has no generation ID")
            except (ValueError, KeyError, TypeError) as e:
                raise UncertainRequest("Журнал генерации повреждён; новый запрос не отправлен") from e

    @property
    def current(self):
        return self.data["attempts"][-1] if self.data["attempts"] else None

    def save(self):
        if self.path:
            atomic_json(self.path, self.data)

    def resume_level(self, source_fingerprint):
        """Recover the actual request stage without accepting a changed source job."""
        old = self.current
        if not old:
            return 0
        previous = old.get("source_job_sha256")
        if previous and previous != source_fingerprint:
            if old.get("status") not in ("failed", "rejected", "lost"):
                raise UncertainRequest("Исходное задание изменилось; сохранённая генерация относится к другому запросу")
            return 0
        level = old.get("soften_level", 0)
        if type(level) is not int or not 0 <= level <= 3:
            raise UncertainRequest("В журнале неверный этап запроса; новый запрос не отправлен")
        return level

    def matches(self, body, source_fingerprint=None):
        old = self.current
        return bool(old and old.get("request_sha256") == fingerprint(body)
                    and (not source_fingerprint or not old.get("source_job_sha256")
                         or old["source_job_sha256"] == source_fingerprint))

    def begin(self, body, source_fingerprint=None, soften_level=0, quote_bubble=None, before_post=None,
              retry_failed=False):
        """None — можно покупать (запись «отправляю» уже на диске); номер — продолжить принятую генерацию.
        before_post() зовётся, когда решено покупать, но до записи «отправляю»: ожидание квоты в нём
        не оставляет неопределённой покупки. Его словарь (например key_id) пишется в ту же запись."""
        digest = fingerprint(body)
        if source_fingerprint:
            self.resume_level(source_fingerprint)
        old = self.current
        if old:
            same = old.get("request_sha256") == digest
            state = old.get("status")
            if state in ("posting", "uncertain"):
                raise UncertainRequest(
                    "FastGen мог принять запрос, но номер не получен. Повторная покупка остановлена; "
                    "нужно проверить генерацию у провайдера")
            if state in ("accepted", "succeeded", "downloaded") and old.get("id"):
                if same:
                    return old["id"]
                if state != "downloaded":
                    raise UncertainRequest("У этого файла уже есть незавершённая генерация с другим запросом")
            # retry_failed (27.09.2026, I2-01): лестница generate_many решила повторить тот же текст после
            # сбоя связи. Без флага повтор только перечитывал эту ошибку и не отправлял ничего нового.
            if same and state == "failed" and not retry_failed:
                raise FailedRequest(old.get("error") or "Предыдущая генерация завершилась ошибкой")
        extra = before_post() if before_post is not None else None
        self.data["attempts"].append({
            "request_sha256": digest, "source_job_sha256": source_fingerprint,
            "soften_level": soften_level, "status": "posting", "started_at": time.time(),
            "model": body.get("model"), "seed": body.get("seed"),
            "aspect_ratio": body.get("aspect_ratio"), "references": len(body.get("inputs") or []),
            "quota_units": None, "quota_units_source": None,
        })
        if quote_bubble:
            # Признак «картинка нарисована с облаком этой реплики» (MANGA_QUOTE_MODE=panel):
            # пишется до POST вместе с запросом, поэтому не расходится с картинкой.
            self.data["attempts"][-1]["quote_bubble"] = quote_bubble
        if extra:
            self.data["attempts"][-1].update(extra)
        self.save()  # before POST, including the crash window before the response
        return None

    def update(self, status, **fields):
        self.current.update(status=status, updated_at=time.time(), **fields)
        self.save()

    def accepted(self, gid, body):
        # Public /models catalogue checked on 2026-09-10. These are operation
        # units, not the shared /usage delta. Unknown/upscaled models stay null.
        units = None
        if body.get("model", "").startswith("nano-banana") and not body.get("generation_config"):
            units = 4
        elif body.get("model") == "openai-image":
            quality = body.get("quality", "fast")
            units = {"fast": 4, "thinking": 6}.get(quality)
        self.update("accepted", id=gid, accepted_at=time.time(), quota_units=units,
                    quota_units_source="model_catalogue_per_accepted_request" if units is not None else None)


def drawn_after(receipt_path, moment):
    """Попытка квитанции, если картинка по ней скачана ПОСЛЕ moment (время нынешней раскадровки), иначе None."""
    try:
        data = json.loads(Path(receipt_path).read_text(encoding="utf-8"))
        cur = (data.get("attempts") or [])[-1]
    except (OSError, ValueError, IndexError, AttributeError, TypeError):
        return None
    if (isinstance(cur, dict) and cur.get("status") == "downloaded"
            and isinstance(cur.get("updated_at"), (int, float)) and cur["updated_at"] > float(moment)):
        return cur
    return None


def valid_image(path):
    if not path or not Path(path).is_file():
        return False
    try:
        from PIL import Image
        with Image.open(path) as im:
            im.load()
            return im.width > 0 and im.height > 0
    except (OSError, ValueError):
        return False
