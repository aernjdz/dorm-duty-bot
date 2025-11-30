import json
from datetime import date, datetime, timedelta, time
from pathlib import Path
from zoneinfo import ZoneInfo
from dotenv import dotenv_values

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
)

# ============ КОНФІГ ============

env = dotenv_values(".env")
TELEGRAM_BOT_TOKEN = env.get("TELEGRAM_BOT_TOKEN")

DATA_FILE = Path("schedule_data.json")
ADMIN_IDS = []  # якщо пустий список – усі вважаються адмінами

# час щоденного нагадування (по Варшаві)
REMINDER_TIME = time(hour=16, minute=35, tzinfo=ZoneInfo("Europe/Warsaw"))

DEFAULT_STATE = {
    "start_date": "2025-12-01",
    "members": [],            # [{id, label}, ...]
    "penalties": {},          # { "123": 2, ... }
    "overrides": {},          # { "YYYY-MM-DD": user_id }
    "global_holidays": [],    # [ {"from": "...", "to": "..."}, ... ]
    "away_ranges": {},        # { "user_id": [ {"from": "...", "to": "..."}, ... ] }
    "notify_chats": []        # [chat_id, ...]
}

# ============ ЗБЕРЕЖЕННЯ / ЗАВАНТАЖЕННЯ СТАНУ ============

def load_state():
    if DATA_FILE.exists():
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)

        state.setdefault("members", [])
        state.setdefault("penalties", {})
        state.setdefault("overrides", {})
        state.setdefault("global_holidays", [])
        state.setdefault("away_ranges", {})
        state.setdefault("notify_chats", [])

        return state

    state = DEFAULT_STATE.copy()
    state["members"] = []
    state["penalties"] = {}
    state["overrides"] = {}
    state["global_holidays"] = []
    state["away_ranges"] = {}
    state["notify_chats"] = []
    save_state(state)
    return state


def save_state(state):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def is_admin(user_id: int) -> bool:
    return (not ADMIN_IDS) or (user_id in ADMIN_IDS)


# ============ ХЕЛПЕРИ ДЛЯ УЧАСНИКІВ ============

def member_label_list(state):
    return [m["label"] for m in state["members"]]


def get_member_by_id(user_id: int, state):
    for m in state["members"]:
        if m.get("id") == user_id:
            return m
    return None


def get_member_by_label(label: str, state):
    for m in state["members"]:
        if m["label"] == label:
            return m
    return None


def ensure_penalty_entry(user_id: int, state):
    pid = str(user_id)
    state.setdefault("penalties", {})
    state["penalties"].setdefault(pid, 0)


# ============ ДАТИ / ІНТЕРВАЛИ ============

def parse_date(s: str) -> date:
    # очікуємо формат YYYY-MM-DD або DD.MM.YYYY
    if "-" in s:
        return datetime.strptime(s, "%Y-%m-%d").date()
    else:
        return datetime.strptime(s, "%d.%m.%Y").date()


def get_start_date(state) -> date:
    return parse_date(state["start_date"])


def format_date_pl(d: date) -> str:
    return d.strftime("%d.%m.%Y")


def day_in_range(d: date, r: dict) -> bool:
    start = parse_date(r["from"])
    end = parse_date(r["to"])
    return start <= d <= end


def is_global_holiday(d: date, state) -> bool:
    for r in state.get("global_holidays", []):
        if day_in_range(d, r):
            return True
    return False


def is_member_away_on(user_id: int, d: date, state) -> bool:
    ranges = state.get("away_ranges", {}).get(str(user_id), [])
    for r in ranges:
        if day_in_range(d, r):
            return True
    return False


# ============ ЛОГІКА ЧЕРГУВАНЬ (СИМУЛЯЦІЯ) ============

