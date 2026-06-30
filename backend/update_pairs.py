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
    
    old_pairs = agent.trading_pairs or []
    print(f'Old trading pairs: {old_pairs}')
    
    # Remove high-minimum coins (BTC ~$630, ETH ~$2100 minimums)
    # Add low-price alts that work with $10
    new_pairs = [
        'DOGEUSDT',      # ~$0.07, very liquid, low minimum
        'SOLUSDT',       # ~$120, popular, reasonable minimum
        'LTCUSDT',       # ~$55, good volume, tradeable minimum
        'XRPUSDT',       # ~$2.5, high volume, low minimum
        'ADAUSDT',       # ~$0.9, low minimum
        'DOTUSDT',       # ~$6, low minimum
        'LINKUSDT',      # ~$14, good volume
        'AVAXUSDT',      # ~$28, reasonable minimum
        'MATICUSDT',     # ~$0.6, very low minimum
        'ARBUSDT',       # ~$0.9, low minimum
    ]
    
    agent.trading_pairs = new_pairs
    await agent.save()
    
    # Verify
    agent = await Agent.get('0843402e-e539-49bd-b9cb-5700a1b446a2')
    print(f'\nNew trading pairs: {agent.trading_pairs}')
    print(f'\nAgent now trades low-price alts suitable for $10 accounts:')
    print(f'  ✓ Removed: BTCUSDT (0.01 min = ~$630), ETHUSDT (0.03 min = ~$2100)')
    print(f'  ✓ Added: DOGE, SOL, LTC, XRP, ADA, DOT, LINK, AVAX, MATIC, ARB')
    print(f'\nNext tick should place a trade on one of these pairs.')

asyncio.run(main())
