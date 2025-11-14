# BOT-2025 Runner & Messaging Flow

This README explains **how prices are polled**, **how trade decisions are made & executed**, and **exactly when Discord messages are sent** in the current setup.

---

## High-Level Overview

```
MetaTrader5 (ticks)
      │
      ▼
 PriceManager ──► Start Price Cache (daily anchors)
      │
      ▼
  runner.py  ──► builds PriceComponent for each symbol every N seconds
      │                      │
      │                      └─► PriceComponent computes pips, ratio, stages
      │                                      │
      │                                      └─► (notify at stage 1 & 2 to Discord: info)
      ▼
 evaluate_threshold()  (threshold_logic.py)
      │
      ▼
 execute_threshold_decision()  ──► place_trade()/close_*() (mt5/trade/trade.py)
                                         │
                                         └─► MT5 order_send(...)
```

---

## Key Files & Responsibilities

* **`runner.py`**

  * Main loop: polls current prices, maintains anchors, creates `PriceComponent`, triggers decisions.
  * Resets per-symbol notification stages on daily anchor rollover.

* **`common/price_component.py`**

  * Holds the price snapshot for one symbol (start/current/high/low).
  * Computes derived metrics:

    * `pips_moved`, `direction`, `strong_direction`, `threshold_ratio`.
  * **Messaging** (Info channel):

    * `notify_threshold_if_hit(last_stage_sent)` sends once when `threshold_ratio` crosses **1×** (stage 1) and **2×** (stage 2).

* **`price_manager.py`**

  * Fetches current prices from MT5.
  * Maintains and refreshes **daily start prices** (anchors).

* **`threshold_logic.py`**

  * Implements **when** to place/close trades.
  * Returns a `TradeDecision` consumed by the runner.
  * Calls the trade executor in response to decisions.

* **`mt5/trade/trade.py`**

  * **The only place that touches MT5** to place/close orders.
  * Uses `mt5.order_send(...)` with FOK/Limits per config.

* **`notify.py`**

  * Thin wrapper over Discord webhooks.
  * Channels supported: `normal`, `info`, `critical`.

---

## Runtime Loop (runner.py)

1. **Initialize MT5**

   * `init_mt5("runner.py")` (may send a startup message depending on your impl.)
2. **Warm Anchors**

   * `PriceManager.maybe_refresh_all_start_prices(symbols)` on boot.
3. **Main Loop (every `interval_sec`, default 1s)**

   * For each symbol:

     1. `current_price = PriceManager.get_current_price(symbol)`
     2. Resolve start anchor (refresh if missing)
     3. Query highs/lows since anchor (best-effort)
     4. Build `PriceComponent(symbol, start_price, current_price, latest_high, latest_low)`
     5. **Notify stage** via `PriceComponent.notify_threshold_if_hit(last_stage_sent[sym])`
     6. Compute `decision = evaluate_threshold(pc, is_position_open)`
     7. `execute_threshold_decision(decision)` → trade executor
   * Sleep `interval_sec`, repeat
4. **Daily Rollover**

   * If `PriceManager.start_price_is_due_to_roll()` is true:

     * Refresh all anchors
     * **Reset** `last_stage_sent[sym] = 0` (so you’ll get fresh stage notifications next day)

---

## Messaging: What is sent, and When

### 1) Threshold Stage Hit (Info channel)

* **Where:** `common/price_component.py`
* **API:** `notify_threshold_if_hit(last_stage_sent: int, min_stage=1) -> int`
* **When it fires:**

  * **Stage 1**: When `threshold_ratio ≥ 1.0` (first threshold reached)
  * **Stage 2**: When `threshold_ratio ≥ 2.0` (second threshold reached)
  * Sends **only once per stage per symbol** until daily reset (or manual reset).
* **Channel:** `info`
* **Example message:**

  ```
  [XAUUSD] threshold x1 hit | start=2410.5 cur=2412.0 pips=15.0 dir=UP strong=UP ratio=1.0
  ```

### 2) Trade Execution (Normal/Critical channel)

* **Where:** typically inside `threshold_logic.execute_threshold_decision(...)` and/or `mt5/trade/trade.py` after MT5 response.
* **Recommended behavior:**

  * On successful order: send to **`normal`** with symbol, side, volume, price, and order id.
  * On failure/reject/error: send to **`critical`** with MT5 error code/details.
* **Sample (suggested):**

  ```python
  # after a successful BUY
  send_discord_message("normal", f"[BUY OK] {symbol} vol={vol} price={price} ticket={result.order}")

  # on failure
  send_discord_message("critical", f"[BUY FAIL] {symbol} code={code} details={details}")
  ```

### 3) Startup / Rollover (Normal/Info channel)

* **Where:** `runner.py` / `price_manager.py` / `mt5.init_mt5()`
* **Recommended behavior:**

  * On MT5 init success: **`normal`** (or `info` if you prefer quieter logs)
  * On anchor rollover: **`info`** with date and symbols

---

## Sequence: From Price to Trade

```
for each symbol every second:
  current = PriceManager.get_current_price(symbol)
  start   = PriceManager.get_start_price(symbol)
  highs/lows since start (best-effort)
  pc = PriceComponent(...)

  # Messaging: stage notifications
  last_stage_sent[symbol] = pc.notify_threshold_if_hit(last_stage_sent[symbol])

  # Decision & execution
  decision = evaluate_threshold(pc, is_position_open)
  execute_threshold_decision(decision)
    └─► (on success) send_discord_message('normal', ...)
    └─► (on error)   send_discord_message('critical', ...)
```

---

## Resetting Stage Notifications

* The runner resets `last_stage_sent[symbol] = 0` on daily anchor rollover.
* If you **close trades** and want to restart stage notifications intra-day, you can also reset when your close logic completes (optional):

```python
# after CLOSE ALL for a symbol
last_stage_sent[symbol] = 0
```

---

## Operational Notes

* Ensure `.env` contains valid webhooks:

  * `DISCORD_WEBHOOK_INFO`, `DISCORD_WEBHOOK_NORMAL`, `DISCORD_WEBHOOK_CRITICAL`
* `notify.py` validates Discord webhook shape; logs helpful errors if missing/invalid.
* If your broker/server returns no tick data for a symbol, the runner safely **skips that symbol** this tick.
* On exceptions in extremes fetching, the loop continues with current price as both high & low (fail-safe).

---

## Quick Start

```bash
python main.py
```

* The runner will start polling prices and sending stage notifications (info) when thresholds are crossed.
* Trade placement is governed by your `threshold_logic.py` and MT5 executor in `mt5/trade/trade.py`.

---

## Where to Look When Debugging

* **No stage messages?** Check that `SYMBOL_CONFIGS` has correct `pip_size` and `threshold_pips` for the symbol.
* **Duplicate messages?** Verify `last_stage_sent` is carried across ticks and only reset on daily rollover or explicit close.
* **No trades firing?** Inspect `evaluate_threshold(...)` return values and the bridge to `execute_threshold_decision(...)`.
* **MT5 errors?** Look at logs from `mt5/trade/trade.py` and ensure the terminal is connected, symbol is selected, and volume is valid.
