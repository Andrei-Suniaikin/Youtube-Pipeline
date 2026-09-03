# Synthetic explicit-target rewrite fixtures

This directory is a small, offline example of the rewrite-only domain contract.
It contains no production configuration, credentials, channels, or donor URLs.

The contract has no fixed group, language, channel, or schedule cardinality:

- `channel_map.json` is an arbitrary catalog. Each synthetic channel owns one
  exact fake UC and a list of zero or more named schedules. Tags such as
  `theme:night-sky` and `language:en` are metadata for display and filtering;
  they never select release targets.
- `donor_queue.json` is the frozen content plan. Every `plan_items` row is an
  independently runnable rewrite and explicitly names exactly one
  `target_channel_uc`, one `schedule_id`, and one concrete `slot_id`.
- `expected_releases.json` is a 1:1 oracle: the expected release count equals
  the exact number of explicit frozen plan rows. A tag, shared source, or batch
  parent never adds an implicit release.

The finite example deliberately uses an irregular catalog (five channels and
seven schedule definitions) so consumers cannot rely on a fixed matrix. Those
numbers are illustrative, not schema limits. A catalog or plan may contain any
number of channels, schedules, and future slots while retaining the same 1:1
rule.

## Scenarios demonstrated

1. The three channels tagged `theme:night-sky` receive three different donors
   on `2030-06-03`. The common tag does not make them share or fan out a donor.
2. The donor `synthetic-source-shared-aurora-guide` is intentionally copied to
   two explicitly selected channels. `bulk_batches` models the optional UX
   action, but its two selections are expanded into two complete, independent
   plan items before either item is frozen. Only those plan items produce
   releases; the parent batch is never executable.
3. Every channel UC, concrete `slot_id`, `plan_item_id`, and `release_id` is
   unique where required. The `(target_channel_uc, slot_id)` pair is also
   unique, preventing two releases from claiming the same channel slot.

All source URLs use the reserved `.invalid` domain. Every UC contains `FAKE`;
profile and host references start with `SYNTHETIC_`; preset values are explicit
`EXISTING_*_PLACEHOLDER` strings. Replace those values only when wiring a real
environment—never treat these fixtures as production configuration.

Run the targeted offline contract check from the bundle root with:

```bash
python3 -m pytest tests/test_domain_fixtures.py
```
