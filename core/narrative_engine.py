"""
NARRATIVE ENGINE (PATCHED)
Detects, categorizes, and scores emerging narratives.

PATCH: Fixed substring matching bug.
"ai" no longer matches "training", "eth" no longer matches "method", etc.
Uses regex word boundaries (\b) for all keyword matching.
"""

import re
from datetime import datetime
from dataclasses import dataclass
from typing import Optional
from collections import defaultdict

NARRATIVE_CATEGORIES = {
    "ai_tech": [
        "gpt", "claude", "clawd", "gemini", "llm", "agi", "sora", "devin", "operator",
        "ai agent", "autonomous", "reasoning model", "o3", "o4", "openai", "anthropic",
        "deepseek", "qwen", "mistral", "groq", "inference", "multimodal", "ai", "agent",
        "aixbt", "labs", "router", "bytedance", "perplexity", "grok",
        # 2026 additions
        "chatgpt", "sam altman", "nvidia", "meta"
    ],
    "animals": [
        "dog", "cat", "frog", "pepe", "bird", "hamster", "penguin", "bear", "wolf",
        "shiba", "doge", "seal", "capybara", "raccoon", "owl", "fox", "goat", "duck",
        "monkey", "panda", "hippo", "luna", "dragon", "tiger", "rabbit", "snake",
        # 2026 additions
        "corgi", "shib", "lobstar", "openclaw", "molty", "claw", "lobster"
    ],
    "culture_meme": [
        "gigachad", "wojak", "npc", "sigma", "based", "irl", "brainrot", "skibidi",
        "rizz", "goat", "giga", "chad", "ratio", "lowkey", "delulu", "slay",
        # nostalgia/throwback memes
        "harambe", "shrek", "ugandan", "dat boi", "nyan", "grumpy", "leeroy",
        "rickroll", "gangnam", "harlem", "planking", "mlg", "doritos", "fedora",
        "hotdog", "numa",
        # 2026 additions
        "apu", "bobo", "sminem", "bogdanoff", "alpha", "cheems"
    ],
    "politics_geo": [
        "whitehouse", "executive", "tariff", "election", "vote", "political", "wlfi",
        # Chinese narrative
        "china", "chinese", "xi", "ccp", "taiwan", "beijing", "shanghai",
        "yuan", "baidu", "alibaba", "tencent", "wechat", "tiktok", "hong kong"
    ],
    "sports": [
        "nba", "nfl", "fifa", "soccer", "football", "basketball",
        "messi", "ronaldo", "lebron", "curry", "kobe",
        "superbowl", "worldcup", "champions", "championship",
        "ufc", "mma", "boxing", "f1", "ferrari", "hamilton",
        "rugby", "cricket", "tennis", "golf", "olympic"
    ],
    "gaming_virtual": [
        "minecraft", "roblox", "fortnite", "gta", "pokemon", "zelda", "anime",
        "vtuber", "streaming", "twitch", "esport", "speedrun", "nft", "metaverse",
        # 2026 additions
        "gta6", "call of duty", "apex"
    ],
    "finance_macro": [
        "fed", "inflation", "recession", "btc", "eth", "rate cut", "rate hike",
        "black swan", "bubble", "crash", "bull", "bear", "halving", "etf",
        # 2026 additions
        "vitalik", "cz", "sbf", "do kwon", "coin", "crypto"
    ]
}

VELOCITY_MULTIPLIERS = {
    "breaking": 2.0,
    "viral": 1.8,
    "trending": 1.6,
    "leaked": 1.5,
    "announced": 1.4,
    "launched": 1.3,
    "rumor": 1.2,
}

# ── Pre-compile regex patterns for each keyword ──────────────────────────────
# This is the FIX: use word boundaries so "ai" doesn't match "training"
_KEYWORD_PATTERNS: dict[str, re.Pattern] = {}

def _build_patterns():
    """Pre-compile regex patterns with word boundaries for all keywords"""
    for category, keywords in NARRATIVE_CATEGORIES.items():
        for kw in keywords:
            if kw not in _KEYWORD_PATTERNS:
                # \b = word boundary. Handles: start/end of string, spaces, punctuation
                # For keywords with special chars, escape them
                escaped = re.escape(kw)
                _KEYWORD_PATTERNS[kw] = re.compile(r'\b' + escaped + r'\b', re.IGNORECASE)

_build_patterns()


def _keyword_in_text(keyword: str, text: str) -> bool:
    """Check if keyword exists in text as a whole word (not substring)"""
    pattern = _KEYWORD_PATTERNS.get(keyword)
    if pattern:
        return bool(pattern.search(text))
    # Fallback for dynamically added keywords
    escaped = re.escape(keyword)
    return bool(re.search(r'\b' + escaped + r'\b', text, re.IGNORECASE))


@dataclass
class Narrative:
    keyword: str
    category: str
    raw_score: float
    velocity_score: float
    final_score: float
    first_seen: datetime
    last_seen: datetime
    mention_count: int
    active: bool = True
    notes: str = ""

    def to_dict(self):
        return {
            "keyword": self.keyword,
            "category": self.category,
            "raw_score": round(self.raw_score, 2),
            "velocity": round(self.velocity_score, 2),
            "score": round(self.final_score, 2),
            "first_seen": self.first_seen.isoformat(),
            "last_seen": self.last_seen.isoformat(),
            "mentions": self.mention_count,
            "active": self.active,
        }


