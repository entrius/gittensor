"""Verify _merge_scan_issues stamps miner identity on scan-discovered issues."""

from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List

from gittensor.classes import Issue, MinerEvaluation
from gittensor.validator.issue_discovery.scoring import _DiscovererData, _merge_scan_issues

UID = 5
HOTKEY = 'hk5'
GH_ID = '9001'
REPO = 'owner/repo'


def _make_issue() -> Issue:
    return Issue(
        number=10,
        pr_number=0,
        repository_full_name=REPO,
        title='scan issue',
        state='CLOSED',
        closed_at=datetime(2025, 3, 1, tzinfo=timezone.utc),
    )


def _run(issues: List[Issue]) -> None:
    scan_issues: Dict[str, List[Issue]] = {GH_ID: issues}
    gh_to_uid = {GH_ID: UID}
    discoverer_data: Dict[str, _DiscovererData] = defaultdict(_DiscovererData)
    evaluations = {UID: MinerEvaluation(uid=UID, hotkey=HOTKEY, github_id=GH_ID)}
    _merge_scan_issues(scan_issues, gh_to_uid, discoverer_data, evaluations)


def test_scan_issue_gets_miner_identity():
    issue = _make_issue()
    _run([issue])

    assert issue.uid == UID
    assert issue.hotkey == HOTKEY
    assert issue.github_id == GH_ID
