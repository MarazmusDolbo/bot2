# telegram_spam_moderator_delete.py
import time
import threading
from collections import defaultdict, deque
import re
import telebot
from telebot import types

import os
bot = telebot.TeleBot(os.getenv("BOT_TOKEN"))

MAX_MSG = 10            # порог сообщений (если > MAX_MSG -> мут)
WINDOW_SECONDS = 10     # окно в секундах
MUTE_SECONDS = 12 * 3600  # 12 часов
CLEAN_SLEEP = 10        # интервал фонового потока в секундах
DELETE_LAST = 25        # сколько последних сообщений удалять
# -------------------------

# key: "chat_id:user_id" -> deque([timestamps...])
user_messages = defaultdict(lambda: deque())
# key: "chat_id:user_id" -> deque([message_ids...])
user_msg_ids = defaultdict(lambda: deque())
# muted users: key -> until_timestamp
muted_users = {}
lock = threading.Lock()

def key(chat_id: int, user_id: int) -> str:
    return f"{chat_id}:{user_id}"

def clean_old_timestamps(dq: deque, now: int):
    while dq and (now - dq[0][0]) > WINDOW_SECONDS:
        dq.popleft()

def bot_has_restrict_rights(chat_id: int) -> bool:
    try:
        me = bot.get_me()
        bot_member = bot.get_chat_member(chat_id, me.id)
        return bot_member.status in ('administrator', 'creator')
    except Exception:
        return False

def schedule_unmute_worker():
    def worker():
        while True:
            now = int(time.time())
            with lock:
                to_unmute = [k for k, until in muted_users.items() if now >= until]
                for k in to_unmute:
                    try:
                        chat_s, user_s = k.split(":")
                        chat_id = int(chat_s); user_id = int(user_s)
                        perms = types.ChatPermissions(
                            can_send_messages=True,
                            can_send_media_messages=True,
                            can_send_other_messages=True,
                            can_add_web_page_previews=True
                        )
                        bot.restrict_chat_member(chat_id, user_id, permissions=perms)
                    except Exception as e:
                        print("unmute error for", k, e)
                    finally:
                        if k in muted_users:
                            del muted_users[k]
            time.sleep(CLEAN_SLEEP)
    t = threading.Thread(target=worker, daemon=True)
    t.start()

def schedule_delete_worker():
    def worker():
        while True:
            with lock:
                for k in list(muted_users.keys()):
                    chat_id, user_id = map(int, k.split(":"))
                    dq_ids = user_msg_ids.get(k, deque())
                    while dq_ids:
                        msg_id = dq_ids.popleft()
                        try:
                            bot.delete_message(chat_id, msg_id)
                        except Exception:
                            pass
            time.sleep(1)
    t = threading.Thread(target=worker, daemon=True)
    t.start()

def parse_time_string(s: str) -> int:
    """Парсит время в секундах. Формат: 10s, 5m, 2h, 1d"""
    pattern = r"(\d+)([smHd])"
    m = re.match(pattern, s)
    if not m:
        return None
    val, unit = m.groups()
    val = int(val)
    unit = unit.lower()
    if unit == 's':
        return val
    elif unit == 'm':
        return val * 60
    elif unit == 'h':
        return val * 3600
    elif unit == 'd':
        return val * 86400
    return None

