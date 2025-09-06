__version__ = "2.0.0"


# Требования: aiogram, pymupdf, requests, apscheduler, python-dotenv
# если не получается поставить aiohttp, то используйте `export AIOHTTP_NO_EXTENSIONS=1` перед установкой

import os
import re
import json
import hashlib
import asyncio
from datetime import datetime
from typing import Dict, Any

import requests
import fitz  # PyMuPDF
from aiogram import Bot, Dispatcher, types
from aiogram.utils import executor
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Поместите BOT_TOKEN в .env")

PDF_URL = "https://kemsu.ru/upload/education/schedule/stf/och/STF_1c_25-26_2.pdf"
PDF_LOCAL = "schedule.pdf"
DATA_FILE = "bot_data.json"

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)
scheduler = AsyncIOScheduler()

# В памяти (и в файле) храним users и кэш расписания
state: Dict[str, Any] = {
    "users": {},  # chat_id -> {"group": str, "subscribed": bool, "awaiting_group": bool}
    "cache": {    # cache: last_hash, full_text, groups_by_inst
        "last_hash": None,
        "full_text": "",
        "groups_by_inst": {}
    }
}

# --- Вспомогательные функции ---

def save_state():
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def load_state():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            state.update(data)

def get_hash(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()

def download_pdf_bytes() -> bytes:
    resp = requests.get(PDF_URL, timeout=30)
    resp.raise_for_status()
    return resp.content

def save_pdf_bytes(b: bytes):
    with open(PDF_LOCAL, "wb") as f:
        f.write(b)

def parse_pdf_text(path: str) -> str:
    doc = fitz.open(path)
    parts = []
    for page in doc:
        parts.append(page.get_text())
    return "\n".join(parts)

# Ищем коды групп вида: Буквы(1-4) - цифры(2-4) (русские буквы тоже)
GROUP_RE = re.compile(r"[\u0400-\u04FF]{1,4}-\d{2,4}")

def extract_groups_by_institute(text: str) -> Dict[str, list]:
    groups = set(re.findall(GROUP_RE, text))
    by_inst = {}
    for g in groups:
        inst = g.split("-", 1)[0].strip()
        by_inst.setdefault(inst, []).append(g)
    # Сортируем списки
    for k in by_inst:
        by_inst[k] = sorted(by_inst[k])
    return by_inst

def extract_schedule_for_group(full_text: str, group: str, max_lines=120) -> str:
    lines = full_text.splitlines()
    # Найдём индекс, где упоминается группа
    idx = None
    for i, ln in enumerate(lines):
        if group in ln:
            idx = i
            break
    if idx is None:
        return "Расписание для группы не найдено в текущем документе."
    # Собирать строки вниз, пока не встретим другую группу в начале или пока не набралось max_lines
    collected = []
    for ln in lines[idx: idx + max_lines]:
        # остановим если появилась строка, явно содержащая другой код группы (и это не первая строка)
        if collected and GROUP_RE.search(ln):
            break
        collected.append(ln)
    # Обрезаем пустые строки спереди/сзади
    while collected and not collected[0].strip():
        collected.pop(0)
    while collected and not collected[-1].strip():
        collected.pop()
    return "\n".join(collected) if collected else "Пустой фрагмент расписания."

# --- Инициализация: загрузить/обновить кэш при старте ---

async def initial_load():
    load_state()
    # Если локального PDF нет или кэша пуст, скачать
    try:
        if not os.path.exists(PDF_LOCAL):
            print("PDF не найден локально — скачиваю...")
            b = await asyncio.to_thread(download_pdf_bytes)
            save_pdf_bytes(b)
        # Получаем текст и группы, если не в кэше
        if not state["cache"].get("full_text"):
            print("Парсю PDF...")
            text = await asyncio.to_thread(parse_pdf_text, PDF_LOCAL)
            state["cache"]["full_text"] = text
            state["cache"]["groups_by_inst"] = extract_groups_by_institute(text)
            # вычислим хеш
            with open(PDF_LOCAL, "rb") as f:
                state["cache"]["last_hash"] = get_hash(f.read())
            save_state()
    except Exception as e:
        print("Ошибка init:", e)

# --- Обработчики бота ---

@dp.message_handler(commands=["start"])
async def cmd_start(message: types.Message):
    chat_id = str(message.chat.id)
    state["users"].setdefault(chat_id, {"group": None, "subscribed": True, "awaiting_group": False})
    # Кнопки институтов (динамически из кэша)
    insts = list(state["cache"].get("groups_by_inst", {}).keys())
    if not insts:
        await message.answer("Не удалось найти институты в кэше. Попробуйте позже.")
        return
    kb = InlineKeyboardMarkup(row_width=2)
    for inst in sorted(insts)[:20]:  # максимум 20 кнопок
        kb.insert(InlineKeyboardButton(inst, callback_data=f"institute|{inst}"))
    await message.answer("Выберите институт:", reply_markup=kb)

@dp.callback_query_handler(lambda c: c.data and c.data.startswith("institute|"))
async def cb_institute(call: types.CallbackQuery):
    _, inst = call.data.split("|", 1)
    chat_id = str(call.message.chat.id)
    state["users"].setdefault(chat_id, {"group": None, "subscribed": True, "awaiting_group": False})
    # Списки групп для института
    groups = state["cache"].get("groups_by_inst", {}).get(inst, [])
    kb = InlineKeyboardMarkup(row_width=2)
    # Если групп много — покажем первые 10 и кнопку "Нету моей группы"
    for g in groups[:10]:
        kb.insert(InlineKeyboardButton(g, callback_data=f"group|{g}"))
    kb.add(InlineKeyboardButton("Нету моей группы", callback_data="group|manual"))
    await call.message.answer(f"Институт: {inst}. Выберите группу (показаны первые 10):", reply_markup=kb)
    await call.answer()

@dp.callback_query_handler(lambda c: c.data and c.data.startswith("group|"))
async def cb_group(call: types.CallbackQuery):
    _, payload = call.data.split("|", 1)
    chat_id = str(call.message.chat.id)
    if payload == "manual":
        state["users"].setdefault(chat_id, {"group": None, "subscribed": True, "awaiting_group": False})
        state["users"][chat_id]["awaiting_group"] = True
        save_state()
        await call.message.answer("Введите код вашей группы вручную, например: ИС-951")
        await call.answer()
        return
    # выбранная группа
    grp = payload
    state["users"].setdefault(chat_id, {"group": None, "subscribed": True, "awaiting_group": False})
    state["users"][chat_id]["group"] = grp
    state["users"][chat_id]["awaiting_group"] = False
    state["users"][chat_id]["subscribed"] = True
    save_state()
    await call.message.answer(f"Группа {grp} сохранена. Используйте /schedule чтобы получить расписание.")
    await call.answer()

@dp.message_handler(lambda m: state["users"].get(str(m.chat.id), {}).get("awaiting_group", False))
async def manual_group_input(message: types.Message):
    chat_id = str(message.chat.id)
    grp = message.text.strip().upper()
    state["users"].setdefault(chat_id, {"group": None, "subscribed": True, "awaiting_group": False})
    state["users"][chat_id]["group"] = grp
    state["users"][chat_id]["awaiting_group"] = False
    state["users"][chat_id]["subscribed"] = True
    save_state()
    await message.reply(f"Группа {grp} сохранена. Используйте /schedule чтобы получить расписание.")

@dp.message_handler(commands=["schedule"])
async def cmd_schedule(message: types.Message):
    chat_id = str(message.chat.id)
    info = state["users"].get(chat_id)
    if not info or not info.get("group"):
        await message.reply("Сначала укажите группу через /start.")
        return
    grp = info["group"]
    full_text = state["cache"].get("full_text", "")
    if not full_text:
        await message.reply("Кэш расписания пуст. Попробуйте позже.")
        return
    frag = await asyncio.to_thread(extract_schedule_for_group, full_text, grp)
    await message.reply(f"Расписание для {grp}:\n\n{frag}")

@dp.message_handler(commands=["mygroup"])
async def cmd_mygroup(message: types.Message):
    chat_id = str(message.chat.id)
    info = state["users"].get(chat_id, {})
    await message.reply(f"Ваша сохранённая группа: {info.get('group')}\nПодписка: {info.get('subscribed')}")

@dp.message_handler(commands=["unsubscribe"])
async def cmd_unsubscribe(message: types.Message):
    chat_id = str(message.chat.id)
    state["users"].setdefault(chat_id, {"group": None, "subscribed": True, "awaiting_group": False})
    state["users"][chat_id]["subscribed"] = False
    save_state()
    await message.reply("Вы отписаны от уведомлений об обновлении расписания.")

@dp.message_handler(commands=["subscribe"])
async def cmd_subscribe(message: types.Message):
    chat_id = str(message.chat.id)
    state["users"].setdefault(chat_id, {"group": None, "subscribed": True, "awaiting_group": False})
    state["users"][chat_id]["subscribed"] = True
    save_state()
    await message.reply("Вы подписаны на уведомления об обновлении расписания.")

# --- Периодическая проверка обновлений ---

async def check_for_updates():
    try:
        b = await asyncio.to_thread(download_pdf_bytes)
    except Exception as e:
        print("Не удалось скачать PDF:", e)
        return
    h = get_hash(b)
    old_h = state["cache"].get("last_hash")
    if h == old_h:
        print(f"{datetime.now()}: PDF не изменился.")
        return
    # Изменился — сохраняем, парсим, уведомляем
    print(f"{datetime.now()}: Найдено изменение расписания. Обновляю кэш...")
    save_pdf_bytes(b)
    text = await asyncio.to_thread(parse_pdf_text, PDF_LOCAL)
    state["cache"]["full_text"] = text
    state["cache"]["groups_by_inst"] = extract_groups_by_institute(text)
    state["cache"]["last_hash"] = h
    save_state()
    # уведомляем подписанных пользователей
    notify_text = "🔔 Обновлено расписание! Вы можете запросить /schedule для своей группы."
    for chat_id, info in state["users"].items():
        if info.get("subscribed"):
            try:
                await bot.send_message(int(chat_id), notify_text)
            except Exception as e:
                print("Ошибка уведомления", chat_id, e)

# --- Запуск ---

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(initial_load())
    # Запустить планировщик (проверка каждый час)
    scheduler.add_job(lambda: asyncio.create_task(check_for_updates()), "interval", hours=1, next_run_time=datetime.now())
    scheduler.start()
    print("Бот запущен. /start")
    executor.start_polling(dp, skip_updates=True)
