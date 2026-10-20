"""
nq_gex_dashboard.py -- strike-by-time GEX heatmap in NQ coordinates for NQ + QQQ + NDX options (a TRACE-style
*model* view), fed by nq_gex_collector.py.

Run (second Terminal window, same folder as the collector), or just double-click start_gex.command:
    python3 -m pip install streamlit plotly pandas numpy      (first time only)
    streamlit run nq_gex_dashboard.py

New in this version
  * Support / resistance (Call wall, Put wall, Flip) can be shown for the combined selection AND for each of NQ, QQQ, NDX
    separately, on the same chart.
  * Every colour is selectable (heatmap scale, every line and band, bars, time-series lines). "Save settings" keeps
    them in gex_dashboard_settings.json next to this file.
  * Abs GEX lens and an optional "pin" line: where gamma is most concentrated, with no dealer-direction assumption.
  * Before/after panel (third column): per-strike change versus a comparison frame (weight change, newly traded volume,
    or speed-up/slow-down of trading), green = more, red = less, with the biggest movers highlighted.
  * Replay mode: every snapshot the collector wrote is a frame. Pick a day, drag the slider or press Play.
    The collector records from the moment it starts; the dashboard does not need to be open for that.

What the picture is -- and is not
  * Model GEX = position weight x live gamma. Weight is either previous-day open interest (OI) or today's running
    volume. Gamma is recomputed from price / IV / time to expiry.
  * OI mode does not see intraday new positions. Volume mode sees today's flow, but volume has no buyer/seller or
    dealer/customer split, so it is a rougher proxy for dealer positioning.
  * Assumes dealers are long calls / short puts (a convention, not a verified fact).
  * QQQ and NDX strikes are mapped to NQ points with one proportional ratio (all three prices read at one instant).
  * Wall / band / flip definitions are this tool's own and are not SpotGamma's or Barchart's.
"""
import json
import os
import sqlite3
import time

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

try:
    HERE = os.path.dirname(os.path.abspath(__file__))
except NameError:
    HERE = os.getcwd()
DB_PATH = os.path.join(HERE, "nq_gex.sqlite")
SETTINGS_FILE = os.path.join(HERE, "gex_dashboard_settings.json")
LOCAL_TZ = "Asia/Taipei"
AUTO_DEFAULT = os.environ.get("NQGEX_NO_REFRESH") != "1"
RISK_FREE = 0.04                      # keep equal to nq_gex_collector.py
SRC_ORDER = ["NQ", "QQQ", "NDX"]
SRC_COLORS = {"NQ": "#3b6fd4", "QQQ": "#2e9e6b", "NDX": "#e09f3e"}
COMBINED = "合計(所選來源)"
BASIS_OPTS = ["未平倉量 (OI)", "成交量 (Volume)"]
SCOPE_OPTS = ["全部", "排除 0DTE", "只看 0DTE"]
LENS_OPTS = ["Net GEX", "Call GEX", "Put GEX", "Abs GEX(|Call|+|Put|,不假設方向)"]
MODE_OPTS = ["數值", "相對前一次快照的變化 (Δ)"]
WALL_OPTS = ["全域最大", "Call 取現價上方、Put 取現價下方"]
HEAT_PRESETS = ["RdBu", "RdYlBu", "RdYlGn", "PiYG", "PRGn", "BrBG", "PuOr", "Spectral", "Picnic", "Portland",
                "Bluered", "Temps", "Tealrose", "Viridis", "Cividis", "Plasma", "Turbo", "自訂(負/中/正)"]
ABS_PRESETS = ["Viridis", "Plasma", "Inferno", "Magma", "Cividis", "Turbo", "YlOrRd", "YlGnBu", "Blues", "Greens",
               "Oranges", "Reds", "Purples", "Hot", "Greys", "自訂(低/高)"]
THEME_OPTS = ["白底", "深色底"]
CHG_WINDOWS = ["前一幀", "5 分鐘", "15 分鐘", "30 分鐘", "60 分鐘"]
CHG_UNITS = ["口數", "名目金額($M)", "Gamma 加權($M / 1%)"]
B_TABLE, B_NET, B_SURF, B_EXPIRY = "各來源結果表", "Net GEX 折線圖", "3D 曲面圖(到期日 × 履約價)", "到期日 × 履約價 熱圖"
BLOCK_OPTS = [B_TABLE, B_NET, B_SURF, B_EXPIRY]
BLOCKS_VERSION = 4
SURF_VIEWS = {"預設斜視": dict(x=1.5, y=-1.8, z=1.0), "俯視": dict(x=0.01, y=0.01, z=2.6),
              "由到期日方向看(各履約價的輪廓)": dict(x=2.6, y=0.01, z=0.35), "由履約價方向看(各到期日的輪廓)": dict(x=0.01, y=-2.6, z=0.35)}          # bump when blocks are added, so an older saved block selection does not hide the new ones
CHG_MODES = ["權重變化(本幀 − 比較幀)", "新增成交量", "新增成交的增減(本期 − 上一期)"]
VKEY = ["src", "expiry", "cls", "strike", "cp"]      # identifies one option contract across snapshots

# every control that is remembered (and can be saved) -> its default
DEFAULTS = dict(
    hours=8, sel_sources=list(SRC_ORDER), basis=BASIS_OPTS[0], scope=SCOPE_OPTS[0], lens=LENS_OPTS[0], mode=MODE_OPTS[0],
    bin_pts=25, window=600,
    show_levels=True, level_sets=[COMBINED], bands_per_src=False, wall_mode=WALL_OPTS[0], band_pct=70, band_alpha=0.22,
    wall_hold=0,
    heat_scale="RdBu", heat_reverse=False, heat_neg="#b2182b", heat_mid="#f7f7f7", heat_pos="#2166ac", chart_theme=THEME_OPTS[0],
    col_spot="#000000", col_call="#00a86b", col_put="#9c27b0", col_flip="#ff9800",
    col_net_all="#636efa", col_net_ex0="#ef553b", col_net_spot="#000000",
    show_blocks=[b_ for b_ in BLOCK_OPTS if b_ != B_EXPIRY], surf_view=list(SURF_VIEWS)[0], surf_floor=True, surf_z=0.6,
    chg_on=True, chg_mode=CHG_MODES[0], chg_window=CHG_WINDOWS[0], chg_unit=CHG_UNITS[0], chg_split=True, chg_top=5,
    col_up="#2e9e6b", col_down="#d1495b", col_prev="#ffa000",
    show_pin=False, col_pin="#d81b60", abs_scale="Viridis", abs_reverse=False, abs_low="#fff5eb", abs_high="#7f2704",
    auto=AUTO_DEFAULT, every=30,
)
for _s in SRC_ORDER:
    for _t in ("call", "put", "flip"):
        DEFAULTS[f"col_{_s}_{_t}"] = SRC_COLORS[_s]
    DEFAULTS[f"bar_{_s}"] = SRC_COLORS[_s]
OPTS = dict(basis=BASIS_OPTS, scope=SCOPE_OPTS, lens=LENS_OPTS, mode=MODE_OPTS, wall_mode=WALL_OPTS, heat_scale=HEAT_PRESETS,
            abs_scale=ABS_PRESETS, chart_theme=THEME_OPTS, bin_pts=[10, 25, 50, 100], every=[15, 30, 60, 120], surf_view=list(SURF_VIEWS),
            chg_window=CHG_WINDOWS, chg_unit=CHG_UNITS, chg_mode=CHG_MODES)
MULTI = dict(show_blocks=BLOCK_OPTS, sel_sources=SRC_ORDER, level_sets=[COMBINED] + SRC_ORDER)
RANGES = dict(surf_z=(0.2, 1.5), chg_top=(0, 10), hours=(1, 24), window=(200, 1500), band_pct=(50, 95), band_alpha=(0.05, 0.6), wall_hold=(0, 50))

st.set_page_config(page_title="NQ GEX Monitor", layout="wide")
st.title("NQ GEX Monitor")


# ----------------------------- small helpers -----------------------------
def to_local(series):
    return pd.to_datetime(series, utc=True).dt.tz_convert(LOCAL_TZ).dt.tz_localize(None)


