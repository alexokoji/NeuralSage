import asyncio
import os
from dotenv import load_dotenv
from app.database import init_db
from app.models.agent import Agent
from app.models.api_key import ApiKey
from app.services.trading_engine import TradingEngine

async def main():
    os.chdir(r'c:/Users/Darker Elf/Downloads/NeuralSage/project/backend')
    load_dotenv('.env')
    await init_db()
    hyper = await Agent.find_one(Agent.name == 'HYPER')
    if not hyper:
        print('HYPER not found')
        return
    api_key = await ApiKey.find_one(ApiKey.id == hyper.api_key_id)
    if not api_key:
        print('API key not found')
        return
    print('before', hyper.last_signal, hyper.last_signal_symbol, hyper.last_error, hyper.total_trades)
    result = await TradingEngine().run_agent_tick(hyper, api_key)
    print('result', result)
    print('after', hyper.last_signal, hyper.last_signal_symbol, hyper.last_error, hyper.total_trades)

if __name__ == '__main__':
    asyncio.run(main())
