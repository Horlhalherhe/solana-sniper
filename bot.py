"""
SOLANA_NARRATIVE_SNIPER — BOT LOOP [v3.0]
Pump.fun -> score -> Telegram alerts
+ Smart money tracking & copy-trade detection
+ Honeypot simulation + wallet cluster detection
+ Adaptive scoring via backtesting
+ Post-alert tracker: X multipliers + migration
+ PnL Leaderboard: 24h, weekly, monthly
+ Outcome recording for weight optimization

v3.0 UPGRADES:
- Smart money wallet tracking (check_token, scan_recent_activity, discover)
- Rug analyzer v2: on-chain fetching, honeypot sim, wallet clusters, bundles
- Entry scorer v2: 8 components (added smart_money, rug_safety, bundles)
- Backtester: records signals + outcomes, evolutionary weight optimization
- New commands: /smartmoney /optimize /performance /outcome
"""

import asyncio
import json
import os
import sys
import logging
import functools
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Dict, List, Tuple

import httpx
import websockets

sys.path.insert(0, str(Path(__file__).parent))
from sniper import SolanaNarrativeSniper

# ── v2 Modules (graceful — bot runs v1 if these fail) ─────────────────────────
try:
    from core.smart_money import SmartMoneyTracker
    from core.rug_analyzer_v2 import RugAnalyzerV2
    from core.entry_scorer_v2 import EntryScorerV2, EntryInputV2
    from core.backtester import SignalHistory, WeightOptimizer, AdaptiveEntryScorer
    V2_AVAILABLE = True
except ImportError as e:
    print(f"[WARN] v2 modules not available ({e}) — running v1 mode")
    V2_AVAILABLE = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("sniper-bot")

# Hide httpx request logs that expose Telegram bot token in URLs
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# ─── Configuration ─────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")  # Supports comma-separated IDs
HELIUS_API_KEY     = os.getenv("HELIUS_API_KEY", "")
BIRDEYE_API_KEY    = os.getenv("BIRDEYE_API_KEY", "")
ALERT_THRESHOLD    = float(os.getenv("ALERT_THRESHOLD", "5.0"))
MIN_LIQUIDITY      = float(os.getenv("MIN_LIQUIDITY_USD", "5000"))
MAX_ENTRY_MCAP     = float(os.getenv("MAX_ENTRY_MCAP", "100000"))
MIN_BONDING_MCAP   = float(os.getenv("MIN_BONDING_MCAP", "2000"))  # For pump.fun tokens with $0 LP

# Blacklist - reject tokens with these keywords even if they match narratives
BLACKLIST_KEYWORDS_STR = os.getenv("BLACKLIST_KEYWORDS", "inu,wif,wif hat,with hat")
BLACKLIST_KEYWORDS = [k.strip().lower() for k in BLACKLIST_KEYWORDS_STR.split(",") if k.strip()]

# Parse chat IDs (supports single or comma-separated)
CHAT_IDS = [cid.strip() for cid in TELEGRAM_CHAT_ID.split(",") if cid.strip()] if TELEGRAM_CHAT_ID else []
PUMP_WS_URL        = "wss://pumpportal.fun/api/data"
X_MILESTONES       = [2, 5, 10, 25, 50, 100]
MAX_LEADERBOARD_HISTORY = 10000
TRACKER_CONCURRENCY = 5

DATA_DIR = Path(os.getenv("SNIPER_DATA_DIR", "./data"))
DATA_DIR.mkdir(exist_ok=True)
LEADERBOARD_FILE = DATA_DIR / "leaderboard.json"

# ─── Locks ─────────────────────────────────────────────────────────────────────
tracked_lock = asyncio.Lock()
history_lock = asyncio.Lock()
alerts_lock  = asyncio.Lock()

def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)

# ─── Tracked Token ─────────────────────────────────────────────────────────────
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
        if self.entry_mcap <= 0:
            return 0.0
        return round(self.current_mcap / self.entry_mcap, 2)
    
    def classify_outcome(self):
        """Classify token performance for analytics"""
        x = self.current_x()
        
        # Success: 5X or more
        if self.peak_x >= 5.0:
            return "success"
        
        # Rugged: Down more than 50% from entry
        elif x < 0.5 and self.status == "closed":
            return "rugged"
        
        # Moderate: Hit 2-5X
        elif self.peak_x >= 2.0 and self.peak_x < 5.0:
            return "moderate"
        
        # No pump: Never hit 2X
        elif self.peak_x < 2.0:
            return "no_pump"
        
        # Still active
        else:
            return "active"
    
    def age_hours(self):
        """Get token age in hours"""
        return (utcnow() - self.added_at).total_seconds() / 3600

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

# ─── Global State ──────────────────────────────────────────────────────────────
tracked: Dict[str, TrackedToken] = {}
leaderboard_history: List[dict] = []
sniper_ref: Optional[SolanaNarrativeSniper] = None
bot_start_time: datetime = utcnow()
total_alerts_fired: int = 0

# ─── v2 Global State ──────────────────────────────────────────────────────────
smart_money_tracker: Optional[SmartMoneyTracker] = None
rug_analyzer_v2: Optional[RugAnalyzerV2] = None
scorer_v2: Optional[EntryScorerV2] = None
signal_history: Optional[SignalHistory] = None
weight_optimizer: Optional[WeightOptimizer] = None

# ─── Persistence ───────────────────────────────────────────────────────────────
async def save_leaderboard_async():
    try:
        async with tracked_lock, history_lock:
            records = [t.to_record() for t in tracked.values()]
            records += leaderboard_history
            if len(records) > MAX_LEADERBOARD_HISTORY:
                records = records[-MAX_LEADERBOARD_HISTORY//2:]
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                lambda: LEADERBOARD_FILE.write_text(json.dumps(records, indent=2), encoding='utf-8')
            )
    except Exception as e:
        log.error(f"[LB] Save error: {e}")

def load_leaderboard():
    global leaderboard_history
    try:
        if LEADERBOARD_FILE.exists():
            with open(LEADERBOARD_FILE) as f:
                data = json.load(f)
            leaderboard_history = data if isinstance(data, list) else []
            cutoff = utcnow() - timedelta(days=90)
            leaderboard_history = [
                r for r in leaderboard_history
                if datetime.fromisoformat(r.get("added_at", "2000-01-01")) > cutoff
            ][-MAX_LEADERBOARD_HISTORY:]
            log.info(f"[LB] Loaded {len(leaderboard_history)} records")
    except Exception as e:
        log.error(f"[LB] Load error: {e}")

# ─── Retry Decorator ───────────────────────────────────────────────────────────
def retry_on_429(max_retries=3, base_delay=1.0):
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            for attempt in range(max_retries):
                try:
                    return await func(*args, **kwargs)
                except httpx.HTTPStatusError as e:
                    if e.response.status_code == 429 and attempt < max_retries - 1:
                        delay = base_delay * (2 ** attempt) + random.random() * 0.5
                        log.warning(f"[RATE LIMIT] retry {attempt+1} in {delay:.1f}s")
                        await asyncio.sleep(delay)
                    else:
                        raise
                except (httpx.TimeoutException, asyncio.TimeoutError):
                    if attempt < max_retries - 1:
                        await asyncio.sleep(base_delay * (2 ** attempt))
                    else:
                        raise
            return await func(*args, **kwargs)
        return wrapper
    return decorator

# ─── Telegram ──────────────────────────────────────────────────────────────────
@retry_on_429(max_retries=3, base_delay=1.0)
async def send_telegram(text: str, chat_id: str = None) -> int:
    """Send message to Telegram. If chat_id is None, broadcasts to all configured channels."""
    if not TELEGRAM_BOT_TOKEN:
        print(text); return 0
    
    # If specific chat_id provided (e.g. for command responses), send only to that one
    if chat_id:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                    json={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                          "disable_web_page_preview": True}
                )
                resp.raise_for_status()
                return resp.json().get("result", {}).get("message_id", 0)
        except Exception as e:
            log.error(f"[TG] Send failed to {chat_id}: {e}")
        return 0
    
    # Broadcast mode: send to all configured channels
    if not CHAT_IDS:
        return 0
    
    last_msg_id = 0
    for cid in CHAT_IDS:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                    json={"chat_id": cid, "text": text, "parse_mode": "HTML",
                          "disable_web_page_preview": True}
                )
                resp.raise_for_status()
                last_msg_id = resp.json().get("result", {}).get("message_id", 0)
        except Exception as e:
            log.error(f"[TG] Send failed to {cid}: {e}")
    return last_msg_id

async def delete_telegram_webhook():
    if not TELEGRAM_BOT_TOKEN: return False
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/deleteWebhook",
                json={"drop_pending_updates": True}
            )
            if resp.status_code == 200 and resp.json().get("ok"):
                log.info("[TG] Webhook deleted")
                return True
    except Exception as e:
        log.warning(f"[TG] Webhook deletion error: {e}")
    return False

