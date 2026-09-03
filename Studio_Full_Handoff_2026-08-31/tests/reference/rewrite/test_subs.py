"""Вкладка «Субтитры»: ядро скачивания и HTTP-ручки.

Сеть не трогаем: живые прогоны делались вручную, а в тестах поведение YouTube
подменяется — иначе набор становится флаки и зависит от чужого сервиса.
"""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import subs_fetch


ROOT = Path(__file__).resolve().parents[1]


class _Track:
    """Заглушка дорожки субтитров youtube-transcript-api."""

    def __init__(self, code, generated, lines=("раз", "два")):
        self.language_code = code
        self.language = code
        self.is_generated = generated
        self._lines = lines

    def fetch(self):
        return [{"text": line} for line in self._lines]


# ---------------- разбор ссылок ----------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://www.youtube.com/watch?v=DEMO0000001", "DEMO0000001"),
        ("https://youtu.be/DEMO0000001", "DEMO0000001"),
        ("https://www.youtube.com/shorts/DEMO0000001", "DEMO0000001"),
        ("https://www.youtube.com/embed/DEMO0000001", "DEMO0000001"),
        ("https://www.youtube.com/live/DEMO0000001", "DEMO0000001"),
        ("https://www.youtube.com/watch?v=DEMO0000001&t=42s", "DEMO0000001"),
        ("  DEMO0000001  ", "DEMO0000001"),
        ("https://example.com/video", ""),
        ("", ""),
        # Длиннее 11 символов — это не идентификатор, а не «первые 11 подойдут»:
        # иначе разные ссылки схлопываются в одну при дедупликации.
        ("https://youtu.be/aaaaaaaaaaaaaaa", ""),
    ],
)
def test_extract_video_id_forms(raw, expected):
    assert subs_fetch.extract_video_id(raw) == expected


def test_parse_urls_dedupes_strips_numbering_and_caps():
    raw = """
    1. https://www.youtube.com/watch?v=DEMO0000001
    - https://youtu.be/DEMO0000001&t=10
    2) https://youtu.be/DEMO0000002

    мусор
    """
    parsed = subs_fetch.parse_urls(raw, 10)
    # Один и тот же ролик двумя формами ссылки — одна запись.
    assert parsed == [
        "https://www.youtube.com/watch?v=DEMO0000001",
        "https://youtu.be/DEMO0000002",
        "мусор",
    ]


def test_parse_urls_respects_limit():
    raw = "\n".join(f"https://youtu.be/vid{i:08d}" for i in range(9))
    assert len(subs_fetch.parse_urls(raw, 4)) == 4


def test_normalize_lang():
    assert subs_fetch.normalize_lang("") == "auto"
    assert subs_fetch.normalize_lang("AUTO") == "auto"
    assert subs_fetch.normalize_lang("RU") == "ru"
    assert subs_fetch.normalize_lang("zh-Hans") == "zh"
    with pytest.raises(subs_fetch.SubtitlesError):
        subs_fetch.normalize_lang("../etc/passwd")


def test_safe_filename_strips_forbidden_characters():
    name = subs_fetch.safe_filename(
        'Декабристы: Герои/Злодеи? "тест"', "DEMO0000003", "ru"
    )
    assert name.endswith("_DEMO0000003.ru.txt")
    for char in '\\/*?:"<>|':
        assert char not in name


def test_safe_filename_falls_back_without_title():
    assert subs_fetch.safe_filename("", "abc", "ru") == "subtitles_abc.ru.txt"


# ---------------- выбор дорожки ----------------


def test_auto_prefers_manual_track_of_original_language():
    """Язык оригинала берём у авто-дорожки, а текст — у ручной того же языка.

    Дорожки приходят в алфавитном порядке, поэтому «первая в списке» — не признак
    оригинала: у английской лекции первой идёт арабская.
    """
    tracks = [
        _Track("ar", False),
        _Track("en", False),
        _Track("ja", False),
        _Track("en", True),
    ]
    chosen = subs_fetch._pick_track(tracks, "auto")
    assert chosen.language_code == "en"
    assert chosen.is_generated is False


def test_auto_falls_back_to_generated_when_no_manual():
    tracks = [_Track("ar", False), _Track("de", True)]
    chosen = subs_fetch._pick_track(tracks, "auto")
    assert chosen.language_code == "de"


def test_explicit_language_prefers_manual():
    tracks = [_Track("ru", True), _Track("ru", False)]
    chosen = subs_fetch._pick_track(tracks, "ru")
    assert chosen.is_generated is False


def test_explicit_language_missing_returns_none():
    assert subs_fetch._pick_track([_Track("en", True)], "ru") is None


# ---------------- сборка текста ----------------


