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
    max_position_size_quote: 140
    max_open_executors: 2
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
  "not supplied": it prints `budget : 0.00 quote ... — NOT SUPPLIED`, reports
  `exit_liquidity : UNVERIFIED (NOT_SIZED)` because a zero requirement is met by any
  book, and builds **no ladder at all**. You get a diagnosis, never an entry.
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
block. When any of those fails the routine prints its reason instead —
`No ladder produced — the decision is HOLD.`, or `No ladder emitted — <reason>`
naming the gate that stopped it. No block means no entry — see rule 9.

| Field | Meaning |
|---|---|
| `decision` | `LONG`, `SHORT` or `HOLD` — already gated on regime and threshold |
| `score` | Blended signal in [-1, 1] |
| `entry_gate` | `both` / `slow` / `fast` / `none` / `halt` |
| `exit_liquidity` | Rendered as `VERIFIED OK` or `VERIFIED THIN` (the book was read *and* judged against your exit size) or `UNVERIFIED (<CODE>)` (no verdict — the book was not read, or was read against no size at all; `<CODE>` says which). `VERIFIED OK` means the depth was measured, not that the venue will accept the close. Match the **whole** prefix: `UNVERIFIED` contains `VERIFIED` as a substring, and reading one for the other turns the blocking state into the passing one |
| `slow_regime`, `fast_regime` | `TRENDING` / `RANGING` / `EXTREME` |
| `components` | Per-feature contributions: cfi, vwap, trend, di, funding |
| `natr_pct`, `vol_multiplier` | Live volatility and the resulting scaling factor |
| `rsi` | Overbought/oversold reading |
| JSON block | `side`, `prices`, `amounts_quote`, `leverage`, `stop_loss`, `take_profit`, `time_limit`, `mode` — and deliberately **no order-type keys**. `dca_executor` has no `triple_barrier_config` and no `*_order_type` field, so one added there is dropped in silence. Its rungs rest as plain LIMIT and **every** barrier exit — take-profit, stop-loss, time limit — closes MARKET |

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
5. **`exit_liquidity` carries no verdict** — `UNVERIFIED (<CODE>)` → the book was
   not read, or was read against no exit size, so you have **no** liquidity reading
   either way. **Do not open**, whatever the code (rule 9 covers the ladder itself).
   Then read the `<CODE>`: it decides what you journal and whether anything belongs
   in `learnings.md`. See *What the UNVERIFIED codes mean* below — the codes do not
   share one cause, so do not assume last week's.
6. **`exit_liquidity` is `VERIFIED THIN`** → do not open. The book was measured and
   does not hold your exit size, so the entry would fill into a position the book
   cannot absorb on the way out. Journal the reported figure. If it stays thin for
   several ticks **with a measured figure in the note**, say so — that is a real
   depth reading, and the size or the pair is worth a line in `learnings.md`. An
   unread book is not a thin book; see rule 5.
7. **At `max_open_executors`, or an active executor on the same side** → do not
   stack. Journal and wait.
8. **Opposite-side executor active** → do not hedge yourself. Let it finish.
9. **No `READY-TO-SUBMIT dca_executor FIELDS` block in the routine output** → do not
   open, whatever the reason and however many gates you cleared. This is absolute:
   never submit, improvise, hand-size or reconstruct a ladder the routine did not
   emit — not from the prices in the text block, not from a previous tick's block,
   not from an older build that still printed one. The routine withholds the block
   for a reason it prints: `No ladder produced — the decision is HOLD.`, otherwise
   `No ladder emitted — <reason>`, which names the gate — an exit book that is not
   `VERIFIED OK`, including `NOT_SIZED` when no budget was forwarded.
   Journal that reason in one line; if it is a missing config key, fix Step 1 next
   tick rather than filling the gap yourself.
10. **Otherwise** → open the ladder per Step 4.

### What the `UNVERIFIED` codes mean

Rule 5 blocks the entry for every code. The code decides the diagnosis:

- `BACKEND_UNREACHABLE` / `VENUE_UNREACHABLE` / `API_ERROR` → a fact about the
  **connection**, not about the venue: the API server could not reach the
  exchange, or the call failed in transit. The routine already prints a
  `connectivity` line naming the server and saying which of the order-book,
  funding and candle probes answered — read that instead of spending a tool call
  re-deriving it, and do **not** re-run the signal against another connector.
  Journal it once per outage rather than once per tick, do **not** switch connector
  or pair, and do **not** conclude in `learnings.md` that this venue or pair is
  unsupported.
