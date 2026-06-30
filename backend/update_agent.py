import asyncio
import os
import sys

os.chdir('c:/Users/Darker Elf/Downloads/NeuralSage/project/backend')
sys.path.insert(0, 'c:/Users/Darker Elf/Downloads/NeuralSage/project/backend')

from dotenv import load_dotenv
load_dotenv('.env')

from app.database import init_db
from app.models.agent import Agent

async def main():
    await init_db()
    agent = await Agent.get('0843402e-e539-49bd-b9cb-5700a1b446a2')
    print(f'Before update:')
    print(f'  assigned_capital: {agent.assigned_capital}')
    print(f'  max_risk_per_trade: {agent.max_risk_per_trade}')
    print(f'  strategy_params: {agent.strategy_params}')
    
    agent.max_risk_per_trade = 5.0
    sp = agent.strategy_params or {}
    sp['stop_loss_pct'] = 0.5
    agent.strategy_params = sp
    await agent.save()
    
    # Verify
    agent = await Agent.get('0843402e-e539-49bd-b9cb-5700a1b446a2')
    print(f'\nAfter update:')
    print(f'  assigned_capital: {agent.assigned_capital}')
    print(f'  max_risk_per_trade: {agent.max_risk_per_trade}')
    print(f'  strategy_params.stop_loss_pct: {agent.strategy_params.get("stop_loss_pct")}')
    print(f'\nAgent updated successfully. Next tick should place a trade.')

asyncio.run(main())
