# The MIT License (MIT)
# Copyright © 2025 Entrius

"""The compute pool on the validator (vault ``26`` §1, ``23`` §8a): read the controller's scorecard, sign and commit it,
and hand its weights to the emission blend. Nothing else — the controller does the checking and the accounting.

* ``COMPUTE_SCORECARD_PATH`` unset: nothing here runs and the blend is today's.
* A scorecard that fails ``read_scorecard`` (sha256 mismatch, past ``valid_until``, malformed) gives an empty pool:
  the whole compute share recycles. Never last-known weights: a dead controller must not keep paying.
* A valid one: its sha256 is committed on chain as ``gt-scorecard:<sha256>`` with ``subtensor.set_commitment``, an
  extrinsic the validator hotkey signs. The hotkey's detached signature over the sha256 is also written to the
  validator's own ``validator_commit.json`` (``COMPUTE_COMMIT_PATH``; else beside its ``state.npz`` under the neuron's
  full path; else ``~/.bittensor/gittensor/``), for anyone checking a published copy of the document. Never into the
  controller's scorecard directory: that is read-only input (9/16 soak: mounted read-only on a shared host, the write
  failed every cycle). Only a new sha256 is committed; a failed commit is retried next round and does not block pay.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Optional, Sequence

import bittensor as bt

from gittensor.controller.pay.scorecard import ScorecardError, read_scorecard

if TYPE_CHECKING:
    from neurons.validator import Validator

COMMITMENT_PREFIX = 'gt-scorecard:'
COMMIT_LOG = 'validator_commit.json'
DEFAULT_COMMIT_DIR = '~/.bittensor/gittensor'


@dataclass
class ComputePool:
    rewards: Dict[int, float] = field(default_factory=dict)  # uid -> share of the compute pool
    sha256: Optional[str] = None  # None: no usable scorecard, the compute share recycles
    reason: str = ''


def pool_from_scorecard(path: str | Path, hotkeys: Sequence[str], now: float) -> ComputePool:
    """Weights by UID for the hotkeys registered now; a hotkey that is not registered is simply not paid (recycled)."""
    try:
        doc, sha = read_scorecard(path, now)
    except ScorecardError as e:
        return ComputePool(reason=str(e))
    uid_of = {hotkey: uid for uid, hotkey in enumerate(hotkeys)}
    rewards: Dict[int, float] = {}
    for entry in doc['hotkeys']:
        uid = uid_of.get(entry['hotkey'])
        weight = float(entry['weight'])
        if uid is not None and weight > 0:
            rewards[uid] = rewards.get(uid, 0.0) + weight
    return ComputePool(rewards, sha)


def commit_path_for(validator: Any, override: str | Path | None = None) -> Path:
    """Where this validator keeps its commit record: ``override`` (``COMPUTE_COMMIT_PATH``), else beside its own state
    (``config.neuron.full_path``), else ``DEFAULT_COMMIT_DIR``."""
    if override:
        return Path(override).expanduser()
    full_path = getattr(getattr(getattr(validator, 'config', None), 'neuron', None), 'full_path', None)
    if full_path:
        return Path(str(full_path)).expanduser() / COMMIT_LOG
    return Path(DEFAULT_COMMIT_DIR).expanduser() / COMMIT_LOG


def sign_and_commit(subtensor: Any, wallet: Any, netuid: int, sha256: str, log_path: Path | None, now: float) -> bool:
    """Sign ``sha256`` with the hotkey and commit it; the record goes to ``log_path`` (the validator's own state). True
    when the chain accepted the commitment."""
    record: Dict[str, Any] = {
        'sha256': sha256,
        'hotkey': wallet.hotkey.ss58_address,
        'signature': '0x' + bytes(wallet.hotkey.sign(sha256.encode())).hex(),
        'commitment': COMMITMENT_PREFIX + sha256,
        'at': now,
        'committed': False,
        'error': '',
    }
    try:
        response = subtensor.set_commitment(wallet=wallet, netuid=netuid, data=record['commitment'])
        record['committed'] = bool(getattr(response, 'success', response))
        if not record['committed']:
            record['error'] = str(getattr(response, 'message', 'rejected'))[:300]
    except Exception as e:
        record['error'] = f'{type(e).__name__}: {e}'[:300]
    if log_path is not None:
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = log_path.with_name(log_path.name + '.tmp')
            tmp.write_text(json.dumps(record, indent=1))
            tmp.replace(log_path)
        except OSError as e:
            bt.logging.warning(f'Compute pool: could not write {log_path} ({e})')
    return record['committed']


def compute_pool_for(
    self: 'Validator', path: str, now: Optional[float] = None, commit_path: str | Path | None = None
) -> ComputePool:
    """``path`` is the controller's scorecard (read-only input); ``commit_path`` overrides where the commit record
    goes (``commit_path_for``)."""
    now = time.time() if now is None else now
    pool = pool_from_scorecard(path, list(self.metagraph.hotkeys), now)
    if pool.sha256 is None:
        bt.logging.warning(f'Compute pool: scorecard refused ({pool.reason}); the compute share recycles')
        return pool
    if pool.sha256 != getattr(self, 'last_scorecard_sha256', None):
        if sign_and_commit(
            self.subtensor,
            self.wallet,
            int(self.metagraph.netuid),
            pool.sha256,
            commit_path_for(self, commit_path),
            now,
        ):
            setattr(self, 'last_scorecard_sha256', pool.sha256)
            bt.logging.info(f'Compute pool: committed scorecard {pool.sha256[:16]}…')
        else:
            bt.logging.warning(f'Compute pool: commit of scorecard {pool.sha256[:16]}… failed; retrying next round')
    bt.logging.info(f'Compute pool: scorecard {pool.sha256[:16]}… pays {len(pool.rewards)} registered miner(s)')
    return pool
