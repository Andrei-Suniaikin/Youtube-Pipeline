import os
import sys
import time
import json
import hashlib
import requests
import argparse
from decimal import Decimal, InvalidOperation
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

# Импорт локализации
try:
    from i18n import _
except ImportError:
    # Если модуль не найден, используем функцию-заглушку
    def _(key, *args, **kwargs):
        return key

# Импорт системы логирования
try:
    from logger_config import print_user, print_debug, print_progress, print_error_user_friendly
except ImportError:
    # Если модуль не найден, используем функции-заглушки
    def print_user(msg, msg_type="INFO"):
        emoji_map = {"INFO": "ℹ️", "SUCCESS": "✅", "WARNING": "⚠️", "ERROR": "❌", "PROCESS": "⏳"}
        emoji = emoji_map.get(msg_type.upper(), "ℹ️")
        print(f"{emoji} {msg}", flush=True)
    def print_debug(msg):
        pass
    def print_progress(current, total, item_name="элементов"):
        percentage = int((current / total) * 100) if total > 0 else 0
        print_user(f"Обработка {item_name}: {current} из {total} ({percentage}%)", "PROCESS")
    def print_error_user_friendly(error_msg, show_debug_hint=True):
        print_user(error_msg, "ERROR")

# 03.09.2026: журнал платных вызовов. Импорт мягкий, по образцу i18n/logger_config выше:
# движок запускают и из каталогов, где call_journal.py рядом не положен, и это не повод
# падать. Заглушка отдаёт "off" — поведение ровно как до подключения журнала.
try:
    from call_journal import CallJournal
except ImportError:
    class _JournalOffDecision:
        action = "off"
        call_id = 0
        remote_order_id = ""
        result_path = ""
        attempts = 0
        error = ""
        may_call = True

    class CallJournal:
        def __init__(self, *args, **kwargs):
            pass

        @staticmethod
        def fingerprint(**parts):
            return ""

        def reserve(self, **kwargs):
            return _JournalOffDecision()

        def ordered(self, *args, **kwargs):
            return False

        def delivered(self, *args, **kwargs):
            return False

        def failed(self, *args, **kwargs):
            return False

# Исправление кодировки для Windows
if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except AttributeError:
        import io
        if hasattr(sys.stdout, 'buffer'):
            sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
        if hasattr(sys.stderr, 'buffer'):
            sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

# --- НАСТРОЙКИ LUMEAN API ---
BASE_URL = os.environ.get("LUMEAN_API_BASE", "https://api.lumean.app/api/public").rstrip("/")
MAX_RETRIES = max(1, int(os.environ.get("LUMEAN_MAX_RETRIES", "3") or "3"))
RETRY_DELAY_SECONDS = max(1, int(os.environ.get("LUMEAN_POLL_SECONDS", "5") or "5"))
# 03.09.2026: ×10 по запросу владельца — ожидание очереди Lumean не должно снимать заказ.
MAX_WAIT_TIME_MINUTES = max(1, int(os.environ.get("LUMEAN_MAX_WAIT_MINUTES", "300") or "300"))
# Новое имя env выше приоритетом; legacy-alias оставлен, чтобы старые
# кабинеты/сборки не потеряли ключ при миграции. Захардкоженного fallback больше нет.
LUMEAN_API_KEY = (os.environ.get("LUMEAN_API_KEY") or
                  os.environ.get("ELEVENLABS_API_KEY") or "").strip()
ELEVENLABS_API_KEY = LUMEAN_API_KEY  # совместимость для внешних импортов
# UUID шаблона Lumean. Старый UUID сохранён как migration-default.
DEFAULT_TEMPLATE_UUID = os.environ.get("LUMEAN_TEMPLATE_ID", "7f312408-d652-4540-a65c-e1df3e1bd498")
AUDIO_DIR_NAME = "_FINAL_AUDIO_ELEVENLABS"
FULL_TEXT_MARKER_NAME = ".lumean-full-text.v1.json"
PARALLEL_REQUESTS = max(1, int(os.environ.get("LUMEAN_PARALLEL", "5") or "5"))
HEARTBEAT_SECONDS = max(30, int(os.environ.get("LUMEAN_HEARTBEAT_SECONDS", "120") or "120"))
# Настройки заказа, заданные у нас (18.09.2026). Шаблон Lumean остаётся выбором голоса,
# а всё остальное — модель, стабильность, схожесть, стиль, буст, скорость, нормализацию,
# размер куска, паузы и ударения — задаёт пресет в нашей студии и присылает их сюда
# одним JSON. Формат тот же, что у самого шаблона (`config`), поэтому ничего
# переводить не надо: то, что пришло, ложится в `config_override` заказа.
# «Настройки вывода» (audio_format) сюда не входят намеренно: кабинет всегда ждёт mp3.
TTS_CONFIG_OVERRIDE = {}
CONFIG_OVERRIDE_SECTIONS = ("tts_settings", "worker_settings", "pause_settings",
                            "stress_settings")

LUMEAN_MODEL_IDS = {
    "eleven_v3", "eleven_v3_beta", "eleven_multilingual_v2",
    "eleven_flash_v2_5", "eleven_turbo_v2_5", "eleven_turbo_v2", "eleven_flash_v2",
}
_SUCCESS_STATUSES = {"completed", "result_delivered"}
_TERMINAL_FAILURE_STATUSES = {"failed", "compensated", "cancelled"}

