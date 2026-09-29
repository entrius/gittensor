# The MIT License (MIT)
# Copyright © 2026 Entrius

"""The optimization challenges' evaluator: miners run a challenger solver head to head against the king (the current
crown) on seeded instances, inside an attested VM; the result is canonical JSON a maintainer only verifies. Each
challenge's package is imported by module name, never a dependency.

* ``runner``: generate -> run ``solve`` in a bubblewrap sandbox under the tier's limits -> check, per seed and solver.
* ``head_to_head``: paired per-seed scores, the bootstrap bound on the gain, the crown rule, the canonical report.
* ``attestation``: ``attestation.json``, the signed result a miner submits (``dev`` key now, Polaris later).
* ``cli``: ``gitt challenge eval``.
"""