def get_duty_member(day: date, state):
    """
    Симулюємо чергування від start_date до day, рухаючи індекс pos.
    Враховуємо:
    - global_holidays (глобальні канікули)
    - away_ranges (періоди відсутності конкретних юзерів)
    - overrides (разові заміни)
    """
    members = state["members"]
    if not members:
        return None

    start_date = get_start_date(state)
    if day < start_date:
        return None

    pos = 0  # індекс в members
    current = start_date

    while current <= day:
        iso = current.isoformat()

        # 1) глобальні канікули – просто пропускаємо, pos не рухається
        if is_global_holiday(current, state):
            assigned_member = None

        else:
            # 2) override – конкретний юзер
            if iso in state.get("overrides", {}):
                override_id = state["overrides"][iso]
                assigned_member = get_member_by_id(override_id, state)
                pos = (pos + 1) % len(members)

            else:
                # 3) шукаємо першого, хто не away
                assigned_member = None
                tried = 0
                idx = pos
                while tried < len(members):
                    candidate = members[idx]
                    if not is_member_away_on(candidate["id"], current, state):
                        assigned_member = candidate
                        pos = (idx + 1) % len(members)
                        break
                    idx = (idx + 1) % len(members)
                    tried += 1
                # якщо всі away → немає чергування, pos не рухається

        if current == day:
            return assigned_member

        current += timedelta(days=7)

    return None


def get_duty_for_day(day: date, state) -> str:
    m = get_duty_member(day, state)
    if not m:
        return "Ніхто"
    return m["label"]


# ============ ЩОДЕННЕ НАГАДУВАННЯ ============

async def daily_reminder(context: ContextTypes.DEFAULT_TYPE):
    state = load_state()
    today = date.today()

    member = get_duty_member(today, state)
    if not member:
        return  # сьогодні чергування немає

    duty = member["label"]
    text = f"🔔 Нагадування: сьогодні ({format_date_pl(today)}) чергує *{duty}*"

    for chat_id in state.get("notify_chats", []):
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="Markdown"
            )
        except Exception as e:
            print(f"Failed to send reminder to {chat_id}: {e}")


# ============ КОМАНДИ ============

async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = load_state()
    txt = [
        "👋 Привіт! Я бот для керування графіком чергувань.",
        "",
        "Основні команди:",
        "/today – хто чергує сьогодні",
        "/next – хто чергує наступної неділі",
        "/week – розклад на 4 тижні вперед",
        "/calendar YYYY-MM – чергування за місяць",
        "/skip YYYY-MM-DD – відпроситись з чергування (на один день, з пошуком заміни)",
        "/away YYYY-MM-DD YYYY-MM-DD – тебе не ставлять на чергування в цей період",
        "/points – показати штрафи",
        "",
        "Реєстрація:",
        "/join Ім'я – додати себе в список з таким ім'ям (label)",
        "",
        "Налаштування (адмін):",
        "/config – показати поточні налаштування",
        "/setstart YYYY-MM-DD – змінити початкову дату",
        "/addmember Ім'я – додати юзера (reply або себе)",
        "/removemember Ім'я – прибрати людину по label",
        "/holidayrange YYYY-MM-DD YYYY-MM-DD – глобальні канікули (для всіх)",
        "/enablenotify – ввімкнути щоденне нагадування в цей чат",
        "/disablenotify – вимкнути нагадування в цей чат",
        "",
        f"Поточна стартова дата: {state['start_date']}",
        "Учасники по колу: " + (", ".join(member_label_list(state)) or "ще нікого немає")
    ]
    await update.message.reply_text("\n".join(txt))


async def today_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = load_state()
    today = date.today()
    duty = get_duty_for_day(today, state)
    await update.message.reply_text(
        f"📅 Сьогодні ({format_date_pl(today)}) чергує: *{duty}*",
        parse_mode="Markdown"
    )


async def next_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = load_state()
    today = date.today()
    days_ahead = (6 - today.weekday()) % 7  # 0-пн ... 6-нд
    next_sunday = today + timedelta(days=days_ahead)
    duty = get_duty_for_day(next_sunday, state)
    await update.message.reply_text(
        f"➡️ Наступне чергування ({format_date_pl(next_sunday)}) – *{duty}*",
        parse_mode="Markdown"
    )


async def week_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = load_state()
    today = date.today()
    days_ahead = (6 - today.weekday()) % 7
    next_sunday = today + timedelta(days=days_ahead)

    txt = ["📅 Розклад чергувань на найближчі 4 тижні:"]
    for i in range(4):
        duty_day = next_sunday + timedelta(weeks=i)
        duty = get_duty_for_day(duty_day, state)
        txt.append(f"{format_date_pl(duty_day)} – *{duty}*")

    await update.message.reply_text("\n".join(txt), parse_mode="Markdown")


