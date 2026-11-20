"""Keep cell lookup and output routing correct after shared notebook edits.

These regressions use real CRDT cells and disk output storage, with session/room
lookups mocked. They can also run with the standard-library unittest runner.
"""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock

from ..outputs import OutputProcessor, OutputsManager
from ..ydocs import YNotebook


def _notebook(*cell_ids):
    notebook = YNotebook()
    notebook.set({
        "nbformat": 4,
        "nbformat_minor": 5,
        "metadata": {},
        "cells": [
            {
                "id": cell_id,
                "cell_type": "code",
                "source": "",
                "metadata": {},
                "execution_count": None,
                "outputs": [],
            }
            for cell_id in cell_ids
        ],
    })
    return notebook


class TestCellLookupAfterEdits(TestCase):
    """Resolve stable IDs after edits invalidate their cached positions."""

    def test_deleted_preceding_cell_moves_cached_tail_out_of_bounds(self):
        notebook = _notebook("head", "other", "target")
        self.assertEqual(notebook.find_cell("target")[0], 2)
        del notebook.ycells[0]

        index, cell = notebook.find_cell("target")
        self.assertEqual(index, 1)
        self.assertEqual(cell["id"], "target")

    def test_deleted_preceding_cell_leaves_wrong_cell_at_cached_index(self):
        notebook = _notebook("head", "target", "other")
        self.assertEqual(notebook.find_cell("target")[0], 1)
        del notebook.ycells[0]

        index, cell = notebook.find_cell("target")
        self.assertEqual(index, 0)
        self.assertEqual(cell["id"], "target")

    def test_inserted_preceding_cell_changes_cached_position(self):
        notebook = _notebook("target", "other")
        notebook.find_cell("target")
        notebook.ycells.insert(0, notebook.create_ycell({
            "id": "inserted", "cell_type": "markdown", "source": "new note",
        }))

        index, cell = notebook.find_cell("target")
        self.assertEqual(index, 1)
        self.assertEqual(cell["id"], "target")

    def test_removed_target_is_missing_and_no_longer_cached(self):
        for cell_ids in (("target", "other"), ("other", "target")):
            with self.subTest(cell_ids=cell_ids):
                notebook = _notebook(*cell_ids)
                index, _ = notebook.find_cell("target")
                del notebook.ycells[index]

                self.assertEqual(notebook.find_cell("target"), (None, None))
                self.assertNotIn("target", notebook._cell_indices)
                self.assertEqual(notebook.find_cell("other")[1]["id"], "other")

    def test_cleared_notebook_has_no_cached_destination(self):
        notebook = _notebook("target")
        notebook.find_cell("target")
        notebook.ycells.clear()

        self.assertEqual(notebook.find_cell("target"), (None, None))
        self.assertNotIn("target", notebook._cell_indices)

    def test_whole_document_reload_retains_cell_at_smaller_index(self):
        notebook = _notebook("head", "other", "target")
        notebook.find_cell("target")
        reloaded = notebook.get()
        reloaded["cells"] = [reloaded["cells"][-1]]
        notebook.set(reloaded)

        index, cell = notebook.find_cell("target")
        self.assertEqual(index, 0)
        self.assertEqual(cell["id"], "target")

    def test_deleted_cell_id_can_be_found_after_reinsertion(self):
        notebook = _notebook("head", "target")
        notebook.find_cell("target")
        target = notebook.get_cell(1)
        del notebook.ycells[1]
        self.assertEqual(notebook.find_cell("target"), (None, None))
        notebook.ycells.insert(0, notebook.create_ycell(target))

        index, cell = notebook.find_cell("target")
        self.assertEqual(index, 0)
        self.assertEqual(cell["id"], "target")


class _NotebookOutputProcessor(OutputProcessor):
    @property
    def outputs_manager(self):
        return self._test_outputs_manager


class TestOutputRoutingAfterEdits(IsolatedAsyncioTestCase):
    """Exercise the actual clear/write coroutines against shifted CRDT cells."""

    async def _exercise(self, operation, use_outputs_service):
        notebook = _notebook("head", "other", "target")
        with TemporaryDirectory() as temporary:
            manager = OutputsManager(outputs_path=Path(temporary) / "outputs")
            processor = _NotebookOutputProcessor()
            processor._test_outputs_manager = manager
            processor.use_outputs_service = use_outputs_service
            processor._get_file_info = AsyncMock(return_value=("notebook", "test.ipynb"))
            processor.get_jupyter_ydoc = AsyncMock(return_value=notebook)
            await processor.output_task("stream", "other", {
                "name": "stdout", "text": "unrelated result\n",
            })
            _, other = notebook.find_cell("other")
            other_outputs = other["outputs"].to_py()
            await processor.output_task("display_data", "target", {
                "data": {"text/plain": "old plot"}, "metadata": {},
            })
            self.assertEqual(notebook.find_cell("target")[0], 2)
            del notebook.ycells[0]

            if operation == "clear":
                await processor.clear_cell_outputs("target")
                self.assertEqual(notebook.find_cell("target")[1]["outputs"].to_py(), [])
                self.assertEqual(manager.get_outputs("notebook", "target"), [])
            elif operation == "deferred_clear":
                await processor.clear_output_task("target", {"wait": True})

            content = {
                "data": {"text/plain": "new plot", "application/json": {"points": [1, 2]}},
                "metadata": {},
            }
            await processor.output_task("display_data", "target", content)
            index, target = notebook.find_cell("target")
            self.assertEqual(index, 1)
            expected_count = 2 if operation == "append" else 1
            self.assertEqual(len(target["outputs"]), expected_count)
            if use_outputs_service:
                stored = [json.loads(value) for value in manager.get_outputs("notebook", "target")]
                # Kernel clear_output only clears YDoc references; an execution
                # request's clear_cell_outputs also clears the backing files.
                expected_stored = 1 if operation == "clear" else 2
                self.assertEqual(len(stored), expected_stored)
                self.assertEqual(stored[-1]["data"], content["data"])
                self.assertEqual(
                    target["outputs"][-1]["metadata"]["url"],
                    f"/api/outputs/notebook/target/{expected_stored - 1}.output",
                )
            else:
                self.assertEqual(target["outputs"][-1].to_py()["data"], content["data"])
            self.assertEqual(notebook.find_cell("other")[1]["outputs"].to_py(), other_outputs)
            self.assertNotIn("target", processor._pending_clear_output_cells)

    async def test_clear_cell_outputs_after_preceding_deletion(self):
        for use_outputs_service in (True, False):
            with self.subTest(use_outputs_service=use_outputs_service):
                await self._exercise("clear", use_outputs_service)

    async def test_output_task_after_preceding_deletion(self):
        for use_outputs_service in (True, False):
            with self.subTest(use_outputs_service=use_outputs_service):
                await self._exercise("append", use_outputs_service)

    async def test_deferred_clear_then_output_after_preceding_deletion(self):
        for use_outputs_service in (True, False):
            with self.subTest(use_outputs_service=use_outputs_service):
                await self._exercise("deferred_clear", use_outputs_service)
