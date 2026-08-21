# Alpaca-to-MetaTrader 5 Copy Trader

Copies completed Alpaca **paper** equity fills to one explicitly configured
MetaTrader 5 terminal. The application is deliberately conservative: it is
long-only, uses a reviewed U.S. stock/ETF allow-list, and pauses rather than
guessing when broker state is uncertain.

The default and recommended setup is **Alpaca paper -> MT5 demo**. A live MT5
target is an explicit local opt-in and is outside the smoke-test workflow;
review its risks, account mode, and broker permissions independently before
starting a copier.

## What it does

- Copies only Alpaca `fill` and `partial_fill` events.
- Sizes target orders by the target/source equity ratio and the MT5 contract
  specification.
- Copies buys and closes only; it never opens a short position.
- Uses a reviewed snapshot of U.S. stocks and ETFs that are full-trade USD
  instruments at both brokers.
- Rejects a copy when the current MT5 quote differs too far from the Alpaca
  fill price (0.50% by default).
- Waits briefly for a fresh positive MT5 quote, then uses that same quote for
  sizing, order validation, and order submission.
- Records executions, allocations, pauses, and reconciliation data in SQLite.
- Pauses on ambiguous execution, connection loss, position drift, unavailable
  prices, closed symbols, or invalid mappings.

## Before you begin

You need:

- Python 3.12 (64-bit).
- An Alpaca **paper** account and its API credentials.
- A locally installed, logged-in MT5 terminal for the intended account.
- A separate checkout, MT5 installation, durable state directory, and log
  directory for each independent copier.

Never commit `.env`, state databases, or logs. Do not set `ALPACA_*` or
`MT5_*` variables globally in Windows: inherited values override a checkout's
local `.env` and can connect the wrong account.

## Install and configure

From the checkout root:

```powershell
.\scripts\setup.ps1
```

The script creates `.venv`, installs the locked dependencies, creates `.env`
when needed, and restricts `.env` to the current Windows user. Add only this
checkout's credentials to `.env`; do not share its file with another instance.

Set the exact MT5 executable in `config.toml`:

```toml
[mt5]
terminal_path = "C:\\Program Files\\Broker MT5 A\\terminal64.exe"
portable = false
require_demo = true
magic = 10001

[copy]
max_price_deviation_pct = 0.5
quote_acquisition_timeout_seconds = 5
```

`terminal_path` is mandatory. The copier passes this exact path to the MT5
Python API and does not search for, discover, or fall back to another terminal.
Use a distinct positive `magic` value for every independent copier.

`quote_acquisition_timeout_seconds` controls how long a fill waits for a
fresh, positive MT5 bid/Ask when the first quote is missing, zero, or stale.
It defaults to 5 seconds and accepts values from 0 to 10; `0` preserves
fail-fast behavior. The retry cadence is 250 ms and a quote older than 2
seconds is rejected. A timeout pauses the event without creating an MT5 order.
The copier never substitutes the Alpaca fill price for the MT5 price, and it
does not retry a previously paused fill.

## Validate before starting

Run read-only preflight after every configuration, terminal, credentials, or
catalog change:

```powershell
.\.venv\Scripts\python.exe -m copytrader --config .\config.toml preflight
```

Do not start the copier unless every check reports `ok: true`. In particular,
verify the expected Alpaca account, MT5 account/server/path, account mode,
trading permission, reviewed symbol catalog, flat initial positions, and the
absence of pauses or unresolved actions.

Preflight does not place an order.

## Symbol catalog maintenance

The copier uses `symbols.us-stocks-etfs.json`, a broker-specific reviewed
snapshot. It is not hot-reloaded. Refresh it from the terminal configured for
that checkout:

```powershell
# Preview only: no snapshot change
.\.venv\Scripts\python.exe -m copytrader --config .\config.toml symbols sync-us

# After reviewing additions, removals, modifications, and exclusions
.\.venv\Scripts\python.exe -m copytrader --config .\config.toml symbols sync-us --write

# Validate the new snapshot, then restart the copier if it was running
.\.venv\Scripts\python.exe -m copytrader --config .\config.toml preflight
```

Review every added symbol before writing: additions expand the instruments the
copier may trade. Symbols reported as `mt5_not_full_trade` are excluded because
the broker does not currently allow unrestricted opening and closing operations
for them.

## Run, inspect, and recover

After a clean preflight, start the copier interactively:

```powershell
.\.venv\Scripts\python.exe -m copytrader --config .\config.toml run
```

