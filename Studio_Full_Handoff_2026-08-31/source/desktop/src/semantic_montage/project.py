"""Portable project configuration and content-addressed asset references.

The persisted project never contains host-specific absolute paths.  Large donor
files may live on named mounts; changing machines only changes the runtime mount
mapping, not project JSON/YAML or downstream manifests.
"""
from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from .common import atomic_write_json, sha256_file


class ProjectError(ValueError):
    """Raised when a portable project contract is invalid."""


_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _portable_relative(value: object, *, field_name: str) -> str:
    raw = str(value or "").strip().replace("\\", "/")
    path = PurePosixPath(raw)
    if not raw or path.is_absolute() or ".." in path.parts:
        raise ProjectError(f"{field_name} must be a portable relative path")
    return str(path)


def _load_mapping(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8-sig")
    if path.suffix.lower() == ".json":
        payload = json.loads(text)
    else:
        try:
            import yaml
        except ImportError as exc:  # pragma: no cover - dependency preflight
            raise ProjectError("PyYAML is required for project.yml") from exc
        payload = yaml.safe_load(text)
    if not isinstance(payload, dict):
        raise ProjectError("project document must be an object")
    return dict(payload)


@dataclass(frozen=True)
class AssetReference:
    asset_id: str
    sha256: str
    relative_path: str
    mount: str | None = None
    media_type: str | None = None
    provenance: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "AssetReference":
        asset_id = str(payload.get("asset_id") or "").strip()
        digest = str(payload.get("sha256") or "").lower().strip()
        mount = str(payload.get("mount") or "").strip() or None
        if not _ID_RE.fullmatch(asset_id):
            raise ProjectError(f"invalid asset_id: {asset_id!r}")
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ProjectError(f"invalid sha256 for asset {asset_id}")
        if mount is not None and not _ID_RE.fullmatch(mount):
            raise ProjectError(f"invalid mount name: {mount!r}")
        relative_path = _portable_relative(
            payload.get("relative_path"), field_name=f"asset {asset_id}.relative_path"
        )
        provenance = payload.get("provenance") or {}
        if not isinstance(provenance, Mapping):
            raise ProjectError(f"asset {asset_id}.provenance must be an object")
        return cls(
            asset_id=asset_id,
            sha256=digest,
            relative_path=relative_path,
            mount=mount,
            media_type=str(payload.get("media_type") or "").strip() or None,
            provenance=dict(provenance),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "sha256": self.sha256,
            "relative_path": self.relative_path,
            "mount": self.mount,
            "media_type": self.media_type,
            "provenance": dict(self.provenance),
        }


@dataclass(frozen=True)
class ProjectConfig:
    config_path: Path
    project_id: str
    schema_version: int
    assets: tuple[AssetReference, ...]
    stages: tuple[str, ...]
    settings: Mapping[str, Any]
    secrets: Mapping[str, str]

    @property
    def root(self) -> Path:
        return self.config_path.parent.resolve()

    @property
    def state_path(self) -> Path:
        raw = self.settings.get("state_path", ".semantic_montage/state.sqlite3")
        return self.root / _portable_relative(raw, field_name="settings.state_path")

    @property
    def work_root(self) -> Path:
        raw = self.settings.get("work_root", "work")
        return self.root / _portable_relative(raw, field_name="settings.work_root")

    @property
    def output_root(self) -> Path:
        raw = self.settings.get("output_root", "output")
        return self.root / _portable_relative(raw, field_name="settings.output_root")

    @property
    def qa_root(self) -> Path:
        raw = self.settings.get("qa_root", "qa")
        return self.root / _portable_relative(raw, field_name="settings.qa_root")

    def resolve_asset(
        self,
        asset: AssetReference | str,
        *,
        mounts: Mapping[str, str | os.PathLike[str]] | None = None,
        verify_hash: bool = True,
    ) -> Path:
        if isinstance(asset, str):
            matches = [row for row in self.assets if row.asset_id == asset]
            if len(matches) != 1:
                raise ProjectError(f"unknown or duplicate asset_id: {asset}")
            asset = matches[0]
        if asset.mount:
            mount_map = dict(mounts or {})
            if asset.mount not in mount_map:
                raise ProjectError(f"runtime mount is missing: {asset.mount}")
            base = Path(mount_map[asset.mount]).expanduser().resolve()
        else:
            base = self.root
        resolved = (base / asset.relative_path).resolve()
        try:
            resolved.relative_to(base)
        except ValueError as exc:
            raise ProjectError(f"asset escapes its root: {asset.asset_id}") from exc
        if not resolved.is_file():
            raise ProjectError(f"asset file is missing: {asset.asset_id}")
        if verify_hash and sha256_file(resolved) != asset.sha256:
            raise ProjectError(f"asset hash mismatch: {asset.asset_id}")
        return resolved

    def fingerprint(self) -> str:
        import hashlib

        portable = {
            "schema_version": self.schema_version,
            "project_id": self.project_id,
            "assets": [row.to_dict() for row in self.assets],
            "stages": list(self.stages),
            "settings": dict(self.settings),
            "secrets": dict(self.secrets),
        }
        encoded = json.dumps(portable, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


def load_project(path: str | os.PathLike[str]) -> ProjectConfig:
    config_path = Path(path).expanduser().resolve()
    payload = _load_mapping(config_path)
    version = int(payload.get("schema_version") or 0)
    if version != 2:
        raise ProjectError(f"unsupported project schema_version: {version}")
    project_id = str(payload.get("project_id") or "").strip()
    if not _ID_RE.fullmatch(project_id):
        raise ProjectError(f"invalid project_id: {project_id!r}")
    asset_rows = payload.get("assets") or []
    if not isinstance(asset_rows, list):
        raise ProjectError("assets must be an array")
    assets = tuple(AssetReference.from_mapping(row) for row in asset_rows)
    if len({row.asset_id for row in assets}) != len(assets):
        raise ProjectError("asset_id values must be unique")
    stages_raw = payload.get("stages") or []
    if not isinstance(stages_raw, list):
        raise ProjectError("stages must be an array")
    stages = tuple(str(row).strip() for row in stages_raw if str(row).strip())
    settings = payload.get("settings") or {}
    secrets = payload.get("secrets") or {}
    if not isinstance(settings, Mapping) or not isinstance(secrets, Mapping):
        raise ProjectError("settings and secrets must be objects")
    production_settings = settings.get("production") or {}
    if isinstance(production_settings, Mapping) and "runtime_mounts" in production_settings:
        raise ProjectError(
            "runtime_mounts are process-local; pass --mount NAME=PATH instead of persisting them"
        )
    secret_envs: dict[str, str] = {}
    for name, env_name in secrets.items():
        env = str(env_name or "").strip()
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{2,127}", env):
            raise ProjectError(f"secret {name} must reference an environment variable")
        secret_envs[str(name)] = env
    config = ProjectConfig(
        config_path=config_path,
        project_id=project_id,
        schema_version=version,
        assets=assets,
        stages=stages,
        settings=dict(settings),
        secrets=secret_envs,
    )
    # Validate all configured persisted paths immediately.
    _ = config.state_path, config.work_root, config.output_root, config.qa_root
    return config


def register_local_asset(
    project_root: str | os.PathLike[str],
    source_path: str | os.PathLike[str],
    *,
    asset_id: str,
    media_type: str | None = None,
    provenance: Mapping[str, Any] | None = None,
) -> AssetReference:
    """Copy a small asset into a content-addressed project registry atomically."""

    if not _ID_RE.fullmatch(asset_id):
        raise ProjectError(f"invalid asset_id: {asset_id!r}")
    root = Path(project_root).expanduser().resolve()
    source = Path(source_path).expanduser().resolve()
    if not source.is_file():
        raise ProjectError(f"source asset is missing: {source}")
    digest = sha256_file(source)
    suffix = source.suffix.lower() if len(source.suffix) <= 12 else ""
    relative = PurePosixPath("assets") / digest[:2] / f"{digest}{suffix}"
    target = root / Path(relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        shutil.copyfile(source, temporary)
        if sha256_file(temporary) != digest:
            temporary.unlink(missing_ok=True)
            raise ProjectError("asset changed while being copied")
        os.replace(temporary, target)
    return AssetReference(
        asset_id=asset_id,
        sha256=digest,
        relative_path=str(relative),
        media_type=media_type,
        provenance=dict(provenance or {}),
    )


def write_project_json(path: str | os.PathLike[str], payload: Mapping[str, Any]) -> None:
    """Write a portable JSON project after validating it through the public loader."""

    target = Path(path).expanduser().resolve()
    atomic_write_json(target, dict(payload))
    load_project(target)


__all__ = [
    "AssetReference",
    "ProjectConfig",
    "ProjectError",
    "load_project",
    "register_local_asset",
    "write_project_json",
]
