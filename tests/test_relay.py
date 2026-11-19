import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock

from relay import Relay, Store, channel_for


def message(ident, second, channel="main", **extra):
    return {"id": ident, "created": f"2026-01-01T00:{second // 60:02d}:{second % 60:02d}Z",
            "channelId": channel, "name": "角色", "text": "BODY_MUST_NOT_PERSIST", **extra}


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state" / "relay.sqlite3"
        self.store = Store(self.path)
        self.auth = {"token": "FAKE-TOKEN", "refresh_token": "FAKE-REFRESH", "user_id": "fake-user"}
        self.meta = {"name": "测试房间"}
        self.main, self.account, _ = self.subscribe("main", "MAIN")

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def subscribe(self, channel, name, target="10001", kind="private", owner="10001", started=0):
        return self.store.subscribe(owner, kind, target, "test-room", {"id": channel, "name": name},
                                    self.meta, self.auth, started)

    def pending(self, binding=None):
        return self.store.rows("SELECT * FROM outbox WHERE sent=0" + (" AND binding=?" if binding else ""),
                               (binding,) if binding else ())

    def test_channels_add_and_repeated_subscription_keeps_state(self):
        self.store.ingest(self.account, [message("a", 0), message("b", 1), message("c", 2)])
        old = self.store.get(self.main)
        ident, account, duplicate = self.subscribe("main", "MAIN", started=999999)
        self.assertEqual((ident, account, duplicate), (self.main, self.account, True))
        self.assertEqual(self.store.get(self.main)["started"], 0)
        self.assertEqual(self.store.get(self.main)["notification_state"], old["notification_state"])
        self.subscribe("info", "INFO")
        self.subscribe("other", "OTHER")
        self.assertEqual(len(self.store.active()), 3)
        self.assertEqual(len(self.store.accounts()), 1)
        self.assertEqual(len(self.pending(self.main)), 3)

    def test_channel_and_target_independent_dedupe(self):
        info, _, _ = self.subscribe("info", "INFO")
        group, _, _ = self.subscribe("main", "MAIN", target="20002", kind="group")
        events = [message("main", 0), message("info", 1, "info"), message("other", 2, "other")]
        self.store.ingest(self.account, events)
        self.store.ingest(self.account, events)
        self.assertEqual([len(self.pending(b)) for b in (self.main, info, group)], [1, 1, 1])

    def test_private_secret_removed_inaccessible_not_notified(self):
        events = [message("dm", 0, to="someone"), message("roll", 1, extend={"roll": {"secret": True}}),
                  message("removed", 2, removed=True), message("private", 3, _hidden=True),
                  message("visible", 4)]
        self.store.ingest(self.account, events)
        self.assertEqual(len(self.pending()), 1)
        self.assertEqual(self.pending()[0]["message"], "visible")
        dump = "\n".join(self.store.db.iterdump())
        self.assertNotIn("BODY_MUST_NOT_PERSIST", dump)
        self.assertNotIn("someone", dump)

    def test_first_subscription_boundary(self):
        from notifications import epoch
        self.store.db.execute("UPDATE bindings SET started=?", (epoch(message("boundary", 5)["created"]),))
        self.store.ingest(self.account, [message("old", 4), message("boundary", 5)])
        self.assertEqual([r["message"] for r in self.pending()], ["boundary"])

    def test_restart_pause_and_resume_once(self):
        from notifications import epoch
        self.store.ingest(self.account, [message("a", 0), message("b", 1), message("c", 2)])
        self.store.db.close()
        self.store = Store(self.path)
        self.assertEqual(json.loads(self.store.get(self.main)["notification_state"])["phase"], "muted")
        now = epoch(message("c", 2)["created"]) + 300
        self.store.resume_due(self.account, now, {"info"})
        self.assertEqual(len(self.pending()), 3)
        self.store.resume_due(self.account, now, {"main"})
        self.store.resume_due(self.account, now + 1, {"main"})
        self.assertEqual(len(self.pending()), 4)

    def test_off_cascades_only_selected_channel_and_removes_unused_tokens(self):
        info, _, _ = self.subscribe("info", "INFO")
        self.store.ingest(self.account, [message("a", 0), message("b", 1, "info")])
        self.store.close([self.store.get(self.main)])
        self.assertEqual(len(self.store.active()), 1)
        self.assertEqual(len(self.pending()), 1)
        self.assertEqual(len(self.store.accounts()), 1)
        self.store.close([self.store.get(info)])
        self.assertFalse(self.store.accounts())
        self.assertFalse(self.pending())
        self.assertFalse(self.store.rows("SELECT * FROM seen"))

    def test_other_owner_cannot_replace_subscription(self):
        with self.assertRaises(ValueError):
            self.subscribe("main", "MAIN", owner="10002")
        self.assertEqual(len(self.store.accounts()), 1)
        self.assertEqual(self.store.get(self.main)["owner"], "10001")

    def test_database_permissions(self):
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)


class CatchupTests(unittest.IsolatedAsyncioTestCase):
    async def test_pagination_traverses_equal_timestamps_and_deduplicates(self):
        relay = Relay.__new__(Relay)
        relay.store = unittest.mock.Mock()
        page1 = [{**message(f"m{i:04d}", 10), "pos": f"cursor-{i}"} for i in range(256)]
        page2 = [{**message("last", 10), "pos": "last-cursor"}]
        api = unittest.mock.Mock()
        api.latest = AsyncMock(side_effect=[page1, page2])
        result = await relay.catchup(api, {"id": "account", "room": "test-room"}, 0)
        self.assertEqual(api.latest.await_count, 2)
        self.assertEqual(api.latest.await_args_list[1].kwargs["before"], "cursor-255")
        self.assertEqual(len(relay.store.ingest.call_args.args[1]), 257)
        self.assertGreater(result, 0)

    async def test_stalled_page_fails_without_partial_progress(self):
        relay = Relay.__new__(Relay)
        relay.store = unittest.mock.Mock()
        page = [{**message(f"m{i:04d}", 10), "pos": "same-cursor"} for i in range(256)]
        api = unittest.mock.Mock()
        api.latest = AsyncMock(return_value=page)
        with self.assertRaises(RuntimeError):
            await relay.catchup(api, {"id": "account", "room": "test-room"}, 0)
        relay.store.ingest.assert_not_called()


class ChannelTests(unittest.TestCase):
    def test_channel_case_fold_and_unknown(self):
        channels = [{"id": "main", "name": "MAIN"}, {"id": "custom", "name": "中文频道"}]
        self.assertEqual(channel_for(channels, "Main")["id"], "main")
        self.assertEqual(channel_for(channels, "中文频道")["id"], "custom")
        with self.assertRaises(ValueError):
            channel_for(channels, "missing")


if __name__ == "__main__":
    unittest.main()
