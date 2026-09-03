from __future__ import annotations

from io import BytesIO
import ast
import hashlib
import json
from pathlib import Path
import sys
import unicodedata

import pytest
from PIL import Image

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from reference_contract import (  # noqa: E402
    HERO_ANGLES_MAX,
    HERO_GROUPS_MAX,
    MODEL_REFERENCE_CAPACITIES,
    STYLE_SOURCE_MAX,
    CanonicalImage,
    LogicalReferenceSlot,
    ManualHeroGroup,
    ReferenceArtifact,
    ReferenceContractError,
    build_contact_sheet,
    build_hero_contact_sheet,
    build_reference_bundle_manifest,
    canonical_fingerprint,
    canonical_json_bytes,
    canonicalize_reference_image,
    combine_fingerprints,
    current_project_reference_revision,
    current_project_reference_state,
    fingerprint_reference_artifacts,
    import_reference_sources,
    load_project_reference_state,
    list_project_reference_revisions,
    normalize_alias,
    pack_scene_references,
    persist_reference_artifact,
    reference_capacity,
    replace_named_reference_group,
    resolve_manual_hero_groups,
    restore_project_reference_revision,
    save_project_reference_state,
)


EXPECTED_CAPACITIES = {
    "OPENAI": 10,
    "FLOWER": 1,
    "GROK": 5,
    "GROK_SPEED": 5,
    "NARWHAL": 10,
    "NARWHAL_LITE": 10,
    "GEM_PIX_2": 10,
    "IMAGEN_3_5": 10,
    "VEO_BANANA": 5,
    "VEO_BANANA_2": 5,
    "VEO_BANANA_LITE": 5,
    "GPT_IMAGE2": 0,
}


def image_bytes(
    color: tuple[int, int, int],
    *,
    image_format: str = "PNG",
    size: tuple[int, int] = (48, 32),
) -> bytes:
    output = BytesIO()
    Image.new("RGB", size, color).save(output, format=image_format)
    return output.getvalue()


def fake_artifact(index: int, *, kind: str = "hero") -> ReferenceArtifact:
    sha256 = hashlib.sha256(f"artifact-{index}".encode()).hexdigest()
    fingerprint = canonical_fingerprint(
        {"kind": kind, "sha256": sha256, "index": index}
    )
    return ReferenceArtifact(
        path=f"/content-addressed/{kind}-{sha256}.jpg",
        sha256=sha256,
        width=64,
        height=64,
        source_hashes=(sha256,),
        kind=kind,
        fingerprint=fingerprint,
    )


def bible(*characters: dict) -> dict:
    return {"characters": list(characters), "locations": []}


def character(
    character_id: str,
    *names: str,
    role: str = "major",
) -> dict:
    return {
        "id": character_id,
        "names": list(names),
        "role": role,
        "appearance": "stable appearance",
    }


def test_capacity_registry_is_exact_live_inventory_and_unknown_fails_closed():
    assert dict(MODEL_REFERENCE_CAPACITIES) == EXPECTED_CAPACITIES
    assert reference_capacity(" narwhal_lite ") == 10
    assert STYLE_SOURCE_MAX == 5
    assert HERO_ANGLES_MAX == 6
    assert HERO_GROUPS_MAX == 6

    with pytest.raises(ReferenceContractError, match="not registered"):
        reference_capacity("future-provider")


def test_desktop_model_picker_covers_the_shared_reference_registry():
    tree = ast.parse((SRC_DIR / "app_main.py").read_text(encoding="utf-8"))
    model_rows = None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "GOOGLER_IMAGE_MODELS"
            for target in node.targets
        ):
            model_rows = ast.literal_eval(node.value)
            break
    assert model_rows is not None
    assert {row[0] for row in model_rows} == set(MODEL_REFERENCE_CAPACITIES)


def test_desktop_image_commands_use_immutable_bundle_and_explicit_regen_mode():
    source = (SRC_DIR / "app_main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    methods = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in {
            "_run_regen_image_worker", "_run_googler_images_worker",
            "_run_image_download_worker",
        }:
            methods[node.name] = ast.get_source_segment(source, node) or ""

    regen = methods["_run_regen_image_worker"]
    assert '"--reference_manifest", reference_manifest' in regen
    assert 'worker_args.append("--analyze_story")' in regen
    assert 'worker_args.append("--no_analyze_story")' in regen
    assert '"--user_refs"' not in regen
    assert '"--style_refs"' not in regen

    full = methods["_run_googler_images_worker"]
    assert full.count('"--reference_manifest", reference_manifest') == 3
    assert '"--user_refs"' not in full
    assert '"--style_refs"' not in full
    assert 'worker_args.append("--trust_legacy_cache")' in full

    bing = methods["_run_image_download_worker"]
    assert "self.user_references or self.style_references" in bing
    assert "self.auto_hero_var.get()" in bing
    assert "self.analyze_story_var.get()" in bing


