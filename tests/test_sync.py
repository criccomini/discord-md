import asyncio
import sqlite3
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import aiohttp
import discord

from discord_md.archive import Archive
from discord_md.bot import Bot, message_record
from discord_md.config import Settings, Source


class SyncTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / "discord"
        self.settings = Settings(self.root, 30, 7, {2: Source(1, Path("project/dev"))})
        self.db = sqlite3.connect(Path(temp.name) / "archive.sqlite3")
        self.addCleanup(self.db.close)
        self.archive = Archive(self.db, self.settings)
        self.bot = Bot(self.settings, self.archive)
        self.channel = Mock(spec=discord.TextChannel, id=2, guild=SimpleNamespace(id=1, me=object()))
        self.channel.name = "dev"
        self.channel.permissions_for.return_value = discord.Permissions(view_channel=True, read_message_history=True)
        self.now = discord.utils.utcnow()

    def message(self, age, content, *, channel=None):
        created = self.now - age
        return SimpleNamespace(
            id=discord.utils.time_snowflake(created), channel=channel or self.channel,
            created_at=created, edited_at=None, author=SimpleNamespace(display_name="Alice"),
            content=content, is_system=lambda: False, reference=None, attachments=[],
        )

    async def test_reconnect_reconciles_recent_history_and_recovers_new_messages(self):
        old = self.message(timedelta(days=8), "Outside recheck")
        edited = self.message(timedelta(days=2), "Before edit")
        deleted = self.message(timedelta(days=1), "Delete me")
        latest = self.message(timedelta(seconds=1), "Live message after gap")
        missed = self.message(timedelta(hours=1), "Missed while away")
        remote = [old, edited, deleted]

        async def history(*, after, **options):
            for message in remote:
                if message.id > after.id:
                    yield message

        self.channel.history = history
        await self.bot.sync_channel(self.channel)
        self.archive.publish()
        self.archive.save(self.bot.stream(self.channel), message_record(latest), self.bot.backfill_floor())

        # Checkpoint only REST history. Receiving a newer live message cannot skip the gap.
        self.assertEqual(self.db.execute("SELECT cursor FROM streams").fetchone(), (deleted.id,))
        edited.edited_at = self.now
        edited.content = "After edit"
        remote[:] = [edited, missed, latest]
        await self.bot.sync_channel(self.channel)
        self.archive.publish()
        transcripts = "\n".join(path.read_text() for path in self.root.rglob("*.md"))
        for text in ("Outside recheck", "After edit", "Missed while away", "Live message after gap"):
            self.assertIn(text, transcripts)
        self.assertNotIn("Before edit", transcripts)
        self.assertNotIn("Delete me", transcripts)

        # Increasing backfill_days imports older history for an already-tracked channel.
        older = self.message(timedelta(days=45), "Older history")
        remote.insert(0, older)
        self.bot.settings = Settings(self.root, 60, 7, self.settings.sources)
        await self.bot.sync_channel(self.channel)
        self.archive.publish()
        self.assertIn("Older history", "\n".join(path.read_text() for path in self.root.rglob("*.md")))

    async def test_live_burst_edits_deletes_and_ignores_unlisted_channels(self):
        posts = [self.message(timedelta(seconds=5, milliseconds=205 - i), f"post {i}") for i in range(205)]
        for message in posts:
            await self.bot.on_message(message)
        await self.bot.on_message(self.message(
            timedelta(days=2), "Unlisted", channel=Mock(spec=discord.TextChannel, id=9),
        ))
        await self.bot.on_raw_bulk_message_delete(SimpleNamespace(guild_id=1, message_ids={posts[0].id, posts[1].id}))
        await self.bot.on_raw_message_edit(SimpleNamespace(
            guild_id=1, message_id=posts[2].id,
            data={"edited_timestamp": self.now.isoformat(), "content": "```rust\nfn main() {}\n```"},
        ))
        drained = asyncio.Event()
        self.bot.jobs.put_nowait(drained.set)
        task = asyncio.create_task(self.bot.work())
        try:
            await asyncio.wait_for(drained.wait(), timeout=5)
            self.assertEqual(self.db.execute("SELECT count(*) FROM messages").fetchone(), (203,))
            page = self.root / "project/dev" / f"{posts[2].created_at.date()}.md"
            self.assertIn("````text\n```rust\nfn main() {}\n```\n````", page.read_text())
            self.assertIn(f"https://discord.com/channels/1/2/{posts[2].id}", page.read_text())
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_rebuild_removes_deleted_thread_page(self):
        thread = Mock(spec=discord.Thread, id=3, guild=self.channel.guild, parent_id=2, parent=None)
        thread.name = "Design notes"
        thread.is_private.return_value = False
        message = self.message(timedelta(seconds=1), "Thread post", channel=thread)

        async def history(**options):
            yield message

        thread.history = history
        await self.bot.sync_channel(thread, self.channel)
        self.archive.publish()
        page = self.root / "project/dev/threads/3.md"
        index = self.root / "project/INDEX.md"
        self.assertIn("[Design notes](dev/threads/3.md)", index.read_text())
        self.assertTrue(page.exists())

        self.archive.delete({message.id})
        # Recover if the process stops after the database commit but before the export.
        Archive(self.db, self.settings).publish(rebuild=True)
        self.assertFalse(page.exists())
        self.assertFalse(page.parent.exists())
        self.assertNotIn("threads/3.md", index.read_text())

    async def test_failed_sync_and_old_archived_threads(self):
        # A mid-history network drop leaves the database and dirty state clean.
        async def broken_history(**options):
            yield self.message(timedelta(seconds=2), "partial")
            raise aiohttp.ServerDisconnectedError("dropped")

        self.channel.history = broken_history
        try:
            await self.bot.sync_channel(self.channel)
        except aiohttp.ClientError:
            pass
        self.archive.publish()
        self.assertEqual(self.db.execute("SELECT count(*) FROM streams").fetchone(), (0,))

        # Tracked threads older than reconcile_days and within floor do not trigger fetches.
        old_thread = Mock(spec=discord.Thread, id=10, guild=self.channel.guild, parent_id=2, parent=None)
        old_thread.name = "Old thread"
        old_thread.is_private.return_value = False
        old_msg = self.message(timedelta(days=20), "Archived post", channel=old_thread)

        async def old_history(**options):
            yield old_msg

        old_thread.history = old_history
        await self.bot.sync_channel(old_thread, self.channel)
        reconcile_cutoff = discord.utils.time_snowflake(discord.utils.utcnow() - timedelta(days=7))
        self.assertEqual(self.archive.tracked_threads(2, reconcile_cutoff, self.bot.backfill_floor()), set())
        wider_floor = discord.utils.time_snowflake(discord.utils.utcnow() - timedelta(days=60))
        self.assertEqual(self.archive.tracked_threads(2, reconcile_cutoff, wider_floor), {10})
