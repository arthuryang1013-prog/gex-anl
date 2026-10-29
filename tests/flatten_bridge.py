"""把 orderflow_bridge.py 的括號續行縮排對齊到該行起點(內容 AST 不變)。Bookmap 編輯器的 Python 摺疊解析器
(RSyntaxTextArea PythonFoldParser 3.3.0)遇到「續行縮排比下一行深」會 NullPointerException,貼不上去。
用法:python3 tests/flatten_bridge.py orderflow_bridge.py(改完 bridge 後跑,再跑 tests/test_bridge_fold_safe.py)"""
import io, sys, tokenize, ast
path = sys.argv[1]
src = open(path, encoding="utf-8").read()
lines = src.split("\n")
toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
inside_str = set()
for t in toks:
    if t.type == tokenize.STRING and t.start[0] != t.end[0]:
        inside_str.update(range(t.start[0] + 1, t.end[0] + 1))
start_indent = {}
cur_start = None
newline_prev = True
first_on_line = {}
for t in toks:
    if t.type in (tokenize.NL, tokenize.COMMENT, tokenize.INDENT, tokenize.DEDENT, tokenize.ENDMARKER):
        continue
    if t.type == tokenize.NEWLINE:
        newline_prev = True; continue
    if newline_prev:
        cur_start = t.start[1]; newline_prev = False
    ln = t.start[0]
    if ln not in first_on_line:
        first_on_line[ln] = cur_start
changed = 0
for ln, ind in first_on_line.items():
    if ln in inside_str:
        continue
    l = lines[ln - 1]
    cur = len(l) - len(l.lstrip(" "))
    if cur != ind and l.strip():
        lines[ln - 1] = " " * ind + l.lstrip(" "); changed += 1
# 純註解行(括號內)也對齊,免得看起來亂
out = "\n".join(lines)
assert ast.dump(ast.parse(src)) == ast.dump(ast.parse(out)), "AST 不同!"
open(path, "w", encoding="utf-8").write(out)
print("重排續行:", changed)
