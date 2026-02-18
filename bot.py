def start_polling():
    db_init_and_migrate()
    print("Bot polling started.")
    bot.infinity_polling(skip_pending=True)