- `TRANSPORT_TIMEOUT` → the probe was accepted and never answered. Not an outage on
  its own: the one time it fired for real the server could reach the venue and the
  pair had simply never been **subscribed** — local tracker state, not a connection
  fact. (A hang stringifies to nothing, so the message arrives empty; that emptiness
  says nothing.) The routine already tests that cheap cause: on this code only it
  runs one `add_trading_pair` plus one retry and prints the outcome on an
  `order-book heal  :` line in the connectivity block — **take that line's verdict**.
  It prints `RECOVERED` when the retry actually read a book — the subscription *was*
  the cause. Otherwise it prints `NOT RECOVERED`, and read the rest of that line
  rather than inferring: an accepted `add_trading_pair` followed by a second timeout
  makes an unsubscribed tracker **less likely but does not disprove it**, because an
  accepted call is not proof the feed came up — the timeout stays **unresolved**, so
  do not promote it to a fault "further along the path" or blame the server's link to
  the venue. Settle it with `get_order_book_diagnostics(<connector>)` (`tracker_ready`,
  `websocket_status`, `trading_pairs`) before concluding anything. Do not ask an
  operator for `add_trading_pair`: this tick already ran it. The line prints on a **healthy** tick too — connectivity OK because the retry
  worked — and that tick is the proof the pair was unsubscribed; the subscription is
  runtime state an API container restart loses. A subscription finding is worth a
  line in `learnings.md`; an outage or "this venue cannot serve depth" is not.
- `EMPTY_BOOK` → the venue **answered** and returned no bids or no asks. That is a
  fact about this read, and it blocks the entry. Before recording it as a property
  of the pair, check `get_order_book_diagnostics(<connector>)`: a tracker that was
  never subscribed or has only just come up can also answer empty, and the
  routine's self-heal does **not** run for this code. If it repeats with the
  tracker ready and the pair subscribed, that **does** belong in `learnings.md` as
  a statement about this pair on this venue.
- `BAD_REQUEST` (HTTP 400/404/422) → the API answered and **rejected** the
  request: for that endpoint the connector name or the pair genuinely may be
  wrong. Support is per **endpoint**, not per connector — `derive_perpetual` is
  in the trading catalogue and serves funding and (once the pair is subscribed)
  order-book depth, while its candles endpoint returns HTTP 400. Journal which
  endpoint rejected which connector and pair, and record that in `learnings.md`.
  Retrying will not fix it; a config change will. If it is the **candle** feed that
  was rejected, fix it with `candle_connector` / `candle_trading_pair` in Step 1 —
  never by moving the trade. Do **not** change the ACTIVE `connector_name` or
  `trading_pair` yourself: `[CURRENT CONFIG]` outranks you, so keep executing
  there and say the config needs an operator fix.
- `MALFORMED_BOOK` → a defect in **our own parser**, not an outage and not a
  venue fact. Journal it as a routine bug with the reported detail and conclude
  nothing about the venue, the pair or the market.
- `NOT_SIZED` → the book **was** read, but you sent no `total_amount_quote`, so
  there was no exit size to test it against and nothing about depth is verified
  (any book clears a zero requirement). This one is yours to fix: forward the
  budget in Step 1 next tick. Conclude nothing about the venue or the pair.
- `UNKNOWN` / `NOT_RUN` → unclassified. Journal the code and detail verbatim and
  conclude nothing.

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
split can land a hair over). Over by more than that means a larger figure than the
session's `total_amount_quote` was forwarded in Step 1, or the split itself is wrong
— re-run Step 1 with the ACTIVE value rather than editing the JSON by hand. If the
re-run returns the same over-budget numbers, HOLD and journal the figures: that is a
routine bug, not something to hand-trim.

Do not recompute prices or barriers — they are already volatility-scaled and
fee-floored. If the routine's prices violate the direction rule against the live
price, HOLD and journal it rather than "fixing" them.

Do not add **order-type** fields — no `triple_barrier_config`, no
`take_profit_order_type`. `dca_executor` accepts neither: the extra key is discarded
without an error and the create still reports success, so adding one buys you a
post-only exit the executor never places. Take-profit, stop-loss and time limit all
close with MARKET orders; that is the executor, not a default you can override. A
`triple_barrier_config` example you meet elsewhere is for `position_executor` or
`grid_executor` — those config classes really carry it — and it does not transfer
here.

This does **not** apply to the call fields `manage_executors` requires around the
block — `connector_name`, `trading_pair`, `controller_id`, `executor_type`. Those are
not part of the routine's JSON and you must still supply them; see AGENT.md.
`controller_id` is your `agent_id` and never `"main"`: it is what isolates your
executors and P&L, so check what the create returns and journal it if it comes back
as anything else.

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

One line per tick via `trading_agent_journal_write`, except where this playbook
says to journal once per condition instead:

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
