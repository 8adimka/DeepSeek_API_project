import os
import signal
import sys
import time
from typing import Optional

import pyperclip
import requests
from dotenv import load_dotenv
from pynput.keyboard import Controller, Key, Listener


class ClipboardSender:
    def __init__(self):
        load_dotenv()
        self.TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
        self.TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
        self.last_clipboard_content = ""
        self.keyboard = Controller()  # Добавлено для эмуляции Ctrl+C

    def send_to_telegram(self, message: str) -> None:
        try:
            requests.post(
                f"https://api.telegram.org/bot{self.TELEGRAM_BOT_TOKEN}/sendMessage",
                data={
                    "chat_id": self.TELEGRAM_CHAT_ID,
                    "text": message,
                },
                timeout=5,
            )
        except Exception:
            pass

    def copy_selected_text(self) -> bool:
        """Копирует выделенный текст через эмуляцию Ctrl+C."""
        try:
            old_text = pyperclip.paste()

            # Эмулируем Ctrl+C
            with self.keyboard.pressed(Key.ctrl):
                self.keyboard.press("c")
                self.keyboard.release("c")

            time.sleep(0.3)
            new_text = pyperclip.paste()

            if new_text and new_text != old_text:
                self.last_clipboard_content = new_text
                return True
            return False
        except Exception:
            return False

    def process_clipboard(self) -> None:
        if self.copy_selected_text() and self.last_clipboard_content:
            self.send_to_telegram(self.last_clipboard_content)


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


def run_daemon():
    sender = ClipboardSender()

    def on_press(key):
        try:
            if key == Key.f8:
                sender.process_clipboard()
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
