# Entrius 2025

"""Settlement orchestrator for merge predictions.

Queries COMPLETED and CANCELLED issues from the smart contract and scores
miners' predictions, updating their EMA.

- COMPLETED issues: scored normally, predictions deleted after settlement.
- CANCELLED issues with a merged PR: scored (solver wasn't an eligible miner,
  but the PR was still merged — predictions are still valid).
- CANCELLED issues without a merged PR: voided — predictions deleted, no EMA impact.

The `settled_issues` table is the durable settled marker — once an issue is
recorded there, subsequent passes skip it regardless of prediction state.
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
    from neurons.validator import Validator


# =============================================================================
# Helper functions
# =============================================================================


def db_storage_void(validator: 'Validator', issue_id: int) -> None:
    """Best-effort mirror of a voided issue to Postgres."""
    if validator.db_storage:
        now = datetime.now(timezone.utc).isoformat()
        validator.db_storage.store_settled_issue(issue_id, 'voided', None, now)


def _build_outcomes(
    predictions: list[dict],
    merged_pr_number: int,
    repository: str,
    pr_open_times: dict[int, datetime],
    settlement_time: datetime,
) -> list[PrOutcome]:
    """Build PrOutcome list from raw prediction rows + merged PR number."""
    predicted_pr_numbers = list({p['pr_number'] for p in predictions})

    if merged_pr_number not in predicted_pr_numbers:
        predicted_pr_numbers.append(merged_pr_number)

    outcomes: list[PrOutcome] = []
    for pr_num in predicted_pr_numbers:
        outcome_value = 1.0 if pr_num == merged_pr_number else 0.0
        pr_open_time = pr_open_times.get(pr_num)
        if pr_open_time is None:
            pr_pred_times = [datetime.fromisoformat(p['timestamp']) for p in predictions if p['pr_number'] == pr_num]
            pr_open_time = min(pr_pred_times) if pr_pred_times else settlement_time

        outcomes.append(PrOutcome(pr_number=pr_num, outcome=outcome_value, pr_open_time=pr_open_time))

    return outcomes


def _group_miner_predictions(
    predictions: list[dict],
    metagraph,
) -> tuple[dict[int, list[PrPrediction]], dict[int, str]]:
    """Filter deregistered miners and group predictions by UID.

    Returns:
        (all_miners_predictions, uid_to_github_id)
    """
    all_miners_predictions: dict[int, list[PrPrediction]] = defaultdict(list)
    uid_to_github_id: dict[int, str] = {}

    for p in predictions:
        uid = p['uid']
        if uid >= len(metagraph.hotkeys) or metagraph.hotkeys[uid] != p['hotkey']:
            bt.logging.debug(f'Merge predictions: skipping deregistered miner uid={uid} hotkey={p["hotkey"][:12]}...')
            continue

        all_miners_predictions[uid].append(
            PrPrediction(
                pr_number=p['pr_number'],
                prediction=p['prediction'],
                prediction_time=datetime.fromisoformat(p['timestamp']),
                variance_at_prediction=p.get('variance_at_prediction', 0.0) or 0.0,
            )
        )
        uid_to_github_id[uid] = p['github_id']

    return dict(all_miners_predictions), uid_to_github_id


def _score_and_update_emas(
    validator: 'Validator',
    miners_preds: dict[int, list[PrPrediction]],
    uid_to_github_id: dict[int, str],
    outcomes: list[PrOutcome],
    settlement_time: datetime,
    peak_variance_time: datetime,
    order_ranks: dict[int, int],
) -> list[dict]:
    """Score each miner and update EMA. Returns list of result dicts for logging."""
    mp_storage = validator.mp_storage
    results = []

    for uid, miner_preds in miners_preds.items():
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

        previous_ema = mp_storage.get_ema(github_id)
        new_ema = update_ema(issue_score.issue_score, previous_ema)
        mp_storage.update_ema(github_id, new_ema)

        # Mirror EMA to Postgres
        if validator.db_storage:
            now = datetime.now(timezone.utc).isoformat()
            validator.db_storage.store_prediction_ema(github_id, new_ema, 1, now)

        results.append(
            {
                'uid': uid,
                'github_id': github_id,
                'score': issue_score.issue_score,
                'previous_ema': previous_ema,
                'new_ema': new_ema,
                'rank': order_ranks.get(uid, 0),
                'prs_predicted': len(miner_preds),
            }
        )

    return results


def _log_issue_settlement(
    issue_label: str,
    merged_pr_number: int,
    all_miners_predictions: dict[int, list[PrPrediction]],
    uid_to_github_id: dict[int, str],
    miner_results: list[dict],
) -> None:
    """Rich per-issue logging block."""
    # Submission summary
    total_submissions = sum(len(preds) for preds in all_miners_predictions.values())
    bt.logging.info(f'  {total_submissions} submissions from {len(all_miners_predictions)} miners:')

    for uid, preds in all_miners_predictions.items():
        gh_id = uid_to_github_id.get(uid, '?')
        merged_preds = [p for p in preds if p.pr_number == merged_pr_number]
        avg_on_merged = sum(p.prediction for p in merged_preds) / len(merged_preds) if merged_preds else 0.0
        bt.logging.info(
            f'    UID: {uid}  (gh: {gh_id})   PRs predicted: {len(preds)}   '
            f'avg on merged PR #{merged_pr_number}: {avg_on_merged:.2f}'
        )

    # Scoring results
    if miner_results:
        sorted_results = sorted(miner_results, key=lambda r: r['score'], reverse=True)
        bt.logging.info('  Scoring results:')
        for r in sorted_results:
            rank_str = f'rank #{r["rank"]}' if r['rank'] > 0 else 'unranked'
            marker = '\u2605' if r == sorted_results[0] else ' '
            bt.logging.info(
                f'    {marker} UID: {r["uid"]}  score: {r["score"]:.4f}  '
                f'ema: {r["previous_ema"]:.4f} \u2192 {r["new_ema"]:.4f}  ({rank_str})'
            )


def _settle_issue(
    validator: 'Validator',
    issue,
    issue_label: str,
    merged_pr_number: int,
    settlement_reason: str = 'completed',
) -> bool:
    """Full settlement pipeline for one issue.

    Loads predictions, builds outcomes, scores, updates EMAs, logs, deletes.
    Shared by both COMPLETED and CANCELLED-with-merge paths.

    Returns True if settled successfully.
    """
    mp_storage = validator.mp_storage

    predictions = mp_storage.get_predictions_for_issue(issue.id)
    if not predictions:
        return False

    unique_prs = {p['pr_number'] for p in predictions}
    bt.logging.info(
        f'--- Settling {settlement_reason} issue ID: {issue.id}, '
        f'{issue.repository_full_name}#{issue.issue_number}, '
        f'{len(unique_prs)} PRs submitted (merged PR #{merged_pr_number}) ---'
    )

    settlement_time = datetime.now(timezone.utc)

    peak_variance_time = mp_storage.get_peak_variance_time(issue.id)
    if peak_variance_time is None:
        peak_variance_time = settlement_time

    # Collect unique PR numbers for open-time lookup
    predicted_pr_numbers = list({p['pr_number'] for p in predictions})
    if merged_pr_number not in predicted_pr_numbers:
        predicted_pr_numbers.append(merged_pr_number)

    pr_open_times = get_pr_open_times(issue.repository_full_name, predicted_pr_numbers, GITTENSOR_VALIDATOR_PAT)

    outcomes = _build_outcomes(
        predictions, merged_pr_number, issue.repository_full_name, pr_open_times, settlement_time
    )
    all_miners_predictions, uid_to_github_id = _group_miner_predictions(predictions, validator.metagraph)

    if not all_miners_predictions:
        bt.logging.debug(f'Merge predictions: no active miners had predictions for {issue_label}')
        rows_deleted = mp_storage.delete_predictions_for_issue(issue.id)
        bt.logging.info(f'  Predictions deleted ({rows_deleted} rows)')
        return False

    order_ranks = compute_merged_pr_order_ranks(all_miners_predictions, merged_pr_number)

    miner_results = _score_and_update_emas(
        validator,
        all_miners_predictions,
        uid_to_github_id,
        outcomes,
        settlement_time,
        peak_variance_time,
        order_ranks,
    )

    _log_issue_settlement(issue_label, merged_pr_number, all_miners_predictions, uid_to_github_id, miner_results)

    rows_deleted = mp_storage.delete_predictions_for_issue(issue.id)
    bt.logging.info(f'  Predictions deleted ({rows_deleted} rows)')

    mp_storage.mark_issue_settled(issue.id, 'scored', merged_pr_number)

    # Mirror settlement + delete to Postgres
    if validator.db_storage:
        now = datetime.now(timezone.utc).isoformat()
        validator.db_storage.store_settled_issue(issue.id, 'scored', merged_pr_number, now)

    return True


# =============================================================================
# Main settlement function
# =============================================================================


async def merge_predictions(
    self: 'Validator',
    miner_evaluations: Dict[int, MinerEvaluation],
) -> None:
    """Settle merge predictions for COMPLETED and CANCELLED issues.

    1. Query COMPLETED issues from contract
       - Skip if already in settled_issues table
       - check_github_issue_closed to get merged PR number
       - Score miners, update EMAs, delete predictions, record in settled_issues

    2. Query CANCELLED issues from contract
       - Skip if already in settled_issues table
       - check_github_issue_closed to determine WHY it was cancelled:
         a) Merged PR exists -> score + delete + record as 'scored'
         b) No merged PR -> void: delete predictions + record as 'voided', no EMA impact
    """
    try:
        if not GITTENSOR_VALIDATOR_PAT:
            bt.logging.warning(
                'GITTENSOR_VALIDATOR_PAT not set, skipping merge predictions settlement. (This DOES affect vstrust/consensus)'
            )
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

        completed_settled = 0
        cancelled_settled = 0
        voided = 0
        skipped = 0

        # --- COMPLETED issues ---
        completed_issues = contract_client.get_issues_by_status(IssueStatus.COMPLETED)
        bt.logging.info(f'Merge predictions: {len(completed_issues)} completed issues to check')

        for issue in completed_issues:
            issue_label = f'{issue.repository_full_name}#{issue.issue_number} (id={issue.id})'
            try:
                if self.mp_storage.is_issue_settled(issue.id):
                    skipped += 1
                    continue

                github_state = check_github_issue_closed(
                    issue.repository_full_name, issue.issue_number, GITTENSOR_VALIDATOR_PAT
                )

                if github_state is None:
                    bt.logging.debug(f'Merge predictions: could not check GitHub state for {issue_label}')
                    continue

                merged_pr_number = github_state.get('pr_number')
                if not merged_pr_number:
                    bt.logging.warning(
                        f'Merge predictions: completed issue {issue_label} has no merged PR on GitHub, voiding'
                    )
                    rows_deleted = self.mp_storage.delete_predictions_for_issue(issue.id)
                    bt.logging.info(
                        f'  Voiding completed issue {issue_label} — no merged PR found, '
                        f'{rows_deleted} predictions deleted, no EMA impact'
                    )
                    self.mp_storage.mark_issue_settled(issue.id, 'voided')
                    db_storage_void(self, issue.id)
                    voided += 1
                    continue

                if _settle_issue(self, issue, issue_label, merged_pr_number):
                    completed_settled += 1
                else:
                    skipped += 1

            except Exception as e:
                bt.logging.error(f'Merge predictions: error processing completed {issue_label}: {e}')

        # --- CANCELLED issues ---
        cancelled_issues = contract_client.get_issues_by_status(IssueStatus.CANCELLED)
        bt.logging.info(f'Merge predictions: {len(cancelled_issues)} cancelled issues to check')

        for issue in cancelled_issues:
            issue_label = f'{issue.repository_full_name}#{issue.issue_number} (id={issue.id})'
            try:
                if self.mp_storage.is_issue_settled(issue.id):
                    skipped += 1
                    continue

                github_state = check_github_issue_closed(
                    issue.repository_full_name, issue.issue_number, GITTENSOR_VALIDATOR_PAT
                )

                if github_state is None:
                    bt.logging.debug(f'Merge predictions: could not check GitHub state for {issue_label}')
                    continue

                merged_pr_number = github_state.get('pr_number')

                if merged_pr_number:
                    # Cancelled but PR was merged (solver not in subnet) — still score
                    if _settle_issue(self, issue, issue_label, merged_pr_number, settlement_reason='cancelled'):
                        cancelled_settled += 1
                    else:
                        skipped += 1
                else:
                    # No merged PR — void: delete predictions, no EMA impact
                    rows_deleted = self.mp_storage.delete_predictions_for_issue(issue.id)
                    bt.logging.info(
                        f'  Voiding cancelled issue ID {issue.id}, {issue.repository_full_name}'
                        f'#{issue.issue_number} — closed without merge, '
                        f'{rows_deleted} predictions deleted, no EMA impact'
                    )
                    self.mp_storage.mark_issue_settled(issue.id, 'voided')
                    db_storage_void(self, issue.id)
                    voided += 1

            except Exception as e:
                bt.logging.error(f'Merge predictions: error processing cancelled {issue_label}: {e}')

        bt.logging.info(
            f'***** Merge Predictions Settlement Complete: '
            f'{completed_settled} completed settled, {cancelled_settled} cancelled settled, '
            f'{voided} voided, {skipped} skipped *****'
        )

    except Exception as e:
        bt.logging.error(f'Merge predictions settlement failed: {e}')
