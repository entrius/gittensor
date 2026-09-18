# The MIT License (MIT)
# Copyright © 2025 Entrius

"""The gateway end to end against fake runtimes: passthrough of tool calling byte for byte, the limits, SSE relay,
reserve-or-429, draining, the key, /v1/models republishing, the usage line, and the table refresh (vault ``26`` §2,
``25`` "Front door types")."""

import asyncio
import json
import socket

from click.testing import CliRunner

from gittensor.gateway.cli import gateway_command
from tests.gateway.conftest import (
    AUTH,
    NAME,
    RUNTIME_ID,
    FakeRuntime,
    gateway,
    manifest_doc,
    post,
    runtimes,
    until,
)

USAGE_FIELDS = {
    'ts', 'instance', 'entry', 'model', 'prompt_tokens', 'completion_tokens', 'ttft_ms', 'total_ms', 'decode_tps',
    'status', 'finish_reason', 'stream',
}  # fmt: skip

# An agent turn: content parts, an assistant tool_calls turn, a role:tool result, tools, tool_choice, parallel calls.
TOOL_BODY = {
    'model': RUNTIME_ID,
    'max_tokens': 256,
    'temperature': 0.2,
    'messages': [
        {'role': 'system', 'content': 'You are terse.'},
        {
            'role': 'user',
            'content': [
                {'type': 'text', 'text': 'What is 17 × 23?'},
                {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,iVBORw0KGgo='}},
            ],
        },
        {
            'role': 'assistant',
            'content': None,
            'tool_calls': [{'id': 'call_0', 'type': 'function', 'function': {'name': 'multiply', 'arguments': '{"a": 17, "b": 23}'}}],
        },
        {'role': 'tool', 'tool_call_id': 'call_0', 'content': '391'},
        {'role': 'user', 'content': 'And 391 × 2? Use the tool again.'},
    ],
    'tools': [
        {
            'type': 'function',
            'function': {
                'name': 'multiply',
                'description': 'a × b',
                'parameters': {'type': 'object', 'properties': {'a': {'type': 'number'}, 'b': {'type': 'number'}}, 'required': ['a', 'b']},
            },
        }
    ],
    'tool_choice': 'auto',
    'parallel_tool_calls': False,
}  # fmt: skip


def test_tools_tool_calls_and_role_tool_reach_the_runtime_byte_for_byte(world):
    async def scenario():
        entry = world.bless()
        async with runtimes(FakeRuntime()) as (rt,):
            world.place('i-a', entry, rt.port)
            async with gateway(world) as (_, client):
                # Odd spacing and non-ASCII: only forwarding the exact bytes keeps this equal.
                raw = json.dumps(TOOL_BODY, indent=3, ensure_ascii=False).encode()
                status, body, headers = await post(client, raw=raw)
        assert status == 200
        assert [r['path'] for r in rt.received] == ['/v1/chat/completions']
        assert rt.received[0]['body'] == raw
        assert json.loads(body)['choices'][0]['message']['tool_calls'][0]['function']['name'] == 'multiply'
        assert (headers['X-GT-Instance'], headers['X-GT-Entry']) == ('i-a', entry)

    asyncio.run(scenario())


def test_the_token_fields_reach_the_runtime_as_sent_and_only_the_manifest_name_becomes_the_runtime_id(world):
    async def scenario():
        entry = world.bless()
        async with runtimes(FakeRuntime()) as (rt,):
            world.place('i-a', entry, rt.port)
            async with gateway(world) as (_, client):
                assert (await post(client, {**TOOL_BODY, 'model': NAME, 'max_tokens': 20_000}))[0] == 200
                no_limit = {k: v for k, v in TOOL_BODY.items() if k != 'max_tokens'}
                no_limit_raw = json.dumps(no_limit, indent=2).encode()
                assert (await post(client, {**no_limit, 'max_completion_tokens': 5000}))[0] == 200
                assert (await post(client, raw=no_limit_raw))[0] == 200
                raw = json.dumps({'model': RUNTIME_ID, 'prompt': '<|im_start|>user\nhi', 'max_tokens': 8}).encode()
                assert (await post(client, path='/v1/completions', raw=raw))[0] == 200
        sent = [json.loads(r['body']) for r in rt.received]
        assert sent[0] == {**TOOL_BODY, 'model': RUNTIME_ID, 'max_tokens': 20_000}  # no cap of ours: as sent
        assert sent[1] == {**no_limit, 'max_completion_tokens': 5000}
        assert rt.received[2]['body'] == no_limit_raw and 'max_tokens' not in sent[2]  # nothing injected: the bytes
        assert (rt.received[3]['path'], rt.received[3]['body']) == ('/v1/completions', raw)

    asyncio.run(scenario())


def test_limits_are_refused_before_any_instance_sees_the_request(world):
    remote_image = json.loads(json.dumps(TOOL_BODY))
    remote_image['messages'][1]['content'][1]['image_url']['url'] = 'https://example.com/cat.png'
    remote_video = {**TOOL_BODY, 'messages': [{'role': 'user', 'content': [{'type': 'video_url', 'video_url': 'http://x/v.mp4'}]}]}  # fmt: skip
    cases = [
        ({**TOOL_BODY, 'n': 2}, 400, 'n must be 1'),
        (remote_image, 400, 'remote media not supported yet'),
        (remote_video, 400, 'remote media not supported yet'),
        ({**TOOL_BODY, 'max_tokens': 'lots'}, 400, 'max_tokens must be a positive integer'),
        ({k: v for k, v in TOOL_BODY.items() if k != 'model'}, 400, 'model is required'),
        ({**TOOL_BODY, 'model': 'not-served'}, 404, 'is not served'),
        ({**TOOL_BODY, 'messages': [{'role': 'user', 'content': 'x' * 5000}]}, 413, 'body over 4096 bytes'),
    ]

    async def scenario():
        entry = world.bless()
        async with runtimes(FakeRuntime()) as (rt,):
            world.place('i-a', entry, rt.port)
            async with gateway(world, max_body_bytes=4096) as (gw, client):
                for body, status, message in cases:
                    got, answer, _ = await post(client, body)
                    assert (got, message in json.loads(answer)['error']['message']) == (status, True), (body, answer)
                got, answer, _ = await post(client, raw=b'{"model": ')
                assert got == 400 and 'not JSON' in json.loads(answer)['error']['message']
                assert gw.table.in_flight == {}
        assert rt.received == []

    asyncio.run(scenario())


def test_streaming_is_relayed_chunk_by_chunk_in_order_and_ends_on_done(world):
    lines: list[str] = []

    async def scenario():
        entry = world.bless()
        async with runtimes(FakeRuntime()) as (rt,):
            rt.after_first = asyncio.Event()
            world.place('i-a', entry, rt.port)
            async with gateway(world, sink=lines.append) as (_, client):
                async with client.post(
                    '/v1/chat/completions', json={**TOOL_BODY, 'stream': True}, headers=AUTH
                ) as resp:
                    assert resp.status == 200
                    assert resp.headers['Content-Type'].startswith('text/event-stream')
                    first = await asyncio.wait_for(resp.content.readuntil(b'\n\n'), 5)
                    assert len(rt.sent) == 1  # the runtime is still parked after its first chunk: this was relayed live
                    rt.after_first.set()
                    rest = await resp.read()
                expected = b''.join(rt.chunks())
        assert first + rest == expected
        assert (first + rest).endswith(b'data: [DONE]\n\n')
        usage = json.loads(lines[-1])
        assert (usage['status'], usage['stream'], usage['finish_reason']) == (200, True, 'tool_calls')
        assert (usage['prompt_tokens'], usage['completion_tokens']) == (42, 9)
        assert usage['ttft_ms'] > 0 and usage['decode_tps'] > 0 and usage['total_ms'] >= usage['ttft_ms']

    asyncio.run(scenario())


def test_429_when_every_slot_is_taken_and_the_slot_frees_after_completion(world):
    lines: list[str] = []

    async def scenario():
        entry = world.bless(manifest_doc(concurrency=1))
        async with runtimes(FakeRuntime()) as (rt,):
            rt.hold = asyncio.Event()
            world.place('i-a', entry, rt.port)
            async with gateway(world, sink=lines.append) as (gw, client):
                first = asyncio.create_task(post(client, TOOL_BODY))
                await until(lambda: len(rt.received) == 1)
                status, body, headers = await post(client, TOOL_BODY)
                assert status == 429 and json.loads(body)['error']['type'] == 'capacity'
                assert headers['Retry-After'] == '1'
                async with client.get('/metrics', headers=AUTH) as resp:
                    busy = await resp.text()
                assert f'gt_gateway_in_flight{{entry="{entry}",instance="i-a"}} 1' in busy
                assert f'gt_gateway_capacity_429_total{{entry="{entry}"}} 1' in busy
                async with client.get('/healthz') as resp:  # keyless: what the controller's drain waits on
                    assert (await resp.json())['in_flight'] == {'i-a': 1}
                rt.hold.set()
                assert (await first)[0] == 200
                assert (await post(client, TOOL_BODY))[0] == 200
                async with client.get('/metrics', headers=AUTH) as resp:
                    idle = await resp.text()
                assert f'gt_gateway_in_flight{{entry="{entry}",instance="i-a"}} 0' in idle
                assert f'gt_gateway_requests_total{{entry="{entry}",instance="i-a"}} 2' in idle
                assert gw.table.in_flight == {}
                async with client.get('/healthz') as resp:
                    assert (await resp.json())['in_flight'] == {}
        assert len(rt.received) == 2  # the refused request never reached the runtime
        refused = [json.loads(line) for line in lines if json.loads(line)['status'] == 429]
        assert [(u['instance'], u['entry'], u['model']) for u in refused] == [(None, entry, RUNTIME_ID)]

    asyncio.run(scenario())


def test_a_client_that_leaves_mid_stream_frees_its_slot(world):
    lines: list[str] = []

    async def scenario():
        entry = world.bless(manifest_doc(concurrency=1))
        async with runtimes(FakeRuntime()) as (rt,):
            rt.after_first = asyncio.Event()
            world.place('i-a', entry, rt.port)
            async with gateway(world, sink=lines.append) as (gw, client):
                resp = await client.post('/v1/chat/completions', json={**TOOL_BODY, 'stream': True}, headers=AUTH)
                await resp.content.readuntil(b'\n\n')
                resp.close()
                await until(lambda: gw.table.in_flight == {})
                rt.after_first.set()
                assert (await post(client, TOOL_BODY))[0] == 200
        assert [json.loads(line)['status'] for line in lines] == [499, 200]

    asyncio.run(scenario())


def test_a_draining_instance_gets_no_new_requests(world):
    async def scenario():
        entry = world.bless()
        async with runtimes(FakeRuntime(), FakeRuntime()) as (a, b):
            world.place('i-a', entry, a.port)
            world.place('i-b', entry, b.port, healthy=False, draining=True)
            async with gateway(world) as (gw, client):
                served = [(await post(client, TOOL_BODY))[2]['X-GT-Instance'] for _ in range(6)]
                assert served == ['i-a'] * 6
                world.update('i-a', healthy=False, draining=True)
                world.update('i-b', healthy=True, draining=False)
                await until(lambda: not gw.table.instances['i-a'].routable and gw.table.instances['i-b'].routable)
                served = [(await post(client, TOOL_BODY))[2]['X-GT-Instance'] for _ in range(4)]
                assert served == ['i-b'] * 4
        assert (len(a.received), len(b.received)) == (6, 4)

    asyncio.run(scenario())


def test_the_gateway_key_is_required_everywhere_but_healthz(world):
    async def scenario():
        entry = world.bless()
        async with runtimes(FakeRuntime()) as (rt,):
            world.place('i-a', entry, rt.port)
            async with gateway(world) as (_, client):
                routes = [
                    ('GET', '/v1/models'),
                    ('GET', '/metrics'),
                    ('POST', '/v1/chat/completions'),
                    ('POST', '/http/x/echo'),
                ]
                for method, path in routes:
                    for headers in ({}, {'X-GT-Gateway-Key': 'wrong'}):
                        async with client.request(method, path, headers=headers, json=TOOL_BODY) as resp:
                            assert resp.status == 401, (method, path, headers)
                            assert (await resp.json())['error']['type'] == 'unauthorized'
                async with client.get('/healthz') as resp:
                    assert resp.status == 200
                    assert (await resp.json())['routable'] == {entry: 1}
        assert rt.received == []

    asyncio.run(scenario())


def test_models_republish_the_runtime_under_the_manifest_name_with_the_override(world):
    async def scenario():
        entry = world.bless()
        cold = world.bless(manifest_doc(name='gt-cold'))
        async with runtimes(FakeRuntime(), FakeRuntime(model_id='cold-runtime')) as (rt, cold_rt):
            world.place('i-a', entry, rt.port)
            world.place('i-cold', cold, cold_rt.port, healthy=False)  # starting: not routable, not published
            (world.root / 'models_override.json').write_text(
                json.dumps(
                    {
                        NAME: {'context_length': 32768, 'capabilities': {'vision': {'video': True}}, 'id': 'ignored'},
                        entry: {'description': 'override by entry id'},
                    }
                )
            )
            async with gateway(world) as (_, client):
                answers = []
                for _ in range(2):  # the override never leaks into the cached runtime object
                    async with client.get('/v1/models', headers=AUTH) as resp:
                        answers.append(await resp.json())
        expected = {
            'id': NAME,
            'object': 'model',
            'owned_by': 'gittensor',
            'created': 0,
            'context_length': 32768,
            'max_output_tokens': 32768,  # the manifest names no cap: the runtime's own advertised number passes through
            'capabilities': {'tools': True, 'vision': {'image': True, 'video': True}},
            'description': 'override by entry id',
        }
        assert answers == [{'object': 'list', 'data': [expected]}] * 2

    asyncio.run(scenario())


def test_models_output_cap_is_the_manifests_else_the_runtimes_own_never_invented(world):
    async def scenario():
        capped = world.bless({**manifest_doc(name='gt-capped'), 'profile': {'max_output_tokens': 16384, 'vram_gb': 30}})
        uncapped = world.bless(manifest_doc(name='gt-open'))
        silent = world.bless(manifest_doc(name='gt-silent'))
        async with runtimes(FakeRuntime(), FakeRuntime(), FakeRuntime(max_output_tokens=None)) as (rt_a, rt_b, rt_c):
            world.place('i-a', capped, rt_a.port)
            world.place('i-b', uncapped, rt_b.port)
            world.place('i-c', silent, rt_c.port)  # the runtime advertises no cap either
            async with gateway(world) as (_, client):
                async with client.get('/v1/models', headers=AUTH) as resp:
                    data = (await resp.json())['data']
        by_id = {m['id']: m for m in data}
        assert by_id['gt-capped']['max_output_tokens'] == 16384  # the manifest wins over the runtime's 32768
        assert by_id['gt-open']['max_output_tokens'] == 32768  # no manifest cap: the runtime's own, as advertised
        assert 'max_output_tokens' not in by_id['gt-silent']  # neither names one: none is invented

    asyncio.run(scenario())


def test_the_usage_line_on_stdout_has_every_field_and_never_estimates_tokens(world, capsys):
    async def scenario():
        entry = world.bless()
        async with runtimes(FakeRuntime(usage=False)) as (rt,):
            world.place('i-a', entry, rt.port)
            async with gateway(world) as (_, client):
                assert (await post(client, TOOL_BODY))[0] == 200
        return entry

    entry = asyncio.run(scenario())
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith('{')]
    assert len(lines) == 1
    usage = lines[0]
    assert set(usage) == USAGE_FIELDS
    assert (usage['instance'], usage['entry'], usage['model'], usage['status']) == ('i-a', entry, RUNTIME_ID, 200)
    assert (usage['prompt_tokens'], usage['completion_tokens'], usage['ttft_ms'], usage['decode_tps']) == (None,) * 4
    assert (usage['finish_reason'], usage['stream']) == ('tool_calls', False)
    assert usage['total_ms'] > 0 and usage['ts'] > 1_700_000_000


