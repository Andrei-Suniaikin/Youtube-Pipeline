"""Credential-gated provider contracts for music and stock sound effects.

Nothing in this module performs HTTP I/O.  Remote providers only build a
validated :class:`PreparedRequest`; the caller owns retries, rate limits,
timeouts, execution, response persistence, and audit receipts.

Suno's public landing page does not expose a stable endpoint/payload schema.
Consequently :class:`SunoOfficialProvider` has no guessed defaults: it accepts
request builders copied from the authenticated official portal contract and
rejects routes outside official Suno hosts.
"""

from __future__ import annotations

import copy
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence
from urllib.parse import urlparse

from .common import sha256_file
from .sound import music_generation_cache_key, stable_cache_key


class ProviderError(RuntimeError):
    """Base class for provider configuration and contract errors."""


class ProviderNotConfiguredError(ProviderError):
    """A request was attempted without the credentials/contract it needs."""


class ProviderCapabilityError(ProviderError):
    """The configured provider is not authorised for this operation."""


def _clean_secret(value: str | None) -> str | None:
    result = str(value or "").strip()
    return result or None


def _nonempty(value: Any, name: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ProviderError(f"{name} must be non-empty")
    return result


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise ProviderError(f"{name} must be numeric")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ProviderError(f"{name} must be numeric") from exc
    if not math.isfinite(result):
        raise ProviderError(f"{name} must be finite")
    return result


@dataclass(frozen=True)
class PreparedRequest:
    """A declarative HTTP request; constructing it never opens a connection."""

    method: str
    url: str
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)
    params: Mapping[str, Any] = field(default_factory=dict)
    json_body: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        method = str(self.method).upper().strip()
        parsed = urlparse(str(self.url))
        if method not in {"GET", "POST"}:
            raise ProviderError("prepared request method must be GET or POST")
        if parsed.scheme != "https" or not parsed.hostname:
            raise ProviderError("prepared request URL must be an absolute HTTPS URL")
        object.__setattr__(self, "method", method)
        object.__setattr__(self, "url", str(self.url))
        object.__setattr__(self, "headers", dict(self.headers))
        object.__setattr__(self, "params", copy.deepcopy(dict(self.params)))
        if self.json_body is not None:
            object.__setattr__(self, "json_body", copy.deepcopy(dict(self.json_body)))

    def as_dict(self, *, include_secrets: bool = False) -> dict[str, Any]:
        headers = dict(self.headers)
        if not include_secrets:
            for key in list(headers):
                folded = key.casefold()
                if any(token in folded for token in ("authorization", "api-key", "token")):
                    headers[key] = "<redacted>"
        return {
            "method": self.method,
            "url": self.url,
            "headers": headers,
            "params": copy.deepcopy(dict(self.params)),
            "json_body": copy.deepcopy(dict(self.json_body))
            if self.json_body is not None
            else None,
        }

    @property
    def cache_key(self) -> str:
        """Cache identity excluding all authentication headers."""

        return stable_cache_key(
            "provider_request",
            {
                "method": self.method,
                "url": self.url,
                "params": self.params,
                "json_body": self.json_body,
            },
        )


@dataclass(frozen=True)
class ProviderCapabilities:
    provider_id: str
    configured: bool
    capabilities: frozenset[str]
    blockers: Mapping[str, str] = field(default_factory=dict)

    def supports(self, capability: str) -> bool:
        return str(capability) in self.capabilities

    def require(self, capability: str) -> None:
        capability = str(capability)
        if self.supports(capability):
            return
        reason = self.blockers.get(capability) or "capability is unavailable"
        error_type = ProviderNotConfiguredError if not self.configured else ProviderCapabilityError
        raise error_type(f"{self.provider_id}.{capability}: {reason}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "configured": self.configured,
            "capabilities": sorted(self.capabilities),
            "blockers": dict(self.blockers),
        }


class MusicProvider(Protocol):
    provider_id: str

    def capabilities(self) -> ProviderCapabilities: ...

    def build_generation_request(self, job: Mapping[str, Any]) -> PreparedRequest: ...


class SFXSearchProvider(Protocol):
    provider_id: str

    def capabilities(self) -> ProviderCapabilities: ...

    def build_search_request(self, query: str, **kwargs: Any) -> PreparedRequest: ...


