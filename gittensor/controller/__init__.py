# The MIT License (MIT)
# Copyright © 2025 Entrius

"""The compute-pool controller: the one party that drives miner boxes (vault ``23`` §8).

Step 1 (``24`` §3) ships two pieces here. ``checks`` is the full hardware check the controller runs over SSH on an
idle box and at every lease boundary — scrape, judge, ADMIT or BENCH — plus the per-box ADMIT / IDLE / BENCHED
state with its bench backoff ladder. ``challenge`` is the GPU-proof bank: seeds precomputed on our own cards so a
box's answer is checked against a stored digest and wall time instead of a live reference. Run-spec execution and
the lease scheduler are WS-B / WS-D.
"""
