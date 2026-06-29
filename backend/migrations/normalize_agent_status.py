"""
Migration: Normalize agent status values.

Converts "inactive" → "idle" for all agents, ensuring data consistency.
Run this manually: python -m migrations.normalize_agent_status
"""
import asyncio
import os
from dotenv import load_dotenv
from app.database import init_db
from app.models.agent import Agent

os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv('.env')


async def migrate():
    """Find all agents with status='inactive' and convert to 'idle'."""
    await init_db()
    
    # Count agents with 'inactive' status
    inactive = await Agent.find(Agent.status == 'inactive').to_list()
    count = len(inactive)
    
    if count == 0:
        print('✓ No agents with "inactive" status found.')
        return
    
    print(f'Found {count} agent(s) with status="inactive". Converting to "idle"...')
    
    # Update each agent
    for agent in inactive:
        agent.status = 'idle'
        await agent.save()
        print(f'  ✓ {agent.name} ({agent.id}) → idle')
    
    print(f'\n✓ Migration complete: {count} agent(s) normalized.')


if __name__ == '__main__':
    asyncio.run(migrate())
