"""
BACKTESTER & ADAPTIVE WEIGHT ENGINE
=====================================
Two core functions:
1. RECORD — Log every token the bot analyzes + its outcome
2. OPTIMIZE — Use historical data to find the best scoring weights

This is what turns gut-feel weights into data-driven ones.
"""

import json
import os
import random
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, asdict, field
from typing import Optional
from pathlib import Path
from collections import defaultdict

DATA_DIR = Path(os.getenv("SNIPER_DATA_DIR", "./data"))
DATA_DIR.mkdir(exist_ok=True)
HISTORY_FILE = DATA_DIR / "signal_history.json"
WEIGHTS_FILE = DATA_DIR / "optimized_weights.json"

# Default weights — these get replaced after optimization
DEFAULT_WEIGHTS = {
    "narrative":    0.25,
    "timing":       0.20,
    "holders":      0.20,
    "deployer":     0.20,
    "momentum":     0.15,
}

# For the full scoring that includes smart money and rug
EXTENDED_WEIGHTS = {
    "narrative":    0.15,
    "timing":       0.10,
    "holders":      0.15,
    "deployer":     0.10,
    "momentum":     0.10,
    "smart_money":  0.20,   # NEW — biggest signal
    "rug_safety":   0.15,   # Inverted rug score (10 - rug_score)
    "bundles":      0.05,   # Low sniper activity
}


@dataclass
class SignalRecord:
    """One record of a token signal and its outcome"""
    mint: str
    name: str = ""
    symbol: str = ""
    timestamp: str = ""
    
    # Input scores (1-10 each)
    narrative_score: float = 5.0
    timing_score: float = 5.0
    holders_score: float = 5.0
    deployer_score: float = 5.0
    momentum_score: float = 5.0
    smart_money_score: float = 0.0
    rug_safety_score: float = 5.0    # 10 - rug_score
    bundles_score: float = 5.0
    
    # Composite score the bot gave
    entry_score: float = 5.0
    
    # Was alert sent?
    alert_sent: bool = False
    
    # Outcome (filled in later)
    outcome: str = "pending"    # pending, winner, loser, rug, unknown
    peak_x: float = 1.0        # Peak return multiplier
    final_x: float = 1.0       # Final return multiplier
    peak_mcap: float = 0.0
    time_to_peak_hours: float = 0.0
    
    # Context
    source: str = ""
    narrative_category: str = ""
    
    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()
    
    def to_dict(self):
        return asdict(self)
    
    def get_component_scores(self) -> dict:
        """Return component scores as a dict for optimization"""
        return {
            "narrative": self.narrative_score,
            "timing": self.timing_score,
            "holders": self.holders_score,
            "deployer": self.deployer_score,
            "momentum": self.momentum_score,
            "smart_money": self.smart_money_score,
            "rug_safety": self.rug_safety_score,
            "bundles": self.bundles_score,
        }


