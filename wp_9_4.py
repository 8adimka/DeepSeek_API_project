import json
import logging
import os
import random
import signal
import subprocess
import sys
import threading
import time
from typing import Any, Optional

import pyperclip
import requests
import tenacity
import websocket
from dotenv import load_dotenv
from pynput.keyboard import Controller, Key, Listener
from websocket import ABNF
from Xlib import X, display

logging.basicConfig(
    filename="daemon.log",
    level=logging.DEBUG,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

typing_paused = False
typing_active = False
telegram_sender = None


class ClipboardSender:
    def __init__(self):
        logging.debug("Initializing ClipboardSender.")
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
        logging.debug(f"Attempting to send to Telegram: {message[:50]}...")
        try:
            message = self.clean_telegram_message(message)
            h = hash(message)
            now = time.time()
            if (
                h == self._last_sent_hash
                and (now - self._last_sent_time) < self._debounce_seconds
            ):
                logging.debug("Message debounced.")
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
                logging.debug(f"Split message sent: {ok}")
                return ok
            else:
                ok = self._send_single_message(message)
                if ok:
                    self._last_sent_hash = h
                    self._last_sent_time = now
                logging.debug(f"Single message sent: {ok}")
                return ok
        except Exception as e:
            logging.error(f"Error sending to Telegram: {e}")
            return False

    def _send_single_message(self, message: str) -> bool:
        if not self._base or not self.TELEGRAM_CHAT_ID:
            logging.warning("Telegram base or chat ID missing.")
            return False
        try:
            r = self._session.post(
                f"{self._base}/sendMessage",
                data={"chat_id": self.TELEGRAM_CHAT_ID, "text": message},
                timeout=6,
            )
            r.raise_for_status()
            logging.debug("Message sent successfully.")
            return True
        except Exception as e:
            logging.error(f"Error in single message send: {e}")
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
        logging.debug("Attempting to copy selected text.")
        try:
            old = pyperclip.paste()
            with self.keyboard.pressed(Key.ctrl):
                self.keyboard.press("c")
                self.keyboard.release("c")
            time.sleep(0.15)
            new = pyperclip.paste()
            if new and new != old:
                self.last_clipboard_content = new
                logging.debug("Text copied successfully.")
                return True
            logging.debug("No new text copied.")
            return False
        except Exception as e:
            logging.error(f"Error copying text: {e}")
            return False

    def process_clipboard(self) -> None:
        logging.debug("Processing clipboard.")
        if self.copy_selected_text() and self.last_clipboard_content:
            self.send_to_telegram(self.last_clipboard_content)


def signal_handler(sig, frame):
    logging.info("Signal received, exiting.")
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
        logging.debug("Daemonizing process.")
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
        logging.debug("Initializing AudioTranscriberRealtime.")
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
        logging.debug("Detecting pulse monitor.")
        try:
            res = subprocess.run(
                ["pactl", "info"], capture_output=True, text=True, check=True
            )
            for line in res.stdout.splitlines():
                if line.startswith("Default Sink:"):
                    sink = line.split(":", 1)[1].strip()
                    logging.debug(f"Detected monitor: {sink}.monitor")
                    return f"{sink}.monitor"
            logging.debug("Using default monitor.")
            return "alsa_output.pci-0000_00_1f.3.analog-stereo.monitor"
        except Exception as e:
            logging.error(f"Error detecting monitor: {e}")
            return "alsa_output.pci-0000_00_1f.3.analog-stereo.monitor"

    def _on_message(self, ws, message):
        logging.debug(f"WebSocket message received: {message[:50]}...")
        try:
            data = json.loads(message)
        except Exception as e:
            logging.error(f"Error parsing message: {e}")
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
                            logging.debug(f"Final chunk added: {text}")
                        self._partial = ""
                    else:
                        self._partial = text
                        logging.debug(f"Partial transcript: {text}")

    def _on_open(self, ws):
        logging.debug("WebSocket opened.")
        self._ws_connected.set()

    def _on_close(self, ws, code, msg):
        logging.debug(f"WebSocket closed: code={code}, msg={msg}")
        self._ws_connected.clear()

    def _on_error(self, ws, err):
        logging.error(f"WebSocket error: {err}")
        self._ws_connected.clear()

    def start_recording(self):
        logging.debug("Starting recording.")
        if self.is_recording:
            logging.warning("Already recording.")
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
            logging.debug("FFmpeg started.")
        except Exception as e:
            logging.error(f"Error starting FFmpeg: {e}")
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
            logging.debug("Starting WebSocket thread.")
            try:
                if self._ws_app is not None:
                    self._ws_app.run_forever(ping_interval=5, ping_timeout=3)
            except Exception as e:
                logging.error(f"WebSocket thread error: {e}")

        self._ws_thread = threading.Thread(target=run_ws, daemon=True)
        self._ws_thread.start()

        if not self._ws_connected.wait(3):
            logging.error("WebSocket connection timeout.")
            try:
                self.ffmpeg_process.kill()
            except Exception:
                pass
            self.is_recording = False
            return

        def send_audio():
            logging.debug("Starting audio send thread.")
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
                            logging.debug("Audio chunk sent.")
                    except Exception as e:
                        logging.error(f"Error sending audio chunk: {e}")
                        break
                try:
                    if self._ws_app and self._ws_connected.is_set():
                        self._ws_app.send(json.dumps({"type": "CloseStream"}))
                        logging.debug("CloseStream signal sent.")
                    time.sleep(0.5)  # Give time for final results
                except Exception as e:
                    logging.error(f"Error sending close stream: {e}")
            except Exception as e:
                logging.error(f"Audio send thread error: {e}")

        self._send_thread = threading.Thread(target=send_audio, daemon=True)
        self._send_thread.start()

    def stop_recording(self) -> Optional[str]:
        logging.debug("Stopping recording.")
        if not self.is_recording:
            logging.warning("Not recording.")
            return None
        self.is_recording = False
        try:
            if self.ffmpeg_process:
                try:
                    self.ffmpeg_process.terminate()
                    self.ffmpeg_process.wait(timeout=2)
                    logging.debug("FFmpeg terminated.")
                except Exception as e:
                    logging.error(f"Error terminating FFmpeg: {e}")
                    try:
                        self.ffmpeg_process.kill()
                        logging.debug("FFmpeg killed.")
                    except Exception as e:
                        logging.error(f"Error killing FFmpeg: {e}")
        except Exception as e:
            logging.error(f"General error stopping FFmpeg: {e}")

        self._stop_sending.set()
        if self._send_thread and self._send_thread.is_alive():
            self._send_thread.join(timeout=2.0)
            logging.debug("Send thread joined.")
        time.sleep(0.5)  # Allow time for final transcription
        try:
            if self._ws_app:
                try:
                    self._ws_app.close()
                    logging.debug("WebSocket closed.")
                except Exception as e:
                    logging.error(f"Error closing WebSocket: {e}")
        except Exception as e:
            logging.error(f"General error closing WebSocket: {e}")

        try:
            if self._ws_thread and self._ws_thread.is_alive():
                self._ws_thread.join(timeout=0.5)
                logging.debug("WS thread joined.")
        except Exception as e:
            logging.error(f"Error joining threads: {e}")

        with self._transcript_lock:
            parts = list(self._final_chunks)
            if self._partial:
                parts.append(self._partial)
            final_text = " ".join(parts).strip()
        self._final_chunks = []
        self._partial = ""
        logging.debug(f"Final transcript: {final_text}")
        return final_text or None


def _extract_text_from_obj(obj: Any) -> str:
    out = []
    if obj is None:
        return ""
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict):
        for k in ("content", "text", "value", "message", "transcript"):
            if k in obj and isinstance(obj[k], str):
                out.append(obj[k])
        if "content" in obj and isinstance(obj["content"], list):
            for item in obj["content"]:
                out.append(_extract_text_from_obj(item))
        if "choices" in obj and isinstance(obj["choices"], list):
            for ch in obj["choices"]:
                out.append(_extract_text_from_obj(ch))
        if "delta" in obj:
            out.append(_extract_text_from_obj(obj["delta"]))
        for v in obj.values():
            out.append(_extract_text_from_obj(v))
    elif isinstance(obj, list):
        for it in obj:
            out.append(_extract_text_from_obj(it))
    return " ".join(
        [s for s in (x.strip() for x in out if isinstance(x, str) and x.strip()) if s]
    )


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


