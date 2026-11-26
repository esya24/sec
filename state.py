from asyncio import CancelledError, Task, create_task, current_task, get_running_loop, shield, to_thread
import json
import logging
import os
import re
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import time
from unicodedata import normalize

from telegram import Message


LOGGER = logging.getLogger(__name__)
PAUSE_SECONDS = 30 * 60


@dataclass(slots=True)
class PendingTurn:
    message: Message | None = None
    revision: int = 0
    request: Task | None = None
    latest_message_id: int | None = None

    def append(self, message):
        if self.latest_message_id is not None and self.latest_message_id >= message.message_id:
            return
        self.latest_message_id = message.message_id
        self.message = message
        self.revision += 1
        self.cancel_request()

    def cancel_request(self):
        if self.request is not None:
            self.request.cancel()

    async def run_request(self, coroutine):
        request = create_task(coroutine)
        self.request = request
        try:
            return await request
        except CancelledError:
            if current_task().cancelling():
                raise
            return ""
        finally:
            self.request = None


class ManualChats:
    def __init__(self, data_dir):
        self.path = Path(data_dir) / "manual-chats.json"
        self.names = {}
        self.deadlines = {}
        self._next_expiry = float("inf")
        self._writing = self._queued = None
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
            pauses = state.get("paused", {}) if isinstance(state, dict) else state
            legacy = isinstance(pauses, list)
            if legacy and all(type(chat_id) is int for chat_id in pauses):
                deadline = time() + PAUSE_SECONDS
                self.deadlines = dict.fromkeys(pauses, deadline)
            elif isinstance(pauses, dict) and all(
                key.isascii() and key.isdecimal() and type(value) in (int, float)
                and isfinite(value) for key, value in pauses.items()
            ):
                self.deadlines = {int(key): value for key, value in pauses.items()}
            else:
                raise ValueError("invalid manual chat state")
            self._next_expiry = min(self.deadlines.values(), default=float("inf"))
            names = state.get("names", {}) if isinstance(state, dict) else {}
            if isinstance(names, dict):
                self.names = {int(k): " ".join(v.split())[:90] for k, v in names.items()
                              if k.isascii() and k.isdecimal() and isinstance(v, str) and v.strip()}
            if legacy:
                self._write(self.deadlines, self.names)
        except FileNotFoundError:
            pass
        except (OSError, ValueError, OverflowError):
            LOGGER.warning("Не удалось восстановить паузы из файла")

    def _expire(self, now):
        if now < self._next_expiry:
            return False
        expired = [cid for cid, deadline in self.deadlines.items() if deadline <= now]
        for cid in expired:
            del self.deadlines[cid]
        self._next_expiry = min(self.deadlines.values(), default=float("inf"))
        return bool(expired)

    def _refresh(self):
        if self._expire(time()):
            self.save()

    def is_paused(self, chat_id):
        self._refresh()
        return chat_id in self.deadlines

    @property
    def paused_count(self):
        self._refresh()
        return len(self.deadlines)

    @property
    def paused(self):
        self._refresh()
        return set(self.deadlines)

    @staticmethod
    def chat_name(chat):
        name = " ".join(part for part in (chat.first_name, chat.last_name) if part)
        return " ".join((name or (f"@{chat.username}" if chat.username else "")).split())[:90]

    def record(self, chat, *, pause_from=None):
        return self._record(chat, pause_from)[0]

    def _record(self, chat, pause_from):
        now = time()
        changed = self._expire(now)
        deadline = pause_from + PAUSE_SECONDS if pause_from is not None else 0
        if deadline > max(now, self.deadlines.get(chat.id, 0)):
            self.deadlines[chat.id] = deadline
            self._next_expiry = min(self._next_expiry, deadline)
            changed = True
        name = self.chat_name(chat)
        if name and self.names.get(chat.id) != name:
            self.names.pop(chat.id, None)
            self.names[chat.id] = name
            excess = len(self.names) - 500
            if excess > 0:
                for chat_id in list(self.names):
                    if chat_id not in self.deadlines and chat_id != chat.id:
                        del self.names[chat_id]
                        excess -= 1
                        if not excess:
                            break
            changed = True
        if changed:
            self.save()
        return chat.id in self.deadlines, changed

    async def record_async(self, chat, *, pause_from=None):
        paused, changed = self._record(chat, pause_from)
        if changed:
            await self.flush()
        return paused

    def resume(self):
        expired = self._expire(time())
        count = len(self.deadlines)
        if count or expired:
            self.deadlines.clear()
            self._next_expiry = float("inf")
            self.save()
        return count

    async def resume_async(self):
        count = self.resume()
        await self.flush()
        return count

    def save(self):
        try:
            loop = get_running_loop()
        except RuntimeError:
            self._write(self.deadlines, self.names)
        else:
            if self._queued is None:
                self._queued = loop.create_task(self._save_after(self._writing))

    async def _save_after(self, previous):
        # Concurrent changes share one snapshot. Older snapshots must finish first.
        if previous is not None:
            await previous
        self._queued = None
        self._writing = current_task()
        try:
            await to_thread(self._write, self.deadlines.copy(), self.names.copy())
        finally:
            self._writing = None

    async def flush(self):
        pending = self._queued or self._writing
        if pending is not None:
            await shield(pending)

    def _write(self, deadlines, names):
        temporary = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with NamedTemporaryFile(dir=self.path.parent, mode="w", encoding="utf-8", delete=False) as file:
                temporary = Path(file.name)
                file.write(json.dumps({"paused": deadlines, "names": names}, ensure_ascii=False))
            os.replace(temporary, self.path)
            temporary = None
        except (OSError, UnicodeError):
            LOGGER.warning("Пауза применена в памяти, но не сохранена на диске")
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass


