from asyncio import Lock, Semaphore, create_task, current_task, gather, shield, timeout, to_thread
import json
import logging
import os
import sqlite3
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from html import escape
from random import choice, random
from secrets import token_urlsafe
from time import monotonic

from openai import AsyncOpenAI, OpenAIError
from telegram import (
    BotCommand, BotCommandScopeChat, InlineKeyboardButton, InlineKeyboardMarkup,
    MenuButtonCommands, Message, ReplyKeyboardMarkup, Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, TypeHandler, filters

from mcp_bridge import SecretaryMCP
from replies import (
    ABUSE, COMMAND, HANDOFF_CALLBACK, IDENTITY_REPLY, REVIEW_INSTRUCTIONS, REVIEW_RISK,
    ROUTE_INSTRUCTIONS, TASK_CHECK_INSTRUCTIONS, VOICE_INSTRUCTIONS, business_question,
    conversational_reply, draft_route, is_model_question, masculine_reply, model_label,
    requires_refusal, short_reply, strip_emoji, task_evidence,
)
from state import Conversation, ManualChats, PendingTurn, Vocabulary

LOGGER = logging.getLogger(__name__)
INPUT_LIMIT = 4000
MAX_BUSINESS_CHATS = 100
PANEL_BUTTON = "Открыть панель управления"
OWNER_KEYBOARD = ReplyKeyboardMarkup([[PANEL_BUTTON]], resize_keyboard=True, is_persistent=True)
PANEL_PAGE_SIZE = 8
PANEL_HOME = InlineKeyboardButton("В панель", callback_data="chat_panel:home")
VOCABULARY_CHANCE = 0.20
HANDOFF_COOLDOWN_SECONDS = 120
RELAY_OFFER = "Чтобы передать сообщение владельцу, напишите «передай владельцу» и добавьте текст."
MODEL_UNAVAILABLE = "Сейчас не могу ответить. Позвать владельца?"
SCOPE_REFUSAL = "Могу передать это владельцу."
HANDOFF_KEYBOARD = InlineKeyboardMarkup([[InlineKeyboardButton(
    "Позвать владельца", callback_data=HANDOFF_CALLBACK
)]])

def telegram_parts(text):
    if len(text) <= 2048:
        if text:
            yield text
        return
    start = 0
    units = 0
    for index, char in enumerate(text):
        width = 2 if ord(char) > 0xFFFF else 1
        if units + width > 4096:
            yield text[start:index]
            start = index
            units = 0
        units += width
    if start < len(text):
        yield text[start:]


class SecretaryBot:
    def __init__(self, client, model, owner_id, chat_completions=False, business_auto_reply=False):
        self.client = client
        self.model = model
        self.owner_id = owner_id
        self.chat_completions = chat_completions
        self.business_auto_reply = business_auto_reply
        self.owner_updates = Lock()
        self.pending_turns = {}
        self.conversations = OrderedDict()
        self.business_model_slots = Semaphore(4)
        data_dir = os.environ.get("DATA_DIR", "/app/data")
        self.manual_chats = ManualChats(data_dir)
        self.vocabulary = Vocabulary(data_dir)
        self.business_connections = {}
        self.connection_requests = {}
        self.handoff_requests = {}
        self.handoff_notices = OrderedDict()
        self.scope_refusals = OrderedDict()
        self.model_notices = OrderedDict()
        self.model_retry_at = 0.0
        self.mcp = SecretaryMCP(self)

    async def on_startup(self, app):
        try:
            await app.bot.set_my_commands(
                [BotCommand("panel", "Открыть панель управления")],
                scope=BotCommandScopeChat(self.owner_id),
            )
            await app.bot.set_chat_menu_button(chat_id=self.owner_id, menu_button=MenuButtonCommands())
        except TelegramError as error:
            LOGGER.warning("Не удалось настроить меню владельца: %s", type(error).__name__)
        await self.mcp.start()
        LOGGER.info("Личный секретарь запущен")
        if app.bot.bot.can_connect_to_business is False:
            LOGGER.info("Для подключения профиля включите Secretary Mode в BotFather")
        if self.business_auto_reply:
            LOGGER.info("Автоответы в подключённых чатах включены в настройках хостинга")

    async def on_shutdown(self, app):
        try:
            await self.mcp.stop()
        finally:
            await gather(*tuple(self.connection_requests.values()), *tuple(self.handoff_requests.values()),
                         return_exceptions=True)
            await self.manual_chats.flush()
            if self.client is not None:
                await self.client.close()

    def ordered_owner_handler(self, handler):
        async def handle(update, context):
            if update.effective_user.id != self.owner_id:
                return await handler(update, context)
            async with self.owner_updates:
                return await handler(update, context)

        return handle

    def model_status(self):
        if self.client is None:
            return "не настроена"
        if monotonic() < self.model_retry_at:
            return "временно недоступна (HTTP 503)"
        return "подключена"

    def status_text(self):
        automatic = "включены" if self.business_auto_reply else "выключены"
        return (
            f"Модель {self.model_status()}. Автоответы {automatic}. "
            f"Чатов на ручной паузе: {self.manual_chats.paused_count}."
        )

    def panel_view(self):
        automatic = "включены" if self.business_auto_reply else "выключены"
        text = (
            "<b>Панель разработчика</b>\n"
            "<i>Telegram-секретарь</i>\n\n"
            f"<b>Автоответы</b> · {automatic}\n"
            f"<b>Модель</b> · {self.model_status()}\n"
            f"<b>Чатов на паузе</b> · {self.manual_chats.paused_count}"
        )
        keyboard = [
            [InlineKeyboardButton("Чаты и паузы", callback_data="chat_panel:0"),
             InlineKeyboardButton("Словарь", callback_data="chat_panel:words:0")],
            [InlineKeyboardButton("Статус системы", callback_data="chat_panel:status"),
             InlineKeyboardButton("Обновить", callback_data="chat_panel:home")],
        ]
        return text, InlineKeyboardMarkup(keyboard)

    def status_view(self):
        automatic = "включены" if self.business_auto_reply else "выключены"
        lines = [
            "<b>Статус системы</b>", "",
            f"<b>Автоответы</b> · {automatic}",
            f"<b>Модель</b> · {self.model_status()}",
        ]
        if self.client is not None:
            model = escape(strip_emoji(self.model)[:160])
            interface = "Chat Completions" if self.chat_completions else "Responses"
            lines.extend((f"<b>Имя модели</b> · <code>{model}</code>", f"<b>API</b> · {interface}"))
        mcp = "запущен" if self.mcp.task and not self.mcp.task.done() else "отключён"
        lines.append(f"<b>MCP</b> · {mcp}")
        return "\n".join(lines), InlineKeyboardMarkup([
            [InlineKeyboardButton("Обновить статус", callback_data="chat_panel:status")],
            [PANEL_HOME],
        ])

    async def chats_view(self, bot, page):
        state = self.manual_chats
        paused = state.paused
        ids = sorted(paused | state.names.keys(),
                     key=lambda cid: (cid not in paused, state.names.get(cid, "").casefold(), cid))
        pages = max(1, (len(ids) + PANEL_PAGE_SIZE - 1) // PANEL_PAGE_SIZE)
        page = min(max(page, 0), pages - 1)
        visible = ids[page * PANEL_PAGE_SIZE:(page + 1) * PANEL_PAGE_SIZE]

        async def fill_name(chat_id):
            try:
                async with timeout(1.5):
                    chat = await bot.get_chat(chat_id)
            except (TelegramError, TimeoutError):
                return
            if chat.id == chat_id and chat.type == "private":
                await state.record_async(chat)

        missing = [cid for cid in visible if cid not in state.names]
        if missing:
            await gather(*(fill_name(cid) for cid in missing))
        paused = state.paused
        lines = ["<b>Чаты и паузы</b>", f"Всего чатов · {len(ids)}    На паузе · {len(paused)}"]
        if not self.business_auto_reply:
            lines.append("Автоответы отключены в настройках хостинга.")
        if visible:
            lines.append("")
            for cid in visible:
                name = strip_emoji(state.names.get(cid, "")) or f"ID {cid}"
                label = "на паузе" if cid in paused else "без ручной паузы"
                lines.append(f"<b>{escape(name)}</b> · {label}")
        else:
            lines.extend(("", "Пока нет известных чатов."))
        keyboard = []
        if paused:
            lines.extend(("", "<i>Ручная пауза длится 30 минут с последнего сообщения владельца.</i>"))
            keyboard.append([InlineKeyboardButton("Снять все ручные паузы", callback_data="chat_panel:resume")])
        if pages > 1:
            lines.extend(("", f"<i>Страница {page + 1} из {pages}</i>"))
            navigation = []
            if page:
                navigation.append(InlineKeyboardButton("Предыдущая", callback_data=f"chat_panel:{page - 1}"))
            if page + 1 < pages:
                navigation.append(InlineKeyboardButton("Далее", callback_data=f"chat_panel:{page + 1}"))
            keyboard.append(navigation)
        keyboard.append([PANEL_HOME, InlineKeyboardButton("Обновить", callback_data=f"chat_panel:{page}")])
        return "\n".join(lines), InlineKeyboardMarkup(keyboard)

    async def vocabulary_view(self, page):
        try:
            entries = await to_thread(self.vocabulary.entries)
        except (OSError, sqlite3.Error) as error:
            LOGGER.warning("Не удалось открыть словарь: %s", type(error).__name__)
            return "<b>Словарь</b>\n\nСловарь сейчас недоступен.", InlineKeyboardMarkup([[PANEL_HOME]])

        page_count = max(1, (len(entries) + PANEL_PAGE_SIZE - 1) // PANEL_PAGE_SIZE)
        page = min(page, page_count - 1)
        first = page * PANEL_PAGE_SIZE
        rows = [[InlineKeyboardButton("Добавить слово", callback_data="chat_panel:add_word")]]
        rows.extend(
            [InlineKeyboardButton(f"Удалить «{strip_emoji(word)}»", callback_data=f"chat_panel:remove_word:{word_id}:{page}")]
            for word_id, word in entries[first:first + PANEL_PAGE_SIZE]
        )
        navigation = []
        if page:
            navigation.append(InlineKeyboardButton("Предыдущая", callback_data=f"chat_panel:words:{page - 1}"))
        if page + 1 < page_count:
            navigation.append(InlineKeyboardButton("Далее", callback_data=f"chat_panel:words:{page + 1}"))
        if navigation:
            rows.append(navigation)
        rows.append([PANEL_HOME])
        text = "<b>Словарь</b>\n\n"
        text += f"Записей · {len(entries)}" if entries else "Добавь слово или короткое выражение для автоответов."
        if page_count > 1:
            text += f"\nСтраница {page + 1} из {page_count}"
        return text, InlineKeyboardMarkup(rows)

    async def panel(self, update, context):
        context.chat_data.pop("adding_word", None)
        context.chat_data.pop("panel_confirmation", None)
        text, keyboard = self.panel_view()
        message = update.effective_message
        await message.reply_text(text, do_quote=False, reply_markup=keyboard, parse_mode=ParseMode.HTML)

    async def refresh_panel(self, update, context):
        query = update.callback_query
        message = query.message
        if (query.from_user.id != self.owner_id or not isinstance(message, Message)
                or message.chat.type != "private" or message.chat.id != self.owner_id
                or message.business_connection_id):
            await query.answer()
            return
        await query.answer()
        action = query.data.partition(":")[2]
        back_to_chats = InlineKeyboardButton("К чатам", callback_data="chat_panel:0")
        context.chat_data.pop("adding_word", None)
        if not action.startswith("confirm:"):
            context.chat_data.pop("panel_confirmation", None)
        if action == "home":
            text, keyboard = self.panel_view()
        elif action == "add_word":
            context.chat_data["adding_word"] = True
            text = "<b>Добавить в словарь</b>\n\nОтправь одно слово или выражение до 40 символов."
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("Отмена", callback_data="chat_panel:words:0"),
            ]])
        elif action.startswith("words:"):
            text, keyboard = await self.vocabulary_view(int(action.split(":")[1]))
        elif action.startswith("remove_word:"):
            _, word_id, page = action.split(":")
            try:
                await to_thread(self.vocabulary.remove, int(word_id))
            except (OSError, sqlite3.Error) as error:
                LOGGER.warning("Не удалось удалить слово: %s", type(error).__name__)
                text = "Не удалось удалить слово. Попробуйте ещё раз."
                keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton("В словарь", callback_data=f"chat_panel:words:{page}"),
                ]])
            else:
                text, keyboard = await self.vocabulary_view(int(page))
        elif action == "status":
            text, keyboard = self.status_view()
        elif action == "resume":
            affected = frozenset(self.manual_chats.paused)
            if affected:
                token = token_urlsafe(9)
                context.chat_data["panel_confirmation"] = (message.message_id, token, affected, monotonic())
                text = f"<b>Снять ручные паузы?</b>\n\nЧатов на паузе · {len(affected)}"
                keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton("Снять паузы", callback_data=f"chat_panel:confirm:{token}"),
                    InlineKeyboardButton("Отмена", callback_data="chat_panel:0"),
                ]])
            else:
                text, keyboard = await self.chats_view(context.bot, 0)
        elif action.startswith("confirm:"):
            pending = context.chat_data.get("panel_confirmation")
            if (pending is None or pending[0] != message.message_id or pending[1] != action.partition(":")[2]
                    or monotonic() - pending[3] > 90):
                text = "Подтверждение устарело. Откройте действие ещё раз."
            else:
                context.chat_data.pop("panel_confirmation")
                if pending[2] != self.manual_chats.paused:
                    text = "Список пауз изменился. Откройте действие ещё раз."
                else:
                    count = await self.manual_chats.resume_async()
                    text = f"<b>Ручные паузы сняты</b>\n\nЧатов · {count}"
            keyboard = InlineKeyboardMarkup([[back_to_chats, PANEL_HOME]])
        else:
            text, keyboard = await self.chats_view(context.bot, int(action))
        try:
            await query.edit_message_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
        except BadRequest as error:
            if "message is not modified" not in str(error).casefold():
                raise

    def model_info(self):
        return f"Для ответов настроена модель {strip_emoji(self.model)}." if self.client else "Сейчас модель отключена."

    async def reply(self, update, context):
        message = update.effective_message
        if context.chat_data.get("adding_word"):
            try:
                word = await to_thread(self.vocabulary.add, message.text)
            except ValueError as error:
                await message.reply_text(str(error), do_quote=False)
            except (OSError, sqlite3.Error) as error:
                LOGGER.warning("Не удалось сохранить слово: %s", type(error).__name__)
                await message.reply_text("Не удалось сохранить слово. Попробуйте ещё раз.", do_quote=False)
            else:
                context.chat_data.pop("adding_word")
                text, keyboard = await self.vocabulary_view(0)
                await message.reply_text(
                    f"{text}\n\nДобавлено «{escape(strip_emoji(word))}».", do_quote=False,
                    reply_markup=keyboard, parse_mode=ParseMode.HTML,
                )
            return
        if is_model_question(message.text):
            await message.reply_text(self.model_info(), do_quote=False, reply_markup=OWNER_KEYBOARD)
            return
        if self.client is None:
            await message.reply_text("Для ответов нужно настроить подключение к модели.",
                                     reply_markup=OWNER_KEYBOARD)
            return
        text = message.text.strip()
        if not text:
            return
        if len(text) > INPUT_LIMIT:
            await message.reply_text(f"Сократите сообщение до {INPUT_LIMIT} символов.", reply_markup=OWNER_KEYBOARD)
            return

        answer = await self.ask_model(
            "Ты помощник владельца. Отвечай на текущее сообщение ясно и по существу. " + VOICE_INSTRUCTIONS,
            [{"role": "user", "content": text}], max_tokens=600,
        )
        if answer:
            answer = strip_emoji(masculine_reply(answer))
        if not answer:
            await message.reply_text("Сейчас не получилось ответить. Попробуйте чуть позже.", reply_markup=OWNER_KEYBOARD)
            return
        parts = list(telegram_parts(answer))
        for part in parts[:-1]:
            await message.reply_text(part, do_quote=False, disable_web_page_preview=True)
        await message.reply_text(parts[-1], do_quote=False, disable_web_page_preview=True, reply_markup=OWNER_KEYBOARD)

    async def business_state(self, update, context):
        connection = update.business_connection
        if connection:
            self.connection_requests.pop(connection.id, None)
            self.business_connections.pop(connection.id, None)
        if connection and connection.user.id == self.owner_id:
            self.business_connections[connection.id] = connection
            for key in list(self.conversations.keys() | self.pending_turns.keys()):
                if key[0] != connection.id:
                    continue
                self.discard_turn(key)
            ready = connection.is_enabled and connection.rights and connection.rights.can_reply
            LOGGER.info("Автоматизация чатов: %s", "ответы разрешены" if ready else "ответы выключены")
        edited = update.edited_business_message
        deleted = update.deleted_business_messages
        notices = (edited, deleted)
        for notice in notices:
            if notice:
                key = (notice.business_connection_id, notice.chat.id)
                self.discard_turn(key)

    async def eligible_connection(self, bot, connection_id):
        if not self.business_auto_reply or not connection_id:
            return None
        connection = self.business_connections.get(connection_id)
        if connection is None:
            request = self.connection_requests.get(connection_id)
            if request is None:
                request = create_task(self.fetch_connection(bot, connection_id))
                self.connection_requests[connection_id] = request
            error = await shield(request)
            connection = self.business_connections.get(connection_id)
            if connection is None and error is not None:
                raise error
        if connection and connection.is_enabled and connection.rights and connection.rights.can_reply:
            return connection
        return None

    async def fetch_connection(self, bot, connection_id):
        try:
            connection = await bot.get_business_connection(connection_id)
            if self.connection_requests.get(connection_id) is current_task() and connection.user.id == self.owner_id:
                self.business_connections[connection_id] = connection
        except TelegramError as error:
            return error
        finally:
            if self.connection_requests.get(connection_id) is current_task():
                del self.connection_requests[connection_id]

    @staticmethod
    async def send_business(bot, message, text, **options):
        for part in telegram_parts(strip_emoji(text)):
            await bot.send_message(chat_id=message.chat.id, text=part,
                                   business_connection_id=message.business_connection_id,
                                   disable_web_page_preview=True, **options)

    async def notify_owner(self, bot, connection, chat_id, sender, excerpt):
        key = (connection.id, chat_id)
        previous = self.handoff_notices.get(key)
        if previous is not None and monotonic() - previous < HANDOFF_COOLDOWN_SECONDS:
            return "Владелец уже получил уведомление."
        request = self.handoff_requests.get(key)
        if request is None:
            request = create_task(self.send_owner_notice(bot, connection, key, sender, excerpt))
            self.handoff_requests[key] = request
        return await shield(request)

    async def send_owner_notice(self, bot, connection, key, sender, excerpt):
        try:
            return await self.deliver_owner_notice(bot, connection, key, sender, excerpt)
        finally:
            del self.handoff_requests[key]

    async def deliver_owner_notice(self, bot, connection, key, sender, excerpt):
        who = strip_emoji(sender.full_name[:150]) or f"ID {sender.id}"
        if sender.username:
            who += f" (@{strip_emoji(sender.username)})"
        notification = f"{who} просит вас ответить в Telegram."
        excerpt = strip_emoji(excerpt[:500])
        if excerpt:
            notification += f"\nСообщение: {excerpt}"
        try:
            await bot.send_message(chat_id=connection.user_chat_id, text=notification,
                                   disable_web_page_preview=True, reply_markup=OWNER_KEYBOARD)
        except TelegramError as error:
            LOGGER.warning("Не удалось уведомить владельца: %s", type(error).__name__)
            return "Уведомление не дошло. Попробуйте чуть позже."
        self.handoff_notices[key] = monotonic()
        self.handoff_notices.move_to_end(key)
        if len(self.handoff_notices) > MAX_BUSINESS_CHATS:
            self.handoff_notices.popitem(last=False)
        return "Владелец получил уведомление."

    async def handoff_button(self, update, context):
        query = update.callback_query
        message = query.message
        if (not isinstance(message, Message) or not message.business_connection_id
                or message.chat.type != "private"
                or query.from_user.id != message.chat.id
                or datetime.now(timezone.utc) - message.date >= timedelta(hours=24)):
            await query.answer("Напишите в чате «позови владельца».")
            return

        if self.manual_chats.is_paused(message.chat.id):
            await query.answer("В этом чате автоответы на паузе.")
            return
        await query.answer()
        connection = await self.eligible_connection(context.bot, message.business_connection_id)
        if not connection or self.manual_chats.is_paused(message.chat.id):
            return
        answer = await self.notify_owner(context.bot, connection, message.chat.id, query.from_user, "")
        await self.send_business(context.bot, message, answer)

    def discard_turn(self, key):
        self.conversations.pop(key, None)
        turn = self.pending_turns.pop(key, None)
        if turn is not None:
            turn.cancel_request()

    async def business_reply(self, update, context):
        message = update.business_message
        sender = message.from_user
        connection_id = message.business_connection_id
        if (not self.business_auto_reply or not connection_id or sender is None
                or sender.is_bot or message.sender_business_bot
                or datetime.now(timezone.utc) - message.date >= timedelta(hours=24)):
            return
        key = (connection_id, message.chat.id)
        if sender.id == self.owner_id:
            if message.is_from_offline:
                return
            self.discard_turn(key)
            if await self.eligible_connection(context.bot, connection_id):
                self.manual_chats.record(message.chat, pause_from=message.date.timestamp())
            return
        if self.manual_chats.is_paused(message.chat.id):
            if (self.manual_chats.chat_name(message.chat) != self.manual_chats.names.get(message.chat.id)
                    and await self.eligible_connection(context.bot, connection_id)):
                self.manual_chats.record(message.chat)
            return
        text = (message.text or "").strip()
        if not text or len(text) > INPUT_LIMIT or COMMAND.match(text):
            return
        turn = self.pending_turns.get(key)
        if turn is None and len(self.pending_turns) >= MAX_BUSINESS_CHATS:
            return
        conversation = turn.conversation if turn else self.conversations.get(key)
        if conversation is None:
            conversation = Conversation()
        if not conversation.append(message, text):
            return
        self.conversations[key] = conversation
        self.conversations.move_to_end(key)
        if len(self.conversations) > MAX_BUSINESS_CHATS:
            self.conversations.popitem(last=False)
        if turn is not None:
            turn.message = message
            return
        turn = PendingTurn(conversation, message)
        self.pending_turns[key] = turn
        context.application.create_task(self.process_turns(context.bot, key, turn), update=update)

    async def process_turns(self, bot, key, turn):
        try:
            while self.pending_turns.get(key) is turn:
                message = turn.message
                try:
                    await self.reply_to_turn(bot, key, turn, message)
                except TelegramError as error:
                    if turn.message is message:
                        raise
                    LOGGER.warning("Ошибка Telegram при обработке реплик: %s", type(error).__name__)
                if turn.message is message:
                    turn.conversation.handled_message_id = message.message_id
                    return
        finally:
            if self.pending_turns.get(key) is turn:
                self.pending_turns.pop(key)

    async def reply_to_turn(self, bot, key, turn, message):
        previous, new = turn.conversation.snapshot(message.date)
        text = "\n".join(new)
        model_input = {"предыдущие предложения": previous, "новые предложения": new}
        started = monotonic()

        def current():
            return (self.pending_turns.get(key) is turn and turn.message is message
                    and not self.manual_chats.is_paused(message.chat.id))

        async def respond(answer, **options):
            if not current():
                return False
            turn.conversation.handled_message_id = message.message_id
            await self.send_business(bot, message, answer, **options)
            LOGGER.info("Автоответ отправлен за %.2f с после начала обработки.", monotonic() - started)
            return True

        async def send_notice(notices, cooldown, text):
            last = notices.get(key)
            if last is not None and monotonic() - last < cooldown:
                return
            if await respond(text, reply_markup=HANDOFF_KEYBOARD):
                notices[key] = monotonic()
                notices.move_to_end(key)
                if len(notices) > MAX_BUSINESS_CHATS:
                    notices.popitem(last=False)

        connection = await self.eligible_connection(bot, message.business_connection_id)
        if not connection or not current():
            return
        # Recording a display name queues a write. The reply can go out while it runs.
        self.manual_chats.record(message.chat)
        question = business_question(text)
        if question == "handoff":
            answer = await self.notify_owner(bot, connection, message.chat.id, message.from_user, text)
            await respond(answer)
            return
        if question == "model":
            await respond(self.model_info())
            return
        if question == "identity":
            await respond(IDENTITY_REPLY, reply_markup=HANDOFF_KEYBOARD)
            return
        if requires_refusal(text):
            await send_notice(self.scope_refusals, 45, SCOPE_REFUSAL)
            return
        if any(ABUSE.search(sentence) for sentence in new):
            return
        immediate = short_reply(text) if not previous else None
        if immediate:
            await respond(immediate)
            return
        if self.client is None:
            return

        draft_input = model_input.copy()
        if random() < VOCABULARY_CHANCE:
            try:
                words = await to_thread(self.vocabulary.entries_if_present)
            except (OSError, sqlite3.Error) as error:
                LOGGER.warning("Не удалось прочитать словарь: %s", type(error).__name__)
            else:
                if words:
                    draft_input["слово из словаря"] = choice(words)[1]
        current_message = [{"role": "user", "content": json.dumps(draft_input, ensure_ascii=False)}]
        async with self.business_model_slots:
            if not current():
                return
            raw = await turn.run_request(self.ask_model(ROUTE_INSTRUCTIONS, current_message, max_tokens=96))
            if not current():
                return
            mode, candidate = draft_route(raw or "")
            verdict = ""
            check_task = mode == "TASK" and task_evidence(candidate, text)
            if mode in {"CHAT", "REVIEW"}:
                answer = conversational_reply(candidate)
                visible = strip_emoji(answer)
                if not visible or ABUSE.search(answer) or (visible != answer and ABUSE.search(visible)):
                    return
                raw_risk = REVIEW_RISK.search(answer)
                visible_risk = not raw_risk and visible != answer and REVIEW_RISK.search(visible)
                if mode == "REVIEW" or raw_risk or visible_risk:
                    review = json.dumps({**model_input, "предложенный ответ": visible if visible_risk else answer},
                                        ensure_ascii=False)
                    verdict = await turn.run_request(self.ask_model(
                        REVIEW_INSTRUCTIONS, [{"role": "user", "content": review}], max_tokens=16,
                    ))
                else:
                    verdict = "SEND"
            elif check_task:
                evidence = json.dumps({"новые предложения": new, "цитата просьбы": candidate}, ensure_ascii=False)
                verdict = await turn.run_request(self.ask_model(
                    TASK_CHECK_INSTRUCTIONS, [{"role": "user", "content": evidence}], max_tokens=16,
                ))
        if not current():
            return
        if not raw or ((mode in {"CHAT", "REVIEW"} or check_task) and not verdict):
            await send_notice(self.model_notices, 60, MODEL_UNAVAILABLE)
            return
        if mode in {"CHAT", "REVIEW"} and model_label(verdict, {"SEND", "DROP"}) == "SEND":
            await respond(answer)
        elif mode == "OWNER":
            await respond(RELAY_OFFER, reply_markup=HANDOFF_KEYBOARD)
        elif mode == "TASK" and model_label(verdict, {"TASK", "NO"}) == "TASK":
            await send_notice(self.scope_refusals, 45, SCOPE_REFUSAL)
        elif mode in {"UNCLEAR", "TASK"} or not mode:
            await respond("Не совсем понял. Ты про что?")

    async def ask_model(self, instructions, messages, *, max_tokens):
        if monotonic() < self.model_retry_at:
            return ""
        started = monotonic()
        try:
            if self.chat_completions:
                response = await self.client.chat.completions.create(
                    model=self.model,
                    messages=[{"role": "system", "content": instructions}, *messages],
                    max_tokens=max_tokens, store=False,
                )
                answer = response.choices[0].message.content if response.choices else None
            else:
                response = await self.client.responses.create(
                    model=self.model, instructions=instructions, input=messages,
                    max_output_tokens=max_tokens, store=False,
                )
                answer = response.output_text
        except OpenAIError as error:
            status = getattr(error, "status_code", None)
            LOGGER.warning(
                "Ошибка модели после %.2f с (%s, HTTP %s)",
                monotonic() - started, type(error).__name__, status or "—",
            )
            if status == 503:
                response = getattr(error, "response", None)
                header = response.headers.get("Retry-After", "") if response is not None else ""
                retry_after = int(header) if header.isascii() and header.isdecimal() and len(header) <= 9 else 0
                self.model_retry_at = max(self.model_retry_at, monotonic() + max(0.65, retry_after))
            return ""
        LOGGER.info("Модель ответила за %.2f с.", monotonic() - started)
        return (answer or "").strip()

