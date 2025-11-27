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

import google.generativeai as genai
import pyperclip
import requests
import websocket
from dotenv import load_dotenv
from pynput.keyboard import Controller, Key, Listener
from websocket import ABNF

# Xlib и display оставлены, но импорт не используется в GeminiSolver, только в human_like_typing
from Xlib import X, display

# --- Настройки ---
# Удалено все логирование (logging)


typing_paused = False
typing_active = False
telegram_sender = None

# --- Загрузка окружения с привязкой к пути скрипта ---
script_dir = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(script_dir, ".env"))


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
        max_tokens: int = 2000,
        summarization_threshold: int = 1500,
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
                except Exception:
                    pass  # Удалено логирование

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

        prompt = f"""Суммаризируй диалог системного аналитика. Сохрани важные технические детали (SQL, API, требования) и сделай краткое резюме на русском.

Диалог:
{dialogue_text}

Краткое резюме:"""

        try:
            summary = self.solver.generate_summary_text(prompt)
            if summary:
                with self._summarization_lock:
                    self.summary = summary.strip()
                    self.last_summarization_time = time.time()
                    # Оставляем последние 2 записи полными, остальное в саммари
                    keep_count = 2
                    if len(self.recent_qa) > keep_count:
                        self.recent_qa = self.recent_qa[-keep_count:]
                        self._token_count = sum(e.tokens for e in self.recent_qa)
                        self._token_count += self._estimate_tokens(self.summary)
        except Exception:
            pass  # Удалено логирование

    def get_context_for_query(self, new_question: str) -> str:
        parts = []
        if self.summary:
            parts.append(f"Резюме прошлого контекста:\n{self.summary}\n")
        if self.recent_qa:
            parts.append("Последние Q&A:")
            for entry in self.recent_qa:
                parts.append(f"Q: {entry.question}")
                parts.append(f"A: {entry.answer}")
        return "\n".join(parts) if parts else ""


class ClipboardSender:
    def __init__(self):
        self.TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
        self.TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
        self.last_clipboard_content = ""
        self.keyboard = Controller()
        self._session = requests.Session()
        if self.TELEGRAM_BOT_TOKEN:
            self._base = f"https://api.telegram.org/bot{self.TELEGRAM_BOT_TOKEN}"
        else:
            self._base = None
            # Удалено логирование

    def send_to_telegram(self, message: str) -> bool:
        if not message or not message.strip():
            return False
        try:
            clean_msg = self.clean_telegram_message(message)
            if len(clean_msg) > 4000:
                parts = self.split_long_message(clean_msg)
                ok = True
                for p in parts:
                    if not self._send_single_message(p):
                        ok = False
                    time.sleep(0.5)  # Небольшая пауза между частями
                return ok
            else:
                return self._send_single_message(clean_msg)
        except Exception:
            # Удалено логирование
            return False

    def _send_single_message(self, message: str) -> bool:
        if not self._base or not self.TELEGRAM_CHAT_ID:
            return False
        try:
            r = self._session.post(
                f"{self._base}/sendMessage",
                data={"chat_id": self.TELEGRAM_CHAT_ID, "text": message},
                timeout=10,
            )
            r.raise_for_status()
            return True
        except Exception:
            # Удалено логирование
            return False

    def clean_telegram_message(self, text: str) -> str:
        # Простая очистка, можно расширить
        return str(text)

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
            # Очищаем буфер перед копированием для надежности
            pyperclip.copy("")
            with self.keyboard.pressed(Key.ctrl):
                self.keyboard.press("c")
                self.keyboard.release("c")
            time.sleep(0.15)
            new = pyperclip.paste()
            if new and new != old:
                self.last_clipboard_content = new
                return True
            # Если не вышло скопировать, возвращаем старое значение
            pyperclip.copy(old)
            return False
        except Exception:
            # Удалено логирование
            return False

    def process_clipboard(self) -> None:
        if self.copy_selected_text() and self.last_clipboard_content:
            # Удалено логирование
            self.send_to_telegram(self.last_clipboard_content)


