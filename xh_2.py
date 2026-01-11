import json
import os
import queue
import random
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, List, Optional

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


@dataclass
class QAEntry:
    question: str
    answer: str
    timestamp: float
    tokens: int = 0


class DialogueContextManager:
    def __init__(
        self,
        max_recent_entries: int = 10,
        max_tokens: int = 1200,
        summarization_threshold: int = 900,
        solver: Optional[Any] = None,
    ):
        self.max_recent_entries = max_recent_entries
        self.max_tokens = max_tokens
        self.summarization_threshold = summarization_threshold
        self.solver = solver
        self.recent_qa: List[QAEntry] = []
        self.summary: str = ""
        self.last_summarization_time: float = 0
        self.min_summarization_interval: float = 60
        self._token_count = 0
        self._summarization_lock = threading.Lock()
        self._summarization_queue = queue.Queue()
        self._summarization_thread = None
        self._start_summarization_worker()

    def _start_summarization_worker(self):
        def worker():
            while True:
                try:
                    qa_entries = self._summarization_queue.get(timeout=300)
                    if qa_entries is None:
                        break
                    self._perform_summarization(qa_entries)
                    self._summarization_queue.task_done()
                except queue.Empty:
                    continue

        self._summarization_thread = threading.Thread(target=worker, daemon=True)
        self._summarization_thread.start()

    def _estimate_tokens(self, text: str) -> int:
        return len(text.split()) + len(text) // 4

    def add_qa(self, question: str, answer: str) -> None:
        entry = QAEntry(
            question=question,
            answer=answer,
            timestamp=time.time(),
            tokens=self._estimate_tokens(question + answer),
        )
        self.recent_qa.append(entry)
        self._token_count += entry.tokens
        while len(self.recent_qa) > self.max_recent_entries:
            removed = self.recent_qa.pop(0)
            self._token_count -= removed.tokens
        self._check_summarization_needed()

    def _check_summarization_needed(self) -> None:
        current_time = time.time()
        if (
            self._token_count > self.summarization_threshold
            and current_time - self.last_summarization_time
            > self.min_summarization_interval
        ):
            qa_to_summarize = self.recent_qa.copy()
            try:
                self._summarization_queue.put_nowait(qa_to_summarize)
            except queue.Full:
                pass

    def _perform_summarization(self, qa_entries: List[QAEntry]) -> None:
        if not qa_entries or not self.solver:
            return

        dialogue_text = "\n".join(
            [f"Вопрос: {e.question}\nОтвет: {e.answer}\n" for e in qa_entries]
        )

        prompt = f"""Ты — системный аналитик, который ведет протокол встречи с Product Owner'ом.

Диалог:
{dialogue_text}

Создай структурированное резюме встречи, выделив:

1. **Основные решения и договорённости**
2. **Открытые вопросы** (что осталось неясным)
3. **Собранные требования** (что уже понятно)
4. **Следующие шаги** (что нужно уточнить на следующей встрече)

Формат резюме:
---
🎯 ОСНОВНЫЕ ДОГОВОРЁННОСТИ
• [Пункт 1]
• [Пункт 2]

⚠️ ОТКРЫТЫЕ ВОПРОСЫ
1. [Вопрос 1 - кто должен ответить, до какого срока]
2. [Вопрос 2 - кто должен ответить, до какого срока]

📋 СОБРАННЫЕ ТРЕБОВАНИЯ
• [Требование 1 - статус: подтверждено/предварительно]
• [Требование 2 - статус: подтверждено/предварительно]

➡️ СЛЕДУЮЩИЕ ШАГИ
• [Что сделать к следующей встрече]
• [Кому и что нужно подготовить]
---

Резюме должно быть конкретным, полезным для дальнейшей работы и написано на русском языке."""

        summary = self.solver.send_summarization(prompt)
        if summary:
            with self._summarization_lock:
                self.summary = summary.strip()
                self.last_summarization_time = time.time()
                if len(self.recent_qa) > 4:
                    self.recent_qa = self.recent_qa[-4:]
                    self._token_count = sum(e.tokens for e in self.recent_qa)
                    self._token_count += self._estimate_tokens(self.summary)

    def get_context_for_query(self, new_question: str) -> str:
        parts = []
        if self.summary:
            parts.append(f"📋 Резюме обсуждения:\n{self.summary}\n")
        if self.recent_qa:
            parts.append("🗣️ Последние реплики:")
            for entry in self.recent_qa[-4:]:
                parts.append(f"👤 PO/Я: {entry.question}")
                parts.append(f"🤖 Ассистент: {entry.answer}")
        return "\n".join(parts) if parts else ""

    def get_full_context(self) -> str:
        parts = []
        if self.summary:
            parts.append(f"📋 Резюме встречи:\n{self.summary}\n")
        if self.recent_qa:
            parts.append("🗣️ История диалога:")
            for entry in self.recent_qa:
                parts.append(f"👤 PO/Я: {entry.question}")
                parts.append(f"🤖 Ассистент: {entry.answer}")
        return "\n".join(parts) if parts else "История диалога пуста."

    def clear(self) -> None:
        self.recent_qa.clear()
        self.summary = ""
        self._token_count = 0


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
                if self._ws_app is not None:
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
                try:
                    self.ffmpeg_process.terminate()
                    self.ffmpeg_process.wait(timeout=2)
                except Exception:
                    try:
                        self.ffmpeg_process.kill()
                    except Exception:
                        pass
        except Exception:
            pass

        self._stop_sending.set()
        if self._send_thread and self._send_thread.is_alive():
            self._send_thread.join(timeout=2.0)
        time.sleep(0.5)
        try:
            if self._ws_app:
                try:
                    self._ws_app.close()
                except Exception:
                    pass
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


