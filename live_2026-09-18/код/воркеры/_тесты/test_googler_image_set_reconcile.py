import argparse
import hashlib
import json
import sys
from pathlib import Path

import pytest


SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import googler_worker as worker  # noqa: E402
import reference_contract as contract  # noqa: E402


def _publish_receipt(project: Path, count: int, *, extra_rows=None, legacy=()):
    output = project / worker.GOOGLER_IMAGES_DIR
    output.mkdir(parents=True, exist_ok=True)
    frames = {}
    names = []
    for index in range(1, count + 1):
        key = f"{index:03d}"
        name = f"{key}.jpg"
        payload = f"current-frame-{index}".encode()
        (output / name).write_bytes(payload)
        generation = hashlib.sha256(f"generation-{index}".encode()).hexdigest()
        is_legacy = index in set(legacy)
        frames[key] = {
            "generation_fingerprint": generation,
            "completed_fingerprint": generation,
            "reference_fingerprint": hashlib.sha256(b"refs").hexdigest(),
            "status": "legacy" if is_legacy else "fresh",
            "used": 0,
            "capacity": 10,
            "references": [],
            "output_sha256": hashlib.sha256(payload).hexdigest(),
            "trusted_by_user": is_legacy,
            "legacy_trusted_at": 123456 if is_legacy else None,
        }
        names.append(name)
    frames.update(extra_rows or {})
    receipt = {
        "schema_version": worker.IMAGE_RECEIPTS_SCHEMA,
        "bundle_id": hashlib.sha256(b"bundle").hexdigest(),
        "reference_fingerprint": hashlib.sha256(b"refs").hexdigest(),
        "model": "OPENAI",
        "frames": frames,
    }
    receipt_path = output / worker.IMAGE_RECEIPTS_FILE
    worker._atomic_write_json(str(receipt_path), receipt)
    return output, receipt_path, receipt, names


def _transaction(project: Path):
    root = (
        project / worker.GOOGLER_IMAGES_DIR / worker.IMAGE_SET_QUARANTINE_DIR
    )
    transactions = sorted(root.glob("txn-*"))
    assert len(transactions) == 1
    manifest = json.loads(
        (transactions[0] / worker.IMAGE_SET_QUARANTINE_MANIFEST).read_text()
    )
    return transactions[0], manifest


def test_success_quarantines_old_ref_bearing_frame_and_republishes_exact_rows(tmp_path):
    project = tmp_path / "project"
    stale_payload = b"old-frame-with-wrong-hero-identity"
    stale_sha = hashlib.sha256(stale_payload).hexdigest()
    generation = hashlib.sha256(b"old-generation").hexdigest()
    extra_receipt_row = {
        "generation_fingerprint": generation,
        "completed_fingerprint": generation,
        "reference_fingerprint": hashlib.sha256(b"old-hero-ref").hexdigest(),
        "status": "fresh",
        "used": 1,
        "capacity": 10,
        "references": [{"canonical_name": "old hero"}],
        "output_sha256": stale_sha,
        "trusted_by_user": False,
        "legacy_trusted_at": None,
    }
    output, receipt_path, receipt, names = _publish_receipt(
        project, 5, extra_rows={"006": extra_receipt_row}
    )
    (output / "006.jpg").write_bytes(stale_payload)

    result = worker._reconcile_authoritative_image_set(
        str(project), str(receipt_path), receipt, names
    )

    assert result["quarantined"] == 1
    assert not (output / "006.jpg").exists()
    assert all((output / name).read_bytes() == f"current-frame-{i}".encode()
               for i, name in enumerate(names, 1))
    transaction, manifest = _transaction(project)
    assert manifest["status"] == "committed"
    assert manifest["files"] == [{
        "source_name": "006.jpg",
        "quarantine_name": "006.jpg",
        "sha256": stale_sha,
        "size": len(stale_payload),
    }]
    assert (transaction / "files" / "006.jpg").read_bytes() == stale_payload
    published = json.loads(receipt_path.read_text())
    assert list(published["frames"]) == ["001", "002", "003", "004", "005"]


