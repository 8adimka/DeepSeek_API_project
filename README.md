# 🇬🇧 English Version
## DeepSeek Live-Coding Assistant Daemon

A lightweight helper daemon designed to assist with solving live-coding problems during technical interviews.

- It grabs the problem description from your clipboard and simulates human-like typing to enter the generated solution directly into your code editor.
- Simply copy the task (Ctrl+C), and when ready, press F8 to begin typing the generated solution.
- You can pause/resume the typing process at any moment by pressing F9.

### ⚠️ Notice

- This is *not* a plug-and-play tool — you must stay attentive during usage.
- Due to technical limitations, the bot may misjudge indentation. Manual corrections might be needed.
- Occasional typos or quirks in generated text are intentional to simulate human input, but still, review everything as you go.

### ✨ Features

- Automatically queries DeepSeek API to generate Python code for the copied task.
- Simulates human typing with realistic speed and behavior.
- Allows pausing/resuming typing via hotkeys (F9).

### 🔧 Requirements

- A valid DeepSeek API key.
- Python 3.7+
- Supported platforms:
  - Linux with X11 (KDE Plasma recommended)
  - Windows is *not supported*.

### 📦 Dependencies

- Python dependencies are listed in `requirements.txt`. Install them with:

```bash
pip install -r requirements.txt
```
- Create file .env
```ini
DEEPSEEK_API_KEY='your_deepseek_api_key_here'

TELEGRAM_BOT_TOKEN="Bot_token from BotFather"
TELEGRAM_CHAT_ID="your chat_ID to connect your telegram to your Bot"
```
- Install to work with clipboard:
```bash
sudo pacman -S xclip
```

### 🚀 How to Use

- (Recommended) Activate your virtual environment with all required dependencies installed:
```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

- And then run app:
```bash
python wp-6
```

- Once activated, the program runs as a daemon (in the background):

  1. Copy the task description to your clipboard (Ctrl+C).

  2. Press F8 to activate input mode.

  3. If needed, press F9 to pause and resume the process.

- I’ve expanded the “toolkit” a bit and added:

  1. SQL_wp_6.py – for solving SQL-related tasks.

  2. cc_1.py – Cash-Cacher: sends the selected text on your screen directly to your Telegram by pressing F8.

  3. wp_7.py – In addition to the main functionality, it includes a built-in Cash-Cacher: sends the selected text on your screen to your Telegram bot by pressing INSERT.

################################################################################
# 🇷🇺 Русская версия
## DeepSeek Live-Coding Assistant Daemon

Небольшой служебный демон, который помогает при решении задач на Live-Coding во время технических собеседований.

- Он вытаскивает описание проблемы из вашего буфера обмена и имитирует человеческую печать, чтобы ввести сгенерированное решение непосредственно в ваш редактор.
- Просто выделите задачу, нажмите Ctrl+C и, когда будете готовы, активируйте ввод готового решения на F8.
- При необходимости вы можете поставить ввод на паузу, нажав F9, что-то поправить и продолжить ввод снова, нажав на F9.

### ⚠️ Внимание

- Это не решение по типу "включил и забыл".
- По понятным причинам бот может некорректно отслеживать отступы — вам придётся вручную проверять и при необходимости исправлять их.
- ИИ может допускать ошибки в орфографии — это нормально и даже добавляет натуральности процессу, но вы тоже должны следить за результатом.

### ✨ Функции

- Автоматически извлекает решение кода для поставленной задачи через DeepSeek API.
- Имитирует человеческую печать с реалистичным таймингом.
- Позволяет приостанавливать/возобновлять печать в середине процесса простыми горячими клавишами (F9).

### 🔧 Требования

- Валидный API-ключ DeepSeek.
- Python 3.7+
- Поддерживаемая среда:
  - Только Linux с X11 (рекомендуется KDE Plasma)
  - Windows не поддерживается.

### 📦 Зависимости

- Зависимости Python определены в `requirements.txt`. Установите их командой:

```bash
pip install -r requirements.txt
```
- Создай файл .env
```ini
DEEPSEEK_API_KEY='your_deepseek_api_key_here'

TELEGRAM_BOT_TOKEN="Bot_token from BotFather"
TELEGRAM_CHAT_ID="your chat_ID to connect your telegram_account to your Bot"
```
- Установи для работы c clipboard
```bash
sudo pacman -S xclip
```


### 🚀 Как использовать

- Активируй виртуальное окружение (желательно) со всеми установленными зависимостями и запусти:
```bash
python wp-6
```
- Когда активирован работает в режиме демона (в фоне):
  1. Скопируйте условие задачи в буфер обмена (Ctrl+C).
  2. Нажмите F8, чтобы активировать ввод.
  3. При необходимости нажмите F9, чтобы поставить на паузу и снова продолжить.

- Я немного расширил "зоопарк" и добавил:
  1. SQL_wp_6.py - для решения задач по SQL
  2. cc_1.py - сash-cacher отправляет выделенный текст на экране сразу тебе в телеграм по нажатию F8
  3. wp_7.py - + к основному функционалу -> встроенный cash_cacher - отправляет выделенный на экране текст тебе в телеграм-бота по нажатию INSERT