class DeepSeekSQLSolver:
    def __init__(self, telegram_sender_instance=None):
        self.API_URL = "https://api.deepseek.com/v1/chat/completions"
        self.API_KEY = self._get_api_key()
        self.last_request_time = 0
        self.RATE_LIMIT_DELAY = 3
        self.keyboard = Controller()
        self.dpy = display.Display()
        self.current_indent = 0
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
                    while typing_paused and typing_active:
                        time.sleep(0.1)

                if not line.strip():
                    self.keyboard.press(Key.enter)
                    self.keyboard.release(Key.enter)
                    time.sleep(random.uniform(0.3, 0.8))
                    continue

                stripped_line = line.lstrip()
                line_indent = len(line) - len(stripped_line)
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
                while typing_paused and typing_active:
                    time.sleep(0.1)
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


class SystemAnalystSolver:
    def __init__(
        self,
        telegram_sender_instance=None,
        context_manager: Optional[DialogueContextManager] = None,
    ):
        load_dotenv()
        self.API_KEY = os.getenv("OPENAI_API_KEY")
        if not self.API_KEY:
            raise RuntimeError("OPENAI_API_KEY not found")
        self.telegram_sender = telegram_sender_instance
        self.context_manager = context_manager
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

    def send_summarization(self, prompt: str) -> Optional[str]:
        payload = {
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 400,
            "temperature": 0.3,
        }
        try:
            r = self._session.post(self.API_URL, json=payload, timeout=30)
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"].strip()
        except Exception:
            return None

    def send_to_api_streaming(self, prompt: str) -> Optional[str]:
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
            return None

        buffer = ""
        full = []
        last_send = 0.0
        try:
            for raw_line in resp.iter_lines(decode_unicode=True):
                if not raw_line:
                    continue
                line = raw_line.strip()
                if not line or line in ("[DONE]", "data: [DONE]"):
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
                    full.append(piece)
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
            assembled = "".join(full).strip()
            self.last_request_time = time.time()
            return assembled if assembled else None
        except Exception:
            return None
        finally:
            try:
                resp.close()
            except Exception:
                pass

    def process_analyst_question(self, question: str) -> None:
        if not question or len(question.strip()) < 5:
            return

        context = (
            self.context_manager.get_context_for_query(question)
            if self.context_manager
            else ""
        )

        system_role = """Ты — опытный бизнес- и системный аналитик (10+ лет), который ведёт встречу с Product Owner'ом для сбора требований.
Твоя задача — помочь мне задавать правильные вопросы, чтобы получить полную информацию для создания качественной документации.
Ты должен мыслить как практикующий аналитик, который понимает, какие именно детали нужны для проектирования системы."""

        if context:
            prompt = f"""{system_role}

Текущий контекст обсуждения:
{context}

Новый вопрос или информация от Product Owner'а:
{question}

Проанализируй текущую ситуацию и:

1. **Резюме понимания**: Кратко (1-2 предложения) сформулируй, что мы уже выяснили
2. **Пробелы в информации**: Определи, какой информации не хватает для полного понимания требования
3. **Уточняющие вопросы**: Предложи 3-5 конкретных вопросов, которые нужно задать Product Owner'у прямо сейчас
4. **Готовые блоки**: Укажи, какие элементы уже можно документировать (если есть достаточно данных)

Формат ответа строго такой:

📌 **Понимание контекста**:
[1-2 предложения о том, что уже понятно]

⚠️ **Требует уточнения**:
• [пробел 1 - что именно неясно]
• [пробел 2 - какая информация отсутствует]
• [пробел 3 - какие допущения есть]

❓ **Вопросы к Product Owner'у** (от наиболее важных):
1. [Бизнес-логика/Данные/UX/Интеграции/НФТ] Вопрос, который поможет закрыть пробел...
2. [Бизнес-логика/Данные/UX/Интеграции/НФТ] Вопрос...
3. [Бизнес-логика/Данные/UX/Интеграции/НФТ] Вопрос...

✅ **Готово к документированию**:
• [Таблица "Название"] - если достаточно данных о структуре
• [API-метод "Название"] - если понятны все параметры
• [Процесс "Название"] - если описан бизнес-процесс
• [—] - если ничего не готово

ВАЖНО: Будь максимально конкретным. Не задавай абстрактных вопросов, а предлагай четкие, закрытые вопросы там, где это возможно.
Фокусируйся на получении информации, которая нужна для конкретных артефактов документации."""
        else:
            prompt = f"""{system_role}

Новая тема/вопрос от Product Owner'а:
{question}

Начни сбор требований. Проанализируй эту тему и:

1. Определи основные направления для уточнения
2. Предложи первоочередные вопросы для понимания масштаба и контекста
3. Наметь структуру информации, которую нужно собрать

Формат ответа:

🎯 **Тема обсуждения**:
[Кратко обозначь основную тему]

🔍 **Области для выяснения**:
1. Бизнес-цель и ценность
2. Пользователи и их роли
3. Основной функционал
4. Данные и их источники
5. Ограничения и требования

❓ **Первые вопросы к Product Owner'у**:
1. Какова основная бизнес-цель этой функции? Какие метрики улучшим?
2. Кто основные пользователи и как они будут использовать эту функцию?
3. Есть ли аналоги в текущей системе или у конкурентов?
4. Какие данные участвуют в процессе? Откуда они берутся?
5. Какие сроки и ограничения по реализации?

✅ **Следующий шаг**: После ответа на эти вопросы мы сможем перейти к детальному уточнению.

Начинай с общего понимания, затем углубляйся в детали."""

        answer = self.send_to_api_streaming(prompt)

        if answer and self.context_manager:
            self.context_manager.add_qa(question, answer)

    def suggest_documentation_structure(self) -> str:
        """Предложение структуры документации на основе накопленных требований"""
        if not self.context_manager:
            return "Контекстный менеджер не инициализирован"

        accumulated_data = self.context_manager.get_full_context()

        prompt = f"""На основе собранных требований:
        {accumulated_data}
        
        Предложи структуру итоговой документации. Укажи, какие разделы нужно создать, и что в них должно быть.
        
        Формат:
        📑 СТРУКТУРА ДОКУМЕНТАЦИИ
        
        1. БИЗНЕС-ТРЕБОВАНИЯ (BRD)
           • Цели и метрики
           • Пользователи и роли
           • Бизнес-процессы
           • Ограничения
        
        2. ФУНКЦИОНАЛЬНЫЕ ТРЕБОВАНИЯ (SRS)
           • Пользовательские сценарии
           • Системные функции
           • Правила бизнес-логики
        
        3. ТЕХНИЧЕСКИЕ ТРЕБОВАНИЯ
           • API-спецификации
           • Структура БД
           • Интеграции
        
        4. НЕФУНКЦИОНАЛЬНЫЕ ТРЕБОВАНИЯ
           • Производительность
           • Безопасность
           • Масштабируемость
        
        🎯 ПРИОРИТЕТЫ ДОКУМЕНТИРОВАНИЯ:
        1. [Что документировать в первую очередь]
        2. [Что можно отложить]
        3. [Что требует дополнительных уточнений]"""

        return self.send_to_api_streaming(prompt)

    def check_requirements_completeness(self) -> str:
        """Проверка, достаточно ли информации для начала документирования"""
        if not self.context_manager:
            return "Контекстный менеджер не инициализирован"

        accumulated_data = self.context_manager.get_full_context()

        prompt = f"""Ты — старший системный аналитик, проверяющий полноту собранных требований.

Собранная информация:
{accumulated_data}

Проанализируй, достаточно ли информации для создания следующих артефактов:

1. **Таблицы БД** — есть ли все поля, типы, ограничения?
2. **API-методы** — понятны ли все параметры, ответы, ошибки?
3. **Бизнес-процессы** — описан ли основной поток и исключения?
4. **Пользовательские сценарии** — понятны ли все шаги и роли?

Для каждого артефакта определи:
✅ ГОТОВ — можно начинать документирование
⚠️ ЧАСТИЧНО ГОТОВ — нужны небольшие уточнения
❌ НЕ ГОТОВ — нужна дополнительная встреча

Формат ответа:

📊 АНАЛИЗ ПОЛНОТЫ ТРЕБОВАНИЙ
---
🗄️ ТАБЛИЦЫ БД
• [Название таблицы] — статус (✅/⚠️/❌)
  Примечание: [что именно нужно уточнить]

🌐 API-МЕТОДЫ
• [Название метода] — статус (✅/⚠️/❌)
  Примечание: [что именно нужно уточнить]

🔄 БИЗНЕС-ПРОЦЕССЫ
• [Название процесса] — статус (✅/⚠️/❌)
  Примечание: [что именно нужно уточнить]

👥 ПОЛЬЗОВАТЕЛЬСКИЕ СЦЕНАРИИ
• [Название сценария] — статус (✅/⚠️/❌)
  Примечание: [что именно нужно уточнить]

🎯 РЕКОМЕНДАЦИИ
• [Что делать дальше — встреча/уточнение/документирование]"""

        return self.send_to_api_streaming(prompt)

    def finalize_requirements_gathering(self) -> str:
        """Финальный анализ собранных требований"""
        if not self.context_manager:
            return "Контекстный менеджер не инициализирован"

        all_context = self.context_manager.get_full_context()

        prompt = f"""Ты завершаешь сбор требований с Product Owner'ом.

Вся собранная информация:
{all_context}

Проанализируй и:
1. Составь итоговый список подтверждённых требований
2. Выдели все допущения и риски
3. Предложи план документирования
4. Определи, что нужно утвердить у PO перед началом разработки

Формат:

🏁 ИТОГ ВСТРЕЧИ ПО СБОРУ ТРЕБОВАНИЙ
---
✅ ПОДТВЕРЖДЁННЫЕ ТРЕБОВАНИЯ
[Список с приоритетами]

⚠️ ДОПУЩЕНИЯ И ОТКРЫТЫЕ ВОПРОСЫ
[Что мы предположили, что требует проверки]

📋 ПЛАН ДОКУМЕНТИРОВАНИЯ
1. [Документ 1] - срок, ответственный
2. [Документ 2] - срок, ответственный
3. [Документ 3] - срок, ответственный

🎯 НА УТВЕРЖДЕНИЕ PO
• [Требование 1 - требует формального согласования]
• [Требование 2 - требует формального согласования]

➡️ СЛЕДУЮЩИЕ ШАГИ
• [Что делать дальше]
• [Кого информировать о результатах]"""

        return self.send_to_api_streaming(prompt)


