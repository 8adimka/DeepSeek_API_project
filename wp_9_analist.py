import json
import os
import random
import signal
import subprocess
import sys
import threading
import time
from typing import Optional

import pyperclip
import requests
import tenacity
import websocket
from dotenv import load_dotenv
from pynput.keyboard import Controller, Key, Listener
from websocket import ABNF
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
        self._session = requests.Session()
        if self.TELEGRAM_BOT_TOKEN:
            self._base = f"https://api.telegram.org/bot{self.TELEGRAM_BOT_TOKEN}"
        else:
            self._base = None
        self._last_sent_hash = None
        self._last_sent_time = 0.0
        self._debounce_seconds = 2.0

    def send_to_telegram(self, message: str) -> bool:
        try:
            message = self.clean_telegram_message(message)
            h = hash(message)
            now = time.time()
            if (
                h == self._last_sent_hash
                and (now - self._last_sent_time) < self._debounce_seconds
            ):
                return False
            if len(message) > 4000:
                parts = self.split_long_message(message)
                ok = True
                for p in parts:
                    if not self._send_single_message(p):
                        ok = False
                if ok:
                    self._last_sent_hash = h
                    self._last_sent_time = now
                return ok
            else:
                ok = self._send_single_message(message)
                if ok:
                    self._last_sent_hash = h
                    self._last_sent_time = now
                return ok
        except Exception:
            return False

    def _send_single_message(self, message: str) -> bool:
        if not self._base or not self.TELEGRAM_CHAT_ID:
            return False
        try:
            r = self._session.post(
                f"{self._base}/sendMessage",
                data={"chat_id": self.TELEGRAM_CHAT_ID, "text": message},
                timeout=6,
            )
            r.raise_for_status()
            return True
        except Exception:
            return False

    def clean_telegram_message(self, text: str) -> str:
        return "".join(ch for ch in text if ch.isprintable() or ch in "\n\r\t")

    def split_long_message(self, text: str, max_length: int = 4000) -> list:
        parts = []
        while text:
            if len(text) <= max_length:
                parts.append(text)
                break
            idx = text.rfind("\n", 0, max_length)
            if idx == -1:
                idx = text.rfind(" ", 0, max_length)
            if idx == -1:
                idx = max_length
            parts.append(text[:idx])
            text = text[idx:].lstrip()
        return parts

    def copy_selected_text(self) -> bool:
        try:
            old = pyperclip.paste()
            with self.keyboard.pressed(Key.ctrl):
                self.keyboard.press("c")
                self.keyboard.release("c")
            time.sleep(0.15)
            new = pyperclip.paste()
            if new and new != old:
                self.last_clipboard_content = new
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


