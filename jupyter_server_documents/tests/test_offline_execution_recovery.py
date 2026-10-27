"""Exercise offline completion and room recreation with a real local kernel."""

import asyncio
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import nbformat
import pycrdt
import pytest

from jupyter_server_documents.websockets.yroom_ws import YRoomWebsocket


async def _wait_for(check, timeout=20):
    """Wait for a synthetic kernel/persistence condition with a fixed deadline."""
    async def poll():
        while True:
            result = await check()
            if result:
                return result
            await asyncio.sleep(0.05)
    return await asyncio.wait_for(poll(), timeout)


def _execute(client, code, cell_id):
    """Send code through the real browser-to-kernel request processing path."""
    request = client.session.msg(
        "execute_request",
        content={
            "code": code, "silent": False, "store_history": True,
            "user_expressions": {}, "allow_stdin": False, "stop_on_error": True,
        },
        metadata={"cellId": cell_id, "recordTiming": True},
    )
    client.handle_incoming_message("shell", client.session.serialize(request)[2:])


@pytest.mark.asyncio
@pytest.mark.parametrize("external_edit", [False, True])
async def test_finish_offline_save_collect_reopen_and_continue(mock_server_docs_app, external_edit):
    """Outputs/timing survive zero clients, GC, and reattach to the same kernel."""
    app = mock_server_docs_app.serverapp
    path = "offline-recovery.ipynb"
    cell_id = "offline-cell"
    code = (
        "import time\n"
        "recovery_witness = globals().get('recovery_witness', 0) + 1\n"
        "time.sleep(0.5)\n"
        "print('finished offline')\n"
        "recovery_witness"
    )
    notebook = nbformat.v4.new_notebook(cells=[
        nbformat.v4.new_code_cell(code, id=cell_id),
        nbformat.v4.new_code_cell("helper = 0", id="helper-cell"),
    ])
    await app.contents_manager.save({"type": "notebook", "content": notebook}, path)
    model = await app.session_manager.create_session(
        path=path, name=path, type="notebook", kernel_name="python3",
    )
    kernel_id = model["kernel"]["id"]
    rooms = app.web_app.settings["yroom_manager"]
    outputs = app.web_app.settings["outputs_manager"]
    room = app.session_manager.get_yroom(model["id"])
    room.inactivity_timeout = 0
    client = app.session_manager.get_kernel_client(kernel_id)
    try:
        shared = await room.get_jupyter_ydoc()
        kernel_manager = app.kernel_manager.get_kernel(kernel_id)

        async def ready():
            return kernel_manager.execution_state == "idle" and client.hb_channel.is_alive()

        await _wait_for(ready)
        socket = Mock()
        browser_id = room.clients.add(socket)
        _execute(client, code, cell_id)

        async def running():
            _, cell = shared.find_cell(cell_id)
            return cell["metadata"].get("execution", {}).get("iopub.status.busy")

        await _wait_for(running)
        room.clients.remove(browser_id)
        assert room.empty
        assert all(getattr(listener, "__self__", None) is kernel_manager for listener in client._listeners)
        assert not rooms._should_free_room(room)

        if external_edit:
            edited = deepcopy(notebook)
            edited["cells"][1]["source"] = "helper = 42"
            edited["cells"].insert(0, nbformat.v4.new_code_cell("agent_added = 42", id="agent-added"))
            target = Path(app.root_dir) / path
            temporary = target.with_suffix(".agent-tmp")
            temporary.write_text(nbformat.writes(edited))
            temporary.replace(target)

        async def saved_completion():
            saved = (await app.contents_manager.get(path))["content"]
            cell = next(cell for cell in saved["cells"] if cell["id"] == cell_id)
            timing = cell["metadata"].get("execution", {})
            edit_present = not external_edit or any(cell["id"] == "agent-added" for cell in saved["cells"])
            if edit_present and timing.get("shell.execute_reply") and timing.get("iopub.status.idle") and len(cell["outputs"]) == 2:
                return saved
            return None

        saved = await _wait_for(saved_completion)
        cell = next(cell for cell in saved["cells"] if cell["id"] == cell_id)
        assert cell["execution_count"] == 1
        assert cell["outputs"][0]["text"] == "finished offline\n"
        assert cell["outputs"][1]["data"]["text/plain"] == "1"
        original_timing = dict(cell["metadata"]["execution"])
        assert original_timing["iopub.status.busy"] <= original_timing["shell.execute_reply"]

        assert rooms._should_free_room(room)
        assert await rooms._free_room(room)
        await asyncio.sleep(0)
        assert room not in client._yrooms
        assert kernel_id in app.kernel_manager

        replacement = rooms.get_room(room.room_id)
        shared = await replacement.get_jupyter_ydoc()
        restored = next(cell for cell in shared.source["cells"] if cell["id"] == cell_id)
        assert restored["metadata"]["execution"] == original_timing
        assert len(restored["outputs"]) == 2
        stored_outputs = [json.loads(value) for value in outputs.get_outputs(replacement.file_api.file_id, cell_id)]
        assert stored_outputs[0]["text"] == "finished offline\n"

        new_socket = SimpleNamespace(
            room_id=replacement.room_id, yroom_manager=rooms,
            settings=app.web_app.settings, close=Mock(), write_message=Mock(),
            ws_connection=True, log=app.log,
        )
        await YRoomWebsocket.open(new_socket)
        assert replacement in client._yrooms
        assert (await app.session_manager.get_session(session_id=model["id"]))["kernel"]["id"] == kernel_id

        next_code = "print('after reconnect', recovery_witness, 'agent_added' in globals())"
        _, shared_cell = shared.find_cell(cell_id)
        shared_cell["source"] = pycrdt.Text(next_code)
        _execute(client, next_code, cell_id)

        async def saved_second_execution():
            saved = (await app.contents_manager.get(path))["content"]
            cell = next(cell for cell in saved["cells"] if cell["id"] == cell_id)
            if cell["execution_count"] == 2 and cell["outputs"] and cell["outputs"][0].get("text") == "after reconnect 1 False\n":
                return cell
            return None

        second = await _wait_for(saved_second_execution)
        assert second["metadata"]["execution"]["iopub.status.busy"] >= original_timing["shell.execute_reply"]
        replacement.clients.remove(new_socket.client_id)
    finally:
        if kernel_id in app.kernel_manager:
            await app.session_manager.delete_session(model["id"])
