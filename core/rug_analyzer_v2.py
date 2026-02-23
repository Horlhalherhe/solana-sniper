"""
RUG ANALYZER v2
===============
Major upgrade over v1:
- Actually fetches on-chain data (v1 was manual-only)
- Honeypot simulation via Jupiter quote
- Wallet cluster detection (funded from same source)
- Bundle/sniper detection in early blocks
- Deployer wallet deep analysis
"""

import os
import asyncio
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
from typing import Optional
from collections import defaultdict
import httpx

HELIUS_API_KEY = os.getenv("HELIUS_API_KEY", "")
BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY", "")
HELIUS_RPC = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
HELIUS_API = f"https://api.helius.xyz/v0"
JUPITER_API = "https://quote-api.jup.ag/v6"

# ── Risk Flags ───────────────────────────────────────────────────────

@dataclass
class RiskFlag:
    code: str
    severity: str       # critical, high, medium, low
    description: str
    score_impact: float

    def to_dict(self):
        return {"code": self.code, "severity": self.severity,
                "description": self.description, "impact": self.score_impact}

FLAGS = {
    # Contract
    "MINT_AUTH_ACTIVE":        RiskFlag("MINT_AUTH_ACTIVE",        "critical", "Mint authority not revoked — can print tokens", 3.0),
    "FREEZE_AUTH_ACTIVE":      RiskFlag("FREEZE_AUTH_ACTIVE",      "critical", "Freeze authority active — can freeze your wallet", 2.5),
    "HONEYPOT_DETECTED":       RiskFlag("HONEYPOT_DETECTED",       "critical", "Cannot sell — honeypot confirmed", 5.0),
    "HONEYPOT_SUSPICIOUS":     RiskFlag("HONEYPOT_SUSPICIOUS",     "high",     "Sell quote much worse than buy — possible honeypot", 2.0),
    
    # Liquidity
    "LP_NOT_LOCKED":           RiskFlag("LP_NOT_LOCKED",           "critical", "Liquidity not locked or burned — can rug", 3.0),
    "LP_LOW":                  RiskFlag("LP_LOW",                  "high",     "Liquidity under $10k", 1.5),
    "LP_UNLOCK_SOON":          RiskFlag("LP_UNLOCK_SOON",          "high",     "Lock expires within 7 days", 2.0),
    
    # Holders
    "TOP10_OVER_30":           RiskFlag("TOP10_OVER_30",           "high",     "Top 10 wallets hold >30%", 2.5),
    "TOP1_OVER_5":             RiskFlag("TOP1_OVER_5",             "high",     "Single wallet holds >5%", 2.0),
    "WALLET_CLUSTER":          RiskFlag("WALLET_CLUSTER",          "high",     "Multiple top holders funded from same source", 2.5),
    "HEAVILY_BUNDLED":         RiskFlag("HEAVILY_BUNDLED",         "high",     "Many snipers bought in block 0", 1.5),
    
    # Deployer
    "DEPLOYER_RUGGED_BEFORE":  RiskFlag("DEPLOYER_RUGGED_BEFORE",  "critical", "Deployer linked to previous rug", 3.0),
    "DEPLOYER_SERIAL":         RiskFlag("DEPLOYER_SERIAL",         "high",     "Deployer launched 5+ tokens recently", 2.0),
    "DEPLOYER_FRESH_WALLET":   RiskFlag("DEPLOYER_FRESH_WALLET",   "medium",   "Deployer wallet < 7 days old", 1.0),
    "DEPLOYER_LOW_SOL":        RiskFlag("DEPLOYER_LOW_SOL",        "medium",   "Deployer has < 0.5 SOL remaining", 0.5),
    
    # Metadata
    "NO_SOCIAL":               RiskFlag("NO_SOCIAL",               "low",      "No social links in metadata", 0.5),
    "COPYCAT_NAME":            RiskFlag("COPYCAT_NAME",            "medium",   "Name similar to established token", 1.0),
}

# ── Data Classes ─────────────────────────────────────────────────────

