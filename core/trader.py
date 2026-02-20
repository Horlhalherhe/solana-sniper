"""
SOLANA TRADER
Executes swaps via Jupiter aggregator
"""

import os
import json
import base58
from typing import Optional, Tuple
from dataclasses import dataclass
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction
import httpx
import asyncio

JUPITER_API = "https://quote-api.jup.ag/v6"
HELIUS_RPC = os.getenv("HELIUS_API_KEY", "")

@dataclass
class TradeResult:
    success: bool
    signature: Optional[str] = None
    amount_out: float = 0.0
    error: Optional[str] = None
    slippage_pct: float = 0.0

class SolanaTrader:
    def __init__(self, private_key_base58: str):
        """Initialize trader with wallet private key (base58 encoded)"""
        self.keypair = Keypair.from_base58_string(private_key_base58)
        self.wallet_address = str(self.keypair.pubkey())
        self.rpc_url = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_RPC}"
    
    async def get_sol_balance(self) -> float:
        """Get wallet SOL balance"""
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    self.rpc_url,
                    json={"jsonrpc": "2.0", "id": 1, "method": "getBalance",
                          "params": [self.wallet_address]}
                )
                if resp.status_code == 200:
                    lamports = resp.json().get("result", {}).get("value", 0)
                    return lamports / 1e9
        except Exception as e:
            print(f"[TRADER] Balance check failed: {e}")
        return 0.0
    
    async def get_token_balance(self, mint: str) -> float:
        """Get wallet token balance"""
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    self.rpc_url,
                    json={"jsonrpc": "2.0", "id": 1, "method": "getTokenAccountsByOwner",
                          "params": [
                              self.wallet_address,
                              {"mint": mint},
                              {"encoding": "jsonParsed"}
                          ]}
                )
                if resp.status_code == 200:
                    accounts = resp.json().get("result", {}).get("value", [])
                    if accounts:
                        amount = accounts[0]["account"]["data"]["parsed"]["info"]["tokenAmount"]
                        return float(amount["uiAmount"] or 0)
        except Exception as e:
            print(f"[TRADER] Token balance check failed: {e}")
        return 0.0
    
    async def buy_token(self, mint: str, sol_amount: float, slippage_bps: int = 1000) -> TradeResult:
        """
        Buy token with SOL via Jupiter
        
        Args:
            mint: Token mint address
            sol_amount: Amount of SOL to spend
            slippage_bps: Slippage tolerance in basis points (1000 = 10%)
        """
        try:
            # Get quote
            quote = await self._get_jupiter_quote(
                input_mint="So11111111111111111111111111111111111111112",  # SOL
                output_mint=mint,
                amount=int(sol_amount * 1e9),  # Convert to lamports
                slippage_bps=slippage_bps
            )
            if not quote:
                return TradeResult(success=False, error="Quote failed")
            
            # Get swap transaction
            swap_tx = await self._get_jupiter_swap(quote)
            if not swap_tx:
                return TradeResult(success=False, error="Swap transaction failed")
            
            # Sign and send
            signature = await self._sign_and_send(swap_tx)
            if not signature:
                return TradeResult(success=False, error="Transaction failed")
            
            # Calculate output amount
            out_amount = float(quote.get("outAmount", 0)) / 1e9  # Assuming 9 decimals
            
            return TradeResult(
                success=True,
                signature=signature,
                amount_out=out_amount,
                slippage_pct=slippage_bps / 100
            )
        
        except Exception as e:
            return TradeResult(success=False, error=str(e))
    
    async def sell_token(self, mint: str, token_amount: float, slippage_bps: int = 1000) -> TradeResult:
        """
        Sell token for SOL via Jupiter
        
        Args:
            mint: Token mint address
            token_amount: Amount of tokens to sell
            slippage_bps: Slippage tolerance in basis points
        """
        try:
            # Get quote
            quote = await self._get_jupiter_quote(
                input_mint=mint,
                output_mint="So11111111111111111111111111111111111111112",  # SOL
                amount=int(token_amount * 1e9),  # Assuming 9 decimals
                slippage_bps=slippage_bps
            )
            if not quote:
                return TradeResult(success=False, error="Quote failed")
            
            # Get swap transaction
            swap_tx = await self._get_jupiter_swap(quote)
            if not swap_tx:
                return TradeResult(success=False, error="Swap transaction failed")
            
            # Sign and send
            signature = await self._sign_and_send(swap_tx)
            if not signature:
                return TradeResult(success=False, error="Transaction failed")
            
            # Calculate SOL received
            sol_out = float(quote.get("outAmount", 0)) / 1e9
            
            return TradeResult(
                success=True,
                signature=signature,
                amount_out=sol_out,
                slippage_pct=slippage_bps / 100
            )
        
        except Exception as e:
            return TradeResult(success=False, error=str(e))
    
    async def _get_jupiter_quote(self, input_mint: str, output_mint: str, 
                                   amount: int, slippage_bps: int) -> Optional[dict]:
        """Get Jupiter quote"""
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    f"{JUPITER_API}/quote",
                    params={
                        "inputMint": input_mint,
                        "outputMint": output_mint,
                        "amount": amount,
                        "slippageBps": slippage_bps,
                    }
                )
                if resp.status_code == 200:
                    return resp.json()
        except Exception as e:
            print(f"[TRADER] Jupiter quote error: {e}")
        return None
    
    async def _get_jupiter_swap(self, quote: dict) -> Optional[str]:
        """Get swap transaction from Jupiter"""
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    f"{JUPITER_API}/swap",
                    json={
                        "quoteResponse": quote,
                        "userPublicKey": self.wallet_address,
                        "wrapAndUnwrapSol": True,
                    }
                )
                if resp.status_code == 200:
                    return resp.json().get("swapTransaction")
        except Exception as e:
            print(f"[TRADER] Jupiter swap error: {e}")
        return None
    
    async def _sign_and_send(self, swap_transaction_base64: str) -> Optional[str]:
        """Sign and send transaction"""
        try:
            # Decode transaction
            tx_bytes = base58.b58decode(swap_transaction_base64)
            tx = VersionedTransaction.from_bytes(tx_bytes)
            
            # Sign transaction
            tx.sign([self.keypair])
            
            # Send transaction
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    self.rpc_url,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "sendTransaction",
                        "params": [
                            base58.b58encode(bytes(tx)).decode('utf-8'),
                            {"encoding": "base58", "skipPreflight": True}
                        ]
                    }
                )
                if resp.status_code == 200:
                    result = resp.json().get("result")
                    if result:
                        # Wait for confirmation
                        await self._confirm_transaction(result)
                        return result
        except Exception as e:
            print(f"[TRADER] Sign/send error: {e}")
        return None
    
    async def _confirm_transaction(self, signature: str, max_attempts: int = 30):
        """Wait for transaction confirmation"""
        for _ in range(max_attempts):
            try:
                async with httpx.AsyncClient(timeout=5) as client:
                    resp = await client.post(
                        self.rpc_url,
                        json={
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "getSignatureStatuses",
                            "params": [[signature]]
                        }
                    )
                    if resp.status_code == 200:
                        result = resp.json().get("result", {}).get("value", [])
                        if result and result[0]:
                            status = result[0]
                            if status.get("confirmationStatus") in ["confirmed", "finalized"]:
                                return True
                            elif status.get("err"):
                                raise Exception(f"Transaction failed: {status['err']}")
            except Exception as e:
                print(f"[TRADER] Confirmation check error: {e}")
            await asyncio.sleep(2)
        raise Exception("Transaction confirmation timeout")
