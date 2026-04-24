import pytest

from gittensor.classes import FileChange, PullRequest


def _file_change(filename: str) -> FileChange:
    return FileChange(
        pr_number=0,
        repository_full_name='mastodon/mastodon',
        filename=filename,
        changes=10,
        additions=10,
        deletions=0,
        status='added',
    )


@pytest.mark.parametrize(
    'filename',
    [
        # Real files in mastodon/mastodon spec/ tree (verified via Contents API).
        'spec/models/account_spec.rb',
        'spec/models/account_alias_spec.rb',
        'spec/models/account_conversation_spec.rb',
        'spec/services/account_search_service_spec.rb',
        'spec/services/after_block_service_spec.rb',
        'spec/controllers/application_controller_spec.rb',
        'spec/controllers/follower_accounts_controller_spec.rb',
        # *_spec.rb outside spec/ — filename pattern alone must catch.
        'lib/foo_spec.rb',
        'app/models/user_spec.rb',
        # Other RSpec-style ecosystems using underscore-spec naming.
        'src/util_spec.js',
        'pkg/handler_spec.ts',
    ],
)
def test_is_test_file_detects_rspec_underscore_spec_suffix(filename):
    assert _file_change(filename).is_test_file() is True


@pytest.mark.parametrize(
    'filename',
    [
        # RSpec helper/support files that don't end in _spec.rb but live in spec/.
        'spec/rails_helper.rb',
        'spec/support/factory_bot.rb',
        'spec/support/database_cleaner.rb',
        'spec/support/shared_examples.rb',
        # Nested spec/ at non-root.
        'engines/users/spec/models/user_spec.rb',
        'engines/users/spec/factories.rb',
    ],
)
def test_is_test_file_detects_spec_directory(filename):
    assert _file_change(filename).is_test_file() is True


@pytest.mark.parametrize(
    'filename',
    [
        # Lookalikes that must NOT trip the new patterns.
        # Filename "spec" prefix without trailing underscore-dot.
        'app/models/specification.rb',
        'src/spectrum.rb',
        'app/models/respec.rb',
        'lib/aspec.rb',
        'lib/inspector.rb',
        'src/spec.rb',
        'src/spec.js',
        # Pre-existing patterns must continue to reject ordinary source.
        'app/models/account.rb',
        'lib/foo.rb',
        'src/main.py',
    ],
)
def test_is_test_file_rejects_non_test_lookalikes(filename):
    assert _file_change(filename).is_test_file() is False


def test_is_test_file_preserves_existing_test_conventions():
    assert _file_change('tests/test_foo.py').is_test_file() is True
    assert _file_change('src/__tests__/foo.test.js').is_test_file() is True
    assert _file_change('pkg/foo_test.go').is_test_file() is True
    assert _file_change('app/foo_tests.rb').is_test_file() is True
    assert _file_change('src/foo.spec.js').is_test_file() is True
    assert _file_change('app/spec_helper.rb').is_test_file() is True
    assert _file_change('app/test_helper.py').is_test_file() is True


def test_pull_request_handles_deleted_label_event():
    pr_data = {
        'number': 42,
        'repository': {'owner': {'login': 'entrius'}, 'name': 'gittensor'},
        'state': 'OPEN',
        'closingIssuesReferences': {'nodes': []},
        'bodyText': 'Fix bug',
        'lastEditedAt': None,
        'mergedAt': None,
        'timelineItems': {'nodes': [{'label': None}]},
        'title': 'fix: guard deleted label events',
        'author': {'login': 'alice'},
        'createdAt': '2026-04-18T00:00:00Z',
        'additions': 3,
        'deletions': 1,
        'commits': {'totalCount': 1},
        'headRefOid': 'abc123',
        'baseRefOid': 'def456',
    }

    pr = PullRequest.from_graphql_response(pr_data, uid=1, hotkey='5Hotkey', github_id='123')

    assert pr.label is None
    assert pr.author_login == 'alice'
