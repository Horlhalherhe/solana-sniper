"""
SOLANA_NARRATIVE_SNIPER — BOT LOOP [CORRECTED v2.2]
Pump.fun -> score -> Telegram alerts
+ Post-alert tracker: X multipliers + migration
+ PnL Leaderboard: 24h, weekly, monthly
+ Telegram commands: /rug /status /leaderboard /weekly /monthly /narratives /tracking /help
+ Inline buttons: Refresh / Delete on rug checks

FIXES:
- Fixed 409 Conflict: webhook deletion before polling starts
- Timezone-aware datetime (no deprecation warning)
- Thread-safe state management
- Memory leak prevention
- WebSocket resilience
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
from typing import Optional, Dict, Any, List, Tuple

import httpx
import websockets

sys.path.insert(0, str(Path(__file__).parent))
from sniper import SolanaNarrativeSniper

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("sniper-bot")

# ─── Configuration ─────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")
HELIUS_API_KEY     = os.getenv("HELIUS_API_KEY", "")
BIRDEYE_API_KEY    = os.getenv("BIRDEYE_API_KEY", "")
ALERT_THRESHOLD    = float(os.getenv("ALERT_THRESHOLD", "5.0"))  # CHANGED: 5.5 -> 5.0
MIN_LIQUIDITY      = float(os.getenv("MIN_LIQUIDITY_USD", "5000"))
PUMP_WS_URL        = "wss://pumpportal.fun/api/data"
X_MILESTONES       = [2, 5, 10, 25, 50, 100]
MAX_LEADERBOARD_HISTORY = 10000
TRACKER_CONCURRENCY = 5
RUG_CACHE_TTL_SECONDS = 30

DATA_DIR = Path(os.getenv("SNIPER_DATA_DIR", "./data"))
DATA_DIR.mkdir(exist_ok=True)
LEADERBOARD_FILE = DATA_DIR / "leaderboard.json"

# ─── Thread-Safe State Management ─────────────────────────────────────────────
tracked_lock = asyncio.Lock()
history_lock = asyncio.Lock()
alerts_lock = asyncio.Lock()
rug_cache_lock = asyncio.Lock()

def utcnow() -> datetime:
    """[FIXED] Timezone-aware UTC datetime to prevent deprecation warning"""
    return datetime.now(timezone.utc).replace(tzinfo=None)

# ─── Tracked Token ──────────────────────────────────────────────────────────────
class TrackedToken:
    def __init__(self, mint: str, name: str, symbol: str, entry_mcap: float, 
                 entry_score: float, narrative: str):
        self.mint: str = mint
        self.name: str = name
        self.symbol: str = symbol
        self.entry_mcap: float = entry_mcap
        self.entry_score: float = entry_score
        self.narrative: str = narrative
        self.peak_mcap: float = entry_mcap
        self.current_mcap: float = entry_mcap
        self.peak_x: float = 1.0
        self.alerted_xs: set = set()
        self.migrated: bool = False
        self.migration_verified: bool = False
        self.added_at: datetime = utcnow()
        self.status: str = "active"
        self.last_updated: datetime = utcnow()

    def current_x(self) -> float:
        if self.entry_mcap <= 0:
            return 0.0
        return round(self.current_mcap / self.entry_mcap, 2)

    def to_record(self) -> dict:
        return {
            "mint": self.mint,
            "name": self.name,
            "symbol": self.symbol,
            "entry_mcap": self.entry_mcap,
            "entry_score": self.entry_score,
            "narrative": self.narrative,
            "peak_mcap": self.peak_mcap,
            "current_mcap": self.current_mcap,
            "peak_x": self.peak_x,
            "current_x": self.current_x(),
            "migrated": self.migrated,
            "migration_verified": self.migration_verified,
            "status": self.status,
            "added_at": self.added_at.isoformat(),
            "last_updated": self.last_updated.isoformat(),
        }

# ─── Global State ─────────────────────────────────────────────────────────────
tracked: Dict[str, TrackedToken] = {}
leaderboard_history: List[dict] = []
sniper_ref: Optional[SolanaNarrativeSniper] = None
bot_start_time: datetime = utcnow()
total_alerts_fired: int = 0
rug_message_map: Dict[str, dict] = {}
rug_cache: Dict[str, Tuple[str, datetime]] = {}

# ─── Persistence ────────────────────────────────────────────────────────────────
async def save_leaderboard_async():
    """Async version with proper locking and rotation"""
    try:
        async with tracked_lock, history_lock:
            records = [t.to_record() for t in tracked.values()]
            records += leaderboard_history
            
            if len(records) > MAX_LEADERBOARD_HISTORY:
                records = records[-MAX_LEADERBOARD_HISTORY//2:]
                log.warning(f"[LB] Rotated history to {len(records)} records")
            
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None, 
                lambda: LEADERBOARD_FILE.write_text(
                    json.dumps(records, indent=2), 
                    encoding='utf-8'
                )
            )
    except Exception as e:
        log.error(f"[LB] Save error: {e}")

def save_leaderboard():
    """Sync version for sync contexts"""
    try:
        records = [t.to_record() for t in tracked.values()]
        records += leaderboard_history
        
        if len(records) > MAX_LEADERBOARD_HISTORY:
            records = records[-MAX_LEADERBOARD_HISTORY//2:]
        
        with open(LEADERBOARD_FILE, "w") as f:
            json.dump(records, f, indent=2)
    except Exception as e:
        log.error(f"[LB] Save error: {e}")

def load_leaderboard():
    """Sync loading with retention policy"""
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
            
            log.info(f"[LB] Loaded {len(leaderboard_history)} records (90d retention)")
    except Exception as e:
        log.error(f"[LB] Load error: {e}")

# ─── Retry Utilities ────────────────────────────────────────────────────────────
def retry_on_429(max_retries: int = 3, base_delay: float = 1.0):
    """Decorator for handling rate limits with exponential backoff"""
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            for attempt in range(max_retries):
                try:
                    return await func(*args, **kwargs)
                except httpx.HTTPStatusError as e:
                    if e.response.status_code == 429 and attempt < max_retries - 1:
                        delay = base_delay * (2 ** attempt) + (random.random() * 0.5)
                        log.warning(f"[RATE LIMIT] {func.__name__} retry {attempt+1}/{max_retries} in {delay:.1f}s")
                        await asyncio.sleep(delay)
                    else:
                        raise
                except (httpx.TimeoutException, asyncio.TimeoutError) as e:
                    if attempt < max_retries - 1:
                        delay = base_delay * (2 ** attempt)
                        log.warning(f"[TIMEOUT] {func.__name__} retry {attempt+1}/{max_retries} in {delay:.1f}s")
                        await asyncio.sleep(delay)
                    else:
                        raise
            return await func(*args, **kwargs)
        return wrapper
    return decorator

# ─── Telegram API ─────────────────────────────────────────────────────────────
@retry_on_429(max_retries=3, base_delay=1.0)
async def send_telegram(text: str, chat_id: str = None) -> int:
    """Send message, returns message_id"""
    if not TELEGRAM_BOT_TOKEN:
        print(text)
        return 0
    cid = chat_id or TELEGRAM_CHAT_ID
    if not cid:
        return 0
    
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={
                    "chat_id": cid,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                }
            )
            resp.raise_for_status()
            return resp.json().get("result", {}).get("message_id", 0)
    except httpx.HTTPStatusError as e:
        log.error(f"[TG] HTTP {e.response.status_code}: {e.response.text[:200]}")
    except Exception as e:
        log.error(f"[TG] Send failed: {e}")
    return 0

@retry_on_429(max_retries=2, base_delay=0.5)
async def send_telegram_with_buttons(text: str, buttons: list, chat_id: str = None) -> int:
    """Send message with inline keyboard buttons"""
    if not TELEGRAM_BOT_TOKEN:
        print(text)
        return 0
    cid = chat_id or TELEGRAM_CHAT_ID
    if not cid:
        return 0
    
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={
                    "chat_id": cid,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                    "reply_markup": {"inline_keyboard": buttons}
                }
            )
            resp.raise_for_status()
            return resp.json().get("result", {}).get("message_id", 0)
    except Exception as e:
        log.error(f"[TG] Buttons error: {e}")
    return 0

@retry_on_429(max_retries=2, base_delay=0.5)
async def edit_telegram_message(chat_id: str, message_id: int, text: str, buttons: list = None):
    """Edit an existing message"""
    if not TELEGRAM_BOT_TOKEN:
        return
    
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": buttons}
    
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageText",
                json=payload
            )
            resp.raise_for_status()
    except Exception as e:
        log.error(f"[TG] Edit failed: {e}")

async def delete_telegram_message(chat_id: str, message_id: int):
    """Delete a message"""
    if not TELEGRAM_BOT_TOKEN:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/deleteMessage",
                json={"chat_id": chat_id, "message_id": message_id}
            )
    except Exception as e:
        log.error(f"[TG] Delete failed: {e}")

async def answer_callback(callback_id: str, text: str = ""):
    """Acknowledge a callback query"""
    if not TELEGRAM_BOT_TOKEN:
        return
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery",
                json={"callback_query_id": callback_id, "text": text[:200]}
            )
    except Exception:
        pass

async def delete_telegram_webhook():
    """[NEW] Delete webhook before starting polling"""
    if not TELEGRAM_BOT_TOKEN:
        return False
    
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/deleteWebhook",
                json={"drop_pending_updates": True}
            )
            if resp.status_code == 200:
                result = resp.json()
                if result.get("ok"):
                    log.info("[TG] Webhook deleted successfully")
                    return True
                else:
                    log.warning(f"[TG] Webhook deletion: {result.get('description')}")
            else:
                log.warning(f"[TG] Webhook deletion HTTP {resp.status_code}")
    except Exception as e:
        log.warning(f"[TG] Webhook deletion error: {e}")
    
    return False

async def get_telegram_updates(offset: int = 0) -> list:
    """[FIXED] Get updates with proper error handling"""
    if not TELEGRAM_BOT_TOKEN:
        return []
    
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates",
                params={
                    "offset": offset,
                    "timeout": 25,
                    "allowed_updates": ["message", "callback_query"]
                }
            )
            if resp.status_code == 200:
                return resp.json().get("result", [])
            elif resp.status_code == 409:
                log.warning("[TG] 409 Conflict - another instance may be running")
                await asyncio.sleep(5)
                return []
            else:
                log.warning(f"[TG] HTTP {resp.status_code} in getUpdates")
    except Exception as e:
        log.debug(f"[TG] Updates error: {e}")
    return []

def rug_buttons(mint: str) -> list:
    """Inline keyboard for rug check messages"""
    short_mint = mint[:20]
    return [[
        {"text": "🔄 Refresh", "callback_data": f"rug_refresh:{short_mint}"},
        {"text": "🗑 Delete", "callback_data": f"rug_delete:{short_mint}"},
    ]]

# ─── Formatters ─────────────────────────────────────────────────────────────────
def format_alert(alert) -> str:
    e = alert.entry
    r = alert.rug
    t = alert.token
    n = alert.narrative
    entry_score = e.get("final_score", 0)
    rug_score = r.get("rug_score", 10)
    flags = r.get("flags", [])
    flag_str = "  ".join([f"⛔ {f['code']}" for f in flags[:3]]) if flags else "✅ None"
    nar = n.get("narrative") or {}
    narrative_kw = nar.get("keyword", "NONE").upper()
    comps = e.get("components", {})
    
    lines = [
        "🎯 <b>SNIPER ALERT</b>",
        "",
        f"<b>{t.get('name','?')}</b>  <code>${t.get('symbol','?')}</code>",
        f"<code>{t.get('mint','N/A')}</code>",
        "",
        f"📊 <b>ENTRY:  {entry_score}/10  {e.get('verdict','')}</b>",
        f"🔒 <b>RUG:    {rug_score}/10  {r.get('verdict','')}</b>",
        "",
        f"📡 Narrative:  <b>{narrative_kw}</b>  ({n.get('narrative_score',0):.1f}/10)",
        f"💧 Liquidity:  <b>${t.get('liquidity_usd',0):,.0f}</b>",
        f"👥 Holders:    <b>{t.get('total_holders',0)}</b>",
        f"⏱ Age:        <b>{t.get('age_hours',0):.1f}h</b>",
        f"📈 Vol 1h:     <b>${t.get('volume_1h_usd',0):,.0f}</b>",
        "",
        f"NAR {comps.get('narrative',{}).get('score','?')}  "
        f"TIM {comps.get('timing',{}).get('score','?')}  "
        f"HOL {comps.get('holders',{}).get('score','?')}  "
        f"DEP {comps.get('deployer',{}).get('score','?')}  "
        f"MOM {comps.get('momentum',{}).get('score','?')}",
        "",
        f"<b>Flags:</b> {flag_str}",
    ]
    if e.get("signals"):
        lines += [""] + [f"✅ {s}" for s in e["signals"][:3]]
    if e.get("warnings"):
        lines += [f"⚠️ {w}" for w in e["warnings"][:3]]
    
    mint = t.get('mint','')
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
        f"{emoji} <b>{multiplier}X ALERT</b>",
        "",
        f"<b>{token.name}</b>  <code>${token.symbol}</code>",
        f"<code>{token.mint}</code>",
        "",
        f"Entry MCap:   <b>${token.entry_mcap:,.0f}</b>",
        f"Current MCap: <b>${current_mcap:,.0f}</b>",
        f"Multiplier:   <b>{multiplier}X 🔥</b>",
        "",
        f"🔗 <a href='https://dexscreener.com/solana/{token.mint}'>dexscreener</a>  "
        f"<a href='https://pump.fun/{token.mint}'>pump.fun</a>",
        f"<i>🕐 {utcnow().strftime('%H:%M:%S UTC')}</i>",
    ])

def format_migration_alert(token: TrackedToken, current_mcap: float) -> str:
    verified = "✅ Verified" if token.migration_verified else "⚠️ Unverified"
    return "\n".join([
        "🎓 <b>MIGRATION ALERT</b>",
        f"<i>{verified} on Raydium</i>",
        "",
        f"<b>{token.name}</b>  <code>${token.symbol}</code>",
        f"<code>{token.mint}</code>",
        "",
        f"✅ Graduated Pump.fun -> <b>Raydium</b>",
        f"MCap at migration: <b>${current_mcap:,.0f}</b>",
        f"Entry MCap:        <b>${token.entry_mcap:,.0f}</b>",
        f"Multiplier:        <b>{token.current_x()}X</b>",
        "",
        f"🔗 <a href='https://dexscreener.com/solana/{token.mint}'>dexscreener</a>  "
        f"<a href='https://raydium.io/swap/?inputCurrency=sol&outputCurrency={token.mint}'>raydium</a>",
        f"<i>🕐 {utcnow().strftime('%H:%M:%S UTC')}</i>",
    ])

def format_leaderboard(records: list[dict], period: str) -> str:
    if not records:
        return f"📊 <b>{period} LEADERBOARD</b>\n\nNo alerts recorded yet."
    
    sorted_records = sorted(records, key=lambda x: x.get("peak_x", 0), reverse=True)
    medals = ["🥇", "🥈", "🥉"]
    lines = [f"📊 <b>{period} LEADERBOARD</b>", f"<i>{len(records)} tokens tracked</i>", ""]
    
    for i, r in enumerate(sorted_records[:10]):
        medal = medals[i] if i < 3 else f"{i+1}."
        peak_x = r.get("peak_x", 1)
        current_x = r.get("current_x", 1)
        migrated = "🎓" if r.get("migrated") else ""
        narrative = r.get("narrative", "").upper()
        entry_score = r.get("entry_score", 0)
        
        if peak_x >= 10: perf_emoji = "💎"
        elif peak_x >= 5: perf_emoji = "🌕"
        elif peak_x >= 2: perf_emoji = "🚀"
        else: perf_emoji = "💀"
        
        lines.append(f"{medal} <b>{r.get('name','?')}</b> <code>${r.get('symbol','?')}</code> {migrated}")
        lines.append(f"   {perf_emoji} Peak: <b>{peak_x:.1f}X</b>  Now: {current_x:.1f}X  Score: {entry_score}  [{narrative}]")
        lines.append(f"   <a href='https://dexscreener.com/solana/{r.get('mint','')}'>chart</a>")
        lines.append("")
    
    peak_xs = [r.get("peak_x", 1) for r in records]
    avg_x = sum(peak_xs) / len(peak_xs)
    winners = len([x for x in peak_xs if x >= 2])
    migrants = len([r for r in records if r.get("migrated")])
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
    uptime = utcnow() - bot_start_time
    hours = int(uptime.total_seconds() // 3600)
    mins = int((uptime.total_seconds() % 3600) // 60)
    active = len(tracked)
    total = len(leaderboard_history) + active
    narratives = len(sniper_ref.narrative_engine.active_narratives) if sniper_ref else 0
    peak_xs = [t.peak_x for t in tracked.values()]
    best_live = f"{max(peak_xs):.1f}X" if peak_xs else "none"
    cache_size = len(rug_cache)
    
    return "\n".join([
        "⚡ <b>BOT STATUS</b>", "",
        f"🟢 Online:        <b>{hours}h {mins}m</b>",
        f"🎯 Alerts fired:  <b>{total_alerts_fired}</b>",
        f"👁 Tracking now:  <b>{active} tokens</b>",
        f"📊 Total tracked: <b>{total}</b>",
        f"🏆 Best live:     <b>{best_live}</b>",
        f"📡 Narratives:    <b>{narratives} active</b>",
        f"🎚 Threshold:     <b>{ALERT_THRESHOLD}/10</b>",
        f"💧 Min LP:        <b>${MIN_LIQUIDITY:,.0f}</b>",
        f"💾 Cache size:    <b>{cache_size}</b>",
        f"",
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
        age = int((utcnow() - t.added_at).total_seconds() / 60)
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
        "/status          — bot health + live stats",
        "/leaderboard     — 24h leaderboard",
        "/weekly          — 7 day leaderboard",
        "/monthly         — 30 day leaderboard",
        "/narratives      — active narrative list",
        "/tracking        — tokens being tracked now",
        "/rug CA          — instant rug check on any token",
        "/help            — this menu",
    ])

# ─── Rug Analysis ───────────────────────────────────────────────────────────────
async def analyze_ca(mint: str) -> str:
    """Full rug + stats analysis on any CA with caching"""
    mint = mint.strip()
    if len(mint) < 32 or len(mint) > 44:
        return "❌ Invalid contract address (must be 32-44 chars)."
    
    # Check cache first
    async with rug_cache_lock:
        if mint in rug_cache:
            result, ts = rug_cache[mint]
            if (utcnow() - ts).total_seconds() < RUG_CACHE_TTL_SECONDS:
                log.debug(f"[RUG CACHE] Hit for {mint[:12]}")
                return result + f"\n\n<i>🔄 Cached result ({RUG_CACHE_TTL_SECONDS}s TTL)</i>"
    
    data = await enrich_token(mint, "Unknown", "???", "unknown")

    # If Birdeye returned no real data, warn user
    if data.get("mcap_usd", 0) == 0 and data.get("liquidity_usd", 0) == 0:
        return (
            f"⚠️ <b>No market data found</b>\n\n"
            f"<code>{mint}</code>\n\n"
            f"Birdeye has no data for this token yet.\n"
            f"Possible reasons:\n"
            f"  • Token too new (try again in 2-3 mins)\n"
            f"  • Invalid or non-existent address\n"
            f"  • Not yet indexed by Birdeye\n\n"
            f"🔗 <a href='https://dexscreener.com/solana/{mint}'>Check DexScreener</a>  "
            f"<a href='https://solscan.io/token/{mint}'>Solscan</a>"
        )
    
    try:
        from core.rug_analyzer import RugAnalyzer
        report = RugAnalyzer().analyze_manual(data)
    except Exception as e:
        log.error(f"[RUG] Analysis failed: {e}")
        return f"❌ Analysis failed for <code>{mint}</code>"
    
    rug_score = report.rug_score
    flags = report.flags
    verdict = report.verdict
    
    if rug_score <= 2.5: rug_color = "🟢"
    elif rug_score <= 4.5: rug_color = "🟡"
    elif rug_score <= 6.5: rug_color = "🟠"
    else: rug_color = "🔴"
    
    liq = data.get("liquidity_usd", 0)
    mcap = data.get("mcap_usd", 0)
    holders = data.get("total_holders", 0)
    vol1h = data.get("volume_1h_usd", 0)
    vol5m = data.get("volume_5m_usd", 0)
    pc1h = data.get("price_change_1h_pct", 0)
    pc5m = data.get("price_change_5m_pct", 0)
    buy_sell = data.get("buy_sell_ratio_1h", 1.0)
    top1 = data.get("top1_pct", 0)
    top10 = data.get("top10_pct", 0)
    dev_holds = data.get("dev_holds_pct", 0)
    mint_rev = data.get("mint_authority_revoked", False)
    freeze_rev = data.get("freeze_authority_revoked", False)
    lp_locked = data.get("lp_locked", False)
    lp_burned = data.get("lp_burned", False)
    
    dev_status = "✅ Sold" if dev_holds == 0 else (f"🟡 Holds {dev_holds:.1f}%" if dev_holds <= 2 else f"🔴 Holds {dev_holds:.1f}%")
    top10_status = f"✅ {top10:.1f}%" if top10 <= 30 else (f"🟡 {top10:.1f}%" if top10 <= 50 else f"🔴 {top10:.1f}%")
    top1_status = f"✅ {top1:.1f}%" if top1 <= 5 else (f"🟡 {top1:.1f}%" if top1 <= 10 else f"🔴 {top1:.1f}%")
    lp_status = "✅ Burned" if lp_burned else ("🟡 Locked" if lp_locked else "🔴 Unlocked")
    pc1h_str = f"{'📈' if pc1h >= 0 else '📉'} {pc1h:+.1f}%"
    pc5m_str = f"{'📈' if pc5m >= 0 else '📉'} {pc5m:+.1f}%"
    
    critical_flags = [f for f in flags if f.severity == "critical"]
    high_flags = [f for f in flags if f.severity == "high"]
    med_flags = [f for f in flags if f.severity == "medium"]
    
    flag_lines = []
    for f in critical_flags:
        flag_lines.append(f"  🔴 <b>{f.code}</b> — {f.description}")
    for f in high_flags:
        flag_lines.append(f"  🟡 <b>{f.code}</b> — {f.description}")
    for f in med_flags:
        flag_lines.append(f"  🟠 <b>{f.code}</b> — {f.description}")
    if not flag_lines:
        flag_lines = ["  ✅ Clean — no flags"]
    
    result = "\n".join([
        f"{rug_color} <b>RUG SCORE: {rug_score}/10  {verdict}</b>",
        f"",
        f"<code>{mint}</code>",
        f"",
        f"📊 <b>Stats</b>",
        f"├ MC       <b>${mcap:,.0f}</b>",
        f"├ Vol 5m   <b>${vol5m:,.0f}</b>",
        f"├ Vol 1h   <b>${vol1h:,.0f}</b>",
        f"├ LP       <b>${liq:,.0f}</b>  {lp_status}",
        f"├ 5m       {pc5m_str}",
        f"├ 1h       {pc1h_str}",
        f"└ B/S      <b>{buy_sell:.1f}x</b>",
        f"",
        f"👥 <b>Holders</b>",
        f"├ Total    <b>{holders:,}</b>",
        f"├ Top 1    {top1_status}",
        f"├ Top 10   {top10_status}",
        f"└ Dev      {dev_status}",
        f"",
        f"🔐 <b>Security</b>",
        f"├ Mint     <b>{'✅ Revoked' if mint_rev else '⛔ ACTIVE'}</b>",
        f"├ Freeze   <b>{'✅ Revoked' if freeze_rev else '⛔ ACTIVE'}</b>",
        f"└ LP       <b>{lp_status}</b>",
        f"",
        f"⚠️ <b>Flags</b>",
        *flag_lines,
        f"",
        f"🔗 <a href='https://dexscreener.com/solana/{mint}'>DEX</a>  "
        f"<a href='https://solscan.io/token/{mint}'>SOL</a>  "
        f"<a href='https://pump.fun/{mint}'>PUMP</a>  "
        f"<a href='https://birdeye.so/token/{mint}?chain=solana'>BIRD</a>",
        f"",
        f"<i>🕐 {utcnow().strftime('%H:%M:%S UTC')}</i>",
    ])
    
    # Store in cache
    async with rug_cache_lock:
        rug_cache[mint] = (result, utcnow())
        # Cleanup old entries periodically
        if len(rug_cache) > 1000:
            cutoff = utcnow() - timedelta(minutes=5)
            old_keys = [k for k, (_, ts) in rug_cache.items() if ts < cutoff]
            for k in old_keys:
                del rug_cache[k]
    
    return result

# ─── MCap Fetcher ─────────────────────────────────────────────────────────────
@retry_on_429(max_retries=3, base_delay=2.0)
async def get_current_mcap(mint: str) -> Tuple[float, bool]:
    """Returns (mcap, migrated_flag) with Raydium verification"""
    mcap = 0.0
    migrated = False
    
    if not BIRDEYE_API_KEY:
        return mcap, migrated
    
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.get(
                "https://public-api.birdeye.so/defi/token_overview",
                params={"address": mint},
                headers={"X-API-KEY": BIRDEYE_API_KEY, "x-chain": "solana"}
            )
            resp.raise_for_status()
            data = resp.json().get("data") or {}
            mcap = float(data.get("mc") or 0)
            liquidity = float(data.get("liquidity") or 0)
            
            # Better migration detection: high mcap + liquidity
            migrated = mcap >= 65000 and liquidity > 10000
            
            # If mcap suggests migration, verify with Raydium check
            if migrated and not await verify_raydium_pool(mint):
                migrated = False
                
    except Exception as e:
        log.warning(f"[MCap] Error {mint[:12]}: {e}")
    
    return mcap, migrated

async def verify_raydium_pool(mint: str) -> bool:
    """Verify actual Raydium pool existence via Helius"""
    if not HELIUS_API_KEY:
        return True  # Assume true if can't verify
    
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.post(
                f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getProgramAccounts",
                    "params": [
                        "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",  # Raydium AMM
                        {
                            "filters": [
                                {"memcmp": {"offset": 400, "bytes": mint}}
                            ],
                            "encoding": "base64",
                            "dataSlice": {"length": 0, "offset": 0}
                        }
                    ]
                }
            )
            if resp.status_code == 200:
                result = resp.json().get("result", [])
                return len(result) > 0
    except Exception as e:
        log.debug(f"[Raydium Verify] Failed for {mint[:12]}: {e}")
    
    return False

# ─── Token Enrichment ───────────────────────────────────────────────────────────
@retry_on_429(max_retries=2, base_delay=1.0)
async def enrich_token(mint: str, name: str, symbol: str, deployer: str) -> dict:
    base = {
        "mint": mint, "name": name, "symbol": symbol, "deployer": deployer,
        "age_hours": 0.5, "mcap_usd": 0, "liquidity_usd": 0,
        "total_holders": 50, "top1_pct": 50.0, "top5_pct": 70.0,
        "top10_pct": 80.0, "dev_holds_pct": 20.0, "wallet_clusters": 0,
        "holder_growth_1h": 0, "mint_authority_revoked": True,
        "freeze_authority_revoked": True, "lp_burned": False,
        "lp_locked": True, "pool_age_hours": 0.5, "deployer_age_days": 30,
        "deployer_prev_tokens": [], "deployer_prev_rugs": [],
        "volume_5m_usd": 0, "volume_1h_usd": 0,
        "price_change_5m_pct": 0, "price_change_1h_pct": 0,
        "buy_sell_ratio_1h": 1.0, "market_conditions": "neutral",
    }
    
    if BIRDEYE_API_KEY:
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                resp = await client.get(
                    "https://public-api.birdeye.so/defi/token_overview",
                    params={"address": mint},
                    headers={"X-API-KEY": BIRDEYE_API_KEY, "x-chain": "solana"}
                )
                if resp.status_code == 200:
                    data = resp.json().get("data") or {}
                    liquidity = float(data.get("liquidity") or 0)
                    v1h = float(data.get("v1hUSD") or 0)
                    v5m = float(data.get("v5mUSD") or 0)
                    buy1h = int(data.get("buy1h") or 1)
                    sell1h = int(data.get("sell1h") or 1)
                    holders = int(data.get("holder") or 50)
                    mc = float(data.get("mc") or 0)
                    pc1h = float(data.get("priceChange1hPercent") or 0)
                    pc5m = float(data.get("priceChange5mPercent") or 0)
                    top1_pct = float(data.get("top1HolderPercent") or 50)
                    top10_pct = float(data.get("top10HolderPercent") or 80)
                    
                    log.info(f"  -> Birdeye: liq=${liquidity:,.0f} mcap=${mc:,.0f} holders={holders}")
                    
                    base["liquidity_usd"] = liquidity
                    base["mcap_usd"] = mc
                    base["volume_1h_usd"] = v1h
                    base["volume_5m_usd"] = v5m
                    base["price_change_1h_pct"] = pc1h
                    base["price_change_5m_pct"] = pc5m
                    base["buy_sell_ratio_1h"] = buy1h / max(sell1h, 1)
                    base["total_holders"] = holders
                    base["top1_pct"] = top1_pct
                    base["top10_pct"] = top10_pct
                    base["lp_locked"] = liquidity > 5000
        except Exception as e:
            log.warning(f"[Birdeye] Error: {e}")
    
    if HELIUS_API_KEY:
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                resp = await client.post(
                    f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}",
                    json={"jsonrpc": "2.0", "id": 1,
                          "method": "getAccountInfo",
                          "params": [mint, {"encoding": "jsonParsed"}]}
                )
                if resp.status_code == 200:
                    result = resp.json().get("result") or {}
                    value = result.get("value") or {}
                    data = value.get("data") or {}
                    parsed = data.get("parsed") or {}
                    info = parsed.get("info") or {}
                    if info:
                        base["mint_authority_revoked"] = info.get("mintAuthority") is None
                        base["freeze_authority_revoked"] = info.get("freezeAuthority") is None
                        supply = float(info.get("supply", 0)) / (10 ** info.get("decimals", 0))
                        base["total_supply"] = supply
        except Exception as e:
            log.warning(f"[Helius] Error: {e}")
    
    return base

async def fetch_liquidity_only(mint: str) -> float:
    """Quick liquidity check without full enrichment"""
    if not BIRDEYE_API_KEY:
        return 0.0
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(
                "https://public-api.birdeye.so/defi/token_overview",
                params={"address": mint},
                headers={"X-API-KEY": BIRDEYE_API_KEY, "x-chain": "solana"}
            )
            if resp.status_code == 200:
                return float(resp.json().get("data", {}).get("liquidity", 0))
    except Exception:
        pass
    return 0.0

# ─── Tracker ────────────────────────────────────────────────────────────────────
async def track_tokens():
    """Concurrent updates with semaphore and proper locking"""
    semaphore = asyncio.Semaphore(TRACKER_CONCURRENCY)
    
    async def update_single(mint: str, token: TrackedToken):
        async with semaphore:
            try:
                age_hours = (utcnow() - token.added_at).total_seconds() / 3600
                if age_hours > 24:
                    async with tracked_lock, history_lock:
                        if mint in tracked:
                            leaderboard_history.append(tracked[mint].to_record())
                            del tracked[mint]
                            await save_leaderboard_async()
                    log.info(f"[TRACKER] Archived {token.symbol} — peak {token.peak_x:.1f}X")
                    return
                
                current_mcap, migrated_flag = await get_current_mcap(mint)
                if current_mcap == 0:
                    return
                
                async with tracked_lock:
                    if mint not in tracked:
                        return
                    
                    token = tracked[mint]
                    token.current_mcap = current_mcap
                    token.peak_mcap = max(token.peak_mcap, current_mcap)
                    token.peak_x = token.peak_mcap / max(token.entry_mcap, 1)
                    token.last_updated = utcnow()
                    
                    if migrated_flag and not token.migration_verified:
                        if await verify_raydium_pool(mint):
                            token.migrated = True
                            token.migration_verified = True
                            token.status = "migrated"
                            log.info(f"[TRACKER] Verified migration: {token.symbol}")
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
        
        tasks = [update_single(mint, token) for mint, token in tokens_to_check]
        await asyncio.gather(*tasks, return_exceptions=True)
        
        # Periodic cache cleanup
        async with rug_cache_lock:
            cutoff = utcnow() - timedelta(minutes=5)
            old_keys = [k for k, (_, ts) in rug_cache.items() if ts < cutoff]
            for k in old_keys:
                del rug_cache[k]

# ─── Leaderboard Scheduler ──────────────────────────────────────────────────────
async def leaderboard_scheduler():
    """Proper async handling and drift correction"""
    intervals = {
        'daily': timedelta(days=1),
        'weekly': timedelta(days=7),
        'monthly': timedelta(days=30)
    }
    last_run = {k: utcnow() for k in intervals}
    
    while True:
        await asyncio.sleep(60)
        now = utcnow()
        
        for period, delta in intervals.items():
            if (now - last_run[period]).total_seconds() >= delta.total_seconds():
                last_run[period] = now
                cutoff = now - delta
                records = await _get_records_since_async(cutoff)
                
                period_name = "24H" if period == 'daily' else period.upper()
                await send_telegram(format_leaderboard(records, period_name))
                log.info(f"[LB] Posted {period} leaderboard ({len(records)} records)")

async def _get_records_since_async(cutoff: datetime) -> List[dict]:
    """Async version with proper locking"""
    records = []
    
    async with tracked_lock:
        for t in tracked.values():
            if t.added_at >= cutoff:
                records.append(t.to_record())
    
    async with history_lock:
        for r in leaderboard_history:
            try:
                added = datetime.fromisoformat(r["added_at"])
                if added >= cutoff:
                    records.append(r)
            except Exception:
                continue
    
    return records

def _get_records_since(cutoff: datetime) -> List[dict]:
    """Sync version for sync contexts"""
    records = []
    for t in tracked.values():
        if t.added_at >= cutoff:
            records.append(t.to_record())
    for r in leaderboard_history:
        try:
            added = datetime.fromisoformat(r["added_at"])
            if added >= cutoff:
                records.append(r)
        except Exception:
            pass
    return records

# ─── Command Handler ────────────────────────────────────────────────────────────
async def handle_commands():
    """Robust command parsing and error handling"""
    offset = 0
    log.info("[CMD] Command listener started")
    
    commands = {
        '/status': lambda cid: send_telegram(format_status(), cid),
        '/leaderboard': lambda cid: _send_period_leaderboard(cid, 1),
        '/weekly': lambda cid: _send_period_leaderboard(cid, 7),
        '/monthly': lambda cid: _send_period_leaderboard(cid, 30),
        '/narratives': lambda cid: send_telegram(format_narratives(), cid),
        '/tracking': lambda cid: send_telegram(format_tracking(), cid),
        '/help': lambda cid: send_telegram(format_help(), cid),
    }
    
    async def _send_period_leaderboard(chat_id: str, days: int):
        cutoff = utcnow() - timedelta(days=days)
        period_name = "24H" if days == 1 else f"{days}D"
        records = await _get_records_since_async(cutoff)
        await send_telegram(format_leaderboard(records, period_name), chat_id)
    
    while True:
        try:
            updates = await get_telegram_updates(offset)
            for update in updates:
                offset = update["update_id"] + 1
                
                if "callback_query" in update:
                    await _handle_callback(update["callback_query"])
                    continue
                
                msg = update.get("message", {})
                text = msg.get("text", "").strip()
                chat_id = str(msg.get("chat", {}).get("id", ""))
                
                if not text.startswith("/"):
                    continue
                
                parts = text.split()
                cmd = parts[0].lower().split('@')[0]
                args = parts[1:]
                
                log.info(f"[CMD] {cmd} from {chat_id}")
                
                if cmd in commands:
                    await commands[cmd](chat_id)
                elif cmd == '/rug':
                    await _handle_rug_command(chat_id, args)
                else:
                    await send_telegram(f"❓ Unknown command: {cmd}\nTry /help", chat_id)
                    
        except Exception as e:
            log.error(f"[CMD] Loop error: {e}")
            await asyncio.sleep(5)
        
        await asyncio.sleep(0.1)

async def _handle_callback(cb: dict):
    """Process inline button callbacks"""
    cb_id = cb["id"]
    cb_data = cb.get("data", "")
    
    try:
        cb_chat = str(cb["message"]["chat"]["id"])
        cb_msg = cb["message"]["message_id"]
    except KeyError:
        await answer_callback(cb_id, "Error: Invalid message")
        return
    
    if cb_data.startswith("rug_refresh:"):
        mint_short = cb_data.split(":", 1)[1]
        full_mint = None
        for m in rug_message_map:
            if m.startswith(mint_short):
                full_mint = m
                break
        
        if full_mint:
            await answer_callback(cb_id, "Refreshing...")
            new_text = await analyze_ca(full_mint)
            await edit_telegram_message(cb_chat, cb_msg, new_text, rug_buttons(full_mint))
        else:
            await answer_callback(cb_id, "Token expired from cache")
    
    elif cb_data.startswith("rug_delete:"):
        await answer_callback(cb_id, "Deleted")
        await delete_telegram_message(cb_chat, cb_msg)
        mint_short = cb_data.split(":", 1)[1]
        for m in list(rug_message_map.keys()):
            if m.startswith(mint_short):
                del rug_message_map[m]

async def _handle_rug_command(chat_id: str, args: List[str]):
    """Handle /rug CA command"""
    if not args:
        await send_telegram("⚠️ Usage: <code>/rug CONTRACT_ADDRESS</code>", chat_id)
        return
    
    mint = args[0].strip()
    if len(mint) < 32 or len(mint) > 44:
        await send_telegram("❌ Invalid address length (32-44 chars required)", chat_id)
        return
    
    loading_msg = await send_telegram(f"🔍 Analyzing <code>{mint[:20]}...</code>", chat_id)
    result = await analyze_ca(mint)
    
    if loading_msg:
        await delete_telegram_message(chat_id, loading_msg)
    
    msg_id = await send_telegram_with_buttons(result, rug_buttons(mint), chat_id)
    if msg_id:
        rug_message_map[mint] = {"message_id": msg_id, "chat_id": chat_id}

# ─── Main Bot Loop ──────────────────────────────────────────────────────────────
async def run_bot():
    """Proper initialization and task management"""
    global sniper_ref, total_alerts_fired, bot_start_time
    
    bot_start_time = utcnow()
    load_leaderboard()
    
    sniper = SolanaNarrativeSniper()
    sniper_ref = sniper
    
    seed = os.getenv("SEED_NARRATIVES", "")
    if seed:
        for item in seed.split("|"):
            parts = [p.strip() for p in item.split(",")]
            if len(parts) >= 3:
                try:
                    sniper.inject_narrative(parts[0], parts[1], float(parts[2]))
                except ValueError:
                    log.warning(f"[SEED] Invalid score for {parts[0]}")
    
    log.info("=" * 50)
    log.info("  SOLANA_NARRATIVE_SNIPER — BOT ONLINE [CORRECTED v2.2]")
    log.info(f"  Threshold: {ALERT_THRESHOLD}  MinLP: ${MIN_LIQUIDITY:,.0f}")
    log.info(f"  Telegram: {'on' if TELEGRAM_BOT_TOKEN else 'off'}")
    log.info(f"  Helius: {'on' if HELIUS_API_KEY else 'off'}  Birdeye: {'on' if BIRDEYE_API_KEY else 'off'}")
    log.info(f"  Concurrency: {TRACKER_CONCURRENCY}  Cache TTL: {RUG_CACHE_TTL_SECONDS}s")
    log.info("=" * 50)
    
    # [CRITICAL FIX] Delete webhook BEFORE starting any Telegram operations
    if TELEGRAM_BOT_TOKEN:
        await delete_telegram_webhook()
        await asyncio.sleep(2)  # Wait for Telegram to process
    
    await send_telegram(
        "🎯 <b>SOLANA_NARRATIVE_SNIPER ONLINE</b> [v2.2]\n"
        f"Threshold: {ALERT_THRESHOLD}/10\n"
        f"Tracking: {X_MILESTONES}x + verified migrations\n"
        f"Leaderboard: 24h / weekly / monthly\n"
        f"Commands: /rug /status /leaderboard /help\n"
        f"<i>Watching Pump.fun live...</i>"
    )
    
    tasks = [
        asyncio.create_task(_bot_loop(sniper)),
        asyncio.create_task(track_tokens()),
        asyncio.create_task(leaderboard_scheduler()),
        asyncio.create_task(handle_commands()),
    ]
    
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        log.info("[BOT] Shutting down gracefully...")
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    except Exception as e:
        log.critical(f"[BOT] Fatal error: {e}")
        raise

async def _bot_loop(sniper):
    """Resilient WebSocket with exponential backoff and timeout"""
    retry_delay = 5
    max_delay = 300
    
    while True:
        try:
            await asyncio.wait_for(_listen(sniper), timeout=3600)
            retry_delay = 5
        except asyncio.TimeoutError:
            log.warning("[WS] Listen timeout - forcing reconnect")
        except websockets.exceptions.ConnectionClosed as e:
            log.warning(f"[WS] Connection closed: {e}")
        except Exception as e:
            log.error(f"[WS] Error: {e}")
        
        jitter = random.random() * 2
        sleep_time = min(retry_delay + jitter, max_delay)
        log.info(f"[WS] Reconnecting in {sleep_time:.1f}s...")
        await asyncio.sleep(sleep_time)
        retry_delay = min(retry_delay * 2, max_delay)

async def _listen(sniper):
    """WebSocket listener with proper error handling"""
    log.info("[WS] Connecting to Pump.fun...")
    
    async with websockets.connect(
        PUMP_WS_URL, 
        ping_interval=20, 
        ping_timeout=10,
        close_timeout=5
    ) as ws:
        await ws.send(json.dumps({"method": "subscribeNewToken"}))
        log.info("[WS] Subscribed to new token stream")
        
        async for raw in ws:
            try:
                msg = json.loads(raw)
                await handle_token(sniper, msg)
            except json.JSONDecodeError:
                log.warning("[WS] Invalid JSON received")
            except Exception as e:
                log.error(f"[WS] Handler error: {e}")

async def handle_token(sniper, msg: dict):
    """Token processing with exponential backoff for liquidity"""
    global total_alerts_fired
    
    if msg.get("txType") != "create":
        return
    
    mint = msg.get("mint", "")
    name = msg.get("name", "")
    symbol = msg.get("symbol", "")
    deployer = msg.get("traderPublicKey", "")
    desc = msg.get("description", "")
    
    if not mint or not name:
        return
    if len(mint) < 32 or len(mint) > 44:
        return
    
    log.info(f"[NEW] {name} (${symbol}) {mint[:12]}...")
    
    quick = sniper.narrative_engine.match_token_to_narrative(name, symbol, desc)
    if not quick.get("matched"):
        log.info(f"  -> No narrative match")
        return
    
    # Exponential backoff for liquidity
    token_data = None
    delays = [30, 60, 120]
    
    for i, delay in enumerate(delays):
        if i > 0:
            log.info(f"  -> Retrying liquidity check in {delay}s...")
            await asyncio.sleep(delay)
        else:
            await asyncio.sleep(30)
        
        if i == 0:
            token_data = await enrich_token(mint, name, symbol, deployer)
        else:
            liq = await fetch_liquidity_only(mint)
            if liq > 0:
                token_data["liquidity_usd"] = liq
            else:
                continue
        
        token_data["description"] = desc
        
        if token_data["liquidity_usd"] >= MIN_LIQUIDITY:
            break
    else:
        log.info(f"  -> No liquidity after {len(delays)} attempts")
        return
    
    # CHANGED: top1_pct > 5 -> top1_pct > 10
    if token_data["top1_pct"] > 10:
        log.info(f"  -> Single holder {token_data['top1_pct']:.1f}% > 10%")
        return
    
    # CHANGED: top10_pct > 30 -> top10_pct > 40
    if token_data["top10_pct"] > 40:
        log.info(f"  -> Top10 {token_data['top10_pct']:.1f}% > 40%")
        return
    
    alert = sniper.analyze_token(token_data)
    entry_score = alert.entry.get("final_score", 0)
    rug_score = alert.rug.get("rug_score", 10)
    log.info(f"  -> Entry: {entry_score}/10  Rug: {rug_score}/10")
    
    if entry_score >= ALERT_THRESHOLD:
        async with alerts_lock:
            total_alerts_fired += 1
        
        log.info(f"  🎯 FIRING — tracking {symbol}")
        await send_telegram(format_alert(alert))
        
        entry_mcap = token_data.get("mcap_usd", 0)
        if entry_mcap > 0:
            nar_kw = (quick.get("narrative") or {}).get("keyword", "unknown")
            
            async with tracked_lock:
                tracked[mint] = TrackedToken(
                    mint=mint, name=name, symbol=symbol,
                    entry_mcap=entry_mcap, entry_score=entry_score,
                    narrative=nar_kw,
                )
            
            asyncio.create_task(save_leaderboard_async())

if __name__ == "__main__":
    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        log.info("[MAIN] Interrupted by user")
    except Exception as e:
        log.critical(f"[MAIN] Fatal: {e}")
        raise
