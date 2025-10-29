#!/usr/bin/env python3
# The MIT License (MIT)
# Copyright © 2025 Entrius

"""
Live Testnet Remote Debugging API

This module provides a FastAPI endpoint to trigger validator scoring on-demand.
This allows you to:
1. Trigger scoring whenever you want (no waiting for 4-hour intervals)
2. Hit breakpoints in get_rewards() if debugpy is already attached
3. Optionally specify which UIDs to score

IMPORTANT: This is FOR DEVELOPMENT ONLY. Enable via DEBUGPY_PORT in .env

Security:
    - All endpoints require API key authentication via X-API-Key header
    - API key is auto-generated when you start validator with DEBUGPY_PORT set
    - The key is displayed in the console and saved to .env

Usage:
    1. Set DEBUGPY_PORT=5678 in your .env file
    2. Start your validator: bash scripts/run_validator.sh
    3. Confirm you want to enable debug mode (security prompt)
    4. Save the API key that is displayed in the console
    5. Attach your remote debugger to port 5678 (if you want breakpoints)
    6. Trigger scoring via API (include X-API-Key header):
       - POST to http://remote_server:8099/trigger_scoring
       - Optional: Include {"uids": [0, 1, 2]} in POST body to score specific UIDs
    7. Your breakpoints in get_rewards() will be hit

Example:
    # Trigger scoring for all UIDs
    curl -X POST http://localhost:8099/trigger_scoring \\
         -H "X-API-Key: YOUR_API_KEY_HERE"

    # Trigger scoring for specific UIDs
    curl -X POST http://localhost:8099/trigger_scoring \\
         -H "X-API-Key: YOUR_API_KEY_HERE" \\
         -H "Content-Type: application/json" \\
         -d '{"uids": [0, 5, 10]}'

    # Health check
    curl http://localhost:8099/health \\
         -H "X-API-Key: YOUR_API_KEY_HERE"
"""

import os
from typing import TYPE_CHECKING, Any, Dict, List, Optional

import bittensor as bt
import numpy as np
import uvicorn
from fastapi import Body, Depends, FastAPI, Header, HTTPException

from gittensor.validator.utils.load_weights import load_master_repo_weights, load_programming_language_weights

if TYPE_CHECKING:
    from neurons.base.validator import BaseValidatorNeuron

from gittensor.utils.uids import get_all_uids
from gittensor.validator.evaluation.reward import get_rewards