# Блокировки API удалены - больше не нужны

def _current_api_key():
    """Read env dynamically: the desktop can update a key after importing this module."""
    return (os.environ.get("LUMEAN_API_KEY") or
            os.environ.get("ELEVENLABS_API_KEY") or
            LUMEAN_API_KEY or ELEVENLABS_API_KEY or "").strip()


def get_headers():
    api_key = _current_api_key()
    if not api_key:
        raise ValueError(_('tts_api_key_error'))
    return {"X-API-KEY": api_key, "Content-Type": "application/json"}


def _response_payload(response):
    try:
        payload = response.json()
    except (ValueError, TypeError):
        payload = None
    return payload if isinstance(payload, dict) else {}


class LumeanAPIError(RuntimeError):
    def __init__(self, response, action):
        payload = _response_payload(response)
        self.status_code = int(response.status_code)
        self.reason_code = str(payload.get("reason") or "").strip()
        retry_after = payload.get("retry_after")
        if retry_after in (None, ""):
            retry_after = (getattr(response, "headers", {}) or {}).get("Retry-After")
        try:
            self.retry_after = max(0.0, float(retry_after))
        except (TypeError, ValueError):
            self.retry_after = None
        message = str(payload.get("message") or response.reason or "неизвестная ошибка")[:300]
        extra = f"; reason={self.reason_code}" if self.reason_code else ""
        if self.status_code == 402 and payload.get("shortfall_lmc") is not None:
            extra += f"; требуется PAYG-добор {payload.get('shortfall_lmc')} LMC"
        super().__init__(f"Lumean: {action}: HTTP {self.status_code}: {message}{extra}")


class LumeanTerminalOrderError(RuntimeError):
    def __init__(self, task_id, status):
        self.status = status
        super().__init__(f"Lumean: заказ {task_id} завершён со статусом {status}")


class LumeanManualRecoveryError(RuntimeError):
    """The existing paid order needs action in Lumean; a new order would double-charge."""


class LumeanFilesExpiredError(RuntimeError):
    """The API explicitly confirms that the result is no longer downloadable."""


def _api_error(response, action):
    return LumeanAPIError(response, action)


def _request_json(method, path, *, json_body=None, timeout=60):
    response = requests.request(
        method,
        f"{BASE_URL}/{path.lstrip('/')}",
        json=json_body,
        headers=get_headers(),
        timeout=timeout,
    )
    if response.status_code >= 400:
        raise _api_error(response, path)
    payload = _response_payload(response)
    if not payload.get("success") or not isinstance(payload.get("data"), dict):
        raise RuntimeError(f"Lumean: {path}: неожиданный формат ответа")
    return payload["data"]


def create_task(text_content, voice_uuid=None, model_uuid=None):
    """Create one paid Lumean order. ``voice_uuid`` remains a legacy name for template_id."""
    effective_template_uuid = (voice_uuid or DEFAULT_TEMPLATE_UUID or "").strip()
    if not effective_template_uuid:
        raise ValueError("Lumean: не указан template_id")
    # Кабинет всегда ожидает mp3. Template может быть настроен на wav/ogg, поэтому
    # фиксируем формат на уровне конкретного заказа, не изменяя сам шаблон.
    config_override = {"output_settings": {"audio_format": "mp3"}}
    # Наши настройки поверх шаблона. Раздел за разделом, а не одной заменой: заказ
    # должен получить именно то, что задал человек, а незаданное — из шаблона.
    for section in CONFIG_OVERRIDE_SECTIONS:
        block = TTS_CONFIG_OVERRIDE.get(section)
        if isinstance(block, dict) and block:
            merged = dict(config_override.get(section) or {})
            for key, value in block.items():
                if key == "voice_settings" and isinstance(value, dict):
                    vs = dict(merged.get("voice_settings") or {})
                    vs.update(value)
                    merged["voice_settings"] = vs
                else:
                    merged[key] = value
            config_override[section] = merged
    data = {
        "template_id": effective_template_uuid,
        "input_text": text_content,
        "config_override": config_override,
    }
    # Legacy csv666 used an arbitrary model UUID. Lumean accepts only documented model_id values;
    # an old UUID must not be sent because it would deterministically fail validation.
    model_id = str(model_uuid or "").strip()
    if model_id in LUMEAN_MODEL_IDS:
        tts = dict(config_override.get("tts_settings") or {})
        tts["model_id"] = model_id
        config_override["tts_settings"] = tts
    elif model_id:
        print_user(f"Lumean: legacy model_uuid '{model_id[:24]}' пропущен; модель берётся из шаблона", "WARNING")
    for attempt in range(MAX_RETRIES):
        try:
            order = _request_json("POST", "/orders", json_body=data, timeout=60)
            break
        except LumeanAPIError as e:
            # An explicit 429 is a documented pre-create rejection: no paid order exists,
            # therefore retrying it is safe. Network errors/5xx remain ambiguous and bubble up.
            if e.status_code != 429 or attempt >= MAX_RETRIES - 1:
                raise
            delay = _transient_delay(e, attempt)
            print_user(f"Lumean: лимит создания заказов, повтор через {delay:g} с", "WARNING")
            _sleep_with_heartbeat(delay, "новый")
    task_id = str(order.get("id") or "").strip()
    if not task_id:
        raise RuntimeError("Lumean: заказ создан без id")
    return task_id