async def get_telegram_updates(offset: int = 0) -> list:
    if not TELEGRAM_BOT_TOKEN: return []
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates",
                params={"offset": offset, "timeout": 25, "allowed_updates": ["message"]}
            )
            if resp.status_code == 200:
                return resp.json().get("result", [])
            elif resp.status_code == 409:
                log.warning("[TG] 409 Conflict - webhook still active?")
                await asyncio.sleep(5)
            else:
                log.error(f"[TG] Updates failed: status {resp.status_code}")
    except httpx.TimeoutException:
        log.warning("[TG] Timeout getting updates (normal if no messages)")
    except Exception as e:
        log.error(f"[TG] Updates error: {e}")
    return []

# ─── DexScreener Fetcher ───────────────────────────────────────────────────────
async def fetch_dexscreener(mint: str) -> dict:
    """
    Fetch token data from DexScreener — free, no API key, indexes pump.fun fast.
    Returns normalized dict with same keys as Birdeye enrichment, or {} on failure.
    """
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.get(
                f"https://api.dexscreener.com/latest/dex/tokens/{mint}",
                headers={"User-Agent": "Mozilla/5.0"}
            )
            if resp.status_code != 200:
                return {}
            pairs = resp.json().get("pairs") or []
            if not pairs:
                return {}

            # Use the pair with highest liquidity (usually the main pool)
            pair = max(pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0))

            liquidity  = float((pair.get("liquidity") or {}).get("usd") or 0)
            fdv        = float(pair.get("fdv") or 0)
            mc         = float(pair.get("marketCap") or fdv or 0)
            vol_h1     = float((pair.get("volume") or {}).get("h1") or 0)
            vol_m5     = float((pair.get("volume") or {}).get("m5") or 0)
            pc_h1      = float((pair.get("priceChange") or {}).get("h1") or 0)
            pc_m5      = float((pair.get("priceChange") or {}).get("m5") or 0)
            buys_h1    = int((pair.get("txns") or {}).get("h1", {}).get("buys") or 1)
            sells_h1   = int((pair.get("txns") or {}).get("h1", {}).get("sells") or 1)

            log.info(f"  -> DexScreener: liq=${liquidity:,.0f} mcap=${mc:,.0f}")

            return {
                "liquidity_usd":        liquidity,
                "mcap_usd":             mc,
                "volume_1h_usd":        vol_h1,
                "volume_5m_usd":        vol_m5,
                "price_change_1h_pct":  pc_h1,
                "price_change_5m_pct":  pc_m5,
                "buy_sell_ratio_1h":    buys_h1 / max(sells_h1, 1),
                "lp_locked":            liquidity > 5000,
                # DexScreener doesn't provide holder breakdown — leave defaults
            }
    except Exception as e:
        log.warning(f"[DexScreener] Error {mint[:12]}: {e}")
    return {}

# ─── Formatters ────────────────────────────────────────────────────────────────
def format_alert(alert) -> str:
    e = alert.entry
    r = alert.rug
    t = alert.token
    n = alert.narrative
    entry_score  = e.get("final_score", 0)
    rug_score    = r.get("rug_score", 10)
    flags        = r.get("flags", [])
    flag_str     = "  ".join([f"⛔ {f['code']}" for f in flags[:3]]) if flags else "✅ None"
    narrative_kw = (n.get("narrative") or {}).get("keyword", "NONE").upper()
    comps        = e.get("components", {})
    mint         = t.get("mint", "")
    lines = [
        "🎯 <b>SNIPER ALERT</b>", "",
        f"<b>{t.get('name','?')}</b>  <code>${t.get('symbol','?')}</code>",
        f"<code>{mint}</code>", "",
        f"📊 <b>ENTRY:  {entry_score}/10  {e.get('verdict','')}</b>",
        f"🔒 <b>RUG:    {rug_score}/10  {r.get('verdict','')}</b>", "",
        f"📡 Narrative:  <b>{narrative_kw}</b>  ({n.get('narrative_score',0):.1f}/10)",
        f"💰 MCap:       <b>${t.get('mcap_usd',0):,.0f}</b>",
        f"💧 Liquidity:  <b>${t.get('liquidity_usd',0):,.0f}</b>",
        f"👥 Holders:    <b>{t.get('total_holders',0)}</b>",
        f"👨‍💻 Dev holds:  <b>{t.get('dev_holds_pct',0):.1f}%</b>",
        f"⏱ Age:        <b>{t.get('age_hours',0):.1f}h</b>",
        f"📈 Vol 1h:     <b>${t.get('volume_1h_usd',0):,.0f}</b>", "",
        f"NAR {comps.get('narrative',{}).get('score','?')}  "
        f"TIM {comps.get('timing',{}).get('score','?')}  "
        f"HOL {comps.get('holders',{}).get('score','?')}  "
        f"DEP {comps.get('deployer',{}).get('score','?')}  "
        f"MOM {comps.get('momentum',{}).get('score','?')}", "",
        f"<b>Flags:</b> {flag_str}",
    ]
    if e.get("signals"):
        lines += [""] + [f"✅ {s}" for s in e["signals"][:3]]
    if e.get("warnings"):
        lines += [f"⚠️ {w}" for w in e["warnings"][:3]]
    lines += [
        "",
        f"🔗 <a href='https://pump.fun/{mint}'>pump.fun</a>  "
        f"<a href='https://dexscreener.com/solana/{mint}'>dexscreener</a>  "
        f"<a href='https://solscan.io/token/{mint}'>solscan</a>",
        f"<i>🕐 {utcnow().strftime('%H:%M:%S UTC')}</i>",
    ]
    return "\n".join(lines)

def format_x_alert(token: TrackedToken, current_mcap: float, multiplier: int) -> str:
    emoji = "🚀" if multiplier < 10 else "🌕" if multiplier < 50 else "💎"
    return "\n".join([
        f"{emoji} <b>{multiplier}X ALERT</b>", "",
        f"<b>{token.name}</b>  <code>${token.symbol}</code>",
        f"<code>{token.mint}</code>", "",
        f"Entry MCap:   <b>${token.entry_mcap:,.0f}</b>",
        f"Current MCap: <b>${current_mcap:,.0f}</b>",
        f"Multiplier:   <b>{multiplier}X 🔥</b>", "",
        f"🔗 <a href='https://dexscreener.com/solana/{token.mint}'>dexscreener</a>  "
        f"<a href='https://pump.fun/{token.mint}'>pump.fun</a>",
        f"<i>🕐 {utcnow().strftime('%H:%M:%S UTC')}</i>",
    ])

