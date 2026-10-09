# The MIT License (MIT)
# Copyright © 2025 Entrius

"""The customer's side of the rental API (vault 29 §6): one small client over urllib, the saved login
(``~/.gittensor/rent.json``: url, key, and the local names a customer gave rentals), and the SSH keys an order
carries. Nothing here prints; the commands in ``rent.py`` do."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from gittensor import __version__
from gittensor.cli.helpers import GITTENSOR_DIR

RENT_CONFIG = GITTENSOR_DIR / 'rent.json'
DEFAULT_URL = 'https://gt-test.venturalabs.ai'  # the testnet product; `gitt rent login --url` for another
ENV_KEYS = ('GITTENSOR_API_KEY', 'GT_API_KEY')  # the runbook's name first, the short one for typing
ENV_URL = 'GT_API_URL'
OPEN_STATES = ('requested', 'starting', 'active', 'ending')
TIMEOUT_S = 20.0


class ApiError(Exception):
    """What the API refused or could not do, with its own words: ``gitt rent`` prints ``message`` as is."""

    def __init__(self, message: str, kind: str = 'api_error', status: int = 0):
        super().__init__(message)
        self.message, self.kind, self.status = message, kind, status


@dataclass
class RentConfig:
    """The saved login and the customer's local names for rentals (the API has ids, not names)."""

    url: str = DEFAULT_URL
    key: str = ''
    names: dict[str, str] = field(default_factory=dict)  # name -> rental id

    @classmethod
    def load(cls, path: Path | None = None) -> RentConfig:
        try:
            raw = json.loads((path or RENT_CONFIG).read_text())
        except (OSError, ValueError):
            raw = {}
        cfg = cls(
            url=str(raw.get('url') or DEFAULT_URL),
            key=str(raw.get('key') or ''),
            names={str(k): str(v) for k, v in (raw.get('names') or {}).items()},
        )
        # the environment wins over the file: an agent exports GITTENSOR_API_KEY (as /llms.txt says) and never logs in
        cfg.url = os.environ.get(ENV_URL) or cfg.url
        cfg.key = next((os.environ[k] for k in ENV_KEYS if os.environ.get(k)), cfg.key)
        return cfg

    def save(self, path: Path | None = None) -> None:
        path = path or RENT_CONFIG
        path.parent.mkdir(parents=True, exist_ok=True)
        body = {'url': self.url, 'key': self.key, 'names': self.names}
        tmp = path.with_suffix('.tmp')
        tmp.write_text(json.dumps(body, indent=2) + '\n')
        tmp.chmod(0o600)
        os.replace(tmp, path)

    def name_of(self, rental_id: str) -> str:
        return next((n for n, i in self.names.items() if i == rental_id), '')


