"""RSyntaxTextArea 3.3.0 PythonFoldParser 的 Python 版(去掉 currentFold!=null 防護的舊行為),用來找會讓它 NPE 的行。
近似:空白行/純註解行/落在多行字串內部的行 → 忽略;其餘用開頭空白數(tab=4)。"""
import sys, io, tokenize

def classify(src):
    """每一行回傳 None(忽略)或縮排數。多行字串內部的行(含結束引號所在行以前)忽略。"""
    lines = src.split("\n")
    ignore = set()
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.STRING and tok.start[0] != tok.end[0]:
                for ln in range(tok.start[0] + 1, tok.end[0] + 1):
                    ignore.add(ln)                      # 從第二行起(含結束行)的開頭是字串 token
    except Exception as e:
        print("tokenize 失敗", e)
    out = []
    for i, l in enumerate(lines, 1):
        s = l.strip()
        if i in ignore or not s or s.startswith("#"):
            out.append(None); continue
        n = 0
        for ch in l:
            if ch == " ": n += 1
            elif ch == "\t": n += 4 - n % 4
            else: break
        out.append(n)
    return lines, out

class Fold:
    def __init__(self, parent=None): self.parent = parent; self.children = []
    def create_child(self):
        c = Fold(self); self.children.append(c); return c
    def remove_from_parent(self):
        if self.parent is not None:
            self.parent.children.pop(); self.parent = None; return True
        return False

def simulate(src):
    """精確移植 3.3.0 的狀態機(含 Fold.removeFromParent 會把 parent 設成 null 的行為)。"""
    lines, ind = classify(src)
    stack = []; cur = 0; cf = None; folds = []
    for i, n in enumerate(ind):
        if n is None: continue
        if n == cur: pass
        elif n > cur:
            if cf is not None: cf = cf.create_child()
            else: cf = Fold(); folds.append(cf)
            stack.append(cur)
        else:
            found = False
            while stack and stack[-1] >= n:
                if cf is None:
                    return i + 1, lines[i]
                cf = cf.parent; stack.pop(); found = True
            if not found and cf is not None and not cf.remove_from_parent():
                folds.pop()
        cur = n
    return None

if __name__ == "__main__":
    for p in sys.argv[1:]:
        r = simulate(open(p, encoding="utf-8").read())
        print(p, "→", "OK(模擬不會當)" if r is None else f"NPE 在第 {r[0]} 行附近: {r[1][:80]!r}")
