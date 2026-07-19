# Tradebot Operations Manual

A Solana memecoin signal bot. Tracks "smart wallets" and dormant "zombie tokens"; fires buy signals when criteria converge; executes paper or live trades through Jupiter. Built on FastAPI plus SQLite, polled by Helius and Dexscreener.

Default mode is paper trading. Going live requires installing two additional packages and providing a wallet private key (covered in section 10).

---

## 1. The Two Strategies

### Wallet Tracker
Watches a set of Solana wallets you choose. When at least `WALLET_TRIGGER_COUNT` of those wallets buy the same token within `WALLET_LOOKBACK_MINUTES`, a signal fires. The bot then runs that signal through the risk manager, requests a Jupiter quote, and opens a position.

Premise: if multiple traders you respect independently converge on the same token, that's worth following.

### Zombie Tracker
Maintains a watchlist of old tokens (pair age above `ZOMBIE_DORMANT_DAYS`). Every cycle it checks each for a volume spike (`vol_1h` > N times the hourly average) combined with a price jump (`>= ZOMBIE_PRICE_CHANGE_PCT` in the last hour). A match fires a signal.

Premise: tokens that were once active and are now waking up can run hard before the rest of the market notices.

---

## 2. Architecture

```
run_all.py                  Process entry point (bot + dashboard)
  |
  +-> main.Tradebot         Orchestrator: signal handler, position monitor
  |     +-> WalletTracker   Polls wallets via Helius, detects convergence
  |     +-> ZombieTracker   Polls watchlist via Dexscreener, detects revival
  |     +-> RiskManager     Pre-trade approval, exit decision logic
  |     +-> JupiterClient   Quote and swap (paper or live)
  |
  +-> server.app (FastAPI)  Dashboard at http://localhost:3001
        +-> WebSocket /ws   3-second snapshot stream to the browser
        +-> REST /api/*     Read endpoints + add-wallet/zombie endpoints
```

Data flow per signal:

```
Helius polls wallets every 30s
   |
   v
record_wallet_activity(buys) -> wallet_activity table
   |
   v
get_hot_tokens() finds tokens with >= TRIGGER_COUNT wallet buys
   |
   v
WalletTracker._check_triggers fires signal -> create_signal(table)
   |
   v
Tradebot.handle_signal -> RiskManager.can_open_position
   |  (checks balance, position cap, liquidity, price > 0)
   v
JupiterClient.buy_token -> get_quote -> execute_swap
   |  (paper mode logs sim; live mode signs and sends tx)
   v
open_position(table) with entry_token_amount from quote.outAmount
   |
   v
Tradebot.monitor_positions (every 30s)
   |  fetches current prices, asks RiskManager.find_exit_candidates
   v
JupiterClient.sell_token -> close_position(table) with exit_tx
```

---

## 3. Files

### Core runtime
| File | Purpose |
|---|---|
| `run_all.py` | Entry point. Launches FastAPI server + bot loop. Configures logging to `tradebot.log` and console. |
| `main.py` | `Tradebot` class. Signal handler, position monitor, SOL price cache, graceful shutdown. |
| `server.py` | FastAPI app. REST endpoints, WebSocket broadcaster, lifespan handler. |
| `config.py` | All tunable parameters and `.env` loading. Strips placeholder values automatically. |
| `database.py` | SQLite schema + helpers. Idempotent migrations on startup. |

### Strategy + clients
| File | Purpose |
|---|---|
| `wallet_tracker.py` | Polls wallets via Helius (primary) or Birdeye (fallback). Records activity. Detects convergence. |
| `zombie_tracker.py` | Maintains zombie watchlist. Detects revivals. |
| `risk_manager.py` | `can_open_position()` and `find_exit_candidates()`. Decision-only, no DB writes. |
| `jupiter_client.py` | Quote + swap. Paper mode logs simulated fills. Live mode signs with `SOLANA_PRIVATE_KEY`. |
| `helius_client.py` | Wraps the Helius Enhanced Transactions API. Parses swaps into `{tx_hash, side, token_address, ...}`. |
| `dexscreener_client.py` | Free public API. Pair stats, token search, zombie revival detection. |
| `birdeye_client.py` | Optional fallback for wallet polling. Silenced when key is unset. |

