# The MIT License (MIT)
# Copyright © 2025 Entrius
"""Tests for the on-chain registry loader: identity shim, param decoding,
fallback ladder, and snapshot atomicity."""

import json
from types import SimpleNamespace

from gittensor.validator.repo_registry.loader import RegistryLoader

SNAPSHOT = 3600
SNAPSHOT_2 = 7200
HASH_1 = '0xsnap1'
HASH_2 = '0xsnap2'


def _repo(github_id=101, full_name='Owner/Repo', active=True):
    return SimpleNamespace(github_id=github_id, full_name=full_name, active=active)


def _state(repos, paused=False, params=None, labels=None, patterns=None):
    return {
        'repos': repos,
        'paused': paused,
        'params': params or {},
        'labels': labels or {},
        'patterns': patterns or {},
    }


class FakeRegistryClient:
    """Canned contract state keyed by block hash; missing hash = unreachable."""

    def __init__(self, states, hashes=None):
        self.states = states
        hashes = hashes or {SNAPSHOT: HASH_1, SNAPSHOT_2: HASH_2}
        self.subtensor = SimpleNamespace(substrate=SimpleNamespace(get_block_hash=hashes.get))
        self.registry_reads = 0
        self.read_ats = []

    def _state(self, at):
        self.read_ats.append(at)
        return self.states.get(at)

    def get_registry(self, at=None):
        self.registry_reads += 1
        state = self._state(at)
        if state is None:
            return None
        return SimpleNamespace(paused=state['paused'])

    def get_all_repos(self, at=None):
        return [repo for repo in self._state(at)['repos'] if repo.active]

    def get_params(self, github_id, at=None):
        return self._state(at)['params'].get(github_id, {})

    def get_label_multipliers(self, github_id, at=None):
        return self._state(at)['labels'].get(github_id, {})

    def get_branch_patterns(self, github_id, at=None):
        return self._state(at)['patterns'].get(github_id, [])


def _loader(client, tmp_path):
    return RegistryLoader(client, tmp_path)


class TestIdentityShim:
    def test_maps_github_ids_to_lowercase_name_keys(self, tmp_path):
        client = FakeRegistryClient({HASH_1: _state([_repo(101, 'Owner/Repo'), _repo(202, 'other/lib')])})
        configs = _loader(client, tmp_path).load(SNAPSHOT)
        assert set(configs) == {'owner/repo', 'other/lib'}
        assert all(config.emission_share == 0.0 for config in configs.values())  # consensus-voted only

    def test_inactive_repos_excluded(self, tmp_path):
        client = FakeRegistryClient({HASH_1: _state([_repo(101, 'a/b'), _repo(202, 'c/d', active=False)])})
        assert set(_loader(client, tmp_path).load(SNAPSHOT)) == {'a/b'}

    def test_rename_maps_forward_only(self, tmp_path):
        client = FakeRegistryClient(
            {HASH_1: _state([_repo(101, 'old/name')]), HASH_2: _state([_repo(101, 'new/name')])}
        )
        loader = _loader(client, tmp_path)
        assert set(loader.load(SNAPSHOT)) == {'old/name'}
        assert set(loader.load(SNAPSHOT_2)) == {'new/name'}


class TestParamDecoding:
    def test_decodes_fp6_and_integer_params_into_config_fields(self, tmp_path):
        params = {
            1: 250_000,  # issue_discovery_share 0.25
            3: 12_500_000,  # fixed_base_score 12.5
            4: 100_000,  # maintainer_cut 0.1
            5: 1,  # trusted_label_pipeline
            6: 3,  # min_valid_merged_prs
            9: 1_000_000_000,  # open_pr_threshold_token_score 1000.0
            17: 30,  # pr_lookback_days
            24: 14_000_000,  # sigmoid_midpoint_days 14.0
        }
        client = FakeRegistryClient({HASH_1: _state([_repo(101, 'a/b')], params={101: params})})
        config = _loader(client, tmp_path).load(SNAPSHOT)['a/b']
        assert config.issue_discovery_share == 0.25
        assert config.fixed_base_score == 12.5
        assert config.maintainer_cut == 0.1
        assert config.trusted_label_pipeline is True
        assert config.eligibility.min_valid_merged_prs == 3
        assert config.eligibility.open_pr_threshold_token_score == 1000.0
        assert config.scoring.pr_lookback_days == 30
        assert config.scoring.time_decay.sigmoid_midpoint_days == 14.0

    def test_missing_and_unknown_keys_fall_back_to_defaults(self, tmp_path):
        client = FakeRegistryClient({HASH_1: _state([_repo(101, 'a/b')], params={101: {3: 0, 99: 7}})})
        config = _loader(client, tmp_path).load(SNAPSHOT)['a/b']
        assert config.fixed_base_score is None  # 0 = unset
        assert config.issue_discovery_share == 0.0  # missing key -> constants default
        assert config.default_label_multiplier == 1.0

    def test_decodes_label_multipliers_and_branch_patterns(self, tmp_path):
        client = FakeRegistryClient(
            {
                HASH_1: _state(
                    [_repo(101, 'a/b')],
                    labels={101: {'bug': 1_500_000, 'king': 2_000_000}},
                    patterns={101: ['release/*']},
                )
            }
        )
        config = _loader(client, tmp_path).load(SNAPSHOT)['a/b']
        assert config.label_multipliers == {'bug': 1.5, 'king': 2.0}
        assert config.additional_acceptable_branches == ['release/*']


