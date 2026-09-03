# Что разработчик должен сделать в первую неделю

## День 1: доказать понимание

- прочитать `START_HERE`, docs 01–08 и source index;
- нарисовать свою state machine release/stage/attempt;
- перечислить все external side effects и границы blind retry;
- поднять только synthetic fixtures;
- показать маленький crash-resume тест без реальных providers.

Результат дня — не новый UI, а короткая схема, risk register и работающий тест идемпотентности.

## Дни 2–3: каркас

- Postgres migrations для tenant/release/stage/attempt/artifact/external operation/event/outbox;
- API create/get release с idempotency;
- transactional claim/lease/fencing;
- fake provider worker, artifact validation и recovery controller;
- restart/failure injection tests.

Результат — один полностью синтетический release переживает kill/restart на каждой границе.

## Дни 4–5: первый legacy adapter

Подключить самый безопасный read/deterministic участок, например transcript или Rewrite на
fake/test credential. Записать exact request/response mapping, timeout classes, artifacts и
receipts. Реальные платные create пока закрыты feature flag и budget ceiling.

## До любого production canary

- code review и threat model;
- tenant isolation, secret scan и restore rehearsal;
- cost ceiling и kill switch;
- observability dashboard и runbook;
- rollback procedure;
- явное разрешение владельца на конкретный tenant/channel/provider.

Если кандидат предлагает в первую неделю перенести монолиты, собрать новый интерфейс и сразу
переключить production, это не соответствует цели устойчивого переписывания.
