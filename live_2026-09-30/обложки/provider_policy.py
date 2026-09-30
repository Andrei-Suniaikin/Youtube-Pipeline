# -*- coding: utf-8 -*-
"""Единая policy генераторов: canonical provider → credential → dispatch/group."""

from dataclasses import dataclass
from typing import Dict


class InvalidGenerationProvider(ValueError):
    """Provider отсутствует в публичном canonical contract."""


@dataclass(frozen=True)
class ProviderPolicy:
    name: str
    credential_family: str
    dispatch: str
    scheduler_group: str


_POLICIES: Dict[str, ProviderPolicy] = {
    "flow": ProviderPolicy("flow", "fastgen", "fastgen", "fastgen"),
    "flower": ProviderPolicy("flower", "fastgen", "fastgen", "fastgen"),
    "grok": ProviderPolicy("grok", "fastgen", "fastgen", "fastgen"),
    # Тот же Grok, но напрямую в xAI, мимо Fast Gen: свой ключ, своя очередь,
    # свои деньги. Отдельное семейство credential — иначе ключ Fast Gen ушёл бы
    # на api.x.ai, а это утечка ключа на чужой хост.
    "grok_api": ProviderPolicy("grok_api", "xai", "xai", "xai"),
    "openai_img": ProviderPolicy("openai_img", "fastgen", "fastgen", "fastgen"),
    "veo": ProviderPolicy("veo", "veo", "veo", "veo"),
    "gpt_image2": ProviderPolicy("gpt_image2", "openai", "gpt_image2", "gpt_image2"),
}


def canonical_generation_provider(value, default: str = "flow") -> str:
    """Возвращает только canonical provider; пробелы/регистр не нормализует молча."""
    if value is None or value == "":
        raw = default
    else:
        raw = str(value)
    if raw != raw.strip():
        raise InvalidGenerationProvider(
            f"generation_provider must not contain surrounding whitespace: {raw!r}"
        )
    if raw != raw.lower():
        raise InvalidGenerationProvider(
            f"generation_provider must be lowercase canonical value: {raw!r}"
        )
    if raw not in _POLICIES:
        raise InvalidGenerationProvider(
            f"unsupported generation_provider {raw!r}; "
            f"allowed: {', '.join(sorted(_POLICIES))}"
        )
    return raw


def provider_policy(value) -> ProviderPolicy:
    return _POLICIES[canonical_generation_provider(value)]


def provider_credential_family(value) -> str:
    return provider_policy(value).credential_family


def provider_scheduler_group(value) -> str:
    return provider_policy(value).scheduler_group


def effective_simple_mode_provider(base_provider, override_provider="") -> str:
    """SIMPLE_MODE_PROVIDER разрешён только внутри той же credential family."""
    base = canonical_generation_provider(base_provider)
    if override_provider is None or override_provider == "":
        return base
    override = canonical_generation_provider(override_provider)
    if provider_credential_family(override) != provider_credential_family(base):
        raise InvalidGenerationProvider(
            "SIMPLE_MODE_PROVIDER cannot change credential family "
            f"({base!r} -> {override!r})"
        )
    return override


def supported_generation_providers():
    return tuple(sorted(_POLICIES))
