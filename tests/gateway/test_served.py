# The MIT License (MIT)
# Copyright © 2025 Entrius

"""The gateway's per-instance totals for the lease accounting check: completion tokens from the runtime's ``usage``;
every request whose count was not learned (no usage, a client that left mid-stream, a runtime error, a dead instance,
an http passthrough) counted as unaccounted at its ``max_tokens`` or the ceiling; ``started_at`` and ``served`` in
``/healthz`` beside every field it had; ``gt_gateway_completion_tokens_total`` in ``/metrics``; the decode rate of
requests that ran alone; and nothing of it changes what the client receives."""

import asyncio

from gittensor.controller.checks import config as cfg
from gittensor.gateway.app import SERVED_KEEP_S
from tests.gateway.conftest import AUTH, FakeRuntime, dead_port, gateway, manifest_doc, post, runtimes, until
from tests.gateway.test_gateway import TOOL_BODY, healthz, metrics

NO_CAP = {k: v for k, v in TOOL_BODY.items() if k != 'max_tokens'}


def served(health: dict, instance: str = 'i-a') -> dict:
    return health['served'][instance]


def test_usage_counts_its_completion_tokens_and_healthz_keeps_every_field(world):
    async def scenario():
        entry = world.bless()
        async with runtimes(FakeRuntime()) as (rt,):
            world.place('i-a', entry, rt.port)
            async with gateway(world) as (gw, client):
                assert (await post(client, TOOL_BODY))[0] == 200
                async with client.post('/v1/chat/completions', json={**TOOL_BODY, 'stream': True}, headers=AUTH) as r:
                    relayed = await r.read()
                health = await healthz(client)
                text = await metrics(client)
        assert relayed == b''.join(rt.chunks())  # the answer relayed as the runtime sent it
        assert {'status', 'routable', 'instances', 'in_flight', 'refreshed_at', 'error', 'tunnels'} <= set(health)
        assert health['started_at'] == gw.started_at
        row = served(health)
        assert (row['requests'], row['completion_tokens']) == (2, 18)
        assert (row['unaccounted_requests'], row['unaccounted_allowance_tokens']) == (0, 0)
        assert f'gt_gateway_completion_tokens_total{{entry="{entry}",instance="i-a"}} 18' in text

    asyncio.run(scenario())


def test_without_usage_a_request_counts_its_max_tokens_or_the_ceiling(world):
    async def scenario():
        plain = world.bless()
        raised = world.bless({**manifest_doc(name='gt-long'), 'profile': {'max_output_tokens': 32768}})
        async with runtimes(FakeRuntime(usage=False), FakeRuntime('fake-org/long', usage=False)) as (rt, rt_long):
            world.place('i-a', plain, rt.port)
            world.place('i-b', raised, rt_long.port)
            async with gateway(world) as (_, client):
                assert (await post(client, TOOL_BODY))[0] == 200  # max_tokens 256
                assert (await post(client, {**NO_CAP, 'max_completion_tokens': 900}))[0] == 200
                assert (await post(client, NO_CAP))[0] == 200  # names none: the runtime's ceiling
                async with client.post('/v1/chat/completions', json={**TOOL_BODY, 'stream': True}, headers=AUTH) as r:
                    assert r.status == 200 and await r.read()  # a stream without include_usage
                assert (await post(client, {**NO_CAP, 'model': 'gt-long'}))[0] == 200
                health = await healthz(client)
        row = served(health)
        assert (row['requests'], row['completion_tokens'], row['unaccounted_requests']) == (4, 0, 4)
        assert row['unaccounted_allowance_tokens'] == 256 + 900 + cfg.RUNTIME_OUTPUT_CEILING_TOKENS + 256
        assert served(health, 'i-b')['unaccounted_allowance_tokens'] == 32768  # the manifest's higher limit

    asyncio.run(scenario())


