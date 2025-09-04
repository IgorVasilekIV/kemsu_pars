import os
import json
import hashlib
import requests
import fitz  # PyMuPDF
from telegram.ext import ApplicationBuilder, CommandHandler
from apscheduler.schedulers.background import BackgroundScheduler

# === Конфиг ===
BOT_TOKEN = "ВАШ_ТОКЕН_БОТА"  # вставь токен
PDF_URL = "https://kemsu.ru/upload/education/schedule/stf/och/STF_1c_25-26_2.pdf"
STATE_FILE = "state.json"
LOCAL_PDF = "schedule.pdf"


# === Служебные функции ===
def load_state():
    if os.path.exists(STATE_FILE):
        return json.load(open(STATE_FILE, "r", encoding="utf-8"))
    return {"hash": "", "subs": {}}  # subs: {group: [chat_id,...]}


def save_state(state):
    json.dump(state, open(STATE_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)


def fetch_pdf():
    r = requests.get(PDF_URL, timeout=20)
    r.raise_for_status()
    with open(LOCAL_PDF, "wb") as f:
        f.write(r.content)
    return LOCAL_PDF


def pdf_hash(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def parse_schedule(path):
    doc = fitz.open(path)
    text = []
    for page in doc:
        text.append(page.get_text())
    full_text = "\n".join(text)

    groups = {}
    lines = full_text.splitlines()
    current_group = None
    current_block = []

    for line in lines:
        line = line.strip()
        if not line:
            continue
        # простое определение группы (например ТР-951)
        if "-" in line and line.split("-")[0].isalpha() and line.split("-")[1].isdigit():
            if current_group:
                groups[current_group] = "\n".join(current_block)
            current_group = line
            current_block = []
        else:
            if current_group:
                current_block.append(line)

    if current_group and current_block:
        groups[current_group] = "\n".join(current_block)

    return groups


# === Команды ===
async def start(update, context):
    await update.message.reply_text(
        "Привет! Я бот с расписанием.\n\n"
        "Команды:\n"
        "/schedule <группа>\n"
        "/subscribe <группа>\n"
        "/unsubscribe <группа>"
    )


async def schedule_cmd(update, context):
    group = " ".join(context.args) if context.args else ""
    if not group:
        await update.message.reply_text("Укажи группу: /schedule ТР-951")
        return
    fetch_pdf()
    groups = parse_schedule(LOCAL_PDF)
    if group in groups:
        await update.message.reply_text(f"Расписание {group}:\n\n{groups[group][:4000]}")
    else:
        await update.message.reply_text("Группа не найдена.")


async def subscribe(update, context):
    group = " ".join(context.args) if context.args else ""
    if not group:
        await update.message.reply_text("Укажи группу: /subscribe ТР-951")
        return
    state = load_state()
    state.setdefault("subs", {})
    state["subs"].setdefault(group, [])
    chat = update.effective_chat.id
    if chat not in state["subs"][group]:
        state["subs"][group].append(chat)
    save_state(state)
    await update.message.reply_text(f"Подписал на {group}")


async def unsubscribe(update, context):
    group = " ".join(context.args) if context.args else ""
    if not group:
        await update.message.reply_text("Укажи группу: /unsubscribe ТР-951")
        return
    state = load_state()
    chat = update.effective_chat.id
    if group in state.get("subs", {}) and chat in state["subs"][group]:
        state["subs"][group].remove(chat)
        save_state(state)
        await update.message.reply_text(f"Отписал от {group}")
    else:
        await update.message.reply_text("Ты не подписан на эту группу.")


# === Проверка обновлений ===
def check_updates(app):
    try:
        fetch_pdf()
        h = pdf_hash(LOCAL_PDF)
        state = load_state()
        if h != state.get("hash"):
            state["hash"] = h
            save_state(state)
            groups = parse_schedule(LOCAL_PDF)
            for group, chats in state.get("subs", {}).items():
                if group in groups:
                    for chat in chats:
                        app.bot.send_message(
                            chat_id=chat,
                            text=f"Расписание обновилось! ({group})\n\n{groups[group][:4000]}"
                        )
    except Exception as e:
        print("Ошибка проверки:", e)


# === Запуск ===
if __name__ == "__main__":
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("schedule", schedule_cmd))
    app.add_handler(CommandHandler("subscribe", subscribe))
    app.add_handler(CommandHandler("unsubscribe", unsubscribe))

    sched = BackgroundScheduler()
    sched.add_job(lambda: check_updates(app), "interval", minutes=30)
    sched.start()

    print("Бот запущен")
    app.run_polling()