class TestFallbackLadder:
    def test_paused_returns_none_even_with_cache(self, tmp_path):
        client = FakeRegistryClient(
            {HASH_1: _state([_repo(101, 'a/b')]), HASH_2: _state([_repo(101, 'a/b')], paused=True)}
        )
        assert _loader(client, tmp_path).load(SNAPSHOT) is not None  # seeds the disk cache
        assert _loader(client, tmp_path).load(SNAPSHOT_2) is None  # paused skips the cache -> baked

    def test_empty_registry_returns_none(self, tmp_path):
        client = FakeRegistryClient({HASH_1: _state([])})
        assert _loader(client, tmp_path).load(SNAPSHOT) is None

    def test_unreachable_falls_back_to_last_good_cache(self, tmp_path):
        good = FakeRegistryClient({HASH_1: _state([_repo(101, 'a/b')], params={101: {4: 100_000}})})
        assert _loader(good, tmp_path).load(SNAPSHOT) is not None
        unreachable = FakeRegistryClient({})
        configs = _loader(unreachable, tmp_path).load(SNAPSHOT_2)
        assert set(configs) == {'a/b'}
        assert configs['a/b'].maintainer_cut == 0.1

    def test_unreachable_without_cache_returns_none(self, tmp_path):
        assert _loader(FakeRegistryClient({}), tmp_path).load(SNAPSHOT) is None

    def test_corrupt_cache_returns_none(self, tmp_path):
        (tmp_path / 'repo_registry_cache.json').write_text('{corrupt')
        assert _loader(FakeRegistryClient({}), tmp_path).load(SNAPSHOT) is None

    def test_out_of_bounds_contract_param_degrades_instead_of_raising(self, tmp_path):
        # review_penalty_rate 0 violates the python-side (0, 1] validation.
        client = FakeRegistryClient({HASH_1: _state([_repo(101, 'a/b')], params={101: {19: 0}})})
        assert _loader(client, tmp_path).load(SNAPSHOT) is None

    def test_cache_round_trips_through_json(self, tmp_path):
        client = FakeRegistryClient(
            {HASH_1: _state([_repo(101, 'a/b')], labels={101: {'bug': 1_500_000}}, patterns={101: ['dev*']})}
        )
        fresh = _loader(client, tmp_path).load(SNAPSHOT)
        payload = json.loads((tmp_path / 'repo_registry_cache.json').read_text())
        assert payload['snapshot'] == SNAPSHOT
        cached = _loader(FakeRegistryClient({}), tmp_path).load(SNAPSHOT_2)
        assert cached == fresh


class TestSnapshotAtomicity:
    def test_single_fetch_per_snapshot(self, tmp_path):
        client = FakeRegistryClient({HASH_1: _state([_repo(101, 'a/b')])})
        loader = _loader(client, tmp_path)
        first = loader.load(SNAPSHOT)
        assert loader.load(SNAPSHOT) is first  # memoized, no refetch
        assert client.registry_reads == 1

    def test_all_reads_pinned_to_snapshot_hash(self, tmp_path):
        client = FakeRegistryClient({HASH_1: _state([_repo(101, 'a/b'), _repo(202, 'c/d')], params={101: {17: 30}})})
        _loader(client, tmp_path).load(SNAPSHOT)
        assert set(client.read_ats) == {HASH_1}

    def test_failed_load_is_not_memoized(self, tmp_path):
        client = FakeRegistryClient({})
        loader = _loader(client, tmp_path)
        assert loader.load(SNAPSHOT) is None
        client.states[HASH_1] = _state([_repo(101, 'a/b')])
        assert set(loader.load(SNAPSHOT)) == {'a/b'}  # recovers on retry
