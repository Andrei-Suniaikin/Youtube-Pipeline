# Сквозные контракты и инварианты

Ниже не предложение по названиям классов. Это поведение, которое новая система обязана
гарантировать независимо от выбранного framework.

## 1. Одна строка — один канал — один release

Одна строка контент-плана назначена одному точному YouTube UC. Теги, язык и ниша — только
фильтры. Если один donor нужен на трёх каналах, создаются три явные строки и три releases.

После freeze поля контента и канала не меняются. Исправление контента или канала создаёт новый
release, а старый остаётся в audit trail. Изменение времени создаёт новую неизменяемую
`schedule_revision`, но сохраняет `release_id`.

Production сейчас умеет автоматически переписывать пропущенный `slot_at` внутри того же release.
В целевой модели так делать нельзя: перенос всегда создаёт новый `schedule_revision_id`, старую
revision сохраняет, а указатель active revision переключает одной транзакцией. Эта модель выбрана
как целевой контракт; это не открытый вопрос реализации.

## 2. Неизменяемая идентичность release

`release_id` связывает:

- tenant;
- exact channel ID/UC;
- source snapshot/hash;
- версии presets;
- immutable content/plan revision.

Повтор команды с тем же idempotency key возвращает существующий release и не создаёт второй.

Время публикации не входит в идентичность release. Оно принадлежит отдельной неизменяемой
`schedule_revision` с полями `schedule_revision_id`, `release_id`, local wall time, IANA timezone,
resolved UTC instant, reason и predecessor ID. Каждый upload/publication attempt навсегда
привязан к одному `schedule_revision_id`; смена active revision не перепривязывает старый attempt.
Idempotency key публикационного действия включает оба ID. После `upload_started` перенос сначала
reconcile-ит существующее видео и меняет расписание только через доказанную операцию над ним;
создание второй загрузки запрещено.

## 3. Стадии — явная state machine

Рекомендуемый минимум:

```text
blocked ──dependency ready──► queued ──claim──► running
  ▲                              │                │
  │                              │ cancel         ├──validated──► succeeded
  │                              ▼                ├──retryable──► retry_wait ─► queued
  └────────terminal/manual──── cancelled          ├──terminal───► failed
                                                 └──ambiguous──► reconcile_required
```

`succeeded` означает, что ожидаемые outputs проверены и опубликованы атомарно. Exit code 0,
HTTP 200 или наличие одного файла недостаточны.

## 4. Каждая попытка отдельна

Stage и attempt — разные сущности. Attempt хранит:

- attempt number;
- worker/capability version;
- claim token и lease deadline;
- input fingerprint;
- provider credential ID только как безопасный opaque reference;
- started/heartbeat/finished;
- normalized error class;
- external operation IDs;
- created artifacts.

Старый attempt после потери lease не может писать progress или terminal result.

## 5. Idempotency key включает реальные входы

Для deterministic stage:

```text
hash(stage type + source hashes + preset versions + normalized settings + model/provider policy)
```

Смена текста, голоса, speed, aspect ratio или model создаёт другой fingerprint. Повтор с тем же
fingerprint использует проверенный artifact; изменение settings не может тихо переиспользовать
старый результат.

## 6. Артефакт публикуется только после проверки

Общий протокол:

```text
write temp → close → validate → compute SHA-256 → fsync file/dir → atomic publish → DB commit
```

Artifact record хранит media type, bytes, SHA, producer attempt, input fingerprint и validation.
Папка сама по себе не является authoritative manifest.

## 7. Полнота считается по ожидаемым slots

Например, images stage ожидает scenes 1..N. Если есть 59 из 60 валидных изображений, состояние
может быть `partial`, но не `succeeded`. При допустимом business threshold это должно быть
явной policy: какие gaps разрешены и как timeline их компенсирует.

Нельзя выводить completeness из exit code или количества найденных файлов без known expected.

