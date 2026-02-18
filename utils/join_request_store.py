import asyncio


class JoinRequestSessionStore:
    def __init__(self):
        self._instances = {}
        self._tasks = {}
        self._lock = asyncio.Lock()

    async def set(self, uuid: str, instance, task: asyncio.Task):
        async with self._lock:
            self._instances[uuid] = instance
            self._tasks[uuid] = task

    async def get(self, uuid: str):
        async with self._lock:
            return self._instances.get(uuid)

    async def remove(self, uuid: str):
        async with self._lock:
            self._instances.pop(uuid, None)
            self._tasks.pop(uuid, None)