def test_a_client_that_leaves_mid_stream_is_unaccounted(world):
    async def scenario():
        entry = world.bless(manifest_doc(concurrency=1))
        async with runtimes(FakeRuntime()) as (rt,):
            rt.after_first = asyncio.Event()
            world.place('i-a', entry, rt.port)
            async with gateway(world) as (gw, client):
                resp = await client.post('/v1/chat/completions', json={**TOOL_BODY, 'stream': True}, headers=AUTH)
                await resp.content.readuntil(b'\n\n')
                resp.close()
                await until(lambda: gw.table.in_flight == {})
                rt.after_first.set()
                health = await healthz(client)
        row = served(health)
        assert (row['requests'], row['completion_tokens'], row['unaccounted_requests']) == (1, 0, 1)
        assert row['unaccounted_allowance_tokens'] == TOOL_BODY['max_tokens']

    asyncio.run(scenario())


def test_runtime_errors_dead_instances_and_http_passthrough_are_unaccounted(world):
    async def scenario():
        entry = world.bless()
        routes = [{'path': '/echo', 'method': 'POST'}, {'path': '/v1/models', 'method': 'GET'}]
        http_entry = world.bless(manifest_doc(name='gt-http', front_door='http', routes=routes))
        async with runtimes(FakeRuntime(), FakeRuntime()) as (rt, rt_http):
            rt.error = (500, {'error': {'type': 'server_error', 'message': 'boom'}})
            world.place('i-a', entry, rt.port)
            world.place('i-h', http_entry, rt_http.port)
            async with gateway(world) as (gw, client):
                assert (await post(client, TOOL_BODY))[0] == 500
                dead = dead_port()
                world.tunnel('i-a', port=dead)
                await until(lambda: gw.table.instances['i-a'].address == ('127.0.0.1', dead))
                assert (await post(client, TOOL_BODY))[0] == 502
                async with client.post('/http/gt-http/echo', data=b'{"max_tokens": 50}', headers=AUTH) as resp:
                    assert resp.status == 200
                async with client.post('/http/gt-http/echo', data=b'\x00raw', headers=AUTH) as resp:
                    assert resp.status == 200
                health = await healthz(client)
        row = served(health)
        assert (row['requests'], row['unaccounted_requests'], row['unaccounted_allowance_tokens']) == (2, 2, 512)
        http_row = served(health, 'i-h')
        assert http_row['unaccounted_requests'] == 2
        assert http_row['unaccounted_allowance_tokens'] == 50 + cfg.RUNTIME_OUTPUT_CEILING_TOKENS

    asyncio.run(scenario())


def test_the_decode_rate_is_recorded_only_for_requests_that_ran_alone(world):
    async def scenario():
        entry = world.bless(manifest_doc(concurrency=4))
        async with runtimes(FakeRuntime()) as (rt,):
            rt.after_first = asyncio.Event()
            world.place('i-a', entry, rt.port)
            async with gateway(world) as (gw, client):

                async def stream():
                    async with client.post(
                        '/v1/chat/completions', json={**TOOL_BODY, 'stream': True}, headers=AUTH
                    ) as resp:
                        return await resp.read()

                shared = [asyncio.create_task(stream()) for _ in range(2)]
                await until(lambda: gw.table.in_flight.get('i-a') == 2)
                rt.after_first.set()
                await asyncio.gather(*shared)
                assert served(await healthz(client))['decode_tps_alone_n'] == 0  # they overlapped
                await stream()
                row = served(await healthz(client))
        assert row['decode_tps_alone_n'] == 1 and row['decode_tps_alone_p50'] > 0
        assert (row['requests'], row['completion_tokens']) == (3, 27)

    asyncio.run(scenario())


def test_the_totals_of_an_instance_long_gone_are_dropped_and_a_restart_starts_over(world):
    async def scenario():
        entry = world.bless()
        async with runtimes(FakeRuntime()) as (rt,):
            world.place('i-a', entry, rt.port)
            async with gateway(world) as (gw, client):
                assert (await post(client, TOOL_BODY))[0] == 200
                del world.records['i-a']
                world.tunnels.pop('i-a')
                world.save()
                await until(lambda: 'i-a' not in gw.table.instances)
                assert 'i-a' in (await healthz(client))['served']  # kept a while after it left the table
                gw.served['i-a'].last_at -= SERVED_KEEP_S + 1
                await until(lambda: 'i-a' not in gw.served)
                first = gw.started_at
            async with gateway(world) as (gw2, client):
                health = await healthz(client)
        assert health['served'] == {} and health['started_at'] == gw2.started_at >= first

    asyncio.run(scenario())
