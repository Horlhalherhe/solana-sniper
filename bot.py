"""
TekkiSniPer v4.0 — CLEAN BUILD
Pump.fun → Narrative Match → Simple Score → Telegram Alert
No v2 modules. No complex rug analysis. Just works.
"""

import asyncio
import json
import os
import re
import sys
import logging
import functools
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Dict, List, Tuple
from collections import defaultdict

import httpx
import websockets

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("sniper-bot")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")
HELIUS_API_KEY     = os.getenv("HELIUS_API_KEY", "")
BIRDEYE_API_KEY    = os.getenv("BIRDEYE_API_KEY", "")
ALERT_THRESHOLD    = float(os.getenv("ALERT_THRESHOLD", "5.0"))
MIN_MCAP           = float(os.getenv("MIN_MCAP", "5000"))
MAX_MCAP           = float(os.getenv("MAX_ENTRY_MCAP", "100000"))
MAX_DEV_HOLDS_PCT  = float(os.getenv("MAX_DEV_HOLDS_PCT", "8.0"))
WAIT_SECONDS       = int(os.getenv("WAIT_SECONDS", "60"))

BLACKLIST_STR = os.getenv("BLACKLIST_KEYWORDS", "inu,wif,wif hat,with hat")
BLACKLIST     = [k.strip().lower() for k in BLACKLIST_STR.split(",") if k.strip()]

CHAT_IDS       = [c.strip() for c in TELEGRAM_CHAT_ID.split(",") if c.strip()] if TELEGRAM_CHAT_ID else []
PUMP_WS_URL    = "wss://pumpportal.fun/api/data"
X_MILESTONES   = [2, 5, 10, 25, 50, 100]

DATA_DIR = Path(os.getenv("SNIPER_DATA_DIR", "./data"))
DATA_DIR.mkdir(exist_ok=True)
LEADERBOARD_FILE = DATA_DIR / "leaderboard.json"

tracked_lock = asyncio.Lock()
history_lock = asyncio.Lock()
alerts_lock  = asyncio.Lock()

def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ═══════════════════════════════════════════════════════════════════════════════
# NARRATIVE ENGINE (built-in, broad categories)
# ═══════════════════════════════════════════════════════════════════════════════
NARRATIVE_CATEGORIES = {
    "ai_tech": [
        "ai", "gpt", "chatgpt", "openai", "claude", "clawd", "anthropic",
        "gemini", "llm", "agi", "sora", "deepseek", "qwen", "mistral",
        "groq", "grok", "perplexity", "agent", "aixbt", "nvidia",
        "sam altman", "bytedance", "meta ai", "copilot", "midjourney",
        "stable diffusion", "transformer", "neural", "machine learning",
    ],
    "animals": [
        # Dogs
        "dog", "doge", "shiba", "shib", "corgi", "poodle", "bulldog",
        "retriever", "husky", "puppy", "pupper", "doggo", "chihuahua",
        "labrador", "beagle", "pitbull", "rottweiler", "dalmatian",
        "pug", "mutt", "hound", "terrier", "spaniel", "collie",
        "shepherd", "dane", "mastiff", "akita", "malinois", "borzoi",
        # Cats
        "cat", "kitten", "kitty", "meow", "tabby", "calico", "persian",
        "siamese", "maine coon", "sphynx", "ragdoll", "bengal",
        # Frogs / Reptiles
        "frog", "pepe", "toad", "gecko", "lizard", "chameleon",
        "iguana", "snake", "python", "cobra", "viper", "dragon",
        "komodo", "salamander", "newt", "turtle", "tortoise",
        "crocodile", "croc", "alligator", "gator", "raptor",
        # Primates
        "monkey", "ape", "gorilla", "chimp", "chimpanzee", "orangutan",
        "baboon", "lemur", "macaque", "primate", "mandrill",
        # Marine
        "whale", "dolphin", "shark", "fish", "octopus", "squid",
        "jellyfish", "seahorse", "starfish", "lobster", "lobstar",
        "crab", "shrimp", "clam", "oyster", "seal", "walrus",
        "orca", "narwhal", "manta", "ray", "barracuda", "tuna",
        "salmon", "cod", "bass", "trout", "koi", "goldfish",
        "pufferfish", "blowfish", "clownfish", "nemo", "coral",
        # Birds
        "bird", "eagle", "hawk", "falcon", "owl", "parrot",
        "penguin", "flamingo", "pelican", "crow", "raven",
        "robin", "sparrow", "pigeon", "dove", "hummingbird",
        "toucan", "peacock", "swan", "goose", "duck", "chicken",
        "rooster", "hen", "turkey", "ostrich", "emu", "kiwi",
        "vulture", "condor", "stork", "crane", "heron", "albatross",
        "woodpecker", "kingfisher", "canary", "finch", "cardinal",
        # Big cats / predators
        "lion", "tiger", "leopard", "jaguar", "cheetah", "panther",
        "cougar", "puma", "lynx", "bobcat", "ocelot", "caracal",
        # Bears
        "bear", "grizzly", "polar bear", "panda", "koala",
        # Farm
        "horse", "pony", "stallion", "mare", "donkey", "mule",
        "cow", "bull", "ox", "bison", "buffalo", "calf",
        "pig", "hog", "boar", "piglet", "sow",
        "sheep", "lamb", "ram", "goat", "llama", "alpaca",
        # African
        "elephant", "rhino", "rhinoceros", "hippo", "hippopotamus",
        "giraffe", "zebra", "hyena", "jackal", "warthog",
        "meerkat", "gazelle", "antelope", "wildebeest", "gnu",
        "impala", "springbok", "kudu", "oryx", "eland",
        # Small mammals
        "rabbit", "bunny", "hare", "hamster", "guinea pig",
        "mouse", "rat", "squirrel", "chipmunk", "beaver",
        "otter", "ferret", "weasel", "badger", "skunk",
        "raccoon", "possum", "opossum", "hedgehog", "porcupine",
        "armadillo", "mole", "bat", "fox", "wolf",
        "coyote", "dingo", "jackal", "wolverine",
        # Insects / Bugs
        "butterfly", "bee", "wasp", "ant", "beetle",
        "ladybug", "dragonfly", "firefly", "cricket", "grasshopper",
        "moth", "caterpillar", "spider", "scorpion", "centipede",
        "mantis", "cockroach", "mosquito", "fly", "worm",
        # Other
        "deer", "elk", "moose", "caribou", "reindeer",
        "camel", "yak", "sloth", "pangolin", "tapir",
        "capybara", "manatee", "platypus", "echidna", "anteater",
        "aardvark", "binturong", "civet", "mongoose", "mink",
        # Mythical / Meme animals
        "unicorn", "phoenix", "griffin", "pegasus", "hydra",
        "kraken", "leviathan", "basilisk", "cerberus", "minotaur",
        "dino", "dinosaur", "trex", "brontosaurus", "velociraptor",
        # Meme specific
        "harambe", "nyan", "grumpy cat", "cheems", "bonk",
        "claw", "openclaw", "molty",
    ],
    "culture_meme": [
        "gigachad", "wojak", "npc", "sigma", "based", "chad", "giga",
        "brainrot", "skibidi", "rizz", "goat", "ratio", "delulu", "slay",
        "harambe", "shrek", "ugandan", "dat boi", "nyan", "grumpy",
        "rickroll", "bogdanoff", "bobo", "sminem", "apu", "alpha",
        "cheems", "bonk", "stonks", "dank", "meme", "pepe",
        "copium", "hopium", "fud", "hodl", "wagmi", "ngmi",
        "wen", "ser", "gm", "frens", "anon", "degen",
        "rug", "yolo", "diamond hands", "paper hands", "moon",
        "tendies", "apes", "retard", "cult",
    ],
    "politics_geo": [
        "trump", "maga", "biden", "whitehouse", "election", "vote",
        "wlfi", "elon", "musk",
        "china", "chinese", "xi", "ccp", "taiwan", "beijing",
        "iran", "russia", "putin", "ukraine", "war", "sanction",
        "tariff", "executive order", "congress", "senate",
    ],
    "sports": [
        "nba", "nfl", "fifa", "ufc", "mma", "boxing", "f1",
        "messi", "ronaldo", "lebron", "curry", "kobe",
        "superbowl", "worldcup", "champions league",
        "ferrari", "tennis", "golf", "olympic", "basketball",
        "football", "soccer", "baseball", "hockey", "cricket",
    ],
    "gaming_virtual": [
        "minecraft", "roblox", "fortnite", "gta", "gta6",
        "pokemon", "zelda", "mario", "sonic", "call of duty",
        "apex", "valorant", "league", "dota", "csgo",
        "twitch", "streaming", "esport", "speedrun",
        "metaverse", "vr", "vtuber", "anime",
    ],
    "finance_macro": [
        "bitcoin", "btc", "eth", "ethereum", "solana", "sol",
        "fed", "inflation", "recession", "rate cut", "rate hike",
        "bull", "halving", "etf", "blackrock",
        "vitalik", "cz", "binance", "coinbase",
        "crypto", "defi", "yield", "staking",
    ],
}

# Pre-compile word boundary patterns for each keyword
_KW_PATTERNS: dict[str, tuple[str, re.Pattern]] = {}

def _build_patterns():
    for cat, keywords in NARRATIVE_CATEGORIES.items():
        for kw in keywords:
            escaped = re.escape(kw)
            _KW_PATTERNS[kw] = (cat, re.compile(r'\b' + escaped + r'\b', re.IGNORECASE))

_build_patterns()


def match_narrative(name: str, symbol: str, description: str = "") -> dict:
    combined = f"{name} {symbol} {description}".lower()
    
    best_kw = None
    best_cat = None
    best_confidence = 0.0
    
    for kw, (cat, pattern) in _KW_PATTERNS.items():
        if pattern.search(combined):
            if pattern.search(name.lower()) or pattern.search(symbol.lower()):
                conf = 0.95
            else:
                conf = 0.65
            
            specificity = len(kw) * conf
            if specificity > (len(best_kw) * best_confidence if best_kw else 0):
                best_kw = kw
                best_cat = cat
                best_confidence = conf
    
    if best_kw:
        return {
            "matched": True,
            "keyword": best_kw.upper(),
            "category": best_cat,
            "confidence": best_confidence,
        }
    
    return {"matched": False, "keyword": None, "category": None, "confidence": 0.0}


# ═══════════════════════════════════════════════════════════════════════════════
# SIMPLE SCORER — designed for pump.fun bonding curve tokens
# ═══════════════════════════════════════════════════════════════════════════════
def score_token(token: dict, narrative: dict) -> dict:
    scores = {}
    signals = []
    warnings = []
    
    # ── Narrative (15%) — bonus, not required ──────────────────────────────
    if narrative["matched"]:
        base = 7.0
        if narrative["confidence"] >= 0.9:
            base = 8.5
        scores["narrative"] = base
        signals.append(f"Narrative: {narrative['keyword']} [{narrative['category']}]")
    else:
        scores["narrative"] = 5.0  # Neutral — no penalty for missing narrative
    
    # ── Momentum (40%) — the real signal ─────────────────────────────────────
    vol_1h = token.get("volume_1h_usd", 0)
    buy_ratio = token.get("buy_sell_ratio_1h", 1.0)
    mcap = token.get("mcap_usd", 0)
    
    mom = 5.0
    if vol_1h > 100_000:    mom += 2.5
    elif vol_1h > 50_000:   mom += 2.0
    elif vol_1h > 10_000:   mom += 1.0
    elif vol_1h > 5_000:    mom += 0.5
    elif vol_1h < 500:      mom -= 1.0
    
    if buy_ratio > 3.0:     mom += 1.5
    elif buy_ratio > 2.0:   mom += 1.0
    elif buy_ratio > 1.5:   mom += 0.5
    elif buy_ratio < 0.5:   mom -= 2.0
    
    # MCap traction — on bonding curve, higher mcap = real buying pressure
    if mcap > 30_000:       mom += 2.0
    elif mcap > 15_000:     mom += 1.5
    elif mcap > 10_000:     mom += 1.0
    elif mcap > 5_000:      mom += 0.5
    
    # Holder count — organic interest signal
    holders = token.get("total_holders", 0)
    if holders > 100:       mom += 1.0
    elif holders > 50:      mom += 0.5
    
    scores["momentum"] = max(min(mom, 10.0), 1.0)
    
    if vol_1h > 10_000:
        signals.append(f"Volume: ${vol_1h:,.0f}/1h")
    if buy_ratio > 2.0:
        signals.append(f"Buy pressure: {buy_ratio:.1f}x")
    if mcap > 10_000:
        signals.append(f"MCap traction: ${mcap:,.0f}")
    if vol_1h < 1_000:
        warnings.append(f"Low volume: ${vol_1h:,.0f}/1h")
    
    # ── Timing (20%) ─────────────────────────────────────────────────────────
    age_h = token.get("age_hours", 0.5)
    if age_h < 0.1:         tim = 6.0
    elif age_h < 0.5:       tim = 8.5
    elif age_h < 2:         tim = 7.5
    elif age_h < 6:         tim = 5.0
    elif age_h < 24:        tim = 3.0
    else:                   tim = 2.0
    
    scores["timing"] = tim
    
    if 0.1 <= age_h <= 2:
        signals.append(f"Sweet spot timing ({age_h:.1f}h)")
    if age_h > 12:
        warnings.append(f"Late entry: {age_h:.0f}h old")
    
    # ── Safety (20%) ─────────────────────────────────────────────────────────
    dev_pct = token.get("dev_holds_pct", 0)
    mint_revoked = token.get("mint_authority_revoked", True)
    freeze_revoked = token.get("freeze_authority_revoked", True)
    
    safe = 6.0
    
    if dev_pct > 5.0:       safe -= 2.0
    elif dev_pct > 2.0:     safe -= 0.5
    elif dev_pct <= 0.5:    safe += 2.0   # Dev holds almost nothing — great
    elif dev_pct <= 1.0:    safe += 1.0
    
    liq = token.get("liquidity_usd", 0)
    if liq > 10_000:
        if not mint_revoked:    safe -= 2.0
        if not freeze_revoked:  safe -= 1.5
    
    scores["safety"] = max(min(safe, 10.0), 1.0)
    
    if dev_pct > 3.0:
        warnings.append(f"Dev holds: {dev_pct:.1f}%")
    if dev_pct <= 1.0 and dev_pct >= 0:
        signals.append(f"Dev holds {dev_pct:.1f}%")
    
    # ── Final Score ──────────────────────────────────────────────────────────
    weights = {"narrative": 0.15, "momentum": 0.40, "timing": 0.20, "safety": 0.25}
    final = sum(scores[k] * weights[k] for k in weights)
    final = round(max(min(final, 10.0), 1.0), 2)
    
    if final >= 7.5:    verdict = "STRONG ENTRY"
    elif final >= 6.0:  verdict = "GOOD ENTRY"
    elif final >= 5.0:  verdict = "WEAK ENTRY"
    elif final >= 3.5:  verdict = "HIGH RISK"
    else:               verdict = "AVOID"
    
    return {
        "final_score": final,
        "verdict": verdict,
        "components": scores,
        "signals": signals[:5],
        "warnings": warnings[:5],
    }


