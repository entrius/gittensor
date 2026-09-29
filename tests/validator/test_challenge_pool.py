# The MIT License (MIT)
# Copyright © 2026 Entrius

"""The validator reads the challenge scorecard: registered hotkeys get their weights by UID; a document read_scorecard
refuses (here: the compute schema) gives an empty pool."""

from types import SimpleNamespace
from typing import Any, cast

from gittensor.controller.pay.scorecard import SCHEMA as COMPUTE_SCHEMA
from gittensor.controller.pay.scorecard import write_scorecard
from gittensor.validator.challenge_pool import SCHEMA, challenge_pool_for
from tests.controller.test_scorecard import HK_A, HK_B, ISSUED


def scorecard_doc(schema: str = SCHEMA) -> dict:
    return {
        'schema': schema,
        'issued_at': ISSUED,
        'valid_until': ISSUED + 1_200,
        'recycle_share': 0.25,
        'hotkeys': [
            {'hotkey': HK_A, 'weight': 0.5},
            {'hotkey': HK_B, 'weight': 0.2},
            {'hotkey': 'unregistered', 'weight': 0.05},
        ],
        'challenges': {
            'routing-cvrptw': {
                'king': {'hotkey': HK_A, 'submission_sha256': 'ab' * 32, 'score': 1.12, 'commit_block': 100},
                'evaluated': [],
            }
        },
        'attestation': {'kind': 'dev'},
    }


def validator() -> Any:
    return cast(Any, SimpleNamespace(metagraph=SimpleNamespace(hotkeys=['recycle', HK_A, HK_B, 'oss-miner'])))


def test_a_valid_scorecard_maps_its_registered_hotkeys_to_uids(tmp_path):
    path, sha = write_scorecard(tmp_path, scorecard_doc())
    pool = challenge_pool_for(validator(), str(path), now=ISSUED + 60)
    assert pool.sha256 == sha and pool.rewards == {1: 0.5, 2: 0.2}  # 'unregistered' has no UID


def test_a_wrong_schema_scorecard_is_refused(tmp_path):
    path, _ = write_scorecard(tmp_path, scorecard_doc(COMPUTE_SCHEMA))
    pool = challenge_pool_for(validator(), str(path), now=ISSUED + 60)
    assert pool.sha256 is None and pool.rewards == {} and f'not a {SCHEMA}' in pool.reason
