"""
Unit tests for weight loading functions.

These tests verify that all weight configuration files load correctly
and contain expected data structures.

Run tests:
    pytest tests/validator/test_load_weights.py -v
"""

import pytest

from gittensor.validator.configurations.tier_config import Tier
from gittensor.validator.utils.load_weights import (
    LanguageConfig,
    RepositoryConfig,
    TokenConfig,
    load_master_repo_weights,
    load_programming_language_weights,
    load_token_config,
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

    def test_repos_have_valid_tiers(self):
        """Repositories should have valid tier assignments."""
        repos = load_master_repo_weights()
        valid_tiers = {Tier.BRONZE, Tier.SILVER, Tier.GOLD, None}
        for repo_name, config in repos.items():
            assert config.tier in valid_tiers, f'{repo_name} has invalid tier: {config.tier}'


class TestBannedOrganizations:
    """Tests ensuring banned organizations are not active in the repository list.

    Any repositories from these orgs MUST be marked as inactive.
    """

    # orgs may be banned for:
    # - exploitative PR manipulation
    # - explicit removal request
    BANNED_ORGS = ['conda', 'conda-incubator', 'conda-archive', 'louislam']

    def test_banned_org_repos_are_inactive(self):
        """Repositories from banned organizations must be marked as inactive."""
        repos = load_master_repo_weights()

        for repo_name, config in repos.items():
            org = repo_name.split('/')[0] if '/' in repo_name else None
            if org in self.BANNED_ORGS:
                assert (
                    config.inactive_at is not None
                ), f'Repository {repo_name} from banned org {org} must be marked inactive'

    def test_no_active_banned_org_repos(self):
        """Count of active repositories from banned orgs should be zero."""
        repos = load_master_repo_weights()

        active_banned = []
        for repo_name, config in repos.items():
            org = repo_name.split('/')[0] if '/' in repo_name else None
            if org in self.BANNED_ORGS and config.inactive_at is None:
                active_banned.append(repo_name)

        assert len(active_banned) == 0, f'Found {len(active_banned)} active repos from banned orgs: {active_banned}'


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
