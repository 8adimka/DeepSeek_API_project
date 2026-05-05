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
        max_recent_entries: int = 8,
        max_tokens: int = 1000,
        summarization_threshold: int = 800,
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

        prompt = f"""Суммаризируй диалог системного аналитика. Сохрани важные моменты и сделай краткое резюме на русском.

Диалог:
{dialogue_text}

Краткое резюме:"""

        summary = self.solver.send_summarization(prompt)
        if summary:
            with self._summarization_lock:
                self.summary = summary.strip()
                self.last_summarization_time = time.time()
                keep_count = 2
                if len(self.recent_qa) > keep_count:
                    self.recent_qa = self.recent_qa[-keep_count:]
                    self._token_count = sum(e.tokens for e in self.recent_qa)
                    self._token_count += self._estimate_tokens(self.summary)

    def get_context_for_query(self, new_question: str) -> str:
        parts = []
        if self.summary:
            parts.append(f"Краткое резюме предыдущего обсуждения:\n{self.summary}\n")
        if self.recent_qa:
            parts.append("Последние вопросы и ответы:")
            for entry in self.recent_qa[-3:]:
                parts.append(f"В: {entry.question}")
                parts.append(f"О: {entry.answer}")
        return "\n".join(parts) if parts else ""

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

        if context:
            prompt = f"""Ты — эксперт-системный аналитик с большим опытом и ведёте обсуждение вот этой постановки - "*Бизнес-цель:*
В системе необходимо внедрить обязательное принятие Пользовательского соглашения (EULA) для всех пользователей. 

*Главное архитектурное правило:*
Соглашение должно отображаться строго на *отдельной промежуточной странице ДО выдачи JWT-токена*. 
Пользователь без акцептованного EULA не должен иметь технической возможности обращаться к защищенным методам API с валидным токеном.

----
В текущей итерации полноценный флоу показа EULA реализуется *только для триальных лицензий*. 
* Текст соглашения для триальной (_Trial_) лицензии готов: [^EULA_АП_демо_2026.docx].
* Для *коммерческих лицензий* (_Commercial_) применяется механизм *автоакцепта*: EULA пользователю не отображается, явное согласие не запрашивается. При авторизации на коммерческом портале (или при переходе с _Trial_ на _Commercial_) система должна автоматически фиксировать актуальную версию соглашения (_0_) в профиле пользователя и осуществлять бесшовный вход (сразу выдавать JWT-токен).
Разработка и внедрение юридического текста EULA для коммерческих лицензий отложены на следующие этапы.

h2. Сценарий работы пользователя:
#* Система должна поддерживать логику пропуска EULA для особой категории пользователей — «Публичный доступ» / «Гостевой вход».
#** Данные учетные записи используются для неаутентифицированного доступа внешних лиц (например, просмотр публичных отчетов) и являются общими.
#** Вход под такими учетными записями должен оставаться полностью прозрачным. 
Показ промежуточной страницы с текстом EULA и кнопками «Принять/Отказаться» для них *недопустим*.
#* Идентификация таких пользователей происходит на стороне сервера до этапа проверки версии EULA. 
Принадлежность к этой категории определяется специальным флагом/ролью в профиле учетной записи.

# Пользователь проходит первичную аутентификацию одним из доступных способов:
#* Логин/пароль
#* Любой Oauth-провайдер / LDAP
#* Bitrix-интеграция (кнопка «Перейти на портал») и т.д.
# После успешной проверки учетных данных, но *до генерации JWT*, система проверяет актуальность принятого соглашения.
# В зависимости от текущего типа лицензии портала (_Trial_ или _Commercial_), система определяет нужный набор полей для проверки. Сравнивается текущая мажорная версия EULA на портале (например, {{current_commercial_agreement_version}}) и версия, принятая пользователем ранее (соответственно, {{accepted_commercial_agreement_version}}):
#* *Если текущая версия выше принятой (или пользователь ещё не принял EULA):*
#*# Пользователь маршрутизируется на промежуточную транзитную страницу.
#*# Отображается полный текст EULA (язык в зависимости от текущей локали пользователя) и две кнопки: *«Принять»* и *«Отказаться»*.
#*#* При нажатии *«Отказаться»*: происходит полный _logout_. Пользователя перенаправлять на страницу «Доступ закрыт» (не на страницу авторизации напрямую), чтобы избежать бесконечного редиректа (в том числе внешних провайдеров (Битрикс, LDAP и др.)
#*#* При нажатии *«Принять»*: система сохраняет факт акцепта (обновляет версию в профиле), выдает JWT-токен и пускает пользователя на портал, кнопка «Принять» активируется только после того, как пользователь прокрутил текст до низа (подтверждение факта прочтения).
#* *Если версия актуальна* ({{current_commercial_agreement_version <= accepted_commercial_agreement_version}}): выдается токен, пользователь бесшовно перенаправляется на портал.

Если версия соглашения изменилась, но у пользователя есть действующий JWT-токен — он продолжает работать до следующего входа. Переподписание требуется только при следующей авторизации.
----


* *Хранение и проверка версий:*
** Реализовать целочисленное (мажорное) версионирование соглашения (1, 2, 3...). Минорные версии не поддерживаются. _Любое изменение или исправление = новая мажорная версия._
** В метаданных портала хранить актуальную версию EULA:
*** для типа лицензии _Commercial_ -> {{current_commercial_agreement_version}},
*** для типа лицензии _Trial_ -> {{current_trial_agreement_version}}.
** В профиле пользователя создать поля для хранения принятой версии: 
*** для типа лицензии _Commercial_ -> {{accepted_commercial_agreement_version}},
*** для типа лицензии _Trial_ -> {{accepted_trial_agreement_version}}.
** Сравнение версий должно осуществляться автоматически, с учётом типа лицензии, при каждом входе для *всех* провайдеров авторизации без исключений.

* *Смена типа лицензии (_Trial_ -> _Commercial)_:*
** Для _Trial_ и _Commercial (Full)_ версий используются разные тексты соглашения (задача: [https://jira.modusbi.ru/browse/MB-3968]).
** Когда тексты соглашений будут готовы, их необходимо _нормализовать_: добавить заголовки, переносы строк, разбивку на параграфы и зафиксировать, в каком формате бэк отдаёт текст (HTML, объект с полями title/paragraphs, или строка с разметкой).
** В случае смены типа лицензии (ключ _Trial_ -> _Commercial_) сравниваются разные поля ({{..._agreement_version}}) в профилях пользователей и в метаданных портала, что должно инициировать повторный показ соглашения в случае смена типа лицензии. Работа пользователей жестко блокируется (маршрутизация на страницу EULA при следующем входе) до принятия нового текста.

* *Политика White Label (WL):*
** Если портал работает по лицензионному ключу _White Label_, нужно *полностью отключить* механику показа нашего EULA.
** Реализация загрузки кастомных текстов соглашения партнером через админку сейчас *не требуется*, но архитектура должна закладывать возможность такого расширения в будущем.
----

* Разработать отдельную изолированную страницу просмотра и акцепта соглашения.
* Страница должна корректно и безопасно функционировать в "транзитном" состоянии — без наличия у клиента JWT-токена в LocalStorage/Cookie.
* *Локализация:* Текст соглашения и интерфейс страницы (кнопки) должны поддерживать несколько локалей и отображаться на языке текущей локали пользователя. Базовый минимум — *Русский и Английский*.
----

h2. Критерии приемки (AC):

* *AC 1: Первичный вход или обновление версии соглашения*
** *Дано:* Пользователь не имеет принятой версии EULA (или, например, {{accepted_commercial_agreement_version < current_commercial_agreement_version}}).
** *Когда:* Пользователь успешно проходит аутентификацию (любым способом).
** *Тогда:* JWT-токен не выдается. Пользователь перенаправляется на промежуточную страницу с текстом соглашения на языке его локали и кнопками «Принять» / «Отказаться».

* *AC 2: Успешный акцепт EULA*
** *Дано:* Пользователь находится на промежуточной странице EULA.
** *Когда:* Пользователь нажимает кнопку «Принять».
** *Тогда:* Поле {{..._agreement_version}}, в соответствии с типом лицензии, в профиле пользователя обновляется до актуального. Система выдает JWT-токен и перенаправляет пользователя на главную страницу портала.

* *AC 3: Отказ от принятия EULA*
** *Дано:* Пользователь находится на промежуточной странице EULA.
** *Когда:* Пользователь нажимает кнопку «Отказаться».
** *Тогда:* Токен не выдается. Текущая транзитная сессия уничтожается (_logout_), пользователь перенаправляется на страницу авторизации.

* *AC 4: Вход с уже актуальным соглашением*
** *Дано:* Соответствующее типу лицензии поле в профиле пользователя (например, {{accepted_commercial_agreement_version}}) равно или больше актуальной версии в метаданных портала.
** *Когда:* Пользователь авторизуется.
** *Тогда:* Промежуточная страница не показывается. Пользователь немедленно получает JWT-токен и переходит на портал (бесшовный вход).

* *AC 5: Смена ключа Trial на Full*
** *Дано:* Портал переведен с лицензии _Trial_ на _Commercial_. Пользователь ранее принимал "триальное" соглашение.
** *Когда:* Пользователь пытается авторизоваться (или его сессия истекает и он логинится заново).
** *Тогда:* Система распознает смену лицензии и принудительно направляет пользователя на страницу с новым текстом коммерческого EULA. Доступ к порталу заблокирован до акцепта.

* *AC 6: Работа в режиме White Label*
** *Дано:* Портал активирован ключом _White Label_.
** *Когда:* Любой пользователь (включая новых) успешно проходит первичную *аутентификация*.
** *Тогда:* Проверка EULA игнорируется. Пользователь получает токен и заходит на портал без отображения промежуточной страницы.

* *AC 7: Универсальность способов авторизации*
** *Дано:* Учтены различные провайдеры (любой Oauth, LDAP, интеграция с Bitrix и т.д.).
** *Когда:* Пользователь авторизуется через Bitrix по кнопке «Перейти на портал» (или через LDAP).
** *Тогда:* Логика перехвата сессии до выдачи токена и показа EULA отрабатывает идентично стандартной авторизации по логину/паролю.

* *AC 8: Пропуск EULA для учетной записи «Публичного доступа»*
** *Дано:* В системе существует предустановленная учетная запись, которая помечена признаком «Публичный доступ» (или техническая роль, освобожденная от подписания EULA). Портал работает по лицензии Trial или Commercial, для которых EULA является обязательным для обычных пользователей.
** *Когда:* Внешний пользователь (не имеющий персональной учетной записи) переходит по ссылке на просмотр публичного отчета. Система в фоновом режиме проводит аутентификацию под общей публичной учетной записью.
** *Тогда:* Система идентифицирует учетную запись как исключение. Промежуточная страница с текстом EULA и кнопками «Принять»/«Отказаться» не отображается. Пользователь немедленно получает валидный JWT-токен и бесшовно перенаправляется к целевому отчету. В профиле публичной учетной записи - запись о принятии версии EULA не создается и не обновляется.".

Разговор ведётся в контексте обратной совместимости для пользовательского соглашения.

Контекст предыдущего обсуждения:
{context}

Новый вопрос: {question}

Ответь тезисно, профессионально и структурировано. Отвечай без воды, но достаточно полно."""
        else:
            prompt = f"""Ты — эксперт-системный аналитик с большим опытом и ведёте обсуждение вот этой постановки - "*Бизнес-цель:*
В системе необходимо внедрить обязательное принятие Пользовательского соглашения (EULA) для всех пользователей. 

*Главное архитектурное правило:*
Соглашение должно отображаться строго на *отдельной промежуточной странице ДО выдачи JWT-токена*. 
Пользователь без акцептованного EULA не должен иметь технической возможности обращаться к защищенным методам API с валидным токеном.

----
В текущей итерации полноценный флоу показа EULA реализуется *только для триальных лицензий*. 
* Текст соглашения для триальной (_Trial_) лицензии готов: [^EULA_АП_демо_2026.docx].
* Для *коммерческих лицензий* (_Commercial_) применяется механизм *автоакцепта*: EULA пользователю не отображается, явное согласие не запрашивается. При авторизации на коммерческом портале (или при переходе с _Trial_ на _Commercial_) система должна автоматически фиксировать актуальную версию соглашения (_0_) в профиле пользователя и осуществлять бесшовный вход (сразу выдавать JWT-токен).
Разработка и внедрение юридического текста EULA для коммерческих лицензий отложены на следующие этапы.

h2. Сценарий работы пользователя:
#* Система должна поддерживать логику пропуска EULA для особой категории пользователей — «Публичный доступ» / «Гостевой вход».
#** Данные учетные записи используются для неаутентифицированного доступа внешних лиц (например, просмотр публичных отчетов) и являются общими.
#** Вход под такими учетными записями должен оставаться полностью прозрачным. 
Показ промежуточной страницы с текстом EULA и кнопками «Принять/Отказаться» для них *недопустим*.
#* Идентификация таких пользователей происходит на стороне сервера до этапа проверки версии EULA. 
Принадлежность к этой категории определяется специальным флагом/ролью в профиле учетной записи.

# Пользователь проходит первичную аутентификацию одним из доступных способов:
#* Логин/пароль
#* Любой Oauth-провайдер / LDAP
#* Bitrix-интеграция (кнопка «Перейти на портал») и т.д.
# После успешной проверки учетных данных, но *до генерации JWT*, система проверяет актуальность принятого соглашения.
# В зависимости от текущего типа лицензии портала (_Trial_ или _Commercial_), система определяет нужный набор полей для проверки. Сравнивается текущая мажорная версия EULA на портале (например, {{current_commercial_agreement_version}}) и версия, принятая пользователем ранее (соответственно, {{accepted_commercial_agreement_version}}):
#* *Если текущая версия выше принятой (или пользователь ещё не принял EULA):*
#*# Пользователь маршрутизируется на промежуточную транзитную страницу.
#*# Отображается полный текст EULA (язык в зависимости от текущей локали пользователя) и две кнопки: *«Принять»* и *«Отказаться»*.
#*#* При нажатии *«Отказаться»*: происходит полный _logout_. Пользователя перенаправлять на страницу «Доступ закрыт» (не на страницу авторизации напрямую), чтобы избежать бесконечного редиректа (в том числе внешних провайдеров (Битрикс, LDAP и др.)
#*#* При нажатии *«Принять»*: система сохраняет факт акцепта (обновляет версию в профиле), выдает JWT-токен и пускает пользователя на портал, кнопка «Принять» активируется только после того, как пользователь прокрутил текст до низа (подтверждение факта прочтения).
#* *Если версия актуальна* ({{current_commercial_agreement_version <= accepted_commercial_agreement_version}}): выдается токен, пользователь бесшовно перенаправляется на портал.

Если версия соглашения изменилась, но у пользователя есть действующий JWT-токен — он продолжает работать до следующего входа. Переподписание требуется только при следующей авторизации.
----


* *Хранение и проверка версий:*
** Реализовать целочисленное (мажорное) версионирование соглашения (1, 2, 3...). Минорные версии не поддерживаются. _Любое изменение или исправление = новая мажорная версия._
** В метаданных портала хранить актуальную версию EULA:
*** для типа лицензии _Commercial_ -> {{current_commercial_agreement_version}},
*** для типа лицензии _Trial_ -> {{current_trial_agreement_version}}.
** В профиле пользователя создать поля для хранения принятой версии: 
*** для типа лицензии _Commercial_ -> {{accepted_commercial_agreement_version}},
*** для типа лицензии _Trial_ -> {{accepted_trial_agreement_version}}.
** Сравнение версий должно осуществляться автоматически, с учётом типа лицензии, при каждом входе для *всех* провайдеров авторизации без исключений.

* *Смена типа лицензии (_Trial_ -> _Commercial)_:*
** Для _Trial_ и _Commercial (Full)_ версий используются разные тексты соглашения (задача: [https://jira.modusbi.ru/browse/MB-3968]).
** Когда тексты соглашений будут готовы, их необходимо _нормализовать_: добавить заголовки, переносы строк, разбивку на параграфы и зафиксировать, в каком формате бэк отдаёт текст (HTML, объект с полями title/paragraphs, или строка с разметкой).
** В случае смены типа лицензии (ключ _Trial_ -> _Commercial_) сравниваются разные поля ({{..._agreement_version}}) в профилях пользователей и в метаданных портала, что должно инициировать повторный показ соглашения в случае смена типа лицензии. Работа пользователей жестко блокируется (маршрутизация на страницу EULA при следующем входе) до принятия нового текста.

* *Политика White Label (WL):*
** Если портал работает по лицензионному ключу _White Label_, нужно *полностью отключить* механику показа нашего EULA.
** Реализация загрузки кастомных текстов соглашения партнером через админку сейчас *не требуется*, но архитектура должна закладывать возможность такого расширения в будущем.
----

* Разработать отдельную изолированную страницу просмотра и акцепта соглашения.
* Страница должна корректно и безопасно функционировать в "транзитном" состоянии — без наличия у клиента JWT-токена в LocalStorage/Cookie.
* *Локализация:* Текст соглашения и интерфейс страницы (кнопки) должны поддерживать несколько локалей и отображаться на языке текущей локали пользователя. Базовый минимум — *Русский и Английский*.
----

h2. Критерии приемки (AC):

* *AC 1: Первичный вход или обновление версии соглашения*
** *Дано:* Пользователь не имеет принятой версии EULA (или, например, {{accepted_commercial_agreement_version < current_commercial_agreement_version}}).
** *Когда:* Пользователь успешно проходит аутентификацию (любым способом).
** *Тогда:* JWT-токен не выдается. Пользователь перенаправляется на промежуточную страницу с текстом соглашения на языке его локали и кнопками «Принять» / «Отказаться».

* *AC 2: Успешный акцепт EULA*
** *Дано:* Пользователь находится на промежуточной странице EULA.
** *Когда:* Пользователь нажимает кнопку «Принять».
** *Тогда:* Поле {{..._agreement_version}}, в соответствии с типом лицензии, в профиле пользователя обновляется до актуального. Система выдает JWT-токен и перенаправляет пользователя на главную страницу портала.

* *AC 3: Отказ от принятия EULA*
** *Дано:* Пользователь находится на промежуточной странице EULA.
** *Когда:* Пользователь нажимает кнопку «Отказаться».
** *Тогда:* Токен не выдается. Текущая транзитная сессия уничтожается (_logout_), пользователь перенаправляется на страницу авторизации.

* *AC 4: Вход с уже актуальным соглашением*
** *Дано:* Соответствующее типу лицензии поле в профиле пользователя (например, {{accepted_commercial_agreement_version}}) равно или больше актуальной версии в метаданных портала.
** *Когда:* Пользователь авторизуется.
** *Тогда:* Промежуточная страница не показывается. Пользователь немедленно получает JWT-токен и переходит на портал (бесшовный вход).

* *AC 5: Смена ключа Trial на Full*
** *Дано:* Портал переведен с лицензии _Trial_ на _Commercial_. Пользователь ранее принимал "триальное" соглашение.
** *Когда:* Пользователь пытается авторизоваться (или его сессия истекает и он логинится заново).
** *Тогда:* Система распознает смену лицензии и принудительно направляет пользователя на страницу с новым текстом коммерческого EULA. Доступ к порталу заблокирован до акцепта.

* *AC 6: Работа в режиме White Label*
** *Дано:* Портал активирован ключом _White Label_.
** *Когда:* Любой пользователь (включая новых) успешно проходит первичную *аутентификация*.
** *Тогда:* Проверка EULA игнорируется. Пользователь получает токен и заходит на портал без отображения промежуточной страницы.

* *AC 7: Универсальность способов авторизации*
** *Дано:* Учтены различные провайдеры (любой Oauth, LDAP, интеграция с Bitrix и т.д.).
** *Когда:* Пользователь авторизуется через Bitrix по кнопке «Перейти на портал» (или через LDAP).
** *Тогда:* Логика перехвата сессии до выдачи токена и показа EULA отрабатывает идентично стандартной авторизации по логину/паролю.

* *AC 8: Пропуск EULA для учетной записи «Публичного доступа»*
** *Дано:* В системе существует предустановленная учетная запись, которая помечена признаком «Публичный доступ» (или техническая роль, освобожденная от подписания EULA). Портал работает по лицензии Trial или Commercial, для которых EULA является обязательным для обычных пользователей.
** *Когда:* Внешний пользователь (не имеющий персональной учетной записи) переходит по ссылке на просмотр публичного отчета. Система в фоновом режиме проводит аутентификацию под общей публичной учетной записью.
** *Тогда:* Система идентифицирует учетную запись как исключение. Промежуточная страница с текстом EULA и кнопками «Принять»/«Отказаться» не отображается. Пользователь немедленно получает валидный JWT-токен и бесшовно перенаправляется к целевому отчету. В профиле публичной учетной записи - запись о принятии версии EULA не создается и не обновляется.".

Разговор ведётся в контексте обратной совместимости для пользовательского соглашения.

Вопрос: {question}

Ответь тезисно, профессионально и структурировано. Отвечай без воды, но достаточно полно."""

        answer = self.send_to_api_streaming(prompt)

        if answer and self.context_manager:
            self.context_manager.add_qa(question, answer)


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

    def process_audio_question():
        question = transcriber.stop_recording()
        if question:
            analyst_solver.process_analyst_question(question)

    def on_press(key):
        nonlocal num_lock_pressed
        try:
            if key == Key.f8:
                sql_solver.process_sql_task()
            elif key == Key.f9:
                toggle_typing_pause()
            elif key == Key.insert:
                telegram_sender.process_clipboard()  # type: ignore
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
        print(f"Ошибка запуска: {e}")
        sys.exit(1)
