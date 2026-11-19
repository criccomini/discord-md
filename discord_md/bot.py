import argparse
import asyncio
import fcntl
import inspect
import logging
import os
import sqlite3
from contextlib import closing
from functools import partial
from pathlib import Path

import aiohttp
import discord

from .archive import Archive, Message, Stream
from .config import Settings

LOG = logging.getLogger("discord-md")
SYNC_INTERVAL = 3600
SOURCE_CHANNELS = (discord.TextChannel, discord.ForumChannel)
NETWORK_ERRORS = (discord.HTTPException, aiohttp.ClientError, OSError)
DAY_IN_SNOWFLAKES = 86_400_000 << 22


class Bot(discord.Client):
    def __init__(self, settings: Settings, archive: Archive):
        intents = discord.Intents(guilds=True, guild_messages=True, message_content=True)
        super().__init__(intents=intents, max_messages=None)
        self.settings = settings
        self.archive = archive
        self.server_ids = {source.server_id for source in settings.sources.values()}
        self.jobs = asyncio.Queue()

    def stream(self, channel) -> Stream | None:
        if isinstance(channel, discord.Thread) and not channel.is_private():
            source_id = channel.parent_id
        elif isinstance(channel, SOURCE_CHANNELS):
            source_id = channel.id
        else:
            return None
        source = self.settings.sources.get(source_id)
        if source and source.server_id == channel.guild.id:
            return Stream(channel.id, source_id, channel.guild.id, channel.name)
        return None

    async def on_ready(self):
        LOG.info("Connected to Discord as %s", self.user)
        self.jobs.put_nowait(self.sync)

    async def on_resumed(self):
        self.jobs.put_nowait(self.sync)

    async def on_message(self, message):
        stream = self.stream(message.channel)
        if stream and not message.is_system():
            self.jobs.put_nowait(partial(
                self.archive.save, stream, message_record(message), self.backfill_floor(),
            ))

    async def on_raw_message_edit(self, event):
        if event.guild_id not in self.server_ids:
            return
        data = event.data
        edited = discord.utils.parse_time(data.get("edited_timestamp"))
        if edited and ("content" in data or "attachments" in data):
            attachments = ", ".join(file["filename"] for file in data["attachments"]) if "attachments" in data else None
            self.jobs.put_nowait(partial(
                self.archive.edit, event.message_id, data.get("content"), attachments, edited.timestamp(),
            ))

    async def on_raw_message_delete(self, event):
        if event.guild_id in self.server_ids:
            self.jobs.put_nowait(partial(self.archive.delete, {event.message_id}))

    async def on_raw_bulk_message_delete(self, event):
        if event.guild_id in self.server_ids:
            self.jobs.put_nowait(partial(self.archive.delete, event.message_ids))

    async def on_thread_create(self, channel):
        if self.stream(channel):
            self.jobs.put_nowait(partial(self.sync_channel, channel))

    async def on_thread_join(self, channel):
        await self.on_thread_create(channel)

    async def on_thread_update(self, before, after):
        stream = self.stream(after)
        if stream and before.name != after.name:
            self.jobs.put_nowait(partial(self.archive.register, stream, self.backfill_floor()))

    async def on_guild_channel_update(self, before, after):
        stream = self.stream(after)
        if stream and before.name != after.name:
            self.jobs.put_nowait(partial(self.archive.register, stream, self.backfill_floor()))

    async def on_raw_thread_delete(self, event):
        if event.guild_id in self.server_ids and event.parent_id in self.settings.sources:
            self.jobs.put_nowait(partial(self.archive.drop_stream, event.thread_id))

    async def on_guild_channel_delete(self, channel):
        if self.stream(channel):
            self.jobs.put_nowait(partial(self.archive.drop_stream, channel.id, children=True))

    def backfill_floor(self) -> int:
        return snowflake_days_ago(self.settings.backfill_days)

    async def sync_channel(self, channel, parent=None) -> None:
        stream = self.stream(channel)
        if stream is None:
            return
        if isinstance(channel, discord.Thread):
            parent = channel.parent or parent or await channel.guild.fetch_channel(channel.parent_id)
        if not readable(parent or channel):
            return

        backfill_floor = self.backfill_floor()
        saved = self.archive.stream_window(stream.id)
        if saved is None or backfill_floor < saved[0]:
            floor = after = backfill_floor
            cursor = saved[1] if saved else backfill_floor
        else:
            floor, cursor = saved
            after = max(floor, min(cursor, snowflake_days_ago(self.settings.reconcile_days)))

        messages = []
        seen = set()
        async for message in channel.history(limit=None, after=discord.Object(id=after), oldest_first=True):
            cursor = max(cursor, message.id)
            seen.add(message.id)
            if not message.is_system():
                messages.append(message_record(message))

        # Discord returns empty history if the bot loses Read Message History.
        self.archive.sync_stream(
            stream, floor, after, cursor, messages, seen, complete=readable(parent or channel),
        )

    async def sync(self) -> None:
        active_by_server = {}
        backfill_floor = self.backfill_floor()
        reconcile_cutoff = snowflake_days_ago(self.settings.reconcile_days)
        cutoff = min(backfill_floor, reconcile_cutoff)
        for id_, source in self.settings.sources.items():
            guild = self.get_guild(source.server_id)
            if guild is None or guild.unavailable:
                LOG.warning("Server %s is unavailable", source.server_id)
                continue

            try:
                channel = guild.get_channel(id_) or await guild.fetch_channel(id_)
                if not isinstance(channel, SOURCE_CHANNELS) or not readable(channel):
                    LOG.warning("Cannot read text or forum channel %s in server %s", id_, guild.id)
                    continue
                if isinstance(channel, discord.TextChannel):
                    await self.sync_channel(channel)
                else:
                    self.archive.register(self.stream(channel), backfill_floor)
            except discord.NotFound:
                self.archive.drop_stream(id_, children=True)
                continue
            except NETWORK_ERRORS as error:
                LOG.warning("Cannot sync channel %s: %s", id_, error)
                continue

            if guild.id not in active_by_server:
                try:
                    active_by_server[guild.id] = await guild.active_threads()
                except NETWORK_ERRORS as error:
                    LOG.warning("Cannot list active threads in server %s: %s", guild.id, error)
                    active_by_server[guild.id] = guild.threads
            threads = {thread.id: thread for thread in active_by_server[guild.id] if thread.parent_id == id_}
            try:
                async for thread in channel.archived_threads(limit=None):
                    if discord.utils.time_snowflake(thread.archive_timestamp) < cutoff:
                        break
                    threads[thread.id] = thread
            except NETWORK_ERRORS as error:
                LOG.warning("Cannot list archived threads in channel %s: %s", id_, error)

            tracked = self.archive.tracked_threads(id_, reconcile_cutoff, backfill_floor)
            for thread_id in sorted(threads.keys() | tracked):
                try:
                    thread = threads.get(thread_id) or await guild.fetch_channel(thread_id)
                    if isinstance(thread, discord.Thread) and self.stream(thread):
                        await self.sync_channel(thread, channel)
                except discord.NotFound:
                    self.archive.drop_stream(thread_id)
                except NETWORK_ERRORS as error:
                    LOG.warning("Cannot sync thread %s: %s", thread_id, error)

    async def work(self) -> None:
        self.archive.publish(rebuild=True)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + SYNC_INTERVAL
        while True:
            if loop.time() >= deadline:
                if self.is_ready():
                    await self.sync()
                    self.archive.publish()
                deadline = loop.time() + SYNC_INTERVAL
            try:
                job = await asyncio.wait_for(self.jobs.get(), max(0.0, deadline - loop.time()))
            except TimeoutError:
                continue

            jobs = [job]
            jobs.extend(self.jobs.get_nowait() for _ in range(min(99, self.jobs.qsize())))
            synced = False
            for job in jobs:
                if job == self.sync:
                    if synced:
                        continue
                    synced = True
                if inspect.iscoroutinefunction(job):
                    try:
                        await job()
                    except NETWORK_ERRORS as error:
                        LOG.warning("Cannot read from Discord: %s", error)
                    if job == self.sync:
                        deadline = loop.time() + SYNC_INTERVAL
                else:
                    job()
            self.archive.publish()

    async def start(self, token: str, *, reconnect: bool = True) -> None:
        tasks = [
            asyncio.create_task(super().start(token, reconnect=reconnect)),
            asyncio.create_task(self.work()),
        ]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


