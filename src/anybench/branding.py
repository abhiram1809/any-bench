"""Shared, offline brand identities for harnesses and observed API endpoints."""
from __future__ import annotations

from functools import lru_cache
import ipaddress
import json
import os
from pathlib import Path
from urllib.parse import urlsplit


@lru_cache(maxsize=1)
def catalog() -> dict:
    return json.loads((Path(__file__).parent / 'brand_assets' / 'catalog.json').read_text())


def endpoint_host(url: str) -> str:
    """Retain only a valid hostname, never credentials, path, query or fragment."""
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ''
        if parsed.scheme not in {'http', 'https'} or not host:
            return ''
        parsed.port  # Reject malformed ports.
        host = host.rstrip('.').lower()
        # Reject whitespace and markup even in imported, unvalidated records.
        if any(char not in 'abcdefghijklmnopqrstuvwxyz0123456789.-:' for char in host):
            return ''
        return host
    except (ValueError, TypeError):
        return ''


def configured_host(config: dict, *, environment: bool = False) -> str:
    url = config.get('base_url', '')
    harness = config.get('harness', 'anybench')
    if not url and harness in {'codex', 'claude'}:
        variable = 'OPENAI_BASE_URL' if harness == 'codex' else 'ANTHROPIC_BASE_URL'
        mapped = config.get('env', {}).get(variable)
        if mapped:
            # Historical manifests cannot tell us what an environment value was.
            return endpoint_host(os.environ.get(mapped, '')) if environment else ''
        url = 'https://api.openai.com' if harness == 'codex' else 'https://api.anthropic.com'
    return endpoint_host(url)


def harness_brand(harness: str) -> dict[str, str]:
    item = catalog()['harnesses'].get(harness)
    return dict(item) if item else {'label': harness or 'Unknown harness', 'icon': 'generic'}


def provider_brand(host: str) -> dict[str, str]:
    # Normalize old/imported host values too; never echo a raw URL into reports.
    host = endpoint_host('https://' + (f'[{host}]' if ':' in host and '/' not in host else host)) if host else ''
    for icon, item in catalog()['providers'].items():
        if host in item['hosts']:
            return {'label': item['label'], 'icon': icon}
    if host:
        try:
            local = ipaddress.ip_address(host).is_loopback
        except ValueError:
            local = host == 'localhost' or host.endswith('.localhost')
        return {'label': 'Local endpoint' if local else 'Custom endpoint', 'icon': 'generic'}
    return {'label': 'Provider unavailable', 'icon': 'generic'}


def group_brands(records: list, manifest: dict | None = None) -> dict:
    inputs = (manifest or {}).get('inputs', {})
    inputs = inputs.get('run_inputs') or inputs
    configs = {item['name']: item for item in inputs.get('configs', [])}
    providers = {}
    for record in records:
        host = record.provider_host
        if host is None:
            host = configured_host(configs.get(record.model, {}))
        brand = provider_brand(host)
        providers[brand['label']] = brand
    return {'harness': harness_brand(records[0].harness),
            'providers': [providers[key] for key in sorted(providers)]}
