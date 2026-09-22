# Cover C2 — независимая приёмка применённой версии

14.09.2026. Применена версия manifest SHA256 `8b633f65d6e89730c07566207cc30feadfc33de900e2488efbf6d0347472b3b8`, production7 файлов. Критик прочитал deploy receipt, результаты service-interpreter matrix и production UI receipt; собственные candidate18 tests и hash audit приведены в `CRITIC_C2_NEIGHBORS_FINAL.md`.

| Сценарий | Корректность /30 | Конкуренция /20 | Recovery /30 | Applied /20 | Итог |
|---|---:|---:|---:|---:|---|
| S02 — личные пулы Cover |30|20|22.5|20|**PASS92**|
| S05 — remote ID и partial Cover |30|20|22.5|20|**PASS92**|

Баллы округлены вниз по заранее заданной рубрике. Recovery75%: неопределённый запрос без ID и invalid-key job имеют конечный partial/quarantine исход и требуют действий пользователя; автоматическое завершение через недоступный provider lookup не заявляется. Это известный ограниченный предел, а не скрытый повтор оплаченной работы. Все обязательные инварианты сохранения ID, исходного ключа, успешного item и конечного статуса проверены.

Доказательства:

- `cover_cycle2_deploy_receipt.json`: fresh baseline-CAS, active0, backup файлов и согласованных SQLite, integrity_check=ok,7 production hashes совпадают.
- `cover_cycle2_deployed_service_python_tests.log`: downloaded production bytes на `/root/cover_tool/venv/bin/python`3.10.12; **23 PASS,197.99s**. Pytest добавлен только во временный harness, application runtime dependencies штатные.102 jobs,3 own ключа34/33/33, per-key1/machine4; shared neighbor сохраняется. Ошибка одного ключа освобождает lease, соседний ключ завершает работу. Четыре real process-kill/restart cases включают title/URL и повтор после failed QC; исходный ID/ordinal сохранён. API idempotency/partial/full/budget/stale publication прошли.
- `cover_cycle2_applied_ui_receipt.json`: собственный режим задан до login; save3/1/1/1→reload→delete0; при задержанном GET и503 Save/generation disabled; Console errors/warnings пусты. Один real detail404 и9 следующих list polls без повторного detail404.
- `cover_cycle2_live_cleanup.json`: синтетический аккаунт/сессии и browser interception удалены; production jobs и paid provider calls не создавались.

S07 как общая межсервисная строка **NOT VERIFIED, не выше77**: покрытый Cover-подконтур не заменяет проверку Rewrite/VSGen consumers, их idempotency и поведения при длительной недоступности потребителя. S09/C3 disk admission/archive также ещё отдельно обязателен; retention-off защищает файлы, но не ограничивает рост диска.

Ещё одна общая граница, переданная root: Cover и VSGen имеют отдельное process-local physical-key accounting. Если один FastGen ключ сохранён в обоих сервисах, суммарный параллелизм ещё не ограничен единым межсервисным budget. S02 доказывает изоляцию внутри Cover; общий S14 обязан проверить совместный budget либо обеспеченную раздельность пулов. Не считать автоматически, что ключи у сервисов различны.

Эти два PASS не означают общей готовности80: остальные обязательные сценарии и целевой100/day профиль ещё принимаются.
