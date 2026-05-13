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
    RepositoryConfig,
    TokenConfig,
    load_master_repo_weights,
    load_programming_language_weights,
    load_token_config,
    resolve_repo_emission_share,
    resolve_repo_weight,
)


def _live_master_repo_metadata():
    from gittensor.validator.utils import load_weights as lw

    with open(lw._get_weights_dir() / 'master_repositories.json', 'r') as f:
        return sorted(json.load(f).items())


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
    """Tests for loading master_repositories.json via load_master_repo_weights()."""

    def test_load_master_repo_weights_returns_dict(self):
        """load_master_repo_weights() should return a dictionary."""
        repos = load_master_repo_weights()
        assert isinstance(repos, dict)

    def test_master_repositories_not_empty(self):
        """Should load at least the entrius core repos."""
        repos = load_master_repo_weights()
        assert len(repos) > 0, 'Should have at least one repository'

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

    def test_trusted_label_pipeline_field_present_on_live_configs(self):
        """Live master_repositories.json entries load with a bool trusted_label_pipeline."""
        repos = load_master_repo_weights()
        for repo_name, config in repos.items():
            assert isinstance(config.trusted_label_pipeline, bool), (
                f'{repo_name} trusted_label_pipeline should be bool, got {type(config.trusted_label_pipeline)}'
            )

    def test_entrius_repos_have_trusted_label_pipeline(self):
        """All entrius/* entries opt into trusted_label_pipeline (issue #911)."""
        repos = load_master_repo_weights()
        entrius_repos = {name: cfg for name, cfg in repos.items() if name.startswith('entrius/')}
        assert entrius_repos, 'expected entrius/* entries in master_repositories.json'
        for repo_name, config in entrius_repos.items():
            assert config.trusted_label_pipeline is True, (
                f'{repo_name} must have trusted_label_pipeline=true so the agentic-maintainer '
                f'labeling worker is honored at scoring time'
            )

    def test_live_master_repository_entries_use_emission_share(self):
        """Live registry entries use bounded emission_share, not legacy weight."""
        for repo_name, metadata in _live_master_repo_metadata():
            assert 'emission_share' in metadata, f'{repo_name} must declare emission_share'
            assert 'weight' not in metadata, f'{repo_name} must not use legacy weight'

    def test_live_emission_shares_are_in_range_and_total_not_above_one(self):
        repos = load_master_repo_weights()
        total_share = sum(config.emission_share for config in repos.values())
        assert 0.0 <= total_share <= 1.0
        for repo_name, config in repos.items():
            assert 0.0 <= config.emission_share <= 1.0, f'{repo_name} emission_share must be within [0, 1]'
            assert 0.0 <= config.issue_discovery_share <= 1.0, (
                f'{repo_name} issue_discovery_share must be within [0, 1]'
            )

    def test_oc_1_opts_out_of_issue_discovery_rewards(self):
        repos = load_master_repo_weights()

        assert repos['entrius/oc-1'].issue_discovery_share == pytest.approx(0.0)

    def test_loader_accepts_sum_less_than_one(self, tmp_path, monkeypatch):
        from gittensor.validator.utils import load_weights as lw

        fake_weights_dir = tmp_path
        (fake_weights_dir / 'master_repositories.json').write_text(
            json.dumps(
                {
                    'foo/one': {'emission_share': 0.2},
                    'foo/two': {'emission_share': 0.3},
                }
            )
        )
        monkeypatch.setattr(lw, '_get_weights_dir', lambda: fake_weights_dir)

        repos = lw.load_master_repo_weights()

        assert repos['foo/one'].emission_share == pytest.approx(0.2)
        assert repos['foo/two'].emission_share == pytest.approx(0.3)

    @pytest.mark.parametrize(
        'field,value',
        [
            ('emission_share', -0.001),
            ('emission_share', 1.001),
            ('emission_share', True),
            ('issue_discovery_share', -0.001),
            ('issue_discovery_share', 1.001),
            ('issue_discovery_share', False),
        ],
    )
    def test_loader_rejects_share_outside_range_or_bool(self, tmp_path, monkeypatch, field, value):
        from gittensor.validator.utils import load_weights as lw

        fake_weights_dir = tmp_path
        metadata = {'emission_share': 0.5}
        metadata[field] = value
        (fake_weights_dir / 'master_repositories.json').write_text(json.dumps({'foo/bad': metadata}))
        monkeypatch.setattr(lw, '_get_weights_dir', lambda: fake_weights_dir)

        with pytest.raises(ValueError, match=field):
            lw.load_master_repo_weights()

    def test_loader_rejects_total_emission_share_above_one(self, tmp_path, monkeypatch):
        from gittensor.validator.utils import load_weights as lw

        fake_weights_dir = tmp_path
        (fake_weights_dir / 'master_repositories.json').write_text(
            json.dumps(
                {
                    'foo/one': {'emission_share': 0.6},
                    'foo/two': {'emission_share': 0.5},
                }
            )
        )
        monkeypatch.setattr(lw, '_get_weights_dir', lambda: fake_weights_dir)

        with pytest.raises(ValueError, match='total repository emission_share'):
            lw.load_master_repo_weights()