### Discovery + maintenance
| File | Purpose |
|---|---|
| `find_wallets.py` | Daily candidate discovery. Outputs ranked `candidates.csv`. |
| `find_zombies.py` | Manual zombie candidate discovery. Outputs ranked `zombies.csv`. |
| `prune_wallets.py` | Weekly pruning. Scores tracked wallets by signal contribution + win rate. |

### UI
| File | Purpose |
|---|---|
| `static/index.html` | Dashboard. Dark/light theme toggle, +Add Wallet, +Add Token, signals table, positions table, history. |

### Data
| File | Purpose |
|---|---|
| `tradebot.db` | SQLite database. Created on first run. |
| `tradebot.log` | Rotating log (10 MB per file, 3 backups). |
| `candidates.csv` | Latest output of `find_wallets.py`. |
| `zombies.csv` | Latest output of `find_zombies.py`. |
| `prune_report.txt` | Latest output of `prune_wallets.py`. |
| `find_wallets_scheduled.log` | Output captured by the daily scheduled task. |

---

## 4. Configuration

### `.env` (secrets)

Located in the project root. Placeholders ending in `_here` or starting with `your_` are treated as unset.

```
HELIUS_API_KEY=<your real 36-char key from dashboard.helius.dev>
BIRDEYE_API_KEY=        (optional; leave blank to disable)
SOLANA_PRIVATE_KEY=     (live-trading only; never share)
PAPER_TRADING=True
DB_PATH=tradebot.db
LOG_LEVEL=INFO
```

### `config.py` (behavior)

| Variable | Default | Effect |
|---|---|---|
| `WALLET_TRIGGER_COUNT` | 6 | Number of tracked wallets that must converge for a signal. |
| `WALLET_POLL_INTERVAL_SEC` | 30 | How often each wallet is polled. |
| `WALLET_LOOKBACK_MINUTES` | 10 | Convergence window. |
| `MAX_TRACKED_WALLETS` | 500 | Hard cap on `smart_wallets` table size. |
| `SMART_WALLET_MIN_WIN_RATE` | 0.60 | Threshold for `score_and_add_wallet` auto-add. |
| `ZOMBIE_DORMANT_DAYS` | 14 | Minimum age before a token qualifies as a zombie. |
| `ZOMBIE_VOLUME_MULTIPLIER` | 5.0 | Volume spike threshold vs dormant average. |
| `ZOMBIE_PRICE_CHANGE_PCT` | 20.0 | Minimum 1-hour price move for zombie signal. |
| `ZOMBIE_POLL_INTERVAL_SEC` | 60 | Watchlist scan cadence. |
| `ZOMBIE_MIN_LIQUIDITY_USD` | 10_000 | Filter out illiquid revivals. |
| `TRADE_SIZE_SOL` | 0.1 | Position size per signal. |
| `MAX_OPEN_POSITIONS` | 10 | Hard cap on concurrent positions. |
| `STOP_LOSS_PCT` | 15.0 | Auto-sell on drawdown. |
| `TAKE_PROFIT_PCT` | 50.0 | Auto-sell on gain. |
| `SLIPPAGE_BPS` | 300 | Jupiter swap slippage (3%). |
| `PAPER_TRADING` | True | Loaded from `.env`; logs trades without sending. |
| `MONITORED_CHAINS` | solana, ethereum, bsc, base | Used by zombie tracker scan; wallet polling is Solana-only. |

Changes to `config.py` take effect on the next bot restart.

---

## 5. Daily Operations (PowerShell)

All commands assume you are in the project folder:

```powershell
cd C:\Users\hp\OneDrive\Documents\Claude\Projects\Tradebot
```

### Start the bot
```powershell
python run_all.py
```
Leave the window open. Dashboard becomes available at `http://localhost:3001`.

### Stop the bot
```powershell
Get-Process python | Stop-Process
```
Stops every Python process. If you have other Python work running, use the specific PID from `Get-Process python | Format-Table Id, StartTime`.

### Check what's running
```powershell
Get-Process python | Format-Table Id, ProcessName, StartTime
```

### Tail the log
```powershell
Get-Content tradebot.log -Tail 50 -Wait
```
`Ctrl+C` to stop tailing.

