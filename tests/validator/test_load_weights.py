"""
Unit tests for weight loading functions.

These tests verify that all weight configuration files load correctly
and contain expected data structures.

Run tests:
    pytest tests/validator/test_load_weights.py -v
"""

import json

import pytest

from gittensor.validator.utils.load_weights import (
    LanguageConfig,
    RepoEligibilityConfig,
    RepoScoringConfig,
    RepositoryConfig,
    RepositoryRegistryError,
    TokenConfig,
    load_master_repo_weights,
    load_programming_language_weights,
    load_token_config,
    resolve_scoring,
)


class TestLoadTokenWeights:
    """Tests for loading token_weights.json via load_token_config()."""

    def test_load_token_config_returns_token_config(self):
        """load_token_config() should return a TokenConfig instance."""
        config = load_token_config()
        assert isinstance(config, TokenConfig)

    def test_token_config_has_structural_bonus(self):
        """TokenConfig should have structural_bonus weights."""
        config = load_token_config()
        assert isinstance(config.structural_bonus, dict)
        assert len(config.structural_bonus) > 0, 'Should have structural bonus weights'

    def test_token_config_has_leaf_tokens(self):
        """TokenConfig should have leaf_tokens weights."""
        config = load_token_config()
        assert isinstance(config.leaf_tokens, dict)
        assert len(config.leaf_tokens) > 0, 'Should have leaf token weights'

    def test_token_config_has_language_configs(self):
        """TokenConfig should include language configs from programming_languages.json."""
        config = load_token_config()
        assert isinstance(config.language_configs, dict)
        assert len(config.language_configs) > 0, 'Should have language configs'

    def test_structural_bonus_has_expected_keys(self):
        """structural_bonus should contain common AST node types."""
        config = load_token_config()
        expected_keys = ['function_definition', 'class_definition']
        for key in expected_keys:
            assert key in config.structural_bonus, f'Missing structural key: {key}'

    def test_structural_weights_are_positive_floats(self):
        """All structural weights should be non-negative floats."""
        config = load_token_config()
        for key, weight in config.structural_bonus.items():
            assert isinstance(weight, (int, float)), f'{key} weight should be numeric'
            assert weight >= 0, f'{key} weight should be non-negative'


class TestLoadProgrammingLanguages:
    """Tests for loading programming_languages.json via load_programming_language_weights()."""

    def test_load_programming_language_weights_returns_dict(self):
        """load_programming_language_weights() should return a dictionary."""
        configs = load_programming_language_weights()
        assert isinstance(configs, dict)

    def test_programming_languages_not_empty(self):
        """Should load multiple programming languages."""
        configs = load_programming_language_weights()
        assert len(configs) > 50, 'Should have many language configs'

    def test_language_configs_are_language_config_objects(self):
        """Each entry should be a LanguageConfig object."""
        configs = load_programming_language_weights()
        for ext, config in configs.items():
            assert isinstance(config, LanguageConfig), f'{ext} should be LanguageConfig'

    def test_tree_sitter_languages_have_language_field(self):
        """Languages with tree-sitter support should have language field set."""
        configs = load_programming_language_weights()
        # Python should have tree-sitter support
        assert configs['py'].language is not None, 'Python should have tree-sitter language'
        assert configs['py'].language == 'python'


class TestLoadMasterRepositories:
    """Tests for the repository registry loader load_master_repo_weights()."""

    def test_load_master_repo_weights_returns_dict(self):
        """load_master_repo_weights() should return a dictionary."""
        repos = load_master_repo_weights()
        assert isinstance(repos, dict)

    def test_repo_configs_are_repository_config_objects(self):
        """Each entry should be a RepositoryConfig object."""
        repos = load_master_repo_weights()
        for repo_name, config in repos.items():
            assert isinstance(config, RepositoryConfig), f'{repo_name} should be RepositoryConfig'

    def test_repo_names_are_lowercase(self):
        """Repository names should be normalized to lowercase."""
        repos = load_master_repo_weights()
        for repo_name in repos.keys():
            assert repo_name == repo_name.lower(), f'{repo_name} should be lowercase'