MAX_WORDS = 100
WORD = re.compile(r"[^\W\d_]+(?:[ '\-’][^\W\d_]+){0,3}")
CREATE_WORDS = """
    CREATE TABLE IF NOT EXISTS words (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        word TEXT NOT NULL,
        folded TEXT NOT NULL UNIQUE
    )
"""


class Vocabulary:
    def __init__(self, data_dir):
        self.path = Path(data_dir) / "vocabulary.sqlite3"

    @contextmanager
    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute(CREATE_WORDS)
            yield connection

    def entries(self, *, create=True):
        if not self.path.is_file():
            if not create:
                return []
            self.path.parent.mkdir(parents=True, exist_ok=True)
        query = "SELECT id, word FROM words ORDER BY id"
        with closing(sqlite3.connect(self.path)) as connection:
            try:
                return connection.execute(query).fetchall()
            except sqlite3.OperationalError as error:
                if str(error) != "no such table: words":
                    raise
            connection.execute(CREATE_WORDS)
            return connection.execute(query).fetchall()

    def entries_if_present(self):
        return self.entries(create=False)

    def add(self, text):
        if text and not text.isprintable():
            raise ValueError("Отправьте одно слово или короткое выражение в одну строку.")
        word = " ".join(normalize("NFC", text).split())
        if len(word) > 40 or not WORD.fullmatch(word):
            raise ValueError("Введите слово или выражение до 40 символов. Можно использовать пробел и дефис.")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            folded = word.casefold()
            duplicate, size = connection.execute(
                "SELECT EXISTS(SELECT 1 FROM words WHERE folded = ?), (SELECT count(*) FROM words)",
                (folded,),
            ).fetchone()
            if duplicate:
                raise ValueError("Это слово уже есть в словаре.")
            if size >= MAX_WORDS:
                raise ValueError(f"В словаре уже {MAX_WORDS} записей. Удалите одну, чтобы добавить новую.")
            connection.execute("INSERT INTO words(word, folded) VALUES (?, ?)", (word, folded))
        return word

    def remove(self, word_id):
        with self.connect() as connection:
            connection.execute("DELETE FROM words WHERE id = ?", (word_id,))
