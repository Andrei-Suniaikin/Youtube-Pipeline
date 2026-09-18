"""Дедуп «🔴 Заливка остановлена: нужен ты» в src/upload_notify.py — офлайн.

Сеть не трогается: доставка подменяется параметром ``deliver`` (инъекция на
границе отправки), функция под проверкой — owner_help — не monkeypatch-ится.
Настоящие ~/.config/video-ops/telegram.json и ~/.local/state/video-studio/ не
читаются и не пишутся: оба пути уведены в tmp через env
VIDEO_UPLOAD_TELEGRAM_CONFIG / VIDEO_UPLOAD_NOTIFY_STATE_DIR.
"""
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
import upload_notify as notify  # noqa: E402

PROFILE = '28'
UC = 'UCIo_iS5kb9MRnhJK8i7IDnA'
OTHER_UC = 'UClNGNGulV7gPOrMxgzEmL-w'
REASON = 'капча или другая проверка «это точно ты»'
OTHER_REASON = 'нужно подтверждение телефона'
MISSING = object()


class Outbox:
    """Подставная доставка: запоминает тексты, отвечает по сценарию."""

    def __init__(self, results=()):
        self.sent = []
        self.results = list(results)

    def __call__(self, text):
        self.sent.append(text)
        return self.results.pop(0) if self.results else True


class NotifyTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state_dir = self.root / 'state'
        self.config_path = self.root / 'telegram.json'
        os.environ[notify.CONFIG_ENV] = str(self.config_path)
        os.environ[notify.STATE_DIR_ENV] = str(self.state_dir)
        for key in (notify.CONFIG_ENV, notify.STATE_DIR_ENV):
            self.addCleanup(os.environ.pop, key, None)
        self.write_config(repeat=30)
        self.outbox = Outbox()

    def write_config(self, repeat=MISSING):
        section = {'enabled': True, 'botToken': 'dummy-token-not-real', 'chatId': '1'}
        if repeat is not MISSING:
            section[notify.REPEAT_KEY] = repeat
        self.config_path.write_text(json.dumps({'telegram': section}), encoding='utf-8')
        # Права строго 0600 — иначе модуль считает конфиг небезопасным и молчит.
        self.config_path.chmod(stat.S_IRUSR | stat.S_IWUSR)

    def ping(self, profile=PROFILE, uc=UC, reason=REASON, **kwargs):
        return notify.owner_help(profile, uc, reason, host='mac-test',
                                 label='Тестовый канал', hint='подсказка',
                                 deliver=self.outbox, **kwargs)

    def record(self, profile=PROFILE, uc=UC):
        path = notify._state_path(profile, uc)
        return json.loads(path.read_text(encoding='utf-8'))


class DedupTests(NotifyTestBase):
    def test_first_ping_is_sent_and_remembered(self):
        before = time.time()
        self.assertTrue(self.ping())
        self.assertEqual(len(self.outbox.sent), 1)
        text = self.outbox.sent[0]
        self.assertTrue(text.startswith('🔴 Заливка остановлена: нужен ты'))
        self.assertIn(f'Причина: {REASON}', text)
        self.assertNotIn('dummy-token-not-real', text)
        record = self.record()
        self.assertEqual(record['reason'], REASON)
        self.assertGreaterEqual(record['sent_at'], before)
        self.assertLessEqual(record['sent_at'], time.time())

    def test_same_reason_within_window_is_suppressed(self):
        # ВТОРОЙ сценарий: запись уже есть — тот же профиль, канал и причина.
        self.assertTrue(self.ping())
        with self.assertLogs(notify.log, level='INFO') as captured:
            self.assertFalse(self.ping())
        self.assertEqual(len(self.outbox.sent), 1)
        self.assertTrue(any('повтор пинга подавлен' in line for line in captured.output),
                        captured.output)
        # Запись не перезаписана вторым (подавленным) вызовом.
        self.assertEqual(self.record()['reason'], REASON)

    def test_changed_reason_goes_out_immediately(self):
        self.assertTrue(self.ping())
        self.assertTrue(self.ping(reason=OTHER_REASON))
        self.assertEqual(len(self.outbox.sent), 2)
        self.assertIn(f'Причина: {OTHER_REASON}', self.outbox.sent[1])
        self.assertEqual(self.record()['reason'], OTHER_REASON)

    def test_expired_record_sends_again(self):
        # Существующая старая запись: та же причина, но старше окна.
        stale_at = time.time() - 31 * 60
        path = notify._state_path(PROFILE, UC)
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'schema_version': 1, 'reason': REASON,
                                    'sent_at': stale_at}), encoding='utf-8')
        self.assertTrue(self.ping())
        self.assertEqual(len(self.outbox.sent), 1)
        self.assertGreater(self.record()['sent_at'], stale_at + 30 * 60)

    def test_reason_whitespace_does_not_defeat_the_dedup(self):
        self.assertTrue(self.ping())
        self.assertFalse(self.ping(reason='  капча   или другая проверка\n«это точно ты» '))
        self.assertEqual(len(self.outbox.sent), 1)

    def test_failed_send_is_not_remembered_and_the_next_attempt_goes_out(self):
        self.outbox = Outbox(results=[False, True])
        self.assertFalse(self.ping())
        self.assertFalse(notify._state_path(PROFILE, UC).exists())
        self.assertTrue(self.ping())
        self.assertEqual(len(self.outbox.sent), 2)
        self.assertEqual(self.record()['reason'], REASON)

    def test_other_channel_of_the_same_profile_is_not_suppressed(self):
        self.assertTrue(self.ping())
        self.assertTrue(self.ping(uc=OTHER_UC))
        self.assertEqual(len(self.outbox.sent), 2)
        self.assertTrue(notify._state_path(PROFILE, UC).exists())
        self.assertTrue(notify._state_path(PROFILE, OTHER_UC).exists())

    def test_odd_profile_and_uc_values_make_a_valid_file_name(self):
        # Ручной вызов 17.09 выглядел ровно так: профиль «—», канал словом.
        self.assertTrue(self.ping(profile='—', uc='проверка'))
        path = notify._state_path('—', 'проверка')
        self.assertEqual(path.parent, self.state_dir)
        self.assertTrue(path.exists(), path)
        self.assertNotIn('/', path.name)
        self.assertFalse(self.ping(profile='—', uc='проверка'))
        self.assertEqual(len(self.outbox.sent), 1)
        # Разные «странные» значения не сливаются в один файл.
        self.assertNotEqual(notify._state_path('—', 'проверка'),
                            notify._state_path('–', 'проверка'))

    def test_record_is_seen_by_another_process(self):
        # Боевой случай: puller запускает отдельный процесс на каждый ролик.
        self.assertTrue(self.ping())
        code = (
            'import sys, json; sys.path.insert(0, sys.argv[1]); import upload_notify as n; '
            'print(json.dumps(n.owner_help(sys.argv[2], sys.argv[3], sys.argv[4], '
            'host="other-process", deliver=lambda text: True)))'
        )
        run = subprocess.run([sys.executable, '-c', code, str(ROOT / 'src'), PROFILE, UC, REASON],
                             capture_output=True, text=True, env=dict(os.environ), timeout=60)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout.strip()), False)
        self.assertEqual(self.record()['reason'], REASON)


