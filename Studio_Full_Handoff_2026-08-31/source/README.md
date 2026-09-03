# О снимках исходников

Базовые `vsgen_server`, `rewrite` и `cover` сняты read-only с процессов 29.08.2026.
31.08 production повторно сверён: в VSGen обновлены изменившиеся `main.py` и
`src/tts_worker.py`; Rewrite и Cover не имели code/schema drift.
`desktop` взят из локального dirty working tree и не подтверждён как содержимое последнего EXE.

Это reference source, не единый запускаемый repository. В снимках нет `.env`, баз, логов,
пользовательских файлов, provider credentials и deployment access. Адреса и персональные literals
санитизированы; поэтому SHA файлов внутри ZIP отличается от original production SHA. Оригинальные
SHA основных файлов записаны в `manifest/SOURCE_PROVENANCE.md` только для сверки происхождения.

Не deploy-ить эти папки напрямую. Сначала прочитать `docs/00_SCOPE_AND_TRUTH.md`,
`docs/12_SOURCE_INDEX.md` и повторно снять live diff конкретного сервиса.