def test_only_adjacent_duplicates_are_collapsed():
    """Повтор через строку — авторский, подряд — артефакт наложения фраз."""
    data = [
        {"text": "припев"},
        {"text": "припев"},
        {"text": "куплет"},
        {"text": "припев"},
    ]
    assert subs_fetch._snippets_to_lines(data) == ["припев", "куплет", "припев"]


def test_whitespace_is_normalized():
    data = [{"text": "  много   пробелов \n и перенос "}]
    assert subs_fetch._snippets_to_lines(data) == ["много пробелов и перенос"]


# ---------------- классификация ошибок ----------------


def test_block_errors_are_flagged():
    class IpBlocked(Exception):
        pass

    message, blocked = subs_fetch._classify_error(IpBlocked("blocked"))
    assert blocked is True
    # Формулировка не должна обещать «попробуйте позже»: блокировка облачных
    # адресов постоянная, и без прокси повтор ничего не даёт.
    assert "прокси" in message
    assert "позже" not in message


def test_video_errors_are_not_flagged_as_block():
    class TranscriptsDisabled(Exception):
        pass

    message, blocked = subs_fetch._classify_error(TranscriptsDisabled("disabled"))
    assert blocked is False
    assert message == "У видео отключены субтитры"


def test_fetch_many_stops_after_block(monkeypatch):
    """Получив блокировку, остаток пачки не запрашиваем — IP дороже."""
    seen = []

    def fake_fetch_one(url, lang="auto", with_title=True, timeout=20.0, proxy=""):
        seen.append(url)
        result = subs_fetch.SubtitleResult(url=url, video_id="x")
        result.blocked = True
        result.error = "YouTube ограничил запросы с сервера"
        return result

    monkeypatch.setattr(subs_fetch, "fetch_one", fake_fetch_one)
    urls = [f"https://youtu.be/id{i}" for i in range(6)]
    results = subs_fetch.fetch_many(urls, parallel=2)

    assert len(seen) == 2, "после блокировки запросы должны прекратиться"
    assert len(results) == 6, "но отчитаться нужно по каждой ссылке"
    assert all(not r.ok for r in results)
    assert "Пропущено" in results[-1].error


def test_fetch_many_keeps_input_order(monkeypatch):
    def fake_fetch_one(url, lang="auto", with_title=True, timeout=20.0, proxy=""):
        result = subs_fetch.SubtitleResult(url=url, video_id=url[-1])
        result.ok = True
        result.text = url
        result.filename = url[-1] + ".txt"
        return result

    monkeypatch.setattr(subs_fetch, "fetch_one", fake_fetch_one)
    urls = [f"https://youtu.be/id{i}" for i in range(7)]
    assert [r.url for r in subs_fetch.fetch_many(urls, parallel=3)] == urls


# ---------------- HTTP-ручки ----------------


def _run_server_script(tmp_path, body: str) -> None:
    """Гоняем server.py в отдельном процессе с изолированными путями и БД."""
    env = os.environ.copy()
    env.update(
        {
            "PYTHONPATH": str(ROOT),
            "HOME": str(tmp_path),
            "REWRITE_SERVER_DIR": str(tmp_path),
            "REWRITE_DB_PATH": str(tmp_path / "jobs.db"),
            "SESSION_DB": str(tmp_path / "sessions.db"),
        }
    )
    proc = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(body)],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_endpoint_requires_session(tmp_path):
    _run_server_script(
        tmp_path,
        """
        from types import SimpleNamespace
        import server
        from fastapi import HTTPException

        request = SimpleNamespace(state=SimpleNamespace())
        try:
            server.subs_fetch(request, {"urls": "DEMO0000001"})
        except HTTPException as exc:
            assert exc.status_code == 401
        else:
            raise AssertionError("неаутентифицированный запрос принят")
        """,
    )


def test_endpoint_validates_input(tmp_path):
    _run_server_script(
        tmp_path,
        """
        from types import SimpleNamespace
        import server
        from fastapi import HTTPException

        server.license_mgr.is_user_active = lambda _uid: True
        request = SimpleNamespace(state=SimpleNamespace(user_id="u1"))

        for payload, code in (
            ({"urls": "   "}, 422),
            ({"urls": "нет ссылок тут", "lang": "../etc"}, 422),
            ({"urls": "\\n".join("https://youtu.be/vid%08d" % i for i in range(60))}, 413),
        ):
            try:
                server.subs_fetch(request, payload)
            except HTTPException as exc:
                assert exc.status_code == code, (payload, exc.status_code)
            else:
                raise AssertionError("принят некорректный ввод: %r" % (payload,))
        """,
    )


