"""bridge 貼進 Bookmap 編輯器不能觸發它的摺疊解析器 bug:
(1) 用 RSyntaxTextArea 3.3.0 PythonFoldParser 的移植版模擬,不能 NPE;
(2) 更嚴格的規則:括號內的續行縮排要等於該邏輯行的起點(這樣就不會有「續行比下一行深」)。"""
import os, sys, io, tokenize
here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, here)
import fold_sim
path = os.path.join(os.path.dirname(here), "orderflow_bridge.py")
src = open(path, encoding="utf-8").read()
r = fold_sim.simulate(src)
assert r is None, f"摺疊解析器模擬會 NPE:第 {r[0]} 行 {r[1][:60]!r}。先跑 python3 tests/flatten_bridge.py orderflow_bridge.py"
lines = src.split("\n")
inside = set(); first = {}; cur = None; nl = True
for t in tokenize.generate_tokens(io.StringIO(src).readline):
    if t.type == tokenize.STRING and t.start[0] != t.end[0]:
        inside.update(range(t.start[0] + 1, t.end[0] + 1))
    if t.type in (tokenize.NL, tokenize.COMMENT, tokenize.INDENT, tokenize.DEDENT, tokenize.ENDMARKER): continue
    if t.type == tokenize.NEWLINE: nl = True; continue
    if nl: cur = t.start[1]; nl = False
    first.setdefault(t.start[0], cur)
bad = [ln for ln, ind in first.items() if ln not in inside and (len(lines[ln-1]) - len(lines[ln-1].lstrip(" "))) != ind]
assert not bad, f"續行縮排沒對齊(第 {bad[:5]} 行…):python3 tests/flatten_bridge.py orderflow_bridge.py"
# 模擬器自己要抓得到已知會當的寫法
bad_src = "def f():\n    if a:\n        if g(x,\n                  y):\n            z()\n    w = 1\n"
assert fold_sim.simulate(bad_src) is not None, "模擬器沒抓到已知的壞寫法"
print("bridge fold safe ok")
