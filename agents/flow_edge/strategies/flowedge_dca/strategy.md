---
name: FlowEdge DCA
description: Tick playbook — run the flow_edge_signal routine, gate its decision against
  open executors and held positions, and open a volatility-scaled 3-level maker DCA
  ladder when it clears.
agent_key: claude-code
skills: []
default_config:
  server_name: local
  frequency_sec: 60
  total_amount_quote: 70
  execution_mode: loop
  max_ticks: 0
  risk_limits:
    max_position_size_quote: 300
    max_open_executors: 4
    max_drawdown_pct: 8
default_trading_context: Trade XRP-USDC on derive_perpetual, 2x leverage, one-way
  mode.
created_by: 5775815348
created_at: '2026-05-17T08:15:43.839293+00:00'
---

# FlowEdge DCA — Tick Playbook

## Objective

Take directional entries only when a trend regime is confirmed, size them to live
volatility, and exit through the executor's own barriers. Never exceed
`total_amount_quote` per entry or the configured risk limits.

---

## Step 1 — Run the analysis routine

Call the `flow_edge_signal` routine, forwarding the ACTIVE values from
`[CURRENT CONFIG]`: `config={"connector_name": ..., "trading_pair": ...,
"candle_connector": ..., "candle_trading_pair": ..., "total_amount_quote": ...}`.
Send every one of those keys the config carries (the candle pair is optional and
falls back to the execution pair). The routine's own defaults are placeholders, not
your config — omit `total_amount_quote` and it sizes the ladder to 100 quote and
judges exit depth against 100, whatever your budget is. `[CURRENT CONFIG]` also
outranks any connector or pair named in the free-text session context; where they
disagree the typed config wins and the context is stale.

It returns a text block and, when the decision is not HOLD **and** the exit book was
measured, a JSON object under `READY-TO-SUBMIT dca_executor FIELDS`:

| Field | Meaning |
|---|---|
| `decision` | `LONG`, `SHORT` or `HOLD` — already gated on regime and threshold |
| `score` | Blended signal in [-1, 1] |
| `entry_gate` | `both` / `slow` / `fast` / `none` / `halt` |
| `exit_liquidity` | `OK` / `THIN` measure the book; `UNVERIFIED (<CODE>)` — older builds spell it `unknown (...)` — means the probe never returned and says nothing about the venue |
| `slow_regime`, `fast_regime` | `TRENDING` / `RANGING` / `EXTREME` |
| `components` | Per-feature contributions: cfi, vwap, trend, di, funding |
| `natr_pct`, `vol_multiplier` | Live volatility and the resulting scaling factor |
| `rsi` | Overbought/oversold reading |
| JSON block | `side`, `prices`, `amounts_quote`, `stop_loss`, `take_profit`, `time_limit`, `mode` |

If the routine returns an error or a warm-up message, **HOLD** and journal one line.
Never improvise a signal from raw candles.

## Step 2 — Read your own state

From `[CORE DATA]`: active executors tagged with your `controller_id`, and any **held
position** left behind by an executor that closed with `keep_position`.

## Step 3 — Decide

First match wins.

1. **Any active executor past -5% net PnL** → stop it now. Nothing else this tick.
2. **Held position exists** → managing it outranks any new entry. See below.
3. **`decision` is HOLD** → do nothing, journal the reason in one line.
4. **`entry_gate` is `halt`** → the slow frame is `EXTREME`. Open nothing until it
   clears. `EXTREME` on the *fast* frame alone is not a halt.
5. **`exit_liquidity` is not a measurement** — `UNVERIFIED (<CODE>)`, or
   `unknown (<note>)` from an older routine build → the probe never returned, so you
   have **no** liquidity reading. Do not open; the routine withholds the
   READY-TO-SUBMIT block in this state, and if an older build still prints one,
   ignore it — never submit or reconstruct a ladder you cannot verify an exit for.
   This is a fact about the **connection**, not about the venue: an empty or
   timed-out note means the API server could not reach the exchange at all. Before
   blaming a venue, a pair or a connector name, verify reachability yourself with one
   read-only call on a different, known-liquid connector. If that also fails the
   backend is unreachable — journal it once per Error recovery step 3, do **not**
   switch connector or pair, and do **not** write a venue/pair/connector-name
   conclusion into `learnings.md`.
6. **`exit_liquidity` is THIN** → do not open. The entry would fill and the exit
   would be refused, leaving a position you cannot close. Journal the reported
   figure. If it stays thin for several ticks **with a measured figure in the note**,
   say so — that is a real depth reading, and the size or the pair is worth a line in
   `learnings.md`. A missing or empty note is not a thin book; see rule 5.
7. **At `max_open_executors`, or an active executor on the same side** → do not
   stack. Journal and wait.
8. **Opposite-side executor active** → do not hedge yourself. Let it finish.
9. **Otherwise** → open the ladder per Step 4.

### Skip-tick conditions

Do nothing and journal one line when: the routine could not reach the API or is
short of candles; `entry_gate` is `none`; unrealised drawdown is near
`max_drawdown_pct`; or you opened an executor within the last 3 minutes.

## Step 4 — Execute

Create the executor with `manage_executors`, passing the routine's JSON **verbatim**.
First check `sum(amounts_quote) <= total_amount_quote`. If it is over, you did not
forward the config in Step 1 — re-run Step 1 with it rather than editing the JSON by
hand. Do not recompute prices or barriers — they are already volatility-scaled and
fee-floored. If the routine's prices violate the direction rule against the live
price, HOLD and journal it rather than "fixing" them.

---

## Managing a held position

When an executor stops with inventory retained it appears in `[CORE DATA]` with a
breakeven price. It is yours until you close it.

1. Compare `breakeven` against the current price from the routine output.
2. **Within 0.3% of breakeven or better** → close it with an `order_executor`.
3. **Underwater by more than 0.3%, signal still agrees** → hold and wait for the
   recovery.
4. **Underwater and the signal has flipped** → close now. A stale position against
   the trend is the most expensive thing you can hold.
5. Never open a new ladder while a held position is open.

---

## Journaling

Exactly one line per tick via `trading_agent_journal_write`:

`Tick #N: <ACTION> — <one-clause reason>. score <x>, gate <y>, regime <z>. [exposure]`

Append to `learnings.md` only when you observe something that will still be true next
session. Do not journal restatements of the routine output, and when the API is down
journal it **once**, not once per tick.

## Error recovery

1. On a failed executor create, call `manage_executors(executor_type="dca_executor")`
   for the live schema, compare against what you sent, fix, and retry **once**.
2. If it fails again, HOLD and journal the exact error.
3. If the API is unreachable, HOLD and journal once — never retry in a tight loop.
   After 10 consecutive unreachable ticks send one `send_notification`, then stop.
