import asyncio
from dotenv import load_dotenv

load_dotenv()

async def main():
    from func.state import db_init
    from func.bot import run_bot
    db_init()
    await run_bot()

if name == "main":
    asyncio.run(main())