async def calendar_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = load_state()

    if not context.args:
        await update.message.reply_text("❗ Використання: /calendar YYYY-MM\nНапр.: /calendar 2025-12")
        return

    ym = context.args[0]
    try:
        year_str, month_str = ym.split("-")
        year = int(year_str)
        month = int(month_str)
        first_day = date(year, month, 1)
    except Exception:
        await update.message.reply_text("❗ Невірний формат. Використовуйте: /calendar YYYY-MM\nНапр.: /calendar 2025-12")
        return

    # останній день місяця
    if month == 12:
        next_month_first = date(year + 1, 1, 1)
    else:
        next_month_first = date(year, month + 1, 1)
    last_day = next_month_first - timedelta(days=1)

    start = get_start_date(state)

    # пошук першого чергового дня в місяці (крок тиждень)
    if first_day <= start:
        curr = start
    else:
        days_diff = (first_day - start).days
        weeks_offset = (days_diff + 6) // 7  # округлення вгору до тижня
        curr = start + timedelta(days=7 * weeks_offset)

    lines = [f"📆 Чергування за {month:02d}.{year}:"]
    if curr > last_day:
        lines.append("У цьому місяці немає жодного чергування.")
    else:
        while curr <= last_day:
            duty = get_duty_for_day(curr, state)
            lines.append(f"{format_date_pl(curr)} – {duty}")
            curr += timedelta(days=7)

    await update.message.reply_text("\n".join(lines))


async def config_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("⛔ Лише адміністратор може дивитись конфіг.")
        return

    state = load_state()
    lines = [
        "⚙️ Поточні налаштування:",
        f"Стартова дата: {state['start_date']}",
        "",
        "Учасники:"
    ]
    for m in state["members"]:
        lines.append(f"- {m['label']} (id: {m['id']})")

    lines.append("")
    lines.append("Глобальні канікули:")
    if state["global_holidays"]:
        for r in state["global_holidays"]:
            lines.append(f"- {r['from']} → {r['to']}")
    else:
        lines.append("- немає")

    lines.append("")
    lines.append("Away-інтервали:")
    if state["away_ranges"]:
        for uid, ranges in state["away_ranges"].items():
            for r in ranges:
                lines.append(f"- id {uid}: {r['from']} → {r['to']}")
    else:
        lines.append("- немає")

    lines.append("")
    lines.append("Чати з нагадуваннями:")
    if state["notify_chats"]:
        for cid in state["notify_chats"]:
            lines.append(f"- chat_id: {cid}")
    else:
        lines.append("- немає")

    await update.message.reply_text("\n".join(lines))


async def setstart_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("⛔ Лише адміністратор може змінювати конфіг.")
        return

    if len(context.args) != 1:
        await update.message.reply_text("❗ Використання: /setstart YYYY-MM-DD")
        return

    new_date_str = context.args[0]
    try:
        new_date = parse_date(new_date_str)
    except ValueError:
        await update.message.reply_text("❗ Невірний формат дати.")
        return

    state = load_state()
    state["start_date"] = new_date.isoformat()
    save_state(state)

    await update.message.reply_text(f"✅ Стартова дата змінена на {new_date.isoformat()}")


async def join_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❗ Використання: /join Ім'я\nНапр.: /join Андрій")
        return

    label = " ".join(context.args)
    user = update.effective_user
    user_id = user.id

    state = load_state()

    existing = get_member_by_id(user_id, state)
    if existing:
        existing["label"] = label
        ensure_penalty_entry(user_id, state)
        save_state(state)
        await update.message.reply_text(f"✅ Оновив твоє ім'я на '{label}'.")
        return

    state["members"].append({"id": user_id, "label": label})
    ensure_penalty_entry(user_id, state)
    save_state(state)

    await update.message.reply_text(f"✅ Ти доданий у список як '{label}'.")


async def addmember_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("⛔ Лише адміністратор може змінювати конфіг.")
        return

    if not context.args:
        await update.message.reply_text("❗ Використання: /addmember Ім'я (у reply або без reply – тоді це ти)")
        return

    label = " ".join(context.args)

    if update.message.reply_to_message:
        target_user = update.message.reply_to_message.from_user
    else:
        target_user = update.effective_user

    target_id = target_user.id

    state = load_state()

    existing = get_member_by_id(target_id, state)
    if existing:
        existing["label"] = label
        ensure_penalty_entry(target_id, state)
        save_state(state)
        await update.message.reply_text(f"✅ Оновлено: {target_id} тепер '{label}'.")
        return

    state["members"].append({"id": target_id, "label": label})
    ensure_penalty_entry(target_id, state)
    save_state(state)

    await update.message.reply_text(f"✅ Доданий учасник '{label}' (id: {target_id}).")