class DaemonContext:
    def __init__(self, detach_process=True, umask=0o022, working_directory="/"):
        self.detach = detach_process
        self.umask = umask
        self.workdir = working_directory

    def __enter__(self):
        if self.detach:
            self._daemonize()
        try:
            os.chdir(self.workdir)
        except Exception:
            pass
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
        self.DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")
        if not self.DEEPGRAM_API_KEY:
            pass  # Удалено логирование
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

    def detect_pulse_monitor(self):
        try:
            # Пробуем найти монитор по умолчанию
            res = subprocess.run(["pactl", "info"], capture_output=True, text=True)
            default_sink = ""
            for line in res.stdout.splitlines():
                if line.startswith("Default Sink:"):
                    default_sink = line.split(":", 1)[1].strip()
                    break

            if default_sink:
                return f"{default_sink}.monitor"

            # Фолбэк на хардкод, если не нашли
            return "alsa_output.pci-0000_00_1f.3.analog-stereo.monitor"
        except Exception:
            # Удалено логирование
            return "auto_null.monitor"  # В крайнем случае

    def _on_message(self, ws, message):
        try:
            data = json.loads(message)
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
        except Exception:
            pass

    def start_recording(self):
        if self.is_recording:
            return
        # Удалено логирование
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
            # Удалено логирование
            self.is_recording = False
            return

        url = "wss://api.deepgram.com/v1/listen?encoding=linear16&sample_rate=16000&channels=1&model=nova-2&language=ru&punctuate=true&interim_results=true&endpointing=300"
        headers = [f"Authorization: Token {self.DEEPGRAM_API_KEY}"]

        self._ws_app = websocket.WebSocketApp(
            url,
            header=headers,
            on_message=self._on_message,
            on_open=lambda ws: self._ws_connected.set(),
            on_close=lambda ws, c, m: self._ws_connected.clear(),
            on_error=lambda ws, e: None,  # Удалено логирование
        )

        self._ws_thread = threading.Thread(
            target=self._ws_app.run_forever, kwargs={"ping_interval": 5}, daemon=True
        )
        self._ws_thread.start()

        if not self._ws_connected.wait(3):
            # Удалено логирование
            self.stop_recording()
            return

        self._send_thread = threading.Thread(target=self._send_audio_loop, daemon=True)
        self._send_thread.start()

    def _send_audio_loop(self):
        CHUNK = 4096
        try:
            while not self._stop_sending.is_set():
                if not self.ffmpeg_process or not self.ffmpeg_process.stdout:
                    break
                chunk = self.ffmpeg_process.stdout.read(CHUNK)
                if not chunk:
                    break
                if self._ws_app and self._ws_connected.is_set():
                    try:
                        self._ws_app.send(chunk, opcode=ABNF.OPCODE_BINARY)
                    except Exception:
                        break

            # Отправка сигнала закрытия стрима
            if self._ws_app and self._ws_connected.is_set():
                self._ws_app.send(json.dumps({"type": "CloseStream"}))
        except Exception:
            pass  # Удалено логирование

    def stop_recording(self) -> Optional[str]:
        if not self.is_recording:
            return None
        # Удалено логирование
        self.is_recording = False

        self._stop_sending.set()

        if self.ffmpeg_process:
            try:
                self.ffmpeg_process.terminate()
                self.ffmpeg_process.wait(timeout=1)
            except Exception:
                pass
            self.ffmpeg_process = None

        if self._send_thread:
            self._send_thread.join(timeout=1)

        time.sleep(0.5)  # Даем время на получение последних сообщений

        if self._ws_app:
            try:
                self._ws_app.close()
            except Exception:
                pass

        with self._transcript_lock:
            parts = list(self._final_chunks)
            if self._partial:
                parts.append(self._partial)
            final_text = " ".join(parts).strip()

        # Удалено логирование
        return final_text or None