def _transient_delay(error, attempt):
    if isinstance(error, LumeanAPIError) and error.retry_after is not None:
        return max(1.0, error.retry_after)
    return min(60.0, max(float(RETRY_DELAY_SECONDS), float(RETRY_DELAY_SECONDS * (2 ** attempt))))


def _sleep_with_heartbeat(seconds, task_id):
    """Long API backoff without going silent past the cabinet's worker lease."""
    remaining = max(0.0, float(seconds))
    while remaining > 0:
        chunk = min(remaining, float(HEARTBEAT_SECONDS))
        time.sleep(chunk)
        remaining -= chunk
        if remaining > 0:
            print_user(f"Lumean: заказ {task_id} ждёт снятия API-лимита…", "PROCESS")


def _request_json_resilient(method, path, *, json_body=None, timeout=60, task_id="", attempts=None):
    """Retry only non-billable/idempotent API calls; never use for POST /orders."""
    attempts = max(1, int(attempts or MAX_RETRIES))
    for attempt in range(attempts):
        try:
            return _request_json(method, path, json_body=json_body, timeout=timeout)
        except (requests.exceptions.RequestException, LumeanAPIError) as e:
            if isinstance(e, LumeanAPIError) and e.status_code != 429 and e.status_code < 500:
                raise
            if attempt >= attempts - 1:
                raise
            delay = _transient_delay(e, attempt)
            print_user(
                f"Lumean: временная ошибка {path}, повтор через {delay:g} с",
                "WARNING",
            )
            _sleep_with_heartbeat(delay, task_id or path)


def require_template_service(template_id, required_service=""):
    """Fail before a paid order when a route is locked to one Lumean service.

    The web cabinet uses this for its dedicated Lumean/HeyGen engine.  The
    legacy ``tts`` route deliberately omits the flag and keeps its historical
    template behaviour unchanged.
    """
    required = str(required_service or "").strip().lower()
    if not required:
        return ""
    template = str(template_id or "").strip()
    if not template:
        raise ValueError(f"Lumean/{required}: template_id обязателен")
    data = _request_json_resilient(
        "GET", f"/templates/{template}", timeout=30, task_id=template,
    )
    service = data.get("service") if isinstance(data.get("service"), dict) else {}
    actual = str(data.get("service_key") or service.get("code") or "").strip().lower()
    if actual != required:
        raise ValueError(
            f"Lumean: шаблон {template} относится к service={actual or 'unknown'}, "
            f"а этот маршрут разрешает только service={required}"
        )
    return actual


def wait_for_audio(task_id):
    start_time = time.time()
    last_heartbeat = 0.0
    last_status = None
    transient_attempt = 0
    bulk_retry_rounds = 0
    handled_failed_items = set()
    while time.time() - start_time < MAX_WAIT_TIME_MINUTES * 60:
        try:
            order = _request_json("GET", f"/orders/{task_id}", timeout=30)
            transient_attempt = 0
        except (requests.exceptions.RequestException, LumeanAPIError) as e:
            if isinstance(e, LumeanAPIError) and e.status_code != 429 and e.status_code < 500:
                raise
            delay = _transient_delay(e, transient_attempt)
            transient_attempt += 1
            print_user(f"Lumean: временная ошибка опроса заказа {task_id}, повтор через {delay:g} с", "WARNING")
            _sleep_with_heartbeat(delay, task_id)
            continue

        now = time.time()
        status = str(order.get("status") or "").strip().lower()
        if status in _SUCCESS_STATUSES:
            return order
        if status in _TERMINAL_FAILURE_STATUSES:
            raise LumeanTerminalOrderError(task_id, status)
        if status == "partially_completed":
            items = order.get("items") if isinstance(order.get("items"), list) else []
            failed = [item for item in items if isinstance(item, dict)
                      and str(item.get("status") or "").lower() == "failed"]
            flagged = [item for item in items if isinstance(item, dict)
                       and str(item.get("status") or "").lower() == "policy_flagged"]
            new_failed = [item for item in failed
                          if str(item.get("id") or "") not in handled_failed_items]
            if new_failed and bulk_retry_rounds >= 5:
                raise LumeanManualRecoveryError(
                    f"Lumean: исчерпаны бесплатные повторы заказа {task_id}; "
                    "проверьте заказ в кабинете Lumean, новый платный заказ не создан"
                )
            if new_failed and bulk_retry_rounds < 5:
                retry_plan = _request_json_resilient(
                    "POST", f"/orders/{task_id}/items/retry-failed",
                    timeout=30, task_id=task_id,
                )
                bulk_retry_rounds += 1
                # Order.items contains the full attempt history. Remember the exact failed
                # item ids so an active retry does not cause the same bulk POST every poll.
                handled_failed_items.update(
                    str(item.get("id") or "") for item in new_failed if item.get("id")
                )
                queued = retry_plan.get("queued") if isinstance(retry_plan.get("queued"), list) else []
                skipped = retry_plan.get("skipped") if isinstance(retry_plan.get("skipped"), list) else []
                handled_failed_items.update(
                    str(item.get("item_id") or "") for item in queued + skipped
                    if isinstance(item, dict) and item.get("item_id")
                )
                skipped_reasons = {
                    str(item.get("reason") or "") for item in skipped if isinstance(item, dict)
                }
                print_user(
                    f"Lumean: бесплатно повторяю {int(retry_plan.get('queued_count') or 0)} "
                    f"технически сбойных чанков заказа {task_id}",
                    "PROCESS",
                )
                if skipped_reasons & {"max_attempts_reached", "insufficient_funds"}:
                    raise LumeanManualRecoveryError(
                        f"Lumean: заказ {task_id} требует ручного восстановления в кабинете Lumean "
                        f"({', '.join(sorted(skipped_reasons))}); новый заказ не создан"
                    )
                if "policy_flagged" in skipped_reasons:
                    flagged = flagged or [{}]
            if flagged:
                raise LumeanManualRecoveryError(
                    f"Lumean: заказ {task_id} содержит policy_flagged-чанки. "
                    "Исправьте и бесплатно повторите именно эти чанки в Lumean; "
                    "VideoStudio не создаёт новый платный заказ"
                )
        if status != last_status or now - last_heartbeat >= HEARTBEAT_SECONDS:
            print_user(f"Lumean: заказ {task_id}, статус {status or 'неизвестно'}…", "PROCESS")
            last_status = status
            last_heartbeat = now
        else:
            print_debug(f"Статус Lumean-заказа {task_id}: {status or 'неизвестно'}...")
        time.sleep(RETRY_DELAY_SECONDS)
    raise TimeoutError(f"Превышено время ожидания Lumean-заказа {task_id}")