async def removemember_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("⛔ Лише адміністратор може змінювати конфіг.")
        return

    if not context.args:
        await update.message.reply_text("❗ Використання: /removemember Ім'я")
        return

    label = " ".join(context.args)
    state = load_state()

    member = get_member_by_label(label, state)
    if not member:
        await update.message.reply_text(f"❗ Учасника '{label}' немає в списку.")
        return

    state["members"].remove(member)
    pid = str(member["id"])
    state["penalties"].pop(pid, None)
    save_state(state)

    await update.message.reply_text(f"✅ Учасник '{label}' видалений.")


async def points_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = load_state()
    if not state["members"]:
        await update.message.reply_text("Ще немає жодних учасників 👌")
        return

    penalties = state.get("penalties", {})
    lines = ["⚠️ Штрафи (скільки разів відпросився):"]
    for m in state["members"]:
        pid = str(m["id"])
        p = penalties.get(pid, 0)
        lines.append(f"{m['label']}: {p}")
    await update.message.reply_text("\n".join(lines))


async def holidayrange_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /holidayrange YYYY-MM-DD YYYY-MM-DD – глобальні канікули (для всіх)
    """
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("⛔ Лише адміністратор може ставити канікули.")
        return

    if len(context.args) != 2:
        await update.message.reply_text("❗ Використання: /holidayrange YYYY-MM-DD YYYY-MM-DD")
        return

    from_str, to_str = context.args
    try:
        d_from = parse_date(from_str)
        d_to = parse_date(to_str)
    except ValueError:
        await update.message.reply_text("❗ Невірний формат дат.")
        return

    if d_to < d_from:
        await update.message.reply_text("❗ Кінцева дата раніше за початкову.")
        return

    state = load_state()
    state["global_holidays"].append({
        "from": d_from.isoformat(),
        "to": d_to.isoformat()
    })
    save_state(state)

    await update.message.reply_text(
        f"✅ Додано глобальні канікули: {format_date_pl(d_from)} → {format_date_pl(d_to)}."
    )


async def away_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /away YYYY-MM-DD YYYY-MM-DD – поточного юзера не ставимо на чергування в цей період
    """
    if len(context.args) != 2:
        await update.message.reply_text("❗ Використання: /away YYYY-MM-DD YYYY-MM-DD")
        return

    from_str, to_str = context.args
    try:
        d_from = parse_date(from_str)
        d_to = parse_date(to_str)
    except ValueError:
        await update.message.reply_text("❗ Невірний формат дат.")
        return

    if d_to < d_from:
        await update.message.reply_text("❗ Кінцева дата раніше за початкову.")
        return

    user = update.effective_user
    user_id = user.id

    state = load_state()

    if not get_member_by_id(user_id, state):
        await update.message.reply_text("❗ Спочатку /join, щоб додати себе в список чергувань.")
        return

    state["away_ranges"].setdefault(str(user_id), [])
    state["away_ranges"][str(user_id)].append({
        "from": d_from.isoformat(),
        "to": d_to.isoformat()
    })
    save_state(state)

    await update.message.reply_text(
        f"✅ Позначено, що тебе не буде: {format_date_pl(d_from)} → {format_date_pl(d_to)}.\n"
        f"В цей період тебе не ставитимуть на чергування, але черга перенесеться далі."
    )


async def enablenotify_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("⛔ Лише адміністратор може вмикати нагадування.")
        return

    chat_id = update.effective_chat.id
    state = load_state()
    chats = state.setdefault("notify_chats", [])

    if chat_id in chats:
        await update.message.reply_text("🔔 Нагадування вже увімкнені для цього чату.")
        return

    chats.append(chat_id)
    save_state(state)
    await update.message.reply_text("✅ Нагадування про чергування увімкнені для цього чату.")


async def disablenotify_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("⛔ Лише адміністратор може вимикати нагадування.")
        return

    chat_id = update.effective_chat.id
    state = load_state()
    chats = state.setdefault("notify_chats", [])

    if chat_id not in chats:
        await update.message.reply_text("ℹ️ Для цього чату й так немає автосповіщень.")
        return

    chats.remove(chat_id)
    save_state(state)
    await update.message.reply_text("✅ Нагадування про чергування вимкнені для цього чату.")


