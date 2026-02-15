"""
ENTRY QUALITY ENGINE
Scores a token entry opportunity 1-10.
"""

from dataclasses import dataclass
from typing import Optional

WEIGHTS = {
    "narrative": 0.25,
    "timing":    0.20,
    "holders":   0.20,
    "deployer":  0.20,
    "momentum":  0.15,
}

@dataclass
class EntryInput:
    narrative_score: float = 5.0
    narrative_confidence: float = 0.5
    launch_age_hours: float = 0.0
    market_conditions: str = "neutral"
    total_holders: int = 0
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

@dataclass
class ScoreBreakdown:
    narrative: float
    timing: float
    holders: float
    deployer: float
    momentum: float
    final: float
    verdict: str
    signals: list[str]
    warnings: list[str]

    def to_dict(self):
        return {
            "components": {
                "narrative": {"score": round(self.narrative, 2), "weight": "25%"},
                "timing":    {"score": round(self.timing, 2),    "weight": "20%"},
                "holders":   {"score": round(self.holders, 2),   "weight": "20%"},
                "deployer":  {"score": round(self.deployer, 2),  "weight": "20%"},
                "momentum":  {"score": round(self.momentum, 2),  "weight": "15%"},
            },
            "final_score": round(self.final, 2),
            "verdict": self.verdict,
            "signals": self.signals,
            "warnings": self.warnings,
        }


class EntryScorer:

    def score(self, data: EntryInput) -> ScoreBreakdown:
        narrative_score = self._score_narrative(data)
        timing_score    = self._score_timing(data)
        holders_score   = self._score_holders(data)
        deployer_score  = self._score_deployer(data)
        momentum_score  = self._score_momentum(data)

        final = (
            narrative_score * WEIGHTS["narrative"] +
            timing_score    * WEIGHTS["timing"] +
            holders_score   * WEIGHTS["holders"] +
            deployer_score  * WEIGHTS["deployer"] +
            momentum_score  * WEIGHTS["momentum"]
        )
        final = round(min(max(final, 1.0), 10.0), 2)
        signals, warnings = self._extract_signals(data, narrative_score, deployer_score)

        return ScoreBreakdown(
            narrative=narrative_score, timing=timing_score,
            holders=holders_score, deployer=deployer_score,
            momentum=momentum_score, final=final,
            verdict=self._verdict(final),
            signals=signals, warnings=warnings,
        )

    def _score_narrative(self, d: EntryInput) -> float:
        base = d.narrative_score
        confidence_mult = 0.5 + (d.narrative_confidence * 0.5)
        return min(base * confidence_mult, 10.0)

    def _score_timing(self, d: EntryInput) -> float:
        h = d.launch_age_hours
        if h < 0.25:    base = 3.0
        elif h < 0.5:   base = 5.0
        elif h < 2:     base = 8.5
        elif h < 12:    base = 7.5
        elif h < 24:    base = 5.0
        elif h < 48:    base = 3.5
        else:           base = 2.0
        mkt_mod = {"bull": 1.2, "neutral": 1.0, "bear": 0.7}.get(d.market_conditions, 1.0)
        return min(base * mkt_mod, 10.0)

    def _score_holders(self, d: EntryInput) -> float:
        score = 10.0
        if d.total_holders < 50:        score -= 3.0
        elif d.total_holders < 200:     score -= 1.5
        elif d.total_holders > 1000:    score += 1.0
        if d.top10_pct > 30:            score -= 5.0
        elif d.top10_pct > 20:          score -= 2.0
        elif d.top10_pct <= 30:         score += 2.0
        if d.top1_pct > 5:              score -= 5.0
        elif d.top1_pct <= 5:           score += 2.0
        if d.wallet_clusters > 3:       score -= 2.5
        elif d.wallet_clusters > 0:     score -= 1.0
        if d.holder_growth_1h > 50:     score += 1.5
        elif d.holder_growth_1h > 20:   score += 0.75
        return max(min(score, 10.0), 1.0)

    def _score_deployer(self, d: EntryInput) -> float:
        base = 11.0 - d.rug_score
        if d.deployer_prev_rugs == 0 and d.deployer_age_days > 90:
            base = min(base + 1.0, 10.0)
        elif d.deployer_prev_rugs > 0:
            base -= d.deployer_prev_rugs * 2.0
        return max(min(base, 10.0), 1.0)

    def _score_momentum(self, d: EntryInput) -> float:
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

    def _verdict(self, score: float) -> str:
        if score >= 8.0:   return "🟢 STRONG ENTRY"
        elif score >= 6.5: return "🟡 VIABLE ENTRY"
        elif score >= 5.0: return "🟠 WEAK ENTRY"
        elif score >= 3.5: return "🔴 HIGH RISK ENTRY"
        else:              return "⛔ AVOID"

    def _extract_signals(self, d: EntryInput, nar_score: float, dep_score: float):
        signals, warnings = [], []
        if d.narrative_score >= 7 and d.narrative_confidence > 0.7:
            signals.append(f"Strong narrative match ({d.narrative_confidence*100:.0f}% confidence)")
        if 0.5 <= d.launch_age_hours <= 2:
            signals.append(f"Optimal timing ({d.launch_age_hours:.1f}h post-launch)")
        if d.top10_pct < 30:
            signals.append(f"Well distributed (top10: {d.top10_pct:.1f}%)")
        if d.buy_sell_ratio_1h > 2:
            signals.append(f"Buy pressure: {d.buy_sell_ratio_1h:.1f}x")
        if d.volume_1h_usd > 100_000:
            signals.append(f"High volume: ${d.volume_1h_usd:,.0f}/1h")
        if d.holder_growth_1h > 30:
            signals.append(f"Holder growth: +{d.holder_growth_1h:.0f}%/h")
        if d.rug_score > 6:
            warnings.append(f"High rug score: {d.rug_score}/10")
        if d.top10_pct > 60:
            warnings.append(f"Concentrated: top10 = {d.top10_pct:.1f}%")
        if d.launch_age_hours > 24:
            warnings.append(f"Late entry: {d.launch_age_hours:.0f}h old")
        if d.wallet_clusters > 0:
            warnings.append(f"{d.wallet_clusters} cluster(s) detected")
        if d.deployer_prev_rugs > 0:
            warnings.append(f"Deployer has {d.deployer_prev_rugs} rug(s)")
        if d.lp_usd < 10_000:
            warnings.append(f"Low liquidity: ${d.lp_usd:,.0f}")
        return signals[:5], warnings[:5]