class SignalHistory:
    """
    Persistent history of all signals + outcomes.
    This is the dataset the optimizer uses.
    """
    
    def __init__(self):
        self.records: dict[str, SignalRecord] = {}
        self._load()
    
    def record_signal(self, mint: str, **kwargs) -> SignalRecord:
        """Record a new signal"""
        record = SignalRecord(mint=mint, **kwargs)
        self.records[mint] = record
        self._save()
        return record
    
    def update_outcome(self, mint: str, outcome: str, peak_x: float = 1.0, 
                       final_x: float = 1.0, peak_mcap: float = 0):
        """Update the outcome of a previously recorded signal"""
        if mint in self.records:
            self.records[mint].outcome = outcome
            self.records[mint].peak_x = peak_x
            self.records[mint].final_x = final_x
            self.records[mint].peak_mcap = peak_mcap
            self._save()
    
    def get_completed_records(self) -> list[SignalRecord]:
        """Get records with known outcomes (not pending)"""
        return [r for r in self.records.values() if r.outcome != "pending"]
    
    def get_winners(self) -> list[SignalRecord]:
        return [r for r in self.records.values() if r.outcome == "winner"]
    
    def get_losers(self) -> list[SignalRecord]:
        return [r for r in self.records.values() if r.outcome in ("loser", "rug")]
    
    def get_stats(self) -> dict:
        """Get overall performance stats"""
        completed = self.get_completed_records()
        if not completed:
            return {"total": 0, "message": "No completed signals yet"}
        
        winners = [r for r in completed if r.outcome == "winner"]
        losers = [r for r in completed if r.outcome in ("loser", "rug")]
        rugs = [r for r in completed if r.outcome == "rug"]
        
        # Alerted vs not alerted performance
        alerted = [r for r in completed if r.alert_sent]
        alerted_winners = [r for r in alerted if r.outcome == "winner"]
        
        return {
            "total_signals": len(self.records),
            "completed": len(completed),
            "winners": len(winners),
            "losers": len(losers),
            "rugs": len(rugs),
            "win_rate": round(len(winners) / len(completed), 3) if completed else 0,
            "avg_peak_x_winners": round(
                sum(r.peak_x for r in winners) / len(winners), 2
            ) if winners else 0,
            "avg_peak_x_losers": round(
                sum(r.peak_x for r in losers) / len(losers), 2
            ) if losers else 0,
            "alerted": len(alerted),
            "alerted_win_rate": round(
                len(alerted_winners) / len(alerted), 3
            ) if alerted else 0,
            "best_trade_x": max((r.peak_x for r in completed), default=0),
        }
    
    def get_component_correlations(self) -> dict:
        """
        Analyze which component scores correlate most with winners.
        This tells you which signals actually matter.
        """
        completed = self.get_completed_records()
        if len(completed) < 10:
            return {"message": "Need at least 10 completed signals for correlation analysis"}
        
        components = list(EXTENDED_WEIGHTS.keys())
        correlations = {}
        
        for comp in components:
            winners_scores = []
            losers_scores = []
            
            for r in completed:
                scores = r.get_component_scores()
                score = scores.get(comp, 0)
                if r.outcome == "winner":
                    winners_scores.append(score)
                elif r.outcome in ("loser", "rug"):
                    losers_scores.append(score)
            
            avg_winner = sum(winners_scores) / len(winners_scores) if winners_scores else 0
            avg_loser = sum(losers_scores) / len(losers_scores) if losers_scores else 0
            
            # Separation: how much higher is the avg winner score vs loser
            separation = avg_winner - avg_loser
            
            correlations[comp] = {
                "avg_winner_score": round(avg_winner, 2),
                "avg_loser_score": round(avg_loser, 2),
                "separation": round(separation, 2),
                "predictive_power": "strong" if separation > 2 
                    else "moderate" if separation > 1 
                    else "weak" if separation > 0 
                    else "negative",
            }
        
        # Sort by separation (strongest predictor first)
        sorted_corr = dict(sorted(
            correlations.items(), 
            key=lambda x: x[1]["separation"], 
            reverse=True
        ))
        
        return sorted_corr
    
    def _save(self):
        try:
            data = {mint: r.to_dict() for mint, r in self.records.items()}
            HISTORY_FILE.write_text(json.dumps(data, indent=2))
        except Exception as e:
            print(f"[History] Save error: {e}")
    
    def _load(self):
        try:
            if HISTORY_FILE.exists():
                raw = json.loads(HISTORY_FILE.read_text())
                for mint, data in raw.items():
                    self.records[mint] = SignalRecord(**data)
                print(f"[History] Loaded {len(self.records)} signal record(s)")
        except Exception as e:
            print(f"[History] Load error: {e}")


