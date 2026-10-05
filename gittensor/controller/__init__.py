# The MIT License (MIT)
# Copyright © 2025 Entrius

"""The compute-pool controller: the one party that drives miner boxes (vault ``23`` §8).

Step 1 (``24`` §3) ships three pieces here. ``ssh`` is how the controller reaches a box: per-visit OpenSSH
certificates from the one CA key it holds (``26`` §5). ``checks`` is the full hardware check it runs over that
transport on an idle box and at every lease boundary — scrape, judge, ADMIT or BENCH — plus the per-box ADMIT /
IDLE / BENCHED state with its bench backoff ladder. ``proof`` is the slot the sealed, rotating GPU-proof binary
plugs into (its own private track, ``23`` §3a): the provider interface, the two-phase all-cards probe, and a
fail-closed default. Run-spec execution and the lease scheduler are WS-B / WS-D.
"""
