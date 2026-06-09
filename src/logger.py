import asyncio

class AsyncLogger:
    def __init__(self):
        self.listeners = []

    async def log(self, message: str):
        print(message)  # Always print to terminal
        
        # Remove dead listeners
        self.listeners = [q for q in self.listeners if not q.empty() or True] # Wait, we shouldn't kill empty ones, just rely on try/except or manual unregister if needed. Actually, a simpler way is to just put it in all queues.
        
        for queue in self.listeners:
            await queue.put(message)

    def listen(self):
        q = asyncio.Queue()
        self.listeners.append(q)
        return q

    def remove_listener(self, q: asyncio.Queue):
        if q in self.listeners:
            self.listeners.remove(q)

stream_logger = AsyncLogger()

async def stream_log(message: str):
    await stream_logger.log(message)
