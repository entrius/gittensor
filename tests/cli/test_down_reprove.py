"""`gitt down` waits for the controller's post-lease re-proof before removing the agent (seen 10/9: removing the agent
under the re-proof cost the box a strike and left the staged proof container behind)."""

from gittensor.cli.up_commands import down


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def test_no_proof_container_ever_means_one_quiet_period():
    clock = Clock()
    assert down.wait_for_reprove(quiet_s=45, cap_s=180, poll_s=5, clock=clock, sleep=clock.sleep, containers=list)
    assert clock.t == 45.0


def test_a_reprove_in_flight_is_waited_out_then_quiet():
    clock = Clock()
    seen = lambda: ['c' * 12] if 10 <= clock.t <= 30 else []  # noqa: E731  the controller stages at 10 s, done by 30 s
    assert down.wait_for_reprove(quiet_s=45, cap_s=180, poll_s=5, clock=clock, sleep=clock.sleep, containers=seen)
    assert clock.t == 75.0  # 45 s after the last sighting at 30 s


def test_a_stale_proof_container_hits_the_cap():
    clock = Clock()
    assert not down.wait_for_reprove(
        quiet_s=45, cap_s=180, poll_s=5, clock=clock, sleep=clock.sleep, containers=lambda: ['x']
    )
    assert clock.t == 180.0