class TestRepositoryConfigTrustedLabelPipeline:
    """Dataclass + JSON-parsing tests for trusted_label_pipeline (issue #911)."""

    def test_trusted_label_pipeline_default_false(self):
        """RepositoryConfig constructor defaults trusted_label_pipeline to False.

        Default-off is the safety property: community repos with
        attacker-controlled auto-labelers (release-drafter, actions/labeler)
        keep the maintainer-association gate in place.
        """
        config = RepositoryConfig(emission_share=0.5)
        assert config.trusted_label_pipeline is False

    def test_loader_parses_trusted_label_pipeline_true(self, tmp_path, monkeypatch):
        """load_master_repo_weights() parses trusted_label_pipeline:true from JSON."""
        import json

        from gittensor.validator.utils import load_weights as lw

        fake_weights_dir = tmp_path
        (fake_weights_dir / 'repos_cache.json').write_text(
            json.dumps(
                {
                    'foo/trusted': {'emission_share': 0.5, 'trusted_label_pipeline': True},
                    'foo/untrusted': {'emission_share': 0.3},
                    'foo/explicit-off': {'emission_share': 0.2, 'trusted_label_pipeline': False},
                }
            )
        )
        repos = lw.load_master_repo_weights()

        assert repos['foo/trusted'].trusted_label_pipeline is True
        assert repos['foo/untrusted'].trusted_label_pipeline is False
        assert repos['foo/explicit-off'].trusted_label_pipeline is False


class TestRepositoryConfigLabelMultipliers:
    """Dataclass + JSON-parsing tests for per-repo label multiplier config."""

    def test_label_multiplier_defaults(self):
        config = RepositoryConfig(emission_share=0.5)

        assert config.label_multipliers is None
        assert config.default_label_multiplier == pytest.approx(1.0)

    def test_loader_parses_label_multiplier_config(self, tmp_path, monkeypatch):
        from gittensor.validator.utils import load_weights as lw

        fake_weights_dir = tmp_path
        (fake_weights_dir / 'repos_cache.json').write_text(
            json.dumps(
                {
                    'foo/labeled': {
                        'emission_share': 0.5,
                        'label_multipliers': {'kind/*': 1.5, 'type:bug': 1.25},
                        'default_label_multiplier': 0.8,
                    },
                    'foo/defaults': {'emission_share': 0.3},
                }
            )
        )
        repos = lw.load_master_repo_weights()

        assert repos['foo/labeled'].label_multipliers == {'kind/*': 1.5, 'type:bug': 1.25}
        assert repos['foo/labeled'].default_label_multiplier == pytest.approx(0.8)

        assert repos['foo/defaults'].label_multipliers is None
        assert repos['foo/defaults'].default_label_multiplier == pytest.approx(1.0)


class TestRepositoryConfigMirrorScoringFields:
    """Dataclass + JSON-parsing tests for mirror scoring + per-repo eligibility fields."""

    def test_mirror_scoring_field_defaults(self):
        config = RepositoryConfig(emission_share=0.5)

        assert config.fixed_base_score is None
        assert config.eligibility == RepoEligibilityConfig()

    def test_loader_parses_fixed_base_score(self, tmp_path, monkeypatch):
        from gittensor.validator.utils import load_weights as lw

        (tmp_path / 'repos_cache.json').write_text(
            json.dumps(
                {
                    'foo/fixed': {'emission_share': 0.5, 'fixed_base_score': 12.5},
                    'foo/defaults': {'emission_share': 0.3},
                }
            )
        )
        repos = lw.load_master_repo_weights()

        assert repos['foo/fixed'].fixed_base_score == pytest.approx(12.5)
        assert repos['foo/defaults'].fixed_base_score is None

    def test_loader_parses_eligibility_overrides(self, tmp_path, monkeypatch):
        from gittensor.validator.utils import load_weights as lw

        (tmp_path / 'repos_cache.json').write_text(
            json.dumps(
                {
                    'foo/custom': {
                        'emission_share': 0.5,
                        'eligibility': {'min_valid_merged_prs': 1, 'min_credibility': 0.5},
                    },
                    'foo/defaults': {'emission_share': 0.3},
                }
            )
        )
        repos = lw.load_master_repo_weights()

        assert repos['foo/custom'].eligibility.min_valid_merged_prs == 1
        assert repos['foo/custom'].eligibility.min_credibility == pytest.approx(0.5)
        # unset fields stay None and resolve to the global default
        assert repos['foo/custom'].eligibility.max_open_pr_threshold is None
        assert repos['foo/defaults'].eligibility == RepoEligibilityConfig()

    def test_loader_rejects_unknown_eligibility_key(self, tmp_path, monkeypatch):
        from gittensor.validator.utils import load_weights as lw

        (tmp_path / 'repos_cache.json').write_text(
            json.dumps({'foo/bad': {'emission_share': 0.5, 'eligibility': {'min_valid_prs': 1}}})
        )
        with pytest.raises(RepositoryRegistryError):
            lw.load_master_repo_weights()

    def test_loader_rejects_out_of_range_credibility(self, tmp_path, monkeypatch):
        from gittensor.validator.utils import load_weights as lw

        (tmp_path / 'repos_cache.json').write_text(
            json.dumps({'foo/bad': {'emission_share': 0.5, 'eligibility': {'min_credibility': 1.5}}})
        )
        with pytest.raises(RepositoryRegistryError):
            lw.load_master_repo_weights()


