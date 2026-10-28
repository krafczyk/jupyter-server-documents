"""Reconcile external source edits without replacing live execution results."""

import asyncio
from copy import deepcopy
from contextlib import suppress
import os
from pathlib import Path

import nbformat
import pycrdt
import pytest
from tornado.web import HTTPError

from jupyter_server_documents.ydocs import YNotebook


async def _loaded_notebook(app):
    """Create an isolated notebook room and pause its watcher for controlled I/O."""
    path = "external-edits.ipynb"
    notebook = nbformat.v4.new_notebook(cells=[nbformat.v4.new_code_cell(
        "old source", id="stable-cell", execution_count=1,
        outputs=[nbformat.v4.new_output("stream", name="stdout", text="saved output\n")],
        metadata={"execution": {"shell.execute_reply": "2000-01-01T00:00:00Z"}},
    )])
    await app.contents_manager.save({"type": "notebook", "content": notebook}, path)
    file_id = app.web_app.settings["file_id_manager"].index(path)
    room = app.web_app.settings["yroom_manager"].get_room(f"json:notebook:{file_id}")
    shared = await room.get_jupyter_ydoc()
    watcher = room.file_api._watch_file_task
    room.file_api.stop()
    with suppress(asyncio.CancelledError):
        await watcher
    return room, shared, notebook, Path(app.root_dir) / path


def _external_write(path, notebook):
    """Replace a synthetic notebook as a disk-writing agent would."""
    previous = path.stat().st_mtime
    temporary = path.with_suffix(".agent-tmp")
    temporary.write_text(nbformat.writes(notebook))
    os.utime(temporary, (previous + 2, previous + 2))
    temporary.replace(path)


@pytest.mark.asyncio
@pytest.mark.parametrize("writer", ["file", "contents"])
async def test_external_edit_and_add_preserve_live_outputs_and_sync_clients(mock_server_docs_app, writer):
    """A stale disk snapshot may edit source/add a cell without reverting results."""
    app = mock_server_docs_app.serverapp
    room, shared, disk_snapshot, path = await _loaded_notebook(app)
    _, cell = shared.find_cell("stable-cell")
    cell["execution_count"] = 8
    timing = {"iopub.status.busy": "2026-10-27T10:00:00Z"}
    cell["metadata"]["execution"] = timing
    output = room.outputs_manager.write(
        room.file_api.file_id, "stable-cell",
        {"output_type": "stream", "name": "stdout", "text": "new live output\n"},
    )
    cell["outputs"].append(output)

    replica = pycrdt.Doc()
    replica.apply_update(room._ydoc.get_update())
    replica_state = replica.get_state()
    before_outputs = room.outputs_manager.get_outputs(room.file_api.file_id, "stable-cell")
    edited = deepcopy(disk_snapshot)
    edited["cells"][0]["source"] = "source edited by an agent"
    edited["cells"].insert(0, nbformat.v4.new_markdown_cell("agent explanation", id="new-markdown"))
    edited["cells"].append(nbformat.v4.new_code_cell("new cell", id="new-code"))
    if writer == "file":
        _external_write(path, edited)
    else:
        await app.contents_manager.save({"type": "notebook", "content": edited}, path.name)
    await room.file_api._check_file(shared)

    updated = shared.source
    assert [item["id"] for item in updated["cells"]] == ["new-markdown", "stable-cell", "new-code"]
    restored = updated["cells"][1]
    assert restored["source"] == "source edited by an agent"
    assert restored["execution_count"] == 8
    assert restored["metadata"]["execution"] == timing
    assert len(restored["outputs"]) == 2
    assert room.outputs_manager.get_outputs(room.file_api.file_id, "stable-cell") == before_outputs

    replica.apply_update(room._ydoc.get_update(replica_state))
    client_notebook = YNotebook(replica, pycrdt.Awareness(replica))
    assert client_notebook.source["cells"][1]["source"] == restored["source"]
    assert client_notebook.source["cells"][1]["metadata"]["execution"] == timing
    assert room.file_api._save_scheduled

    await room.file_api.save(shared)
    saved = nbformat.read(path, as_version=4)
    assert saved["cells"][1]["source"] == restored["source"]
    assert saved["cells"][1]["execution_count"] == 8
    assert saved["cells"][1]["outputs"][1]["text"] == "new live output\n"
    await room.file_api._check_file(shared)
    assert not room.file_api._save_scheduled