def test_legacy_preset_without_reference_fields_preserves_project_state():
    source = (SRC_DIR / "app_main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    apply_source = ""
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "apply_settings":
            apply_source = ast.get_source_segment(source, node) or ""
            break
    assert "refs_fields_present" in apply_source
    assert "if refs_fields_present:" in apply_source
    assert "settings.get('auto_hero', False)" in apply_source


def test_alias_normalization_is_nfkc_casefold_punctuation_and_exact():
    nfc = "ЙОСЕФ"
    nfd = unicodedata.normalize("NFD", "йосеф")
    assert normalize_alias(f"  {nfc}  ") == normalize_alias(nfd)
    assert unicodedata.is_normalized("NFC", normalize_alias(nfd))
    assert normalize_alias("«Ｃａｐｔａｉｎ»") == normalize_alias("Captain")

    story = bible(
        character("anna", "Anna"),
        character("bob", "Bob"),
    )
    with pytest.raises(ReferenceContractError, match="no exact"):
        resolve_manual_hero_groups(
            [ManualHeroGroup("ann", ("Ann",))], story
        )


def test_manual_group_resolves_nfc_nfd_alias_and_preserves_input_order():
    nfc_name = "Élodie"
    nfd_name = unicodedata.normalize("NFD", nfc_name)
    story = bible(
        character("elodie", nfc_name, role="protagonist"),
        character("marta", "Marta"),
    )
    result = resolve_manual_hero_groups(
        [
            ManualHeroGroup("marta-photos", ("MARTA",)),
            ManualHeroGroup("elodie-photos", (nfd_name.upper(),)),
        ],
        story,
    )
    assert [(row.group_id, row.character_id, row.match_kind) for row in result] == [
        ("marta-photos", "marta", "exact"),
        ("elodie-photos", "elodie", "exact"),
    ]


def test_ambiguous_bible_alias_is_blocking():
    story = bible(
        character("alex-one", "Alex"),
        character("alex-two", "ALEX"),
    )
    with pytest.raises(ReferenceContractError, match="ambiguously"):
        resolve_manual_hero_groups(
            [ManualHeroGroup("alex-photos", ("Alex",))], story
        )


def test_duplicate_manual_alias_and_duplicate_character_resolution_are_blocking():
    story = bible(character("alice", "Alice", "Alicia"))
    with pytest.raises(ReferenceContractError, match="duplicated by groups"):
        resolve_manual_hero_groups(
            [
                ManualHeroGroup("first", ("Alice",)),
                ManualHeroGroup("second", ("ALICE",)),
            ],
            story,
        )

    with pytest.raises(ReferenceContractError, match="multiple manual groups"):
        resolve_manual_hero_groups(
            [
                ManualHeroGroup("first", ("Alice",)),
                ManualHeroGroup("second", ("Alicia",)),
            ],
            story,
        )


def test_bible_never_guesses_an_unmatched_group_from_character_role():
    story = bible(
        character("hero", "Canonical Hero", role="protagonist"),
        character("cameo", "Cameo", role="minor"),
    )
    with pytest.raises(ReferenceContractError, match="no exact"):
        resolve_manual_hero_groups(
            [ManualHeroGroup("uploaded-hero", ("Unlabelled portrait",))], story
        )

    # Even one total bible character is not authority to reinterpret a named
    # upload.  The UI must ask the user to use the canonical name.
    with pytest.raises(ReferenceContractError, match="canonical names"):
        resolve_manual_hero_groups(
            [ManualHeroGroup("uploaded-hero", ("Someone else",))],
            bible(character("hero", "Canonical Hero", role="protagonist")),
            single_subject=True,
        )

    ambiguous_story = bible(
        character("one", "One", role="protagonist"),
        character("two", "Two", role="major"),
    )
    with pytest.raises(ReferenceContractError, match="no exact"):
        resolve_manual_hero_groups(
            [ManualHeroGroup("uploaded-hero", ("Unlabelled portrait",))],
            ambiguous_story,
        )

    with pytest.raises(ReferenceContractError, match="no exact"):
        resolve_manual_hero_groups(
            [
                ManualHeroGroup("hero-one", ("Unknown one",)),
                ManualHeroGroup("hero-two", ("Unknown two",)),
            ],
            story,
        )


def test_without_bible_requires_explicit_single_subject_and_exactly_one_group():
    group = ManualHeroGroup("the-only-subject", ("The subject",))
    result = resolve_manual_hero_groups([group], None, single_subject=True)
    assert result[0].character_id is None
    assert result[0].match_kind == "explicit_single_subject"

    with pytest.raises(ReferenceContractError, match="single_subject=True"):
        resolve_manual_hero_groups([group], None, single_subject=False)
    with pytest.raises(ReferenceContractError, match="exactly one"):
        resolve_manual_hero_groups(
            [group, ManualHeroGroup("second", ("Second",))],
            None,
            single_subject=True,
        )


def test_desktop_named_group_update_is_atomic_and_collision_safe():
    original = {"Captain": ["front.jpg"], "Alice": ["alice.jpg"]}
    renamed = replace_named_reference_group(
        original,
        selected_name="Captain",
        new_name="Commander",
        sources=["front-new.jpg", "profile.jpg"],
    )
    assert original == {"Captain": ["front.jpg"], "Alice": ["alice.jpg"]}
    assert list(renamed) == ["Commander", "Alice"]
    assert renamed["Commander"] == ["front-new.jpg", "profile.jpg"]

    with pytest.raises(ReferenceContractError, match="conflicts"):
        replace_named_reference_group(
            original,
            selected_name="Captain",
            new_name="ＡＬＩＣＥ",
            sources=["replacement.jpg"],
        )
    assert original["Alice"] == ["alice.jpg"]


@pytest.mark.parametrize(
    ("image_format", "declared_format"),
    [("JPEG", "image/jpeg"), ("PNG", ".png"), ("WEBP", "webp")],
)
def test_allowed_images_are_fully_decoded_to_canonical_jpeg(
    image_format: str, declared_format: str
):
    canonical = canonicalize_reference_image(
        image_bytes((12, 34, 56), image_format=image_format),
        declared_format=declared_format,
    )
    assert canonical.jpeg_bytes.startswith(b"\xff\xd8")
    assert canonical.sha256 == hashlib.sha256(canonical.jpeg_bytes).hexdigest()
    assert canonical.width == 48
    assert canonical.height == 32
    with Image.open(BytesIO(canonical.jpeg_bytes)) as decoded:
        assert decoded.format == "JPEG"
        decoded.load()


def test_corrupt_spoofed_disallowed_and_declared_mismatch_are_blocking():
    with pytest.raises(ReferenceContractError, match="spoofed"):
        canonicalize_reference_image(b"\xff\xd8\xff\xe0this-is-not-a-jpeg")

    gif = image_bytes((1, 2, 3), image_format="GIF")
    with pytest.raises(ReferenceContractError, match="JPEG, PNG, or WEBP"):
        canonicalize_reference_image(gif)

    png = image_bytes((1, 2, 3), image_format="PNG")
    with pytest.raises(ReferenceContractError, match="actually decodes as PNG"):
        canonicalize_reference_image(png, declared_format="image/jpeg")


def test_upload_byte_pixel_and_dimension_limits_are_enforced_before_acceptance():
    payload = image_bytes((10, 20, 30), size=(10, 8))
    with pytest.raises(ReferenceContractError, match="byte upload limit"):
        canonicalize_reference_image(payload, max_bytes=len(payload) - 1)
    with pytest.raises(ReferenceContractError, match="pixel limit"):
        canonicalize_reference_image(payload, max_pixels=79)
    with pytest.raises(ReferenceContractError, match="dimension exceeds"):
        canonicalize_reference_image(payload, max_dimension=9)


def test_transparent_png_is_canonicalized_against_white():
    output = BytesIO()
    transparent = Image.new("RGBA", (8, 8), (255, 0, 0, 0))
    transparent.save(output, format="PNG")
    canonical = canonicalize_reference_image(output.getvalue())
    with Image.open(BytesIO(canonical.jpeg_bytes)) as decoded:
        pixel = decoded.convert("RGB").getpixel((4, 4))
    assert all(channel >= 250 for channel in pixel)


def test_one_hero_angle_stays_one_content_addressed_canonical_artifact(
    tmp_path: Path,
):
    source = image_bytes((200, 10, 20), image_format="PNG")
    canonical = canonicalize_reference_image(source)
    artifact = build_hero_contact_sheet([source], tmp_path)
    assert artifact.kind == "hero"
    assert artifact.sha256 == canonical.sha256
    assert artifact.source_hashes == (canonical.source_sha256,)
    assert Path(artifact.path).read_bytes() == canonical.jpeg_bytes
    assert artifact.sha256 in Path(artifact.path).name


def test_contact_sheet_is_deterministic_across_names_and_input_order_and_atomic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    red = image_bytes((200, 0, 0), size=(80, 40))
    blue = image_bytes((0, 0, 200), size=(40, 80))
    first_red = tmp_path / "angle-z.png"
    first_blue = tmp_path / "angle-a.png"
    first_red.write_bytes(red)
    first_blue.write_bytes(blue)

    replace_calls: list[tuple[str, str]] = []
    import reference_contract as contract

    real_replace = contract.os.replace

    def tracked_replace(source, target):
        replace_calls.append((str(source), str(target)))
        return real_replace(source, target)

    monkeypatch.setattr(contract.os, "replace", tracked_replace)
    artifact_one = build_contact_sheet(
        [first_red, first_blue], tmp_path / "one", kind="hero", cell_size=64
    )

    renamed_red = tmp_path / "totally-different-name.webp"
    renamed_blue = tmp_path / "other-name.jpg"
    renamed_red.write_bytes(red)
    renamed_blue.write_bytes(blue)
    artifact_two = build_contact_sheet(
        [renamed_blue, renamed_red], tmp_path / "two", kind="hero", cell_size=64
    )

    assert artifact_one.sha256 == artifact_two.sha256
    assert artifact_one.source_hashes == artifact_two.source_hashes
    assert Path(artifact_one.path).read_bytes() == Path(artifact_two.path).read_bytes()
    assert replace_calls
    assert all(Path(target).suffix == ".jpg" for _source, target in replace_calls)
    assert not list(tmp_path.rglob("*.tmp"))


def test_contact_sheet_hash_changes_with_source_pixels(tmp_path: Path):
    common = image_bytes((10, 20, 30))
    first = build_contact_sheet(
        [common, image_bytes((40, 50, 60))],
        tmp_path / "first",
        kind="hero",
        cell_size=48,
    )
    changed = build_contact_sheet(
        [common, image_bytes((140, 150, 161))],
        tmp_path / "changed",
        kind="hero",
        cell_size=48,
    )
    assert first.sha256 != changed.sha256
    assert first.fingerprint != changed.fingerprint


def test_style_five_sources_become_one_moodboard_slot_with_source_audit(
    tmp_path: Path,
):
    sources = [image_bytes((index * 30, 10, 20)) for index in range(1, 6)]
    moodboard = build_contact_sheet(
        sources, tmp_path, kind="style", cell_size=48
    )
    assert moodboard.kind == "style-moodboard"
    assert len(moodboard.source_hashes) == STYLE_SOURCE_MAX

    packed = pack_scene_references(
        [LogicalReferenceSlot("style-moodboard", "style", moodboard)],
        "FLOWER",
    )
    assert len(packed.selected) == 1
    assert len(packed.provider_artifacts) == 1
    assert [row.status for row in packed.audit].count("selected") == 1
    source_audit = [row for row in packed.audit if row.kind == "style_source"]
    assert len(source_audit) == STYLE_SOURCE_MAX
    assert {row.status for row in source_audit} == {"superseded"}
    assert {row.superseded_by for row in source_audit} == {"style-moodboard"}
    assert {row.artifact_sha256 for row in source_audit} == set(
        moodboard.source_hashes
    )


def test_contact_sheet_source_limits_and_duplicates_fail_closed(tmp_path: Path):
    style_sources = [image_bytes((index, 2, 3)) for index in range(6)]
    with pytest.raises(ReferenceContractError, match="1..5"):
        build_contact_sheet(style_sources, tmp_path, kind="style")

    hero_sources = [image_bytes((index, 4, 5)) for index in range(7)]
    with pytest.raises(ReferenceContractError, match="1..6"):
        build_contact_sheet(hero_sources, tmp_path, kind="hero")

    duplicate = image_bytes((1, 1, 1))
    with pytest.raises(ReferenceContractError, match="duplicate source"):
        build_contact_sheet([duplicate, duplicate], tmp_path, kind="hero")


def test_desktop_import_owns_bytes_and_survives_external_source_deletion(
    tmp_path: Path,
):
    project = tmp_path / "project"
    external = tmp_path / "camera-card" / "captain.png"
    external.parent.mkdir()
    external.write_bytes(image_bytes((44, 55, 66), image_format="PNG"))

    imported = import_reference_sources(
        [external], project, kind="hero_source", max_sources=HERO_ANGLES_MAX
    )
    managed_path = Path(imported[0].path)
    assert managed_path.is_relative_to(project / "_ASSETS" / "reference_uploads")
    assert managed_path.suffix == ".jpg"
    save_project_reference_state(project, {"Captain": [managed_path]}, [])

    external.unlink()
    manual, style, issues = load_project_reference_state(project)
    assert style == []
    assert issues == ()
    assert manual == {"Captain": [str(managed_path.resolve())]}
    assert managed_path.is_file()


def test_project_owned_reference_clone_is_byte_exact_and_idempotent(tmp_path: Path):
    external = tmp_path / "external.png"
    external.write_bytes(image_bytes((145, 33, 201), image_format="PNG"))
    first_project = tmp_path / "first"
    first = import_reference_sources(
        [external], first_project, kind="hero_source", max_sources=HERO_ANGLES_MAX
    )[0]
    save_project_reference_state(first_project, {"Captain": [first.path]}, [])

    second_project = tmp_path / "second"
    second = import_reference_sources(
        [first.path], second_project, kind="hero_source", max_sources=HERO_ANGLES_MAX
    )[0]
    save_project_reference_state(second_project, {"Captain": [second.path]}, [])
    third_project = tmp_path / "third"
    third = import_reference_sources(
        [second.path], third_project, kind="hero_source", max_sources=HERO_ANGLES_MAX
    )[0]

    assert first.sha256 == second.sha256 == third.sha256
    assert Path(first.path).read_bytes() == Path(second.path).read_bytes()
    assert Path(second.path).read_bytes() == Path(third.path).read_bytes()


def test_desktop_import_validates_whole_batch_before_persisting(tmp_path: Path):
    project = tmp_path / "project"
    good = tmp_path / "good.png"
    bad = tmp_path / "bad.png"
    good.write_bytes(image_bytes((1, 2, 3)))
    bad.write_bytes(b"not an image")

    with pytest.raises(ReferenceContractError, match="corrupt|spoofed"):
        import_reference_sources(
            [good, bad], project, kind="style_source", max_sources=STYLE_SOURCE_MAX
        )
    assert not list(project.rglob("*.jpg"))


def test_desktop_manifest_keeps_missing_rows_visible_and_rejects_shared_identity(
    tmp_path: Path,
):
    project = tmp_path / "project"
    first = import_reference_sources(
        [image_bytes((10, 20, 30))],
        project,
        kind="hero_source",
        max_sources=HERO_ANGLES_MAX,
    )[0]
    second = import_reference_sources(
        [image_bytes((40, 50, 60))],
        project,
        kind="hero_source",
        max_sources=HERO_ANGLES_MAX,
    )[0]
    manifest = save_project_reference_state(
        project, {"Captain": [first.path], "Alice": [second.path]}, []
    )
    original_manifest = manifest.read_bytes()

    with pytest.raises(ReferenceContractError, match="assigned to both"):
        save_project_reference_state(
            project, {"Captain": [first.path], "Alice": [first.path]}, []
        )
    assert manifest.read_bytes() == original_manifest

    Path(first.path).unlink()
    manual, _style, issues = load_project_reference_state(project)
    assert "Captain" in manual and len(manual["Captain"]) == 1
    assert any("Captain" in issue and "missing" in issue for issue in issues)


def test_desktop_state_save_rejects_noncanonical_content_addressed_name(
    tmp_path: Path,
):
    project = tmp_path / "project"
    artifact = import_reference_sources(
        [image_bytes((63, 73, 83))],
        project,
        kind="hero_source",
        max_sources=HERO_ANGLES_MAX,
    )[0]
    source = Path(artifact.path)
    invalid_name = source.with_name(f"{source.stem}-junk.jpg")
    invalid_name.write_bytes(source.read_bytes())

    with pytest.raises(ReferenceContractError, match="content-addressed name"):
        save_project_reference_state(project, {"Captain": [invalid_name]}, [])


def test_desktop_manifest_revisions_are_recoverable(tmp_path: Path):
    project = tmp_path / "project"
    first = import_reference_sources(
        [image_bytes((70, 80, 90))],
        project,
        kind="hero_source",
        max_sources=HERO_ANGLES_MAX,
    )[0]
    manifest = save_project_reference_state(project, {"Captain": [first.path]}, [])
    first_revision = manifest.read_bytes()

    save_project_reference_state(project, {}, [])
    history = list((project / "_ASSETS" / "reference_state_history").glob("*.json"))
    assert len(history) == 2
    first_revision_id = hashlib.sha256(first_revision).hexdigest()
    first_history = (
        project
        / "_ASSETS"
        / "reference_state_history"
        / f"{first_revision_id}.json"
    )
    assert first_history.read_bytes() == first_revision

    revisions = list_project_reference_revisions(project)
    assert len(revisions) == 2
    assert sum(bool(row["is_current"]) for row in revisions) == 1
    assert {row["manual_count"] for row in revisions} == {0, 1}

    restore_project_reference_revision(project, first_revision_id)
    manual, style, issues = load_project_reference_state(project)
    assert list(manual) == ["Captain"]
    assert style == []
    assert issues == ()
    assert current_project_reference_revision(project) == first_revision_id


def test_desktop_manifest_compare_and_swap_rejects_lost_update(tmp_path: Path):
    project = tmp_path / "project"
    first = import_reference_sources(
        [image_bytes((12, 34, 56))],
        project,
        kind="hero_source",
        max_sources=HERO_ANGLES_MAX,
    )[0]
    save_project_reference_state(project, {"Captain": [first.path]}, [])
    loaded_revision = current_project_reference_revision(project)
    assert loaded_revision is not None

    save_project_reference_state(
        project,
        {},
        [],
        expected_revision=loaded_revision,
    )
    with pytest.raises(ReferenceContractError, match="another app instance"):
        save_project_reference_state(
            project,
            {"Captain": [first.path]},
            [],
            expected_revision=loaded_revision,
        )


def test_desktop_commit_cas_rejects_aba_and_journal_preserves_order(tmp_path: Path):
    project = tmp_path / "project"
    captain = import_reference_sources(
        [image_bytes((21, 31, 41))],
        project,
        kind="hero_source",
        max_sources=HERO_ANGLES_MAX,
    )[0]
    alice = import_reference_sources(
        [image_bytes((51, 61, 71))],
        project,
        kind="hero_source",
        max_sources=HERO_ANGLES_MAX,
    )[0]

    save_project_reference_state(project, {"Captain": [captain.path]}, [])
    stale_a = current_project_reference_state(project)
    save_project_reference_state(
        project,
        {"Alice": [alice.path]},
        [],
        expected_revision=stale_a["revision_id"],
        expected_commit_id=stale_a["commit_id"],
    )
    state_b = current_project_reference_state(project)
    save_project_reference_state(
        project,
        {"Captain": [captain.path]},
        [],
        expected_revision=state_b["revision_id"],
        expected_commit_id=state_b["commit_id"],
    )
    with pytest.raises(ReferenceContractError, match="commit changed"):
        save_project_reference_state(
            project,
            {},
            [],
            expected_revision=stale_a["revision_id"],
            expected_commit_id=stale_a["commit_id"],
        )

    state_a_again = current_project_reference_state(project)
    save_project_reference_state(
        project,
        {},
        [],
        expected_revision=state_a_again["revision_id"],
        expected_commit_id=state_a_again["commit_id"],
    )
    rows = list_project_reference_revisions(project)
    assert [row["sequence"] for row in rows] == [4, 3, 2, 1]
    assert [row["manual_count"] for row in rows] == [0, 1, 1, 1]
    assert rows[1]["revision_id"] == rows[3]["revision_id"]
    assert rows[1]["commit_id"] != rows[3]["commit_id"]

    previous = rows[1]
    restore_project_reference_revision(
        project,
        previous["revision_id"],
        expected_current_commit_id=previous["current_commit_id"],
        expected_state_token=previous["current_state_token"],
    )
    manual, _style, issues = load_project_reference_state(project)
    assert list(manual) == ["Captain"]
    assert issues == ()


def test_desktop_restore_cas_rejects_change_after_history_dialog(tmp_path: Path):
    project = tmp_path / "project"
    hero = import_reference_sources(
        [image_bytes((81, 82, 83))],
        project,
        kind="hero_source",
        max_sources=HERO_ANGLES_MAX,
    )[0]
    save_project_reference_state(project, {"Captain": [hero.path]}, [])
    save_project_reference_state(project, {}, [])
    selected = list_project_reference_revisions(project)[1]

    current = current_project_reference_state(project)
    save_project_reference_state(
        project,
        {"Captain": [hero.path]},
        [],
        expected_revision=current["revision_id"],
        expected_commit_id=current["commit_id"],
    )
    with pytest.raises(ReferenceContractError, match="changed after restore"):
        restore_project_reference_revision(
            project,
            selected["revision_id"],
            expected_current_commit_id=selected["current_commit_id"],
            expected_state_token=selected["current_state_token"],
        )

    manual, _style, issues = load_project_reference_state(project)
    assert list(manual) == ["Captain"]
    assert issues == ()


def test_desktop_manifest_pointer_uses_verified_immutable_revision(tmp_path: Path):
    project = tmp_path / "project"
    first = import_reference_sources(
        [image_bytes((93, 94, 95))],
        project,
        kind="hero_source",
        max_sources=HERO_ANGLES_MAX,
    )[0]
    manifest = save_project_reference_state(project, {"Captain": [first.path]}, [])
    manifest.write_bytes(b"corrupt compatibility projection")

    manual, style, issues = load_project_reference_state(project)
    assert list(manual) == ["Captain"]
    assert style == []
    assert issues == ()

    revision_id = current_project_reference_revision(project)
    assert revision_id is not None
    revision = project / "_ASSETS" / "reference_state_history" / f"{revision_id}.json"
    revision.write_bytes(revision.read_bytes() + b"tampered")
    with pytest.raises(ReferenceContractError, match="hash changed"):
        load_project_reference_state(project)


@pytest.mark.parametrize(
    "damage",
    ["pointer", "current_revision", "pointer_symlink", "projection_symlink"],
)
def test_desktop_revision_restore_repairs_damaged_commit_marker(
    tmp_path: Path,
    damage: str,
):
    project = tmp_path / "project"
    first = import_reference_sources(
        [image_bytes((141, 142, 143))],
        project,
        kind="hero_source",
        max_sources=HERO_ANGLES_MAX,
    )[0]
    save_project_reference_state(project, {"Captain": [first.path]}, [])
    captain_revision = current_project_reference_revision(project)
    assert captain_revision is not None
    save_project_reference_state(project, {}, [])

    assets = project / "_ASSETS"
    if damage == "pointer":
        (assets / "desktop_references.current.v1.json").write_bytes(b"broken")
    elif damage == "pointer_symlink":
        pointer = assets / "desktop_references.current.v1.json"
        pointer.unlink()
        pointer.symlink_to(tmp_path / "missing-pointer-target.json")
    elif damage == "projection_symlink":
        pointer = assets / "desktop_references.current.v1.json"
        pointer.unlink()
        projection = assets / "desktop_references.v1.json"
        projection.unlink()
        projection.symlink_to(tmp_path / "missing-projection-target.json")
    else:
        current = current_project_reference_revision(project)
        assert current is not None
        (assets / "reference_state_history" / f"{current}.json").write_bytes(
            b"broken"
        )

    revisions = list_project_reference_revisions(project)
    captain_row = next(
        row for row in revisions if row["revision_id"] == captain_revision
    )
    assert captain_row["issues"] == ()
    assert captain_row["current_state_issue"]

    restore_project_reference_revision(
        project,
        captain_revision,
        expected_current_commit_id=(captain_row["current_commit_id"] or ""),
        expected_state_token=captain_row["current_state_token"],
        repair_damaged_current=True,
    )
    manual, style, issues = load_project_reference_state(project)
    assert list(manual) == ["Captain"]
    assert style == []
    assert issues == ()


def test_empty_desktop_state_rejects_assets_symlink_escape(tmp_path: Path):
    project = tmp_path / "project"
    outside = tmp_path / "outside-assets"
    project.mkdir()
    outside.mkdir()
    (project / "_ASSETS").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ReferenceContractError, match="symlink"):
        save_project_reference_state(project, {}, [])
    assert not list(outside.iterdir())


