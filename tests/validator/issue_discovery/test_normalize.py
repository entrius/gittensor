from gittensor.classes import MinerEvaluation
from gittensor.validator.issue_discovery.normalize import normalize_issue_discovery_rewards


def test_normalize_scales_by_fetch_coverage():
    miner_a = MinerEvaluation(uid=1, hotkey='hk1', github_id='gh1')
    miner_b = MinerEvaluation(uid=2, hotkey='hk2', github_id='gh2')
    miner_a.issue_discovery_score = 10.0
    miner_b.issue_discovery_score = 30.0

    rewards = normalize_issue_discovery_rewards({1: miner_a, 2: miner_b}, reward_coverage=0.5)

    assert rewards == {1: 0.125, 2: 0.375}
    assert sum(rewards.values()) == 0.5


def test_normalize_defaults_to_full_coverage():
    miner = MinerEvaluation(uid=1, hotkey='hk1', github_id='gh1')
    miner.issue_discovery_score = 10.0

    rewards = normalize_issue_discovery_rewards({1: miner})

    assert rewards == {1: 1.0}