class GeminiSolver:
    def __init__(
        self,
        telegram_sender_instance=None,
        context_manager: Optional[DialogueContextManager] = None,
    ):
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError(
                "GEMINI_API_KEY not found"
            )  # Изменено на исключение для немедленного выхода

        genai.configure(api_key=api_key)

        # Настройка безопасности
        self.safety_settings = [
            {
                "category": "HARM_CATEGORY_HARASSMENT",
                "threshold": "BLOCK_NONE",
            },
            {
                "category": "HARM_CATEGORY_HATE_SPEECH",
                "threshold": "BLOCK_NONE",
            },
            {
                "category": "HARM_CATEGORY_SEXUALLY_EXPLICIT",
                "threshold": "BLOCK_NONE",
            },
            {
                "category": "HARM_CATEGORY_DANGEROUS_CONTENT",
                "threshold": "BLOCK_NONE",
            },
        ]

        # Имя модели, работающей на Free Tier
        self.model_name = "gemini-2.5-flash"

        try:
            self.model = genai.GenerativeModel(self.model_name)
        except Exception as e:
            # Оставлено простое информирование в случае критической ошибки
            print(
                f"Ошибка инициализации модели {self.model_name}: {e}", file=sys.stderr
            )
            raise

        self.telegram_sender = telegram_sender_instance
        self.context_manager = context_manager

    def generate_summary_text(self, prompt: str) -> Optional[str]:
        # Для саммари не нужен стриминг, оно всегда должно быть полным и быстрым
        try:
            response = self.model.generate_content(
                prompt,
                safety_settings=self.safety_settings,
                generation_config=genai.types.GenerationConfig(
                    max_output_tokens=500, temperature=0.3
                ),
            )
            return response.text.strip()
        except Exception:
            return None

    def send_to_api_and_forward(self, prompt: str) -> Optional[str]:
        """
        Отправляет запрос в Gemini и пересылает ответ в Telegram потоково (по частям).
        """
        try:
            # Удалено логирование
            response = self.model.generate_content(
                prompt,
                stream=True,  # Включаем стриминг
                safety_settings=self.safety_settings,
                generation_config=genai.types.GenerationConfig(
                    max_output_tokens=3000, temperature=0.4
                ),
            )

            full_text = []
            chunk_buffer = ""
            for chunk in response:
                try:
                    if chunk.text:
                        chunk_buffer += chunk.text
                        full_text.append(chunk.text)

                        # Отправка буфера по готовности (аналог вашего решения)
                        if (
                            len(chunk_buffer) >= 200  # Чанк достаточно большой
                            or "\n\n" in chunk_buffer  # Закончился параграф
                        ):
                            if self.telegram_sender:
                                self.telegram_sender.send_to_telegram(chunk_buffer)
                            chunk_buffer = ""

                except ValueError:
                    # Ошибка, если сработал фильтр безопасности
                    pass

            # Отправляем оставшуюся часть буфера, если что-то осталось
            if chunk_buffer and self.telegram_sender:
                self.telegram_sender.send_to_telegram(chunk_buffer)

            result_text = "".join(full_text).strip()
            return result_text

        except Exception as e:
            error_message = str(e)
            # Улучшенная обработка ошибки 429: квота
            if "429" in error_message and self.telegram_sender:
                self.telegram_sender.send_to_telegram(
                    "⚠️ Ошибка Квоты (429): Превышен лимит Free Tier. Попробуйте через минуту."
                )
            elif self.telegram_sender:
                self.telegram_sender.send_to_telegram(f"⚠️ Ошибка API: {error_message}")
            return None

    def process_sql_task(self) -> None:
        if typing_active:
            return
        task = pyperclip.paste().strip()
        if not task:
            return

        # Удалено логирование
        prompt = (
            f"Задача: {task}\n\n"
            "Напиши только корректный SQL запрос для решения. Без markdown, без объяснений. "
            "Только чистый код."
        )

        # Для SQL используем синхронный вызов (не стриминг), нам нужен код сразу
        try:
            response = self.model.generate_content(
                prompt, safety_settings=self.safety_settings
            )
            solution = response.text.strip()
            if solution:
                threading.Thread(
                    target=self.human_like_typing, args=(solution,)
                ).start()
        except Exception as e:
            if "429" in str(e) and self.telegram_sender:
                self.telegram_sender.send_to_telegram(
                    "⚠️ Ошибка Квоты (429) при генерации SQL. Попробуйте позже."
                )

    def process_analyst_question(self, question: str) -> None:
        if not question or len(question.strip()) < 3:
            return

        # Удалено логирование
        context = (
            self.context_manager.get_context_for_query(question)
            if self.context_manager
            else ""
        )

        prompt = f"""Ты — Senior системный аналитик на собеседовании.
Контекст: {context}

Вопрос интервьюера: {question}

Дай профессиональный, структурированный, уверенный ответ. Используй терминологию."""

        answer = self.send_to_api_and_forward(prompt)

        if answer and self.context_manager:
            self.context_manager.add_qa(question, answer)

    def human_like_typing(self, text: str) -> None:
        global typing_paused, typing_active
        if not text or typing_active:
            return
        typing_active = True
        keyboard = Controller()
        dpy = display.Display()

        # Удаляем markdown обрамление если есть
        text = text.replace("```sql", "").replace("```", "").strip()

        try:
            # Попытка фокуса (работает не везде в Wayland/современных X11 без прав)
            try:
                window = dpy.get_input_focus().focus
                if window and isinstance(window, X.Window):
                    window.set_input_focus(X.RevertToParent, X.CurrentTime)
                    dpy.flush()
            except Exception:
                pass

            lines = text.split("\n")
            for line in lines:
                if typing_paused:
                    while typing_paused:
                        time.sleep(0.1)

                for char in line:
                    keyboard.type(char)
                    time.sleep(random.uniform(0.01, 0.05))  # Чуть быстрее для кода

                keyboard.press(Key.enter)
                keyboard.release(Key.enter)
                time.sleep(0.1)

        except Exception:
            pass  # Удалено логирование
        finally:
            typing_active = False