# ═══════════════════════════════════════════════════════════════════════════════
# TRACKED TOKEN
# ═══════════════════════════════════════════════════════════════════════════════
class TrackedToken:
    def __init__(self, mint, name, symbol, entry_mcap, entry_score, narrative):
        self.mint               = mint
        self.name               = name
        self.symbol             = symbol
        self.entry_mcap         = entry_mcap
        self.entry_score        = entry_score
        self.narrative          = narrative
        self.peak_mcap          = entry_mcap
        self.current_mcap       = entry_mcap
        self.peak_x             = 1.0
        self.alerted_xs         = set()
        self.migrated           = False
        self.migration_verified = False
        self.added_at           = utcnow()
        self.status             = "active"
        self.last_updated       = utcnow()
        self.lifecycle_data     = {}   # Filled by lifecycle_tracker
        self.has_socials        = False

    def current_x(self):
        return min(round(self.current_mcap / max(self.entry_mcap, 1000), 2), 500)
    
    def classify_outcome(self):
        if self.status == "active":
            return "active"
        if self.peak_x >= 5.0:       return "success"
        elif self.peak_x >= 2.0:     return "moderate"
        elif self.peak_x < 0.5:      return "rugged"
        else:                         return "no_pump"

    def to_record(self):
        return {
            "mint": self.mint, "name": self.name, "symbol": self.symbol,
            "entry_mcap": self.entry_mcap, "entry_score": self.entry_score,
            "narrative": self.narrative, "peak_mcap": self.peak_mcap,
            "current_mcap": self.current_mcap, "peak_x": self.peak_x,
            "current_x": self.current_x(), "migrated": self.migrated,
            "migration_verified": self.migration_verified, "status": self.status,
            "added_at": self.added_at.isoformat(),
            "last_updated": self.last_updated.isoformat(),
            "lifecycle": self.lifecycle_data,
            "has_socials": self.has_socials,
        }


# ═══════════════════════════════════════════════════════════════════════════════
# GLOBAL STATE
# ═══════════════════════════════════════════════════════════════════════════════
tracked: Dict[str, TrackedToken] = {}
leaderboard_history: List[dict] = []
bot_start_time: datetime = utcnow()
total_alerts_fired: int = 0
active_narratives: dict = {}


# ═══════════════════════════════════════════════════════════════════════════════
# TRENDING BURST DETECTION
# ═══════════════════════════════════════════════════════════════════════════════
BURST_WINDOW = 300          # 5 minutes to detect burst
BURST_MIN_TOKENS = 3        # Need 3+ similar tokens to detect a burst
BURST_ALERTED: Dict[str, float] = {}  # theme -> timestamp of last alert (avoid spam)
BURST_EVAL_DELAY = 600      # 10 minutes — wait for candidates before picking winner

# Burst candidates: theme -> list of {mint, name, symbol, token, score, narrative, ...}
_burst_candidates: Dict[str, list] = {}
_burst_evaluating: Dict[str, bool] = {}  # theme -> True if evaluator already started

# Recent tokens: list of (timestamp, name, symbol, mint, keywords)
_recent_tokens: list = []
_recent_lock = asyncio.Lock()

# Common words to ignore when extracting keywords
_STOP_WORDS = {
    "the", "a", "an", "of", "for", "to", "in", "on", "is", "it", "my",
    "and", "or", "but", "not", "no", "its", "new", "buy", "just", "your",
    "this", "that", "with", "from", "are", "was", "has", "had", "get",
    "got", "can", "will", "all", "one", "two", "you", "we", "he", "she",
    "token", "coin", "sol", "solana", "pump", "fun", "pumpfun", "ca",
    "bio", "check", "website", "hello", "look", "here", "bro", "sir",
    "please", "lol", "lmao", "bruh", "wtf", "omg", "dev", "based",
}

def extract_theme_keywords(name: str, symbol: str) -> set:
    """Extract meaningful keywords from a token name/symbol for burst matching."""
    words = set()
    combined = f"{name} {symbol}".lower()
    # Split on non-alpha characters
    for word in re.split(r'[^a-zA-Z]+', combined):
        word = word.strip().lower()
        if len(word) >= 3 and word not in _STOP_WORDS:
            words.add(word)
    return words

def find_burst_theme(keywords: set) -> tuple:
    """Check if these keywords match a current burst. Returns (theme, count) or (None, 0)."""
    now = utcnow()
    cutoff = now - timedelta(seconds=BURST_WINDOW)
    
    # Count how many recent tokens share keywords with this one
    best_theme = None
    best_count = 0
    
    # Group recent tokens by shared keywords
    keyword_counts = defaultdict(int)
    for ts, _, _, _, kws in _recent_tokens:
        if ts < cutoff:
            continue
        shared = keywords & kws
        for kw in shared:
            keyword_counts[kw] += 1
    
    # Find the most common shared keyword
    for kw, count in keyword_counts.items():
        if count >= BURST_MIN_TOKENS - 1 and count > best_count:  # -1 because current token not yet added
            best_count = count + 1  # Include current token
            best_theme = kw
    
    return (best_theme, best_count) if best_theme else (None, 0)

async def register_token_for_burst(name: str, symbol: str, mint: str):
    """Register a token in the recent tokens list for burst detection."""
    keywords = extract_theme_keywords(name, symbol)
    if not keywords:
        return
    
    now = utcnow()
    async with _recent_lock:
        _recent_tokens.append((now, name, symbol, mint, keywords))
        # Cleanup old entries
        cutoff = now - timedelta(seconds=BURST_WINDOW * 2)
        while _recent_tokens and _recent_tokens[0][0] < cutoff:
            _recent_tokens.pop(0)


async def burst_evaluator(theme: str, count: int):
    """Wait 10 minutes, then pick the best candidate from a trending burst."""
    global total_alerts_fired
    try:
        log.info(f"[BURST] Evaluating '{theme}' — waiting {BURST_EVAL_DELAY}s for candidates...")
        await asyncio.sleep(BURST_EVAL_DELAY)
        
        candidates = _burst_candidates.get(theme, [])
        if not candidates:
            log.info(f"[BURST] '{theme}' — no candidates after wait")
            return
        
        log.info(f"[BURST] '{theme}' — evaluating {len(candidates)} candidates")
        
        # Re-check each candidate on DexScreener for current data
        best = None
        best_score = -1
        
        for cand in candidates:
            try:
                mint = cand["mint"]
                dex = await fetch_dexscreener(mint)
                
                current_mcap = dex.get("mcap_usd", 0)
                current_liq = dex.get("liquidity_usd", 0)
                current_vol = dex.get("volume_1h_usd", 0)
                dex_paid = dex.get("dex_paid", False)
                boosts = dex.get("boosts", 0)
                buy_ratio = dex.get("buy_sell_ratio_1h", 1.0)
                price_change = dex.get("price_change_1h_pct", 0)
                
                # Skip dead tokens
                if current_mcap < 3000 and current_liq < 1000:
                    log.info(f"  [BURST] {cand['symbol']} — dead (mcap=${current_mcap:,.0f})")
                    continue
                
                # Score candidates — organic growth matters most when multiple have dex_paid
                eval_score = 0
                
                # Tier 1: Dex paid + boosts (entry ticket)
                if dex_paid:
                    eval_score += 30
                if boosts > 0:
                    eval_score += min(boosts * 5, 20)
                
                # Tier 2: Real liquidity (migrated to Raydium = graduated)
                if current_liq > 0:
                    eval_score += 25
                    eval_score += min(current_liq / 1000, 25)  # More liq = better
                
                # Tier 3: MCap traction (THE key differentiator)
                eval_score += min(current_mcap / 500, 60)  # Up to 60 points for $30k+
                
                # Tier 4: Volume relative to mcap (organic trading activity)
                if current_mcap > 0:
                    vol_mcap_ratio = current_vol / current_mcap
                    eval_score += min(vol_mcap_ratio * 20, 40)  # High vol/mcap = active
                
                # Tier 5: Buy pressure (still being bought = organic)
                if buy_ratio > 2.0:
                    eval_score += 20
                elif buy_ratio > 1.5:
                    eval_score += 10
                elif buy_ratio < 0.8:
                    eval_score -= 15  # Being dumped
                
                # Tier 6: Price trend (positive = momentum)
                if price_change > 50:
                    eval_score += 15
                elif price_change > 20:
                    eval_score += 10
                elif price_change < -30:
                    eval_score -= 20  # Crashing
                
                # Tier 7: MCap growth from entry
                entry_mcap_cand = cand.get("token", {}).get("mcap_usd", 5000)
                if entry_mcap_cand > 0 and current_mcap > entry_mcap_cand:
                    growth = current_mcap / entry_mcap_cand
                    eval_score += min(growth * 10, 30)  # Growing from entry
                
                log.info(f"  [BURST] {cand['symbol']} — mcap=${current_mcap:,.0f} liq=${current_liq:,.0f} vol=${current_vol:,.0f} b/s={buy_ratio:.1f} dex={dex_paid} boosts={boosts} → eval={eval_score:.0f}")
                
                # Update candidate with fresh data
                cand["current_mcap"] = current_mcap
                cand["current_liq"] = current_liq
                cand["current_vol"] = current_vol
                cand["dex_paid"] = dex_paid
                cand["boosts"] = boosts
                cand["buy_ratio"] = buy_ratio
                cand["price_change"] = price_change
                cand["eval_score"] = eval_score
                
                if eval_score > best_score:
                    best_score = eval_score
                    best = cand
                
                await asyncio.sleep(0.3)  # Rate limit
            except Exception as e:
                log.warning(f"  [BURST] Eval error for {cand.get('symbol','?')}: {e}")
        
        if not best:
            log.info(f"[BURST] '{theme}' — no viable candidates after evaluation")
            return
        
        # Build and send trending alert for the winner
        token_data = best.get("token", {})
        token_data["mcap_usd"] = best.get("current_mcap", token_data.get("mcap_usd", 0))
        token_data["liquidity_usd"] = best.get("current_liq", token_data.get("liquidity_usd", 0))
        token_data["volume_1h_usd"] = best.get("current_vol", token_data.get("volume_1h_usd", 0))
        
        # Add dex_paid info
        dex_paid = best.get("dex_paid", False)
        boosts = best.get("boosts", 0)
        
        result = best.get("result", {})
        narrative = best.get("narrative", {})
        mint = best["mint"]
        name = best["name"]
        symbol = best["symbol"]
        
        async with alerts_lock:
            global total_alerts_fired
            total_alerts_fired += 1
        
        # Format special trending winner alert
        lines = [
            "🔥🔥 <b>TRENDING WINNER — evaluated & confirmed</b> 🔥🔥", "",
            f"📡 <b>{len(candidates)} tokens with '{theme}' — THIS ONE LEADS</b>",
        ]
        
        if dex_paid:
            lines.append("💎 <b>DEX PAID — profile active</b>")
        if boosts > 0:
            lines.append(f"🚀 <b>{boosts} DexScreener boosts</b>")
        if best.get("current_liq", 0) > 0:
            lines.append("🎓 <b>MIGRATED — has real liquidity</b>")
        
        is_cult = "cult" in f"{name} {symbol}".lower()
        if is_cult:
            lines.append("🔥 <b>CULT TOKEN</b>")
        
        lines += [
            "",
            f"<b>{name}</b>  <code>${symbol}</code>",
            f"<code>{mint}</code>", "",
            f"📊 <b>SCORE: {result.get('final_score', 0)}/10</b>  {result.get('verdict', '')}",
            "",
        ]
        
        if narrative.get("matched"):
            lines.append(f"📡 Narrative:  <b>{narrative.get('keyword','')}</b>  [{narrative.get('category','')}]")
        
        lines += [
            f"💰 MCap:       <b>${best.get('current_mcap', 0):,.0f}</b>",
            f"💧 Liquidity:  <b>${best.get('current_liq', 0):,.0f}</b>",
            f"📈 Vol 1h:     <b>${best.get('current_vol', 0):,.0f}</b>",
            f"📊 Buy/Sell:   <b>{best.get('buy_ratio', 1.0):.1f}</b>",
            f"👨‍💻 Dev holds:  <b>{token_data.get('dev_holds_pct', 0):.1f}%</b>",
            f"🏆 Eval score: <b>{best.get('eval_score', 0):.0f}</b>",
            "",
            f"🔗 <a href='https://pump.fun/{mint}'>pump.fun</a>  "
            f"<a href='https://dexscreener.com/solana/{mint}'>dexscreener</a>  "
            f"<a href='https://gmgn.ai/sol/token/{mint}'>gmgn</a>  "
            f"<a href='https://solscan.io/token/{mint}'>solscan</a>",
        ]
        
        # Socials
        socials = token_data.get("socials", {})
        social_lines = []
        if socials.get("twitter"):
            tw = socials["twitter"]
            if not tw.startswith("http"): tw = f"https://x.com/{tw}"
            social_lines.append(f"🐦 <a href='{tw}'>X / Twitter</a>")
        if socials.get("telegram"):
            tg = socials["telegram"]
            if not tg.startswith("http"): tg = f"https://t.me/{tg}"
            social_lines.append(f"💬 <a href='{tg}'>Telegram</a>")
        if socials.get("website"):
            ws = socials["website"]
            if not ws.startswith("http"): ws = f"https://{ws}"
            social_lines.append(f"🌐 <a href='{ws}'>Website</a>")
        if social_lines:
            lines.append("  ".join(social_lines))
        
        # Show other candidates that lost
        losers = [c for c in candidates if c["mint"] != mint]
        if losers:
            lines.append("")
            lines.append(f"<i>Rejected {len(losers)} others:</i>")
            for l in sorted(losers, key=lambda x: x.get("eval_score", 0), reverse=True)[:3]:
                l_mcap = l.get("current_mcap", 0)
                l_paid = "💎dex" if l.get("dex_paid") else ""
                l_liq = "🎓migrated" if l.get("current_liq", 0) > 0 else ""
                l_br = l.get("buy_ratio", 0)
                l_score = l.get("eval_score", 0)
                lines.append(f"  <i>{l.get('name','?')} — ${l_mcap:,.0f} b/s:{l_br:.1f} {l_paid} {l_liq} (eval:{l_score:.0f})</i>")
        
        lines.append(f"<i>🕐 {utcnow().strftime('%H:%M:%S UTC')}</i>")
        
        await send_tg("\n".join(lines))
        log.info(f"[BURST] ✅ Winner: {name} (${symbol}) — mcap=${best.get('current_mcap',0):,.0f} dex_paid={dex_paid}")
        
        # Track the winner
        entry_mcap = max(best.get("current_mcap", MIN_MCAP), MIN_MCAP)
        async with tracked_lock:
            t = TrackedToken(
                mint=mint, name=name, symbol=symbol,
                entry_mcap=entry_mcap, entry_score=result.get("final_score", 0),
                narrative=narrative.get("keyword", "?"),
            )
            t.has_socials = bool(socials.get("twitter") or socials.get("telegram") or socials.get("website"))
            tracked[mint] = t
        asyncio.create_task(save_leaderboard())
        
        # Start lifecycle for winner
        deployer = best.get("deployer", "")
        desc = best.get("desc", "")
        asyncio.create_task(lifecycle_tracker(mint, name, symbol, deployer, desc, narrative, entry_mcap, result.get("final_score", 0)))
        
    except Exception as e:
        log.error(f"[BURST] Evaluator error for '{theme}': {e}")
    finally:
        # Cleanup
        _burst_candidates.pop(theme, None)
        _burst_evaluating.pop(theme, None)


