"""Extend shared notebooks with stable-ID cell lookup and metadata utilities.

Cell positions are cached locally and revalidated against the current CRDT
array, so edits and reloads can move or remove cells without breaking lookup.
"""
import sys
# See compatibility note on `group` keyword in
# https://docs.python.org/3/library/importlib.metadata.html#entry-points
if sys.version_info < (3, 10):
    from importlib_metadata import entry_points
else:
    from importlib.metadata import entry_points
    
from jupyter_ydoc.ynotebook import YNotebook as UpstreamYNotebook


class YNotebook(UpstreamYNotebook):
    __doc__ = """
    Extends upstream YNotebook with stable-ID lookup and metadata utilities.
    Cached cell positions are repaired after shared-document edits and reloads.
    
    Upstream docstring:
    """ + UpstreamYNotebook.__doc__
    
    _cell_indices: dict # a map from cell_id -> cell index in notebook

    def __init__(self, *args, **kwargs): 
        super().__init__(*args, **kwargs)
        self._cell_indices = {}
    
    @property
    def ymeta(self):
        """
        Returns the Y-meta.

        :return: The Y-meta.
        :rtype: :class:`pycrdt.Map`
        """
        return self._ymeta

    def find_cell(self, cell_id):
        """Find a cell by stable ID, repairing stale cached positions as needed.

        Args:
            cell_id (str): ID of the cell to locate in the current document.

        Returns:
            tuple: The current integer index and ``pycrdt.Map`` cell, or
            ``(None, None)`` if the cell was removed or does not exist.

        Updates the local index cache without changing the shared document.
        An out-of-range or mismatched cached index triggers a fresh scan rather
        than raising an indexing error or returning another cell.
        """
        # Find the target_cell and its cell_index and cache
        target_cell = None
        cell_index = None
        try:
            # See if we have a cached value for the cell_index
            cell_index = self._cell_indices[cell_id]
            target_cell = self.ycells[cell_index]
        except (KeyError, IndexError):
            # Do a linear scan to find the cell
            cell_index, target_cell = self.scan_cells(cell_id)
        else:
            # Verify that the cached value still matches
            if target_cell["id"] != cell_id:
                cell_index, target_cell = self.scan_cells(cell_id)
        return cell_index, target_cell

    def scan_cells(self, cell_id):
        """Scan the current cells in reverse order and refresh a cached index.

        Args:
            cell_id (str): ID of the cell to locate in the current document.

        Returns:
            tuple: The current integer index and ``pycrdt.Map`` cell, or
            ``(None, None)`` if the cell does not exist. A missing cell's stale
            cache entry is removed; the shared document is not modified.

        Reverse order favors the cells near the end that users often execute.
        """
        self._cell_indices.pop(cell_id, None)
        target_cell = None
        cell_index = None
        for i in reversed(range(0, len(self.ycells))):
            cell = self.ycells[i]
            if cell["id"] == cell_id:
                target_cell = cell
                cell_index = i
                self._cell_indices[cell_id] = cell_index
                break
        return cell_index, target_cell

    def get_cell_list(self):
        """Get a list of all cells in the notebook.
        
        Returns a list of pycrdt.Map objects representing the cells.
        This method is used by the integration tests.
        
        :return: List of cells
        :rtype: List[pycrdt.Map]
        """
        return [self.ycells[i] for i in range(len(self.ycells))]

    def get_meta(self):
        """Get the notebook metadata.
        
        Returns the full metadata structure including nbformat info and custom metadata.
        This method is used by the integration tests.
        
        :return: The notebook metadata
        :rtype: Dict
        """
        return self._ymeta.to_py()



ydocs = {ep.name: ep.load() for ep in entry_points(group="jupyter_ydoc")}

# Replace the YNotebook with our local version
ydocs["notebook"] = YNotebook
