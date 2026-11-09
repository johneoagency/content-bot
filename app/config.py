"""Configuration and secret loading.

Hard rules enforced here:
  * Secrets come from secrets/.env (or the environment), never from config.json
    and never logged.
  * config.json carries only non-secret settings and *verified* Telegram IDs.
  * Prompt templates are read from prompts/ and each gets a stable version hash
    so every job can snapshot exactly which text it used.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - dotenv is a hard dependency at runtime
    load_dotenv = None

# Project root = parent of the app/ package.
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
SECRETS_DIR = ROOT / "secrets"
PROMPTS_DIR = ROOT / "prompts"

TOPIC_KEYS = (
    "content_input",
    "content_output",
    "video_input",
    "video_output",
    "instagram_content",
    "tech_log",
)

# Secret names that must never appear in logs or Telegram messages.
SECRET_ENV_KEYS = (
    "TELEGRAM_BOT_TOKEN",
    "WAVESPEED_API_KEY",
    "APIFY_TOKEN",
    "TELEGRAM_API_ID",
    "TELEGRAM_API_HASH",
)


class ConfigError(RuntimeError):
    """Raised for missing files or invalid configuration values."""


@dataclass
class Secrets:
    telegram_bot_token: str
    wavespeed_api_key: str
    apify_token: str = ""
    telegram_api_id: str = ""
    telegram_api_hash: str = ""

    @property
    def has_apify(self) -> bool:
        return bool(self.apify_token)

    @property
    def has_local_api_credentials(self) -> bool:
        return bool(self.telegram_api_id and self.telegram_api_hash)


@dataclass
class Prompt:
    name: str
    text: str
    version: str  # short sha256 of the text

    @classmethod
    def load(cls, name: str, path: Path) -> "Prompt":
        if not path.exists():
            raise ConfigError(f"Missing prompt file: {path}")
        text = path.read_text(encoding="utf-8").strip()
        version = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        return cls(name=name, text=text, version=version)


@dataclass
class Config:
    raw: dict[str, Any]
    secrets: Secrets
    prompts: dict[str, Prompt] = field(default_factory=dict)
    path: Path | None = None

    # -- convenience accessors ------------------------------------------------
    @property
    def group_chat_id(self) -> int:
        return int(self.raw.get("group_chat_id", 0) or 0)

    @property
    def admin_user_ids(self) -> list[int]:
        return [int(x) for x in self.raw.get("admin_user_ids", []) or []]

    @property
    def topics(self) -> dict[str, int | None]:
        t = self.raw.get("topics", {}) or {}
        return {k: t.get(k) for k in TOPIC_KEYS}

    def topic_id(self, key: str) -> int | None:
        return self.topics.get(key)

    def topic_for_thread(self, thread_id: int | None) -> str | None:
        """Reverse lookup: which named topic owns this message_thread_id."""
        for key, tid in self.topics.items():
            if tid is not None and int(tid) == int(thread_id or 0):
                return key
        return None

    def get(self, *path: str, default: Any = None) -> Any:
        node: Any = self.raw
        for key in path:
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node

    @property
    def config_version(self) -> str:
        """Stable hash of the non-secret config, used to tag jobs/snapshots."""
        stable = json.dumps(self.raw, sort_keys=True, default=str)
        return hashlib.sha256(stable.encode("utf-8")).hexdigest()[:16]

    def is_admin(self, user_id: int | None) -> bool:
        return user_id is not None and int(user_id) in self.admin_user_ids

    # -- validation -----------------------------------------------------------
    def missing_activation_items(self) -> list[str]:
        """Human-readable list of what still blocks real operation."""
        missing: list[str] = []
        if not self.secrets.telegram_bot_token:
            missing.append("TELEGRAM_BOT_TOKEN")
        if not self.secrets.wavespeed_api_key:
            missing.append("WAVESPEED_API_KEY")
        if not self.group_chat_id:
            missing.append("group_chat_id")
        if not self.admin_user_ids:
            missing.append("admin_user_ids")
        for key in TOPIC_KEYS:
            if self.topics.get(key) in (None, 0):
                missing.append(f"topics.{key}")
        if not self.get("reference", "image_path"):
            missing.append("reference.image_path")
        return missing


def load_secrets() -> Secrets:
    if load_dotenv is not None:
        env_path = SECRETS_DIR / ".env"
        if env_path.exists():
            load_dotenv(env_path)
        else:
            load_dotenv()  # fall back to process environment / .env in CWD
    return Secrets(
        telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
        wavespeed_api_key=os.environ.get("WAVESPEED_API_KEY", "").strip(),
        apify_token=os.environ.get("APIFY_TOKEN", "").strip(),
        telegram_api_id=os.environ.get("TELEGRAM_API_ID", "").strip(),
        telegram_api_hash=os.environ.get("TELEGRAM_API_HASH", "").strip(),
    )


def load_config(config_path: str | os.PathLike[str] | None = None) -> Config:
    path = Path(config_path) if config_path else (ROOT / "config.json")
    if not path.exists():
        raise ConfigError(
            f"Missing {path.name}. Copy config.example.json to config.json and edit it."
        )
    raw = json.loads(path.read_text(encoding="utf-8"))
    secrets = load_secrets()
    prompts = {
        "image": Prompt.load("image", PROMPTS_DIR / "image.txt"),
        "video": Prompt.load("video", PROMPTS_DIR / "video.txt"),
        "caption": Prompt.load("caption", PROMPTS_DIR / "caption.txt"),
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return Config(raw=raw, secrets=secrets, prompts=prompts, path=path)


def redact(text: str, secrets: Secrets) -> str:
    """Strip any known secret value out of a string before it is logged/sent."""
    out = text
    for value in (
        secrets.telegram_bot_token,
        secrets.wavespeed_api_key,
        secrets.apify_token,
        secrets.telegram_api_hash,
    ):
        if value and value in out:
            out = out.replace(value, "***")
    return out
