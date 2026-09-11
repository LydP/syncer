# GUI rule management & first-run

Parent: [Map: Local file syncer](../map.md)
Type: grilling
Status: resolved
Blocked by: 02, 04

## Question

How does the user create, edit, and delete sync rules through the GUI, and what happens on first run?

Decide: the main-window layout (list of rules, per-rule check status, entry point to a check/review); the flow for adding a rule (pick master file/folder, add replica paths, name it); the flow for adding a replica to an existing rule; editing and deleting rules and what that does to the state file; first-run with no config (empty state, prompt to create the first rule); how the "syncing Matt Pocock skills" and "resume master" cases each look concretely in this UI; and whether the config file is meant to be hand-editable alongside the GUI or GUI-only.

Also decide: a **single-instance lock** (added by [Sync-rule config model](02-sync-rule-config-model.md)) — a `syncer.lock` file in `base_dir` with stale-lock detection by PID, so two copies of the portable tool can't run against the same `config.toml` / `state.json`. What the user sees when a second instance is launched; how a stale lock (crashed process) is recovered; and what deleting state entries for a removed rule/replica does to `state.json`.

Depends on the config model ([Sync-rule config model](02-sync-rule-config-model.md)) and the review UI it launches into ([Review-and-sync tree UI](04-review-and-sync-tree-ui.md)). Consult `CONTEXT.md`.

## Answer

### Main window

- **Two entry surfaces for every rule action** (add, edit, delete, check): a persistent toolbar/button row (`+ Add rule`, `Edit`, `Delete`, `Check`) above or below the rule list, *and* the same actions on a right-click context menu on a rule row. Redundant on purpose — a first-time user finds the buttons, a repeat user right-clicks.
- Right pane stays the [review-and-sync tree](04-review-and-sync-tree-ui.md) for the selected rule; nothing about that changes here.

### First run

No `config.toml` → the main window opens as normal with an empty rule list and an inline empty-state prompt in the right pane ("No sync rules yet — click **+ Add rule** to get started"). No separate onboarding wizard; first-run uses the exact same add-rule flow as every later addition.

### Add rule

Single modal, not a multi-step wizard — a rule only ever has one master and a handful of replicas, too little ceremony to justify paging.

- **Master**: two explicit buttons, **"Choose file…"** / **"Choose folder…"** — never one ambiguous "Browse…". This sets `master_type` unambiguously from which button was used, not by inspecting the picked path.
- **Name**: pre-filled from the master's basename (folder or file name, extension stripped for a file), freely editable. Not enforced unique (per [02](02-sync-rule-config-model.md)).
- **Replicas**: a list with add/remove controls, fed three ways: a "Browse…" button (one at a time); drag-and-drop of one or more folders/files onto the list at once (the common case — fanning one skill folder out to many project dirs); and a text field accepting a typed/pasted path for a replica that doesn't exist yet (first-time provisioning is normal — [03](03-divergence-state-model.md)). The typed path is validated only for "parent directory exists and is writable," never that the path itself already exists.
- **Validation**: uniqueness conflicts (replica or master path already used by another rule, per [02](02-sync-rule-config-model.md)) surface as an inline error on the offending row, not a blocking modal — the rest of the dialog stays usable.

### Add a replica to an existing rule

No separate lightweight action. **Reuse the same rule editor** (opened via `Edit`), replica list pre-populated — a second parallel "add replica" surface would duplicate the validation and drag-and-drop logic for no gain. Editing a replica's path here is still remove-old + add-new per [02](02-sync-rule-config-model.md), dropping that replica's last-sync snapshot.

### Editing a rule

- **Master path is immutable after creation.** A rule's master is its identity in practice ("the cursor-rules skill folder"); letting it change in place would silently reassign the rule's whole sync history to a different master. To point a rule at a different master, delete the rule and create a new one — rare enough not to need a shortcut.
- **Name** is freely editable (join key is the rule's `id`, per [02](02-sync-rule-config-model.md), so renaming is inert elsewhere).
- **Removing a single replica** from the editor (not deleting the whole rule) purges just that replica's `state.json` entry immediately — same "no orphans left lying around" principle as rule deletion below, applied at replica granularity.

### Deleting a rule

- Confirm dialog names the rule and states plainly that replica **files are left untouched** — deleting a rule only stops Syncer tracking it: *"Delete '\<rule name\>'? This stops syncing it — replica files are left as-is. This can't be undone."*
- Purges that rule's `state.json` entries **immediately**, as part of the same delete action — no lazy/deferred cleanup.

### `state.json` reconciliation

Not limited to GUI-driven deletes. **Every config load — startup and the manual "Reload config" action below — reconciles `state.json` against the freshly-loaded config**: any entry (by rule id + normalised replica path) no longer present in `config.toml` is purged, whether it disappeared via a GUI delete, a hand-edit of `config.toml`, or an external tool. One code path regardless of how the rule/replica went away.

### `config.toml`: GUI-only intent, tolerant of hand edits

The GUI is the intended workflow, but the file is plain TOML and not locked against outside edits. `config.toml` is reloaded on startup; a manual **"Reload config"** action re-reads it on demand (no live filesystem watch). If the GUI's own save would overwrite changes made externally since it last loaded the file, it warns before clobbering them.

### Single-instance lock

- `syncer.lock` (PID-based, `base_dir`, per [02](02-sync-rule-config-model.md)) blocks a second launch: a short dialog — *"Syncer is already running — check the existing window."* — then the second instance exits. No attempt to bring the first instance's window to the foreground (unreliable for a portable exe with no installer-level OS integration).
- **Stale-lock recovery is silent.** If the PID in `syncer.lock` isn't a running process (prior crash), the new instance deletes the stale lock and proceeds normally — no confirmation prompt. PID-liveness is a reliable enough signal that prompting would only add friction to the ordinary "closed my laptop mid-sync" case.

### Concrete walkthroughs (sanity-checked against the above, no changes needed)

- **Matt Pocock skill folders**: `+ Add rule` → "Choose folder…" → master = `~/.claude/skills/cursor-rules`, name pre-fills "cursor-rules" → drag 5 project skill-folder paths onto the replica list at once (several don't exist yet — provisioned on first sync) → Add. Later, a 6th project: `Edit` → drop one more folder → Save. Retiring a project: `Edit` → remove that replica → Save, its state entry purged immediately.
- **Resume master**: `+ Add rule` → "Choose file…" → master = `resume.md`, name pre-fills "resume" → paste two not-yet-existing folder paths into the replica text field → Add.

### Domain model

No `CONTEXT.md` change and no ADR — checked via `domain-modeling`. Everything decided here (config reload/hand-edit tolerance, `syncer.lock` PID staleness, reconcile-on-load, master immutability) is GUI mechanics or persistence bookkeeping, not new vocabulary for the master/replica/sync-rule/check/sync model, and none of it clears the bar (hard-to-reverse + surprising-without-context + genuine trade-off) that produced [ADR 0001](../../../docs/adr/0001-portable-self-contained-storage.md).