try:
    from portable_paths import apply_audio_speed
except Exception:
    def apply_audio_speed(_p, _s, strict=False):
        try:
            unchanged = abs(float(_s) - 1.0) < 0.01
        except (TypeError, ValueError):
            unchanged = False
        if strict and not unchanged:
            raise RuntimeError("не удалось загрузить модуль изменения скорости аудио")
        return unchanged
TTS_SPEED = 1.0  # темп речи (atempo); ставится из --speed в main()


def get_audio_result(task_id, output_path, order=None):
    order = order or _request_json_resilient(
        "GET", f"/orders/{task_id}", timeout=30, task_id=task_id,
    )
    if order.get("files_expired") is True:
        raise LumeanFilesExpiredError(
            f"Lumean: файлы заказа {task_id} уже удалены по сроку хранения"
        )
    files = ((order.get("result") or {}).get("files") or [])
    if not files or not isinstance(files[0], str):
        raise RuntimeError(f"Lumean: заказ {task_id} завершён без аудиофайла")
    link = _request_json_resilient(
        "POST", "/storage/url", json_body={"path": files[0], "download": True},
        timeout=30, task_id=task_id,
    ).get("url")
    if not link:
        raise RuntimeError(f"Lumean: не получена ссылка на аудио заказа {task_id}")
    response = requests.get(link, timeout=120, stream=True)
    response.raise_for_status()
    root, ext = os.path.splitext(output_path)
    part_path = f"{root}.part{ext or '.mp3'}"
    with open(part_path, 'wb') as f:
        for chunk in response.iter_content(chunk_size=8192):
            if chunk:
                f.write(chunk)
    if not os.path.exists(part_path) or os.path.getsize(part_path) <= 0:
        raise RuntimeError(f"Lumean: скачан пустой аудиофайл заказа {task_id}")
    apply_audio_speed(part_path, TTS_SPEED, strict=True)
    os.replace(part_path, output_path)
    print_debug(f"Аудиофайл успешно сохранен: {output_path}")


def _receipt_path(output_mp3_path):
    return f"{output_mp3_path}.lumean.json"


def _canonical_speed(value):
    try:
        speed = Decimal(str(value or "1")).normalize()
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("Lumean: скорость должна быть числом") from exc
    if not speed.is_finite() or speed <= 0:
        raise ValueError("Lumean: скорость должна быть положительным конечным числом")
    return format(speed, "f")


def _receipt_identity(text, template_id, model_id, identity_scope=None):
    payload = {
        "text": text,
        "template_id": template_id or DEFAULT_TEMPLATE_UUID,
        "model_id": model_id or "",
    }
    if identity_scope:
        payload["identity_scope"] = identity_scope
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _api_scope():
    raw = f"{BASE_URL}\0{_current_api_key()}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _project_full_text_marker(
    source_bytes, normalized_text, source_file, project_dir, template_id, model_id, speed,
):
    """Enable one-order mode only for an explicitly bound project/source/template.

    A freshly spawned worker sees this file without restarting the web process.
    If the marker exists but no longer matches, fail closed instead of silently
    falling back to hundreds of paid line-by-line orders.
    """
    marker_path = os.path.join(project_dir, FULL_TEXT_MARKER_NAME)
    if not os.path.isfile(marker_path):
        return False
    try:
        with open(marker_path, encoding="utf-8") as f:
            marker = json.load(f)
    except (OSError, ValueError, TypeError) as exc:
        raise RuntimeError(f"Lumean full-text: marker повреждён: {exc}") from None
    required_keys = {
        "schema_version", "mode", "tenant_id", "project_id", "source_sha256",
        "submission_sha256", "template_id", "model_id", "speed",
    }
    if not isinstance(marker, dict) or set(marker) != required_keys:
        raise RuntimeError("Lumean full-text: marker имеет неверную схему")
    if marker.get("schema_version") != 1 or marker.get("mode") != "lumean_full_text":
        raise RuntimeError("Lumean full-text: marker имеет неверную версию/режим")
    project_path = Path(project_dir).resolve()
    source_path = Path(source_file).resolve()
    if source_path != project_path / "source.txt":
        raise RuntimeError("Lumean full-text: source_file не принадлежит проекту")
    try:
        path_tenant_id = project_path.parent.parent.name
        path_shape_ok = (
            project_path.parent.name == "projects"
            and project_path.parent.parent.parent.name == "tenants"
        )
    except IndexError:
        path_tenant_id = ""
        path_shape_ok = False
    if not path_shape_ok:
        raise RuntimeError("Lumean full-text: каталог проекта имеет неверную структуру")
    explicit_template_id = str(template_id or "").strip()
    if not explicit_template_id:
        raise RuntimeError("Lumean full-text: template_id должен быть задан явно")
    try:
        canonical_speed = _canonical_speed(speed)
    except ValueError as exc:
        raise RuntimeError(str(exc)) from None
    expected = {
        "tenant_id": path_tenant_id,
        "project_id": project_path.name,
        "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "submission_sha256": hashlib.sha256(normalized_text.encode("utf-8")).hexdigest(),
        "template_id": explicit_template_id,
        "model_id": str(model_id or "").strip(),
        "speed": canonical_speed,
    }
    mismatched = [key for key, value in expected.items() if marker.get(key) != value]
    if mismatched:
        raise RuntimeError(
            "Lumean full-text: marker не совпадает с проектом/текстом/шаблоном: "
            + ", ".join(mismatched)
        )
    return True