def test_desktop_worker_bundle_is_immutable_structured_and_supports_empty_set(
    tmp_path: Path,
):
    project = tmp_path / "project"
    hero = import_reference_sources(
        [image_bytes((101, 102, 103)), image_bytes((111, 112, 113))],
        project,
        kind="hero_source",
        max_sources=HERO_ANGLES_MAX,
    )
    style = import_reference_sources(
        [image_bytes((121, 122, 123))],
        project,
        kind="style_source",
        max_sources=STYLE_SOURCE_MAX,
    )
    manifest = build_reference_bundle_manifest(
        project,
        {"Captain: | literal name": [item.path for item in hero]},
        [style[0].path],
    )
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert set(payload) == {
        "schema_version", "bundle_id", "characters", "style", "source_fingerprint"
    }
    assert payload["characters"][0]["name"] == "Captain: | literal name"
    assert payload["characters"][0]["files"] == sorted(
        payload["characters"][0]["files"]
    )
    assert payload["style"] == sorted(payload["style"])
    assert manifest.parent.name == payload["bundle_id"]

    for item in (*hero, *style):
        Path(item.path).unlink()
    assert all((manifest.parent / rel).is_file() for rel in (
        payload["characters"][0]["files"] + payload["style"]
    ))

    empty = build_reference_bundle_manifest(project, {}, [])
    empty_payload = json.loads(empty.read_text(encoding="utf-8"))
    assert empty_payload["characters"] == []
    assert empty_payload["style"] == []


