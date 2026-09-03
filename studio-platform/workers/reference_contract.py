#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Deterministic, fail-closed contract for image references.

This module deliberately knows nothing about HTTP providers or project jobs.  It
is the shared boundary between uploaded reference files and provider-specific
generation code: uploaded pixels are canonicalized, manual hero groups are
resolved exactly, and every logical scene reference receives an auditable
status before a provider can be called.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, is_dataclass
from io import BytesIO
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
import threading
import time
from types import MappingProxyType
from typing import Any, Mapping, Sequence
import unicodedata
import warnings

from PIL import Image, ImageOps, UnidentifiedImageError


CONTRACT_SCHEMA_VERSION = 1
PROJECT_REFERENCE_STATE_SCHEMA_VERSION = 1
PROJECT_REFERENCE_STATE_FILE = "desktop_references.v1.json"
PROJECT_REFERENCE_STATE_POINTER_FILE = "desktop_references.current.v1.json"
PROJECT_REFERENCE_STATE_HISTORY_DIR = "reference_state_history"
PROJECT_REFERENCE_STATE_COMMITS_DIR = "reference_state_commits"
MAX_REFERENCE_STATE_BYTES = 512 * 1024
MAX_REFERENCE_COMMIT_BYTES = 4096

# Live provider capabilities verified on 2026-08-21.  This is intentionally the
# only model -> reference capacity registry in the contract layer.  Callers must
# fail on an unknown model instead of guessing a conservative/default value.
MODEL_REFERENCE_CAPACITIES = MappingProxyType(
    {
        "OPENAI": 10,
        "FLOWER": 1,  # Flower reference support is its single-image edit path.
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
)

STYLE_SOURCE_MAX = 5
HERO_ANGLES_MAX = 6
HERO_GROUPS_MAX = 6
MAX_STYLE_SOURCES = STYLE_SOURCE_MAX
# Backwards-readable name for UI counters: this is a *source* limit.  The five
# sources are consolidated into one provider-level style moodboard slot.
MAX_STYLE_REFERENCES = MAX_STYLE_SOURCES
MAX_HERO_ANGLES = HERO_ANGLES_MAX
MAX_HERO_GROUPS = HERO_GROUPS_MAX
MAX_REFERENCE_BYTES = 20 * 1024 * 1024
MAX_REFERENCE_PIXELS = 36_000_000
MAX_REFERENCE_DIMENSION = 12_000

REFERENCE_AUDIT_STATUSES = (
    "selected",
    "not_applicable",
    "superseded",
    "blocking",
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ARTIFACT_KIND_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_ALLOWED_IMAGE_FORMATS = frozenset({"JPEG", "PNG", "WEBP"})
_DECLARED_FORMATS = {
    "jpg": "JPEG",
    "jpeg": "JPEG",
    ".jpg": "JPEG",
    ".jpeg": "JPEG",
    "image/jpg": "JPEG",
    "image/jpeg": "JPEG",
    "png": "PNG",
    ".png": "PNG",
    "image/png": "PNG",
    "webp": "WEBP",
    ".webp": "WEBP",
    "image/webp": "WEBP",
}

_PROJECT_STATE_THREAD_LOCK = threading.RLock()


class ReferenceContractError(ValueError):
    """A stable fail-closed reference validation/allocation error."""

    def __init__(
        self,
        message: str,
        *,
        audit: Sequence["ReferenceSlotAudit"] = (),
    ) -> None:
        super().__init__(message)
        self.audit = tuple(audit)


@dataclass(frozen=True)
class ManualHeroGroup:
    """One user-supplied hero group and the exact aliases naming its subject."""

    group_id: str
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class ManualGroupResolution:
    group_id: str
    character_id: str | None
    primary_name: str | None
    match_kind: str  # exact | unique_single_major | explicit_single_subject


@dataclass(frozen=True)
class CanonicalImage:
    """Decoded reference represented as deterministic metadata-free JPEG."""

    jpeg_bytes: bytes
    sha256: str
    source_sha256: str
    width: int
    height: int
    source_format: str


@dataclass(frozen=True)
class ReferenceArtifact:
    """A content-addressed canonical reference persisted on local storage."""

    path: str
    sha256: str
    width: int
    height: int
    source_hashes: tuple[str, ...]
    kind: str
    fingerprint: str


@dataclass(frozen=True)
class LogicalReferenceSlot:
    """One required logical input to a scene's provider request.

    ``applicable=False`` records a deliberate absence.  ``superseded_by`` must
    explicitly point at another active slot (for example a manual hero replacing
    an auto hero); the packer never invents precedence or silently drops a slot.
    Every remaining active slot is required.
    """

    slot_id: str
    kind: str
    artifact: ReferenceArtifact | None = None
    applicable: bool = True
    superseded_by: str | None = None


@dataclass(frozen=True)
class ReferenceSlotAudit:
    slot_id: str
    kind: str
    status: str
    artifact_sha256: str | None
    reason: str
    superseded_by: str | None = None


@dataclass(frozen=True)
class SceneReferencePack:
    model: str
    capacity: int
    selected: tuple[LogicalReferenceSlot, ...]
    audit: tuple[ReferenceSlotAudit, ...]
    fingerprint: str

    @property
    def provider_artifacts(self) -> tuple[ReferenceArtifact, ...]:
        return tuple(slot.artifact for slot in self.selected if slot.artifact is not None)


def reference_capacity(model: str) -> int:
    """Return the verified capacity for ``model``; unknown models are blocking."""

    if not isinstance(model, str) or not model.strip():
        raise ReferenceContractError("reference model must be a non-empty string")
    normalized = model.strip().upper()
    try:
        return MODEL_REFERENCE_CAPACITIES[normalized]
    except KeyError as exc:
        raise ReferenceContractError(
            f"reference capacity is not registered for model {normalized!r}"
        ) from exc


def normalize_alias(value: str) -> str:
    """NFKC + punctuation/whitespace + casefold for exact alias comparison.

    Typography is not identity: ``«Captain»``, full-width compatibility
    characters, and repeated separators normalize to the same alias.  This is
    still exact matching after normalization; collisions are rejected by the
    resolver instead of being resolved heuristically.
    """

    if not isinstance(value, str):
        raise ReferenceContractError("reference alias must be a string")
    compatible = unicodedata.normalize("NFKC", value).casefold()
    normalized = "".join(
        " " if char.isspace() or unicodedata.category(char).startswith(("P", "Z"))
        else char
        for char in compatible
    )
    normalized = unicodedata.normalize("NFC", " ".join(normalized.split()))
    if not normalized:
        raise ReferenceContractError("reference alias must not be empty")
    return normalized


def _validated_manual_groups(
    groups: Sequence[ManualHeroGroup],
) -> tuple[tuple[ManualHeroGroup, tuple[str, ...]], ...]:
    if isinstance(groups, (str, bytes, bytearray)) or not isinstance(groups, Sequence):
        raise ReferenceContractError("manual hero groups must be an ordered sequence")

    result: list[tuple[ManualHeroGroup, tuple[str, ...]]] = []
    seen_ids: set[str] = set()
    alias_owner: dict[str, str] = {}
    for index, group in enumerate(groups):
        if not isinstance(group, ManualHeroGroup):
            raise ReferenceContractError(
                f"manual hero group {index} must be ManualHeroGroup"
            )
        group_id = group.group_id.strip() if isinstance(group.group_id, str) else ""
        if not group_id or group_id in seen_ids:
            raise ReferenceContractError(
                f"manual hero group {index} has an empty or duplicate group_id"
            )
        if (
            isinstance(group.aliases, (str, bytes, bytearray))
            or not isinstance(group.aliases, Sequence)
            or not group.aliases
        ):
            raise ReferenceContractError(
                f"manual hero group {group_id!r} must have at least one alias"
            )

        aliases: list[str] = []
        seen_here: set[str] = set()
        for raw_alias in group.aliases:
            alias = normalize_alias(raw_alias)
            if alias in seen_here:
                continue
            other_group = alias_owner.get(alias)
            if other_group is not None:
                raise ReferenceContractError(
                    f"manual alias {raw_alias!r} is duplicated by groups "
                    f"{other_group!r} and {group_id!r}"
                )
            seen_here.add(alias)
            alias_owner[alias] = group_id
            aliases.append(alias)

        seen_ids.add(group_id)
        result.append((ManualHeroGroup(group_id, tuple(group.aliases)), tuple(aliases)))
    return tuple(result)


def _validated_bible_characters(
    story_bible: Mapping[str, Any],
) -> tuple[dict[str, Any], ...]:
    if not isinstance(story_bible, Mapping):
        raise ReferenceContractError("story bible must be a mapping")
    rows = story_bible.get("characters")
    if isinstance(rows, (str, bytes, bytearray)) or not isinstance(rows, Sequence):
        raise ReferenceContractError("story bible characters must be an array")

    checked: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, raw in enumerate(rows):
        if not isinstance(raw, Mapping):
            raise ReferenceContractError(
                f"story bible character {index} must be a mapping"
            )
        character_id = raw.get("id")
        names = raw.get("names")
        role = raw.get("role")
        if not isinstance(character_id, str) or not character_id.strip():
            raise ReferenceContractError(
                f"story bible character {index} has no stable id"
            )
        if character_id in seen_ids:
            raise ReferenceContractError(
                f"story bible has duplicate character id {character_id!r}"
            )
        if (
            isinstance(names, (str, bytes, bytearray))
            or not isinstance(names, Sequence)
            or not names
        ):
            raise ReferenceContractError(
                f"story bible character {character_id!r} has no names"
            )
        normalized_names = tuple(dict.fromkeys(normalize_alias(name) for name in names))
        if role not in {"protagonist", "major", "minor"}:
            raise ReferenceContractError(
                f"story bible character {character_id!r} has invalid role"
            )
        checked.append(
            {
                "id": character_id,
                "names": tuple(names),
                "normalized_names": normalized_names,
                "role": role,
            }
        )
        seen_ids.add(character_id)
    return tuple(checked)


def resolve_manual_hero_groups(
    groups: Sequence[ManualHeroGroup],
    story_bible: Mapping[str, Any] | None,
    *,
    single_subject: bool = False,
) -> tuple[ManualGroupResolution, ...]:
    """Resolve manual groups to story characters without fuzzy guessing.

    Exact normalized aliases are authoritative whenever a story bible exists.
    An unmatched name is blocking even when only one major character exists:
    silently guessing that identity can attach another person's face.  Without
    a bible, one group is accepted only when the caller explicitly proves that
    the project has a single subject.
    """

    validated_groups = _validated_manual_groups(groups)
    if not validated_groups:
        return ()

    if story_bible is None:
        if len(validated_groups) != 1 or single_subject is not True:
            raise ReferenceContractError(
                "without a story bible exactly one manual group requires "
                "explicit single_subject=True"
            )
        group = validated_groups[0][0]
        return (
            ManualGroupResolution(
                group_id=group.group_id,
                character_id=None,
                primary_name=None,
                match_kind="explicit_single_subject",
            ),
        )

    characters = _validated_bible_characters(story_bible)
    alias_index: dict[str, set[str]] = {}
    by_id = {row["id"]: row for row in characters}
    for row in characters:
        for alias in row["normalized_names"]:
            alias_index.setdefault(alias, set()).add(row["id"])

    resolutions: list[ManualGroupResolution] = []
    resolved_character_ids: set[str] = set()
    unmatched: list[tuple[ManualHeroGroup, tuple[str, ...]]] = []

    for group, aliases in validated_groups:
        matched_ids: set[str] = set()
        ambiguous_aliases: list[str] = []
        for alias in aliases:
            owners = alias_index.get(alias, set())
            if len(owners) > 1:
                ambiguous_aliases.append(alias)
            matched_ids.update(owners)
        if ambiguous_aliases or len(matched_ids) > 1:
            raise ReferenceContractError(
                f"manual group {group.group_id!r} ambiguously matches multiple "
                "story-bible characters"
            )
        if not matched_ids:
            unmatched.append((group, aliases))
            continue

        character_id = next(iter(matched_ids))
        if character_id in resolved_character_ids:
            raise ReferenceContractError(
                f"multiple manual groups resolve to character {character_id!r}"
            )
        row = by_id[character_id]
        resolutions.append(
            ManualGroupResolution(
                group_id=group.group_id,
                character_id=character_id,
                primary_name=row["names"][0],
                match_kind="exact",
            )
        )
        resolved_character_ids.add(character_id)

    if unmatched:
        names = ", ".join(group.group_id for group, _aliases in unmatched)
        raise ReferenceContractError(
            "manual hero groups have no exact story-bible match: "
            f"{names}; rename each group to one of that character's canonical names"
        )

    order = {group.group_id: index for index, (group, _aliases) in enumerate(validated_groups)}
    resolutions.sort(key=lambda item: order[item.group_id])
    return tuple(resolutions)


def replace_named_reference_group(
    groups: Mapping[str, Sequence[str]],
    *,
    selected_name: str | None,
    new_name: str,
    sources: Sequence[str],
) -> dict[str, list[str]]:
    """Atomically add, rename, or replace one desktop manual-ref group.

    Normalized-name collisions are rejected instead of overwriting another
    group's photos.  Renaming preserves the selected group's dictionary order,
    and callers receive a new mapping so cancel/error paths leave state intact.
    """

    if not isinstance(groups, Mapping):
        raise ReferenceContractError("named reference groups must be a mapping")
    if selected_name is not None and selected_name not in groups:
        raise ReferenceContractError("selected reference group no longer exists")
    display_name = unicodedata.normalize("NFC", str(new_name or "").strip())
    if not display_name:
        raise ReferenceContractError("reference group name must not be empty")
    alias = normalize_alias(display_name)
    for existing_name in groups:
        if not isinstance(existing_name, str) or not existing_name.strip():
            raise ReferenceContractError("existing reference group name is invalid")
        if existing_name != selected_name and normalize_alias(existing_name) == alias:
            raise ReferenceContractError(
                f"reference group name conflicts with existing group {existing_name!r}"
            )
    if selected_name is None and len(groups) >= MAX_HERO_GROUPS:
        raise ReferenceContractError(
            f"project accepts at most {MAX_HERO_GROUPS} hero groups"
        )
    if (
        isinstance(sources, (str, bytes, bytearray))
        or not isinstance(sources, Sequence)
        or not 1 <= len(sources) <= MAX_HERO_ANGLES
        or any(not isinstance(path, str) or not path for path in sources)
    ):
        raise ReferenceContractError(
            f"hero reference requires 1..{MAX_HERO_ANGLES} source paths"
        )

    replacement = list(sources)
    result: dict[str, list[str]] = {}
    inserted = False
    for existing_name, existing_sources in groups.items():
        if existing_name == selected_name:
            result[display_name] = replacement
            inserted = True
        else:
            result[existing_name] = list(existing_sources)
    if not inserted:
        result[display_name] = replacement
    return result


def _positive_limit(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ReferenceContractError(f"{name} must be a positive integer")
    return value


def _normalize_declared_format(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ReferenceContractError("declared image format must be a non-empty string")
    normalized = _DECLARED_FORMATS.get(value.strip().casefold())
    if normalized is None:
        raise ReferenceContractError(
            f"declared image format {value!r} is not JPEG, PNG, or WEBP"
        )
    return normalized


def _rgb_without_transparency(image: Image.Image) -> Image.Image:
    if image.mode in {"RGBA", "LA"} or (
        image.mode == "P" and "transparency" in image.info
    ):
        rgba = image.convert("RGBA")
        background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        try:
            background.alpha_composite(rgba)
            return background.convert("RGB")
        finally:
            rgba.close()
            background.close()
    return image.convert("RGB")


def _encode_jpeg(image: Image.Image) -> bytes:
    output = BytesIO()
    image.save(
        output,
        format="JPEG",
        quality=95,
        subsampling=0,
        optimize=False,
        progressive=False,
    )
    return output.getvalue()


def canonicalize_reference_image(
    payload: bytes | bytearray | memoryview,
    *,
    declared_format: str | None = None,
    max_bytes: int = MAX_REFERENCE_BYTES,
    max_pixels: int = MAX_REFERENCE_PIXELS,
    max_dimension: int = MAX_REFERENCE_DIMENSION,
) -> CanonicalImage:
    """Decode an upload and return deterministic JPEG pixels plus hashes."""

    max_bytes = _positive_limit(max_bytes, "max_bytes")
    max_pixels = _positive_limit(max_pixels, "max_pixels")
    max_dimension = _positive_limit(max_dimension, "max_dimension")
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise ReferenceContractError("reference image payload must be bytes")
    raw = bytes(payload)
    if not raw:
        raise ReferenceContractError("reference image payload is empty")
    if len(raw) > max_bytes:
        raise ReferenceContractError(
            f"reference image exceeds the {max_bytes}-byte upload limit"
        )

    expected_format = _normalize_declared_format(declared_format)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(raw)) as probe:
                source_format = str(probe.format or "").upper()
                width, height = probe.size
                frames = int(getattr(probe, "n_frames", 1))
                if source_format not in _ALLOWED_IMAGE_FORMATS:
                    raise ReferenceContractError(
                        "reference image must decode as JPEG, PNG, or WEBP"
                    )
                if expected_format is not None and expected_format != source_format:
                    raise ReferenceContractError(
                        f"declared {expected_format} image actually decodes as "
                        f"{source_format}"
                    )
                if frames != 1:
                    raise ReferenceContractError(
                        "animated or multi-frame reference images are not supported"
                    )
                if width <= 0 or height <= 0:
                    raise ReferenceContractError("reference image has invalid dimensions")
                if width > max_dimension or height > max_dimension:
                    raise ReferenceContractError(
                        f"reference image dimension exceeds {max_dimension} pixels"
                    )
                if width * height > max_pixels:
                    raise ReferenceContractError(
                        f"reference image exceeds the {max_pixels}-pixel limit"
                    )
                probe.verify()

            # ``verify`` intentionally invalidates its decoder, so reopen and
            # force a full pixel decode before accepting the upload.
            with Image.open(BytesIO(raw)) as decoded:
                decoded.load()
                transposed = ImageOps.exif_transpose(decoded)
                rgb = _rgb_without_transparency(transposed)
                try:
                    jpeg_bytes = _encode_jpeg(rgb)
                    canonical_width, canonical_height = rgb.size
                finally:
                    rgb.close()
                    if transposed is not decoded:
                        transposed.close()
    except ReferenceContractError:
        raise
    except (
        UnidentifiedImageError,
        OSError,
        SyntaxError,
        ValueError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        raise ReferenceContractError(
            "reference image is corrupt, spoofed, or unsafe to decode"
        ) from exc

    return CanonicalImage(
        jpeg_bytes=jpeg_bytes,
        sha256=hashlib.sha256(jpeg_bytes).hexdigest(),
        source_sha256=hashlib.sha256(raw).hexdigest(),
        width=canonical_width,
        height=canonical_height,
        source_format=source_format,
    )


def _verified_managed_reference_source(
    source: str | os.PathLike[str],
    *,
    max_bytes: int,
    max_pixels: int,
    max_dimension: int,
) -> CanonicalImage | None:
    """Return exact managed JPEG bytes, or ``None`` for an external source.

    Re-decoding/re-encoding an already canonical JPEG is lossy and changes its
    content hash on every project clone.  Exact-byte reuse is allowed only for a
    non-symlinked ``_ASSETS/reference_uploads`` file whose bytes match its
    content-addressed name. This also keeps deliberately retained history/orphan
    copies recoverable after their current binding was removed.
    """

    raw_path = Path(source).absolute()
    resolved = raw_path.resolve()
    if resolved.parent.name != "reference_uploads" or resolved.parent.parent.name != "_ASSETS":
        return None
    if raw_path.is_symlink() or raw_path.parent.is_symlink() or raw_path.parent.parent.is_symlink():
        raise ReferenceContractError(
            "managed reference source must not use symlink paths"
        )
    match = re.fullmatch(
        r"[a-z0-9][a-z0-9_-]{0,63}-([0-9a-f]{64})\.jpg",
        resolved.name,
    )
    if match is None:
        return None
    payload = _read_regular_file_bounded(
        resolved,
        max_bytes=max_bytes,
        label=f"managed reference source {resolved.name}",
    )
    if not payload or len(payload) > max_bytes:
        raise ReferenceContractError("managed reference source exceeds byte limits")
    digest = hashlib.sha256(payload).hexdigest()
    if digest != match.group(1):
        raise ReferenceContractError(
            "managed reference source does not match its content-addressed name"
        )
    # Full decode validates safety, while the returned bytes remain exact.
    checked = canonicalize_reference_image(
        payload,
        declared_format="image/jpeg",
        max_bytes=max_bytes,
        max_pixels=max_pixels,
        max_dimension=max_dimension,
    )
    return CanonicalImage(
        jpeg_bytes=payload,
        sha256=digest,
        source_sha256=digest,
        width=checked.width,
        height=checked.height,
        source_format="JPEG",
    )


def _read_reference_source(
    source: CanonicalImage | bytes | bytearray | memoryview | str | os.PathLike[str],
    *,
    max_bytes: int,
    max_pixels: int,
    max_dimension: int,
) -> CanonicalImage:
    if isinstance(source, CanonicalImage):
        if hashlib.sha256(source.jpeg_bytes).hexdigest() != source.sha256:
            raise ReferenceContractError("canonical reference image hash is invalid")
        if not _SHA256_RE.fullmatch(source.source_sha256):
            raise ReferenceContractError("canonical reference source hash is invalid")
        return source
    if isinstance(source, (str, os.PathLike)):
        path = Path(source)
        managed = _verified_managed_reference_source(
            path,
            max_bytes=max_bytes,
            max_pixels=max_pixels,
            max_dimension=max_dimension,
        )
        if managed is not None:
            return managed
        try:
            with path.open("rb") as handle:
                payload = handle.read(max_bytes + 1)
        except OSError as exc:
            raise ReferenceContractError(
                f"cannot read reference image {path}"
            ) from exc
    else:
        payload = source
    return canonicalize_reference_image(
        payload,
        max_bytes=max_bytes,
        max_pixels=max_pixels,
        max_dimension=max_dimension,
    )


def _lexically_exists(path: Path) -> bool:
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ReferenceContractError(f"cannot inspect reference path {path.name}") from exc
    return True


def _fsync_directory(path: Path) -> None:
    """Make directory-entry changes durable on POSIX filesystems.

    Windows does not expose a portable Python directory-fsync primitive, so its
    durability boundary remains the replace/file flush implemented by
    ``_atomic_write``.  On POSIX, however, silently accepting an open, fsync, or
    close error would let a successful API return precede a durable directory
    entry.  Every such error is therefore blocking.
    """

    if os.name != "posix":
        return

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ReferenceContractError(
            f"cannot open reference directory for durability: {path}"
        ) from exc

    operation_failure: tuple[str, OSError] | None = None
    contract_failure: ReferenceContractError | None = None
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISDIR(opened.st_mode):
            contract_failure = ReferenceContractError(
                f"reference durability path is not a directory: {path}"
            )
        else:
            os.fsync(descriptor)
    except OSError as exc:
        operation_failure = ("fsync", exc)

    close_failure: OSError | None = None
    try:
        os.close(descriptor)
    except OSError as exc:
        close_failure = exc

    if contract_failure is not None:
        raise contract_failure
    if operation_failure is not None:
        operation, cause = operation_failure
        raise ReferenceContractError(
            f"cannot {operation} reference directory: {path}"
        ) from cause
    if close_failure is not None:
        raise ReferenceContractError(
            f"cannot close reference directory after fsync: {path}"
        ) from close_failure


def _atomic_write(
    path: Path,
    payload: bytes,
    *,
    replace_invalid_target: bool = False,
) -> None:
    if (
        (_is_link_or_reparse(path) and not replace_invalid_target)
        or _is_link_or_reparse(path.parent)
    ):
        raise ReferenceContractError("atomic reference write must not follow symlinks")
    _ensure_durable_directory(path.parent, parents=True)
    if _is_link_or_reparse(path.parent):
        raise ReferenceContractError("atomic reference write must not follow symlinks")
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
        temporary_name = None
        # The file fsync above protects its contents; fsync the directory too so
        # the rename itself survives a power loss. A directory-fsync failure is
        # a failed write, never a successful but potentially disappearing ref.
        _fsync_directory(path.parent)
    finally:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


def _is_link_or_reparse(path: Path) -> bool:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(info.st_mode):
        return True
    # Windows junctions/reparse points are not consistently reported by
    # Path.is_symlink(), but must not be trusted as project-owned storage.
    attributes = getattr(info, "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _ensure_durable_directory(path: Path, *, parents: bool) -> bool:
    """Ensure one non-linked directory and durably publish new path entries.

    The explicit mkdir loop lets us fsync every parent that receives a newly
    created child, including intermediate parents.  If another process wins a
    creation race after our missing-path observation, syncing the parent is
    harmless and closes the same durability uncertainty before we proceed.
    Existing directories are also parent-synced on create paths so a retry
    after an earlier fsync error cannot turn that failed attempt into a false
    success.
    """

    path = Path(path)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        info = None
    except OSError as exc:
        raise ReferenceContractError(
            f"cannot inspect reference directory: {path}"
        ) from exc

    if info is not None:
        if _is_link_or_reparse(path) or not stat.S_ISDIR(info.st_mode):
            raise ReferenceContractError(
                f"reference storage path must be a non-symlink directory: {path}"
            )
        _fsync_directory(path.parent)
        return False

    try:
        path.mkdir()
    except FileNotFoundError as exc:
        if not parents or path.parent == path:
            raise ReferenceContractError(
                f"cannot create reference directory: {path}"
            ) from exc
        _ensure_durable_directory(path.parent, parents=True)
        return _ensure_durable_directory(path, parents=False)
    except FileExistsError:
        # A concurrent creator won after lstat. Validate the node and make the
        # parent entry durable before treating the directory as available.
        try:
            raced = os.lstat(path)
        except OSError as exc:
            raise ReferenceContractError(
                f"cannot inspect raced reference directory: {path}"
            ) from exc
        if _is_link_or_reparse(path) or not stat.S_ISDIR(raced.st_mode):
            raise ReferenceContractError(
                f"reference storage path must be a non-symlink directory: {path}"
            )
        _fsync_directory(path.parent)
        return False
    except OSError as exc:
        raise ReferenceContractError(
            f"cannot create reference directory: {path}"
        ) from exc

    try:
        created = os.lstat(path)
    except OSError as exc:
        raise ReferenceContractError(
            f"cannot verify created reference directory: {path}"
        ) from exc
    if _is_link_or_reparse(path) or not stat.S_ISDIR(created.st_mode):
        raise ReferenceContractError(
            f"created reference storage is not a safe directory: {path}"
        )
    _fsync_directory(path.parent)
    return True


def _read_regular_file_bounded(
    path: Path,
    *,
    max_bytes: int,
    label: str,
) -> bytes:
    """Read a regular file without following links and with a hard byte cap."""

    try:
        info = os.lstat(path)
    except OSError as exc:
        raise ReferenceContractError(f"cannot read {label}") from exc
    if _is_link_or_reparse(path) or not stat.S_ISREG(info.st_mode):
        raise ReferenceContractError(f"{label} must be a regular non-symlink file")
    if info.st_size < 0 or info.st_size > max_bytes:
        raise ReferenceContractError(f"{label} exceeds the {max_bytes}-byte limit")

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ReferenceContractError(f"cannot open {label}") from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or opened.st_size > max_bytes:
            raise ReferenceContractError(f"{label} is not a bounded regular file")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
    finally:
        os.close(descriptor)
    if len(payload) > max_bytes:
        raise ReferenceContractError(f"{label} exceeds the {max_bytes}-byte limit")
    return payload


def _confined_directory(
    parent: Path,
    name: str,
    *,
    project_root: Path,
    create: bool,
) -> Path:
    if not name or Path(name).name != name:
        raise ReferenceContractError("reference storage directory name is invalid")
    candidate = parent / name
    if _is_link_or_reparse(candidate):
        raise ReferenceContractError("reference storage must not use symlink directories")
    if create:
        _ensure_durable_directory(candidate, parents=False)
    resolved = candidate.resolve()
    try:
        resolved.relative_to(project_root)
    except ValueError as exc:
        raise ReferenceContractError(
            "reference storage escapes the project directory"
        ) from exc
    return resolved


@contextmanager
def _exclusive_project_state_lock(lock_path: Path):
    """Serialize desktop state commits across threads and app processes."""

    if _is_link_or_reparse(lock_path) or _is_link_or_reparse(lock_path.parent):
        raise ReferenceContractError("reference state lock must not use symlinks")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    with _PROJECT_STATE_THREAD_LOCK:
        try:
            descriptor = os.open(lock_path, flags, 0o600)
        except OSError as exc:
            raise ReferenceContractError("cannot open reference state lock") from exc
        handle = os.fdopen(descriptor, "r+b", closefd=True)
        try:
            if os.name == "nt":
                import msvcrt

                if os.fstat(handle.fileno()).st_size == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if os.name == "nt":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _persist_artifact(
    image: CanonicalImage,
    output_dir: str | os.PathLike[str],
    *,
    kind: str,
    source_hashes: Sequence[str],
) -> ReferenceArtifact:
    if not isinstance(kind, str) or _ARTIFACT_KIND_RE.fullmatch(kind.strip()) is None:
        raise ReferenceContractError(
            "reference artifact kind must be a safe lowercase slug"
        )
    hashes = tuple(source_hashes)
    if not hashes or any(_SHA256_RE.fullmatch(value) is None for value in hashes):
        raise ReferenceContractError("reference artifact source hashes are invalid")
    output = Path(output_dir)
    target = output / f"{kind.strip()}-{image.sha256}.jpg"
    if target.exists():
        existing = _read_regular_file_bounded(
            target,
            max_bytes=MAX_REFERENCE_BYTES,
            label="content-addressed reference artifact",
        )
        if hashlib.sha256(existing).hexdigest() != image.sha256:
            raise ReferenceContractError(
                "content-addressed reference artifact was modified"
            )
    else:
        _atomic_write(target, image.jpeg_bytes)

    fingerprint = canonical_fingerprint(
        {
            "schema_version": CONTRACT_SCHEMA_VERSION,
            "kind": kind.strip(),
            "sha256": image.sha256,
            "source_hashes": list(hashes),
            "width": image.width,
            "height": image.height,
        }
    )
    return ReferenceArtifact(
        path=str(target),
        sha256=image.sha256,
        width=image.width,
        height=image.height,
        source_hashes=hashes,
        kind=kind.strip(),
        fingerprint=fingerprint,
    )


def _project_assets_root(project_root: Path, *, create: bool) -> Path:
    assets = project_root / "_ASSETS"
    if _is_link_or_reparse(assets):
        raise ReferenceContractError("project _ASSETS directory must not be a symlink")
    if create:
        _ensure_durable_directory(project_root, parents=True)
        _ensure_durable_directory(assets, parents=False)
    resolved = assets.resolve()
    try:
        resolved.relative_to(project_root)
    except ValueError as exc:
        raise ReferenceContractError(
            "project _ASSETS storage escapes the project directory"
        ) from exc
    return resolved


def _project_reference_uploads_root(project_root: Path, *, create: bool = False) -> Path:
    assets = _project_assets_root(project_root, create=create)
    return _confined_directory(
        assets,
        "reference_uploads",
        project_root=project_root,
        create=create,
    )


def persist_reference_artifact(
    source: CanonicalImage | bytes | bytearray | memoryview | str | os.PathLike[str],
    output_dir: str | os.PathLike[str],
    *,
    kind: str = "reference",
    max_bytes: int = MAX_REFERENCE_BYTES,
    max_pixels: int = MAX_REFERENCE_PIXELS,
    max_dimension: int = MAX_REFERENCE_DIMENSION,
) -> ReferenceArtifact:
    """Canonicalize and atomically persist one generic content-addressed ref."""

    canonical = _read_reference_source(
        source,
        max_bytes=_positive_limit(max_bytes, "max_bytes"),
        max_pixels=_positive_limit(max_pixels, "max_pixels"),
        max_dimension=_positive_limit(max_dimension, "max_dimension"),
    )
    return _persist_artifact(
        canonical,
        output_dir,
        kind=kind,
        source_hashes=(canonical.source_sha256,),
    )


def import_reference_sources(
    sources: Sequence[
        CanonicalImage | bytes | bytearray | memoryview | str | os.PathLike[str]
    ],
    project_dir: str | os.PathLike[str],
    *,
    kind: str,
    max_sources: int,
) -> tuple[ReferenceArtifact, ...]:
    """Validate a selection, then copy it into project-owned storage.

    External paths are import sources only. Every returned path lives below
    ``<project>/_ASSETS/reference_uploads`` and is named by canonical pixel
    content. Validation and duplicate detection finish before caller state can
    change; an interrupted content-addressed write is merely an unreferenced
    orphan, never a partly accepted reference selection.
    """

    if isinstance(sources, (str, bytes, bytearray, memoryview)) or not isinstance(
        sources, Sequence
    ):
        raise ReferenceContractError("reference sources must be an ordered sequence")
    limit = _positive_limit(max_sources, "max_sources")
    if not 1 <= len(sources) <= limit:
        raise ReferenceContractError(
            f"reference source selection must contain 1..{limit} images"
        )
    if not isinstance(kind, str) or _ARTIFACT_KIND_RE.fullmatch(kind.strip()) is None:
        raise ReferenceContractError(
            "reference import kind must be a safe lowercase slug"
        )

    # A bad final file must not leave the UI pointing at a partly accepted batch.
    canonical_images = tuple(
        _read_reference_source(
            source,
            max_bytes=MAX_REFERENCE_BYTES,
            max_pixels=MAX_REFERENCE_PIXELS,
            max_dimension=MAX_REFERENCE_DIMENSION,
        )
        for source in sources
    )
    seen_pixels: set[str] = set()
    for image in canonical_images:
        if image.sha256 in seen_pixels:
            raise ReferenceContractError(
                "reference source selection contains duplicate image content"
            )
        seen_pixels.add(image.sha256)

    project_root = Path(project_dir).resolve()
    _ensure_durable_directory(project_root, parents=True)
    uploads_real = _project_reference_uploads_root(project_root, create=True)

    return tuple(
        _persist_artifact(
            image,
            uploads_real,
            kind=kind.strip(),
            source_hashes=(image.source_sha256,),
        )
        for image in canonical_images
    )


def _project_reference_source_row(
    project_root: Path,
    source_path: str | os.PathLike[str],
) -> dict[str, str]:
    uploads_root = _project_reference_uploads_root(project_root)
    raw_path = Path(source_path).absolute()
    if _is_link_or_reparse(raw_path):
        raise ReferenceContractError(
            f"project-owned reference source {source_path!s} must not be a symlink"
        )
    path = raw_path.resolve()
    try:
        relative = path.relative_to(project_root)
        path.relative_to(uploads_root)
    except ValueError as exc:
        raise ReferenceContractError(
            f"reference source {source_path!s} is not project-owned"
        ) from exc
    payload = _read_regular_file_bounded(
        path,
        max_bytes=MAX_REFERENCE_BYTES,
        label=f"project-owned reference source {path.name}",
    )
    canonicalize_reference_image(payload)
    digest = hashlib.sha256(payload).hexdigest()
    filename_match = re.fullmatch(
        r"[a-z0-9][a-z0-9_-]{0,63}-([0-9a-f]{64})\.jpg",
        path.name,
    )
    if filename_match is None or filename_match.group(1) != digest:
        raise ReferenceContractError(
            f"project-owned reference source {source_path!s} does not match its content-addressed name"
        )
    return {"path": relative.as_posix(), "sha256": digest}


_REFERENCE_COMMIT_FILE_RE = re.compile(
    r"^(?P<sequence>[0-9]{20})-(?P<commit>[0-9a-f]{64})\.json$"
)


def _read_reference_commit(path: Path) -> dict[str, Any]:
    match = _REFERENCE_COMMIT_FILE_RE.fullmatch(path.name)
    if match is None:
        raise ReferenceContractError("reference commit filename is invalid")
    payload = _read_regular_file_bounded(
        path,
        max_bytes=MAX_REFERENCE_COMMIT_BYTES,
        label="immutable reference state commit",
    )
    try:
        row = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ReferenceContractError("reference state commit is unreadable") from exc
    expected_keys = {
        "schema_version",
        "sequence",
        "revision_id",
        "previous_commit_id",
        "committed_at_ns",
        "commit_id",
    }
    if not isinstance(row, dict) or set(row) != expected_keys:
        raise ReferenceContractError("reference state commit schema is invalid")
    sequence = row.get("sequence")
    revision_id = row.get("revision_id")
    previous_commit_id = row.get("previous_commit_id")
    committed_at_ns = row.get("committed_at_ns")
    commit_id = row.get("commit_id")
    if row.get("schema_version") != PROJECT_REFERENCE_STATE_SCHEMA_VERSION:
        raise ReferenceContractError("reference state commit version is unsupported")
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
        raise ReferenceContractError("reference state commit sequence is invalid")
    if int(match.group("sequence")) != sequence:
        raise ReferenceContractError("reference state commit sequence/name mismatch")
    if not isinstance(revision_id, str) or _SHA256_RE.fullmatch(revision_id) is None:
        raise ReferenceContractError("reference state commit revision is invalid")
    if previous_commit_id is not None and (
        not isinstance(previous_commit_id, str)
        or _SHA256_RE.fullmatch(previous_commit_id) is None
    ):
        raise ReferenceContractError("reference state previous commit is invalid")
    if (
        not isinstance(committed_at_ns, int)
        or isinstance(committed_at_ns, bool)
        or committed_at_ns < 1
    ):
        raise ReferenceContractError("reference state commit timestamp is invalid")
    if not isinstance(commit_id, str) or _SHA256_RE.fullmatch(commit_id) is None:
        raise ReferenceContractError("reference state commit id is invalid")
    unsigned = {key: row[key] for key in expected_keys if key != "commit_id"}
    calculated = canonical_fingerprint(unsigned)
    if calculated != commit_id or match.group("commit") != commit_id:
        raise ReferenceContractError("reference state commit fingerprint changed")
    return row


def _list_reference_commits(commits_root: Path) -> list[dict[str, Any]]:
    if not commits_root.exists():
        return []
    rows: list[dict[str, Any]] = []
    seen_sequences: set[int] = set()
    seen_commits: set[str] = set()
    for path in commits_root.iterdir():
        if path.name.startswith("."):
            continue
        row = _read_reference_commit(path)
        if row["sequence"] in seen_sequences or row["commit_id"] in seen_commits:
            raise ReferenceContractError("reference state commit history is duplicated")
        seen_sequences.add(row["sequence"])
        seen_commits.add(row["commit_id"])
        rows.append(row)
    rows.sort(key=lambda item: item["sequence"])
    return rows


def _reference_commit_append_cursor(
    commits_root: Path,
) -> tuple[int, dict[str, Any] | None]:
    """Find a collision-free sequence while keeping damaged history recoverable."""

    max_sequence = 0
    valid: list[dict[str, Any]] = []
    if not commits_root.exists():
        return max_sequence, None
    for path in commits_root.iterdir():
        if path.name.startswith("."):
            continue
        match = _REFERENCE_COMMIT_FILE_RE.fullmatch(path.name)
        if match is not None:
            max_sequence = max(max_sequence, int(match.group("sequence")))
        try:
            valid.append(_read_reference_commit(path))
        except ReferenceContractError as exc:
            # A damaged historical record is surfaced by the revision-list UI,
            # but must not permanently prevent a verified current state from
            # receiving a new recoverable commit.
            warnings.warn(
                f"ignored damaged historical reference commit {path.name}: {exc}",
                RuntimeWarning,
                stacklevel=2,
            )
    valid.sort(key=lambda item: item["sequence"])
    return max_sequence, valid[-1] if valid else None


def _write_reference_commit(
    commits_root: Path,
    *,
    sequence: int,
    revision_id: str,
    previous_commit_id: str | None,
) -> dict[str, Any]:
    unsigned = {
        "schema_version": PROJECT_REFERENCE_STATE_SCHEMA_VERSION,
        "sequence": sequence,
        "revision_id": revision_id,
        "previous_commit_id": previous_commit_id,
        "committed_at_ns": time.time_ns(),
    }
    row = {**unsigned, "commit_id": canonical_fingerprint(unsigned)}
    path = commits_root / f"{sequence:020d}-{row['commit_id']}.json"
    payload = canonical_json_bytes(row)
    if path.exists():
        if _read_regular_file_bounded(
            path,
            max_bytes=MAX_REFERENCE_COMMIT_BYTES,
            label="immutable reference state commit",
        ) != payload:
            raise ReferenceContractError("immutable reference state commit was modified")
    else:
        _atomic_write(path, payload)
    return row


def _reference_state_marker_token(target: Path, pointer: Path) -> str:
    rows: list[dict[str, Any]] = []
    for label, path, limit in (
        ("pointer", pointer, MAX_REFERENCE_COMMIT_BYTES),
        ("projection", target, MAX_REFERENCE_STATE_BYTES),
    ):
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            rows.append({"label": label, "state": "missing"})
            continue
        row: dict[str, Any] = {
            "label": label,
            "mode": int(info.st_mode),
            "size": int(info.st_size),
            "mtime_ns": int(getattr(info, "st_mtime_ns", 0)),
        }
        if (
            not _is_link_or_reparse(path)
            and stat.S_ISREG(info.st_mode)
            and 0 <= info.st_size <= limit
        ):
            try:
                row["sha256"] = hashlib.sha256(
                    _read_regular_file_bounded(path, max_bytes=limit, label=label)
                ).hexdigest()
            except ReferenceContractError as exc:
                row["read_error"] = str(exc)
        rows.append(row)
    return canonical_fingerprint({"state_markers": rows})


def _validate_reference_projection_node(target: Path) -> None:
    """Require the compatibility projection to be a replaceable local file.

    The immutable revision selected by the pointer remains the content
    authority, so a stale/corrupt *regular* projection cannot change the
    loaded reference state.  A missing, linked, or non-regular projection is
    different: ordinary saves cannot safely replace it and the user would be
    locked out of the recovery path.  Surface that condition as damaged state
    so the explicit, CAS-protected restore operation can repair it.
    """

    try:
        info = os.lstat(target)
    except FileNotFoundError as exc:
        raise ReferenceContractError(
            "desktop reference compatibility projection is missing"
        ) from exc
    except OSError as exc:
        raise ReferenceContractError(
            "cannot inspect desktop reference compatibility projection"
        ) from exc
    if _is_link_or_reparse(target) or not stat.S_ISREG(info.st_mode):
        raise ReferenceContractError(
            "desktop reference compatibility projection must be a regular "
            "non-symlink file"
        )


def save_project_reference_state(
    project_dir: str | os.PathLike[str],
    manual_groups: Mapping[str, Sequence[str | os.PathLike[str]]],
    style_sources: Sequence[str | os.PathLike[str]],
    *,
    expected_revision: str | None = None,
    expected_commit_id: str | None = None,
) -> Path:
    """Durably commit a desktop mapping through an immutable revision.

    ``expected_revision`` enables compare-and-swap for UI clients. Pass ``""``
    to assert that no state exists yet. Omitting it preserves compatibility for
    one-shot importers, while the filesystem lock still prevents torn commits.
    """

    if not isinstance(manual_groups, Mapping):
        raise ReferenceContractError("manual reference groups must be a mapping")
    if len(manual_groups) > HERO_GROUPS_MAX:
        raise ReferenceContractError(
            f"manual reference groups exceed the {HERO_GROUPS_MAX}-group limit"
        )
    if isinstance(style_sources, (str, bytes, bytearray)) or not isinstance(
        style_sources, Sequence
    ):
        raise ReferenceContractError("style reference sources must be a sequence")
    if len(style_sources) > STYLE_SOURCE_MAX:
        raise ReferenceContractError(
            f"style references exceed the {STYLE_SOURCE_MAX}-source limit"
        )

    project_root = Path(project_dir).resolve()
    _ensure_durable_directory(project_root, parents=True)
    rows: list[dict[str, Any]] = []
    aliases: dict[str, str] = {}
    digest_owner: dict[str, str] = {}
    for raw_name, raw_sources in manual_groups.items():
        if not isinstance(raw_name, str) or not raw_name.strip():
            raise ReferenceContractError("manual reference group name must not be empty")
        name = unicodedata.normalize("NFC", raw_name.strip())
        alias = normalize_alias(name)
        if alias in aliases:
            raise ReferenceContractError(
                f"manual reference group {name!r} conflicts with {aliases[alias]!r}"
            )
        aliases[alias] = name
        if isinstance(raw_sources, (str, bytes, bytearray)) or not isinstance(
            raw_sources, Sequence
        ) or not 1 <= len(raw_sources) <= HERO_ANGLES_MAX:
            raise ReferenceContractError(
                f"manual reference group {name!r} must contain 1..{HERO_ANGLES_MAX} images"
            )
        source_rows = [
            _project_reference_source_row(project_root, source)
            for source in raw_sources
        ]
        for source_row in source_rows:
            digest = source_row["sha256"]
            previous = digest_owner.get(digest)
            if previous is not None:
                raise ReferenceContractError(
                    f"the same reference image is assigned to both {previous!r} and {name!r}"
                )
            digest_owner[digest] = name
        rows.append({"name": name, "sources": source_rows})

    style_rows = [
        _project_reference_source_row(project_root, source)
        for source in style_sources
    ]
    for source_row in style_rows:
        digest = source_row["sha256"]
        previous = digest_owner.get(digest)
        if previous is not None:
            raise ReferenceContractError(
                f"the same reference image is assigned to both {previous!r} and 'style'"
            )
        digest_owner[digest] = "style"

    payload = canonical_json_bytes(
        {
            "schema_version": PROJECT_REFERENCE_STATE_SCHEMA_VERSION,
            "manual_groups": rows,
            "style_sources": style_rows,
        }
    )
    assets_root = _project_assets_root(project_root, create=True)
    target = assets_root / PROJECT_REFERENCE_STATE_FILE
    pointer = assets_root / PROJECT_REFERENCE_STATE_POINTER_FILE
    history_root = _confined_directory(
        assets_root,
        PROJECT_REFERENCE_STATE_HISTORY_DIR,
        project_root=project_root,
        create=True,
    )
    commits_root = _confined_directory(
        assets_root,
        PROJECT_REFERENCE_STATE_COMMITS_DIR,
        project_root=project_root,
        create=True,
    )
    lock_path = assets_root / ".desktop_references.lock"
    revision_id = hashlib.sha256(payload).hexdigest()
    revision_path = history_root / f"{revision_id}.json"

    if expected_revision is not None and expected_revision != "":
        if _SHA256_RE.fullmatch(expected_revision) is None:
            raise ReferenceContractError("expected reference revision is invalid")
    if expected_commit_id is not None and expected_commit_id != "":
        if _SHA256_RE.fullmatch(expected_commit_id) is None:
            raise ReferenceContractError("expected reference commit is invalid")

    with _exclusive_project_state_lock(lock_path):
        (
            current_revision,
            _current_payload,
            current_commit_id,
            current_commit_sequence,
        ) = _read_current_state_payload(
            project_root,
            assets_root,
            target,
            pointer,
            history_root,
            commits_root,
        )
        if _current_payload is not None:
            # Ordinary edits must never overwrite an unreadable mapping. The
            # explicit restore API is the only operation allowed to repair a
            # damaged commit marker/current revision.
            _decode_project_reference_state(project_root, _current_payload)
        expected = None if expected_revision == "" else expected_revision
        if expected_revision is not None and current_revision != expected:
            raise ReferenceContractError(
                "reference state changed in another app instance; reload before saving"
            )
        expected_commit = None if expected_commit_id == "" else expected_commit_id
        if expected_commit_id is not None and current_commit_id != expected_commit:
            raise ReferenceContractError(
                "reference state commit changed in another app instance; reload before saving"
            )

        if current_revision is not None and _current_payload is not None:
            current_revision_path = history_root / f"{current_revision}.json"
            if current_revision_path.exists():
                archived_current = _read_regular_file_bounded(
                    current_revision_path,
                    max_bytes=MAX_REFERENCE_STATE_BYTES,
                    label="immutable current reference state revision",
                )
                if archived_current != _current_payload:
                    raise ReferenceContractError(
                        "immutable current reference state revision was modified"
                    )
            else:
                _atomic_write(current_revision_path, _current_payload)

        # Re-read every content-addressed source after acquiring the lock. This
        # closes the validation/commit gap if a managed file was replaced while
        # a UI edit was being prepared.
        for group in rows:
            for source_row in group["sources"]:
                actual_row = _project_reference_source_row(
                    project_root, project_root / source_row["path"]
                )
                if actual_row != source_row:
                    raise ReferenceContractError(
                        "manual reference changed before state commit"
                    )
        for source_row in style_rows:
            actual_row = _project_reference_source_row(
                project_root, project_root / source_row["path"]
            )
            if actual_row != source_row:
                raise ReferenceContractError("style reference changed before state commit")

        if revision_path.exists():
            existing = _read_regular_file_bounded(
                revision_path,
                max_bytes=MAX_REFERENCE_STATE_BYTES,
                label="immutable reference state revision",
            )
            if existing != payload:
                raise ReferenceContractError(
                    "immutable reference state revision was modified"
                )
        else:
            _atomic_write(revision_path, payload)

        max_sequence, last_valid_commit = _reference_commit_append_cursor(
            commits_root
        )
        next_sequence = max_sequence + 1
        active_commit_id = current_commit_id
        active_commit_sequence = current_commit_sequence
        if current_revision is not None and current_commit_id is None:
            baseline = _write_reference_commit(
                commits_root,
                sequence=next_sequence,
                revision_id=current_revision,
                previous_commit_id=(
                    last_valid_commit["commit_id"]
                    if last_valid_commit is not None else None
                ),
            )
            active_commit_id = baseline["commit_id"]
            active_commit_sequence = baseline["sequence"]
            next_sequence += 1
        if current_revision != revision_id or active_commit_id is None:
            committed = _write_reference_commit(
                commits_root,
                sequence=next_sequence,
                revision_id=revision_id,
                previous_commit_id=active_commit_id,
            )
            active_commit_id = committed["commit_id"]
            active_commit_sequence = committed["sequence"]

        # The legacy projection remains for compatibility; the small pointer is
        # the authoritative commit point and is written last.
        _atomic_write(target, payload)
        _atomic_write(
            pointer,
            canonical_json_bytes(
                {
                    "schema_version": PROJECT_REFERENCE_STATE_SCHEMA_VERSION,
                    "revision_id": revision_id,
                    "commit_id": active_commit_id,
                    "commit_sequence": active_commit_sequence,
                }
            ),
        )
    return target


def _read_current_state_payload(
    project_root: Path,
    assets_root: Path,
    target: Path,
    pointer: Path,
    history_root: Path,
    commits_root: Path,
) -> tuple[str | None, bytes | None, str | None, int | None]:
    del project_root, assets_root  # documented arguments make trust boundaries explicit
    if _lexically_exists(pointer):
        pointer_payload = _read_regular_file_bounded(
            pointer,
            max_bytes=4096,
            label="reference state pointer",
        )
        try:
            pointer_data = json.loads(pointer_payload.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ReferenceContractError("reference state pointer is unreadable") from exc
        legacy_keys = {"schema_version", "revision_id"}
        journal_keys = {
            "schema_version", "revision_id", "commit_id", "commit_sequence"
        }
        if not isinstance(pointer_data, dict) or frozenset(pointer_data) not in {
            frozenset(legacy_keys), frozenset(journal_keys)
        }:
            raise ReferenceContractError("reference state pointer schema is invalid")
        if pointer_data.get("schema_version") != PROJECT_REFERENCE_STATE_SCHEMA_VERSION:
            raise ReferenceContractError("reference state pointer version is unsupported")
        revision_id = pointer_data.get("revision_id")
        if not isinstance(revision_id, str) or _SHA256_RE.fullmatch(revision_id) is None:
            raise ReferenceContractError("reference state pointer revision is invalid")
        revision_payload = _read_regular_file_bounded(
            history_root / f"{revision_id}.json",
            max_bytes=MAX_REFERENCE_STATE_BYTES,
            label="current immutable reference state revision",
        )
        if hashlib.sha256(revision_payload).hexdigest() != revision_id:
            raise ReferenceContractError("current reference state revision hash changed")
        commit_id = pointer_data.get("commit_id")
        commit_sequence = pointer_data.get("commit_sequence")
        if commit_id is not None or commit_sequence is not None:
            if (
                not isinstance(commit_id, str)
                or _SHA256_RE.fullmatch(commit_id) is None
                or not isinstance(commit_sequence, int)
                or isinstance(commit_sequence, bool)
                or commit_sequence < 1
            ):
                raise ReferenceContractError("reference state pointer commit is invalid")
            commit_path = (
                commits_root / f"{commit_sequence:020d}-{commit_id}.json"
            )
            commit = _read_reference_commit(commit_path)
            if commit["revision_id"] != revision_id:
                raise ReferenceContractError(
                    "reference state pointer revision/commit mismatch"
                )
        _validate_reference_projection_node(target)
        return revision_id, revision_payload, commit_id, commit_sequence
    if not _lexically_exists(target):
        return None, None, None, None
    legacy_payload = _read_regular_file_bounded(
        target,
        max_bytes=MAX_REFERENCE_STATE_BYTES,
        label="legacy desktop reference manifest",
    )
    return hashlib.sha256(legacy_payload).hexdigest(), legacy_payload, None, None


def _decode_project_reference_state(
    project_root: Path,
    payload: bytes,
) -> tuple[dict[str, list[str]], list[str], tuple[str, ...]]:
    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ReferenceContractError("desktop reference manifest is unreadable") from exc
    if not isinstance(data, dict) or set(data) != {
        "schema_version", "manual_groups", "style_sources"
    }:
        raise ReferenceContractError("desktop reference manifest schema is invalid")
    if data.get("schema_version") != PROJECT_REFERENCE_STATE_SCHEMA_VERSION:
        raise ReferenceContractError("desktop reference manifest version is unsupported")

    manual_rows = data.get("manual_groups")
    style_rows = data.get("style_sources")
    if not isinstance(manual_rows, list) or not isinstance(style_rows, list):
        raise ReferenceContractError("desktop reference manifest collections are invalid")
    if len(manual_rows) > HERO_GROUPS_MAX or len(style_rows) > STYLE_SOURCE_MAX:
        raise ReferenceContractError("desktop reference manifest exceeds source limits")

    uploads_root = _project_reference_uploads_root(project_root)
    issues: list[str] = []
    digest_owner: dict[str, str] = {}

    def load_source(row: Any, owner: str) -> str:
        if not isinstance(row, dict) or set(row) != {"path", "sha256"}:
            raise ReferenceContractError("desktop reference source row is invalid")
        relative = row.get("path")
        expected = row.get("sha256")
        if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
            raise ReferenceContractError("desktop reference source path is invalid")
        if not isinstance(expected, str) or _SHA256_RE.fullmatch(expected) is None:
            raise ReferenceContractError("desktop reference source hash is invalid")
        raw_path = project_root / Path(relative)
        path = raw_path.resolve()
        try:
            path.relative_to(uploads_root)
        except ValueError as exc:
            raise ReferenceContractError(
                "desktop reference source escapes project-owned storage"
            ) from exc
        if _is_link_or_reparse(raw_path):
            raise ReferenceContractError(
                "desktop reference source must not be a symlink"
            )
        filename_match = re.fullmatch(
            r"[a-z0-9][a-z0-9_-]{0,63}-([0-9a-f]{64})\.jpg",
            path.name,
        )
        if filename_match is None or filename_match.group(1) != expected:
            raise ReferenceContractError(
                "desktop reference source name/hash contract is invalid"
            )
        previous = digest_owner.get(expected)
        if previous is not None:
            raise ReferenceContractError(
                f"the same reference image is assigned to both {previous!r} and {owner!r}"
            )
        digest_owner[expected] = owner
        if not raw_path.exists():
            issues.append(f"{owner}: project-owned file is missing: {path.name}")
            return str(path)
        try:
            source_payload = _read_regular_file_bounded(
                raw_path,
                max_bytes=MAX_REFERENCE_BYTES,
                label=f"project-owned reference source {path.name}",
            )
        except ReferenceContractError as exc:
            issues.append(f"{owner}: project-owned file is invalid: {exc}")
            return str(path)
        actual = hashlib.sha256(source_payload).hexdigest()
        if actual != expected:
            issues.append(f"{owner}: project-owned file hash changed: {path.name}")
            return str(path)
        try:
            canonicalize_reference_image(source_payload)
        except ReferenceContractError as exc:
            issues.append(f"{owner}: project-owned file is invalid: {exc}")
        return str(path)

    manual: dict[str, list[str]] = {}
    aliases: dict[str, str] = {}
    for row in manual_rows:
        if not isinstance(row, dict) or set(row) != {"name", "sources"}:
            raise ReferenceContractError("desktop manual reference row is invalid")
        name = row.get("name")
        sources = row.get("sources")
        if not isinstance(name, str) or not name.strip():
            raise ReferenceContractError("desktop manual reference name is invalid")
        name = unicodedata.normalize("NFC", name.strip())
        alias = normalize_alias(name)
        if alias in aliases:
            raise ReferenceContractError("desktop manual reference names are ambiguous")
        aliases[alias] = name
        if not isinstance(sources, list) or not 1 <= len(sources) <= HERO_ANGLES_MAX:
            raise ReferenceContractError(
                f"desktop manual reference {name!r} has an invalid source count"
            )
        manual[name] = [load_source(source, name) for source in sources]

    style = [load_source(source, "style") for source in style_rows]
    return manual, style, tuple(issues)


def load_project_reference_state(
    project_dir: str | os.PathLike[str],
    *,
    revision_id: str | None = None,
) -> tuple[dict[str, list[str]], list[str], tuple[str, ...]]:
    """Load refs while keeping missing/changed configured rows visible.

    Structural/path tampering raises. Missing or byte-modified sources remain in
    the returned mapping and are reported in ``issues`` so the UI blocks instead
    of silently dropping the configured reference.
    """

    project_root = Path(project_dir).resolve()
    assets_root = _project_assets_root(project_root, create=False)
    target = assets_root / PROJECT_REFERENCE_STATE_FILE
    pointer = assets_root / PROJECT_REFERENCE_STATE_POINTER_FILE
    history_root = _confined_directory(
        assets_root,
        PROJECT_REFERENCE_STATE_HISTORY_DIR,
        project_root=project_root,
        create=False,
    )
    commits_root = _confined_directory(
        assets_root,
        PROJECT_REFERENCE_STATE_COMMITS_DIR,
        project_root=project_root,
        create=False,
    )
    if revision_id is not None:
        if _SHA256_RE.fullmatch(revision_id) is None:
            raise ReferenceContractError("reference revision id is invalid")
        payload = _read_regular_file_bounded(
            history_root / f"{revision_id}.json",
            max_bytes=MAX_REFERENCE_STATE_BYTES,
            label="immutable reference state revision",
        )
        if hashlib.sha256(payload).hexdigest() != revision_id:
            raise ReferenceContractError("reference state revision hash changed")
    else:
        _current_revision, payload, _commit_id, _commit_sequence = (
            _read_current_state_payload(
                project_root,
                assets_root,
                target,
                pointer,
                history_root,
                commits_root,
            )
        )
        if payload is None:
            return {}, [], ()
    return _decode_project_reference_state(project_root, payload)


def current_project_reference_revision(
    project_dir: str | os.PathLike[str],
) -> str | None:
    return current_project_reference_state(project_dir)["revision_id"]


def current_project_reference_state(
    project_dir: str | os.PathLike[str],
) -> dict[str, Any]:
    project_root = Path(project_dir).resolve()
    assets_root = _project_assets_root(project_root, create=False)
    target = assets_root / PROJECT_REFERENCE_STATE_FILE
    pointer = assets_root / PROJECT_REFERENCE_STATE_POINTER_FILE
    if not _lexically_exists(target) and not _lexically_exists(pointer):
        return {
            "revision_id": None,
            "commit_id": None,
            "commit_sequence": None,
            "state_token": _reference_state_marker_token(target, pointer),
        }
    history_root = _confined_directory(
        assets_root,
        PROJECT_REFERENCE_STATE_HISTORY_DIR,
        project_root=project_root,
        create=False,
    )
    commits_root = _confined_directory(
        assets_root,
        PROJECT_REFERENCE_STATE_COMMITS_DIR,
        project_root=project_root,
        create=False,
    )
    lock_path = assets_root / ".desktop_references.lock"
    with _exclusive_project_state_lock(lock_path):
        revision_id, _payload, commit_id, commit_sequence = (
            _read_current_state_payload(
                project_root,
                assets_root,
                target,
                pointer,
                history_root,
                commits_root,
            )
        )
        state_token = _reference_state_marker_token(target, pointer)
    return {
        "revision_id": revision_id,
        "commit_id": commit_id,
        "commit_sequence": commit_sequence,
        "state_token": state_token,
    }


def list_project_reference_revisions(
    project_dir: str | os.PathLike[str],
) -> tuple[dict[str, Any], ...]:
    """Return every valid immutable revision, newest first, for UI restore."""

    project_root = Path(project_dir).resolve()
    assets_root = _project_assets_root(project_root, create=False)
    history_root = _confined_directory(
        assets_root,
        PROJECT_REFERENCE_STATE_HISTORY_DIR,
        project_root=project_root,
        create=False,
    )
    if not history_root.exists():
        return ()
    commits_root = _confined_directory(
        assets_root,
        PROJECT_REFERENCE_STATE_COMMITS_DIR,
        project_root=project_root,
        create=False,
    )
    lock_path = assets_root / ".desktop_references.lock"
    rows: list[dict[str, Any]] = []
    with _exclusive_project_state_lock(lock_path):
        target = assets_root / PROJECT_REFERENCE_STATE_FILE
        pointer = assets_root / PROJECT_REFERENCE_STATE_POINTER_FILE
        current_state_token = _reference_state_marker_token(target, pointer)
        current_issue = ""
        try:
            current, _payload, current_commit, _current_sequence = (
                _read_current_state_payload(
                    project_root,
                    assets_root,
                    target,
                    pointer,
                    history_root,
                    commits_root,
                )
            )
        except ReferenceContractError as exc:
            # Recovery must remain reachable when the commit marker or current
            # revision is exactly what was damaged. Individual immutable
            # revisions below are still validated independently.
            current = None
            current_commit = None
            current_issue = str(exc)

        referenced_revisions: set[str] = set()
        if commits_root.exists():
            for commit_path in commits_root.iterdir():
                if commit_path.name.startswith("."):
                    continue
                commit_match = _REFERENCE_COMMIT_FILE_RE.fullmatch(commit_path.name)
                try:
                    commit = _read_reference_commit(commit_path)
                    revision_id = commit["revision_id"]
                    revision_payload = _read_regular_file_bounded(
                        history_root / f"{revision_id}.json",
                        max_bytes=MAX_REFERENCE_STATE_BYTES,
                        label="immutable reference state revision",
                    )
                    if hashlib.sha256(revision_payload).hexdigest() != revision_id:
                        raise ReferenceContractError("revision hash changed")
                    manual, style, issues = _decode_project_reference_state(
                        project_root, revision_payload
                    )
                    referenced_revisions.add(revision_id)
                    rows.append(
                        {
                            "revision_id": revision_id,
                            "commit_id": commit["commit_id"],
                            "sequence": commit["sequence"],
                            "is_current": commit["commit_id"] == current_commit,
                            "created_at": commit["committed_at_ns"] / 1_000_000_000,
                            "manual_count": sum(
                                len(paths) for paths in manual.values()
                            ),
                            "style_count": len(style),
                            "issues": issues,
                            "current_state_issue": current_issue,
                            "current_state_token": current_state_token,
                            "current_revision_id": current,
                            "current_commit_id": current_commit,
                        }
                    )
                except (OSError, ReferenceContractError) as exc:
                    rows.append(
                        {
                            "revision_id": "",
                            "commit_id": (
                                commit_match.group("commit")
                                if commit_match is not None else ""
                            ),
                            "sequence": (
                                int(commit_match.group("sequence"))
                                if commit_match is not None else 0
                            ),
                            "is_current": False,
                            "created_at": 0.0,
                            "manual_count": 0,
                            "style_count": 0,
                            "issues": (str(exc),),
                            "current_state_issue": current_issue,
                            "current_state_token": current_state_token,
                            "current_revision_id": current,
                            "current_commit_id": current_commit,
                        }
                    )

        # Legacy/unreferenced revisions remain independently recoverable.
        for path in history_root.glob("*.json"):
            revision_id = path.stem
            if revision_id in referenced_revisions or _SHA256_RE.fullmatch(revision_id) is None:
                continue
            try:
                revision_payload = _read_regular_file_bounded(
                    path,
                    max_bytes=MAX_REFERENCE_STATE_BYTES,
                    label="immutable reference state revision",
                )
                if hashlib.sha256(revision_payload).hexdigest() != revision_id:
                    raise ReferenceContractError("revision hash changed")
                manual, style, issues = _decode_project_reference_state(
                    project_root, revision_payload
                )
                created_at = path.stat().st_mtime
            except (OSError, ReferenceContractError) as exc:
                rows.append(
                    {
                        "revision_id": revision_id,
                        "commit_id": "",
                        "sequence": 0,
                        "is_current": current_commit is None and revision_id == current,
                        "created_at": 0.0,
                        "manual_count": 0,
                        "style_count": 0,
                        "issues": (str(exc),),
                        "current_state_issue": current_issue,
                        "current_state_token": current_state_token,
                        "current_revision_id": current,
                        "current_commit_id": current_commit,
                    }
                )
                continue
            rows.append(
                {
                    "revision_id": revision_id,
                    "commit_id": "",
                    "sequence": 0,
                    "is_current": current_commit is None and revision_id == current,
                    "created_at": created_at,
                    "manual_count": sum(len(paths) for paths in manual.values()),
                    "style_count": len(style),
                    "issues": issues,
                    "current_state_issue": current_issue,
                    "current_state_token": current_state_token,
                    "current_revision_id": current,
                    "current_commit_id": current_commit,
                }
            )
    rows.sort(
        key=lambda row: (int(row.get("sequence") or 0), float(row["created_at"])),
        reverse=True,
    )
    return tuple(rows)


def restore_project_reference_revision(
    project_dir: str | os.PathLike[str],
    revision_id: str,
    *,
    expected_current_commit_id: str | None = None,
    expected_state_token: str | None = None,
    repair_damaged_current: bool = False,
) -> Path:
    """Restore a verified revision using compare-and-swap against current."""

    manual, style, issues = load_project_reference_state(
        project_dir, revision_id=revision_id
    )
    if issues:
        raise ReferenceContractError(
            "reference revision cannot be restored until its source files are repaired: "
            + "; ".join(issues)
        )
    del manual, style  # validation above proves the selected revision is usable
    project_root = Path(project_dir).resolve()
    assets_root = _project_assets_root(project_root, create=True)
    history_root = _confined_directory(
        assets_root,
        PROJECT_REFERENCE_STATE_HISTORY_DIR,
        project_root=project_root,
        create=False,
    )
    commits_root = _confined_directory(
        assets_root,
        PROJECT_REFERENCE_STATE_COMMITS_DIR,
        project_root=project_root,
        create=True,
    )
    revision_path = history_root / f"{revision_id}.json"
    lock_path = assets_root / ".desktop_references.lock"
    target = assets_root / PROJECT_REFERENCE_STATE_FILE
    pointer = assets_root / PROJECT_REFERENCE_STATE_POINTER_FILE
    with _exclusive_project_state_lock(lock_path):
        observed_token = _reference_state_marker_token(target, pointer)
        if expected_state_token is not None and observed_token != expected_state_token:
            raise ReferenceContractError(
                "reference state changed after restore was selected; reload history"
            )
        try:
            (
                _current_revision,
                _current_payload,
                current_commit_id,
                _current_sequence,
            ) = _read_current_state_payload(
                project_root,
                assets_root,
                target,
                pointer,
                history_root,
                commits_root,
            )
        except ReferenceContractError:
            if not repair_damaged_current:
                raise
            current_commit_id = None
        else:
            if repair_damaged_current:
                raise ReferenceContractError(
                    "reference state is no longer damaged; reload before restoring"
                )
        expected_commit = (
            None if expected_current_commit_id == "" else expected_current_commit_id
        )
        if (
            expected_current_commit_id is not None
            and current_commit_id != expected_commit
        ):
            raise ReferenceContractError(
                "reference state commit changed after restore was selected"
            )
        payload = _read_regular_file_bounded(
            revision_path,
            max_bytes=MAX_REFERENCE_STATE_BYTES,
            label="immutable reference state revision selected for restore",
        )
        if hashlib.sha256(payload).hexdigest() != revision_id:
            raise ReferenceContractError("reference state revision hash changed")
        _decode_project_reference_state(project_root, payload)
        max_sequence = 0
        for path in commits_root.iterdir():
            match = _REFERENCE_COMMIT_FILE_RE.fullmatch(path.name)
            if match is not None:
                max_sequence = max(max_sequence, int(match.group("sequence")))
        committed = _write_reference_commit(
            commits_root,
            sequence=max_sequence + 1,
            revision_id=revision_id,
            previous_commit_id=current_commit_id,
        )
        # The user explicitly selected a verified revision, so this repair path
        # may replace an unreadable pointer/current projection. Ordinary saves
        # still use CAS and fail closed instead.
        _atomic_write(
            target,
            payload,
            replace_invalid_target=repair_damaged_current,
        )
        _atomic_write(
            pointer,
            canonical_json_bytes(
                {
                    "schema_version": PROJECT_REFERENCE_STATE_SCHEMA_VERSION,
                    "revision_id": revision_id,
                    "commit_id": committed["commit_id"],
                    "commit_sequence": committed["sequence"],
                }
            ),
            replace_invalid_target=repair_damaged_current,
        )
    return target


def build_reference_bundle_manifest(
    project_dir: str | os.PathLike[str],
    manual_groups: Mapping[str, Sequence[str | os.PathLike[str]]],
    style_sources: Sequence[str | os.PathLike[str]],
) -> Path:
    """Freeze current managed refs into the worker/server immutable v1 bundle."""

    project_root = Path(project_dir).resolve()
    if not isinstance(manual_groups, Mapping) or len(manual_groups) > HERO_GROUPS_MAX:
        raise ReferenceContractError("manual reference groups are invalid")
    if isinstance(style_sources, (str, bytes, bytearray)) or not isinstance(
        style_sources, Sequence
    ) or len(style_sources) > STYLE_SOURCE_MAX:
        raise ReferenceContractError("style reference sources are invalid")

    source_payloads: dict[str, bytes] = {}
    digest_owner: dict[str, str] = {}
    alias_owner: dict[str, str] = {}
    characters: list[dict[str, Any]] = []
    fingerprint_characters: list[dict[str, Any]] = []

    for raw_name, paths in sorted(
        manual_groups.items(), key=lambda item: normalize_alias(item[0])
    ):
        if not isinstance(raw_name, str) or not raw_name.strip():
            raise ReferenceContractError("manual reference group name must not be empty")
        name = unicodedata.normalize("NFC", raw_name.strip())
        alias = normalize_alias(name)
        if alias in alias_owner:
            raise ReferenceContractError(
                f"manual reference group {name!r} conflicts with {alias_owner[alias]!r}"
            )
        alias_owner[alias] = name
        if isinstance(paths, (str, bytes, bytearray)) or not isinstance(
            paths, Sequence
        ) or not 1 <= len(paths) <= HERO_ANGLES_MAX:
            raise ReferenceContractError(
                f"manual reference group {name!r} must contain 1..{HERO_ANGLES_MAX} images"
            )
        hashes: list[str] = []
        for path in paths:
            row = _project_reference_source_row(project_root, path)
            digest = row["sha256"]
            previous = digest_owner.get(digest)
            if previous is not None:
                raise ReferenceContractError(
                    f"the same reference image is assigned to both {previous!r} and {name!r}"
                )
            digest_owner[digest] = name
            payload = _read_regular_file_bounded(
                Path(path).resolve(),
                max_bytes=MAX_REFERENCE_BYTES,
                label=f"project-owned reference source {Path(path).name}",
            )
            source_payloads[digest] = payload
            hashes.append(digest)
        hashes.sort()
        group_id = canonical_fingerprint(
            {"schema_version": 1, "manual_group_name": name}
        )
        files = [f"sources/{digest}.jpg" for digest in hashes]
        characters.append({"group_id": group_id, "name": name, "files": files})
        fingerprint_characters.append(
            {"group_id": group_id, "name": name, "hashes": hashes}
        )

    style_hashes: list[str] = []
    for path in style_sources:
        row = _project_reference_source_row(project_root, path)
        digest = row["sha256"]
        previous = digest_owner.get(digest)
        if previous is not None:
            raise ReferenceContractError(
                f"the same reference image is assigned to both {previous!r} and 'style'"
            )
        digest_owner[digest] = "style"
        source_payloads[digest] = _read_regular_file_bounded(
            Path(path).resolve(),
            max_bytes=MAX_REFERENCE_BYTES,
            label=f"project-owned reference source {Path(path).name}",
        )
        style_hashes.append(digest)
    style_hashes.sort()

    source_fingerprint = canonical_fingerprint(
        {"characters": fingerprint_characters, "style": style_hashes}
    )
    unsigned = {
        "schema_version": 1,
        "characters": characters,
        "style": [f"sources/{digest}.jpg" for digest in style_hashes],
        "source_fingerprint": source_fingerprint,
    }
    bundle_id = canonical_fingerprint(unsigned)
    assets_dir = _project_assets_root(project_root, create=True)
    bundles_root = _confined_directory(
        assets_dir,
        "reference_bundles",
        project_root=project_root,
        create=True,
    )
    bundle_dir = _confined_directory(
        bundles_root,
        bundle_id,
        project_root=project_root,
        create=True,
    )
    source_dir = _confined_directory(
        bundle_dir,
        "sources",
        project_root=project_root,
        create=True,
    )

    for digest, payload in source_payloads.items():
        target = source_dir / f"{digest}.jpg"
        if target.exists():
            existing = _read_regular_file_bounded(
                target,
                max_bytes=MAX_REFERENCE_BYTES,
                label="immutable reference bundle source",
            )
            if hashlib.sha256(existing).hexdigest() != digest:
                raise ReferenceContractError(
                    "immutable reference bundle source was modified"
                )
        else:
            _atomic_write(target, payload)
    manifest = bundle_dir / "manifest.json"
    manifest_payload = canonical_json_bytes({**unsigned, "bundle_id": bundle_id})
    if manifest.exists():
        existing_manifest = _read_regular_file_bounded(
            manifest,
            max_bytes=MAX_REFERENCE_STATE_BYTES,
            label="immutable reference bundle manifest",
        )
        if existing_manifest != manifest_payload:
            raise ReferenceContractError(
                "immutable reference bundle manifest was modified"
            )
    else:
        _atomic_write(manifest, manifest_payload)
    return manifest


def build_hero_contact_sheet(
    sources: Sequence[
        CanonicalImage | bytes | bytearray | memoryview | str | os.PathLike[str]
    ],
    output_dir: str | os.PathLike[str],
    *,
    max_bytes: int = MAX_REFERENCE_BYTES,
    max_pixels: int = MAX_REFERENCE_PIXELS,
    max_dimension: int = MAX_REFERENCE_DIMENSION,
    cell_size: int = 768,
    gutter: int = 16,
) -> ReferenceArtifact:
    """Persist one deterministic hero artifact made from one to six angles.

    Inputs are sorted by their canonical pixel hash, so changing filenames or
    directory iteration order cannot change the resulting contact sheet.
    """

    return build_contact_sheet(
        sources,
        output_dir,
        kind="hero",
        max_bytes=max_bytes,
        max_pixels=max_pixels,
        max_dimension=max_dimension,
        cell_size=cell_size,
        gutter=gutter,
    )


def build_contact_sheet(
    sources: Sequence[
        CanonicalImage | bytes | bytearray | memoryview | str | os.PathLike[str]
    ],
    output_dir: str | os.PathLike[str],
    *,
    kind: str,
    max_bytes: int = MAX_REFERENCE_BYTES,
    max_pixels: int = MAX_REFERENCE_PIXELS,
    max_dimension: int = MAX_REFERENCE_DIMENSION,
    cell_size: int = 768,
    gutter: int = 16,
) -> ReferenceArtifact:
    """Build one hero sheet or style moodboard provider-level artifact.

    A hero group accepts up to six camera angles; a style moodboard accepts up
    to five source images.  They intentionally remain separate artifacts: a
    provider with capacity one (Flower) must block a scene requiring both.
    """

    if not isinstance(kind, str) or kind.strip().casefold() not in {"hero", "style"}:
        raise ReferenceContractError("contact sheet kind must be 'hero' or 'style'")
    normalized_kind = kind.strip().casefold()
    source_limit = MAX_HERO_ANGLES if normalized_kind == "hero" else MAX_STYLE_SOURCES
    source_label = "hero angles" if normalized_kind == "hero" else "style sources"
    if isinstance(sources, (str, bytes, bytearray)) or not isinstance(sources, Sequence):
        raise ReferenceContractError(f"{source_label} must be an ordered sequence")
    if not 1 <= len(sources) <= source_limit:
        raise ReferenceContractError(
            f"{normalized_kind} reference requires 1..{source_limit} sources"
        )
    cell_size = _positive_limit(cell_size, "cell_size")
    if isinstance(gutter, bool) or not isinstance(gutter, int) or gutter < 0:
        raise ReferenceContractError("gutter must be a non-negative integer")

    checked_max_bytes = _positive_limit(max_bytes, "max_bytes")
    checked_max_pixels = _positive_limit(max_pixels, "max_pixels")
    checked_max_dimension = _positive_limit(max_dimension, "max_dimension")
    canonical = [
        _read_reference_source(
            source,
            max_bytes=checked_max_bytes,
            max_pixels=checked_max_pixels,
            max_dimension=checked_max_dimension,
        )
        for source in sources
    ]
    canonical.sort(key=lambda image: (image.sha256, image.source_sha256))
    source_hashes = tuple(image.source_sha256 for image in canonical)
    if len({image.sha256 for image in canonical}) != len(canonical):
        raise ReferenceContractError(
            f"{normalized_kind} reference contains duplicate source images"
        )

    if len(canonical) == 1:
        return _persist_artifact(
            canonical[0],
            output_dir,
            kind="hero" if normalized_kind == "hero" else "style-moodboard",
            source_hashes=source_hashes,
        )

    columns = 2 if len(canonical) <= 4 else 3
    rows = math.ceil(len(canonical) / columns)
    sheet_width = columns * cell_size + (columns + 1) * gutter
    sheet_height = rows * cell_size + (rows + 1) * gutter
    sheet = Image.new("RGB", (sheet_width, sheet_height), (255, 255, 255))
    try:
        for index, item in enumerate(canonical):
            with Image.open(BytesIO(item.jpeg_bytes)) as decoded:
                tile = decoded.convert("RGB")
                tile.thumbnail(
                    (cell_size, cell_size),
                    resample=Image.Resampling.LANCZOS,
                    reducing_gap=3.0,
                )
                row, column = divmod(index, columns)
                left = gutter + column * (cell_size + gutter)
                top = gutter + row * (cell_size + gutter)
                left += (cell_size - tile.width) // 2
                top += (cell_size - tile.height) // 2
                sheet.paste(tile, (left, top))
                tile.close()
        sheet_bytes = _encode_jpeg(sheet)
        sheet_image = CanonicalImage(
            jpeg_bytes=sheet_bytes,
            sha256=hashlib.sha256(sheet_bytes).hexdigest(),
            source_sha256=canonical_fingerprint(
                {
                    "layout": f"{normalized_kind}-grid-v1",
                    "cell_size": cell_size,
                    "gutter": gutter,
                    "source_hashes": list(source_hashes),
                }
            ),
            width=sheet_width,
            height=sheet_height,
            source_format="CONTACT_SHEET",
        )
    finally:
        sheet.close()

    return _persist_artifact(
        sheet_image,
        output_dir,
        kind="hero-sheet" if normalized_kind == "hero" else "style-moodboard",
        source_hashes=source_hashes,
    )


def _validate_artifact(artifact: ReferenceArtifact) -> None:
    if not isinstance(artifact, ReferenceArtifact):
        raise ReferenceContractError("logical reference slot has an invalid artifact")
    if _SHA256_RE.fullmatch(artifact.sha256) is None:
        raise ReferenceContractError("reference artifact hash is invalid")
    if artifact.width <= 0 or artifact.height <= 0:
        raise ReferenceContractError("reference artifact dimensions are invalid")
    if not artifact.path:
        raise ReferenceContractError("reference artifact path is empty")
    if _SHA256_RE.fullmatch(artifact.fingerprint) is None:
        raise ReferenceContractError("reference artifact fingerprint is invalid")


def _audit_for_slots(
    slots: Sequence[LogicalReferenceSlot],
    *,
    active_status: str,
    active_reason: str,
) -> tuple[ReferenceSlotAudit, ...]:
    audits: list[ReferenceSlotAudit] = []
    for slot in slots:
        artifact_hash = slot.artifact.sha256 if isinstance(slot.artifact, ReferenceArtifact) else None
        if not slot.applicable:
            status = "not_applicable"
            reason = "slot is explicitly not applicable to this scene"
        elif slot.superseded_by is not None:
            status = "superseded"
            reason = f"slot is explicitly superseded by {slot.superseded_by!r}"
        else:
            status = active_status
            reason = active_reason
        audits.append(
            ReferenceSlotAudit(
                slot_id=slot.slot_id,
                kind=slot.kind,
                status=status,
                artifact_sha256=artifact_hash,
                reason=reason,
                superseded_by=slot.superseded_by,
            )
        )
        if (
            status in {"selected", "blocking"}
            and isinstance(slot.artifact, ReferenceArtifact)
            and len(slot.artifact.source_hashes) > 1
        ):
            # The original uploads remain visible in the audit even though one
            # deterministic sheet/moodboard consumes just one provider slot.
            for source_index, source_hash in enumerate(slot.artifact.source_hashes, 1):
                audits.append(
                    ReferenceSlotAudit(
                        slot_id=f"{slot.slot_id}:source:{source_index}",
                        kind=f"{slot.kind}_source",
                        status="superseded",
                        artifact_sha256=source_hash,
                        reason="source consolidated into a deterministic provider artifact",
                        superseded_by=slot.slot_id,
                    )
                )
    return tuple(audits)


def pack_scene_references(
    required_slots: Sequence[LogicalReferenceSlot],
    model: str,
) -> SceneReferencePack:
    """Allocate all required scene refs or raise before any provider request."""

    if (
        isinstance(required_slots, (str, bytes, bytearray))
        or not isinstance(required_slots, Sequence)
    ):
        raise ReferenceContractError("logical reference slots must be a sequence")
    slots = tuple(required_slots)
    if any(not isinstance(slot, LogicalReferenceSlot) for slot in slots):
        raise ReferenceContractError(
            "every logical reference slot must be LogicalReferenceSlot"
        )

    ids: set[str] = set()
    by_id: dict[str, LogicalReferenceSlot] = {}
    for slot in slots:
        if not isinstance(slot.slot_id, str) or not slot.slot_id.strip():
            raise ReferenceContractError("logical reference slot id must not be empty")
        if slot.slot_id in ids:
            raise ReferenceContractError(
                f"duplicate logical reference slot id {slot.slot_id!r}"
            )
        if not isinstance(slot.kind, str) or not slot.kind.strip():
            raise ReferenceContractError(
                f"logical reference slot {slot.slot_id!r} has no kind"
            )
        if not isinstance(slot.applicable, bool):
            raise ReferenceContractError(
                f"logical reference slot {slot.slot_id!r} has invalid applicability"
            )
        if not slot.applicable and slot.superseded_by is not None:
            raise ReferenceContractError(
                f"logical reference slot {slot.slot_id!r} cannot be both "
                "not-applicable and superseded"
            )
        ids.add(slot.slot_id)
        by_id[slot.slot_id] = slot

    for slot in slots:
        if slot.superseded_by is None:
            continue
        target = by_id.get(slot.superseded_by)
        if target is None:
            raise ReferenceContractError(
                f"slot {slot.slot_id!r} supersedes to missing slot "
                f"{slot.superseded_by!r}"
            )
        if target.slot_id == slot.slot_id or not target.applicable or target.superseded_by:
            raise ReferenceContractError(
                f"slot {slot.slot_id!r} has a non-active superseding target"
            )

    active = tuple(
        slot for slot in slots if slot.applicable and slot.superseded_by is None
    )
    problems: list[str] = []
    for slot in active:
        if slot.artifact is None:
            problems.append(f"required slot {slot.slot_id!r} has no artifact")
        else:
            try:
                _validate_artifact(slot.artifact)
            except ReferenceContractError as exc:
                problems.append(f"slot {slot.slot_id!r}: {exc}")

    active_hashes: dict[str, str] = {}
    for slot in active:
        if slot.artifact is None:
            continue
        previous = active_hashes.get(slot.artifact.sha256)
        if previous is not None:
            problems.append(
                f"slots {previous!r} and {slot.slot_id!r} use the same artifact; "
                "represent the replacement explicitly with superseded_by"
            )
        active_hashes[slot.artifact.sha256] = slot.slot_id

    style_slots = [slot for slot in active if slot.kind.casefold() == "style"]
    if len(style_slots) > 1:
        problems.append(
            f"scene has {len(style_slots)} style slots; 1..{MAX_STYLE_SOURCES} "
            "style sources must first be consolidated into one moodboard slot"
        )

    normalized_model = model.strip().upper() if isinstance(model, str) else ""
    try:
        capacity = reference_capacity(model)
    except ReferenceContractError as exc:
        audits = _audit_for_slots(
            slots,
            active_status="blocking",
            active_reason=str(exc),
        )
        raise ReferenceContractError(str(exc), audit=audits) from exc

    if len(active) > capacity:
        problems.append(
            f"model {normalized_model} accepts {capacity} references but this "
            f"scene requires {len(active)}"
        )

    if problems:
        reason = "; ".join(problems)
        audits = _audit_for_slots(
            slots,
            active_status="blocking",
            active_reason=reason,
        )
        raise ReferenceContractError(reason, audit=audits)

    audits = _audit_for_slots(
        slots,
        active_status="selected",
        active_reason="required logical slot selected for the provider request",
    )
    fingerprint = canonical_fingerprint(
        {
            "schema_version": CONTRACT_SCHEMA_VERSION,
            "model": normalized_model,
            "capacity": capacity,
            "slots": [
                {
                    "slot_id": audit.slot_id,
                    "kind": audit.kind,
                    "status": audit.status,
                    "artifact_sha256": audit.artifact_sha256,
                    "superseded_by": audit.superseded_by,
                }
                for audit in audits
            ],
        }
    )
    return SceneReferencePack(
        model=normalized_model,
        capacity=capacity,
        selected=active,
        audit=audits,
        fingerprint=fingerprint,
    )


def _canonical_fingerprint_value(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        value = asdict(value)
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ReferenceContractError("fingerprint values cannot contain NaN/Infinity")
        return value
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, bytes):
        return {"$bytes_sha256": hashlib.sha256(value).hexdigest()}
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            if not isinstance(raw_key, str):
                raise ReferenceContractError("fingerprint mapping keys must be strings")
            key = unicodedata.normalize("NFC", raw_key)
            if key in normalized:
                raise ReferenceContractError(
                    "fingerprint mapping has duplicate keys after NFC normalization"
                )
            normalized[key] = _canonical_fingerprint_value(raw_value)
        return normalized
    if isinstance(value, (list, tuple)):
        return [_canonical_fingerprint_value(item) for item in value]
    raise ReferenceContractError(
        f"unsupported fingerprint value type: {type(value).__name__}"
    )


def canonical_json_bytes(value: Any) -> bytes:
    """Return strict, stable UTF-8 JSON for a fingerprintable value."""

    normalized = _canonical_fingerprint_value(value)
    return json.dumps(
        normalized,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def combine_fingerprints(*fingerprints: str) -> str:
    if not fingerprints:
        raise ReferenceContractError("at least one fingerprint is required")
    normalized: list[str] = []
    for value in fingerprints:
        if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
            raise ReferenceContractError("combined fingerprint is not a sha256 hex digest")
        normalized.append(value)
    return canonical_fingerprint(
        {"schema_version": CONTRACT_SCHEMA_VERSION, "fingerprints": normalized}
    )


def fingerprint_reference_artifacts(
    artifacts: Sequence[ReferenceArtifact],
) -> str:
    if isinstance(artifacts, (str, bytes, bytearray)) or not isinstance(artifacts, Sequence):
        raise ReferenceContractError("reference artifacts must be a sequence")
    rows = []
    for artifact in artifacts:
        _validate_artifact(artifact)
        rows.append(
            {
                "kind": artifact.kind,
                "sha256": artifact.sha256,
                "source_hashes": list(artifact.source_hashes),
                "width": artifact.width,
                "height": artifact.height,
                "fingerprint": artifact.fingerprint,
            }
        )
    return canonical_fingerprint(
        {"schema_version": CONTRACT_SCHEMA_VERSION, "artifacts": rows}
    )


__all__ = [
    "CONTRACT_SCHEMA_VERSION",
    "PROJECT_REFERENCE_STATE_SCHEMA_VERSION",
    "PROJECT_REFERENCE_STATE_FILE",
    "PROJECT_REFERENCE_STATE_POINTER_FILE",
    "PROJECT_REFERENCE_STATE_HISTORY_DIR",
    "PROJECT_REFERENCE_STATE_COMMITS_DIR",
    "MAX_REFERENCE_STATE_BYTES",
    "MAX_REFERENCE_COMMIT_BYTES",
    "MODEL_REFERENCE_CAPACITIES",
    "STYLE_SOURCE_MAX",
    "HERO_ANGLES_MAX",
    "HERO_GROUPS_MAX",
    "MAX_STYLE_SOURCES",
    "MAX_STYLE_REFERENCES",
    "MAX_HERO_ANGLES",
    "MAX_HERO_GROUPS",
    "MAX_REFERENCE_BYTES",
    "MAX_REFERENCE_PIXELS",
    "MAX_REFERENCE_DIMENSION",
    "REFERENCE_AUDIT_STATUSES",
    "ReferenceContractError",
    "ManualHeroGroup",
    "ManualGroupResolution",
    "CanonicalImage",
    "ReferenceArtifact",
    "LogicalReferenceSlot",
    "ReferenceSlotAudit",
    "SceneReferencePack",
    "reference_capacity",
    "normalize_alias",
    "resolve_manual_hero_groups",
    "replace_named_reference_group",
    "canonicalize_reference_image",
    "persist_reference_artifact",
    "import_reference_sources",
    "save_project_reference_state",
    "load_project_reference_state",
    "current_project_reference_revision",
    "current_project_reference_state",
    "list_project_reference_revisions",
    "restore_project_reference_revision",
    "build_reference_bundle_manifest",
    "build_contact_sheet",
    "build_hero_contact_sheet",
    "pack_scene_references",
    "canonical_json_bytes",
    "canonical_fingerprint",
    "combine_fingerprints",
    "fingerprint_reference_artifacts",
]
