# The MIT License (MIT)
# Copyright © 2025 Entrius
# GitTensor Miner

import typing
import bittensor as bt
import time
from neurons.base.miner import BaseMinerNeuron
from gittensor.classes import GitPatSynapse
from gittensor.miner import token_mgmt

class Miner(BaseMinerNeuron):

    def __init__(self, config=None):
        bt.logging.info("="*70)
        bt.logging.info("Initializing GitTensor Miner")
        bt.logging.info("="*70)
        
        super(Miner, self).__init__(config=config)
        
        bt.logging.info("Initializing token management...")
        token_mgmt.init()
        bt.logging.info("✓ Token management initialized")
        
        bt.logging.info(f"Miner configuration:")
        bt.logging.info(f"  - Hotkey: {self.wallet.hotkey.ss58_address}")
        bt.logging.info(f"  - Network: {self.config.subtensor.network}")
        bt.logging.info(f"  - Netuid: {self.config.netuid}")
        bt.logging.info(f"  - Dev mode: {self.config.dev_mode}")
        
        if self.config.dev_mode:
            bt.logging.warning("⚠ DEV MODE ENABLED - Blacklist disabled")
        
        bt.logging.info("="*70)
        bt.logging.info("✓ GitTensor Miner initialized successfully")
        bt.logging.info("="*70)

    async def forward(self, synapse: GitPatSynapse) -> GitPatSynapse:
        """
        Processes the incoming GitPatSynapse by loading GitHub access token.
        
        Args:
            synapse (GitPatSynapse): The synapse object representing the token request.
        
        Returns:
            GitPatSynapse: The same synapse object with the GitHub access token set.
        """
        import time
        
        request_start = time.time()
        requester_hotkey = synapse.dendrite.hotkey if synapse.dendrite else "unknown"
        
        bt.logging.info(f"→ Received token request from {requester_hotkey}")
        
        try:
            github_token = token_mgmt.load_token()
            if github_token:
                synapse.github_access_token = github_token
                request_time = time.time() - request_start
                bt.logging.info(f"✓ Token provided to {requester_hotkey} in {request_time:.3f}s")
            else:
                bt.logging.error(f"✗ Failed to load GitHub token for {requester_hotkey}")
        except Exception as e:
            bt.logging.error(f"✗ Error processing request from {requester_hotkey}: {e}")

        return synapse
    
    async def blacklist(
        self, synapse: GitPatSynapse
    ) -> typing.Tuple[bool, str]:
        """
        Determines whether an incoming request should be blacklisted.
        """
        
        requester_hotkey = synapse.dendrite.hotkey if synapse.dendrite else "unknown"
        bt.logging.debug(f"Checking blacklist for {requester_hotkey}")

        if self.config.dev_mode:
            bt.logging.debug("Dev mode: accepting all requests")
            return False, "Blacklist disabled in dev mode"

        # TODO: REPLACE WITH OUR OWNER HOTKEY
        if synapse.dendrite.hotkey == "5F9r26H1BoLteKh4QeHi2Vt1h5B2XQRgVaRa3WBDi3AdpkjV":  
            bt.logging.info(f"✓ Owner hotkey accepted: {requester_hotkey}")
            return False, "Owner hotkey accepted"

        if synapse.dendrite is None or synapse.dendrite.hotkey is None:
            bt.logging.warning("✗ Request rejected: Missing dendrite or hotkey")
            return True, "Missing dendrite or hotkey"

        try:
            uid = self.metagraph.hotkeys.index(synapse.dendrite.hotkey)
            bt.logging.debug(f"Found UID {uid} for hotkey {requester_hotkey}")
        except ValueError:
            if not self.config.blacklist.allow_non_registered:
                bt.logging.warning(f"✗ Blacklisting unregistered hotkey: {requester_hotkey}")
                return True, "Unrecognized hotkey"
            bt.logging.debug(f"Allowing unregistered hotkey: {requester_hotkey}")
            return False, "Unregistered but allowed"

        if self.config.blacklist.force_validator_permit:
            # If the config is set to force validator permit, then we should only allow requests from validators
            has_permit = self.metagraph.validator_permit[uid]
            stake = self.metagraph.S[uid]
            min_stake = self.config.blacklist.min_stake
            
            bt.logging.debug(f"Validator check for UID {uid}:")
            bt.logging.debug(f"  - Has permit: {has_permit}")
            bt.logging.debug(f"  - Stake: {stake:.2f} (min: {min_stake})")
            
            if not has_permit or stake < min_stake:
                bt.logging.warning(f"✗ Blacklisting non-validator: {requester_hotkey} (UID {uid})")
                bt.logging.warning(f"  - Permit: {has_permit}, Stake: {stake:.2f}/{min_stake}")
                return True, "Non-validator hotkey"
            
            bt.logging.info(f"✓ Validator accepted: {requester_hotkey} (UID {uid}, Stake: {stake:.2f})")

        bt.logging.debug(f"✓ Hotkey accepted: {requester_hotkey}")
        return False, "Hotkey recognized!"

    async def priority(self, synapse: GitPatSynapse) -> float:
        """
        Determines the processing priority for incoming token requests.
        This function is unchanged.
        """
        if synapse.dendrite is None or synapse.dendrite.hotkey is None:
            bt.logging.warning(
                "Received a request without a dendrite or hotkey."
            )
            return 0.0

        caller_uid = self.metagraph.hotkeys.index(
            synapse.dendrite.hotkey
        )  # Get the caller index.
        priority = float(
            self.metagraph.S[caller_uid]
        )  # Return the stake as the priority.
        bt.logging.trace(
            f"Prioritizing {synapse.dendrite.hotkey} with value: {priority}"
        )
        return priority

if __name__ == "__main__":
    with Miner() as miner:
        bt.logging.info("\n" + "="*70)
        bt.logging.info("GitTensor Miner Started")
        bt.logging.info("Repeating an action makes a habit.")
        bt.logging.info("Your habits create your character.")
        bt.logging.info("And your character is your destiny.")
        bt.logging.info("="*70 + "\n")
        
        iteration = 0
        while True:
            iteration += 1
            bt.logging.info(f"[Iteration {iteration}] GitTensor miner running... (Block: {miner.block})")
            time.sleep(30)
