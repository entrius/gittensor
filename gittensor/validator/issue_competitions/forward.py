# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Issue bounties forward pass — harvest, verify, and vote on active issues."""

from typing import TYPE_CHECKING, Dict

import bittensor as bt

from gittensor.classes import MinerEvaluation
from gittensor.utils.github_api_tools import check_github_issue_closed
from gittensor.validator.issue_competitions.contract_client import IssueCompetitionContractClient, IssueStatus
from gittensor.validator.utils.config import GITTENSOR_VALIDATOR_PAT
from gittensor.validator.utils.issue_competitions import (
    get_contract_address,
    get_miner_coldkey,
)

if TYPE_CHECKING:
    from neurons.base.validator import BaseValidatorNeuron


async def issue_competitions(
    self: 'BaseValidatorNeuron',
    miner_evaluations: Dict[int, MinerEvaluation],
) -> None:
    """
    Run the issue bounties forward pass.

    1. Harvest emissions into the bounty pool
    2. Get active issues from the smart contract
    3. For each active issue, check GitHub:
       - If solved by bronze+ miner -> vote_solution
       - If closed but not by eligible miner -> vote_cancel_issue

    Args:
        self: The validator instance
        miner_evaluations: Fresh scoring data from oss_contributions(), keyed by UID
    """
    try:
        if not GITTENSOR_VALIDATOR_PAT:
            bt.logging.info('GITTENSOR_VALIDATOR_PAT not set, skipping issue bounties voting entirely.')
            return

        contract_addr = get_contract_address()
        if not contract_addr:
            bt.logging.warning('Issue bounties: no contract address configured')
            return

        bt.logging.info('***** Starting Issue Bounties *****')
        bt.logging.info(f'Contract address: {contract_addr}')

        # Create contract client
        contract_client = IssueCompetitionContractClient(
            contract_address=contract_addr,
            subtensor=self.subtensor,
        )

        # Harvest emissions first - flush accumulated stake into bounty pool
        harvest_result = contract_client.harvest_emissions(self.wallet)
        if harvest_result and harvest_result.get('status') == 'success':
            bt.logging.success(f'Harvested emissions! Extrinsic: {harvest_result.get("tx_hash", "")}')

        # Build mapping of github_id->hotkey for bronze+ miners only (eligible for payouts)
        eligible_miners = {
            eval.github_id: eval.hotkey
            for eval in miner_evaluations.values()
            if eval.github_id and eval.github_id != '0' and eval.current_tier is not None
        }
        bt.logging.info(
            f'Issue bounties: {len(eligible_miners)} eligible miners (bronze+) out of {len(miner_evaluations)} total'
        )
        for github_id, hotkey in eligible_miners.items():
            bt.logging.info(f'  Eligible miner: github_id={github_id}, hotkey={hotkey[:12]}...')

        # Get active issues from contract
        active_issues = contract_client.get_issues_by_status(IssueStatus.ACTIVE)
        bt.logging.info(f'Found {len(active_issues)} active issues')

        votes_cast = 0
        cancels_cast = 0
        errors = []

        for issue in active_issues:
            bounty_display = issue.bounty_amount / 1e9
            issue_label = (
                f'{issue.repository_full_name}#{issue.issue_number} (id={issue.id}, bounty={bounty_display:.2f} ALPHA)'
            )
            try:
                bt.logging.info(f'--- Processing issue: {issue_label} ---')

                github_state = check_github_issue_closed(
                    issue.repository_full_name, issue.issue_number, GITTENSOR_VALIDATOR_PAT
                )

                if github_state is None:
                    bt.logging.warning(f'Could not check GitHub state for {issue_label}')
                    continue

                if not github_state.get('is_closed'):
                    bt.logging.info(f'Issue still open on GitHub: {issue_label}')
                    continue

                solver_github_id = github_state.get('solver_github_id')
                pr_number = github_state.get('pr_number')
                bt.logging.info(
                    f'Issue closed on GitHub: {issue_label} | solver_github_id={solver_github_id}, pr_number={pr_number}'
                )

                if not solver_github_id:
                    bt.logging.info(f'No identifiable solver, voting cancel: {issue_label}')
                    success = contract_client.vote_cancel_issue(
                        issue_id=issue.id,
                        reason='Issue closed without identifiable solver',
                        wallet=self.wallet,
                    )
                    if success:
                        cancels_cast += 1
                        bt.logging.info(f'Voted cancel (no solver): {issue_label}')
                    continue

                miner_hotkey = eligible_miners.get(str(solver_github_id))
                if not miner_hotkey:
                    bt.logging.info(f'Solver {solver_github_id} not in eligible miners, voting cancel: {issue_label}')
                    success = contract_client.vote_cancel_issue(
                        issue_id=issue.id,
                        reason=f'Issue closed externally (not by eligible miner, solver: {solver_github_id})',
                        wallet=self.wallet,
                    )
                    if success:
                        cancels_cast += 1
                        bt.logging.info(f'Voted cancel (solver {solver_github_id} not eligible): {issue_label}')
                    continue

                miner_coldkey = get_miner_coldkey(miner_hotkey, self.subtensor, self.config.netuid)
                if not miner_coldkey:
                    bt.logging.warning(
                        f'Could not get coldkey for hotkey {miner_hotkey} (solver {solver_github_id}): {issue_label}'
                    )
                    continue

                bt.logging.info(
                    f'Voting solution: {issue_label} | PR#{pr_number}, solver={solver_github_id}, hotkey={miner_hotkey[:12]}...'
                )
                success = contract_client.vote_solution(
                    issue_id=issue.id,
                    solver_hotkey=miner_hotkey,
                    solver_coldkey=miner_coldkey,
                    pr_number=pr_number or 0,
                    wallet=self.wallet,
                )
                if success:
                    votes_cast += 1
                    bt.logging.success(
                        f'Voted solution for {issue_label}: hotkey={miner_hotkey[:12]}..., PR#{pr_number}'
                    )
                else:
                    bt.logging.warning(f'Vote solution call failed: {issue_label}')
                    errors.append(f'Vote failed for {issue_label}')

            except Exception as e:
                bt.logging.error(f'Error processing {issue_label}: {e}')
                errors.append(f'{issue_label}: {str(e)}')

        if errors:
            bt.logging.warning(f'Issue bounties errors: {errors[:3]}')

        if votes_cast > 0 or cancels_cast > 0:
            bt.logging.success(
                f'=== Issue Bounties Complete: processed {len(active_issues)} issues, '
                f'{votes_cast} solution votes, {cancels_cast} cancel votes ==='
            )
        else:
            bt.logging.info(
                '***** Issue Bounties Complete: processed {len(active_issues)} issues (no state changes) *****'
            )

    except Exception as e:
        bt.logging.error(f'Issue bounties forward failed: {e}')