def test_a_newly_healthy_instance_is_picked_up_without_a_restart(world):
    async def scenario():
        entry = world.bless()
        async with runtimes(FakeRuntime()) as (rt,):
            world.place('i-a', entry, rt.port, healthy=False)
            async with gateway(world) as (gw, client):
                # By manifest name: the runtime's own id is only known once a healthy instance's /v1/models was read.
                assert (await post(client, {**TOOL_BODY, 'model': NAME}))[0] == 429
                world.update('i-a', healthy=True)
                await until(lambda: gw.table.instances['i-a'].routable)
                assert (await post(client, {**TOOL_BODY, 'model': NAME}))[0] == 200
        assert len(rt.received) == 1

    asyncio.run(scenario())


def test_a_tampered_registry_entry_leaves_its_instances_unroutable(world):
    async def scenario():
        entry = world.bless()
        async with runtimes(FakeRuntime()) as (rt,):
            world.place('i-a', entry, rt.port)
            async with gateway(world) as (gw, client):
                assert (await post(client, TOOL_BODY))[0] == 200
                path = world.root / 'registry' / f'{entry}.json'
                path.write_bytes(path.read_bytes().replace(b'"concurrency":4', b'"concurrency":400'))
                await until(lambda: gw.table.instances['i-a'].manifest is None)
                assert (await post(client, {**TOOL_BODY, 'model': NAME}))[0] == 429
                async with client.get('/healthz') as resp:
                    assert (await resp.json())['routable'] == {entry: 0}
                async with client.get('/v1/models', headers=AUTH) as resp:
                    assert (await resp.json())['data'] == []
        assert len(rt.received) == 1

    asyncio.run(scenario())


