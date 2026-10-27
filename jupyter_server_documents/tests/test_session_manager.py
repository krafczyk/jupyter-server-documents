"""Tests for the YDocSessionManager stop callback behavior.

When create_session() sets up a notebook, it registers a stop callback on
the yroom. This callback fires when the room is freed by the GC (via
yroom.stop()). The GC frees rooms that are inactive and empty (no WebSocket
clients connected for a while).

The stop callback must only disconnect the room from the kernel client — it
must NOT delete the session or shut down the kernel. Room GC reclaims memory;
kernel lifecycle is the user's responsibility.
"""
import asyncio
import inspect
from types import SimpleNamespace
import pytest
from unittest.mock import AsyncMock, Mock, patch
from traitlets.config import LoggingConfigurable
from jupyter_server.services.kernels.kernelmanager import MappingKernelManager
from jupyter_server_documents.session_manager import YDocSessionManager
from jupyter_server_documents.websockets.yroom_ws import YRoomWebsocket
from tornado.web import HTTPError


@pytest.fixture
def session_manager():
    """Create a YDocSessionManager with mocked server dependencies.

    Mocks the kernel_manager, file_id_manager, and yroom_manager so we
    can test the session manager's logic without a running Jupyter server.
    """
    mock_file_id_manager = Mock()
    mock_yroom_manager = Mock()
    # Use spec=MappingKernelManager to satisfy the traitlet type check
    mock_kernel_manager = Mock(spec=MappingKernelManager)

    class MockServerApp(LoggingConfigurable):
        @property
        def kernel_manager(self):
            return mock_kernel_manager

        @property
        def web_app(self):
            mock_web_app = Mock()
            mock_web_app.settings = {
                "file_id_manager": mock_file_id_manager,
                "yroom_manager": mock_yroom_manager
            }
            return mock_web_app

    manager = YDocSessionManager(parent=MockServerApp())
    manager._room_ids = {}
    # YDocSessionManager.kernel_manager is a traitlet inherited from
    # SessionManager. We set it with a spec'd mock to pass type validation.
    manager.kernel_manager = mock_kernel_manager
    return manager


