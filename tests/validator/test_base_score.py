# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Integration tests for calculate_base_score - verifying per-category density
calculation and SOURCE-only contribution bonus using real tree-sitter scoring"""

from typing import Dict, List, Optional

import pytest

from gittensor.classes import FileChange, PullRequest
from gittensor.constants import MIN_TOKEN_SCORE_FOR_BASE_SCORE
from gittensor.utils.github_api_tools import FileContentPair
from gittensor.validator.oss_contributions.scoring import calculate_base_score
from gittensor.validator.utils.load_weights import (
    LanguageConfig,
    TokenConfig,
    load_programming_language_weights,
    load_token_config,
)
from tests.validator.conftest import PRBuilder

_THRESHOLD = MIN_TOKEN_SCORE_FOR_BASE_SCORE

_SOURCE_CODE = """\
def validate_input(value, min_val, max_val):
    if not isinstance(value, (int, float)):
        raise TypeError("Expected numeric value")
    if value < min_val or value > max_val:
        raise ValueError(f"Value {value} out of range [{min_val}, {max_val}]")
    return True

def clamp(value, low, high):
    return max(low, min(high, value))

class Processor:
    def __init__(self, name):
        self.name = name
        self.results = []

    def process(self, items):
        for item in items:
            if validate_input(item, 0, 100):
                self.results.append(clamp(item, 0, 100))
        return self.results
"""

_TEST_CODE = """\
def test_validate_input_valid():
    assert validate_input(5, 0, 10) is True

def test_validate_input_type_error():
    try:
        validate_input("abc", 0, 10)
        assert False
    except TypeError:
        pass

def test_validate_input_range_error():
    try:
        validate_input(20, 0, 10)
        assert False
    except ValueError:
        pass

def test_clamp_within_range():
    assert clamp(5, 0, 10) == 5

def test_clamp_below():
    assert clamp(-1, 0, 10) == 0

def test_clamp_above():
    assert clamp(15, 0, 10) == 10
"""

_LARGE_TEST_CODE = """\
def test_processor_init():
    p = Processor("test")
    assert p.name == "test"
    assert p.results == []

def test_processor_process_valid():
    p = Processor("test")
    result = p.process([1, 50, 99])
    assert result == [1, 50, 99]

def test_processor_process_clamp():
    p = Processor("test")
    result = p.process([150, -10, 50])
    assert result == [100, 0, 50]

def test_validate_boundary():
    assert validate_input(0, 0, 100) is True
    assert validate_input(100, 0, 100) is True

def test_clamp_boundary():
    assert clamp(0, 0, 100) == 0
    assert clamp(100, 0, 100) == 100
"""

# Same logic as _SOURCE_CODE but spread across more lines
_VERBOSE_SOURCE = """\
def validate_input(
    value,
    min_val,
    max_val,
):
    if not isinstance(
        value,
        (int, float),
    ):
        raise TypeError(
            "Expected numeric value"
        )
    if (
        value < min_val
        or value > max_val
    ):
        raise ValueError(
            f"Value {value} out of range [{min_val}, {max_val}]"
        )
    return True

def clamp(
    value,
    low,
    high,
):
    return max(
        low,
        min(
            high,
            value,
        ),
    )

class Processor:
    def __init__(
        self,
        name,
    ):
        self.name = name
        self.results = []

    def process(
        self,
        items,
    ):
        for item in items:
            if validate_input(
                item,
                0,
                100,
            ):
                self.results.append(
                    clamp(
                        item,
                        0,
                        100,
                    )
                )
        return self.results
"""

_SOURCE_CODE_V1 = """\
def clamp(value, low, high):
    return max(low, min(high, value))
"""

_SOURCE_CODE_V2 = """\
def clamp(value, low, high):
    if not isinstance(value, (int, float)):
        raise TypeError("Expected numeric")
    if low > high:
        raise ValueError("low must be <= high")
    return max(low, min(high, value))