class WeightOptimizer:
    """
    Finds the optimal scoring weights using historical signal data.
    
    Uses a simple evolutionary approach:
    1. Start with current weights
    2. Generate random mutations
    3. Score each weight set against historical data
    4. Keep the best performers
    5. Repeat
    
    No ML libraries needed — pure Python.
    """
    
    def __init__(self, history: SignalHistory):
        self.history = history
        self.current_weights = self._load_weights()
    
    def optimize(self, generations: int = 100, population: int = 50,
                 min_records: int = 20) -> dict:
        """
        Run weight optimization.
        
        Args:
            generations: Number of optimization rounds
            population: Number of weight candidates per generation
            min_records: Minimum completed records needed
        
        Returns:
            Dict with optimized weights and stats
        """
        completed = self.history.get_completed_records()
        if len(completed) < min_records:
            return {
                "success": False,
                "message": f"Need {min_records} completed signals, have {len(completed)}",
                "weights": self.current_weights,
            }
        
        print(f"[Optimizer] Starting optimization with {len(completed)} records...")
        
        components = list(EXTENDED_WEIGHTS.keys())
        
        # Seed population with current weights + random mutations
        pop = [self.current_weights.copy()]
        for _ in range(population - 1):
            pop.append(self._mutate(self.current_weights, components))
        
        best_weights = self.current_weights.copy()
        best_fitness = -float("inf")
        
        for gen in range(generations):
            # Evaluate fitness of each weight set
            scored = []
            for weights in pop:
                fitness = self._evaluate_fitness(weights, completed)
                scored.append((fitness, weights))
            
            # Sort by fitness (higher = better)
            scored.sort(key=lambda x: x[0], reverse=True)
            
            # Track best
            if scored[0][0] > best_fitness:
                best_fitness = scored[0][0]
                best_weights = scored[0][1].copy()
            
            # Create next generation
            # Keep top 20% (elitism)
            elite_count = max(2, population // 5)
            new_pop = [s[1] for s in scored[:elite_count]]
            
            # Fill rest with mutations of top performers
            while len(new_pop) < population:
                parent = random.choice([s[1] for s in scored[:elite_count * 2]])
                child = self._mutate(parent, components)
                new_pop.append(child)
            
            pop = new_pop
        
        # Normalize final weights
        best_weights = self._normalize(best_weights)
        
        # Calculate improvement
        old_fitness = self._evaluate_fitness(self.current_weights, completed)
        improvement = ((best_fitness - old_fitness) / abs(old_fitness) * 100) if old_fitness != 0 else 0
        
        print(f"[Optimizer] Done! Fitness: {old_fitness:.3f} → {best_fitness:.3f} "
              f"({improvement:+.1f}%)")
        
        # Save optimized weights
        self.current_weights = best_weights
        self._save_weights(best_weights)
        
        return {
            "success": True,
            "weights": best_weights,
            "old_fitness": round(old_fitness, 4),
            "new_fitness": round(best_fitness, 4),
            "improvement_pct": round(improvement, 1),
            "records_used": len(completed),
            "components_ranked": self._rank_components(best_weights),
        }
    
    def _evaluate_fitness(self, weights: dict, records: list[SignalRecord]) -> float:
        """
        Evaluate how well a set of weights separates winners from losers.
        
        Fitness = (avg score of winners) - (avg score of losers) 
                  + bonus for high win rate at threshold
        
        Higher fitness = better weights.
        """
        winner_scores = []
        loser_scores = []
        
        for record in records:
            components = record.get_component_scores()
            # Calculate weighted score
            score = sum(
                components.get(k, 5.0) * weights.get(k, 0) 
                for k in weights
            )
            
            if record.outcome == "winner":
                winner_scores.append(score)
            elif record.outcome in ("loser", "rug"):
                loser_scores.append(score)
        
        if not winner_scores or not loser_scores:
            return 0.0
        
        avg_winner = sum(winner_scores) / len(winner_scores)
        avg_loser = sum(loser_scores) / len(loser_scores)
        
        # Base fitness: separation between winners and losers
        separation = avg_winner - avg_loser
        
        # Bonus: simulate threshold-based alerting
        # Find threshold that maximizes precision
        all_scores = [(s, True) for s in winner_scores] + [(s, False) for s in loser_scores]
        all_scores.sort(key=lambda x: x[0], reverse=True)
        
        # Try top 30% as threshold
        top_n = max(1, len(all_scores) // 3)
        top_signals = all_scores[:top_n]
        precision = sum(1 for _, is_win in top_signals if is_win) / len(top_signals)
        
        # Penalize if rug tokens get high scores
        rug_penalty = 0
        for record in records:
            if record.outcome == "rug":
                components = record.get_component_scores()
                score = sum(components.get(k, 5.0) * weights.get(k, 0) for k in weights)
                if score > 6.0:  # Rug scored above 6 = bad
                    rug_penalty += 0.5
        
        fitness = separation * 2.0 + precision * 3.0 - rug_penalty
        return fitness
    
    def _mutate(self, weights: dict, components: list) -> dict:
        """Create a random mutation of weights"""
        new_weights = weights.copy()
        
        # Pick 1-3 components to mutate
        n_mutations = random.randint(1, min(3, len(components)))
        targets = random.sample(components, n_mutations)
        
        for comp in targets:
            if comp in new_weights:
                # Random adjustment ±50%
                factor = random.uniform(0.5, 1.5)
                new_weights[comp] = max(0.01, new_weights.get(comp, 0.1) * factor)
        
        return self._normalize(new_weights)
    
    def _normalize(self, weights: dict) -> dict:
        """Normalize weights to sum to 1.0"""
        total = sum(weights.values())
        if total == 0:
            return {k: 1.0 / len(weights) for k in weights}
        return {k: round(v / total, 4) for k, v in weights.items()}
    
    def _rank_components(self, weights: dict) -> list[dict]:
        """Rank components by weight"""
        sorted_w = sorted(weights.items(), key=lambda x: x[1], reverse=True)
        return [{"component": k, "weight": round(v, 4), "pct": f"{v*100:.1f}%"} 
                for k, v in sorted_w]
    
    def get_weights(self) -> dict:
        """Get current weights (optimized or default)"""
        return self.current_weights
    
    def _save_weights(self, weights: dict):
        try:
            data = {
                "weights": weights,
                "optimized_at": datetime.now(timezone.utc).isoformat(),
                "records_used": len(self.history.get_completed_records()),
            }
            WEIGHTS_FILE.write_text(json.dumps(data, indent=2))
            print(f"[Optimizer] Weights saved")
        except Exception as e:
            print(f"[Optimizer] Save error: {e}")
    
    def _load_weights(self) -> dict:
        try:
            if WEIGHTS_FILE.exists():
                data = json.loads(WEIGHTS_FILE.read_text())
                weights = data.get("weights", {})
                if weights:
                    print(f"[Optimizer] Loaded optimized weights "
                          f"(from {data.get('optimized_at', 'unknown')})")
                    return weights
        except Exception as e:
            print(f"[Optimizer] Load error: {e}")
        
        print(f"[Optimizer] Using default weights")
        return EXTENDED_WEIGHTS.copy()


class AdaptiveEntryScorer:
    """
    Upgraded EntryScorer that uses optimized weights from backtesting.
    
    Drop-in replacement for your existing EntryScorer.
    Uses the same component scores but with data-driven weights.
    """
    
    def __init__(self, optimizer: WeightOptimizer):
        self.optimizer = optimizer
    
    def score(self, components: dict) -> dict:
        """
        Score a token using adaptive weights.
        
        Args:
            components: Dict of component scores (1-10 each):
                {
                    "narrative": 7.0,
                    "timing": 8.0,
                    "holders": 6.0,
                    "deployer": 5.0,
                    "momentum": 7.0,
                    "smart_money": 9.0,
                    "rug_safety": 8.0,
                    "bundles": 7.0,
                }
        
        Returns:
            Scoring result with breakdown
        """
        weights = self.optimizer.get_weights()
        
        # Calculate weighted score
        weighted_scores = {}
        for comp, weight in weights.items():
            raw_score = components.get(comp, 5.0)
            weighted_scores[comp] = {
                "raw": round(raw_score, 2),
                "weight": round(weight, 4),
                "weighted": round(raw_score * weight, 3),
            }
        
        total = sum(ws["weighted"] for ws in weighted_scores.values())
        # Scale to 1-10
        final = max(1.0, min(10.0, total))
        
        return {
            "final_score": round(final, 2),
            "verdict": self._verdict(final),
            "breakdown": weighted_scores,
            "weights_source": "optimized" if WEIGHTS_FILE.exists() else "default",
        }
    
    def _verdict(self, score: float) -> str:
        if score >= 8.0:   return "🟢 STRONG ENTRY"
        elif score >= 6.5: return "🟡 VIABLE ENTRY"
        elif score >= 5.0: return "🟠 WEAK ENTRY"
        elif score >= 3.5: return "🔴 HIGH RISK ENTRY"
        else:              return "⛔ AVOID"
