# Сбои, восстановление и наблюдаемость

## Матрица типичных сбоев

| Стадия | Неоднозначность | Безопасное действие |
|---|---|---|
| Rewrite LLM read/call | ответ мог прийти частично | сохранить chunk evidence, bounded retry только незавершённого |
| TTS create | заказ мог быть создан до timeout | reconcile по operation/idempotency ID, не blind create |
| Image/video create | платная генерация могла начаться | poll/reconcile, затем manual recovery |
| File download | файл мог скачаться частично | temp + validate + repeat download |
| Scene publish | plan/meta могли разойтись | fail closed, восстановить последнюю согласованную revision |
| Render | process умер с partial MP4 | удалить/quarantine temp, не трогать current artifact |
| Cover batch | часть variants готова | `partial`, resume missing slots |
| Cover download/write | body оборвался после первых bytes | temp + decode/dimensions/SHA; current не менять |
| Upload до file input | side effect ещё не начат | можно requeue |
| Upload после file input | видео могло уйти | reconcile exact draft/video, re-upload запрещён |
| Schedule/finalize | UI ответ потерян | прочитать machine state строки Studio |
| Agent crash | lease истёк | fence old attempt, classify receipts, requeue/reconcile |
| Provider account limit | reset time известен | durable retry_after, не tight loop |
| Auth gate | нужен человек/устройство | human task, остальные releases продолжают |

## Retry policy

Retry должен задаваться по error class и stage. Общий бесконечный цикл запрещён.

Каждая policy включает:

- maximum attempts;
- exponential backoff + jitter;
- maximum elapsed time;
- provider reset/Retry-After support;
- budget/cost limit;
- terminal predicate;
- action after exhaustion: failed, human task или quarantine.

Одинаковая ошибка не должна логироваться каждую секунду. Progress heartbeat и warning rate
limit — разные механизмы.

## Recovery controller

Controller регулярно проверяет:

- expired leases;
- jobs без heartbeat;
- inconsistent stage/artifact states;
- paid operations без terminal receipt;
- upload phases `upload_started` без verified row;
- releases, чей scheduled slot приближается или прошёл;
- provider capacity leases и cooldowns;
- orphan files и DB references.

Recovery всегда идемпотентен. Он не выполняет expensive work сам, а ставит безопасную команду
в очередь или создаёт human task.

## Health

Нужны разные probes:

- **liveness**: process отвечает;
- **readiness**: DB/storage/migrations доступны, dispatcher запущен;
- **dependency health**: provider/agent/tunnel состояние, не блокирует liveness;
- **queue health**: oldest wait, running without heartbeat, retry volume;
- **business health**: покрытие ближайших channel slots и verified publications.

HTTP 200 главной страницы ничего не говорит о работающем scheduler.

## Structured events

Каждый log/event содержит:

- timestamp UTC;
- tenant/release/stage/job/attempt IDs;
- worker and build;
- event code;
- duration/progress;
- safe provider/account alias;
- normalized error class;
- no secret values and no raw user content by default.

User-facing event и operator log могут ссылаться на один event ID, но иметь разный уровень
деталей.

## Метрики

Минимум:

- queue depth/oldest age по stage/provider/tenant;
- running/lease expired;
- success/partial/failure/reconcile rates;
- p50/p95 stage duration;
- provider 429/5xx/auth/safety;
- retry attempts и recovered jobs;
- artifact validation failures;
- cost/usage по provider/tenant/stage;
- upload exact success, duplicate prevention, auth gates;
- scheduled slots: expected, assigned, in production, verified, empty;
- worker version distribution.

## Alerts

Alert нужен на устойчивый actionable симптом, а не на один transient timeout:

- dispatcher не делает claims при pending jobs;
- oldest queue age выше SLO;
- серия provider errors;
- expired leases без recovery;
- release прошёл deadline без verified schedule/publication;
- новый build резко увеличил partial/failure;
- shared account capacity исчерпана дольше допустимого;
- upload profile lease conflict;
- DB backup или restore verification не выполнялись.

## Операторский интерфейс

Для одного release оператор должен видеть timeline:

```text
frozen → rewrite succeeded → TTS retry 1 → TTS succeeded → images partial
→ missing scene regenerated → render succeeded → upload_started
→ browser crashed → reconciled draft → verified scheduled
```

Рядом нужны кнопки только для разрешённых действий: retry safe stage, resume missing,
reconcile external, cancel future work, open human task. Кнопки не должны скрывать разницу
между этими операциями.

## Deployment stability

- build artifact immutable и подписан hash;
- migrations выполняются отдельно и наблюдаемо;
- new worker сначала canary и сообщает capability/version;
- provider/lane можно pause точечно без global shutdown;
- old workers drain либо fenced;
- rollback не запускает code, который не понимает новую state machine;
- после deploy выполняются synthetic smoke и один controlled real canary.