class ConfigTests(NotifyTestBase):
    def test_without_repeat_minutes_behaviour_is_unchanged(self):
        self.write_config()  # ключа нет вовсе — как на машинах до правки конфига
        self.assertTrue(self.ping())
        self.assertTrue(self.ping())
        self.assertEqual(len(self.outbox.sent), 2)
        self.assertFalse(self.state_dir.exists())

    def test_zero_switches_the_dedup_off_quietly(self):
        self.write_config(repeat=0)
        self.assertEqual(notify._repeat_window_seconds(), 0)
        self.assertTrue(self.ping())
        self.assertTrue(self.ping())
        self.assertEqual(len(self.outbox.sent), 2)
        self.assertFalse(self.state_dir.exists())

    def test_garbage_value_warns_and_keeps_the_old_behaviour(self):
        for garbage in ('abc', -5, [30]):
            with self.subTest(value=garbage):
                self.write_config(repeat=garbage)
                self.outbox = Outbox()
                with self.assertLogs(notify.log, level='WARNING') as captured:
                    self.assertTrue(self.ping())
                    self.assertTrue(self.ping())
                self.assertEqual(len(self.outbox.sent), 2)
                self.assertTrue(any(notify.REPEAT_KEY in line for line in captured.output),
                                captured.output)
        self.assertFalse(self.state_dir.exists())

    def test_string_minutes_are_accepted(self):
        self.write_config(repeat='30')
        self.assertEqual(notify._repeat_window_seconds(), 1800)

    def test_unwritable_state_dir_still_sends_and_warns(self):
        blocker = self.root / 'not-a-directory'
        blocker.write_text('x', encoding='utf-8')
        os.environ[notify.STATE_DIR_ENV] = str(blocker)
        with self.assertLogs(notify.log, level='WARNING') as captured:
            self.assertTrue(self.ping())
        self.assertEqual(len(self.outbox.sent), 1)
        self.assertTrue(any('не сохран' in line for line in captured.output), captured.output)
        # Без записи повтор уйдёт снова — деградация видна в логе, а не молчит.
        self.assertTrue(self.ping())
        self.assertEqual(len(self.outbox.sent), 2)

    def test_unexpected_state_error_before_sending_still_sends_and_warns(self):
        # Дедуп «мягкий»: сюрприз при чтении памяти (не OSError/ValueError) не
        # должен глотать ПЕРВОЕ сообщение. Подменяется только чтение памяти —
        # owner_help под проверкой вызывается по-настоящему.
        with mock.patch.object(notify, '_read_last', side_effect=RuntimeError('boom')):
            with self.assertLogs(notify.log, level='WARNING') as captured:
                self.assertTrue(self.ping())
        self.assertEqual(len(self.outbox.sent), 1)
        self.assertTrue(any('отправляю без' in line for line in captured.output),
                        captured.output)

    def test_unencodable_reason_is_sent_and_does_not_raise_after_sending(self):
        # Суррогат в причине: JSON пишется без ensure_ascii → UnicodeEncodeError
        # (ValueError, не OSError). Сообщение уже ушло — падать поверх нельзя.
        bad = 'капча \udcff'
        with self.assertLogs(notify.log, level='WARNING') as captured:
            self.assertTrue(self.ping(reason=bad))
        self.assertEqual(len(self.outbox.sent), 1)
        self.assertTrue(any('не сохран' in line for line in captured.output), captured.output)
        # Записи нет → повтор уйдёт снова; деградация видна в логе, не молчит.
        self.assertTrue(self.ping(reason=bad))
        self.assertEqual(len(self.outbox.sent), 2)

    def test_disabled_config_sends_nothing_and_writes_nothing(self):
        section = {'enabled': False, 'botToken': 'dummy-token-not-real', 'chatId': '1',
                   notify.REPEAT_KEY: 30}
        self.config_path.write_text(json.dumps({'telegram': section}), encoding='utf-8')
        self.config_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        self.assertFalse(notify.available())
        # Настоящая send() при выключенном конфиге не шлёт и до сети не доходит.
        self.assertFalse(notify.owner_help(PROFILE, UC, REASON))
        self.assertFalse(self.state_dir.exists())


if __name__ == '__main__':
    unittest.main()
