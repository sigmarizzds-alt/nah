import asyncio
import threading
import os
from dotenv import load_dotenv

load_dotenv()

async def main():
    loop = asyncio.get_running_loop()
    from func.bot import run_bot
    await run_bot()


if name == "main":
    asyncio.run(main())
