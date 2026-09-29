# The MIT License (MIT)
# Copyright © 2026 Entrius

"""The optimization-challenge pool's evaluator (v0, default-off): miners submit solvers, the evaluator runs them on
fresh seeded instances, and the current best per challenge (the king) is paid.

* ``registry``: ``challenges.json``, and each challenge's package imported by module name (never a dependency).
* ``runner``: generate -> run ``solve`` in a bubblewrap sandbox under the tier's limits -> check, per seed; mean score.
* ``leaderboard``: king of the hill per challenge, walked in commit order with a margin to dethrone; persisted JSON.
* ``scorecard``: the signed document the validator reads (the compute scorecard's mechanics, its own schema).
* ``evaluator``: one round, candidates to board to attested scorecard (``gitt challenge round``).
* ``attest``: ``dev`` (a local ed25519 key) or ``polaris`` (not wired yet).
* ``submission``: the deterministic bundle, its sha256, the chain commitment, and the Hippius upload.
"""