class OpenAISolver:
    def __init__(self, telegram_sender_instance=None):
        logging.debug("Initializing OpenAISolver.")
        load_dotenv()
        self.API_KEY = os.getenv("OPENAI_API_KEY")
        if not self.API_KEY:
            logging.error("OPENAI_API_KEY not found.")
            raise RuntimeError("OPENAI_API_KEY not found in environment")
        self.last_request_time = 0.0
        self.RATE_LIMIT_DELAY = 0.5
        self.keyboard = Controller()
        try:
            self.dpy = display.Display()
        except Exception as e:
            logging.error(f"Error initializing display: {e}")
            self.dpy = None
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
        self.API_URL = "https://api.openai.com/v1/chat/completions"

    def _post_with_retries(
        self, payload: dict, timeout: int = 10, attempts: int = 2, stream: bool = False
    ):
        logging.debug(f"Sending API request with payload: {json.dumps(payload)}")
        for attempt in range(attempts):
            try:
                r = self._session.post(
                    self.API_URL, json=payload, timeout=timeout, stream=stream
                )
                r.raise_for_status()
                logging.debug("API request successful.")
                return r
            except Exception as e:
                logging.error(f"API request error on attempt {attempt + 1}: {e}")
                if attempt + 1 < attempts:
                    time.sleep(0.3)
                else:
                    return None

    def send_to_api(
        self, prompt: str, timeout: int = 8, max_tokens: int = 300
    ) -> Optional[str]:
        logging.debug(f"Sending to API: {prompt[:50]}...")
        now = time.time()
        since = now - self.last_request_time
        if since < self.RATE_LIMIT_DELAY:
            time.sleep(self.RATE_LIMIT_DELAY - since)
        payload = {
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": 0.0,
        }
        r = self._post_with_retries(payload, timeout=timeout, attempts=2, stream=False)
        if not r:
            logging.error("API response is None.")
            return None
        try:
            data = r.json()
            txt = data["choices"][0]["message"]["content"]
            self.last_request_time = time.time()
            logging.debug(f"API response text: {txt[:50]}...")
            return txt.strip() or None
        except Exception as e:
            logging.error(f"Error parsing API response: {e}")
            return None

    def send_to_api_streaming(
        self, prompt: str, buffer_chars: int = 200, min_send_interval: float = 1.0
    ) -> Optional[str]:
        logging.debug(f"Starting streaming API: {prompt[:50]}...")
        now = time.time()
        since = now - self.last_request_time
        if since < self.RATE_LIMIT_DELAY:
            time.sleep(self.RATE_LIMIT_DELAY - since)

        payload = {
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 800,
            "temperature": 0.0,
            "stream": True,
        }

        resp = self._post_with_retries(payload, timeout=60, attempts=2, stream=True)
        if not resp:
            logging.error("Streaming API response is None.")
            return None

        buffer = ""
        full = []
        last_send = 0.0

        try:
            for raw_line in resp.iter_lines(decode_unicode=True):
                if not raw_line:
                    continue
                line = raw_line.strip()
                if line == "" or line == b"":
                    continue
                if line.startswith("data:"):
                    line = line[len("data:") :].strip()
                if line == "[DONE]":
                    break
                try:
                    chunk_json = json.loads(line)
                except Exception as e:
                    logging.error(f"Error parsing chunk: {e}")
                    continue
                piece = (
                    chunk_json.get("choices", [{}])[0]
                    .get("delta", {})
                    .get("content", "")
                )
                if piece:
                    buffer += piece
                    full.append(piece)
                    nowt = time.time()
                    if (
                        len(buffer) >= buffer_chars
                        or "\n\n" in buffer
                        or (nowt - last_send) > min_send_interval
                    ):
                        if self.telegram_sender:
                            sent = self.telegram_sender.send_to_telegram(buffer)
                            if sent:
                                logging.debug("Buffer sent to Telegram.")
                            else:
                                logging.error("Failed to send buffer to Telegram.")
                        last_send = nowt
                        buffer = ""
            if buffer:
                if self.telegram_sender:
                    sent = self.telegram_sender.send_to_telegram(buffer)
                    if sent:
                        logging.debug("Final buffer sent to Telegram.")
                    else:
                        logging.error("Failed to send final buffer to Telegram.")
            assembled = "".join(full).strip()
            if assembled:
                self.last_request_time = time.time()
                logging.debug(f"Streaming assembled: {assembled[:50]}...")
                return assembled
            return None
        except Exception as e:
            logging.error(f"Streaming error: {e}")
            return None
        finally:
            try:
                resp.close()
            except Exception as e:
                logging.error(f"Error closing response: {e}")

    def _start_stream_send_background(self, prompt: str):
        logging.debug("Starting background streaming.")

        def job():
            self.send_to_api_streaming(prompt)

        t = threading.Thread(target=job, daemon=True)
        t.start()

    def human_like_typing(self, text: str) -> None:
        logging.debug(f"Starting human-like typing: {text[:50]}...")
        global typing_paused, typing_active
        if not text or typing_active:
            return
        typing_active = True
        self.current_indent = 0
        self.prev_line_ended_with_colon = False
        try:
            if self.dpy:
                try:
                    w = self.dpy.get_input_focus().focus
                    if w:
                        w.set_input_focus(X.RevertToParent, X.CurrentTime)
                        self.dpy.flush()
                except Exception as e:
                    logging.error(f"Error setting input focus: {e}")
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

        except Exception as e:
            logging.error(f"Error in human-like typing: {e}")
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
        logging.debug("Processing task.")
        if typing_active:
            logging.warning("Typing active, skipping.")
            return
        t = pyperclip.paste().strip()
        if not t:
            logging.debug("No task in clipboard.")
            return
        prompt = (
            f"{t}\n\nProvide only the correct Python code solution without any comments, explanations or additional text. "
            "The code must be perfectly formatted with proper indentation (without extra spaces) and no typos. Return only the code."
        )
        sol = self.send_to_api(prompt, timeout=7, max_tokens=300)
        if sol:
            sol = sol.replace("```python", "").replace("```", "").strip()
            threading.Thread(
                target=self.human_like_typing, args=(sol,), daemon=True
            ).start()
        else:
            logging.error("No solution from API.")

    def process_interview_question(self, question: str) -> None:
        logging.debug(f"Processing interview question: {question}")
        if not question or len(question.strip()) < 3:
            logging.warning("Question too short.")
            return
        prompt = (
            f"Подготовь краткий тезисный ответ на вопрос в контексте программирования на Python: {question}\n\n"
            "Ответь по порядку, только ключевые пункты, без введения, заключения и лишних слов. Если потребуется писать код, то пиши его на Python."
        )
        self._start_stream_send_background(prompt)