class AudioTranscriberRealtime:
    def __init__(self):
        load_dotenv()
        self.DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")
        self.is_recording = False
        self.ffmpeg_process = None
        self._ws_app = None
        self._ws_thread = None
        self._send_thread = None
        self._ws_connected = threading.Event()
        self._stop_sending = threading.Event()
        self._transcript_lock = threading.Lock()
        self._final_chunks = []
        self._partial = ""
        self._session = requests.Session()

    def detect_pulse_monitor(self):
        try:
            res = subprocess.run(
                ["pactl", "info"], capture_output=True, text=True, check=True
            )
            for line in res.stdout.splitlines():
                if line.startswith("Default Sink:"):
                    sink = line.split(":", 1)[1].strip()
                    return f"{sink}.monitor"
            return "alsa_output.pci-0000_00_1f.3.analog-stereo.monitor"
        except Exception:
            return "alsa_output.pci-0000_00_1f.3.analog-stereo.monitor"

    def _on_message(self, ws, message):
        try:
            data = json.loads(message)
        except Exception:
            return
        if not isinstance(data, dict):
            return
        if data.get("type") == "Results":
            alts = data.get("channel", {}).get("alternatives", [])
            if alts:
                alt = alts[0]
                text = alt.get("transcript", "").strip()
                is_final = data.get("is_final", False)
                with self._transcript_lock:
                    if is_final:
                        if text:
                            self._final_chunks.append(text)
                        self._partial = ""
                    else:
                        self._partial = text

    def _on_open(self, ws):
        self._ws_connected.set()

    def _on_close(self, ws, code, msg):
        self._ws_connected.clear()

    def _on_error(self, ws, err):
        self._ws_connected.clear()

    def start_recording(self):
        if self.is_recording:
            return
        self.is_recording = True
        self._final_chunks = []
        self._partial = ""
        self._stop_sending.clear()
        monitor = self.detect_pulse_monitor()
        cmd = [
            "ffmpeg",
            "-f",
            "pulse",
            "-i",
            monitor,
            "-ac",
            "1",
            "-ar",
            "16000",
            "-f",
            "s16le",
            "-loglevel",
            "error",
            "-",
        ]
        try:
            self.ffmpeg_process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
        except Exception:
            self.is_recording = False
            return

        url = "wss://api.deepgram.com/v1/listen?encoding=linear16&sample_rate=16000&channels=1&model=nova-2&language=ru&punctuate=true&interim_results=true&endpointing=300"
        headers = (
            [f"Authorization: Token {self.DEEPGRAM_API_KEY}"]
            if self.DEEPGRAM_API_KEY
            else []
        )
        self._ws_app = websocket.WebSocketApp(
            url,
            header=headers,
            on_message=self._on_message,
            on_open=self._on_open,
            on_close=self._on_close,
            on_error=self._on_error,
        )

        def run_ws():
            try:
                if self._ws_app:
                    self._ws_app.run_forever(ping_interval=5, ping_timeout=3)
            except Exception:
                pass

        self._ws_thread = threading.Thread(target=run_ws, daemon=True)
        self._ws_thread.start()

        if not self._ws_connected.wait(3):
            try:
                self.ffmpeg_process.kill()
            except Exception:
                pass
            self.is_recording = False
            return

        def send_audio():
            try:
                CHUNK = 4096
                while not self._stop_sending.is_set():
                    if not self.ffmpeg_process or not self.ffmpeg_process.stdout:
                        break
                    chunk = self.ffmpeg_process.stdout.read(CHUNK)
                    if not chunk:
                        break
                    try:
                        if self._ws_app and self._ws_connected.is_set():
                            self._ws_app.send(chunk, opcode=ABNF.OPCODE_BINARY)
                    except Exception:
                        break
                try:
                    if self._ws_app and self._ws_connected.is_set():
                        self._ws_app.send(json.dumps({"type": "CloseStream"}))
                    time.sleep(0.5)
                except Exception:
                    pass
            except Exception:
                pass

        self._send_thread = threading.Thread(target=send_audio, daemon=True)
        self._send_thread.start()

    def stop_recording(self) -> Optional[str]:
        if not self.is_recording:
            return None
        self.is_recording = False
        try:
            if self.ffmpeg_process:
                self.ffmpeg_process.terminate()
                self.ffmpeg_process.wait(timeout=2)
        except Exception:
            pass
        self._stop_sending.set()
        if self._send_thread and self._send_thread.is_alive():
            self._send_thread.join(timeout=2.0)
        time.sleep(0.5)
        try:
            if self._ws_app:
                self._ws_app.close()
        except Exception:
            pass
        try:
            if self._ws_thread and self._ws_thread.is_alive():
                self._ws_thread.join(timeout=0.5)
        except Exception:
            pass

        with self._transcript_lock:
            parts = list(self._final_chunks)
            if self._partial:
                parts.append(self._partial)
            final_text = " ".join(parts).strip()

        self._final_chunks = []
        self._partial = ""
        return final_text or None


# ==================== SQL-решатель (DeepSeek) ====================
class DeepSeekSQLSolver:
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
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {self.API_KEY}",
                "Content-Type": "application/json",
            }
        )

    def _get_api_key(self) -> str:
        load_dotenv()
        api_key = os.getenv("DEEPSEEK_API_KEY")
        if not api_key:
            raise RuntimeError("DEEPSEEK_API_KEY не найден")
        return api_key

    @tenacity.retry(wait=tenacity.wait_exponential(multiplier=1, min=4, max=10))
    def send_to_api(self, prompt: str) -> Optional[str]:
        current_time = time.time()
        if current_time - self.last_request_time < self.RATE_LIMIT_DELAY:
            time.sleep(self.RATE_LIMIT_DELAY - (current_time - self.last_request_time))
        data = {
            "model": "deepseek-chat",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
            "max_tokens": 2000,
        }
        try:
            response = self._session.post(self.API_URL, json=data, timeout=30)
            response.raise_for_status()
            self.last_request_time = time.time()
            code = response.json()["choices"][0]["message"]["content"]
            return code.replace("```sql", "").replace("```", "").strip()
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

                if i < len(clean_lines) - 1:
                    self.keyboard.press(Key.enter)
                    self.keyboard.release(Key.enter)
                    time.sleep(random.uniform(0.3, 0.9))

        finally:
            typing_active = False
            self.current_indent = 0

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

    def process_sql_task(self) -> None:
        if typing_active:
            return
        task = pyperclip.paste().strip()
        if not task:
            return
        prompt = (
            f"{task}\n\n"
            "Provide only the correct raw SQL code solution without any comments, explanations or additional text. "
            "The code must be perfectly formatted with proper indentation (without extra spaces) and no typos. "
            "Return only the code."
        )
        solution = self.send_to_api(prompt)
        if solution:
            threading.Thread(target=self.human_like_typing, args=(solution,)).start()


