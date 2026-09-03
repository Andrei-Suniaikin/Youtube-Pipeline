"""Aspect-ratio contract for Bing image search (offline, no network calls)."""

import importlib.util
import os
from types import SimpleNamespace

import pytest
from PIL import Image


IMAGE_WORKER = "/Users/owner/VideoStudio/src/image_worker.py"


@pytest.fixture(scope="module")
def iw():
    spec = importlib.util.spec_from_file_location(
        "vs_image_worker_aspect_under_test", IMAGE_WORKER
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_jpeg(path, size):
    """Write a noisy JPEG large enough to pass the worker's file-size gate."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    pixel_count = size[0] * size[1] * 3
    payload = os.urandom(pixel_count)
    Image.frombytes("RGB", size, payload).save(path, "JPEG", quality=95)
    assert os.path.getsize(path) >= 12000


def test_cli_accepts_only_supported_aspect_ratios(iw, tmp_path, capsys):
    parser = iw.build_arg_parser()
    base = ["--project_dir", str(tmp_path), "--from_scenes"]

    assert parser.parse_args(base).aspect_ratio == ""
    for aspect_ratio in ("landscape", "portrait", "square"):
        args = parser.parse_args(base + ["--aspect_ratio", aspect_ratio])
        assert args.aspect_ratio == aspect_ratio

    with pytest.raises(SystemExit) as exc:
        parser.parse_args(base + ["--aspect_ratio", "vertical"])
    assert exc.value.code == 2
    capsys.readouterr()


def test_bing_crawler_receives_native_layout_filter(iw, tmp_path, monkeypatch):
    calls = []

    class FakeCrawler:
        def __init__(self, **kwargs):
            self.downloader = SimpleNamespace(blocked_count=0)

        def crawl(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr(iw, "BingImageCrawler", FakeCrawler)

    expected = {
        "landscape": "wide",
        "portrait": "tall",
        "square": "square",
    }
    for aspect_ratio, layout in expected.items():
        iw._crawl_bing(
            "roman forum", str(tmp_path / aspect_ratio), 3,
            aspect_ratio=aspect_ratio,
        )
        assert calls[-1]["filters"] == {"size": "large", "layout": layout}

    # Backward compatibility: omitted aspect ratio keeps the exact old filter.
    iw._crawl_bing("roman forum", str(tmp_path / "legacy"), 3)
    assert calls[-1]["filters"] == {"size": "large"}


def test_candidate_orientation_rules_and_square_tolerance(iw, tmp_path):
    landscape = tmp_path / "landscape.jpg"
    portrait = tmp_path / "portrait.jpg"
    square_ok = tmp_path / "square-ok.jpg"
    square_bad = tmp_path / "square-bad.jpg"
    write_jpeg(str(landscape), (640, 400))
    write_jpeg(str(portrait), (400, 640))
    write_jpeg(str(square_ok), (1000, 910))   # 9% difference: accepted
    write_jpeg(str(square_bad), (1000, 899))  # 10.1% difference: rejected

    assert iw._image_candidate_ok(str(landscape), "landscape") is True
    assert iw._image_candidate_ok(str(landscape), "portrait") is False
    assert iw._image_candidate_ok(str(portrait), "portrait") is True
    assert iw._image_candidate_ok(str(portrait), "landscape") is False
    assert iw._image_candidate_ok(str(square_ok), "square") is True
    assert iw._image_candidate_ok(str(square_bad), "square") is False

    # Legacy mode has no orientation gate.
    assert iw._image_candidate_ok(str(portrait)) is True


def test_scene_search_skips_vertical_candidate_for_landscape(iw, tmp_path, monkeypatch):
    calls = []

    def fake_crawl(keyword, dest_dir, max_num, offset=0, downloader_threads=2,
                   aspect_ratio=""):
        calls.append(aspect_ratio)
        write_jpeg(os.path.join(dest_dir, "a-vertical.jpg"), (400, 640))
        write_jpeg(os.path.join(dest_dir, "b-landscape.jpg"), (640, 400))

    monkeypatch.setattr(iw, "_crawl_bing", fake_crawl)
    output = tmp_path / "googler" / "001.jpg"

    assert iw.search_image_for_scene(
        "roman forum", str(output), str(tmp_path / "tmp"),
        aspect_ratio="landscape",
    ) is True
    with Image.open(output) as image:
        assert image.width > image.height
    assert calls == ["landscape"]


def test_scene_mode_replaces_wrong_orientation_cache(iw, tmp_path, monkeypatch):
    project_dir = str(tmp_path)
    output = iw.scene_output_path(project_dir, 1)
    write_jpeg(output, (400, 640))
    calls = []

    def fake_search(query, output_path, tmp_dir, **kwargs):
        calls.append(kwargs.get("aspect_ratio", ""))
        write_jpeg(output_path, (640, 400))
        return True

    monkeypatch.setattr(iw, "search_image_for_scene", fake_search)
    have, total = iw.download_images_for_scenes(
        project_dir,
        [{"visual_prompt": "roman forum"}],
        threads=1,
        use_cache=True,
        aspect_ratio="landscape",
    )

    assert (have, total) == (1, 1)
    assert calls == ["landscape"]
    with Image.open(output) as image:
        assert image.width > image.height


def test_regen_threads_aspect_ratio_to_search(iw, tmp_path, monkeypatch):
    output = tmp_path / "googler" / "001.jpg"
    write_jpeg(str(output), (640, 400))
    calls = []

    def fake_search(query, output_path, tmp_dir, **kwargs):
        calls.append(kwargs)
        return True

    monkeypatch.setattr(iw, "search_image_for_scene", fake_search)
    args = SimpleNamespace(
        project_dir=str(tmp_path),
        regen_image=str(output),
        tags="",
        query="roman forum",
        scenes_file="",
        aspect_ratio="portrait",
    )

    assert iw.regen_scene_image(args) is True
    assert len(calls) == 1
    assert calls[0]["aspect_ratio"] == "portrait"


def test_legacy_tag_mode_is_unchanged_without_explicit_aspect(iw, tmp_path, monkeypatch):
    crawl_calls = []
    candidate_calls = []

    class FakeCrawler:
        def __init__(self, storage, **kwargs):
            self.root_dir = storage["root_dir"]
            self.downloader = SimpleNamespace(blocked_count=0)

        def crawl(self, **kwargs):
            crawl_calls.append(kwargs)
            write_jpeg(os.path.join(self.root_dir, "portrait.jpg"), (400, 640))

    def candidate_check(*args, **kwargs):
        candidate_calls.append((args, kwargs))
        return False

    monkeypatch.setattr(iw, "BingImageCrawler", FakeCrawler)
    monkeypatch.setattr(iw, "_image_candidate_ok", candidate_check)
    base = tmp_path / "Images"

    iw.download_images_for_tag(
        "roman forum", 1, 1, str(base), use_cache=False,
    )

    assert crawl_calls[0]["filters"] == {"size": "large"}
    assert candidate_calls == []
    assert (base / "1" / "portrait.jpg").exists()


def test_explicit_aspect_also_filters_legacy_tag_results(iw, tmp_path, monkeypatch):
    crawl_calls = []

    class FakeCrawler:
        def __init__(self, storage, **kwargs):
            self.root_dir = storage["root_dir"]
            self.downloader = SimpleNamespace(blocked_count=0)

        def crawl(self, **kwargs):
            crawl_calls.append(kwargs)
            write_jpeg(os.path.join(self.root_dir, "a-portrait.jpg"), (400, 640))
            write_jpeg(os.path.join(self.root_dir, "b-landscape.jpg"), (640, 400))

    monkeypatch.setattr(iw, "BingImageCrawler", FakeCrawler)
    base = tmp_path / "Images"

    iw.download_images_for_tag(
        "roman forum", 1, 2, str(base), use_cache=False,
        aspect_ratio="landscape",
    )

    assert crawl_calls[0]["filters"] == {"size": "large", "layout": "wide"}
    remaining = sorted((base / "1").glob("*.jpg"))
    assert [path.name for path in remaining] == ["b-landscape.jpg"]

