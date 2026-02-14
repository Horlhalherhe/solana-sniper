"""
SOLANA_NARRATIVE_SNIPER — MAIN ORCHESTRATOR
"""

import os
from datetime import datetime
from typing import Optional

from core.narrative_engine import NarrativeEngine
from core.rug_analyzer import RugAnalyzer
from core.entry_scorer import EntryScorer, EntryInput
from core.meta_intelligence import MetaIntelligence

ALERT_THRESHOLD = float(os.getenv("ALERT_THRESHOLD", "5.5"))


class Alert:
    def __init__(self, token_data, narrative_match, rug_report, entry_score, meta):
        self.token = token_data
        self.narrative = narrative_match
        self.rug = rug_report
        self.entry = entry_score
        self.meta = meta
        self.timestamp = datetime.utcnow().isoformat()

    def render(self) -> str:
        e = self.entry
        r = self.rug
        n = self.narrative
        lines = [
            "╔══════════════════════════════════════════════╗",
            f"  🎯 SNIPER ALERT — {self.token.get('name','???')} (${self.token.get('symbol','???')})",
            "╚══════════════════════════════════════════════╝",
            f"  MINT     {self.token.get('mint','N/A')}",
            f"  AGE      {self.token.get('age_hours','?')}h",
            f"  LP       ${self.token.get('liquidity_usd',0):,.0f}",
            "",
            f"  NARRATIVE  {n.get('narrative',{}).get('keyword','N/A') if n.get('narrative') else 'NONE'}",
            f"  STRENGTH   {n.get('narrative_score',0):.1f}/10  ({n.get('confidence',0)*100:.0f}% match)",
            "",
            f"  RUG SCORE  {r.get('rug_score','?')}/10  {r.get('verdict','')}",
            f"  FLAGS      {', '.join([f['code'] for f in r.get('flags',[])[:3]]) or 'None'}",
            "",
            f"  ENTRY SCORE  {e.get('final_score','?')}/10  {e.get('verdict','')}",
        ]
        if e.get("signals"):
            lines += [""] + [f"  ✅ {s}" for s in e["signals"][:3]]
        if e.get("warnings"):
            lines += [f"  ⚠️  {w}" for w in e["warnings"][:3]]
        if self.meta.get("deployer_risk"):
            lines += ["", f"  {self.meta['deployer_risk']}"]
        lines += ["", f"  🕐 {self.timestamp}", "═" * 48]
        return "\n".join(lines)

    def to_dict(self):
        return {
            "token": self.token,
            "narrative": self.narrative,
            "rug": self.rug,
            "entry": self.entry,
            "meta": self.meta,
            "timestamp": self.timestamp,
        }


class SolanaNarrativeSniper:

    def __init__(self):
        self.narrative_engine = NarrativeEngine()
        self.rug_analyzer = RugAnalyzer()
        self.entry_scorer = EntryScorer()
        self.meta_intel = MetaIntelligence()
        self.alert_log: list[Alert] = []
        print("[SNIPER] All engines online.")

    def analyze_token(self, token_data: dict) -> Alert:
        name   = token_data.get("name", "")
        symbol = token_data.get("symbol", "")
        desc   = token_data.get("description", "")

        narrative_match = self.narrative_engine.match_token_to_narrative(name, symbol, desc)
        if not narrative_match["matched"]:
            narrative_match["narrative_score"] = 3.0
            narrative_match["confidence"] = 0.2

        rug_report = self.rug_analyzer.analyze_manual(token_data)

        entry_input = EntryInput(
            narrative_score=narrative_match.get("narrative_score", 3.0),
            narrative_confidence=narrative_match.get("confidence", 0.2),
            launch_age_hours=token_data.get("age_hours", 0),
            market_conditions=token_data.get("market_conditions", "neutral"),
            total_holders=token_data.get("total_holders", 0),
            top10_pct=token_data.get("top10_pct", 50),
            dev_holds_pct=token_data.get("dev_holds_pct", 5),
            wallet_clusters=token_data.get("wallet_clusters", 0),
            holder_growth_1h=token_data.get("holder_growth_1h", 0),
            rug_score=rug_report.rug_score,
            deployer_age_days=token_data.get("deployer_age_days", 0),
            deployer_prev_rugs=len(token_data.get("deployer_prev_rugs", [])),
            volume_5m_usd=token_data.get("volume_5m_usd", 0),
            volume_1h_usd=token_data.get("volume_1h_usd", 0),
            price_change_5m_pct=token_data.get("price_change_5m_pct", 0),
            price_change_1h_pct=token_data.get("price_change_1h_pct", 0),
            buy_sell_ratio_1h=token_data.get("buy_sell_ratio_1h", 1.0),
            lp_usd=token_data.get("liquidity_usd", 0),
        )
        entry_breakdown = self.entry_scorer.score(entry_input)

        deployer = token_data.get("deployer", "unknown")
        deployer_profile = self.meta_intel.get_deployer(deployer)
        narrative_farm = self.meta_intel.detect_narrative_farming(deployer)
        meta_notes = ""
        if deployer_profile and deployer_profile["risk_rating"] == "serial_rugger":
            meta_notes = f"⚠️ SERIAL RUGGER — {deployer_profile['rug_rate']*100:.0f}% rug rate"
        elif narrative_farm.get("detected"):
            meta_notes = f"⚠️ NARRATIVE FARMER detected"
        elif deployer_profile and deployer_profile["risk_rating"] == "clean":
            meta_notes = f"✅ Clean deployer — 0 rugs"

        narrative_kw = (narrative_match.get("narrative", {}) or {}).get("keyword", "unknown")
        self.meta_intel.register_token(
            mint=token_data.get("mint", "unknown"),
            name=name, symbol=symbol, deployer=deployer,
            narrative=narrative_kw,
            entry_score=entry_breakdown.final,
            rug_score=rug_report.rug_score,
        )

        alert = Alert(
            token_data=token_data,
            narrative_match={**narrative_match,
                             "narrative_score": narrative_match.get("narrative_score", 3.0)},
            rug_report=rug_report.to_dict(),
            entry_score=entry_breakdown.to_dict(),
            meta={"deployer_risk": meta_notes},
        )
        self.alert_log.append(alert)
        return alert

    def feed_narrative(self, texts: list[str]):
        detected = self.narrative_engine.ingest_batch(texts)
        print(f"[SNIPER] Narratives: {[n.keyword for n in detected]}")
        return detected

    def inject_narrative(self, keyword: str, category: str, score: float, notes: str = ""):
        n = self.narrative_engine.inject_manual_narrative(keyword, category, score, notes)
        print(f"[SNIPER] Injected: {keyword} ({category}) score={score}")
        return n

    def get_active_narratives(self) -> list[dict]:
        return self.narrative_engine.get_active_sorted()

    def get_intel_summary(self) -> dict:
        return self.meta_intel.get_intel_summary()

    def get_recent_alerts(self, n: int = 10) -> list[dict]:
        return [a.to_dict() for a in self.alert_log[-n:]]
