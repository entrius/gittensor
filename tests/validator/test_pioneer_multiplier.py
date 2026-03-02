import pytest
from datetime import datetime, timezone, timedelta
from typing import Dict
from unittest.mock import MagicMock

from gittensor.classes import MinerEvaluation, PullRequest, PRState
from gittensor.validator.evaluation.scoring import calculate_pioneer_multiplier
from gittensor.constants import PIONEER_PR_BOOST

def create_mock_pr(repo_name: str, merged_at: datetime, uid: int, pr_number: int = 1) -> PullRequest:
    pr = MagicMock(spec=PullRequest)
    pr.repository_full_name = repo_name
    pr.merged_at = merged_at
    pr.uid = uid
    pr.number = pr_number
    return pr

def create_mock_eval(uid: int, prs: list) -> MinerEvaluation:
    evaluation = MagicMock(spec=MinerEvaluation)
    evaluation.uid = uid
    evaluation.merged_pull_requests = prs
    return evaluation

def test_single_pr_gets_pioneer_bonus():
    base_time = datetime.now(timezone.utc)
    pr1 = create_mock_pr("owner/repo1", base_time, 1)
    
    eval1 = create_mock_eval(1, [pr1])
    all_evals = {1: eval1}
    
    multiplier = calculate_pioneer_multiplier(pr1, eval1, all_evals)
    assert multiplier == 1.0 + PIONEER_PR_BOOST

def test_multiple_prs_different_times():
    base_time = datetime.now(timezone.utc)
    pr1 = create_mock_pr("owner/repo1", base_time, 1, pr_number=101)
    pr2 = create_mock_pr("owner/repo1", base_time + timedelta(hours=1), 2, pr_number=102)
    
    eval1 = create_mock_eval(1, [pr1])
    eval2 = create_mock_eval(2, [pr2])
    all_evals = {1: eval1, 2: eval2}
    
    multiplier1 = calculate_pioneer_multiplier(pr1, eval1, all_evals)
    multiplier2 = calculate_pioneer_multiplier(pr2, eval2, all_evals)
    
    # First miner gets max boost (1.0 + 5.0)
    assert multiplier1 == 1.0 + PIONEER_PR_BOOST
    # Second miner gets halved boost (1.0 + 2.5)
    assert multiplier2 == 1.0 + (PIONEER_PR_BOOST * 0.5)

def test_multiple_prs_same_time_tiebreaker():
    base_time = datetime.now(timezone.utc)
    # Miner 5 and Miner 2 merge at the exact same moment
    pr1 = create_mock_pr("owner/repo1", base_time, 5, pr_number=201)
    pr2 = create_mock_pr("owner/repo1", base_time, 2, pr_number=202)
    
    eval1 = create_mock_eval(5, [pr1])
    eval2 = create_mock_eval(2, [pr2])
    all_evals = {5: eval1, 2: eval2}
    
    # UID 2 SHOULD get first place (lower UID), UID 5 gets second place
    multiplier1 = calculate_pioneer_multiplier(pr1, eval1, all_evals) 
    multiplier2 = calculate_pioneer_multiplier(pr2, eval2, all_evals) 
    
    assert multiplier2 == 1.0 + PIONEER_PR_BOOST
    assert multiplier1 == 1.0 + (PIONEER_PR_BOOST * 0.5)

def test_repo_already_has_pr_from_last_scoring_lookback():
    base_time = datetime.now(timezone.utc)
    pr1 = create_mock_pr("owner/repo1", base_time, 1, pr_number=301)
    pr2 = create_mock_pr("owner/repo1", base_time + timedelta(days=2), 2, pr_number=302)
    
    eval1 = create_mock_eval(1, [pr1])
    all_evals = {1: eval1}
    
    # First PR gets max boost
    assert calculate_pioneer_multiplier(pr1, eval1, all_evals) == 1.0 + PIONEER_PR_BOOST
    # Subsequent PRs on the same repo get halved boost
    assert calculate_pioneer_multiplier(pr2, eval1, all_evals) == 1.0 + (PIONEER_PR_BOOST * 0.5)

def test_different_repos_get_different_bonuses():
    base_time = datetime.now(timezone.utc)
    pr_repo1 = create_mock_pr("owner/repo1", base_time, 1, pr_number=401)
    pr_repo2 = create_mock_pr("owner/repo2", base_time, 2, pr_number=402)
    
    eval1 = create_mock_eval(1, [pr_repo1])
    eval2 = create_mock_eval(2, [pr_repo2])
    all_evals = {1: eval1, 2: eval2}
    
    assert calculate_pioneer_multiplier(pr_repo1, eval1, all_evals) == 1.0 + PIONEER_PR_BOOST
    assert calculate_pioneer_multiplier(pr_repo2, eval2, all_evals) == 1.0 + PIONEER_PR_BOOST