class TestRepositoryConfigTrustedLabelPipeline:
    """Dataclass + JSON-parsing tests for trusted_label_pipeline (issue #911)."""

    def test_trusted_label_pipeline_default_false(self):
        """RepositoryConfig constructor defaults trusted_label_pipeline to False.

        Default-off is the safety property: community repos with
        attacker-controlled auto-labelers (release-drafter, actions/labeler)
        keep the maintainer-association gate in place.
        """
        config = RepositoryConfig(weight=0.5)
        assert config.trusted_label_pipeline is False

    def test_loader_parses_trusted_label_pipeline_true(self, tmp_path, monkeypatch):
        """load_master_repo_weights() parses trusted_label_pipeline:true from JSON."""
        import json

        from gittensor.validator.utils import load_weights as lw

        fake_weights_dir = tmp_path
        (fake_weights_dir / 'master_repositories.json').write_text(
            json.dumps(
                {
                    'foo/trusted': {'weight': 0.5, 'trusted_label_pipeline': True},
                    'foo/untrusted': {'weight': 0.3},
                    'foo/explicit-off': {'weight': 0.2, 'trusted_label_pipeline': False},
                }
            )
        )
        monkeypatch.setattr(lw, '_get_weights_dir', lambda: fake_weights_dir)

        repos = lw.load_master_repo_weights()

        assert repos['foo/trusted'].trusted_label_pipeline is True
        assert repos['foo/untrusted'].trusted_label_pipeline is False
        assert repos['foo/explicit-off'].trusted_label_pipeline is False


class TestRepositoryConfigLabelMultipliers:
    """Dataclass + JSON-parsing tests for per-repo label multiplier config."""

    def test_label_multiplier_defaults(self):
        config = RepositoryConfig(weight=0.5)

        assert config.label_multipliers is None
        assert config.default_label_multiplier == pytest.approx(1.0)

    def test_loader_parses_label_multiplier_config(self, tmp_path, monkeypatch):
        from gittensor.validator.utils import load_weights as lw

        fake_weights_dir = tmp_path
        (fake_weights_dir / 'master_repositories.json').write_text(
            json.dumps(
                {
                    'foo/labeled': {
                        'weight': 0.5,
                        'label_multipliers': {'kind/*': 1.5, 'type:bug': 1.25},
                        'default_label_multiplier': 0.8,
                    },
                    'foo/defaults': {'weight': 0.3},
                }
            )
        )
        monkeypatch.setattr(lw, '_get_weights_dir', lambda: fake_weights_dir)

        repos = lw.load_master_repo_weights()

        assert repos['foo/labeled'].label_multipliers == {'kind/*': 1.5, 'type:bug': 1.25}
        assert repos['foo/labeled'].default_label_multiplier == pytest.approx(0.8)

        assert repos['foo/defaults'].label_multipliers is None
        assert repos['foo/defaults'].default_label_multiplier == pytest.approx(1.0)

    @pytest.mark.parametrize('repo_name,metadata', _live_master_repo_metadata())
    def test_live_label_multiplier_maps_are_bounded(self, repo_name, metadata):
        label_multipliers = metadata.get('label_multipliers')
        if label_multipliers is None:
            return

        assert isinstance(label_multipliers, dict), f'{repo_name} label_multipliers must be a dict'
        assert len(label_multipliers) <= 10, f'{repo_name} label_multipliers has too many entries'

    @pytest.mark.parametrize('repo_name,metadata', _live_master_repo_metadata())
    def test_live_label_multiplier_values_are_in_range(self, repo_name, metadata):
        for pattern, multiplier in (metadata.get('label_multipliers') or {}).items():
            assert isinstance(pattern, str), f'{repo_name} label_multipliers keys must be strings'
            assert 0.0 <= float(multiplier) <= 20.0, (
                f'{repo_name} label_multipliers[{pattern!r}] must be within [0.0, 20.0]'
            )

    @pytest.mark.parametrize('repo_name,metadata', _live_master_repo_metadata())
    def test_live_default_label_multiplier_values_are_in_range(self, repo_name, metadata):
        if 'default_label_multiplier' not in metadata:
            return

        assert 0.0 <= float(metadata['default_label_multiplier']) <= 20.0, (
            f'{repo_name} default_label_multiplier must be within [0.0, 20.0]'
        )


