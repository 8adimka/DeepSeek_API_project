# 🇬🇧 English Version

## DeepSeek Live-Coding Assistant Daemon

A sophisticated multi-functional AI-assistant daemon designed to assist with technical interviews, featuring AI-powered code generation, real-time audio transcription, and intelligent dialogue management.

### ✨ Advanced Features

- **Dual AI Integration**: Supports both DeepSeek and OpenAI APIs for different use cases
- **Real-time Audio Transcription**: Transcribes spoken interview questions using Deepgram API
- **Intelligent Dialogue Context**: Maintains conversation context across multiple questions
- **Human-like Typing Simulation**: Realistic typing
- **Telegram Integration**: Sends selected text and AI responses to Telegram
- **Hotkey Controls**: Multiple hotkeys for different functions
- **Background Daemon**: Runs as a system daemon for continuous operation

### 🔧 Requirements

- **Python 3.7+**
- **Supported platforms**: Linux with X11 (KDE Plasma recommended)
- **Required APIs**:
  - DeepSeek API key
  - OpenAI API key (optional, for audio questions)
  - Deepgram API key (optional, for audio transcription)
  - Telegram Bot Token (optional, for notifications)

### 📦 Dependencies

Install dependencies from `requirements.txt`:

```bash
pip install -r requirements.txt
```

### 🔑 Environment Configuration

Create a `.env` file with the following variables:

```ini
DEEPSEEK_API_KEY='your_deepseek_api_key_here'
OPENAI_API_KEY='your_openai_api_key_here'
DEEPGRAM_API_KEY='your_deepgram_api_key_here'
TELEGRAM_BOT_TOKEN="your_telegram_bot_token"
TELEGRAM_CHAT_ID="your_telegram_chat_id"
```

### 🚀 How to Use

1. **Setup Environment**:

   ```bash
   python -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```

2. **Run the Application**:

   ```bash
   python wp_10.py
   ```

### ⌨️ Hotkey Controls

- **F8**: Solve coding task from clipboard using DeepSeek
- **F9**: Pause/resume typing process
- **INSERT**: Send selected text to Telegram
- **NumLock**: Start/stop audio recording for interview questions (uses OpenAI)

### 🔄 Available Scripts

- **wp_10.py**: Main application with all features (recommended)
- **wp_9.py**: Previous version with audio transcription
- **wp_8.py**: Basic version with DeepSeek integration
- **SQL_wp_6.py**: SQL task solver
- **cc_1.py**: Cash-Cacher for sending text to Telegram

### ⚠️ Important Notes

- This is an advanced tool requiring active monitoring during use
- Audio transcription requires Deepgram API key
- OpenAI integration is optional but recommended for audio questions
- Manual indentation correction may be needed occasionally
- The tool simulates human typing with realistic imperfections

### 🎯 Use Cases

1. **Live Coding Interviews**: Copy task description and press F8 for AI-generated solution
2. **Technical Q&A**: Use NumLock to record and transcribe interview questions
3. **Code Sharing**: Press INSERT to send selected code to Telegram
4. **SQL Tasks**: Use SQL_wp_6.py for database-related problems

---

# Русская версия

## DeepSeek Live-Coding Assistant Daemon

Многофункциональный АИ-ассистент для технических собеседований с поддержкой генерации кода, транскрипции аудио и интеллектуальным управлением диалогом.

### ✨ Расширенные возможности

- **Двойная AI-интеграция**: Поддержка DeepSeek и OpenAI API для разных задач
- **Транскрипция аудио в реальном времени**: Расшифровка устных вопросов через Deepgram API
- **Интеллектуальный контекст диалога**: Сохранение контекста между вопросами
- **Реалистичная имитация печати**: Естественная печать
- **Интеграция с Telegram**: Отправка выделенного текста и ответов AI в Telegram
- **Горячие клавиши**: Множество горячих клавиш для разных функций
- **Фоновый демон**: Работа в фоновом режиме как системный демон

### 🔧 Требования

- **Python 3.7+**
- **Поддерживаемые платформы**: Linux с X11 (рекомендуется KDE Plasma)
- **Необходимые API**:
  - DeepSeek API ключ
  - OpenAI API ключ (опционально, для аудиовопросов)
  - Deepgram API ключ (опционально, для транскрипции)
  - Telegram Bot Token (опционально, для уведомлений)

### 📦 Зависимости

Установите зависимости из `requirements.txt`:

```bash
pip install -r requirements.txt
```

### 🔑 Настройка окружения

Создайте файл `.env` со следующими переменными:

```ini
DEEPSEEK_API_KEY='ваш_deepseek_api_ключ'
OPENAI_API_KEY='ваш_openai_api_ключ'
DEEPGRAM_API_KEY='ваш_deepgram_api_ключ'
TELEGRAM_BOT_TOKEN="токен_вашего_телеграм_бота"
TELEGRAM_CHAT_ID="ваш_телеграм_chat_id"
```

### 🚀 Как использовать

1. **Настройка окружения**:

   ```bash
   python -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```

2. **Запуск приложения**:

   ```bash
   python wp_11.py
   ```

### ⌨️ Горячие клавиши

- **F8**: Решение алгоритмических и других задач (Python) из буфера обмена (CTRL+C) (DeepSeek)
- **F9**: Пауза/возобновление ввода задачи
- **INSERT**: Отправка выделенного текста в Telegram
- **NumLock**: Старт/стоп записи аудио для вопросов (использует OpenAI)

### 🔄 Доступные скрипты

- **wp_11.py**: Основное приложение со всеми функциями (рекомендуется)
- **wp_9.py**: Предыдущая версия с транскрипцией аудио, но без контекстного окна
- **wp_8.py**: Базовая версия с интеграцией DeepSeek под все задачи
- **SQL_wp_6.py**: Решение SQL-задач
- **cc_1.py**: Cash-Cacher для отправки текста в Telegram
- **xh_2.py**: eXplain Helper - ассистент для системного аналитика

### ⚠️ Важные замечания

- Это продвинутый инструмент, требующий активного контроля во время использования
- Транскрипция аудио требует Deepgram API ключ
- Интеграция с OpenAI опциональна, но рекомендуется для аудиовопросов
- Иногда может потребоваться ручная коррекция отступов
- Инструмент имитирует человеческую печать с реалистичными несовершенствами

### 🎯 Сценарии использования

1. **Live Coding собеседования**: Скопируйте условие задачи (CTRL+C) и нажмите F8 для AI-решения
2. **Технические вопросы**: Используйте NumLock для записи аудио-вопросов - ответы отправляются в ваш телеграм-бот (требуется TELEGRAM_BOT_TOKEN и CHAT_ID)
3. **Сохранение важной информации**: Нажмите INSERT для отправки выделенного кода в Telegram (требуется TELEGRAM_BOT_TOKEN и CHAT_ID)
4. **SQL задачи**: Используйте SQL_wp_6.py для задач по базам данных SQL
5. **Помощь в сборе требований и общении с Product Owners для системного аналитика**: Используйте xh_2.py