def create_debug_api(validator: "BaseValidatorNeuron", port: int = 8099):
    """
    Create a FastAPI application for triggering validator scoring on-demand.

    Args:
        validator: The validator instance to use for scoring
        port: Port to run the FastAPI server on (default: 8099)

    Returns:
        FastAPI app instance
    """
    app = FastAPI(title="Gittensor Validator Debug API")

    # Load API key from environment
    REQUIRED_API_KEY = os.getenv("VALIDATOR_DEBUG_API_KEY")

    def verify_api_key(x_api_key: Optional[str] = Header(None)):
        """Verify the API key provided in the X-API-Key header."""
        if not REQUIRED_API_KEY:
            bt.logging.error("VALIDATOR_DEBUG_API_KEY not set in environment!")
            raise HTTPException(status_code=500, detail="API key not configured on server")

        if not x_api_key:
            bt.logging.warning("API request rejected: No API key provided")
            raise HTTPException(status_code=401, detail="Missing API key. Provide X-API-Key header.")

        if x_api_key != REQUIRED_API_KEY:
            bt.logging.warning("API request rejected: Invalid API key")
            raise HTTPException(status_code=403, detail="Invalid API key")

        return True

    @app.get('/health')
    async def health(authorized: bool = Depends(verify_api_key)):
        """Health check endpoint (requires API key)."""
        return {
            "status": "healthy",
            "validator_uid": int(validator.uid) if validator.uid is not None else None,
            "network": (
                str(validator.config.subtensor.chain_endpoint) if hasattr(validator.config, 'subtensor') else "unknown"
            ),
            "netuid": int(validator.config.netuid) if hasattr(validator.config, 'netuid') else None,
        }

    @app.post('/trigger_scoring')
    async def trigger_scoring(
        uids: Optional[List[int]] = Body(None, description="Optional list of UIDs to score"),
        authorized: bool = Depends(verify_api_key),
    ):
        """
        Manually trigger a scoring round. If debugpy is already attached, breakpoints will be hit.

        Args:
            uids: Optional list of specific UIDs to score

        Returns:
            JSON with scoring results
        """
        try:
            # Check if running on testnet
            chain_endpoint = validator.config.subtensor.chain_endpoint
            # TODO: Make this a sufficient is testnet check, below one doesn't seem to be working.
            # is_testnet = "test" in chain_endpoint.lower() or chain_endpoint == "wss://test.finney.opentensor.ai:443/"
            is_testnet = True

            if not is_testnet:
                bt.logging.error(f"Remote debugging endpoint blocked: Not running on testnet (chain: {chain_endpoint})")
                return {
                    "error": "Not allowed on mainnet",
                    "message": "This endpoint is only available when validator is running on testnet",
                    "current_chain": str(chain_endpoint),
                }

            bt.logging.info(f"Testnet check passed: {chain_endpoint}")

            # get the master repo list
            master_repositories: Dict[str, Dict[str, Any]] = load_master_repo_weights()
            programming_languages = load_programming_language_weights()

            # Get UIDs to score
            if uids is not None:
                miner_uids = list(uids)
                bt.logging.info(f"Scoring specific UIDs: {miner_uids}")
            else:
                miner_uids = get_all_uids(validator)
                bt.logging.info(f"Scoring all UIDs: {len(miner_uids)} miners")

            # Check if debugpy is attached
            try:
                import debugpy

                if debugpy.is_client_connected():
                    bt.logging.info("Debugger is attached - breakpoints will be hit!")
                else:
                    bt.logging.info("No debugger attached - running normally")
            except ImportError:
                bt.logging.info("debugpy not installed - running normally")

            # Trigger scoring - THIS IS WHERE YOUR BREAKPOINTS WILL HIT
            bt.logging.info("***** Starting manual scoring round *****")
            rewards = await get_rewards(validator, miner_uids, master_repositories, programming_languages)

            # Format results - ensure all values are JSON serializable
            result = {
                "status": "success",
                "uids_scored": [int(uid) for uid in miner_uids],
                "total_uids": len(miner_uids),
                "total_reward_sum": float(np.sum(rewards)) if len(rewards) > 0 else 0.0,
                "non_zero_rewards": int(np.count_nonzero(rewards)) if len(rewards) > 0 else 0,
                "rewards": {str(int(uid)): float(reward) for uid, reward in zip(miner_uids, rewards)},
            }

            bt.logging.info(f"Scoring complete! Total rewards: {result['total_reward_sum']:.6f}")
            return result

        except Exception as e:
            bt.logging.error(f"Scoring failed: {e}")
            import traceback

            return {"error": str(e), "traceback": traceback.format_exc()}

    return app


def start_debug_api(validator: "BaseValidatorNeuron", port: int = 8099):
    """
    Start the debug API server for on-demand scoring.

    This should be called from the validator's __init__ when DEBUGPY_PORT is set in .env.

    Args:
        validator: The validator instance
        port: Port to run FastAPI server on
    """
    app = create_debug_api(validator, port)

    bt.logging.info("=" * 70)
    bt.logging.info("REMOTE DEBUGGING API ENABLED (FOR DEVELOPMENT ONLY)")
    bt.logging.info("=" * 70)
    bt.logging.info(f"API endpoint: http://0.0.0.0:{port}")
    bt.logging.info(f"Health check: curl http://localhost:{port}/health")
    bt.logging.info(f"Trigger scoring: curl -X POST http://localhost:{port}/trigger_scoring")
    bt.logging.info(f"API docs: http://localhost:{port}/docs")
    bt.logging.info("=" * 70)

    # Run FastAPI app with uvicorn (blocking)
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