class TestRepositoryConfigScoringBlock:
    """Dataclass + JSON-parsing tests for the per-repo scoring block."""

    def test_scoring_field_defaults(self):
        config = RepositoryConfig(emission_share=0.5)
        assert config.scoring == RepoScoringConfig()

    def test_loader_parses_scoring_overrides(self, tmp_path, monkeypatch):
        from gittensor.validator.utils import load_weights as lw

        (tmp_path / 'repos_cache.json').write_text(
            json.dumps(
                {
                    'foo/custom': {
                        'emission_share': 0.5,
                        'scoring': {
                            'pr_lookback_days': 45,
                            'open_pr_collateral_percent': 0.4,
                            'review_penalty_rate': 0.25,
                        },
                    },
                    'foo/defaults': {'emission_share': 0.3},
                }
            )
        )
        repos = lw.load_master_repo_weights()

        assert repos['foo/custom'].scoring.pr_lookback_days == 45
        assert repos['foo/custom'].scoring.open_pr_collateral_percent == pytest.approx(0.4)
        assert repos['foo/custom'].scoring.review_penalty_rate == pytest.approx(0.25)
        assert repos['foo/defaults'].scoring == RepoScoringConfig()

    def test_loader_rejects_unknown_scoring_key(self, tmp_path, monkeypatch):
        from gittensor.validator.utils import load_weights as lw

        (tmp_path / 'repos_cache.json').write_text(
            json.dumps({'foo/bad': {'emission_share': 0.5, 'scoring': {'bogus': 1}}})
        )
        with pytest.raises(RepositoryRegistryError):
            lw.load_master_repo_weights()

    def test_loader_rejects_out_of_range_collateral(self, tmp_path, monkeypatch):
        from gittensor.validator.utils import load_weights as lw

        (tmp_path / 'repos_cache.json').write_text(
            json.dumps({'foo/bad': {'emission_share': 0.5, 'scoring': {'open_pr_collateral_percent': 1.5}}})
        )
        with pytest.raises(RepositoryRegistryError):
            lw.load_master_repo_weights()

    def test_loader_rejects_zero_review_penalty_rate(self, tmp_path, monkeypatch):
        from gittensor.validator.utils import load_weights as lw

        (tmp_path / 'repos_cache.json').write_text(
            json.dumps({'foo/bad': {'emission_share': 0.5, 'scoring': {'review_penalty_rate': 0.0}}})
        )
        with pytest.raises(RepositoryRegistryError):
            lw.load_master_repo_weights()

    def test_loader_rejects_out_of_range_issue_multiplier(self, tmp_path, monkeypatch):
        from gittensor.validator.utils import load_weights as lw

        (tmp_path / 'repos_cache.json').write_text(
            json.dumps({'foo/bad': {'emission_share': 0.5, 'scoring': {'standard_issue_multiplier': 0.5}}})
        )
        with pytest.raises(RepositoryRegistryError):
            lw.load_master_repo_weights()

    def test_loader_parses_time_decay_overrides(self, tmp_path, monkeypatch):
        from gittensor.validator.utils import load_weights as lw

        (tmp_path / 'repos_cache.json').write_text(
            json.dumps({'foo/custom': {'emission_share': 0.5, 'scoring': {'time_decay': {'grace_period_hours': 24}}}})
        )
        repos = lw.load_master_repo_weights()

        assert repos['foo/custom'].scoring.time_decay.grace_period_hours == 24

    def test_loader_rejects_unknown_time_decay_key(self, tmp_path, monkeypatch):
        from gittensor.validator.utils import load_weights as lw

        (tmp_path / 'repos_cache.json').write_text(
            json.dumps({'foo/bad': {'emission_share': 0.5, 'scoring': {'time_decay': {'bogus': 1}}}})
        )
        with pytest.raises(RepositoryRegistryError):
            lw.load_master_repo_weights()

    def test_loader_rejects_out_of_range_lookback(self, tmp_path, monkeypatch):
        from gittensor.validator.utils import load_weights as lw

        (tmp_path / 'repos_cache.json').write_text(
            json.dumps({'foo/bad': {'emission_share': 0.5, 'scoring': {'pr_lookback_days': 200}}})
        )
        with pytest.raises(RepositoryRegistryError):
            lw.load_master_repo_weights()

    @pytest.mark.parametrize('scale', [5.0, 501.0])
    def test_loader_rejects_out_of_range_saturation_scale(self, tmp_path, monkeypatch, scale):
        from gittensor.validator.utils import load_weights as lw

        (tmp_path / 'repos_cache.json').write_text(
            json.dumps({'foo/bad': {'emission_share': 0.5, 'scoring': {'src_tok_saturation_scale': scale}}})
        )
        with pytest.raises(RepositoryRegistryError):
            lw.load_master_repo_weights()

    def test_loader_accepts_saturation_scale_at_bounds(self, tmp_path, monkeypatch):
        from gittensor.validator.utils import load_weights as lw

        (tmp_path / 'repos_cache.json').write_text(
            json.dumps(
                {
                    'foo/low': {'emission_share': 0.4, 'scoring': {'src_tok_saturation_scale': 10.0}},
                    'foo/high': {'emission_share': 0.4, 'scoring': {'src_tok_saturation_scale': 500.0}},
                }
            )
        )
        repos = lw.load_master_repo_weights()
        assert resolve_scoring(repos['foo/low'].scoring).src_tok_saturation_scale == pytest.approx(10.0)
        assert resolve_scoring(repos['foo/high'].scoring).src_tok_saturation_scale == pytest.approx(500.0)


