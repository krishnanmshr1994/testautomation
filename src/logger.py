import asyncio
import os
from datetime import datetime
from zoneinfo import ZoneInfo

class AsyncLogger:
    def __init__(self):
        self.listeners = []
        os.makedirs("logs", exist_ok=True)
        self.log_file = os.path.join("logs", "automation.log")

    async def log(self, message: str, write_to_file: bool = True):
        print(message)  # Always print to terminal
        
        # Append to log file
        if write_to_file:
            try:
                with open(self.log_file, "a", encoding="utf-8") as f:
                    nyc_tz = ZoneInfo("America/New_York")
                    timestamp = datetime.now(nyc_tz).strftime("%Y-%m-%d %H:%M:%S")
                    f.write(f"[{timestamp}] {message}\n")
            except Exception:
                pass
        
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

async def stream_log(message: str, write_to_file: bool = True):
    await stream_logger.log(message, write_to_file)