@dataclass
class HolderData:
    total_holders: int = 0
    top1_pct: float = 0.0
    top5_pct: float = 0.0
    top10_pct: float = 0.0
    dev_holds_pct: float = 0.0
    wallet_clusters_detected: int = 0
    cluster_details: list = field(default_factory=list)

@dataclass
class LiquidityData:
    pool_address: str = ""
    liquidity_usd: float = 0.0
    is_burned: bool = False
    is_locked: bool = False
    lock_expiry_days: Optional[int] = None
    pool_age_hours: float = 0.0

@dataclass
class DeployerData:
    wallet: str = ""
    age_days: int = 0
    sol_balance: float = 0.0
    previous_tokens: list = field(default_factory=list)
    rugged_tokens: list = field(default_factory=list)
    tokens_last_30d: int = 0
    verified_clean: bool = False

@dataclass  
class HoneypotResult:
    can_buy: bool = False
    can_sell: bool = False
    buy_tax_pct: float = 0.0
    sell_tax_pct: float = 0.0
    is_honeypot: bool = False
    is_suspicious: bool = False

@dataclass
class BundleData:
    early_buyer_count: int = 0
    unique_signers_block0: int = 0
    sniper_estimate: int = 0
    is_heavily_bundled: bool = False

@dataclass
class RugReport:
    mint: str
    name: str = ""
    symbol: str = ""
    rug_score: float = 1.0
    flags: list = field(default_factory=list)
    holder_data: Optional[HolderData] = None
    liquidity_data: Optional[LiquidityData] = None
    deployer_data: Optional[DeployerData] = None
    honeypot_data: Optional[HoneypotResult] = None
    bundle_data: Optional[BundleData] = None
    verdict: str = ""
    analysis_time: str = ""

    def to_dict(self):
        return {
            "mint": self.mint,
            "name": self.name,
            "symbol": self.symbol,
            "rug_score": round(self.rug_score, 2),
            "verdict": self.verdict,
            "flags": [f.to_dict() if isinstance(f, RiskFlag) else f for f in self.flags],
            "holder_data": {
                "total": self.holder_data.total_holders,
                "top1_pct": self.holder_data.top1_pct,
                "top10_pct": self.holder_data.top10_pct,
                "clusters": self.holder_data.wallet_clusters_detected,
            } if self.holder_data else None,
            "liquidity": {
                "usd": self.liquidity_data.liquidity_usd,
                "burned": self.liquidity_data.is_burned,
                "locked": self.liquidity_data.is_locked,
            } if self.liquidity_data else None,
            "deployer": {
                "wallet": self.deployer_data.wallet,
                "age_days": self.deployer_data.age_days,
                "prev_tokens": self.deployer_data.tokens_last_30d,
                "rugs": len(self.deployer_data.rugged_tokens),
            } if self.deployer_data else None,
            "honeypot": {
                "can_sell": self.honeypot_data.can_sell,
                "sell_tax": self.honeypot_data.sell_tax_pct,
                "is_honeypot": self.honeypot_data.is_honeypot,
            } if self.honeypot_data else None,
            "bundles": {
                "snipers": self.bundle_data.sniper_estimate,
                "heavily_bundled": self.bundle_data.is_heavily_bundled,
            } if self.bundle_data else None,
        }


# ── Main Analyzer ────────────────────────────────────────────────────

