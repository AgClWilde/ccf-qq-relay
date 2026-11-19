"""Real OneBot WebSocket + relay command integration, with a local CCF fake."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from aiohttp import WSMsgType, web

from ccfolia import CCFoliaError
from relay import Relay


ROOM = "https://ccfolia.com/rooms/exampleRoom"
EMAIL = "member@example.invalid"
PASSWORD = "example password with spaces"


class FakeCCFolia:
    calls = []
    fail_login = False
    metadata = {"id": "exampleRoom", "name": "Example Room", "channels": [
        {"id": "main", "name": "MAIN"}, {"id": "info", "name": "INFO"},
        {"id": "other", "name": "OTHER"}]}

    def __init__(self, session, token="", refresh_token=""):
        self.token = token
        self.refresh_token = refresh_token
        self.user_id = ""

    async def login(self, username, password):
        type(self).calls.append(("login", username, password))
        if type(self).fail_login:
            raise CCFoliaError("INVALID_LOGIN")
        self.token, self.refresh_token, self.user_id = "example-token", "example-refresh", "example-user"
        return {"token": self.token, "refresh_token": self.refresh_token, "user_id": self.user_id}

    async def room(self, room):
        type(self).calls.append(("room", room))
        return type(self).metadata

    async def latest(self, room, channel_id=None, limit=256, before=None):
        type(self).calls.append(("latest", room, channel_id, limit))
        return []


class RelayIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.received = asyncio.Queue()
        self.socket = None
        self.sequence = 0
        app = web.Application()
        app.router.add_get("/onebot", self._onebot_server)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        port = self.site._server.sockets[0].getsockname()[1]
        config = {"database": str(Path(self.temporary.name) / "relay.sqlite3"),
                  "onebot_url": f"ws://127.0.0.1:{port}/onebot", "onebot_token": "",
                  "allowed_users": ["101", "202"]}
        self.relay = Relay(config)
        # This suite exercises commands through the real OneBot transport.
        # Watch/catchup transitions are covered separately with source fixtures.
        self.relay.reconcile = AsyncMock()
        self.relay.restart = AsyncMock()
        self.patcher = patch("relay.CCFolia", FakeCCFolia)
        self.patcher.start()
        FakeCCFolia.calls = []
        FakeCCFolia.fail_login = False
        self.transport = asyncio.create_task(self.relay.onebot.run())
        await asyncio.wait_for(self.relay.onebot.ready.wait(), 2)

    async def asyncTearDown(self):
        await self.relay.onebot.close()
        self.transport.cancel()
        await asyncio.gather(self.transport, return_exceptions=True)
        await self.runner.cleanup()
        self.relay.store.db.close()
        self.patcher.stop()
        self.temporary.cleanup()

    async def _onebot_server(self, request):
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        self.socket = socket
        async for frame in socket:
            if frame.type == WSMsgType.TEXT:
                request = json.loads(frame.data)
                await socket.send_json({"status": "ok", "retcode": 0, "echo": request["echo"],
                                        "data": {"message_id": 123}})
                await self.received.put(request)
        return socket

    async def _send_event(self, text, kind="private", owner=101, target=909, message_id=None):
        self.sequence += 1
        event = {"post_type": "message", "message_type": kind, "user_id": owner,
                 "self_id": 999, "message_id": message_id if message_id is not None else self.sequence,
                 "message": [{"type": "text", "data": {"text": text}}]}
        if kind == "group":
            event["group_id"] = target
        await self.socket.send_json(event)

    async def _command(self, text, **kwargs):
        await self._send_event(text, **kwargs)
        result = await asyncio.wait_for(self.received.get(), 2)
        self.assertEqual(result["params"]["message"][0]["type"], "text")
        return result["params"]["message"][0]["data"]["text"], result

    async def _bind(self, channel=None):
        before = (channel + " ") if channel else ""
        return await self._command(f".ccf提醒 {before}{ROOM} {EMAIL} {PASSWORD}")

    async def test_unlisted_qq_can_bind_and_use_group_commands(self):
        # Existing installations may still contain a restrictive allowlist.
        # It must not filter any private or group CCF commands.
        owner = 345678
        reply, request = await self._command(".ccf提醒", owner=owner)
        self.assertIn(".ccf提醒", reply)
        self.assertEqual(request["params"]["user_id"], owner)

        reply, _ = await self._command(f".ccf提醒 {ROOM} {EMAIL} {PASSWORD}", owner=owner)
        self.assertIn("Example Room-MAIN", reply)
        reply, request = await self._command(f".ccf提醒 INFO {ROOM}", owner=owner, kind="group")
        self.assertIn("Example Room-INFO", reply)
        self.assertEqual(request["params"]["group_id"], 909)
        reply, _ = await self._command(".ccf提醒 状态", owner=owner, kind="group")
        self.assertIn("Example Room-INFO", reply)
        reply, _ = await self._command(f".ccf提醒 关闭 INFO {ROOM}", owner=owner, kind="group")
        self.assertIn("已关闭 1 个", reply)

    async def test_unlisted_qq_without_existing_account_must_bind_first(self):
        reply, _ = await self._command(f".ccf提醒 INFO {ROOM}", owner=987654321, kind="group")
        self.assertIn("请先私聊", reply)

    async def test_default_main_repeated_channels_and_idempotent_subscription(self):
        reply, request = await self._bind()
        self.assertIn("Example Room-MAIN", reply)
        self.assertEqual(request["action"], "send_private_msg")
        self.assertEqual(request["params"]["user_id"], 101)
        main = self.relay.store.active()[0]
        self.assertEqual(main["channel"], "main")
        self.relay.store.db.execute("INSERT INTO seen VALUES (?,?)", (main["id"], "already-delivered"))
        self.relay.store.db.commit()

        for channel in ("INFO", "OTHER"):
            reply, _ = await self._command(f".ccf提醒 {channel} {ROOM}")
            self.assertIn("Example Room-" + channel, reply)
        self.assertEqual({b["channel"] for b in self.relay.store.active()}, {"main", "info", "other"})
        self.assertEqual(len(self.relay.store.accounts()), 1)

        reply, _ = await self._command(f".ccf提醒 {ROOM}")
        self.assertIn("已保留订阅", reply)
        current = next(b for b in self.relay.store.active() if b["channel"] == "main")
        self.assertEqual(current["id"], main["id"])
        self.assertEqual(current["started"], main["started"])
        self.assertEqual(self.relay.store.rows("SELECT message FROM seen"), [{"message": "already-delivered"}])
        logins = [c for c in FakeCCFolia.calls if c[0] == "login"]
        self.assertEqual(logins, [("login", EMAIL, PASSWORD)])
        dumped = "\n".join(self.relay.store.db.iterdump())
        self.assertNotIn(EMAIL, dumped)
        self.assertNotIn(PASSWORD, dumped)

    async def test_group_credentials_are_rejected_without_login_or_storage(self):
        reply, request = await self._command(f".ccf提醒 {ROOM} {EMAIL} {PASSWORD}", kind="group")
        self.assertIn("请先私聊", reply)
        self.assertEqual(request["action"], "send_group_msg")
        self.assertEqual(request["params"]["group_id"], 909)
        self.assertEqual(FakeCCFolia.calls, [])
        self.assertEqual(self.relay.store.active(), [])
        self.assertNotIn(EMAIL, reply)
        self.assertNotIn(PASSWORD, reply)

    async def test_group_attach_reuses_owners_private_account_and_perchannel_close(self):
        await self._bind()
        await self._command(f".ccf提醒 {ROOM}", kind="group")
        await self._command(f".ccf提醒 INFO {ROOM}", kind="group")
        await self._command(f".ccf提醒 OTHER {ROOM}", kind="group")
        self.assertEqual(len(self.relay.store.active()), 4)
        self.assertEqual(len(self.relay.store.accounts()), 1)
        reply, _ = await self._command(f".ccf提醒 关闭 INFO {ROOM}", kind="group")
        self.assertIn("已关闭 1 个", reply)
        groups = [b for b in self.relay.store.active() if b["target_type"] == "group"]
        self.assertEqual({b["channel"] for b in groups}, {"main", "other"})
        self.assertEqual(len([b for b in self.relay.store.active() if b["target_type"] == "private"]), 1)

        reply, _ = await self._command(f".ccf提醒 关闭 {ROOM}", kind="group")
        self.assertIn("已关闭 2 个", reply)
        self.assertEqual(len(self.relay.store.active()), 1)
        self.assertEqual(len(self.relay.store.accounts()), 1)

    async def test_another_authorized_user_cannot_reuse_someone_elses_private_account(self):
        await self._bind()
        reply, _ = await self._command(f".ccf提醒 {ROOM}", kind="group", owner=202)
        self.assertIn("请先私聊", reply)
        self.assertEqual(len(self.relay.store.active()), 1)

    async def test_another_member_can_manage_bound_group_without_replacing_identity(self):
        await self._bind()
        await self._command(f".ccf提醒 {ROOM}", kind="group")
        await self._command(f".ccf提醒 {ROOM} {EMAIL} {PASSWORD}", owner=202)
        reply, _ = await self._command(f".ccf提醒 {ROOM}", kind="group", owner=202)
        self.assertIn("已保留订阅", reply)
        group = next(b for b in self.relay.store.active() if b["target_type"] == "group")
        self.assertEqual(group["owner"], "101")
        reply, _ = await self._command(f".ccf提醒 关闭 {ROOM}", kind="group", owner=202)
        self.assertIn("已关闭 1 个", reply)
        await self._command(f".ccf提醒 {ROOM}", kind="group", owner=202)
        group = next(b for b in self.relay.store.active() if b["target_type"] == "group")
        self.assertEqual(group["owner"], "202")
        self.assertEqual(len(self.relay.store.active()), 3)

    async def test_channel_list_and_status_use_saved_account_without_new_credentials(self):
        await self._bind()
        reply, _ = await self._command(f".ccf提醒 频道 {ROOM}")
        self.assertIn("MAIN、INFO、OTHER", reply)
        reply, _ = await self._command(".ccf提醒 状态")
        self.assertIn("Example Room-MAIN", reply)
        self.assertIn("待发 0 条", reply)
        self.assertNotIn("example-token", reply)
        self.assertNotIn("example-refresh", reply)

    async def test_bad_channel_does_not_add_subscription(self):
        await self._bind()
        reply, _ = await self._command(f".ccf提醒 UNKNOWN {ROOM}")
        self.assertIn("频道不存在", reply)
        self.assertEqual(len(self.relay.store.active()), 1)

    async def test_test_commands_read_source_without_changing_subscriptions(self):
        await self._bind()
        before = self.relay.store.active()
        FakeCCFolia.calls = []
        for text in (f".ccf提醒 测试 {ROOM}", f".ccf提醒 测试 MAIN {ROOM}",
                     f".ccf提醒 test {ROOM} MAIN"):
            reply, _ = await self._command(text)
            self.assertIn("访问成功", reply)
        self.assertTrue(any(c[0] == "latest" for c in FakeCCFolia.calls))
        self.assertEqual(self.relay.store.active(), before)
        self.assertEqual(self.relay.store.rows("SELECT * FROM outbox"), [])

    async def test_auth_failure_response_contains_no_credentials(self):
        FakeCCFolia.fail_login = True
        reply, _ = await self._bind()
        self.assertIn("登录或访问失败", reply)
        self.assertNotIn(EMAIL, reply)
        self.assertNotIn(PASSWORD, reply)
        self.assertEqual(self.relay.store.active(), [])

    async def test_replayed_onebot_command_does_not_repeat_response_or_side_effect(self):
        await self._bind()
        text = f".ccf提醒 INFO {ROOM}"
        await self._command(text, message_id=88)
        await self._send_event(text, message_id=88)
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(self.received.get(), 0.05)
        self.assertEqual(len(self.relay.store.active()), 2)


if __name__ == "__main__":
    unittest.main()