class RentApi:
    """``GET /rentals/offers``, ``GET /balance``, ``POST /rentals``, ``GET /rentals[/:id]``, ``DELETE /rentals/:id``,
    ``POST /rentals/:id/extend``, bearer-authenticated. ``opener`` is urlopen or a test double."""

    def __init__(self, url: str, key: str, opener=None, timeout: float = TIMEOUT_S):
        self.url, self.key, self.timeout = url.rstrip('/'), key, timeout
        self._open = opener or urllib.request.urlopen  # looked up now, not at import: tests swap it

    def _call(self, method: str, path: str, body: dict | None = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f'{self.url}{path}', data=data, method=method)
        req.add_header('Authorization', f'Bearer {self.key}')
        req.add_header('Accept', 'application/json')
        req.add_header('User-Agent', f'gitt/{__version__}')  # Cloudflare answers urllib's own agent with a bare 403
        if data is not None:
            req.add_header('Content-Type', 'application/json')
        try:
            with self._open(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                err = json.loads(raw).get('error') or {}
            except ValueError:
                err = {}
            text = raw.decode(errors='replace').strip()
            message = err.get('message') or (f'HTTP {e.code}: {text[:120]}' if text and not err else f'HTTP {e.code}')
            if e.code == 401 and not err:
                message = 'not logged in: `gitt rent login <api key>` (keys: the app, /keys)'
            elif e.code == 403 and not err:  # the edge (Cloudflare) answering, not the API: a wrong URL or agent
                message = (
                    f'HTTP 403 from {self.url} with no API error: is the URL the product API? `gitt rent login --url`'
                )
            elif e.code == 404 and not err:
                message = f'HTTP 404 at {self.url}{path}: is the URL the product API? `gitt rent login --url`'
            raise ApiError(message, err.get('type') or f'http_{e.code}', e.code) from None
        except urllib.error.URLError as e:
            raise ApiError(
                f'{self.url}: {e.reason} (unreachable: the URL, your network? `gitt rent login --url`)', 'unreachable'
            ) from None
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except ValueError:
            raise ApiError(f'{method} {path}: the reply was not JSON', 'bad_reply') from None

    def offers(self) -> dict:
        return self._call('GET', '/rentals/offers')

    def balance(self) -> dict:
        return self._call('GET', '/balance')

    def rentals(self) -> list[dict]:
        return list(self._call('GET', '/rentals').get('rentals') or [])

    def rental(self, rental_id: str) -> dict:
        return self._call('GET', f'/rentals/{urllib.parse.quote(rental_id)}')

    def order(self, body: dict) -> dict:
        return self._call('POST', '/rentals', body)

    def stop(self, rental_id: str) -> dict:
        return self._call('DELETE', f'/rentals/{urllib.parse.quote(rental_id)}')

    def extend(self, rental_id: str, hours: float) -> dict:
        return self._call('POST', f'/rentals/{urllib.parse.quote(rental_id)}/extend', {'hours': hours})


def ssh_public_keys(paths: list[Path] | None = None, ssh_dir: Path | None = None) -> list[str]:
    """The keys an order carries: the files given, else every ``id_*.pub`` under ``~/.ssh``."""
    if not paths:
        ssh_dir = ssh_dir or Path.home() / '.ssh'
        paths = sorted(ssh_dir.glob('id_*.pub'))
    keys: list[str] = []
    for p in paths:
        text = p.read_text().strip()
        if text and text not in keys:
            keys.append(text)
    return keys


def find_offer(offers: dict, gpu_type: str, gpu_count: int) -> tuple[str, dict] | None:
    """The offer row for a type (case-insensitive) and box size: ``(canonical type, box row)``, or None."""
    for o in offers.get('offers') or []:
        if str(o.get('gpu_type', '')).lower() != gpu_type.lower():
            continue
        for b in o.get('boxes') or []:
            if int(b.get('gpu_count', 0)) == gpu_count:
                return str(o['gpu_type']), b
    return None


def resolve(cfg: RentConfig, rentals: list[dict], ref: str | None) -> dict:
    """The rental a customer means: a local name, an id, a unique id prefix; or, with no ref, the one open rental."""
    if not ref:
        open_ = [r for r in rentals if r.get('state') in OPEN_STATES]
        if len(open_) == 1:
            return open_[0]
        if not open_:
            raise ApiError('no open rental: `gitt rent up <gpu type>` first', 'no_rental')
        names = ', '.join(cfg.name_of(r['id']) or r['id'] for r in open_)
        raise ApiError(f'{len(open_)} open rentals: say which ({names})', 'ambiguous')
    rid = cfg.names.get(ref, ref)
    exact = [r for r in rentals if r.get('id') == rid]
    if exact:
        return exact[0]
    prefix = [r for r in rentals if str(r.get('id', '')).startswith(rid)]
    if len(prefix) == 1:
        return prefix[0]
    if len(prefix) > 1:
        raise ApiError(f'{ref!r} matches {len(prefix)} rentals: give more of the id', 'ambiguous')
    raise ApiError(f'no rental {ref!r} (`gitt rent ps --all` lists yours)', 'no_rental')
