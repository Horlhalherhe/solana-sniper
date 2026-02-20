"""
TRADING CONFIGURATION
Runtime-adjustable trading parameters
"""

import os
import json
from pathlib import Path
from dataclasses import dataclass, asdict

DATA_DIR = Path(os.getenv("SNIPER_DATA_DIR", "./data"))
CONFIG_FILE = DATA_DIR / "trading_config.json"

@dataclass
class TradingConfig:
    # Trading mode
    auto_buy_enabled: bool = False
    paper_trading_mode: bool = True  # Safety: default to paper trading
    
    # Buy settings
    buy_amount_sol: float = 0.05
    max_concurrent_positions: int = 5
    max_slippage_bps: int = 1000  # 10%
    
    # Take profit levels
    tp1_x: float = 2.0
    tp1_sell_pct: float = 50.0
    tp2_x: float = 5.0
    tp2_sell_pct: float = 25.0
    tp3_x: float = 10.0
    tp3_sell_pct: float = 100.0
    
    # Risk management
    stop_loss_pct: float = 30.0
    trailing_stop_enabled: bool = True
    trailing_stop_pct: float = 20.0
    max_hold_time_hours: float = 24.0
    
    # Daily limits
    max_daily_loss_sol: float = 0.5
    max_daily_buys: int = 20
    
    def to_dict(self):
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: dict):
        return cls(**data)
    
    def save(self):
        """Save config to disk"""
        try:
            CONFIG_FILE.write_text(json.dumps(self.to_dict(), indent=2))
        except Exception as e:
            print(f"[TradingConfig] Save error: {e}")
    
    @classmethod
    def load(cls):
        """Load config from disk or create default"""
        try:
            if CONFIG_FILE.exists():
                data = json.loads(CONFIG_FILE.read_text())
                return cls.from_dict(data)
        except Exception as e:
            print(f"[TradingConfig] Load error: {e}")
        
        # Return default config
        config = cls()
        
        # Override with env variables if present
        if os.getenv("AUTO_BUY_ENABLED"):
            config.auto_buy_enabled = os.getenv("AUTO_BUY_ENABLED").lower() == "true"
        if os.getenv("PAPER_TRADING_MODE"):
            config.paper_trading_mode = os.getenv("PAPER_TRADING_MODE").lower() == "true"
        if os.getenv("BUY_AMOUNT_SOL"):
            config.buy_amount_sol = float(os.getenv("BUY_AMOUNT_SOL"))
        if os.getenv("MAX_CONCURRENT_POSITIONS"):
            config.max_concurrent_positions = int(os.getenv("MAX_CONCURRENT_POSITIONS"))
        
        # Take profit levels
        if os.getenv("TP1_X"):
            config.tp1_x = float(os.getenv("TP1_X"))
        if os.getenv("TP1_SELL_PCT"):
            config.tp1_sell_pct = float(os.getenv("TP1_SELL_PCT"))
        if os.getenv("TP2_X"):
            config.tp2_x = float(os.getenv("TP2_X"))
        if os.getenv("TP2_SELL_PCT"):
            config.tp2_sell_pct = float(os.getenv("TP2_SELL_PCT"))
        if os.getenv("TP3_X"):
            config.tp3_x = float(os.getenv("TP3_X"))
        if os.getenv("TP3_SELL_PCT"):
            config.tp3_sell_pct = float(os.getenv("TP3_SELL_PCT"))
        
        # Risk management
        if os.getenv("STOP_LOSS_PCT"):
            config.stop_loss_pct = float(os.getenv("STOP_LOSS_PCT"))
        if os.getenv("TRAILING_STOP_ENABLED"):
            config.trailing_stop_enabled = os.getenv("TRAILING_STOP_ENABLED").lower() == "true"
        if os.getenv("TRAILING_STOP_PCT"):
            config.trailing_stop_pct = float(os.getenv("TRAILING_STOP_PCT"))
        if os.getenv("MAX_HOLD_TIME_HOURS"):
            config.max_hold_time_hours = float(os.getenv("MAX_HOLD_TIME_HOURS"))
        
        config.save()
        return config
