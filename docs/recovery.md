# Notebook recovery and external edits

The MkChad maintenance build `0.2.5+mkchad.1` restores execution routing after
notebook room cleanup, records timing on the server, and reconciles external
notebook source edits. It targets ordinary JupyterLab use and OpenCode file-tool
edits; Jupyter AI-specific behavior is not required.

## Disconnect, finish offline, and reconnect

1. A browser executes a cell with its stable cell ID.
2. The browser may disappear. The server keeps collecting kernel output into
   the shared notebook and output service.
3. Normal automatic saving persists output and enabled timing to the `.ipynb`.
4. Once idle, the room may be collected while its session and kernel survive.
5. A returning document WebSocket binds that session's existing kernel client
   to the replacement room, which reconstructs saved notebook state.

Reconnection does not resubmit code or start/restart a kernel. Room IDs are
matched against existing session mappings. Unrelated notebooks are not bound.
Multiple clients do not accumulate duplicate room-stop callbacks. Expired
sessions are skipped; unexpected binding errors unregister the new client and
close its WebSocket with code 1011 rather than silently leaving it detached.

The same-kernel session-update workaround also registers cleanup on replacement
rooms. A kernel change removes routing from the previous kernel client before
attaching the new one.

## Server-owned timing

Enable JupyterLab's **Record Timing** notebook setting. Its execute request
contains `recordTiming`; the server honors that flag and writes these compatible
cell `metadata.execution` fields:

- `iopub.status.busy`
- `iopub.execute_input`
- `shell.execute_reply.started`, when supplied by the kernel
- `shell.execute_reply`
- `iopub.status.idle`

Kernel message timestamps are used, with UTC receipt time as fallback when a
kernel omits its message date. Shell and IOPub updates merge independently of
browser listeners. The first timing event starts a fresh record so a rerun does
not inherit a previous finish time. Superseded replies are ignored. Error and
aborted execute replies also receive completion timestamps.

Normal notebook persistence saves those fields with the outputs. Execute Time
can display the recovered duration without retaining its original browser
execution future. Queued time is not substituted for the kernel start time.

## OpenCode file-tool edits and Contents API edits

External `.ipynb` modifications are detected by the existing ContentsManager
`last_modified` polling path. Preserve existing cell IDs and give new cells
unique IDs. Write a complete valid notebook, preferably by atomic replacement,
so a reader cannot observe half-written JSON.

On a detected change, the server reconciles the file into the current shared
document and broadcasts its CRDT update to connected clients:

- Source/type changes and new cells are incorporated without executing code.
- Existing code-cell IDs keep live output references, execution counts, and
  execution metadata. An older disk snapshot cannot replace newer results.
- Source is compared with the last observed/saved file. Disjoint cell edits
  merge, and unchanged fields in an older snapshot do not undo browser edits.
- Conflicting source/type edits to the same cell, or deletion of an edited
  cell, raise a conflict before changing the live document or output cache.
  Both versions remain available: one in the open document, one in the file.
- Removing/changing the IDs of running or queued cells is rejected, preserving
  the destination for their execution messages. Retain those IDs until done.
- Malformed/incomplete files leave the live document and output cache intact;
  the watcher can load a subsequent complete write.
- One acknowledged save persists the combined source/execution state without
  creating an external-reload/save feedback loop.

The file watcher logs conflicts and retries after the file is corrected. A
conflicting rewrite is not automatically resolved by overwriting either version.
Pre-save and garbage-collection checks also reconcile external edits. A pending
conflict blocks final saving and idle collection, so closing all clients cannot
silently overwrite the file or discard the live room. Resolve conflicts before
stopping the server; recovery of unsaved live edits across server death is not
provided by this maintenance patch.
Execution fields for existing cells remain server-owned during file reloads;
clear their output through the notebook rather than by stripping JSON fields.

This covers tools that rewrite files directly and tools that save whole
notebooks through the Contents API. An MCP tool that edits the live shared model
already uses the normal collaboration path. No particular MCP tool's protocol
has been qualified by these backend tests.

Direct whole-file writers are not transactional participants in the server's
autosave lock. Preserve IDs, read the latest file before editing, and avoid
concurrent cell reordering or same-cell edits through separate writers. For
transactional concurrent editing, use a tool that updates the live document API.
The reconciliation applies to detected external changes; it cannot recover an
external file version overwritten by another writer before detection.

## Recovery limits and deployment

Successful saving is required to reconstruct a collected room. Output or timing
never captured/persisted cannot be reconstructed retrospectively. The notebook
stores current cell results, not a permanent history of all executions.
Server/kernel restart recovery and browser HTTP-fetch retries are separate work.

Installing the maintenance wheel affects newly started servers; it does not
replace classes in an existing process. Schedule deployment around active work.
The backend-only wheel reuses the published 0.2.5 frontend assets and identifies
the Python patch with its local version suffix.

## Verification

Backend regressions cover room cleanup/recreation, concurrent reconnects,
document routing, expired sessions, failure cleanup, timing arrival order,
superseded requests, external source conflicts, and incomplete writes.

The real-kernel acceptance test disconnects a synthetic document client while
code runs, optionally edits/adds cells on disk, waits for output/timing to save
automatically, collects the room, reconstructs it, and executes another cell on
the same kernel. A witness variable verifies that the original computation was
not replayed and kernel variables survived. A CRDT replica test verifies that
the reconciled external edits are available to document clients.
