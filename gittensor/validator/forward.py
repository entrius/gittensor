# The MIT License (MIT)
# Copyright © 2025 Entrius

import asyncio
from typing import TYPE_CHECKING, Dict

import bittensor as bt

from gittensor.classes import MinerEvaluation
from gittensor.constants import ISSUES_TREASURY_EMISSION_SHARE, ISSUES_TREASURY_UID
from gittensor.validator.utils.load_weights import (
    load_master_repo_weights,
    load_programming_language_weights,
    load_token_config,
)

# ADD THIS for proper type hinting to navigate code easier.
if TYPE_CHECKING:
    from neurons.base.validator import BaseValidatorNeuron

# Issue bounties integration
from gittensor.utils.github_api_tools import check_github_issue_closed
from gittensor.utils.uids import get_all_uids
from gittensor.validator.evaluation.reward import get_rewards
from gittensor.validator.issue_competitions.contract_client import IssueCompetitionContractClient, IssueStatus
from gittensor.validator.utils.config import GITTENSOR_VALIDATOR_PAT, VALIDATOR_STEPS_INTERVAL, VALIDATOR_WAIT
from gittensor.validator.utils.issue_competitions import (
    get_contract_address,
    get_miner_coldkey,
)


async def forward(self: 'BaseValidatorNeuron') -> None:
    """Execute the validator's forward pass.

    Performs the core validation cycle every VALIDATOR_STEPS_INTERVAL steps:
    1. Get all available miner UIDs
    2. Score OSS contributions and get miner evaluations
    3. Update scores using exponential moving average
    4. Run issue bounties verification (needs tier data from scoring)

    Args:
        self: The validator instance containing all necessary state
    """

    if self.step % VALIDATOR_STEPS_INTERVAL == 0:
        miner_uids = get_all_uids(self)

        # Score OSS contributions - returns evaluations for issue verification
        miner_evaluations = await oss_contributions(self, miner_uids)

        # Issue bounties verification
        await issues_competition(self, miner_evaluations)

    await asyncio.sleep(VALIDATOR_WAIT)


async def oss_contributions(self: 'BaseValidatorNeuron', miner_uids: set[int]) -> Dict[int, MinerEvaluation]:
    """Score OSS contributions and return miner evaluations for downstream use."""
    master_repositories = load_master_repo_weights()
    programming_languages = load_programming_language_weights()
    token_config = load_token_config()

    tree_sitter_count = sum(1 for c in token_config.language_configs.values() if c.language is not None)

    bt.logging.info('***** Starting scoring round *****')
    bt.logging.info(f'Total Repositories loaded: {len(master_repositories)}')
    bt.logging.info(f'Total Languages loaded: {len(programming_languages)}')
    bt.logging.info(f'Token config: {tree_sitter_count} tree-sitter languages')
    bt.logging.info(f'Neurons to evaluate: {len(miner_uids)}')

    rewards, miner_evaluations = await get_rewards(
        self, miner_uids, master_repositories, programming_languages, token_config
    )

    # -------------------------------------------------------------------------
    # Issue Bounties Treasury Allocation
    # The smart contract neuron (ISSUES_TREASURY_UID) accumulates emissions
    # which fund issue bounty payouts. We allocate a fixed percentage of
    # total emissions to this treasury by scaling down all miner rewards
    # and assigning the remainder to the treasury UID.
    # -------------------------------------------------------------------------
    if ISSUES_TREASURY_UID > 0 and ISSUES_TREASURY_UID in miner_uids:
        treasury_share = ISSUES_TREASURY_EMISSION_SHARE
        miner_share = 1.0 - treasury_share

        # rewards array is indexed by position in sorted(miner_uids)
        sorted_uids = sorted(miner_uids)
        treasury_idx = sorted_uids.index(ISSUES_TREASURY_UID)

        # Scale down all rewards proportionally
        rewards *= miner_share

        # Assign treasury's share
        rewards[treasury_idx] = treasury_share

        bt.logging.info(
            f"Treasury allocation: Smart Contract UID {ISSUES_TREASURY_UID} receives "
            f"{treasury_share * 100:.0f}% of emissions, miners share {miner_share * 100:.0f}%"
        )

    self.update_scores(rewards, miner_uids)

    return miner_evaluations