def test_endpoint_rejects_second_parallel_download(tmp_path):
    _run_server_script(
        tmp_path,
        """
        from types import SimpleNamespace
        import server
        from fastapi import HTTPException

        server.license_mgr.is_user_active = lambda _uid: True
        request = SimpleNamespace(state=SimpleNamespace(user_id="u1"))

        # Пользователь уже качает — вторая попытка должна получить 429,
        # иначе F5 на медленной пачке удваивает нагрузку на тот же IP.
        server._subs_active_users.add("u1")
        try:
            server.subs_fetch(request, {"urls": "https://youtu.be/aaaaaaaaaaa"})
        except HTTPException as exc:
            assert exc.status_code == 429
        else:
            raise AssertionError("вторая параллельная выгрузка принята")
        finally:
            server._subs_active_users.discard("u1")
        """,
    )


def test_download_single_file_is_plain_text(tmp_path):
    _run_server_script(
        tmp_path,
        """
        from types import SimpleNamespace
        import server

        batch = server._subs_store_batch("u1", [
            {"filename": "Декабристы: тест.ru.txt", "text": "строка"},
        ])
        request = SimpleNamespace(
            state=SimpleNamespace(user_id="u1"),
            query_params={"batch": batch},
        )
        response = server.subs_download(request)
        assert response.media_type.startswith("text/plain")
        disposition = response.headers["content-disposition"]
        # Заголовок собирается руками, поэтому ASCII-часть обязана быть ASCII,
        # иначе Starlette упадёт на latin-1 при отдаче.
        disposition.encode("latin-1")
        assert "filename*=UTF-8''" in disposition
        assert response.body.decode("utf-8") == "строка"
        """,
    )


def test_download_many_files_is_zip_with_ascii_name(tmp_path):
    _run_server_script(
        tmp_path,
        """
        import io
        import zipfile
        from types import SimpleNamespace
        import server

        batch = server._subs_store_batch("u1", [
            {"filename": "Первый ролик.ru.txt", "text": "раз"},
            {"filename": "Второй ролик.ru.txt", "text": "два"},
        ])
        request = SimpleNamespace(
            state=SimpleNamespace(user_id="u1"),
            query_params={"batch": batch},
        )
        response = server.subs_download(request)
        assert response.media_type == "application/zip"
        disposition = response.headers["content-disposition"]
        disposition.encode("ascii")
        archive = zipfile.ZipFile(io.BytesIO(response.body))
        names = archive.namelist()
        assert names == ["Первый ролик.ru.txt", "Второй ролик.ru.txt"]
        # Windows и macOS прочитают кириллицу только по этому флагу.
        assert all(info.flag_bits & 0x800 for info in archive.infolist())
        assert archive.read(names[0]).decode("utf-8") == "раз"
        """,
    )


def test_download_rejects_foreign_batch(tmp_path):
    _run_server_script(
        tmp_path,
        """
        from types import SimpleNamespace
        import server
        from fastapi import HTTPException

        batch = server._subs_store_batch("owner", [
            {"filename": "секрет.txt", "text": "чужой текст"},
        ])
        request = SimpleNamespace(
            state=SimpleNamespace(user_id="stranger"),
            query_params={"batch": batch},
        )
        try:
            server.subs_download(request)
        except HTTPException as exc:
            assert exc.status_code == 404
        else:
            raise AssertionError("чужой батч отдан")
        """,
    )


def test_batch_expires(tmp_path):
    _run_server_script(
        tmp_path,
        """
        from types import SimpleNamespace
        import server
        from fastapi import HTTPException

        batch = server._subs_store_batch("u1", [{"filename": "a.txt", "text": "x"}])
        server._subs_batches[batch]["created"] -= server.REWRITE_SUBS_BATCH_TTL_SEC + 1
        request = SimpleNamespace(
            state=SimpleNamespace(user_id="u1"),
            query_params={"batch": batch},
        )
        try:
            server.subs_download(request)
        except HTTPException as exc:
            assert exc.status_code == 404
        else:
            raise AssertionError("протухший батч отдан")
        """,
    )


def test_duplicate_filenames_are_separated(tmp_path):
    _run_server_script(
        tmp_path,
        """
        import io
        import zipfile
        from types import SimpleNamespace
        import server

        batch = server._subs_store_batch("u1", [
            {"filename": "одно имя.ru.txt", "text": "первый"},
            {"filename": "одно имя.ru.txt", "text": "второй"},
        ])
        request = SimpleNamespace(
            state=SimpleNamespace(user_id="u1"),
            query_params={"batch": batch},
        )
        archive = zipfile.ZipFile(io.BytesIO(server.subs_download(request).body))
        names = archive.namelist()
        assert len(set(names)) == 2, names
        assert archive.read(names[0]).decode() == "первый"
        assert archive.read(names[1]).decode() == "второй"
        """,
    )


def test_urls_are_not_written_to_logs():
    """Ссылки и тексты пользователя не должны утекать в лог сервера."""
    source = (ROOT / "server.py").read_text(encoding="utf-8")
    start = source.index("# ============== SUBTITLES")
    end = source.index("# ============== API v1")
    section = source[start:end]
    assert "print(" not in section
    assert "append_log" not in section