class TestRepositoryConfigMirrorScoringFields:
    """Dataclass + JSON-parsing tests for mirror-only scoring fields."""

    def test_mirror_scoring_field_defaults(self):
        config = RepositoryConfig(weight=0.5)

        assert config.fixed_base_score is None
        assert config.eligibility_mode is True

    def test_loader_parses_mirror_scoring_fields(self, tmp_path, monkeypatch):
        from gittensor.validator.utils import load_weights as lw

        fake_weights_dir = tmp_path
        (fake_weights_dir / 'master_repositories.json').write_text(
            json.dumps(
                {
                    'foo/fixed': {
                        'weight': 0.5,
                        'fixed_base_score': 12.5,
                        'eligibility_mode': False,
                    },
                    'foo/defaults': {'weight': 0.3},
                }
            )
        )
        monkeypatch.setattr(lw, '_get_weights_dir', lambda: fake_weights_dir)

        repos = lw.load_master_repo_weights()

        assert repos['foo/fixed'].fixed_base_score == pytest.approx(12.5)
        assert repos['foo/fixed'].eligibility_mode is False
        assert repos['foo/defaults'].fixed_base_score is None
        assert repos['foo/defaults'].eligibility_mode is True

    def test_live_mirror_scoring_fields_have_valid_shape(self):
        """Loader passes mirror scoring fields through unchanged — CI fails when a
        bad value is committed to master_repositories.json."""
        repos = load_master_repo_weights()
        for repo_name, config in repos.items():
            if config.fixed_base_score is not None:
                assert isinstance(config.fixed_base_score, (int, float)) and not isinstance(
                    config.fixed_base_score, bool
                ), f'{repo_name} fixed_base_score must be numeric, got {type(config.fixed_base_score)}'
                assert 0.0 <= float(config.fixed_base_score) <= 100.0, (
                    f'{repo_name} fixed_base_score must be within [0.0, 100.0]'
                )
            assert isinstance(config.eligibility_mode, bool), (
                f'{repo_name} eligibility_mode must be bool, got {type(config.eligibility_mode)}'
            )


class TestBannedOrganizations:
    """Tests ensuring banned organizations are not active in the repository list.

    Any repositories from these orgs MUST be marked as inactive.
    """

    # orgs may be banned for:
    # - exploitative PR manipulation
    # - explicit removal request
    BANNED_ORGS = [
        'conda',
        'conda-incubator',
        'conda-archive',
        'louislam',
        'python',
        'fastapi',
        'astral-sh',
        'astropy',
        'numpy',
        'scipy',
    ]

    def test_banned_org_repos_are_inactive(self):
        """Repositories from banned organizations must be marked as inactive."""
        repos = load_master_repo_weights()

        for repo_name, config in repos.items():
            org = repo_name.split('/')[0] if '/' in repo_name else None
            if org in self.BANNED_ORGS:
                assert config.inactive_at is not None, (
                    f'Repository {repo_name} from banned org {org} must be marked inactive'
                )

    def test_no_active_banned_org_repos(self):
        """Count of active repositories from banned orgs should be zero."""
        repos = load_master_repo_weights()

        active_banned = []
        for repo_name, config in repos.items():
            org = repo_name.split('/')[0] if '/' in repo_name else None
            if org in self.BANNED_ORGS and config.inactive_at is None:
                active_banned.append(repo_name)

        assert len(active_banned) == 0, f'Found {len(active_banned)} active repos from banned orgs: {active_banned}'


class TestResolveRepoWeight:
    """Tests for repo emission-share resolution — full-precision lookup."""

    def test_none_returns_default(self):
        assert resolve_repo_emission_share(None) == 0.01

    @pytest.mark.parametrize(
        'weight',
        [0.0349, 0.0351, 0.0487, 0.1025, 0.2017, 1.0],
    )
    def test_preserves_full_precision(self, weight):
        config = RepositoryConfig(weight=weight)
        assert resolve_repo_emission_share(config) == weight

    def test_live_master_repo_precision(self):
        """cronboard (0.0349) and fzf (0.0351) must not collapse to 0.03/0.04."""
        repos = load_master_repo_weights()
        if 'antoniorodr/cronboard' in repos:
            assert resolve_repo_emission_share(repos['antoniorodr/cronboard']) == pytest.approx(0.0349, abs=1e-9)
        if 'junegunn/fzf' in repos:
            assert resolve_repo_emission_share(repos['junegunn/fzf']) == pytest.approx(0.0351, abs=1e-9)

    def test_legacy_weight_resolver_aliases_emission_share_resolver(self):
        config = RepositoryConfig(emission_share=0.1234)

        assert resolve_repo_weight(config) == pytest.approx(resolve_repo_emission_share(config))


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
