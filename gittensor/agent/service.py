# The MIT License (MIT)
# Copyright © 2025 Entrius

"""The agent's behaviour behind its three routes, independent of the HTTP transport.

Each method returns ``(status, body)`` so :mod:`gittensor.agent.app` stays a thin adapter and tests can drive the
service directly.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from typing import Any

from gittensor.agent import authorized_keys
from gittensor.agent.auth import AuthError, SignedKeyRequest, Verifier
from gittensor.agent.config import ACTION_INSTALL, ACTION_REMOVE, AGENT_VERSION, KEY_COMMENT_TAG, AgentSettings
from gittensor.agent.gpu import GpuInventory, gpu_inventory

logger = logging.getLogger('gt-agent')


class AgentService:
    def __init__(
        self,
        settings: AgentSettings,
        verifier: Verifier | None = None,
        inventory: Callable[[], GpuInventory] = gpu_inventory,
        clock: Callable[[], float] = time.time,
    ):
        self.settings = settings
        self.verifier = verifier if verifier is not None else Verifier(clock=clock)
        self.inventory = inventory
        self.clock = clock

    # -- routes -------------------------------------------------------------------------------------------------

    def info(self) -> tuple[int, dict[str, Any]]:
        """``GET /info``: self-report. Nothing here is trusted by the controller; it is for operators and mapping."""
        inv = self.inventory()
        return 200, {
            'agent_version': AGENT_VERSION,
            'image': self.settings.image,
            'image_digest': self.settings.image_digest,
            'controller_hotkey': self.verifier.controller_hotkey,
            'miner_hotkey': self.settings.miner_hotkey,
            'sshd_port': self.settings.ssh_port,
            'ssh_user': 'root',
            'http_port': self.settings.http_port,
            'installed_keys': len(authorized_keys.tagged_keys(self.settings.authorized_keys_path)),
            **inv.as_dict(),
            'ts': self.clock(),
        }

    def install_ssh_key(self, payload: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        """``POST /install_ssh_key``: append the controller's per-operation pubkey to root's authorized_keys."""
        try:
            request = self._authorize(ACTION_INSTALL, payload)
            added = authorized_keys.install_key(self.settings.authorized_keys_path, request.pubkey)
        except AuthError as e:
            return self._refuse(ACTION_INSTALL, e)
        logger.info('install_ssh_key: %s (nonce %s)', 'added' if added else 'already present', request.nonce)
        return 200, {
            'installed': added,
            'key': authorized_keys.normalize_pubkey(request.pubkey),
            'tag': KEY_COMMENT_TAG,
            'ssh_user': 'root',
            'ssh_port': self.settings.ssh_port,
        }

    def remove_ssh_key(self, payload: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        """``DELETE /install_ssh_key``: pull the pubkey back out once the operation is over."""
        try:
            request = self._authorize(ACTION_REMOVE, payload)
            removed = authorized_keys.remove_key(self.settings.authorized_keys_path, request.pubkey)
        except AuthError as e:
            return self._refuse(ACTION_REMOVE, e)
        logger.info('remove_ssh_key: %s (nonce %s)', 'removed' if removed else 'not present', request.nonce)
        return 200, {'removed': removed, 'key': authorized_keys.normalize_pubkey(request.pubkey)}

    # -- internals ----------------------------------------------------------------------------------------------

    def _authorize(self, action: str, payload: Mapping[str, Any]) -> SignedKeyRequest:
        request = SignedKeyRequest.from_payload(action, payload)
        self.verifier.verify(request)
        return request

    @staticmethod
    def _refuse(action: str, error: AuthError) -> tuple[int, dict[str, Any]]:
        logger.warning('%s refused (%d): %s', action, error.status, error)
        return error.status, {'error': str(error)}
