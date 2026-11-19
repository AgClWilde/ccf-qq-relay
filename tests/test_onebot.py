"""Real loopback WebSocket tests; no NapCat or external account is contacted."""

import asyncio
import json
import socket
import unittest

from aiohttp import web

from onebot import (
    OneBot,
    OneBotActionError,
    OneBotDisconnected,
    OneBotTimeout,
)


class OneBotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clients = []
        self.run_tasks = []
        self.headers = []
        self.connections = 0
        self.handler = self.default_handler
        app = web.Application()
        app.router.add_get("/onebot", self.serve)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.sock.setblocking(False)
        port = self.sock.getsockname()[1]
        self.site = web.SockSite(self.runner, self.sock)
        await self.site.start()
        self.url = "http://127.0.0.1:%d/onebot" % port

    async def asyncTearDown(self):
        for client in self.clients:
            await client.close()
        for task in self.run_tasks:
            try:
                await asyncio.wait_for(task, timeout=1)
            except asyncio.TimeoutError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        await self.runner.cleanup()

    async def serve(self, request):
        self.headers.append(request.headers.get("Authorization"))
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.connections += 1
        await self.handler(ws)
        return ws

    async def default_handler(self, ws):
        async for frame in ws:
            request = json.loads(frame.data)
            await self.reply(ws, request, request["params"])

    async def reply(self, ws, request, data=None, status="ok", retcode=0):
        await ws.send_json(
            {
                "echo": request["echo"],
                "status": status,
                "retcode": retcode,
                "data": data,
            }
        )

    async def start_client(self, callback=None):
        async def discard_event(event):
            pass

        client = OneBot(self.url, "local-test-token", callback or discard_event)
        client.RECONNECT_MIN = 0.01
        client.RECONNECT_MAX = 0.02
        client.CALL_TIMEOUT = 0.3
        self.clients.append(client)
        task = asyncio.create_task(client.run())
        self.run_tasks.append(task)
        await asyncio.wait_for(client.ready.wait(), timeout=1)
        return client

    async def test_auth_echo_correlation_and_reentrant_callback(self):
        seen = []
        callback_finished = asyncio.Event()
        client_holder = []

        async def callback(event):
            seen.append(event)
            result = await client_holder[0].call("callback_action", {})
            self.assertEqual(result, {"from": "callback"})
            callback_finished.set()

        async def handler(ws):
            requests = []
            async for frame in ws:
                request = json.loads(frame.data)
                if request["action"] == "callback_action":
                    await self.reply(ws, request, {"from": "callback"})
                    continue
                requests.append(request)
                if len(requests) == 2:
                    await ws.send_str("invalid-json")
                    await ws.send_json(["not-an-event"])
                    await ws.send_json({"echo": "unknown", "status": "ok"})
                    await self.reply(ws, requests[1], requests[1]["params"])
                    await ws.send_json({"post_type": "message", "user_id": 7})
                    await self.reply(ws, requests[0], requests[0]["params"])

        self.handler = handler
        client = await self.start_client(callback)
        client_holder.append(client)
        results = await asyncio.gather(
            client.call("first", {"index": 1}),
            client.call("second", {"index": 2}),
        )
        self.assertEqual(results, [{"index": 1}, {"index": 2}])
        await asyncio.wait_for(callback_finished.wait(), timeout=1)
        self.assertEqual(seen, [{"post_type": "message", "user_id": 7}])
        self.assertEqual(self.headers, ["Bearer local-test-token"])
        self.assertFalse(client._pending)

    async def test_failure_response_is_sanitized(self):
        async def handler(ws):
            async for frame in ws:
                request = json.loads(frame.data)
                await self.reply(
                    ws, request, {"message": "sensitive-value"}, "failed", 1400
                )

        self.handler = handler
        client = await self.start_client()
        with self.assertRaises(OneBotActionError) as result:
            await client.call("send_msg", {"message": "private-content"})
        self.assertEqual(str(result.exception), "OneBot action failed")
        self.assertFalse(client._pending)

    async def test_timeout_releases_pending_call_and_ignores_late_reply(self):
        async def handler(ws):
            async for frame in ws:
                request = json.loads(frame.data)
                if request["action"] == "slow":
                    await asyncio.sleep(0.08)
                await self.reply(ws, request, {"action": request["action"]})

        self.handler = handler
        client = await self.start_client()
        client.CALL_TIMEOUT = 0.03
        with self.assertRaises(OneBotTimeout):
            await client.call("slow", {})
        self.assertFalse(client._pending)
        client.CALL_TIMEOUT = 0.3
        self.assertEqual(await client.call("next", {}), {"action": "next"})
        self.assertFalse(client._pending)

    async def test_disconnect_fails_inflight_and_reconnects(self):
        async def handler(ws):
            async for frame in ws:
                request = json.loads(frame.data)
                if self.connections == 1:
                    await ws.close()
                    return
                await self.reply(ws, request, {"reconnected": True})

        self.handler = handler
        client = await self.start_client()
        with self.assertRaises(OneBotDisconnected):
            await client.call("send_msg", {})
        self.assertEqual(await client.call("get_status", {}), {"reconnected": True})
        self.assertEqual(self.connections, 2)

    async def test_close_cancels_callback_and_wakes_waiting_calls(self):
        callback_started = asyncio.Event()
        callback_stopped = asyncio.Event()

        async def callback(event):
            callback_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                callback_stopped.set()

        async def handler(ws):
            await ws.send_json({"post_type": "message"})
            async for frame in ws:
                pass

        self.handler = handler
        client = await self.start_client(callback)
        await asyncio.wait_for(callback_started.wait(), timeout=1)
        await client.close()
        self.assertTrue(callback_stopped.is_set())
        self.assertFalse(client._event_tasks)
        self.assertFalse(client.ready.is_set())
        with self.assertRaises(OneBotDisconnected):
            await client.call("closed", {})

        # Also cover a caller waiting before run() ever establishes a connection.
        unstarted = OneBot(self.url, "", callback)
        self.clients.append(unstarted)
        waiting = asyncio.create_task(unstarted.call("waiting", {}))
        await asyncio.sleep(0)
        await unstarted.close()
        with self.assertRaises(OneBotDisconnected):
            await asyncio.wait_for(waiting, timeout=1)


if __name__ == "__main__":
    unittest.main()
