pip install pyrogram tgcrypto
python -c "
from pyrogram import Client
api_id = int(input('API_ID: '))
api_hash = input('API_HASH: ')
with Client('gen', api_id=api_id, api_hash=api_hash) as app:
    print('\nSESSION_STRING:')
    print(app.export_session_string())
"