SunoRequestBuilder = Callable[[Mapping[str, Any], str], PreparedRequest]
SunoIdRequestBuilder = Callable[[str, str], PreparedRequest]


_SUNO_OFFICIAL_HOSTS = frozenset({"api.suno.com", "platform.suno.com"})


def _require_official_suno_url(url: str, name: str) -> None:
    parsed = urlparse(str(url))
    if parsed.scheme != "https" or parsed.hostname not in _SUNO_OFFICIAL_HOSTS:
        raise ProviderError(f"{name} must use an official Suno HTTPS host")


class SunoOfficialProvider:
    """Official Suno adapter whose live route schema comes from the user portal.

    ``submit_builder``/``status_builder``/``download_builder`` must only build
    :class:`PreparedRequest` values.  There are deliberately no fallback URLs,
    model names, payload keys, or polling assumptions in this class.
    """

    provider_id = "suno_official"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        portal_contract_ref: str | None = None,
        submit_builder: SunoRequestBuilder | None = None,
        status_builder: SunoIdRequestBuilder | None = None,
        download_builder: SunoIdRequestBuilder | None = None,
    ) -> None:
        self._api_key = _clean_secret(api_key)
        self._contract_ref = _clean_secret(portal_contract_ref)
        self._submit_builder = submit_builder
        self._status_builder = status_builder
        self._download_builder = download_builder
        if self._contract_ref is not None:
            _require_official_suno_url(self._contract_ref, "portal_contract_ref")

    def capabilities(self) -> ProviderCapabilities:
        has_key = bool(self._api_key)
        has_contract = bool(self._contract_ref)
        configured = has_key and has_contract
        enabled: set[str] = set()
        blockers: dict[str, str] = {}
        builders = {
            "generate": self._submit_builder,
            "status": self._status_builder,
            "download": self._download_builder,
        }
        for capability, builder in builders.items():
            missing: list[str] = []
            if not has_key:
                missing.append("SUNO API credential")
            if not has_contract:
                missing.append("authenticated official portal contract reference")
            if builder is None:
                missing.append(f"portal-confirmed {capability} request builder")
            if missing:
                blockers[capability] = "missing " + ", ".join(missing)
            else:
                enabled.add(capability)
        return ProviderCapabilities(
            provider_id=self.provider_id,
            configured=configured,
            capabilities=frozenset(enabled),
            blockers=blockers,
        )

    def _validate_built_request(self, request: Any, operation: str) -> PreparedRequest:
        if not isinstance(request, PreparedRequest):
            raise ProviderError(f"Suno {operation} builder must return PreparedRequest")
        _require_official_suno_url(request.url, f"Suno {operation} request")
        # API keys belong in headers.  Reject accidental leakage into URLs,
        # query strings or request bodies before an executor ever sees it.
        secret = self._api_key or ""
        public_material = json.dumps(
            {
                "url": request.url,
                "params": request.params,
                "json_body": request.json_body,
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        if secret and secret in public_material:
            raise ProviderError("Suno credential leaked outside request headers")
        return request

    def build_generation_request(self, job: Mapping[str, Any]) -> PreparedRequest:
        self.capabilities().require("generate")
        if not isinstance(job, Mapping):
            raise ProviderError("Suno generation job must be a mapping")
        _nonempty(job.get("prompt"), "Suno job.prompt")
        if job.get("instrumental") is not True:
            raise ProviderError("Suno music jobs must be explicitly instrumental")
        assert self._submit_builder is not None and self._api_key is not None
        request = self._submit_builder(copy.deepcopy(dict(job)), self._api_key)
        return self._validate_built_request(request, "generation")

    def build_status_request(self, generation_id: str) -> PreparedRequest:
        self.capabilities().require("status")
        generation_id = _nonempty(generation_id, "Suno generation_id")
        assert self._status_builder is not None and self._api_key is not None
        request = self._status_builder(generation_id, self._api_key)
        return self._validate_built_request(request, "status")

    def build_download_request(self, generation_id: str) -> PreparedRequest:
        self.capabilities().require("download")
        generation_id = _nonempty(generation_id, "Suno generation_id")
        assert self._download_builder is not None and self._api_key is not None
        request = self._download_builder(generation_id, self._api_key)
        return self._validate_built_request(request, "download")

    @staticmethod
    def generation_cache_key(job: Mapping[str, Any]) -> str:
        return music_generation_cache_key(job)


class ShutterstockSFXProvider:
    """Official Shutterstock SFX search/license request contracts."""

    provider_id = "shutterstock_sfx"
    SEARCH_URL = "https://api.shutterstock.com/v2/sfx/search"
    LICENSE_URL = "https://api.shutterstock.com/v2/sfx/licenses"
    _SORTS = frozenset({"popular", "newest", "relevance", "random", "oldest"})
    _VIEWS = frozenset({"minimal", "full"})
    _FORMATS = frozenset({"wav", "mp3"})
    _LAYOUTS = frozenset({"ambisonic", "5.1", "stereo"})

    def __init__(
        self, *, api_token: str | None = None, subscription_id: str | None = None
    ) -> None:
        self._token = _clean_secret(api_token)
        self._subscription_id = _clean_secret(subscription_id)

    def capabilities(self) -> ProviderCapabilities:
        enabled: set[str] = set()
        blockers: dict[str, str] = {}
        if self._token:
            enabled.update({"search", "details", "redownload"})
        else:
            for name in ("search", "details", "redownload"):
                blockers[name] = "missing Shutterstock OAuth/API token"
        if self._token and self._subscription_id:
            enabled.add("license")
        else:
            missing = []
            if not self._token:
                missing.append("OAuth/API token")
            if not self._subscription_id:
                missing.append("SFX subscription_id")
            blockers["license"] = "missing " + ", ".join(missing)
        return ProviderCapabilities(
            provider_id=self.provider_id,
            configured=bool(self._token),
            capabilities=frozenset(enabled),
            blockers=blockers,
        )

    def _headers(self, *, json_content: bool = False) -> dict[str, str]:
        assert self._token is not None
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._token}",
        }
        if json_content:
            headers["Content-Type"] = "application/json"
        return headers

    def build_search_request(
        self,
        query: str,
        *,
        duration_from: int | None = None,
        duration_to: int | None = 8,
        page: int = 1,
        per_page: int = 20,
        sort: str = "relevance",
        view: str = "full",
        safe: bool = True,
    ) -> PreparedRequest:
        self.capabilities().require("search")
        query = _nonempty(query, "Shutterstock SFX query")
        if isinstance(page, bool) or not isinstance(page, int) or page < 1:
            raise ProviderError("Shutterstock page must be a positive integer")
        if isinstance(per_page, bool) or not isinstance(per_page, int) or not 1 <= per_page <= 500:
            raise ProviderError("Shutterstock per_page must be in [1, 500]")
        if sort not in self._SORTS or view not in self._VIEWS:
            raise ProviderError("unsupported Shutterstock search sort/view")
        params: dict[str, Any] = {
            "query": query,
            "page": page,
            "per_page": per_page,
            "sort": sort,
            "view": view,
            "safe": bool(safe),
        }
        for key, value in (("duration_from", duration_from), ("duration_to", duration_to)):
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ProviderError(f"Shutterstock {key} must be a non-negative integer")
            params[key] = value
        if duration_from is not None and duration_to is not None and duration_from > duration_to:
            raise ProviderError("Shutterstock duration_from exceeds duration_to")
        return PreparedRequest(
            method="GET", url=self.SEARCH_URL, headers=self._headers(), params=params
        )

    def build_details_request(
        self, sfx_id: str, *, search_id: str | None = None
    ) -> PreparedRequest:
        self.capabilities().require("details")
        sfx_id = _nonempty(sfx_id, "Shutterstock sfx_id")
        params: dict[str, Any] = {"view": "full", "library": "shutterstock"}
        if search_id is not None:
            params["search_id"] = _nonempty(search_id, "Shutterstock search_id")
        return PreparedRequest(
            method="GET",
            url=f"https://api.shutterstock.com/v2/sfx/{sfx_id}",
            headers=self._headers(),
            params=params,
        )

    def build_license_request(
        self,
        sfx_id: str,
        *,
        search_id: str,
        format: str = "wav",
        audio_layout: str = "stereo",
        metadata: Mapping[str, Any] | None = None,
    ) -> PreparedRequest:
        """Prepare (but do not execute) licensing for the selected winner only."""

        self.capabilities().require("license")
        sfx_id = _nonempty(sfx_id, "Shutterstock sfx_id")
        search_id = _nonempty(search_id, "Shutterstock search_id")
        if format not in self._FORMATS or audio_layout not in self._LAYOUTS:
            raise ProviderError("unsupported Shutterstock SFX format/audio_layout")
        item: dict[str, Any] = {
            "sfx_id": sfx_id,
            "subscription_id": self._subscription_id,
            "format": format,
            "audio_layout": audio_layout,
            "search_id": search_id,
        }
        if metadata is not None:
            metadata_copy = copy.deepcopy(dict(metadata))
            if len(metadata_copy) > 4:
                raise ProviderError("Shutterstock license metadata supports at most 4 fields")
            item["metadata"] = metadata_copy
        return PreparedRequest(
            method="POST",
            url=self.LICENSE_URL,
            headers=self._headers(json_content=True),
            json_body={"sound_effects": [item]},
        )

    def build_redownload_request(self, license_id: str) -> PreparedRequest:
        self.capabilities().require("redownload")
        license_id = _nonempty(license_id, "Shutterstock license_id")
        return PreparedRequest(
            method="POST",
            url=(
                "https://api.shutterstock.com/v2/sfx/licenses/"
                f"{license_id}/downloads"
            ),
            headers=self._headers(),
        )

    @staticmethod
    def normalise_search_response(
        payload: Mapping[str, Any], *, query: str
    ) -> dict[str, Any]:
        """Keep only auditable preview facts used before purchasing a winner."""

        if not isinstance(payload, Mapping):
            raise ProviderError("Shutterstock search response must be a mapping")
        search_id = _nonempty(payload.get("search_id"), "Shutterstock response.search_id")
        raw = payload.get("data")
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
            raise ProviderError("Shutterstock response.data must be a list")
        candidates: list[dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, Mapping):
                continue
            asset_id = str(item.get("id") or "").strip()
            assets = item.get("assets")
            preview = assets.get("preview_mp3") if isinstance(assets, Mapping) else None
            preview_url = preview.get("url") if isinstance(preview, Mapping) else None
            if not asset_id or not preview_url:
                continue
            contributor = item.get("contributor")
            candidates.append(
                {
                    "provider": "shutterstock_sfx",
                    "asset_id": asset_id,
                    "search_id": search_id,
                    "query": str(query),
                    "title": str(item.get("title") or ""),
                    "description": str(item.get("description") or ""),
                    "preview_url": str(preview_url),
                    "contributor_id": str(contributor.get("id") or "")
                    if isinstance(contributor, Mapping)
                    else "",
                }
            )
        return {
            "provider": "shutterstock_sfx",
            "search_id": search_id,
            "query": str(query),
            "candidates": candidates,
            "cache_key": stable_cache_key(
                "shutterstock_sfx_search",
                {"search_id": search_id, "query": str(query), "candidates": candidates},
            ),
        }


