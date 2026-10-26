"""Loads secrets from .env and non-secret settings from config/settings.yaml."""

import os
from functools import lru_cache
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")


@lru_cache
def settings() -> dict:
    with open(ROOT / "config" / "settings.yaml") as f:
        return yaml.safe_load(f)


def env(name: str, default: str | None = None) -> str:
    value = os.getenv(name, default)
    if value is None or value == "":
        raise RuntimeError(f"Missing required environment variable {name} (see .env.example)")
    return value


def database_url() -> str:
    """DATABASE_URL wins when set (hosted Postgres in CI/production); otherwise the
    local docker-compose database described by the POSTGRES_* variables."""
    if url := os.getenv("DATABASE_URL"):
        return url
    return (
        f"postgresql://{env('POSTGRES_USER')}:{env('POSTGRES_PASSWORD')}"
        f"@{env('POSTGRES_HOST', 'localhost')}:{env('POSTGRES_PORT', '5433')}/{env('POSTGRES_DB')}"
    )
