# The MIT License (MIT)
# Copyright © 2025 Entrius

"""The GPU-proof slot (vault ``23`` §3a–b, ``24`` §3 WS-C).

The proof itself — our sealed, rotating binary — is its own track in a private repo. What lives here is the
*slot* it plugs into: the interface a provider implements (stage a challenge on the box, start it on every card at
one instant, judge the sealed result), the two-phase probe that drives it, and ``UnconfiguredProof``, which fails
closed so a controller with no provider admits nobody.
"""

from gittensor.controller.proof.slot import (
    BoxIdentity,
    GpuProof,
    ProbeResult,
    ProofUnavailable,
    ProofVerdict,
    StagedProof,
    UnconfiguredProof,
    create_command,
    fire_box,
    image_ref,
    probe_box,
    remove_command,
    stage_box,
    start_command,
)

__all__ = [
    'BoxIdentity',
    'GpuProof',
    'ProbeResult',
    'ProofUnavailable',
    'ProofVerdict',
    'StagedProof',
    'UnconfiguredProof',
    'create_command',
    'fire_box',
    'image_ref',
    'probe_box',
    'remove_command',
    'stage_box',
    'start_command',
]
