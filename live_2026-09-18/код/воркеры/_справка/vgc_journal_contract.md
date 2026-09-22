# VoiceGenCore C2 — durable remote IDs, кандидат для review

Дата: 2026-09-14. Production не изменялся; пакет разворачивает root после независимого review.

## Пакет и baseline

- `/root/VideoStudio/src/voicegencore_worker.py`: свежий VPS baseline SHA256 `70b6e7eeba2c77564c213bca1a3d5c39f1ce87356d4e2a27785628dbf58fd70d`, совпадал с локальным `src/voicegencore_worker.py`. Candidate SHA256 `6595e76d2132aa02d73782b1d59e3eeec392275452eec021a5df2bfbdecf6ea0`.
- Новый `/root/VideoStudio/src/remote_journal_vgc.py`: SHA256 `0b3b102902fffbaabe74328c9b799e2b234c8b5708e7ccf12cc6de13ff4fc3ac`.
- Кандидаты: `worker_candidate2/`; diff `vgc_cycle2.diff`; manifest `vgc_cycle2_manifest.json`; baseline evidence `vgc_cycle2_baseline.json`. Остальные файлы в `worker_candidate2` принадлежат root.
- Использует существующие requests, ffprobe/ffmpeg и Linux flock; новых pip зависимостей нет. `portable_paths.apply_audio_speed(..., strict=True)` существует на живом VPS (проверено SSH чтением сигнатуры), этот helper не менялся. При отсутствии helper скорость 1.0 разрешена, отличная от 1.0 останавливается, вместо тихой подмены результата.

## Контракт с сервером

Сервер задаёт `VS_REMOTE_JOURNAL_DIR=<resolved project>/.remote_jobs/<jid>`, `VS_QUEUE_JOB_ID=<jid>`, `VS_CREDENTIAL_SOURCE=byok|server` и `VS_QUEUE_CANCEL_FILE=<journal>/cancel.requested`. Каталог проверяется на точное совпадение с проектом, чужой путь отвергается. Без server ENV отдельный каталог `<project>/.remote_jobs/voicegencore-standalone` сохраняет работоспособность standalone-вызовов.

До платного POST атомарно fsync записывается `credential_pin.json` с `{family:voicegencore,sha256:<SHA256 raw key UTF-8>,state:uncertain,source:byok|server}`, затем request intent. В журнале хранятся SHA текста и параметров/выходного файла, SHA ключа, номер попытки, remote ID, состояние и контрольная сумма готового файла. Исходный ключ и текст не сохраняются. После принятого ответа ID фиксируется до первого poll. Server scheduler должен брать ровно закреплённый ключ/источник. Несовпадение даёт `__VGC_CREDENTIAL_REQUIRED__|<sha>` до HTTP.

Уточнённая политика освобождения pin: незавершённые/неопределённые запросы закрепляют исходный ключ; когда все записи completed/failed/rejected/cancelled, pin released. Проверенный локальный completed cache доступен по новому ключу без HTTP. При потере/порче completed-файла скачивание старого remote ID требует исходного ключа, заново оплачивать генерацию код не будет. Это позволяет сохранить 10 готовых абзацев по ключу A и создавать следующие по B, если незавершённых запросов A нет.

## Поведение отказов

- Потерянный ответ create, HTTP 5xx, ответ без ID или обрыв оставляют uncertain/intent, новый POST блокируется до ручной сверки у провайдера. У API в данном клиенте нет доказанного idempotency key или поиска по client request ID, поэтому автоматическое повторное создание здесь было бы повторной оплатой.
- Однозначные HTTP 400/401/403/404/422/429 сохраняются как rejected. После подтверждённого terminal failed следующая попытка может создать новую задачу; счётчик persisted, всего максимум 3 POST для этой identity. История старых terminal IDs сохранена.
- Poll/download ошибки и локальный таймаут сохраняют тот же ID. Таймаут не вызывает cancel. Poll deadline конечен (450 минут по умолчанию, `VGC_MAX_WAIT_MINUTES`), retry poll/download ограничен 8 попытками. `__VGC_REMOTE_PENDING__|<id>|...` даёт серверу/оператору различимый результат для continuation.
- Только explicit cancel file вызывает один bounded cancel POST (15 секунд). Флаг сохраняется до запроса: потеря ACK и перезапуск не повторяют cancel. Обычный SIGKILL/перезапуск сервиса никогда не отменяет remote task.
- Автозапуск остановленной provider queue — сохранённый one-shot: callback фиксирует флаг до POST `/client/queue/start`.
- Download: приватный временный MP3, Content-Length/непустота, fsync, строгая скорость, ffprobe audio+duration, atomic replace. Частичный/HTML результат не перетирает существующий final. Network deadline по умолчанию 900 секунд плюс максимум read timeout 30 секунд; преобразование скорости ограничивает существующий helper (900–7200 секунд, env override), ffprobe — 20 секунд.
- Exclusive output flock действует между процессами и job-каталогами одного проекта; изменение текста/настроек одного уже зарегистрированного output в той же job блокируется. Повреждённый журнал fail-closed без POST.
- `--full_text` resume не удаляет уже готовый файл; preflight не требует повторной квоты за уже принятые задачи; SHA готового cache проверяется при resume.

