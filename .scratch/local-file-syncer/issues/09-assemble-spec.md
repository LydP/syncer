# Assemble the build-ready spec

Parent: [Map: Local file syncer](../map.md)
Type: task
Status: resolved
Blocked by: 02, 03, 04, 05, 06, 07, 08, 10

## Question

Nothing to decide — this ticket assembles the resolved decisions into `.scratch/local-file-syncer/spec.md`, a document someone can implement the tool from without re-litigating anything.

Pull from every closed ticket and the map's "Decisions so far": the domain model (`CONTEXT.md`), the sync-rule config model and file formats, the divergence state model, the check engine's change taxonomy and algorithm, the review-and-sync tree UI (with the prototype linked), the divergence UX, GUI rule management and first-run, deletion/rename handling, sync application (the executor), and the settled constraints from the map's Notes. Fold in the "Not yet specified" fog items that got resolved along the way; note any that are deliberately deferred to the build effort.

If ticket 01 concluded "adopt tool X", this ticket instead writes a short adoption note (tool, config, how each use case is set up) rather than a build spec, and the map's destination is considered reached.

## Answer

[Survey free tools for one-way fan-out sync with review](01-free-tool-survey.md) concluded
build-from-scratch, so this ticket assembled the full build spec at
[`spec.md`](../spec.md) rather than an adoption note.

The spec pulls together, section by section: the domain model and settled constraints; portable
storage (ADR 0001); the `config.toml` schema and rule/replica identity rules
([02](02-sync-rule-config-model.md)); the check engine's walk/match/hash algorithm and full
per-file taxonomy ([05](05-check-engine.md), including its `dircmp`-drop correction to
[01](01-free-tool-survey.md)); the `state.json` baseline shape and write/consistency rules
([03](03-divergence-state-model.md)); the review-and-sync tree UI, its selection roll-up and the
three action-category split, with the prototype linked
([04](04-review-and-sync-tree-ui.md)); deletion, rename-non-detection, and the master-missing
rule-level block ([08](08-deletion-and-rename-handling.md)); the divergence-resolution dialog and
its three actions plus bulk resolution ([06](06-divergence-ux.md)); GUI rule management,
first-run, and the single-instance lock ([07](07-gui-rule-management.md)); and the sync executor's
copy/delete mechanics, ordering, partial-failure handling, and logging
([10](10-sync-application.md)).

It also carries forward, as explicitly deferred (not decided): packaging/run model, large-folder
performance strategy beyond the size+mtime re-hash skip, and config/state migration mechanics —
all left to the future build effort. Out-of-scope items (watching, reverse sync, merge,
non-Windows, network sync, git operations) are restated as a closing section.

No new decisions were made assembling this — every claim in `spec.md` traces to a resolved
ticket or the map's settled Notes. The map's destination is reached.
