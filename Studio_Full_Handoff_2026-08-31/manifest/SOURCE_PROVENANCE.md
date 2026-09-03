# Происхождение снимка

Базовая фиксация: 2026-08-29. Повторная live-сверка: 2026-08-31. Серверные файлы получены
read-only из активных systemd working directories. После копирования адреса, персональные
данные и credential-like literals прошли санитизацию. `SHA256SUMS` подтверждает уже
передаваемую, санитизированную копию.

## Оригинальные production fingerprints до санитизации

| Контур | Файл | Строк | SHA-256 prefix |
|---|---|---:|---|
| VSGen | `main.py` | 16 965 | `e1cc373f` |
| VSGen | `src/tts_worker.py` | 786 | `fa7d884c` |
| Rewrite | `server.py` | 3 443 | `60070eb8` |
| Rewrite | `rewrite_core.py` | 9 930 | `f13696c` |
| Rewrite | `job_queue.py` | 1 116 | `924be72a` |
| Cover | `cover_server.py` | 2 064 | `00254e21` |
| Cover | `cover_core.py` | 2 828 | `55ffb9a9` |
| Cover | `job_queue_cover.py` | 1 002 | `12f953c1` |

Prefixes нужны только для идентификации снимка, не для проверки передаваемых файлов.

VSGen baseline `main.py` до дельты имел raw SHA-256 prefix `9a1cee81` и 16 742 строки.
Дельта применена к уже санитизированной копии; автоматическая проверка подтвердила, что её
отличие от current raw состоит ровно из прежних четырёх redaction transforms. У TTS повторно
удалён tenant-specific default template UUID.

31.08 raw fingerprints всех runtime-файлов Rewrite и Cover совпали с baseline 29.08.
Их исходники не копировались повторно, чтобы не вернуть удалённые credential/privacy literals.

## Локальные репозитории на дату снимка

| Контур | HEAD | Changed/untracked entries | Роль |
|---|---|---:|---|
| VideoStudio | `6033c0d035f1ac192de4ccf0264cd6301bc9faa1` | 208 | Desktop working tree/reference tests |
| Rewrite | `c88631c72463aa6290bd3506dc12172c58e32965` | 6 | не источник production source |
| Cover | `f69dbae064cf3795fd8d8a359640faff96047bfb` | 7 | не источник production source |

Из-за dirty trees пакет собран allowlist-ом, а не `git archive`.

## Недоступные источники

- Windows build host: не удалось снять build directory/EXE manifest.
- Отдельная render/upload node: не удалось подтвердить exact worker/puller versions.

Эти пробелы нельзя закрывать предположением. Перед первой миграцией соответствующей stage нужен
новый read-only inventory и SHA diff.