async def report_error(update, context):
    LOGGER.error("Ошибка при обработке обновления: %s", type(context.error).__name__)


def main(owner_id: int):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    for name in ("httpx", "httpcore", "telegram"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip() or os.environ.get("BOT_TOKEN", "").strip()
    key = os.environ.get("MODEL_API_KEY", "").strip() or os.environ.get("OPENAI_API_KEY", "").strip()
    local_url = os.environ.get("LOCAL_MODEL_URL", "").strip()
    external_url = os.environ.get("MODEL_BASE_URL", "").strip()
    base_url = local_url or external_url
    default_model = "qwen2.5:3b" if local_url else "gpt-4.1-mini"
    model = os.environ.get("MODEL_NAME", "").strip() or os.environ.get("OPENAI_MODEL", "").strip()
    if not token:
        LOGGER.error("Укажите токен Telegram-бота в TELEGRAM_BOT_TOKEN.")
        return 1

    try:
        owner_id = int(os.environ.get("TELEGRAM_OWNER_ID", str(owner_id)))
        if not 0 < owner_id < 2**52:
            raise ValueError
    except ValueError:
        LOGGER.error("TELEGRAM_OWNER_ID должен содержать числовой Telegram ID владельца.")
        return 1

    client = None
    if (key or local_url) and (model or not external_url):
        if base_url:
            client = AsyncOpenAI(base_url=base_url, api_key="ollama" if local_url else key,
                                 timeout=120 if local_url else 40, max_retries=0)
        else:
            client = AsyncOpenAI(api_key=key, timeout=25, max_retries=0)
    else:
        LOGGER.info("Модель не подключена")
    bot = SecretaryBot(
        client, model or default_model, owner_id,
        chat_completions=bool(base_url),
        business_auto_reply=os.environ.get("BUSINESS_AUTO_REPLY", "").strip() == "1",
    )
    try:
        app = (
            Application.builder().token(token)
            .post_init(bot.on_startup).post_shutdown(bot.on_shutdown).build()
        )
        owner_chat = filters.UpdateType.MESSAGE & filters.ChatType.PRIVATE & filters.User(user_id=bot.owner_id)
        business_chat = filters.UpdateType.BUSINESS_MESSAGE & filters.ChatType.PRIVATE
        plain_text = filters.TEXT & ~filters.COMMAND
        app.add_handler(TypeHandler(Update, bot.business_state), group=-1)
        # Keep commands and panel actions in order without holding up business updates.
        panel_handler = bot.ordered_owner_handler(bot.panel)
        app.add_handler(CommandHandler(("start", "panel"), panel_handler, filters=owner_chat, block=False))
        app.add_handler(MessageHandler(
            owner_chat & filters.Regex(rf"(?i)^\s*(?:{PANEL_BUTTON}|меню)[!. ]*\s*$"), panel_handler, block=False,
        ))
        app.add_handler(MessageHandler(owner_chat & plain_text, bot.ordered_owner_handler(bot.reply), block=False))
        app.add_handler(MessageHandler(business_chat, bot.business_reply))
        app.add_handler(CallbackQueryHandler(bot.handoff_button, pattern=r"^" + HANDOFF_CALLBACK + r"$"))
        app.add_handler(CallbackQueryHandler(
            bot.ordered_owner_handler(bot.refresh_panel), pattern=(
                r"^chat_panel:(?:[0-9]{1,6}|home|status|resume|confirm:[A-Za-z0-9_-]{12}"
                r"|words:[0-9]{1,3}|add_word|remove_word:[0-9]{1,18}:[0-9]{1,3})$"
            ), block=False,
        ))
        app.add_error_handler(report_error)
        app.run_polling(
            allowed_updates=[
                Update.MESSAGE, Update.BUSINESS_CONNECTION, Update.BUSINESS_MESSAGE,
                Update.EDITED_BUSINESS_MESSAGE, Update.DELETED_BUSINESS_MESSAGES, Update.CALLBACK_QUERY,
            ],
            bootstrap_retries=0,
        )
    except TelegramError as error:
        LOGGER.error("Не удалось запустить Telegram-бота: %s", type(error).__name__)
        return 1
    except (OSError, RuntimeError, ValueError) as error:
        LOGGER.error("Ошибка запуска секретаря: %s", error)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(owner_id=906361129))
