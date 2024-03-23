#!/usr/bin/python3 -u

import json
import logging
import os
from queue import SimpleQueue
import re
import select
import struct
import sys
import time
from threading import Thread
from typing import Any, NamedTuple

from i3ipc import Connection, Event, WindowEvent, WorkspaceEvent  # type: ignore


ConID = int
JsonValue = None | bool | int | float | str | list | dict[str, Any]
UUID = str
WindowID = int
Workspace = str


class Shutdown:
    pass


SHUTDOWN = Shutdown()


class Request(NamedTuple):
    """
    Incoming message.
    """
    body: JsonValue


class Notification(NamedTuple):
    """
    Outgoing message.
    """
    body: JsonValue


class ReceiverThread(Thread):
    """
    A thread that reads and decodes messages from stdin and puts them into queue.
    """

    def __init__(self, q: SimpleQueue) -> None:
        super().__init__(daemon=True, name='receiver')
        self._breaker_w, self._breaker_r = os.pipe()
        self._q = q

    def stop(self) -> None:
        """
        Tell the thread to terminate, and wait until it does.

        This can be called from outside the thread.
        """
        os.close(self._breaker_w)
        self.join()
        os.close(self._breaker_r)

    def _get_message(self) -> JsonValue | Shutdown:
        """
        Read and decode a message from stdin.

        Return SHUTDOWN if the host app should terminate.
        """
        readable, _, _ = select.select([sys.stdin, self._breaker_r], [], [])
        if self._breaker_r in readable:
            logging.info('normal shutdown')
            return SHUTDOWN
        raw_length = sys.stdin.buffer.read(4)
        if len(raw_length) == 0:
            logging.warning('cannot read message length, shutting down')
            return SHUTDOWN
        [message_length] = struct.unpack('@I', raw_length)
        message = sys.stdin.buffer.read(message_length).decode('utf-8')
        return json.loads(message)

    def run(self) -> None:
        """
        Receive incoming messages, decode and put them in the queue.
        """
        try:
            while True:
                received_message = self._get_message()
                if isinstance(received_message, Shutdown):
                    self._q.put(SHUTDOWN)
                    return
                logging.info('→ %s', received_message)
                self._q.put(Request(received_message))
        except Exception:
            logging.exception('exception')


class I3Thread(Thread):
    """
    A thread that maintains a connection to i3 and handles subscribed events.
    """

    def __init__(self, q: SimpleQueue) -> None:
        super().__init__(daemon=True, name='i3')
        self._i3: Connection | None = None
        self._q = q
        self._stopping = False
        self._windows: dict[WindowID, UUID] = {}
        self._workspaces: dict[ConID, Workspace] = {}
        self._inhibit_move = 0

    def stop(self) -> None:
        """
        Tell the thread to stop and wait until it does.
        """
        self._stopping = True
        if self._i3:
            self._i3.main_quit()
        self.join()

    def handle_windows(self, windows: dict[UUID, Workspace | None]) -> Notification:
        """
        Handle the incoming request to identify, move and/or locate windows.

        Return the corresponding outgoing message.
        """
        self._inhibit_move += 1
        i3 = Connection()
        time.sleep(0.1)
        tree = i3.get_tree()
        response_payload: dict[UUID, Workspace] = {}
        for uuid, workspace in windows.items():
            cons = tree.find_titled(fr'^{re.escape(uuid)} \|')
            if not cons:
                logging.error('%s not found', uuid)
                continue
            if len(cons) > 1:
                logging.warning('%s found more than once', uuid)

            self._windows[cons[0].window] = uuid

            if workspace is not None:
                cons[0].command(f'move --no-auto-back-and-forth container to workspace "{workspace}"')
                response_payload[uuid] = workspace
            else:
                response_payload[uuid] = cons[0].workspace().name

        self._inhibit_move -= 1
        return Notification({'windows': response_payload})

    def window_move(self, i3: Connection, e: WindowEvent) -> None:
        """
        When a window is moved, tell the addon which and where.
        """
        if self._inhibit_move:
            return  # handle_windows is moving things around

        window = e.container.window
        uuid = self._windows.get(window)
        if uuid is None:
            return  # not a window we’re tracking

        workspace = Connection().get_tree().find_by_window(window).workspace().name
        self._q.put(Notification({'window::move': {uuid: workspace}}))

    def workspace_renamed(self, i3: Connection, e: WorkspaceEvent) -> None:
        """
        When a workspace is renamed, tell the addon its old and new names.
        """
        old_name = self._workspaces.get(e.current.id, None)
        self._workspaces[e.current.id] = e.current.name
        if old_name:
            self._q.put(Notification({'workspace::rename': {old_name: e.current.name}}))

    def run(self) -> None:
        """
        Maintain an i3 connection. If disconnected, reconnect and resubscribe.
        Maintain a map of workspace container IDs to names.
        Watch for window move and workspace rename events, and forward them to the addon.
        """
        try:
            while True:
                try:
                    self._i3 = Connection()
                except FileNotFoundError:
                    time.sleep(0.1)
                    continue
                logging.info('connected')

                self._workspaces = {ws.id: ws.name for ws in self._i3.get_tree().workspaces()}
                self._i3.on(Event.WINDOW_MOVE, self.window_move)
                self._i3.on(Event.WORKSPACE_RENAME, self.workspace_renamed)
                self._i3.main()
                logging.info('disconnected')
                self._i3 = None
                if self._stopping:
                    return
        except Exception:
            logging.exception('exception')


def send_message(message_content: JsonValue) -> None:
    """
    Encode and send a message to stdout.
    """
    logging.info('← %s', message_content)
    encoded_content = json.dumps(message_content, separators=(',', ':')).encode('utf-8')
    encoded_length = struct.pack('@I', len(encoded_content))
    sys.stdout.buffer.write(encoded_length)
    sys.stdout.buffer.write(encoded_content)
    sys.stdout.buffer.flush()


def main():
    logging.basicConfig(
        filename='/tmp/i3_workspaces.log',
        level=logging.DEBUG,
        format='%(levelname)-8s %(asctime)s [%(threadName)s] %(message)s')
    # logging.basicConfig(handlers=[], level=logging.ERROR)
    try:
        q = SimpleQueue()

        receiver = ReceiverThread(q)
        receiver.start()

        i3thread = I3Thread(q)
        i3thread.start()

        try:
            while True:
                message = q.get()
                if message is SHUTDOWN:
                    logging.info('shutting down')
                    break
                if isinstance(message, Notification):
                    send_message(message.body)
                if isinstance(message, Request):
                    if 'windows' in message.body:
                        response = i3thread.handle_windows(message.body['windows'])
                        send_message(response.body)
        finally:
            receiver.stop()
            i3thread.stop()

    except BaseException:
        logging.exception('exception')


if __name__ == '__main__':
    main()