class TestCreateSessionStopCallback:
    """Tests for the stop callback registered during create_session.

    These tests verify that when a room is freed by GC:
    1. The yroom is removed from the kernel client (stops message routing)
    2. The session is NOT deleted (kernel stays alive)
    3. The _room_ids mapping persists (enables reconnection)
    """

    @pytest.mark.asyncio
    async def test_stop_callback_removes_yroom_from_kernel_client(self, session_manager):
        """GC'd room must be unregistered from the kernel client.

        If the freed room remains in kernel_client._yrooms, incoming kernel
        messages route to the stopped room, triggering room.restart() and
        defeating the GC.
        """
        session_id = "session-123"
        kernel_id = "kernel-456"
        file_id = "test-file-id"

        # Mock the yroom — we intercept add_stop_callback to capture
        # what callback create_session registers, so we can invoke it later.
        mock_yroom = Mock()
        mock_yroom.room_id = f"json:notebook:{file_id}"

        captured_callbacks = []
        mock_yroom.add_stop_callback = lambda cb: captured_callbacks.append(cb)

        # Mock the kernel client — this is what the stop callback should
        # call remove_yroom() on.
        mock_kernel_client = Mock()
        mock_kernel_client.add_yroom = AsyncMock()
        mock_kernel_client.remove_yroom = AsyncMock()

        mock_kernel_mgr = Mock()
        mock_kernel_mgr.main_client = mock_kernel_client

        # Wire up the session manager's dependencies
        session_manager.serverapp.kernel_manager.get_kernel.return_value = mock_kernel_mgr
        session_manager.yroom_manager.get_room.return_value = mock_yroom
        session_manager.file_id_manager.index.return_value = file_id

        # --- Act: create a session (this registers the stop callback) ---
        mock_session = {"id": session_id, "kernel": {"id": kernel_id}}
        with patch('jupyter_server.services.sessions.sessionmanager.SessionManager.create_session', new_callable=AsyncMock) as mock_parent:
            mock_parent.return_value = mock_session
            await session_manager.create_session(
                path="/path/to/notebook.ipynb",
                name="notebook.ipynb",
                type="notebook",
                kernel_name="python3"
            )

        # Verify a stop callback was registered during create_session
        assert len(captured_callbacks) == 1

        # --- Act: simulate the GC freeing the room (fires stop callbacks) ---
        callback_result = captured_callbacks[0]()

        # The callback returns a coroutine (remove_yroom is async).
        # In production, YRoom.stop() wraps this in asyncio.create_task().
        assert asyncio.iscoroutine(callback_result)
        await callback_result

        # --- Assert: the yroom was disconnected from the kernel client ---
        mock_kernel_client.remove_yroom.assert_called_once_with(mock_yroom)

    @pytest.mark.asyncio
    async def test_stop_callback_does_not_delete_session(self, session_manager):
        """GC'd room must NOT trigger delete_session or shutdown_kernel.

        delete_session() terminates the kernel process, destroying all
        in-memory state. Kernels are only shut down by explicit user action
        or an idle culler — never as a side effect of room GC.
        """
        session_id = "session-123"
        kernel_id = "kernel-456"
        file_id = "test-file-id"

        mock_yroom = Mock()
        mock_yroom.room_id = f"json:notebook:{file_id}"

        captured_callbacks = []
        mock_yroom.add_stop_callback = lambda cb: captured_callbacks.append(cb)

        mock_kernel_client = Mock()
        mock_kernel_client.add_yroom = AsyncMock()
        mock_kernel_client.remove_yroom = AsyncMock()

        mock_kernel_mgr = Mock()
        mock_kernel_mgr.main_client = mock_kernel_client

        session_manager.serverapp.kernel_manager.get_kernel.return_value = mock_kernel_mgr
        session_manager.yroom_manager.get_room.return_value = mock_yroom
        session_manager.file_id_manager.index.return_value = file_id

        mock_session = {"id": session_id, "kernel": {"id": kernel_id}}
        with patch('jupyter_server.services.sessions.sessionmanager.SessionManager.create_session', new_callable=AsyncMock) as mock_parent:
            mock_parent.return_value = mock_session

            # Spy on delete_session — if the stop callback calls it, this
            # mock will record the call.
            with patch.object(session_manager, 'delete_session', new_callable=AsyncMock) as mock_delete:
                await session_manager.create_session(
                    path="/path/to/notebook.ipynb",
                    name="notebook.ipynb",
                    type="notebook",
                    kernel_name="python3"
                )

                # --- Act: simulate the GC freeing the room ---
                callback_result = captured_callbacks[0]()
                if asyncio.iscoroutine(callback_result):
                    await callback_result

                # --- Assert: delete_session was NOT called ---
                # Room GC must never kill kernels.
                mock_delete.assert_not_called()

                # --- Assert: shutdown_kernel was NOT called ---
                session_manager.serverapp.kernel_manager.shutdown_kernel.assert_not_called()

    @pytest.mark.asyncio
    async def test_stop_callback_preserves_room_id_mapping(self, session_manager):
        """GC'd room must leave _room_ids intact for reconnection.

        _room_ids maps session_id -> room_id. When the user reconnects,
        _ensure_yroom_connected() uses this mapping to re-link the new
        room to the kernel. The room_id is deterministic (derived from the
        file's ID), so the mapping becomes valid again when the room is
        recreated.
        """
        session_id = "session-123"
        kernel_id = "kernel-456"
        file_id = "test-file-id"
        room_id = f"json:notebook:{file_id}"

        mock_yroom = Mock()
        mock_yroom.room_id = room_id

        captured_callbacks = []
        mock_yroom.add_stop_callback = lambda cb: captured_callbacks.append(cb)

        mock_kernel_client = Mock()
        mock_kernel_client.add_yroom = AsyncMock()
        mock_kernel_client.remove_yroom = AsyncMock()

        mock_kernel_mgr = Mock()
        mock_kernel_mgr.main_client = mock_kernel_client

        session_manager.serverapp.kernel_manager.get_kernel.return_value = mock_kernel_mgr
        session_manager.yroom_manager.get_room.return_value = mock_yroom
        session_manager.file_id_manager.index.return_value = file_id

        mock_session = {"id": session_id, "kernel": {"id": kernel_id}}
        with patch('jupyter_server.services.sessions.sessionmanager.SessionManager.create_session', new_callable=AsyncMock) as mock_parent:
            mock_parent.return_value = mock_session
            await session_manager.create_session(
                path="/path/to/notebook.ipynb",
                name="notebook.ipynb",
                type="notebook",
                kernel_name="python3"
            )

        # --- Act: simulate the GC freeing the room ---
        callback_result = captured_callbacks[0]()
        if asyncio.iscoroutine(callback_result):
            await callback_result

        # --- Assert: _room_ids still has the mapping ---
        # This entry is what allows _ensure_yroom_connected to reconnect
        # the user to their kernel when they return.
        assert session_id in session_manager._room_ids
        assert session_manager._room_ids[session_id] == room_id


