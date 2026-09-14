# The MIT License (MIT)
# Copyright © 2025 Entrius

"""The compute agent: the one privileged container a miner runs with ``gitt up``.

Lium's executor shape, reduced (vault ``22`` §4, ``23`` §2, ``24`` §3 WS-A, ``26`` §5): an sshd that trusts one
SSH certificate authority compiled into the image, and nothing else. The controller mints a throwaway key and a
~5-minute certificate for every visit and does everything over SSH against the host docker daemon
(``/var/run/docker.sock`` is mounted). There is no agent HTTP port and no signed key-install route: nothing on the
box accepts a request, and the agent image has no Python in it.

What lives here is the part the CLI needs on the miner's host: the constants both sides agree on (``config``), the
``docker run`` lines (``launch``), and the signed release channel the runner follows (``channel``). Stdlib only.
"""