# ─── v2 Alert Formatter ───────────────────────────────────────────────────────
def format_alert_v2(alert, v2_score, smart_signal, rug_v2, token_data, narrative_match) -> str:
    """Upgraded alert format with smart money, honeypot, and v2 scoring"""
    e = alert.entry
    r = alert.rug
    t = alert.token
    n = narrative_match
    mint = token_data.get("mint", "")
    name = token_data.get("name", "?")
    symbol = token_data.get("symbol", "?")

    # Use v2 score if available, else v1
    if v2_score:
        final_score = v2_score["final_score"]
        verdict = v2_score["verdict"]
        weight_src = v2_score.get("weights_source", "static")
    else:
        final_score = e.get("final_score", 0)
        verdict = e.get("verdict", "")
        weight_src = "v1"

    rug_score = rug_v2.rug_score if rug_v2 else r.get("rug_score", 10)
    rug_verdict = rug_v2.verdict if rug_v2 else r.get("verdict", "")

    # Score bar
    filled = int(final_score)
    bar = "█" * filled + "░" * (10 - filled)

    lines = [
        "🎯 <b>SNIPER ALERT</b> [v3]", "",
        f"<b>{name}</b>  <code>${symbol}</code>",
        f"<code>{mint}</code>", "",
        f"📊 <b>SCORE: {final_score}/10</b>  {verdict}",
        f"{bar}",
        f"🔒 <b>RUG:   {rug_score}/10</b>  {rug_verdict}",
        f"<i>weights: {weight_src}</i>", "",
    ]

    # Smart money section (most important new info)
    if smart_signal and smart_signal.wallets_in:
        lines.append(f"💰 <b>SMART MONEY</b>")
        lines.append(f"├ {len(smart_signal.wallets_in)} wallet(s) in ({smart_signal.total_smart_money_sol:.1f} SOL)")
        lines.append(f"├ Signal: {smart_signal.signal_strength:.1f}/10")
        if smart_signal.is_consensus:
            lines.append(f"└ 🔥 <b>CONSENSUS (3+ wallets)</b>")
        else:
            lines.append(f"└ Confidence: {smart_signal.avg_confidence:.0%}")
        # Show wallet tags
        for w in smart_signal.wallets_in[:3]:
            lines.append(f"   • {w.get('tag', w['address'][:8])} (WR: {w.get('win_rate', 0):.0%})")
        lines.append("")

    # Token info
    lines += [
        f"📡 Narrative:  <b>{((n.get('narrative') or {}).get('keyword', 'NONE')).upper()}</b>",
        f"💰 MCap:       <b>${token_data.get('mcap_usd', 0):,.0f}</b>",
        f"💧 Liquidity:  <b>${token_data.get('liquidity_usd', 0):,.0f}</b>",
        f"👥 Holders:    <b>{token_data.get('total_holders', 0)}</b>",
        f"📈 Vol 1h:     <b>${token_data.get('volume_1h_usd', 0):,.0f}</b>",
    ]

    # Honeypot status (critical safety info)
    if rug_v2 and rug_v2.honeypot_data:
        hp = rug_v2.honeypot_data
        if hp.can_sell and hp.sell_tax_pct < 15:
            lines.append(f"✅ Can sell (tax: {hp.sell_tax_pct:.0f}%)")
        elif hp.is_suspicious:
            lines.append(f"⚠️ Suspicious sell tax: {hp.sell_tax_pct:.0f}%")

    # Bundles
    if rug_v2 and rug_v2.bundle_data and rug_v2.bundle_data.sniper_estimate > 0:
        lines.append(f"🤖 Snipers: {rug_v2.bundle_data.sniper_estimate}")

    # Wallet clusters
    if rug_v2 and rug_v2.holder_data and rug_v2.holder_data.wallet_clusters_detected > 0:
        lines.append(f"🕸️ Clusters: {rug_v2.holder_data.wallet_clusters_detected}")

    lines.append("")

    # v2 Score breakdown
    if v2_score and v2_score.get("breakdown"):
        lines.append("<b>Score Breakdown</b>")
        for comp, data in v2_score["breakdown"].items():
            if isinstance(data, dict):
                raw = data.get("raw", 0)
                mini = "▓" * int(raw) + "░" * (10 - int(raw))
                lines.append(f"  {comp[:8]:8s} {raw:4.1f} {mini}")
        lines.append("")

    # Signals & warnings from v2
    if v2_score and v2_score.get("signals"):
        for s in v2_score["signals"][:4]:
            lines.append(f"✅ {s}")
    if v2_score and v2_score.get("warnings"):
        for w in v2_score["warnings"][:4]:
            lines.append(f"⚠️ {w}")

    lines += [
        "",
        f"🔗 <a href='https://pump.fun/{mint}'>pump.fun</a>  "
        f"<a href='https://dexscreener.com/solana/{mint}'>dexscreener</a>  "
        f"<a href='https://gmgn.ai/sol/token/{mint}'>gmgn</a>  "
        f"<a href='https://solscan.io/token/{mint}'>solscan</a>",
        f"<i>🕐 {utcnow().strftime('%H:%M:%S UTC')}</i>",
    ]
    return "\n".join(lines)
    emoji = "🚀" if multiplier < 10 else "🌕" if multiplier < 50 else "💎"
    return "\n".join([
        f"{emoji} <b>{multiplier}X ALERT</b>", "",
        f"<b>{token.name}</b>  <code>${token.symbol}</code>",
        f"<code>{token.mint}</code>", "",
        f"Entry MCap:   <b>${token.entry_mcap:,.0f}</b>",
        f"Current MCap: <b>${current_mcap:,.0f}</b>",
        f"Multiplier:   <b>{multiplier}X 🔥</b>", "",
        f"🔗 <a href='https://dexscreener.com/solana/{token.mint}'>dexscreener</a>  "
        f"<a href='https://pump.fun/{token.mint}'>pump.fun</a>",
        f"<i>🕐 {utcnow().strftime('%H:%M:%S UTC')}</i>",
    ])

def format_migration_alert(token: TrackedToken, current_mcap: float) -> str:
    verified = "✅ Verified" if token.migration_verified else "⚠️ Unverified"
    return "\n".join([
        "🎓 <b>MIGRATION ALERT</b>", f"<i>{verified} on Raydium</i>", "",
        f"<b>{token.name}</b>  <code>${token.symbol}</code>",
        f"<code>{token.mint}</code>", "",
        f"✅ Graduated Pump.fun -> <b>Raydium</b>",
        f"MCap at migration: <b>${current_mcap:,.0f}</b>",
        f"Entry MCap:        <b>${token.entry_mcap:,.0f}</b>",
        f"Multiplier:        <b>{token.current_x()}X</b>", "",
        f"🔗 <a href='https://dexscreener.com/solana/{token.mint}'>dexscreener</a>  "
        f"<a href='https://raydium.io/swap/?inputCurrency=sol&outputCurrency={token.mint}'>raydium</a>",
        f"<i>🕐 {utcnow().strftime('%H:%M:%S UTC')}</i>",
    ])

def format_leaderboard(records: list, period: str) -> str:
    if not records:
        return f"📊 <b>{period} LEADERBOARD</b>\n\nNo alerts recorded yet."
    sorted_records = sorted(records, key=lambda x: x.get("peak_x", 0), reverse=True)
    medals = ["🥇", "🥈", "🥉"]
    lines  = [f"📊 <b>{period} LEADERBOARD</b>", f"<i>{len(records)} tokens tracked</i>", ""]
    for i, r in enumerate(sorted_records[:10]):
        medal       = medals[i] if i < 3 else f"{i+1}."
        peak_x      = r.get("peak_x", 1)
        current_x   = r.get("current_x", 1)
        migrated    = "🎓" if r.get("migrated") else ""
        narrative   = r.get("narrative", "").upper()
        entry_score = r.get("entry_score", 0)
        if peak_x >= 10:   perf = "💎"
        elif peak_x >= 5:  perf = "🌕"
        elif peak_x >= 2:  perf = "🚀"
        else:              perf = "💀"
        lines.append(f"{medal} <b>{r.get('name','?')}</b> <code>${r.get('symbol','?')}</code> {migrated}")
        lines.append(f"   {perf} Peak: <b>{peak_x:.1f}X</b>  Now: {current_x:.1f}X  Score: {entry_score}  [{narrative}]")
        lines.append(f"   <a href='https://dexscreener.com/solana/{r.get('mint','')}'>chart</a>")
        lines.append("")
    peak_xs    = [r.get("peak_x", 1) for r in records]
    avg_x      = sum(peak_xs) / len(peak_xs)
    winners    = len([x for x in peak_xs if x >= 2])
    migrants   = len([r for r in records if r.get("migrated")])
    moon_shots = len([x for x in peak_xs if x >= 10])
    lines += [
        "── Stats ──",
        f"Avg peak X:   <b>{avg_x:.1f}X</b>",
        f"2X+ winners:  <b>{winners}/{len(records)}</b>",
        f"10X+:         <b>{moon_shots}</b>",
        f"Migrations:   <b>{migrants}</b>",
        f"<i>🕐 {utcnow().strftime('%d %b %Y %H:%M UTC')}</i>",
    ]
    return "\n".join(lines)

