📘 Инструкция по развёртыванию на Render

1️⃣ Создай репозиторий на GitHub (например, binary-signal-bot).
2️⃣ Скопируй файлы из этой папки и добавь в репозиторий:
   git init
   git add .
   git commit -m "init"
   git branch -M main
   git remote add origin https://github.com/USERNAME/binary-signal-bot.git
   git push -u origin main

3️⃣ На сайте https://render.com:
   - Нажми "New + → Web Service"
   - Подключи GitHub, выбери свой репозиторий
   - В настройках:
       Environment: Python 3
       Build command: pip install -r requirements.txt
       Start command: python bot.py

4️⃣ В разделе "Environment Variables" добавь:
   TELEGRAM_BOT_TOKEN=твой_токен_от_BotFather
   TWELVE_API_KEY=твой_API_ключ_от_TwelveData

5️⃣ Нажми Deploy 🚀
   Бот запустится и будет работать 24/7.

В Telegram:
   /start - выбрать пару и время
   /auto on - включить автоанализ
   /auto off - выключить
