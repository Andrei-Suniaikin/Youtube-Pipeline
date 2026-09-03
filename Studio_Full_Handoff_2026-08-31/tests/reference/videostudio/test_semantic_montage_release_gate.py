from __future__ import annotations

import json
from pathlib import Path

from semantic_montage.audio import AMBIENCE_ENERGY_FLOOR_POLICY_VERSION
from semantic_montage.common import canonical_hash, sha256_file
from semantic_montage.release_gate import (
    ReleaseInputs,
    build_release_report,
    verify_release_report,
)


def _write(path: Path, payload):
    path.write_text(json.dumps(payload), encoding="utf-8")


def _fixture(tmp_path: Path) -> ReleaseInputs:
    master = tmp_path / "master.mp4"
    master.write_bytes(b"master-v1")
    timeline = tmp_path / "timeline.json"
    sound = tmp_path / "sound.json"
    manifest = tmp_path / "manifest.json"
    _write(
        timeline,
        {
            "schema_version": 2,
            "contract": "production_frame_exact",
            "narration_duration": 4.0,
            "slots": [
                {
                    "slot_id": "b001",
                    "start": 0.0,
                    "end": 4.0,
                    "status": "resolved",
                    "verdict": "relevant",
                    "selection_provenance": {"method": "beam"},
                    "selected_clip": {
                        "fill_strategy": "none",
                        "retime_factor": 1.0,
                        "overlay_analysis": {"status": "clean"},
                    },
                }
            ],
        },
    )
    _write(
        sound,
        {
            "schema_version": 2,
            "status": "ready",
            # This scalar is intentionally false: the gate must ignore it and
            # derive coverage from intervals plus hash-bound rendered QA.
            "ambience_coverage": 0.0,
            "ambience_regions": [{"start": 0.0, "end": 4.0}],
        },
    )
    canonical_energy_floor_policy = {
        "version": AMBIENCE_ENERGY_FLOOR_POLICY_VERSION,
        "outer_threshold_dbfs": -65.0,
        "outer_window_frames": 480,
        "outer_max_silent_windows": 10,
        "semantic_threshold_dbfs": -55.0,
        "semantic_window_frames": 2_400,
        "semantic_max_silent_windows": 2,
        "safety_margin_db": 2.0,
        "max_effective_gain_db": 12.0,
        "per_region_peak_ceiling_dbfs": -9.0,
        "method": "static_per_region_gain_from_exact_provisional_pcm",
        "window_grid_origin_frame": 0,
        "region_assignment": "every_failing_global_block_intersecting_region",
        "dynamic_normalization": False,
    }
    energy_floor_base = {
        "contract": AMBIENCE_ENERGY_FLOOR_POLICY_VERSION,
        "status": "verified_rebuilt",
        "provisional_stem_sha256": "5" * 64,
        "policy": canonical_energy_floor_policy,
        "rebuilt_stem": {
            "sha256": "2" * 64,
            "outer_energy_gaps": [],
            "semantic_energy_gaps": [],
            "boundary_click_flags": [],
        },
    }
    energy_floor = {
        **energy_floor_base,
        "receipt_sha256": canonical_hash(energy_floor_base),
    }
    _write(
        manifest,
        {
            "output": {"sha256": sha256_file(master)},
            "donor_audio_proof": {
                "passed": True,
                "final_video_input_audio_map": False,
                "aac_encode_count": 1,
            },
            "audio": {
                "mode": "production_v2_five_lane_ambience_energy_floor_two_pass",
                "aac_encode_count": 1,
                "ambience_energy_floor": energy_floor,
                "stems": {
                    "music": {"sha256": "1" * 64},
                    "ambience": {"sha256": "2" * 64},
                    "presence": {"sha256": "3" * 64},
                    "events": {"sha256": "4" * 64},
                },
            },
            "qa": {"passed": True, "audio_codec": "aac"},
            "clips": [{"slot_id": "b001", "fill_strategy": "none", "retime_factor": 1.0}],
        },
    )
    inputs = ReleaseInputs(
        master=master,
        timeline=timeline,
        sound_plan=sound,
        render_manifest=manifest,
        visual_qa=tmp_path / "visual.json",
        boundary_qa=tmp_path / "boundary.json",
        overlay_qa=tmp_path / "overlay.json",
        audio_qa=tmp_path / "audio.json",
    )
    base_hashes = {
        "master": sha256_file(master),
        "timeline": sha256_file(timeline),
        "sound_plan": sha256_file(sound),
        "render_manifest": sha256_file(manifest),
    }
    for name in ("visual_qa", "boundary_qa", "overlay_qa"):
        _write(getattr(inputs, name), {"passed": True, "input_hashes": base_hashes})
    _write(
        inputs.audio_qa,
        {
            "passed": True,
            "input_hashes": base_hashes,
            "stems": {
                "ambience": {
                    "passed": True,
                    "sample_rate": 48_000,
                    "frames": 192_000,
                }
            },
            "ambience_energy": {"passed": True, "gaps": []},
            "stem_hashes": {
                "ambience": "2" * 64,
                "presence": "3" * 64,
                "events": "4" * 64,
            },
            "semantic": {
                "sound_qa": {
                    "verdict": "PASS",
                    "lanes": {
                        "ambience": {
                            "coverage": {
                                "coverage_ratio": 1.0,
                                "gaps": [],
                                "overlaps": [],
                            }
                        }
                    },
                }
            },
        },
    )
    return inputs