# ============ /skip (на один день) ============

async def skip_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or len(context.args) != 1:
        await update.message.reply_text("❗ Використання: /skip YYYY-MM-DD")
        return

    date_str = context.args[0]
    try:
        d = parse_date(date_str)
    except ValueError:
        await update.message.reply_text("❗ Невірний формат дати.")
        return

    state = load_state()
    duty_member = get_duty_member(d, state)

    if not duty_member:
        await update.message.reply_text("На цю дату немає призначеного чергування.")
        return

    requester = update.effective_user
    if duty_member["id"] != requester.id:
        await update.message.reply_text(
            f"❗ Ви не чергуєте {format_date_pl(d)}. Чергує: {duty_member['label']}"
        )
        return

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Я можу 🗿", callback_data=f"volunteer|{d.isoformat()}|{requester.id}")],
    ])
    text = (
        f"❗ {duty_member['label']} не може чергувати {format_date_pl(d)}.\n"
        f"Хто може замінити? Натисніть кнопку нижче."
    )
    await update.message.reply_text(text, reply_markup=keyboard)


async def volunteer_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    # формат: volunteer|YYYY-MM-DD|ABSENT_ID
    data = query.data
    _, date_str, absent_id_str = data.split("|")

    state = load_state()
    duty_date = parse_date(date_str)
    iso = duty_date.isoformat()

    volunteer_user = query.from_user
    volunteer_id = volunteer_user.id

    volunteer_member = get_member_by_id(volunteer_id, state)
    if not volunteer_member:
        await query.edit_message_text("⛔ Тебе немає в списку чергувань, ти не можеш замінити.")
        return

    # якщо вже хтось замінив
    if iso in state.get("overrides", {}):
        already_id = state["overrides"][iso]
        already_member = get_member_by_id(already_id, state)
        name = already_member["label"] if already_member else f"id {already_id}"
        await query.edit_message_text(
            f"На {format_date_pl(duty_date)} вже погодився {name}."
        )
        return

    # записуємо override
    state.setdefault("overrides", {})
    state["overrides"][iso] = volunteer_id

    # штраф відсутньому
    absent_id = int(absent_id_str)
    ensure_penalty_entry(absent_id, state)
    pid = str(absent_id)
    state["penalties"][pid] += 1

    absent_member = get_member_by_id(absent_id, state)
    absent_label = absent_member["label"] if absent_member else f"id {absent_id}"

    save_state(state)

    new_text = (
        f"✅ На {format_date_pl(duty_date)} замість {absent_label} буде чергувати {volunteer_member['label']}.\n"
        f"{absent_label} отримує 1 штрафний бал."
    )
    await query.edit_message_text(new_text)


# ============ MAIN ============

# ============ MAIN ============

def main():
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CommandHandler("today", today_handler))
    app.add_handler(CommandHandler("next", next_handler))
    app.add_handler(CommandHandler("week", week_handler))
    app.add_handler(CommandHandler("calendar", calendar_handler))

    app.add_handler(CommandHandler("config", config_handler))
    app.add_handler(CommandHandler("setstart", setstart_handler))

    app.add_handler(CommandHandler("join", join_handler))
    app.add_handler(CommandHandler("addmember", addmember_handler))
    app.add_handler(CommandHandler("removemember", removemember_handler))

    app.add_handler(CommandHandler("points", points_handler))
    app.add_handler(CommandHandler("holidayrange", holidayrange_handler))
    app.add_handler(CommandHandler("away", away_handler))

    app.add_handler(CommandHandler("enablenotify", enablenotify_handler))
    app.add_handler(CommandHandler("disablenotify", disablenotify_handler))

    app.add_handler(CommandHandler("skip", skip_handler))
    app.add_handler(CallbackQueryHandler(volunteer_callback, pattern=r"^volunteer\|"))

    # 🔔 щоденне нагадування
    job_queue = app.job_queue
    if job_queue is None:
        print('⚠️ JobQueue недоступний. Перевір, що встановлено: python-telegram-bot[job-queue]')
    else:
        job_queue.run_daily(daily_reminder, time=REMINDER_TIME)

    app.run_polling()


if __name__ == "__main__":
    main()
