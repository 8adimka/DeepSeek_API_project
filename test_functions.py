#!/usr/bin/env python3
"""
Тестовый скрипт для проверки основных функций программы
"""

import os
import sys

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from wp_10 import ClipboardSender, DeepSeekSolver, DialogueContextManager, OpenAISolver


def test_context_manager():
    """Тестирование менеджера контекста"""
    print("=== Тестирование DialogueContextManager ===")
    context_manager = DialogueContextManager()

    # Добавляем несколько QA пар
    context_manager.add_qa(
        "Что такое декораторы в Python?",
        "Декораторы - это функции, которые принимают другую функцию и расширяют её функциональность без изменения её кода.",
    )

    context_manager.add_qa(
        "Как работает менеджер контекста with?",
        "Менеджер контекста with автоматически управляет ресурсами, вызывая __enter__ при входе и __exit__ при выходе.",
    )

    # Проверяем получение контекста
    context = context_manager.get_context_for_query(
        "Расскажи о функциях высшего порядка"
    )
    print("Контекст для нового вопроса:")
    print(context)
    print()

    # Проверяем добавление новой QA пары
    context_manager.add_qa(
        "Что такое функции высшего порядка?",
        "Функции высшего порядка - это функции, которые принимают другие функции в качестве аргументов или возвращают их.",
    )

    print("Контекст после добавления новой QA пары:")
    context = context_manager.get_context_for_query("Что такое замыкания?")
    print(context)
    print()


def test_clipboard_sender():
    """Тестирование отправки в Telegram (только инициализация)"""
    print("=== Тестирование ClipboardSender ===")
    try:
        sender = ClipboardSender()
        print("ClipboardSender инициализирован успешно")
        print(
            f"Telegram Bot Token: {'установлен' if sender.TELEGRAM_BOT_TOKEN else 'не установлен'}"
        )
        print(
            f"Telegram Chat ID: {'установлен' if sender.TELEGRAM_CHAT_ID else 'не установлен'}"
        )
    except Exception as e:
        print(f"Ошибка инициализации ClipboardSender: {e}")
    print()


def test_solvers():
    """Тестирование инициализации решателей"""
    print("=== Тестирование решателей ===")

    try:
        # DeepSeekSolver
        deepseek = DeepSeekSolver()
        print("DeepSeekSolver инициализирован успешно")
    except Exception as e:
        print(f"Ошибка инициализации DeepSeekSolver: {e}")

    try:
        # OpenAISolver
        openai = OpenAISolver()
        print("OpenAISolver инициализирован успешно")
    except Exception as e:
        print(f"Ошибка инициализации OpenAISolver: {e}")

    print()


def test_context_integration():
    """Тестирование интеграции контекста с решателями"""
    print("=== Тестирование интеграции контекста ===")

    context_manager = DialogueContextManager()

    # Добавляем историю диалога
    context_manager.add_qa(
        "Что такое списки в Python?",
        "Списки - это упорядоченные изменяемые коллекции объектов в Python.",
    )

    context_manager.add_qa(
        "Как работают генераторы списков?",
        "Генераторы списков - это компактный способ создания списков с использованием выражения в квадратных скобках.",
    )

    # Создаем решатель с контекстом
    try:
        solver = OpenAISolver(context_manager=context_manager)
        print("OpenAISolver с контекстом инициализирован успешно")

        # Проверяем получение контекста
        context = context_manager.get_context_for_query("Что такое кортежи?")
        print("Контекст для вопроса о кортежах:")
        print(context)

    except Exception as e:
        print(f"Ошибка интеграции контекста: {e}")

    print()


if __name__ == "__main__":
    print("Тестирование функций программы wp_10.py")
    print("=" * 50)
    print()

    test_context_manager()
    test_clipboard_sender()
    test_solvers()
    test_context_integration()

    print("Тестирование завершено!")
