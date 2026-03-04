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
MIN_MCAP           = float(os.getenv("MIN_MCAP", "2000"))
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
        "tendies", "apes", "retard",
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

    def current_x(self):
        return round(self.current_mcap / max(self.entry_mcap, 1), 2)
    
    def classify_outcome(self):
        x = self.current_x()
        if self.peak_x >= 5.0:       return "success"
        elif x < 0.5 and self.status == "closed": return "rugged"
        elif self.peak_x >= 2.0:     return "moderate"
        elif self.peak_x < 2.0:      return "no_pump"
        else:                         return "active"

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
            return {
                "liquidity_usd":       float((pair.get("liquidity") or {}).get("usd") or 0),
                "mcap_usd":            float(pair.get("marketCap") or pair.get("fdv") or 0),
                "volume_1h_usd":       float((pair.get("volume") or {}).get("h1") or 0),
                "volume_5m_usd":       float((pair.get("volume") or {}).get("m5") or 0),
                "price_change_1h_pct": float((pair.get("priceChange") or {}).get("h1") or 0),
                "buy_sell_ratio_1h":   int((pair.get("txns") or {}).get("h1", {}).get("buys") or 1) /
                                       max(int((pair.get("txns") or {}).get("h1", {}).get("sells") or 1), 1),
                "total_holders":       int(pair.get("holders", 50)),
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
                                            top1_pct = (amounts[0] / supply) * 100
                                            top10_sum = sum(amounts[:10])
                                            top10_pct = (top10_sum / supply) * 100
                                            base["top1_pct"] = round(top1_pct, 1)
                                            base["top10_pct"] = round(top10_pct, 1)
                                            base["total_holders"] = max(len(accounts), base["total_holders"])
                                            log.info(f"  -> Holders: top1={top1_pct:.1f}% top10={top10_pct:.1f}% ({len(accounts)} accounts)")
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

    # DexScreener fallback
    if source == "none":
        dex = await fetch_dexscreener(mint)
        if dex.get("mcap_usd", 0) > 0 or dex.get("liquidity_usd", 0) > 0:
            base.update(dex)
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
# FORMATTERS
# ═══════════════════════════════════════════════════════════════════════════════
def format_alert(token: dict, score: dict, narrative: dict) -> str:
    mint = token.get("mint", "")
    comps = score["components"]
    lines = [
        "🎯 <b>TekkiSniPer</b>", "",
        f"<b>{token.get('name','?')}</b>  <code>${token.get('symbol','?')}</code>",
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
        f"<i>🕐 {utcnow().strftime('%H:%M:%S UTC')}</i>",
    ]
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
        "/analytics    — performance stats",
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
                    t.peak_x = t.peak_mcap / max(t.entry_mcap, 1)
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

    async def send_analytics(cid):
        try:
            all_t = []
            async with tracked_lock:
                for t in tracked.values():
                    all_t.append(t)
            async with history_lock:
                for r in leaderboard_history:
                    try:
                        t = TrackedToken(
                            r.get("mint", ""), r.get("name", "?"), r.get("symbol", "?"),
                            r.get("entry_mcap", 0), r.get("entry_score", 0), r.get("narrative", "?"))
                        t.peak_x = r.get("peak_x", 1.0)
                        t.current_mcap = r.get("current_mcap", 0)
                        t.status = r.get("status", "closed")
                        all_t.append(t)
                    except Exception:
                        continue
            
            if not all_t:
                await send_tg("📊 No data yet. Wait for some alerts!", cid)
                return
            
            total = len(all_t)
            outcomes = {"success": 0, "moderate": 0, "rugged": 0, "no_pump": 0, "active": 0}
            top_performers = []
            
            for t in all_t:
                try:
                    outcome = t.classify_outcome()
                    outcomes[outcome] = outcomes.get(outcome, 0) + 1
                    if outcome == "success":
                        top_performers.append(t)
                except Exception:
                    continue
            
            msg = f"📊 <b>PERFORMANCE ANALYTICS</b>\n\n"
            msg += f"<b>Total:</b> {total}\n\n"
            msg += f"✅ ≥5X: {outcomes['success']} ({outcomes['success']/total*100:.0f}%)\n"
            msg += f"⚠️ 2-5X: {outcomes['moderate']} ({outcomes['moderate']/total*100:.0f}%)\n"
            msg += f"💀 Rugged: {outcomes['rugged']} ({outcomes['rugged']/total*100:.0f}%)\n"
            msg += f"📉 <2X: {outcomes['no_pump']} ({outcomes['no_pump']/total*100:.0f}%)\n"
            msg += f"⏳ Active: {outcomes['active']}\n"
            
            if top_performers:
                msg += "\n<b>🔥 Top:</b>\n"
                for t in sorted(top_performers, key=lambda t: t.peak_x, reverse=True)[:3]:
                    msg += f"  {t.name} — {t.peak_x:.1f}X\n"
            
            await send_tg(msg, cid)
        except Exception as e:
            log.error(f"[ANALYTICS] {e}")
            await send_tg(f"⚠️ Analytics error: {e}", cid)

    commands = {
        '/status':      lambda cid: send_tg(format_status(), cid),
        '/leaderboard': lambda cid: send_lb(cid, 1),
        '/weekly':      lambda cid: send_lb(cid, 7),
        '/monthly':     lambda cid: send_lb(cid, 30),
        '/narratives':  lambda cid: send_tg(format_narratives(), cid),
        '/tracking':    lambda cid: send_tg(format_tracking(), cid),
        '/analytics':   lambda cid: send_analytics(cid),
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

    if not mint or not name: return
    if len(mint) < 32 or len(mint) > 44: return

    log.info(f"[NEW] {name} (${symbol}) {mint[:12]}...")

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
    TRACTION_MCAP = 3000  # Just above bonding curve base (~$2,400)
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

    # ── Score ────────────────────────────────────────────────────────────────
    log.info(f"  -> ✅ PASSED FILTERS — dev={dev_pct:.1f}% top10={top10:.1f}% ({holders}h) — scoring")
    result = score_token(token, narrative)
    score = result["final_score"]
    log.info(f"  -> Score: {score}/10  {result['verdict']}")

    # ── Alert ────────────────────────────────────────────────────────────────
    if score >= ALERT_THRESHOLD:
        async with alerts_lock:
            total_alerts_fired += 1
        log.info(f"  🎯 FIRING — {name} (${symbol})")
        await send_tg(format_alert(token, result, narrative))

        # Track
        entry_mcap = mcap if mcap > 0 else liq
        if entry_mcap > 0:
            async with tracked_lock:
                tracked[mint] = TrackedToken(
                    mint=mint, name=name, symbol=symbol,
                    entry_mcap=entry_mcap, entry_score=score,
                    narrative=narrative.get("keyword", "?"),
                )
            asyncio.create_task(save_leaderboard())
    else:
        log.info(f"  -> Score {score} < {ALERT_THRESHOLD} — skip")


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
                        await handle_token(json.loads(raw))
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
