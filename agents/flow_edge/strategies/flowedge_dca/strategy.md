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
  connector_name: derive_perpetual
  trading_pair: XRP-USDC
  candle_connector: hyperliquid_perpetual
  candle_trading_pair: XRP-USD
  leverage: 2
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
"candle_connector": ..., "candle_trading_pair": ..., "total_amount_quote": ...,
"leverage": ...}`. Send all six. The routine's own defaults are placeholders, not
your config, and each omission has its own consequence:

- Omit `total_amount_quote` and it defaults to **0**, which the routine reads as
  "not supplied": it builds **no ladder at all** and prints
  `budget : 0.00 quote ... — NOT SUPPLIED`. You get a diagnosis, never an entry.
- Omit `leverage` and it defaults to **1**, and that 1 is written verbatim into the
  JSON block you are told to submit unedited — a 1x executor against a 2x session.
- Omit `candle_connector` and the candle feed falls back to `connector_name`. That
  is not harmless: `derive_perpetual` serves no candles, so the routine returns
  `candle fetch failed ... BAD_REQUEST: HTTP 400` and HOLDs **every** tick. If
  `[CURRENT CONFIG]` carries no candle keys (older sessions predate them), pass the
  proxy feed `hyperliquid_perpetual` / `XRP-USD` — `binance_perpetual` /
  `XRP-USDT` also works — and journal once that the session config is missing them.
- `candle_trading_pair` falls back to `trading_pair`, which is safe only when the
  proxy venue quotes that exact pair. Send it whenever the config carries it.

`[CURRENT CONFIG]` outranks any connector or pair named in the free-text session
context; where they disagree the typed config wins and the context is stale.

It returns a text block. It appends a JSON object under `READY-TO-SUBMIT
dca_executor FIELDS` **only** when all four of these hold: `decision` is not HOLD,
`exit_liquidity` is `VERIFIED OK`, a budget was supplied, and the execution venue
returned a live price. `VERIFIED THIN` is a real measurement and still yields no
block. When any of those fails the routine prints a `No ladder emitted — <reason>`
line instead: read it, it names the gate that stopped it. No block means no entry —
see rule 9.

| Field | Meaning |
|---|---|
| `decision` | `LONG`, `SHORT` or `HOLD` — already gated on regime and threshold |
| `score` | Blended signal in [-1, 1] |
| `entry_gate` | `both` / `slow` / `fast` / `none` / `halt` |
| `exit_liquidity` | Rendered as `VERIFIED OK` or `VERIFIED THIN` (the book was read and measured) or `UNVERIFIED (<CODE>)` (the book was not read at all; `<CODE>` says why — older builds spell it `unknown (...)`). Match the **whole** prefix: `UNVERIFIED` contains `VERIFIED` as a substring, and reading one for the other turns the blocking state into the passing one |
| `slow_regime`, `fast_regime` | `TRENDING` / `RANGING` / `EXTREME` |
| `components` | Per-feature contributions: cfi, vwap, trend, di, funding |
| `natr_pct`, `vol_multiplier` | Live volatility and the resulting scaling factor |
| `rsi` | Overbought/oversold reading |
| JSON block | `side`, `prices`, `amounts_quote`, `leverage`, `stop_loss`, `take_profit`, `time_limit`, `mode` |

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
   `unknown (<note>)` from an older routine build → the book was never read, so you
   have **no** liquidity reading. **Always: do not open**, whatever the code (rule 9
   covers the ladder itself). What the `<CODE>` means, and therefore what you
   journal and what may be written to `learnings.md`, differs by code — read the
   code you were handed rather than assuming last week's cause:

   - `BACKEND_UNREACHABLE` / `VENUE_UNREACHABLE` / `TRANSPORT_TIMEOUT` /
     `API_ERROR` → a fact about the **connection**, not about the venue: the API
     server could not reach the exchange, or did not answer in time. The routine
     already prints a `connectivity` line naming the server and saying which of the
     order-book, funding and candle probes answered — read that instead of spending
     a tool call re-deriving it, and do **not** re-run the signal against another
     connector. Journal once per outage (Error recovery step 3), do **not** switch
     connector or pair, and do **not** write a venue/pair/connector-name conclusion
     into `learnings.md`.
   - `EMPTY_BOOK` → the venue **answered** and returned no bids or no asks. That is
     a fact about the venue and the pair, and the strongest do-not-trade-this-pair
     evidence there is. Journal it as such; if it repeats, that **does** belong in
     `learnings.md` as a statement about this pair on this venue.
   - `BAD_REQUEST` (HTTP 400/404/422) → the API answered and **rejected** the
     request: for that endpoint the connector name or the pair genuinely may be
     wrong. Support is per **endpoint**, not per connector — `derive_perpetual` is
     in the trading catalogue and serves funding, while its candles endpoint returns
     HTTP 400 and its order book never answers. Journal which endpoint rejected
     which connector and pair, and record that in `learnings.md`. Still do **not**
     change the ACTIVE `connector_name` or `trading_pair` yourself — `[CURRENT
     CONFIG]` outranks you; say the config needs an operator fix.
   - `MALFORMED_BOOK` → a defect in **our own parser**, not an outage and not a
     venue fact. Journal it as a routine bug with the reported detail and conclude
     nothing about the venue, the pair or the market.
   - `UNKNOWN` / `NOT_RUN` → unclassified. Journal the code and detail verbatim and
     conclude nothing.
6. **`exit_liquidity` is `VERIFIED THIN`** → do not open. The entry would fill and
   the exit would be refused, leaving a position you cannot close. Journal the
   reported figure. If it stays thin for several ticks **with a measured figure in
   the note**, say so — that is a real depth reading, and the size or the pair is
   worth a line in `learnings.md`. An unread book is not a thin book; see rule 5.
7. **At `max_open_executors`, or an active executor on the same side** → do not
   stack. Journal and wait.
8. **Opposite-side executor active** → do not hedge yourself. Let it finish.
9. **No `READY-TO-SUBMIT dca_executor FIELDS` block in the routine output** → do not
   open, whatever the reason and however many gates you cleared. This is absolute:
   never submit, improvise, hand-size or reconstruct a ladder the routine did not
   emit — not from the prices in the text block, not from a previous tick's block,
   not from an older build that still printed one. The routine withholds the block
   for a reason it states on the `No ladder emitted — <reason>` line (HOLD, exit
   book not `VERIFIED OK`, no budget forwarded, no live execution-venue price).
   Journal that reason in one line; if it is a missing config key, fix Step 1 next
   tick rather than filling the gap yourself.
10. **Otherwise** → open the ladder per Step 4.

### Skip-tick conditions

Do nothing and journal one line when: the routine could not reach the API or is
short of candles; `entry_gate` is `none`; unrealised drawdown is near
`max_drawdown_pct`; or you opened an executor within the last 3 minutes.

## Step 4 — Execute

Create the executor with `manage_executors`, passing the routine's JSON **verbatim**
— including its `leverage`. You reach this step only with a block in hand; if there
is none, rule 9 applies and there is nothing to submit.

First check `sum(amounts_quote) <= total_amount_quote`, allowing 0.01 quote of
rounding slack (the routine rounds each rung to 4 dp independently, so an exact
split can land a hair over). Over by more than that means you did not forward
`total_amount_quote` in Step 1 — re-run Step 1 with it rather than editing the JSON
by hand. If the re-run returns the same over-budget numbers, HOLD and journal the
figures: that is a routine bug, not something to hand-trim.

Do not recompute prices or barriers — they are already volatility-scaled and
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