def _read_receipt(output_mp3_path):
    try:
        with open(_receipt_path(output_mp3_path), encoding="utf-8") as f:
            value = json.load(f)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _load_receipt(output_mp3_path, identity):
    receipt = _read_receipt(output_mp3_path)
    # Ambiguous create and manual item-recovery protect the paid order even if the user
    # edits text/template: silently accepting a new identity here would double-charge.
    if receipt.get("state") == "creating" and not receipt.get("order_id"):
        raise RuntimeError(
            "Lumean: прошлый POST /orders имеет неопределённый результат; "
            f"проверьте Lumean и receipt {_receipt_path(output_mp3_path)}, "
            "новый платный заказ автоматически не создан"
        )
    if receipt.get("state") == "manual_recovery":
        raise RuntimeError(
            f"Lumean: оплаченный заказ {receipt.get('order_id') or ''} требует ручного "
            "item-retry в Lumean; новый платный заказ автоматически не создан"
        )
    if receipt.get("identity") != identity:
        return ""
    if receipt.get("state") == "terminal":
        return ""
    if receipt.get("api_scope") != _api_scope() and receipt.get("order_id"):
        raise RuntimeError(
            f"Lumean: API-ключ изменился при незавершённом оплаченном заказе "
            f"{receipt.get('order_id')}; сначала проверьте доступ к нему новым ключом, "
            "новый платный заказ автоматически не создан"
        )
    if receipt.get("order_id"):
        return str(receipt["order_id"])
    return ""


