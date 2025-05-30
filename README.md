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
```
- Install to work with clipboard:
```bash
sudo pacman -S xclip
```

### 🚀 How to Use

1. Copy the task description to your clipboard (Ctrl+C).
2. Press F8 to begin human-like typing of the generated solution.
3. Press F9 to pause/resume typing as needed.

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
```
- Установи для работы c clipboard
```bash
sudo pacman -S xclip
```


### 🚀 Как использовать

1. Скопируйте условие задачи в буфер обмена (Ctrl+C).
2. Нажмите F8, чтобы активировать ввод.
3. При необходимости нажмите F9, чтобы поставить на паузу и снова продолжить.




