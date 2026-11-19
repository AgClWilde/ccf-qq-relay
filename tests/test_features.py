"""Account registration, room watches and management regression coverage."""
import asyncio
import copy
import datetime as dt
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from commands import command
from relay import Relay, Store, process_lock


URL = "https://ccfolia.com/rooms/fixture-room"
AUTH = {"token": "fixture-token", "refresh_token": "fixture-refresh", "user_id": "fixture-user"}
CHANNELS = [{"id": "main", "name": "MAIN"}, {"id": "side", "name": "Side Chat"}]


class Source:
    calls = []
    messages = []
    channels = CHANNELS

    def __init__(self, session, token="", refresh_token=""):
        self.token, self.refresh_token, self.user_id = token, refresh_token, "fixture-user"

    async def login(self, username, password):
        type(self).calls.append(("login", username))
        self.token, self.refresh_token = AUTH["token"], AUTH["refresh_token"]
        return dict(AUTH)

    async def room(self, room):
        type(self).calls.append(("room", room))
        return {"id": room, "name": "Room " + room, "channels": copy.deepcopy(type(self).channels)}

    async def latest(self, room, channel_id=None, limit=256, before=None):
        type(self).calls.append(("latest", room, channel_id))
        return [m for m in type(self).messages if channel_id is None or m["channelId"] == channel_id][:limit]


def message(identifier, channel="main", **extra):
    return {"id": identifier, "channelId": channel, "name": "Fixture Speaker",
            "created": dt.datetime.fromtimestamp(time.time() + 1, dt.timezone.utc).isoformat(), **extra}


class FeatureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "relay.sqlite3"
        self.relay = Relay({"database": str(self.path), "onebot_url": "ws://127.0.0.1:1"})
        self.relay.send = AsyncMock()
        self.relay.reconcile = AsyncMock()
        self.relay.restart = AsyncMock()
        self.patcher = patch("relay.CCFolia", Source)
        self.patcher.start()
        Source.calls, Source.messages, Source.channels = [], [], CHANNELS

    async def asyncTearDown(self):
        self.patcher.stop()
        await self.relay.onebot.close()
        self.relay.store.db.close()
        self.temporary.cleanup()

    async def run_command(self, text, owner="101", kind="private", target=None):
        await self.relay.handle(command(text), owner, kind, target or owner)
        return self.relay.send.await_args.args[2]

    async def register(self):
        return await self.run_command("。ccf提醒 登记 member@example.invalid fixture pass  word")

    async def test_registration_reuses_identity_for_new_rooms_after_restart(self):
        await self.register()
        self.relay.store.db.close()
        self.relay.store = Store(self.path)
        await self.run_command(f".ccf提醒 {URL}", kind="group", target="909")
        await self.run_command(".ccf提醒 全频道 https://ccfolia.com/rooms/another-room")
        self.assertEqual([c for c in Source.calls if c[0] == "login"], [("login", "member@example.invalid")])
        self.assertEqual(len(self.relay.store.active()), 3)
        self.assertEqual(self.relay.store.registered("101")["username"], "member@example.invalid")
        self.assertNotIn("fixture pass  word", "\n".join(self.relay.store.db.iterdump()))
        self.assertNotIn("fixture-token", "\n".join(call.args[2] for call in self.relay.send.await_args_list))

    async def test_private_registration_prompt_and_next_message(self):
        await self.run_command("。ccf提醒 登记")
        event = {"post_type": "message", "message_type": "private", "user_id": 101,
                 "self_id": 999, "message_id": 42,
                 "raw_message": "member@example.invalid\nfixture pass  word"}
        await self.relay.on_event(event)
        self.assertIsNotNone(self.relay.store.registered("101"))
        self.assertNotIn("101", self.relay.registration_pending)
        with self.assertRaisesRegex(ValueError, "仅限私聊"):
            await self.run_command(".ccf提醒 登记 member@example.invalid never-save", kind="group", target="909")
        self.assertNotIn("never-save", "\n".join(self.relay.store.db.iterdump()))

    async def test_all_channels_discovery_name_close_and_reenable(self):
        await self.register()
        await self.run_command(f".ccf提醒 {URL} 全频道", kind="group", target="909")
        old = {b["channel"]: b for b in self.relay.store.active()}
        # A different group member needs no personal login to manage this room.
        await self.run_command(".ccf提醒 关闭 Side Chat", owner="202", kind="group", target="909")
        account = self.relay.store.accounts()[0]
        metadata = await Source(None).room("fixture-room")
        metadata["channels"].append({"id": "new", "name": "New Channel"})
        self.relay.store.sync_watches(account, metadata)
        self.assertEqual({b["channel"] for b in self.relay.store.active()}, {"main", "new"})
        self.assertEqual(self.relay.store.get(old["main"]["id"])["started"], old["main"]["started"])
        self.relay.store.ingest(account["id"], [message("new-message", "new"), message("excluded", "side")])
        pending = self.relay.store.rows("SELECT * FROM outbox")
        self.assertEqual([p["message"] for p in pending], ["new-message"])
        self.assertIn("New Channel", pending[0]["text"])
        reply = await self.run_command(".ccf提醒 状态", owner="202", kind="group", target="909")
        for fragment in ("全频道", "Room fixture-room-MAIN", "Room fixture-room-New Channel", URL):
            self.assertIn(fragment, reply)
        await self.run_command(f".ccf提醒 side {URL}", owner="202", kind="group", target="909")
        self.assertEqual({b["channel"] for b in self.relay.store.active()}, {"main", "side", "new"})
        self.assertEqual(self.relay.store.rows("SELECT * FROM excluded_channels"), [])

    async def test_watcher_survives_all_current_channels_excluded_and_restart(self):
        await self.register()
        await self.run_command(f".ccf提醒 全频道 {URL}")
        await self.run_command(".ccf提醒 关闭 MAIN")
        await self.run_command(".ccf提醒 关闭 Side Chat")
        self.assertEqual(self.relay.store.active(), [])
        self.relay.store.db.close()
        self.relay.store = Store(self.path)
        self.assertEqual(len(self.relay.store.accounts()), 1)
        account = self.relay.store.accounts()[0]
        meta = {"name": "Room fixture-room", "channels": CHANNELS + [{"id": "future", "name": "Future"}]}
        self.relay.store.sync_watches(account, meta)
        self.assertEqual([b["channel"] for b in self.relay.store.active()], ["future"])
        await self.run_command(f".ccf提醒 关闭 {URL}")
        self.assertEqual(self.relay.store.accounts(), [])
        self.assertEqual(self.relay.store.watches(), [])
        self.assertEqual(self.relay.store.rows("SELECT * FROM excluded_channels"), [])
        self.assertIsNotNone(self.relay.store.registered("101"))

    async def test_name_only_close_ambiguity_leaves_all_subscriptions_intact(self):
        await self.register()
        await self.run_command(f".ccf提醒 {URL}")
        await self.run_command(".ccf提醒 https://ccfolia.com/rooms/another-room")
        before = self.relay.store.active()
        with self.assertRaisesRegex(ValueError, "匹配多个"):
            await self.run_command(".ccf提醒 关闭 MAIN")
        self.assertEqual(self.relay.store.active(), before)
        await self.run_command(f".ccf提醒 关闭 MAIN {URL}")
        self.assertEqual(len(self.relay.store.active()), 1)

    async def test_latest_existing_public_message_uses_standard_render_without_cursor_mutation(self):
        await self.register()
        await self.run_command(f".ccf提醒 {URL}")
        before = self.relay.store.active()
        Source.messages = [message("hidden", to="private-user"), message("public", "side")]
        reply = await self.run_command(f".ccf提醒 测试 {URL}")
        self.assertRegex(reply, r"^「Room fixture-room-Side Chat」\[\d\d:\d\d:\d\d\] Fixture Speaker回复了。$")
        self.assertEqual(self.relay.store.active(), before)
        self.assertEqual(self.relay.store.rows("SELECT * FROM seen"), [])
        self.assertEqual(self.relay.store.rows("SELECT * FROM outbox"), [])

    async def test_repeated_event_without_message_id_is_processed_once(self):
        event = {"post_type": "message", "message_type": "private", "user_id": 101,
                 "self_id": 999, "time": 123456, "raw_message": ".ccf提醒"}
        await self.relay.on_event(event)
        await self.relay.on_event(event)
        self.relay.send.assert_awaited_once()

    async def test_legacy_command_receipt_suppresses_replayed_confirmation(self):
        with self.relay.store.db:
            self.relay.store.db.execute("INSERT INTO commands VALUES (?,?)", ("999:42:101", time.time()))
        event = {"post_type": "message", "message_type": "private", "user_id": 101,
                 "self_id": 999, "message_id": 42, "raw_message": ".ccf提醒"}
        await self.relay.on_event(event)
        self.relay.send.assert_not_awaited()

    async def test_additive_schema_migration_preserves_subscriptions_and_delivery_state(self):
        await self.register()
        await self.run_command(f".ccf提醒 {URL}")
        account = self.relay.store.accounts()[0]
        self.relay.store.ingest(account["id"], [message("one"), message("two"), message("three")])
        before = {table: self.relay.store.rows("SELECT * FROM " + table)
                  for table in ("accounts", "bindings", "seen", "outbox", "commands")}
        self.relay.store.db.executescript("DROP TABLE excluded_channels; DROP TABLE room_watches; DROP TABLE registered_accounts;")
        self.relay.store.db.close()
        self.relay.store = Store(self.path)
        after = {table: self.relay.store.rows("SELECT * FROM " + table) for table in before}
        self.assertEqual(after, before)
        self.assertEqual(self.relay.store.watches(), [])


class LockTests(unittest.TestCase):
    def test_same_database_cannot_start_twice_and_lock_releases(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "state.sqlite3"
            with process_lock(database):
                with self.assertRaises(SystemExit):
                    with process_lock(database):
                        self.fail("duplicate process acquired lock")
                with process_lock(Path(temporary) / "other.sqlite3"):
                    pass
            with process_lock(database):
                pass


if __name__ == "__main__":
    unittest.main()
