"""Source polling and durable delivery tests, using synthetic local fixtures."""

import asyncio
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from ccfolia import CCFoliaError
from relay import Relay, Store


BASE = 1_800_000_000
AUTH = {"token": "fixture-token", "refresh_token": "fixture-refresh", "user_id": "fixture-user"}
META = {"name": "Fixture Room", "channels": [
    {"id": "main", "name": "MAIN"}, {"id": "info", "name": "INFO"}]}


def message(identifier, at=BASE, channel="main"):
    created = dt.datetime.fromtimestamp(at, dt.timezone.utc).isoformat()
    return {"id": identifier, "created": created, "pos": created + "|" + identifier,
            "channelId": channel, "name": "Fixture Sender", "text": "BODY_MUST_NOT_PERSIST"}


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "state.sqlite3"
        self.relay = Relay({"database": str(self.path), "onebot_url": "ws://127.0.0.1:1/",
                            "allowed_users": ["101"], "poll_seconds": 1,
                            "rescan_seconds": 1_000_000, "send_interval": 0.1})
        self.main, self.account, _ = self.subscribe("main", "MAIN")
        self.account_row = self.relay.store.accounts()[0]
        self.api = Mock(token="updated-token", refresh_token="updated-refresh", user_id="fixture-user")
        self.api.room = AsyncMock(return_value=META)
        self.api.latest = AsyncMock(return_value=[])
        self.relay.api = Mock(return_value=self.api)
        self.relay.send = AsyncMock()

    async def asyncTearDown(self):
        await self.relay.onebot.close()
        self.relay.store.db.close()
        self.temporary.cleanup()

    def subscribe(self, channel, name, kind="group", target="909"):
        return self.relay.store.subscribe("101", kind, target, "fixture-room",
                                          {"id": channel, "name": name}, META, AUTH, BASE)

    async def watch_for_polls(self, count):
        # The terminal cancellation is raised at a poll boundary, after all
        # work for that iteration has completed. No wall-clock wait is needed.
        sleeps = [None] * (count - 1) + [asyncio.CancelledError()]
        with patch("relay.asyncio.sleep", AsyncMock(side_effect=sleeps)), \
                patch("relay.time.time", return_value=BASE + 10):
            with self.assertRaises(asyncio.CancelledError):
                await self.relay.watch(self.account_row)

    async def test_metadata_refreshes_every_poll_and_publicity_filters_resume(self):
        self.subscribe("info", "INFO")
        self.api.room.side_effect = [META, {"name": "Fixture Room", "channels": [META["channels"][0]]}]
        self.relay.catchup = AsyncMock(return_value=BASE + 1)
        self.relay.store.resume_due = Mock(wraps=self.relay.store.resume_due)
        await self.watch_for_polls(2)
        self.assertEqual(self.api.room.await_count, 2)
        self.assertEqual(self.relay.catchup.await_count, 2)
        calls = self.relay.store.resume_due.call_args_list
        self.assertEqual(calls[0].args[2], {"main", "info"})
        self.assertEqual(calls[1].args[2], {"main"})
        self.assertEqual(self.relay.store.accounts()[0]["token"], "updated-token")
        self.assertIsNone(self.relay.store.accounts()[0]["last_error"])

    async def test_room_watch_discovers_new_channel_even_with_all_existing_channels_excluded(self):
        self.relay.store.watch_room(self.account, "group", "909", "fixture-room", BASE)
        self.relay.store.sync_watches(self.account_row, META)
        self.relay.store.close(self.relay.store.active(), exclude=True)
        self.assertFalse(self.relay.store.active())
        self.api.room.return_value = {**META, "channels": META["channels"] + [{"id": "future", "name": "Future"}]}
        self.relay.catchup = AsyncMock(return_value=BASE + 1)
        await self.watch_for_polls(1)
        binding = self.relay.store.active()[0]
        self.assertEqual(binding["channel"], "future")
        self.assertEqual(binding["started"], BASE)
        self.assertEqual(self.relay.catchup.await_args.args[2], BASE)
        self.assertEqual(len(self.relay.store.watches()), 1)

    async def test_watch_paginated_scan_commits_all_ids_without_body_and_saves_auth(self):
        page = [message(f"m{i:04d}", BASE + 1) for i in range(256)]
        tail = [message("last", BASE)]
        self.api.latest.side_effect = [page, tail]
        await self.watch_for_polls(1)
        self.assertEqual(self.api.latest.await_count, 2)
        self.assertEqual(self.api.latest.await_args_list[1].kwargs["before"], page[-1]["pos"])
        self.assertEqual(len(self.relay.store.rows("SELECT * FROM seen")), 257)
        self.assertEqual(len(self.relay.store.rows("SELECT * FROM outbox")), 3)
        state = json.loads(self.relay.store.get(self.main)["notification_state"])
        self.assertEqual(state["phase"], "muted")
        self.assertEqual(self.relay.source_states[self.account], "轮询监听中")
        dump = "\n".join(self.relay.store.db.iterdump())
        self.assertNotIn("BODY_MUST_NOT_PERSIST", dump)
        self.assertEqual(self.relay.store.accounts()[0]["refresh_token"], "updated-refresh")

    async def test_reauth_failure_warns_owner_privately_once_across_retries(self):
        self.api.latest.side_effect = CCFoliaError("REAUTH_REQUIRED")
        await self.watch_for_polls(2)
        self.assertEqual(self.api.latest.await_count, 2)
        self.relay.send.assert_awaited_once()
        args = self.relay.send.await_args.args
        self.assertEqual(args[:2], ("private", "101"))
        self.assertIn("重新绑定", args[2])
        self.assertNotIn("fixture-token", args[2])
        self.assertIn("重新绑定", self.relay.store.accounts()[0]["last_error"])

    async def test_failed_scan_does_not_resume_or_commit_a_partial_page(self):
        self.relay.store.ingest(self.account, [message("old-1", BASE), message("old-2", BASE + 1),
                                               message("old-3", BASE + 2)])
        first_page = [message(f"new-{i:04d}", BASE + 400) for i in range(256)]
        self.api.latest.side_effect = [first_page, CCFoliaError("NETWORK_ERROR")]
        self.relay.store.resume_due = Mock(wraps=self.relay.store.resume_due)
        await self.watch_for_polls(1)
        self.relay.store.resume_due.assert_not_called()
        self.assertEqual(len(self.relay.store.rows("SELECT * FROM seen")), 3)
        self.assertEqual(len(self.relay.store.rows("SELECT * FROM outbox")), 3)
        state = json.loads(self.relay.store.get(self.main)["notification_state"])
        self.assertEqual(state["phase"], "muted")
        self.relay.send.assert_not_awaited()

    async def test_failed_delivery_does_not_block_another_target_and_restart_deduplicates(self):
        private, _, _ = self.subscribe("main", "MAIN", kind="private", target="101")
        event = message("one-message")
        self.relay.store.ingest(self.account, [event])
        self.relay.onebot.ready.set()
        attempts = []

        async def fail_group(kind, target, text):
            attempts.append((kind, target, text))
            if kind == "group":
                raise RuntimeError("synthetic unavailable target")

        self.relay.send.side_effect = fail_group
        with patch("relay.asyncio.sleep", AsyncMock(side_effect=asyncio.CancelledError())):
            with self.assertRaises(asyncio.CancelledError):
                await self.relay.deliver()
        self.assertEqual([(kind, target) for kind, target, _ in attempts],
                         [("group", "909"), ("private", "101")])
        rows = self.relay.store.rows("SELECT * FROM outbox ORDER BY id")
        self.assertEqual([row["sent"] for row in rows], [0, 1])
        self.assertTrue(rows[0]["text"])
        self.assertEqual(rows[1]["text"], "")

        # Retry after reopening the persistent Store. The successful private
        # target must never be sent again; the failed group is still pending.
        self.relay.store.db.close()
        self.relay.store = Store(self.path)
        self.relay.send = AsyncMock()
        with patch("relay.asyncio.sleep", AsyncMock(side_effect=asyncio.CancelledError())):
            with self.assertRaises(asyncio.CancelledError):
                await self.relay.deliver()
        self.relay.send.assert_awaited_once()
        self.assertEqual(self.relay.send.await_args.args[:2], ("group", "909"))
        rows = self.relay.store.rows("SELECT * FROM outbox")
        self.assertTrue(all(row["sent"] == 1 and row["text"] == "" for row in rows))

        self.assertEqual(self.relay.store.ingest(self.account, [event]), 0)
        self.relay.send.reset_mock()
        with patch("relay.asyncio.sleep", AsyncMock(side_effect=asyncio.CancelledError())):
            with self.assertRaises(asyncio.CancelledError):
                await self.relay.deliver()
        self.relay.send.assert_not_awaited()
        self.assertEqual(len(self.relay.store.rows("SELECT * FROM outbox")), 2)
        self.assertIsNotNone(self.relay.store.get(private))


if __name__ == "__main__":
    unittest.main()