def hex_rgba(h, a):
    h = h.lstrip("#")
    return f"rgba({int(h[0:2], 16)},{int(h[2:4], 16)},{int(h[4:6], 16)},{a})"


def n0(x, nd=0):
    return "n/a" if x is None or (isinstance(x, float) and np.isnan(x)) else f"{x:,.{nd}f}"


def valid(k, v):
    d = DEFAULTS[k]
    try:
        if k in OPTS:
            return v in OPTS[k]
        if k in MULTI:
            return isinstance(v, list) and all(x in MULTI[k] for x in v)
        if k in RANGES:
            lo, hi = RANGES[k]
            return isinstance(v, (int, float)) and not isinstance(v, bool) and lo <= v <= hi
        if isinstance(d, bool):
            return isinstance(v, bool)
        if isinstance(d, str) and d.startswith("#"):
            return isinstance(v, str) and len(v) == 7 and v.startswith("#") and int(v[1:], 16) >= 0
    except Exception:
        return False
    return isinstance(v, type(d))


# ----------------------------- saved settings -----------------------------
def init_state():
    """Populate widget state once per browser session: defaults, overridden by gex_dashboard_settings.json."""
    if st.session_state.get("_init"):
        return
    st.session_state["_init"] = True
    saved = {}
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            saved = json.load(f)
    except Exception:
        pass
    if not isinstance(saved, dict):
        saved = {}
    if saved.get("blocks_v") != BLOCKS_VERSION:
        saved.pop("show_blocks", None)
    for k, d in DEFAULTS.items():
        v = saved.get(k, d)
        st.session_state[k] = v if valid(k, v) else d
    st.session_state.setdefault("rp_on", False)
    st.session_state.setdefault("rp_play", False)
    st.session_state.setdefault("rp_speed", 1.0)


def save_settings():
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump({**{k: st.session_state[k] for k in DEFAULTS}, "blocks_v": BLOCKS_VERSION}, f, ensure_ascii=False, indent=1)
        st.session_state["_msg"] = f"已儲存到 {os.path.basename(SETTINGS_FILE)}"
    except Exception as e:
        st.session_state["_msg"] = f"儲存失敗:{e}"


def reset_settings():
    try:
        if os.path.exists(SETTINGS_FILE):
            os.remove(SETTINGS_FILE)
    except Exception:
        pass
    for k, d in DEFAULTS.items():
        st.session_state[k] = d
    st.session_state["_msg"] = "已還原預設"


# ----------------------------- database access -----------------------------
def _ro():
    return sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)


@st.cache_data(ttl=10, show_spinner=False)
def all_snapshot_ts(_rev=0):
    if not os.path.exists(DB_PATH):
        return None
    con = _ro()
    try:
        return [r[0] for r in con.execute("SELECT ts FROM snapshots ORDER BY ts")]
    finally:
        con.close()


def day_bounds(date_str):
    start = pd.Timestamp(date_str, tz=LOCAL_TZ)
    end = start + pd.Timedelta(days=1)
    return (start.tz_convert("UTC").isoformat(timespec="seconds"), end.tz_convert("UTC").isoformat(timespec="seconds"))


def replay_dates(all_ts):
    d = pd.to_datetime(pd.Series(all_ts), utc=True).dt.tz_convert(LOCAL_TZ).dt.strftime("%Y-%m-%d")
    return sorted(set(d), reverse=True)


def frames_for(all_ts, date_str):
    s, e = day_bounds(date_str)
    return [t for t in all_ts if s <= t < e]


@st.cache_data(ttl=20, show_spinner=False)
def load_range(start, end, rev):
    """Snapshots and contracts with start <= ts <= end. Works on an older NQ-only database too."""
    con = _ro()
    try:
        snaps = pd.read_sql("SELECT * FROM snapshots WHERE ts >= ? AND ts <= ? ORDER BY ts", con, params=(start, end))
        have = {r[1] for r in con.execute("PRAGMA table_info(contracts)")}

        def col(name, default):
            return f"COALESCE({name}, {default}) AS {name}" if name in have else f"{default} AS {name}"

        sql = ("SELECT ts, dte, expiry, cls, strike, cp, oi, iv, F, T, gamma, gex, "
               + ", ".join([col("src", "'NQ'"), col("strike_nq", "strike"), col("vol", "0.0"), col("gex_vol", "0.0"),
                            col("mult", "20.0"), col("q", str(RISK_FREE))])
               + " FROM contracts WHERE ts >= ? AND ts <= ?")
        cons = pd.read_sql(sql, con, params=(start, end))
    finally:
        con.close()
    return snaps, cons


# ----------------------------- GEX maths (levels) -----------------------------
def bs_gamma(S, K, T, iv, r, q):
    sd = iv * np.sqrt(T)
    d1 = (np.log(S / K) + (r - q + 0.5 * iv**2) * T) / sd
    return np.exp(-q * T) * np.exp(-0.5 * d1**2) / np.sqrt(2 * np.pi) / (S * sd)


def flip_of(sub, spot, wcol):
    """Zero-gamma flip (NQ points) of one snapshot: the crossing of the sticky-strike profile closest to spot."""
    if sub.empty:
        return np.nan
    grid = np.linspace(spot * 0.96, spot * 1.04, 121)
    Fg = sub["F"].values[:, None] * (grid[None, :] / spot)
    g = bs_gamma(Fg, sub["strike"].values[:, None], sub["T"].values[:, None], sub["iv"].values[:, None],
                 RISK_FREE, sub["q"].values[:, None])
    sign = np.where(sub["cp"].values == "C", 1.0, -1.0)[:, None]
    prof = (sign * g * sub[wcol].values[:, None] * sub["mult"].values[:, None] * Fg**2 * 0.01).sum(axis=0)
    flips = []
    for k in np.where(np.sign(prof[:-1]) * np.sign(prof[1:]) < 0)[0]:
        x0, x1, y0, y1 = grid[k], grid[k + 1], prof[k], prof[k + 1]
        flips.append(x0 - y0 * (x1 - x0) / (y1 - y0))
    return min(flips, key=lambda x: abs(x - spot)) if flips else np.nan


@st.cache_data(show_spinner=False, max_entries=24)
def flip_cached(_base, _spots, wcol, key):
    return pd.Series({ts: flip_of(g, _spots[ts], wcol) for ts, g in _base.groupby("ts")}, dtype=float)


def wall_series(base, side, spot_by_ts, restrict, thr_pct, step, hold_pct=0):
    """Per snapshot: the wall (bin with the largest Call weight, or the most negative Put weight) and a band around it.
    hold_pct > 0 keeps the previous wall unless a new peak is more than hold_pct % larger (stops jumping between
    two similar clusters). Returns DataFrame indexed by ts with columns center / lo / hi (band edges are bin edges)."""
    sub = base[base["cp"] == side]
    if sub.empty:
        return pd.DataFrame(columns=["center", "lo", "hi"])
    pv = sub.pivot_table(index="ts", columns="bin", values="w", aggfunc="sum", fill_value=0.0).sort_index(axis=1)
    bins = pv.columns.values.astype(float)
    rows, prev = {}, None
    for ts, r in pv.iterrows():
        v = r.values if side == "C" else -r.values            # magnitudes (>= 0)
        if restrict:
            s = spot_by_ts[ts]
            v = np.where(bins >= s if side == "C" else bins <= s, v, 0.0)
        i = int(np.argmax(v))
        if v[i] <= 0:
            continue
        if hold_pct > 0 and prev is not None and v[prev] > 0 and v[prev] >= v[i] / (1 + hold_pct / 100.0):
            i = prev
        prev = i
        cut = v[i] * thr_pct / 100.0
        lo = hi = i
        while lo - 1 >= 0 and v[lo - 1] >= cut:
            lo -= 1
        while hi + 1 < len(v) and v[hi + 1] >= cut:
            hi += 1
        rows[ts] = (bins[i], bins[lo] - step / 2, bins[hi] + step / 2)
    return pd.DataFrame.from_dict(rows, orient="index", columns=["center", "lo", "hi"])


