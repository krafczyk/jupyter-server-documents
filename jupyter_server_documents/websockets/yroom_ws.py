"""Synchronize document rooms and restore their execution routing on connect."""

from __future__ import annotations
from tornado.httpclient import HTTPError
from tornado.websocket import WebSocketHandler
from typing import TYPE_CHECKING
import asyncio
from ..rooms import YRoomManager
import logging

if TYPE_CHECKING:
    from jupyter_server_fileid.manager import BaseFileIdManager
    from jupyter_server.services.contents.manager import AsyncContentsManager, ContentsManager
    from ..rooms import YRoom

class YRoomWebsocket(WebSocketHandler):
    """Serve a room and reconnect its existing notebook kernel bindings.

    Accepts Tornado's standard WebSocket handler constructor arguments. Failed
    reattachment unregisters the new client and closes its connection with code
    1011; kernel processes and other clients are not shut down.
    """
    yroom: YRoom
    room_id: str
    client_id: str | None
    # TODO: change this. we should pass `self.log` from our
    # `ExtensionApp` to log messages w/ "ServerDocsApp" prefix
    log = logging.Logger("TEMP")

    @property
    def ping_interval(self) -> float:
        return self.settings.get("ws_ping_interval", 25000) / 1000

    @property
    def ping_timeout(self) -> float:
        return self.settings.get("ws_ping_timeout", 25000) / 1000

    @property
    def yroom_manager(self) -> YRoomManager:
        return self.settings["yroom_manager"]


    @property
    def fileid_manager(self) -> BaseFileIdManager:
        return self.settings["file_id_manager"]
    

    @property
    def contents_manager(self) -> AsyncContentsManager | ContentsManager:
        return self.settings["contents_manager"]


    def prepare(self):
        # Bind `room_id` attribute
        request_path: str = self.request.path
        self.room_id = request_path.strip("/").split("/")[-1]

        # Verify the file ID contained in the room ID points to a valid file.
        if self.room_id != "JupyterLab:globalAwareness":
            fileid = self.room_id.split(":")[-1]
            path = self.fileid_manager.get_path(fileid)
            if not path:
                raise HTTPError(404, f"No file with ID '{fileid}'.")
    

    async def open(self, *_, **__):
        """Register this client and restore a notebook room's execution routing.

        Unused route arguments are accepted for Tornado compatibility. Returns
        ``None``; initialization/binding errors propagate after client cleanup.
        Session managers without a reconnection hook retain their old behavior.
        """
        self.client_id = None
        # Create the YRoom
        yroom = self.yroom_manager.get_room(self.room_id)
        if not yroom:
            raise HTTPError(500, f"Unable to initialize YRoom '{self.room_id}'.")
        self.yroom = yroom

        # Add self as a client to the YRoom.
        # If `client_id is None`, then the YRoom is being stopped, and this WS
        # should be closed immediately w/ close code 1001.
        self.client_id = self.yroom.clients.add(self)
        if not self.client_id:
            self.close(code=1001)
            return

        session_manager = self.settings.get("session_manager")
        reconnect = getattr(session_manager, "reconnect_yroom", None)
        if self.room_id.startswith("json:notebook:") and reconnect is not None:
            try:
                await reconnect(self.yroom)
            except Exception:
                self.yroom.clients.remove(self.client_id)
                self.client_id = None
                self.close(code=1011, reason="Unable to reconnect notebook execution")
                raise


    def on_message(self, message: bytes):
        if not self.client_id:
            self.close(code=1001)
            return

        # Route all messages to the YRoom for processing
        self.yroom.add_message(self.client_id, message)


    def on_close(self):
        if self.client_id:
            self.log.info(f"Closed Websocket to client '{self.client_id}'.")
            self.yroom.clients.remove(self.client_id)
