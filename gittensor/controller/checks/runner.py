# The MIT License (MIT)
# Copyright © 2025 Entrius

"""The command runner the full check talks through.

The controller reaches a box as root over SSH with a per-visit certificate (``26`` §5); that transport is
``gittensor.controller.ssh.SshRunner``. The checks only need ``run(command) -> CommandResult``, so this module
defines the interface and a ``FakeRunner`` that answers from recorded output for tests. ``stdin`` carries bytes
to the command (``docker cp -`` takes a tar on stdin: that is how the proof binary reaches a box, ``23`` §3a).
"""

import re
from dataclasses import dataclass
from typing import Callable, List, Optional, Pattern, Protocol, Tuple, Union


@dataclass
class CommandResult:
    exit_code: int
    stdout: str = ''
    stderr: str = ''

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


class HostRunner(Protocol):
    """Run one shell command on the box; raise on transport failure, return a non-zero exit on command failure."""

    def run(self, command: str, timeout: Optional[float] = None, stdin: Optional[bytes] = None) -> CommandResult: ...


Response = Union[str, CommandResult, Exception, Callable[[str], Union[str, CommandResult]]]


class FakeRunner:
    """A ``HostRunner`` that replays canned responses. Rules match an exact command string or a regex; the most
    recently added rule wins, so a test can start from a passing box and override one command. A ``str`` response
    is that stdout with exit 0; an ``Exception`` is raised (a dead transport); a callable is given the command."""

    def __init__(self, responses: Optional[dict] = None):
        self._rules: List[Tuple[Union[str, Pattern[str]], Response]] = []
        self.calls: List[str] = []
        self.stdins: dict = {}  # command -> the bytes it was given
        for matcher, response in (responses or {}).items():
            self.on(matcher, response)

    def on(self, matcher: Union[str, Pattern[str]], response: Response) -> 'FakeRunner':
        self._rules.append((matcher, response))
        return self

    def run(self, command: str, timeout: Optional[float] = None, stdin: Optional[bytes] = None) -> CommandResult:
        self.calls.append(command)
        if stdin is not None:
            self.stdins[command] = stdin
        for matcher, response in reversed(self._rules):
            hit = matcher == command if isinstance(matcher, str) else matcher.search(command) is not None
            if hit:
                return self._render(response, command)
        return CommandResult(127, '', f'FakeRunner: no response for {command!r}')

    @staticmethod
    def _render(response: Response, command: str) -> CommandResult:
        if isinstance(response, Exception):
            raise response
        if callable(response):
            response = response(command)
        if isinstance(response, str):
            return CommandResult(0, response)
        return response


def regex(pattern: str) -> Pattern[str]:
    """Shorthand for a regex matcher in ``FakeRunner.on``."""
    return re.compile(pattern)