class TestRepositoryConfigMaintainerCut:
    """Dataclass + JSON-parsing tests for the maintainer_cut emission carve-out."""

    def test_maintainer_cut_defaults_zero(self):
        config = RepositoryConfig(emission_share=0.5)
        assert config.maintainer_cut == pytest.approx(0.0)

    def test_loader_parses_maintainer_cut(self, tmp_path, monkeypatch):
        from gittensor.validator.utils import load_weights as lw

        fake_weights_dir = tmp_path
        (fake_weights_dir / 'repos_cache.json').write_text(
            json.dumps(
                {
                    'foo/with-cut': {'emission_share': 0.5, 'maintainer_cut': 0.3},
                    'foo/defaults': {'emission_share': 0.3},
                }
            )
        )
        repos = lw.load_master_repo_weights()

        assert repos['foo/with-cut'].maintainer_cut == pytest.approx(0.3)
        assert repos['foo/defaults'].maintainer_cut == pytest.approx(0.0)


class TestRepositoryEmissionShare:
    """Tests for bounded repo emission_share loading."""

    @pytest.mark.parametrize(
        'emission_share',
        [0.0349, 0.0351, 0.0487, 0.1025, 0.2017, 1.0],
    )
    def test_preserves_full_precision(self, emission_share):
        config = RepositoryConfig(emission_share=emission_share)
        assert config.emission_share == emission_share

    def test_issue_discovery_share_defaults_even_split(self):
        config = RepositoryConfig(emission_share=0.2)
        assert config.issue_discovery_share == pytest.approx(0.5)

    def test_loader_parses_issue_discovery_share(self, tmp_path, monkeypatch):
        from gittensor.validator.utils import load_weights as lw

        fake_weights_dir = tmp_path
        (fake_weights_dir / 'repos_cache.json').write_text(
            json.dumps(
                {
                    'foo/pr-only': {'emission_share': 0.4, 'issue_discovery_share': 0.0},
                    'foo/issues-only': {'emission_share': 0.6, 'issue_discovery_share': 1.0},
                }
            )
        )
        repos = lw.load_master_repo_weights()

        assert repos['foo/pr-only'].issue_discovery_share == pytest.approx(0.0)
        assert repos['foo/issues-only'].issue_discovery_share == pytest.approx(1.0)

    def test_loader_accepts_sum_less_than_one(self, tmp_path, monkeypatch):
        from gittensor.validator.utils import load_weights as lw

        fake_weights_dir = tmp_path
        (fake_weights_dir / 'repos_cache.json').write_text(
            json.dumps({'foo/a': {'emission_share': 0.2}, 'foo/b': {'emission_share': 0.3}})
        )
        repos = lw.load_master_repo_weights()

        assert set(repos) == {'foo/a', 'foo/b'}
        assert sum(config.emission_share for config in repos.values()) == pytest.approx(0.5)

    @pytest.mark.parametrize(
        'metadata',
        [
            {'emission_share': -0.01},
            {'emission_share': 1.01},
            {'emission_share': 0.5, 'issue_discovery_share': -0.01},
            {'emission_share': 0.5, 'issue_discovery_share': 1.01},
            {'emission_share': 0.5, 'maintainer_cut': -0.01},
            {'emission_share': 0.5, 'maintainer_cut': 1.01},
        ],
    )
    def test_loader_rejects_out_of_range_values(self, tmp_path, monkeypatch, metadata):
        from gittensor.validator.utils import load_weights as lw

        fake_weights_dir = tmp_path
        (fake_weights_dir / 'repos_cache.json').write_text(json.dumps({'foo/bad': metadata}))
        with pytest.raises(RepositoryRegistryError):
            lw.load_master_repo_weights()

    @pytest.mark.parametrize(
        'metadata',
        [
            {'emission_share': True},
            {'emission_share': 0.5, 'issue_discovery_share': False},
            {'emission_share': 0.5, 'maintainer_cut': True},
        ],
    )
    def test_loader_rejects_boolean_share_values(self, tmp_path, monkeypatch, metadata):
        from gittensor.validator.utils import load_weights as lw

        fake_weights_dir = tmp_path
        (fake_weights_dir / 'repos_cache.json').write_text(json.dumps({'foo/bad': metadata}))
        with pytest.raises(RepositoryRegistryError, match='must be a float'):
            lw.load_master_repo_weights()

    def test_loader_rejects_sum_greater_than_one(self, tmp_path, monkeypatch):
        from gittensor.validator.utils import load_weights as lw

        fake_weights_dir = tmp_path
        (fake_weights_dir / 'repos_cache.json').write_text(
            json.dumps({'foo/a': {'emission_share': 0.6}, 'foo/b': {'emission_share': 0.5}})
        )
        with pytest.raises(RepositoryRegistryError, match='total emission_share must be <= 1.0'):
            lw.load_master_repo_weights()


