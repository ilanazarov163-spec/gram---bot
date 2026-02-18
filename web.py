import os
import threading
from flask import Flask
import bot as telegram_bot

app = Flask(__name__)

@app.get("/")
def home():
    return "OK", 200

def run_bot():
    telegram_bot.start_polling()

if __name__ == "__main__":
    t = threading.Thread(target=run_bot, daemon=True)
    t.start()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