def format_status() -> str:
    uptime     = utcnow() - bot_start_time
    hours      = int(uptime.total_seconds() // 3600)
    mins       = int((uptime.total_seconds() % 3600) // 60)
    active     = len(tracked)
    total      = len(leaderboard_history) + active
    narratives = len(sniper_ref.narrative_engine.active_narratives) if sniper_ref else 0
    peak_xs    = [t.peak_x for t in tracked.values()]
    best_live  = f"{max(peak_xs):.1f}X" if peak_xs else "none"
    
    sm_count = smart_money_tracker.get_wallet_count() if smart_money_tracker else 0
    sig_count = len(signal_history.records) if signal_history else 0
    completed = len(signal_history.get_completed_records()) if signal_history else 0
    w_src = "adaptive" if weight_optimizer and Path(os.getenv("SNIPER_DATA_DIR", "./data"), "optimized_weights.json").exists() else "static"
    
    return "\n".join([
        "⚡ <b>BOT STATUS</b> [v3.0]", "",
        f"🟢 Online:        <b>{hours}h {mins}m</b>",
        f"🎯 Alerts fired:  <b>{total_alerts_fired}</b>",
        f"👁 Tracking now:  <b>{active} tokens</b>",
        f"📊 Total tracked: <b>{total}</b>",
        f"🏆 Best live:     <b>{best_live}</b>",
        f"📡 Narratives:    <b>{narratives} active</b>", "",
        "<b>── v2 Intelligence ──</b>",
        f"💰 Smart Money:   <b>{sm_count} wallets</b>",
        f"📝 Signals:       <b>{sig_count} recorded ({completed} with outcomes)</b>",
        f"⚙️ Weights:       <b>{w_src}</b>", "",
        f"🎚 Threshold:     <b>{ALERT_THRESHOLD}/10</b>",
        f"💧 Min LP:        <b>${MIN_LIQUIDITY:,.0f}</b>", "",
        f"<i>🕐 {utcnow().strftime('%H:%M:%S UTC')}</i>",
    ])

def format_narratives() -> str:
    if not sniper_ref:
        return "No narrative data."
    active = sniper_ref.narrative_engine.get_active_sorted()
    if not active:
        return "📡 <b>ACTIVE NARRATIVES</b>\n\nNone loaded."
    lines = ["📡 <b>ACTIVE NARRATIVES</b>", ""]
    for n in active[:20]:
        lines.append(f"<b>{n['keyword'].upper()}</b>  {n['score']:.1f}/10  [{n['category']}]")
    lines.append(f"\n<i>{len(active)} narratives active</i>")
    return "\n".join(lines)

def format_tracking() -> str:
    if not tracked:
        return "👁 <b>TRACKING</b>\n\nNo tokens being tracked right now."
    lines = [f"👁 <b>TRACKING ({len(tracked)} tokens)</b>", ""]
    for t in sorted(tracked.values(), key=lambda x: x.peak_x, reverse=True):
        age      = int((utcnow() - t.added_at).total_seconds() / 60)
        migrated = "🎓✅" if t.migration_verified else "🎓?" if t.migrated else ""
        lines.append(
            f"<b>{t.name}</b> <code>${t.symbol}</code> {migrated}\n"
            f"   {t.current_x():.1f}X now  Peak: {t.peak_x:.1f}X  Age: {age}m\n"
            f"   <a href='https://dexscreener.com/solana/{t.mint}'>chart</a>"
        )
        lines.append("")
    return "\n".join(lines)

def format_help() -> str:
    return "\n".join([
        "🤖 <b>SNIPER COMMANDS</b>", "",
        "<b>── Info ──</b>",
        "/status          — bot health + live stats",
        "/tracking        — tokens being tracked now",
        "/narratives      — active narrative list",
        "/help            — this menu", "",
        "<b>── Leaderboards ──</b>",
        "/leaderboard     — 24h leaderboard",
        "/weekly          — 7 day leaderboard",
        "/monthly         — 30 day leaderboard",
        "/analytics       — performance stats", "",
        "<b>── v2 Intelligence ──</b>",
        "/smartmoney      — tracked wallets + top performers",
        "/performance     — signal correlations + weight analysis",
        "/optimize        — run weight optimization (needs 20+ outcomes)",
    ])

# ─── MCap Fetcher (tracker) ────────────────────────────────────────────────────
@retry_on_429(max_retries=3, base_delay=2.0)
async def get_current_mcap(mint: str) -> Tuple[float, bool]:
    """Try Birdeye first, fall back to DexScreener for tracked tokens."""
    if BIRDEYE_API_KEY:
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                resp = await client.get(
                    "https://public-api.birdeye.so/defi/token_overview",
                    params={"address": mint},
                    headers={"X-API-KEY": BIRDEYE_API_KEY, "x-chain": "solana"}
                )
                if resp.status_code == 200:
                    data      = resp.json().get("data") or {}
                    mcap      = float(data.get("mc") or 0)
                    liquidity = float(data.get("liquidity") or 0)
                    migrated  = mcap >= 65000 and liquidity > 10000
                    if migrated and not await verify_raydium_pool(mint):
                        migrated = False
                    if mcap > 0:
                        return mcap, migrated
        except Exception as e:
            log.warning(f"[MCap/Birdeye] {mint[:12]}: {e}")

    # Fallback to DexScreener
    try:
        dex = await fetch_dexscreener(mint)
        if dex.get("mcap_usd", 0) > 0:
            mcap     = dex["mcap_usd"]
            migrated = mcap >= 65000 and dex.get("liquidity_usd", 0) > 10000
            return mcap, migrated
    except Exception as e:
        log.warning(f"[MCap/DexScreener] {mint[:12]}: {e}")

    return 0.0, False

async def verify_raydium_pool(mint: str) -> bool:
    if not HELIUS_API_KEY:
        return True
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.post(
                f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}",
                json={"jsonrpc": "2.0", "id": 1,
                      "method": "getProgramAccounts",
                      "params": [
                          "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",
                          {"filters": [{"memcmp": {"offset": 400, "bytes": mint}}],
                           "encoding": "base64", "dataSlice": {"length": 0, "offset": 0}}
                      ]}
            )
            if resp.status_code == 200:
                return len(resp.json().get("result", [])) > 0
    except Exception as e:
        log.debug(f"[Raydium] {mint[:12]}: {e}")
    return False

# ─── Token Enrichment ──────────────────────────────────────────────────────────
@retry_on_429(max_retries=2, base_delay=1.0)
async def enrich_token(mint: str, name: str, symbol: str, deployer: str) -> Tuple[dict, str]:
    """
    Returns (token_data, source) where source is 'birdeye', 'dexscreener', or 'none'.
    Caller must check source != 'none' before scoring.
    """
    base = {
        "mint": mint, "name": name, "symbol": symbol, "deployer": deployer,
        "age_hours": 0.5, "mcap_usd": 0, "liquidity_usd": 0,
        "total_holders": 50, "top1_pct": 5.0, "top5_pct": 20.0,
        "top10_pct": 25.0, "dev_holds_pct": 0.0, "wallet_clusters": 0,
        "holder_growth_1h": 0, "mint_authority_revoked": True,
        "freeze_authority_revoked": True, "lp_burned": False, "lp_locked": False,
        "pool_age_hours": 0.5, "deployer_age_days": 30,
        "deployer_prev_tokens": [], "deployer_prev_rugs": [],
        "volume_5m_usd": 0, "volume_1h_usd": 0,
        "price_change_5m_pct": 0, "price_change_1h_pct": 0,
        "buy_sell_ratio_1h": 1.0, "market_conditions": "neutral",
    }
    source = "none"

    # ── Helius: mint/freeze authority + deployer holdings ──────────────────────────
    if HELIUS_API_KEY:
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                resp = await client.post(
                    f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}",
                    json={"jsonrpc": "2.0", "id": 1, "method": "getAccountInfo",
                          "params": [mint, {"encoding": "jsonParsed"}]}
                )
                if resp.status_code == 200:
                    result = resp.json().get("result") or {}
                    value  = result.get("value") or {}
                    info   = ((value.get("data") or {}).get("parsed") or {}).get("info") or {}
                    if info:
                        base["mint_authority_revoked"]   = info.get("mintAuthority") is None
                        base["freeze_authority_revoked"] = info.get("freezeAuthority") is None
                        supply = float(info.get("supply", 0)) / (10 ** info.get("decimals", 0))
                        base["total_supply"] = supply
                        
                        # Get deployer's token balance
                        if supply > 0 and deployer:
                            balance_resp = await client.post(
                                f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}",
                                json={"jsonrpc": "2.0", "id": 2, "method": "getTokenAccountsByOwner",
                                      "params": [deployer, {"mint": mint}, {"encoding": "jsonParsed"}]}
                            )
                            if balance_resp.status_code == 200:
                                accounts = balance_resp.json().get("result", {}).get("value", [])
                                if accounts:
                                    token_amount = accounts[0].get("account", {}).get("data", {}).get("parsed", {}).get("info", {}).get("tokenAmount", {})
                                    deployer_balance = float(token_amount.get("uiAmount", 0))
                                    base["dev_holds_pct"] = (deployer_balance / supply) * 100 if supply > 0 else 0
                                    log.info(f"  -> Dev wallet: {deployer_balance:,.0f} / {supply:,.0f} ({base['dev_holds_pct']:.1f}%)")
                                else:
                                    log.info(f"  -> Dev wallet: no token accounts found")
                            else:
                                log.warning(f"  -> Dev balance check failed: {balance_resp.status_code}")
        except Exception as e:
            log.warning(f"[Helius] {mint[:12]}: {e}")

    # ── Birdeye: full market data ───────────────────────────────────────────
    if BIRDEYE_API_KEY:
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                resp = await client.get(
                    "https://public-api.birdeye.so/defi/token_overview",
                    params={"address": mint},
                    headers={"X-API-KEY": BIRDEYE_API_KEY, "x-chain": "solana"}
                )
                if resp.status_code == 200:
                    data      = resp.json().get("data") or {}
                    liquidity = float(data.get("liquidity") or 0)
                    mc        = float(data.get("mc") or 0)
                    if mc > 0 or liquidity > 0:
                        base.update({
                            "liquidity_usd":       liquidity,
                            "mcap_usd":            mc,
                            "volume_1h_usd":       float(data.get("v1hUSD") or 0),
                            "volume_5m_usd":       float(data.get("v5mUSD") or 0),
                            "price_change_1h_pct": float(data.get("priceChange1hPercent") or 0),
                            "price_change_5m_pct": float(data.get("priceChange5mPercent") or 0),
                            "buy_sell_ratio_1h":   int(data.get("buy1h") or 1) / max(int(data.get("sell1h") or 1), 1),
                            "total_holders":       int(data.get("holder") or 50),
                            "top1_pct":            float(data.get("top1HolderPercent") or 5),
                            "top10_pct":           float(data.get("top10HolderPercent") or 25),
                            "lp_locked":           liquidity > 5000,
                        })
                        log.info(f"  -> Birdeye: liq=${liquidity:,.0f} mcap=${mc:,.0f}")
                        source = "birdeye"
                elif resp.status_code == 400:
                    log.info(f"  -> Birdeye 400 — trying DexScreener...")
        except Exception as e:
            log.warning(f"[Birdeye] {mint[:12]}: {e}")

    # ── DexScreener fallback ────────────────────────────────────────────────
    if source == "none":
        dex = await fetch_dexscreener(mint)
        if dex.get("mcap_usd", 0) > 0 or dex.get("liquidity_usd", 0) > 0:
            base.update(dex)
            source = "dexscreener"

    return base, source

