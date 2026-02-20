"""
TRADING ENGINE
Coordinates trader, positions, and strategy execution
"""

import asyncio
from datetime import datetime, timezone
from typing import Optional, Tuple
from .trader import SolanaTrader, TradeResult
from .position_manager import PositionManager, Position
from .trading_config import TradingConfig

class TradingEngine:
    def __init__(self, trader: SolanaTrader, position_manager: PositionManager, config: TradingConfig):
        self.trader = trader
        self.pm = position_manager
        self.config = config
        self.daily_buys_count = 0
        self.last_reset_date = datetime.now(timezone.utc).date()
        self.pending_confirmations = {}  # For multi-step confirmations
    
    async def execute_buy(self, mint: str, name: str, symbol: str, alert_mcap: float) -> Tuple[bool, str, Optional[Position]]:
        """
        Execute buy trade (real or paper)
        
        Returns: (success, message, position)
        """
        # Reset daily counter if new day
        self._check_daily_reset()
        
        # Pre-flight checks
        checks_ok, check_msg = await self._pre_buy_checks(mint)
        if not checks_ok:
            return False, check_msg, None
        
        # Paper trading mode
        if self.config.paper_trading_mode:
            return await self._execute_paper_buy(mint, name, symbol, alert_mcap)
        
        # Real trading mode
        return await self._execute_real_buy(mint, name, symbol, alert_mcap)
    
    async def _pre_buy_checks(self, mint: str) -> Tuple[bool, str]:
        """Pre-flight safety checks"""
        # Check if auto-buy enabled
        if not self.config.auto_buy_enabled:
            return False, "Auto-buy disabled"
        
        # Check daily buy limit
        if self.daily_buys_count >= self.config.max_daily_buys:
            return False, f"Daily buy limit reached ({self.config.max_daily_buys})"
        
        # Check position limit
        active_positions = len(self.pm.get_active_positions())
        if active_positions >= self.config.max_concurrent_positions:
            return False, f"Max positions ({self.config.max_concurrent_positions}) reached"
        
        # Check daily loss limit
        daily_pnl = self.pm.get_daily_pnl()
        if daily_pnl < -self.config.max_daily_loss_sol:
            return False, f"Daily loss limit hit ({daily_pnl:.3f} SOL)"
        
        # Check if already have position in this token
        if self.pm.get_position(mint):
            return False, "Already have position in this token"
        
        # Check SOL balance (real trading only)
        if not self.config.paper_trading_mode:
            balance = await self.trader.get_sol_balance()
            required = self.config.buy_amount_sol + 0.01  # +0.01 for gas
            if balance < required:
                return False, f"Insufficient SOL ({balance:.3f} < {required:.3f})"
        
        return True, "OK"
    
    async def _execute_paper_buy(self, mint: str, name: str, symbol: str, alert_mcap: float) -> Tuple[bool, str, Optional[Position]]:
        """Simulate buy for paper trading"""
        # Simulate token amount (rough estimate based on mcap)
        sol_amount = self.config.buy_amount_sol
        estimated_price = alert_mcap / 1_000_000_000 if alert_mcap > 0 else 0.00001  # Rough estimate
        tokens = sol_amount / estimated_price if estimated_price > 0 else 1_000_000
        
        # Open position
        pos = self.pm.open_position(
            mint=mint,
            name=name,
            symbol=symbol,
            entry_sol=sol_amount,
            entry_tokens=tokens,
            entry_price_sol=estimated_price
        )
        
        self.daily_buys_count += 1
        
        msg = f"📝 PAPER BUY: {name} (${symbol})\n"
        msg += f"   Amount: {tokens:,.0f} tokens\n"
        msg += f"   Cost: {sol_amount} SOL (simulated)\n"
        msg += f"   Entry Price: ${estimated_price:.8f}"
        
        return True, msg, pos
    
    async def _execute_real_buy(self, mint: str, name: str, symbol: str, alert_mcap: float) -> Tuple[bool, str, Optional[Position]]:
        """Execute real buy on blockchain"""
        sol_amount = self.config.buy_amount_sol
        
        # Execute trade via Jupiter
        result = await self.trader.buy_token(
            mint=mint,
            sol_amount=sol_amount,
            slippage_bps=self.config.max_slippage_bps
        )
        
        if not result.success:
            return False, f"Buy failed: {result.error}", None
        
        # Calculate entry price
        entry_price = sol_amount / result.amount_out if result.amount_out > 0 else 0
        
        # Open position
        pos = self.pm.open_position(
            mint=mint,
            name=name,
            symbol=symbol,
            entry_sol=sol_amount,
            entry_tokens=result.amount_out,
            entry_price_sol=entry_price
        )
        
        self.daily_buys_count += 1
        
        msg = f"🟢 BOUGHT: {name} (${symbol})\n"
        msg += f"   Amount: {result.amount_out:,.0f} tokens\n"
        msg += f"   Cost: {sol_amount} SOL\n"
        msg += f"   Entry Price: ${entry_price:.8f}\n"
        msg += f"   Tx: <code>{result.signature[:16]}...</code>"
        
        return True, msg, pos
    
    async def check_and_execute_exits(self, mint: str, current_price_sol: float, current_mcap: float) -> Optional[str]:
        """
        Check exit conditions and execute if triggered
        
        Returns: Message if exit executed, None otherwise
        """
        pos = self.pm.get_position(mint)
        if not pos or pos.status == "closed":
            return None
        
        # Update position with current price
        self.pm.update_position_price(mint, current_price_sol, current_mcap)
        
        # Check stop loss
        if current_price_sol < pos.entry_price_sol * (1 - self.config.stop_loss_pct / 100):
            return await self._execute_sell(pos, 100, "STOP LOSS", current_price_sol)
        
        # Check trailing stop
        if self.config.trailing_stop_enabled and pos.trailing_stop_triggered(self.config.trailing_stop_pct):
            return await self._execute_sell(pos, 100, f"TRAILING STOP (-{self.config.trailing_stop_pct}%)", current_price_sol)
        
        # Check max hold time
        if pos.age_hours() > self.config.max_hold_time_hours:
            return await self._execute_sell(pos, 100, f"MAX HOLD TIME ({self.config.max_hold_time_hours}h)", current_price_sol)
        
        # Check take profit levels
        current_x = pos.current_x
        
        # TP3 - sell remaining
        if current_x >= self.config.tp3_x and pos.status != "closed":
            return await self._execute_sell(pos, self.config.tp3_sell_pct, f"TP3 ({self.config.tp3_x}X)", current_price_sol)
        
        # TP2 - partial exit
        if current_x >= self.config.tp2_x and pos.status == "active":
            return await self._execute_sell(pos, self.config.tp2_sell_pct, f"TP2 ({self.config.tp2_x}X)", current_price_sol)
        
        # TP1 - partial exit
        if current_x >= self.config.tp1_x and pos.status == "active":
            return await self._execute_sell(pos, self.config.tp1_sell_pct, f"TP1 ({self.config.tp1_x}X)", current_price_sol)
        
        return None
    
    async def _execute_sell(self, pos: Position, sell_pct: float, reason: str, current_price: float) -> str:
        """Execute sell (real or paper)"""
        tokens_to_sell = pos.remaining_tokens * (sell_pct / 100)
        
        if self.config.paper_trading_mode:
            return await self._execute_paper_sell(pos, tokens_to_sell, sell_pct, reason, current_price)
        else:
            return await self._execute_real_sell(pos, tokens_to_sell, sell_pct, reason)
    
    async def _execute_paper_sell(self, pos: Position, tokens: float, sell_pct: float, reason: str, current_price: float) -> str:
        """Simulate sell for paper trading"""
        sol_received = tokens * current_price
        
        # Update position
        self.pm.close_position_partial(pos.mint, tokens, sol_received)
        
        # Get updated position
        pos = self.pm.get_position(pos.mint)
        
        msg = f"📝 PAPER SELL {sell_pct:.0f}%: {pos.name}\n"
        msg += f"   Reason: {reason}\n"
        msg += f"   Tokens: {tokens:,.0f}\n"
        msg += f"   Received: {sol_received:.4f} SOL (simulated)\n"
        msg += f"   PnL: {pos.pnl_pct():+.1f}%"
        
        if pos.status == "closed":
            msg += f"\n   Position CLOSED"
        
        return msg
    
    async def _execute_real_sell(self, pos: Position, tokens: float, sell_pct: float, reason: str) -> str:
        """Execute real sell on blockchain"""
        result = await self.trader.sell_token(
            mint=pos.mint,
            token_amount=tokens,
            slippage_bps=self.config.max_slippage_bps
        )
        
        if not result.success:
            return f"❌ Sell failed: {result.error}"
        
        # Update position
        self.pm.close_position_partial(pos.mint, tokens, result.amount_out)
        
        # Get updated position
        pos = self.pm.get_position(pos.mint)
        
        msg = f"💰 SOLD {sell_pct:.0f}%: {pos.name}\n"
        msg += f"   Reason: {reason}\n"
        msg += f"   Tokens: {tokens:,.0f}\n"
        msg += f"   Received: {result.amount_out:.4f} SOL\n"
        msg += f"   PnL: {pos.pnl_pct():+.1f}%\n"
        msg += f"   Tx: <code>{result.signature[:16]}...</code>"
        
        if pos.status == "closed":
            msg += f"\n   Position CLOSED"
        
        return msg
    
    def _check_daily_reset(self):
        """Reset daily counters if new day"""
        today = datetime.now(timezone.utc).date()
        if today > self.last_reset_date:
            self.daily_buys_count = 0
            self.last_reset_date = today
    
    def get_status_summary(self) -> dict:
        """Get trading status summary"""
        active_positions = self.pm.get_active_positions()
        total_deployed = self.pm.get_total_capital_deployed()
        total_pnl = self.pm.get_total_pnl()
        daily_pnl = self.pm.get_daily_pnl()
        
        return {
            "auto_buy": self.config.auto_buy_enabled,
            "paper_mode": self.config.paper_trading_mode,
            "active_positions": len(active_positions),
            "max_positions": self.config.max_concurrent_positions,
            "total_deployed_sol": total_deployed,
            "total_pnl_sol": total_pnl,
            "daily_pnl_sol": daily_pnl,
            "daily_buys": self.daily_buys_count,
            "max_daily_buys": self.config.max_daily_buys,
            "buy_amount_sol": self.config.buy_amount_sol,
        }
