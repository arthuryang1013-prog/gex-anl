# Bookmap Python API — 第 0 步測試用 addon
# 目的：確認 (1) addon 跑得起來 (2) 讀得到 MBO（逐筆訂單）資料
# 用法：在 Windows 的 Bookmap 裡跑這支，然後在 Bookmap 開一張 NQ 圖，
#       看輸出有沒有印出 "MBO event=..." 就代表 MBO 通了。
import bookmap as bm

MAX_LINES = 20        # 每種事件最多印幾行，避免洗版
depth_n = 0
mbo_n = 0


def handle_subscribe_instrument(addon, alias, full_name, is_crypto, pips,
                                size_multiplier, instrument_multiplier, supported_features):
    print("=== 訂閱到商品: " + str(alias) + " (" + str(full_name) + ") ===", flush=True)
    print("pips=" + str(pips) + "  size_multiplier=" + str(size_multiplier) +
          "  is_crypto=" + str(is_crypto), flush=True)
    # 同時訂 depth 與 MBO：有些資料源只給其中一種，兩個都訂最保險
    bm.subscribe_to_depth(addon, alias, 1)
    bm.subscribe_to_mbo(addon, alias, 2)


def handle_unsubscribe_instrument(addon, alias):
    print("=== 取消訂閱: " + str(alias) + " ===", flush=True)


def handle_depth(addon, alias, is_bid, price_level, size_level):
    global depth_n
    if depth_n < MAX_LINES:
        print("DEPTH " + ("BID" if is_bid else "ASK") +
              "  price=" + str(price_level) + "  size=" + str(size_level), flush=True)
        depth_n += 1


def handle_mbo(addon, alias, event_type, order_id, price_level, size):
    global mbo_n
    if mbo_n < MAX_LINES:
        print("MBO  event=" + str(event_type) + "  order_id=" + str(order_id) +
              "  price=" + str(price_level) + "  size=" + str(size), flush=True)
        mbo_n += 1


if __name__ == "__main__":
    addon = bm.create_addon()
    # 先註冊 handler 再開始，避免漏掉第一批事件
    bm.add_depth_handler(addon, handle_depth)
    bm.add_mbo_handler(addon, handle_mbo)
    bm.start_addon(addon, handle_subscribe_instrument, handle_unsubscribe_instrument)
    bm.wait_until_addon_is_turned_off(addon)