# ─── Tracker ───────────────────────────────────────────────────────────────────
async def track_tokens():
    semaphore = asyncio.Semaphore(TRACKER_CONCURRENCY)

    async def update_single(mint: str, token: TrackedToken):
        async with semaphore:
            try:
                if (utcnow() - token.added_at).total_seconds() / 3600 > 24:
                    # ── Record outcome for backtesting before archiving ──
                    if signal_history:
                        try:
                            outcome = token.classify_outcome()
                            backtest_outcome = {
                                "success": "winner", "moderate": "winner",
                                "rugged": "rug", "no_pump": "loser",
                            }.get(outcome, "unknown")
                            signal_history.update_outcome(
                                mint, backtest_outcome,
                                peak_x=token.peak_x,
                                final_x=token.current_x(),
                                peak_mcap=token.peak_mcap,
                            )
                            log.info(f"  [BACKTEST] Recorded {token.symbol}: {backtest_outcome} (peak {token.peak_x:.1f}X)")
                            
                            # Discover smart money from winners
                            if backtest_outcome == "winner" and token.peak_x >= 3.0 and smart_money_tracker:
                                async with httpx.AsyncClient(timeout=15) as session:
                                    discovered = await smart_money_tracker.discover_from_token(
                                        session, mint, "winner", token.peak_x
                                    )
                                    if discovered:
                                        log.info(f"  [SMART$] Discovered {len(discovered)} wallet(s) from {token.symbol}")
                        except Exception as e:
                            log.warning(f"  [BACKTEST] Record error: {e}")
                    
                    async with tracked_lock, history_lock:
                        if mint in tracked:
                            leaderboard_history.append(tracked[mint].to_record())
                            del tracked[mint]
                    asyncio.create_task(save_leaderboard_async())
                    log.info(f"[TRACKER] Archived {token.symbol} peak={token.peak_x:.1f}X")
                    return
                current_mcap, migrated_flag = await get_current_mcap(mint)
                if current_mcap == 0:
                    return
                async with tracked_lock:
                    if mint not in tracked:
                        return
                    token              = tracked[mint]
                    token.current_mcap = current_mcap
                    token.peak_mcap    = max(token.peak_mcap, current_mcap)
                    token.peak_x       = token.peak_mcap / max(token.entry_mcap, 1)
                    token.last_updated = utcnow()
                    if migrated_flag and not token.migration_verified:
                        if await verify_raydium_pool(mint):
                            token.migrated = token.migration_verified = True
                            token.status = "migrated"
                            log.info(f"[TRACKER] Migration: {token.symbol}")
                            await send_telegram(format_migration_alert(token, current_mcap))
                        else:
                            token.migrated = True
                    if token.entry_mcap > 0:
                        multiplier = current_mcap / token.entry_mcap
                        for x in X_MILESTONES:
                            if multiplier >= x and x not in token.alerted_xs:
                                token.alerted_xs.add(x)
                                log.info(f"[TRACKER] {x}X: {token.symbol}")
                                await send_telegram(format_x_alert(token, current_mcap, x))
            except Exception as e:
                log.error(f"[TRACKER] Error {mint[:12]}: {e}")

    while True:
        await asyncio.sleep(120)
        async with tracked_lock:
            tokens_to_check = list(tracked.items())
        if not tokens_to_check:
            continue
        await asyncio.gather(*[update_single(m, t) for m, t in tokens_to_check], return_exceptions=True)

# ─── Leaderboard Scheduler ─────────────────────────────────────────────────────
async def leaderboard_scheduler():
    intervals = {'daily': timedelta(days=1), 'weekly': timedelta(days=7), 'monthly': timedelta(days=30)}
    last_run  = {k: utcnow() for k in intervals}
    while True:
        await asyncio.sleep(60)
        now = utcnow()
        for period, delta in intervals.items():
            if (now - last_run[period]).total_seconds() >= delta.total_seconds():
                last_run[period] = now
                records = await _get_records_since_async(now - delta)
                period_name = "24H" if period == 'daily' else period.upper()
                await send_telegram(format_leaderboard(records, period_name))

async def _get_records_since_async(cutoff: datetime) -> List[dict]:
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