"""


@pytest.fixture
def token_config() -> TokenConfig:
    return load_token_config()


@pytest.fixture
def programming_languages() -> Dict[str, LanguageConfig]:
    return load_programming_language_weights()


def _change(filename: str, content: str, status: str = 'added') -> FileChange:
    lines: int = content.count('\n')
    return FileChange(
        pr_number=1,
        repository_full_name='test/repo',
        filename=filename,
        changes=lines,
        additions=lines if status != 'removed' else 0,
        deletions=lines if status == 'removed' else 0,
        status=status,
    )


def _contents(
    filename: str, new_content: Optional[str], old_content: Optional[str] = None
) -> tuple[str, FileContentPair]:
    return filename, FileContentPair(old_content=old_content, new_content=new_content)


def _score(
    pr: PullRequest,
    file_changes: List[FileChange],
    file_contents: List[tuple[str, FileContentPair]],
    token_config: TokenConfig,
    programming_languages: Dict[str, LanguageConfig],
) -> float:
    """Set file_changes on PR and call calculate_base_score"""
    pr.set_file_changes(file_changes)
    return calculate_base_score(pr, programming_languages, token_config, dict(file_contents))


def test_adding_tests_does_not_reduce_score(
    pr_factory: PRBuilder,
    token_config: TokenConfig,
    programming_languages: Dict[str, LanguageConfig],
):
    """Adding test files to a source PR must never lower the base score"""
    source_change = _change('main.py', _SOURCE_CODE)
    source_content = _contents('main.py', _SOURCE_CODE)
    test_change = _change('tests/test_main.py', _TEST_CODE)
    test_content = _contents('tests/test_main.py', _TEST_CODE)

    pr1 = pr_factory.merged()
    score_without = _score(pr1, [source_change], [source_content], token_config, programming_languages)

    pr2 = pr_factory.merged()
    score_with = _score(
        pr2,
        [source_change, test_change],
        [source_content, test_content],
        token_config,
        programming_languages,
    )

    assert score_with > score_without
    assert score_without > 0


def test_tests_do_not_affect_contribution_bonus(
    pr_factory: PRBuilder,
    token_config: TokenConfig,
    programming_languages: Dict[str, LanguageConfig],
):
    """Adding small or large test files should produce the same modest
    increase - the difference is only from test density, not from bonus"""
    source_change = _change('main.py', _SOURCE_CODE)
    source_content = _contents('main.py', _SOURCE_CODE)
    small_test_change = _change('tests/test_a.py', _TEST_CODE)
    small_test_content = _contents('tests/test_a.py', _TEST_CODE)
    big_test = _TEST_CODE + _LARGE_TEST_CODE
    big_test_change = _change('tests/test_a.py', big_test)
    big_test_content = _contents('tests/test_a.py', big_test)

    pr_base = pr_factory.merged()
    score_without = _score(pr_base, [source_change], [source_content], token_config, programming_languages)

    pr_small = pr_factory.merged()
    score_small = _score(
        pr_small,
        [source_change, small_test_change],
        [source_content, small_test_content],
        token_config,
        programming_languages,
    )

    pr_big = pr_factory.merged()
    score_big = _score(
        pr_big,
        [source_change, big_test_change],
        [source_content, big_test_content],
        token_config,
        programming_languages,
    )

    # Both increase over baseline
    assert score_small > score_without
    assert score_big > score_without

    # Increases are modest relative to baseline (test weight is 0.05x)
    assert (score_small - score_without) / score_without < 0.1
    assert (score_big - score_without) / score_without < 0.1


def test_same_code_in_test_path_scores_much_lower(
    pr_factory: PRBuilder,
    token_config: TokenConfig,
    programming_languages: Dict[str, LanguageConfig],
):
    """Identical code placed in a test directory scores much lower than
    in a source path, because test weight is 0.05x and no contribution bonus"""
    source_change = _change('main.py', _SOURCE_CODE)
    source_content = _contents('main.py', _SOURCE_CODE)
    source_as_test_change = _change('tests/test_main.py', _SOURCE_CODE)
    source_as_test_content = _contents('tests/test_main.py', _SOURCE_CODE)

    pr_src = pr_factory.merged()
    score_as_source = _score(pr_src, [source_change], [source_content], token_config, programming_languages)

    pr_test = pr_factory.merged()
    score_as_test = _score(
        pr_test,
        [source_as_test_change],
        [source_as_test_content],
        token_config,
        programming_languages,
    )

    assert score_as_source > (score_as_test * 10)


def test_tests_do_not_affect_threshold(
    pr_factory: PRBuilder,
    token_config: TokenConfig,
    programming_languages: Dict[str, LanguageConfig],
):
    """A PR below the token score threshold stays below even if large
    test files are added - the threshold only checks SOURCE category"""
    tiny_change = _change('tiny.py', 'x = 1\n')
    tiny_content = _contents('tiny.py', 'x = 1\n')
    big_test = _TEST_CODE + _LARGE_TEST_CODE
    big_test_change = _change('tests/test_a.py', big_test)
    big_test_content = _contents('tests/test_a.py', big_test)

    pr_tiny = pr_factory.merged(token_score=0.0)
    score_tiny = _score(pr_tiny, [tiny_change], [tiny_content], token_config, programming_languages)

    pr_tiny_with_tests = pr_factory.merged(token_score=0.0)
    score_tiny_with_tests = _score(
        pr_tiny_with_tests,
        [tiny_change, big_test_change],
        [tiny_content, big_test_content],
        token_config,
        programming_languages,
    )

    assert score_tiny == score_tiny_with_tests


def test_adding_non_code_files_does_not_reduce_score(
    pr_factory: PRBuilder,
    token_config: TokenConfig,
    programming_languages: Dict[str, LanguageConfig],
):
    """Adding non-code files (markdown, yaml) must never lower the base score"""
    source_change = _change('main.py', _SOURCE_CODE)
    source_content = _contents('main.py', _SOURCE_CODE)
    readme = '# Project\n\nSome documentation about the project\n' * 10
    readme_change = _change('README.md', readme)
    readme_content = _contents('README.md', readme)

    pr1 = pr_factory.merged()
    score_without = _score(pr1, [source_change], [source_content], token_config, programming_languages)

    pr2 = pr_factory.merged()
    score_with = _score(
        pr2,
        [source_change, readme_change],
        [source_content, readme_content],
        token_config,
        programming_languages,
    )

    assert score_with > score_without


def test_non_code_does_not_affect_contribution_bonus(
    pr_factory: PRBuilder,
    token_config: TokenConfig,
    programming_languages: Dict[str, LanguageConfig],
):
    """Adding small or large non-code files should produce the same increase
    because line-count density = lang_weight regardless of size"""
    source_change = _change('main.py', _SOURCE_CODE)
    source_content = _contents('main.py', _SOURCE_CODE)
    small_yaml = 'key: value\n' * 5
    small_yaml_change = _change('config.yaml', small_yaml)
    small_yaml_content = _contents('config.yaml', small_yaml)
    big_yaml = 'key: value\nlist:\n  - item1\n  - item2\n' * 50
    big_yaml_change = _change('config.yaml', big_yaml)
    big_yaml_content = _contents('config.yaml', big_yaml)

    pr_base = pr_factory.merged()
    score_without = _score(pr_base, [source_change], [source_content], token_config, programming_languages)

    pr_small = pr_factory.merged()
    score_small = _score(
        pr_small,
        [source_change, small_yaml_change],
        [source_content, small_yaml_content],
        token_config,
        programming_languages,
    )

    pr_big = pr_factory.merged()
    score_big = _score(
        pr_big,
        [source_change, big_yaml_change],
        [source_content, big_yaml_content],
        token_config,
        programming_languages,
    )

    assert score_small > score_without
    assert score_big > score_without
    assert score_big == score_small


def test_source_code_scores_much_higher_than_non_code(
    pr_factory: PRBuilder,
    token_config: TokenConfig,
    programming_languages: Dict[str, LanguageConfig],
):
    """Tree-diff scored source code produces a much higher base score than
    line-count scored non-code files"""
    source_change = _change('main.py', _SOURCE_CODE)
    source_content = _contents('main.py', _SOURCE_CODE)
    big_yaml = 'key: value\nlist:\n  - item1\n  - item2\n' * 50
    yaml_change = _change('config.yaml', big_yaml)
    yaml_content = _contents('config.yaml', big_yaml)

    pr_src = pr_factory.merged()
    score_as_source = _score(pr_src, [source_change], [source_content], token_config, programming_languages)

    pr_unc = pr_factory.merged()
    score_as_non_code = _score(
        pr_unc,
        [yaml_change],
        [yaml_content],
        token_config,
        programming_languages,
    )

    assert score_as_source > (score_as_non_code * 10)


def test_non_code_does_not_affect_threshold(
    pr_factory: PRBuilder,
    token_config: TokenConfig,
    programming_languages: Dict[str, LanguageConfig],
):
    """A PR below the token score threshold stays below even if large
    non-code files are added"""
    tiny_change = _change('tiny.py', 'x = 1\n')
    tiny_content = _contents('tiny.py', 'x = 1\n')
    big_yaml = 'key: value\nlist:\n  - item1\n  - item2\n' * 50
    big_yaml_change = _change('config.yaml', big_yaml)
    big_yaml_content = _contents('config.yaml', big_yaml)

    pr_tiny = pr_factory.merged(token_score=0.0)
    score_tiny = _score(pr_tiny, [tiny_change], [tiny_content], token_config, programming_languages)

    pr_tiny_with_yaml = pr_factory.merged(token_score=0.0)
    score_tiny_with_yaml = _score(
        pr_tiny_with_yaml,
        [tiny_change, big_yaml_change],
        [tiny_content, big_yaml_content],
        token_config,
        programming_languages,
    )

    assert score_tiny == score_tiny_with_yaml


def test_deleted_file_does_not_change_score(
    pr_factory: PRBuilder,
    token_config: TokenConfig,
    programming_languages: Dict[str, LanguageConfig],
):
    """A deleted file contributes score=0 and must not reduce the base score"""
    source_change = _change('main.py', _SOURCE_CODE)
    source_content = _contents('main.py', _SOURCE_CODE)
    deleted_change = _change('old.py', 'def old(): pass\n', status='removed')
    deleted_content = _contents('old.py', None)

    pr1 = pr_factory.merged()
    score_without = _score(pr1, [source_change], [source_content], token_config, programming_languages)

    pr2 = pr_factory.merged()
    score_with = _score(
        pr2,
        [source_change, deleted_change],
        [source_content, deleted_content],
        token_config,
        programming_languages,
    )

    assert score_without == score_with


def test_unsupported_file_does_not_change_score(
    pr_factory: PRBuilder,
    token_config: TokenConfig,
    programming_languages: Dict[str, LanguageConfig],
):
    """A file with an unsupported extension contributes score=0 and must
    not reduce the base score"""
    source_change = _change('main.py', _SOURCE_CODE)
    source_content = _contents('main.py', _SOURCE_CODE)
    unknown_change = _change('data.xyz', 'some unknown format\n' * 10)
    unknown_content = _contents('data.xyz', 'some unknown format\n' * 10)

    pr1 = pr_factory.merged()
    score_without = _score(pr1, [source_change], [source_content], token_config, programming_languages)

    pr2 = pr_factory.merged()
    score_with = _score(
        pr2,
        [source_change, unknown_change],
        [source_content, unknown_content],
        token_config,
        programming_languages,
    )

    assert score_without == score_with


def test_adding_test_category_increases_score_beyond_single_cap(
    pr_factory: PRBuilder,
    token_config: TokenConfig,
    programming_languages: Dict[str, LanguageConfig],
):
    """Each category has its own density cap, so adding a test category
    can push the total score above what a single source category achieves"""
    source_change = _change('main.py', _SOURCE_CODE)
    source_content = _contents('main.py', _SOURCE_CODE)
    test_change = _change('tests/test_main.py', _TEST_CODE)
    test_content = _contents('tests/test_main.py', _TEST_CODE)

    pr_one = pr_factory.merged()
    score_source = _score(pr_one, [source_change], [source_content], token_config, programming_languages)

    pr_two = pr_factory.merged()
    score_both = _score(
        pr_two,
        [source_change, test_change],
        [source_content, test_content],
        token_config,
        programming_languages,
    )

    assert score_both > score_source


def test_verbose_formatting_decreases_score(
    pr_factory: PRBuilder,
    token_config: TokenConfig,
    programming_languages: Dict[str, LanguageConfig],
):
    """Same logic reformatted across more lines produces a lower score
    because density (token_score / lines) drops"""
    compact_change = _change('main.py', _SOURCE_CODE)
    compact_content = _contents('main.py', _SOURCE_CODE)
    verbose_change = _change('main.py', _VERBOSE_SOURCE)
    verbose_content = _contents('main.py', _VERBOSE_SOURCE)

    pr_compact = pr_factory.merged()
    score_compact = _score(pr_compact, [compact_change], [compact_content], token_config, programming_languages)

    pr_verbose = pr_factory.merged()
    score_verbose = _score(pr_verbose, [verbose_change], [verbose_content], token_config, programming_languages)

    assert score_compact > score_verbose
    assert score_verbose > 0


def test_modified_file_scores_diff_only(
    pr_factory: PRBuilder,
    token_config: TokenConfig,
    programming_languages: Dict[str, LanguageConfig],
):
    """A modified file scores only the AST diff between old and new content,
    not the entire new file"""
    new_change = _change('main.py', _SOURCE_CODE_V2)
    new_content = _contents('main.py', _SOURCE_CODE_V2)
    mod_change = _change('main.py', _SOURCE_CODE_V2, status='modified')
    mod_content = _contents('main.py', _SOURCE_CODE_V2, old_content=_SOURCE_CODE_V1)

    pr_new = pr_factory.merged()
    score_new_file = _score(pr_new, [new_change], [new_content], token_config, programming_languages)

    pr_mod = pr_factory.merged()
    score_modified = _score(pr_mod, [mod_change], [mod_content], token_config, programming_languages)

    assert score_new_file > score_modified
    assert score_modified > 0


def test_threshold_uses_source_category_only(
    pr_factory: PRBuilder,
    token_config: TokenConfig,
    programming_languages: Dict[str, LanguageConfig],
):
    """Threshold check uses only SOURCE category score - substantial code
    placed entirely in a test path gets base_score=0 because SOURCE is empty"""
    # Substantial code in a test directory - categorized as TEST, not SOURCE
    test_change = _change('tests/test_main.py', _SOURCE_CODE)
    test_content = _contents('tests/test_main.py', _SOURCE_CODE)

    pr_test_only = pr_factory.merged()
    score_test_only = _score(pr_test_only, [test_change], [test_content], token_config, programming_languages)

    # SOURCE category is empty so threshold fails - base score must be 0
    assert score_test_only == 0

    # Same code in a source path scores well above 0
    source_change = _change('main.py', _SOURCE_CODE)
    source_content = _contents('main.py', _SOURCE_CODE)

    pr_source = pr_factory.merged()
    score_source = _score(pr_source, [source_change], [source_content], token_config, programming_languages)

    assert score_source > 0


def test_below_threshold_scores_less(
    pr_factory: PRBuilder,
    token_config: TokenConfig,
    programming_languages: Dict[str, LanguageConfig],
):
    """A trivial change (below token score threshold) scores strictly less
    than a substantial change (above threshold)"""
    tiny_change = _change('tiny.py', 'x = 1\n')
    tiny_content = _contents('tiny.py', 'x = 1\n')
    source_change = _change('main.py', _SOURCE_CODE)
    source_content = _contents('main.py', _SOURCE_CODE)

    pr_below = pr_factory.merged(token_score=0.0)
    score_below = _score(pr_below, [tiny_change], [tiny_content], token_config, programming_languages)

    pr_above = pr_factory.merged()
    score_above = _score(pr_above, [source_change], [source_content], token_config, programming_languages)

    assert score_above > score_below