def toggle_typing_pause():
    global typing_paused
    typing_paused = not typing_paused
    # Удалено логирование


def signal_handler(sig, frame):
    # Удалено логирование
    sys.exit(0)


def run_daemon():
    # Удалено логирование
    global telegram_sender

    # Обязательная проверка токена при инициализации для немедленного выхода
    if not os.getenv("GEMINI_API_KEY"):
        print("GEMINI_API_KEY не найден в .env. Выход.", file=sys.stderr)
        sys.exit(1)

    context_manager = DialogueContextManager()
    telegram_sender = ClipboardSender()

    # Проверка Telegram
    if not telegram_sender.TELEGRAM_BOT_TOKEN:
        print(
            "TELEGRAM_BOT_TOKEN не найден в .env.", file=sys.stderr
        )  # Оставлено только критическое оповещение

    try:
        gemini_solver = GeminiSolver(
            telegram_sender_instance=telegram_sender, context_manager=context_manager
        )
    except RuntimeError:
        sys.exit(1)  # Выход, если API KEY не найден
    except Exception:
        sys.exit(1)  # Выход, если модель не инициализировалась

    context_manager.solver = gemini_solver

    transcriber = AudioTranscriberRealtime()
    num_lock_pressed = False

    def process_audio_question():
        try:
            question = transcriber.stop_recording()
            if question:
                gemini_solver.process_analyst_question(question)
            else:
                pass  # Удалено логирование
        except Exception:
            pass  # Удалено логирование

    def on_press(key):
        nonlocal num_lock_pressed
        try:
            if key == Key.f8:
                # Удалено логирование
                gemini_solver.process_sql_task()
            elif key == Key.f9:
                toggle_typing_pause()
            elif key == Key.insert:
                # Удалено логирование
                telegram_sender.process_clipboard()
            elif key == Key.num_lock:
                if not num_lock_pressed:
                    num_lock_pressed = True
                    transcriber.start_recording()
        except Exception:
            pass  # Удалено логирование

    def on_release(key):
        nonlocal num_lock_pressed
        try:
            if key == Key.num_lock:
                if num_lock_pressed:
                    num_lock_pressed = False
                    threading.Thread(target=process_audio_question, daemon=True).start()
        except Exception:
            pass

    with Listener(on_press=on_press, on_release=on_release) as listener:
        listener.join()


if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    cwd = os.path.dirname(os.path.abspath(__file__))

    try:
        # Теперь все ошибки инициализации API KEY обрабатываются в run_daemon
        # и приводят к выходу до демонизации, если это критично.
        with DaemonContext(detach_process=True, umask=0o022, working_directory=cwd):
            run_daemon()
    except Exception as e:
        print(f"Критическая ошибка запуска: {e}", file=sys.stderr)
        sys.exit(1)
