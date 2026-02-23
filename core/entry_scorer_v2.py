"""
ENTRY SCORER v2
===============
Upgraded scorer that integrates:
- Smart money signals (NEW)
- Rug analyzer v2 data (UPGRADED)  
- Adaptive weights from backtesting (NEW)
- Original narrative/timing/holders/momentum scoring (KEPT)

Drop-in replacement for your existing EntryScorer.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass
class EntryInputV2:
    """Extended input that includes smart money + v2 rug data"""
    # Original fields (kept from your v1)
    narrative_score: float = 5.0
    narrative_confidence: float = 0.5
    launch_age_hours: float = 0.0
    market_conditions: str = "neutral"
    total_holders: int = 0
    top1_pct: float = 5.0
    top10_pct: float = 50.0
    dev_holds_pct: float = 5.0
    wallet_clusters: int = 0
    holder_growth_1h: float = 0.0
    rug_score: float = 5.0
    deployer_age_days: int = 0
    deployer_prev_rugs: int = 0
    volume_5m_usd: float = 0.0
    volume_1h_usd: float = 0.0
    price_change_5m_pct: float = 0.0
    price_change_1h_pct: float = 0.0
    buy_sell_ratio_1h: float = 1.0
    lp_usd: float = 0.0
    
    # NEW: Smart money data
    smart_money_wallets_in: int = 0
    smart_money_signal_strength: float = 0.0
    smart_money_avg_confidence: float = 0.0
    smart_money_total_sol: float = 0.0
    smart_money_is_consensus: bool = False
    
    # NEW: Enhanced rug data
    is_honeypot: bool = False
    honeypot_sell_tax_pct: float = 0.0
    is_honeypot_suspicious: bool = False
    mint_authority_revoked: bool = True
    freeze_authority_revoked: bool = True
    lp_burned: bool = False
    lp_locked: bool = False
    sniper_count: int = 0
    is_heavily_bundled: bool = False
    deployer_is_serial: bool = False


class EntryScorerV2:
    """
    Scores token entries with smart money integration.
    
    Can work in two modes:
    1. STATIC weights (like your v1, but with more components)
    2. ADAPTIVE weights (from backtester optimization)
    """
    
    def __init__(self, adaptive_scorer=None):
        """
        Args:
            adaptive_scorer: Optional AdaptiveEntryScorer from backtester.
                            If None, uses static weights.
        """
        self.adaptive = adaptive_scorer
    
    def score(self, data: EntryInputV2) -> dict:
        """
        Score a token entry opportunity.
        
        Returns dict with final score, breakdown, signals, warnings.
        """
        # Calculate component scores (each 1-10)
        components = {
            "narrative":    self._score_narrative(data),
            "timing":       self._score_timing(data),
            "holders":      self._score_holders(data),
            "deployer":     self._score_deployer(data),
            "momentum":     self._score_momentum(data),
            "smart_money":  self._score_smart_money(data),
            "rug_safety":   self._score_rug_safety(data),
            "bundles":      self._score_bundles(data),
        }
        
        # Use adaptive scorer if available, otherwise static
        if self.adaptive:
            result = self.adaptive.score(components)
        else:
            result = self._static_score(components)
        
        # Extract signals and warnings
        signals, warnings = self._extract_signals(data, components)
        result["signals"] = signals
        result["warnings"] = warnings
        result["components"] = components
        
        # Hard fails — override if critical issues
        if data.is_honeypot:
            result["final_score"] = 1.0
            result["verdict"] = "⛔ HONEYPOT — DO NOT BUY"
            result["warnings"].insert(0, "🍯 CONFIRMED HONEYPOT")
        
        return result
    
    def _static_score(self, components: dict) -> dict:
        """Static weighted scoring (fallback when no optimizer)"""
        weights = {
            "narrative":    0.15,
            "timing":       0.10,
            "holders":      0.15,
            "deployer":     0.10,
            "momentum":     0.10,
            "smart_money":  0.20,
            "rug_safety":   0.15,
            "bundles":      0.05,
        }
        
        total = sum(components[k] * weights[k] for k in weights)
        final = max(1.0, min(10.0, total))
        
        breakdown = {k: {"raw": round(components[k], 2), 
                         "weight": round(weights[k], 4),
                         "weighted": round(components[k] * weights[k], 3)}
                     for k in weights}
        
        return {
            "final_score": round(final, 2),
            "verdict": self._verdict(final),
            "breakdown": breakdown,
            "weights_source": "static",
        }
    
    # =========================================================
    # COMPONENT SCORERS (each returns 1-10)
    # =========================================================
    
    def _score_narrative(self, d: EntryInputV2) -> float:
        """Score based on narrative match (kept from your v1)"""
        base = d.narrative_score
        confidence_mult = 0.5 + (d.narrative_confidence * 0.5)
        return min(max(base * confidence_mult, 1.0), 10.0)
    
    def _score_timing(self, d: EntryInputV2) -> float:
        """Score based on token age (kept from your v1)"""
        h = d.launch_age_hours
        if h < 0.25:    base = 3.0    # Too early, no data
        elif h < 0.5:   base = 5.0
        elif h < 2:     base = 8.5    # Sweet spot
        elif h < 12:    base = 7.5
        elif h < 24:    base = 5.0
        elif h < 48:    base = 3.5
        else:           base = 2.0
        
        mkt_mod = {"bull": 1.2, "neutral": 1.0, "bear": 0.7}.get(d.market_conditions, 1.0)
        return min(max(base * mkt_mod, 1.0), 10.0)
    
    def _score_holders(self, d: EntryInputV2) -> float:
        """Score based on holder distribution (upgraded from v1)"""
        score = 7.0
        
        # Holder count
        if d.total_holders < 50:        score -= 2.0
        elif d.total_holders < 200:     score -= 1.0
        elif d.total_holders > 1000:    score += 1.5
        
        # Concentration
        if d.top10_pct > 50:            score -= 4.0
        elif d.top10_pct > 30:          score -= 2.0
        elif d.top10_pct <= 20:         score += 1.5
        
        if d.top1_pct > 10:             score -= 3.0
        elif d.top1_pct > 5:            score -= 1.5
        
        # Wallet clusters (NEW — from rug_analyzer_v2)
        if d.wallet_clusters > 3:       score -= 3.0
        elif d.wallet_clusters > 1:     score -= 1.5
        elif d.wallet_clusters > 0:     score -= 0.5
        
        # Growth
        if d.holder_growth_1h > 50:     score += 1.5
        elif d.holder_growth_1h > 20:   score += 0.75
        
        return max(min(score, 10.0), 1.0)
    
    def _score_deployer(self, d: EntryInputV2) -> float:
        """Score based on deployer analysis"""
        score = 7.0
        
        # Age
        if d.deployer_age_days < 1:      score -= 3.0
        elif d.deployer_age_days < 7:    score -= 1.5
        elif d.deployer_age_days > 90:   score += 1.0
        
        # Rug history
        if d.deployer_prev_rugs > 2:     score -= 5.0
        elif d.deployer_prev_rugs > 0:   score -= 3.0
        
        # Serial deployer (NEW)
        if d.deployer_is_serial:         score -= 2.0
        
        return max(min(score, 10.0), 1.0)
    
    def _score_momentum(self, d: EntryInputV2) -> float:
        """Score based on trading momentum (kept from v1)"""
        score = 5.0
        
        if d.volume_1h_usd > 500_000:   score += 2.5
        elif d.volume_1h_usd > 100_000: score += 1.5
        elif d.volume_1h_usd > 10_000:  score += 0.5
        elif d.volume_1h_usd < 1_000:   score -= 2.0
        
        if d.buy_sell_ratio_1h > 3.0:   score += 2.0
        elif d.buy_sell_ratio_1h > 2.0: score += 1.0
        elif d.buy_sell_ratio_1h > 1.5: score += 0.5
        elif d.buy_sell_ratio_1h < 0.5: score -= 2.0
        
        pc = d.price_change_1h_pct
        if 5 < pc < 100:    score += 1.0
        elif pc > 200:      score -= 1.0
        elif pc < -20:      score -= 2.0
        
        if d.lp_usd > 100_000:  score += 1.0
        elif d.lp_usd < 5_000:  score -= 1.5
        
        return max(min(score, 10.0), 1.0)
    
    def _score_smart_money(self, d: EntryInputV2) -> float:
        """
        NEW: Score based on smart money presence.
        This is the most impactful signal.
        """
        if d.smart_money_wallets_in == 0:
            return 3.0  # Neutral — absence isn't necessarily bad
        
        # Base on signal strength (already 0-10)
        score = d.smart_money_signal_strength
        
        # Boost for consensus (3+ wallets agree)
        if d.smart_money_is_consensus:
            score = min(score + 1.5, 10.0)
        
        # Boost for high-confidence wallets
        if d.smart_money_avg_confidence > 0.7:
            score = min(score + 1.0, 10.0)
        
        # Boost for significant capital deployed
        if d.smart_money_total_sol > 5:
            score = min(score + 0.5, 10.0)
        
        return max(min(score, 10.0), 1.0)
    
    def _score_rug_safety(self, d: EntryInputV2) -> float:
        """
        NEW: Inverted rug score — high = safe, low = risky.
        Incorporates honeypot data from v2.
        """
        # Start with inverted rug score
        score = 10.0 - d.rug_score  # rug_score 2 → safety 8
        
        # Honeypot override
        if d.is_honeypot:
            return 1.0
        if d.is_honeypot_suspicious:
            score = min(score, 3.0)
        
        # High sell tax
        if d.honeypot_sell_tax_pct > 30:
            score -= 3.0
        elif d.honeypot_sell_tax_pct > 15:
            score -= 1.5
        
        # Authority checks
        if not d.mint_authority_revoked:
            score -= 2.0
        if not d.freeze_authority_revoked:
            score -= 1.5
        
        # LP status
        if d.lp_burned:
            score += 1.0
        elif not d.lp_locked:
            score -= 1.0
        
        return max(min(score, 10.0), 1.0)
    
    def _score_bundles(self, d: EntryInputV2) -> float:
        """
        NEW: Score based on sniper/bundle activity.
        Low snipers = good (organic buying).
        """
        if d.is_heavily_bundled:
            return 2.0
        
        snipers = d.sniper_count
        if snipers <= 2:    return 9.0
        elif snipers <= 5:  return 7.0
        elif snipers <= 8:  return 5.0
        elif snipers <= 12: return 3.0
        else:               return 1.0
    
    # =========================================================
    # SIGNALS & WARNINGS
    # =========================================================
    
    def _extract_signals(self, d: EntryInputV2, components: dict) -> tuple:
        signals, warnings = [], []
        
        # Smart money signals (most important)
        if d.smart_money_wallets_in > 0:
            signals.append(
                f"💰 {d.smart_money_wallets_in} smart money wallet(s) in "
                f"({d.smart_money_total_sol:.1f} SOL, "
                f"confidence: {d.smart_money_avg_confidence:.0%})"
            )
        if d.smart_money_is_consensus:
            signals.append("🔥 Smart money CONSENSUS (3+ wallets agree)")
        
        # Narrative
        if d.narrative_score >= 7 and d.narrative_confidence > 0.7:
            signals.append(f"📖 Strong narrative ({d.narrative_confidence*100:.0f}% conf)")
        
        # Timing
        if 0.5 <= d.launch_age_hours <= 2:
            signals.append(f"⏰ Sweet spot timing ({d.launch_age_hours:.1f}h)")
        
        # Holders
        if d.top10_pct < 25:
            signals.append(f"👥 Well distributed (top10: {d.top10_pct:.1f}%)")
        
        # Momentum
        if d.buy_sell_ratio_1h > 2:
            signals.append(f"📈 Buy pressure: {d.buy_sell_ratio_1h:.1f}x")
        if d.volume_1h_usd > 100_000:
            signals.append(f"📊 High volume: ${d.volume_1h_usd:,.0f}/1h")
        
        # Safety
        if d.mint_authority_revoked and d.freeze_authority_revoked:
            signals.append("🔒 Contract safe (authorities revoked)")
        if d.lp_burned:
            signals.append("🔥 LP burned")
        
        # Warnings
        if d.is_honeypot:
            warnings.append("🍯 HONEYPOT CONFIRMED — CANNOT SELL")
        elif d.is_honeypot_suspicious:
            warnings.append(f"⚠️ Possible honeypot ({d.honeypot_sell_tax_pct:.0f}% sell tax)")
        
        if not d.mint_authority_revoked:
            warnings.append("🔴 Mint authority NOT revoked")
        if not d.freeze_authority_revoked:
            warnings.append("🔴 Freeze authority active")
        
        if d.rug_score > 6:
            warnings.append(f"☠️ High rug risk: {d.rug_score}/10")
        if d.wallet_clusters > 0:
            warnings.append(f"🕸️ {d.wallet_clusters} wallet cluster(s)")
        if d.is_heavily_bundled:
            warnings.append(f"🤖 Heavily bundled ({d.sniper_count} snipers)")
        if d.deployer_prev_rugs > 0:
            warnings.append(f"⚠️ Deployer rugged {d.deployer_prev_rugs} time(s)")
        if d.deployer_age_days < 7:
            warnings.append(f"⚠️ Fresh deployer wallet ({d.deployer_age_days}d)")
        if d.lp_usd < 10_000:
            warnings.append(f"💧 Low liquidity: ${d.lp_usd:,.0f}")
        if d.launch_age_hours > 24:
            warnings.append(f"⏰ Late entry: {d.launch_age_hours:.0f}h old")
        
        return signals[:6], warnings[:6]
    
    def _verdict(self, score: float) -> str:
        if score >= 8.0:   return "🟢 STRONG ENTRY"
        elif score >= 6.5: return "🟡 VIABLE ENTRY"
        elif score >= 5.0: return "🟠 WEAK ENTRY"
        elif score >= 3.5: return "🔴 HIGH RISK ENTRY"
        else:              return "⛔ AVOID"