class RugAnalyzerV2:
    """
    Full on-chain rug analysis.
    
    Unlike v1 which required manual data input, v2 fetches everything
    from the blockchain using Helius + Birdeye APIs.
    """
    
    def __init__(self, meta_intel=None):
        """
        Args:
            meta_intel: Optional MetaIntelligence instance for deployer history lookups
        """
        self.meta_intel = meta_intel
    
    async def analyze(self, session: httpx.AsyncClient, mint: str,
                      deployer: str = None, name: str = "", symbol: str = "") -> RugReport:
        """
        Run full on-chain rug analysis.
        
        This is the main entry point. It fetches all data from chain
        and returns a complete rug report.
        """
        print(f"[RugV2] Analyzing {mint[:16]}...")
        start = datetime.now(timezone.utc)
        
        # Fetch all data in parallel where possible
        contract_data, holder_data, lp_data, deployer_data, honeypot_data, bundle_data = (
            await asyncio.gather(
                self._check_contract(session, mint),
                self._check_holders(session, mint, deployer),
                self._check_liquidity(session, mint),
                self._check_deployer(session, deployer or "", mint),
                self._check_honeypot(session, mint),
                self._check_bundles(session, mint),
                return_exceptions=True,
            )
        )
        
        # Handle exceptions from parallel execution
        if isinstance(contract_data, Exception):
            print(f"  [RugV2] Contract check failed: {contract_data}")
            contract_data = {}
        if isinstance(holder_data, Exception):
            print(f"  [RugV2] Holder check failed: {holder_data}")
            holder_data = HolderData()
        if isinstance(lp_data, Exception):
            print(f"  [RugV2] LP check failed: {lp_data}")
            lp_data = LiquidityData()
        if isinstance(deployer_data, Exception):
            print(f"  [RugV2] Deployer check failed: {deployer_data}")
            deployer_data = DeployerData(wallet=deployer or "")
        if isinstance(honeypot_data, Exception):
            print(f"  [RugV2] Honeypot check failed: {honeypot_data}")
            honeypot_data = HoneypotResult()
        if isinstance(bundle_data, Exception):
            print(f"  [RugV2] Bundle check failed: {bundle_data}")
            bundle_data = BundleData()
        
        # Evaluate flags
        flags = self._evaluate_flags(contract_data, holder_data, lp_data, 
                                      deployer_data, honeypot_data, bundle_data)
        
        # Calculate rug score
        rug_score = self._calculate_score(flags)
        verdict = self._verdict(rug_score)
        
        elapsed = (datetime.now(timezone.utc) - start).total_seconds()
        print(f"[RugV2] Score: {rug_score}/10 — {verdict} ({elapsed:.1f}s)")
        
        return RugReport(
            mint=mint, name=name, symbol=symbol,
            rug_score=rug_score, flags=flags,
            holder_data=holder_data, liquidity_data=lp_data,
            deployer_data=deployer_data, honeypot_data=honeypot_data,
            bundle_data=bundle_data, verdict=verdict,
            analysis_time=start.isoformat(),
        )
    
    # Also keep manual analysis for backward compatibility
    def analyze_manual(self, data: dict) -> RugReport:
        """Legacy manual analysis — same as v1"""
        holder_data = HolderData(
            total_holders=data.get("total_holders", 0),
            top1_pct=data.get("top1_pct", 0),
            top5_pct=data.get("top5_pct", 0),
            top10_pct=data.get("top10_pct", 0),
            dev_holds_pct=data.get("dev_holds_pct", 0),
            wallet_clusters_detected=data.get("wallet_clusters", 0),
        )
        lp_data = LiquidityData(
            liquidity_usd=data.get("liquidity_usd", 0),
            is_burned=data.get("lp_burned", False),
            is_locked=data.get("lp_locked", False),
            lock_expiry_days=data.get("lp_lock_days"),
        )
        deployer_data = DeployerData(
            wallet=data.get("deployer", ""),
            age_days=data.get("deployer_age_days", 0),
            rugged_tokens=data.get("deployer_prev_rugs", []),
        )
        contract_data = {
            "mint_revoked": data.get("mint_authority_revoked", False),
            "freeze_revoked": data.get("freeze_authority_revoked", False),
            "has_social": data.get("has_social", True),
        }
        honeypot_data = HoneypotResult()
        bundle_data = BundleData()
        
        flags = self._evaluate_flags(contract_data, holder_data, lp_data,
                                      deployer_data, honeypot_data, bundle_data)
        rug_score = self._calculate_score(flags)
        
        return RugReport(
            mint=data.get("mint", "manual"),
            rug_score=rug_score, flags=flags,
            holder_data=holder_data, liquidity_data=lp_data,
            deployer_data=deployer_data, honeypot_data=honeypot_data,
            bundle_data=bundle_data, verdict=self._verdict(rug_score),
            analysis_time=datetime.now(timezone.utc).isoformat(),
        )
    
    # =========================================================
    # ON-CHAIN CHECKS
    # =========================================================
    
    async def _check_contract(self, session: httpx.AsyncClient, mint: str) -> dict:
        """Check mint/freeze authority status"""
        result = {"mint_revoked": False, "freeze_revoked": False, "has_social": False}
        
        try:
            resp = await session.post(
                HELIUS_RPC,
                json={"jsonrpc": "2.0", "id": 1, "method": "getAccountInfo",
                      "params": [mint, {"encoding": "jsonParsed"}]},
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json().get("result", {}).get("value", {})
                if data:
                    parsed = data.get("data", {}).get("parsed", {}).get("info", {})
                    result["mint_revoked"] = parsed.get("mintAuthority") is None
                    result["freeze_revoked"] = parsed.get("freezeAuthority") is None
        except Exception as e:
            print(f"  [RugV2] Contract check error: {e}")
        
        # Check metadata for social links
        try:
            resp = await session.post(
                HELIUS_RPC,
                json={"jsonrpc": "2.0", "id": 1, "method": "getAsset",
                      "params": {"id": mint}},
                timeout=10,
            )
            if resp.status_code == 200:
                asset = resp.json().get("result", {})
                content = asset.get("content", {})
                links = content.get("links", {})
                if links and any(links.values()):
                    result["has_social"] = True
        except:
            pass
        
        return result
    
    async def _check_holders(self, session: httpx.AsyncClient, mint: str,
                              deployer: str = None) -> HolderData:
        """Fetch holder distribution and detect wallet clusters"""
        data = HolderData()
        
        try:
            # Get largest holders
            resp = await session.post(
                HELIUS_RPC,
                json={"jsonrpc": "2.0", "id": 1, "method": "getTokenLargestAccounts",
                      "params": [mint]},
                timeout=10,
            )
            if resp.status_code != 200:
                return data
            
            accounts = resp.json().get("result", {}).get("value", [])
            
            # Get total supply
            supply_resp = await session.post(
                HELIUS_RPC,
                json={"jsonrpc": "2.0", "id": 1, "method": "getTokenSupply",
                      "params": [mint]},
                timeout=10,
            )
            total_supply = 0
            if supply_resp.status_code == 200:
                total_supply = float(
                    supply_resp.json().get("result", {}).get("value", {}).get("uiAmount", 0)
                )
            
            if not accounts or total_supply == 0:
                return data
            
            # Calculate holder percentages
            holders = []
            for acc in accounts:
                amount = float(acc.get("uiAmount", 0) or 0)
                pct = (amount / total_supply * 100) if total_supply > 0 else 0
                holders.append({"address": acc.get("address", ""), "pct": pct})
            
            data.total_holders = len(holders)
            if holders:
                data.top1_pct = round(holders[0]["pct"], 2)
                data.top5_pct = round(sum(h["pct"] for h in holders[:5]), 2)
                data.top10_pct = round(sum(h["pct"] for h in holders[:10]), 2)
            
            # Check if deployer holds tokens
            if deployer:
                for h in holders:
                    if h["address"] == deployer:
                        data.dev_holds_pct = round(h["pct"], 2)
            
            # Detect wallet clusters (top holders funded from same source)
            data.wallet_clusters_detected = await self._detect_clusters(
                session, [h["address"] for h in holders[:10]]
            )
            
        except Exception as e:
            print(f"  [RugV2] Holder check error: {e}")
        
        return data
    
    async def _detect_clusters(self, session: httpx.AsyncClient,
                                holder_addresses: list) -> int:
        """
        Check if top holders were funded from the same source wallet.
        This is a common rug pattern — deployer creates multiple wallets,
        funds them all from one source, then buys the token.
        """
        if len(holder_addresses) < 3:
            return 0
        
        funding_sources = defaultdict(list)
        
        for addr in holder_addresses[:8]:  # Check top 8 to save API calls
            try:
                resp = await session.post(
                    HELIUS_RPC,
                    json={"jsonrpc": "2.0", "id": 1,
                          "method": "getSignaturesForAddress",
                          "params": [addr, {"limit": 5}]},
                    timeout=10,
                )
                if resp.status_code == 200:
                    sigs = resp.json().get("result", [])
                    if sigs:
                        # Check the oldest transactions — likely the funding tx
                        oldest_sig = sigs[-1].get("signature", "")
                        if oldest_sig:
                            tx_resp = await session.post(
                                f"{HELIUS_API}/transactions",
                                params={"api-key": HELIUS_API_KEY},
                                json={"transactions": [oldest_sig]},
                                timeout=10,
                            )
                            if tx_resp.status_code == 200:
                                txns = tx_resp.json()
                                if txns:
                                    fee_payer = txns[0].get("feePayer", "")
                                    if fee_payer:
                                        funding_sources[fee_payer].append(addr)
                
                await asyncio.sleep(0.15)
            except:
                continue
        
        # Count clusters (same source funding 2+ holders)
        clusters = sum(1 for source, funded in funding_sources.items() 
                       if len(funded) >= 2)
        
        if clusters > 0:
            print(f"  [RugV2] ⚠️ {clusters} wallet cluster(s) detected")
        
        return clusters
    
    async def _check_liquidity(self, session: httpx.AsyncClient, mint: str) -> LiquidityData:
        """Check liquidity pool data via Birdeye"""
        data = LiquidityData()
        
        if not BIRDEYE_API_KEY:
            return data
        
        try:
            resp = await session.get(
                f"https://public-api.birdeye.so/defi/token_overview",
                params={"address": mint},
                headers={"X-API-KEY": BIRDEYE_API_KEY},
                timeout=10,
            )
            if resp.status_code == 200:
                overview = resp.json().get("data", {})
                data.liquidity_usd = overview.get("liquidity", 0) or 0
                
                # Check creation time for pool age
                created = overview.get("createdAt")
                if created:
                    try:
                        created_dt = datetime.fromtimestamp(created, tz=timezone.utc)
                        data.pool_age_hours = (
                            datetime.now(timezone.utc) - created_dt
                        ).total_seconds() / 3600
                    except:
                        pass
        except Exception as e:
            print(f"  [RugV2] Liquidity check error: {e}")
        
        return data
    
    async def _check_deployer(self, session: httpx.AsyncClient, 
                               deployer: str, mint: str) -> DeployerData:
        """Deep analysis of the deployer wallet"""
        data = DeployerData(wallet=deployer)
        
        if not deployer:
            # Try to find deployer from token's first transaction
            deployer = await self._find_deployer(session, mint)
            data.wallet = deployer
        
        if not deployer:
            return data
        
        # Check deployer SOL balance
        try:
            resp = await session.post(
                HELIUS_RPC,
                json={"jsonrpc": "2.0", "id": 1, "method": "getBalance",
                      "params": [deployer]},
                timeout=10,
            )
            if resp.status_code == 200:
                lamports = resp.json().get("result", {}).get("value", 0)
                data.sol_balance = lamports / 1e9
        except:
            pass
        
        # Check deployer transaction history for wallet age & token launches
        try:
            resp = await session.post(
                HELIUS_RPC,
                json={"jsonrpc": "2.0", "id": 1,
                      "method": "getSignaturesForAddress",
                      "params": [deployer, {"limit": 50}]},
                timeout=10,
            )
            if resp.status_code == 200:
                sigs = resp.json().get("result", [])
                if sigs:
                    # Wallet age from oldest tx
                    oldest_time = sigs[-1].get("blockTime", 0)
                    if oldest_time:
                        age_seconds = datetime.now(timezone.utc).timestamp() - oldest_time
                        data.age_days = int(age_seconds / 86400)
                    
                    # Count recent token launches (rough heuristic)
                    thirty_days_ago = datetime.now(timezone.utc).timestamp() - (30 * 86400)
                    recent_txns = [s for s in sigs if s.get("blockTime", 0) > thirty_days_ago]
                    data.tokens_last_30d = len(recent_txns)  # Rough proxy
        except:
            pass
        
        # Check meta intelligence for known rug history
        if self.meta_intel:
            deployer_info = self.meta_intel.get_deployer(deployer)
            if deployer_info:
                data.rugged_tokens = deployer_info.get("rugged_tokens", [])
                data.verified_clean = deployer_info.get("risk_rating") == "clean"
        
        return data
    
    async def _find_deployer(self, session: httpx.AsyncClient, mint: str) -> str:
        """Find the deployer/creator of a token"""
        try:
            resp = await session.post(
                HELIUS_RPC,
                json={"jsonrpc": "2.0", "id": 1,
                      "method": "getSignaturesForAddress",
                      "params": [mint, {"limit": 5}]},
                timeout=10,
            )
            if resp.status_code == 200:
                sigs = resp.json().get("result", [])
                if sigs:
                    oldest_sig = sigs[-1].get("signature", "")
                    if oldest_sig:
                        tx_resp = await session.post(
                            f"{HELIUS_API}/transactions",
                            params={"api-key": HELIUS_API_KEY},
                            json={"transactions": [oldest_sig]},
                            timeout=10,
                        )
                        if tx_resp.status_code == 200:
                            txns = tx_resp.json()
                            if txns:
                                return txns[0].get("feePayer", "")
        except:
            pass
        return ""
    
    async def _check_honeypot(self, session: httpx.AsyncClient, mint: str) -> HoneypotResult:
        """
        Simulate a buy AND sell via Jupiter to detect honeypots.
        
        If we can get a buy quote but NOT a sell quote (or sell quote
        has massive price impact), it's a honeypot.
        """
        result = HoneypotResult()
        SOL_MINT = "So11111111111111111111111111111111111111112"
        TEST_AMOUNT = int(0.01 * 1e9)  # 0.01 SOL test
        
        try:
            # Try to get a BUY quote
            buy_resp = await session.get(
                f"{JUPITER_API}/quote",
                params={
                    "inputMint": SOL_MINT,
                    "outputMint": mint,
                    "amount": TEST_AMOUNT,
                    "slippageBps": 5000,
                },
                timeout=10,
            )
            
            if buy_resp.status_code == 200:
                buy_quote = buy_resp.json()
                result.can_buy = True
                tokens_received = int(buy_quote.get("outAmount", 0))
                
                if tokens_received > 0:
                    # Try to get a SELL quote (sell back what we'd receive)
                    sell_resp = await session.get(
                        f"{JUPITER_API}/quote",
                        params={
                            "inputMint": mint,
                            "outputMint": SOL_MINT,
                            "amount": tokens_received,
                            "slippageBps": 5000,
                        },
                        timeout=10,
                    )
                    
                    if sell_resp.status_code == 200:
                        sell_quote = sell_resp.json()
                        sol_back = int(sell_quote.get("outAmount", 0))
                        result.can_sell = True
                        
                        if sol_back > 0:
                            # Calculate effective tax
                            round_trip_loss = 1 - (sol_back / TEST_AMOUNT)
                            result.sell_tax_pct = round(round_trip_loss * 100, 1)
                            
                            # >50% loss = suspicious, >80% = honeypot
                            if round_trip_loss > 0.8:
                                result.is_honeypot = True
                                print(f"  [RugV2] 🍯 HONEYPOT: {round_trip_loss:.0%} loss on round trip")
                            elif round_trip_loss > 0.5:
                                result.is_suspicious = True
                                print(f"  [RugV2] ⚠️ Suspicious: {round_trip_loss:.0%} loss on round trip")
                        else:
                            result.is_honeypot = True
                    else:
                        # Can't get sell quote = honeypot
                        result.is_honeypot = True
                        result.can_sell = False
                        print(f"  [RugV2] 🍯 HONEYPOT: No sell route available")
            else:
                result.can_buy = False
                
        except Exception as e:
            print(f"  [RugV2] Honeypot check error: {e}")
        
        return result
    
    async def _check_bundles(self, session: httpx.AsyncClient, mint: str) -> BundleData:
        """Check for sniper/bundle activity in early transactions"""
        data = BundleData()
        
        try:
            resp = await session.post(
                HELIUS_RPC,
                json={"jsonrpc": "2.0", "id": 1,
                      "method": "getSignaturesForAddress",
                      "params": [mint, {"limit": 20}]},
                timeout=10,
            )
            if resp.status_code != 200:
                return data
            
            sigs = resp.json().get("result", [])
            if not sigs:
                return data
            
            # Get the creation block time
            creation_time = sigs[-1].get("blockTime", 0) if sigs else 0
            
            # Count unique signers in first few seconds
            early_signers = set()
            for sig_info in sigs:
                block_time = sig_info.get("blockTime", 0)
                # Within 10 seconds of creation
                if creation_time and block_time and (block_time - creation_time) <= 10:
                    sig = sig_info.get("signature", "")
                    if sig:
                        try:
                            tx_resp = await session.post(
                                f"{HELIUS_API}/transactions",
                                params={"api-key": HELIUS_API_KEY},
                                json={"transactions": [sig]},
                                timeout=10,
                            )
                            if tx_resp.status_code == 200:
                                txns = tx_resp.json()
                                if txns:
                                    early_signers.add(txns[0].get("feePayer", ""))
                        except:
                            pass
                        await asyncio.sleep(0.15)
            
            data.early_buyer_count = len(early_signers)
            data.sniper_estimate = max(0, len(early_signers) - 2)  # Subtract creator + LP
            data.is_heavily_bundled = data.sniper_estimate > 8
            
        except Exception as e:
            print(f"  [RugV2] Bundle check error: {e}")
        
        return data
    
    # =========================================================
    # SCORING
    # =========================================================
    
    def _evaluate_flags(self, contract: dict, holders: HolderData, 
                        lp: LiquidityData, deployer: DeployerData,
                        honeypot: HoneypotResult, bundles: BundleData) -> list:
        """Evaluate all risk flags based on collected data"""
        active = []
        
        # Contract flags
        if not contract.get("mint_revoked", True):
            active.append(FLAGS["MINT_AUTH_ACTIVE"])
        if not contract.get("freeze_revoked", True):
            active.append(FLAGS["FREEZE_AUTH_ACTIVE"])
        if not contract.get("has_social", True):
            active.append(FLAGS["NO_SOCIAL"])
        
        # Honeypot flags (NEW in v2)
        if honeypot.is_honeypot:
            active.append(FLAGS["HONEYPOT_DETECTED"])
        elif honeypot.is_suspicious:
            active.append(FLAGS["HONEYPOT_SUSPICIOUS"])
        
        # Liquidity flags
        if lp.liquidity_usd > 0:
            if not lp.is_burned and not lp.is_locked:
                active.append(FLAGS["LP_NOT_LOCKED"])
            if lp.liquidity_usd < 10_000:
                active.append(FLAGS["LP_LOW"])
            if lp.lock_expiry_days is not None and lp.lock_expiry_days <= 7:
                active.append(FLAGS["LP_UNLOCK_SOON"])
        
        # Holder flags
        if holders.top10_pct > 30:
            active.append(FLAGS["TOP10_OVER_30"])
        if holders.top1_pct > 5:
            active.append(FLAGS["TOP1_OVER_5"])
        if holders.wallet_clusters_detected > 0:
            active.append(FLAGS["WALLET_CLUSTER"])
        
        # Bundle flags (NEW in v2)
        if bundles.is_heavily_bundled:
            active.append(FLAGS["HEAVILY_BUNDLED"])
        
        # Deployer flags
        if deployer.rugged_tokens:
            active.append(FLAGS["DEPLOYER_RUGGED_BEFORE"])
        if deployer.age_days < 7 and deployer.age_days >= 0:
            active.append(FLAGS["DEPLOYER_FRESH_WALLET"])
        if deployer.tokens_last_30d > 10:
            active.append(FLAGS["DEPLOYER_SERIAL"])
        if 0 < deployer.sol_balance < 0.5:
            active.append(FLAGS["DEPLOYER_LOW_SOL"])
        
        return active
    
    def _calculate_score(self, flags: list) -> float:
        """Calculate rug score from flags (1-10, higher = more risky)"""
        return min(round(1.0 + sum(f.score_impact for f in flags), 2), 10.0)
    
    def _verdict(self, score: float) -> str:
        if score <= 2.5:   return "✅ SAFE"
        elif score <= 4.5: return "⚠️ CAUTION"
        elif score <= 6.5: return "🔶 HIGH RISK"
        else:              return "☠️ LIKELY RUG"
