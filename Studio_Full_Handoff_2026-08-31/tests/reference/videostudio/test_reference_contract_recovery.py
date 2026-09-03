from __future__ import annotations

from io import BytesIO
from pathlib import Path
import sys

import pytest
from PIL import Image

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from reference_contract import (  # noqa: E402
    HERO_ANGLES_MAX,
    ReferenceContractError,
    current_project_reference_revision,
    import_reference_sources,
    list_project_reference_revisions,
    load_project_reference_state,
    restore_project_reference_revision,
    save_project_reference_state,
)


def _image_bytes(color: tuple[int, int, int]) -> bytes:
    output = BytesIO()
    Image.new("RGB", (24, 24), color).save(output, format="PNG")
    return output.getvalue()


@pytest.mark.parametrize("outside_exists", [False, True])
def test_valid_pointer_can_repair_linked_compatibility_projection(
    tmp_path: Path,
    outside_exists: bool,
) -> None:
    project = tmp_path / "project"
    hero = import_reference_sources(
        [_image_bytes((31, 41, 51))],
        project,
        kind="hero_source",
        max_sources=HERO_ANGLES_MAX,
    )[0]
    save_project_reference_state(project, {"Captain": [hero.path]}, [])
    captain_revision = current_project_reference_revision(project)
    assert captain_revision is not None
    save_project_reference_state(project, {}, [])

    assets = project / "_ASSETS"
    pointer = assets / "desktop_references.current.v1.json"
    projection = assets / "desktop_references.v1.json"
    outside = tmp_path / "outside-projection.json"
    outside_before = b"outside must remain unchanged"
    if outside_exists:
        outside.write_bytes(outside_before)
    assert pointer.is_file()
    projection.unlink()
    projection.symlink_to(outside)

    revisions = list_project_reference_revisions(project)
    captain_row = next(
        row for row in revisions if row["revision_id"] == captain_revision
    )
    assert captain_row["issues"] == ()
    assert "projection" in captain_row["current_state_issue"]

    with pytest.raises(ReferenceContractError, match="projection"):
        save_project_reference_state(project, {}, [])

    restore_project_reference_revision(
        project,
        captain_revision,
        expected_current_commit_id=(captain_row["current_commit_id"] or ""),
        expected_state_token=captain_row["current_state_token"],
        repair_damaged_current=True,
    )

    assert projection.is_file()
    assert not projection.is_symlink()
    if outside_exists:
        assert outside.read_bytes() == outside_before
    else:
        assert not outside.exists()
    manual, style, issues = load_project_reference_state(project)
    assert list(manual) == ["Captain"]
    assert style == []
    assert issues == ()
