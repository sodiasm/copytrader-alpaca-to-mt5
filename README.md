# Alpaca to MetaTrader 5 Copy Trader

This project copies **actual Alpaca paper fills** to the local **Darwinex MetaTrader 5 Demo1** terminal. It is intentionally long-only and paper/demo-only in version 1.

## Safety behavior

- Copies only `fill` and `partial_fill` events, never submitted or accepted orders.
- Uses a reviewed broker snapshot of U.S. stocks and ETFs that are tradable on both accounts.
- Refuses an initial activation unless mapped positions are flat on both accounts.
- Never lets a copied sell cross through zero into a short.
- Blocks execution above 0.50% source/target price deviation.
- Uses MT5 margin/order checks and verifies the resulting deal and position.
- Persists execution IDs in SQLite and does not blindly retry ambiguous MT5 results.
- Pauses on disconnects, drift, closed/untradable symbols, or uncertain execution.
- Has no portfolio exposure or daily-loss cap, as selected for this build.

## Install

Open PowerShell in this directory and run:

```powershell
.\scripts\setup.ps1
```

The setup creates `.venv`, installs `alpaca-py` and `MetaTrader5`, and restricts `.env` to the current Windows user. Put the Alpaca paper keys in `.env`. Leave the MT5 login fields blank when Demo1 is already logged in.

## Select the MetaTrader terminal

Set `[mt5].terminal_path` in `config.toml` to the exact executable for the MT5 installation the copier must use. Windows backslashes must be doubled in a TOML quoted string:

```toml
[mt5]
terminal_path = "C:\\Program Files\\Darwinex MetaTrader 5 Demo1\\terminal64.exe"
require_demo = true
```

The configured path must exist and point to a file. The copier passes that exact path to MetaTrader 5; it does not search for another installation or silently fall back to one. Stop the copier before changing the path.

After changing it, run the read-only preflight and check that `mt5_connected` reports the intended terminal path and `mt5_demo_mode` reports `demo`:

```powershell
.\.venv\Scripts\python.exe -m copytrader --config .\config.toml preflight
```

The symbol universe is configured in `config.toml` and stored in
`symbols.us-stocks-etfs.json`:

```toml
[symbol_universe]
snapshot_path = "symbols.us-stocks-etfs.json"
stock_path_prefix = "Stocks\\US\\"
etf_path_prefix = "ETFs\\"

[symbol_universe.aliases]
"BRK.B" = "BRKb"
```

Preview a refreshed MT5/Alpaca catalog without changing the snapshot:

```powershell
.\.venv\Scripts\python.exe -m copytrader --config .\config.toml symbols sync-us
```

After reviewing the added, removed, modified, and excluded symbols, stop the
copier and atomically write the snapshot:

```powershell
.\.venv\Scripts\python.exe -m copytrader --config .\config.toml symbols sync-us --write
```

The running process never hot-reloads mappings. Run preflight and restart the
copier to activate a new snapshot:

```powershell
.\.venv\Scripts\python.exe -m copytrader --config .\config.toml preflight
```

`status` reports the stored and active hashes and whether a restart is needed.
The legacy `[[symbols]]` configuration is intentionally rejected.

## Run and inspect

```powershell
.\.venv\Scripts\python.exe -m copytrader --config .\config.toml run
.\.venv\Scripts\python.exe -m copytrader --config .\config.toml status
```

On any pause, generate a state-bound reconciliation plan:

```powershell
.\.venv\Scripts\python.exe -m copytrader --config .\config.toml reconcile plan
.\.venv\Scripts\python.exe -m copytrader --config .\config.toml reconcile apply PLAN_ID --yes
```

`apply` only unpauses when live positions already agree with durable state. Version 1 deliberately does not place corrective reconciliation trades.

## Automated paper/demo smoke test

Only after preflight passes and both mapped accounts are flat:

```powershell
.\.venv\Scripts\python.exe -m copytrader --config .\config.toml smoke-test --symbol AAPL --yes
```

The test calculates the smallest source quantity that reaches one valid MT5 volume step, refuses source notional above $1,000, buys on Alpaca paper, verifies the copied MT5 demo deal, closes the Alpaca position, and confirms both accounts return flat. It will not place an order while the Alpaca stock market is closed.

## Scheduled Task

After the interactive run and smoke test pass:

```powershell
.\scripts\install-task.ps1 -WhatIf
.\scripts\install-task.ps1
```

The task runs as the current interactive user 30 seconds after logon and restarts after failures. The Demo1 terminal must remain running and logged in under that same user.

## Run two independent copies

Two independent application installations can run on the same Windows computer
when each one connects a different Alpaca paper account to a different MT5 demo
account:

```text
Alpaca paper account A -> application A -> MT5 demo account A
Alpaca paper account B -> application B -> MT5 demo account B
```

The Alpaca accounts must have different account IDs and their own API
credentials. Two API key pairs for the same underlying Alpaca account are not
independent accounts and can still contend for that account's streaming
connection limit.

Use two clean checkouts instead of copying a running installation. This avoids
copying credentials, the path-bound virtual environment, durable execution
history, or logs:

```powershell
git clone https://github.com/sodiasm/copytrader-alpaca-to-mt5.git copytrader-a
git clone https://github.com/sodiasm/copytrader-alpaca-to-mt5.git copytrader-b
```

In each checkout:

1. Run `.\scripts\setup.ps1` to create a local `.venv` and restricted `.env`.
2. Put only that Alpaca paper account's credentials and MT5 credentials in its
   `.env`.
3. Install the MT5 terminals in different directories. MetaTrader cannot run
   two terminal copies from the same installation directory.
4. Set `[mt5].terminal_path` to that checkout's intended terminal executable
   and give each installation a different positive `[mt5].magic` value.
5. Preview `symbols sync-us`, review the broker-specific mappings and contract
   specifications, then write a separate symbol snapshot for that checkout.
6. Run the read-only preflight and verify the reported Alpaca account ID, MT5
   login, broker server, terminal path, demo mode, and trading permission.
7. Complete the interactive run and paper/demo smoke test before registering
   its scheduled task.

Do not define `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, or the `MT5_*` settings as
global Windows environment variables. An inherited global value takes
precedence over a checkout's `.env` and could make both processes connect to
the same account.

Register each checkout under a unique task name so the second task does not
replace the first:

```powershell
# Run from copytrader-a
.\scripts\install-task.ps1 -TaskName 'Alpaca-MT5-CopyTrader-A'

# Run from copytrader-b
.\scripts\install-task.ps1 -TaskName 'Alpaca-MT5-CopyTrader-B'
```

Each checkout keeps its own human-readable audit in `logs/copytrader.log` and
its own durable execution, allocation, pause, and reconciliation state in
`state/copytrader.db`. Never share either path between the two processes. A
pause or unresolved action must be reviewed and reconciled in the affected
checkout only.

## Important operational boundary

Mapped MT5 symbols are reserved for this copier. Do not place manual MT5 trades in them. In a netting account, MT5 merges exposure by symbol and cannot reliably separate manual and copied positions.