class NarrativeEngine:
    def __init__(self):
        self.active_narratives: dict[str, Narrative] = {}
        self.narrative_history: list[Narrative] = []
        self.keyword_hits: defaultdict = defaultdict(int)

    def ingest_text(self, text: str, source: str = "manual") -> list[Narrative]:
        text_lower = text.lower()
        detected = []
        velocity_mult = 1.0
        for booster, mult in VELOCITY_MULTIPLIERS.items():
            if booster in text_lower:
                velocity_mult = max(velocity_mult, mult)
        for category, keywords in NARRATIVE_CATEGORIES.items():
            for kw in keywords:
                # FIX: word boundary match instead of substring
                if _keyword_in_text(kw, text_lower):
                    self.keyword_hits[kw] += 1
                    narrative = self._update_or_create(kw, category, velocity_mult)
                    detected.append(narrative)
        return detected

    def ingest_batch(self, texts: list[str]) -> list[Narrative]:
        all_detected = []
        for text in texts:
            all_detected.extend(self.ingest_text(text))
        seen = set()
        unique = []
        for n in all_detected:
            if n.keyword not in seen:
                seen.add(n.keyword)
                unique.append(n)
        return unique

    def _update_or_create(self, keyword: str, category: str, velocity_mult: float) -> Narrative:
        now = datetime.utcnow()
        count = self.keyword_hits[keyword]
        if keyword in self.active_narratives:
            n = self.active_narratives[keyword]
            n.mention_count += 1
            n.last_seen = now
            n.velocity_score = min(velocity_mult * (1 + n.mention_count * 0.05), 3.0)
            n.final_score = self._compute_score(n.mention_count, n.velocity_score)
            return n
        raw = min(count * 0.5 + 1.0, 8.0)
        velocity = min(velocity_mult, 3.0)
        final = self._compute_score(count, velocity)
        n = Narrative(keyword=keyword, category=category, raw_score=raw,
                      velocity_score=velocity, final_score=final,
                      first_seen=now, last_seen=now, mention_count=1)
        self.active_narratives[keyword] = n
        return n

    def _compute_score(self, mention_count: int, velocity: float) -> float:
        mention_score = min(mention_count / 20.0 * 7.0, 7.0)
        velocity_bonus = min((velocity - 1.0) * 1.5, 3.0)
        return round(min(1.0 + mention_score + velocity_bonus, 10.0), 2)

    def score_keyword(self, keyword: str) -> Optional[float]:
        kw_lower = keyword.lower()
        if kw_lower in self.active_narratives:
            return self.active_narratives[kw_lower].final_score
        # FIX: use word boundary match for partial lookups too
        for active_kw, narrative in self.active_narratives.items():
            if _keyword_in_text(active_kw, kw_lower) or _keyword_in_text(kw_lower, active_kw):
                return narrative.final_score * 0.7
        return None

    def match_token_to_narrative(self, name: str, symbol: str, description: str = "") -> dict:
        combined = f"{name} {symbol} {description}".lower()
        
        # Debug: print what we're searching in
        print(f"[NARRATIVE] Checking: '{combined[:100]}'")
        
        best_match = None
        best_score = 0.0
        best_confidence = 0.0
        
        for kw, narrative in self.active_narratives.items():
            # FIX: word boundary match instead of substring
            if _keyword_in_text(kw, combined):
                # Higher confidence if keyword matches name or symbol directly
                confidence = 0.95 if (_keyword_in_text(kw, name.lower()) or _keyword_in_text(kw, symbol.lower())) else 0.65
                weighted = narrative.final_score * confidence
                if weighted > best_score:
                    best_score = weighted
                    best_match = narrative
                    best_confidence = confidence
                    print(f"[NARRATIVE] ✓ Matched '{kw}' (score={narrative.final_score})")
        
        if not best_match:
            # Show first few keywords to help debug
            sample_kw = list(self.active_narratives.keys())[:5]
            print(f"[NARRATIVE] ✗ No match (checked {len(self.active_narratives)} keywords, e.g. {sample_kw})")
            return {"matched": False, "narrative": None, "confidence": 0.0, "narrative_score": 0.0}
        
        return {
            "matched": True,
            "narrative": best_match.to_dict(),
            "confidence": round(best_confidence, 2),
            "narrative_score": round(best_match.final_score, 2)
        }

    def get_active_sorted(self, min_score: float = 3.0) -> list[dict]:
        return sorted(
            [n.to_dict() for n in self.active_narratives.values()
             if n.active and n.final_score >= min_score],
            key=lambda x: x["score"], reverse=True)

    def decay_narratives(self, hours_threshold: float = 24.0):
        now = datetime.utcnow()
        for kw, narrative in self.active_narratives.items():
            age = (now - narrative.last_seen).total_seconds() / 3600
            if age > hours_threshold:
                narrative.active = False
                narrative.final_score *= max(0.5, 1 - (age / 48))

    def inject_manual_narrative(self, keyword: str, category: str, score: float, notes: str = ""):
        """Manually inject a narrative with a given score"""
        now = datetime.utcnow()
        kw_lower = keyword.lower()
        self.keyword_hits[kw_lower] = max(int(score * 2), 1)
        
        # Also build a pattern for the new keyword
        if kw_lower not in _KEYWORD_PATTERNS:
            escaped = re.escape(kw_lower)
            _KEYWORD_PATTERNS[kw_lower] = re.compile(r'\b' + escaped + r'\b', re.IGNORECASE)
        
        n = Narrative(
            keyword=kw_lower,
            category=category,
            raw_score=score,
            velocity_score=1.5,
            final_score=min(score, 10.0),
            first_seen=now,
            last_seen=now,
            mention_count=1,
            notes=notes
        )
        self.active_narratives[kw_lower] = n
        return n
