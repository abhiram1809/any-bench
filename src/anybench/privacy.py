"""Redact configured credentials before diagnostics are persisted."""
from __future__ import annotations

import os


def redact(value: str, config) -> str:
    names = ([config.api_key_env] if config.api_key_env else []) + list(config.env.values())
    for name in names:
        secret = os.environ.get(name)
        if secret:
            value = value.replace(secret, "[REDACTED]")
    return value


def redact_value(value, config):
    if isinstance(value, str):
        return redact(value, config)
    if isinstance(value, list):
        return [redact_value(item, config) for item in value]
    if isinstance(value, dict):
        return {key: redact_value(item, config) for key, item in value.items()}
    return value