Inspect durable state without starting a stream:

```powershell
.\.venv\Scripts\python.exe -m copytrader --config .\config.toml status
```

On a pause, create a fresh reconciliation plan tied to current broker state,
then apply the exact ID returned by that command:

```powershell
$plan = .\.venv\Scripts\python.exe -m copytrader --config .\config.toml reconcile plan | ConvertFrom-Json
.\.venv\Scripts\python.exe -m copytrader --config .\config.toml reconcile apply $plan.plan_id --yes
```

`[copy].reconciliation_plan_ttl_seconds` controls how long a plan is valid:
300 seconds by default, with a minimum of 60 seconds. `reconcile apply`
rejects expired plans and changed live state; it never invents corrective
trades. It unpauses only when positions already agree with durable copier
state. Preserve `state/` and `logs/`; do not delete them to bypass a pause or
before preflight.

## Demo smoke test

For a paper-to-demo setup only, and only after a clean preflight with flat
accounts:

```powershell
.\.venv\Scripts\python.exe -m copytrader --config .\config.toml smoke-test --symbol AAPL --yes
```

The smoke test submits a bounded Alpaca paper order, verifies its copied MT5
demo deal, closes the paper position, and confirms both accounts return flat.
It can place an MT5 order, so **never use it with a live MT5 target**.

## Two independent copiers

Use the following isolation model:

```text
Alpaca paper account A -> checkout A -> MT5 account A
Alpaca paper account B -> checkout B -> MT5 account B
```

For each checkout, keep separate:

- Alpaca credentials and underlying account ID.
- MT5 installation, terminal path, account, and `magic` value.
- `.venv`, `state/copytrader.db`, `logs/`, and symbol snapshot.
- Scheduled Task name, if using Task Scheduler.

Create clean checkouts rather than copying an active installation:

```powershell
git clone https://github.com/sodiasm/copytrader-alpaca-to-mt5.git copytrader-a
git clone https://github.com/sodiasm/copytrader-alpaca-to-mt5.git copytrader-b
```

Do not share state or logs between instances. API key pairs for the same
underlying Alpaca account are not independent account pairs.

For a new isolated project, clone into a new folder, run `setup.ps1`, then
configure its own credentials, MT5 terminal path, `magic`, catalog, state,
logs, and scheduled-task name. Preview and review its catalog, run preflight,
and start it only after every check is `ok: true`. The paper-to-demo smoke test
is allowed only for a demo MT5 target.

## Portable MT5 recovery

Creating a new project folder does not by itself require portable mode. Keep
`portable = false` when using one existing terminal after the old copier and
terminal are stopped. Use a separate MT5 installation for each concurrent
copier; set `portable = true` only for a dedicated copied MT5 installation
started with `/portable`, or when that installation needs portable IPC
isolation.

If an independently installed terminal is running but Python reports an IPC
initialization error, create a non-destructive portable copy in a
user-writable directory. Keep the original installation as rollback evidence.

1. Stop only the affected copier and terminal.
2. Copy the complete MT5 installation to a dedicated user-writable directory.
3. Start that copy with the `/portable` argument and log in manually.
4. Set its checkout's exact `terminal_path` and `portable = true`.
5. Confirm the connection with preflight before any `run` command.

Example configuration:

```toml
[mt5]
terminal_path = "C:\\MT5\\Broker-MT5-B\\terminal64.exe"
portable = true
require_demo = true
magic = 10002
```

Portable mode applies only to that configured terminal; it does not select a
different terminal automatically. Run the terminal and copier under the same
interactive Windows user and privilege level.

## Optional scheduled startup

After an interactive demo run and smoke test pass:

```powershell
.\scripts\install-task.ps1 -WhatIf
.\scripts\install-task.ps1 -TaskName 'Alpaca-MT5-CopyTrader-A'
```

Use a distinct task name for every checkout. The task starts after logon; it
does not start the copier immediately when installed. Keep the matching MT5
terminal running and logged in for that same user.

## Operational rules

- Keep `require_demo = true` unless you have deliberately approved a live MT5
  target and performed a separate activation review.
- Never start a second process for the same checkout while one is already
  running.
- Do not manually trade symbols reserved for the copier; netting accounts can
  merge manual and copied exposure.
- Do not clear durable state to resolve a mismatch. Use a fresh reconciliation
  plan and a passing preflight.
- Treat preflight, catalog data, account balances, positions, permissions, and
  broker connectivity as time-sensitive; rerun preflight immediately before
  starting or recovering the copier.