def test_desktop_managed_state_rejects_upload_directory_symlink(tmp_path: Path):
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    outside.mkdir()
    (project / "_ASSETS").mkdir(parents=True)
    (project / "_ASSETS" / "reference_uploads").symlink_to(
        outside, target_is_directory=True
    )
    with pytest.raises(ReferenceContractError, match="symlink"):
        import_reference_sources(
            [image_bytes((8, 9, 10))],
            project,
            kind="hero_source",
            max_sources=HERO_ANGLES_MAX,
        )


def test_desktop_worker_bundle_rejects_bundle_directory_symlink(tmp_path: Path):
    project = tmp_path / "project"
    artifact = import_reference_sources(
        [image_bytes((18, 19, 20))],
        project,
        kind="hero_source",
        max_sources=HERO_ANGLES_MAX,
    )[0]
    outside = tmp_path / "outside-bundles"
    outside.mkdir()
    bundles = project / "_ASSETS" / "reference_bundles"
    bundles.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ReferenceContractError, match="symlink"):
        build_reference_bundle_manifest(
            project, {"Captain": [artifact.path]}, []
        )


def test_packer_reports_selected_not_applicable_and_explicit_supersession():
    manual = fake_artifact(1, kind="hero")
    auto = fake_artifact(2, kind="auto_hero")
    slots = [
        LogicalReferenceSlot("manual", "hero", manual),
        LogicalReferenceSlot("auto", "auto_hero", auto, superseded_by="manual"),
        LogicalReferenceSlot("location", "location", applicable=False),
    ]
    packed = pack_scene_references(slots, "OPENAI")
    assert [slot.slot_id for slot in packed.selected] == ["manual"]
    assert {row.slot_id: row.status for row in packed.audit} == {
        "manual": "selected",
        "auto": "superseded",
        "location": "not_applicable",
    }
    assert packed.audit[1].superseded_by == "manual"
    assert {row.status for row in packed.audit}.issubset(
        {"selected", "not_applicable", "superseded", "blocking"}
    )


