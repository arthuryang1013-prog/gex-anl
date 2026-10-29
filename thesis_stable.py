# -*- coding: utf-8 -*-
"""thesis_stable.py — 讓「盤面指標」的方向標籤變穩:平滑 + 中性區 + 遲滯。純函式,不碰資料庫與網路。

問題:舊標籤是「分數 >= 0 就偏多、否則偏空」,分數在 0 附近晃一下標籤就翻,每幾分鐘一套說法,是雜訊。
做法(**預設參數是我先訂的,還沒有用真資料調過**,用 thesis_stability.py 看翻轉次數再調):
  1) 對分數做指數平滑(ALPHA);
  2) 平滑後分數絕對值 < T_OUT 視為「中性」;要從中性進入偏多/偏空,平滑分數要連續 K 個快照 >= T_IN;
  3) 已經偏多時,要掉到 < T_OUT 才回中性,不能直接翻成偏空(反之亦然)。
這只會讓標籤變穩,**不會讓它變準**;是否有預測力要靠 thesis_outcome.py 另外驗證。
"""
ALPHA = 0.3     # 約看最近 3 個快照(每個約 3 分鐘)
T_IN = 20.0     # 進入偏多/偏空:平滑分數(-100~100)絕對值要達到
T_OUT = 8.0     # 離開偏多/偏空回中性:絕對值掉到低於
K_IN = 2        # 連續幾個快照達標才進入


def stabilize(series, alpha=ALPHA, t_in=T_IN, t_out=T_OUT, k=K_IN):
    """series: [(t, score)],依時間排序,score 為 -100~100。回傳 list[dict(t, score, ema, label, since)]。
    label ∈ 偏多 / 偏空 / 中性;since = 目前這個標籤開始的時間。"""
    out = []
    ema = None
    state, since = "中性", None
    run_dir, run_n = None, 0
    for (t, s) in series:
        if s is None:
            continue
        ema = s if ema is None else alpha * s + (1 - alpha) * ema
        if since is None:
            since = t
        want = "偏多" if ema >= t_in else "偏空" if ema <= -t_in else None
        if want is not None and want == run_dir:
            run_n += 1
        elif want is not None:
            run_dir, run_n = want, 1
        else:
            run_dir, run_n = None, 0
        new = state
        if state == "中性":
            if run_dir is not None and run_n >= k:
                new = run_dir
        elif state == "偏多":
            if ema < t_out:
                new = "中性"
        elif state == "偏空":
            if ema > -t_out:
                new = "中性"
        if new != state:
            state, since = new, t
        out.append(dict(t=t, score=s, ema=round(ema, 1), label=state, since=since))
    return out


def flips(labels):
    """標籤序列中,『多 ↔ 空 ↔ 中性』切換的次數(相鄰不同就算一次)。"""
    n = 0
    prev = None
    for x in labels:
        if prev is not None and x != prev:
            n += 1
        prev = x
    return n


def legacy_label(score):
    return "偏多" if score >= 0 else "偏空"
