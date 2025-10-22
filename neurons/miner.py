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
        super(Miner, self).__init__(config=config)
        token_mgmt.init()

        if self.config.dev_mode:
            bt.logging.info("DEV MODE ENABLED")

    async def forward(self, synapse: GitPatSynapse) -> GitPatSynapse:
        """
        Processes the incoming GitPatSynapse by loading GitHub access token.
        
        Args:
            synapse (GitPatSynapse): The synapse object representing the token request.
        
        Returns:
            GitPatSynapse: The same synapse object with the GitHub access token set.
        """

        github_token = token_mgmt.load_token()
        synapse.github_access_token = github_token
        bt.logging.debug(f"synapse received from hotkey: {synapse.axon.hotkey}")

        return synapse
    
    async def blacklist(
        self, synapse: GitPatSynapse
    ) -> typing.Tuple[bool, str]:
        """
        Determines whether an incoming request should be blacklisted.
        """

        if self.config.dev_mode:
            return False, "Blacklist disabled in dev mode"

        # TODO: REPLACE WITH OUR OWNER HOTKEY
        if synapse.dendrite.hotkey == "5F9r26H1BoLteKh4QeHi2Vt1h5B2XQRgVaRa3WBDi3AdpkjV":  
            return False, "Owner hotkey accepted"

        bt.logging.info(f"Received synapse from {synapse.dendrite.hotkey}")
        if synapse.dendrite is None or synapse.dendrite.hotkey is None:
            bt.logging.warning("Received a request without a dendrite or hotkey.")
            return True, "Missing dendrite or hotkey"

        uid = self.metagraph.hotkeys.index(synapse.dendrite.hotkey)
        if (
            not self.config.blacklist.allow_non_registered
            and synapse.dendrite.hotkey not in self.metagraph.hotkeys
        ):
            # Ignore requests from un-registered entities.
            bt.logging.trace(
                f"Blacklisting un-registered hotkey {synapse.dendrite.hotkey}"
            )
            return True, "Unrecognized hotkey"

        if self.config.blacklist.force_validator_permit:
            # If the config is set to force validator permit, then we should only allow requests from validators.
            bt.logging.debug(f"Validator permit: {self.metagraph.validator_permit[uid]}, Stake: {self.metagraph.S[uid]}")
            if not self.metagraph.validator_permit[uid] or self.metagraph.S[uid] < self.config.blacklist.min_stake:
                bt.logging.warning(
                    f"Blacklisting a request from non-validator hotkey {synapse.dendrite.hotkey}"
                )
                return True, "Non-validator hotkey"

        bt.logging.trace(
            f"Not Blacklisting recognized hotkey {synapse.dendrite.hotkey}"
        )
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
        bt.logging.info("Repeating an action makes a habit. Your habits create your character. And your character is your destiny.")
        while True:
            bt.logging.info(f"GitTensor miner running...")
            time.sleep(30)
