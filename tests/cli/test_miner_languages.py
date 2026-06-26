# Entrius 2025

"""Tests for the `gitt miner languages` CLI command and its scoring helpers."""

import json
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from gittensor.cli.main import cli
from gittensor.cli.miner_commands.languages import (
    METHOD_LINE,
    METHOD_LINE_CAPPED,
    METHOD_TOKEN,
    _build_language_rows,
    _filter_languages,
    _rank_languages,
    _scoring_method,
    _summarize_languages,
)
from gittensor.validator.utils.load_weights import LanguageConfig

LANGS_PATH = 'gittensor.cli.miner_commands.languages.load_programming_language_weights'


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def sample_langs():
    """A mix of the three scoring methods: tree-sitter token, plain line-count, capped doc."""
    return {
        'rs': LanguageConfig(weight=2.0, language='rust'),
        'py': LanguageConfig(weight=1.5, language='python'),
        'svg': LanguageConfig(weight=0.30, language=None),  # no tree-sitter -> line-count
        'md': LanguageConfig(weight=0.12, language=None),  # non-code -> capped (md is in NON_CODE_EXTENSIONS)
    }


class TestScoringMethod:
    def test_tree_sitter_is_token(self):
        assert _scoring_method('rs', LanguageConfig(weight=2.0, language='rust')) == METHOD_TOKEN

    def test_no_language_is_line_count(self):
        assert _scoring_method('svg', LanguageConfig(weight=0.3, language=None)) == METHOD_LINE

    def test_non_code_extension_is_capped(self):
        assert _scoring_method('md', LanguageConfig(weight=0.12, language='markdown')) == METHOD_LINE_CAPPED

    def test_non_code_overrides_tree_sitter(self):
        # Even if a non-code ext carries a language mapping, it is capped line-count, never token.
        assert _scoring_method('json', LanguageConfig(weight=0.2, language='json')) == METHOD_LINE_CAPPED

    def test_leading_dot_and_case_normalized(self):
        assert _scoring_method('.RS', LanguageConfig(weight=2.0, language='rust')) == METHOD_TOKEN


class TestFilterLanguages:
    def test_code_only_keeps_token_methods(self, sample_langs):
        result = _filter_languages(sample_langs, code_only=True, search=None)
        assert set(result) == {'rs', 'py'}

    def test_no_filter_keeps_all(self, sample_langs):
        assert len(_filter_languages(sample_langs, code_only=False, search=None)) == 4

    def test_search_case_insensitive(self, sample_langs):
        assert set(_filter_languages(sample_langs, code_only=False, search='PY')) == {'py'}

    def test_code_only_combines_with_search(self, sample_langs):
        # 'svg' matches search but is not token-scored -> excluded under code_only.
        assert _filter_languages(sample_langs, code_only=True, search='svg') == {}


class TestRankLanguages:
    def test_sorted_by_weight_descending(self, sample_langs):
        ranked = _rank_languages(sample_langs)
        assert [ext for ext, _ in ranked] == ['rs', 'py', 'svg', 'md']

    def test_ties_broken_alphabetically(self):
        langs = {'zig': LanguageConfig(weight=1.0, language='zig'), 'awk': LanguageConfig(weight=1.0, language='awk')}
        assert [ext for ext, _ in _rank_languages(langs)] == ['awk', 'zig']


class TestSummarize:
    def test_method_counts(self, sample_langs):
        summary = _summarize_languages(_rank_languages(sample_langs))
        assert summary['total'] == 4
        assert summary['token_scored'] == 2
        assert summary['line_count'] == 1
        assert summary['line_count_capped'] == 1
        assert summary['non_code_line_cap'] == 300


class TestBuildRows:
    def test_rows_carry_method_and_tree_sitter(self, sample_langs):
        rows = _build_language_rows(_rank_languages(sample_langs), top=None)
        assert rows[0] == {'rank': 1, 'extension': 'rs', 'weight': 2.0, 'method': METHOD_TOKEN, 'tree_sitter': 'rust'}
        # line-count rows expose no tree-sitter language
        md_row = next(r for r in rows if r['extension'] == 'md')
        assert md_row['method'] == METHOD_LINE_CAPPED
        assert md_row['tree_sitter'] is None

    def test_top_n_truncates(self, sample_langs):
        rows = _build_language_rows(_rank_languages(sample_langs), top=2)
        assert [r['extension'] for r in rows] == ['rs', 'py']


class TestMinerLanguagesCommand:
    def test_help_text(self, runner):
        result = runner.invoke(cli, ['miner', 'languages', '--help'])
        assert result.exit_code == 0
        assert 'ranked by scoring weight' in result.output

    def test_alias(self, runner):
        result = runner.invoke(cli, ['m', 'languages', '--help'])
        assert result.exit_code == 0

    def test_table_output(self, runner, sample_langs):
        with patch(LANGS_PATH, return_value=sample_langs):
            result = runner.invoke(cli, ['miner', 'languages'])
        assert result.exit_code == 0
        assert 'rs' in result.output and 'token' in result.output

    def test_code_only_excludes_line_count(self, runner, sample_langs):
        with patch(LANGS_PATH, return_value=sample_langs):
            result = runner.invoke(cli, ['miner', 'languages', '--code-only', '--json-output'])
        payload = json.loads(result.output)
        exts = [r['extension'] for r in payload['languages']]
        assert set(exts) == {'rs', 'py'}

    def test_json_summary_counts_full_set(self, runner, sample_langs):
        # Summary reflects the full language set even when rows are filtered by --code-only.
        with patch(LANGS_PATH, return_value=sample_langs):
            result = runner.invoke(cli, ['miner', 'languages', '--code-only', '--json-output'])
        payload = json.loads(result.output)
        assert payload['summary']['total'] == 4
        assert payload['summary']['token_scored'] == 2

    def test_top_flag(self, runner, sample_langs):
        with patch(LANGS_PATH, return_value=sample_langs):
            result = runner.invoke(cli, ['miner', 'languages', '--top', '1', '--json-output'])
        assert len(json.loads(result.output)['languages']) == 1

    def test_search_no_match_table(self, runner, sample_langs):
        with patch(LANGS_PATH, return_value=sample_langs):
            result = runner.invoke(cli, ['miner', 'languages', '--search', 'zzz'])
        assert result.exit_code == 0
        assert 'No extensions match' in result.output

    def test_empty_weights_file_errors(self, runner):
        with patch(LANGS_PATH, return_value={}):
            result = runner.invoke(cli, ['miner', 'languages'])
        assert result.exit_code != 0
        assert 'No languages found' in result.output

    def test_empty_weights_file_json_error(self, runner):
        with patch(LANGS_PATH, return_value={}):
            result = runner.invoke(cli, ['miner', 'languages', '--json-output'])
        assert result.exit_code != 0
        assert json.loads(result.output)['success'] is False
