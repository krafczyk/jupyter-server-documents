"""Protect shutdown saves from unresolved paths and unacknowledged writes.

Real shared documents and locks are used with mocked file-ID and contents
services. No live notebook, file-ID database, or Jupyter service is accessed.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock

from pycrdt import Text
from traitlets.config import LoggingConfigurable

from ..outputs import OutputsManager
from ..rooms import YRoomFileAPI
from ..ydocs import YNotebook


class TestShutdownSaves(IsolatedAsyncioTestCase):
    """Keep failed saves pending and never infer a destination from stale state."""

    def setUp(self):
        temporary = self.enterContext(TemporaryDirectory())
        self.modified = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.saved_modified = self.modified + timedelta(seconds=1)
        self.owner = LoggingConfigurable()
        self.owner.room_id = "json:notebook:test-file-id"
        self.owner.fileid_manager = Mock()
        self.owner.fileid_manager.get_path.return_value = "notebook.ipynb"
        self.owner.contents_manager = Mock(
            get=AsyncMock(return_value={"last_modified": self.modified}),
            save=AsyncMock(return_value={"last_modified": self.saved_modified}),
        )
        self.owner.outputs_manager = OutputsManager(outputs_path=Path(temporary) / "outputs")
        self.api = YRoomFileAPI(parent=self.owner)
        self.api._last_path = "notebook.ipynb"
        self.api._last_modified = self.modified
        self.api._content_load_event.set()
        self.api.schedule_save()
        self.shared = YNotebook()
        self.shared.source = {
            "nbformat": 4,
            "nbformat_minor": 5,
            "metadata": {},
            "cells": [{
                "id": "retained-cell", "cell_type": "code", "source": "valuable source",
                "metadata": {"execution": {"iopub.status.idle": "2026-01-01T00:00:00Z"}},
                "execution_count": 8,
                "outputs": [{"output_type": "stream", "name": "stdout", "text": "retained result\n"}],
            }],
        }
        self.shared.dirty = True
        self.before = self.shared.source

    def _assert_pending(self):
        self.assertTrue(self.shared.dirty)
        self.assertTrue(self.api._save_scheduled)
        self.assertEqual(self.api._last_modified, self.modified)
        self.assertEqual(self.shared.source, self.before)

    async def test_missing_path_before_preflight_never_reaches_contents_manager(self):
        self.owner.fileid_manager.get_path.return_value = None
        with self.assertRaisesRegex(RuntimeError, "no path"):
            await self.api.save(self.shared)
        self.owner.contents_manager.get.assert_not_awaited()
        self.owner.contents_manager.save.assert_not_awaited()
        self._assert_pending()

    async def test_path_disappears_after_preflight(self):
        self.owner.fileid_manager.get_path.side_effect = ["notebook.ipynb", None]
        with self.assertRaisesRegex(RuntimeError, "no path"):
            await self.api.save(self.shared)
        self.owner.contents_manager.save.assert_not_awaited()
        self._assert_pending()

    async def test_path_disappears_while_waiting_for_write_lock(self):
        preflight_complete = asyncio.Event()

        async def preflight(_shared):
            preflight_complete.set()

        self.api.reconcile_before_save = AsyncMock(side_effect=preflight)
        await self.api._content_lock.acquire()
        task = asyncio.create_task(self.api.save(self.shared))
        try:
            await asyncio.wait_for(preflight_complete.wait(), timeout=2)
            self.owner.fileid_manager.get_path.return_value = None
        finally:
            self.api._content_lock.release()
        with self.assertRaisesRegex(RuntimeError, "no path"):
            await task
        self.owner.contents_manager.save.assert_not_awaited()
        self._assert_pending()

    async def test_path_change_requires_new_preflight_before_retry(self):
        resolver = self.owner.fileid_manager.get_path
        resolver.side_effect = ["notebook.ipynb", "renamed.ipynb"]
        with self.assertRaisesRegex(RuntimeError, "path changed"):
            await self.api.save(self.shared)
        self.owner.contents_manager.save.assert_not_awaited()
        self._assert_pending()

        resolver.side_effect = None
        resolver.return_value = "renamed.ipynb"
        await self.api.save(self.shared)
        self.assertEqual(self.owner.contents_manager.save.await_args.args[1], "renamed.ipynb")
        self.assertEqual(self.api._last_path, "renamed.ipynb")
        self.assertFalse(self.shared.dirty)
        self.assertFalse(self.api._save_scheduled)

    async def test_unloaded_document_without_path_is_not_saved(self):
        self.api._content_load_event.clear()
        self.owner.fileid_manager.get_path.return_value = None
        await self.api.save(self.shared)
        self.owner.contents_manager.get.assert_not_awaited()
        self.owner.contents_manager.save.assert_not_awaited()
        self._assert_pending()

    async def test_unloaded_document_cannot_overwrite_resolved_file(self):
        self.api._content_load_event.clear()
        await self.api.save(self.shared)
        self.owner.contents_manager.get.assert_not_awaited()
        self.owner.contents_manager.save.assert_not_awaited()
        self._assert_pending()

    async def test_readonly_document_does_not_require_a_write_path(self):
        self.api._is_writable = False
        self.owner.fileid_manager.get_path.return_value = None
        await self.api.save(self.shared)
        self.owner.fileid_manager.get_path.assert_not_called()
        self.owner.contents_manager.save.assert_not_awaited()
        self._assert_pending()

    async def test_preflight_can_discover_readonly_content(self):
        async def preflight(_shared):
            self.api._is_writable = False

        self.api.reconcile_before_save = AsyncMock(side_effect=preflight)
        await self.api.save(self.shared)
        self.owner.contents_manager.save.assert_not_awaited()
        self._assert_pending()

    async def test_write_failure_propagates_and_can_be_retried(self):
        self.owner.contents_manager.save.side_effect = OSError("synthetic storage failure")
        with self.assertRaisesRegex(OSError, "synthetic storage failure"):
            await self.api.save(self.shared)
        self._assert_pending()

        self.owner.contents_manager.save.side_effect = None
        await self.api.save(self.shared)
        self.assertFalse(self.shared.dirty)
        self.assertFalse(self.api._save_scheduled)
        self.assertEqual(self.api._last_modified, self.saved_modified)
        self.assertEqual(self.shared.source, self.before)

    async def test_stopped_file_api_can_save_loaded_content(self):
        self.api.stop()
        await self.api.save(self.shared)
        self.owner.contents_manager.save.assert_awaited_once()
        self.assertEqual(self.owner.contents_manager.save.await_args.args[1], "notebook.ipynb")
        self.assertFalse(self.shared.dirty)
        self.assertFalse(self.api._save_scheduled)
        self.assertEqual(self.api._last_modified, self.saved_modified)

    async def test_new_update_during_write_remains_dirty_and_pending(self):
        async def write(_model, _path):
            self.shared.ycells[0]["source"] = Text("newer edit during write")
            self.shared.dirty = True
            self.api.schedule_save()
            return {"last_modified": self.saved_modified}

        self.owner.contents_manager.save.side_effect = write
        await self.api.save(self.shared)
        self.assertTrue(self.api._save_scheduled)
        self.assertTrue(self.shared.dirty)
        self.assertEqual(self.shared.source["cells"][0]["source"], "newer edit during write")
        written = self.owner.contents_manager.save.await_args.args[0]["content"]
        self.assertEqual(written["cells"][0]["source"], "valuable source")

    async def test_stop_before_initial_load_does_not_write_placeholder(self):
        self.api._content_load_event.clear()
        self.api.stop()
        await self.api.save(self.shared)
        self.assertTrue(self.api.stopped)
        self.owner.contents_manager.save.assert_not_awaited()
        self._assert_pending()
