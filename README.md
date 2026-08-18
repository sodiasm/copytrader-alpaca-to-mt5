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

## Important operational boundary

Mapped MT5 symbols are reserved for this copier. Do not place manual MT5 trades in them. In a netting account, MT5 merges exposure by symbol and cannot reliably separate manual and copied positions.
