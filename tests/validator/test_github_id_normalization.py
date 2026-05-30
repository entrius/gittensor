from gittensor.classes import MinerEvaluation
from gittensor.validator.oss_contributions.inspections import detect_and_penalize_miners_sharing_github


def test_duplicate_penalty_normalizes_github_id_types():
    evaluations = {
        1: MinerEvaluation(uid=1, hotkey='hotkey_1', github_id='12345'),
        2: MinerEvaluation(uid=2, hotkey='hotkey_2', github_id=12345),
        3: MinerEvaluation(uid=3, hotkey='hotkey_3', github_id='67890'),
    }

    penalized_uids = detect_and_penalize_miners_sharing_github(evaluations)

    assert penalized_uids == {1, 2}
    assert evaluations[1].failed_reason is not None
    assert evaluations[2].failed_reason is not None
    assert evaluations[3].failed_reason is None
