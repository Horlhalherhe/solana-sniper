"""
SMART MONEY TRACKER v2
======================
Tracks known profitable wallets, detects when they buy new tokens,
auto-discovers new smart money wallets, and scores copy-trade signals.

This is the #1 edge — knowing what profitable wallets are doing
before the crowd catches on.
"""

import json
import os
import time
import asyncio
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field, asdict
from collections import defaultdict
from typing import Optional
from pathlib import Path
import httpx

DATA_DIR = Path(os.getenv("SNIPER_DATA_DIR", "./data"))
DATA_DIR.mkdir(exist_ok=True)
SMART_MONEY_DB = DATA_DIR / "smart_money_db.json"

HELIUS_API_KEY = os.getenv("HELIUS_API_KEY", "")
HELIUS_RPC = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
HELIUS_API = f"https://api.helius.xyz/v0"
BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY", "")

# Known profitable wallet tags (you build this over time)
# Format: wallet -> {"tag": "name", "win_rate": 0.0, "avg_x": 0.0}
SEED_WALLETS_FILE = DATA_DIR / "seed_wallets.json"


@dataclass
class WalletProfile:
    """Profile of a tracked wallet"""
    address: str
    tag: str = ""                          # Human label (e.g. "degen_whale_1")
    source: str = "manual"                 # How we found them: manual, discovered, gmgn
    first_tracked: str = ""
    
    # Performance stats
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    avg_return_x: float = 1.0
    best_trade_x: float = 1.0
    total_pnl_sol: float = 0.0
    
    # Behavioral stats
    avg_hold_minutes: float = 0.0
    avg_entry_mcap: float = 0.0
    prefers_narratives: list = field(default_factory=list)
    active_hours: list = field(default_factory=list)  # UTC hours when most active
    
    # Token history
    recent_buys: list = field(default_factory=list)    # [{mint, time, sol_amount}]
    recent_sells: list = field(default_factory=list)
    
    # Calculated
    win_rate: float = 0.0
    confidence_score: float = 0.0  # 0-1, how much we trust this wallet's alpha
    is_active: bool = True
    last_activity: str = ""
    
    def __post_init__(self):
        if not self.first_tracked:
            self.first_tracked = datetime.now(timezone.utc).isoformat()
    
    def update_win_rate(self):
        if self.total_trades > 0:
            self.win_rate = self.winning_trades / self.total_trades
        self._update_confidence()
    
    def _update_confidence(self):
        """
        Confidence based on:
        - Win rate (40%)
        - Number of trades / sample size (30%)
        - Average return (20%)
        - Recency of activity (10%)
        """
        # Win rate component (0-0.4)
        wr_score = min(self.win_rate * 0.5, 0.4)  # 80%+ win rate = max
        
        # Sample size component (0-0.3)
        # Need at least 10 trades for high confidence
        sample_score = min(self.total_trades / 30, 1.0) * 0.3
        
        # Return component (0-0.2)
        return_score = min(max(self.avg_return_x - 1, 0) / 5, 1.0) * 0.2
        
        # Recency component (0-0.1)
        recency = 0.1
        if self.last_activity:
            try:
                last = datetime.fromisoformat(self.last_activity.replace('Z', '+00:00'))
                hours_ago = (datetime.now(timezone.utc) - last).total_seconds() / 3600
                recency = max(0, 0.1 * (1 - hours_ago / 168))  # Decays over 7 days
            except:
                pass
        
        self.confidence_score = round(wr_score + sample_score + return_score + recency, 3)
    
    def record_trade(self, mint: str, is_win: bool, return_x: float, 
                     sol_amount: float = 0, hold_minutes: float = 0):
        """Record a completed trade"""
        self.total_trades += 1
        if is_win:
            self.winning_trades += 1
        else:
            self.losing_trades += 1
        
        # Rolling average return
        self.avg_return_x = (
            (self.avg_return_x * (self.total_trades - 1) + return_x) / self.total_trades
        )
        self.best_trade_x = max(self.best_trade_x, return_x)
        
        if hold_minutes > 0:
            self.avg_hold_minutes = (
                (self.avg_hold_minutes * (self.total_trades - 1) + hold_minutes) / self.total_trades
            )
        
        self.last_activity = datetime.now(timezone.utc).isoformat()
        self.update_win_rate()
    
    def record_buy(self, mint: str, sol_amount: float = 0):
        """Record a new buy (before outcome is known)"""
        entry = {
            "mint": mint,
            "time": datetime.now(timezone.utc).isoformat(),
            "sol_amount": sol_amount,
        }
        self.recent_buys.append(entry)
        # Keep last 50
        if len(self.recent_buys) > 50:
            self.recent_buys = self.recent_buys[-50:]
        self.last_activity = entry["time"]
    
    def to_dict(self):
        return asdict(self)


