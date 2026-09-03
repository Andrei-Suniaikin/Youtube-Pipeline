from __future__ import annotations

from collections import Counter
import datetime as dt
import json
from pathlib import Path
import re
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "contracts" / "domain_fixtures"


def read_json(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_rewrite_only_domain_fixture_contract() -> None:
    channel_map = read_json("channel_map.json")
    donor_queue = read_json("donor_queue.json")
    release_oracle = read_json("expected_releases.json")

    channels = channel_map["channels"]
    plan_items = donor_queue["plan_items"]
    bulk_batches = donor_queue["bulk_batches"]
    releases = release_oracle["releases"]

    assert channel_map["schema_version"] == 2
    assert donor_queue["schema_version"] == 2
    assert release_oracle["schema_version"] == 2
    assert {
        channel_map["fixture_kind"],
        donor_queue["fixture_kind"],
        release_oracle["fixture_kind"],
    } == {"synthetic_explicit_target_rewrite"}

    # The catalog is not a group/language matrix. Schedule arrays may have
    # different cardinalities, and tags never form routing dimensions.
    assert channels
    channel_ucs = [channel["channel_uc"] for channel in channels]
    assert len(channel_ucs) == len(set(channel_ucs))
    assert len({channel["channel_key"] for channel in channels}) == len(channels)
    assert "groups" not in channel_map
    assert "languages" not in channel_map

    fake_uc = re.compile(r"^UCFAKE\d{18}$")
    channels_by_uc = {channel["channel_uc"]: channel for channel in channels}
    schedules_by_id: dict[str, tuple[str, dict]] = {}
    schedule_counts: list[int] = []
    for channel in channels:
        assert fake_uc.fullmatch(channel["channel_uc"])
        assert channel["adspower_profile_ref"].startswith("SYNTHETIC_PROFILE_")
        assert channel["upload_host_ref"].startswith("SYNTHETIC_HOST_")
        assert "group_id" not in channel
        assert len(channel["tags"]) >= 3
        assert len(channel["tags"]) == len(set(channel["tags"]))
        assert "mode:rewrite" in channel["tags"]

        schedules = channel["schedules"]
        assert isinstance(schedules, list)
        schedule_counts.append(len(schedules))
        for schedule in schedules:
            schedule_id = schedule["schedule_id"]
            assert schedule_id not in schedules_by_id
            assert schedule["timezone"] == "UTC"
            dt.time.fromisoformat(schedule["local_time"])
            schedules_by_id[schedule_id] = (channel["channel_uc"], schedule)

        assert set(channel["existing_preset_refs"]) == {
            "video_studio",
            "rewrite_text",
            "cover",
        }
        assert all(
            "EXISTING_" in value and "_PLACEHOLDER_" in value
            for value in channel["existing_preset_refs"].values()
        )

    assert min(schedule_counts) >= 1
    assert max(schedule_counts) > min(schedule_counts)
    assert len(schedules_by_id) == sum(schedule_counts)

    # Every frozen rewrite row owns one exact UC and one concrete slot. No
    # group, language, tag, or plural-target field can imply extra rows.
    assert plan_items
    assert [item["input_order"] for item in plan_items] == list(
        range(1, len(plan_items) + 1)
    )
    plan_item_ids = [item["plan_item_id"] for item in plan_items]
    slot_ids = [item["slot_id"] for item in plan_items]
    channel_slots = [
        (item["target_channel_uc"], item["slot_id"]) for item in plan_items
    ]
    assert len(plan_item_ids) == len(set(plan_item_ids))
    assert len(slot_ids) == len(set(slot_ids))
    assert len(channel_slots) == len(set(channel_slots))

    plan_items_by_id = {item["plan_item_id"]: item for item in plan_items}
    for item in plan_items:
        assert {key for key in item if key.startswith("target_")} == {
            "target_channel_uc"
        }
        assert item["target_channel_uc"] in channels_by_uc
        assert item["source_mode"] == "rewrite"
        assert item["freeze_state"] == "frozen"
        assert "group_id" not in item
        assert "language" not in item
        assert "tags" not in item
        assert urlparse(item["source_url"]).hostname == "donor.invalid"
        dt.date.fromisoformat(item["slot_date"])

        schedule_owner_uc, _ = schedules_by_id[item["schedule_id"]]
        assert schedule_owner_uc == item["target_channel_uc"]

    # Three thematically related channels intentionally receive three distinct
    # donors on the same day; the shared theme tag does not cause a fan-out.
    related_ucs = {
        channel["channel_uc"]
        for channel in channels
        if "theme:night-sky" in channel["tags"]
    }
    assert len(related_ucs) == 3
    related_day_items = [
        item
        for item in plan_items
        if item["slot_date"] == "2030-06-03"
        and item["target_channel_uc"] in related_ucs
    ]
    assert len(related_day_items) == 3
    assert {item["target_channel_uc"] for item in related_day_items} == related_ucs
    assert len({item["source_id"] for item in related_day_items}) == 3
    assert len({item["source_url"] for item in related_day_items}) == 3

    # Optional bulk UX is only a parent record. Its selected targets have
    # already become complete per-channel plan items before freeze.
    assert len(bulk_batches) == 1
    batch = bulk_batches[0]
    assert batch["expansion_state"] == "expanded_before_freeze"
    selected_targets = batch["selected_targets"]
    assert len(selected_targets) == 2
    assert len({target["target_channel_uc"] for target in selected_targets}) == 2
    assert batch["expanded_plan_item_ids"] == [
        target["plan_item_id"] for target in selected_targets
    ]

    batch_plan_items = []
    for selected in selected_targets:
        assert {key for key in selected if key.startswith("target_")} == {
            "target_channel_uc"
        }
        item = plan_items_by_id[selected["plan_item_id"]]
        assert item["origin"] == {
            "kind": "bulk_expansion",
            "batch_id": batch["batch_id"],
        }
        assert item["source_id"] == batch["source_id"]
        assert item["source_url"] == batch["source_url"]
        for field in ("target_channel_uc", "schedule_id", "slot_id", "slot_date"):
            assert item[field] == selected[field]
        batch_plan_items.append(item)

    assert {
        item["plan_item_id"]
        for item in plan_items
        if item["source_id"] == batch["source_id"]
    } == set(batch["expanded_plan_item_ids"])
    assert len({item["source_id"] for item in batch_plan_items}) == 1
    assert len({item["target_channel_uc"] for item in batch_plan_items}) == 2

    # The release oracle is exactly 1:1 with explicit rows. Duplicate source
    # IDs are allowed only because multiple target rows explicitly copied them.
    assert "fanout_per_source" not in release_oracle
    assert release_oracle["expected_release_count"] == len(plan_items)
    assert len(releases) == len(plan_items)
    assert [release["release_order"] for release in releases] == list(
        range(1, len(releases) + 1)
    )
    assert len({release["release_id"] for release in releases}) == len(releases)
    assert len({release["plan_item_id"] for release in releases}) == len(releases)
    assert len({release["slot_id"] for release in releases}) == len(releases)
    assert len(
        {(release["channel_uc"], release["slot_id"]) for release in releases}
    ) == len(releases)
    assert {release["plan_item_id"] for release in releases} == set(plan_item_ids)
    assert Counter(release["source_id"] for release in releases) == Counter(
        item["source_id"] for item in plan_items
    )

    for release in releases:
        item = plan_items_by_id[release["plan_item_id"]]
        assert release["source_id"] == item["source_id"]
        assert release["source_mode"] == item["source_mode"]
        assert release["source_url"] == item["source_url"]
        assert release["channel_uc"] == item["target_channel_uc"]
        assert release["schedule_id"] == item["schedule_id"]
        assert release["slot_id"] == item["slot_id"]
        assert "group_id" not in release
        assert "language" not in release

        publish_at = dt.datetime.fromisoformat(release["publish_at"])
        _, schedule = schedules_by_id[item["schedule_id"]]
        assert publish_at.utcoffset() == dt.timedelta(0)
        assert publish_at.date().isoformat() == item["slot_date"]
        assert publish_at.strftime("%H:%M:%S") == schedule["local_time"]

    shared_releases = [
        release for release in releases if release["source_id"] == batch["source_id"]
    ]
    assert len(shared_releases) == len(selected_targets) == 2
    assert {release["channel_uc"] for release in shared_releases} == {
        target["target_channel_uc"] for target in selected_targets
    }

    serialized = json.dumps(
        [channel_map, donor_queue, release_oracle], ensure_ascii=False
    )
    assert not re.search(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", serialized)
    assert "@" not in serialized
    assert not any(marker in serialized for marker in ("/Users/", "/home/", "/opt/"))
