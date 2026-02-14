"""
RUG ANALYZER
Evaluates on-chain risk for Solana tokens.
"""

import os
from dataclasses import dataclass, field
from typing import Optional
import asyncio

HELIUS_API_KEY = os.getenv("HELIUS_API_KEY", "")
BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY", "")
HELIUS_BASE = "https://api.helius.xyz/v0"
BIRDEYE_BASE = "https://public-api.birdeye.so"

@dataclass
class RiskFlag:
    code: str
    severity: str
    description: str
    score_impact: float

    def to_dict(self):
        return {"code": self.code, "severity": self.severity,
                "description": self.description, "impact": self.score_impact}

FLAGS = {
    "MINT_AUTH_ACTIVE":       RiskFlag("MINT_AUTH_ACTIVE",       "critical", "Mint authority not revoked", 3.0),
    "FREEZE_AUTH_ACTIVE":     RiskFlag("FREEZE_AUTH_ACTIVE",     "critical", "Freeze authority active", 2.5),
    "LP_NOT_LOCKED":          RiskFlag("LP_NOT_LOCKED",          "critical", "Liquidity not locked or burned", 3.0),
    "LP_LOW":                 RiskFlag("LP_LOW",                 "high",     "Liquidity under $10k", 1.5),
    "LP_UNLOCK_SOON":         RiskFlag("LP_UNLOCK_SOON",         "high",     "Lock expires within 7 days", 2.0),
    "TOP10_OVER_50":          RiskFlag("TOP10_OVER_50",          "high",     "Top 10 wallets hold >50%", 1.5),
    "TOP1_OVER_20":           RiskFlag("TOP1_OVER_20",           "high",     "Single wallet holds >20%", 2.0),
    "DEPLOYER_RUGGED_BEFORE": RiskFlag("DEPLOYER_RUGGED_BEFORE", "critical", "Deployer linked to previous rug", 3.0),
    "DEPLOYER_FRESH_WALLET":  RiskFlag("DEPLOYER_FRESH_WALLET",  "medium",   "Deployer wallet < 7 days old", 1.0),
    "WALLET_CLUSTER":         RiskFlag("WALLET_CLUSTER",         "high",     "Top holders funded from same source", 2.0),
    "NO_SOCIAL":              RiskFlag("NO_SOCIAL",              "low",      "No social links in metadata", 0.5),
}

@dataclass
class HolderData:
    total_holders: int
    top1_pct: float
    top5_pct: float
    top10_pct: float
    dev_holds_pct: float
    wallet_clusters_detected: int

@dataclass
class LiquidityData:
    pool_address: str
    liquidity_usd: float
    is_burned: bool
    is_locked: bool
    lock_expiry_days: Optional[int]
    pool_age_hours: float

@dataclass
class DeployerData:
    wallet: str
    age_days: int
    sol_balance: float
    previous_tokens: list[str]
    rugged_tokens: list[str]
    verified_clean: bool

@dataclass
class RugReport:
    mint: str
    rug_score: float
    flags: list[RiskFlag]
    holder_data: Optional[HolderData]
    liquidity_data: Optional[LiquidityData]
    deployer_data: Optional[DeployerData]
    verdict: str
    analysis_time: str

    def to_dict(self):
        return {
            "mint": self.mint,
            "rug_score": round(self.rug_score, 2),
            "verdict": self.verdict,
            "flags": [f.to_dict() for f in self.flags],
            "holder_data": {
                "total": self.holder_data.total_holders if self.holder_data else None,
                "top1_pct": self.holder_data.top1_pct if self.holder_data else None,
                "top10_pct": self.holder_data.top10_pct if self.holder_data else None,
                "clusters": self.holder_data.wallet_clusters_detected if self.holder_data else 0,
            } if self.holder_data else None,
            "liquidity": {
                "usd": self.liquidity_data.liquidity_usd if self.liquidity_data else None,
                "burned": self.liquidity_data.is_burned if self.liquidity_data else None,
                "locked": self.liquidity_data.is_locked if self.liquidity_data else None,
            } if self.liquidity_data else None,
            "deployer": {
                "wallet": self.deployer_data.wallet if self.deployer_data else None,
                "age_days": self.deployer_data.age_days if self.deployer_data else None,
                "rugs": self.deployer_data.rugged_tokens if self.deployer_data else [],
            } if self.deployer_data else None,
        }