### Restart cleanly
```powershell
Get-Process python | Stop-Process
python run_all.py
```

---

## 6. Discovery + Maintenance Scripts

### Find new wallet candidates
```powershell
python find_wallets.py
```
Runs in ~60-180 seconds. Pulls trending Solana tokens from Dexscreener (free), finds buyers via Helius, cross-token filters, vets each. Writes `candidates.csv` sorted by composite score. Prints top 15 to console plus a shortlist count.

Helius credit budget: ~500-1500 per run (you have 1,000,000/month).

### Find new zombie candidates
```powershell
python find_zombies.py
```
No Helius credits used (Dexscreener-only). Outputs `zombies.csv`. When no full matches exist, prints a "near miss" table showing what each token failed.

### Prune dead-weight wallets
```powershell
python prune_wallets.py          # report only
python prune_wallets.py --apply  # also delete DROP-flagged wallets
```
Decision logic:
- `DROP` if not observed in 14+ days, or win rate below 30% across 3+ signal contributions
- `KEEP` if win rate is 60%+ across 3+ contributions
- `WATCH` otherwise

Run weekly after the bot has been collecting data.

---

## 7. Adding Wallets and Tokens

### Via the dashboard (recommended for single adds)
1. Open `http://localhost:3001`
2. Click the **Wallets** tab
3. Click **+ Add Wallet**
4. Paste address, chain (Solana), optional win rate + notes
5. Submit

Same flow for **+ Add Token** on the Zombie Watchlist tab.

### Bulk-add wallets from `candidates.csv` (PowerShell)

Pulls top 8 wallets with `bot_score=0` and POSTs each to the bot:

```powershell
Import-Csv "C:\Users\hp\OneDrive\Documents\Claude\Projects\Tradebot\candidates.csv" |
  Where-Object { [int]$_.bot_score -eq 0 } |
  Sort-Object { [double]$_.composite_score } -Descending |
  Select-Object -First 8 |
  ForEach-Object {
    $body = @{
      address = $_.address
      chain   = 'solana'
      notes   = "fw-$(Get-Date -Format yyyy-MM-dd) score=$($_.composite_score) spd=$($_.swaps_per_day) tok=$($_.distinct_tokens) xtok=$($_.cross_token_appearances)"
    } | ConvertTo-Json -Compress
    try {
      Invoke-RestMethod -Method Post -Uri http://localhost:3001/api/wallets/add `
                        -ContentType 'application/json' -Body $body | Out-Null
      Write-Host "OK  $($_.address)" -ForegroundColor Green
    } catch {
      Write-Host "ERR $($_.address) - $($_.Exception.Message)" -ForegroundColor Yellow
    }
  }
```

Tweaks:
- `-First 8` to take more or fewer
- Add `Where-Object { [int]$_.wallet_age_days -gt 30 }` to require old wallets only
- Change `Sort-Object` expression to prefer different metrics

### Bulk-add zombies from `zombies.csv` (PowerShell)
```powershell
Import-Csv "C:\Users\hp\OneDrive\Documents\Claude\Projects\Tradebot\zombies.csv" |
  Sort-Object { [double]$_.score } -Descending |
  Select-Object -First 10 |
  ForEach-Object {
    $body = @{
      chain          = 'solana'
      token_address  = $_.address
      token_symbol   = $_.symbol
      name           = ''
      peak_volume_usd = 0
    } | ConvertTo-Json -Compress
    try {
      Invoke-RestMethod -Method Post -Uri http://localhost:3001/api/zombies/add `
                        -ContentType 'application/json' -Body $body | Out-Null
      Write-Host "OK  $($_.symbol) $($_.address)" -ForegroundColor Green
    } catch {
      Write-Host "ERR $($_.address) - $($_.Exception.Message)" -ForegroundColor Yellow
    }
  }
