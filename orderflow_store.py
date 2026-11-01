"""訂單流事件與每分鐘彙總的「長期保存」:讓熱圖歷史回放看得到 Bookmap 指標。

為什麼需要:orderflow_history.jsonl 只留最近 2 小時、orderflow_events.jsonl 只留 2 天,回放更早的日子就空了。
這裡另存兩份很小的長期檔(預設不刪),都是「一天一個檔(UTC 日期)」的 jsonl,只追加不改寫:
  orderflow_events_archive/YYYYMMDD.jsonl   每行一個事件(跟 orderflow_events.jsonl 同格式,有 id 可去重)
  orderflow_minutes_archive/YYYYMMDD.jsonl  每行一分鐘彙總(orderflow_minutes.minute_rows 的一列,含 n = 該分鐘收到幾包)
重送/重啟造成的重複,讀取時用 id(事件)或 t 取 n 最大者(分鐘)去掉。壞行一律略過。
"""
import json
import os
import threading
import time

from orderflow_minutes import minute_rows

KINDS = ("sweep", "stop", "absorb", "iceberg", "exhaust")


def day_of(t):
    return time.strftime("%Y%m%d", time.gmtime(t))


def _days(lo, hi):
    d = int(lo // 86400) * 86400
    out = []
    while d <= hi:
        out.append(time.strftime("%Y%m%d", time.gmtime(d)))
        d += 86400
    return out


def _append_lines(path, lines):
    if not lines:
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write("".join(ln + "\n" for ln in lines))


def _read_jsonl(path):
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for ln in f:
                try:
                    r = json.loads(ln)
                except ValueError:
                    continue
                if isinstance(r, dict):
                    yield r
    except OSError:
        return


# ---------------- 事件 ----------------
def append_events(dir_, events):
    """events: list of dict(要有 t)。依事件發生的 UTC 日期分檔追加。"""
    by = {}
    for e in events:
        t = e.get("t")
        if isinstance(t, (int, float)):
            by.setdefault(day_of(t), []).append(json.dumps(e, ensure_ascii=False, separators=(",", ":")))
    for day, lines in by.items():
        _append_lines(os.path.join(dir_, day + ".jsonl"), lines)


def read_events(dir_, lo, hi):
    """[lo,hi] 內的事件(去重、依時間排序)。"""
    seen, out = set(), []
    for day in _days(lo, hi):
        for e in _read_jsonl(os.path.join(dir_, day + ".jsonl")):
            t = e.get("t")
            if not isinstance(t, (int, float)) or not (lo <= t <= hi) or e.get("k") not in KINDS:
                continue
            eid = e.get("id")
            if eid:
                if eid in seen:
                    continue
                seen.add(eid)
            out.append(e)
    out.sort(key=lambda e: e["t"])
    return out


def event_ids(dir_, day):
    return {e.get("id") for e in _read_jsonl(os.path.join(dir_, day + ".jsonl")) if e.get("id")}


# ---------------- 每分鐘彙總 ----------------
class MinuteAgg:
    """一筆一筆餵 orderflow 紀錄(要有 received_at);分鐘換了就把上一分鐘彙總成一列寫進檔。"""

    def __init__(self, dir_):
        self.dir = dir_
        self.cur = None
        self.recs = []
        self.lock = threading.Lock()

    def _row(self):
        rows = minute_rows(self.recs)
        return rows[0] if rows else None

    def _write(self, row):
        if row:
            _append_lines(os.path.join(self.dir, day_of(row["t"]) + ".jsonl"), [json.dumps(row, ensure_ascii=False, separators=(",", ":"))])

    def add(self, rec):
        try:
            ra = float(rec.get("received_at"))
        except (TypeError, ValueError):
            return
        m = int(ra // 60) * 60
        with self.lock:
            if self.cur is not None and m != self.cur:
                self._write(self._row())
                self.recs = []
            self.cur = m
            self.recs.append(rec)

    def flush_if_stale(self, now, secs=120):
        """流量停了(收盤、斷線)時,不用等下一包也把上一分鐘寫出去。"""
        with self.lock:
            if self.recs and self.cur is not None and now - self.cur >= secs:
                self._write(self._row())
                self.recs = []
                self.cur = None

    def flush(self):
        """程式結束時把還沒滿一分鐘的那列也寫出去(重複的列讀取時會取 n 較大的)。"""
        with self.lock:
            if self.recs:
                self._write(self._row())
                self.recs = []
                self.cur = None


def append_minutes(dir_, rows):
    by = {}
    for r in rows:
        by.setdefault(day_of(r["t"]), []).append(json.dumps(r, ensure_ascii=False, separators=(",", ":")))
    for day, lines in by.items():
        _append_lines(os.path.join(dir_, day + ".jsonl"), lines)


def read_minutes(dir_, lo, hi):
    """{分鐘起點 epoch: 列}。同一分鐘有多列時取收到包數 n 最大的。"""
    best = {}
    for day in _days(lo, hi):
        for r in _read_jsonl(os.path.join(dir_, day + ".jsonl")):
            t = r.get("t")
            if not isinstance(t, (int, float)) or not (lo <= t <= hi):
                continue
            if t not in best or (r.get("n") or 0) >= (best[t].get("n") or 0):
                best[t] = r
    return best


def minute_times(dir_, day):
    return {r.get("t") for r in _read_jsonl(os.path.join(dir_, day + ".jsonl"))}