# ─── Command Handler ───────────────────────────────────────────────────────────
async def handle_commands():
    offset = 0
    last_heartbeat = utcnow()
    log.info("[CMD] Command listener started")

    async def _send_leaderboard(chat_id: str, days: int):
        records = await _get_records_since_async(utcnow() - timedelta(days=days))
        period_name = "24H" if days == 1 else f"{days}D"
        await send_telegram(format_leaderboard(records, period_name), chat_id)
    
    async def send_analytics(chat_id: str):
        """Send performance analytics"""
        try:
            # Collect all tokens (active + history)
            all_tokens = []
            
            async with tracked_lock:
                active_count = len(tracked)
                all_tokens.extend(tracked.values())
                log.info(f"[ANALYTICS] Collected {active_count} active tokens")
            
            async with history_lock:
                history_count = len(leaderboard_history)
                log.info(f"[ANALYTICS] Processing {history_count} history records")
                for record in leaderboard_history:
                    try:
                        t = TrackedToken(
                            mint=record["mint"],
                            name=record["name"],
                            symbol=record["symbol"],
                            entry_mcap=record["entry_mcap"],
                            entry_score=record["entry_score"],
                            narrative=record.get("narrative", "unknown")
                        )
                        t.peak_x = record["peak_x"]
                        t.current_mcap = record["current_mcap"]
                        t.status = record["status"]
                        all_tokens.append(t)
                    except Exception as e:
                        log.warning(f"[ANALYTICS] Skip record: {e}")
                        continue
            
            log.info(f"[ANALYTICS] Total tokens collected: {len(all_tokens)}")
            
            if not all_tokens:
                log.warning("[ANALYTICS] No tokens found in tracked or history")
                await send_telegram("📊 No data yet. Wait for some alerts!", chat_id)
                return
            
            # Classify outcomes
            outcomes = {
                "success": [],
                "moderate": [],
                "rugged": [],
                "no_pump": [],
                "active": []
            }
            
            for t in all_tokens:
                try:
                    outcome = t.classify_outcome()
                    outcomes[outcome].append(t)
                except Exception as e:
                    log.warning(f"[ANALYTICS] Skip classify {t.symbol}: {e}")
            
            total = len(all_tokens)
            success_count = len(outcomes["success"])
            moderate_count = len(outcomes["moderate"])
            rugged_count = len(outcomes["rugged"])
            no_pump_count = len(outcomes["no_pump"])
            active_count = len(outcomes["active"])
            
            # Calculate averages
            avg_score_success = sum(t.entry_score for t in outcomes["success"]) / len(outcomes["success"]) if outcomes["success"] else 0
            avg_score_failure = sum(t.entry_score for t in outcomes["no_pump"]) / len(outcomes["no_pump"]) if outcomes["no_pump"] else 0
            
            # Build message
            msg = f"📊 <b>PERFORMANCE ANALYTICS</b>\n\n"
            msg += f"<b>Total Alerts:</b> {total}\n\n"
            
            msg += f"<b>Outcomes:</b>\n"
            msg += f"✅ Success (≥5X): {success_count} ({success_count/total*100:.1f}%)\n"
            msg += f"⚠️  Moderate (2-5X): {moderate_count} ({moderate_count/total*100:.1f}%)\n"
            msg += f"💀 Rugged: {rugged_count} ({rugged_count/total*100:.1f}%)\n"
            msg += f"📉 No Pump (<2X): {no_pump_count} ({no_pump_count/total*100:.1f}%)\n"
            msg += f"⏳ Active: {active_count}\n\n"
            
            msg += f"<b>Entry Score Analysis:</b>\n"
            msg += f"Success avg: {avg_score_success:.1f}/10\n"
            msg += f"Failure avg: {avg_score_failure:.1f}/10\n\n"
            
            # Top performers
            if outcomes["success"]:
                top_3 = sorted(outcomes["success"], key=lambda t: t.peak_x, reverse=True)[:3]
                msg += f"<b>🔥 Top Performers:</b>\n"
                for i, t in enumerate(top_3, 1):
                    msg += f"{i}. {t.name} - {t.peak_x:.1f}X\n"
                msg += "\n"
            
            # Worst performers
            if outcomes["rugged"]:
                msg += f"<b>💀 Rugged ({len(outcomes['rugged'])}):</b>\n"
                for t in outcomes["rugged"][:3]:
                    msg += f"- {t.name}\n"
                msg += "\n"
            
            # Success rate and recommendation
            success_rate = (success_count / total * 100) if total > 0 else 0
            msg += f"<b>📈 Success Rate: {success_rate:.1f}%</b>\n"
            
            if success_rate < 30:
                msg += "\n⚠️ <i>Consider raising filters</i>"
            elif success_rate > 50:
                msg += "\n✅ <i>Great performance!</i>"
            
            await send_telegram(msg, chat_id)
            
        except Exception as e:
            log.error(f"[ANALYTICS] Error: {e}")
            await send_telegram(f"⚠️ Analytics error: {str(e)}", chat_id)

    # ── v2 Command Handlers ──────────────────────────────────────────────────
    async def send_smart_money_status(chat_id: str):
        """Show smart money tracker status"""
        if not smart_money_tracker:
            await send_telegram("💰 Smart money tracker not initialized", chat_id)
            return
        
        wallet_count = smart_money_tracker.get_wallet_count()
        top = smart_money_tracker.get_top_wallets(5)
        
        lines = [f"💰 <b>SMART MONEY TRACKER</b>", "",
                 f"Wallets tracked: <b>{wallet_count}</b>", ""]
        
        if top:
            lines.append("<b>Top Wallets by Confidence:</b>")
            for i, w in enumerate(top, 1):
                tag = w.get("tag", w["address"][:8])
                conf = w.get("confidence_score", 0)
                wr = w.get("win_rate", 0)
                trades = w.get("total_trades", 0)
                lines.append(f"{i}. <b>{tag}</b>")
                lines.append(f"   WR: {wr:.0%} | Trades: {trades} | Conf: {conf:.2f}")
        else:
            lines.append("<i>No wallets added yet.</i>")
            lines.append("Add wallets from GMGN.ai or Birdeye leaderboards.")
        
        await send_telegram("\n".join(lines), chat_id)
    
    async def send_performance(chat_id: str):
        """Show signal performance analysis"""
        if not signal_history:
            await send_telegram("📊 Signal history not initialized", chat_id)
            return
        
        stats = signal_history.get_stats()
        correlations = signal_history.get_component_correlations()
        
        lines = [f"📊 <b>SIGNAL PERFORMANCE</b>", ""]
        
        if stats.get("total_signals", 0) == 0:
            lines.append("No signals recorded yet.")
            await send_telegram("\n".join(lines), chat_id)
            return
        
        lines += [
            f"Total signals: <b>{stats.get('total_signals', 0)}</b>",
            f"Completed: <b>{stats.get('completed', 0)}</b>",
            f"Win rate: <b>{stats.get('win_rate', 0):.1%}</b>",
            f"Alerted win rate: <b>{stats.get('alerted_win_rate', 0):.1%}</b>", "",
            f"Winners: {stats.get('winners', 0)} | Losers: {stats.get('losers', 0)} | Rugs: {stats.get('rugs', 0)}",
            f"Best trade: <b>{stats.get('best_trade_x', 0):.1f}X</b>",
            f"Avg peak (winners): <b>{stats.get('avg_peak_x_winners', 0):.1f}X</b>", "",
        ]
        
        if isinstance(correlations, dict) and "message" not in correlations:
            lines.append("<b>Signal Predictive Power:</b>")
            for comp, data in list(correlations.items())[:6]:
                power = data.get("predictive_power", "?")
                sep = data.get("separation", 0)
                emoji = {"strong": "🟢", "moderate": "🟡", "weak": "🟠", "negative": "🔴"}.get(power, "⚪")
                lines.append(f"  {emoji} {comp}: {power} (sep: {sep:+.1f})")
        
        if weight_optimizer:
            weights = weight_optimizer.get_weights()
            lines += ["", "<b>Current Weights:</b>"]
            sorted_w = sorted(weights.items(), key=lambda x: x[1], reverse=True)
            for k, v in sorted_w:
                lines.append(f"  {k}: {v*100:.1f}%")
        
        await send_telegram("\n".join(lines), chat_id)
    
    async def run_optimization(chat_id: str):
        """Run weight optimization"""
        if not weight_optimizer:
            await send_telegram("⚙️ Optimizer not initialized", chat_id)
            return
        
        await send_telegram("⚙️ Running weight optimization...", chat_id)
        result = weight_optimizer.optimize()
        
        if result.get("success"):
            lines = [
                "✅ <b>OPTIMIZATION COMPLETE</b>", "",
                f"Improvement: <b>{result['improvement_pct']:+.1f}%</b>",
                f"Records used: {result['records_used']}", "",
                "<b>New Weights:</b>",
            ]
            for comp in result.get("components_ranked", []):
                lines.append(f"  {comp['component']}: {comp['pct']}")
            await send_telegram("\n".join(lines), chat_id)
        else:
            await send_telegram(f"⚠️ {result.get('message', 'Optimization failed')}", chat_id)

    commands = {
        '/status':      lambda cid: send_telegram(format_status(), cid),
        '/leaderboard': lambda cid: _send_leaderboard(cid, 1),
        '/weekly':      lambda cid: _send_leaderboard(cid, 7),
        '/monthly':     lambda cid: _send_leaderboard(cid, 30),
        '/narratives':  lambda cid: send_telegram(format_narratives(), cid),
        '/tracking':    lambda cid: send_telegram(format_tracking(), cid),
        '/analytics':   lambda cid: send_analytics(cid),
        '/smartmoney':  lambda cid: send_smart_money_status(cid),
        '/performance': lambda cid: send_performance(cid),
        '/optimize':    lambda cid: run_optimization(cid),
        '/help':        lambda cid: send_telegram(format_help(), cid),
    }

    while True:
        try:
            # Heartbeat every 10 minutes
            if (utcnow() - last_heartbeat).total_seconds() > 600:
                log.info("[CMD] Heartbeat - command handler alive")
                last_heartbeat = utcnow()
            
            updates = await get_telegram_updates(offset)
            if not updates:
                # No updates - normal, just wait
                await asyncio.sleep(1)
                continue
                
            for update in updates:
                offset  = update["update_id"] + 1
                msg     = update.get("message", {})
                text    = msg.get("text", "").strip()
                chat_id = str(msg.get("chat", {}).get("id", ""))
                if not text.startswith("/"): continue
                cmd = text.split()[0].lower().split('@')[0]
                log.info(f"[CMD] {cmd} from user")
                try:
                    if cmd in commands:
                        await commands[cmd](chat_id)
                    else:
                        await send_telegram(f"❓ Unknown: {cmd}\nTry /help", chat_id)
                except Exception as cmd_error:
                    log.error(f"[CMD] Error executing {cmd}: {cmd_error}")
                    await send_telegram(f"⚠️ Error executing {cmd}", chat_id)
        except Exception as e:
            log.error(f"[CMD] Loop error: {e}")
            await asyncio.sleep(5)
        await asyncio.sleep(0.1)

# ─── Main Bot Loop ─────────────────────────────────────────────────────────────
async def run_bot():
    global sniper_ref, total_alerts_fired, bot_start_time
    global smart_money_tracker, rug_analyzer_v2, scorer_v2, signal_history, weight_optimizer
    bot_start_time = utcnow()
    load_leaderboard()

    sniper     = SolanaNarrativeSniper()
    sniper_ref = sniper

    # ── Initialize v2 Modules ─────────────────────────────────────────────────
    if V2_AVAILABLE:
        try:
            signal_history = SignalHistory()
            weight_optimizer = WeightOptimizer(signal_history)
            adaptive_scorer = AdaptiveEntryScorer(weight_optimizer)
            scorer_v2 = EntryScorerV2(adaptive_scorer=adaptive_scorer)
            smart_money_tracker = SmartMoneyTracker()
            rug_analyzer_v2 = RugAnalyzerV2()
            
            sm_count = smart_money_tracker.get_wallet_count()
            sig_count = len(signal_history.records)
            log.info(f"[v2] Modules initialized — Smart money: {sm_count} wallets, History: {sig_count} signals")
        except Exception as e:
            log.error(f"[v2] Init error (running with v1 fallback): {e}")
    else:
        log.info("[v2] Modules not available — running v1 only")

    seed = os.getenv("SEED_NARRATIVES", "")
    if seed:
        for item in seed.split("|"):
            parts = [p.strip() for p in item.split(",")]
            if len(parts) >= 3:
                try:
                    sniper.inject_narrative(parts[0], parts[1], float(parts[2]))
                except ValueError:
                    log.warning(f"[SEED] Invalid score for {parts[0]}")

    sm_status = f"{smart_money_tracker.get_wallet_count()} wallets" if smart_money_tracker else "off"
    
    log.info("=" * 50)
    log.info("  TekkiSniPer — BOT ONLINE [v3.0]")
    log.info(f"  Threshold: {ALERT_THRESHOLD}  MinLP: ${MIN_LIQUIDITY:,.0f}  MaxMCap: ${MAX_ENTRY_MCAP:,.0f}")
    log.info(f"  Bonding curve min MCap: ${MIN_BONDING_MCAP:,.0f} (when LP=$0)")
    log.info(f"  Data: Birdeye -> DexScreener fallback")
    log.info(f"  Smart Money: {sm_status}")
    log.info(f"  Scoring: {'adaptive' if weight_optimizer else 'static'} weights")
    log.info(f"  Telegram: {'on' if TELEGRAM_BOT_TOKEN else 'off'} ({len(CHAT_IDS)} channel{'s' if len(CHAT_IDS) != 1 else ''})")
    log.info(f"  Helius: {'on' if HELIUS_API_KEY else 'off'}  Birdeye: {'on' if BIRDEYE_API_KEY else 'off'}")
    log.info("=" * 50)

    if TELEGRAM_BOT_TOKEN:
        await delete_telegram_webhook()
        await asyncio.sleep(2)

    await send_telegram(
        "🎯 <b>TekkiSniPer ONLINE</b> [v3.0]\n"
        f"Threshold: {ALERT_THRESHOLD}/10  |  Min LP: ${MIN_LIQUIDITY:,.0f}  |  Max MCap: ${MAX_ENTRY_MCAP:,.0f}\n"
        f"Bonding curve min: ${MIN_BONDING_MCAP:,.0f} (when LP=$0)\n"
        f"💰 Smart Money: {sm_status}\n"
        f"📊 Scoring: {'adaptive' if weight_optimizer else 'static'} weights\n"
        f"🛡️ Rug v2: honeypot sim + clusters + bundles\n"
        f"Commands: /help for full list\n"
        f"<i>Watching Pump.fun live...</i>"
    )

    tasks = [
        asyncio.create_task(_bot_loop(sniper)),
        asyncio.create_task(track_tokens()),
        asyncio.create_task(leaderboard_scheduler()),
        asyncio.create_task(handle_commands()),
        asyncio.create_task(_smart_money_scanner()),
    ]
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        log.info("[BOT] Shutting down...")
        for t in tasks: t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    except Exception as e:
        log.critical(f"[BOT] Fatal: {e}"); raise

