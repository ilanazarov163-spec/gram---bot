import threading
from flask import Flask
import bot as telegram_bot

app = Flask(__name__)

@app.get("/")
def home():
    return "OK", 200

def run_bot():
    telegram_bot.start_polling()

# важно: запускаем при импорте, чтобы работало под gunicorn
threading.Thread(target=run_bot, daemon=True).start()
