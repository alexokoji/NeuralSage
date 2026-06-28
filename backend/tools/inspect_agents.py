import asyncio
import os
from pprint import pprint

from beanie import init_beanie
from motor.motor_asyncio import AsyncIOMotorClient

from app.models.agent import Agent

MONGODB_URL = os.environ.get("MONGODB_URL", "mongodb://localhost:27017/neuraltrade")

async def main():
    client = AsyncIOMotorClient(MONGODB_URL)
    await init_beanie(database=client.get_default_database(), document_models=[Agent])

    agents = await Agent.find_all().to_list()
    out = []
    for a in agents:
        out.append({
            "id": str(a.id),
            "name": a.name,
            "status": a.status,
            "assigned_capital": float(a.assigned_capital or 0),
            "api_key_id": str(a.api_key_id) if a.api_key_id else None,
            "trading_pairs": a.trading_pairs,
            "is_paper_trade": a.is_paper_trade,
            "protect_mode": getattr(a, "protect_mode", False),
            "tick_count": int(a.tick_count or 0),
        })
    pprint(out)

if __name__ == "__main__":
    asyncio.run(main())
