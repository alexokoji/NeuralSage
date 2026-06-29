import asyncio
import os
from dotenv import load_dotenv
from app.database import init_db
from app.models.api_key import ApiKey
from app.services.exchange.factory import build_client

async def main():
    os.chdir('c:/Users/Darker Elf/Downloads/NeuralSage/project/backend')
    load_dotenv('.env')
    await init_db()
    
    bybit_keys = await ApiKey.find(ApiKey.exchange == 'bybit').to_list()
    print('Bybit keys analysis:\n')
    
    for k in bybit_keys:
        try:
            client = build_client(k)
            info = await client._signed('GET', '/v5/user/query-api')
            readonly = info.get('readOnly', 0)
            apikey_display = info.get('apiKey', 'N/A')
            note = info.get('note', 'N/A')
            print(f'Label: {k.label}')
            print(f'  ID: {k.id}')
            print(f'  Read-only: {readonly}')
            print(f'  API Key: {apikey_display}')
            print(f'  Note: {note}')
            print(f'  App perms: {k.permissions}')
            print()
            await client.close()
        except Exception as exc:
            print(f'Label: {k.label}')
            print(f'  ERROR: {type(exc).__name__}: {exc}')
            print()

asyncio.run(main())
