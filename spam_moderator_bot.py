#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram spam moderator bot
Requirements:
    pip install pyTelegramBotAPI
Run:
    export BOT_TOKEN="1234:ABC..."
    python3 spam_moderator_bot.py
"""
import os
import time
import threading
import logging
import re
from collections import defaultdict, deque

import telebot
from telebot import types

# ---------- Настройки ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN not set. Set BOT_TOKEN environment variable.")

SPAM_LIMIT = 10              # больше этого числа сообщений считается спамом
WINDOW_SECONDS = 10         # окно времени (секунд)
AUTO_MUTE_SECONDS = 12 * 3600  # 12 часов
DELETE_LAST_MESSAGES = 25    # сколько последних сообщений удалить при триггере

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = telebot.TeleBot(BOT_TOKEN, parse_mode='HTML')

# chat_id -> user_id -> deque of (timestamp, message_id)
recent_msgs = defaultdict(lambda: defaultdict(lambda: deque()))
recent_lock = threading.Lock()

# (chat_id, user_id) -> until_timestamp (unix)
active_mutes = {}
mutes_lock = threading.Lock()

duration_re = re.compile(r'(\d+)([smhdM])')  # s,m,h,d,M(months)

# ---------- Утилиты ----------
def parse_duration(s: str) -> int:
    s = (s or "").strip()
    if not s:
        raise ValueError("Empty duration")
    total = 0
    pos = 0
    for m in duration_re.finditer(s):
        if m.start() != pos:
            raise ValueError("Bad duration format near: " + s[pos:m.start()+1])
        v = int(m.group(1)); u = m.group(2)
        if u == 's': total += v
        elif u == 'm': total += v * 60
        elif u == 'h': total += v * 3600
        elif u == 'd': total += v * 86400
        elif u == 'M': total += v * 30 * 86400
        pos = m.end()
    if pos != len(s):
        raise ValueError("Bad duration format")
    if total <= 0:
        raise ValueError("Duration must be > 0")
    return total

def is_admin(chat_id: int, user_id: int) -> bool:
    try:
        member = bot.get_chat_member(chat_id, user_id)
        return member.status in ("administrator", "creator")
    except Exception as e:
        logger.exception("is_admin check failed: %s", e)
        return False

def restrict_user(chat_id: int, user_id: int, until_ts: int):
    perms = types.ChatPermissions(
        can_send_messages=False,
        can_send_media_messages=False,
        can_send_polls=False,
        can_send_other_messages=False,
        can_add_web_page_previews=False
    )
    bot.restrict_chat_member(chat_id, user_id, permissions=perms, until_date=until_ts)
    logger.info("Restricted %s in %s until %s", user_id, chat_id, until_ts)

def unrestrict_user(chat_id: int, user_id: int):
    perms = types.ChatPermissions(
        can_send_messages=True,
        can_send_media_messages=True,
        can_send_polls=True,
        can_send_other_messages=True,
        can_add_web_page_previews=True
    )
    bot.restrict_chat_member(chat_id, user_id, permissions=perms, until_date=None)
    logger.info("Unrestricted %s in %s", user_id, chat_id)

def ban_user(chat_id: int, user_id: int):
    bot.kick_chat_member(chat_id, user_id)
    logger.info("Banned %s from %s", user_id, chat_id)

def unban_user(chat_id: int, user_id: int):
    # allow_rejoin True -> previously kicked user can rejoin (unban)
    bot.unban_chat_member(chat_id, user_id)
    logger.info("Unbanned %s in %s", user_id, chat_id)

# callback_data: short form "U:<id>" or "B:<id>" to keep it small
def build_mute_keyboard(target_user_id: int):
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("Размутить", callback_data=f"U:{target_user_id}"))
    kb.add(types.InlineKeyboardButton("Бан", callback_data=f"B:{target_user_id}"))
    return kb

# ---------- Обработка сообщений (спам детект) ----------
@bot.message_handler(func=lambda m: True, content_types=['text','sticker','photo','video','audio','document','voice','animation','video_note','location','contact'])
def handle_all_messages(message: types.Message):
    # ignore private chats
    if message.chat.type == "private":
        return

    chat_id = message.chat.id
    user_id = message.from_user.id
    msg_id = message.message_id
    now = time.time()

    # store message
    with recent_lock:
        dq = recent_msgs[chat_id][user_id]
        dq.append((now, msg_id))
        # remove old
        cutoff = now - WINDOW_SECONDS
        while dq and dq[0][0] < cutoff:
            dq.popleft()
        count = len(dq)

    # If over limit -> auto mute
    if count > SPAM_LIMIT:
        key = (chat_id, user_id)
        with mutes_lock:
            if key in active_mutes:
                logger.debug("User %s in chat %s already muted", user_id, chat_id)
                return
            until_ts = int(now + AUTO_MUTE_SECONDS)
            try:
                restrict_user(chat_id, user_id, until_ts)
            except Exception as e:
                logger.exception("Failed to restrict: %s", e)
                try:
                    bot.reply_to(message, "Не могу замутить пользователя — проверь права бота (должен быть админ).")
                except Exception:
                    pass
                return
            active_mutes[key] = until_ts

        # delete last DELETE_LAST_MESSAGES messages (from that deque)
        with recent_lock:
            all_msgs = list(recent_msgs[chat_id][user_id])  # list of (ts, id)
        # sort by timestamp just in case and take last N
        all_msgs.sort(key=lambda x: x[0])
        to_delete = [mid for ts, mid in all_msgs[-DELETE_LAST_MESSAGES:]]
        deleted = 0
        for mid in to_delete:
            try:
                bot.delete_message(chat_id, mid)
                deleted += 1
            except Exception:
                # ignore any delete errors (message already deleted or permissions)
                pass
        logger.info("Auto-mute: deleted %d messages of user %s in chat %s", deleted, user_id, chat_id)

        # send notification with inline buttons
        user_mention = f"<a href='tg://user?id={user_id}'>{escape_html(message.from_user.first_name)}</a>"
        text = f"Пользователь {user_mention} автоматически замучен за спам на 12 часов."
        kb = build_mute_keyboard(user_id)
        try:
            bot.send_message(chat_id, text, reply_markup=kb)
        except Exception as e:
            logger.exception("Failed to send mute notification: %s", e)
        return

# ---------- Команды: /mute /ban /unmute /unban ----------
def extract_args(text: str):
    if not text:
        return []
    parts = text.strip().split(maxsplit=2)
    return parts[1:] if len(parts) > 1 else []

@bot.message_handler(commands=['mute'])
def cmd_mute(message: types.Message):
    chat_id = message.chat.id
    args = extract_args(message.text)
    if not message.reply_to_message and len(args) < 2:
        bot.reply_to(message, "Использование (reply): /mute 1d причина\nИли: /mute <user_id> <duration> [причина]")
        return

    # determine target
    if message.reply_to_message:
        target_user = message.reply_to_message.from_user
        if not args:
            bot.reply_to(message, "Укажи длительность, напр. /mute 1d причина")
            return
        duration_token = args[0]
        reason = args[1] if len(args) > 1 else ""
    else:
        try:
            uid = int(args[0])
        except ValueError:
            bot.reply_to(message, "user_id должен быть числом")
            return
        try:
            cm = bot.get_chat_member(chat_id, uid)
            target_user = cm.user
        except Exception as e:
            bot.reply_to(message, f"Не удалось найти участника {uid}: {e}")
            return
        duration_token = args[1]
        reason = args[2] if len(args) > 2 else ""

    try:
        seconds = parse_duration(duration_token)
    except Exception as e:
        bot.reply_to(message, f"Неправильный формат длительности: {e}")
        return

    until_ts = int(time.time() + seconds)
    try:
        restrict_user(chat_id, target_user.id, until_ts)
    except Exception:
        bot.reply_to(message, "Не удалось замутить пользователя (проверь права бота).")
        return

    with mutes_lock:
        active_mutes[(chat_id, target_user.id)] = until_ts

    user_mention = f"<a href='tg://user?id={target_user.id}'>{escape_html(target_user.first_name)}</a>"
    bot.reply_to(message, f"Пользователь {user_mention} замучен до {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(until_ts))}. Причина: {escape_html(reason)}")
    # send buttons for admins
    try:
        bot.send_message(chat_id, f"Пользователь {user_mention} замучен.", reply_markup=build_mute_keyboard(target_user.id))
    except Exception:
        pass

@bot.message_handler(commands=['ban'])
def cmd_ban(message: types.Message):
    chat_id = message.chat.id
    args = extract_args(message.text)
    if not message.reply_to_message and not args:
        bot.reply_to(message, "Использование (reply): /ban причина\nИли: /ban <user_id> [причина]")
        return

    if message.reply_to_message:
        target_user = message.reply_to_message.from_user
        reason = args[0] if args else ""
    else:
        try:
            uid = int(args[0])
        except ValueError:
            bot.reply_to(message, "user_id должен быть числом")
            return
        try:
            cm = bot.get_chat_member(chat_id, uid)
            target_user = cm.user
        except Exception as e:
            bot.reply_to(message, f"Не удалось найти участника {uid}: {e}")
            return
        reason = args[1] if len(args) > 1 else ""

    try:
        ban_user(chat_id, target_user.id)
    except Exception:
        bot.reply_to(message, "Не удалось забанить пользователя (проверь права бота).")
        return

    with mutes_lock:
        active_mutes.pop((chat_id, target_user.id), None)

    user_mention = f"<a href='tg://user?id={target_user.id}'>{escape_html(target_user.first_name)}</a>"
    bot.reply_to(message, f"Пользователь {user_mention} забанен. Причина: {escape_html(reason)}")

@bot.message_handler(commands=['unmute'])
def cmd_unmute(message: types.Message):
    chat_id = message.chat.id
    args = extract_args(message.text)
    if message.reply_to_message:
        target_user = message.reply_to_message.from_user
    else:
        if not args:
            bot.reply_to(message, "Использование: /unmute <user_id> или reply на сообщение")
            return
        try:
            uid = int(args[0])
            target_user = bot.get_chat_member(chat_id, uid).user
        except Exception as e:
            bot.reply_to(message, f"Не удалось: {e}")
            return
    try:
        unrestrict_user(chat_id, target_user.id)
    except Exception:
        bot.reply_to(message, "Не удалось размутить (проверь права бота).")
        return
    with mutes_lock:
        active_mutes.pop((chat_id, target_user.id), None)
    bot.reply_to(message, f"Пользователь <a href='tg://user?id={target_user.id}'>{escape_html(target_user.first_name)}</a> размучен.", parse_mode='HTML')

@bot.message_handler(commands=['unban'])
def cmd_unban(message: types.Message):
    chat_id = message.chat.id
    args = extract_args(message.text)
    if not args:
        bot.reply_to(message, "Использование: /unban <user_id>")
        return
    try:
        uid = int(args[0])
        unban_user(chat_id, uid)
    except Exception as e:
        bot.reply_to(message, f"Не удалось разбанить: {e}")
        return
    bot.reply_to(message, f"Пользователь {uid} разбанен.")

# ---------- Callback query (кнопки) ----------
@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call: types.CallbackQuery):
    logger.info("Callback received: %s from %s", call.data, call.from_user.id)
    data = (call.data or "").strip()
    if ":" not in data:
        bot.answer_callback_query(call.id, "Неправильные данные.")
        return
    action, sid = data.split(":", 1)
    try:
        target_id = int(sid)
    except ValueError:
        bot.answer_callback_query(call.id, "Неправильный id.")
        return

    chat_id = call.message.chat.id
    caller_id = call.from_user.id

    # только админы могут нажимать кнопки
    if not is_admin(chat_id, caller_id):
        bot.answer_callback_query(call.id, "Только администратор может нажимать эти кнопки.")
        return

    if action == "U":  # unmute
        try:
            unrestrict_user(chat_id, target_id)
        except Exception:
            bot.answer_callback_query(call.id, "Не удалось размутить (проверь права бота).")
            return
        with mutes_lock:
            active_mutes.pop((chat_id, target_id), None)
        try:
            bot.edit_message_text(chat_id=chat_id, message_id=call.message.message_id,
                                  text=f"Пользователь <a href='tg://user?id={target_id}'>пользователь</a> был размучен администратором.",
                                  parse_mode='HTML')
        except Exception:
            # возможно нельзя редактировать — просто уведомим
            pass
        bot.answer_callback_query(call.id, "Пользователь размучен.")
    elif action == "B":  # ban
        try:
            ban_user(chat_id, target_id)
        except Exception:
            bot.answer_callback_query(call.id, "Не удалось забанить (проверь права бота).")
            return
        with mutes_lock:
            active_mutes.pop((chat_id, target_id), None)
        try:
            bot.edit_message_text(chat_id=chat_id, message_id=call.message.message_id,
                                  text=f"Пользователь <a href='tg://user?id={target_id}'>пользователь</a> был забанен администратором.",
                                  parse_mode='HTML')
        except Exception:
            pass
        bot.answer_callback_query(call.id, "Пользователь забанен.")
    else:
        bot.answer_callback_query(call.id, "Неизвестное действие.")

# ---------- Очистка просроченных мьютов ----------
def mute_cleanup_loop():
    while True:
        now = int(time.time())
        to_unmute = []
        with mutes_lock:
            for (chat_id, user_id), until_ts in list(active_mutes.items()):
                if until_ts <= now:
                    to_unmute.append((chat_id, user_id))
            for k in to_unmute:
                active_mutes.pop(k, None)
        for chat_id, user_id in to_unmute:
            try:
                unrestrict_user(chat_id, user_id)
            except Exception:
                logger.exception("Auto unmute failed for %s in %s", user_id, chat_id)
        time.sleep(30)

cleanup_thread = threading.Thread(target=mute_cleanup_loop, daemon=True)
cleanup_thread.start()

# ---------- Helpers ----------
def escape_html(s: str) -> str:
    if s is None:
        return ""
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

# ---------- Run ----------
if __name__ == "__main__":
    logger.info("Bot started.")
    # long polling
    bot.infinity_polling(timeout=60, long_polling_timeout=65)
