"""Live WebSocket pushes that never wait on the network (issue #84).

A phone whose connection stays up but has stopped reading (out of Wi-Fi range, tab
suspended) fills its TCP send buffer, and the next send waits until the connection really
dies. Sends used to happen inline from the lap engine, so that one phone froze lap timing for
everyone. Now publish() only serializes the message once and puts it on each client's own
bounded queue; a sender task per client does the network I/O. A client that can't take a
message within SEND_TIMEOUT, or falls MAX_QUEUED messages behind, is dropped and its socket
closed; the pages reconnect by themselves and get a fresh snapshot.
"""
import asyncio
import json
import logging
import weakref
from typing import Any, Callable, Dict, Iterable, Optional, Set

logger = logging.getLogger("Broadcaster")

SEND_TIMEOUT = 5.0  # seconds one message may take to go out to one client
MAX_QUEUED = 100    # messages a client may fall behind before it's dropped
CLOSE_TIMEOUT = 1.0


def serialize(message: Dict[str, Any]) -> str:
    # Same encoding as Starlette's WebSocket.send_json, so pages see the exact same text
    return json.dumps(message, separators=(",", ":"), ensure_ascii=False)


class _Client:
    def __init__(self, ws, broadcaster: "Broadcaster"):
        self.ws = ws
        self.queue: "asyncio.Queue[str]" = asyncio.Queue(maxsize=broadcaster.max_queued)
        self.loop = asyncio.get_running_loop()
        self.busy = False
        self.task = self.loop.create_task(self._run(broadcaster))

    def alive(self) -> bool:
        try:
            return not self.task.done() and self.loop is asyncio.get_running_loop()
        except RuntimeError:
            return False

    async def _run(self, broadcaster: "Broadcaster"):
        while True:
            text = await self.queue.get()
            self.busy = True
            try:
                await asyncio.wait_for(self.ws.send_text(text), broadcaster.send_timeout)
            except asyncio.TimeoutError:
                broadcaster.drop(self.ws, f"send took over {broadcaster.send_timeout:g}s")
                return
            except Exception as e:  # closed socket, reset connection, ...
                broadcaster.drop(self.ws, f"send failed: {type(e).__name__}", close=False)
                return
            finally:
                self.busy = False


class Broadcaster:
    def __init__(self, on_drop: Optional[Callable[[Any], None]] = None,
                 send_timeout: float = SEND_TIMEOUT, max_queued: int = MAX_QUEUED):
        self.on_drop = on_drop
        self.send_timeout = send_timeout
        self.max_queued = max_queued
        self._clients: Dict[Any, _Client] = {}
        self._gone: "weakref.WeakSet[Any]" = weakref.WeakSet()  # dropped or ended: never again
        self._closing: Set[asyncio.Task] = set()
        self.dropped = 0

    def _client(self, ws) -> _Client:
        client = self._clients.get(ws)
        if client is None or not client.alive():
            client = self._clients[ws] = _Client(ws, self)
        return client

    def send(self, ws, message: Dict[str, Any]):
        """Queues one message for one client. Must be called on the event loop."""
        self.publish(message, [ws])

    def publish(self, message: Dict[str, Any], targets: Iterable[Any]):
        """Queues `message` for every target and returns at once. Must be called on the event loop."""
        targets = list(targets)
        if not targets:
            return
        text = serialize(message)
        for ws in targets:
            if ws in self._gone:
                continue
            try:
                self._client(ws).queue.put_nowait(text)
            except asyncio.QueueFull:
                self.drop(ws, f"more than {self.max_queued} messages behind")

    def drop(self, ws, reason: str, close: bool = True):
        """Stops sending to `ws`, forgets it and (unless it's already gone) closes it."""
        client = self._clients.pop(ws, None)
        self._gone.add(ws)
        if client is None:
            return
        self.dropped += 1
        logger.warning(f"Dropping live client: {reason}")
        if client.alive() and client.task is not asyncio.current_task():
            client.task.cancel()
        if self.on_drop:
            self.on_drop(ws)
        if close and hasattr(ws, "close"):
            task = asyncio.get_running_loop().create_task(self._close(ws))
            self._closing.add(task)  # the loop only keeps weak references to tasks
            task.add_done_callback(self._closing.discard)

    async def _close(self, ws):
        try:
            await asyncio.wait_for(ws.close(code=1011), CLOSE_TIMEOUT)
        except Exception:
            pass  # stalled or already closed; it's out of the lists either way

    def forget(self, ws):
        """The connection ended: stop its sender (no close, no on_drop)."""
        self._gone.add(ws)
        client = self._clients.pop(ws, None)
        if client is not None and client.alive() and client.task is not asyncio.current_task():
            client.task.cancel()

    async def drain(self, timeout: float = 2.0):
        """Waits until every live client's queue has gone out (for tests and shutdown)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while any(c.alive() and (c.busy or not c.queue.empty()) for c in list(self._clients.values())):
            if loop.time() > deadline:
                return
            await asyncio.sleep(0.005)

    def cancel(self):
        for ws in list(self._clients):
            self.forget(ws)
        for task in list(self._closing):
            task.cancel()
