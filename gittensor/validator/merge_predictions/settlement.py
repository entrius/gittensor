# Entrius 2025

"""Settlement orchestrator for merge predictions.

On each forward pass, checks for settled issues (closed on GitHub with a
merged PR) and scores miners' predictions, updating their EMA.

Issues that are still open or closed without a merge are skipped — no
EMA impact.
"""

from collections import defaultdict
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Dict

import bittensor as bt

from gittensor.classes import MinerEvaluation
from gittensor.utils.github_api_tools import check_github_issue_closed, get_pr_open_times
from gittensor.validator.issue_competitions.contract_client import IssueCompetitionContractClient, IssueStatus
from gittensor.validator.merge_predictions.scoring import (
    MinerIssueScore,
    PrOutcome,
    PrPrediction,
    compute_merged_pr_order_ranks,
    score_miner_issue,
    update_ema,
)
from gittensor.validator.utils.config import GITTENSOR_VALIDATOR_PAT
from gittensor.validator.utils.issue_competitions import get_contract_address

if TYPE_CHECKING:
    from neurons.base.validator import BaseValidatorNeuron


async def merge_predictions(
    self: 'BaseValidatorNeuron',
    miner_evaluations: Dict[int, MinerEvaluation],
) -> None:
    """Settle merge predictions for closed issues and update miner EMAs.

    For each active issue in the smart contract that is now closed on
    GitHub with a merged PR:
      1. Load all predictions from storage
      2. Compute per-miner scores using the scoring module
      3. Update each miner's prediction EMA

    Args:
        self: The validator instance (has mp_storage, metagraph, subtensor, wallet)
        miner_evaluations: Fresh scoring data from oss_contributions(), keyed by UID
    """
    try:
        if not GITTENSOR_VALIDATOR_PAT:
            bt.logging.info('GITTENSOR_VALIDATOR_PAT not set, skipping merge predictions settlement.')
            return

        contract_addr = get_contract_address()
        if not contract_addr:
            bt.logging.warning('Merge predictions: no contract address configured')
            return

        bt.logging.info('***** Starting Merge Predictions Settlement *****')

        contract_client = IssueCompetitionContractClient(
            contract_address=contract_addr,
            subtensor=self.subtensor,
        )

        active_issues = contract_client.get_issues_by_status(IssueStatus.ACTIVE)
        bt.logging.info(f'Merge predictions: {len(active_issues)} active issues to check')

        settled_count = 0
        skipped_count = 0

        for issue in active_issues:
            issue_label = f'{issue.repository_full_name}#{issue.issue_number} (id={issue.id})'
            try:
                github_state = check_github_issue_closed(
                    issue.repository_full_name, issue.issue_number, GITTENSOR_VALIDATOR_PAT
                )

                if github_state is None:
                    bt.logging.debug(f'Merge predictions: could not check GitHub state for {issue_label}')
                    continue

                if not github_state.get('is_closed'):
                    # Issue still open — no settlement
                    continue

                merged_pr_number = github_state.get('pr_number')
                if not merged_pr_number:
                    # Closed without merge — voided, no EMA impact
                    bt.logging.debug(f'Merge predictions: {issue_label} closed without merged PR, skipping')
                    skipped_count += 1
                    continue

                # Load predictions for this issue
                predictions = self.mp_storage.get_predictions_for_issue(issue.id)
                if not predictions:
                    bt.logging.debug(f'Merge predictions: no predictions for {issue_label}, skipping')
                    skipped_count += 1
                    continue

                settlement_time = datetime.now(timezone.utc)

                # Peak variance time (fallback to settlement_time if only 1 prediction)
                peak_variance_time = self.mp_storage.get_peak_variance_time(issue.id)
                if peak_variance_time is None:
                    peak_variance_time = settlement_time

                # Collect unique PR numbers from predictions
                predicted_pr_numbers = list({p['pr_number'] for p in predictions})

                # Ensure merged PR is in the list
                if merged_pr_number not in predicted_pr_numbers:
                    predicted_pr_numbers.append(merged_pr_number)

                # Fetch PR open times from GitHub
                pr_open_times = get_pr_open_times(
                    issue.repository_full_name, predicted_pr_numbers, GITTENSOR_VALIDATOR_PAT
                )

                # Build PrOutcome list: merged PR = 1.0, all others = 0.0
                outcomes: list[PrOutcome] = []
                for pr_num in predicted_pr_numbers:
                    outcome_value = 1.0 if pr_num == merged_pr_number else 0.0
                    # Use GitHub PR open time, fall back to earliest prediction timestamp for that PR
                    pr_open_time = pr_open_times.get(pr_num)
                    if pr_open_time is None:
                        # Fallback: earliest prediction timestamp for this PR
                        pr_pred_times = [
                            datetime.fromisoformat(p['timestamp'])
                            for p in predictions
                            if p['pr_number'] == pr_num
                        ]
                        pr_open_time = min(pr_pred_times) if pr_pred_times else settlement_time

                    outcomes.append(PrOutcome(pr_number=pr_num, outcome=outcome_value, pr_open_time=pr_open_time))

                # Group predictions by miner UID -> build PrPrediction lists
                all_miners_predictions: dict[int, list[PrPrediction]] = defaultdict(list)
                for p in predictions:
                    uid = p['uid']
                    # Skip deregistered miners
                    if uid >= len(self.metagraph.hotkeys) or self.metagraph.hotkeys[uid] != p['hotkey']:
                        bt.logging.debug(
                            f'Merge predictions: skipping deregistered miner uid={uid} hotkey={p["hotkey"][:12]}...'
                        )
                        continue

                    all_miners_predictions[uid].append(
                        PrPrediction(
                            pr_number=p['pr_number'],
                            prediction=p['prediction'],
                            prediction_time=datetime.fromisoformat(p['timestamp']),
                            variance_at_prediction=p.get('variance_at_prediction', 0.0) or 0.0,
                        )
                    )

                if not all_miners_predictions:
                    bt.logging.debug(f'Merge predictions: no active miners had predictions for {issue_label}')
                    skipped_count += 1
                    continue

                # Compute order ranks (cross-miner ranking for merged PR)
                order_ranks = compute_merged_pr_order_ranks(all_miners_predictions, merged_pr_number)

                # Build uid -> github_id mapping from predictions
                uid_to_github_id: dict[int, str] = {}
                for p in predictions:
                    if p['uid'] in all_miners_predictions:
                        uid_to_github_id[p['uid']] = p['github_id']

                # Score each miner and update EMA (keyed by github_id)
                for uid, miner_preds in all_miners_predictions.items():
                    github_id = uid_to_github_id.get(uid)
                    if not github_id:
                        bt.logging.warning(f'Merge predictions: no github_id for uid={uid}, skipping EMA update')
                        continue

                    issue_score: MinerIssueScore = score_miner_issue(
                        uid=uid,
                        predictions=miner_preds,
                        outcomes=outcomes,
                        settlement_time=settlement_time,
                        peak_variance_time=peak_variance_time,
                        merged_pr_order_ranks=order_ranks,
                    )

                    previous_ema = self.mp_storage.get_ema(github_id)
                    new_ema = update_ema(issue_score.issue_score, previous_ema)
                    self.mp_storage.update_ema(github_id, new_ema)

                    bt.logging.info(
                        f'Merge predictions: {issue_label} uid={uid} github_id={github_id} '
                        f'issue_score={issue_score.issue_score:.4f} '
                        f'ema={previous_ema:.4f}->{new_ema:.4f} '
                        f'order_rank={order_ranks.get(uid, 0)}'
                    )

                settled_count += 1
                bt.logging.success(
                    f'Merge predictions: settled {issue_label} '
                    f'(merged PR#{merged_pr_number}, {len(all_miners_predictions)} miners scored)'
                )

            except Exception as e:
                bt.logging.error(f'Merge predictions: error processing {issue_label}: {e}')

        bt.logging.info(
            f'***** Merge Predictions Settlement Complete: '
            f'{settled_count} settled, {skipped_count} skipped *****'
        )

    except Exception as e:
        bt.logging.error(f'Merge predictions settlement failed: {e}')
