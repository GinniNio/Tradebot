"""
jupiter_client.py — Jupiter v6 Swap API integration (Solana).

Jupiter is the primary DEX aggregator on Solana.
API docs: https://station.jup.ag/docs/apis/swap-api

No API key required. Transactions are signed with your wallet private key.

PAPER_TRADING mode (default=True in config): simulates trades without sending txs.
Set PAPER_TRADING=False in .env only when you are ready to trade real funds.
"""

import base64
import json
import logging
from typing import Optional

import aiohttp

# Solana signing libs are only required for LIVE trading.
# Paper mode skips them entirely so the bot runs without them installed.
# Install via: pip install solders solana   (uncomment lines in requirements.txt)
try:
    from solders.keypair import Keypair
    from solders.transaction import VersionedTransaction
    from solana.rpc.async_api import AsyncClient
    _SOLANA_LIBS_AVAILABLE = True
except ImportError:
    Keypair = None                  # type: ignore
    VersionedTransaction = None     # type: ignore
    AsyncClient = None              # type: ignore
    _SOLANA_LIBS_AVAILABLE = False

from config import (
    JUPITER_QUOTE_URL, JUPITER_SWAP_URL, HELIUS_RPC_URL,
    SOLANA_PRIVATE_KEY, SLIPPAGE_BPS, PAPER_TRADING,
)

logger = logging.getLogger(__name__)

# Well-known token mints
SOL_MINT  = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

# 1 SOL in lamports
LAMPORTS_PER_SOL = 1_000_000_000