async def _smart_money_scanner():
    """Periodically scan tracked wallets for new buys — only alert on quality signals"""
    await asyncio.sleep(60)  # Wait for startup
    while True:
        try:
            if smart_money_tracker and smart_money_tracker.get_wallet_count() > 0:
                async with httpx.AsyncClient(timeout=15) as session:
                    new_buys = await smart_money_tracker.scan_recent_activity(
                        session, lookback_minutes=15
                    )
                    if new_buys:
                        # Group by token
                        from collections import defaultdict
                        by_token = defaultdict(list)
                        for buy in new_buys:
                            by_token[buy["mint"]].append(buy)
                        
                        for mint, buys in by_token.items():
                            # Require 3+ UNIQUE wallets (not 2) — reduces noise
                            unique_wallets = {b["wallet"] for b in buys}
                            if len(unique_wallets) < 3:
                                continue
                            
                            # Quick quality check — skip if token has no liquidity AND low mcap
                            try:
                                dex = await fetch_dexscreener(mint)
                                liq = dex.get("liquidity_usd", 0)
                                mcap = dex.get("mcap_usd", 0)
                                if liq < MIN_LIQUIDITY and mcap < MIN_BONDING_MCAP:
                                    log.info(f"[SmartMoney] {mint[:12]} — {len(unique_wallets)} wallets but LP ${liq:,.0f} MCap ${mcap:,.0f} — skip")
                                    continue
                            except Exception:
                                continue  # Can't verify = skip
                            
                            tags = ", ".join(b.get("tag", b["wallet"][:8]) for b in buys[:3])
                            total_sol = sum(b.get("sol_amount", 0) for b in buys)
                            await send_telegram(
                                f"💰 <b>SMART MONEY ALERT</b>\n\n"
                                f"{len(unique_wallets)} tracked wallets buying:\n"
                                f"<code>{mint}</code>\n"
                                f"Wallets: {tags}\n"
                                f"Total: {total_sol:.2f} SOL\n"
                                f"LP: ${liq:,.0f} | MCap: ${mcap:,.0f}\n\n"
                                f"🔗 <a href='https://dexscreener.com/solana/{mint}'>chart</a> · "
                                f"<a href='https://gmgn.ai/sol/token/{mint}'>gmgn</a>"
                            )
                
                # Decay inactive wallets weekly
                smart_money_tracker.decay_inactive(days_threshold=14)
                
        except Exception as e:
            log.warning(f"[SmartMoney Scanner] Error: {e}")
        
        await asyncio.sleep(300)  # Scan every 5 minutes

async def _bot_loop(sniper):
    retry_delay = 5
    max_delay   = 30
    while True:
        try:
            await asyncio.wait_for(_listen(sniper), timeout=3600)
            retry_delay = 5
        except asyncio.TimeoutError:
            log.warning("[WS] Timeout — reconnecting")
        except websockets.exceptions.ConnectionClosed as e:
            log.warning(f"[WS] Closed: {e}")
        except Exception as e:
            log.error(f"[WS] Error: {e}")
        sleep_time = min(retry_delay + random.random() * 2, max_delay)
        log.info(f"[WS] Reconnecting in {sleep_time:.1f}s...")
        await asyncio.sleep(sleep_time)
        retry_delay = min(retry_delay * 2, max_delay)

async def _listen(sniper):
    log.info("[WS] Connecting to Pump.fun...")
    async with websockets.connect(
        PUMP_WS_URL, ping_interval=20, ping_timeout=10, close_timeout=5
    ) as ws:
        await ws.send(json.dumps({"method": "subscribeNewToken"}))
        log.info("[WS] Subscribed")
        async for raw in ws:
            try:
                await handle_token(sniper, json.loads(raw))
            except json.JSONDecodeError:
                pass
            except Exception as e:
                log.error(f"[WS] Handler error: {e}")

