"""
Unit tests for weight loading functions.

These tests verify that all weight configuration files load correctly
and contain expected data structures.

Run tests:
    pytest tests/validator/test_load_weights.py -v
"""

import pytest

from gittensor.validator.utils.load_weights import (
    LanguageConfig,
    RepositoryConfig,
    TokenConfig,
    load_master_repo_weights,
    load_programming_language_weights,
    load_token_config,
    resolve_repo_weight,
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
    """Tests for loading master_repositories.json via load_master_repo_weights()."""

    def test_load_master_repo_weights_returns_dict(self):
        """load_master_repo_weights() should return a dictionary."""
        repos = load_master_repo_weights()
        assert isinstance(repos, dict)

    def test_master_repositories_not_empty(self):
        """Should load many repositories."""
        repos = load_master_repo_weights()
        assert len(repos) > 100, 'Should have many repositories'

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

    def test_mirror_enabled_field_present_on_live_configs(self):
        """Live master_repositories.json entries load with a bool mirror_enabled."""
        repos = load_master_repo_weights()
        for repo_name, config in repos.items():
            assert isinstance(config.mirror_enabled, bool), (
                f'{repo_name} mirror_enabled should be bool, got {type(config.mirror_enabled)}'
            )

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


class TestRepositoryConfigMirrorFlag:
    """Dataclass-level tests for the mirror_enabled field + its JSON parsing."""

    def test_mirror_enabled_default_false(self):
        """RepositoryConfig constructor defaults mirror_enabled to False."""
        config = RepositoryConfig(weight=0.5)
        assert config.mirror_enabled is False

    def test_mirror_enabled_explicit_true(self):
        """RepositoryConfig accepts mirror_enabled=True."""
        config = RepositoryConfig(weight=0.5, mirror_enabled=True)
        assert config.mirror_enabled is True

    def test_loader_parses_mirror_enabled_true(self, tmp_path, monkeypatch):
        """load_master_repo_weights() parses mirror_enabled:true from JSON."""
        import json

        from gittensor.validator.utils import load_weights as lw

        fake_weights_dir = tmp_path
        (fake_weights_dir / 'master_repositories.json').write_text(
            json.dumps(
                {
                    'foo/mirror-repo': {'weight': 0.5, 'mirror_enabled': True},
                    'foo/legacy-repo': {'weight': 0.3},
                    'foo/explicit-off': {'weight': 0.2, 'mirror_enabled': False},
                }
            )
        )
        monkeypatch.setattr(lw, '_get_weights_dir', lambda: fake_weights_dir)

        repos = lw.load_master_repo_weights()

        assert repos['foo/mirror-repo'].mirror_enabled is True
        assert repos['foo/legacy-repo'].mirror_enabled is False
        assert repos['foo/explicit-off'].mirror_enabled is False


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

    def test_trusted_label_pipeline_explicit_true(self):
        """RepositoryConfig accepts trusted_label_pipeline=True."""
        config = RepositoryConfig(weight=0.5, trusted_label_pipeline=True)
        assert config.trusted_label_pipeline is True

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
    """Tests for resolve_repo_weight — full-precision repo weight lookup."""

    def test_none_returns_default(self):
        assert resolve_repo_weight(None) == 0.01

    @pytest.mark.parametrize(
        'weight',
        [0.0349, 0.0351, 0.0487, 0.1025, 0.2017, 1.0],
    )
    def test_preserves_full_precision(self, weight):
        config = RepositoryConfig(weight=weight)
        assert resolve_repo_weight(config) == weight

    def test_live_master_repo_precision(self):
        """cronboard (0.0349) and fzf (0.0351) must not collapse to 0.03/0.04."""
        repos = load_master_repo_weights()
        if 'antoniorodr/cronboard' in repos:
            assert resolve_repo_weight(repos['antoniorodr/cronboard']) == pytest.approx(0.0349, abs=1e-9)
        if 'junegunn/fzf' in repos:
            assert resolve_repo_weight(repos['junegunn/fzf']) == pytest.approx(0.0351, abs=1e-9)


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
