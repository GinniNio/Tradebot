"""
risk_manager.py — Position sizing, stop loss, take profit, and trade gating.

All trade decisions pass through here before execution.
The risk manager is the last line of defence before a swap is sent.
"""

import logging
from database import get_open_positions, get_trade_stats
from config import (
    TRADE_SIZE_SOL, MAX_OPEN_POSITIONS, STOP_LOSS_PCT,
    TAKE_PROFIT_PCT, PAPER_TRADING,
    REJECT_PRE_GRADUATION_DEX_IDS, ENFORCE_GRADUATION_FILTER,
    strategy_can_open_position, strategy_for_signal_type,
)

logger = logging.getLogger(__name__)


class RiskManager:
    """
    Stateless decision layer — reads DB for current exposure,
    applies rules, returns go/no-go decisions with parameters.
    """

    def __init__(
        self,
        trade_size_sol: float = TRADE_SIZE_SOL,
        max_positions: int = MAX_OPEN_POSITIONS,
        stop_loss_pct: float = STOP_LOSS_PCT,
        take_profit_pct: float = TAKE_PROFIT_PCT,
    ):
        self.trade_size_sol = trade_size_sol
        self.max_positions  = max_positions
        self.stop_loss_pct  = stop_loss_pct
        self.take_profit_pct = take_profit_pct

    # ─── Pre-trade checks ──────────────────────────────────────────────────────

    def can_open_position(
        self,
        token_address: str,
        stats: dict,
        sol_balance: float = 999.0,
        signal_type: str = "wallet",
    ) -> tuple[bool, str]:
        """
        Returns (approved, reason_string).
        """
        # FIRST gate: strategy state machine. RESEARCH/PAUSED/BACKLOG/STOP
        # strategies can fire signals (logged for measurement) but cannot
        # open positions. Promote via STRATEGY_STATES in config.py.
        strategy_name = strategy_for_signal_type(signal_type)
        approved, reason = strategy_can_open_position(strategy_name)
        if not approved:
            return False, reason

        open_pos = get_open_positions()
        held = {p["token_address"] for p in open_pos}
        if token_address in held:
            return False, "Already holding this token"

        if len(open_pos) >= self.max_positions:
            return False, f"Max open positions ({self.max_positions}) reached"

        if sol_balance < self.trade_size_sol * 1.05:
            return False, f"Insufficient SOL balance: {sol_balance:.3f} < {self.trade_size_sol:.3f}"

        # Liquidity check — aligned with find_zombies.py threshold so picks
        # that just barely qualify for the watchlist still trade through.
        liquidity = stats.get("liquidity_usd", 0) or 0
        min_liq = 15_000 if signal_type == "zombie" else 10_000
        if liquidity < min_liq:
            return False, f"Liquidity too low: ${liquidity:,.0f} < ${min_liq:,.0f}"

        price = stats.get("price_usd", 0) or 0
        if price <= 0:
            return False, "Token price is 0 or unavailable"

        # Universal graduation filter — reject tokens still on a bonding curve.
        # Academic data: ~0.63% of pump.fun tokens graduate; the rest die. Trading
        # pre-graduation tokens means buying into a 99%-mortality sub-population.
        # Applies to all strategy_tags. Can be disabled via ENFORCE_GRADUATION_FILTER.
        if ENFORCE_GRADUATION_FILTER:
            dex = (stats.get("dex") or "").lower()
            if dex in REJECT_PRE_GRADUATION_DEX_IDS:
                return False, f"Pre-graduation (still on bonding curve, dex={dex})"

        return True, "OK"

    def compute_position(
        self,
        entry_price_usd: float,
        sol_price_usd: float,
        signal_type: str = "wallet",
    ) -> dict:
        """
        Compute stop loss, take profit, and size for a new position.
        Zombies get a wider take-profit (higher upside potential).
        """
        if signal_type == "zombie":
            sl_pct = self.stop_loss_pct
            tp_pct = self.take_profit_pct * 1.5
        else:
            sl_pct = self.stop_loss_pct
            tp_pct = self.take_profit_pct

        stop_loss_usd   = entry_price_usd * (1 - sl_pct / 100)
        take_profit_usd = entry_price_usd * (1 + tp_pct / 100)
        size_usd        = self.trade_size_sol * sol_price_usd

        return {
            "trade_size_sol":  self.trade_size_sol,
            "size_usd":        size_usd,
            "stop_loss_usd":   stop_loss_usd,
            "take_profit_usd": take_profit_usd,
            "stop_loss_pct":   sl_pct,
            "take_profit_pct": tp_pct,
        }

    # ─── Exit management ───────────────────────────────────────────────────────

    def find_exit_candidates(
        self,
        current_prices: dict,
    ) -> list[dict]:
        """
        DECISION-ONLY. Returns positions that should be exited NOW based on SL/TP.
        Caller is responsible for executing the sell and then calling
        database.close_position() with the actual exit tx + price.
        """
        open_pos = get_open_positions()
        exits = []

        for pos in open_pos:
            token_addr = pos["token_address"]
            current_px = current_prices.get(token_addr)
            if current_px is None:
                continue

            sl = pos["stop_loss_usd"]
            tp = pos["take_profit_usd"]
            reason = None
            if sl is not None and current_px <= sl:
                reason = "stop_loss"
            elif tp is not None and current_px >= tp:
                reason = "take_profit"
            if not reason:
                continue

            entry_px = pos["entry_price_usd"] or 0
            pnl_pct = ((current_px - entry_px) / entry_px * 100) if entry_px > 0 else 0.0

            exits.append({
                "position_id":         pos["id"],
                "chain":               pos.get("chain"),
                "token_address":       token_addr,
                "token_symbol":        pos.get("token_symbol") or token_addr[:8],
                "entry_token_amount":  pos.get("entry_token_amount") or 0,
                "entry_price_usd":     entry_px,
                "current_price":       current_px,
                "exit_reason":         reason,
                "pnl_pct":             pnl_pct,
            })

        return exits

    # ─── Stats / reporting ─────────────────────────────────────────────────────

    def print_summary(self):
        stats = get_trade_stats()
        open_pos = get_open_positions()

        total  = stats.get("total_trades") or 0
        wins   = stats.get("wins") or 0
        pnl    = stats.get("total_pnl_usd") or 0.0
        avg    = stats.get("avg_pnl_pct") or 0.0
        wr     = (wins / total * 100) if total > 0 else 0

        logger.info("=" * 50)
        logger.info("TRADE SUMMARY")
        logger.info("  Completed trades : %d (win rate %.1f%%)", total, wr)
        logger.info("  Total PnL        : $%.2f", pnl)
        logger.info("  Avg PnL/trade    : %.1f%%", avg)
        logger.info("  Open positions   : %d / %d", len(open_pos), self.max_positions)
        logger.info("  Mode             : %s", "PAPER" if PAPER_TRADING else "LIVE")
        logger.info("=" * 50)

        return {
            "total_trades": total,
            "win_rate_pct": wr,
            "total_pnl_usd": pnl,
            "avg_pnl_pct": avg,
            "open_positions": len(open_pos),
        }