@pytest.mark.asyncio
async def test_partial_external_file_does_not_clear_live_output(mock_server_docs_app):
    """An interrupted writer must not replace the shared document/output cache."""
    room, shared, original, path = await _loaded_notebook(mock_server_docs_app.serverapp)
    before = shared.source
    outputs = room.outputs_manager.get_outputs(room.file_api.file_id, "stable-cell")
    path.write_text('{"cells":')
    with pytest.raises(HTTPError):
        await room.file_api._check_file(shared)
    assert shared.source == before
    assert room.outputs_manager.get_outputs(room.file_api.file_id, "stable-cell") == outputs
    original["cells"][0]["source"] = "completed external write"
    _external_write(path, original)
    await room.file_api._check_file(shared)
    assert shared.source["cells"][0]["source"] == "completed external write"


@pytest.mark.asyncio
async def test_external_edit_cannot_remove_running_cell(mock_server_docs_app):
    """The destination of an active execution must survive an external rewrite."""
    room, shared, original, path = await _loaded_notebook(mock_server_docs_app.serverapp)
    room.set_cell_execution_state("stable-cell", "busy")
    before = shared.source
    original["cells"] = [nbformat.v4.new_code_cell("replacement", id="different-cell")]
    _external_write(path, original)
    with pytest.raises(ValueError, match="running"):
        await room.file_api._check_file(shared)
    assert shared.source == before
    original["cells"] = [nbformat.v4.new_code_cell("old source", id="stable-cell")]
    _external_write(path, original)
    await room.file_api._check_file(shared)


@pytest.mark.asyncio
async def test_external_add_preserves_unrelated_browser_source_edits(mock_server_docs_app):
    """An agent's stale snapshot must not revert an unchanged cell's live source."""
    room, shared, original, path = await _loaded_notebook(mock_server_docs_app.serverapp)
    _, cell = shared.find_cell("stable-cell")
    cell["source"] = pycrdt.Text("edited in browser")
    original["cells"].append(nbformat.v4.new_code_cell("agent addition", id="agent-new"))
    _external_write(path, original)
    await room.file_api._check_file(shared)
    assert shared.source["cells"][0]["source"] == "edited in browser"
    assert shared.source["cells"][1]["source"] == "agent addition"


@pytest.mark.asyncio
async def test_conflicting_external_source_edits_preserve_both_versions(mock_server_docs_app):
    """Conflicting same-cell edits must not silently overwrite either writer."""
    room, shared, original, path = await _loaded_notebook(mock_server_docs_app.serverapp)
    _, cell = shared.find_cell("stable-cell")
    cell["source"] = pycrdt.Text("browser version")
    original["cells"][0]["source"] = "agent version"
    _external_write(path, original)
    with pytest.raises(ValueError, match="[Cc]onflict"):
        await room.file_api._check_file(shared)
    assert shared.source["cells"][0]["source"] == "browser version"
    assert nbformat.read(path, as_version=4)["cells"][0]["source"] == "agent version"
    room.inactivity_timeout = 0
    manager = mock_server_docs_app.serverapp.web_app.settings["yroom_manager"]
    assert not await manager._free_room(room)
    assert not room.stopped
    assert not manager._should_free_room(room)
    with pytest.raises(ValueError, match="[Cc]onflict"):
        await room.file_api.save(shared)
    assert nbformat.read(path, as_version=4)["cells"][0]["source"] == "agent version"
    original["cells"][0]["source"] = "browser version"
    _external_write(path, original)
    await room.file_api._check_file(shared)
    await room.file_api.save(shared)
    assert not room.file_api.external_change_pending


@pytest.mark.asyncio
@pytest.mark.parametrize("live_trust", [True, False, None])
async def test_external_source_edit_preserves_only_existing_output_trust(mock_server_docs_app, live_trust):
    """Fresh clients need retained output trust, never trust from a rewritten file."""
    room, shared, original, path = await _loaded_notebook(mock_server_docs_app.serverapp)
    _, cell = shared.find_cell("stable-cell")
    if live_trust is None:
        if "trusted" in cell["metadata"]:
            del cell["metadata"]["trusted"]
    else:
        cell["metadata"]["trusted"] = live_trust
    original["cells"][0]["source"] = "edited source"
    original["cells"][0]["metadata"]["trusted"] = True
    original["cells"].append(nbformat.v4.new_code_cell(
        "new external cell", id="external-rich-cell", metadata={"trusted": True},
        outputs=[nbformat.v4.new_output("display_data", data={"text/html": "<b>external</b>"})],
    ))
    _external_write(path, original)
    await room.file_api._check_file(shared)
    _, retained = shared.find_cell("stable-cell")
    _, added = shared.find_cell("external-rich-cell")
    assert retained["metadata"].get("trusted") is (live_trust is True)
    assert added["metadata"].get("trusted") is False