class LocalLicensedLibrary:
    """Deterministic fallback over user-owned, already licensed local assets."""

    provider_id = "local_licensed"

    def __init__(self, assets: Sequence[Mapping[str, Any]]) -> None:
        if isinstance(assets, (str, bytes)):
            raise ProviderError("local asset catalog must be a list")
        self._assets: dict[str, dict[str, Any]] = {}
        for raw in assets:
            self._register(raw)

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider_id=self.provider_id,
            configured=True,
            capabilities=frozenset({"search", "resolve"}),
        )

    def _register(self, raw: Mapping[str, Any]) -> None:
        if not isinstance(raw, Mapping):
            raise ProviderError("local asset entry must be a mapping")
        asset_id = _nonempty(raw.get("asset_id"), "local asset_id")
        if asset_id in self._assets:
            raise ProviderError(f"duplicate local asset_id: {asset_id}")
        path = Path(_nonempty(raw.get("path"), "local asset path")).expanduser().resolve()
        if not path.is_file():
            raise ProviderError(f"local licensed asset does not exist: {path}")
        license_info = raw.get("license")
        if not isinstance(license_info, Mapping):
            raise ProviderError("local asset license must be a mapping")
        license_copy = copy.deepcopy(dict(license_info))
        if license_copy.get("commercial_use_allowed") is not True:
            raise ProviderCapabilityError(
                f"local asset {asset_id} is not explicitly cleared for commercial use"
            )
        _nonempty(license_copy.get("license_type"), "local license_type")
        _nonempty(license_copy.get("license_ref"), "local license_ref")
        acquired_at = _nonempty(raw.get("acquired_at"), "local asset acquired_at")
        actual_digest = sha256_file(path)
        declared_digest = str(raw.get("sha256") or "").strip().casefold()
        if declared_digest and declared_digest != actual_digest:
            raise ProviderError(f"local asset {asset_id} SHA-256 mismatch")
        keywords_raw = raw.get("keywords", [])
        if not isinstance(keywords_raw, Sequence) or isinstance(keywords_raw, (str, bytes)):
            raise ProviderError("local asset keywords must be a list")
        duration = raw.get("duration")
        if duration is not None:
            duration = _finite(duration, "local asset duration")
            if duration <= 0:
                raise ProviderError("local asset duration must be positive")
        provenance = {
            "provider": self.provider_id,
            "asset_id": asset_id,
            "source_ref": str(raw.get("source_ref") or path),
            "sha256": actual_digest,
            "acquired_at": acquired_at,
            "license": license_copy,
        }
        self._assets[asset_id] = {
            "asset_id": asset_id,
            "path": str(path),
            "title": str(raw.get("title") or path.stem),
            "description": str(raw.get("description") or ""),
            "keywords": [str(item).strip() for item in keywords_raw if str(item).strip()],
            "duration": duration,
            "provenance": provenance,
        }

    @staticmethod
    def _tokens(value: str) -> set[str]:
        return set(re.findall(r"[a-zа-яё0-9]+", value.casefold()))

    def search(
        self,
        query: str,
        *,
        duration_to: float | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        query = _nonempty(query, "local asset query")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ProviderError("local search limit must be a positive integer")
        maximum = None
        if duration_to is not None:
            maximum = _finite(duration_to, "local duration_to")
            if maximum <= 0:
                raise ProviderError("local duration_to must be positive")
        query_tokens = self._tokens(query)
        ranked: list[tuple[float, str, dict[str, Any]]] = []
        for asset_id, asset in self._assets.items():
            if maximum is not None and asset["duration"] is not None and asset["duration"] > maximum:
                continue
            haystack = " ".join(
                [asset["title"], asset["description"], *asset["keywords"]]
            )
            overlap = query_tokens & self._tokens(haystack)
            if not overlap:
                continue
            score = len(overlap) / max(1, len(query_tokens))
            if query.casefold() in haystack.casefold():
                score += 0.25
            candidate = copy.deepcopy(asset)
            candidate["provider"] = self.provider_id
            candidate["score"] = round(score, 6)
            candidate["search_cache_key"] = stable_cache_key(
                "local_sfx_search",
                {"query": query.casefold(), "duration_to": maximum},
            )
            ranked.append((-score, asset_id, candidate))
        ranked.sort(key=lambda row: (row[0], row[1]))
        return [row[2] for row in ranked[:limit]]

    def resolve_asset(self, asset_id: str) -> dict[str, Any]:
        asset_id = _nonempty(asset_id, "local asset_id")
        try:
            asset = self._assets[asset_id]
        except KeyError as exc:
            raise ProviderError(f"unknown local asset_id: {asset_id}") from exc
        return {
            "path": asset["path"],
            "duration": asset["duration"],
            "provenance": copy.deepcopy(asset["provenance"]),
        }


class FreesoundProvider:
    """Freesound APIv2, disabled until a commercial API agreement is recorded."""

    provider_id = "freesound"
    SEARCH_URL = "https://freesound.org/apiv2/search/"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        oauth_access_token: str | None = None,
        commercial_api_agreement: bool = False,
        agreement_ref: str | None = None,
    ) -> None:
        self._api_key = _clean_secret(api_key)
        self._oauth_token = _clean_secret(oauth_access_token)
        self._commercial_agreement = commercial_api_agreement is True
        self._agreement_ref = _clean_secret(agreement_ref)

    def capabilities(self) -> ProviderCapabilities:
        agreement_ok = self._commercial_agreement and bool(self._agreement_ref)
        enabled: set[str] = set()
        blockers: dict[str, str] = {}
        if agreement_ok and self._api_key:
            enabled.add("search")
        else:
            missing = []
            if not agreement_ok:
                missing.append("written commercial API agreement reference")
            if not self._api_key:
                missing.append("Freesound API key")
            blockers["search"] = "missing " + ", ".join(missing)
        if agreement_ok and self._oauth_token:
            enabled.add("download_original")
        else:
            missing = []
            if not agreement_ok:
                missing.append("written commercial API agreement reference")
            if not self._oauth_token:
                missing.append("OAuth2 access token")
            blockers["download_original"] = "missing " + ", ".join(missing)
        return ProviderCapabilities(
            provider_id=self.provider_id,
            configured=agreement_ok and bool(self._api_key or self._oauth_token),
            capabilities=frozenset(enabled),
            blockers=blockers,
        )

    def build_search_request(
        self,
        query: str,
        *,
        duration_to: float | None = 8.0,
        page: int = 1,
        page_size: int = 30,
    ) -> PreparedRequest:
        self.capabilities().require("search")
        query = _nonempty(query, "Freesound query")
        if isinstance(page, bool) or not isinstance(page, int) or page < 1:
            raise ProviderError("Freesound page must be a positive integer")
        if isinstance(page_size, bool) or not isinstance(page_size, int) or not 1 <= page_size <= 150:
            raise ProviderError("Freesound page_size must be in [1, 150]")
        params: dict[str, Any] = {
            "query": query,
            "sort": "score",
            "page": page,
            "page_size": page_size,
            "fields": (
                "id,name,tags,username,license,previews,duration,url,md5,description"
            ),
        }
        if duration_to is not None:
            maximum = _finite(duration_to, "Freesound duration_to")
            if maximum <= 0:
                raise ProviderError("Freesound duration_to must be positive")
            params["filter"] = f"duration:[0.05 TO {maximum:g}]"
        assert self._api_key is not None
        return PreparedRequest(
            method="GET",
            url=self.SEARCH_URL,
            headers={"Authorization": f"Token {self._api_key}"},
            params=params,
        )

    def build_download_request(self, sound_id: str) -> PreparedRequest:
        self.capabilities().require("download_original")
        sound_id = _nonempty(sound_id, "Freesound sound_id")
        if not re.fullmatch(r"[1-9]\d*", sound_id):
            raise ProviderError("Freesound sound_id must be a positive integer")
        assert self._oauth_token is not None
        return PreparedRequest(
            method="GET",
            url=f"https://freesound.org/apiv2/sounds/{sound_id}/download/",
            headers={"Authorization": f"Bearer {self._oauth_token}"},
        )

    def normalise_search_response(self, payload: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Drop NonCommercial results and retain attribution/license metadata."""

        self.capabilities().require("search")
        raw = payload.get("results") if isinstance(payload, Mapping) else None
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
            raise ProviderError("Freesound response.results must be a list")
        output: list[dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, Mapping):
                continue
            license_name = str(item.get("license") or "").strip()
            folded = license_name.casefold()
            if not license_name or "noncommercial" in folded or "by-nc" in folded:
                continue
            previews = item.get("previews")
            preview_url = (
                previews.get("preview-hq-mp3") if isinstance(previews, Mapping) else None
            )
            if not item.get("id") or not preview_url:
                continue
            output.append(
                {
                    "provider": self.provider_id,
                    "asset_id": str(item["id"]),
                    "title": str(item.get("name") or ""),
                    "description": str(item.get("description") or ""),
                    "preview_url": str(preview_url),
                    "duration": item.get("duration"),
                    "username": str(item.get("username") or ""),
                    "content_license": license_name,
                    "source_ref": str(item.get("url") or ""),
                    "md5": str(item.get("md5") or ""),
                    "api_agreement_ref": self._agreement_ref,
                }
            )
        return output


__all__ = [
    "FreesoundProvider",
    "LocalLicensedLibrary",
    "MusicProvider",
    "PreparedRequest",
    "ProviderCapabilities",
    "ProviderCapabilityError",
    "ProviderError",
    "ProviderNotConfiguredError",
    "SFXSearchProvider",
    "ShutterstockSFXProvider",
    "SunoOfficialProvider",
]