def test_packer_overflow_is_blocking_before_provider_with_no_dropped_status():
    slots = [
        LogicalReferenceSlot(f"slot-{index}", "character", fake_artifact(index))
        for index in range(11)
    ]
    with pytest.raises(ReferenceContractError, match="accepts 10") as caught:
        pack_scene_references(slots, "OPENAI")

    assert len(caught.value.audit) == 11
    assert {row.status for row in caught.value.audit} == {"blocking"}
    serialized = repr(caught.value.audit).casefold()
    assert "dropped" not in serialized
    assert "unknown" not in serialized


def test_flower_blocks_mixed_hero_and_style_but_accepts_no_refs_for_gpt_image2():
    hero = LogicalReferenceSlot("hero", "hero", fake_artifact(1, kind="hero"))
    style = LogicalReferenceSlot("style", "style", fake_artifact(2, kind="style"))
    with pytest.raises(ReferenceContractError, match="accepts 1") as caught:
        pack_scene_references([hero, style], "FLOWER")
    assert {row.status for row in caught.value.audit} == {"blocking"}

    empty = pack_scene_references([], "GPT_IMAGE2")
    assert empty.capacity == 0
    assert empty.selected == ()


def test_missing_artifact_duplicate_content_and_unconsolidated_style_are_blocking():
    with pytest.raises(ReferenceContractError, match="has no artifact") as missing:
        pack_scene_references(
            [LogicalReferenceSlot("required-hero", "hero")], "OPENAI"
        )
    assert missing.value.audit[0].status == "blocking"

    repeated = fake_artifact(4)
    with pytest.raises(ReferenceContractError, match="same artifact"):
        pack_scene_references(
            [
                LogicalReferenceSlot("first", "hero", repeated),
                LogicalReferenceSlot("second", "style", repeated),
            ],
            "OPENAI",
        )

    with pytest.raises(ReferenceContractError, match="one moodboard slot"):
        pack_scene_references(
            [
                LogicalReferenceSlot("style-one", "style", fake_artifact(5)),
                LogicalReferenceSlot("style-two", "style", fake_artifact(6)),
            ],
            "OPENAI",
        )


def test_canonical_fingerprints_are_nfc_key_order_stable_and_path_independent(
    tmp_path: Path,
):
    nfc = "café"
    nfd = unicodedata.normalize("NFD", nfc)
    assert canonical_json_bytes({"b": 2, "a": nfc}) == canonical_json_bytes(
        {"a": nfd, "b": 2}
    )
    first = canonical_fingerprint({"b": 2, "a": nfc})
    second = canonical_fingerprint({"a": nfd, "b": 2})
    assert first == second
    assert combine_fingerprints(first, second) == combine_fingerprints(first, second)

    artifact = persist_reference_artifact(
        image_bytes((11, 22, 33)), tmp_path / "one", kind="style"
    )
    moved = ReferenceArtifact(
        path="/a/different/local/name.jpg",
        sha256=artifact.sha256,
        width=artifact.width,
        height=artifact.height,
        source_hashes=artifact.source_hashes,
        kind=artifact.kind,
        fingerprint=artifact.fingerprint,
    )
    assert fingerprint_reference_artifacts([artifact]) == fingerprint_reference_artifacts(
        [moved]
    )

    with pytest.raises(ReferenceContractError, match="NaN/Infinity"):
        canonical_fingerprint({"unsafe": float("nan")})


