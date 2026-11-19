"""OneBot v11 forward-WebSocket client.

Protocol payloads and exception details deliberately never enter logs here.
"""

import asyncio
import json
import uuid
from typing import Any, Awaitable, Callable, Dict, Optional, Set

import aiohttp


class OneBotError(RuntimeError):
    """Base class for sanitized transport errors."""


class OneBotDisconnected(OneBotError):
    def __init__(self) -> None:
        super().__init__("OneBot connection unavailable")


class OneBotTimeout(OneBotError):
    def __init__(self) -> None:
        super().__init__("OneBot action timed out")


class OneBotActionError(OneBotError):
    def __init__(self) -> None:
        super().__init__("OneBot action failed")


class OneBot:
    """Reconnect continuously and correlate API responses by ``echo``.

    Event callbacks run in tracked tasks, allowing a callback to call an API
    without obstructing the sole WebSocket reader. A disconnect fails every
    in-flight API call; the caller decides whether retrying is appropriate.
    """

    CALL_TIMEOUT = 25.0
    RECONNECT_MIN = 1.0
    RECONNECT_MAX = 30.0

    def __init__(
        self,
        url: str,
        token: str,
        on_event: Callable[[Dict[str, Any]], Awaitable[None]],
    ) -> None:
        self.url = url
        self._token = token
        self._on_event = on_event
        self.ready = asyncio.Event()
        self._stopped = asyncio.Event()
        self._send_lock = asyncio.Lock()
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._pending: Dict[str, asyncio.Future] = {}
        self._event_tasks: Set[asyncio.Task] = set()
        self._closed = False
        self._running = False

    async def run(self) -> None:
        """Run until closed or cancelled. Only one run task is supported."""
        if self._running:
            raise OneBotError("OneBot client already running")
        if self._closed:
            return
        self._running = True
        delay = self.RECONNECT_MIN
        headers = {"Authorization": "Bearer " + self._token} if self._token else {}
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=10)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                while not self._closed:
                    connected_at = None
                    try:
                        async with session.ws_connect(
                            self.url,
                            headers=headers,
                            heartbeat=30,
                            max_msg_size=4 * 1024 * 1024,
                        ) as ws:
                            if self._closed:
                                break
                            self._ws = ws
                            connected_at = asyncio.get_running_loop().time()
                            self.ready.set()
                            await self._read(ws)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        # Errors may contain URLs, credentials, or message data.
                        pass
                    finally:
                        self.ready.clear()
                        self._ws = None
                        self._fail_pending()
                    if self._closed:
                        break
                    if connected_at is not None:
                        duration = asyncio.get_running_loop().time() - connected_at
                        if duration >= 30:
                            delay = self.RECONNECT_MIN
                    try:
                        await asyncio.wait_for(self._stopped.wait(), timeout=delay)
                    except asyncio.TimeoutError:
                        pass
                    delay = min(delay * 2, self.RECONNECT_MAX)
        finally:
            self.ready.clear()
            self._ws = None
            self._fail_pending()
            self._running = False

    async def _read(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        async for frame in ws:
            if frame.type == aiohttp.WSMsgType.TEXT:
                try:
                    payload = json.loads(frame.data)
                except (ValueError, TypeError):
                    continue
                if not isinstance(payload, dict):
                    continue
                echo = payload.get("echo")
                if isinstance(echo, str) and echo in self._pending:
                    future = self._pending[echo]
                    if not future.done():
                        future.set_result(payload)
                elif isinstance(payload.get("post_type"), str):
                    task = asyncio.create_task(self._dispatch(payload))
                    self._event_tasks.add(task)
                    task.add_done_callback(self._event_tasks.discard)
            elif frame.type in (
                aiohttp.WSMsgType.ERROR,
                aiohttp.WSMsgType.CLOSE,
                aiohttp.WSMsgType.CLOSED,
            ):
                break

    async def _dispatch(self, payload: Dict[str, Any]) -> None:
        try:
            await self._on_event(payload)
        except asyncio.CancelledError:
            raise
        except Exception:
            # The caller owns diagnostics; never expose the event or credentials.
            pass

    async def call(self, action: str, params: Dict[str, Any]) -> Any:
        """Return ``data`` only after a successful action response.

        The 25-second deadline includes waiting for a connection. A timeout or
        disconnect does not prove that a non-idempotent action was not executed.
        """
        if self._closed:
            raise OneBotDisconnected()
        try:
            return await asyncio.wait_for(
                self._call(action, params), timeout=self.CALL_TIMEOUT
            )
        except asyncio.TimeoutError:
            raise OneBotTimeout() from None

    async def _call(self, action: str, params: Dict[str, Any]) -> Any:
        # wait_for schedules this coroutine separately; close() may run after
        # call() checked _closed but before this coroutine reaches Event.wait.
        if self._closed:
            raise OneBotDisconnected()
        await self.ready.wait()
        ws = self._ws
        if self._closed or ws is None or ws.closed:
            raise OneBotDisconnected()
        echo = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self._pending[echo] = future
        try:
            async with self._send_lock:
                if self._closed or self._ws is not ws or ws.closed:
                    raise OneBotDisconnected()
                try:
                    await ws.send_json(
                        {"action": action, "params": params, "echo": echo}
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    raise OneBotDisconnected() from None
            response = await future
            if response.get("status") != "ok" or response.get("retcode") != 0:
                raise OneBotActionError()
            return response.get("data")
        finally:
            self._pending.pop(echo, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                # A disconnect can race with send_json failing before we await.
                future.exception()

    def _fail_pending(self) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(OneBotDisconnected())

    async def close(self) -> None:
        """Stop reconnecting and cancel outstanding event callbacks."""
        self._closed = True
        self._stopped.set()
        # Wake API calls waiting for the first connection so they fail promptly.
        self.ready.set()
        self._fail_pending()
        ws = self._ws
        if ws is not None:
            await ws.close()
        current = asyncio.current_task()
        tasks = [task for task in self._event_tasks if task is not current]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.ready.clear()
