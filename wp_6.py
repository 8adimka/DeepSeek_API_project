import os
import random
import signal
import sys
import threading
import time
from typing import Optional

import pyperclip
import requests
import tenacity
from dotenv import load_dotenv
from pynput.keyboard import Controller, Key, Listener
from Xlib import X, display

typing_paused = False
typing_active = False


def signal_handler(sig, frame):
    sys.exit(0)


class DaemonContext:
    def __init__(self, detach_process=True, umask=0o022, working_directory="/"):
        self.detach = detach_process
        self.umask = umask
        self.workdir = working_directory

    def __enter__(self):
        if self.detach:
            self._daemonize()
        os.chdir(self.workdir)
        os.umask(self.umask)
        return self

    def __exit__(self, *args):
        pass

    def _daemonize(self):
        try:
            if os.fork() > 0:
                sys.exit(0)
        except OSError:
            sys.exit(1)

        os.setsid()
        os.umask(0)

        try:
            if os.fork() > 0:
                sys.exit(0)
        except OSError:
            sys.exit(1)

        sys.stdout.flush()
        sys.stderr.flush()

        with open(os.devnull, "r") as si, open(os.devnull, "a+") as so, open(
            os.devnull, "a+"
        ) as se:
            os.dup2(si.fileno(), sys.stdin.fileno())
            os.dup2(so.fileno(), sys.stdout.fileno())
            os.dup2(se.fileno(), sys.stderr.fileno())