class TestRegistryApiLoading:
    """Tests for the API-first loader with on-disk last-good cache fallback."""

    def test_loads_from_api_when_available(self, monkeypatch):
        from gittensor.validator.utils import load_weights as lw

        payload = {'Owner/Repo': {'emission_share': 0.1, 'label_multipliers': {'feature': 2.0}}}
        monkeypatch.setattr(lw, '_fetch_registry_from_api', lambda: payload)

        repos = lw.load_master_repo_weights()

        assert 'owner/repo' in repos  # normalized to lowercase
        assert repos['owner/repo'].emission_share == 0.1
        assert repos['owner/repo'].label_multipliers == {'feature': 2.0}

    def test_successful_fetch_writes_disk_cache(self, monkeypatch):
        # A successful fetch persists the last-good registry for outage resilience.
        from gittensor.validator.utils import load_weights as lw

        payload = {'owner/repo': {'emission_share': 0.1}}
        monkeypatch.setattr(lw, '_fetch_registry_from_api', lambda: payload)

        lw.load_master_repo_weights()

        cache_path = lw._get_repos_cache_path()
        assert cache_path.exists(), 'successful API fetch should write the last-good cache'
        assert json.loads(cache_path.read_text()) == payload

    def test_falls_back_to_disk_cache_when_api_unavailable(self, tmp_path):
        # autouse fixture fails the API and points the cache at tmp_path; warm it.
        from gittensor.validator.utils import load_weights as lw

        lw._get_repos_cache_path().write_text(json.dumps({'a/b': {'emission_share': 0.2}}))
        repos = lw.load_master_repo_weights()

        assert repos['a/b'].emission_share == 0.2

    def test_api_invalid_content_falls_back_to_cache(self, monkeypatch):
        from gittensor.validator.utils import load_weights as lw

        # API serves data violating the emission contract -> fall back to the cache.
        monkeypatch.setattr(lw, '_fetch_registry_from_api', lambda: {'a/b': {'emission_share': 5.0}})
        lw._get_repos_cache_path().write_text(json.dumps({'a/b': {'emission_share': 0.3}}))
        repos = lw.load_master_repo_weights()

        assert repos['a/b'].emission_share == 0.3

    def test_returns_empty_when_api_down_and_no_cache(self):
        # autouse fixture disables the API and points the cache at an empty tmp path.
        from gittensor.validator.utils import load_weights as lw

        assert lw.load_master_repo_weights() == {}

    def test_invalid_cache_still_raises(self):
        # A cache that violates the registry contract is a real problem and must surface.
        from gittensor.validator.utils import load_weights as lw

        lw._get_repos_cache_path().write_text(
            json.dumps({'foo/a': {'emission_share': 0.6}, 'foo/b': {'emission_share': 0.5}})
        )
        with pytest.raises(RepositoryRegistryError, match='total emission_share must be <= 1.0'):
            lw.load_master_repo_weights()


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
