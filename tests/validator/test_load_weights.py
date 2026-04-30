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


class TestLoadMasterRepoWeightsMalformedEntries:
    """Per-entry malformed values must not collapse the whole loader to {}.

    The companion loader load_programming_language_weights already isolates
    malformed entries. This class pins down the same contract for
    load_master_repo_weights so a single bad chunk of master_repositories.json
    cannot zero-out an entire scoring round.
    """

    @staticmethod
    def _write_master_repos(tmp_path, payload):
        import json

        (tmp_path / 'master_repositories.json').write_text(json.dumps(payload))

    def test_non_dict_entry_isolated_other_entries_load(self, tmp_path, monkeypatch):
        """A non-dict entry (e.g. list) is skipped; well-formed siblings still load."""
        from gittensor.validator.utils import load_weights as lw

        self._write_master_repos(
            tmp_path,
            {
                'foo/good-one': {'weight': 0.5, 'mirror_enabled': True},
                'foo/bad-list': ['not', 'a', 'dict'],
                'foo/good-two': {'weight': 0.25},
            },
        )
        monkeypatch.setattr(lw, '_get_weights_dir', lambda: tmp_path)

        repos = lw.load_master_repo_weights()

        assert 'foo/good-one' in repos
        assert 'foo/good-two' in repos
        assert 'foo/bad-list' not in repos
        assert repos['foo/good-one'].weight == 0.5
        assert repos['foo/good-one'].mirror_enabled is True
        assert repos['foo/good-two'].weight == 0.25

    def test_plain_float_entry_back_compat(self, tmp_path, monkeypatch):
        """Plain-float weight loads as RepositoryConfig with default flags.

        Mirrors the back-compat path already accepted by
        load_programming_language_weights().
        """
        from gittensor.validator.utils import load_weights as lw

        self._write_master_repos(
            tmp_path,
            {
                'foo/dict-form': {'weight': 0.5},
                'foo/legacy-float': 0.42,
            },
        )
        monkeypatch.setattr(lw, '_get_weights_dir', lambda: tmp_path)

        repos = lw.load_master_repo_weights()

        assert repos['foo/legacy-float'].weight == 0.42
        assert repos['foo/legacy-float'].mirror_enabled is False
        assert repos['foo/legacy-float'].inactive_at is None

    def test_unparseable_weight_skipped_others_survive(self, tmp_path, monkeypatch):
        """A non-numeric weight in a dict entry is skipped, not promoted to default."""
        from gittensor.validator.utils import load_weights as lw

        self._write_master_repos(
            tmp_path,
            {
                'foo/good': {'weight': 0.5},
                'foo/bad-weight': {'weight': 'not-a-number'},
                'foo/good-two': {'weight': 0.1},
            },
        )
        monkeypatch.setattr(lw, '_get_weights_dir', lambda: tmp_path)

        repos = lw.load_master_repo_weights()

        assert 'foo/good' in repos
        assert 'foo/good-two' in repos
        assert 'foo/bad-weight' not in repos

    def test_string_entry_isolated(self, tmp_path, monkeypatch):
        """String values are not float-coercible and must skip cleanly."""
        from gittensor.validator.utils import load_weights as lw

        self._write_master_repos(
            tmp_path,
            {
                'foo/good': {'weight': 0.5},
                'foo/bad-string': 'oops',
            },
        )
        monkeypatch.setattr(lw, '_get_weights_dir', lambda: tmp_path)

        repos = lw.load_master_repo_weights()

        assert 'foo/good' in repos
        assert 'foo/bad-string' not in repos
        assert len(repos) == 1

    def test_only_malformed_entries_returns_empty_dict_not_outer_except(self, tmp_path, monkeypatch):
        """All-malformed JSON yields {} via the per-entry skip path, not the outer except.

        Regression: previously an AttributeError on the first non-dict entry escaped
        through to the outer ``except Exception`` block, so a payload with one bad
        entry plus many good ones returned {} for the *whole* file.
        """
        from gittensor.validator.utils import load_weights as lw

        self._write_master_repos(
            tmp_path,
            {
                'foo/bad-list': ['x'],
                'foo/bad-string': 'x',
            },
        )
        monkeypatch.setattr(lw, '_get_weights_dir', lambda: tmp_path)

        repos = lw.load_master_repo_weights()
        assert repos == {}


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