def readable(channel) -> bool:
    member = channel.guild.me
    if member is None:
        return False
    permissions = channel.permissions_for(member)
    return permissions.view_channel and permissions.read_message_history


def snowflake_days_ago(days: int) -> int:
    return max(0, discord.utils.time_snowflake(discord.utils.utcnow()) - days * DAY_IN_SNOWFLAKES)


def message_record(message: discord.Message) -> Message:
    edit = message.edited_at
    return Message(
        message.id,
        message.created_at.isoformat(timespec="seconds").replace("+00:00", "Z"),
        message.author.display_name,
        message.content,
        message.reference.jump_url if message.reference and message.reference.message_id else "",
        ", ".join(file.filename for file in message.attachments),
        edit.timestamp() if edit else 0,
    )


def user_dir(variable: str, default: str) -> Path:
    return Path(os.environ.get(variable) or Path.home() / default) / "discord-md"


def main() -> None:
    parser = argparse.ArgumentParser(description="Export Discord channels to Perch as Markdown")
    parser.add_argument("--config", type=Path, default=user_dir("XDG_CONFIG_HOME", ".config") / "config.toml")
    args = parser.parse_args()
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        parser.error("Set DISCORD_TOKEN to the bot token")

    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        settings = Settings.load(args.config.expanduser())
        state_dir = user_dir("XDG_STATE_HOME", ".local/state")
        state_dir.mkdir(parents=True, exist_ok=True)
        with (state_dir / "archive.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                parser.exit(1, "discord-md already uses this state directory\n")
            with closing(sqlite3.connect(state_dir / "archive.sqlite3")) as db:
                Bot(settings, Archive(db, settings)).run(token, log_handler=None)
    except (OSError, ValueError, sqlite3.Error, discord.DiscordException, aiohttp.ClientError) as error:
        parser.exit(1, f"discord-md: {error}\n")