@dataclass
class SmartMoneySignal:
    """Signal generated when smart money buys a token"""
    mint: str
    wallets_in: list                  # [{address, tag, confidence, sol_amount}]
    total_smart_money_sol: float
    avg_confidence: float
    signal_strength: float            # 0-10 composite score
    first_buy_time: str
    time_since_first_buy_min: float
    is_consensus: bool                # 3+ wallets agree
    narrative_match: str = ""
    
    def to_dict(self):
        return {
            "mint": self.mint,
            "wallets_count": len(self.wallets_in),
            "wallets": self.wallets_in[:5],  # Top 5
            "total_sol": round(self.total_smart_money_sol, 3),
            "avg_confidence": round(self.avg_confidence, 3),
            "signal_strength": round(self.signal_strength, 2),
            "is_consensus": self.is_consensus,
            "first_buy_min_ago": round(self.time_since_first_buy_min, 1),
        }


class SmartMoneyTracker:
    """
    Tracks smart money wallets and generates signals.
    
    Two modes:
    1. PASSIVE — Monitor known wallets for new buys
    2. DISCOVERY — Find new smart money from on-chain patterns
    """
    
    def __init__(self):
        self.wallets: dict[str, WalletProfile] = {}
        self.token_smart_buys: dict[str, list] = defaultdict(list)  # mint -> [buy events]
        self.api_calls_remaining = 100  # Rough rate limit tracking
        self._load()
    
    # =========================================================
    # CORE: Check if smart money is in a token
    # =========================================================
    
    async def check_token(self, session: httpx.AsyncClient, mint: str) -> SmartMoneySignal:
        """
        Check if any tracked smart money wallets hold a given token.
        This is called by the main sniper for every new token detected.
        
        Returns a SmartMoneySignal with signal strength.
        """
        wallets_in = []
        total_sol = 0.0
        first_buy_time = None
        
        for addr, profile in self.wallets.items():
            if not profile.is_active:
                continue
            
            try:
                balance = await self._get_wallet_token_balance(session, addr, mint)
                if balance and balance > 0:
                    # Estimate SOL value (rough)
                    sol_est = await self._estimate_position_sol(session, addr, mint, balance)
                    
                    wallets_in.append({
                        "address": addr,
                        "tag": profile.tag or addr[:8],
                        "confidence": profile.confidence_score,
                        "sol_amount": round(sol_est, 4),
                        "win_rate": round(profile.win_rate, 2),
                    })
                    total_sol += sol_est
                    
                    # Record buy in profile
                    profile.record_buy(mint, sol_est)
                    
                    # Track first buy time
                    buy_time = self._find_first_buy_time(profile, mint)
                    if buy_time and (not first_buy_time or buy_time < first_buy_time):
                        first_buy_time = buy_time
                    
            except Exception as e:
                print(f"  [SmartMoney] Error checking {addr[:8]}...: {e}")
                continue
            
            await asyncio.sleep(0.15)  # Rate limiting
        
        # Calculate signal strength
        now = datetime.now(timezone.utc)
        first_buy_str = first_buy_time.isoformat() if first_buy_time else now.isoformat()
        minutes_ago = (now - (first_buy_time or now)).total_seconds() / 60
        
        avg_conf = 0.0
        if wallets_in:
            avg_conf = sum(w["confidence"] for w in wallets_in) / len(wallets_in)
        
        signal_strength = self._calculate_signal_strength(
            wallet_count=len(wallets_in),
            avg_confidence=avg_conf,
            total_sol=total_sol,
            minutes_since_first=minutes_ago,
        )
        
        signal = SmartMoneySignal(
            mint=mint,
            wallets_in=wallets_in,
            total_smart_money_sol=total_sol,
            avg_confidence=avg_conf,
            signal_strength=signal_strength,
            first_buy_time=first_buy_str,
            time_since_first_buy_min=minutes_ago,
            is_consensus=len(wallets_in) >= 3,
        )
        
        if wallets_in:
            print(f"  [SmartMoney] ✅ {len(wallets_in)} wallet(s) in {mint[:12]}... "
                  f"(signal: {signal_strength:.1f}/10)")
        
        self._save()
        return signal
    
    def _calculate_signal_strength(self, wallet_count: int, avg_confidence: float,
                                   total_sol: float, minutes_since_first: float) -> float:
        """
        Composite signal score (0-10):
        - Wallet count:     30% (more wallets = stronger signal)
        - Avg confidence:   30% (higher confidence wallets = stronger)
        - SOL deployed:     20% (more capital = more conviction)
        - Timing:           20% (fresher = better)
        """
        if wallet_count == 0:
            return 0.0
        
        # Wallet count component (0-3)
        count_score = min(wallet_count / 5, 1.0) * 3.0
        
        # Confidence component (0-3)
        conf_score = avg_confidence * 3.0
        
        # Capital component (0-2)
        sol_score = min(total_sol / 10.0, 1.0) * 2.0  # 10+ SOL = max
        
        # Timing component (0-2) — fresher is better
        if minutes_since_first <= 5:
            time_score = 2.0
        elif minutes_since_first <= 15:
            time_score = 1.5
        elif minutes_since_first <= 60:
            time_score = 1.0
        elif minutes_since_first <= 180:
            time_score = 0.5
        else:
            time_score = 0.2
        
        return min(count_score + conf_score + sol_score + time_score, 10.0)
    
    # =========================================================
    # MONITORING: Scan wallets for recent buys
    # =========================================================
    
    async def scan_recent_activity(self, session: httpx.AsyncClient, 
                                    lookback_minutes: int = 30) -> list[dict]:
        """
        Scan all tracked wallets for recent token purchases.
        Returns list of new buy events.
        
        Call this periodically (e.g. every 5 min) to catch new buys.
        """
        new_buys = []
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=lookback_minutes)
        
        for addr, profile in self.wallets.items():
            if not profile.is_active:
                continue
            
            try:
                txns = await self._get_recent_swaps(session, addr, limit=5)
                for tx in txns:
                    tx_time = tx.get("timestamp")
                    if tx_time and datetime.fromisoformat(tx_time) > cutoff:
                        # This is a recent swap
                        token_bought = tx.get("token_out_mint")
                        if token_bought and not self._is_known_token(token_bought):
                            buy_event = {
                                "wallet": addr,
                                "tag": profile.tag,
                                "mint": token_bought,
                                "sol_amount": tx.get("sol_in", 0),
                                "time": tx_time,
                                "confidence": profile.confidence_score,
                            }
                            new_buys.append(buy_event)
                            self.token_smart_buys[token_bought].append(buy_event)
                            profile.record_buy(token_bought, tx.get("sol_in", 0))
                
            except Exception as e:
                print(f"  [SmartMoney] Scan error for {addr[:8]}...: {e}")
            
            await asyncio.sleep(0.2)  # Rate limiting
        
        if new_buys:
            print(f"[SmartMoney] Found {len(new_buys)} new buy(s) from tracked wallets")
        
        self._save()
        return new_buys
    
    # =========================================================
    # DISCOVERY: Find new smart money wallets
    # =========================================================
    
    async def discover_from_token(self, session: httpx.AsyncClient, mint: str,
                                   outcome: str = "winner", return_x: float = 5.0) -> list[str]:
        """
        After a token pumps, look at who bought early and profited.
        These wallets become candidates for tracking.
        
        Call this when you identify a token that did well (e.g. 5x+).
        
        Args:
            mint: Token that pumped
            outcome: "winner" or "loser"
            return_x: How much the token returned
        
        Returns:
            List of newly discovered wallet addresses
        """
        if outcome != "winner" or return_x < 3.0:
            return []
        
        print(f"[SmartMoney] Discovering wallets from {mint[:12]}... ({return_x}x winner)")
        
        discovered = []
        try:
            # Get early buyers of this token
            early_buyers = await self._get_early_token_buyers(session, mint, limit=20)
            
            for buyer in early_buyers:
                addr = buyer.get("wallet")
                if not addr or addr in self.wallets:
                    continue
                
                # Check this wallet's track record
                track_record = await self._evaluate_wallet_history(session, addr)
                
                if track_record["win_rate"] >= 0.4 and track_record["total_trades"] >= 3:
                    # This wallet has a decent track record — add it
                    profile = WalletProfile(
                        address=addr,
                        tag=f"discovered_{len(self.wallets)}",
                        source="discovered",
                        total_trades=track_record["total_trades"],
                        winning_trades=track_record["winning_trades"],
                        avg_return_x=track_record["avg_return_x"],
                        win_rate=track_record["win_rate"],
                    )
                    profile.update_win_rate()
                    self.wallets[addr] = profile
                    discovered.append(addr)
                    print(f"  [SmartMoney] 🆕 Discovered: {addr[:12]}... "
                          f"(WR: {track_record['win_rate']:.0%}, trades: {track_record['total_trades']})")
                
                await asyncio.sleep(0.3)
        
        except Exception as e:
            print(f"  [SmartMoney] Discovery error: {e}")
        
        if discovered:
            print(f"[SmartMoney] Added {len(discovered)} new wallet(s) to tracking")
            self._save()
        
        return discovered
    
    async def _evaluate_wallet_history(self, session: httpx.AsyncClient, 
                                        wallet: str) -> dict:
        """
        Evaluate a wallet's historical trading performance.
        Looks at recent swaps and estimates win rate.
        """
        result = {"total_trades": 0, "winning_trades": 0, 
                  "avg_return_x": 1.0, "win_rate": 0.0}
        
        try:
            txns = await self._get_recent_swaps(session, wallet, limit=20)
            
            # Group by token to identify round trips (buy + sell)
            token_trades = defaultdict(list)
            for tx in txns:
                mint = tx.get("token_out_mint") or tx.get("token_in_mint")
                if mint:
                    token_trades[mint].append(tx)
            
            total_return = 0.0
            trade_count = 0
            wins = 0
            
            for mint, trades in token_trades.items():
                if self._is_known_token(mint):
                    continue
                # Simplified: if they bought and sold, check if profit
                buys = [t for t in trades if t.get("token_out_mint") == mint]
                sells = [t for t in trades if t.get("token_in_mint") == mint]
                
                if buys and sells:
                    trade_count += 1
                    buy_sol = sum(t.get("sol_in", 0) for t in buys)
                    sell_sol = sum(t.get("sol_out", 0) for t in sells)
                    
                    if buy_sol > 0:
                        ret_x = sell_sol / buy_sol
                        total_return += ret_x
                        if ret_x > 1.0:
                            wins += 1
            
            if trade_count > 0:
                result["total_trades"] = trade_count
                result["winning_trades"] = wins
                result["avg_return_x"] = total_return / trade_count
                result["win_rate"] = wins / trade_count
        
        except Exception as e:
            print(f"  [SmartMoney] History eval error: {e}")
        
        return result
    
    # =========================================================
    # WALLET MANAGEMENT
    # =========================================================
    
    def add_wallet(self, address: str, tag: str = "", source: str = "manual") -> WalletProfile:
        """Manually add a wallet to track"""
        if address in self.wallets:
            return self.wallets[address]
        
        profile = WalletProfile(address=address, tag=tag, source=source)
        self.wallets[address] = profile
        self._save()
        print(f"[SmartMoney] Added wallet: {tag or address[:12]}...")
        return profile
    
    def remove_wallet(self, address: str):
        """Remove a wallet from tracking"""
        if address in self.wallets:
            del self.wallets[address]
            self._save()
    
    def get_top_wallets(self, n: int = 10) -> list[dict]:
        """Get top N wallets by confidence score"""
        sorted_wallets = sorted(
            self.wallets.values(),
            key=lambda w: w.confidence_score,
            reverse=True
        )
        return [w.to_dict() for w in sorted_wallets[:n]]
    
    def get_wallet_count(self) -> int:
        return len([w for w in self.wallets.values() if w.is_active])
    
    def decay_inactive(self, days_threshold: int = 14):
        """Reduce confidence of wallets that haven't been active"""
        cutoff = datetime.now(timezone.utc) - timedelta(days=days_threshold)
        for addr, profile in self.wallets.items():
            if profile.last_activity:
                try:
                    last = datetime.fromisoformat(profile.last_activity.replace('Z', '+00:00'))
                    if last < cutoff:
                        profile.confidence_score *= 0.8
                        if profile.confidence_score < 0.05:
                            profile.is_active = False
                except:
                    pass
        self._save()
    
    # =========================================================
    # ON-CHAIN DATA FETCHERS
    # =========================================================
    
    async def _get_wallet_token_balance(self, session: httpx.AsyncClient,
                                         wallet: str, mint: str) -> float:
        """Check if a wallet holds a specific token"""
        try:
            resp = await session.post(
                HELIUS_RPC,
                json={
                    "jsonrpc": "2.0", "id": 1,
                    "method": "getTokenAccountsByOwner",
                    "params": [
                        wallet,
                        {"mint": mint},
                        {"encoding": "jsonParsed"}
                    ]
                },
                timeout=10,
            )
            if resp.status_code == 200:
                accounts = resp.json().get("result", {}).get("value", [])
                if accounts:
                    amount = accounts[0]["account"]["data"]["parsed"]["info"]["tokenAmount"]
                    return float(amount.get("uiAmount") or 0)
        except Exception:
            pass
        return 0.0
    
    async def _estimate_position_sol(self, session: httpx.AsyncClient,
                                      wallet: str, mint: str, balance: float) -> float:
        """Rough estimate of SOL value of a token position"""
        # Try Birdeye for price
        if BIRDEYE_API_KEY:
            try:
                resp = await session.get(
                    f"https://public-api.birdeye.so/defi/price",
                    params={"address": mint},
                    headers={"X-API-KEY": BIRDEYE_API_KEY},
                    timeout=10,
                )
                if resp.status_code == 200:
                    price_usd = resp.json().get("data", {}).get("value", 0)
                    # Rough SOL conversion (assume ~$150 SOL)
                    return (balance * price_usd) / 150
            except:
                pass
        return 0.0  # Can't estimate without price
    
    async def _get_recent_swaps(self, session: httpx.AsyncClient,
                                 wallet: str, limit: int = 10) -> list[dict]:
        """Get recent swap transactions for a wallet using Helius parsed transactions"""
        swaps = []
        try:
            resp = await session.get(
                f"{HELIUS_API}/addresses/{wallet}/transactions",
                params={
                    "api-key": HELIUS_API_KEY,
                    "limit": limit,
                    "type": "SWAP",
                },
                timeout=15,
            )
            if resp.status_code == 200:
                txns = resp.json()
                for tx in txns:
                    swap = self._parse_swap_tx(tx)
                    if swap:
                        swaps.append(swap)
        except Exception as e:
            print(f"  [SmartMoney] Swap fetch error: {e}")
        return swaps
    
    def _parse_swap_tx(self, tx: dict) -> Optional[dict]:
        """Parse a Helius parsed transaction into a clean swap record"""
        try:
            token_transfers = tx.get("tokenTransfers", [])
            native_transfers = tx.get("nativeTransfers", [])
            timestamp = tx.get("timestamp")
            
            ts_str = ""
            if timestamp:
                ts_str = datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()
            
            # Find what went in and out
            sol_in = 0
            sol_out = 0
            token_in_mint = None
            token_out_mint = None
            
            fee_payer = tx.get("feePayer", "")
            
            for nt in native_transfers:
                if nt.get("fromUserAccount") == fee_payer:
                    sol_in += nt.get("amount", 0) / 1e9
                if nt.get("toUserAccount") == fee_payer:
                    sol_out += nt.get("amount", 0) / 1e9
            
            for tt in token_transfers:
                mint = tt.get("mint", "")
                if self._is_known_token(mint):
                    continue
                if tt.get("toUserAccount") == fee_payer:
                    token_out_mint = mint  # Received tokens (buy)
                elif tt.get("fromUserAccount") == fee_payer:
                    token_in_mint = mint   # Sent tokens (sell)
            
            if token_out_mint or token_in_mint:
                return {
                    "timestamp": ts_str,
                    "sol_in": round(sol_in, 4),
                    "sol_out": round(sol_out, 4),
                    "token_out_mint": token_out_mint,
                    "token_in_mint": token_in_mint,
                    "signature": tx.get("signature", ""),
                }
        except Exception:
            pass
        return None
    
    async def _get_early_token_buyers(self, session: httpx.AsyncClient,
                                       mint: str, limit: int = 20) -> list[dict]:
        """Get wallets that bought a token early"""
        buyers = []
        try:
            # Get first transactions for this token
            resp = await session.post(
                HELIUS_RPC,
                json={
                    "jsonrpc": "2.0", "id": 1,
                    "method": "getSignaturesForAddress",
                    "params": [mint, {"limit": limit}]
                },
                timeout=15,
            )
            if resp.status_code == 200:
                sigs = resp.json().get("result", [])
                
                # Get parsed transactions
                sig_strings = [s["signature"] for s in sigs if s.get("signature")]
                if sig_strings:
                    resp2 = await session.post(
                        f"{HELIUS_API}/transactions",
                        params={"api-key": HELIUS_API_KEY},
                        json={"transactions": sig_strings[:10]},
                        timeout=15,
                    )
                    if resp2.status_code == 200:
                        txns = resp2.json()
                        seen_wallets = set()
                        for tx in txns:
                            fee_payer = tx.get("feePayer", "")
                            if fee_payer and fee_payer not in seen_wallets:
                                seen_wallets.add(fee_payer)
                                buyers.append({"wallet": fee_payer})
        except Exception as e:
            print(f"  [SmartMoney] Early buyers fetch error: {e}")
        
        return buyers
    
    def _find_first_buy_time(self, profile: WalletProfile, mint: str) -> Optional[datetime]:
        """Find when a wallet first bought a token"""
        for buy in profile.recent_buys:
            if buy.get("mint") == mint:
                try:
                    return datetime.fromisoformat(buy["time"].replace('Z', '+00:00'))
                except:
                    pass
        return None
    
    def _is_known_token(self, mint: str) -> bool:
        """Skip well-known tokens"""
        return mint in {
            "So11111111111111111111111111111111111111112",    # SOL
            "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
            "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",   # USDT
            "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So",   # mSOL
        }
    
    # =========================================================
    # PERSISTENCE
    # =========================================================
    
    def _save(self):
        try:
            data = {addr: p.to_dict() for addr, p in self.wallets.items()}
            SMART_MONEY_DB.write_text(json.dumps(data, indent=2))
        except Exception as e:
            print(f"[SmartMoney] Save error: {e}")
    
    def _load(self):
        try:
            if SMART_MONEY_DB.exists():
                raw = json.loads(SMART_MONEY_DB.read_text())
                for addr, data in raw.items():
                    # Handle loading — remove fields that aren't in constructor
                    self.wallets[addr] = WalletProfile(**data)
            
            # Also load seed wallets if exists
            if SEED_WALLETS_FILE.exists():
                seeds = json.loads(SEED_WALLETS_FILE.read_text())
                for addr, info in seeds.items():
                    if addr not in self.wallets:
                        self.wallets[addr] = WalletProfile(
                            address=addr,
                            tag=info.get("tag", ""),
                            source="seed",
                            win_rate=info.get("win_rate", 0),
                        )
                        self.wallets[addr].update_win_rate()
            
            print(f"[SmartMoney] Loaded {len(self.wallets)} tracked wallet(s)")
        except Exception as e:
            print(f"[SmartMoney] Load error: {e}")
