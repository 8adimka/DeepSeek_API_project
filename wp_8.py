import os
import random
import signal
import subprocess
import sys
import tempfile
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

telegram_sender = None


class ClipboardSender:
    def __init__(self):
        load_dotenv()
        self.TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
        self.TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
        self.last_clipboard_content = ""
        self.keyboard = Controller()

    def send_to_telegram(self, message: str) -> bool:
        try:
            message = self.clean_telegram_message(message)

            if len(message) > 4000:
                messages = self.split_long_message(message)
                success = True
                for msg in messages:
                    if not self._send_single_message(msg):
                        success = False
                return success
            else:
                return self._send_single_message(message)

        except Exception:
            return False

    def _send_single_message(self, message: str) -> bool:
        try:
            response = requests.post(
                f"https://api.telegram.org/bot{self.TELEGRAM_BOT_TOKEN}/sendMessage",
                data={
                    "chat_id": self.TELEGRAM_CHAT_ID,
                    "text": message,
                },
                timeout=10,
            )
            response.raise_for_status()
            return True
        except Exception:
            return False

    def clean_telegram_message(self, text: str) -> str:
        text = "".join(char for char in text if char.isprintable() or char in "\n\r\t")
        return text

    def split_long_message(self, text: str, max_length: int = 4000) -> list:
        parts = []
        while text:
            if len(text) <= max_length:
                parts.append(text)
                break

            split_index = text.rfind("\n", 0, max_length)
            if split_index == -1:
                split_index = text.rfind(" ", 0, max_length)
            if split_index == -1:
                split_index = max_length

            parts.append(text[:split_index])
            text = text[split_index:].lstrip()

        return parts

    def copy_selected_text(self) -> bool:
        try:
            old_text = pyperclip.paste()

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

        with (
            open(os.devnull, "r") as si,
            open(os.devnull, "a+") as so,
            open(os.devnull, "a+") as se,
        ):
            os.dup2(si.fileno(), sys.stdin.fileno())
            os.dup2(so.fileno(), sys.stdout.fileno())
            os.dup2(se.fileno(), sys.stderr.fileno())


class AudioTranscriber:
    def __init__(self):
        load_dotenv()
        self.DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")
        self.is_recording = False
        self.ffmpeg_process = None
        self.audio_file = None
        self.recording_start_time = 0

    def detect_pulse_monitor(self):
        try:
            result = subprocess.run(
                ["pactl", "info"], capture_output=True, text=True, check=True
            )
            for line in result.stdout.splitlines():
                if line.startswith("Default Sink:"):
                    default_sink = line.split(":", 1)[1].strip()
                    return f"{default_sink}.monitor"
            return "alsa_output.pci-0000_00_1f.3.analog-stereo.monitor"
        except Exception:
            return "alsa_output.pci-0000_00_1f.3.analog-stereo.monitor"

    def start_recording(self):
        if self.is_recording:
            return

        self.is_recording = True
        self.recording_start_time = time.time()

        self.audio_file = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        self.audio_file.close()

        monitor_source = self.detect_pulse_monitor()

        cmd = [
            "ffmpeg",
            "-y",
            "-f",
            "pulse",
            "-i",
            monitor_source,
            "-ac",
            "1",
            "-ar",
            "16000",
            "-t",
            "60",
            "-loglevel",
            "error",
            self.audio_file.name,
        ]

        try:
            self.ffmpeg_process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
        except Exception:
            self.is_recording = False
            if self.audio_file is not None and os.path.exists(self.audio_file.name):
                os.unlink(self.audio_file.name)

    def stop_recording(self) -> Optional[str]:
        if not self.is_recording or not self.ffmpeg_process:
            return None

        self.is_recording = False
        recording_duration = time.time() - self.recording_start_time

        if recording_duration < 1.0:
            try:
                self.ffmpeg_process.terminate()
                self.ffmpeg_process.wait(timeout=1)
            except Exception:
                self.ffmpeg_process.kill()
            self.ffmpeg_process = None
            if self.audio_file is not None and os.path.exists(self.audio_file.name):
                os.unlink(self.audio_file.name)
            return None

        try:
            self.ffmpeg_process.terminate()
            stdout, stderr = self.ffmpeg_process.communicate(timeout=5)

            if (
                self.audio_file is not None
                and os.path.exists(self.audio_file.name)
                and os.path.getsize(self.audio_file.name) > 0
            ):
                transcript = self.transcribe_audio()
                if self.audio_file is not None and os.path.exists(self.audio_file.name):
                    os.unlink(self.audio_file.name)
                return transcript
            else:
                if self.audio_file is not None and os.path.exists(self.audio_file.name):
                    os.unlink(self.audio_file.name)

        except subprocess.TimeoutExpired:
            self.ffmpeg_process.kill()
            if self.audio_file is not None and os.path.exists(self.audio_file.name):
                os.unlink(self.audio_file.name)
        except Exception:
            if self.audio_file is not None and os.path.exists(self.audio_file.name):
                os.unlink(self.audio_file.name)

        self.ffmpeg_process = None
        return None

    def transcribe_audio(self) -> Optional[str]:
        if not self.audio_file or not os.path.exists(self.audio_file.name):
            return None

        file_size = os.path.getsize(self.audio_file.name)
        if file_size < 1000:
            return None

        if not self.DEEPGRAM_API_KEY:
            return None

        try:
            url = "https://api.deepgram.com/v1/listen"
            headers = {
                "Authorization": f"Token {self.DEEPGRAM_API_KEY}",
                "Content-Type": "audio/wav",
            }
            params = {
                "model": "nova-2",
                "language": "ru",
                "punctuate": "true",
                "sample_rate": 16000,
            }

            with open(self.audio_file.name, "rb") as audio_file:
                response = requests.post(
                    url, headers=headers, params=params, data=audio_file, timeout=30
                )

            response.raise_for_status()
            result = response.json()

            if (
                result.get("results")
                and result["results"].get("channels")
                and len(result["results"]["channels"]) > 0
            ):
                channel = result["results"]["channels"][0]
                if channel.get("alternatives") and len(channel["alternatives"]) > 0:
                    transcript = (
                        channel["alternatives"][0].get("transcript", "").strip()
                    )
                    if transcript:
                        return transcript

            return None

        except Exception:
            return None


