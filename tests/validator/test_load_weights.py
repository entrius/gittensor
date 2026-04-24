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
    _clamp_weight,
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


class TestClampWeight:
    """Unit tests for the _clamp_weight load-time guard."""

    def test_valid_positive_float_passes_through(self):
        assert _clamp_weight(0.5, default=1.0, source='x') == 0.5

    def test_zero_is_allowed(self):
        """0.0 is a legitimate 'this bucket contributes nothing' value."""
        assert _clamp_weight(0.0, default=1.0, source='x') == 0.0

    def test_none_uses_default(self):
        assert _clamp_weight(None, default=0.01, source='x') == 0.01

    def test_int_is_coerced_to_float(self):
        assert _clamp_weight(2, default=1.0, source='x') == 2.0

    @pytest.mark.parametrize('bad', [-1.0, -0.0001, -1e9])
    def test_negative_falls_back_to_default(self, bad):
        assert _clamp_weight(bad, default=0.01, source='x') == 0.01

    def test_nan_falls_back_to_default(self):
        assert _clamp_weight(float('nan'), default=1.0, source='x') == 1.0

    @pytest.mark.parametrize('bad', [float('inf'), float('-inf')])
    def test_infinity_falls_back_to_default(self, bad):
        assert _clamp_weight(bad, default=1.0, source='x') == 1.0

    @pytest.mark.parametrize('bad', ['not a number', {}, [1, 2]])
    def test_non_numeric_falls_back_to_default(self, bad):
        assert _clamp_weight(bad, default=0.5, source='x') == 0.5

    def test_warning_mentions_source(self, caplog):
        """Warning log must identify the offending source so operators can trace bad JSON."""
        import logging

        with caplog.at_level(logging.WARNING):
            _clamp_weight(-1.0, default=0.01, source='repo:owner/evil')
        assert any('repo:owner/evil' in rec.getMessage() for rec in caplog.records)


class TestLoadersRejectBadWeights:
    """Integration tests: loaders must clamp out-of-range JSON values to safe defaults."""

    def _write(self, path, payload):
        path.write_text(json.dumps(payload))

    def test_master_repos_negative_weight_clamped(self, tmp_path, monkeypatch):
        from gittensor.constants import DEFAULT_REPO_WEIGHT
        from gittensor.validator.utils import load_weights as module

        bad = tmp_path / 'master_repositories.json'
        self._write(bad, {'owner/good': {'weight': 0.5}, 'owner/bad': {'weight': -1.0}})
        monkeypatch.setattr(module, '_get_weights_dir', lambda: tmp_path)

        repos = module.load_master_repo_weights()

        assert repos['owner/good'].weight == 0.5
        assert repos['owner/bad'].weight == DEFAULT_REPO_WEIGHT

    def test_master_repos_non_finite_clamped(self, tmp_path, monkeypatch):
        from gittensor.constants import DEFAULT_REPO_WEIGHT
        from gittensor.validator.utils import load_weights as module

        bad = tmp_path / 'master_repositories.json'
        # JSON has no native NaN; simulate via string that float() would coerce in Python
        # but json.load returns None for "null"; use float('inf') path via Python-representable value.
        self._write(bad, {'owner/infinite': {'weight': 1e400}})
        monkeypatch.setattr(module, '_get_weights_dir', lambda: tmp_path)

        repos = module.load_master_repo_weights()

        assert repos['owner/infinite'].weight == DEFAULT_REPO_WEIGHT

    def test_language_weights_negative_clamped_dict_form(self, tmp_path, monkeypatch):
        from gittensor.validator.utils import load_weights as module

        bad = tmp_path / 'programming_languages.json'
        self._write(bad, {'py': {'weight': -0.5, 'language': 'python'}, 'js': {'weight': 1.2}})
        monkeypatch.setattr(module, '_get_weights_dir', lambda: tmp_path)

        langs = module.load_programming_language_weights()

        assert langs['py'].weight == 1.0  # defaulted
        assert langs['py'].language == 'python'  # unrelated field preserved
        assert langs['js'].weight == 1.2

    def test_language_weights_negative_clamped_plain_float_form(self, tmp_path, monkeypatch):
        """Backwards-compat path: plain-float JSON entries must also be clamped."""
        from gittensor.validator.utils import load_weights as module

        bad = tmp_path / 'programming_languages.json'
        self._write(bad, {'py': -0.5, 'js': 1.2})
        monkeypatch.setattr(module, '_get_weights_dir', lambda: tmp_path)

        langs = module.load_programming_language_weights()

        assert langs['py'].weight == 1.0  # defaulted
        assert langs['js'].weight == 1.2

    def test_token_weights_negative_entries_clamped(self, tmp_path, monkeypatch):
        """A negative structural or leaf token weight would invert scoring for that
        AST node type. Both dicts in token_weights.json must be bounds-checked."""
        from gittensor.validator.utils import load_weights as module

        token_file = tmp_path / 'token_weights.json'
        self._write(
            token_file,
            {
                'structural_bonus': {'function_declaration': 2.5, 'class_declaration': -1.0},
                'leaf_tokens': {'identifier': 0.1, 'string': -0.5},
            },
        )
        # load_token_config also calls load_programming_language_weights, which expects
        # its own file in the same weights dir; stub it with a minimal valid dict.
        lang_file = tmp_path / 'programming_languages.json'
        self._write(lang_file, {'py': {'weight': 1.0, 'language': 'python'}})
        monkeypatch.setattr(module, '_get_weights_dir', lambda: tmp_path)

        config = module.load_token_config()

        assert config.structural_bonus['function_declaration'] == 2.5
        assert config.structural_bonus['class_declaration'] == 0.0  # clamped
        assert config.leaf_tokens['identifier'] == 0.1
        assert config.leaf_tokens['string'] == 0.0  # clamped


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
