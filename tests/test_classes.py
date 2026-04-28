import pytest

from gittensor.classes import FileChange, PullRequest


def _file(filename: str) -> FileChange:
    return FileChange(
        pr_number=0,
        repository_full_name='owner/repo',
        filename=filename,
        changes=1,
        additions=1,
        deletions=0,
        status='added',
    )


class TestIsTestFile:
    @pytest.mark.parametrize(
        'path',
        [
            # Existing directory patterns (regression coverage)
            'tests/test_foo.py',
            'test/foo.py',
            '__tests__/x.tsx',
            'src/__test__/y.ts',
            # Existing filename patterns (regression coverage)
            'pkg/test_module.py',
            'pkg/spec_module.rb',
            'internal/foo_test.go',
            'internal/foo_tests.go',
            'src/foo.test.ts',
            'src/foo.tests.ts',
            'src/foo.spec.ts',
            'src/test.py',
            'src/tests.py',
            # Android Gradle source sets (closes #765)
            'app/src/androidTest/java/com/owncloud/android/UploadIT.java',
            'app/src/androidTestGeneric/java/com/example/Foo.kt',
            'app/src/androidTestGplay/java/com/example/Bar.kt',
            'app/src/integrationTest/java/com/example/Baz.java',
            # JavaScript / TypeScript e2e conventions
            'e2e/src/api/specs/duplicate.e2e-spec.ts',
            'e2e/src/utils.ts',
            'cypress/e2e/login.cy.ts',
            'apps/web/e2e/checkout.spec.ts',
            'src/foo.e2e-spec.ts',
            'src/foo.e2e-test.ts',
        ],
    )
    def test_classified_as_test(self, path: str) -> None:
        assert _file(path).is_test_file() is True

    @pytest.mark.parametrize(
        'path',
        [
            # Production sources that must NOT be classified as tests
            'src/main.py',
            'app/src/main/java/com/example/MainActivity.java',
            'lib/server.go',
            'src/index.ts',
            # Guard against the IT-suffix false-positive trap (Edit, Audit, Commit etc.)
            # described in #765. A naive r'it\.(java|kt)$' would match these.
            'src/Edit.java',
            'src/Audit.java',
            'lib/Commit.kt',
            'lib/Permit.java',
            'lib/Submit.kt',
            'lib/Init.kt',
            'lib/Visit.kt',
        ],
    )
    def test_classified_as_production(self, path: str) -> None:
        assert _file(path).is_test_file() is False


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