class JupiterClient:
    """
    Wraps Jupiter v6 quote + swap endpoints.
    Signs and sends transactions via Helius RPC.
    """

    def __init__(self):
        self._wallet = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._rpc = None

        if PAPER_TRADING:
            # Paper mode: don't even try to load a wallet
            logger.debug("JupiterClient initialised in PAPER mode (no wallet loaded)")
            return

        if not _SOLANA_LIBS_AVAILABLE:
            logger.warning(
                "solders/solana libs not installed — live trading disabled. "
                "Run: pip install solders solana"
            )
            return

        if SOLANA_PRIVATE_KEY:
            try:
                self._wallet = Keypair.from_base58_string(SOLANA_PRIVATE_KEY)
                logger.info("Wallet loaded: %s", str(self._wallet.pubkey())[:16])
            except Exception as e:
                logger.error("Failed to load wallet private key: %s", e)
        else:
            logger.warning("SOLANA_PRIVATE_KEY not set — real swaps will fail.")

    async def __aenter__(self):
        self._session = aiohttp.ClientSession()
        if not PAPER_TRADING and _SOLANA_LIBS_AVAILABLE:
            self._rpc = AsyncClient(HELIUS_RPC_URL)
        return self

    async def __aexit__(self, *_):
        if self._session:
            await self._session.close()
        if self._rpc:
            await self._rpc.close()

    # ─── Quote ─────────────────────────────────────────────────────────────────

    async def get_quote(
        self,
        input_mint: str,
        output_mint: str,
        amount_lamports: int,
        slippage_bps: int = SLIPPAGE_BPS,
    ) -> dict:
        """
        Get a swap quote from Jupiter.
        Returns the quote response (includes route, price impact, outAmount, etc.)
        """
        params = {
            "inputMint":        input_mint,
            "outputMint":       output_mint,
            "amount":           str(amount_lamports),
            "slippageBps":      str(slippage_bps),
            "onlyDirectRoutes": "false",
        }
        try:
            async with self._session.get(JUPITER_QUOTE_URL, params=params) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.error("Jupiter quote error %d: %s", resp.status, text[:200])
                    return {}
                return await resp.json()
        except Exception as e:
            logger.error("Jupiter get_quote exception: %s", e)
            return {}

    def quote_price_usd(self, quote: dict, sol_price_usd: float) -> float:
        """
        Estimate USD value of the output tokens from a quote.
        For SOL→TOKEN swaps, approximates using SOL price.
        """
        in_amount  = int(quote.get("inAmount", 0))
        sol_spent  = in_amount / LAMPORTS_PER_SOL
        return sol_spent * sol_price_usd

    # ─── Swap ──────────────────────────────────────────────────────────────────

    async def execute_swap(
        self,
        quote: dict,
        dynamic_compute_unit_limit: bool = True,
        priority_fee_lamports: int = 5_000,
    ) -> dict:
        """
        Execute a swap using a quote object from get_quote().
        Returns {"success": bool, "tx_hash": str, "error": str}

        If PAPER_TRADING=True, logs the trade but does NOT send the transaction.
        """
        if not quote:
            return {"success": False, "error": "Empty quote"}

        if PAPER_TRADING:
            out_amount = int(quote.get("outAmount", 0))
            in_amount  = int(quote.get("inAmount", 0))
            logger.info(
                "[PAPER TRADE] SWAP %d lamports → %d output tokens | priceImpact=%.3f%%",
                in_amount, out_amount,
                float(quote.get("priceImpactPct", 0)) * 100
            )
            return {
                "success":  True,
                "tx_hash":  f"PAPER_{in_amount}_{out_amount}",
                "paper":    True,
                "in_amount": in_amount,
                "out_amount":out_amount,
            }

        if not _SOLANA_LIBS_AVAILABLE:
            return {
                "success": False,
                "error": "Live trading requires solders+solana packages. "
                         "Run: pip install solders solana"
            }

        if not self._wallet:
            return {"success": False, "error": "No wallet loaded"}

        # Step 1: get serialized transaction from Jupiter
        swap_payload = {
            "quoteResponse":           quote,
            "userPublicKey":           str(self._wallet.pubkey()),
            "wrapAndUnwrapSol":        True,
            "dynamicComputeUnitLimit": dynamic_compute_unit_limit,
            "prioritizationFeeLamports": priority_fee_lamports,
        }

        try:
            async with self._session.post(
                JUPITER_SWAP_URL,
                json=swap_payload,
                headers={"Content-Type": "application/json"},
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    return {"success": False, "error": f"Jupiter swap API error: {text[:200]}"}
                swap_data = await resp.json()
        except Exception as e:
            return {"success": False, "error": str(e)}

        # Step 2: decode, sign, and send transaction
        try:
            raw_tx = base64.b64decode(swap_data["swapTransaction"])
            tx = VersionedTransaction.from_bytes(raw_tx)
            tx.sign([self._wallet])

            result = await self._rpc.send_raw_transaction(
                bytes(tx),
                opts={"skipPreflight": False, "preflightCommitment": "confirmed"},
            )
            sig = str(result.value)
            logger.info("Swap sent: https://solscan.io/tx/%s", sig)
            return {"success": True, "tx_hash": sig}

        except Exception as e:
            logger.error("Swap signing/sending error: %s", e)
            return {"success": False, "error": str(e)}

    # ─── Convenience buy/sell ──────────────────────────────────────────────────

    async def buy_token(
        self,
        token_mint: str,
        sol_amount: float,
        sol_price_usd: float = 0.0,
    ) -> dict:
        """
        Buy `token_mint` using `sol_amount` SOL.
        Returns swap result dict.
        """
        lamports = int(sol_amount * LAMPORTS_PER_SOL)
        quote = await self.get_quote(
            input_mint=SOL_MINT,
            output_mint=token_mint,
            amount_lamports=lamports,
        )
        if not quote:
            return {"success": False, "error": "No quote returned"}

        price_impact = float(quote.get("priceImpactPct", 0)) * 100
        if price_impact > 10:
            logger.warning(
                "High price impact %.1f%% for %s — consider smaller size",
                price_impact, token_mint[:12]
            )

        result = await self.execute_swap(quote)
        result["quote"] = quote
        result["sol_spent"] = sol_amount
        result["price_impact_pct"] = price_impact
        return result

    async def sell_token(
        self,
        token_mint: str,
        token_amount: int,  # in token's smallest unit (e.g. lamports for SPL)
    ) -> dict:
        """
        Sell `token_amount` of `token_mint` back to SOL.
        """
        quote = await self.get_quote(
            input_mint=token_mint,
            output_mint=SOL_MINT,
            amount_lamports=token_amount,  # "lamports" is a misnomer here; it's the base unit
        )
        if not quote:
            return {"success": False, "error": "No quote for sell"}

        result = await self.execute_swap(quote)
        result["quote"] = quote
        return result

    # ─── Wallet balance ────────────────────────────────────────────────────────

    async def get_sol_balance(self) -> float:
        """Returns SOL balance of the trading wallet."""
        if not self._wallet or not self._rpc:
            return 0.0
        try:
            resp = await self._rpc.get_balance(self._wallet.pubkey())
            return resp.value / LAMPORTS_PER_SOL
        except Exception as e:
            logger.error("get_sol_balance error: %s", e)
            return 0.0
