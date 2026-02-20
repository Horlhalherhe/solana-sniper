"""
POSITION MANAGER
Tracks open positions, calculates PnL, manages exits
"""

import json
import os
from datetime import datetime, timezone
from typing import Optional, List, Dict
from dataclasses import dataclass, asdict
from pathlib import Path

DATA_DIR = Path(os.getenv("SNIPER_DATA_DIR", "./data"))
POSITIONS_FILE = DATA_DIR / "positions.json"

@dataclass
class Position:
    mint: str
    name: str
    symbol: str
    entry_time: str
    entry_sol: float
    entry_tokens: float
    entry_price_sol: float
    current_price_sol: float = 0.0
    peak_price_sol: float = 0.0
    current_mcap: float = 0.0
    current_x: float = 1.0
    peak_x: float = 1.0
    status: str = "active"  # active, partial_exit, closed
    remaining_tokens: float = 0.0
    total_sol_out: float = 0.0
    realized_pnl_sol: float = 0.0
    last_updated: str = ""
    
    def __post_init__(self):
        if not self.remaining_tokens:
            self.remaining_tokens = self.entry_tokens
        if not self.peak_price_sol:
            self.peak_price_sol = self.entry_price_sol
        if not self.last_updated:
            self.last_updated = datetime.now(timezone.utc).isoformat()
    
    def update_price(self, current_price_sol: float, current_mcap: float = 0):
        """Update current price and calculate metrics"""
        self.current_price_sol = current_price_sol
        self.current_mcap = current_mcap
        self.current_x = current_price_sol / self.entry_price_sol if self.entry_price_sol > 0 else 1.0
        self.peak_price_sol = max(self.peak_price_sol, current_price_sol)
        self.peak_x = self.peak_price_sol / self.entry_price_sol if self.entry_price_sol > 0 else 1.0
        self.last_updated = datetime.now(timezone.utc).isoformat()
    
    def execute_partial_exit(self, tokens_sold: float, sol_received: float):
        """Record partial exit"""
        self.remaining_tokens -= tokens_sold
        self.total_sol_out += sol_received
        self.realized_pnl_sol = self.total_sol_out - (self.entry_sol * (1 - self.remaining_tokens / self.entry_tokens))
        
        if self.remaining_tokens <= 0:
            self.status = "closed"
        else:
            self.status = "partial_exit"
        self.last_updated = datetime.now(timezone.utc).isoformat()
    
    def unrealized_pnl_sol(self) -> float:
        """Calculate unrealized PnL on remaining position"""
        if self.remaining_tokens <= 0:
            return 0.0
        current_value = self.remaining_tokens * self.current_price_sol
        cost_basis = self.entry_sol * (self.remaining_tokens / self.entry_tokens)
        return current_value - cost_basis
    
    def total_pnl_sol(self) -> float:
        """Total PnL (realized + unrealized)"""
        return self.realized_pnl_sol + self.unrealized_pnl_sol()
    
    def pnl_pct(self) -> float:
        """Total PnL as percentage of entry capital"""
        if self.entry_sol == 0:
            return 0.0
        return (self.total_pnl_sol() / self.entry_sol) * 100
    
    def age_hours(self) -> float:
        """Position age in hours"""
        entry = datetime.fromisoformat(self.entry_time.replace('Z', '+00:00'))
        now = datetime.now(timezone.utc)
        return (now - entry).total_seconds() / 3600
    
    def trailing_stop_triggered(self, trailing_pct: float) -> bool:
        """Check if trailing stop loss triggered"""
        if self.peak_price_sol == 0:
            return False
        drawdown_pct = ((self.peak_price_sol - self.current_price_sol) / self.peak_price_sol) * 100
        return drawdown_pct >= trailing_pct
    
    def to_dict(self):
        return asdict(self)


class PositionManager:
    def __init__(self):
        self.positions: Dict[str, Position] = {}
        self._load()
    
    def open_position(self, mint: str, name: str, symbol: str, 
                     entry_sol: float, entry_tokens: float, entry_price_sol: float) -> Position:
        """Open new position"""
        pos = Position(
            mint=mint,
            name=name,
            symbol=symbol,
            entry_time=datetime.now(timezone.utc).isoformat(),
            entry_sol=entry_sol,
            entry_tokens=entry_tokens,
            entry_price_sol=entry_price_sol,
        )
        self.positions[mint] = pos
        self._save()
        return pos
    
    def get_position(self, mint: str) -> Optional[Position]:
        """Get position by mint"""
        return self.positions.get(mint)
    
    def get_active_positions(self) -> List[Position]:
        """Get all active positions"""
        return [p for p in self.positions.values() if p.status == "active" or p.status == "partial_exit"]
    
    def get_all_positions(self) -> List[Position]:
        """Get all positions (including closed)"""
        return list(self.positions.values())
    
    def update_position_price(self, mint: str, current_price_sol: float, current_mcap: float = 0):
        """Update position with new price"""
        if mint in self.positions:
            self.positions[mint].update_price(current_price_sol, current_mcap)
            self._save()
    
    def close_position_partial(self, mint: str, tokens_sold: float, sol_received: float):
        """Execute partial exit"""
        if mint in self.positions:
            self.positions[mint].execute_partial_exit(tokens_sold, sol_received)
            self._save()
    
    def close_position_full(self, mint: str, sol_received: float):
        """Close position completely"""
        if mint in self.positions:
            pos = self.positions[mint]
            pos.execute_partial_exit(pos.remaining_tokens, sol_received)
            self._save()
    
    def get_total_capital_deployed(self) -> float:
        """Total SOL currently in active positions"""
        active = self.get_active_positions()
        return sum(p.entry_sol * (p.remaining_tokens / p.entry_tokens) for p in active)
    
    def get_total_pnl(self) -> float:
        """Total PnL across all positions"""
        return sum(p.total_pnl_sol() for p in self.positions.values())
    
    def get_daily_pnl(self) -> float:
        """PnL from positions opened today"""
        now = datetime.now(timezone.utc)
        today_positions = [
            p for p in self.positions.values()
            if datetime.fromisoformat(p.entry_time.replace('Z', '+00:00')).date() == now.date()
        ]
        return sum(p.total_pnl_sol() for p in today_positions)
    
    def _save(self):
        """Save positions to disk"""
        try:
            data = {mint: pos.to_dict() for mint, pos in self.positions.items()}
            POSITIONS_FILE.write_text(json.dumps(data, indent=2))
        except Exception as e:
            print(f"[PositionManager] Save error: {e}")
    
    def _load(self):
        """Load positions from disk"""
        try:
            if POSITIONS_FILE.exists():
                data = json.loads(POSITIONS_FILE.read_text())
                for mint, pos_dict in data.items():
                    self.positions[mint] = Position(**pos_dict)
        except Exception as e:
            print(f"[PositionManager] Load error: {e}")