def toggle_typing_pause():
    global typing_paused
    typing_paused = not typing_paused


def run_daemon():
    global telegram_sender

    context_manager = DialogueContextManager()
    telegram_sender = ClipboardSender()

    sql_solver = DeepSeekSQLSolver(telegram_sender_instance=telegram_sender)
    analyst_solver = SystemAnalystSolver(
        telegram_sender_instance=telegram_sender, context_manager=context_manager
    )
    context_manager.solver = analyst_solver

    transcriber = AudioTranscriberRealtime()
    num_lock_pressed = False
    ctrl_pressed = False

    def send_to_telegram_with_prefix(message: str, prefix: str = "📋"):
        """Отправить сообщение в Telegram с префиксом"""
        if (
            telegram_sender
            and message
            and message != "Контекстный менеджер не инициализирован"
        ):
            formatted_message = f"{prefix}\n{message}"
            telegram_sender.send_to_telegram(formatted_message)

    def process_audio_question():
        question = transcriber.stop_recording()
        if question:
            analyst_solver.process_analyst_question(question)

    def on_press(key):
        nonlocal num_lock_pressed, ctrl_pressed
        try:
            if key in (Key.ctrl_l, Key.ctrl_r):
                ctrl_pressed = True
                return

            if key == Key.num_lock:
                if not num_lock_pressed:
                    num_lock_pressed = True
                    if not ctrl_pressed:  # Только если Ctrl не нажат
                        transcriber.start_recording()
                return

            if ctrl_pressed and not num_lock_pressed:
                if key == Key.f1:
                    threading.Thread(
                        target=lambda: send_to_telegram_with_prefix(
                            analyst_solver.check_requirements_completeness(),
                            "📊 Анализ полноты требований",
                        ),
                        daemon=True,
                    ).start()
                elif key == Key.f2:
                    threading.Thread(
                        target=lambda: send_to_telegram_with_prefix(
                            analyst_solver.suggest_documentation_structure(),
                            "📑 Структура документации",
                        ),
                        daemon=True,
                    ).start()
                elif key == Key.f3:
                    threading.Thread(
                        target=lambda: send_to_telegram_with_prefix(
                            analyst_solver.finalize_requirements_gathering(),
                            "🏁 Итоги сбора требований",
                        ),
                        daemon=True,
                    ).start()
                return

            if key == Key.f8:
                sql_solver.process_sql_task()
            elif key == Key.f9:
                toggle_typing_pause()
            elif key == Key.insert:
                telegram_sender.process_clipboard()

        except AttributeError:
            pass

    def on_release(key):
        nonlocal num_lock_pressed, ctrl_pressed
        try:
            if key in (Key.ctrl_l, Key.ctrl_r):
                ctrl_pressed = False
            elif key == Key.num_lock:
                if num_lock_pressed:
                    num_lock_pressed = False
                    if not ctrl_pressed:  # Только если Ctrl не нажат
                        threading.Thread(
                            target=process_audio_question, daemon=True
                        ).start()
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
        print(f"Ошибка запуска: {e}")
        sys.exit(1)