class RugAnalyzer:
    def __init__(self):
        self.helius_key = HELIUS_API_KEY
        self.birdeye_key = BIRDEYE_API_KEY

    def analyze_manual(self, data: dict) -> RugReport:
        from datetime import datetime
        holder_data = HolderData(
            total_holders=data.get("total_holders", 0),
            top1_pct=data.get("top1_pct", 0),
            top5_pct=data.get("top5_pct", 0),
            top10_pct=data.get("top10_pct", 0),
            dev_holds_pct=data.get("dev_holds_pct", 0),
            wallet_clusters_detected=data.get("wallet_clusters", 0)
        )
        liquidity_data = LiquidityData(
            pool_address=data.get("pool_address", "unknown"),
            liquidity_usd=data.get("liquidity_usd", 0),
            is_burned=data.get("lp_burned", False),
            is_locked=data.get("lp_locked", False),
            lock_expiry_days=data.get("lp_lock_days"),
            pool_age_hours=data.get("pool_age_hours", 0)
        )
        deployer_data = DeployerData(
            wallet=data.get("deployer", "unknown"),
            age_days=data.get("deployer_age_days", 0),
            sol_balance=data.get("deployer_sol", 0),
            previous_tokens=data.get("deployer_prev_tokens", []),
            rugged_tokens=data.get("deployer_prev_rugs", []),
            verified_clean=len(data.get("deployer_prev_rugs", [])) == 0
        )
        authority_data = {
            "mint_revoked": data.get("mint_authority_revoked", False),
            "freeze_revoked": data.get("freeze_authority_revoked", False),
            "has_social": data.get("has_social", True),
        }
        flags = self._evaluate_flags(holder_data, liquidity_data, authority_data, deployer_data)
        rug_score = self._calculate_rug_score(flags)
        verdict = self._verdict(rug_score)
        return RugReport(mint=data.get("mint", "manual"), rug_score=rug_score,
                         flags=flags, holder_data=holder_data, liquidity_data=liquidity_data,
                         deployer_data=deployer_data, verdict=verdict,
                         analysis_time=datetime.utcnow().isoformat())

    def _evaluate_flags(self, holders, liquidity, authority, deployer) -> list[RiskFlag]:
        active_flags = []
        if authority and not authority.get("mint_revoked", True):
            active_flags.append(FLAGS["MINT_AUTH_ACTIVE"])
        if authority and not authority.get("freeze_revoked", True):
            active_flags.append(FLAGS["FREEZE_AUTH_ACTIVE"])
        if authority and not authority.get("has_social", True):
            active_flags.append(FLAGS["NO_SOCIAL"])
        if liquidity:
            if not liquidity.is_burned and not liquidity.is_locked:
                active_flags.append(FLAGS["LP_NOT_LOCKED"])
            if liquidity.liquidity_usd < 10_000:
                active_flags.append(FLAGS["LP_LOW"])
            if liquidity.lock_expiry_days is not None and liquidity.lock_expiry_days <= 7:
                active_flags.append(FLAGS["LP_UNLOCK_SOON"])
        if holders:
            if holders.top10_pct > 50:
                active_flags.append(FLAGS["TOP10_OVER_50"])
            if holders.top1_pct > 20:
                active_flags.append(FLAGS["TOP1_OVER_20"])
            if holders.wallet_clusters_detected > 0:
                active_flags.append(FLAGS["WALLET_CLUSTER"])
        if deployer:
            if deployer.rugged_tokens:
                active_flags.append(FLAGS["DEPLOYER_RUGGED_BEFORE"])
            if deployer.age_days < 7:
                active_flags.append(FLAGS["DEPLOYER_FRESH_WALLET"])
        return active_flags

    def _calculate_rug_score(self, flags: list[RiskFlag]) -> float:
        return min(round(1.0 + sum(f.score_impact for f in flags), 2), 10.0)

    def _verdict(self, score: float) -> str:
        if score <= 2.5:   return "✅ SAFE"
        elif score <= 4.5: return "⚠️ CAUTION"
        elif score <= 6.5: return "🔶 HIGH RISK"
        else:              return "☠️ LIKELY RUG"