def toggle_typing_pause():
    global typing_paused
    typing_paused = not typing_paused
    logging.debug(f"Typing pause toggled to: {typing_paused}")


def run_daemon():
    logging.info("Starting daemon.")
    global telegram_sender
    telegram_sender = ClipboardSender()
    solver = OpenAISolver(telegram_sender_instance=telegram_sender)
    transcriber = AudioTranscriberRealtime()
    num_lock_pressed = False

    def process_audio_question():
        logging.debug("Processing audio question.")
        question = transcriber.stop_recording()
        if question:
            logging.debug(f"Got question: {question}")
            solver.process_interview_question(question)
        else:
            logging.warning("No question from transcription.")

    def on_press(key):
        nonlocal num_lock_pressed
        try:
            if key == Key.f8:
                logging.debug("F8 pressed - process task.")
                solver.process_task()
            elif key == Key.f9:
                logging.debug("F9 pressed - toggle pause.")
                toggle_typing_pause()
            elif key == Key.insert:
                logging.debug("Insert pressed - process clipboard.")
                if telegram_sender:
                    telegram_sender.process_clipboard()
            elif key == Key.num_lock:
                logging.debug("NumLock pressed - start recording.")
                if not num_lock_pressed:
                    num_lock_pressed = True
                    transcriber.start_recording()
        except AttributeError:
            pass

    def on_release(key):
        nonlocal num_lock_pressed
        try:
            if key == Key.num_lock:
                logging.debug("NumLock released - stop recording.")
                if num_lock_pressed:
                    num_lock_pressed = False
                    threading.Thread(target=process_audio_question, daemon=True).start()
        except AttributeError:
            pass

    with Listener(on_press=on_press, on_release=on_release) as listener:
        logging.info("Listener started.")
        listener.join()


if __name__ == "__main__":
    logging.info("Program starting.")
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    try:
        with DaemonContext(
            detach_process=True, umask=0o022, working_directory=os.path.expanduser("~")
        ):
            run_daemon()
    except Exception as e:
        logging.error(f"Fatal error: {e}")
        sys.exit(1)