# ═══════════════════════════════════════════════════════════════════════════════
# PERSISTENCE
# ═══════════════════════════════════════════════════════════════════════════════
async def save_leaderboard():
    try:
        async with tracked_lock, history_lock:
            records = [t.to_record() for t in tracked.values()] + leaderboard_history
            if len(records) > 5000:
                records = records[-2500:]
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None, lambda: LEADERBOARD_FILE.write_text(json.dumps(records, indent=2)))
    except Exception as e:
        log.error(f"[LB] Save error: {e}")

def load_leaderboard():
    global leaderboard_history
    try:
        if LEADERBOARD_FILE.exists():
            with open(LEADERBOARD_FILE) as f:
                data = json.load(f)
            cutoff = utcnow() - timedelta(days=90)
            leaderboard_history = [
                r for r in (data if isinstance(data, list) else [])
                if datetime.fromisoformat(r.get("added_at", "2000-01-01")) > cutoff
            ][-5000:]
            log.info(f"[LB] Loaded {len(leaderboard_history)} records")
    except Exception as e:
        log.error(f"[LB] Load error: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# TELEGRAM
# ═══════════════════════════════════════════════════════════════════════════════
async def send_tg(text: str, chat_id: str = None) -> int:
    if not TELEGRAM_BOT_TOKEN:
        print(text); return 0
    
    targets = [chat_id] if chat_id else CHAT_IDS
    if not targets: return 0
    
    last_id = 0
    for cid in targets:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                    json={"chat_id": cid, "text": text, "parse_mode": "HTML",
                          "disable_web_page_preview": True})
                resp.raise_for_status()
                last_id = resp.json().get("result", {}).get("message_id", 0)
        except Exception as e:
            log.error(f"[TG] Send failed to {cid}: {e}")
    return last_id

async def delete_webhook():
    if not TELEGRAM_BOT_TOKEN: return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/deleteWebhook",
                json={"drop_pending_updates": True})
        log.info("[TG] Webhook deleted")
    except Exception as e:
        log.warning(f"[TG] Webhook delete failed: {e}")

async def get_updates(offset: int = 0) -> list:
    if not TELEGRAM_BOT_TOKEN: return []
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates",
                params={"offset": offset, "timeout": 10, "allowed_updates": '["message"]'})
            if resp.status_code == 200:
                return resp.json().get("result", [])
            if resp.status_code == 409:
                log.warning("[TG] 409 Conflict - webhook still active?")
    except Exception:
        pass
    return []


# ═══════════════════════════════════════════════════════════════════════════════
# DATA FETCHERS
# ═══════════════════════════════════════════════════════════════════════════════
async def fetch_dexscreener(mint: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.get(
                f"https://api.dexscreener.com/latest/dex/tokens/{mint}",
                headers={"User-Agent": "Mozilla/5.0"})
            if resp.status_code != 200: return {}
            pairs = resp.json().get("pairs") or []
            if not pairs: return {}
            pair = max(pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0))
            
            # Check for DexScreener paid/boosted indicators
            boosts = pair.get("boosts", 0) or 0
            has_profile = bool(pair.get("profile") or pair.get("header") or pair.get("links"))
            info = pair.get("info") or {}
            has_dex_paid = bool(info.get("imageUrl") or info.get("websites") or info.get("socials") or boosts > 0 or has_profile)
            
            return {
                "liquidity_usd":       float((pair.get("liquidity") or {}).get("usd") or 0),
                "mcap_usd":            float(pair.get("marketCap") or pair.get("fdv") or 0),
                "volume_1h_usd":       float((pair.get("volume") or {}).get("h1") or 0),
                "volume_5m_usd":       float((pair.get("volume") or {}).get("m5") or 0),
                "price_change_1h_pct": float((pair.get("priceChange") or {}).get("h1") or 0),
                "buy_sell_ratio_1h":   int((pair.get("txns") or {}).get("h1", {}).get("buys") or 1) /
                                       max(int((pair.get("txns") or {}).get("h1", {}).get("sells") or 1), 1),
                "total_holders":       int(pair.get("holders", 50)),
                "dex_paid":            has_dex_paid,
                "boosts":              int(boosts),
            }
    except Exception as e:
        log.warning(f"[DexScreener] {mint[:12]}: {e}")
    return {}


async def enrich_token(mint: str, name: str, symbol: str, deployer: str) -> Tuple[dict, str]:
    base = {
        "mint": mint, "name": name, "symbol": symbol, "deployer": deployer,
        "age_hours": 0.5, "mcap_usd": 0, "liquidity_usd": 0,
        "total_holders": 50, "top1_pct": 5.0, "top10_pct": 25.0,
        "dev_holds_pct": 0.0, "mint_authority_revoked": True,
        "freeze_authority_revoked": True,
        "volume_5m_usd": 0, "volume_1h_usd": 0,
        "price_change_1h_pct": 0, "buy_sell_ratio_1h": 1.0,
    }
    source = "none"

    # Helius: mint/freeze + dev wallet + top holders
    if HELIUS_API_KEY:
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                resp = await client.post(
                    f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}",
                    json={"jsonrpc": "2.0", "id": 1, "method": "getAccountInfo",
                          "params": [mint, {"encoding": "jsonParsed"}]})
                if resp.status_code == 200:
                    info = ((resp.json().get("result") or {}).get("value") or {})
                    info = ((info.get("data") or {}).get("parsed") or {}).get("info") or {}
                    if info:
                        base["mint_authority_revoked"] = info.get("mintAuthority") is None
                        base["freeze_authority_revoked"] = info.get("freezeAuthority") is None
                        supply = float(info.get("supply", 0)) / (10 ** info.get("decimals", 0))
                        decimals = info.get("decimals", 0)
                        
                        # Dev wallet check
                        if supply > 0 and deployer:
                            bal_resp = await client.post(
                                f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}",
                                json={"jsonrpc": "2.0", "id": 2, "method": "getTokenAccountsByOwner",
                                      "params": [deployer, {"mint": mint}, {"encoding": "jsonParsed"}]})
                            if bal_resp.status_code == 200:
                                accs = bal_resp.json().get("result", {}).get("value", [])
                                if accs:
                                    amt = accs[0].get("account", {}).get("data", {}).get("parsed", {}).get("info", {}).get("tokenAmount", {})
                                    dev_bal = float(amt.get("uiAmount", 0))
                                    base["dev_holds_pct"] = (dev_bal / supply) * 100 if supply > 0 else 0
                                    log.info(f"  -> Dev: {dev_bal:,.0f} / {supply:,.0f} ({base['dev_holds_pct']:.1f}%)")
                                else:
                                    log.info(f"  -> Dev: 0 tokens")
                        
                        # Top holders check (real on-chain data)
                        if supply > 0:
                            try:
                                top_resp = await client.post(
                                    f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}",
                                    json={"jsonrpc": "2.0", "id": 3, "method": "getTokenLargestAccounts",
                                          "params": [mint]})
                                if top_resp.status_code == 200:
                                    accounts = top_resp.json().get("result", {}).get("value", [])
                                    if accounts:
                                        amounts = []
                                        for acc in accounts[:20]:
                                            amt_str = acc.get("amount", "0")
                                            ui_amt = float(amt_str) / (10 ** decimals) if decimals > 0 else float(amt_str)
                                            amounts.append(ui_amt)
                                        
                                        if amounts and supply > 0:
                                            # Check if largest holder is bonding curve (>40% of supply)
                                            # On BC tokens, the AMM pool holds unsold tokens
                                            top1_raw_pct = (amounts[0] / supply) * 100
                                            
                                            if top1_raw_pct > 40:
                                                # Likely bonding curve — exclude it from stats
                                                real_amounts = amounts[1:]  # Skip BC contract
                                                if real_amounts:
                                                    top1_pct = (real_amounts[0] / supply) * 100
                                                    top10_sum = sum(real_amounts[:10])
                                                    top10_pct = (top10_sum / supply) * 100
                                                else:
                                                    top1_pct = 0.0
                                                    top10_pct = 0.0
                                                base["total_holders"] = max(len(accounts) - 1, 1)
                                                log.info(f"  -> Holders (excl BC): top1={top1_pct:.1f}% top10={top10_pct:.1f}% ({len(accounts)-1} real)")
                                            else:
                                                # No BC detected — normal calculation
                                                top1_pct = top1_raw_pct
                                                top10_sum = sum(amounts[:10])
                                                top10_pct = (top10_sum / supply) * 100
                                                base["total_holders"] = max(len(accounts), base["total_holders"])
                                                log.info(f"  -> Holders: top1={top1_pct:.1f}% top10={top10_pct:.1f}% ({len(accounts)} accounts)")
                                            
                                            base["top1_pct"] = round(top1_pct, 1)
                                            base["top10_pct"] = round(top10_pct, 1)
                            except Exception as e:
                                log.warning(f"[Helius] Top holders: {e}")
        except Exception as e:
            log.warning(f"[Helius] {e}")

    # Birdeye
    if BIRDEYE_API_KEY:
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                resp = await client.get(
                    "https://public-api.birdeye.so/defi/token_overview",
                    params={"address": mint},
                    headers={"X-API-KEY": BIRDEYE_API_KEY, "x-chain": "solana"})
                if resp.status_code == 200:
                    d = resp.json().get("data") or {}
                    liq = float(d.get("liquidity") or 0)
                    mc  = float(d.get("mc") or 0)
                    if mc > 0 or liq > 0:
                        base.update({
                            "liquidity_usd": liq, "mcap_usd": mc,
                            "volume_1h_usd": float(d.get("v1hUSD") or 0),
                            "volume_5m_usd": float(d.get("v5mUSD") or 0),
                            "price_change_1h_pct": float(d.get("priceChange1hPercent") or 0),
                            "buy_sell_ratio_1h": int(d.get("buy1h") or 1) / max(int(d.get("sell1h") or 1), 1),
                            "total_holders": int(d.get("holder") or 50),
                            "top1_pct": float(d.get("top1HolderPercent") or 5),
                            "top10_pct": float(d.get("top10HolderPercent") or 25),
                        })
                        log.info(f"  -> Birdeye: liq=${liq:,.0f} mcap=${mc:,.0f}")
                        source = "birdeye"
                elif resp.status_code == 400:
                    log.info(f"  -> Birdeye 400 — trying DexScreener...")
        except Exception as e:
            log.warning(f"[Birdeye] {e}")

    # DexScreener fallback — preserve Helius holder data
    if source == "none":
        dex = await fetch_dexscreener(mint)
        if dex.get("mcap_usd", 0) > 0 or dex.get("liquidity_usd", 0) > 0:
            # Save Helius holder data before DexScreener overwrites
            helius_holders = base.get("total_holders", 50)
            helius_top1 = base.get("top1_pct", 5.0)
            helius_top10 = base.get("top10_pct", 25.0)
            helius_dev = base.get("dev_holds_pct", 0.0)
            
            base.update(dex)
            
            # Restore Helius data (more accurate than DexScreener defaults)
            if helius_holders != 50:  # Only restore if Helius gave real data
                base["total_holders"] = helius_holders
            if helius_top1 != 5.0:
                base["top1_pct"] = helius_top1
            if helius_top10 != 25.0:
                base["top10_pct"] = helius_top10
            base["dev_holds_pct"] = helius_dev  # Always keep Helius dev data
            
            source = "dexscreener"
            log.info(f"  -> DexScreener: liq=${base['liquidity_usd']:,.0f} mcap=${base['mcap_usd']:,.0f}")

    return base, source