def test_canonical_image_object_is_hash_checked_before_persist(tmp_path: Path):
    valid = canonicalize_reference_image(image_bytes((9, 8, 7)))
    spoofed = CanonicalImage(
        jpeg_bytes=valid.jpeg_bytes + b"tampered",
        sha256=valid.sha256,
        source_sha256=valid.source_sha256,
        width=valid.width,
        height=valid.height,
        source_format=valid.source_format,
    )
    with pytest.raises(ReferenceContractError, match="hash is invalid"):
        persist_reference_artifact(spoofed, tmp_path)


def _load_desktop_method(method_name: str, **global_values):
    """Compile one ProjectFrame method without importing the Tk application."""
    source = (SRC_DIR / "app_main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    method = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == method_name
    )
    module = ast.Module(body=[method], type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = dict(global_values)
    exec(compile(module, str(SRC_DIR / "app_main.py"), "exec"), namespace)
    return namespace[method_name]


def test_desktop_cross_project_reuse_is_purely_safe_only_without_continuity_refs(
    tmp_path: Path,
):
    class Value:
        def __init__(self, value):
            self.value = value

        def get(self):
            return self.value

    class Frame:
        pass

    def frame(project: Path):
        result = Frame()
        result.project_dir = str(project)
        result._reference_copy_blocker = None
        result._reference_state_issues = ()
        result.user_references = {}
        result.style_references = []
        result.auto_hero_var = Value(False)
        result.analyze_story_var = Value(False)
        result.googler_style = Value("same style")
        result.googler_model = Value("OPENAI")
        result.googler_aspect_ratio = Value("landscape")
        result.image_prompt_mode = Value("gpt")
        return result

    status = _load_desktop_method(
        "_image_reference_reuse_status",
        load_project_reference_state=lambda _project: ({}, [], ()),
        ReferenceContractError=ReferenceContractError,
    )
    safe_method = _load_desktop_method("_image_reference_reuse_safe")
    preset_key_from_values = _load_desktop_method(
        "_image_preset_key_from_values", os=__import__("os")
    )
    preset_key = _load_desktop_method(
        "_image_preset_key",
        os=__import__("os"),
        ReferenceContractError=ReferenceContractError,
    )

    clean_a = frame(tmp_path / "clean-a")
    clean_b = frame(tmp_path / "clean-b")
    clean_a._image_reference_reuse_status = lambda: status(clean_a)
    clean_b._image_reference_reuse_status = lambda: status(clean_b)
    clean_a._image_reference_reuse_safe = lambda: safe_method(clean_a)
    clean_b._image_reference_reuse_safe = lambda: safe_method(clean_b)
    clean_a._image_preset_key_from_values = (
        lambda **values: preset_key_from_values(clean_a, **values)
    )
    clean_b._image_preset_key_from_values = (
        lambda **values: preset_key_from_values(clean_b, **values)
    )
    assert clean_a._image_reference_reuse_safe() is True
    assert clean_b._image_reference_reuse_safe() is True
    assert preset_key(clean_a) == preset_key(clean_b)

    referenced_a = frame(tmp_path / "referenced-a")
    referenced_b = frame(tmp_path / "referenced-b")
    referenced_a.user_references = {"Captain": ["front.jpg"]}
    referenced_b.style_references = ["look.jpg"]
    referenced_a._image_reference_reuse_status = lambda: status(referenced_a)
    referenced_b._image_reference_reuse_status = lambda: status(referenced_b)
    referenced_a._image_reference_reuse_safe = lambda: safe_method(referenced_a)
    referenced_b._image_reference_reuse_safe = lambda: safe_method(referenced_b)
    referenced_a._image_preset_key_from_values = (
        lambda **values: preset_key_from_values(referenced_a, **values)
    )
    referenced_b._image_preset_key_from_values = (
        lambda **values: preset_key_from_values(referenced_b, **values)
    )
    assert referenced_a._image_reference_reuse_safe() is False
    assert referenced_b._image_reference_reuse_safe() is False
    # Unsafe projects receive project-unique preset identities, so different
    # reference contracts cannot become grouping/copy candidates by equality.
    assert preset_key(referenced_a) != preset_key(referenced_b)

    auto = frame(tmp_path / "auto")
    auto.auto_hero_var = Value(True)
    assert status(auto)[0] is False
    analyzed = frame(tmp_path / "analyzed")
    analyzed.analyze_story_var = Value(True)
    assert status(analyzed)[0] is False

    disk_status = _load_desktop_method(
        "_image_reference_reuse_status",
        load_project_reference_state=lambda _project: (
            {"Captain": ["owned.jpg"]}, [], ()
        ),
        ReferenceContractError=ReferenceContractError,
    )
    assert disk_status(frame(tmp_path / "disk-reference"))[0] is False


def test_desktop_cross_project_image_reuse_is_a_hard_disabled_contract():
    source = (SRC_DIR / "app_main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    methods = {
        node.name: ast.get_source_segment(source, node) or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name in {
            "_apply_cross_project_image_reuse_setting",
            "_copy_googler_images_to_similar_projects",
            "_finalize_googler_images_success",
            "run_googler_images_for_all",
        }
    }

    invariant = next(
        node for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name)
            and target.id == "CROSS_PROJECT_IMAGE_REUSE_ENABLED"
            for target in node.targets
        )
    )
    assert isinstance(invariant.value, ast.Constant)
    assert invariant.value.value is False

    copier = methods["_copy_googler_images_to_similar_projects"]
    assert "BLOCKED" in copier
    assert copier.index("return 0") < copier.index(
        "if not isinstance(run_snapshot, ImageGenerationSnapshot):"
    )

    batch = methods["run_googler_images_for_all"]
    assert "for name, frame in items:" in batch
    assert "if _queue(name, frame):" in batch
    for removed_grouping in (
        "same_text", "covered", "reuse_status", "target_status",
        "_project_name_similarity", "_image_preset_key",
    ):
        assert removed_grouping not in batch

    finalizer = methods["_finalize_googler_images_success"]
    assert "_copy_googler_images_to_similar_projects" not in finalizer

    migration = methods["_apply_cross_project_image_reuse_setting"]
    assert "self.googler_copy_similar.set(False)" in migration
    assert "_cross_project_reuse_notice_logged" in migration


def test_desktop_old_reuse_preset_is_coerced_false_and_logged_once():
    class Value:
        def __init__(self, value):
            self.value = value

        def get(self):
            return self.value

        def set(self, value):
            self.value = value

    class Frame:
        googler_copy_similar = Value(True)
        _cross_project_reuse_notice_logged = False

        def __init__(self):
            self.logs = []

        def log(self, message, **_kwargs):
            self.logs.append(message)

    migrate = _load_desktop_method(
        "_apply_cross_project_image_reuse_setting",
        CROSS_PROJECT_IMAGE_REUSE_BLOCK_REASON="project-owned passports",
    )
    frame = Frame()
    assert migrate(frame, True) is False
    assert frame.googler_copy_similar.get() is False
    assert len(frame.logs) == 1
    migrate(frame, True)
    assert len(frame.logs) == 1

    source = (SRC_DIR / "app_main.py").read_text(encoding="utf-8")
    assert "self.googler_copy_similar = BooleanVar(value=False)" in source
    assert 'state="disabled"' in source
    assert "отключено: разные паспорта кадров" in source


