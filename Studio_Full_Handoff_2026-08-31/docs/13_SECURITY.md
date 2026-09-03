# Безопасность и доступы

Архив намеренно не содержит доступов. Разработчику сначала нужен локальный синтетический стенд;
production read-only и тем более write-доступ выдаётся только под конкретную проверку.

## Границы доверия

- browser/user ↔ public API;
- control plane ↔ workers/edge agents;
- platform ↔ provider APIs;
- upload agent ↔ AdsPower/YouTube session;
- tenant A ↔ tenant B;
- database ↔ object storage;
- operator/admin ↔ audit-sensitive actions.

Для каждой границы нужны отдельная identity, scope, timeout, rate limit и audit. Один общий admin
token или SSH-доступ ко всему контуру не является сервисной аутентификацией.

## Известные риски текущей реализации

- часть соседних service keys и upload token хранится открыто в admin state;
- BYOK/OAuth state может храниться как raw JSON;
- legacy admin routes принимают token в query string;
- browser session ID используется клиентом длительно;
- CORS слишком широк в отдельных сервисах;
- некоторые user errors могут раскрывать traceback, path, prompt/provider fragments;
- login throttle process-local;
- сервисы запущены с избыточными системными правами;
- Rewrite/Cover users связаны с VSGen по username и прямому чтению чужих DB;
- удаление Cover account и отзыв его API keys не образуют гарантированную единую операцию;
- public docs/OpenAPI и response headers не имеют единой policy;
- batch limits и storage/cost quotas различаются между UI и partner API.

Это checklist для исправления, а не разрешение эксплуатировать уязвимость.

## Целевые правила

### Secrets

- secret manager или envelope-encrypted store;
- в DB/jobs только opaque credential ID;
- rotation и revocation без deploy;
- разные credentials для dev/staging/prod;
- никогда не передавать secret через URL, log, exception или artifact manifest;
- automated secret scan в CI и перед release archive.

### Auth и tenancy

- stable tenant/user UUID, не username;
- короткая server session с secure HttpOnly/SameSite cookie;
- machine API keys: hash-at-rest, scopes, expiration, last-used и revoke;
- RBAC для operator/admin/provider management;
- tenant condition во всех queries и object-store policies;
- account deletion сначала отзывает sessions/API keys, затем ставит auditable cleanup job.

### Files и URLs

- canonical path containment;
- проверка MIME по bytes, size/dimension/duration limits;
- безопасная распаковка архивов;
- SSRF allowlist/egress proxy для user URLs;
- presigned URLs с коротким сроком и конкретным object key;
- antivirus/sandbox по выбранной risk policy.

### Edge agents

- отдельная host identity и минимальный scoped token;
- heartbeat, version, fencing token;
- команды только для allowlisted capability;
- browser profile lease глобальна и fail-closed;
- локальные receipts шифруются/защищаются правами и синхронизируются durable.

### Web

- explicit CORS origins;
- CSP, HSTS, frame-ancestors/X-Frame-Options, Referrer-Policy;
- CSRF protection для cookie-auth mutations;
- durable distributed rate limit;
- safe public error code вместо stack trace;
- `/docs` и admin endpoints по явной environment policy.

## Доступ разработчика

Рекомендуемый порядок:

1. архив и synthetic fixtures без доступа;
2. локальный compose/staging с fake providers;
3. read-only source/schema/metrics по VPN и персональной identity;
4. scoped test provider keys с budget ceiling;
5. один canary tenant/channel;
6. production mutation только через deploy pipeline и audit.

Доступы нельзя класть в issue, чат, ZIP или `.env.example`. При увольнении/смене подрядчика
отзываются персональные credentials и проверяется audit, а shared secret ротируется.

## Security gate перед миграцией

- threat model и data classification согласованы;
- список secrets и владельцев составлен без выгрузки значений;
- backup/restore проверен;
- tenant isolation tests зелёные;
- paid operations имеют cost ceiling;
- dev не может обратиться к production storage/DB случайной переменной;
- redaction тестируется на логах и error responses;
- rollback не восстанавливает отозванные credentials.
