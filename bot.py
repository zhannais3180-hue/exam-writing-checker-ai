import asyncio
import base64
import io
import os
import re
from typing import Dict, Any

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from dotenv import load_dotenv
from openai import AsyncOpenAI

from prompts2 import build_check_prompt

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_MODEL = os.getenv(
    "OPENROUTER_MODEL",
    "google/gemma-4-26b-a4b-it:free"
)

AI_TIMEOUT_SECONDS = 90

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("Не задан TELEGRAM_BOT_TOKEN")
if not OPENROUTER_API_KEY:
    raise RuntimeError("Не задан OPENROUTER_API_KEY")

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()

client = AsyncOpenAI(
    api_key=OPENROUTER_API_KEY,
    base_url="https://openrouter.ai/api/v1",
)

sessions: Dict[int, Dict[str, Any]] = {}

TASK_NAMES = {
    "oge35": "ОГЭ · Задание 35",
    "ege37": "ЕГЭ · Задание 37",
    "ege38": "ЕГЭ · Задание 38",
}

def main_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="9️⃣ ОГЭ · Задание 35", callback_data="task:oge35")],
        [InlineKeyboardButton(text="1️⃣1️⃣ ЕГЭ · Задание 37", callback_data="task:ege37")],
        [InlineKeyboardButton(text="📊 ЕГЭ · Задание 38", callback_data="task:ege38")],
    ])

def result_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="♻️ Следующая работа по этому заданию", callback_data="same_task")],
        [InlineKeyboardButton(text="🆕 Загрузить новое задание", callback_data="new_task")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="home")],
    ])

def count_words(text: str) -> int:
    tokens = re.findall(
        r"[A-Za-zА-Яа-яЁё0-9]+(?:['’-][A-Za-zА-Яа-яЁё0-9]+)*",
        text
    )
    return len(tokens)

async def call_ai(messages):
    return await asyncio.wait_for(
        client.chat.completions.create(
            model=OPENROUTER_MODEL,
            messages=messages,
            temperature=0.2,
        ),
        timeout=AI_TIMEOUT_SECONDS,
    )

async def transcribe_photo(message: Message) -> str:
    photo = message.photo[-1]
    file = await bot.get_file(photo.file_id)
    buf = io.BytesIO()
    await bot.download_file(file.file_path, destination=buf)
    data = base64.b64encode(buf.getvalue()).decode("utf-8")

    response = await call_ai([{
        "role": "user",
        "content": [
            {
                "type": "text",
                "text": (
                    "Точно перепиши весь читаемый текст с изображения. "
                    "Не исправляй грамматику, лексику, орфографию или пунктуацию. "
                    "Сохрани ошибки ученика. Верни только распознанный текст."
                ),
            },
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/jpeg;base64,{data}"
                },
            },
        ],
    }])

    return (response.choices[0].message.content or "").strip()

async def extract_text(message: Message) -> str | None:
    if message.text:
        return message.text.strip()

    if message.photo:
        await message.answer("📷 Фото получила. Распознаю текст…")
        try:
            text = await transcribe_photo(message)
            if not text:
                raise RuntimeError("Пустой результат распознавания")
            return text
        except asyncio.TimeoutError:
            await message.answer(
                "⚠️ ИИ слишком долго распознаёт фото. "
                "Попробуйте ещё раз или пришлите текст."
            )
            return None
        except Exception as e:
            print(f"Image recognition error: {type(e).__name__}: {e}")
            await message.answer(
                "Не получилось надёжно прочитать фото. "
                "Пришлите, пожалуйста, эту работу текстом."
            )
            return None

    return None

async def send_long(chat_id: int, text: str):
    limit = 3900
    while text:
        if len(text) <= limit:
            await bot.send_message(chat_id, text)
            return

        cut = text.rfind("\n", 0, limit)
        if cut < 1000:
            cut = limit

        part, text = text[:cut], text[cut:].lstrip()
        await bot.send_message(chat_id, part)

