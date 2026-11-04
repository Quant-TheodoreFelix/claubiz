import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Event:
    kind: str
    run_id: str
    task_id: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)


class EventBus:
    def __init__(self) -> None:
        self._queues: list[asyncio.Queue[Event]] = []
        self._listeners: list[Callable[[Event], None]] = []

    def subscribe(self, maxsize: int = 1000) -> asyncio.Queue[Event]:
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=maxsize)
        self._queues.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[Event]) -> None:
        if queue in self._queues:
            self._queues.remove(queue)

    def listen(self, callback: Callable[[Event], None]) -> None:
        self._listeners.append(callback)

    def emit(self, kind: str, run_id: str, task_id: str | None = None, **data: Any) -> Event:
        event = Event(kind=kind, run_id=run_id, task_id=task_id, data=data)
        for queue in self._queues:
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(event)
        for callback in self._listeners:
            callback(event)
        return event