async def get_current_mcap(mint: str) -> Tuple[float, bool]:
    if BIRDEYE_API_KEY:
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                resp = await client.get(
                    "https://public-api.birdeye.so/defi/token_overview",
                    params={"address": mint},
                    headers={"X-API-KEY": BIRDEYE_API_KEY, "x-chain": "solana"})
                if resp.status_code == 200:
                    d = resp.json().get("data") or {}
                    mc = float(d.get("mc") or 0)
                    liq = float(d.get("liquidity") or 0)
                    if mc > 0:
                        return mc, (mc >= 65000 and liq > 10000)
        except Exception:
            pass
    try:
        dex = await fetch_dexscreener(mint)
        mc = dex.get("mcap_usd", 0)
        if mc > 0:
            return mc, (mc >= 65000 and dex.get("liquidity_usd", 0) > 10000)
    except Exception:
        pass
    return 0.0, False


# ═══════════════════════════════════════════════════════════════════════════════
# SOCIAL LINK PARSER
# ═══════════════════════════════════════════════════════════════════════════════
_URL_PATTERN = re.compile(r'https?://[^\s<>"\')\]]+', re.IGNORECASE)

async def fetch_token_socials(uri: str) -> dict:
    """Fetch IPFS metadata JSON to extract social links."""
    socials = {"twitter": "", "telegram": "", "website": ""}
    if not uri:
        return socials
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(uri, headers={"User-Agent": "Mozilla/5.0"})
            if resp.status_code == 200:
                data = resp.json()
                socials["twitter"] = str(data.get("twitter", "") or "")
                socials["telegram"] = str(data.get("telegram", "") or "")
                socials["website"] = str(data.get("website", "") or "")
                log.info(f"  -> Socials: tw={bool(socials['twitter'])} tg={bool(socials['telegram'])} web={bool(socials['website'])}")
    except Exception as e:
        log.warning(f"[SOCIALS] Fetch failed: {e}")
    return socials

def parse_socials(description: str) -> dict:
    """Extract social links from token description."""
    socials = {"twitter": None, "telegram": None, "website": None}
    if not description:
        return socials
    
    urls = _URL_PATTERN.findall(description)
    for url in urls:
        url_lower = url.lower().rstrip("/.,;:!?")
        try:
            if "twitter.com/" in url_lower or "x.com/" in url_lower:
                socials["twitter"] = url_lower
            elif "t.me/" in url_lower or "telegram" in url_lower:
                socials["telegram"] = url_lower
            elif socials["website"] is None:
                # First non-twitter/telegram URL = website
                if "pump.fun" not in url_lower and "dexscreener" not in url_lower and "solscan" not in url_lower:
                    socials["website"] = url_lower
        except Exception:
            continue
    
    return socials


# ═══════════════════════════════════════════════════════════════════════════════
# FORMATTERS
# ═══════════════════════════════════════════════════════════════════════════════
def format_alert(token: dict, score: dict, narrative: dict) -> str:
    mint = token.get("mint", "")
    comps = score["components"]
    token_name = token.get('name', '?')
    token_symbol = token.get('symbol', '?')
    
    # Detect CULT tokens — special treatment
    is_cult = "cult" in f"{token_name} {token_symbol}".lower()
    
    if is_cult:
        lines = [
            "🔥🔥🔥 <b>TekkiSniPer — CULT ALERT</b> 🔥🔥🔥", "",
            f"⚡ <b>CULT TOKEN DETECTED</b> ⚡", "",
            f"<b>{token_name}</b>  <code>${token_symbol}</code>",
            f"<code>{mint}</code>", "",
            f"📊 <b>SCORE: {score['final_score']}/10</b>  {score['verdict']}",
            f"<i>weights: narrative 15% | momentum 40% | timing 20% | safety 25%</i>", "",
        ]
    else:
        lines = [
            "🎯 <b>TekkiSniPer</b>", "",
            f"<b>{token_name}</b>  <code>${token_symbol}</code>",
            f"<code>{mint}</code>", "",
            f"📊 <b>SCORE: {score['final_score']}/10</b>  {score['verdict']}",
            f"<i>weights: narrative 15% | momentum 40% | timing 20% | safety 25%</i>", "",
        ]
    
    if narrative["matched"]:
        lines.append(f"📡 Narrative:  <b>{narrative['keyword']}</b>  [{narrative['category']}]")
    
    lines += [
        f"💰 MCap:       <b>${token.get('mcap_usd', 0):,.0f}</b>",
        f"💧 Liquidity:  <b>${token.get('liquidity_usd', 0):,.0f}</b>",
        f"👥 Holders:    <b>{token.get('total_holders', 0)}</b>",
        f"🏦 Top10:      <b>{token.get('top10_pct', 0):.1f}%</b>",
        f"📈 Vol 1h:     <b>${token.get('volume_1h_usd', 0):,.0f}</b>",
        f"👨‍💻 Dev holds:  <b>{token.get('dev_holds_pct', 0):.1f}%</b>",
        "",
        "<b>Score Breakdown</b>",
        f"  narrativ {comps.get('narrative', 0):.1f}  {'█' * int(comps.get('narrative', 0))}",
        f"  momentum {comps.get('momentum', 0):.1f}  {'█' * int(comps.get('momentum', 0))}",
        f"  timing   {comps.get('timing', 0):.1f}  {'█' * int(comps.get('timing', 0))}",
        f"  safety   {comps.get('safety', 0):.1f}  {'█' * int(comps.get('safety', 0))}",
    ]
    
    if score["signals"]:
        lines += [""] + [f"✅ {s}" for s in score["signals"]]
    if score["warnings"]:
        lines += [f"⚠️ {w}" for w in score["warnings"]]
    
    lines += [
        "",
        f"🔗 <a href='https://pump.fun/{mint}'>pump.fun</a>  "
        f"<a href='https://dexscreener.com/solana/{mint}'>dexscreener</a>  "
        f"<a href='https://gmgn.ai/sol/token/{mint}'>gmgn</a>  "
        f"<a href='https://solscan.io/token/{mint}'>solscan</a>",
    ]
    
    # Social links from pump.fun token data
    socials = token.get("socials", {})
    # Fallback: also try parsing description for URLs
    if not any(socials.values()):
        socials = parse_socials(token.get("description", ""))
    
    social_lines = []
    if socials.get("twitter"):
        tw = socials["twitter"]
        if not tw.startswith("http"):
            tw = f"https://x.com/{tw}"
        social_lines.append(f"🐦 <a href='{tw}'>X / Twitter</a>")
    if socials.get("telegram"):
        tg = socials["telegram"]
        if not tg.startswith("http"):
            tg = f"https://t.me/{tg}"
        social_lines.append(f"💬 <a href='{tg}'>Telegram</a>")
    if socials.get("website"):
        ws = socials["website"]
        if not ws.startswith("http"):
            ws = f"https://{ws}"
        social_lines.append(f"🌐 <a href='{ws}'>Website</a>")
    
    if social_lines:
        lines.append("  ".join(social_lines))
    else:
        lines.append("⚠️ <i>No socials found</i>")
    
    lines.append(f"<i>🕐 {utcnow().strftime('%H:%M:%S UTC')}</i>")
    
    return "\n".join(lines)


def format_trending_alert(token: dict, score: dict, narrative: dict, theme: str, count: int, is_first: bool) -> str:
    mint = token.get("mint", "")
    comps = score["components"]
    token_name = token.get('name', '?')
    token_symbol = token.get('symbol', '?')
    
    # Also check for cult
    is_cult = "cult" in f"{token_name} {token_symbol}".lower()
    
    if is_first:
        header = f"🔥🔥 <b>TRENDING ALERT — viral event detected</b> 🔥🔥"
    else:
        header = f"🔥 <b>TRENDING — {theme.upper()}</b>"
    
    lines = [
        header, "",
        f"📡 <b>{count} tokens with '{theme}' in last 5min — something is trending</b>",
    ]
    
    if is_first:
        lines.append("⚡ <b>FIRST MOVER WITH TRACTION</b>")
    
    if is_cult:
        lines.append("🔥 <b>CULT TOKEN</b>")
    
    lines += [
        "",
        f"<b>{token_name}</b>  <code>${token_symbol}</code>",
        f"<code>{mint}</code>", "",
        f"📊 <b>SCORE: {score['final_score']}/10</b>  {score['verdict']}",
        "",
    ]
    
    if narrative["matched"]:
        lines.append(f"📡 Narrative:  <b>{narrative['keyword']}</b>  [{narrative['category']}]")
    
    lines += [
        f"💰 MCap:       <b>${token.get('mcap_usd', 0):,.0f}</b>",
        f"💧 Liquidity:  <b>${token.get('liquidity_usd', 0):,.0f}</b>",
        f"👥 Holders:    <b>{token.get('total_holders', 0)}</b>",
        f"📈 Vol 1h:     <b>${token.get('volume_1h_usd', 0):,.0f}</b>",
        f"👨‍💻 Dev holds:  <b>{token.get('dev_holds_pct', 0):.1f}%</b>",
        "",
        f"🔗 <a href='https://pump.fun/{mint}'>pump.fun</a>  "
        f"<a href='https://dexscreener.com/solana/{mint}'>dexscreener</a>  "
        f"<a href='https://gmgn.ai/sol/token/{mint}'>gmgn</a>  "
        f"<a href='https://solscan.io/token/{mint}'>solscan</a>",
    ]
    
    # Social links
    socials = token.get("socials", {})
    if not any(socials.values()):
        socials = parse_socials(token.get("description", ""))
    social_lines = []
    if socials.get("twitter"):
        tw = socials["twitter"]
        if not tw.startswith("http"):
            tw = f"https://x.com/{tw}"
        social_lines.append(f"🐦 <a href='{tw}'>X / Twitter</a>")
    if socials.get("telegram"):
        tg = socials["telegram"]
        if not tg.startswith("http"):
            tg = f"https://t.me/{tg}"
        social_lines.append(f"💬 <a href='{tg}'>Telegram</a>")
    if socials.get("website"):
        ws = socials["website"]
        if not ws.startswith("http"):
            ws = f"https://{ws}"
        social_lines.append(f"🌐 <a href='{ws}'>Website</a>")
    if social_lines:
        lines.append("  ".join(social_lines))
    else:
        lines.append("⚠️ <i>No socials found</i>")
    
    lines.append(f"<i>🕐 {utcnow().strftime('%H:%M:%S UTC')}</i>")
    
    return "\n".join(lines)