# ==================== Решатель вопросов системного аналитика (OpenAI) ====================
class SystemAnalystSolver:
    def __init__(self, telegram_sender_instance=None):
        load_dotenv()
        self.API_KEY = os.getenv("OPENAI_API_KEY")
        if not self.API_KEY:
            raise RuntimeError("OPENAI_API_KEY not found")
        self.telegram_sender = telegram_sender_instance
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {self.API_KEY}",
                "Content-Type": "application/json",
            }
        )
        self.API_URL = "https://api.openai.com/v1/chat/completions"
        self.last_request_time = 0.0
        self.RATE_LIMIT_DELAY = 0.5

    def send_to_api_streaming(self, prompt: str) -> None:
        now = time.time()
        if now - self.last_request_time < self.RATE_LIMIT_DELAY:
            time.sleep(self.RATE_LIMIT_DELAY - (now - self.last_request_time))

        payload = {
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 1500,
            "temperature": 0.3,
            "stream": True,
        }
        try:
            resp = self._session.post(
                self.API_URL, json=payload, timeout=60, stream=True
            )
            resp.raise_for_status()
        except Exception:
            return

        buffer = ""
        last_send = 0.0
        try:
            for raw_line in resp.iter_lines(decode_unicode=True):
                if not raw_line:
                    continue
                line = raw_line.strip()
                if not line or line == "[DONE]":
                    continue
                if line.startswith("data:"):
                    line = line[5:].strip()
                try:
                    chunk = json.loads(line)
                    piece = (
                        chunk.get("choices", [{}])[0]
                        .get("delta", {})
                        .get("content", "")
                    )
                except Exception:
                    continue

                if piece:
                    buffer += piece
                    now = time.time()
                    if (
                        len(buffer) >= 200
                        or "\n\n" in buffer
                        or (now - last_send) > 1.0
                    ):
                        if self.telegram_sender:
                            self.telegram_sender.send_to_telegram(buffer)
                        last_send = now
                        buffer = ""
            if buffer and self.telegram_sender:
                self.telegram_sender.send_to_telegram(buffer)
            self.last_request_time = time.time()
        except Exception:
            pass
        finally:
            try:
                resp.close()
            except Exception:
                pass

    def process_analyst_question(self, question: str) -> None:
        if not question or len(question.strip()) < 5:
            return
        prompt = (
            f"Ты — эксперт-системный аналитик с большим опытом. Ответь максимально профессионально, структурировано и по делу.\n"
            f"Вопрос: {question}\n\n"
            "Отвечай тезисно, без воды, но полно."
        )
        threading.Thread(
            target=self.send_to_api_streaming, args=(prompt,), daemon=True
        ).start()


def toggle_typing_pause():
    global typing_paused
    typing_paused = not typing_paused


def run_daemon():
    global telegram_sender
    telegram_sender = ClipboardSender()

    sql_solver = DeepSeekSQLSolver(telegram_sender_instance=telegram_sender)
    analyst_solver = SystemAnalystSolver(telegram_sender_instance=telegram_sender)
    transcriber = AudioTranscriberRealtime()

    num_lock_pressed = False

    def process_audio_question():
        question = transcriber.stop_recording()
        if question:
            analyst_solver.process_analyst_question(question)

    def on_press(key):
        nonlocal num_lock_pressed
        try:
            if key == Key.f8:
                sql_solver.process_sql_task()  # SQL-задачи
            elif key == Key.f9:
                toggle_typing_pause()  # Пауза печати
            elif key == Key.insert:
                if telegram_sender:
                    telegram_sender.process_clipboard()  # Копировать выделенное → Telegram
            elif key == Key.num_lock:
                if not num_lock_pressed:
                    num_lock_pressed = True
                    transcriber.start_recording()  # Начало записи голоса
        except AttributeError:
            pass

    def on_release(key):
        nonlocal num_lock_pressed
        try:
            if key == Key.num_lock:
                if num_lock_pressed:
                    num_lock_pressed = False
                    threading.Thread(target=process_audio_question, daemon=True).start()
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
    except Exception as e:
        print(f"Ошибка запуска демона: {e}")
        sys.exit(1)