def test_desktop_reuse_target_requires_empty_idle_googler(tmp_path: Path):
    target_status = _load_desktop_method(
        "_image_reuse_target_status", os=__import__("os")
    )

    class Frame:
        project_name = "target"
        googler_queue = None
        thief_queue = None
        googler_proc_handle = None
        bing_proc_handle = None
        thief_proc_handle = None
        _googler_images_busy = False
        _images_download_busy = False

        @staticmethod
        def _image_reference_reuse_status():
            return True, "clean"

    frame = Frame()
    frame.project_dir = str(tmp_path / "target")
    assert target_status(frame)[0] is True

    googler_dir = Path(frame.project_dir) / "googler"
    googler_dir.mkdir(parents=True)
    (googler_dir / "006.jpg").write_bytes(b"stale output")
    safe, reason = target_status(frame)
    assert safe is False
    assert "006.jpg" in reason

    (googler_dir / "006.jpg").unlink()
    frame._googler_images_busy = True
    safe, reason = target_status(frame)
    assert safe is False
    assert "мутация" in reason


def test_desktop_batch_queues_every_project_even_with_legacy_reuse_true(
    tmp_path: Path,
):
    class Value:
        def __init__(self, value):
            self.value = value

        def get(self):
            return self.value

        def set(self, value):
            self.value = value

    class Queue:
        def __init__(self):
            self.names = []

        def add_to_queue(self, name, _callback):
            self.names.append(name)
            return True, "queued"

        @staticmethod
        def get_status():
            return {"active": 0, "queued": 0, "max": 1}

    class Frame:
        def __init__(self, name, source_path, target_empty):
            self.project_name = name
            self.project_dir = str(tmp_path / name)
            self.source_text_file = Value(str(source_path))
            self.image_prompt_mode = Value("gpt")
            self.googler_copy_similar = Value(True)
            self._target_empty = target_empty
            self.logs = []

        def _image_reference_reuse_status(self):
            return True, "clean"

        def _image_reuse_target_status(self):
            return (
                (True, "empty") if self._target_empty
                else (False, "целевая папка уже содержит 006.jpg")
            )

        @staticmethod
        def _content_key():
            return "story"

        @staticmethod
        def _image_preset_key():
            return ("same-preset",)

        @staticmethod
        def _prepare_legacy_cache_consent():
            return True

        @staticmethod
        def _run_googler_images_worker():
            return None

        def log(self, message, **_kwargs):
            self.logs.append(message)

    source_text = tmp_path / "source.txt"
    target_text = tmp_path / "target.txt"
    source_text.write_text("story", encoding="utf-8")
    target_text.write_text("story", encoding="utf-8")
    run_all = _load_desktop_method(
        "run_googler_images_for_all",
        os=__import__("os"),
        re=__import__("re"),
        _project_name_similarity=lambda *_args: 1.0,
        CROSS_PROJECT_IMAGE_REUSE_BLOCK_REASON="project-owned passports",
    )

    def queued_names(target_empty):
        source = Frame("source", source_text, False)
        target = Frame("target", target_text, target_empty)
        app = type("App", (), {})()
        app.projects = {"source": source, "target": target}
        app.googler_queue = Queue()
        app._app_log = lambda *_args, **_kwargs: None
        run_all(app)
        return app.googler_queue.names

    # Even an empty, otherwise identical target is never covered by a clone.
    assert queued_names(True) == ["source_images", "target_images"]
    # Existing target outputs cannot change the same one-worker-per-project rule.
    assert queued_names(False) == ["source_images", "target_images"]


def test_desktop_image_success_finalizer_covers_all_worker_execution_paths():
    source = (SRC_DIR / "app_main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    methods = {
        node.name: ast.get_source_segment(source, node) or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name in {
            "_finalize_googler_images_success", "_run_googler_images_worker",
        }
    }

    worker = methods["_run_googler_images_worker"]
    assert worker.count(
        "self._finalize_googler_images_success(run_snapshot)"
    ) == 4
    assert worker.count("if code == 0:") == 3
    subprocess_success = worker.index("if process.returncode == 0:")
    assert worker.index(
        "self._finalize_googler_images_success(run_snapshot)",
        subprocess_success,
    ) > subprocess_success

    finalizer = methods["_finalize_googler_images_success"]
    assert "self._copy_googler_images_to_similar_projects" not in finalizer
    assert "copy_variable.set(False)" in finalizer
    assert "self.run_googler_video(_skip_defer=True)" in finalizer


def test_desktop_finalizer_never_reaches_copier_even_for_stale_true_state():
    class Value:
        def __init__(self, value):
            self.value = value

        def get(self):
            return self.value

        def set(self, value):
            self.value = value

    class Snapshot:
        copy_similar = True
        reference_reuse_allowed = False
        reference_reuse_reason = "captured manifest had hero refs"

    class Frame:
        pass

    frame = Frame()
    frame.googler_copy_similar = Value(True)
    frame._deferred_video_pending = False
    frame.googler_video_after_images = Value(False)
    frame.log_rows = []
    frame.log = lambda message, **_kwargs: frame.log_rows.append(message)
    copy_calls = []
    frame._copy_googler_images_to_similar_projects = (
        lambda *_args: copy_calls.append(_args)
    )
    finalizer = _load_desktop_method(
        "_finalize_googler_images_success",
        ImageGenerationSnapshot=Snapshot,
        os=__import__("os"),
        CROSS_PROJECT_IMAGE_REUSE_BLOCK_REASON="project-owned passports",
    )

    # Neither a legacy live toggle nor an old true snapshot can reach copier.
    finalizer(frame, Snapshot())
    assert copy_calls == []
    assert frame.googler_copy_similar.get() is False
    assert any("BLOCKED" in row and "project-owned" in row for row in frame.log_rows)


def test_desktop_copier_unconditionally_returns_zero():
    class Snapshot:
        reference_reuse_allowed = True
        reference_reuse_reason = "clean no-reference run"

    class Frame:
        pass

    frame = Frame()
    frame.log_rows = []
    frame.log = lambda message, **_kwargs: frame.log_rows.append(message)
    frame._image_generation_snapshot_intact = lambda _snapshot: (True, "intact")
    frame._image_reference_reuse_status = lambda: (
        False, "current passport now contains style refs"
    )
    copier = _load_desktop_method(
        "_copy_googler_images_to_similar_projects",
        ImageGenerationSnapshot=Snapshot,
        os=__import__("os"),
        CROSS_PROJECT_IMAGE_REUSE_BLOCK_REASON="project-owned passports",
    )

    assert copier(frame, "/unused/googler", Snapshot()) == 0
    assert any("BLOCKED" in row and "project-owned" in row for row in frame.log_rows)


