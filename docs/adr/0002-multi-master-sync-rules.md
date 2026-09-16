# Sync rules generalize to many masters, many replicas

## Status

accepted

## Decision

A sync rule is no longer one master paired with a list of replicas. It's a set of masters paired with a set of replicas, with full fan-out inside the rule (every master lands in every replica of that rule). A folder-type master's content lands under a subfolder of the replica named for the master's own basename, so several masters in one rule don't collide; a file-type master lands directly at the replica path. Replica paths are no longer required to be unique across rules — the same folder may be a replica of more than one rule.

## Why

The driving case is keeping Claude Code skill folders current: a project should receive a shared "baseline dev skills" master *and* a language-specific master (Python, Flutter, ...) without redeclaring the baseline inside every project's rule. The old one-master-per-rule model forced either duplicating the baseline master into every rule, or giving up on composing masters at all. Letting a replica belong to more than one rule is what makes the baseline reusable; letting a rule hold more than one master is what makes "this project's skills = baseline + language" expressible as a single unit when that's simpler than splitting it.

## Considered Options

- **Flat merge instead of per-master namespacing**: every master's files land directly in the replica root. Rejected — two unrelated masters both containing e.g. `README.md` would silently collide with no way to tell which one wrote what.
- **A new "project" term above sync rule**, keeping "sync rule" as an internal one-master building block. Rejected — the destination use case ("one rule per project" for the simple case) falls out naturally from a rule just holding several masters; a second layer of terminology would exist for a distinction nothing else needs.

## Consequences

- Supersedes `spec.md` §2/§4's shipped-v1 constraints ("one master paired with the list of replicas... one rule per syncable unit"; "master paths unique across all rules"). `spec.md` stays as the historical record of the v1 build, not current.
- `config.toml`'s `[[rule]]` shape, `check.py`'s three-way comparison, and `state.json`'s keying all need rework to add a master dimension — tracked in [Multi-master sync rules: decision map](https://github.com/LydP/syncer/issues/12), not resolved by this ADR alone.
- Introduces a new conflict category (cross-rule master-basename collision, when two rules' masters collide by basename against a shared replica) not yet in `CONTEXT.md`'s Conflict taxonomy — detection and UX deferred to that map's tickets.
- No migration was needed: no rules existed in `config.toml`/`state.json` at the time of this decision.
