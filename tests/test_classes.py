import pytest

from gittensor.classes import FileChange, PullRequest


def _file_change(filename: str) -> FileChange:
    return FileChange(
        pr_number=0,
        repository_full_name='nextcloud/android',
        filename=filename,
        changes=10,
        additions=10,
        deletions=0,
        status='added',
    )


@pytest.mark.parametrize(
    'filename',
    [
        'app/src/androidTest/java/com/owncloud/android/UploadIT.java',
        'app/src/androidTest/java/com/owncloud/android/AbstractIT.java',
        'app/src/androidTest/java/com/nextcloud/client/ActivitiesFragmentIT.kt',
        'app/src/androidTestGeneric/java/com/example/FooIT.java',
        'app/src/androidTestGplay/java/com/example/BarIT.kt',
        'app/src/androidTest/java/com/example/HelperUtils.kt',
        'src/integrationTest/java/com/example/FooIT.java',
        'src/integrationTest/kotlin/com/example/HelperKt.kt',
    ],
)
def test_is_test_file_detects_gradle_test_source_sets(filename):
    assert _file_change(filename).is_test_file() is True


@pytest.mark.parametrize(
    'filename',
    [
        'spec/models/account_spec.rb',
        'spec/services/account_search_service_spec.rb',
        'spec/rails_helper.rb',
        'spec/support/factory_bot.rb',
        'engines/users/spec/models/user_spec.rb',
        'lib/foo_spec.rb',
        'app/models/user_spec.rb',
        'src/util_spec.js',
    ],
)
def test_is_test_file_detects_rspec_conventions(filename):
    assert _file_change(filename).is_test_file() is True


@pytest.mark.parametrize(
    'filename',
    [
        'app/build.gradle.kts',
        'src/main/java/com/example/Bar.java',
        'docs/androidtest.md',
        'tools/androidtestutils.py',
        'app/models/specification.rb',
        'lib/spectrum.rb',
        'config/spec.rb',
    ],
)
def test_is_test_file_rejects_non_test_lookalikes(filename):
    assert _file_change(filename).is_test_file() is False


def test_is_test_file_preserves_existing_test_conventions():
    assert _file_change('src/tests/test_foo.py').is_test_file() is True
    assert _file_change('src/__tests__/foo.test.js').is_test_file() is True
    assert _file_change('pkg/foo_test.go').is_test_file() is True
    assert _file_change('src/foo/bar.py').is_test_file() is False


@pytest.mark.parametrize(
    'filename,expected',
    [
        ('Dockerfile', 'dockerfile'),
        ('dockerfile', 'dockerfile'),
        ('ops/Dockerfile', 'dockerfile'),
        ('Makefile', 'makefile'),
        ('makefile', 'makefile'),
        ('build.mk', 'mk'),
        ('README', ''),
    ],
)
def test_file_extension_handles_configured_extensionless_filenames(filename, expected):
    assert _file_change(filename).file_extension == expected


@pytest.mark.parametrize(
    'filename',
    [
        'src/MyProject.Tests/AccountServiceTests.cs',
        'src/MyProject.Tests/AccountServiceTest.cs',
    ],
)
def test_is_test_file_detects_dotnet_dotted_tests_directory(filename):
    assert _file_change(filename).is_test_file() is True


@pytest.mark.parametrize(
    'filename',
    [
        'conftest.py',
        'tests/conftest.py',
        'project/conftest.py',
        'project/sub/package/conftest.py',
    ],
)
def test_is_test_file_detects_conftest_at_any_depth(filename):
    assert _file_change(filename).is_test_file() is True


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