```

---

## 8. Inspecting State

### Snapshot (everything)
```powershell
Invoke-RestMethod http://localhost:3001/api/snapshot | ConvertTo-Json -Depth 4
```

### Just the stats
```powershell
(Invoke-RestMethod http://localhost:3001/api/snapshot).stats
```

### Open positions
```powershell
Invoke-RestMethod http://localhost:3001/api/positions |
  Format-Table token_symbol, entry_price_usd, stop_loss_usd, take_profit_usd, opened_at -AutoSize
```

### Tracked wallets
```powershell
Invoke-RestMethod http://localhost:3001/api/wallets |
  Format-Table address, last_seen, win_rate, notes -Wrap
```

### Recent signals
```powershell
Invoke-RestMethod http://localhost:3001/api/signals |
  Format-Table created_at, signal_type, token_symbol, trigger_count, confidence -AutoSize
```

### Zombie watchlist
```powershell
Invoke-RestMethod http://localhost:3001/api/zombies | Format-Table -AutoSize
```

### Database query (read-only)
```powershell
python -c "import sqlite3; c = sqlite3.connect('tradebot.db'); c.row_factory = sqlite3.Row; rows = c.execute('SELECT token_symbol, pnl_pct, exit_reason, closed_at FROM trade_history ORDER BY closed_at DESC LIMIT 10').fetchall(); [print(dict(r)) for r in rows]"
```

---

## 9. Reset and Cleanup

### Clear all open positions and pending signals
```powershell
python -c "import sqlite3; c = sqlite3.connect('tradebot.db'); n1 = c.execute('DELETE FROM open_positions').rowcount; n2 = c.execute('DELETE FROM signals').rowcount; c.commit(); print(f'Cleared {n1} positions, {n2} signals')"
```

### Drop invalid (truncated) wallets
```powershell
python -c "import sqlite3; c = sqlite3.connect('tradebot.db'); n = c.execute('DELETE FROM smart_wallets WHERE LENGTH(address) < 32').rowcount; c.commit(); print(f'Deleted {n} invalid wallets')"
```

### Nuke and restart from zero
```powershell
Get-Process python | Stop-Process
Remove-Item tradebot.db, tradebot.log -ErrorAction SilentlyContinue
python run_all.py
```
The schema is recreated on startup. All wallets and history are gone.

### Remove the scheduled task
```powershell
Unregister-ScheduledTask -TaskName "Tradebot-find-wallets-daily" -Confirm:$false
```

---

## 10. Going Live (Real Money)

**Read this section twice before flipping anything.** Live mode signs and submits real transactions.

### Prerequisites
1. A funded Solana wallet you are willing to lose. Use a dedicated wallet, not your main one.
2. The wallet's Base58 private key.
3. SOL in the wallet (at least 0.5 SOL for trading + tx fees).
4. Install the two libraries left out of `requirements.txt`:
   ```powershell
   pip install solders solana
   ```

### Enable
1. Edit `.env`:
   ```
   SOLANA_PRIVATE_KEY=<your base58 private key>
   PAPER_TRADING=False
   ```
2. Set `TRADE_SIZE_SOL` to something tiny in `config.py`. Suggested first run: `0.01` (a few dollars per trade).
3. Restart:
   ```powershell
   python run_all.py
   ```
4. Watch the log carefully for the first hour. Each trade should show `Swap sent: https://solscan.io/tx/<sig>` instead of the paper-mode `PAPER_xxx` marker.

### Safety checks the bot already enforces
- `RiskManager.can_open_position`: rejects if SOL balance below 1.05x trade size, if liquidity below threshold, if already holding the token, if at position cap.
- Jupiter price-impact warning at >10%, but does not block.
- Position monitor enforces stop-loss and take-profit every 30 seconds.
- Failed sells leave the position open and retry next cycle (won't lose track of held tokens).

### What the bot does NOT do
- No portfolio rebalancing.
- No DCA, no laddering.
- No slippage adaptation based on volatility.
- No protection against rug pulls beyond the liquidity check at entry.
- No notifications. You watch the log.

### Going back to paper
1. Edit `.env`: `PAPER_TRADING=True`
2. Restart.

---

## 11. API Reference

Base URL: `http://localhost:3001`

| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/` | — | Dashboard HTML |
| GET | `/api/snapshot` | — | Full state: stats, positions, signals, wallets, zombies |
| GET | `/api/stats` | — | Trade summary (total trades, win rate, PnL) |
| GET | `/api/positions` | — | Currently open positions |
| GET | `/api/signals` | — | Last 30 signals with confidence scores |
| GET | `/api/wallets` | — | All tracked wallets |
| GET | `/api/zombies` | — | All watchlist tokens |
| POST | `/api/wallets/add` | `{address, chain, win_rate, notes}` | `{status, address}` or 409 if duplicate |
| POST | `/api/zombies/add` | `{chain, token_address, token_symbol, name, peak_volume_usd}` | `{status, token_address}` or 409 |
| POST | `/api/strategy/create` | `{description, strategy_type}` | Parsed parameters from plain English |
| WS | `/ws` | — | Snapshot pushed every 3s while clients connected |

---

## 12. Database Schema

All tables in `tradebot.db`. Created and migrated automatically on `init_db()`.

### `smart_wallets`
Tracked wallets the bot polls.
- `id`, `address` (unique), `chain`, `win_rate`, `trade_count`, `profit_usd`, `added_at`, `last_seen`, `notes`

### `wallet_activity`
Raw observed swaps per tracked wallet. Used by the convergence query.
- `id`, `wallet`, `chain`, `token_address`, `token_symbol`, `action` (buy/sell), `amount_usd`, `tx_hash` (unique), `observed_at`

### `signals`
Each time the bot fires a buy signal.
- `id`, `signal_type` (wallet/zombie), `chain`, `token_address`, `token_symbol`, `trigger_count`, `extra_data` (JSON), `created_at`, `acted` (0/1)

### `open_positions`
Currently held tokens.
- `id`, `chain`, `token_address`, `token_symbol`, `entry_price_usd`, `entry_amount_sol`, `entry_token_amount`, `size_usd`, `stop_loss_usd`, `take_profit_usd`, `signal_id`, `signal_type`, `opened_at`, `tx_hash`

### `trade_history`
Closed positions with realized PnL.
- `id`, `chain`, `token_address`, `token_symbol`, `entry_price_usd`, `exit_price_usd`, `entry_amount_sol`, `entry_token_amount`, `pnl_usd`, `pnl_pct`, `exit_reason`, `signal_type`, `opened_at`, `closed_at`, `entry_tx`, `exit_tx`

### `zombie_watchlist`
Old tokens being watched for revival.
- `id`, `chain`, `token_address` (unique), `token_symbol`, `name`, `peak_volume_usd`, `last_active_at`, `dormant_since`, `added_at`, `status` (watching/triggered/ignored)

---

## 13. Scheduled Tasks

### Currently registered
- **Tradebot-bot-always-on**: auto-starts `python run_all.py` (via `pythonw.exe`, hidden window) at user logon. Auto-restarts up to 3 times on crash, 1 minute apart. No time limit. Survives logouts. PC must be powered on and not sleeping.
- **Tradebot-find-wallets-daily**: runs `find_wallets.py` every day at 6:00 AM. Output appended to `find_wallets_scheduled.log`. 10-minute timeout. Catches up on next boot if PC was off at 6am.

### Always-on bot task: register
Run this once to set up auto-start at logon:

```powershell
$projectDir = "C:\Users\hp\OneDrive\Documents\Claude\Projects\Tradebot"
$pythonwExe = (Get-Command python).Source -replace 'python\.exe$','pythonw.exe'

$action = New-ScheduledTaskAction `
    -Execute $pythonwExe `
    -Argument "run_all.py" `
    -WorkingDirectory $projectDir

$trigger = New-ScheduledTaskTrigger -AtLogon -User $env:USERNAME

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew

$principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName "Tradebot-bot-always-on" `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Tradebot main bot - auto-starts at user logon, restarts on crash"
```

### Always-on bot task: caveats
1. **PC has to be on and not sleeping.** Scheduled Tasks can't run on a powered-off machine. Set **Settings → System → Power & battery → Sleep** to "Never" (at least when plugged in).
2. **Trigger is AtLogon, not AtStartup.** A true AtStartup trigger would need to run as SYSTEM, which loses access to your user-scoped `.env` file. AtLogon means: PC boots, you log in once, bot starts. Reboot without logging in = bot stays off.
3. **Hidden window.** Uses `pythonw.exe` (no console). All output goes to `tradebot.log` only. You'll see `pythonw.exe` in Task Manager, not `python.exe`.

### Always-on bot task: manage
```powershell
# Health + last/next run
Get-ScheduledTask -TaskName "Tradebot-bot-always-on" | Get-ScheduledTaskInfo

# Confirm the bot is actually running
Get-Process pythonw -ErrorAction SilentlyContinue | Format-Table Id, StartTime
Invoke-RestMethod http://localhost:3001/api/snapshot | Select-Object -ExpandProperty paper_trading

# Trigger right now (test without waiting for next logon)
Start-ScheduledTask -TaskName "Tradebot-bot-always-on"

# Stop the bot without disabling the task (it'll restart on next logon)
Get-Process pythonw -ErrorAction SilentlyContinue | Stop-Process

# Disable the task temporarily (won't fire on next logon)
Disable-ScheduledTask -TaskName "Tradebot-bot-always-on"
Enable-ScheduledTask -TaskName "Tradebot-bot-always-on"

# Remove entirely
Unregister-ScheduledTask -TaskName "Tradebot-bot-always-on" -Confirm:$false
```

### find_wallets daily task: manage
```powershell
# When did it last run, when does it next?
Get-ScheduledTask -TaskName "Tradebot-find-wallets-daily" | Get-ScheduledTaskInfo

# Trigger it right now (testing)
Start-ScheduledTask -TaskName "Tradebot-find-wallets-daily"

# Change the time
$newTrigger = New-ScheduledTaskTrigger -Daily -At 8am
Set-ScheduledTask -TaskName "Tradebot-find-wallets-daily" -Trigger $newTrigger

# Remove
Unregister-ScheduledTask -TaskName "Tradebot-find-wallets-daily" -Confirm:$false
```

### Add a daily prune task (optional)
```powershell
$action = New-ScheduledTaskAction `
    -Execute "cmd.exe" `
    -Argument "/c python prune_wallets.py >> prune_scheduled.log 2>&1" `
    -WorkingDirectory "C:\Users\hp\OneDrive\Documents\Claude\Projects\Tradebot"
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At 7am
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
Register-ScheduledTask -TaskName "Tradebot-prune-weekly" `
    -Action $action -Trigger $trigger -Settings $settings `
    -Description "Weekly wallet prune report (review-only)"
```

Note: this version does NOT pass `--apply`, so it only writes the report. Inspect `prune_report.txt` and run `python prune_wallets.py --apply` manually when you agree with the suggestions.

---

## 14. Operating Costs

| Service | Tier | Cost | Used For |
|---|---|---|---|
| Helius | Free | $0/mo (1M credits) | Wallet swap polling |
| Dexscreener | Public API | $0 | Price + pair data, zombie discovery |
| Jupiter Lite | Free | $0 | Quote + swap (paper sim) |
| Birdeye | Optional | $39+/mo | Fallback wallet polling (not needed if Helius works) |

Helius credit usage estimate at default settings: ~25-50 credits per wallet per day for polling, plus ~500-1500 per `find_wallets.py` run. Even with 50 tracked wallets and daily discovery, well under the 1M monthly cap.

---

## 15. Iteration Playbook

### Week 1: Bootstrap
- Day 0: install everything, configure `.env`, seed 8-12 wallets from `find_wallets.py`.
- Day 1-7: bot runs, you check the dashboard once a day. Don't tune yet; collect data.
- End of day 7: `python prune_wallets.py` (report only). Read it. Decide which wallets to drop. Run with `--apply`.

### Tuning levers, in order of impact
1. **Wallet quality.** The biggest variable. Bad wallets = no useful signals. Rerun `find_wallets.py` weekly; replace dropped wallets with fresh picks.
2. **`WALLET_TRIGGER_COUNT`.** Higher = fewer but stronger signals. With sniper-correlated wallets, set to 6-7 of 8. With genuinely independent wallets, 3-4 of 8 is fine.
3. **`STOP_LOSS_PCT` and `TAKE_PROFIT_PCT`.** Defaults are 15/50 (1:3.3 risk-reward). Tighten if your win rate is below 30% and losses are dominating. Widen if you're getting stopped out before tokens have time to run.
4. **`TRADE_SIZE_SOL`.** In paper mode, irrelevant. In live mode, start at 0.01 SOL and scale up only after 30+ profitable trades.

### When to stop iterating
- 30 days of paper trading
- Win rate consistently above 40%
- Net PnL positive across at least 50 trades
- You understand why each tracked wallet is contributing

Below those bars, do not enable live mode.

---

## 16. Troubleshooting

### `python` not recognized
Python is not installed or not on PATH. See section 4 of the original setup notes, or install from `https://www.python.org/downloads/windows/` and tick "Add to PATH."

### `HELIUS_API_KEY not set in .env - aborting.` (from `find_wallets.py`)
The `.env` file is missing the key, has the placeholder, or you're running from the wrong folder. Check:
```powershell
python -c "from config import HELIUS_API_KEY; print('key set:', bool(HELIUS_API_KEY), 'len:', len(HELIUS_API_KEY))"
```

### `Helius error 400 ... invalid address`
You have truncated (under 32-char) wallets in the database. Run:
```powershell
python -c "import sqlite3; c = sqlite3.connect('tradebot.db'); n = c.execute('DELETE FROM smart_wallets WHERE LENGTH(address) < 32').rowcount; c.commit(); print(f'Deleted {n}')"
```

### `WebSocket client connected` followed by `No supported WebSocket library detected`
Missing library. Run:
```powershell
pip install websockets
```

### Bot fires too many signals (5+ per second)
Your tracked wallets are sniper-correlated. Either bump `WALLET_TRIGGER_COUNT` in `config.py`, or drop wallets and re-seed using stricter criteria in `find_wallets.py`.

### Bot fires zero signals after 24+ hours
Either `WALLET_TRIGGER_COUNT` is too high for your wallet set, or the wallets are too inactive. Lower the trigger by 1 each day until you see signals; if you reach trigger=2 with no signals, replace the wallet set.

### `UnicodeEncodeError ... cp1252` in console
Already patched (see `run_all.py` UTF-8 reconfigure). If it returns, check that `sys.stdout.reconfigure(encoding="utf-8")` is at the top of `run_all.py`.

### `Cannot connect to host quote-api.jup.ag`
Jupiter migrated. `config.py` should point at `https://lite-api.jup.ag/swap/v1/quote`. If it doesn't, fix it.

### Position opened but trade_history shows weird PnL
Check `entry_token_amount` on the position. If it's 0, the buy quote didn't return `outAmount` properly. The next exit will close the position with `_no_qty` suffix on `exit_reason`.

### Dashboard shows stale data
WebSocket library may be missing (see above). Even without it, the dashboard falls back to polling `/api/snapshot` every 10 seconds. Hard-refresh with `Ctrl+F5`.

---

## 17. What Is NOT Built

For future iteration:
- **`find_early_wallets.py`**: discovery that specifically filters for wallets that entered tokens within the first 30 minutes of trading (rather than just "active across multiple tokens"). The current `find_wallets.py` selects for correlated bots in trending-token-buyer sets.
- **Notifications**: no Discord, Telegram, email, or desktop alerts on signal fire or position close. Log-only.
- **Multi-chain execution**: wallet tracker and zombie tracker both support multi-chain in data collection, but Jupiter only swaps on Solana. Non-Solana signals get logged and skipped.
- **Position-sizing intelligence**: every trade is the same `TRADE_SIZE_SOL`. No Kelly criterion, no volatility-adjusted sizing.
- **Backtest harness**: there's no way to replay historical wallet activity through the strategy to validate it offline. All evaluation is forward-paper.

---

## 18. Quick Reference Card

```
Start              python run_all.py
Stop               Get-Process python | Stop-Process
Dashboard          http://localhost:3001
Tail log           Get-Content tradebot.log -Tail 50 -Wait
Discover wallets   python find_wallets.py
Discover zombies   python find_zombies.py
Prune (report)     python prune_wallets.py
Prune (apply)      python prune_wallets.py --apply
Clear positions    python -c "import sqlite3; c = sqlite3.connect('tradebot.db'); c.execute('DELETE FROM open_positions'); c.execute('DELETE FROM signals'); c.commit()"
Stats              (Invoke-RestMethod http://localhost:3001/api/snapshot).stats
```