@st.cache_data(show_spinner=False, max_entries=64)
def wall_cached(_base, _spot_by_ts, side, restrict, thr_pct, step, hold_pct, key):
    return wall_series(_base, side, _spot_by_ts, restrict, thr_pct, step, hold_pct)


@st.cache_data(show_spinner=False, max_entries=24)
def pin_cached(_base, key):
    """Per snapshot: the strike bin with the largest absolute gamma (sum of |Call| + |Put|) -- the 'pin' level."""
    pv = _base.assign(w=_base["w"].abs()).pivot_table(index="ts", columns="bin", values="w", aggfunc="sum", fill_value=0.0)
    return pv.idxmax(axis=1).astype(float)


def volume_change(base, ts_new, ts_old, unit):
    """Newly traded volume per contract between two snapshots. Today's volume is a running total, so it is the
    difference of the two readings (a drop is read as a new session starting). Contracts whose quote carried over
    (stale) show 0 here and the traded volume appears in the next fresh reading."""
    new = base[base["ts"] == ts_new]
    old = base.loc[base["ts"] == ts_old, VKEY + ["vol"]].rename(columns={"vol": "vol_old"})
    d = new.merge(old, on=VKEY, how="left")
    d["vol_old"] = d["vol_old"].fillna(d["vol"])           # absent in the older frame: unknown -> no change
    dv = d["vol"] - d["vol_old"]
    d["dv"] = np.where(dv < 0, d["vol"], dv)
    if unit == CHG_UNITS[1]:
        d["m"] = d["dv"] * d["mult"] * d["F"] / 1e6
    elif unit == CHG_UNITS[2]:
        d["m"] = d["dv"] * d["gamma"] * d["mult"] * d["F"] ** 2 * 0.01 / 1e6
    else:
        d["m"] = d["dv"]
    return d


def make_base(cons, srcs, scope, gcol, bin_pts):
    b = cons[cons["src"].isin(srcs)]
    if scope == "排除 0DTE":
        b = b[b["dte"] > 0]
    elif scope == "只看 0DTE":
        b = b[b["dte"] == 0]
    return b.assign(w=b[gcol], bin=(b["strike_nq"] / bin_pts).round() * bin_pts)


def fmt_wall(w):
    if w.empty:
        return "n/a", ""
    r = w.iloc[-1]
    return f"{r['center']:,.0f}", f"帶 {r['lo']:,.0f} – {r['hi']:,.0f}"


# ----------------------------- controls -----------------------------
def on_replay_toggle():
    if st.session_state.get("rp_on"):
        ts = all_snapshot_ts()
        if ts:
            dates = replay_dates(ts)
            st.session_state["rp_date"] = dates[0]
            st.session_state["rp_idx"] = max(len(frames_for(ts, dates[0])) - 1, 0)
    st.session_state["rp_play"] = False


def on_date_change():
    ts = all_snapshot_ts() or []
    st.session_state["rp_idx"] = max(len(frames_for(ts, st.session_state["rp_date"])) - 1, 0)
    st.session_state["rp_play"] = False


def color_row(sb, items):
    cols = sb.columns(len(items))
    for c, (label, key) in zip(cols, items):
        c.color_picker(label, key=key)


def controls():
    init_state()
    sb = st.sidebar
    all_ts = all_snapshot_ts() or []

    # ---- replay ----
    sb.checkbox("回放模式(播放已記錄的快照)", key="rp_on", on_change=on_replay_toggle,
                help="collector 從啟動那一刻起就持續記錄,每一輪快照就是一幀。回放不需要 collector 正在執行。")
    if st.session_state.get("rp_on") and all_ts:
        dates = replay_dates(all_ts)
        if st.session_state.get("rp_date") not in dates:
            st.session_state["rp_date"] = dates[0]
        sb.selectbox("日期(台北時間)", dates, key="rp_date", on_change=on_date_change)
        n = len(frames_for(all_ts, st.session_state["rp_date"]))
        if st.session_state.pop("_rp_next", False):
            nxt = st.session_state.get("rp_idx", 0) + 1
            if nxt >= n - 1:
                st.session_state["rp_play"] = False
            st.session_state["rp_idx"] = min(nxt, n - 1)
        st.session_state["rp_idx"] = int(min(max(st.session_state.get("rp_idx", n - 1), 0), max(n - 1, 0)))
        if n >= 2:
            sb.slider("回放位置(幀)", 0, n - 1, key="rp_idx")
            c1, c2 = sb.columns(2)
            c1.checkbox("播放", key="rp_play")
            c2.select_slider("秒/幀", [0.25, 0.5, 1.0, 2.0], key="rp_speed")
        else:
            sb.caption("這一天只有一幀。")
    sb.markdown("---")

    # ---- data selection ----
    sb.slider("時間範圍(小時)", 1, 24, key="hours")
    sb.multiselect("來源(熱圖與合計)", SRC_ORDER, key="sel_sources")
    sb.radio("權重基準", BASIS_OPTS, key="basis")
    sb.radio("到期日範圍", SCOPE_OPTS, key="scope")
    sb.radio("鏡頭", LENS_OPTS, key="lens")
    sb.radio("顯示", MODE_OPTS, key="mode")
    sb.select_slider("履約價分組(NQ 點)", [10, 25, 50, 100], key="bin_pts")
    sb.slider("現價上下範圍(NQ 點)", 200, 1500, step=50, key="window")
    sb.markdown("---")
    sb.checkbox("顯示「前後對比」面板(右側第三欄)", key="chg_on",
                help="把本幀和比較幀逐個履約價對比:右邊是增加、左邊是減少。中間欄的短橫線標出比較幀的位置。")
    sb.radio("面板顯示內容", CHG_MODES, key="chg_mode",
             help="權重變化:本幀與比較幀的權重(依左側的權重基準與鏡頭)之差,可增可減。\n"
                  "新增成交量:這段時間新增的成交,只會增加(成交量是當天累計值)。\n"
                  "新增成交的增減:本期新增的成交,比上一個同長度期間多(加快)或少(放慢)。")
    sb.selectbox("比較區間", CHG_WINDOWS, key="chg_window")
    sb.radio("單位(成交量模式)", CHG_UNITS, key="chg_unit",
             help="口數不能跨商品直接比:一口 NDX 的名目金額約是一口 QQQ 的 40 倍。要比較請選名目金額或 Gamma 加權。")
    sb.slider("標示變化最大的履約價(個,0 = 不標示)", 0, 10, key="chg_top")
    sb.checkbox("Call / Put 左右分開(只用於「新增成交量」)", key="chg_split")
    sb.markdown("---")

    sb.selectbox("3D 曲面視角", list(SURF_VIEWS), key="surf_view",
                 help="也可以直接用滑鼠在圖上拖曳旋轉、滾輪縮放、雙擊還原。")
    sb.slider("3D 曲面高度比例", 0.2, 1.5, step=0.05, key="surf_z", help="把 GEX 的高低拉高或壓扁,不改變數值。")
    sb.checkbox("3D 曲面底部投影等高線", key="surf_floor")
    sb.multiselect("要顯示的圖表區塊", BLOCK_OPTS, key="show_blocks",
                   help="沒有勾選的區塊會隱藏。")
    sb.markdown("---")

    # ---- levels ----
    sb.checkbox("顯示 Call / Put wall 與 flip", key="show_levels")
    sb.multiselect("支撐壓力要畫哪些", [COMBINED] + SRC_ORDER, key="level_sets",
                   help="可同時畫「合計」和各個商品自己的牆與 flip。各商品的線不受上面「來源」選取影響。")
    sb.checkbox("各商品也畫支撐壓力帶(陰影)", key="bands_per_src")
    sb.checkbox("畫 Abs gamma 峰值線(釘住價位)", key="show_pin",
                help="每個時間點 |Call|+|Put| 加總最大的履約價分組,不依賴 dealer 多空的假設。依「來源」「到期日範圍」「權重基準」計算。")
    sb.radio("牆的定義", WALL_OPTS, key="wall_mode")
    sb.slider("支撐壓力帶寬門檻(%)", 50, 95, step=5, key="band_pct",
              help="帶 = 從牆的位置往兩側延伸,所有權重不低於該牆峰值此比例的連續履約價分組。")
    sb.slider("牆的黏性(%)", 0, 50, step=5, key="wall_hold",
              help="0 = 每個時間點都取最大值(兩個大小接近的群集之間會來回跳)。調高後,新的最大值要比舊的牆大出這個百分比才會換。")
    sb.markdown("---")

    # ---- colours ----
    with sb.expander("顏色設定"):
        st.selectbox("熱圖色階(Net / Call / Put,以及變化模式)", HEAT_PRESETS, key="heat_scale")
        st.checkbox("反轉熱圖色階(正負顏色對調)", key="heat_reverse")
        st.caption("選「自訂」時使用下面三色(負值、零附近、正值):")
        color_row(st, [("負值", "heat_neg"), ("中間", "heat_mid"), ("正值", "heat_pos")])
        st.markdown("**Abs GEX 鏡頭的熱圖色階**(只有正值,從低到高)")
        st.selectbox("Abs 熱圖色階", ABS_PRESETS, key="abs_scale")
        st.checkbox("反轉 Abs 色階", key="abs_reverse")
        color_row(st, [("低", "abs_low"), ("高", "abs_high")])
        st.selectbox("圖表底色", THEME_OPTS, key="chart_theme",
                     help="選深色底時,請把 NQ 價格線等黑色的線改成淺色。")
        st.slider("陰影透明度", 0.05, 0.6, step=0.01, key="band_alpha")
        st.markdown("**NQ 價格線**")
        color_row(st, [("NQ 價格", "col_spot"), ("Abs 峰值線", "col_pin")])
        st.markdown("**合計的牆與 flip**")
        color_row(st, [("Call wall", "col_call"), ("Put wall", "col_put"), ("Flip", "col_flip")])
        for s in SRC_ORDER:
            st.markdown(f"**{s} 的牆與 flip**(實線=Call、虛線=Put、點線=Flip)")
            color_row(st, [("Call", f"col_{s}_call"), ("Put", f"col_{s}_put"), ("Flip", f"col_{s}_flip")])
        st.markdown("**右側長條圖(各來源)**")
        color_row(st, [(s, f"bar_{s}") for s in SRC_ORDER])
        st.markdown("**前後對比面板**")
        color_row(st, [("增加", "col_up"), ("減少", "col_down"), ("比較幀", "col_prev")])
        st.markdown("**下方折線圖**")
        color_row(st, [("全部", "col_net_all"), ("排除0DTE", "col_net_ex0"), ("NQ", "col_net_spot")])
        b1, b2 = st.columns(2)
        b1.button("儲存設定", on_click=save_settings)
        b2.button("還原預設", on_click=reset_settings)
        if st.session_state.get("_msg"):
            st.caption(st.session_state.pop("_msg"))
    sb.markdown("---")
    sb.checkbox("自動更新(即時模式)", key="auto")
    sb.select_slider("更新間隔(秒)", [15, 30, 60, 120], key="every")
    return {k: st.session_state[k] for k in list(DEFAULTS) + ["rp_on", "rp_play", "rp_speed"]} | {
        "rp_idx": st.session_state.get("rp_idx", 0), "rp_date": st.session_state.get("rp_date")}