# -------------------- Команды модерации --------------------
@bot.message_handler(commands=['mute', 'ban'])
def on_command(message: types.Message):
    """Команды вида /mute 2d ответ_пользователю комментарий"""
    if message.chat.type != 'supergroup':
        return
    if not bot_has_restrict_rights(message.chat.id):
        bot.reply_to(message, "Бот должен быть админом с правами.")
        return
    if not message.reply_to_message:
        bot.reply_to(message, "Команду нужно использовать ответом на сообщение пользователя.")
        return

    parts = message.text.split()
    if len(parts) < 2:
        bot.reply_to(message, "Укажите время и комментарий: /mute 2d причина")
        return

    time_str = parts[1]
    duration = parse_time_string(time_str)
    if duration is None:
        bot.reply_to(message, "Неверный формат времени. Пример: 10s, 5m, 2h, 1d")
        return

    comment = " ".join(parts[2:]) if len(parts) > 2 else ""

    chat_id = message.chat.id
    user_id = message.reply_to_message.from_user.id
    k = key(chat_id, user_id)

    try:
        if message.text.startswith("/mute"):
            perms = types.ChatPermissions(
                can_send_messages=False,
                can_send_media_messages=False,
                can_send_other_messages=False,
                can_add_web_page_previews=False
            )
            until = int(time.time()) + duration
            bot.restrict_chat_member(chat_id, user_id, permissions=perms, until_date=until)
            with lock:
                muted_users[k] = until
            bot.send_message(chat_id,
                             f"⚠️ Пользователь <a href='tg://user?id={user_id}'>"
                             f"{message.reply_to_message.from_user.full_name}</a> замучен на {time_str}. {comment}",
                             parse_mode="HTML")
        elif message.text.startswith("/ban"):
            bot.kick_chat_member(chat_id, user_id)
            with lock:
                if k in muted_users: del muted_users[k]
                if k in user_messages: del user_messages[k]
                if k in user_msg_ids: del user_msg_ids[k]
            bot.send_message(chat_id,
                             f"⛔ Пользователь <a href='tg://user?id={user_id}'>"
                             f"{message.reply_to_message.from_user.full_name}</a> забанен. {comment}",
                             parse_mode="HTML")
    except Exception as e:
        bot.reply_to(message, f"Ошибка: {e}")

@bot.message_handler(commands=['unmute', 'unban'])
def on_unmute_unban_command(message: types.Message):
    """Команды /unmute и /unban через ответ на пользователя"""
    if message.chat.type != 'supergroup':
        return
    if not bot_has_restrict_rights(message.chat.id):
        bot.reply_to(message, "Бот должен быть админом с правами.")
        return
    if not message.reply_to_message:
        bot.reply_to(message, "Команду нужно использовать ответом на сообщение пользователя.")
        return

    chat_id = message.chat.id
    user_id = message.reply_to_message.from_user.id
    k = key(chat_id, user_id)
    comment = " ".join(message.text.split()[1:]) if len(message.text.split()) > 1 else ""

    try:
        if message.text.startswith("/unmute"):
            with lock:
                if k not in muted_users:
                    bot.reply_to(message, "Пользователь не находится в муте.")
                    return
                perms = types.ChatPermissions(
                    can_send_messages=True,
                    can_send_media_messages=True,
                    can_send_other_messages=True,
                    can_add_web_page_previews=True
                )
                bot.restrict_chat_member(chat_id, user_id, permissions=perms)
                del muted_users[k]
            bot.send_message(chat_id,
                             f"🔊 Пользователь <a href='tg://user?id={user_id}'>"
                             f"{message.reply_to_message.from_user.full_name}</a> был размучен. {comment}",
                             parse_mode="HTML")
        elif message.text.startswith("/unban"):
            try:
                bot.unban_chat_member(chat_id, user_id)
            except Exception as e:
                bot.reply_to(message, f"Ошибка при разбане: {e}")
                return
            with lock:
                if k in muted_users: del muted_users[k]
                if k in user_messages: del user_messages[k]
                if k in user_msg_ids: del user_msg_ids[k]
            bot.send_message(chat_id,
                             f"✅ Пользователь <a href='tg://user?id={user_id}'>"
                             f"{message.reply_to_message.from_user.full_name}</a> был разбанен. {comment}",
                             parse_mode="HTML")
    except Exception as e:
        bot.reply_to(message, f"Ошибка: {e}")

# -------------------- Обработка сообщений --------------------
@bot.message_handler(func=lambda m: True,
                     content_types=['text', 'sticker', 'photo', 'video', 'voice', 'animation', 'document'])