def format_x_alert(t: TrackedToken, mcap: float, x: int) -> str:
    emoji = "🚀" if x < 10 else "🌕" if x < 50 else "💎"
    return "\n".join([
        f"{emoji} <b>{x}X ALERT</b>", "",
        f"<b>{t.name}</b> <code>${t.symbol}</code>",
        f"<code>{t.mint}</code>", "",
        f"Entry:   <b>${t.entry_mcap:,.0f}</b>",
        f"Current: <b>${mcap:,.0f}</b>",
        f"Peak:    <b>{t.peak_x:.1f}X</b> 🔥", "",
        f"🔗 <a href='https://dexscreener.com/solana/{t.mint}'>dexscreener</a>  "
        f"<a href='https://pump.fun/{t.mint}'>pump.fun</a>",
        f"<i>🕐 {utcnow().strftime('%H:%M:%S UTC')}</i>",
    ])

def format_migration(t: TrackedToken, mcap: float) -> str:
    return "\n".join([
        "🎓 <b>MIGRATION ALERT</b>", "",
        f"<b>{t.name}</b> <code>${t.symbol}</code>",
        f"<code>{t.mint}</code>", "",
        f"✅ Graduated Pump.fun → <b>Raydium</b>",
        f"MCap: <b>${mcap:,.0f}</b>  |  Entry: <b>${t.entry_mcap:,.0f}</b>  |  <b>{t.current_x()}X</b>", "",
        f"🔗 <a href='https://dexscreener.com/solana/{t.mint}'>dexscreener</a>",
        f"<i>🕐 {utcnow().strftime('%H:%M:%S UTC')}</i>",
    ])

def format_leaderboard(records: list, period: str) -> str:
    if not records:
        return f"📊 <b>{period} LEADERBOARD</b>\n\nNo alerts yet."
    sorted_r = sorted(records, key=lambda x: x.get("peak_x", 0), reverse=True)
    medals = ["🥇", "🥈", "🥉"]
    lines = [f"📊 <b>{period} LEADERBOARD</b>", f"<i>{len(records)} tokens</i>", ""]
    for i, r in enumerate(sorted_r[:10]):
        m = medals[i] if i < 3 else f"{i+1}."
        px = r.get("peak_x", 1)
        perf = "💎" if px >= 10 else "🌕" if px >= 5 else "🚀" if px >= 2 else "💀"
        lines.append(f"{m} <b>{r.get('name','?')}</b> ${r.get('symbol','?')}")
        lines.append(f"   {perf} Peak: <b>{px:.1f}X</b>  Now: {r.get('current_x',1):.1f}X  [{r.get('narrative','?').upper()}]")
        lines.append("")
    peak_xs = [r.get("peak_x", 1) for r in records]
    lines += [
        "── Stats ──",
        f"Avg peak: <b>{sum(peak_xs)/len(peak_xs):.1f}X</b>",
        f"2X+ winners: <b>{len([x for x in peak_xs if x >= 2])}/{len(records)}</b>",
        f"10X+: <b>{len([x for x in peak_xs if x >= 10])}</b>",
    ]
    return "\n".join(lines)

