"""
PROTOTYPE - throwaway code answering ticket 04 "Review-and-sync tree UI".

Question: what does the review screen look like, and how does syncing at any
level of the rule -> replica -> folder -> file hierarchy work?

These three variants pull hardest on TREE SHAPE & SELECTION ROLL-UP:

  A  Unified tri-state tree     one tree, rule > replica > folder > file,
                                checkbox roll-up both up and down
  B  Two-pane, replica-scoped   pick a replica on the left, tick its file
                                tree on the right; no cross-replica selection
  C  Flat grouped change list   no folder tree at all; rows grouped by change
                                category, tick per-row or per-category

Everything else (delete category kept separate, diverged/conflict hand-off to
ticket 06, "sync all" scope) is handled the same way in all three so the tree
shape is what you are actually comparing.

Run:
    venv\\Scripts\\python.exe .scratch\\local-file-syncer\\prototype\\review_prototype.py

Switch variants: bottom bar arrows, Left/Right arrow keys, or --variant A|B|C

Not production. No persistence. Fake check data only. shutil/hashlib never touched.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass

from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QBrush, QColor, QFont, QShortcut, QKeySequence
from PySide6.QtWidgets import (
    QApplication, QComboBox, QFrame, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QPlainTextEdit, QPushButton, QSplitter, QStackedWidget,
    QToolButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

# --------------------------------------------------------------------------- #
#  Change taxonomy  (a rough stand-in for ticket 05's full set - enough to     #
#  exercise the UI; 05 owns the authoritative list)                            #
# --------------------------------------------------------------------------- #

CLEAN, SAFE, DELETE, CONFLICT = "clean", "safe", "delete", "conflict"

CATEGORIES = {
    "in_sync":      dict(label="In sync",        glyph="=", color="#7c7c7c", bucket=CLEAN),
    "new":          dict(label="New in master",  glyph="+", color="#2e7d32", bucket=SAFE),
    "changed":      dict(label="Changed",        glyph="~", color="#1565c0", bucket=SAFE),
    "will_delete":  dict(label="Master deleted", glyph="\u2212", color="#c62828", bucket=DELETE),
    "diverged":     dict(label="Diverged",       glyph="!", color="#e65100", bucket=CONFLICT),
    "both_changed": dict(label="Changed on both sides", glyph="\u2260", color="#6a1b9a", bucket=CONFLICT),
    "no_baseline":  dict(label="No baseline",    glyph="?", color="#4e342e", bucket=CONFLICT),
}

BUCKET_ACTION = {
    SAFE:     "overwrite from master",
    DELETE:   "delete from replica (confirmed separately)",
    CONFLICT: "hand off to Divergence UX (ticket 06)",
}


# --------------------------------------------------------------------------- #
#  Fake check result                                                           #
# --------------------------------------------------------------------------- #

@dataclass
class FileChange:
    rel_path: str      # POSIX-style, preserves master casing (per ticket 03)
    category: str


@dataclass
class Replica:
    path: str
    files: list[FileChange]


@dataclass
class Rule:
    name: str
    master: str
    master_type: str   # "dir" | "file"
    replicas: list[Replica]


def _skill_files(**overrides) -> list[FileChange]:
    base = {
        "SKILL.md": "in_sync",
        "references/api.md": "in_sync",
        "references/patterns.md": "in_sync",
        "examples/basic.md": "in_sync",
        "examples/advanced.md": "in_sync",
    }
    base.update(overrides)
    return [FileChange(p, c) for p, c in base.items()]


FAKE: list[Rule] = [
    Rule(
        name="cursor-rules skill",
        master=r"C:\MyStuff\skills\cursor-rules",
        master_type="dir",
        replicas=[
            Replica(
                r"C:\Projects\alpha\.claude\skills\cursor-rules",
                _skill_files(**{
                    "SKILL.md": "changed",
                    "references/patterns.md": "new",
                    "examples/advanced.md": "will_delete",
                }),
            ),
            Replica(
                r"C:\Projects\beta\.claude\skills\cursor-rules",
                _skill_files(),  # fully in sync
            ),
            Replica(
                r"C:\Projects\gamma\.claude\skills\cursor-rules",
                _skill_files(**{
                    "SKILL.md": "diverged",
                    "references/api.md": "both_changed",
                    "references/patterns.md": "new",
                    "examples/advanced.md": "will_delete",
                }) + [FileChange("notes.local.md", "no_baseline")],
            ),
        ],
    ),
    Rule(
        name="master resume",
        master=r"C:\MyStuff\Career\resume.md",
        master_type="file",
        replicas=[
            Replica(r"C:\Users\Lloyd\Documents\Jobs\resume.md",
                    [FileChange("resume.md", "changed")]),
            Replica(r"C:\Users\Lloyd\Desktop\resume.md",
                    [FileChange("resume.md", "in_sync")]),
        ],
    ),
]


# --------------------------------------------------------------------------- #
#  Selection roll-up helpers, shared by every variant                          #
# --------------------------------------------------------------------------- #

ROLE_CAT = Qt.UserRole + 1      # category string on a leaf
ROLE_KEY = Qt.UserRole + 2      # (rule, replica_path, rel_path) tuple on a leaf
ROLE_BRANCH = Qt.UserRole + 3   # True on a rule / replica / folder row


class RollupTree(QTreeWidget):
    """A QTreeWidget whose checkboxes propagate down to children and up to
    parents as tri-state. Only leaves that carry a category are checkable;
    clean rows are shown but frozen."""

    def __init__(self, headers: list[str], on_change):
        super().__init__()
        self.setHeaderLabels(headers)
        self.setColumnCount(len(headers))
        self.setUniformRowHeights(True)
        self.setAlternatingRowColors(True)
        self.setAnimated(True)
        self._on_change = on_change
        self._guard = False
        self.itemChanged.connect(self._item_changed)

    # -- construction --------------------------------------------------------
    def add_leaf(self, parent, text, cat, key) -> QTreeWidgetItem:
        it = QTreeWidgetItem(parent, [text, _cat_label(cat), _key_hint(key)])
        it.setData(0, ROLE_CAT, cat)
        it.setData(0, ROLE_KEY, key)
        meta = CATEGORIES[cat]
        it.setForeground(1, QBrush(QColor(meta["color"])))
        if meta["bucket"] == CLEAN:
            # in sync - nothing to do, shown greyed for context
            it.setFlags(it.flags() & ~Qt.ItemIsUserCheckable & ~Qt.ItemIsEnabled)
        elif meta["bucket"] == CONFLICT:
            # diverged / changed-on-both / no-baseline: never a plain tick.
            # Resolution is ticket 06's flow, so the row is display-only here.
            it.setFlags(it.flags() & ~Qt.ItemIsUserCheckable)
            it.setText(2, "needs a decision → ticket 06")
        else:
            # safe (overwrite from master) and delete (own confirm gate)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(0, Qt.Unchecked)
        return it

    def add_branch(self, parent, text) -> QTreeWidgetItem:
        it = QTreeWidgetItem(parent, [text, "", ""])
        # NB: neither ItemIsAutoTristate nor ItemIsUserTristate.
        #  - AutoTristate: Qt would recompute a parent from its children the
        #    instant you tick it, killing roll-DOWN.
        #  - UserTristate: a click would cycle unchecked -> PART -> checked, so
        #    the first click only part-ticks. We want one click = full tick +
        #    roll-down; PART is a *computed* display state only, set by
        #    _recompute_ancestors when some (not all) children are ticked.
        it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
        it.setData(0, ROLE_BRANCH, True)
        it.setCheckState(0, Qt.Unchecked)
        f = it.font(0); f.setBold(True); it.setFont(0, f)
        return it

    # -- roll-up -----------------------------------------------------------
    def _item_changed(self, item, col):
        if col != 0 or self._guard:
            return
        self._guard = True
        try:
            state = item.checkState(0)
            if state != Qt.PartiallyChecked and item.childCount():
                _roll_down(item, state)
            _recompute_ancestors(item)
        finally:
            self._guard = False
        self._on_change()

    # -- read selection --------------------------------------------------
    def checked_leaves(self):
        out = []
        it = self.invisibleRootItem()
        stack = [it.child(i) for i in range(it.childCount())]
        while stack:
            node = stack.pop()
            cat = node.data(0, ROLE_CAT)
            if cat is not None:
                if node.checkState(0) == Qt.Checked:
                    out.append((node.data(0, ROLE_KEY), cat))
            else:
                stack.extend(node.child(i) for i in range(node.childCount()))
        return out


def _selectable_leaf(n) -> bool:
    return (n.data(0, ROLE_CAT) is not None
            and bool(n.flags() & Qt.ItemIsUserCheckable))


def _roll_down(item, state):
    """Push a Checked/Unchecked state onto every descendant: selectable leaves
    take it directly, branches mirror it so the tree reads consistently."""
    for i in range(item.childCount()):
        ch = item.child(i)
        if _selectable_leaf(ch):
            ch.setCheckState(0, state)
        elif ch.data(0, ROLE_BRANCH):
            ch.setCheckState(0, state)
        _roll_down(ch, state)


def _leaf_states(node):
    out = []
    stack = [node.child(i) for i in range(node.childCount())]
    while stack:
        n = stack.pop()
        if _selectable_leaf(n):
            out.append(n.checkState(0))
        stack.extend(n.child(i) for i in range(n.childCount()))
    return out


def _recompute_ancestors(item):
    parent = item.parent()
    while parent is not None:
        states = _leaf_states(parent)
        if not states:
            pass  # no selectable leaves under here; leave as-is
        elif all(s == Qt.Checked for s in states):
            parent.setCheckState(0, Qt.Checked)
        elif all(s == Qt.Unchecked for s in states):
            parent.setCheckState(0, Qt.Unchecked)
        else:
            parent.setCheckState(0, Qt.PartiallyChecked)
        parent = parent.parent()


# --------------------------------------------------------------------------- #
#  Small formatting helpers                                                    #
# --------------------------------------------------------------------------- #

def _cat_label(cat: str) -> str:
    m = CATEGORIES[cat]
    return f"{m['glyph']}  {m['label']}"


def _key_hint(key) -> str:
    return ""


def _tally(rule_list) -> dict[str, int]:
    t = {b: 0 for b in (CLEAN, SAFE, DELETE, CONFLICT)}
    for rule in rule_list:
        for rep in rule.replicas:
            for fc in rep.files:
                t[CATEGORIES[fc.category]["bucket"]] += 1
    return t


def _replica_tally(rep: Replica) -> dict[str, int]:
    t = {b: 0 for b in (CLEAN, SAFE, DELETE, CONFLICT)}
    for fc in rep.files:
        t[CATEGORIES[fc.category]["bucket"]] += 1
    return t


def _tally_text(t: dict[str, int]) -> str:
    bits = []
    if t[SAFE]:
        bits.append(f"{t[SAFE]} to sync")
    if t[DELETE]:
        bits.append(f"{t[DELETE]} to delete")
    if t[CONFLICT]:
        bits.append(f"{t[CONFLICT]} conflict")
    if not bits:
        bits.append("in sync")
    return ", ".join(bits)


def _folder_split(rel_path: str):
    parts = rel_path.split("/")
    return parts[:-1], parts[-1]


# --------------------------------------------------------------------------- #
#  Shared bottom action bar                                                    #
# --------------------------------------------------------------------------- #

class ActionBar(QFrame):
    """Sync buttons + a live readout of what the current selection would do.
    Identical in every variant so only the tree above it is under test."""

    def __init__(self, log):
        super().__init__()
        self.setFrameShape(QFrame.StyledPanel)
        self._log = log
        self._get_selection = lambda: []      # variant plugs this in
        self._totals = {SAFE: 0, DELETE: 0, CONFLICT: 0}

        self.summary = QLabel("-")
        self.summary.setWordWrap(True)
        self.btn_sel = QPushButton("Sync selected")
        self.btn_all = QPushButton("Sync all safe changes")
        self.btn_del = QPushButton("Review deletes")
        self.btn_con = QPushButton("Resolve conflicts \u2192 (ticket 06)")
        self.btn_con.setEnabled(False)
        for b in (self.btn_sel, self.btn_all, self.btn_del):
            b.clicked.connect(self._fire)
        self.btn_sel._kind = "selected"
        self.btn_all._kind = "all_safe"
        self.btn_del._kind = "deletes"

        row = QHBoxLayout()
        row.addWidget(self.btn_sel)
        row.addWidget(self.btn_all)
        row.addWidget(self.btn_del)
        row.addWidget(self.btn_con)
        row.addStretch(1)

        lay = QVBoxLayout(self)
        lay.addWidget(self.summary)
        lay.addLayout(row)

    def wire(self, get_selection, totals):
        self._get_selection = get_selection
        self._totals = totals
        self.btn_all.setText(f"Sync all safe changes ({totals[SAFE]})")
        self.btn_del.setText(f"Review deletes ({totals[DELETE]})")
        self.btn_del.setEnabled(bool(totals[DELETE]))
        self.btn_con.setText(f"Resolve conflicts \u2192 ticket 06 ({totals[CONFLICT]})")
        self.refresh()

    def refresh(self):
        sel = self._get_selection()
        by_bucket = {SAFE: 0, DELETE: 0, CONFLICT: 0}
        for _key, cat in sel:
            by_bucket[CATEGORIES[cat]["bucket"]] += 1
        n = len(sel)
        self.btn_sel.setText(f"Sync selected ({n})")
        self.btn_sel.setEnabled(n > 0)
        if not sel:
            self.summary.setText(
                f"Nothing ticked.  Check found: {self._totals[SAFE]} safe, "
                f"{self._totals[DELETE]} deletes, {self._totals[CONFLICT]} conflicts."
            )
            return
        self.summary.setText(
            f"Ticked {n}:  {by_bucket[SAFE]} overwrite from master, "
            f"{by_bucket[DELETE]} delete, {by_bucket[CONFLICT]} conflict "
            f"(conflicts can't be synced here - use ticket 06's flow)."
        )

    def _fire(self):
        kind = self.sender()._kind
        if kind == "selected":
            items = self._get_selection()
        elif kind == "all_safe":
            items = [(k, c) for (k, c) in self._all_leaves()
                     if CATEGORIES[c]["bucket"] == SAFE]
        else:  # deletes
            items = [(k, c) for (k, c) in self._all_leaves()
                     if CATEGORIES[c]["bucket"] == DELETE]

        self._log.appendPlainText(f"\n=== PROTOTYPE action: {kind} ===")
        if not items:
            self._log.appendPlainText("  (nothing)")
            return
        skipped = 0
        for (rule_name, rep_path, rel), cat in sorted(items):
            b = CATEGORIES[cat]["bucket"]
            if b == CONFLICT:
                skipped += 1
                continue
            self._log.appendPlainText(
                f"  {BUCKET_ACTION[b]:<45}  {rule_name} / ...{rep_path[-40:]} / {rel}"
            )
        if skipped:
            self._log.appendPlainText(f"  {skipped} conflict file(s) left for ticket 06's flow")

    # filled in by the host window
    _all_leaves = staticmethod(lambda: [])


def all_fake_leaves():
    out = []
    for rule in FAKE:
        for rep in rule.replicas:
            for fc in rep.files:
                if CATEGORIES[fc.category]["bucket"] != CLEAN:
                    out.append(((rule.name, rep.path, fc.rel_path), fc.category))
    return out


# --------------------------------------------------------------------------- #
#  Variant A - unified tri-state tree                                          #
# --------------------------------------------------------------------------- #

class VariantA(QWidget):
    key = "A"
    title = "Unified tri-state tree"
    blurb = ("One tree for everything: rule > replica > folder > file. Tick any "
             "node; the check rolls down to its files and up to a part-tick on "
             "its parents. 'Sync all safe' ignores what you've ticked.")

    def __init__(self, log):
        super().__init__()
        self.tree = RollupTree(["Master / replica / file", "Change", ""], self._changed)
        self.bar = ActionBar(log)
        ActionBar._all_leaves = staticmethod(all_fake_leaves)

        lay = QVBoxLayout(self)
        lay.addWidget(_hint(self.blurb))
        lay.addWidget(self.tree, 1)
        lay.addWidget(self.bar)

        self._build()
        self.bar.wire(self.tree.checked_leaves, _tally(FAKE))
        self.tree.expandToDepth(1)

    def _build(self):
        for rule in FAKE:
            r_it = self.tree.add_branch(
                self.tree, f"{rule.name}   \u2014   master: {rule.master}")
            r_it.setData(0, Qt.UserRole, "rule")
            for rep in rule.replicas:
                tally = _replica_tally(rep)
                rep_it = self.tree.add_branch(r_it, rep.path)
                rep_it.setText(2, _tally_text(tally))
                folders: dict[str, QTreeWidgetItem] = {"": rep_it}
                for fc in sorted(rep.files, key=lambda f: f.rel_path):
                    dirs, name = _folder_split(fc.rel_path)
                    parent = rep_it
                    acc = ""
                    for d in dirs:
                        acc = f"{acc}/{d}" if acc else d
                        if acc not in folders:
                            folders[acc] = self.tree.add_branch(parent, d + "/")
                        parent = folders[acc]
                    self.tree.add_leaf(parent, name, fc.category,
                                       (rule.name, rep.path, fc.rel_path))
        _recolour_disabled(self.tree)

    def _changed(self):
        self.bar.refresh()


# --------------------------------------------------------------------------- #
#  Variant B - two-pane, replica-scoped                                        #
# --------------------------------------------------------------------------- #

class VariantB(QWidget):
    key = "B"
    title = "Two-pane, replica-scoped"
    blurb = ("Left: every replica with a one-line status. Right: the file tree "
             "for the one replica you've selected, with roll-up inside that "
             "replica only. You sync one replica at a time.")

    def __init__(self, log):
        super().__init__()
        self._log = log
        self.list = QListWidget()
        self.list.setMaximumWidth(360)
        self.detail = RollupTree(["Folder / file", "Change", ""], self._changed)
        self.bar = ActionBar(log)
        ActionBar._all_leaves = staticmethod(all_fake_leaves)

        split = QSplitter()
        split.addWidget(self.list)
        split.addWidget(self.detail)
        split.setStretchFactor(1, 1)

        lay = QVBoxLayout(self)
        lay.addWidget(_hint(self.blurb))
        lay.addWidget(split, 1)
        lay.addWidget(self.bar)

        self._replicas: list[tuple[Rule, Replica]] = []
        for rule in FAKE:
            for rep in rule.replicas:
                self._replicas.append((rule, rep))
                t = _replica_tally(rep)
                item = QListWidgetItem(f"{rule.name}\n   {rep.path}\n   {_tally_text(t)}")
                if t[SAFE] or t[DELETE] or t[CONFLICT]:
                    item.setForeground(QBrush(QColor("#1565c0")))
                else:
                    item.setForeground(QBrush(QColor("#7c7c7c")))
                self.list.addItem(item)
        self.list.currentRowChanged.connect(self._show_replica)
        self.bar.wire(self.detail.checked_leaves, _tally(FAKE))
        self.list.setCurrentRow(0)

    def _show_replica(self, row):
        self.detail.clear()
        if row < 0:
            return
        rule, rep = self._replicas[row]
        folders: dict[str, QTreeWidgetItem] = {}
        for fc in sorted(rep.files, key=lambda f: f.rel_path):
            dirs, name = _folder_split(fc.rel_path)
            parent = self.detail.invisibleRootItem()
            acc = ""
            for d in dirs:
                acc = f"{acc}/{d}" if acc else d
                if acc not in folders:
                    folders[acc] = self.detail.add_branch(parent, d + "/")
                parent = folders[acc]
            self.detail.add_leaf(parent, name, fc.category,
                                 (rule.name, rep.path, fc.rel_path))
        self.detail.expandAll()
        _recolour_disabled(self.detail)
        self._changed()

    def _changed(self):
        self.bar.refresh()


# --------------------------------------------------------------------------- #
#  Variant C - flat grouped change list                                       #
# --------------------------------------------------------------------------- #

class VariantC(QWidget):
    key = "C"
    title = "Flat grouped change list"
    blurb = ("No folder tree. Every changed file is one row, grouped by change "
             "type. Tick a whole category or individual rows. Folder structure "
             "shows only as text in the path column.")

    def __init__(self, log):
        super().__init__()
        self.tree = RollupTree(["Change / file", "Rule / replica", ""], self._changed)
        self.tree.setAlternatingRowColors(False)
        self.bar = ActionBar(log)
        ActionBar._all_leaves = staticmethod(all_fake_leaves)

        lay = QVBoxLayout(self)
        lay.addWidget(_hint(self.blurb))
        lay.addWidget(self.tree, 1)
        lay.addWidget(self.bar)

        order = ["new", "changed", "will_delete", "both_changed", "diverged", "no_baseline"]
        grouped: dict[str, list] = {k: [] for k in order}
        for rule in FAKE:
            for rep in rule.replicas:
                for fc in rep.files:
                    if fc.category in grouped:
                        grouped[fc.category].append((rule, rep, fc))

        for cat in order:
            rows = grouped[cat]
            if not rows:
                continue
            meta = CATEGORIES[cat]
            head = self.tree.add_branch(
                self.tree, f"{meta['glyph']}  {meta['label']}  ({len(rows)})")
            head.setForeground(0, QBrush(QColor(meta["color"])))
            if meta["bucket"] == CONFLICT:
                head.setText(2, "\u2192 ticket 06")
                head.setFlags(head.flags() & ~Qt.ItemIsUserCheckable)
            elif meta["bucket"] == DELETE:
                head.setText(2, "confirm separately")
            for rule, rep, fc in rows:
                leaf = self.tree.add_leaf(
                    head, fc.rel_path, fc.category,
                    (rule.name, rep.path, fc.rel_path))
                leaf.setText(1, f"{rule.name}  /  ...{rep.path[-34:]}")
                leaf.setText(2, "")
        self.tree.expandAll()
        _recolour_disabled(self.tree)
        self.bar.wire(self.tree.checked_leaves, _tally(FAKE))

    def _changed(self):
        self.bar.refresh()


# --------------------------------------------------------------------------- #
#  Variant D - two-pane, rule-scoped (B, but the left list holds rules)        #
# --------------------------------------------------------------------------- #

class VariantD(QWidget):
    key = "D"
    title = "Two-pane, rule-scoped"
    blurb = ("Left: every sync rule with a one-line status. Right: replica > "
             "folder > file for the one rule you've selected, with roll-up "
             "across that rule's replicas. You sync one rule at a time.")

    def __init__(self, log):
        super().__init__()
        self._log = log
        self.list = QListWidget()
        self.list.setMaximumWidth(360)
        self.detail = RollupTree(["Replica / folder / file", "Change", ""], self._changed)
        self.bar = ActionBar(log)
        ActionBar._all_leaves = staticmethod(all_fake_leaves)

        split = QSplitter()
        split.addWidget(self.list)
        split.addWidget(self.detail)
        split.setStretchFactor(1, 1)

        lay = QVBoxLayout(self)
        lay.addWidget(_hint(self.blurb))
        lay.addWidget(split, 1)
        lay.addWidget(self.bar)

        for rule in FAKE:
            t = {b: 0 for b in (CLEAN, SAFE, DELETE, CONFLICT)}
            for rep in rule.replicas:
                for k, v in _replica_tally(rep).items():
                    t[k] += v
            n_rep = len(rule.replicas)
            item = QListWidgetItem(
                f"{rule.name}\n   master: {rule.master}\n"
                f"   {n_rep} replica{'s' if n_rep != 1 else ''}  ·  {_tally_text(t)}")
            item.setForeground(QBrush(QColor(
                "#1565c0" if (t[SAFE] or t[DELETE] or t[CONFLICT]) else "#7c7c7c")))
            self.list.addItem(item)

        self.list.currentRowChanged.connect(self._show_rule)
        self.bar.wire(self.detail.checked_leaves, _tally(FAKE))
        self.list.setCurrentRow(0)

    def _show_rule(self, row):
        self.detail.clear()
        if row < 0:
            return
        rule = FAKE[row]
        for rep in rule.replicas:
            tally = _replica_tally(rep)
            rep_it = self.detail.add_branch(self.detail.invisibleRootItem(), rep.path)
            rep_it.setText(2, _tally_text(tally))
            folders: dict[str, QTreeWidgetItem] = {}
            for fc in sorted(rep.files, key=lambda f: f.rel_path):
                dirs, name = _folder_split(fc.rel_path)
                parent = rep_it
                acc = ""
                for d in dirs:
                    acc = f"{acc}/{d}" if acc else d
                    if acc not in folders:
                        folders[acc] = self.detail.add_branch(parent, d + "/")
                    parent = folders[acc]
                self.detail.add_leaf(parent, name, fc.category,
                                     (rule.name, rep.path, fc.rel_path))
        self.detail.expandAll()
        _recolour_disabled(self.detail)
        self._changed()

    def _changed(self):
        self.bar.refresh()


# --------------------------------------------------------------------------- #
#  Chrome                                                                      #
# --------------------------------------------------------------------------- #

def _hint(text: str) -> QLabel:
    lab = QLabel(text)
    lab.setWordWrap(True)
    lab.setStyleSheet("color:#444; padding:6px 2px;")
    return lab


def _recolour_disabled(tree: QTreeWidget):
    grey = QBrush(QColor("#9a9a9a"))
    it = tree.invisibleRootItem()
    stack = [it.child(i) for i in range(it.childCount())]
    while stack:
        n = stack.pop()
        if not (n.flags() & Qt.ItemIsEnabled):
            for c in range(tree.columnCount()):
                n.setForeground(c, grey)
        stack.extend(n.child(i) for i in range(n.childCount()))


class Switcher(QFrame):
    def __init__(self, keys, on_pick):
        super().__init__()
        self.setFrameShape(QFrame.StyledPanel)
        self.setStyleSheet(
            "QFrame{background:#222;border-radius:8px;} "
            "QLabel{color:#eee;} QToolButton{color:#eee;font-size:16px;}")
        self._keys = keys
        self._on_pick = on_pick
        self.combo = QComboBox()
        prev = QToolButton(); prev.setText("\u25c0")
        nxt = QToolButton(); nxt.setText("\u25b6")
        prev.clicked.connect(lambda: self._step(-1))
        nxt.clicked.connect(lambda: self._step(1))
        self.combo.currentIndexChanged.connect(self._on_combo)
        lay = QHBoxLayout(self)
        lay.addWidget(QLabel("PROTOTYPE - variant:"))
        lay.addWidget(prev)
        lay.addWidget(self.combo)
        lay.addWidget(nxt)

    def set_options(self, labels):
        self.combo.blockSignals(True)
        self.combo.addItems(labels)
        self.combo.blockSignals(False)

    def _on_combo(self, i):
        self._on_pick(i)

    def _step(self, d):
        i = (self.combo.currentIndex() + d) % len(self._keys)
        self.combo.setCurrentIndex(i)

    def select(self, i):
        self.combo.setCurrentIndex(i)


class Main(QWidget):
    def __init__(self, start="A"):
        super().__init__()
        self.setWindowTitle("Syncer - review & sync screen  [PROTOTYPE / ticket 04]")
        self.resize(QSize(1080, 760))

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(150)
        self.log.setPlaceholderText(
            "Action log - clicking a Sync button prints what it WOULD do here. "
            "Nothing is ever written.")
        self.log.setStyleSheet("font-family:Consolas,monospace;font-size:11px;")

        self.stack = QStackedWidget()
        self.variants = [VariantA(self.log), VariantB(self.log),
                         VariantC(self.log), VariantD(self.log)]
        for v in self.variants:
            self.stack.addWidget(v)

        self.switch = Switcher([v.key for v in self.variants], self._pick)
        self.switch.set_options([f"{v.key}  \u2014  {v.title}" for v in self.variants])

        lay = QVBoxLayout(self)
        lay.addWidget(self.stack, 1)
        lay.addWidget(self.log)
        lay.addWidget(self.switch)

        QShortcut(QKeySequence(Qt.Key_Left), self, activated=lambda: self.switch._step(-1))
        QShortcut(QKeySequence(Qt.Key_Right), self, activated=lambda: self.switch._step(1))

        idx = {"A": 0, "B": 1, "C": 2, "D": 3}.get(start.upper(), 0)
        self.switch.select(idx)
        self._pick(idx)

    def _pick(self, i):
        self.stack.setCurrentIndex(i)
        v = self.variants[i]
        # re-wire the shared bar's "sync selected" reader to the visible variant
        bar = v.bar
        bar.refresh()


def main():
    start = "A"
    if "--variant" in sys.argv:
        start = sys.argv[sys.argv.index("--variant") + 1]
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    w = Main(start)
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