def _save_receipt(output_mp3_path, identity, order_id="", state="created", status=""):
    path = _receipt_path(output_mp3_path)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({
            "provider": "lumean",
            "identity": identity,
            "api_scope": _api_scope(),
            "state": state,
            "status": status,
            "order_id": order_id,
        }, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _discard_creating_receipt(output_mp3_path, identity):
    receipt = _read_receipt(output_mp3_path)
    if (receipt.get("identity") == identity and receipt.get("api_scope") == _api_scope()
            and receipt.get("state") == "creating" and not receipt.get("order_id")):
        try:
            os.remove(_receipt_path(output_mp3_path))
        except FileNotFoundError:
            pass

def process_paragraph(paragraph_data):
    if len(paragraph_data) == 6:
        idx, paragraph_text, output_mp3_path, voice_uuid, model_uuid, use_cache = paragraph_data
        identity_scope = None
    else:
        idx, paragraph_text, output_mp3_path, voice_uuid, model_uuid, use_cache, identity_scope = paragraph_data
    print_debug(f"[{idx}] Начинаю обработку абзаца...")
    if voice_uuid:
        print_debug(f"   Использую Lumean Template UUID: {voice_uuid}")
    if model_uuid:
        print_debug(f"   Использую Lumean Model ID: {model_uuid}")

    identity = _receipt_identity(paragraph_text, voice_uuid, model_uuid, identity_scope)

    # 03.09.2026: заявка в журнал платных вызовов ДО POST /orders — строка о вызове
    # обязана появиться раньше денег, иначе оборванный заказ не с чем сопоставить.
    # Экземпляр локальный: env может быть выставлен после импорта модуля, а потокам
    # ThreadPoolExecutor общее состояние не нужно.
    journal = CallJournal(log=lambda msg: print_user(msg, "WARNING"))
    try:
        speed_for_fingerprint = _canonical_speed(TTS_SPEED)
    except ValueError:
        speed_for_fingerprint = str(TTS_SPEED)
    decision = journal.reserve(
        unit=os.path.basename(output_mp3_path),
        fingerprint=journal.fingerprint(
            template_id=str(voice_uuid or DEFAULT_TEMPLATE_UUID or ""),
            model_id=str(model_uuid or ""),
            speed=speed_for_fingerprint,
            text_sha=hashlib.sha256(paragraph_text.encode("utf-8")).hexdigest(),
            # Настройки заказа (стабильность, схожесть, стиль, паузы, нормализация…)
            # меняют сам звук — значит, это ДРУГОЙ платный вызов. Без них журнал считал
            # бы новую озвучку уже оплаченной и молча отдавал бы файл со старыми
            # настройками. В квитанции они уже учтены (identity_scope), а в журнале —
            # не были: расхождение появилось вместе с настройками Lumean 18.09.2026.
            # Пусто — отпечаток прежний: `fingerprint` выбрасывает пустые значения,
            # поэтому у заказов без настроек ничего не меняется и повторно платить
            # за уже сделанное не придётся.
            config=(json.dumps(TTS_CONFIG_OVERRIDE, ensure_ascii=False, sort_keys=True)
                    if TTS_CONFIG_OVERRIDE else ""),
        ),
        kind="tts",
        provider="lumean",
        model=str(model_uuid or ""),
    )
    if decision.action == "skip" and os.path.exists(output_mp3_path) and os.path.getsize(output_mp3_path) > 0:
        # 03.09.2026: журнал уже видел доставленный результат этого отпечатка, и файл
        # на месте — платить второй раз не за что.
        print_debug(f"[{idx}] Журнал: вызов уже оплачен и доставлен, заказ не создаётся")
        return True

    try:
        # Receipt защищает оплату, поэтому используется независимо от файлового --use_cache.
        task_id = _load_receipt(output_mp3_path, identity)
    except Exception as e:
        # 03.09.2026: все эти ошибки означают возможный уже созданный платный заказ,
        # поэтому строка журнала остаётся «спросить провайдера».
        journal.failed(decision.call_id, f"receipt: {e}", ordered=True)
        print_error_user_friendly(f"Lumean: абзац {idx}: {e}")
        return False
    if not task_id and decision.action == "verify":
        # 03.09.2026: журнал знает, что заказ по этому отпечатку уже создавался. Второй
        # POST /orders — двойная оплата, поэтому опрашиваем существующий заказ через
        # тот же GET /orders/{id}, что и обычное ожидание.
        task_id = str(decision.remote_order_id or "").strip()
        if task_id:
            print_user(
                f"Lumean: абзац {idx}: журнал вернул ранее созданный заказ {task_id}; "
                "новый платный заказ не создаётся",
                "WARNING",
            )
    if task_id:
        print_debug(f"[{idx}] Продолжаю ранее оплаченный Lumean-заказ {task_id}")
        # 03.09.2026: заказ уже существует — фиксируем его id в журнале, не создавая новый.
        journal.ordered(decision.call_id, task_id)
    else:
        if not decision.may_call:
            # 03.09.2026: платить нельзя. unknown — кабинет не ответил и мы не знаем,
            # не оплачен ли этот вызов; verify/skip без id заказа и без файла — заказ
            # уже был, и его надо искать в кабинете Lumean, а не создавать заново.
            print_error_user_friendly(
                f"Lumean: абзац {idx}: журнал не разрешает новый платный заказ "
                f"(решение {decision.action}"
                f"{'; ' + decision.error if decision.error else ''}); "
                "проверьте заказ в кабинете Lumean, новый платный заказ не создан"
            )
            return False
        try:
            # POST /orders платный и не имеет idempotency key. После неопределённой
            # сетевой ошибки не создаём второй заказ молча: полученный id сразу
            # фиксируем receipt-ом, а неудавшийся POST оставляем fail-closed.
            _save_receipt(output_mp3_path, identity, state="creating")
            task_id = create_task(paragraph_text, voice_uuid=voice_uuid, model_uuid=model_uuid)
            # 03.09.2026: id заказа уходит в журнал сразу после POST /orders и ДО ожидания
            # готовности — деньги теряются именно в этом промежутке.
            journal.ordered(decision.call_id, task_id)
            _save_receipt(output_mp3_path, identity, task_id)
            print_debug(f"[{idx}] Lumean-заказ создан: {task_id}")
        except Exception as e:
            # Явный 4xx/локальная валидация означает, что заказ не создан. При timeout/5xx
            # marker остаётся: повторный запуск fail-closed и не списывает деньги дважды.
            # 03.09.2026: тот же признак отдаём журналу — условие вынесено в переменную,
            # чтобы receipt и журнал не могли разойтись в оценке «оплачено/не оплачено».
            rejected_before_order = (
                isinstance(e, ValueError) or
                (isinstance(e, LumeanAPIError) and e.status_code < 500 and e.status_code != 408))
            if rejected_before_order:
                _discard_creating_receipt(output_mp3_path, identity)
            journal.failed(decision.call_id, f"create: {e}", ordered=not rejected_before_order)
            print_error_user_friendly(f"Lumean: не удалось создать заказ для абзаца {idx}: {e}")
            return False

    try:
        order = wait_for_audio(task_id)
    except LumeanTerminalOrderError as e:
        _save_receipt(output_mp3_path, identity, task_id, state="terminal", status=e.status)
        # 03.09.2026: заказ существует — ordered=True, иначе журнал разрешит оплатить снова.
        journal.failed(decision.call_id, str(e), ordered=True)
        print_error_user_friendly(str(e))
        return False
    except LumeanManualRecoveryError as e:
        _save_receipt(output_mp3_path, identity, task_id, state="manual_recovery", status="manual_recovery")
        # 03.09.2026: оплаченный заказ ждёт ручного разбора в кабинете Lumean.
        journal.failed(decision.call_id, str(e), ordered=True)
        print_error_user_friendly(str(e))
        return False
    except Exception as e:
        # 03.09.2026: обрыв/таймаут/5xx уже ПОСЛЕ создания заказа — строка остаётся
        # «спросить провайдера», повтор без проверки запрещён.
        journal.failed(decision.call_id, f"wait: {e}", ordered=True)
        print_error_user_friendly(f"Lumean: не удалось дождаться заказа {task_id}: {e}")
        return False

    for attempt in range(MAX_RETRIES):
        try:
            get_audio_result(task_id, output_mp3_path, order=order)
            print_debug(f"Абзац '{idx}' успешно обработан")
            # 03.09.2026: mp3 лежит на диске — закрываем строку журнала.
            journal.delivered(decision.call_id, output_mp3_path)
            return True
        except LumeanFilesExpiredError as e:
            _save_receipt(output_mp3_path, identity, task_id, state="terminal", status="files_expired")
            # 03.09.2026: заказ был оплачен, файлы удалены по сроку хранения.
            journal.failed(decision.call_id, str(e), ordered=True)
            print_error_user_friendly(str(e))
            return False
        except Exception as e:
            print_debug(f"Попытка {attempt + 1}/{MAX_RETRIES} - Ошибка Lumean-заказа '{idx}': {e}")
            if attempt < MAX_RETRIES - 1:
                print_user("Проблема с сетью, пробую снова...", "WARNING")
                time.sleep(RETRY_DELAY_SECONDS)
    # 03.09.2026: заказ оплачен, но результат не забран — строка не закрывается как
    # доставленная и остаётся под вопросом к провайдеру.
    journal.failed(decision.call_id, f"download: не удалось забрать заказ {task_id}", ordered=True)
    print_error_user_friendly(f"Не удалось забрать озвучку Lumean для абзаца '{idx}'")
    return False

def main():
    global TTS_SPEED, AUDIO_DIR_NAME
    parser = argparse.ArgumentParser(description="Worker для озвучки текста через Lumean.")
    parser.add_argument("--source_file", required=True)
    parser.add_argument("--project_dir", required=True)
    parser.add_argument("--use_cache", action="store_true", help="Использовать существующие файлы.")
    parser.add_argument(
        "--full_text",
        action="store_true",
        help="Отправить весь непустой текст одним Lumean-заказом и сохранить один 1.mp3.",
    )
    parser.add_argument("--template_id", "--voice_uuid", dest="template_id", default=None,
                        help="UUID TTS-шаблона Lumean (--voice_uuid — legacy alias)")
    parser.add_argument("--model_id", "--model_uuid", dest="model_id", default=None,
                        help="Model ID Lumean (--model_uuid — legacy alias)")
    parser.add_argument(
        "--required_service", default="",
        help="До оплаты проверить service_key шаблона (серверный HeyGen-маршрут: heygen).",
    )
    parser.add_argument(
        "--audio_dir_name", default=AUDIO_DIR_NAME,
        choices=("_FINAL_AUDIO_ELEVENLABS", "_FINAL_AUDIO_LUMEAN"),
        help="Изолированная папка результата для выбранного серверного движка.",
    )
    parser.add_argument("--speed", default=os.environ.get("TTS_SPEED", "1.0"), help="Темп речи (atempo): 1.0=норма, <1 медленнее, >1 быстрее")
    parser.add_argument(
        "--tts_config", default="",
        help="JSON с настройками заказа (tts_settings/worker_settings/pause_settings/"
             "stress_settings) — кладутся в config_override поверх шаблона Lumean.",
    )
    args = parser.parse_args()
    if args.tts_config:
        try:
            parsed = json.loads(args.tts_config)
        except ValueError:
            print_error_user_friendly("Настройки озвучки пришли не JSON — заказ не отправляю")
            sys.exit(1)
        if not isinstance(parsed, dict):
            print_error_user_friendly("Настройки озвучки должны быть объектом")
            sys.exit(1)
        # Разделы, которых мы не знаем, не пропускаем: заказ платный, и отправлять в
        # него то, чего мы не понимаем, нельзя.
        unknown = [k for k in parsed if k not in CONFIG_OVERRIDE_SECTIONS]
        if unknown:
            print_error_user_friendly("Неизвестные разделы настроек: " + ", ".join(unknown))
            sys.exit(1)
        TTS_CONFIG_OVERRIDE.update(parsed)
    AUDIO_DIR_NAME = args.audio_dir_name
    try:
        TTS_SPEED = float(args.speed)
    except (TypeError, ValueError):
        TTS_SPEED = 1.0

    print_user("Начинаю озвучку...", "PROCESS")
    
    # Проверка API ключа
    if not _current_api_key():
        print_error_user_friendly(_('tts_api_key_error'))
        sys.exit(1)

    try:
        require_template_service(args.template_id, args.required_service)
    except Exception as exc:
        print_error_user_friendly(str(exc))
        sys.exit(1)
    
    print_debug("API ключ Lumean установлен")
    
    try:
        source_bytes = Path(args.source_file).read_bytes()
        paragraphs = [p.strip() for p in source_bytes.decode('utf-8', errors='replace').splitlines() if p.strip()]
    except FileNotFoundError:
        print_error_user_friendly(f"{_('file_not_found')}: {args.source_file}")
        return

    if not paragraphs:
        print_error_user_friendly("Файл для озвучки пуст.")
        sys.exit(1)

    normalized_text = "\n".join(paragraphs)
    try:
        marker_full_text = _project_full_text_marker(
            source_bytes, normalized_text, args.source_file, args.project_dir,
            args.template_id, args.model_id, args.speed,
        )
    except RuntimeError as exc:
        print_error_user_friendly(str(exc))
        sys.exit(1)
    full_text = bool(args.full_text or marker_full_text)
    identity_scope = (
        {"mode": "full_text_v1", "speed": _canonical_speed(args.speed)}
        if full_text else None
    )
    # Настройки — часть отпечатка: сменили стабильность или модель — это ДРУГАЯ озвучка,
    # и старый оплаченный файл переиспользовать нельзя (правило 4 ТЗ наоборот: повтор
    # платного вызова только когда вход тот же).
    if TTS_CONFIG_OVERRIDE:
        scope = dict(identity_scope or {})
        scope["config"] = json.dumps(TTS_CONFIG_OVERRIDE, ensure_ascii=False, sort_keys=True)
        identity_scope = scope

    if full_text:
        paragraphs = [normalized_text]
        print_user("Режим «весь текст»: один Lumean-заказ вместо поабзацной озвучки.", "INFO")

    output_dir = os.path.join(args.project_dir, AUDIO_DIR_NAME)
    os.makedirs(output_dir, exist_ok=True)
    print_debug(f"Аудиофайлы будут сохранены в: {output_dir}")

    if full_text:
        # Не смешиваем один full-text файл с прежней поабзацной озвучкой: рендер иначе
        # задвоит текст. Автоматически оплаченные/готовые артефакты не удаляем — такой
        # каталог требует отдельного явного разбора.
        existing_names = set(os.listdir(output_dir))
        protected_names = {
            name for name in existing_names
            if name.lower().endswith(".mp3")
            or name.lower().endswith(".mp3.lumean.json")
            or name.lower().endswith(".part.mp3")
        }
        allowed_names = {"1.mp3", "1.mp3.lumean.json"}
        unexpected_names = sorted(protected_names - allowed_names)
        if unexpected_names:
            print_error_user_friendly(
                "Lumean full-text: в выходной папке есть чужие аудио/receipt/part-файлы; "
                "автоматическое удаление запрещено: " + ", ".join(unexpected_names)
            )
            sys.exit(1)
        output_mp3_path = os.path.join(output_dir, "1.mp3")
        identity = _receipt_identity(
            paragraphs[0], args.template_id, args.model_id, identity_scope,
        )
        receipt = _read_receipt(output_mp3_path)
        if "1.mp3" in existing_names:
            cache_ok = (
                args.use_cache
                and os.path.getsize(output_mp3_path) > 0
                and receipt.get("provider") == "lumean"
                and receipt.get("state") == "created"
                and receipt.get("identity") == identity
                and receipt.get("api_scope") == _api_scope()
                and bool(receipt.get("order_id"))
            )
            if not cache_ok:
                print_error_user_friendly(
                    "Lumean full-text: существующий 1.mp3 не подтверждён точным receipt; "
                    "перезапись и повторная оплата запрещены."
                )
                sys.exit(1)
        elif "1.mp3.lumean.json" in existing_names:
            # Never overwrite an orphan receipt with a new paid order.  Exact identity
            # lets process_paragraph resume the already-created order; every other state
            # requires manual inspection.
            if not (
                receipt.get("provider") == "lumean"
                and receipt.get("state") == "created"
                and receipt.get("identity") == identity
                and receipt.get("api_scope") == _api_scope()
                and bool(receipt.get("order_id"))
            ):
                print_error_user_friendly(
                    "Lumean full-text: orphan receipt не подтверждает точный оплаченный заказ; "
                    "новый заказ автоматически не создан."
                )
                sys.exit(1)

    tasks = []
    for idx, paragraph_text in enumerate(paragraphs, 1):
        filename = f"{idx}.mp3"
        output_mp3_path = os.path.join(output_dir, filename)
        if args.use_cache and os.path.exists(output_mp3_path):
            print_debug(f"Пропускаем абзац {idx} (кэш)")
            continue
        tasks.append((
            idx, paragraph_text, output_mp3_path, args.template_id, args.model_id,
            args.use_cache, identity_scope,
        ))

    if not tasks:
        print_user(_('tts_no_new_paragraphs'), "INFO")
        return

    unit_name = "текстов" if full_text else "абзацев"
    print_user(f"Обработка {len(tasks)} {unit_name}...", "PROCESS")
    print_debug(f"Потоков: {PARALLEL_REQUESTS}")
    
    # Обработка без блокировок
    completed = 0
    failures = 0
    with ThreadPoolExecutor(max_workers=PARALLEL_REQUESTS) as executor:
        for result in executor.map(process_paragraph, tasks):
            completed += 1
            if result is not True:
                failures += 1
            if completed % 5 == 0:
                print_progress(completed, len(tasks), unit_name)

    if failures > 0:
        print_error_user_friendly(f"Озвучивание завершено с ошибками: {failures} из {len(tasks)} абзацев не обработано")
        sys.exit(1)
    print_user("Озвучивание завершено", "SUCCESS")

if __name__ == "__main__":
    main()
