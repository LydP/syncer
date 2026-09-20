# A Session module owns the check/sync workflow; the Qt layer is an adapter

## Status

accepted

## Decision

The live working set for one run of the app — a **Session** (see `CONTEXT.md`) — lives in a
Qt-free `src/syncer/session.py`, not in a `QWidget`. It owns the in-memory `State` and its
`state_path`/`logs_dir`, every rule's cached review tree, the per-rule unlock and tick selection,
the namespace-collision set, the check queue and its stale-result gate. `gui/` keeps widgets,
which rule row is current, the `QThread` handles, and every `QMessageBox` and its wording.

Four parts follow from that:

- **Checks are pulled, not pushed.** The Session never starts a thread. The Qt layer asks
  `session.next_check()` for a job, runs `check()` on a `CheckWorker` exactly as before, and hands
  the result back via `session.check_finished(job, result, cancelled)`. Every Session mutation
  therefore still happens on the GUI thread.
- **The interface is coarse, and its queries are bound.** `session.sync_selected(rule_id)` derives
  the changes from the Session's own selection rather than taking them; `session.tree(rule_id)`
  returns a tree with blocking already resolved rather than a raw `ReviewRule` the caller must
  remember to pass through `visible_replica`. Callers do not re-derive what the Session knows.
- **`ConfigStore` is composed, not absorbed.** It keeps its own concern — the warn-before-clobber
  mtime guard — and the Session calls it.
- **`app.py` keeps the bootstrap.** The Session is constructed from finished values
  (`Session(layout, config_store, config, state)`); it does not load config or state itself.

## Why

`gui/review_pane.py` had become the de facto orchestrator while its own docstring claimed to be
thin wiring: it held the single authoritative in-memory `State`, and `main_window.py` read that
state back *out of the widget* (`self.review_pane.state`) in order to reconcile it. Because
`gui/` is excluded from the pytest loop by convention and PySide6 is an optional extra, every
decision that had drifted there was also a behaviour with no test — including the stale-result
gate, which is what stops a check made against an edited rule being synced into a replica the
config no longer has. Moving the workflow behind one Qt-free interface is what makes those
behaviours reachable from `pytest` at all.

Pulling rather than pushing is what preserves the existing threading model. Today only `check()`
runs off the GUI thread; the review cache, unlock set and queue are read and written solely from
GUI-thread slots. A Session that started its own worker would mutate `_review` and `_unlocked`
off-thread while `check_finished`'s gate read them on it — a race the current design does not
have. A future reader should not "finish the job" by giving the Session its own threading.

`app.py` keeps the bootstrap because ADR 0004 places an app update's success point between the
config load and the state load (`report_launched`, after `config_store.load()` and before
`load_state()`): everything before it only reads user data, everything after may write it, and the
old build can only be rolled back to before that line. A self-bootstrapping
`Session.open(layout)` would bury that ordering inside a constructor and leave `report_launched`
nowhere correct to sit.

## Considered Options

- **A `StateStore` only**, mirroring `ConfigStore`, leaving reviews, unlocks and the queue in the
  pane. Rejected — the stale-result gate lives *inside* the queue drain, so the highest-value
  untested behaviour would have stayed in Qt.
- **Absorbing `ConfigStore` and the whole config lifecycle too.** Rejected — `ConfigStore` already
  works and is unit-tested, and widening the Session to cover it fixes no untested behaviour.
- **An immutable Session value** threaded through transitions, matching `State`/`Config`/
  `ReviewRule`. Rejected — immutability here is a convention for *data*; this module's job is
  sequencing I/O (saving `state.json`, copying replica files, draining a queue), and `ConfigStore`
  is the repo's existing precedent for a stateful object that does exactly that.
- **An injected runner** (`Session(..., runner=...)`) with a Qt runner in the app and a synchronous
  one in tests. Rejected — one production adapter makes that a hypothetical seam, not a real one,
  and it inverts control back into Qt for no gain over pulling.
- **Having `ConflictDialog` return a batch of resolutions** for the caller to apply, instead of
  handing it the Session. Rejected — the dialog applies each resolution as the user steps through
  it, so closing halfway keeps what was already resolved; batching to the end would change that.

## Consequences

- Supersedes `MainWindow`'s docstring claim that "from then on `review_pane` owns the in-memory
  state", and makes the "no decisions live in the GUI layer" invariant in `CLAUDE.md` and the
  `gui/` module docstrings true again rather than aspirational.
- `gui/conflict_dialog.py`'s `bulk_overwrite` — a function that copies and deletes files while
  taking a Qt `parent` — loses its apply half to the Session; the confirm half stays Qt. The
  upward import (`review_pane` importing from `conflict_dialog`) goes away with it.
- `review_pane.conflictsRequested`, declared but never emitted or connected, is deleted rather
  than wired: the pane opens the dialog directly.
- "Is a check running" stops being stored in `btn_check.isEnabled()` and becomes
  `session.has_pending_checks() or self._worker is not None`.
- The refactor is **behaviour-preserving by reading and by running the app, not by tests**. A
  characterization suite could not be written first: the behaviours being moved are reachable only
  through Qt, which the test suite does not install. Tests land with each slice instead.
- Sync still runs synchronously on the GUI thread, uncancellable and without progress, even though
  `sync()` accepts `progress=`/`cancel=`. That is unchanged here deliberately — it is what makes
  "a sync can't overlap a click" true — and is left to its own issue.
- `review.py`'s functions keep their `unlocked` parameter; the Session binds it at one call site
  each. The separate proposal to fold `unlocked` into `ReviewRule` loses most of its value once
  there is a single caller, and should be re-judged rather than assumed. **Update (issue #49):**
  re-judged and reversed. The binding does not collapse, it relocates: roughly seven call sites
  inside the Session bind it instead of seven in the pane, behind a tested seam rather than none.
  What decided it is that folding makes two of the three unlock transitions structural rather
  than coded — a rebuilt tree carries no unlocks, so the re-block-on-recheck rule needs no code,
  and the unlock dies with the review it belongs to, so the two cannot fall out of step. The fold
  lands in `review.py` *before* the Session work, so the Session is built against the folded
  interface and never writes the binding code only to delete it.