class DeepSeekSolver:
    def __init__(self):
        self.API_URL = "https://api.deepseek.com/v1/chat/completions"
        self.API_KEY = self._get_api_key()
        self.last_request_time = 0
        self.RATE_LIMIT_DELAY = 3
        self.keyboard = Controller()
        self.dpy = display.Display()
        self.current_indent = 0
        self.prev_line_ended_with_colon = False

    def _get_api_key(self) -> str:
        load_dotenv()
        api_key = os.getenv("DEEPSEEK_API_KEY")
        if not api_key:
            raise RuntimeError("API ключ не найден")
        return api_key

    @tenacity.retry(wait=tenacity.wait_exponential(multiplier=1, min=4, max=10))
    def send_to_api(self, prompt: str) -> Optional[str]:
        current_time = time.time()
        if current_time - self.last_request_time < self.RATE_LIMIT_DELAY:
            time.sleep(self.RATE_LIMIT_DELAY - (current_time - self.last_request_time))

        headers = {
            "Authorization": f"Bearer {self.API_KEY}",
            "Content-Type": "application/json",
        }
        data = {
            "model": "deepseek-chat",
            "messages": [
                {
                    "role": "user",
                    "content": f"{prompt}\n\nProvide only the correct Python code solution without any comments, explanations or additional text. The code must be perfectly formatted with proper indentation (without extra spaces) and no typos. Return only the code.",
                }
            ],
            "temperature": 0.2,
            "max_tokens": 2000,
        }

        try:
            response = requests.post(
                self.API_URL, json=data, headers=headers, timeout=30
            )
            response.raise_for_status()
            self.last_request_time = time.time()
            code = response.json()["choices"][0]["message"]["content"]
            return code.replace("```python", "").replace("```", "").strip()
        except requests.exceptions.RequestException:
            return None

    def human_like_typing(self, text: str) -> None:
        global typing_paused, typing_active

        if not text or typing_active:
            return

        typing_active = True
        self.current_indent = 0
        self.prev_line_ended_with_colon = False

        try:
            window = self.dpy.get_input_focus().focus
            if window:
                window.set_input_focus(X.RevertToParent, X.CurrentTime)
                self.dpy.flush()

            # Сохраняем все строки, включая пустые
            lines = text.split("\n")
            if len(lines) < 2:
                return

            # Вычисляем минимальный отступ только для непустых строк (кроме первой)
            non_empty_lines = [line for line in lines[1:] if line.strip()]
            min_indent = (
                min((len(line) - len(line.lstrip()) for line in non_empty_lines))
                if non_empty_lines
                else 0
            )

            # Первую строку оставляем как есть, остальные обрабатываем
            clean_lines = [lines[0]] + [
                line[min_indent:] if line.strip() else line for line in lines[1:]
            ]

            for i, line in enumerate(clean_lines[1:], start=1):
                if typing_paused:
                    while typing_paused:
                        time.sleep(0.1)
                        if not typing_active:
                            return

                # Просто печатаем пустые строки без обработки
                if not line.strip():
                    self.keyboard.press(Key.enter)
                    self.keyboard.release(Key.enter)
                    time.sleep(random.uniform(0.3, 0.8))
                    continue

                stripped_line = line.lstrip()
                line_indent = len(line) - len(stripped_line)

                # Для первой печатаемой строки пропускаем отступы
                if i > 1:
                    while self.current_indent < line_indent:
                        if typing_paused:
                            time.sleep(0.1)
                            continue
                        self.keyboard.press(Key.space)
                        self.keyboard.release(Key.space)
                        self.current_indent += 1
                        time.sleep(0.05)

                self._type_line(stripped_line)

                if typing_paused:
                    while typing_paused:
                        time.sleep(0.1)
                        if not typing_active:
                            return

                # Не добавляем Enter после последней строки
                if i < len(clean_lines) - 1:
                    self.keyboard.press(Key.enter)
                    self.keyboard.release(Key.enter)
                    time.sleep(random.uniform(0.3, 0.9))

                # Обновляем состояние для логических блоков
                self.prev_line_ended_with_colon = line.rstrip().endswith(":")
                if self.prev_line_ended_with_colon:
                    self.current_indent += 4
                elif stripped_line.startswith(("return", "break", "continue", "pass")):
                    self.current_indent = max(0, self.current_indent - 4)

        finally:
            typing_active = False
            self.current_indent = 0
            self.prev_line_ended_with_colon = False

    def _type_line(self, line: str) -> None:
        global typing_paused
        time.sleep(random.uniform(0.1, 0.2))
        word_buffer = ""

        for char in line:
            if typing_paused:
                while typing_paused:
                    time.sleep(0.1)
                    if not typing_active:
                        return

            word_buffer += char

            if char.isspace():
                if len(word_buffer.strip()) > 3 and random.random() < 0.3:
                    time.sleep(random.uniform(0.4, 0.8))
                word_buffer = ""

            delay = random.gauss(0.14, 0.08)
            delay = min(max(0.08, delay), 0.27)
            time.sleep(delay)

            self.keyboard.press(char)
            self.keyboard.release(char)

            # Имитация опечаток только для длинных слов
            if random.random() < 0.02 and char.isalpha() and len(word_buffer) > 6:
                wrong_char = chr(ord(char) + random.randint(-1, 1))
                self.keyboard.press(wrong_char)
                self.keyboard.release(wrong_char)
                time.sleep(0.1)
                self.keyboard.press(Key.backspace)
                self.keyboard.release(Key.backspace)
                time.sleep(0.1)
                self.keyboard.press(char)
                self.keyboard.release(char)
                time.sleep(0.1)

    def process_task(self) -> None:
        if typing_active:
            return

        task = pyperclip.paste().strip()
        if not task:
            return

        solution = self.send_to_api(task)
        if solution:
            threading.Thread(target=self.human_like_typing, args=(solution,)).start()


def toggle_typing_pause():
    global typing_paused
    typing_paused = not typing_paused


def run_daemon():
    solver = DeepSeekSolver()

    def on_press(key):
        try:
            if key == Key.f8:
                solver.process_task()
            elif key == Key.f9:
                toggle_typing_pause()
        except AttributeError:
            pass

    with Listener(on_press=on_press) as listener:
        listener.join()


if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        with DaemonContext(
            detach_process=True, umask=0o022, working_directory=os.path.expanduser("~")
        ):
            run_daemon()
    except RuntimeError:
        sys.exit(1)
