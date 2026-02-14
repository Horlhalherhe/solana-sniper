"""
META INTELLIGENCE ENGINE
Tracks wallet overlaps, serial deployers, narrative farming.
"""

import json
import os
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from collections import defaultdict
from typing import Optional
from pathlib import Path

DATA_DIR = Path(os.getenv("SNIPER_DATA_DIR", "./data"))
DATA_DIR.mkdir(exist_ok=True)

DEPLOYER_DB_PATH = DATA_DIR / "deployer_db.json"
WALLET_DB_PATH   = DATA_DIR / "wallet_db.json"
TOKEN_DB_PATH    = DATA_DIR / "token_db.json"

@dataclass
class DeployerProfile:
    wallet: str
    first_seen: str
    tokens_launched: list[str] = field(default_factory=list)
    rugged_tokens: list[str]   = field(default_factory=list)
    narratives_used: list[str] = field(default_factory=list)
    co_deployers: list[str]    = field(default_factory=list)
    risk_rating: str = "unknown"
    notes: str = ""

    def rug_rate(self) -> float:
        if not self.tokens_launched:
            return 0.0
        return len(self.rugged_tokens) / len(self.tokens_launched)

    def to_dict(self):
        return {
            "wallet": self.wallet,
            "first_seen": self.first_seen,
            "tokens_launched": self.tokens_launched,
            "rugged_tokens": self.rugged_tokens,
            "narratives_used": self.narratives_used,
            "risk_rating": self.risk_rating,
            "rug_rate": self.rug_rate(),
        }

@dataclass
class TokenRecord:
    mint: str
    name: str
    symbol: str
    deployer: str
    launched_at: str
    narrative: str
    entry_score: float
    rug_score: float
    outcome: str = "active"
    peak_mcap: float = 0.0

    def to_dict(self):
        return {
            "mint": self.mint,
            "name": self.name,
            "symbol": self.symbol,
            "deployer": self.deployer,
            "launched_at": self.launched_at,
            "narrative": self.narrative,
            "entry_score": self.entry_score,
            "rug_score": self.rug_score,
            "outcome": self.outcome,
            "peak_mcap": self.peak_mcap,
        }


class MetaIntelligence:

    def __init__(self):
        self.deployers: dict[str, DeployerProfile] = {}
        self.tokens: dict[str, TokenRecord] = {}
        self.wallet_funding_map: dict[str, list[str]] = defaultdict(list)
        self._load()

    def register_token(self, mint: str, name: str, symbol: str, deployer: str,
                       narrative: str, entry_score: float, rug_score: float) -> TokenRecord:
        record = TokenRecord(
            mint=mint, name=name, symbol=symbol, deployer=deployer,
            launched_at=datetime.utcnow().isoformat(),
            narrative=narrative, entry_score=entry_score, rug_score=rug_score,
        )
        self.tokens[mint] = record
        self._update_deployer(deployer, mint, narrative)
        self._save()
        return record

    def update_outcome(self, mint: str, outcome: str, peak_mcap: float = 0.0):
        if mint in self.tokens:
            self.tokens[mint].outcome = outcome
            if peak_mcap:
                self.tokens[mint].peak_mcap = peak_mcap
            if outcome == "rugged":
                deployer = self.tokens[mint].deployer
                if deployer in self.deployers:
                    if mint not in self.deployers[deployer].rugged_tokens:
                        self.deployers[deployer].rugged_tokens.append(mint)
                    self._classify_deployer(deployer)
            self._save()

    def _update_deployer(self, wallet: str, mint: str, narrative: str):
        if wallet not in self.deployers:
            self.deployers[wallet] = DeployerProfile(
                wallet=wallet, first_seen=datetime.utcnow().isoformat())
        profile = self.deployers[wallet]
        if mint not in profile.tokens_launched:
            profile.tokens_launched.append(mint)
        if narrative and narrative not in profile.narratives_used:
            profile.narratives_used.append(narrative)
        self._classify_deployer(wallet)

    def _classify_deployer(self, wallet: str):
        p = self.deployers.get(wallet)
        if not p:
            return
        rug_rate = p.rug_rate()
        total = len(p.tokens_launched)
        if rug_rate >= 0.5 and total >= 2:
            p.risk_rating = "serial_rugger"
        elif rug_rate >= 0.25 or (total >= 3 and len(p.rugged_tokens) >= 1):
            p.risk_rating = "suspicious"
        elif total >= 2 and rug_rate == 0:
            p.risk_rating = "clean"
        else:
            p.risk_rating = "unknown"

    def get_deployer(self, wallet: str) -> Optional[dict]:
        if wallet in self.deployers:
            return self.deployers[wallet].to_dict()
        return None

    def detect_narrative_farming(self, deployer: str) -> dict:
        profile = self.deployers.get(deployer)
        if not profile:
            return {"detected": False}
        narrative_counts = defaultdict(int)
        for mint in profile.tokens_launched:
            token = self.tokens.get(mint)
            if token:
                narrative_counts[token.narrative] += 1
        farmed = {n: c for n, c in narrative_counts.items() if c >= 2}
        if farmed:
            return {"detected": True, "deployer": deployer,
                    "farmed_narratives": farmed,
                    "severity": "high" if max(farmed.values()) >= 3 else "medium"}
        return {"detected": False}

    def weekly_trending_tokens(self, top_n: int = 10) -> list[dict]:
        cutoff = datetime.utcnow() - timedelta(days=7)
        recent = [t for t in self.tokens.values()
                  if datetime.fromisoformat(t.launched_at) > cutoff]
        return sorted([t.to_dict() for t in recent],
                      key=lambda x: x["entry_score"], reverse=True)[:top_n]

    def get_serial_ruggers(self) -> list[dict]:
        return [d.to_dict() for d in self.deployers.values()
                if d.risk_rating == "serial_rugger"]

    def get_intel_summary(self) -> dict:
        from collections import Counter
        outcomes = Counter(t.outcome for t in self.tokens.values())
        return {
            "total_tokens_tracked": len(self.tokens),
            "total_deployers": len(self.deployers),
            "outcomes": dict(outcomes),
            "serial_ruggers": len(self.get_serial_ruggers()),
        }

    def _save(self):
        try:
            with open(DEPLOYER_DB_PATH, "w") as f:
                json.dump({k: v.to_dict() for k, v in self.deployers.items()}, f, indent=2)
            with open(TOKEN_DB_PATH, "w") as f:
                json.dump({k: v.to_dict() for k, v in self.tokens.items()}, f, indent=2)
        except Exception as e:
            print(f"[MetaIntel] Save error: {e}")

    def _load(self):
        try:
            if DEPLOYER_DB_PATH.exists():
                with open(DEPLOYER_DB_PATH) as f:
                    raw = json.load(f)
                    for wallet, data in raw.items():
                        self.deployers[wallet] = DeployerProfile(
                            wallet=data["wallet"],
                            first_seen=data["first_seen"],
                            tokens_launched=data.get("tokens_launched", []),
                            rugged_tokens=data.get("rugged_tokens", []),
                            narratives_used=data.get("narratives_used", []),
                            risk_rating=data.get("risk_rating", "unknown"),
                        )
        except Exception as e:
            print(f"[MetaIntel] Load error: {e}")