async def handle_token(sniper, msg: dict):
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

    # Check narrative (for display only, not required)
    quick = sniper.narrative_engine.match_token_to_narrative(name, symbol, desc)
    narrative_matched = quick.get("matched", False)
    if narrative_matched:
        log.info(f"  -> Narrative: {quick.get('narrative', {}).get('keyword', 'unknown')}")
    else:
        log.info(f"  -> No narrative match (proceeding to quality check)")
    
    # Blacklist filter - reject obvious rugs/scams
    combined_text = f"{name} {symbol}".lower()
    for blacklisted in BLACKLIST_KEYWORDS:
        if blacklisted in combined_text:
            log.info(f"  -> Blacklisted keyword '{blacklisted}' detected — skip")
            return

    # Wait 60s then fetch (gives DexScreener time to index without missing the move)
    await asyncio.sleep(60)
    token_data, source = await enrich_token(mint, name, symbol, deployer)
    token_data["description"] = desc

    # If no market data yet, wait 45s and try once more
    if source == "none":
        log.info(f"  -> No market data yet — retrying in 45s...")
        await asyncio.sleep(45)
        token_data, source = await enrich_token(mint, name, symbol, deployer)
        token_data["description"] = desc

    # Skip if still no data from either source
    if source == "none":
        log.info(f"  -> No data from Birdeye or DexScreener — skip")
        return

    log.info(f"  -> Source: {source}  liq=${token_data['liquidity_usd']:,.0f}  mcap=${token_data['mcap_usd']:,.0f}")

    # ── Quality gate: LP or MCap ─────────────────────────────────────────────
    # Pump.fun bonding curve tokens have $0 LP (no DEX pool until graduation ~$69k).
    # For these, mcap IS the quality signal. Accept if EITHER meets minimum.
    liq = token_data["liquidity_usd"]
    mcap = token_data.get("mcap_usd", 0)
    
    if liq >= MIN_LIQUIDITY:
        pass  # Has real LP — good (migrated token)
    elif mcap >= MIN_BONDING_MCAP:
        pass  # No LP but bonding curve has traction — acceptable
    else:
        log.info(f"  -> LP ${liq:,.0f} & MCap ${mcap:,.0f} below minimums — skip")
        return
    
    # MCap ceiling (prefer micro caps with more upside)
    if mcap > MAX_ENTRY_MCAP:
        log.info(f"  -> MCap ${mcap:,.0f} > ${MAX_ENTRY_MCAP:,.0f} — skip")
        return

    # Holder filters (only when source provides real holder data)
    if source == "birdeye":
        if token_data["top1_pct"] > 10:
            log.info(f"  -> Top1 {token_data['top1_pct']:.1f}% > 10% — skip")
            return
        if token_data["top10_pct"] > 40:
            log.info(f"  -> Top10 {token_data['top10_pct']:.1f}% > 40% — skip")
            return
        # Dev holds filter (only when Birdeye has real data)
        if token_data.get("dev_holds_pct", 0) > 5.0:
            log.info(f"  -> Dev holds {token_data['dev_holds_pct']:.1f}% > 5% — skip")
            return

    # ── v1 analysis (narrative + entry + rug) ─────────────────────────────────
    log.info(f"  -> ✅ PASSED FILTERS — entering scoring pipeline")
    alert       = sniper.analyze_token(token_data)
    entry_score = alert.entry.get("final_score", 0)
    rug_score   = alert.rug.get("rug_score", 10)
    flags       = alert.rug.get("flags", [])
    log.info(f"  -> v1 Entry: {entry_score}/10  Rug: {rug_score}/10  {alert.entry.get('verdict','')}")

    # ── v2: Smart Money Check ─────────────────────────────────────────────────
    smart_signal = None
    if smart_money_tracker:
        try:
            async with httpx.AsyncClient(timeout=15) as session:
                smart_signal = await smart_money_tracker.check_token(session, mint)
                if smart_signal and smart_signal.wallets_in:
                    log.info(f"  -> 💰 Smart Money: {len(smart_signal.wallets_in)} wallet(s), "
                             f"signal={smart_signal.signal_strength:.1f}/10")
        except Exception as e:
            log.warning(f"  -> Smart money check error: {e}")

    # ── v2: Rug Analysis (on-chain + honeypot + clusters + bundles) ───────────
    rug_v2_report = None
    if rug_analyzer_v2:
        try:
            async with httpx.AsyncClient(timeout=20) as session:
                rug_v2_report = await rug_analyzer_v2.analyze(
                    session, mint, deployer=deployer, name=name, symbol=symbol
                )
                log.info(f"  -> v2 Rug: {rug_v2_report.rug_score}/10 — {rug_v2_report.verdict}")
                if rug_v2_report.honeypot_data and rug_v2_report.honeypot_data.is_honeypot:
                    log.info(f"  -> 🍯 HONEYPOT DETECTED — skip")
                    return
        except Exception as e:
            log.warning(f"  -> Rug v2 error (using v1 fallback): {e}")

    # ── v2: Adaptive Scoring ──────────────────────────────────────────────────
    v2_score_result = None
    v2_entry_score = entry_score  # Fallback to v1 score

    if scorer_v2:
        try:
            # Build v2 input from all available data
            v2_input = EntryInputV2(
                # Narrative
                narrative_score=quick.get("narrative_score", 0),
                narrative_confidence=quick.get("confidence", 0),
                # Timing
                launch_age_hours=token_data.get("age_hours", 0.5),
                market_conditions=token_data.get("market_conditions", "neutral"),
                # Holders
                total_holders=token_data.get("total_holders", 0),
                top1_pct=token_data.get("top1_pct", 5),
                top10_pct=token_data.get("top10_pct", 25),
                dev_holds_pct=token_data.get("dev_holds_pct", 0),
                wallet_clusters=rug_v2_report.holder_data.wallet_clusters_detected if rug_v2_report and rug_v2_report.holder_data else 0,
                holder_growth_1h=token_data.get("holder_growth_1h", 0),
                # Deployer
                rug_score=rug_v2_report.rug_score if rug_v2_report else rug_score,
                deployer_age_days=rug_v2_report.deployer_data.age_days if rug_v2_report and rug_v2_report.deployer_data else token_data.get("deployer_age_days", 30),
                deployer_prev_rugs=len(rug_v2_report.deployer_data.rugged_tokens) if rug_v2_report and rug_v2_report.deployer_data else 0,
                deployer_is_serial=rug_v2_report.deployer_data.tokens_last_30d > 10 if rug_v2_report and rug_v2_report.deployer_data else False,
                # Momentum
                volume_1h_usd=token_data.get("volume_1h_usd", 0),
                price_change_1h_pct=token_data.get("price_change_1h_pct", 0),
                buy_sell_ratio_1h=token_data.get("buy_sell_ratio_1h", 1.0),
                lp_usd=token_data.get("liquidity_usd", 0),
                # Smart Money (NEW)
                smart_money_wallets_in=len(smart_signal.wallets_in) if smart_signal else 0,
                smart_money_signal_strength=smart_signal.signal_strength if smart_signal else 0,
                smart_money_avg_confidence=smart_signal.avg_confidence if smart_signal else 0,
                smart_money_total_sol=smart_signal.total_smart_money_sol if smart_signal else 0,
                smart_money_is_consensus=smart_signal.is_consensus if smart_signal else False,
                # Honeypot (NEW)
                is_honeypot=rug_v2_report.honeypot_data.is_honeypot if rug_v2_report and rug_v2_report.honeypot_data else False,
                honeypot_sell_tax_pct=rug_v2_report.honeypot_data.sell_tax_pct if rug_v2_report and rug_v2_report.honeypot_data else 0,
                is_honeypot_suspicious=rug_v2_report.honeypot_data.is_suspicious if rug_v2_report and rug_v2_report.honeypot_data else False,
                mint_authority_revoked=token_data.get("mint_authority_revoked", True),
                freeze_authority_revoked=token_data.get("freeze_authority_revoked", True),
                lp_burned=token_data.get("lp_burned", False),
                lp_locked=token_data.get("lp_locked", False),
                # Bundles (NEW)
                sniper_count=rug_v2_report.bundle_data.sniper_estimate if rug_v2_report and rug_v2_report.bundle_data else 0,
                is_heavily_bundled=rug_v2_report.bundle_data.is_heavily_bundled if rug_v2_report and rug_v2_report.bundle_data else False,
                # Pump.fun bonding curve detection
                is_bonding_curve=token_data.get("liquidity_usd", 0) < 1000,  # No real LP = bonding curve
            )

            v2_score_result = scorer_v2.score(v2_input)
            v2_entry_score = v2_score_result["final_score"]
            log.info(f"  -> v2 Score: {v2_entry_score}/10 — {v2_score_result['verdict']} "
                     f"({v2_score_result.get('weights_source', 'static')} weights)")
        except Exception as e:
            log.warning(f"  -> v2 scoring error (using v1 fallback): {e}")
            v2_entry_score = entry_score

    # ── Use the HIGHER of v1 and v2 flags for safety ──────────────────────────
    effective_rug_score = rug_v2_report.rug_score if rug_v2_report else rug_score
    
    # Critical v2 flags (honeypot, etc.) — always skip
    v2_flags = rug_v2_report.flags if rug_v2_report else []
    critical_v2_flags = [f for f in v2_flags if hasattr(f, 'severity') and f.severity == "critical"]
    
    # v1 flags: Only hard-skip on the truly dangerous ones.
    # Pump.fun tokens ALWAYS have mint/freeze authority active + no LP lock,
    # so blocking on any flag = blocking 100% of tokens.
    HARD_BLOCK_FLAGS = {"HONEYPOT_DETECTED", "MASSIVE_DEV_DUMP", "LP_FULLY_DRAINED"}
    if flags:
        hard_flags = [f for f in flags if f.get('code') in HARD_BLOCK_FLAGS]
        if hard_flags:
            flag_codes = [f['code'] for f in hard_flags[:3]]
            log.info(f"  -> Critical v1 flags {flag_codes} — skip")
            return
        # Log non-critical flags but continue
        flag_codes = [f['code'] for f in flags[:3]]
        log.info(f"  -> v1 flags (non-blocking): {flag_codes}")
    
    if critical_v2_flags:
        flag_codes = [f.code for f in critical_v2_flags[:3]]
        log.info(f"  -> v2 Critical flags {flag_codes} — skip")
        return

    # ── Decision: use v2 score if available, else v1 ──────────────────────────
    final_score = v2_entry_score

    if final_score >= ALERT_THRESHOLD:
        async with alerts_lock:
            total_alerts_fired += 1
        log.info(f"  🎯 FIRING — {name} (${symbol})  [{source}]  v2={v2_entry_score:.1f}")
        
        # Send upgraded alert with v2 data
        alert_msg = format_alert_v2(alert, v2_score_result, smart_signal, rug_v2_report, token_data, quick)
        await send_telegram(alert_msg)
        
        # Track token
        entry_mcap = token_data.get("mcap_usd", 0)
        if entry_mcap > 0:
            nar_kw = (quick.get("narrative") or {}).get("keyword", "unknown")
            async with tracked_lock:
                tracked[mint] = TrackedToken(
                    mint=mint, name=name, symbol=symbol,
                    entry_mcap=entry_mcap, entry_score=final_score,
                    narrative=nar_kw,
                )
            asyncio.create_task(save_leaderboard_async())
        
        # ── Record signal for backtesting ─────────────────────────────────
        if signal_history and v2_score_result:
            try:
                components = v2_score_result.get("components", {})
                signal_history.record_signal(
                    mint=mint, name=name, symbol=symbol,
                    narrative_score=components.get("narrative", 5),
                    timing_score=components.get("timing", 5),
                    holders_score=components.get("holders", 5),
                    deployer_score=components.get("deployer", 5),
                    momentum_score=components.get("momentum", 5),
                    smart_money_score=components.get("smart_money", 0),
                    rug_safety_score=components.get("rug_safety", 5),
                    bundles_score=components.get("bundles", 5),
                    entry_score=final_score,
                    alert_sent=True,
                    source=source,
                    narrative_category=(quick.get("narrative") or {}).get("category", ""),
                )
            except Exception as e:
                log.warning(f"  -> Signal recording error: {e}")

if __name__ == "__main__":
    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        log.info("[MAIN] Interrupted")
    except Exception as e:
        log.critical(f"[MAIN] Fatal: {e}"); raise
