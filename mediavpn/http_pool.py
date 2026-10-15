"""Process-local connection reuse and concurrent read coalescing; no auth TTL."""
import asyncio

import httpx


class HttpPool:
    def __init__(self, **options):
        self.options = options
        self.client = None
        self.pending = {}

    async def request(self, method, path, *, coalesce=False, **options):
        if self.client is None:
            self.client = httpx.AsyncClient(**self.options)
        if not coalesce or method != "GET":
            return await self.client.request(method, path, **options)
        key = (path, tuple(sorted(options.get("headers", {}).items())))
        task = self.pending.get(key)
        if task is None:
            task = asyncio.create_task(self.client.request(method, path, **options))
            self.pending[key] = task
            def finished(done):
                if self.pending.get(key) is done:
                    self.pending.pop(key, None)
                if not done.cancelled():
                    done.exception()
            task.add_done_callback(finished)
        return await asyncio.shield(task)

    async def close(self):
        for task in tuple(self.pending.values()):
            task.cancel()
        await asyncio.gather(*self.pending.values(), return_exceptions=True)
        self.pending.clear()
        if self.client is not None:
            await self.client.aclose()
            self.client = None