def test_release_gate_is_hash_bound_and_mutation_invalidates(tmp_path: Path):
    inputs = _fixture(tmp_path)
    report = build_release_report(inputs)
    assert report["passed"], report["errors"]
    report_path = tmp_path / "release.json"
    _write(report_path, report)
    assert verify_release_report(report_path, inputs)
    inputs.master.write_bytes(b"master-v2")
    assert not verify_release_report(report_path, inputs)


def test_release_gate_blocks_hidden_fill(tmp_path: Path):
    inputs = _fixture(tmp_path)
    timeline = json.loads(inputs.timeline.read_text())
    timeline["slots"][0]["selected_clip"]["fill_strategy"] = "tpad_clone"
    _write(inputs.timeline, timeline)
    report = build_release_report(inputs)
    assert not report["passed"]
    assert any("hidden fill" in row for row in report["errors"])


def test_release_gate_rejects_old_mix_without_verified_energy_floor(tmp_path: Path):
    inputs = _fixture(tmp_path)
    manifest = json.loads(inputs.render_manifest.read_text())
    manifest["audio"]["mode"] = "production_v2_five_lane_two_pass"
    manifest["audio"].pop("ambience_energy_floor")
    _write(inputs.render_manifest, manifest)

    report = build_release_report(inputs)

    assert not report["passed"]
    assert any("energy-floor production mix" in row for row in report["errors"])
    assert any("lacks verified ambience energy-floor" in row for row in report["errors"])


def test_release_gate_rejects_old_mix_even_with_valid_energy_floor(tmp_path: Path):
    inputs = _fixture(tmp_path)
    manifest = json.loads(inputs.render_manifest.read_text())
    manifest["audio"]["mode"] = "production_v2_five_lane_two_pass"
    _write(inputs.render_manifest, manifest)

    report = build_release_report(inputs)

    assert not report["passed"]
    assert any("energy-floor production mix" in row for row in report["errors"])


def test_release_gate_rejects_rehashed_noncanonical_energy_floor_policy(
    tmp_path: Path,
):
    inputs = _fixture(tmp_path)
    manifest = json.loads(inputs.render_manifest.read_text())
    energy_floor = manifest["audio"]["ambience_energy_floor"]
    energy_floor["policy"]["semantic_threshold_dbfs"] = -120.0
    unsigned = dict(energy_floor)
    unsigned.pop("receipt_sha256")
    energy_floor["receipt_sha256"] = canonical_hash(unsigned)
    _write(inputs.render_manifest, manifest)

    report = build_release_report(inputs)

    assert not report["passed"]
    assert any("energy-floor proof is invalid" in row for row in report["errors"])


def test_release_gate_rejects_rehashed_rebuilt_stem_hash_mismatch(
    tmp_path: Path,
):
    inputs = _fixture(tmp_path)
    manifest = json.loads(inputs.render_manifest.read_text())
    energy_floor = manifest["audio"]["ambience_energy_floor"]
    energy_floor["rebuilt_stem"]["sha256"] = "9" * 64
    unsigned = dict(energy_floor)
    unsigned.pop("receipt_sha256")
    energy_floor["receipt_sha256"] = canonical_hash(unsigned)
    _write(inputs.render_manifest, manifest)

    report = build_release_report(inputs)

    assert not report["passed"]
    assert any("energy-floor proof is invalid" in row for row in report["errors"])


def test_release_gate_rejects_tampered_ambience_energy_floor_receipt(
    tmp_path: Path,
):
    inputs = _fixture(tmp_path)
    manifest = json.loads(inputs.render_manifest.read_text())
    manifest["audio"]["ambience_energy_floor"]["rebuilt_stem"][
        "semantic_energy_gaps"
    ] = [{"start": 1.0, "end": 1.2}]
    _write(inputs.render_manifest, manifest)

    report = build_release_report(inputs)

    assert not report["passed"]
    assert any("energy-floor proof is invalid" in row for row in report["errors"])
