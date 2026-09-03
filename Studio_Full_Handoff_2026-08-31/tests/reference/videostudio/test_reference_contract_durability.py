from __future__ import annotations

from io import BytesIO
import errno
import os
from pathlib import Path
import stat
import sys

import pytest
from PIL import Image


SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import reference_contract as contract  # noqa: E402
from reference_contract import (  # noqa: E402
    ReferenceContractError,
    current_project_reference_state,
    import_reference_sources,
    save_project_reference_state,
)


def _image_bytes(color: tuple[int, int, int]) -> bytes:
    output = BytesIO()
    Image.new("RGB", (32, 24), color).save(output, format="PNG")
    return output.getvalue()


def _event_after(
    events: list[tuple[str, object]],
    event: tuple[str, object],
    after: int,
) -> int:
    return next(index for index in range(after + 1, len(events)) if events[index] == event)


def test_brand_new_reference_directories_are_synced_before_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "brand-new-project"
    events: list[tuple[str, object]] = []
    real_mkdir = Path.mkdir
    real_directory_fsync = contract._fsync_directory

    def traced_mkdir(self: Path, *args, **kwargs):
        existed = os.path.lexists(self)
        result = real_mkdir(self, *args, **kwargs)
        if not existed and os.path.lexists(self):
            events.append(("mkdir", self.resolve()))
        return result

    def traced_directory_fsync(path: Path) -> None:
        resolved = Path(path).resolve()
        events.append(("fsync", resolved))
        real_directory_fsync(resolved)

    monkeypatch.setattr(Path, "mkdir", traced_mkdir)
    monkeypatch.setattr(contract, "_fsync_directory", traced_directory_fsync)

    hero = import_reference_sources(
        [_image_bytes((30, 60, 90))],
        project,
        kind="hero",
        max_sources=1,
    )[0]
    events.append(("success", "import"))
    save_project_reference_state(project, {"Captain": [hero.path]}, [])
    events.append(("success", "save"))

    assets = project / "_ASSETS"
    uploads = assets / "reference_uploads"
    history = assets / contract.PROJECT_REFERENCE_STATE_HISTORY_DIR
    commits = assets / contract.PROJECT_REFERENCE_STATE_COMMITS_DIR
    import_success = events.index(("success", "import"))
    save_success = events.index(("success", "save"))

    project_mkdir = events.index(("mkdir", project.resolve()))
    project_publish = _event_after(
        events, ("fsync", project.parent.resolve()), project_mkdir
    )
    assets_mkdir = events.index(("mkdir", assets.resolve()))
    assets_publish = _event_after(
        events, ("fsync", project.resolve()), assets_mkdir
    )
    uploads_mkdir = events.index(("mkdir", uploads.resolve()))
    uploads_publish = _event_after(
        events, ("fsync", assets.resolve()), uploads_mkdir
    )

    assert project_mkdir < project_publish < import_success
    assert assets_mkdir < assets_publish < import_success
    assert uploads_mkdir < uploads_publish < import_success

    history_mkdir = events.index(("mkdir", history.resolve()))
    history_publish = _event_after(
        events, ("fsync", assets.resolve()), history_mkdir
    )
    commits_mkdir = events.index(("mkdir", commits.resolve()))
    commits_publish = _event_after(
        events, ("fsync", assets.resolve()), commits_mkdir
    )
    assert import_success < history_mkdir < history_publish < save_success
    assert import_success < commits_mkdir < commits_publish < save_success


def _fail_directory_fsync_after_file_flush(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, bool]:
    """Inject EIO specifically at the post-file directory durability barrier."""

    real_fsync = contract.os.fsync
    state = {"file_flushed": False}

    def faulting_fsync(descriptor: int) -> None:
        mode = os.fstat(descriptor).st_mode
        if stat.S_ISDIR(mode) and state["file_flushed"]:
            raise OSError(errno.EIO, "injected directory fsync failure")
        real_fsync(descriptor)
        if stat.S_ISREG(mode):
            state["file_flushed"] = True

    monkeypatch.setattr(contract.os, "fsync", faulting_fsync)
    return state


@pytest.mark.skipif(os.name != "posix", reason="directory fsync is a POSIX contract")
def test_import_reports_directory_fsync_eio_and_never_returns_false_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "import-eio"
    state = _fail_directory_fsync_after_file_flush(monkeypatch)

    with pytest.raises(ReferenceContractError, match="fsync reference directory"):
        import_reference_sources(
            [_image_bytes((100, 20, 30))],
            project,
            kind="hero",
            max_sources=1,
        )

    assert state["file_flushed"] is True
    # replace(2) happened before the failed durability barrier, so the orphan
    # may be visible in this process. The API still failed and a retry under the
    # same storage fault must fail instead of accepting that uncertain entry.
    assert list((project / "_ASSETS" / "reference_uploads").glob("hero-*.jpg"))
    with pytest.raises(ReferenceContractError, match="fsync reference directory"):
        import_reference_sources(
            [_image_bytes((100, 20, 30))],
            project,
            kind="hero",
            max_sources=1,
        )


@pytest.mark.skipif(os.name != "posix", reason="directory fsync is a POSIX contract")
def test_state_save_reports_directory_fsync_eio_without_committing_pointer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "save-eio"
    hero = import_reference_sources(
        [_image_bytes((15, 120, 200))],
        project,
        kind="hero",
        max_sources=1,
    )[0]
    state = _fail_directory_fsync_after_file_flush(monkeypatch)

    with pytest.raises(ReferenceContractError, match="fsync reference directory"):
        save_project_reference_state(project, {"Captain": [hero.path]}, [])

    assert state["file_flushed"] is True
    assert current_project_reference_state(project)["revision_id"] is None
    assets = project / "_ASSETS"
    assert not (assets / contract.PROJECT_REFERENCE_STATE_POINTER_FILE).exists()
    assert not (assets / contract.PROJECT_REFERENCE_STATE_FILE).exists()
    with pytest.raises(ReferenceContractError, match="fsync reference directory"):
        save_project_reference_state(project, {"Captain": [hero.path]}, [])
