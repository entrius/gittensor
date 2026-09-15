# The MIT License (MIT)
# Copyright © 2025 Entrius

"""How the controller reaches a miner box: OpenSSH certificates from one CA key it alone holds (vault ``26`` §5).

``certs`` mints a throwaway ed25519 key and a ~5-minute certificate per visit; ``runner`` is the ``HostRunner`` the
checks and the run-spec executor drive commands through, logging in as root with that certificate against the host
key pinned at ADMIT. Nothing here touches the chain and nothing on the box has to be updated for a visit: the
certificate expires on its own.
"""

from gittensor.controller.ssh.certs import CertificateAuthority, VisitCredential, cert_details
from gittensor.controller.ssh.runner import (
    SshRunner,
    SshTransportError,
    known_hosts_line,
    pinned_host_key,
    scan_host_key,
    write_host_key,
)

__all__ = [
    'CertificateAuthority',
    'SshRunner',
    'SshTransportError',
    'VisitCredential',
    'cert_details',
    'known_hosts_line',
    'pinned_host_key',
    'scan_host_key',
    'write_host_key',
]