class TestConsoleSessionHandling:
    """Tests for kernel console session support (#225).

    Console sessions have no backing YDoc or YRoom. The session manager
    must skip all YRoom operations for consoles and delegate directly to
    the parent SessionManager.
    """

    @pytest.mark.asyncio
    async def test_create_session_console_skips_yroom_setup(self, session_manager):
        """Console sessions must not set up a YRoom or touch file_id_manager."""
        session_id = "console-session-1"
        kernel_id = "kernel-789"

        mock_session = {"id": session_id, "kernel": {"id": kernel_id}}
        with patch('jupyter_server.services.sessions.sessionmanager.SessionManager.create_session', new_callable=AsyncMock) as mock_parent:
            mock_parent.return_value = mock_session
            result = await session_manager.create_session(
                path="console-1-abc123",
                name="console-1-abc123",
                type="console",
                kernel_name="python3"
            )

        # Session model returned normally
        assert result == mock_session

        # Console tracked in _console_session_ids
        assert session_id in session_manager._console_session_ids

        # No YRoom setup attempted
        session_manager.file_id_manager.index.assert_not_called()
        session_manager.yroom_manager.get_room.assert_not_called()

        # No room_id stored
        assert session_id not in session_manager._room_ids

    @pytest.mark.asyncio
    async def test_update_session_console_delegates_to_parent(self, session_manager):
        """update_session on a console must not attempt YRoom operations."""
        session_id = "console-session-1"
        session_manager._console_session_ids.add(session_id)

        with patch('jupyter_server.services.sessions.sessionmanager.SessionManager.update_session', new_callable=AsyncMock) as mock_parent:
            await session_manager.update_session(session_id, kernel_id="new-kernel")

        mock_parent.assert_called_once_with(session_id, kernel_id="new-kernel")
        # No kernel client lookup attempted
        session_manager.serverapp.kernel_manager.get_kernel.assert_not_called()

    @pytest.mark.asyncio
    async def test_delete_session_console_delegates_to_parent(self, session_manager):
        """delete_session on a console must not attempt YRoom operations."""
        session_id = "console-session-1"
        session_manager._console_session_ids.add(session_id)

        with patch('jupyter_server.services.sessions.sessionmanager.SessionManager.delete_session', new_callable=AsyncMock) as mock_parent:
            await session_manager.delete_session(session_id)

        mock_parent.assert_called_once_with(session_id)
        # Console ID cleaned up
        assert session_id not in session_manager._console_session_ids

    def test_is_console_session(self, session_manager):
        """_is_console_session returns True only for tracked console sessions."""
        assert not session_manager._is_console_session("some-session")
        session_manager._console_session_ids.add("console-1")
        assert session_manager._is_console_session("console-1")
        assert not session_manager._is_console_session("notebook-1")