# ----------------------------- page -----------------------------
def render(c):
    all_ts = all_snapshot_ts()
    if all_ts is None:
        st.warning("找不到 nq_gex.sqlite。請先執行 collector(或雙擊 start_gex.command)。")
        return None
    if not all_ts:
        st.info("資料庫已建立,但還沒有第一筆快照。第一輪可能要幾分鐘,請稍候。")
        return None

    hours, scope, lens, bin_pts, window = c["hours"], c["scope"], c["lens"], c["bin_pts"], c["window"]
    use_oi = c["basis"] == BASIS_OPTS[0]
    gcol, wcol = ("gex", "oi") if use_oi else ("gex_vol", "vol")
    basis_short = "OI" if use_oi else "Volume"
    restrict = c["wall_mode"] != WALL_OPTS[0]
    band_pct, hold, delta = c["band_pct"], c["wall_hold"], c["mode"] == MODE_OPTS[1]
    replay = bool(c["rp_on"])
    sel = c["sel_sources"]

    # ---- which frame are we showing, and which data range do we load ----
    if replay:
        frames = frames_for(all_ts, c["rp_date"])
        if not frames:
            st.info("這一天沒有快照。")
            return None
        idx = int(min(max(c["rp_idx"], 0), len(frames) - 1))
        as_of = frames[idx]
        d_start, d_end = day_bounds(c["rp_date"])
        start = (pd.Timestamp(d_start) - pd.Timedelta(hours=hours)).isoformat(timespec="seconds")
        end = d_end
    else:
        as_of = all_ts[-1]
        start = (pd.Timestamp(as_of) - pd.Timedelta(hours=hours)).isoformat(timespec="seconds")
        end = "9999-12-31T00:00:00+00:00"
    cutoff = (pd.Timestamp(as_of) - pd.Timedelta(hours=hours)).isoformat(timespec="seconds")
    snaps, cons = load_range(start, end, all_ts[-1])
    if snaps.empty or cons.empty:
        st.info("這段時間沒有資料。")
        return None

    present = [s for s in SRC_ORDER if s in set(cons["src"])]
    sel = [s for s in sel if s in present]
    if not sel:
        st.info("目前選取的來源在這段時間內沒有資料(QQQ、NDX 只在美股選擇權交易時段有資料)。")
        return None

    snaps["t"] = to_local(snaps["ts"])
    spot_by_ts = dict(zip(snaps["ts"], snaps["spot"]))
    sv = snaps[(snaps["ts"] >= cutoff) & (snaps["ts"] <= as_of)]
    last = sv.iloc[-1]
    last_ts = last["ts"]
    spot_now = float(last["spot"])
    rkey = (start, end, all_ts[-1])

    def in_win(s):
        return s[(s.index >= cutoff) & (s.index <= as_of)]

    # ---- the combined selection ----
    base = make_base(cons, sel, scope, gcol, bin_pts)
    if base.empty:
        st.info("目前的篩選條件下沒有資料。")
        return None
    base_v = base[(base["ts"] >= cutoff) & (base["ts"] <= as_of)]
    base_lat = base_v[base_v["ts"] == last_ts]
    ckey = (rkey, scope, gcol, bin_pts, restrict, band_pct, hold)

    def levels_for(b, srcs):
        k = ckey + (tuple(srcs),)
        return (in_win(wall_cached(b, spot_by_ts, "C", restrict, band_pct, bin_pts, hold, k)),
                in_win(wall_cached(b, spot_by_ts, "P", restrict, band_pct, bin_pts, hold, k)),
                in_win(flip_cached(b, spot_by_ts, wcol, k).dropna()))

    call_w, put_w, flip_s = levels_for(base, sel)
    flip_now = float(flip_s.iloc[-1]) if len(flip_s) and flip_s.index[-1] == last_ts else None

    # ---- header ----
    age_min = (pd.Timestamp.now(tz="UTC") - pd.to_datetime(last_ts, utc=True)).total_seconds() / 60
    tag = f"{'+'.join(sel)}|{basis_short}|{scope}"
    if replay:
        st.info(f"回放中:{last['t']:%Y-%m-%d %H:%M:%S}(台北時間)|第 {idx + 1} / {len(frames)} 幀。取消左側「回放模式」回到即時。")
    m = st.columns(6)
    m[0].metric("快照時間(台北)", last["t"].strftime("%H:%M:%S"))
    m[0].caption("回放" if replay else f"{age_min:.0f} 分鐘前")
    m[1].metric("NQ 現價", f"{spot_now:,.1f}")
    m[2].metric("Net GEX", f"{base_lat['w'].sum() / 1e6:+,.0f}M")
    m[2].caption(tag)
    m[3].metric("Flip(NQ 點)", n0(flip_now))
    cw_v, cw_b = fmt_wall(call_w)
    pw_v, pw_b = fmt_wall(put_w)
    m[4].metric("Call wall(壓力)", cw_v)
    m[4].caption(cw_b)
    m[5].metric("Put wall(支撐)", pw_v)
    m[5].caption(pw_b)
    if not replay and age_min > 12:
        st.warning(f"最新快照是 {age_min:.0f} 分鐘前。collector 可能已停止,或目前休市。")
    srcs_now = [s for s in sel if s in set(base_lat["src"])]
    if set(sel) - set(srcs_now):
        st.caption(f"這個時間點沒有 {'、'.join(sorted(set(sel) - set(srcs_now)))} 的資料(美股選擇權休市,或該來源本輪失敗)。")
    if last["n_stale"]:
        st.caption(f"這筆快照中有 {int(last['n_stale'])} / {int(last['n_contracts'])} 個合約沿用上一輪數值(stale)。")

    # ---- per-source table (NQ points and native units) ----
    lat_all = cons[cons["ts"] == last_ts]
    ratios = (lat_all["strike_nq"] / lat_all["strike"]).groupby(lat_all["src"]).median()
    rows = []
    for s in srcs_now:
        sub = base_lat[base_lat["src"] == s]
        r = float(ratios.get(s, 1.0))
        fl = flip_of(sub, spot_now, wcol)
        cw = wall_series(sub, "C", {last_ts: spot_now}, restrict, band_pct, bin_pts)
        pw = wall_series(sub, "P", {last_ts: spot_now}, restrict, band_pct, bin_pts)
        cwv = float(cw["center"].iloc[-1]) if not cw.empty else np.nan
        pwv = float(pw["center"].iloc[-1]) if not pw.empty else np.nan
        pos = "—" if np.isnan(fl) else ("flip 上方(正 gamma 側)" if spot_now > fl else "flip 下方(負 gamma 側)")
        absb = sub.assign(w=sub["w"].abs()).groupby("bin")["w"].sum()
        pkv = float(absb.idxmax()) if len(absb) and absb.max() > 0 else np.nan
        rows.append({"來源": s, "現價(原生)": n0(spot_now / r, 2), "NQ/來源": n0(r, 4), "合約數": f"{len(sub)}",
                     "Net GEX $M": f"{sub['w'].sum() / 1e6:+,.0f}", "Flip(NQ點)": n0(fl), "Flip(原生)": n0(fl / r, 2),
                     "Call wall(NQ點)": n0(cwv), "Call wall(原生)": n0(cwv / r, 2),
                     "Put wall(NQ點)": n0(pwv), "Put wall(原生)": n0(pwv / r, 2),
                     "Abs 峰值(NQ點)": n0(pkv), "Abs 峰值(原生)": n0(pkv / r, 2), "現價位置": pos})
    if rows and B_TABLE in c["show_blocks"]:
        with st.expander(f"各來源結果(原生單位可直接和 Barchart 等工具比較)|{basis_short}|{scope}", expanded=True):
            st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")

    # ---- lens + view window + pivot ----
    abs_lens = lens == LENS_OPTS[3]
    lens_full = base                                    # every loaded snapshot, every strike
    if lens == "Call GEX":
        lens_full = lens_full[lens_full["cp"] == "C"]
    elif lens == "Put GEX":
        lens_full = lens_full[lens_full["cp"] == "P"]
    if abs_lens:
        lens_full = lens_full.assign(w=lens_full["w"].abs())  # |Call| + |Put| per strike: no dealer-direction assumption
    lens_all = lens_full[(lens_full["bin"] >= spot_now - window) & (lens_full["bin"] <= spot_now + window)]   # the comparison frame may lie before the view
    d = lens_all[(lens_all["ts"] >= cutoff) & (lens_all["ts"] <= as_of)]
    if d.empty:
        st.info("目前的篩選條件下沒有資料。")
        return None
    pv = (d.pivot_table(index="bin", columns="ts", values="w", aggfunc="sum", fill_value=0.0) / 1e6).sort_index()
    shown = pv.diff(axis=1).fillna(0.0) if delta else pv
    xs = to_local(pd.Series(pv.columns))
    lim = float(np.nanpercentile(np.abs(shown.values), 98)) or 1.0
    unit = ("Δ " if delta else "") + ("|GEX| " if abs_lens else "") + "$M / 1%"
    if abs_lens and not delta:            # only positive values: a sequential scale from 0 to the peak
        if c["abs_scale"].startswith("自訂"):
            cscale = [[0.0, c["abs_low"]], [1.0, c["abs_high"]]]
        else:
            cscale = c["abs_scale"]
        reverse = bool(c["abs_reverse"])
        zargs = dict(zmin=0.0, zmax=lim)
    else:
        if c["heat_scale"].startswith("自訂"):
            cscale = [[0.0, c["heat_neg"]], [0.5, c["heat_mid"]], [1.0, c["heat_pos"]]]
        else:
            cscale = c["heat_scale"]
        reverse = bool(c["heat_reverse"])
        zargs = dict(zmid=0, zmin=-lim, zmax=lim)
    template = "plotly_dark" if c["chart_theme"] == THEME_OPTS[1] else "plotly_white"

    # ---- before / after comparison (third panel) ----
    cmp_ = None            # what the third panel draws
    chg_note = ""
    ghost = None           # per-bin weight of the comparison frame, drawn as 'before' ticks on the middle panel
    t_old_l = None
    unit_lbl = "$M/1%"
    if c["chg_on"]:
        ts_sorted = sorted(snaps["ts"])

        def older_than(ts_ref):
            i = ts_sorted.index(ts_ref)
            if c["chg_window"] == CHG_WINDOWS[0]:
                return (ts_sorted[i - 1] if i > 0 else None), ""
            minutes = int(c["chg_window"].split()[0])
            target = (pd.Timestamp(ts_ref) - pd.Timedelta(minutes=minutes)).isoformat(timespec="seconds")
            earlier = [t for t in ts_sorted[:i] if t <= target]
            if earlier:
                return earlier[-1], ""
            return (ts_sorted[0] if i > 0 else None), (f"資料不足 {minutes} 分鐘,改和最早一幀比較。" if i > 0 else "")

        def in_view(df):
            return df[(df["bin"] >= spot_now - window) & (df["bin"] <= spot_now + window)]

        ts_old, chg_note = older_than(last_ts)
        mode = c["chg_mode"]
        if ts_old is None:
            chg_note = "還沒有更早的快照可以比較,前後對比面板暫時空白。"
        else:
            t_new_l = to_local(pd.Series([last_ts])).iloc[0]
            t_old_l = to_local(pd.Series([ts_old])).iloc[0]
            span = (pd.Timestamp(last_ts) - pd.Timestamp(ts_old)).total_seconds() / 60
            chg_note = (chg_note + " " if chg_note else "") + f"對比:{t_old_l:%H:%M}(前)→ {t_new_l:%H:%M}(後),間隔約 {span:.0f} 分鐘。"
            ghost = lens_all[lens_all["ts"] == ts_old].groupby("bin")["w"].sum() / 1e6
            if mode == CHG_MODES[0]:
                a = lens_all[lens_all["ts"] == last_ts].groupby(["bin", "src"])["w"].sum() / 1e6
                b = lens_all[lens_all["ts"] == ts_old].groupby(["bin", "src"])["w"].sum() / 1e6
                cmp_ = dict(kind="delta", dsrc=a.sub(b, fill_value=0.0).rename("dv").reset_index(),
                            title=f"與比較幀的差({'|GEX|' if abs_lens else 'GEX'},$M/1%):右 增加|左 減少")
                if lens == LENS_OPTS[0]:
                    chg_note += " Net 鏡頭的「增加」是數值變大(例如從 −5 變成 −2 也算增加);想看量的大小請切到 Abs 鏡頭。"
            elif base["vol"].max() <= 0:
                chg_note = "資料庫裡的成交量全是 0(舊資料庫,或 IBKR 沒有回傳成交量),成交量模式無法計算。"
            else:
                unit_lbl = {CHG_UNITS[0]: "口", CHG_UNITS[1]: "$M", CHG_UNITS[2]: "$M/1%"}[c["chg_unit"]]
                d1 = in_view(volume_change(base, last_ts, ts_old, c["chg_unit"]))
                if mode == CHG_MODES[1]:
                    if d1["m"].abs().sum() > 0:
                        cmp_ = dict(kind="new", bars=d1.groupby(["bin", "src", "cp"])["m"].sum().reset_index(),
                                    title=("新增成交:左 Put|右 Call" if c["chg_split"] else "新增成交(Call+Put)") + f"({unit_lbl})")
                    else:
                        chg_note += " 這段時間沒有新增成交。"
                else:
                    ts_older, n2 = older_than(ts_old)
                    if ts_older is None:
                        chg_note += " 要比「上一期」還需要再更早的一幀。"
                    else:
                        d0 = in_view(volume_change(base, ts_old, ts_older, c["chg_unit"]))
                        a = d1.groupby(["bin", "src"])["m"].sum()
                        b = d0.groupby(["bin", "src"])["m"].sum()
                        cmp_ = dict(kind="delta", dsrc=a.sub(b, fill_value=0.0).rename("dv").reset_index(),
                                    title=f"新增成交的增減({unit_lbl}):右 加快|左 放慢")
                        chg_note += " 上一期 = 比較幀往前同樣長度的期間。"
    if cmp_ is not None and cmp_["kind"] == "delta" and cmp_["dsrc"]["dv"].abs().sum() == 0:
        chg_note += " 兩個時間點之間沒有差異,面板空白。"
        cmp_ = None
    n_right = 3 if cmp_ is not None else 2
    widths = [0.64, 0.18, 0.18] if n_right == 3 else [0.78, 0.22]

    # ---- heatmap + latest strike plot (+ volume change) ----
    fig = make_subplots(rows=1, cols=n_right, column_widths=widths, shared_yaxes=True, horizontal_spacing=0.02)
    fig.add_trace(go.Heatmap(z=shown.values, x=xs, y=shown.index, colorscale=cscale, reversescale=reverse,
                             colorbar=dict(title=unit, x=1.02), **zargs,
                             hovertemplate="%{x|%H:%M}  strike %{y}<br>%{z:,.1f}<extra></extra>"), row=1, col=1)

    alpha = c["band_alpha"]
    level_names = [x for x in c["level_sets"] if x == COMBINED or x in present]
    level_data = {}
    if c["show_levels"]:
        for name in level_names:
            if name == COMBINED:
                level_data[name] = (call_w, put_w, flip_s)
            else:
                bs = make_base(cons, [name], scope, gcol, bin_pts)
                level_data[name] = levels_for(bs, [name]) if not bs.empty else (call_w.iloc[0:0], put_w.iloc[0:0], flip_s.iloc[0:0])

        def band(w, color, with_band):
            if with_band and not w.empty:
                x = to_local(pd.Series(w.index))
                fig.add_trace(go.Scatter(x=x, y=w["lo"], mode="lines", line=dict(width=0), showlegend=False, hoverinfo="skip"), row=1, col=1)
                fig.add_trace(go.Scatter(x=x, y=w["hi"], mode="lines", line=dict(width=0), fill="tonexty",
                                         fillcolor=hex_rgba(color, alpha), showlegend=False, hoverinfo="skip"), row=1, col=1)

        for name, (cw_, pw_, fl_) in level_data.items():
            comb = name == COMBINED
            cc = c["col_call"] if comb else c[f"col_{name}_call"]
            pc = c["col_put"] if comb else c[f"col_{name}_put"]
            fc = c["col_flip"] if comb else c[f"col_{name}_flip"]
            tag_ = "合計" if comb else name
            with_band = comb or c["bands_per_src"]
            band(cw_, cc, with_band)
            band(pw_, pc, with_band)
            for w, col, dash, nm in ((cw_, cc, "solid", f"Call wall {tag_}"), (pw_, pc, "solid" if comb else "dash", f"Put wall {tag_}")):
                if not w.empty:
                    fig.add_trace(go.Scatter(x=to_local(pd.Series(w.index)), y=w["center"],
                                             mode="lines+markers" if len(w) < 3 else "lines", name=nm,   # a lone point has no line
                                             line=dict(color=col, width=2 if comb else 1.8, dash=dash),
                                             marker=dict(size=9, color=col, symbol="diamond"),
                                             hovertemplate=nm + " %{y:,.0f}<extra></extra>"), row=1, col=1)
            if len(fl_):
                nm = f"Flip {tag_}"
                fig.add_trace(go.Scatter(x=to_local(pd.Series(fl_.index)), y=fl_.values,
                                         mode="lines+markers" if len(fl_) < 3 else "lines", name=nm,
                                         line=dict(color=fc, width=2.5 if comb else 1.8, dash="dot"),
                                         marker=dict(size=9, color=fc, symbol="diamond"),
                                         hovertemplate=nm + " %{y:,.0f}<extra></extra>"), row=1, col=1)
    pin_s = None
    if c["show_pin"]:
        pin_s = in_win(pin_cached(base, ckey + (tuple(sel), "pin")))
        if len(pin_s):
            fig.add_trace(go.Scatter(x=to_local(pd.Series(pin_s.index)), y=pin_s.values,
                                     mode="lines+markers" if len(pin_s) < 3 else "lines", name="Abs 峰值(釘住)",
                                     line=dict(color=c["col_pin"], width=2, dash="dashdot"),
                                     marker=dict(size=9, color=c["col_pin"], symbol="diamond"),
                                     hovertemplate="Abs 峰值 %{y:,.0f}<extra></extra>"), row=1, col=1)
    fig.add_trace(go.Scatter(x=sv["t"], y=sv["spot"], mode="lines", name="NQ", line=dict(color=c["col_spot"], width=2)),
                  row=1, col=1)

    lat = d[d["ts"] == last_ts].groupby(["bin", "src"])["w"].sum().div(1e6).reset_index()
    for s in SRC_ORDER:
        part = lat[lat["src"] == s]
        if not part.empty:
            fig.add_trace(go.Bar(x=part["w"], y=part["bin"], orientation="h", name=s, marker_color=c[f"bar_{s}"],
                                 width=bin_pts * 0.9, hovertemplate=s + " %{x:,.1f}M @ %{y}<extra></extra>"), row=1, col=2)
    if ghost is not None:
        g = ghost[ghost != 0]
        if len(g):
            fig.add_trace(go.Scatter(x=g.values, y=g.index, mode="markers", name="比較幀位置",
                                     marker=dict(symbol="line-ns", size=16, line=dict(color=c["col_prev"], width=3)),
                                     hovertemplate="比較幀 %{x:,.1f}M @ %{y}<extra></extra>", showlegend=False), row=1, col=2)
    if t_old_l is not None:                      # dotted marker on the heatmap at the comparison frame
        fig.add_trace(go.Scatter(x=[t_old_l, t_old_l], y=[spot_now - window, spot_now + window], mode="lines",
                                 line=dict(color=c["col_prev"], width=1.5, dash="dot"), showlegend=False,
                                 hoverinfo="skip"), row=1, col=1)

    def fmt_chg(v):
        a_ = abs(v)
        return f"{v:+,.0f}" if a_ >= 100 else (f"{v:+,.1f}" if a_ >= 1 else f"{v:+,.2f}")

    hl_bins, hl_vals = [], []
    if cmp_ is not None and cmp_["kind"] == "new":
        for s_ in SRC_ORDER:
            for cp_, sgn, side in (("C", 1, "Call"), ("P", -1 if c["chg_split"] else 1, "Put")):
                part = cmp_["bars"][(cmp_["bars"]["src"] == s_) & (cmp_["bars"]["cp"] == cp_)]
                part = part[part["m"] != 0]
                if part.empty:
                    continue
                fig.add_trace(go.Bar(x=part["m"] * sgn, y=part["bin"], orientation="h", name=f"{s_} 新增成交", legendgroup=s_,
                                     showlegend=False, marker_color=c[f"bar_{s_}"], width=bin_pts * 0.9,
                                     customdata=part["m"], hovertemplate=f"{s_} {side} 新增 %{{customdata:,.1f}} {unit_lbl} @ %{{y}}<extra></extra>"),
                              row=1, col=3)
        tot = cmp_["bars"].groupby("bin")["m"].sum()
        hl_bins, hl_vals = list(tot.index), list(tot.values)
    elif cmp_ is not None:
        dsrc = cmp_["dsrc"]
        dbin = dsrc.groupby("bin")["dv"].sum()
        piv = dsrc.pivot_table(index="bin", columns="src", values="dv", aggfunc="sum", fill_value=0.0)
        tips = {b_: "<br>".join(f"{s_} {piv.loc[b_, s_]:+,.1f}" for s_ in piv.columns if abs(piv.loc[b_, s_]) > 0) for b_ in piv.index}
        for sgn, colr in ((1, c["col_up"]), (-1, c["col_down"])):
            part = dbin[(dbin * sgn) > 0]
            if part.empty:
                continue
            fig.add_trace(go.Bar(x=part.values, y=part.index, orientation="h", showlegend=False, marker_color=colr,
                                 width=bin_pts * 0.9, customdata=[tips[b_] for b_ in part.index],
                                 hovertemplate="%{y}: %{x:+,.1f}<br>%{customdata}<extra></extra>"), row=1, col=3)
        fig.add_vline(x=0, line_color="gray", line_width=1, row=1, col=3)
        hl_bins, hl_vals = list(dbin.index), list(dbin.values)

    topn = int(c["chg_top"])
    if cmp_ is not None and topn > 0 and hl_bins:
        order = sorted(zip(hl_bins, hl_vals), key=lambda t_: -abs(t_[1]))
        top = [(b_, v_) for b_, v_ in order[:topn] if abs(v_) > 0]
        for b_, v_ in top:                                   # translucent band across both right-hand panels
            colr = c["col_up"] if v_ > 0 else c["col_down"]
            for col_ in (2, 3):
                fig.add_hrect(y0=b_ - bin_pts / 2, y1=b_ + bin_pts / 2, fillcolor=hex_rgba(colr, 0.20), line_width=0,
                              layer="below", row=1, col=col_)
        if top:
            fig.add_trace(go.Scatter(x=[v_ for _, v_ in top], y=[b_ for b_, _ in top], mode="text",
                                     text=[fmt_chg(v_) for _, v_ in top], showlegend=False, hoverinfo="skip",
                                     textposition=["middle right" if v_ > 0 else "middle left" for _, v_ in top],
                                     textfont=dict(size=11)), row=1, col=3)

    def right_levels(col_):
        fig.add_hline(y=spot_now, line_dash="dash", line_color="gray", row=1, col=col_)
        if pin_s is not None and len(pin_s) and pin_s.index[-1] == last_ts:
            fig.add_hline(y=float(pin_s.iloc[-1]), line_dash="dashdot", line_color=c["col_pin"], line_width=2, row=1, col=col_)
        for name, (cw_, pw_, fl_) in level_data.items():
            comb = name == COMBINED
            cc = c["col_call"] if comb else c[f"col_{name}_call"]
            pc = c["col_put"] if comb else c[f"col_{name}_put"]
            fc = c["col_flip"] if comb else c[f"col_{name}_flip"]
            for w, col, dash in ((cw_, cc, "solid"), (pw_, pc, "solid" if comb else "dash")):
                if not w.empty and w.index[-1] == last_ts:
                    rr = w.iloc[-1]
                    if comb or c["bands_per_src"]:
                        fig.add_hrect(y0=rr["lo"], y1=rr["hi"], fillcolor=hex_rgba(col, alpha), line_width=0, row=1, col=col_)
                    fig.add_hline(y=rr["center"], line_color=col, line_width=2 if comb else 1.5, line_dash=dash, row=1, col=col_)
            if len(fl_) and fl_.index[-1] == last_ts:
                fig.add_hline(y=float(fl_.iloc[-1]), line_dash="dot", line_color=fc, line_width=2 if comb else 1.5, row=1, col=col_)

    for col_ in range(2, n_right + 1):
        right_levels(col_)
    fig.update_xaxes(title_text="最新(" + ("|GEX|" if abs_lens else "GEX") + ", $M/1%)|橫線 = 比較幀", title_font=dict(size=11), row=1, col=2)
    if cmp_ is not None:
        fig.update_xaxes(title_text=cmp_["title"], title_font=dict(size=11), row=1, col=3)
    fig.update_yaxes(range=[spot_now - window, spot_now + window], tickformat=",.0f")
    fig.update_layout(height=680, template=template, margin=dict(l=10, r=10, t=40, b=10), barmode="relative",
                      legend=dict(orientation="h", x=0, y=1.08))
    st.markdown(f"**{lens}|{tag}|{'變化' if delta else '數值'}|{bin_pts} 點分組**")
    if chg_note:
        st.caption(chg_note)
    st.plotly_chart(fig, width="stretch")

    # ---- time series of the selection's net weight ----
    if B_NET in c["show_blocks"]:
        sel_cons = cons[(cons["src"].isin(sel)) & (cons["ts"] >= cutoff) & (cons["ts"] <= as_of)]
        net_all = sel_cons.groupby("ts")[gcol].sum() / 1e6
        net_ex0 = sel_cons[sel_cons["dte"] > 0].groupby("ts")[gcol].sum() / 1e6
        ts_fig = make_subplots(specs=[[{"secondary_y": True}]])
        ts_fig.add_trace(go.Scatter(x=to_local(pd.Series(net_all.index)), y=net_all.values, name=f"Net GEX 全部({basis_short})",
                                    line=dict(color=c["col_net_all"])), secondary_y=False)
        ts_fig.add_trace(go.Scatter(x=to_local(pd.Series(net_ex0.index)), y=net_ex0.values, name="Net GEX 排除 0DTE",
                                    line=dict(color=c["col_net_ex0"])), secondary_y=False)
        ts_fig.add_trace(go.Scatter(x=sv["t"], y=sv["spot"], name="NQ", line=dict(color=c["col_net_spot"], width=1)), secondary_y=True)
        ts_fig.add_hline(y=0, line_color="gray", line_width=1)
        ts_fig.update_yaxes(title_text="Net GEX ($M / 1%)", secondary_y=False)
        ts_fig.update_yaxes(title_text="NQ", tickformat=",.0f", secondary_y=True)
        ts_fig.update_layout(height=320, template=template, margin=dict(l=10, r=10, t=10, b=10), legend=dict(orientation="h", y=1.12))
        st.plotly_chart(ts_fig, width="stretch")

    # ---- expiry x strike grid of the frame being shown (feeds the 3D surface and the 2D heatmap) ----
    if B_SURF in c["show_blocks"] or B_EXPIRY in c["show_blocks"]:
        ex = lens_all[lens_all["ts"] == last_ts]
        if ex.empty:
            st.info("到期日 × 履約價圖:這個時間點沒有資料。")
        else:
            piv = (ex.groupby(["bin", "expiry"])["w"].sum() / 1e6).unstack("expiry").fillna(0.0).sort_index()
            piv = piv[sorted(piv.columns)]
            dte_by = ex.groupby("expiry")["dte"].min()
            dte_vals = [int(dte_by[e]) for e in piv.columns]
            labels = [f"{str(e)[4:6]}/{str(e)[6:8]}" + (" (0DTE)" if dte_by[e] == 0 else f" ({int(dte_by[e])}天)") for e in piv.columns]
            tot = (lens_full[lens_full["ts"] == last_ts].groupby("expiry")["w"].sum() / 1e6).reindex(piv.columns).fillna(0.0)
            lim_e = float(np.nanpercentile(np.abs(piv.values), 98)) or 1.0
            if abs_lens:
                e_scale = [[0.0, c["abs_low"]], [1.0, c["abs_high"]]] if c["abs_scale"].startswith("自訂") else c["abs_scale"]
                e_rev, e_z = bool(c["abs_reverse"]), dict(zmin=0.0, zmax=lim_e)
                s_z = dict(cmin=0.0, cmax=lim_e)
                bar_cols = [c["col_net_all"]] * len(tot)
            else:
                e_scale = ([[0.0, c["heat_neg"]], [0.5, c["heat_mid"]], [1.0, c["heat_pos"]]]
                           if c["heat_scale"].startswith("自訂") else c["heat_scale"])
                e_rev, e_z = bool(c["heat_reverse"]), dict(zmid=0, zmin=-lim_e, zmax=lim_e)
                s_z = dict(cmin=-lim_e, cmax=lim_e, cmid=0)
                bar_cols = [c["col_up"] if v >= 0 else c["col_down"] for v in tot.values]
            ttl = f"{lens}|{tag}|{last['t']:%m/%d %H:%M}(台北)"

            # ---- 3D surface ----
            if B_SURF in c["show_blocks"]:
                if len(dte_vals) < 2 or len(piv.index) < 2:
                    st.info("3D 曲面至少需要 2 個到期日和 2 個履約價分組。目前只有 "
                            f"{len(dte_vals)} 個到期日、{len(piv.index)} 個履約價分組:請把「到期日範圍」改回「全部」,或把「履約價分組」調小;"
                            "只有一個到期日時可改看下面的 2D 熱圖。")
                else:
                    f3 = go.Figure()
                    f3.add_trace(go.Surface(
                        x=dte_vals, y=list(piv.index), z=piv.values, colorscale=e_scale, reversescale=e_rev, **s_z,
                        text=[[lb for lb in labels] for _ in range(len(piv.index))], showscale=True,
                        colorbar=dict(title="$M / 1%", len=0.6),
                        hovertemplate="%{text}<br>履約價 %{y:,.0f}<br>%{z:,.1f} $M<extra></extra>",
                        contours=dict(z=dict(show=bool(c["surf_floor"]), usecolormap=True, project_z=bool(c["surf_floor"]), highlight=False))))
                    x0, x1 = min(dte_vals), max(dte_vals)
                    f3.add_trace(go.Scatter3d(x=[x0, x1], y=[spot_now, spot_now], z=[0, 0], mode="lines", name="NQ 現價",
                                              line=dict(color=c["col_spot"], width=7), hoverinfo="name"))
                    if flip_now:
                        f3.add_trace(go.Scatter3d(x=[x0, x1], y=[flip_now, flip_now], z=[0, 0], mode="lines", name="Flip",
                                                  line=dict(color=c["col_flip"], width=7, dash="dash"), hoverinfo="name"))
                    f3.update_layout(
                        height=680, template=template, margin=dict(l=0, r=0, t=10, b=0), legend=dict(orientation="h", y=1.02),
                        scene=dict(xaxis=dict(title="到期日", tickmode="array", tickvals=dte_vals, ticktext=labels),
                                   yaxis=dict(title="履約價(NQ 點)", tickformat=",.0f"),
                                   zaxis=dict(title="GEX ($M / 1%)"),
                                   aspectmode="manual", aspectratio=dict(x=1.3, y=1.6, z=float(c["surf_z"])),
                                   camera=dict(eye=SURF_VIEWS[c["surf_view"]])))
                    st.markdown(f"**3D 曲面:到期日 × 履約價 × GEX|{ttl}**")
                    st.plotly_chart(f3, width="stretch")
                    st.caption("用滑鼠拖曳旋轉、滾輪縮放、雙擊還原視角;左側可切換視角、調整高度比例、開關底部投影。"
                               "線是 NQ 現價(實線)和 flip(虛線),畫在 GEX = 0 的平面上。到期日軸依實際天數排列,週末會有空隙。"
                               "只包含 collector 抓取的到期日(7 天內);顏色與正負的解讀同主熱圖。"
                               "3D 圖前面的高峰會擋住後面,要讀精確數字請看 2D 熱圖或滑鼠移上去的數值。")

            # ---- 2D heatmap ----
            if B_EXPIRY in c["show_blocks"]:
                efig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.04, row_heights=[0.8, 0.2])
                efig.add_trace(go.Heatmap(z=piv.values, x=labels, y=piv.index, colorscale=e_scale, reversescale=e_rev,
                                          colorbar=dict(title="$M / 1%", len=0.75, y=0.62),
                                          hovertemplate="%{x}  strike %{y}<br>%{z:,.1f} $M<extra></extra>", **e_z), row=1, col=1)
                efig.add_hline(y=spot_now, line_dash="dash", line_color=c["col_spot"], line_width=2, row=1, col=1)
                if flip_now:
                    efig.add_hline(y=flip_now, line_dash="dot", line_color=c["col_flip"], line_width=2, row=1, col=1)
                efig.add_trace(go.Bar(x=labels, y=tot.values, marker_color=bar_cols,
                                      hovertemplate="%{x} 合計 %{y:,.1f} $M<extra></extra>"), row=2, col=1)
                efig.add_hline(y=0, line_color="gray", line_width=1, row=2, col=1)
                efig.update_xaxes(type="category")
                efig.update_yaxes(range=[spot_now - window, spot_now + window], tickformat=",.0f", title_text="履約價(NQ 點)", row=1, col=1)
                efig.update_yaxes(title_text="整條鏈合計($M)", title_font=dict(size=11), row=2, col=1)
                efig.update_layout(height=560, template=template, margin=dict(l=10, r=10, t=10, b=10), showlegend=False)
                st.markdown(f"**到期日 × 履約價(2D)|{ttl}**")
                st.plotly_chart(efig, width="stretch")
                st.caption("每一欄是一個到期日,每一列是一個履約價分組(NQ 點),顏色是該格的 GEX($M / 1%),實線是 NQ 現價、點線是 flip。"
                           "下方長條是該到期日整條選擇權鏈的合計(不受畫面上下範圍限制)。只包含 collector 抓取的到期日(7 天內),"
                           "看不到更遠月份;顏色和正負的解讀與主熱圖相同(依左側的來源、權重基準、到期日範圍與鏡頭)。")

    st.caption(
        "模型 GEX = 部位權重 × 即時重算的 gamma(價格、IV、到期時間)。權重可選前一日未平倉量(OI,看不到盤中新增部位)"
        "或今日累計成交量(Volume,反映今天的流量,但沒有買賣方與 dealer/客戶之分)。假設 dealer 做多 call、做空 put(慣例,非已驗證事實)。"
        "QQQ、NDX 的履約價以同一時刻的價格比例換算成 NQ 點位。Call wall = 所選範圍內 Call 權重最大的履約價分組;Put wall 取 Put 權重絕對值最大者;"
        "帶 = 從牆往兩側延伸、權重不低於峰值門檻的連續分組;Flip = Net GEX 曲線穿越零的價位(假設 IV 固定)。"
        "前後對比面板:把本幀和比較幀逐個履約價對比,右邊增加、左邊減少;中間欄的短橫線是比較幀的位置,陰影標出變化最大的幾個履約價。"
        "成交量是當天累計值,只會增加,所以「新增成交量」不會有減少;想看增減請用「權重變化」或「新增成交的增減」。"
        "成交量不分買賣方向,也不知道是開倉還是平倉;跨商品比較請用名目金額或 Gamma 加權,口數不能直接比。"
        "Abs GEX 鏡頭 = 每個履約價 |Call| 與 |Put| 的 gamma 權重相加,不假設 dealer 多空,看的是哪個價位的 gamma 最集中(釘住力);它不告訴你方向。"
        "美股選擇權休市時只有 NQ 有資料,所以多來源的合計在盤內外之間會跳動;想看連續的走勢,請改看單一商品。"
        "這些定義是本工具自訂的,與 SpotGamma、Barchart 不一定相同。")
    return replay


c = controls()
replay_mode = render(c)

if replay_mode is not None:
    if replay_mode:
        if c["rp_play"]:
            time.sleep(float(c["rp_speed"]))
            st.session_state["_rp_next"] = True
            st.rerun()
    elif c["auto"]:
        time.sleep(c["every"])
        st.rerun()
elif c["auto"] and not c["rp_on"]:
    time.sleep(c["every"])
    st.rerun()