class DeepSeekSolver:
    def __init__(self, telegram_sender_instance=None):
        self.API_URL = "https://api.deepseek.com/v1/chat/completions"
        self.API_KEY = self._get_api_key()
        self.last_request_time = 0
        self.RATE_LIMIT_DELAY = 3
        self.keyboard = Controller()
        self.dpy = display.Display()
        self.current_indent = 0
        self.prev_line_ended_with_colon = False
        self.telegram_sender = telegram_sender_instance

    def _get_api_key(self) -> str:
        load_dotenv()
        api_key = os.getenv("DEEPSEEK_API_KEY")
        if not api_key:
            raise RuntimeError("API ключ не найден")
        return api_key

    @tenacity.retry(
        wait=tenacity.wait_exponential(multiplier=1, min=4, max=10),
        stop=tenacity.stop_after_attempt(3),
    )
    def send_to_api(self, prompt: str, timeout: int = 60) -> Optional[str]:
        current_time = time.time()
        if current_time - self.last_request_time < self.RATE_LIMIT_DELAY:
            time.sleep(self.RATE_LIMIT_DELAY - (current_time - self.last_request_time))

        headers = {
            "Authorization": f"Bearer {self.API_KEY}",
            "Content-Type": "application/json",
        }
        data = {
            "model": "deepseek-chat",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
            "max_tokens": 800,
        }

        try:
            response = requests.post(
                self.API_URL, json=data, headers=headers, timeout=timeout
            )
            response.raise_for_status()
            self.last_request_time = time.time()
            result = response.json()
            return result["choices"][0]["message"]["content"]
        except Exception:
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

            lines = text.split("\n")
            if len(lines) < 2:
                return

            non_empty_lines = [line for line in lines[1:] if line.strip()]
            min_indent = (
                min((len(line) - len(line.lstrip()) for line in non_empty_lines))
                if non_empty_lines
                else 0
            )

            clean_lines = [lines[0]] + [
                line[min_indent:] if line.strip() else line for line in lines[1:]
            ]

            for i, line in enumerate(clean_lines[1:], start=1):
                if typing_paused:
                    while typing_paused:
                        time.sleep(0.1)
                        if not typing_active:
                            return

                if not line.strip():
                    self.keyboard.press(Key.enter)
                    self.keyboard.release(Key.enter)
                    time.sleep(random.uniform(0.3, 0.8))
                    continue

                stripped_line = line.lstrip()
                line_indent = len(line) - len(stripped_line)

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

                if i < len(clean_lines) - 1:
                    self.keyboard.press(Key.enter)
                    self.keyboard.release(Key.enter)
                    time.sleep(random.uniform(0.3, 0.9))

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

        solution = self.send_to_api(
            f"{task}\n\nProvide only the correct Python code solution without any comments, explanations or additional text. The code must be perfectly formatted with proper indentation (without extra spaces) and no typos. Return only the code.",
            timeout=30,
        )
        if solution:
            solution = solution.replace("```python", "").replace("```", "").strip()
            threading.Thread(target=self.human_like_typing, args=(solution,)).start()

    def process_interview_question(self, question: str) -> None:
        if not question or len(question.strip()) < 3:
            return

        prompt = f"""Подготовь краткий тезисный ответ на вопрос в контексте программирования на Python: {question}

Ответь по порядку, только ключевые пункты, без введения, заключения и лишних слов. Если потребуется писать код, то пиши его на Python."""

        answer = self.send_to_api(prompt, timeout=30)

        if answer and self.telegram_sender:
            self.telegram_sender.send_to_telegram("_______\n_______")
            self.telegram_sender.send_to_telegram(answer)


def toggle_typing_pause():
    global typing_paused
    typing_paused = not typing_paused


def run_daemon():
    global telegram_sender

    telegram_sender = ClipboardSender()

    solver = DeepSeekSolver(telegram_sender_instance=telegram_sender)
    transcriber = AudioTranscriber()

    num_lock_pressed = False

    def process_audio_question():
        question = transcriber.stop_recording()
        if question:
            solver.process_interview_question(question)

    def on_press(key):
        nonlocal num_lock_pressed

        try:
            if key == Key.f8:
                solver.process_task()
            elif key == Key.f9:
                toggle_typing_pause()
            elif key == Key.insert:
                if telegram_sender:
                    telegram_sender.process_clipboard()
            elif key == Key.num_lock:
                if not num_lock_pressed:
                    num_lock_pressed = True
                    transcriber.start_recording()

        except AttributeError:
            pass

    def on_release(key):
        nonlocal num_lock_pressed

        try:
            if key == Key.num_lock:
                if num_lock_pressed:
                    num_lock_pressed = False
                    threading.Thread(target=process_audio_question).start()

        except AttributeError:
            pass

    with Listener(on_press=on_press, on_release=on_release) as listener:
        listener.join()


if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        with DaemonContext(
            detach_process=True, umask=0o022, working_directory=os.path.expanduser("~")
        ):
            run_daemon()
    except Exception:
        sys.exit(1)