def on_message(message: types.Message):
    if message.chat.type != 'supergroup':
        return

    chat_id = message.chat.id
    user_id = message.from_user.id
    now = int(time.time())
    k = key(chat_id, user_id)

    if not bot_has_restrict_rights(chat_id):
        return

    with lock:
        dq_ids = user_msg_ids[k]
        dq_ids.append(message.message_id)
        while len(dq_ids) > DELETE_LAST:
            dq_ids.popleft()

        if k in muted_users:
            return

        dq_times = user_messages[k]
        dq_times.append((now, message.message_id))
        clean_old_timestamps(dq_times, now)

        if len(dq_times) > MAX_MSG:
            until = now + MUTE_SECONDS
            try:
                perms = types.ChatPermissions(
                    can_send_messages=False,
                    can_send_media_messages=False,
                    can_send_other_messages=False,
                    can_add_web_page_previews=False
                )
                bot.restrict_chat_member(chat_id, user_id, permissions=perms, until_date=until)
            except telebot.apihelper.ApiException as e:
                bot.send_message(chat_id, f"Ошибка при попытке замутить пользователя: {e}")
                return

            muted_users[k] = until
            user_messages[k].clear()

            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton("Размутить", callback_data=f"unmute:{chat_id}:{user_id}"))
            kb.add(types.InlineKeyboardButton("Забанить", callback_data=f"ban:{chat_id}:{user_id}"))

            name = message.from_user.full_name or message.from_user.username or str(user_id)
            text = (f"⚠️ Пользователь <a href='tg://user?id={user_id}'>{name}</a> "
                    f"замучен на 12 часов за спам: {MAX_MSG + 1}+ сообщений за {WINDOW_SECONDS} сек. "
                    f"Последние {DELETE_LAST} сообщений будут удалены.")
            bot.send_message(chat_id, text, reply_markup=kb, parse_mode="HTML")

# -------------------- Inline кнопки --------------------
@bot.callback_query_handler(func=lambda cq: True)
def on_callback(cq: types.CallbackQuery):
    data = cq.data or ""
    parts = data.split(":")
    if len(parts) != 3:
        bot.answer_callback_query(cq.id, "Неверная команда.")
        return

    action, chat_s, user_s = parts
    try:
        chat_id = int(chat_s)
        target_user_id = int(user_s)
    except ValueError:
        bot.answer_callback_query(cq.id, "Неверные данные.")
        return

    # проверяем, что нажимает админ
    try:
        invoker_id = cq.from_user.id
        inv_member = bot.get_chat_member(chat_id, invoker_id)
        if inv_member.status not in ('administrator', 'creator'):
            bot.answer_callback_query(cq.id, "Только админы могут нажимать эти кнопки.", show_alert=True)
            return
    except Exception:
        bot.answer_callback_query(cq.id, "Не удалось проверить права вызывающего.")
        return

    k = key(chat_id, target_user_id)
    try:
        target_name = bot.get_chat_member(chat_id, target_user_id).user.full_name
    except Exception:
        target_name = str(target_user_id)

    with lock:
        if action == "unmute":
            if k not in muted_users:
                bot.answer_callback_query(cq.id, "Пользователь не в муте.")
                return
            try:
                perms = types.ChatPermissions(
                    can_send_messages=True,
                    can_send_media_messages=True,
                    can_send_other_messages=True,
                    can_add_web_page_previews=True
                )
                bot.restrict_chat_member(chat_id, target_user_id, permissions=perms)
                del muted_users[k]
            except Exception as e:
                bot.answer_callback_query(cq.id, f"Ошибка размуты: {e}")
                return
            bot.answer_callback_query(cq.id, "Пользователь размучен.")
            bot.send_message(chat_id, f"🔊 Пользователь <a href='tg://user?id={target_user_id}'>{target_name}</a> был размучен админом.", parse_mode="HTML")

        elif action == "ban":
            try:
                bot.kick_chat_member(chat_id, target_user_id)
            except Exception as e:
                bot.answer_callback_query(cq.id, f"Ошибка при бане: {e}")
                return
            if k in muted_users: del muted_users[k]
            if k in user_messages: del user_messages[k]
            if k in user_msg_ids: del user_msg_ids[k]
            bot.answer_callback_query(cq.id, "Пользователь забанен.")
            bot.send_message(chat_id, f"⛔ Пользователь <a href='tg://user?id={target_user_id}'>{target_name}</a> был забанен админом.", parse_mode="HTML")
        else:
            bot.answer_callback_query(cq.id, "Неизвестное действие.")

if __name__ == "__main__":
    schedule_unmute_worker()
    schedule_delete_worker()
    print("Бот запущен...")
    bot.infinity_polling(timeout=60, long_polling_timeout=60)
