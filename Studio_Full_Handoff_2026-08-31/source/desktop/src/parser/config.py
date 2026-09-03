"""Конфиг парсера: профиль ниши (YAML) + ключи API."""
import json
import os

import yaml

BASE = os.path.dirname(os.path.abspath(__file__))
KEYS_PATH = os.path.expanduser("~/video_studio_api_keys.json")
UA = "VideoStudioParser/0.1 (visual sourcing; contact: owner@example.invalid)"


def load_niche(name: str) -> dict:
    path = os.path.join(BASE, "niches", f"{name}.yaml")
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_keys() -> dict:
    with open(KEYS_PATH, encoding="utf-8") as f:
        return json.load(f)


def openai_key() -> str:
    k = load_keys().get("openai", "")
    if not k:
        raise RuntimeError("Нет ключа 'openai' в ~/video_studio_api_keys.json")
    return k
