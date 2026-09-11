# Prototype — Review-and-sync tree UI (ticket 04)

**Throwaway.** Answers: what does the review screen look like, and how does syncing
at any level of the rule → replica → folder → file hierarchy work? These three
variants pull hardest on **tree shape & selection roll-up**.

## Run

```
venv\Scripts\python.exe .scratch\local-file-syncer\prototype\review_prototype.py
```

Switch variants: the bottom bar arrows, the `←` / `→` arrow keys, or
`--variant A|B|C|D` on the command line.

## The variants

| | Shape | Roll-up | You sync… |
|---|---|---|---|
| **A** Unified tri-state tree | one tree: rule › replica › folder › file | tick any node → rolls down to its files, up to a part-tick on parents | any subtree, across replicas, in one go |
| **B** Two-pane, replica-scoped | replica list ‹ · › file tree of the selected replica | roll-up lives *inside* one replica only | one replica at a time |
| **C** Flat grouped change list | no folder tree; rows grouped by change category | tick a whole category or single rows | by change type |
| **D** Two-pane, rule-scoped | rule list ‹ · › replica › folder › file tree for the selected rule | roll-up across that rule's replicas | one rule at a time |

Selecting a parent = one click → full tick + roll-down. A row shows the dash
only when *computed* partial (some but not all children ticked).

## Held constant in all three (so only the tree shape is under test)

- **Safe drift** (new / changed) — checkable, `Sync all safe` ignores your ticks.
- **Master-deleted** — checkable but its own `Review deletes (N)` gate, never in `Sync all safe`.
- **Conflicts** (diverged / changed-on-both / no-baseline) — *not* checkable anywhere;
  the row is display-only and points at ticket 06's resolution flow.
- Bottom action bar + action log are identical per variant. Nothing is ever written —
  `Sync` buttons print what they *would* do.

## Fake check data

Two rules: a `cursor-rules` skill folder fanned to 3 replicas (alpha = ordinary
drift, beta = fully in sync, gamma = diverged + a foreign file), and a single-file
`resume.md` master to 2 replicas. Covers every category.

## Files

- `review_prototype.py` — the prototype
- `_smoke.py` — headless construction + roll-up check (`QT_QPA_PLATFORM=offscreen`)