def test_desktop_snapshot_binds_manifest_and_full_source_preset_identity():
    source = (SRC_DIR / "app_main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    methods = {
        node.name: ast.get_source_segment(source, node) or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name in {
            "_capture_image_generation_snapshot",
            "_image_preset_key_from_values",
            "_copy_googler_images_to_similar_projects",
            "_run_googler_images_worker",
        }
    }

    capture = methods["_capture_image_generation_snapshot"]
    for required in (
        "reference_manifest_sha256", "reference_bundle_id",
        "reference_source_fingerprint", "auto_hero", "analyze_story",
        "source_preset_key",
    ):
        assert required in capture

    preset = methods["_image_preset_key_from_values"]
    for required in (
        "style", "model", "aspect_ratio", "prompt_mode", "custom_prompts",
        "existing_prompts", "seed", "use_legacy", "split_long",
        "split_long_seconds", "sentences_per_prompt", "seconds_per_image",
    ):
        assert required in preset

    worker = methods["_run_googler_images_worker"]
    assert "if auto_hero_snapshot:" in worker
    assert "if analyze_story_snapshot:" in worker
    assert "if self.auto_hero_var.get():" not in worker
    assert "self._finalize_googler_images_success(run_snapshot)" in worker

    copier = methods["_copy_googler_images_to_similar_projects"]
    assert "run_snapshot.source_preset_key" in copier
    assert "!= self._image_preset_key()" not in copier


def _write_reusable_receipt_set(directory: Path, count: int) -> dict:
    directory.mkdir(parents=True, exist_ok=True)
    frames = {}
    for index in range(1, count + 1):
        key = f"{index:03d}"
        payload = f"frame-{index}".encode()
        (directory / f"{key}.jpg").write_bytes(payload)
        generation = hashlib.sha256(f"generation-{index}".encode()).hexdigest()
        frames[key] = {
            "status": "fresh",
            "generation_fingerprint": generation,
            "completed_fingerprint": generation,
            "output_sha256": hashlib.sha256(payload).hexdigest(),
            "trusted_by_user": False,
            "legacy_trusted_at": None,
        }
    (directory / "image_receipts.v1.json").write_text(
        json.dumps({"schema_version": 2, "frames": frames}),
        encoding="utf-8",
    )
    return frames


def _load_reusable_outputs_validator():
    validator = _load_desktop_method(
        "_validated_reusable_image_outputs",
        os=__import__("os"),
        re=__import__("re"),
        json=json,
        ReferenceContractError=ReferenceContractError,
    )

    class Frame:
        @staticmethod
        def _sha256_file_for_cache(path):
            return hashlib.sha256(Path(path).read_bytes()).hexdigest()

    return validator, Frame()


def test_desktop_receipt_only_copier_never_copies_old_extra_frame(tmp_path: Path):
    source_dir = tmp_path / "source" / "googler"
    _write_reusable_receipt_set(source_dir, 5)
    (source_dir / "006.jpg").write_bytes(b"old hero from former run")

    validator, source = _load_reusable_outputs_validator()
    outputs = validator(source, str(source_dir))
    assert [name for name, _sha in outputs] == [
        "001.jpg", "002.jpg", "003.jpg", "004.jpg", "005.jpg"
    ]

    class Value:
        def get(self):
            return True

    class Snapshot:
        reference_reuse_allowed = True
        reference_reuse_reason = "clean"
        source_preset_key = ("captured-preset",)

    target_status = _load_desktop_method(
        "_image_reuse_target_status", os=__import__("os")
    )

    class Target:
        googler_copy_similar = Value()
        googler_queue = None
        thief_queue = None
        googler_proc_handle = None
        bing_proc_handle = None
        thief_proc_handle = None
        _googler_images_busy = False
        _images_download_busy = False

        def __init__(self, project_dir, project_name):
            self.project_dir = str(project_dir)
            self.project_name = project_name

        @staticmethod
        def _content_key():
            return "story"

        @staticmethod
        def _image_reference_reuse_status():
            return True, "clean"

        def _image_reuse_target_status(self):
            return target_status(self)

        @staticmethod
        def _image_preset_key():
            return ("captured-preset",)

    source.project_dir = str(tmp_path / "source")
    source._validated_reusable_image_outputs = lambda path: validator(source, path)
    source._image_generation_snapshot_intact = lambda _snapshot: (True, "intact")
    source._image_reference_reuse_status = lambda: (True, "clean")
    source._content_key = lambda: "story"
    source._write_reuse_scenes = lambda *_args: False
    source.log = lambda *_args, **_kwargs: None
    empty_target = Target(tmp_path / "target-empty", "target-empty")
    stale_target = Target(tmp_path / "target-stale", "target-stale")
    stale_dir = Path(stale_target.project_dir) / "googler"
    stale_dir.mkdir(parents=True)
    (stale_dir / "006.jpg").write_bytes(b"old target frame")
    source.app_reference = type(
        "App", (),
        {"projects": {"target-empty": empty_target, "target-stale": stale_target}},
    )()
    copier = _load_desktop_method(
        "_copy_googler_images_to_similar_projects",
        ImageGenerationSnapshot=Snapshot,
        ReferenceContractError=ReferenceContractError,
        os=__import__("os"),
        tempfile=__import__("tempfile"),
        shutil=__import__("shutil"),
        _project_name_similarity=lambda *_args: 1.0,
        CROSS_PROJECT_IMAGE_REUSE_BLOCK_REASON="project-owned passports",
    )

    assert copier(source, str(source_dir), Snapshot()) == 0
    target_dir = Path(empty_target.project_dir) / "googler"
    assert sorted(path.name for path in target_dir.glob("*.jpg")) == []
    assert not (target_dir / "006.jpg").exists()
    assert sorted(path.name for path in stale_dir.glob("*.jpg")) == ["006.jpg"]


@pytest.mark.parametrize("damage", ["tampered", "missing", "incomplete", "gap"])
def test_desktop_reusable_outputs_fail_closed_on_bad_receipt_or_file(
    tmp_path: Path,
    damage: str,
):
    source_dir = tmp_path / damage / "googler"
    frames = _write_reusable_receipt_set(source_dir, 3)
    if damage == "tampered":
        (source_dir / "002.jpg").write_bytes(b"tampered")
    elif damage == "missing":
        (source_dir / "002.jpg").unlink()
    elif damage == "incomplete":
        frames["002"]["status"] = "stale"
        (source_dir / "image_receipts.v1.json").write_text(
            json.dumps({"schema_version": 2, "frames": frames}),
            encoding="utf-8",
        )
    else:
        frames["004"] = frames.pop("003")
        (source_dir / "004.jpg").write_bytes((source_dir / "003.jpg").read_bytes())
        (source_dir / "image_receipts.v1.json").write_text(
            json.dumps({"schema_version": 2, "frames": frames}),
            encoding="utf-8",
        )

    validator, frame = _load_reusable_outputs_validator()
    with pytest.raises(ReferenceContractError):
        validator(frame, str(source_dir))


def test_desktop_reusable_outputs_accept_canonical_frame_1000(tmp_path: Path):
    source_dir = tmp_path / "thousand" / "googler"
    _write_reusable_receipt_set(source_dir, 1000)
    validator, frame = _load_reusable_outputs_validator()
    outputs = validator(frame, str(source_dir))
    assert len(outputs) == 1000
    assert outputs[998][0] == "999.jpg"
    assert outputs[999][0] == "1000.jpg"


def test_desktop_copier_has_no_directory_extension_scan():
    source = (SRC_DIR / "app_main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    copier = next(
        ast.get_source_segment(source, node) or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name == "_copy_googler_images_to_similar_projects"
    )
    assert "_validated_reusable_image_outputs" in copier
    assert "os.listdir(googler_dir)" not in copier
    assert "endswith(('.jpg', '.jpeg', '.png', '.webp'))" not in copier