В текущем VSGen auto-render допускает известный legacy threshold 70%. Целевая модель не должна
терять эту бизнес-возможность, но обязана разделять факты: media stage остаётся `partial`, а
render stage получает явную policy `allow_gaps` с minimum ratio и детерминированным fallback.
Порог не должен превращать «42 из 60» в ложное «60 из 60».

## 8. Оплачиваемые/необратимые операции имеют receipts

Внешние действия делятся на классы:

| Класс | Пример | Повтор после timeout |
|---|---|---|
| read | poll provider, read Studio row | безопасен |
| deterministic write | upload artifact по content-address | безопасен с idempotency key |
| paid create | заказать TTS/image/video | только по provider idempotency или reconcile |
| irreversible browser action | отправить MP4 в YouTube | blind retry запрещён |

До paid/create side effect attempt сохраняет intent и idempotency key. После ответа сохраняет
external operation ID. Если сеть оборвалась между ними, job переходит в `reconcile_required`,
а не делает новый create.

## 9. Upload boundary

До первого фактического выбора MP4 в file input загрузку можно безопасно начать заново.
Непосредственно перед ним durable receipt должен перейти в `upload_started`.

После `upload_started` разрешены только:

- найти точный draft/video по evidence;
- продолжить существующий wizard;
- reconcile final row;
- отправить проблему человеку.

Новый blind upload того же release запрещён.

## 10. Exact YouTube proof

`scheduled` считается подтверждённым только при совпадении:

- exact channel UC;
- video ID;
- normalized title hash или другой release marker;
- `userSetVisibility=SCHEDULED`;
- expected timezone-aware epoch с заданным tolerance;
- timestamp чтения и machine-state evidence.

После наступления срока `published` требует фактический `PUBLIC`. Текст финального диалога не
используется как источник истины.

## 11. AdsPower profile lease

Профиль нельзя использовать одновременно на двух machines или двумя processes. Lease должен:

- быть глобальным для всей инфраструктуры;
- охватывать start, browser session, finalize и stop;
- иметь heartbeat/fencing token;
- проверять фактический `/browser/local-active` на обеих машинах;
- не освобождаться, пока возможен отложенный старт browser process.

Недоступность машины проверки означает fail closed, а не предположение «там пусто».

## 12. Fairness и shared capacity

Fair queue: round-robin по tenants/users, FIFO внутри tenant, отдельные provider capacity groups.

Один физический credential/account может использоваться разными products. Capacity manager
должен быть глобальным, а не по одному scheduler на сайт. Дубликат одного credential не должен
увеличивать capacity.

## 13. Cancel

Cancel — persistent intent. Для queued job он terminal. Для running job worker прекращает
безопасные действия cooperative, а после внешней границы сначала завершает reconcile.

Cancel не удаляет audit, receipts и уже созданные оплаченные artifacts.

## 14. Recovery

После restart controller:

1. помечает expired leases;
2. fenced old workers;
3. классифицирует stage по последнему durable receipt;
4. безопасные stages requeue;
5. ambiguous paid/upload stages отправляет на reconcile;
6. terminal/manual problems изолирует;
7. продолжает остальные releases.

## 15. Error taxonomy

Минимальные classes:

- transient network/provider;
- provider rate/account limit;
- authentication/credential disabled;
- input validation/schema;
- safety/policy rejection;
- incomplete artifact;
- external operation ambiguous;
- manual auth gate;
- capacity/queue pause;
- terminal internal invariant violation.

User получает короткую причину и действие. Operator получает structured error с cause chain.
Сырой stack trace не показывается пользователю.

## 16. Tenant isolation

Каждый DB query, artifact path и API operation привязан к tenant/release. Client не передаёт
server credential ID. Download path разрешается только внутри tenant artifact root. API key
имеет scope, hash-at-rest, created/revoked metadata и показывается plain только один раз.

## 17. Версии и provenance

Любой результат должен отвечать на вопросы:

- какой commit/image создал его;
- какой worker capability использовался;
- какие provider/model/preset versions применены;
- какие exact inputs и hashes;
- какие проверки прошли;
- каким attempt он опубликован.

Без этого ошибка на отдельной render-нode или Desktop build не воспроизводится.
