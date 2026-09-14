# The MIT License (MIT)
# Copyright © 2025 Entrius

"""The compute agent: the one privileged container a miner runs with ``gitt up``.

Lium's executor shape, reduced (vault ``22`` §4, ``23`` §2, ``24`` §3 WS-A): sshd for root-by-key, one signed
write route (``POST /install_ssh_key``, plus its ``DELETE``) that only the controller hotkey compiled into
:mod:`gittensor.agent.config` can drive, and a read-only ``GET /info``. Everything else the controller does over
SSH against the host docker daemon (``/var/run/docker.sock`` is mounted). The agent updates itself through the
runner in ``docker/agent/``.

Import discipline: nothing under this package imports ``bittensor`` — the agent image ships only
``bittensor-wallet`` (sr25519 verify) and the standard library, so the modules here stay importable in that image
and cheap to import from the CLI.
"""
