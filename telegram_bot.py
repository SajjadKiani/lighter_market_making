import requests
import os

CHAT_ID=os.getenv('CHAT_ID')
BOT_TOKEN=os.getenv('TOKEN_BOT')

def send_message(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "Markdown"  # Optional: can be 'HTML' or 'Markdown'
    }
    response = requests.post(url, json=payload)
    return response.json()
