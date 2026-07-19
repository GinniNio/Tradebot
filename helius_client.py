"""
helius_client.py — Helius Enhanced Transactions API wrapper (Solana).

Uses Helius's "parsed transactions by address" endpoint to fetch swap activity
for tracked wallets. Replaces the Birdeye /v1/wallet/tx_list path for free-tier
operation.

Free tier: 1M credits/month, 10 RPC req/s, 2 req/s on Enhanced APIs.
Endpoint: https://api-mainnet.helius-rpc.com/v0/addresses/{addr}/transactions

Auth: ?api-key=YOUR_HELIUS_API_KEY query param. No headers required.

Sign up at https://dashboard.helius.dev/signup?plan=free
"""

import asyncio
import logging
from typing import Optional

import aiohttp

from config import HELIUS_API_KEY

logger = logging.getLogger(__name__)

# Enhanced Transactions API base URL (parsed transactions, not raw RPC)
HELIUS_ENHANCED_BASE = "https://api-mainnet.helius-rpc.com/v0"

# Common Solana mints we don't want to track as "buys" of interest
_SKIP_MINTS = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
    "So11111111111111111111111111111111111111112",   # wSOL (native marker)
}


class HeliusClient:
    """Async client for Helius's parsed-transactions endpoint."""

    def __init__(self, api_key: str = HELIUS_API_KEY):
        if not api_key:
            logger.warning("HELIUS_API_KEY not set — Helius calls will fail.")
        self.api_key = api_key
        self._session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self):
        self._session = aiohttp.ClientSession(headers={"accept": "application/json"})
        return self

    async def __aexit__(self, *_):
        if self._session:
            await self._session.close()

    async def get_address_transactions(
        self,
        address: str,
        tx_type: str = "SWAP",
        limit: int = 20,
        before: Optional[str] = None,
    ) -> list[dict]:
        """
        Fetch enhanced parsed transactions for a wallet, optionally filtered by type.

        tx_type: "SWAP" by default. Other useful values: "TRANSFER", "NFT_SALE", etc.
                 Pass None/empty to get every tx type.
        limit:   max 100 per request (Helius cap).
        before:  signature to paginate before (for older txs). Usually None for live polling.

        Returns list of parsed-tx dicts. Empty list on error.
        """
        if not self._session:
            raise RuntimeError("Use HeliusClient as an async context manager.")
        if not self.api_key:
            return []

        url = f"{HELIUS_ENHANCED_BASE}/addresses/{address}/transactions"
        params = {"api-key": self.api_key, "limit": str(limit)}
        if tx_type:
            params["type"] = tx_type
        if before:
            params["before"] = before

        try:
            async with self._session.get(url, params=params) as resp:
                if resp.status == 401:
                    logger.error("Helius: invalid API key")
                    return []
                if resp.status == 429:
                    logger.warning("Helius: rate limited — backing off 5s")
                    await asyncio.sleep(5)
                    return []
                if resp.status != 200:
                    text = await resp.text()
                    logger.error("Helius error %d for %s: %s", resp.status, address[:8], text[:200])
                    return []
                data = await resp.json()
                return data if isinstance(data, list) else []
        except aiohttp.ClientError as e:
            logger.error("Helius request error for %s: %s", address[:8], e)
            return []

    @staticmethod
    def parse_swap_for_tracker(tx: dict, wallet: str) -> Optional[dict]:
        """
        Normalize a Helius enhanced-tx swap record into the shape wallet_tracker expects.

        Returns {
            "tx_hash": signature,
            "side": "buy" | "sell",
            "token_address": str,
            "token_symbol": str,
            "amount_usd": float,
        } — or None if this isn't a usable swap for our purposes.

        Logic: look at tokenTransfers; the leg where `toUserAccount == wallet` and the
        mint isn't a stablecoin/wSOL is the token being acquired ("buy"). The leg
        with `fromUserAccount == wallet` and a non-stable mint is a "sell".
        """
        sig = tx.get("signature")
        if not sig:
            return None

        # Helius normalizes swap data into tokenTransfers + events.swap
        token_transfers = tx.get("tokenTransfers") or []
        if not token_transfers:
            return None

        bought = None    # leg where this wallet RECEIVED a non-stable token
        sold = None      # leg where this wallet SENT a non-stable token
        for t in token_transfers:
            mint = t.get("mint") or ""
            if mint in _SKIP_MINTS:
                continue
            if t.get("toUserAccount") == wallet and bought is None:
                bought = t
            elif t.get("fromUserAccount") == wallet and sold is None:
                sold = t

        # Prefer "buy" classification — that's what triggers wallet signals
        if bought:
            side = "buy"
            chosen = bought
        elif sold:
            side = "sell"
            chosen = sold
        else:
            return None

        # USD value: Helius's enhanced tx sometimes provides nativeTransfers in lamports
        # but no consistent USD. We'll leave amount_usd = 0 here; the wallet tracker
        # logic doesn't actually require USD to count buys.
        return {
            "tx_hash": sig,
            "side": side,
            "token_address": chosen.get("mint"),
            "token_symbol": chosen.get("tokenSymbol", "") or "",  # rarely populated
            "amount_usd": 0.0,
        }