class TestRoomReconnection:
    """A returning notebook client must recover its existing kernel binding."""

    async def _linked_session(self, manager):
        """Create a synthetic notebook session with observable kernel bindings."""
        model = {
            "id": "session-123", "type": "notebook", "path": "test.ipynb",
            "name": "test.ipynb", "kernel": {"id": "kernel-456"},
        }
        connected = set()
        client = Mock()

        async def add(yroom):
            connected.add(yroom)

        async def remove(yroom):
            connected.discard(yroom)

        client.add_yroom = AsyncMock(side_effect=add)
        client.remove_yroom = AsyncMock(side_effect=remove)
        manager.kernel_manager.get_kernel.return_value = SimpleNamespace(main_client=client)
        manager.file_id_manager.index.return_value = "file-789"
        room = self._room()
        manager.yroom_manager.get_room.return_value = room
        with patch('jupyter_server.services.sessions.sessionmanager.SessionManager.create_session', AsyncMock(return_value=model)):
            await manager.create_session(path="test.ipynb", name="test.ipynb", type="notebook")
        return model, room, client, connected

    def _room(self):
        """Return a room stub that retains its registered cleanup callbacks."""
        room = Mock()
        room.room_id = "json:notebook:file-789"
        room.callbacks = []
        room.add_stop_callback.side_effect = room.callbacks.append
        return room

    async def _open(self, manager, room):
        """Exercise the actual room WebSocket open path without a network port."""
        manager.yroom_manager.get_room.return_value = room
        socket = SimpleNamespace(
            room_id=room.room_id, yroom_manager=manager.yroom_manager,
            settings={"session_manager": manager}, close=Mock(), log=Mock(),
        )
        result = YRoomWebsocket.open(socket)
        if inspect.isawaitable(result):
            await result
        return socket

    @pytest.mark.asyncio
    async def test_reopen_after_gc_reattaches_existing_kernel(self, session_manager):
        """Room GC followed by a new WebSocket must preserve output routing."""
        model, old, client, connected = await self._linked_session(session_manager)
        await old.callbacks[0]()
        assert not connected
        replacement = self._room()
        with patch('jupyter_server.services.sessions.sessionmanager.SessionManager.get_session', AsyncMock(return_value=model)):
            await self._open(session_manager, replacement)
        assert connected == {replacement}
        assert session_manager._room_ids[model["id"]] == replacement.room_id
        session_manager.kernel_manager.shutdown_kernel.assert_not_called()
        session_manager.kernel_manager.start_kernel.assert_not_called()

    @pytest.mark.asyncio
    async def test_repeated_reconnects_register_cleanup_once(self, session_manager):
        """Multiple clients must not accumulate duplicate room-stop callbacks."""
        model, room, client, connected = await self._linked_session(session_manager)
        with patch('jupyter_server.services.sessions.sessionmanager.SessionManager.get_session', AsyncMock(return_value=model)):
            await asyncio.gather(*(self._open(session_manager, room) for _ in range(5)))
        assert connected == {room}
        assert len(room.callbacks) == 1
        await room.callbacks[0]()
        assert not connected

    @pytest.mark.asyncio
    async def test_repeated_gc_cycles_keep_routing_and_cleanup(self, session_manager):
        """Each replacement room must be attached and later detached exactly once."""
        model, room, client, connected = await self._linked_session(session_manager)
        with patch('jupyter_server.services.sessions.sessionmanager.SessionManager.get_session', AsyncMock(return_value=model)):
            for _ in range(3):
                await room.callbacks[0]()
                assert not connected
                room = self._room()
                await self._open(session_manager, room)
                assert connected == {room}
                assert len(room.callbacks) == 1

    @pytest.mark.asyncio
    async def test_reconnect_does_not_attach_another_notebook(self, session_manager):
        """A room must not be bound to sessions belonging to another document."""
        model, room, client, connected = await self._linked_session(session_manager)
        other = self._room()
        other.room_id = "json:notebook:other-file"
        lookup = AsyncMock(return_value=model)
        with patch('jupyter_server.services.sessions.sessionmanager.SessionManager.get_session', lookup):
            await self._open(session_manager, other)
        lookup.assert_not_awaited()
        assert connected == {room}

    @pytest.mark.asyncio
    async def test_expired_session_does_not_start_a_kernel(self, session_manager):
        """A closed session is skipped instead of recreated during room reconnect."""
        model, room, client, connected = await self._linked_session(session_manager)
        await room.callbacks[0]()
        replacement = self._room()
        with patch('jupyter_server.services.sessions.sessionmanager.SessionManager.get_session', AsyncMock(side_effect=HTTPError(404))):
            await self._open(session_manager, replacement)
        assert not connected
        session_manager.kernel_manager.start_kernel.assert_not_called()

    @pytest.mark.asyncio
    async def test_reconnect_failure_closes_and_unregisters_client(self, session_manager):
        """Unexpected binding errors must not leave a falsely connected client."""
        model, room, client, connected = await self._linked_session(session_manager)
        room.clients.add.return_value = "new-client"
        with patch('jupyter_server.services.sessions.sessionmanager.SessionManager.get_session', AsyncMock(side_effect=HTTPError(500))):
            with pytest.raises(HTTPError):
                await self._open(session_manager, room)
        room.clients.remove.assert_called_once_with("new-client")

    @pytest.mark.asyncio
    async def test_same_kernel_refresh_registers_replacement_cleanup(self, session_manager):
        """The manual recovery path must also install cleanup on the new room."""
        model, old, client, connected = await self._linked_session(session_manager)
        await old.callbacks[0]()
        replacement = self._room()
        session_manager.yroom_manager.get_room.return_value = replacement
        with patch('jupyter_server.services.sessions.sessionmanager.SessionManager.get_session', AsyncMock(return_value=model)), \
             patch('jupyter_server.services.sessions.sessionmanager.SessionManager.update_session', AsyncMock()):
            await session_manager.update_session(model["id"], kernel_id=model["kernel"]["id"])
        assert connected == {replacement}
        assert len(replacement.callbacks) == 1
        await replacement.callbacks[0]()
        assert not connected