## Воспроизводимые проверки

Команда: `python3 research/launch_hardening_20260914/vgc_verify_cycle2.py`.

Результат: **29/29 PASS**, `vgc_verify_cycle2_result.json`, полный лог `vgc_verify_cycle2.log`. Проверяемые worker/journal функции не monkeypatch-ятся. Реальный localhost HTTPServer заменяет только внешнего провайдера; endpoint, polling delay и retry limits тестом конфигурируются. Использованы настоящие requests, JSON/fsync/flock, ffmpeg/ffprobe, child CLI и SIGKILL. Синтетические ключи, платных вызовов нет.

Покрытие: completed cache на новом ключе; временный poll 500 и продолжение того же ID; lost-create-ACK quarantine; rejected 429 и key rotation; persistent max3; обрыв download с сохранением старого final; HTML вместо MP3; poll timeout без cancel и user cancel ровно один раз; queue start lost ACK; потерянный completed file; changed identity; byok/server mismatch; корректный/чужой job scope; настоящая FS ошибка и recovery; output lock; 10 cached assets A → B; crash между pin/intent; crash между accepted record/pin; malformed journal; NaN speed до POST; реальный atempo 0.8; SIGKILL после сохранённого ID; полный CLI resume без cache flag с нулевой новой квотой; 100 отдельных job journals/аудиофайлов, 3 synthetic keys, 6 параллельных потоков.

Тест 100 jobs проверяет журнал/worker, **не** доказывает реальную скорость TTS-провайдера, серверный key leasing или нагрузочную мощность VPS. Key leasing и orchestration интеграционно проверяет root. Initial test harness имел неправильные поля synthetic `/client/me`; исправлен именно ответ HTTP-стенда по production `_vgc_access_error` (`has_active_subscription`, `free_remaining`), production preflight не ослаблялся.

## Deployment / drain / rollback

1. Root сверяет baseline worker hash и deployed helper signature, делает `cp` backup обоих существующих target файлов, если второй уже появился — сверяет его происхождение; атомарно ставит новый module до worker.
2. Worker читается новым subprocess, рестарт отдельного VGC unit не нужен: он запускается VSGen. Серверный unit и DB остаются root-owned (`vsgen`, `/root/VideoStudio/server/vsgen.db`); новой SQL migration в этом пакете нет.
3. При rollout не убивать старую VGC job посреди accepted remote task: у старого worker нет journal, её ID может быть только в stdout. Дождаться завершения ранее запущенных VGC workers либо явно сохранить их ID перед новым запуском. Новые запускаются с C2 ENV/pin-aware scheduler.
4. Отдельно подтвердить на VPS изолированный этот тест и root integration: restart/reclaim получает тот же SHA, удалённый credential приводит к actionable error, explicit cancel допускает grace для bounded request.
5. Rollback **не** запускать старый worker на job с unresolved C2 journals: он не понимает remote ID и может создать платный дубль. Остановить только новые VGC claims, сохранить журнал и output, дождаться/сверить затронутые accepted/uncertain задачи; предпочтителен forward fix. Никакие journals/MP3 при откате не удалять.

Открытая граница: автоматическая сверка uncertain create отсутствует, потому что её безопасная возможность у провайдера не доказана. Это видимая остановка одной задачи с сохранением улик, независимая очередь может продолжать работу. Новые требования к overall queue retry/deadline, tenant/key admission и cancel grace находятся в серверном C2 root.

Independent critic follow-up: lost/corrupt completed cache атомарно закрепляет исходный key/source ДО состояния pending и CredentialRequired. Pin меняется под pin.lock, только если нет другого unresolved credential; иначе видимый conflict без HTTP. Это исключает цикл scheduler с повторным выбором B/C. Дополненный suite PASS на новом hash.

## Cancel-only и manual retry (финальный C2 freeze)

`/usr/bin/python3 /root/VideoStudio/src/voicegencore_worker.py --project_dir <proj> --cancel_pending` использует pinned VGC_API_TOKEN и scoped ENV. Source file не требуется, новых create POST никогда нет. Batch ограничен hard wall clock 30 сек, cancel POST — 15 сек, status GET — 5 сек; сохраняемый cancel_attempted предотвращает второй POST после timeout/рестарта. Код 0 = все сохранённые запросы terminal, код 2 = неизвестный/неподтверждённый исход, отсутствующий original credential или storage conflict. Unknown create без ID нельзя безопасно отменить: он остаётся uncertain. Pin-only crash также не объявляется cancelled; terminal-record / stale-pin crash восстанавливает released pin.

Обычная отмена работающего worker дополнена одним GET подтверждения после POST (итого максимум 20 сек), укладывается в server35s grace. После подтверждённого cancelled и явного удаления cancel.requested сервером manual resume допускает новое создание в рамках persisted max3, preflight требует новую квоту. Если отмена опоздала и provider уже done/completed, resume закрепляет старый ключ и скачивает прежний оплаченный ID; новая генерация не запускается.

Новые actual HTTP/CLI cases: no-source cancel CLI с lost POST ACK и повтором без второго POST; uncertain cancel без guessed ID; hard batch deadline; orphan/terminal pin reconciliation; late completed cancel → исходный paid ID. Весь 29-case suite PASS.