def test_custom_images_all_cache_hit_still_reconciles(tmp_path, monkeypatch):
    project = tmp_path / "project"
    output, receipt_path, receipt, names = _publish_receipt(project, 3)
    (output / "004.png").write_bytes(b"old-cache-hit-identity")
    runtime = {
        "schema_version": 1,
        "bundle_id": hashlib.sha256(b"bundle").hexdigest(),
        "reference_fingerprint": hashlib.sha256(b"refs").hexdigest(),
        "scenes_sha256": hashlib.sha256(b"unused-by-mock").hexdigest(),
        "capacity": 10,
        "scenes": [{"index": index} for index in range(3)],
    }
    monkeypatch.setattr(worker, "_assert_image_generation_key", lambda *_args: None)
    monkeypatch.setattr(worker, "_load_reference_sources", lambda _args: {
        "characters": [], "style": [], "manifest": None,
    })
    monkeypatch.setattr(worker, "_prepare_reference_runtime", lambda *_args, **_kwargs: runtime)
    monkeypatch.setattr(
        worker, "_prepare_image_receipts",
        lambda *_args, **_kwargs: (str(receipt_path), receipt, []),
    )
    monkeypatch.setattr(
        worker, "generate_image",
        lambda *_args, **_kwargs: pytest.fail("cache-hit path generated an image"),
    )
    monkeypatch.setattr(worker, "print_user", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(worker, "print_debug", lambda *_args, **_kwargs: None)
    args = argparse.Namespace(
        project_dir=str(project), prompts_json=json.dumps(["a", "b", "c"]),
        style="ink", seed=7, aspect_ratio="landscape", model="OPENAI",
        user_refs=None, style_refs=None, reference_manifest=None,
        image_threads=1, use_cache=True, trust_legacy_cache=False,
    )

    worker.cmd_generate_images(args)

    assert names == ["001.jpg", "002.jpg", "003.jpg"]
    assert not (output / "004.png").exists()
    transaction, manifest = _transaction(project)
    assert manifest["status"] == "committed"
    assert (transaction / "files" / "004.png").read_bytes() == b"old-cache-hit-identity"


@pytest.mark.parametrize("damage", ["missing", "hash", "nonfresh"])
def test_incomplete_expected_set_blocks_reconcile_and_preserves_stale_output(
    tmp_path, damage
):
    project = tmp_path / damage
    output, receipt_path, receipt, names = _publish_receipt(project, 2)
    stale = output / "003.webp"
    stale.write_bytes(b"must-not-move")
    if damage == "missing":
        (output / "002.jpg").unlink()
    elif damage == "hash":
        (output / "002.jpg").write_bytes(b"tampered")
    else:
        receipt["frames"]["002"]["status"] = "stale"
        worker._atomic_write_json(str(receipt_path), receipt)

    with pytest.raises(contract.ReferenceContractError):
        worker._reconcile_authoritative_image_set(
            str(project), str(receipt_path), receipt, names
        )

    assert stale.read_bytes() == b"must-not-move"
    quarantine = output / worker.IMAGE_SET_QUARANTINE_DIR
    assert not quarantine.exists() or not list(quarantine.glob("txn-*"))


def test_failed_multi_file_reconcile_rolls_back_every_move(tmp_path, monkeypatch):
    project = tmp_path / "rollback"
    output, receipt_path, receipt, names = _publish_receipt(project, 2)
    old_receipt = receipt_path.read_bytes()
    (output / "003.jpg").write_bytes(b"stale-three")
    (output / "004.jpeg").write_bytes(b"stale-four")
    real_move = worker._atomic_quarantine_move

    def fail_second_forward_move(source, destination):
        if Path(source).name == "004.jpeg" and Path(destination).parent.name == "files":
            raise OSError("injected second move failure")
        return real_move(source, destination)

    monkeypatch.setattr(worker, "_atomic_quarantine_move", fail_second_forward_move)

    with pytest.raises(contract.ReferenceContractError, match="rolled back"):
        worker._reconcile_authoritative_image_set(
            str(project), str(receipt_path), receipt, names
        )

    assert (output / "003.jpg").read_bytes() == b"stale-three"
    assert (output / "004.jpeg").read_bytes() == b"stale-four"
    assert receipt_path.read_bytes() == old_receipt
    transaction, manifest = _transaction(project)
    assert manifest["status"] == "rolled_back"
    assert not list((transaction / "files").iterdir())


def test_next_reconcile_recovers_interrupted_prepared_transaction(tmp_path):
    project = tmp_path / "crash-recovery"
    output, receipt_path, receipt, names = _publish_receipt(project, 1)
    stale_payload = b"stale-interrupted-move"
    stale_sha = hashlib.sha256(stale_payload).hexdigest()
    stale = output / "002.jpg"
    stale.write_bytes(stale_payload)
    quarantine_root = output / worker.IMAGE_SET_QUARANTINE_DIR
    tx_dir = quarantine_root / "txn-00000000000000000001-0123456789abcdef"
    files_dir = tx_dir / "files"
    files_dir.mkdir(parents=True)
    before = receipt_path.read_bytes()
    (tx_dir / worker.IMAGE_SET_RECEIPT_BACKUP).write_bytes(before)
    manifest = {
        "schema_version": worker.IMAGE_SET_QUARANTINE_SCHEMA,
        "transaction_id": tx_dir.name,
        "status": "prepared",
        "created_at": 1,
        "reason": "outside-authoritative-image-set",
        "receipt_before_sha256": hashlib.sha256(before).hexdigest(),
        "receipt_backup_present": True,
        "expected_files": [{
            "name": "001.jpg",
            "sha256": receipt["frames"]["001"]["output_sha256"],
        }],
        "files": [{
            "source_name": "002.jpg",
            "quarantine_name": "002.jpg",
            "sha256": stale_sha,
            "size": len(stale_payload),
        }],
    }
    worker._atomic_write_json(
        str(tx_dir / worker.IMAGE_SET_QUARANTINE_MANIFEST), manifest
    )
    worker._atomic_quarantine_move(str(stale), str(files_dir / "002.jpg"))

    result = worker._reconcile_authoritative_image_set(
        str(project), str(receipt_path), receipt, names
    )

    assert result["quarantined"] == 1
    transactions = sorted(quarantine_root.glob("txn-*"))
    assert len(transactions) == 2
    states = {
        path.name: json.loads(
            (path / worker.IMAGE_SET_QUARANTINE_MANIFEST).read_text()
        )["status"]
        for path in transactions
    }
    assert states[tx_dir.name] == "rolled_back"
    committed = next(path for path in transactions if states[path.name] == "committed")
    assert (committed / "files" / "002.jpg").read_bytes() == stale_payload


def test_strict_validator_accepts_only_explicitly_trusted_legacy(tmp_path):
    project = tmp_path / "legacy"
    output, _receipt_path, receipt, names = _publish_receipt(
        project, 1, legacy=(1,)
    )
    assert worker._validate_authoritative_image_set(
        str(output), receipt, names
    ) == {"001.jpg": receipt["frames"]["001"]["output_sha256"]}

    receipt["frames"]["001"]["trusted_by_user"] = False
    with pytest.raises(contract.ReferenceContractError, match="no explicit user trust"):
        worker._validate_authoritative_image_set(str(output), receipt, names)


def test_sequence_contract_and_reconcile_support_more_than_999_frames(tmp_path):
    project = tmp_path / "large"
    output, receipt_path, receipt, names = _publish_receipt(project, 1000)
    assert names[-1] == "1000.jpg"
    assert worker._canonical_numeric_image_name("1000.jpg")
    assert not worker._canonical_numeric_image_name("0001.jpg")
    (output / "1001.webp").write_bytes(b"stale-after-thousand")

    result = worker._reconcile_authoritative_image_set(
        str(project), str(receipt_path), receipt, names
    )

    assert result["quarantined"] == 1
    assert (output / "1000.jpg").exists()
    assert not (output / "1001.webp").exists()
    transaction, manifest = _transaction(project)
    assert manifest["status"] == "committed"
    assert (transaction / "files" / "1001.webp").read_bytes() == b"stale-after-thousand"


def test_blocked_frame_without_file_does_not_fail_the_whole_run(tmp_path):
    """Кадр, насовсем зарубленный контент-фильтром, больше не валит весь прогон.

    Второй сценарий (не первый кадр и не пустой набор): из трёх запланированных
    кадров второй не получен — файла нет, а строка receipt осталась незавершённой
    (`completed_fingerprint=None`, как пишет воркер после «⛔ пропускаю картинку»).
    Раньше `_validate_authoritative_image_set` падал на нём и уносил два готовых.
    """
    project = tmp_path / "gap"
    output, receipt_path, receipt, names = _publish_receipt(project, 3)
    (output / "002.jpg").unlink()
    receipt["frames"]["002"]["completed_fingerprint"] = None
    receipt["frames"]["002"]["status"] = "stale"
    worker._atomic_write_json(str(receipt_path), receipt)

    present = worker._present_output_names(str(output), names)
    assert present == ["001.jpg", "003.jpg"]

    with pytest.raises(contract.ReferenceContractError, match="not exact for frame 002"):
        worker._validate_authoritative_image_set(str(output), receipt, names)

    result = worker._reconcile_authoritative_image_set(
        str(project), str(receipt_path), receipt, present
    )
    assert sorted(result["verified"]) == ["001.jpg", "003.jpg"]
    assert (output / "001.jpg").exists() and (output / "003.jpg").exists()


def test_present_frame_with_broken_receipt_still_fails(tmp_path):
    """Файл на месте, а receipt не сходится — гейт обязан падать, как и раньше."""
    project = tmp_path / "tampered"
    output, receipt_path, receipt, names = _publish_receipt(project, 3)
    receipt["frames"]["002"]["completed_fingerprint"] = None
    worker._atomic_write_json(str(receipt_path), receipt)

    present = worker._present_output_names(str(output), names)
    assert present == names

    with pytest.raises(contract.ReferenceContractError, match="not exact for frame 002"):
        worker._reconcile_authoritative_image_set(
            str(project), str(receipt_path), receipt, present
        )
