"""Main Music Assistant class."""
from __future__ import annotations

import asyncio
import functools
import logging
import threading
from time import time
from types import TracebackType
from typing import Any, Callable, Coroutine, List, Optional, Tuple, Type, Union

import aiohttp
from databases import DatabaseURL
from music_assistant.constants import EventType
from music_assistant.controllers.metadata import MetaDataController
from music_assistant.controllers.music import MusicController
from music_assistant.controllers.players import PlayerController
from music_assistant.helpers.cache import Cache
from music_assistant.helpers.database import Database

EventCallBackType = Callable[[EventType, Any], None]
EventSubscriptionType = Tuple[EventCallBackType, Optional[Tuple[EventType]]]


class MusicAssistant:
    """Main MusicAssistant object."""

    def __init__(
        self,
        db_url: DatabaseURL,
        stream_port: int = 8095,
        session: Optional[aiohttp.ClientSession] = None,
    ) -> None:
        """
        Create an instance of MusicAssistant.

            db_url: Database connection string/url.
            stream_port: TCP port used for streaming audio.
            session: Optionally provide an aiohttp clientsession
        """

        self.loop: asyncio.AbstractEventLoop = None
        self.http_session: aiohttp.ClientSession = session
        self.http_session_provided = session is not None
        self.logger = logging.getLogger(__name__)

        self._listeners = []
        self._jobs = asyncio.Queue()

        # init core controllers
        self.database = Database(self, db_url)
        self.cache = Cache(self)
        self.metadata = MetaDataController(self)
        self.music = MusicController(self)
        self.players = PlayerController(self, stream_port)
        self._tracked_tasks: List[asyncio.Task] = []
        self.closed = False

    async def setup(self) -> None:
        """Async setup of music assistant."""
        # initialize loop
        self.loop = asyncio.get_event_loop()
        # create shared aiohttp ClientSession
        if not self.http_session:
            self.http_session = aiohttp.ClientSession(
                loop=self.loop,
                connector=aiohttp.TCPConnector(ssl=False),
            )
        # setup core controllers
        await self.cache.setup()
        await self.music.setup()
        await self.metadata.setup()
        await self.players.setup()
        self.create_task(self.__process_jobs())

    async def stop(self) -> None:
        """Stop running the music assistant server."""
        self.logger.info("Stop called, cleaning up...")
        # cancel all running tasks
        for task in self._tracked_tasks:
            task.cancel()
        self.signal_event(EventType.SHUTDOWN)
        self.closed = True
        if self.http_session and not self.http_session_provided:
            await self.http_session.close()

    def signal_event(self, event_type: EventType, event_details: Any = None) -> None:
        """
        Signal (systemwide) event.

            :param event_msg: the eventmessage to signal
            :param event_details: optional details to send with the event.
        """
        if self.closed:
            return
        for cb_func, event_filter in self._listeners:
            if event_filter is None or event_type in event_filter:
                self.create_task(cb_func, event_type, event_details)

    def subscribe(
        self,
        cb_func: EventCallBackType,
        event_filter: Union[EventType, Tuple[EventType], None] = None,
    ) -> Callable:
        """
        Add callback to event listeners.

        Returns function to remove the listener.
            :param cb_func: callback function or coroutine
            :param event_filter: Optionally only listen for these events
        """
        if isinstance(event_filter, EventType):
            event_filter = (event_filter,)
        listener = (cb_func, event_filter)
        self._listeners.append(listener)

        def remove_listener():
            self._listeners.remove(listener)

        return remove_listener

    def add_job(self, job: Coroutine, name: Optional[str] = None) -> None:
        """Add job to be (slowly) processed in the background (one by one)."""
        if not name:
            name = job.__qualname__ or job.__name__
        self._jobs.put_nowait((name, job))

    def create_task(
        self,
        target: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> Union[asyncio.Task, asyncio.Future]:
        """
        Create Task on (main) event loop from Callable or awaitable.

        Tasks created by this helper will be properly cancelled on stop.
        """
        if self.closed:
            return

        # Check for partials to properly determine if coroutine function
        check_target = target
        while isinstance(check_target, functools.partial):
            check_target = check_target.func

        async def executor_wrapper(_target: Callable, *_args, **_kwargs):
            return await self.loop.run_in_executor(None, _target, *_args, **_kwargs)

        # called from other thread
        if threading.current_thread() is not threading.main_thread():
            if asyncio.iscoroutine(check_target):
                task = asyncio.run_coroutine_threadsafe(target, self.loop)
            elif asyncio.iscoroutinefunction(check_target):
                task = asyncio.run_coroutine_threadsafe(target(*args), self.loop)
            else:
                task = asyncio.run_coroutine_threadsafe(
                    executor_wrapper(target, *args, **kwargs), self.loop
                )
        else:
            if asyncio.iscoroutine(check_target):
                task = self.loop.create_task(target)
            elif asyncio.iscoroutinefunction(check_target):
                task = self.loop.create_task(target(*args))
            else:
                task = self.loop.create_task(executor_wrapper(target, *args, **kwargs))

        def task_done_callback(*args, **kwargs):
            self._tracked_tasks.remove(task)

        self._tracked_tasks.append(task)
        task.add_done_callback(task_done_callback)
        return task

    async def __process_jobs(self):
        """Process jobs in the background."""
        while True:
            name, job = await self._jobs.get()
            time_start = time()
            self.logger.debug("Start processing job [%s].", name)
            try:
                # await job
                task = self.create_task(job, name=name)
                await task
            except Exception as err:  # pylint: disable=broad-except
                self.logger.error(
                    "Job [%s] failed with error %s.", name, str(err), exc_info=err
                )
            else:
                duration = round(time() - time_start, 2)
                self.logger.info("Finished job [%s] in %s seconds.", name, duration)

    async def __aenter__(self) -> "MusicAssistant":
        """Return Context manager."""
        await self.setup()
        return self

    async def __aexit__(
        self,
        exc_type: Type[BaseException],
        exc_val: BaseException,
        exc_tb: TracebackType,
    ) -> Optional[bool]:
        """Exit context manager."""
        await self.stop()
        if exc_val:
            raise exc_val
        return exc_type
