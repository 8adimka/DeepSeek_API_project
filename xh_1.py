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
        max_recent_entries: int = 12,
        max_tokens: int = 1400,
        summarization_threshold: int = 1000,
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
        prompt = f"""Ты — старший системный аналитик, ведущий протокол встречи с Product Owner'ом.

Диалог:
{dialogue_text}

Создай максимально полезное и структурированное резюме на русском языке:

---
TARGET ОСНОВНЫЕ ДОГОВОРЁННОСТИ
• [Ключевые решения и подтверждённые факты]

WARNING ОТКРЫТЫЕ ВОПРОСЫ
1. [Что осталось неясным — кому задать — до какого срока]

PACKAGE СОБРАННЫЕ ТРЕБОВАНИЯ
• [Подтверждённое требование — статус: подтверждено/предварительно]

RIGHT-ARROW СЛЕДУЮЩИЕ ШАГИ
• [Что нужно уточнить на следующей встрече]
• [Кто и что должен подготовить]
---
Резюме должно быть конкретным, с чёткими действиями и ответственными."""
        summary = self.solver.send_summarization(prompt)
        if summary:
            with self._summarization_lock:
                self.summary = summary.strip()
                self.last_summarization_time = time.time()
                if len(self.recent_qa) > 5:
                    self.recent_qa = self.recent_qa[-5:]
                    self._token_count = sum(e.tokens for e in self.recent_qa)
                    self._token_count += self._estimate_tokens(self.summary)

    def get_full_context(self) -> str:
        parts = []
        if self.summary:
            parts.append(f"RESUME Резюме встречи:\n{self.summary}\n")
        if self.recent_qa:
            parts.append("SPEECH История диалога:")
            for e in self.recent_qa:
                parts.append(f"USER PO/Я: {e.question}")
                parts.append(f"ROBOT Ассистент: {e.answer}")
        return "\n".join(parts) if parts else "Контекст пуст."

    def get_context_for_query(self, new_question: str) -> str:
        parts = []
        if self.summary:
            parts.append(f"RESUME Резюме обсуждения:\n{self.summary}\n")
        if self.recent_qa:
            parts.append("SPEECH Последние реплики:")
            for entry in self.recent_qa[-4:]:
                parts.append(f"USER PO/Я: {entry.question}")
                parts.append(f"ROBOT Ассистент: {entry.answer}")
        return "\n".join(parts) if parts else ""


