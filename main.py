import asyncio
import threading
import os
from dotenv import load_dotenv

load_dotenv()

WEB_HOST = os.getenv("WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.getenv("WEB_PORT", "13897"))


async def main():
    loop = asyncio.get_running_loop()
  import run_web
    threading.Thread(target=run_web, args=(loop, WEB_HOST, WEB_PORT), daemon=True).start()
    print(f"[WEB] http://{WEB_HOST}:{WEB_PORT}/")
    from func.bot import run_bot
    await run_bot()


if __name__ == "__main__":

    asyncio.run(main())
