# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Bounds on per-repo label scoring fields in ``master_repositories.json``."""

import json
from pathlib import Path

import pytest

from gittensor.validator.utils.load_weights import load_master_repo_weights

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MASTER_JSON = _REPO_ROOT / 'gittensor' / 'validator' / 'weights' / 'master_repositories.json'


def _raw_entries():
    with open(_MASTER_JSON, encoding='utf-8') as f:
        data = json.load(f)
    assert isinstance(data, dict)
    return sorted(data.items())


@pytest.mark.parametrize('repo_name,metadata', _raw_entries())
def test_repo_label_fields_shape(repo_name, metadata):
    """Every repo: ≤10 label_multipliers keys, multipliers in [0,20], default in [0,20]."""
    lm = metadata.get('label_multipliers')
    if lm is not None:
        assert isinstance(lm, dict), f'{repo_name}: label_multipliers must be an object'
        assert len(lm) <= 10, f'{repo_name}: at most 10 label_multipliers entries'
        for key, val in lm.items():
            assert isinstance(val, (int, float)), f'{repo_name}: multiplier for {key!r} must be numeric'
            assert 0.0 <= float(val) <= 20.0, f'{repo_name}: multiplier for {key!r} out of [0, 20]'

    dlm = metadata.get('default_label_multiplier')
    if dlm is not None:
        v = float(dlm)
        assert 0.0 <= v <= 20.0, f'{repo_name}: default_label_multiplier out of [0, 20]'


def test_loaded_repository_config_matches_bounds():
    """``load_master_repo_weights`` produces configs consistent with raw JSON rules."""
    repos = load_master_repo_weights()
    assert repos
    for name, cfg in repos.items():
        if cfg.label_multipliers:
            assert len(cfg.label_multipliers) <= 10, name
            for mult in cfg.label_multipliers.values():
                assert 0.0 <= mult <= 20.0, name
        assert 0.0 <= cfg.default_label_multiplier <= 20.0, name