def test_runtime_errors_pass_through_with_their_body_and_a_dead_instance_is_a_502(world):
    runtime_error = {'error': {'type': 'invalid_request_error', 'message': 'context_length_exceeded: 70000 > 65536'}}

    async def scenario():
        entry = world.bless()
        async with runtimes(FakeRuntime()) as (rt,):
            rt.error = (400, runtime_error)
            world.place('i-a', entry, rt.port)
            async with gateway(world) as (gw, client):
                status, body, _ = await post(client, TOOL_BODY)
                assert (status, json.loads(body)) == (400, runtime_error)
                with socket.socket() as probe:
                    probe.bind(('127.0.0.1', 0))
                    dead = probe.getsockname()[1]
                world.update('i-a', port=dead)
                await until(lambda: gw.table.instances['i-a'].record.port == dead)
                status, body, _ = await post(client, TOOL_BODY)
                assert (status, json.loads(body)['error']['type']) == (502, 'upstream')
                assert gw.table.in_flight == {}

    asyncio.run(scenario())


def test_http_front_doors_pass_through_declared_routes_only(world):
    async def scenario():
        routes = [{'path': '/echo', 'method': 'POST'}, {'path': '/v1/models', 'method': 'GET'}]
        entry = world.bless(manifest_doc(name='gt-http', front_door='http', routes=routes))
        async with runtimes(FakeRuntime()) as (rt,):
            world.place('i-h', entry, rt.port)
            async with gateway(world) as (_, client):
                async with client.post('/http/gt-http/echo', data=b'\x00raw bytes\xff', headers=AUTH) as resp:
                    assert (resp.status, await resp.read()) == (200, b'\x00raw bytes\xff')
                for path in ('/http/gt-http/not-declared', '/http/nobody/echo'):
                    async with client.post(path, data=b'x', headers=AUTH) as resp:
                        assert resp.status == 404
                assert (await post(client, {**TOOL_BODY, 'model': 'gt-http'}))[0] == 404  # not an OpenAI model
        assert [r['path'] for r in rt.received] == ['/echo']

    asyncio.run(scenario())


def test_the_command_refuses_to_start_without_the_key(monkeypatch, tmp_path):
    monkeypatch.delenv('GT_GATEWAY_KEY', raising=False)
    result = CliRunner().invoke(gateway_command, ['--state-dir', str(tmp_path)])
    assert result.exit_code == 2 and 'GT_GATEWAY_KEY is not set' in result.output
    result = CliRunner().invoke(gateway_command, ['--listen', 'nope'], env={'GT_GATEWAY_KEY': 'k'})
    assert result.exit_code == 2 and 'expected host:port' in result.output