async def issues_competition(
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
        contract_addr = get_contract_address()
        if not contract_addr:
            bt.logging.warning("Issue bounties: no contract address configured")
            return

        bt.logging.info('***** Starting Issue Bounties *****')
        bt.logging.info(f"Contract address: {contract_addr}")

        # Create contract client
        contract_client = IssueCompetitionContractClient(
            contract_address=contract_addr,
            subtensor=self.subtensor,
        )

        # Harvest emissions first - flush accumulated stake into bounty pool
        harvest_result = contract_client.harvest_emissions(self.wallet)
        if harvest_result and harvest_result.get('status') == 'success':
            bt.logging.success(f"Harvested emissions! Extrinsic: {harvest_result.get('tx_hash', '')}")

        # Build mapping of github_id->hotkey for bronze+ miners only (eligible for payouts)
        eligible_miners = {
            eval.github_id: eval.hotkey
            for eval in miner_evaluations.values()
            if eval.github_id and eval.github_id != '0' and eval.current_tier is not None
        }
        bt.logging.info(
            f"Issue bounties: {len(eligible_miners)} eligible miners (bronze+) out of {len(miner_evaluations)} total"
        )
        for github_id, hotkey in eligible_miners.items():
            bt.logging.info(f"  Eligible miner: github_id={github_id}, hotkey={hotkey[:12]}...")

        # Get active issues from contract
        active_issues = contract_client.get_issues_by_status(IssueStatus.ACTIVE)
        bt.logging.info(f"Found {len(active_issues)} active issues")

        votes_cast = 0
        cancels_cast = 0
        errors = []

        for issue in active_issues:
            issue_label = (
                f"{issue.repository_full_name}#{issue.issue_number} (id={issue.id}, bounty={issue.bounty_amount})"
            )
            try:
                bt.logging.info(f"--- Processing issue: {issue_label} ---")

                github_state = check_github_issue_closed(
                    issue.repository_full_name, issue.issue_number, GITTENSOR_VALIDATOR_PAT
                )

                if github_state is None:
                    bt.logging.warning(f"Could not check GitHub state for {issue_label}")
                    continue

                if not github_state.get('is_closed'):
                    bt.logging.info(f"Issue still open on GitHub: {issue_label}")
                    continue

                solver_github_id = github_state.get('solver_github_id')
                pr_number = github_state.get('pr_number')
                bt.logging.info(
                    f"Issue closed on GitHub: {issue_label} | solver_github_id={solver_github_id}, pr_number={pr_number}"
                )

                if not solver_github_id:
                    bt.logging.info(f"No identifiable solver, voting cancel: {issue_label}")
                    success = contract_client.vote_cancel_issue(
                        issue_id=issue.id,
                        reason="Issue closed without identifiable solver",
                        wallet=self.wallet,
                    )
                    if success:
                        cancels_cast += 1
                        bt.logging.info(f"Voted cancel (no solver): {issue_label}")
                    continue

                miner_hotkey = eligible_miners.get(str(solver_github_id))
                if not miner_hotkey:
                    bt.logging.info(f"Solver {solver_github_id} not in eligible miners, voting cancel: {issue_label}")
                    success = contract_client.vote_cancel_issue(
                        issue_id=issue.id,
                        reason=f"Issue closed externally (not by eligible miner, solver: {solver_github_id})",
                        wallet=self.wallet,
                    )
                    if success:
                        cancels_cast += 1
                        bt.logging.info(f"Voted cancel (solver {solver_github_id} not eligible): {issue_label}")
                    continue

                miner_coldkey = get_miner_coldkey(miner_hotkey, self.subtensor, self.config.netuid)
                if not miner_coldkey:
                    bt.logging.warning(
                        f"Could not get coldkey for hotkey {miner_hotkey} (solver {solver_github_id}): {issue_label}"
                    )
                    continue

                bt.logging.info(
                    f"Voting solution: {issue_label} | PR#{pr_number}, solver={solver_github_id}, hotkey={miner_hotkey[:12]}..."
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
                        f"Voted solution for {issue_label}: hotkey={miner_hotkey[:12]}..., PR#{pr_number}"
                    )
                else:
                    bt.logging.warning(f"Vote solution call failed: {issue_label}")
                    errors.append(f"Vote failed for {issue_label}")

            except Exception as e:
                bt.logging.error(f"Error processing {issue_label}: {e}")
                errors.append(f"{issue_label}: {str(e)}")

        if votes_cast > 0 or cancels_cast > 0:
            bt.logging.success(
                f"Issue bounties: processed {len(active_issues)} issues, "
                f"{votes_cast} solution votes, {cancels_cast} cancel votes"
            )
        elif active_issues:
            bt.logging.debug(f"Issue bounties: processed {len(active_issues)} issues (no state changes)")

        if errors:
            bt.logging.warning(f"Issue bounties errors: {errors[:3]}")

    except Exception as e:
        bt.logging.error(f"Issue bounties forward failed: {e}")
