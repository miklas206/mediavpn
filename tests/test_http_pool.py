import asyncio

import httpx

from mediavpn.http_pool import HttpPool


def test_concurrent_identity_reads_share_only_inflight_request_and_keep_tokens_separate():
    async def scenario():
        calls = []
        async def handle(request):
            calls.append(request.headers.get('Authorization'))
            await asyncio.sleep(.01)
            return httpx.Response(200, json={'ok': True})
        pool = HttpPool(base_url='https://synthetic.invalid', transport=httpx.MockTransport(handle))
        async def read(token):
            return await pool.request('GET', '/session', coalesce=True, headers={'Authorization': token})
        await asyncio.gather(read('a'), read('a'), read('b'))
        assert sorted(calls) == ['a', 'b']
        await read('a')
        assert len(calls) == 3  # No authorization result survives completion.
        await asyncio.gather(pool.request('POST','/action'),pool.request('POST','/action'))
        assert len(calls) == 5  # Never merge mutations.
        await pool.close()
        assert pool.client is None and not pool.pending
    asyncio.run(scenario())