def format_status() -> str:
    up = utcnow() - bot_start_time
    h, m = int(up.total_seconds() // 3600), int((up.total_seconds() % 3600) // 60)
    active = len(tracked)
    total = len(leaderboard_history) + active
    peak_xs = [t.peak_x for t in tracked.values()]
    best = f"{max(peak_xs):.1f}X" if peak_xs else "none"
    kw_count = len(_KW_PATTERNS)
    return "\n".join([
        "⚡ <b>BOT STATUS [v4.0]</b>", "",
        f"🟢 Online:        <b>{h}h {m}m</b>",
        f"🎯 Alerts fired:  <b>{total_alerts_fired}</b>",
        f"👁 Tracking now:  <b>{active} tokens</b>",
        f"📊 Total tracked: <b>{total}</b>",
        f"🏆 Best live:     <b>{best}</b>",
        f"📡 Keywords:      <b>{kw_count}</b>",
        f"🎚 Threshold:     <b>{ALERT_THRESHOLD}/10</b>",
        f"💰 Min MCap:      <b>${MIN_MCAP:,.0f}</b>", "",
        f"<i>🕐 {utcnow().strftime('%H:%M:%S UTC')}</i>",
    ])

def format_narratives() -> str:
    cats = {}
    for kw, (cat, _) in _KW_PATTERNS.items():
        cats.setdefault(cat, []).append(kw)
    lines = ["📡 <b>NARRATIVE CATEGORIES</b>", ""]
    for cat, kws in sorted(cats.items()):
        lines.append(f"<b>{cat}</b>: {len(kws)} keywords")
        sample = ", ".join(kws[:8])
        lines.append(f"  <i>{sample}...</i>")
        lines.append("")
    lines.append(f"Total: <b>{len(_KW_PATTERNS)} keywords</b>")
    return "\n".join(lines)

def format_tracking() -> str:
    if not tracked:
        return "👁 <b>TRACKING</b>\n\nNo tokens being tracked."
    lines = [f"👁 <b>TRACKING ({len(tracked)} tokens)</b>", ""]
    for t in sorted(tracked.values(), key=lambda x: x.peak_x, reverse=True):
        age = int((utcnow() - t.added_at).total_seconds() / 60)
        mig = "🎓" if t.migrated else ""
        lines.append(f"<b>{t.name}</b> ${t.symbol} {mig}")
        lines.append(f"   {t.current_x():.1f}X now | Peak: {t.peak_x:.1f}X | {age}m old")
        lines.append("")
    return "\n".join(lines)

def format_help() -> str:
    return "\n".join([
        "🤖 <b>SNIPER COMMANDS</b>", "",
        "/status       — bot health",
        "/leaderboard  — 24h leaderboard",
        "/weekly       — 7 day leaderboard",
        "/monthly      — 30 day leaderboard",
        "/narratives   — keyword categories",
        "/tracking     — live tracked tokens",
        "/analytics    — full performance report",
        "/analytics24  — last 24h report",
        "/analytics7   — last 7 day report",
        "/analytics30  — last 30 day report",
        "/patterns     — win vs loss patterns",
        "/help         — this menu",
    ])


# ═══════════════════════════════════════════════════════════════════════════════
# TRACKER (monitors alerted tokens for X milestones & migration)
# ═══════════════════════════════════════════════════════════════════════════════
async def track_tokens():
    sem = asyncio.Semaphore(5)

    async def update(mint, token):
        async with sem:
            try:
                if (utcnow() - token.added_at).total_seconds() / 3600 > 24:
                    async with tracked_lock, history_lock:
                        if mint in tracked:
                            leaderboard_history.append(tracked[mint].to_record())
                            del tracked[mint]
                    asyncio.create_task(save_leaderboard())
                    log.info(f"[TRACKER] Archived {token.symbol} peak={token.peak_x:.1f}X")
                    return
                
                mcap, migrated = await get_current_mcap(mint)
                if mcap == 0: return
                
                async with tracked_lock:
                    if mint not in tracked: return
                    t = tracked[mint]
                    t.current_mcap = mcap
                    t.peak_mcap = max(t.peak_mcap, mcap)
                    t.peak_x = min(t.peak_mcap / max(t.entry_mcap, 1000), 500)  # Cap at 500X
                    t.last_updated = utcnow()
                    
                    if migrated and not t.migration_verified:
                        t.migrated = t.migration_verified = True
                        t.status = "migrated"
                        log.info(f"[TRACKER] Migration: {t.symbol}")
                        await send_tg(format_migration(t, mcap))
                    
                    if t.entry_mcap > 0:
                        mult = mcap / t.entry_mcap
                        for x in X_MILESTONES:
                            if mult >= x and x not in t.alerted_xs:
                                t.alerted_xs.add(x)
                                log.info(f"[TRACKER] {x}X: {t.symbol}")
                                await send_tg(format_x_alert(t, mcap, x))
            except Exception as e:
                log.error(f"[TRACKER] {mint[:12]}: {e}")

    while True:
        await asyncio.sleep(120)
        async with tracked_lock:
            items = list(tracked.items())
        if items:
            await asyncio.gather(*[update(m, t) for m, t in items], return_exceptions=True)


# ═══════════════════════════════════════════════════════════════════════════════
# LEADERBOARD SCHEDULER
# ═══════════════════════════════════════════════════════════════════════════════
async def leaderboard_scheduler():
    intervals = {'daily': timedelta(days=1), 'weekly': timedelta(days=7), 'monthly': timedelta(days=30)}
    last_run = {k: utcnow() for k in intervals}
    while True:
        await asyncio.sleep(60)
        now = utcnow()
        for period, delta in intervals.items():
            if (now - last_run[period]).total_seconds() >= delta.total_seconds():
                last_run[period] = now
                records = await get_records_since(now - delta)
                name = "24H" if period == 'daily' else period.upper()
                await send_tg(format_leaderboard(records, name))

async def get_records_since(cutoff) -> list:
    records = []
    async with tracked_lock:
        for t in tracked.values():
            if t.added_at >= cutoff:
                records.append(t.to_record())
    async with history_lock:
        for r in leaderboard_history:
            try:
                if datetime.fromisoformat(r["added_at"]) >= cutoff:
                    records.append(r)
            except Exception:
                continue
    return records


# ═══════════════════════════════════════════════════════════════════════════════
# COMMAND HANDLER
# ═══════════════════════════════════════════════════════════════════════════════
async def handle_commands():
    offset = 0
    last_hb = utcnow()
    log.info("[CMD] Command listener started")

    async def send_lb(cid, days):
        records = await get_records_since(utcnow() - timedelta(days=days))
        name = "24H" if days == 1 else f"{days}D"
        await send_tg(format_leaderboard(records, name), cid)

    async def send_analytics(cid, days=None):
        log.info(f"[ANALYTICS] Command received (days={days})")
        try:
            # Step 1: Snapshot data quickly
            active_list = []
            history_list = []
            try:
                async with tracked_lock:
                    active_list = list(tracked.values())
            except Exception:
                pass
            try:
                async with history_lock:
                    history_list = list(leaderboard_history)
            except Exception:
                pass

            # Step 2: Build unified list of plain dicts (no object creation)
            all_items = []
            for t in active_list:
                try:
                    all_items.append({
                        "name": str(getattr(t, 'name', '?')),
                        "peak_x": float(getattr(t, 'peak_x', 1.0)),
                        "entry_score": float(getattr(t, 'entry_score', 0)),
                        "narrative": str(getattr(t, 'narrative', '?')),
                        "added_at": getattr(t, 'added_at', utcnow()).isoformat() if hasattr(getattr(t, 'added_at', None), 'isoformat') else "",
                        "status": "active",
                    })
                except Exception:
                    pass
            for r in history_list:
                try:
                    all_items.append({
                        "name": str(r.get("name", "?")),
                        "peak_x": float(r.get("peak_x", 1.0)),
                        "entry_score": float(r.get("entry_score", 0)),
                        "narrative": str(r.get("narrative", "?")),
                        "added_at": str(r.get("added_at", "")),
                        "status": str(r.get("status", "closed")),
                    })
                except Exception:
                    continue

            # Step 3: Filter by time period
            period_label = "ALL TIME"
            if days:
                cutoff = utcnow() - timedelta(days=days)
                filtered = []
                for item in all_items:
                    try:
                        added = datetime.fromisoformat(item["added_at"])
                        if added >= cutoff:
                            filtered.append(item)
                    except Exception:
                        filtered.append(item)
                all_items = filtered
                period_label = "24H" if days == 1 else f"{days}D"

            total = len(all_items)
            log.info(f"[ANALYTICS] {total} items for {period_label}")

            if total == 0:
                await send_tg(f"📊 No data for {period_label}.", cid)
                return

            # Step 4: Classify — all in one pass, plain math only
            success = moderate = rugged = no_pump = active_count = 0
            winner_scores = []
            loser_scores = []
            b70 = [0, 0]  # [total, winners] for 7.0-7.5
            b75 = [0, 0]  # 7.5-8.0
            b80 = [0, 0]  # 8.0+
            narr_stats = {}
            best_calls = []
            worst_calls = []

            for item in all_items:
                try:
                    px = item["peak_x"]
                    sc = item["entry_score"]
                    narr = item["narrative"]
                    name = item["name"]
                    status = item["status"]
                    is_winner = px >= 2.0

                    # Outcomes
                    if status == "active":
                        active_count += 1
                    elif px >= 5.0:
                        success += 1
                    elif px >= 2.0:
                        moderate += 1
                    elif px < 0.5:
                        rugged += 1
                    else:
                        no_pump += 1

                    # Winner/loser scores
                    if is_winner:
                        winner_scores.append(sc)
                    elif status != "active":
                        loser_scores.append(sc)

                    # Score brackets
                    if sc >= 8.0:
                        b80[0] += 1
                        if is_winner: b80[1] += 1
                    elif sc >= 7.5:
                        b75[0] += 1
                        if is_winner: b75[1] += 1
                    elif sc >= 7.0:
                        b70[0] += 1
                        if is_winner: b70[1] += 1

                    # Narrative
                    if narr not in narr_stats:
                        narr_stats[narr] = [0, 0.0, 0]  # count, total_x, winners
                    narr_stats[narr][0] += 1
                    narr_stats[narr][1] += px
                    if is_winner:
                        narr_stats[narr][2] += 1

                    # Best/worst
                    best_calls.append((name, px, sc))
                    if status != "active":
                        worst_calls.append((name, px, sc))
                except Exception:
                    continue

            # Step 5: Build message
            winners_total = success + moderate
            pct = lambda n: int(n * 100 / total) if total else 0

            msg = f"📊 <b>PERFORMANCE ANALYTICS — {period_label}</b>\n"
            msg += f"<i>{total} alerts</i>\n\n"

            msg += f"<b>── Results ──</b>\n"
            msg += f"✅ 5X+: {success} ({pct(success)}%)\n"
            msg += f"⚠️ 2-5X: {moderate} ({pct(moderate)}%)\n"
            msg += f"💀 Rugged: {rugged} ({pct(rugged)}%)\n"
            msg += f"📉 No pump: {no_pump} ({pct(no_pump)}%)\n"
            msg += f"⏳ Active: {active_count}\n"
            msg += f"<b>Win rate (2X+): {pct(winners_total)}%</b>\n\n"

            avg_w = sum(winner_scores) / len(winner_scores) if winner_scores else 0
            avg_l = sum(loser_scores) / len(loser_scores) if loser_scores else 0
            msg += f"<b>── Score vs Performance ──</b>\n"
            msg += f"Winners avg score: <b>{avg_w:.1f}</b>\n"
            msg += f"Losers avg score:  <b>{avg_l:.1f}</b>\n\n"

            def bstr(b):
                if b[0] == 0: return "0 alerts"
                return f"{b[0]} alerts → <b>{int(b[1]*100/b[0])}% hit 2X+</b>"

            msg += f"<b>── Score Brackets ──</b>\n"
            msg += f"7.0-7.5: {bstr(b70)}\n"
            msg += f"7.5-8.0: {bstr(b75)}\n"
            msg += f"8.0+:    {bstr(b80)}\n\n"

            best_sorted = sorted(best_calls, key=lambda x: x[1], reverse=True)[:3]
            if best_sorted:
                msg += f"<b>── Best Calls ──</b>\n"
                for i, (n, px, sc) in enumerate(best_sorted):
                    medal = ["🥇", "🥈", "🥉"][i]
                    msg += f"{medal} {n} — <b>{px:.1f}X</b> (score: {sc:.1f})\n"
                msg += "\n"

            worst_sorted = sorted(worst_calls, key=lambda x: x[1])[:3]
            if worst_sorted:
                msg += f"<b>── Worst Calls ──</b>\n"
                for n, px, sc in worst_sorted:
                    msg += f"💀 {n} — {px:.1f}X (score: {sc:.1f})\n"
                msg += "\n"

            if narr_stats:
                msg += f"<b>── Narrative Performance ──</b>\n"
                sorted_n = sorted(narr_stats.items(), key=lambda x: x[1][0], reverse=True)[:6]
                for narr, (cnt, tot_x, wins) in sorted_n:
                    avg_x = tot_x / cnt if cnt else 0
                    w_rate = int(wins * 100 / cnt) if cnt else 0
                    label = narr if narr != "?" else "no match"
                    msg += f"  <b>{label}</b>: {cnt} → avg {avg_x:.1f}X ({w_rate}% win)\n"

            # Step 6: Send — split if too long
            if len(msg) > 4000:
                mid = msg.rfind("\n\n", 0, 4000)
                if mid > 0:
                    await send_tg(msg[:mid], cid)
                    await send_tg(msg[mid:], cid)
                else:
                    await send_tg(msg[:4000], cid)
            else:
                await send_tg(msg, cid)
            log.info("[ANALYTICS] Sent OK")
        except Exception as e:
            log.error(f"[ANALYTICS] FAILED: {e}")
            try:
                await send_tg(f"⚠️ Analytics error: {e}", cid)
            except Exception:
                pass

    async def send_patterns(cid):
        log.info("[PATTERNS] Command received")
        try:
            # Snapshot data
            all_records = []
            try:
                async with tracked_lock:
                    for t in tracked.values():
                        r = t.to_record()
                        all_records.append(r)
            except Exception:
                pass
            try:
                async with history_lock:
                    all_records.extend(list(leaderboard_history))
            except Exception:
                pass
            
            # Filter to tokens with lifecycle data
            with_lc = [r for r in all_records if r.get("lifecycle") and isinstance(r.get("lifecycle"), dict) and "5min" in r.get("lifecycle", {})]
            
            if len(with_lc) < 3:
                await send_tg(f"📊 Need more data. Only {len(with_lc)} tokens have lifecycle data. Keep running!", cid)
                return
            
            # Split into winners and losers
            winners = [r for r in with_lc if r.get("peak_x", 1.0) >= 2.0]
            losers = [r for r in with_lc if r.get("peak_x", 1.0) < 2.0 and r.get("status") != "active"]
            active = [r for r in with_lc if r.get("status") == "active"]
            
            total = len(with_lc)
            
            def avg_safe(lst):
                return sum(lst) / len(lst) if lst else 0
            
            def get_lc(record, checkpoint, field):
                try:
                    return float(record.get("lifecycle", {}).get(checkpoint, {}).get(field, 0))
                except Exception:
                    return 0
            
            msg = f"📊 <b>WIN vs LOSS PATTERNS</b>\n"
            msg += f"<i>{total} tokens with lifecycle data</i>\n"
            msg += f"<i>{len(winners)} winners | {len(losers)} losers | {len(active)} active</i>\n\n"
            
            # Build comparison for each checkpoint
            for cp in ["5min", "15min", "30min", "1hr"]:
                w_mcap = avg_safe([get_lc(r, cp, "mcap") for r in winners]) if winners else 0
                l_mcap = avg_safe([get_lc(r, cp, "mcap") for r in losers]) if losers else 0
                w_hold = avg_safe([get_lc(r, cp, "holders") for r in winners]) if winners else 0
                l_hold = avg_safe([get_lc(r, cp, "holders") for r in losers]) if losers else 0
                w_vol = avg_safe([get_lc(r, cp, "vol") for r in winners]) if winners else 0
                l_vol = avg_safe([get_lc(r, cp, "vol") for r in losers]) if losers else 0
                w_br = avg_safe([get_lc(r, cp, "buy_ratio") for r in winners]) if winners else 0
                l_br = avg_safe([get_lc(r, cp, "buy_ratio") for r in losers]) if losers else 0
                w_x = avg_safe([get_lc(r, cp, "x") for r in winners]) if winners else 0
                l_x = avg_safe([get_lc(r, cp, "x") for r in losers]) if losers else 0
                
                msg += f"<b>── At {cp} ──</b>\n"
                msg += f"{'':16s} {'WIN':>8s}  {'LOSS':>8s}\n"
                msg += f"MCap:        ${w_mcap:>7,.0f}  ${l_mcap:>7,.0f}\n"
                msg += f"Holders:     {w_hold:>8.0f}  {l_hold:>8.0f}\n"
                msg += f"Volume:      ${w_vol:>7,.0f}  ${l_vol:>7,.0f}\n"
                msg += f"Buy/Sell:    {w_br:>8.1f}  {l_br:>8.1f}\n"
                msg += f"X from entry:{w_x:>8.1f}  {l_x:>8.1f}\n\n"
            
            # Key signals
            msg += f"<b>── Key Signals ──</b>\n"
            
            # Holder growth: 5min to 1hr
            w_hgrowth = []
            l_hgrowth = []
            for r in winners:
                h5 = get_lc(r, "5min", "holders")
                h60 = get_lc(r, "1hr", "holders")
                if h5 > 0:
                    w_hgrowth.append((h60 - h5) / h5 * 100)
            for r in losers:
                h5 = get_lc(r, "5min", "holders")
                h60 = get_lc(r, "1hr", "holders")
                if h5 > 0:
                    l_hgrowth.append((h60 - h5) / h5 * 100)
            
            if w_hgrowth or l_hgrowth:
                wg = avg_safe(w_hgrowth) if w_hgrowth else 0
                lg = avg_safe(l_hgrowth) if l_hgrowth else 0
                msg += f"Holder growth 5m→1h: WIN <b>+{wg:.0f}%</b> vs LOSS <b>+{lg:.0f}%</b>\n"
            
            # Volume trend: 5min to 1hr
            w_vtrend = []
            l_vtrend = []
            for r in winners:
                v5 = get_lc(r, "5min", "vol")
                v60 = get_lc(r, "1hr", "vol")
                if v5 > 0:
                    w_vtrend.append((v60 - v5) / v5 * 100)
            for r in losers:
                v5 = get_lc(r, "5min", "vol")
                v60 = get_lc(r, "1hr", "vol")
                if v5 > 0:
                    l_vtrend.append((v60 - v5) / v5 * 100)
            
            if w_vtrend or l_vtrend:
                wv = avg_safe(w_vtrend) if w_vtrend else 0
                lv = avg_safe(l_vtrend) if l_vtrend else 0
                msg += f"Volume trend 5m→1h: WIN <b>{wv:+.0f}%</b> vs LOSS <b>{lv:+.0f}%</b>\n"
            
            # Socials comparison
            w_social = len([r for r in winners if r.get("has_socials")]) 
            l_social = len([r for r in losers if r.get("has_socials")])
            w_social_pct = int(w_social * 100 / len(winners)) if winners else 0
            l_social_pct = int(l_social * 100 / len(losers)) if losers else 0
            msg += f"Has socials: WIN <b>{w_social_pct}%</b> vs LOSS <b>{l_social_pct}%</b>\n"
            
            # Score comparison
            w_scores = [float(r.get("entry_score", 0)) for r in winners if r.get("entry_score")]
            l_scores = [float(r.get("entry_score", 0)) for r in losers if r.get("entry_score")]
            if w_scores or l_scores:
                msg += f"Avg score: WIN <b>{avg_safe(w_scores):.1f}</b> vs LOSS <b>{avg_safe(l_scores):.1f}</b>\n"
            
            # Quick rules
            msg += f"\n<b>── Quick Rules ──</b>\n"
            
            # Rule: holders at 5min
            h5_winners = [get_lc(r, "5min", "holders") for r in winners]
            h5_losers = [get_lc(r, "5min", "holders") for r in losers]
            if h5_winners and h5_losers:
                avg_hw = avg_safe(h5_winners)
                avg_hl = avg_safe(h5_losers)
                if avg_hw > avg_hl * 1.3:
                    msg += f"🟢 Winners have more holders at 5min ({avg_hw:.0f} vs {avg_hl:.0f})\n"
                elif avg_hl > avg_hw * 1.3:
                    msg += f"⚠️ Losers have more holders at 5min ({avg_hl:.0f} vs {avg_hw:.0f})\n"
            
            # Rule: buy ratio at 5min
            br5_winners = [get_lc(r, "5min", "buy_ratio") for r in winners if get_lc(r, "5min", "buy_ratio") > 0]
            br5_losers = [get_lc(r, "5min", "buy_ratio") for r in losers if get_lc(r, "5min", "buy_ratio") > 0]
            if br5_winners and br5_losers:
                avg_brw = avg_safe(br5_winners)
                avg_brl = avg_safe(br5_losers)
                if avg_brw > avg_brl:
                    msg += f"🟢 Winners have higher buy pressure at 5min ({avg_brw:.1f} vs {avg_brl:.1f})\n"
                else:
                    msg += f"⚠️ Losers have higher buy pressure at 5min ({avg_brl:.1f} vs {avg_brw:.1f})\n"
            
            # Rule: X at 5min
            x5_high_win = len([r for r in winners if get_lc(r, "5min", "x") >= 1.5])
            x5_high_lose = len([r for r in losers if get_lc(r, "5min", "x") >= 1.5])
            x5_total_high = x5_high_win + x5_high_lose
            if x5_total_high > 0:
                x5_win_rate = int(x5_high_win * 100 / x5_total_high)
                msg += f"🔍 Tokens already 1.5X+ at 5min: {x5_win_rate}% become winners\n"
            
            # Send — split if needed
            if len(msg) > 4000:
                mid = msg.rfind("\n\n", 0, 4000)
                if mid > 0:
                    await send_tg(msg[:mid], cid)
                    await send_tg(msg[mid:], cid)
                else:
                    await send_tg(msg[:4000], cid)
            else:
                await send_tg(msg, cid)
            log.info("[PATTERNS] Sent OK")
        except Exception as e:
            log.error(f"[PATTERNS] FAILED: {e}")
            try:
                await send_tg(f"⚠️ Patterns error: {e}", cid)
            except Exception:
                pass

    commands = {
        '/status':      lambda cid: send_tg(format_status(), cid),
        '/leaderboard': lambda cid: send_lb(cid, 1),
        '/weekly':      lambda cid: send_lb(cid, 7),
        '/monthly':     lambda cid: send_lb(cid, 30),
        '/narratives':  lambda cid: send_tg(format_narratives(), cid),
        '/tracking':    lambda cid: send_tg(format_tracking(), cid),
        '/analytics':   lambda cid: send_analytics(cid),
        '/analytics24': lambda cid: send_analytics(cid, 1),
        '/analytics7':  lambda cid: send_analytics(cid, 7),
        '/analytics30': lambda cid: send_analytics(cid, 30),
        '/patterns':    lambda cid: send_patterns(cid),
        '/help':        lambda cid: send_tg(format_help(), cid),
    }

    while True:
        try:
            if (utcnow() - last_hb).total_seconds() > 600:
                log.info("[CMD] Heartbeat")
                last_hb = utcnow()
            
            updates = await get_updates(offset)
            for update in updates:
                offset = update["update_id"] + 1
                msg = update.get("message", {})
                text = msg.get("text", "").strip()
                cid = str(msg.get("chat", {}).get("id", ""))
                if not text.startswith("/"): continue
                cmd = text.split()[0].lower().split('@')[0]
                log.info(f"[CMD] {cmd}")
                try:
                    if cmd in commands:
                        await commands[cmd](cid)
                    else:
                        await send_tg(f"❓ Unknown: {cmd}\nTry /help", cid)
                except Exception as cmd_err:
                    log.error(f"[CMD] Error in {cmd}: {cmd_err}")
                    await send_tg(f"⚠️ Error running {cmd}: {cmd_err}", cid)
        except Exception as e:
            log.error(f"[CMD] {e}")
            await asyncio.sleep(5)
        await asyncio.sleep(0.5)


# ═══════════════════════════════════════════════════════════════════════════════
# COPYCAT DETECTION — skip PVP copies of existing tokens
# ═══════════════════════════════════════════════════════════════════════════════
COPYCAT_MIN_MCAP = float(os.getenv("COPYCAT_MIN_MCAP", "50000"))

async def check_copycat(symbol: str, name: str, new_mint: str) -> bool:
    if not symbol or len(symbol) < 2:
        return False
    
    search_terms = [symbol]
    name_clean = name.strip().split()[0] if name else ""
    if name_clean.lower() not in [symbol.lower(), "the", "baby", "king", "queen", "sir", "mr", "ms", "dr", "new"]:
        if len(name_clean) >= 3:
            search_terms.append(name_clean)
    
    try:
        async with httpx.AsyncClient(timeout=6) as client:
            for term in search_terms:
                resp = await client.get(
                    f"https://api.dexscreener.com/latest/dex/search?q={term}",
                    headers={"User-Agent": "Mozilla/5.0"})
                if resp.status_code != 200:
                    continue
                
                pairs = resp.json().get("pairs") or []
                for pair in pairs:
                    base_token = pair.get("baseToken") or {}
                    
                    if base_token.get("address", "") == new_mint:
                        continue
                    
                    if pair.get("chainId", "") != "solana":
                        continue
                    
                    existing_sym = base_token.get("symbol", "").upper()
                    existing_name = base_token.get("name", "").lower()
                    
                    # Only match on symbol, not name — name matching causes too many false positives
                    # e.g. "Leo" the dog matching "Bitfinex LEO Token"
                    sym_match = (existing_sym == symbol.upper())
                    
                    if not sym_match:
                        continue
                    
                    existing_mcap = float(pair.get("marketCap") or pair.get("fdv") or 0)
                    existing_liq = float((pair.get("liquidity") or {}).get("usd") or 0)
                    
                    if existing_mcap >= COPYCAT_MIN_MCAP or existing_liq >= COPYCAT_MIN_MCAP:
                        log.info(f"  -> Found existing: {base_token.get('name','?')} (${existing_sym}) mcap=${existing_mcap:,.0f} liq=${existing_liq:,.0f}")
                        return True
                
                await asyncio.sleep(0.2)
    except Exception as e:
        log.warning(f"[Copycat] Check failed: {e}")
    
    return False


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN TOKEN HANDLER
# ═══════════════════════════════════════════════════════════════════════════════
_token_semaphore = asyncio.Semaphore(20)

async def handle_token(msg: dict):
    async with _token_semaphore:
        await _process_token(msg)

async def _process_token(msg: dict):
    global total_alerts_fired

    if msg.get("txType") != "create":
        return

    mint     = msg.get("mint", "")
    name     = msg.get("name", "")
    symbol   = msg.get("symbol", "")
    deployer = msg.get("traderPublicKey", "")
    desc     = msg.get("description", "")
    
    # Pump.fun stores socials in IPFS metadata (uri field)
    # We'll fetch it only for tokens that pass all filters
    token_uri = msg.get("uri", "")
    socials_raw = {"twitter": "", "telegram": "", "website": ""}

    if not mint or not name: return
    if len(mint) < 32 or len(mint) > 44: return

    # Skip Mayhem Mode tokens — extreme volatility, almost always rugs
    if msg.get("is_mayhem_mode"):
        log.info(f"[NEW] {name} (${symbol}) {mint[:12]}... -> ⚠️ MAYHEM MODE — skip")
        return

    log.info(f"[NEW] {name} (${symbol}) {mint[:12]}...")

    # Register for burst detection (before any filters)
    await register_token_for_burst(name, symbol, mint)

    # ── Narrative match (optional — boosts score but not required) ──────────
    narrative = match_narrative(name, symbol, desc)
    if narrative["matched"]:
        log.info(f"  -> ✓ Narrative: {narrative['keyword']} [{narrative['category']}]")
    else:
        log.info(f"  -> No narrative match (continuing to filters)")

    # ── Blacklist ────────────────────────────────────────────────────────────
    combined = f"{name} {symbol}".lower()
    for bl in BLACKLIST:
        if bl in combined:
            log.info(f"  -> Blacklisted '{bl}' — skip")
            return

    # ── Wait for data ────────────────────────────────────────────────────────
    await asyncio.sleep(WAIT_SECONDS)
    
    # ── Quick DexScreener check first (free, no API key) ─────────────────────
    # Low bar: just checking "is anyone buying this?" — quality gate comes later
    TRACTION_MCAP = max(MIN_MCAP, 3000)  # Match quality gate — no point enriching tokens that'll fail
    dex = await fetch_dexscreener(mint)
    dex_mcap = dex.get("mcap_usd", 0)
    dex_liq = dex.get("liquidity_usd", 0)
    
    if dex_mcap < TRACTION_MCAP and dex_liq < 2000:
        # No traction — try Birdeye as backup
        if BIRDEYE_API_KEY:
            try:
                async with httpx.AsyncClient(timeout=8) as client:
                    resp = await client.get(
                        "https://public-api.birdeye.so/defi/token_overview",
                        params={"address": mint},
                        headers={"X-API-KEY": BIRDEYE_API_KEY, "x-chain": "solana"})
                    if resp.status_code == 200:
                        d = resp.json().get("data") or {}
                        be_liq = float(d.get("liquidity") or 0)
                        be_mc = float(d.get("mc") or 0)
                        if be_mc >= TRACTION_MCAP or be_liq >= 2000:
                            pass  # Has traction on Birdeye, continue
                        else:
                            log.info(f"  -> No traction (mcap=${be_mc:,.0f} liq=${be_liq:,.0f}) — skip")
                            return
                    else:
                        log.info(f"  -> No traction (DexScreener mcap=${dex_mcap:,.0f}) — skip")
                        return
            except Exception:
                log.info(f"  -> No traction (DexScreener mcap=${dex_mcap:,.0f}) — skip")
                return
        else:
            log.info(f"  -> No traction (mcap=${dex_mcap:,.0f} liq=${dex_liq:,.0f}) — skip")
            return

    log.info(f"  -> Token has traction — enriching with full data")

    # ── Full enrichment (Helius + Birdeye/DexScreener) ────────────────────────
    token, source = await enrich_token(mint, name, symbol, deployer)
    token["description"] = desc

    if source == "none":
        log.info(f"  -> No data — skip")
        return

    liq = token["liquidity_usd"]
    mcap = token["mcap_usd"]
    log.info(f"  -> Source: {source}  liq=${liq:,.0f}  mcap=${mcap:,.0f}")

    # ── Copycat filter (only for tokens that passed traction check) ──────────
    is_copy = await check_copycat(symbol, name, mint)
    if is_copy:
        log.info(f"  -> ❌ Copycat of existing token — skip (PVP trap)")
        return

    # ── Quality gate ─────────────────────────────────────────────────────────
    has_lp = liq >= 3000
    has_mcap = mcap >= MIN_MCAP
    is_bc_with_reserve = (liq >= 2000 and mcap == 0)
    
    if not (has_lp or has_mcap or is_bc_with_reserve):
        log.info(f"  -> LP ${liq:,.0f} & MCap ${mcap:,.0f} too low — skip")
        return

    if mcap > 0 and mcap > MAX_MCAP:
        log.info(f"  -> MCap ${mcap:,.0f} > ${MAX_MCAP:,.0f} — skip")
        return

    # ── Dev holds filter ─────────────────────────────────────────────────────
    dev_pct = token.get("dev_holds_pct", 0)
    if dev_pct > MAX_DEV_HOLDS_PCT:
        log.info(f"  -> Dev holds {dev_pct:.1f}% > {MAX_DEV_HOLDS_PCT}% — skip")
        return

    # ── Top 10 holder filter ─────────────────────────────────────────────────
    # ONLY apply to migrated tokens with real liquidity. On bonding curve (liq=0),
    # getTokenLargestAccounts includes the bonding curve contract itself which
    # holds 50-80% of supply. That's the AMM pool, not a whale.
    top10 = token.get("top10_pct", 25.0)
    holders = token.get("total_holders", 50)
    
    if liq > 0:
        # Migrated token — top10 data is meaningful
        if holders >= 100:
            top10_limit = 30
        elif holders >= 50:
            top10_limit = 45
        else:
            top10_limit = 60
        
        if top10 > top10_limit:
            log.info(f"  -> Top10 holds {top10:.1f}% > {top10_limit}% ({holders} holders) — skip")
            return
    else:
        log.info(f"  -> Bonding curve — top10 filter skipped (BC inflates holder data)")

    # ── Hard MCap floor — no alerts below $5k regardless of env vars ──────
    if mcap < 5000 and liq < 5000:
        log.info(f"  -> MCap ${mcap:,.0f} below $5k floor — skip")
        return

    # ── Score ────────────────────────────────────────────────────────────────
    log.info(f"  -> ✅ PASSED FILTERS — dev={dev_pct:.1f}% top10={top10:.1f}% ({holders}h) — scoring")
    result = score_token(token, narrative)
    score = result["final_score"]
    log.info(f"  -> Score: {score}/10  {result['verdict']}")

    # ── Alert ────────────────────────────────────────────────────────────────
    if score >= ALERT_THRESHOLD:
        # Fetch socials from IPFS metadata (only for tokens that pass all filters)
        if token_uri:
            socials_raw = await fetch_token_socials(token_uri)
        token["socials"] = socials_raw
        
        # Check if this token is part of a trending burst
        keywords = extract_theme_keywords(name, symbol)
        burst_theme, burst_count = (None, 0)
        if keywords:
            async with _recent_lock:
                burst_theme, burst_count = find_burst_theme(keywords)
        
        is_trending = burst_theme and burst_count >= BURST_MIN_TOKENS
        
        if is_trending:
            # Store as candidate — evaluator will pick the best one later
            now_ts = utcnow().timestamp()
            last_alerted = BURST_ALERTED.get(burst_theme, 0)
            
            # Don't accept candidates if we already evaluated this theme recently
            if (now_ts - last_alerted) < BURST_WINDOW * 2:
                log.info(f"  -> Trending '{burst_theme}' already evaluated recently — skip")
                return
            
            candidate = {
                "mint": mint, "name": name, "symbol": symbol,
                "deployer": deployer, "desc": desc,
                "token": dict(token), "result": dict(result), "narrative": dict(narrative),
                "socials_raw": socials_raw, "token_uri": token_uri,
                "added_at": utcnow().isoformat(),
            }
            
            if burst_theme not in _burst_candidates:
                _burst_candidates[burst_theme] = []
            _burst_candidates[burst_theme].append(candidate)
            
            log.info(f"  -> Trending candidate '{burst_theme}' — {len(_burst_candidates[burst_theme])} candidates queued")
            
            # Start evaluator if not already running for this theme
            if burst_theme not in _burst_evaluating:
                _burst_evaluating[burst_theme] = True
                BURST_ALERTED[burst_theme] = now_ts
                asyncio.create_task(burst_evaluator(burst_theme, burst_count))
                log.info(f"  -> Burst evaluator started for '{burst_theme}' — will pick winner in {BURST_EVAL_DELAY}s")
            
            return  # Don't send normal alert — evaluator will handle it
        else:
            async with alerts_lock:
                total_alerts_fired += 1
            log.info(f"  🎯 FIRING — {name} (${symbol})")
            await send_tg(format_alert(token, result, narrative))

        # Track — entry_mcap must be at least MIN_MCAP to avoid fake X multipliers
        entry_mcap = mcap if mcap >= MIN_MCAP else liq if liq >= 1000 else mcap
        entry_mcap = max(entry_mcap, MIN_MCAP)  # Floor — never track below MIN_MCAP
        
        async with tracked_lock:
            t = TrackedToken(
                mint=mint, name=name, symbol=symbol,
                entry_mcap=entry_mcap, entry_score=score,
                narrative=narrative.get("keyword", "?"),
            )
            socials = socials_raw
            t.has_socials = bool(socials.get("twitter") or socials.get("telegram") or socials.get("website"))
            tracked[mint] = t
        asyncio.create_task(save_leaderboard())

        # Lifecycle tracker — monitors at 5min, 15min, 30min, 1hr
        asyncio.create_task(lifecycle_tracker(mint, name, symbol, deployer, desc, narrative, entry_mcap, score))
    else:
        log.info(f"  -> Score {score} < {ALERT_THRESHOLD} — skip")


# ═══════════════════════════════════════════════════════════════════════════════
# LIFECYCLE TRACKER — monitors token at checkpoints after alert
# ═══════════════════════════════════════════════════════════════════════════════
LIFECYCLE_CHECKPOINTS = [
    (300, "5min"),     # 5 minutes
    (900, "15min"),    # 15 minutes (cumulative: wait 600 more)
    (1800, "30min"),   # 30 minutes (cumulative: wait 900 more)
    (3600, "1hr"),     # 1 hour (cumulative: wait 1800 more)
]

async def lifecycle_tracker(mint, name, symbol, deployer, desc, narrative, entry_mcap, initial_score):
    try:
        checkpoints = []
        last_time = 0
        
        # Record entry snapshot
        entry_snap = {"label": "Entry", "mcap": entry_mcap, "holders": 0, "vol": 0, "buy_ratio": 0}
        checkpoints.append(entry_snap)
        
        for delay_total, label in LIFECYCLE_CHECKPOINTS:
            try:
                # Wait the difference from last checkpoint
                wait_secs = delay_total - last_time
                await asyncio.sleep(wait_secs)
                last_time = delay_total
                
                # Fetch fresh data
                mcap_now = 0.0
                liq_now = 0.0
                vol_now = 0.0
                holders_now = 0
                buy_ratio_now = 0.0
                
                try:
                    dex = await fetch_dexscreener(mint)
                    mcap_now = dex.get("mcap_usd", 0)
                    liq_now = dex.get("liquidity_usd", 0)
                    vol_now = dex.get("volume_1h_usd", 0)
                    holders_now = dex.get("total_holders", 0)
                    buy_ratio_now = dex.get("buy_sell_ratio_1h", 0)
                except Exception:
                    pass
                
                # If DexScreener failed, try Birdeye
                if mcap_now == 0 and BIRDEYE_API_KEY:
                    try:
                        async with httpx.AsyncClient(timeout=8) as client:
                            resp = await client.get(
                                "https://public-api.birdeye.so/defi/token_overview",
                                params={"address": mint},
                                headers={"X-API-KEY": BIRDEYE_API_KEY, "x-chain": "solana"})
                            if resp.status_code == 200:
                                d = resp.json().get("data") or {}
                                mcap_now = float(d.get("mc") or 0)
                                liq_now = float(d.get("liquidity") or 0)
                                vol_now = float(d.get("v1hUSD") or 0)
                                holders_now = int(d.get("holder") or 0)
                                buys = int(d.get("buy1h") or 1)
                                sells = int(d.get("sell1h") or 1)
                                buy_ratio_now = buys / max(sells, 1)
                    except Exception:
                        pass
                
                current_x = min(mcap_now / max(entry_mcap, 1000), 500) if mcap_now > 0 else 0
                
                snap = {
                    "label": label,
                    "mcap": mcap_now,
                    "holders": holders_now,
                    "vol": vol_now,
                    "buy_ratio": buy_ratio_now,
                    "liq": liq_now,
                    "x": current_x,
                }
                checkpoints.append(snap)
                
                log.info(f"[LIFECYCLE] {symbol} @ {label}: mcap=${mcap_now:,.0f} ({current_x:.1f}X) holders={holders_now} vol=${vol_now:,.0f}")
                
            except Exception as e:
                log.warning(f"[LIFECYCLE] {symbol} @ {label}: {e}")
                checkpoints.append({"label": label, "mcap": 0, "holders": 0, "vol": 0, "buy_ratio": 0, "liq": 0, "x": 0})
        
        # ── Build final lifecycle report ──────────────────────────────────────
        lines = [
            "📊 <b>LIFECYCLE REPORT</b>", "",
            f"<b>{name}</b>  <code>${symbol}</code>",
            f"<code>{mint}</code>", "",
        ]
        
        # Find peak
        peak_x = 0
        peak_label = "Entry"
        for snap in checkpoints:
            x = snap.get("x", 0)
            if x > peak_x:
                peak_x = x
                peak_label = snap["label"]
        
        # Determine pattern
        xs = [snap.get("x", 0) for snap in checkpoints[1:]]  # Skip entry
        if len(xs) >= 4:
            if xs[3] >= 2.0 and xs[3] >= xs[0]:
                pattern = "🟢 STRONG HOLD — still growing at 1hr"
                suggestion = "Could run further — watch for migration"
            elif peak_x >= 3.0 and xs[3] < peak_x * 0.5:
                pattern = "🔴 PUMP & DUMP — peaked then crashed"
                suggestion = f"Exit window was before {peak_label}"
            elif peak_x >= 2.0 and xs[3] >= 1.0:
                pattern = "🟡 PUMPED & SETTLING — took profit window existed"
                suggestion = f"Best exit around {peak_label} at {peak_x:.1f}X"
            elif all(x < 1.5 for x in xs):
                pattern = "🟠 SLOW — never gained momentum"
                suggestion = "Skip similar setups"
            elif xs[3] < 0.5:
                pattern = "💀 DEAD — collapsed to near zero"
                suggestion = "Likely rug or complete dump"
            else:
                pattern = "⚪ MIXED — no clear pattern"
                suggestion = "Monitor manually"
        else:
            pattern = "⚪ INCOMPLETE — missing checkpoints"
            suggestion = "Data was unavailable"
        
        lines.append(f"<b>{pattern}</b>")
        lines.append(f"<i>{suggestion}</i>")
        lines.append("")
        
        # Timeline
        lines.append("<b>── Timeline ──</b>")
        lines.append(f"  Entry:  ${entry_mcap:,.0f}")
        
        for snap in checkpoints[1:]:
            label = snap["label"]
            sm = snap.get("mcap", 0)
            sx = snap.get("x", 0)
            sh = snap.get("holders", 0)
            sv = snap.get("vol", 0)
            sbr = snap.get("buy_ratio", 0)
            
            if sx >= 2.0:
                dot = "🟢"
            elif sx >= 1.0:
                dot = "🟡"
            elif sx > 0:
                dot = "🔴"
            else:
                dot = "⚫"
            
            lines.append(f"  {label:5s}: ${sm:,.0f} ({sx:.1f}X) {dot}  |  {sh}h  ${sv:,.0f}vol  {sbr:.1f}b/s")
        
        lines.append("")
        lines.append(f"📈 Peak: <b>{peak_x:.1f}X</b> at {peak_label}")
        
        # Holder growth analysis
        h_5 = checkpoints[1].get("holders", 0) if len(checkpoints) > 1 else 0
        h_60 = checkpoints[-1].get("holders", 0) if len(checkpoints) > 1 else 0
        if h_5 > 0 and h_60 > h_5:
            h_growth = int((h_60 - h_5) / h_5 * 100)
            lines.append(f"👥 Holder growth: {h_5} → {h_60} (+{h_growth}%)")
        elif h_5 > 0:
            lines.append(f"👥 Holders stalled: {h_5} → {h_60}")
        
        # Volume trend
        v_5 = checkpoints[1].get("vol", 0) if len(checkpoints) > 1 else 0
        v_60 = checkpoints[-1].get("vol", 0) if len(checkpoints) > 1 else 0
        if v_5 > 0 and v_60 > 0:
            if v_60 > v_5 * 1.5:
                lines.append(f"📈 Volume increasing: ${v_5:,.0f} → ${v_60:,.0f}")
            elif v_60 < v_5 * 0.5:
                lines.append(f"📉 Volume dying: ${v_5:,.0f} → ${v_60:,.0f}")
        
        lines += [
            "",
            f"🔗 <a href='https://pump.fun/{mint}'>pump.fun</a>  "
            f"<a href='https://dexscreener.com/solana/{mint}'>dexscreener</a>  "
            f"<a href='https://gmgn.ai/sol/token/{mint}'>gmgn</a>",
            f"<i>🕐 {utcnow().strftime('%H:%M:%S UTC')}</i>",
        ]
        
        await send_tg("\n".join(lines))
        log.info(f"[LIFECYCLE] Sent report for {symbol} — peak {peak_x:.1f}X at {peak_label} — {pattern[:20]}")
        
        # Save lifecycle data to tracked token for pattern analysis
        try:
            lifecycle_record = {
                "peak_x": peak_x, "peak_label": peak_label, "pattern": pattern[:30],
            }
            for snap in checkpoints[1:]:
                lbl = snap.get("label", "?")
                lifecycle_record[lbl] = {
                    "mcap": snap.get("mcap", 0), "holders": snap.get("holders", 0),
                    "vol": snap.get("vol", 0), "buy_ratio": snap.get("buy_ratio", 0),
                    "x": snap.get("x", 0),
                }
            async with tracked_lock:
                if mint in tracked:
                    tracked[mint].lifecycle_data = lifecycle_record
            asyncio.create_task(save_leaderboard())
        except Exception:
            pass
        
    except Exception as e:
        log.error(f"[LIFECYCLE] {mint[:12]}: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# WEBSOCKET LOOP
# ═══════════════════════════════════════════════════════════════════════════════
async def ws_loop():
    delay = 5
    while True:
        try:
            log.info("[WS] Connecting to Pump.fun...")
            async with websockets.connect(
                PUMP_WS_URL,
                ping_interval=30,
                ping_timeout=60,
                close_timeout=10,
                max_size=2**20,
            ) as ws:
                await ws.send(json.dumps({"method": "subscribeNewToken"}))
                log.info("[WS] Subscribed")
                delay = 5
                async for raw in ws:
                    try:
                        asyncio.create_task(handle_token(json.loads(raw)))
                    except json.JSONDecodeError:
                        pass
                    except Exception as e:
                        log.error(f"[WS] Handler: {e}")
        except Exception as e:
            log.warning(f"[WS] {e}")
        sleep = min(delay + random.random() * 2, 30)
        log.info(f"[WS] Reconnecting in {sleep:.0f}s...")
        await asyncio.sleep(sleep)
        delay = min(delay * 2, 30)


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════
async def run():
    global bot_start_time
    bot_start_time = utcnow()
    load_leaderboard()

    kw_count = len(_KW_PATTERNS)
    cat_counts = {}
    for kw, (cat, _) in _KW_PATTERNS.items():
        cat_counts[cat] = cat_counts.get(cat, 0) + 1

    log.info("=" * 50)
    log.info("  TekkiSniPer — BOT ONLINE [v4.0 CLEAN]")
    log.info(f"  Threshold: {ALERT_THRESHOLD}  MinMCap: ${MIN_MCAP:,.0f}  MaxMCap: ${MAX_MCAP:,.0f}")
    log.info(f"  MaxDevHolds: {MAX_DEV_HOLDS_PCT}%  Wait: {WAIT_SECONDS}s")
    log.info(f"  Keywords: {kw_count} across {len(cat_counts)} categories")
    for cat, count in sorted(cat_counts.items()):
        log.info(f"    {cat}: {count} keywords")
    log.info(f"  Telegram: {'on' if TELEGRAM_BOT_TOKEN else 'off'} ({len(CHAT_IDS)} channel)")
    log.info(f"  Helius: {'on' if HELIUS_API_KEY else 'off'}  Birdeye: {'on' if BIRDEYE_API_KEY else 'off'}")
    log.info("=" * 50)

    if TELEGRAM_BOT_TOKEN:
        await delete_webhook()
        await asyncio.sleep(2)

    await send_tg(
        "🎯 <b>TekkiSniPer ONLINE [v4.0]</b>\n"
        f"Threshold: {ALERT_THRESHOLD}/10  |  Min MCap: ${MIN_MCAP:,.0f}\n"
        f"Keywords: {kw_count} ({', '.join(f'{c}:{n}' for c, n in sorted(cat_counts.items()))})\n"
        f"<i>Watching Pump.fun live...</i>"
    )

    tasks = [
        asyncio.create_task(ws_loop()),
        asyncio.create_task(track_tokens()),
        asyncio.create_task(leaderboard_scheduler()),
        asyncio.create_task(handle_commands()),
    ]
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        for t in tasks: t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    except Exception as e:
        log.critical(f"[FATAL] {e}"); raise


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        log.info("Interrupted")
    except Exception as e:
        log.critical(f"Fatal: {e}"); raise
