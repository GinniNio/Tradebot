"""
config.py — Central configuration for the Tradebot.
All tuneable parameters live here. Copy .env.example to .env and fill in your keys.

API budget doctrine (tiered candidate funnel — read before adding new sources):
  Dexscreener   = broad + cheap. Batch up to 30 tokens per request. Tier 0.
  Jupiter       = tradability oracle. Only after Dex shortlists. Tier 1.
  GoPlus        = security reject gate. Only after Jupiter route exists. Tier 2.
  Helius        = expensive enrichment (creator/wallet). Only after Dex + Jupiter
                  filter the candidate. NEVER use it for wide background polling.
  GeckoTerminal = OHLCV backfill only. No polling loop.
  Dune          = offline cohort research only.
  Birdeye       = manual enrichment only. Standard plan (1 RPS, 30k CU/mo)
                  cannot sustain any polling loop. Demoted.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ─── API Keys ──────────────────────────────────────────────────────────────────
def _strip_placeholder(v: str) -> str:
    """Treat .env.example placeholders ('your_xxx_here') as unset."""
    if not v:
        return ""
    s = v.strip()
    if s.startswith("your_") or s.endswith("_here"):
        return ""
    return s

BIRDEYE_API_KEY     = _strip_placeholder(os.getenv("BIRDEYE_API_KEY", ""))
HELIUS_API_KEY      = _strip_placeholder(os.getenv("HELIUS_API_KEY", ""))
SOLANA_PRIVATE_KEY  = _strip_placeholder(os.getenv("SOLANA_PRIVATE_KEY", ""))

# ─── Telegram push alerts ──────────────────────────────────────────────────────
# Optional. When both are set, telegram_notifier pushes signal fires and
# throttled tracker-failure alerts to Kaye's phone. When unset, every notifier
# call is a silent no-op — safe to leave unconfigured.
# Setup steps: see telegram_notifier.py module docstring.
TELEGRAM_BOT_TOKEN  = _strip_placeholder(os.getenv("TELEGRAM_BOT_TOKEN", ""))
TELEGRAM_CHAT_ID    = _strip_placeholder(os.getenv("TELEGRAM_CHAT_ID", ""))

# ─── RPC Endpoints ─────────────────────────────────────────────────────────────
HELIUS_RPC_URL      = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
HELIUS_WS_URL       = f"wss://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
JUPITER_QUOTE_URL   = "https://lite-api.jup.ag/swap/v1/quote"
JUPITER_SWAP_URL    = "https://lite-api.jup.ag/swap/v1/swap"

BIRDEYE_BASE_URL    = "https://public-api.birdeye.so"
DEXSCREENER_BASE_URL = "https://api.dexscreener.com/latest/dex"

DB_PATH = os.getenv("DB_PATH", "tradebot.db")

# ─── Wallet Tracker settings ───────────────────────────────────────────────────
# Kill switch — when False, main.py skips starting WalletTracker entirely.
# Flip to True (or remove from .env) once Helius credits reset (2026-06-20).
WALLET_TRACKER_ENABLED     = os.getenv("WALLET_TRACKER_ENABLED", "True").strip().lower() == "true"

WALLET_TRIGGER_COUNT       = 6
WALLET_POLL_INTERVAL_SEC   = 30
WALLET_LOOKBACK_MINUTES    = 10
MAX_TRACKED_WALLETS        = 500
SMART_WALLET_MIN_WIN_RATE  = 0.60

# ─── Zombie Tracker settings ───────────────────────────────────────────────────
# When False, zombie_tracker uses Dexscreener-only paths (free, unlimited).
# Set True only if Birdeye plan has enough RPS/CU headroom for ~50+ calls per
# watchlist cycle. Birdeye Standard (1 RPS, 30k CU/mo) is NOT enough.
ZOMBIE_USE_BIRDEYE         = os.getenv("ZOMBIE_USE_BIRDEYE", "False").strip().lower() == "true"

ZOMBIE_DORMANT_DAYS        = 14
ZOMBIE_VOLUME_MULTIPLIER   = 5.0
ZOMBIE_PRICE_CHANGE_PCT    = 20.0
ZOMBIE_POLL_INTERVAL_SEC   = 60
ZOMBIE_MIN_LIQUIDITY_USD   = 15_000  # Matches risk_manager zombie floor

# v2 selector filters — applied at signal-fire time inside zombie_tracker.
# Derived from the 2026-05-28 winners-vs-losers forensic on the 41 v1 outcomes.
# A signal passes v2 (and gets tagged 'zombie_v2') only if ALL three hold;
# otherwise it falls through as v1 'zombie' (STOP-blocked, measurement only).
ZOMBIE_V2_PC1H_MIN        = 20.0   # reject revivals below this — noise floor
ZOMBIE_V2_PC1H_MAX        = 50.0   # reject moonshots — losers had max +1,250%
ZOMBIE_V2_VOL1H_MAX_USD   = 8_000  # winner median $3,385, loser median $10,774
ZOMBIE_V2_ALLOWED_CHAINS  = {"base", "solana"}  # base had 0 catastrophes in n=5

# ─── Risk Management ──────────────────────────────────────────────────────────
# These top-level values are the v1-momentum-baseline defaults. They stay here
# for backward compat with code paths that haven't been migrated to the
# per-strategy config yet. See STRATEGIES dict below for the source of truth.
TRADE_SIZE_SOL             = 0.1
MAX_OPEN_POSITIONS         = 10
STOP_LOSS_PCT              = 15.0
TAKE_PROFIT_PCT            = 50.0
SLIPPAGE_BPS               = 300
PAPER_TRADING              = True

# ─── Per-strategy config ──────────────────────────────────────────────────────
# Each strategy_tag has its own trade structure. Risk manager and main loop
# look up the right block based on the signal's strategy_tag. Keep v1-momentum-
# baseline locked to its original values so the parallel comparison stays clean.
#
# v2-options:
#   - Treats every position as a small call option on attention.
#   - No traditional stop loss — the position size IS the option premium.
#   - Tiered TPs: take 20% off at +100%, another 20% at +300%, remaining 60%
#     trails with no upper cap (trail = peak * (1 - trailing_stop_pct_from_peak/100)).
#   - Tiered state is tracked per-position in open_positions.tp_tier_state (JSON).
STRATEGIES = {
    "v1-momentum-baseline": {
        "trade_size_sol":            0.1,
        "max_open_positions":        10,
        "stop_loss_pct":             15.0,
        "take_profit_pct":           50.0,
        "zombie_tp_multiplier":      1.5,
        "tp_ladder":                 None,   # single TP, no tiered exits
        "use_trailing_stop":         False,
        "trailing_stop_pct_from_peak": None,
        "min_liquidity_usd_wallet":  10_000,
        "min_liquidity_usd_zombie":  15_000,
    },
    "v2-options": {
        "trade_size_sol":            0.02,
        "max_open_positions":        40,
        "stop_loss_pct":             None,   # NO STOP — let positions go to zero
        "take_profit_pct":           None,   # uses tp_ladder instead
        "zombie_tp_multiplier":      1.0,    # n/a, tp_ladder is shared across signal types
        "tp_ladder": [
            {"trigger_pct": 100, "sell_fraction": 0.20},   # +100%: sell 20%
            {"trigger_pct": 300, "sell_fraction": 0.20},   # +300%: sell another 20%
        ],
        "use_trailing_stop":         True,    # remaining 60% trails with no upper cap
        "trailing_stop_pct_from_peak": 30.0,  # exit if price drops 30% from local peak
        # Structural filter floors (Phase 3 wires these into the gates)
        "min_liquidity_usd_wallet":  20_000,
        "min_liquidity_usd_zombie":  20_000,
    },
}

DEFAULT_STRATEGY_TAG = "v1-momentum-baseline"


def get_strategy_config(strategy_tag: str = None) -> dict:
    """Return the config block for a strategy_tag, falling back to v1-momentum-baseline."""
    return STRATEGIES.get(strategy_tag or DEFAULT_STRATEGY_TAG, STRATEGIES[DEFAULT_STRATEGY_TAG])


# ─── Strategy state machine ───────────────────────────────────────────────────
# Every signal source has an explicit state. risk_manager.can_open_position
# blocks position opens for any strategy not in TRADABLE_STATES — RESEARCH
# signals still log via signal_outcomes, they just can't open a (paper) position.
#
# State flow: BACKLOG → RESEARCH → PAPER → LIVE_CANDIDATE → LIVE
#             STOP is terminal for strategies proven negative-EV.
#
# Promotion gates:
#   RESEARCH → PAPER:           30–50 logged outcomes, positive avg return,
#                               acceptable route availability, manual review.
#   PAPER → LIVE_CANDIDATE:     100 paper trades, positive net P&L after costs,
#                               drawdown within cap, no single outlier carrying.
#   LIVE_CANDIDATE → LIVE:      250+ samples, kill switch tested, max daily loss
#                               hard-coded, manual enable, tiny stake only.
#
# Kill criterion: if no strategy reaches LIVE_CANDIDATE by 2026-12-31, archive
# or downgrade the project. No "one more tweak" beyond that date.
STRATEGY_STATES = {
    # 2026-05-28 verdict: n=41 live outcomes, median 24h return -77%, mean -62%,
    # 12% positive. Median worse than mean = systemic decay, not tail rugs. The
    # 13.9% avg pop at 15m doesn't translate to held returns; 88% of picks die
    # within 24h. Candidate selector (find_zombies.py) is the bottleneck. No
    # exit-timing variant fixes a cohort that mostly heads to zero. Strategy
    # archived. Do not re-litigate without a redesigned selector and a fresh
    # 30-outcome clock under a new strategy key.
    "zombie_revival":     "STOP",
    # 2026-05-28 hypothesis: redesigned selector based on winners-vs-losers
    # forensic on the 41 v1 outcomes. Three filters separate the 5 winners:
    #   (1) price_change_1h in [20%, 50%] — reject FOMO blow-offs (loser mean
    #       was +169%, max +1250%; all winners landed between +20% and +49%)
    #   (2) volume_1h <= $8,000 — prefer quiet revivals (winner median $3,385
    #       vs loser median $10,774, 3x cleaner separator than liquidity)
    #   (3) chain in {base, solana} — Base had 0 catastrophes in 5 signals
    #       vs Solana's 59% catastrophe rate; n=5 is small so kept Solana in.
    # In-sample filter — must collect ≥30 fresh outcomes under this selector
    # before any promotion decision. Outcome-tagging via signal_type='zombie_v2'
    # in record_signal_outcome (signals.signal_type stays 'zombie' for CHECK).
    "zombie_revival_v2":  "RESEARCH",
    "wallet_convergence": "PAUSED",     # Helius credits exhausted until 2026-06-20; bot-contaminated universe
    "launch_momentum":    "RESEARCH",   # Not built yet
    "route_arbitrage":    "RESEARCH",   # Spread persistence must be measured before paper trading
    "ai_agent_attention": "BACKLOG",    # Idea only, no code path
}

TRADABLE_STATES = {"PAPER", "LIVE_CANDIDATE", "LIVE"}

# Maps the signal_type emitted by trackers ('zombie', 'wallet', ...) to the
# strategy state key above. Trackers don't need to know the state key; they
# emit a signal_type and risk_manager looks up the right state.
SIGNAL_TYPE_TO_STRATEGY = {
    "zombie":    "zombie_revival",     # v1 selector, STOP — still emitted for measurement only
    "zombie_v2": "zombie_revival_v2",  # v2 selector — fires when v2 filter passes (see zombie_tracker)
    "wallet":    "wallet_convergence",
    "launch":    "launch_momentum",
    "arb":       "route_arbitrage",
}


def strategy_for_signal_type(signal_type: str) -> str:
    """Map a tracker's signal_type to its strategy state key. Defaults to
    zombie_revival so unknown types fail closed (gated by RESEARCH state)."""
    return SIGNAL_TYPE_TO_STRATEGY.get(signal_type, "zombie_revival")


def strategy_can_open_position(strategy_name: str) -> tuple[bool, str]:
    """Returns (approved, reason). Strategy must be in TRADABLE_STATES."""
    state = STRATEGY_STATES.get(strategy_name, "RESEARCH")
    if state not in TRADABLE_STATES:
        return False, f"blocked_by_strategy_state:{state}"
    return True, "approved"

# ─── Universal quality filters ────────────────────────────────────────────────
# Applied to ALL strategies (v1, v2, future). These reject tokens that are
# structurally in the catastrophic-mortality population, before strategy-
# specific logic runs.
#
# REJECT_PRE_GRADUATION: dexId values that indicate a token is still on a
# bonding curve (hasn't graduated to a real AMM). Academic data: only 0.5%-
# 1.4% of pump.fun tokens graduate, so 99%+ of pre-graduation tokens die.
# Trading them is buying into a 99%-mortality sub-population.
#   pumpfun  — Solana pump.fun bonding curve
#   four-meme / fourmeme — BNB four.meme bonding curve (covered for future use)
# Tokens on pumpswap, raydium, orca, meteora, etc. are POST-graduation and pass.
REJECT_PRE_GRADUATION_DEX_IDS = {
    "pumpfun",
    "four-meme",
    "fourmeme",
}

# Toggle to disable the filter entirely if we ever need to measure pre-grad
# performance for a research run. Default: enabled.
ENFORCE_GRADUATION_FILTER = True

# ─── Launch Momentum (Graduation) Tracker settings ────────────────────────────
# Polls pump.fun for tokens that just graduated from the bonding curve.
# Detection lag vs Dexscreener polling: ~1-2 minutes. Helius websocket
# (available 2026-06-20+) will close this to ~10 seconds when re-enabled.
#
# Kill switch: set LAUNCH_TRACKER_ENABLED=False in .env to pause without
# touching code. Default is True — tracker starts with the bot.
LAUNCH_TRACKER_ENABLED     = os.getenv("LAUNCH_TRACKER_ENABLED", "True").strip().lower() == "true"

LAUNCH_POLL_INTERVAL_SEC   = 60       # how often to poll pump.fun for new graduates
LAUNCH_MAX_AGE_MINUTES     = 15       # reject graduates older than this at signal time
LAUNCH_MIN_LIQUIDITY_USD   = 15_000   # must have real liquidity post-graduation
LAUNCH_MAX_MARKET_CAP_USD  = 500_000  # reject tokens that already ran (>7x from grad)
LAUNCH_MIN_VOLUME_USD      = 2_000    # minimum 1h volume — filters zero-interest listings

# ─── Chains for multi-chain Dexscreener monitoring ─────────────────────────────
MONITORED_CHAINS = [
    "solana",
    "ethereum",
    "bsc",
    "base",
]

# ─── Logging ───────────────────────────────────────────────────────────────────
LOG_LEVEL   = os.getenv("LOG_LEVEL", "INFO")
LOG_FILE    = "tradebot.log"
