"""throwaway smoke check - construct every variant headless, exercise roll-up."""
import os, sys
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.argv = ["_smoke"]

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt
app = QApplication([])

import review_prototype as rp

w = rp.Main("A")
for v in w.variants:
    assert getattr(v, "tree", None) or getattr(v, "detail", None)
    print(f"variant {v.key}: {v.title}")

# roll-DOWN on variant A: tick rule 0, expect only safe+delete leaves ticked
a = w.variants[0]
root = a.tree.invisibleRootItem()
rule0 = root.child(0)
rule0.setCheckState(0, Qt.Checked)
checked = a.tree.checked_leaves()
buckets = {}
for _k, cat in checked:
    b = rp.CATEGORIES[cat]["bucket"]
    buckets[b] = buckets.get(b, 0) + 1
print(f"  tick rule 0 -> {len(checked)} leaves: {buckets}")
assert rp.CLEAN not in buckets and rp.CONFLICT not in buckets, buckets
assert buckets.get(rp.SAFE) and buckets.get(rp.DELETE)

# roll-UP: untick one leaf, rule 0 must go partial
first_leaf = next(a.tree.invisibleRootItem().child(0).child(0).child(j)
                  for j in range(rule0.child(0).childCount())
                  if rule0.child(0).child(j).data(0, rp.ROLE_CAT))
first_leaf.setCheckState(0, Qt.Unchecked)
assert rule0.checkState(0) == Qt.PartiallyChecked, rule0.checkState(0)
print("  untick one leaf -> rule 0 partial OK")

rule0.setCheckState(0, Qt.Unchecked)
assert not a.tree.checked_leaves()
print("  untick rule -> clear OK")

# variant B: switching replica rebuilds the right pane
b = w.variants[1]
b.list.setCurrentRow(0)
n0 = b.detail.invisibleRootItem().childCount()
b.list.setCurrentRow(2)
n2 = b.detail.invisibleRootItem().childCount()
print(f"  B replica 0 top rows={n0}, replica 2 top rows={n2}")
assert n0 and n2

# variant D: switching rule rebuilds the right pane, top rows are replicas
d = w.variants[3]
d.list.setCurrentRow(0)
dr = d.detail.invisibleRootItem()
top = [dr.child(i).text(0) for i in range(dr.childCount())]
print(f"  D rule 0 top rows (replicas)={len(top)}")
assert len(top) == len(rp.FAKE[0].replicas)
# roll-down across a replica
dr.child(0).setCheckState(0, Qt.Checked)
print(f"  D tick replica 0 -> {len(d.detail.checked_leaves())} leaves")
assert d.detail.checked_leaves()

# variant C: conflict group heads are not tickable
c = w.variants[2]
cr = c.tree.invisibleRootItem()
for i in range(cr.childCount()):
    h = cr.child(i)
    tickable = bool(h.flags() & Qt.ItemIsUserCheckable)
    print(f"  C group {h.text(0)!r} tickable={tickable}")

print(f"totals: {rp._tally(rp.FAKE)}")
print("ALL OK")