@dp.message(CommandStart())
async def start(message: Message):
    sessions[message.from_user.id] = {}
    await message.answer(
        "👋 Exam Writing Checker AI\n\n"
        "Проверяю письменные работы ОГЭ и ЕГЭ по критериям ФИПИ.\n\n"
        "Выбери тип задания:",
        reply_markup=main_menu(),
    )

@dp.callback_query(F.data.startswith("task:"))
async def choose_task(callback: CallbackQuery):
    task_key = callback.data.split(":", 1)[1]

    sessions[callback.from_user.id] = {
        "task_key": task_key,
        "step": "waiting_task",
    }

    await callback.answer()
    await callback.message.answer(
        f"✅ Выбрано: {TASK_NAMES[task_key]}\n\n"
        "Шаг 1 из 2.\n"
        "Пришли условие задания / стимул текстом или фотографией."
    )

@dp.callback_query(F.data == "same_task")
async def same_task(callback: CallbackQuery):
    session = sessions.get(callback.from_user.id, {})

    if not session.get("task_text"):
        await callback.answer("Сохранённого задания нет", show_alert=True)
        return

    session["step"] = "waiting_student"
    await callback.answer()
    await callback.message.answer(
        "♻️ Используем то же задание.\n"
        "Пришли следующую работу ученика текстом или фотографией."
    )

@dp.callback_query(F.data == "new_task")
async def new_task(callback: CallbackQuery):
    session = sessions.get(callback.from_user.id, {})

    if not session.get("task_key"):
        await callback.answer()
        await callback.message.answer(
            "Выбери тип задания:",
            reply_markup=main_menu()
        )
        return

    session.pop("task_text", None)
    session["step"] = "waiting_task"

    await callback.answer()
    await callback.message.answer(
        "Пришли новое условие задания текстом или фотографией."
    )

@dp.callback_query(F.data == "home")
async def home(callback: CallbackQuery):
    sessions[callback.from_user.id] = {}
    await callback.answer()
    await callback.message.answer(
        "Выбери тип задания:",
        reply_markup=main_menu()
    )

@dp.message()
async def handle_message(message: Message):
    session = sessions.get(message.from_user.id)

    if not session or not session.get("step"):
        await message.answer("Нажми /start и выбери тип задания.")
        return

    text = await extract_text(message)

    if not text:
        if not message.photo:
            await message.answer("Пришли текст или фотографию.")
        return

    if session["step"] == "waiting_task":
        session["task_text"] = text
        session["step"] = "waiting_student"

        await message.answer(
            "✅ Задание сохранено.\n\n"
            "Шаг 2 из 2.\n"
            "Теперь пришли работу ученика текстом или фотографией."
        )
        return

    if session["step"] == "waiting_student":
        student_text = text
        word_count = count_words(student_text)

        await message.answer(
            f"🧮 Получено. Предварительный подсчёт: {word_count} слов.\n"
            "Проверяю по критериям…"
        )

        prompt = build_check_prompt(
            session["task_key"],
            session["task_text"],
            student_text,
            word_count,
        )

        try:
            response = await call_ai([
                {"role": "user", "content": prompt}
            ])

            result = (response.choices[0].message.content or "").strip()

            if not result:
                raise RuntimeError("ИИ вернул пустой ответ")

        except asyncio.TimeoutError:
            await message.answer(
                "⚠️ ИИ слишком долго отвечает. "
                "Попробуйте повторить проверку через минуту."
            )
            return

        except Exception as e:
            print(f"OpenRouter error: {type(e).__name__}: {e}")
            await message.answer(
                "⚠️ Не удалось получить ответ от ИИ. "
                "Попробуйте ещё раз через минуту. "
                "Если ошибка повторится — проверим логи Railway."
            )
            return

        await send_long(message.chat.id, result)
        await message.answer(
            "Что дальше?",
            reply_markup=result_menu()
        )

        session["step"] = "done"
        return

    await message.answer(
        "Выбери действие кнопкой ниже или нажми /start.",
        reply_markup=result_menu()
    )

async def main():
    print(f"Bot started with model: {OPENROUTER_MODEL}")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