class ClipboardSender:
    def __init__(self):
        load_dotenv()
        self.TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
        self.TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
        self.keyboard = Controller()
        self._session = requests.Session()
        self._base = (
            f"https://api.telegram.org/bot{self.TELEGRAM_BOT_TOKEN}"
            if self.TELEGRAM_BOT_TOKEN
            else None
        )
        self._last_sent_hash = None
        self._last_sent_time = 0.0
        self._debounce_seconds = 2.0

    def send_to_telegram(self, message: str) -> bool:
        if not message or not self._base or not self.TELEGRAM_CHAT_ID:
            return False
        try:
            message = self.clean_telegram_message(message)
            h = hash(message)
            now = time.time()
            if (
                h == self._last_sent_hash
                and (now - self._last_sent_time) < self._debounce_seconds
            ):
                return False

            if len(message) <= 4000:
                success = self._send_single_message(message)
            else:
                parts = self.split_long_message(message)
                success = True
                for i, part in enumerate(parts, 1):
                    prefixed = f"[Часть {i}/{len(parts)}]\n{part}"
                    if not self._send_single_message(prefixed):
                        success = False

            if success:
                self._last_sent_hash = h
                self._last_sent_time = now
            return success
        except Exception:
            return False

    def _send_single_message(self, message: str) -> bool:
        try:
            r = self._session.post(
                f"{self._base}/sendMessage",
                data={
                    "chat_id": self.TELEGRAM_CHAT_ID,
                    "text": message,
                    "parse_mode": "HTML",
                },
                timeout=10,
            )
            r.raise_for_status()
            return True
        except Exception:
            return False

    def clean_telegram_message(self, text: str) -> str:
        return "".join(ch for ch in text if ch.isprintable() or ch in "\n\r\t")

    def split_long_message(self, text: str, max_length: int = 3900) -> list:
        lines = text.splitlines()
        parts = []
        current = []
        current_len = 0
        for line in lines:
            if current_len + len(line) + 1 > max_length:
                parts.append("\n".join(current))
                current = [line]
                current_len = len(line) + 1
            else:
                current.append(line)
                current_len += len(line) + 1
        if current:
            parts.append("\n".join(current))
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
        if self.copy_selected_text():
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
                    return f"{line.split(':', 1)[1].strip()}.monitor"
            return "alsa_output.pci-0000_00_1f.3.analog-stereo.monitor"
        except Exception:
            return "alsa_output.pci-0000_00_1f.3.analog-stereo.monitor"

    def _on_message(self, ws, message):
        try:
            data = json.loads(message)
            if data.get("type") == "Results":
                alts = data.get("channel", {}).get("alternatives", [])
                if alts:
                    text = alts[0].get("transcript", "").strip()
                    is_final = data.get("is_final", False)
                    with self._transcript_lock:
                        if is_final:
                            if text:
                                self._final_chunks.append(text)
                            self._partial = ""
                        else:
                            self._partial = text
        except Exception:
            pass

    def _on_open(self, ws):
        self._ws_connected.set()

    def _on_close(self, ws, *args):
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

        self._ws_thread = threading.Thread(
            target=lambda: self._ws_app.run_forever(ping_interval=5, ping_timeout=3),
            daemon=True,
        )
        self._ws_thread.start()

        if not self._ws_connected.wait(3):
            if self.ffmpeg_process:
                self.ffmpeg_process.kill()
            self.is_recording = False
            return

        def send_audio():
            try:
                while (
                    not self._stop_sending.is_set()
                    and self.ffmpeg_process
                    and self.ffmpeg_process.stdout
                ):
                    chunk = self.ffmpeg_process.stdout.read(4096)
                    if not chunk:
                        break
                    if self._ws_app and self._ws_connected.is_set():
                        self._ws_app.send(chunk, opcode=ABNF.OPCODE_BINARY)
                if self._ws_app and self._ws_connected.is_set():
                    self._ws_app.send(json.dumps({"type": "CloseStream"}))
            except Exception:
                pass

        self._send_thread = threading.Thread(target=send_audio, daemon=True)
        self._send_thread.start()

    def stop_recording(self) -> Optional[str]:
        if not self.is_recording:
            return None
        self.is_recording = False

        if self.ffmpeg_process:
            self.ffmpeg_process.terminate()
            try:
                self.ffmpeg_process.wait(timeout=2)
            except Exception:
                self.ffmpeg_process.kill()

        self._stop_sending.set()
        if self._send_thread and self._send_thread.is_alive():
            self._send_thread.join(timeout=2)

        time.sleep(0.5)
        if self._ws_app:
            self._ws_app.close()
        if self._ws_thread and self._ws_thread.is_alive():
            self._ws_thread.join(timeout=0.5)

        with self._transcript_lock:
            text = " ".join(
                self._final_chunks + ([self._partial] if self._partial else [])
            ).strip()

        self._final_chunks = []
        self._partial = ""
        return text or None


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
        now = time.time()
        if now - self.last_request_time < self.RATE_LIMIT_DELAY:
            time.sleep(self.RATE_LIMIT_DELAY - (now - self.last_request_time))

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

            non_empty = [l for l in lines[1:] if l.strip()]
            min_indent = min((len(l) - len(l.lstrip()) for l in non_empty), default=0)
            clean_lines = [lines[0]] + [
                l[min_indent:] if l.strip() else l for l in lines[1:]
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

                indent = len(line) - len(line.lstrip())
                while self.current_indent < indent:
                    self.keyboard.press(Key.space)
                    self.keyboard.release(Key.space)
                    self.current_indent += 1
                    time.sleep(0.05)

                self._type_line(line.lstrip())
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
        word = ""
        for ch in line:
            if typing_paused:
                while typing_paused and typing_active:
                    time.sleep(0.1)
            word += ch
            if ch.isspace():
                if len(word.strip()) > 3 and random.random() < 0.3:
                    time.sleep(random.uniform(0.4, 0.8))
                word = ""
            delay = random.gauss(0.14, 0.08)
            delay = max(0.08, min(0.27, delay))
            time.sleep(delay)
            self.keyboard.press(ch)
            self.keyboard.release(ch)

    def process_sql_task(self) -> None:
        if typing_active:
            return
        task = pyperclip.paste().strip()
        if not task:
            return
        prompt = f"{task}\n\nProvide only the correct raw SQL code solution without any comments, explanations or additional text. The code must be perfectly formatted with proper indentation (without extra spaces) and no typos. Return only the code."
        solution = self.send_to_api(prompt)
        if solution:
            threading.Thread(
                target=self.human_like_typing, args=(solution,), daemon=True
            ).start()


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
            return assembled
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

        system_role = """Ты — эксперт бизнес- и системный аналитик с 12+ годами опыта в продуктовых компаниях.
Ты ведёшь встречу с Product Owner'ом и помогаешь собрать 100% требований для BRD, SRS, API, БД, BPMN.
Ты знаешь, какие детали критичны, и никогда не оставляешь пробелов."""

        if context:
            prompt = f"""{system_role}

Текущий контекст:
{context}

Последняя реплика от PO или от меня:
{question}

Проанализируй и ответь строго по шаблону:

PINNED **Что уже понятно**
• [1–2 предложения — чёткое резюме]

WARNING **Пробелы и риски**
• [что неясно, какие допущения сделаны]

QUESTION **Вопросы к Product Owner'у (по приоритету)**
1. [Тема] Конкретный закрытый вопрос…
2. [Тема] …
3. [Тема] …

CHECKMARK **Готово к документированию**
• Таблица «Users», «Payments»
• API «POST /orders/create»
• Процесс «Оформление заказа»
• — (если ничего не готово)

Будь максимально конкретным. Задавай только те вопросы, которые закроют реальные пробелы в документации."""
        else:
            prompt = f"""{system_role}
Новая тема от Product Owner'а:
{question}
Определи основную цель и задай первые приоритетные вопросы:
1. Бизнес-цель и метрики успеха
2. Пользователи и сценарии
3. Ключевые данные и ограничения
4. Интеграции и сроки"""

        answer = self.send_to_api_streaming(prompt)
        if answer and self.context_manager:
            self.context_manager.add_qa(question, answer)

    def check_requirements_completeness(self):
        context = self.context_manager.get_full_context()
        prompt = f"""CHECKMARK Проверь полноту требований

Вся информация:
{context}

Для каждого артефакта:
TABLES ТАБЛИЦЫ БД
• [Таблица] — ГОТОВ / ЧАСТИЧНО / НЕ ГОТОВ (что уточнить)

API API-МЕТОДЫ
• [Метод] — ГОТОВ / ЧАСТИЧНО / НЕ ГОТОВ

PROCESS БИЗНЕС-ПРОЦЕССЫ
• [Процесс] — ГОТОВ / ЧАСТИЧНО / НЕ ГОТОВ

RECOMMENDATION РЕКОМЕНДАЦИИ
• Что можно документировать сейчас
• Какие вопросы задать на следующей встрече"""
        self.send_to_api_streaming(prompt)

    def suggest_documentation_structure(self):
        context = self.context_manager.get_full_context()
        prompt = f"""FILE-CABINET Предложи структуру документации

На основе:
{context}

STRUCTURE СТРУКТУРА ДОКУМЕНТАЦИИ
1. BRD → цели, пользователи, бизнес-процессы
2. SRS → use cases, правила
3. API → методы, схемы
4. DB → таблицы, связи
5. BPMN → диаграммы

PRIORITY ПРИОРИТЕТЫ
1. [Что делать первым]
2. [Что можно отложить]"""
        self.send_to_api_streaming(prompt)

    def finalize_requirements_gathering(self):
        context = self.context_manager.get_full_context()
        prompt = f"""TROPHY ИТОГИ ВСТРЕЧИ

Вся информация:
{context}

CHECKMARK ПОДТВЕРЖДЁННЫЕ ТРЕБОВАНИЯ
• [список]

WARNING ДОПУЩЕНИЯ И РИСКИ
• [что предположили]

CLIPBOARD ПЛАН ДОКУМЕНТИРОВАНИЯ
1. [Документ] — срок

SHIELD НА УТВЕРЖДЕНИЕ У PO
• [требование]

RIGHT-ARROW СЛЕДУЮЩИЕ ШАГИ"""
        self.send_to_api_streaming(prompt)


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
                    if not ctrl_pressed:
                        transcriber.start_recording()
                return

            if ctrl_pressed and not num_lock_pressed:
                if key == Key.num_1:
                    threading.Thread(
                        target=analyst_solver.check_requirements_completeness,
                        daemon=True,
                    ).start()
                elif key == Key.num_2:
                    threading.Thread(
                        target=analyst_solver.suggest_documentation_structure,
                        daemon=True,
                    ).start()
                elif key == Key.num_3:
                    threading.Thread(
                        target=analyst_solver.finalize_requirements_gathering,
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
                    if not ctrl_pressed:
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
