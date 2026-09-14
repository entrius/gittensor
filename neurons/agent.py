# The MIT License (MIT)
# Copyright © 2025 Entrius

# Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
# documentation files (the “Software”), to deal in the Software without restriction, including without limitation
# the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in all copies or substantial portions of
# the Software.

# THE SOFTWARE IS PROVIDED “AS IS”, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO
# THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL
# THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
# DEALINGS IN THE SOFTWARE.

"""Compute agent entrypoint: the process inside the ``gitt up`` container (docker/agent/entrypoint.sh starts it
next to sshd).

Not a neuron in the axon sense — no wallet, no chain, no metagraph. It reads its settings from the environment
the runner set (GT_AGENT_HTTP_PORT, GT_AGENT_SSH_PORT, GT_AGENT_MINER_HOTKEY, GT_AGENT_IMAGE,
GT_AGENT_IMAGE_DIGEST) and serves :mod:`gittensor.agent.app`. The controller hotkey it trusts is compiled in
(gittensor/agent/config.py), never read from the environment.

    GT_AGENT_HTTP_PORT=8200 GT_AGENT_SSH_PORT=2200 python neurons/agent.py
"""

import logging

from gittensor.agent.app import make_server
from gittensor.agent.config import CONTROLLER_HOTKEY_SS58, AgentSettings
from gittensor.agent.service import AgentService

logger = logging.getLogger('gt-agent')


def main() -> None:
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(name)s %(levelname)s %(message)s')
    settings = AgentSettings.from_env()
    server = make_server(AgentService(settings), settings.bind_host, settings.http_port)
    logger.info(
        'gt-agent listening on %s:%d (sshd :%d, controller %s, image %s%s)',
        settings.bind_host,
        settings.http_port,
        settings.ssh_port,
        CONTROLLER_HOTKEY_SS58,
        settings.image,
        f' @ {settings.image_digest}' if settings.image_digest else '',
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
