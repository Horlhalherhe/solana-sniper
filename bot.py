"""
SOLANA_NARRATIVE_SNIPER — BOT LOOP [v2.4]
Pump.fun -> score -> Telegram alerts
+ Post-alert tracker: X multipliers + migration
+ PnL Leaderboard: 24h, weekly, monthly
+ Telegram commands: /status /leaderboard /weekly /monthly /narratives /tracking /help

FIXES v2.4:
- DexScreener added as fallback when Birdeye returns 400
- DexScreener indexes pump.fun tokens within ~30s of launch (free, no key needed)
- Tokens skipped cleanly if both Birdeye AND DexScreener unavailable
- WS reconnect max 30s
- No crashes on Helius-only data
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
# Trading imports
try:
    from core.trader import SolanaTrader
    from core.position_manager import PositionManager
    from core.trading_config import TradingConfig
    from core.trading_engine import TradingEngine
    TRADING_AVAILABLE = True
except ImportError as e:
    log.warning(f"[TRADING] Modules not available: {e}")
    TRADING_AVAILABLE = False
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
# Trading configuration
SOLANA_PRIVATE_KEY = os.getenv("SOLANA_PRIVATE_KEY", "")
TRADING_ENABLED = TRADING_AVAILABLE and bool(SOLANA_PRIVATE_KEY)
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
# Trading globals
trading_engine: Optional['TradingEngine'] = None
trader: Optional['SolanaTrader'] = None
position_manager: Optional['PositionManager'] = None
trading_config: Optional['TradingConfig'] = None
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
                log.warning("[TG] 409 Conflict")
                await asyncio.sleep(5)
    except Exception as e:
        log.debug(f"[TG] Updates error: {e}")
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
    return "\n".join([
        "⚡ <b>BOT STATUS</b>", "",
        f"🟢 Online:        <b>{hours}h {mins}m</b>",
        f"🎯 Alerts fired:  <b>{total_alerts_fired}</b>",
        f"👁 Tracking now:  <b>{active} tokens</b>",
        f"📊 Total tracked: <b>{total}</b>",
        f"🏆 Best live:     <b>{best_live}</b>",
        f"📡 Narratives:    <b>{narratives} active</b>",
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
        "/status          — bot health + live stats",
        "/leaderboard     — 24h leaderboard",
        "/weekly          — 7 day leaderboard",
        "/monthly         — 30 day leaderboard",
        "/narratives      — active narrative list",
        "/tracking        — tokens being tracked now",
        "/help            — this menu",
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
async def monitor_positions():
    """Background task to monitor positions and execute exits"""
    if not TRADING_ENABLED or not trading_engine:
        return
    
    log.info("[TRADING] Position monitor started")
    
    while True:
        try:
            await asyncio.sleep(30)  # Check every 30 seconds
            
            active_positions = position_manager.get_active_positions()
            if not active_positions:
                continue
            
            for pos in active_positions:
                try:
                    # Get current price
                    current_mcap, _ = await get_current_mcap(pos.mint)
                    if current_mcap == 0:
                        continue
                    
                    # Calculate current price per token
                    current_price_sol = (current_mcap / 1_000_000_000) if current_mcap > 0 else pos.entry_price_sol
                    
                    # Check exit conditions
                    exit_msg = await trading_engine.check_and_execute_exits(
                        mint=pos.mint,
                        current_price_sol=current_price_sol,
                        current_mcap=current_mcap
                    )
                    
                    if exit_msg:
                        await send_telegram(exit_msg)
                        log.info(f"[TRADING] Exit executed: {pos.symbol}")
                
                except Exception as e:
                    log.error(f"[TRADING] Position check error {pos.symbol}: {e}")
        
        except Exception as e:
            log.error(f"[TRADING] Monitor loop error: {e}")
            await asyncio.sleep(5)
# ─── Command Handler ───────────────────────────────────────────────────────────
async def handle_trading_command(chat_id: str, args: str):
    """Handle /trading subcommands"""
    if not TRADING_ENABLED:
        await send_telegram("⚠️ Trading not enabled", chat_id)
        return
    
    parts = args.strip().split()
    if not parts:
        # /trading with no args = status
        await send_trading_status(chat_id)
        return
    
    subcmd = parts[0].lower()
    
    if subcmd == "status":
        await send_trading_status(chat_id)
    elif subcmd == "positions":
        await send_positions_list(chat_id)
    elif subcmd == "pnl":
        await send_pnl_summary(chat_id)
    elif subcmd == "pause":
        trading_config.auto_buy_enabled = False
        trading_config.save()
        await send_telegram("⏸ Auto-buy PAUSED", chat_id)
    elif subcmd == "resume":
        trading_config.auto_buy_enabled = True
        trading_config.save()
        await send_telegram("▶️ Auto-buy RESUMED", chat_id)
    else:
        await send_telegram(f"Unknown /trading command: {subcmd}\nTry /trading status", chat_id)

async def send_trading_status(chat_id: str):
    """Send trading status"""
    status = trading_engine.get_status_summary()
    
    mode = "📝 PAPER" if status["paper_mode"] else "💰 LIVE"
    auto = "✅ ON" if status["auto_buy"] else "⏸ PAUSED"
    
    msg = f"🤖 <b>TRADING STATUS</b> {mode}\n\n"
    msg += f"Auto-buy: {auto}\n"
    msg += f"Positions: {status['active_positions']}/{status['max_positions']}\n"
    msg += f"Deployed: {status['total_deployed_sol']:.3f} SOL\n"
    msg += f"Buy amount: {status['buy_amount_sol']} SOL\n\n"
    msg += f"<b>PnL</b>\n"
    msg += f"Total: {status['total_pnl_sol']:+.4f} SOL\n"
    msg += f"Today: {status['daily_pnl_sol']:+.4f} SOL\n\n"
    msg += f"Daily buys: {status['daily_buys']}/{status['max_daily_buys']}"
    
    await send_telegram(msg, chat_id)

async def send_positions_list(chat_id: str):
    """Send list of active positions"""
    positions = position_manager.get_active_positions()
    
    if not positions:
        await send_telegram("No active positions", chat_id)
        return
    
    msg = f"📊 <b>OPEN POSITIONS ({len(positions)})</b>\n\n"
    
    for pos in sorted(positions, key=lambda p: p.current_x, reverse=True):
        pnl_emoji = "🟢" if pos.total_pnl_sol() >= 0 else "🔴"
        msg += f"{pnl_emoji} <b>{pos.name}</b> <code>${pos.symbol}</code>\n"
        msg += f"   Entry: {pos.entry_sol} SOL @ {pos.current_x:.2f}X\n"
        msg += f"   PnL: {pos.pnl_pct():+.1f}% ({pos.total_pnl_sol():+.4f} SOL)\n"
        msg += f"   Age: {pos.age_hours():.1f}h | Peak: {pos.peak_x:.2f}X\n\n"
    
    await send_telegram(msg, chat_id)

async def send_pnl_summary(chat_id: str):
    """Send PnL summary"""
    total_pnl = position_manager.get_total_pnl()
    daily_pnl = position_manager.get_daily_pnl()
    
    all_positions = position_manager.get_all_positions()
    total_deployed = sum(p.entry_sol for p in all_positions)
    
    winners = len([p for p in all_positions if p.total_pnl_sol() > 0])
    losers = len([p for p in all_positions if p.total_pnl_sol() < 0])
    
    win_rate = (winners / len(all_positions) * 100) if all_positions else 0
    
    msg = f"💰 <b>PnL SUMMARY</b>\n\n"
    msg += f"Total PnL: <b>{total_pnl:+.4f} SOL</b>\n"
    msg += f"Daily PnL: <b>{daily_pnl:+.4f} SOL</b>\n\n"
    msg += f"Total traded: {total_deployed:.3f} SOL\n"
    msg += f"Win rate: {win_rate:.1f}% ({winners}W {losers}L)\n"
    msg += f"Positions: {len(all_positions)} total"
    
    await send_telegram(msg, chat_id)
async def handle_commands():
    offset = 0
    log.info("[CMD] Command listener started")

    async def _send_leaderboard(chat_id: str, days: int):
        records = await _get_records_since_async(utcnow() - timedelta(days=days))
        period_name = "24H" if days == 1 else f"{days}D"
        await send_telegram(format_leaderboard(records, period_name), chat_id)

    commands = {
        '/status':      lambda cid: send_telegram(format_status(), cid),
        '/leaderboard': lambda cid: _send_leaderboard(cid, 1),
        '/weekly':      lambda cid: _send_leaderboard(cid, 7),
        '/monthly':     lambda cid: _send_leaderboard(cid, 30),
        '/narratives':  lambda cid: send_telegram(format_narratives(), cid),
        '/tracking':    lambda cid: send_telegram(format_tracking(), cid),
        '/help':        lambda cid: send_telegram(format_help(), cid),
    }

    while True:
        try:
            updates = await get_telegram_updates(offset)
            for update in updates:
                offset  = update["update_id"] + 1
                msg     = update.get("message", {})
                text    = msg.get("text", "").strip()
                chat_id = str(msg.get("chat", {}).get("id", ""))
                if not text.startswith("/"): continue
                
                # Parse command and args
                parts = text.split(maxsplit=1)
                cmd = parts[0].lower().split('@')[0]
                args = parts[1] if len(parts) > 1 else ""
                
                log.info(f"[CMD] {cmd} from user")
                
                # Handle /trading specially (has subcommands)
                if cmd == "/trading":
                    await handle_trading_command(chat_id, args)
                    continue
                if cmd in commands:
                    await commands[cmd](chat_id)
                else:
                    await send_telegram(f"❓ Unknown: {cmd}\nTry /help", chat_id)
        except Exception as e:
            log.error(f"[CMD] Loop error: {e}")
            await asyncio.sleep(5)
        await asyncio.sleep(0.1)

# ─── Main Bot Loop ─────────────────────────────────────────────────────────────
async def run_bot():
    global sniper_ref, total_alerts_fired, bot_start_time
    bot_start_time = utcnow()
    load_leaderboard()
# Initialize trading if enabled
    global trading_engine, trader, position_manager, trading_config
    
    if TRADING_ENABLED and SOLANA_PRIVATE_KEY:
        try:
            trader = SolanaTrader(SOLANA_PRIVATE_KEY)
            position_manager = PositionManager()
            trading_config = TradingConfig.load()
            trading_engine = TradingEngine(trader, position_manager, trading_config)
            
            log.info(f"[TRADING] Initialized - Paper mode: {trading_config.paper_trading_mode}")
            log.info(f"[TRADING] Wallet: {trader.wallet_address}")
            log.info(f"[TRADING] Auto-buy: {'ON' if trading_config.auto_buy_enabled else 'OFF'}")
        except Exception as e:
            log.error(f"[TRADING] Initialization failed: {e}")
            TRADING_ENABLED = False
    else:
        log.info("[TRADING] Disabled - no private key configured")
    sniper     = SolanaNarrativeSniper()
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
    log.info("  TekkiSniPer — BOT ONLINE [v2.4]")
    log.info(f"  Threshold: {ALERT_THRESHOLD}  MinLP: ${MIN_LIQUIDITY:,.0f}  MaxMCap: ${MAX_ENTRY_MCAP:,.0f}")
    log.info(f"  Data: Birdeye -> DexScreener fallback")
    log.info(f"  Holder filters: top1 < 10%  top10 < 40%")
    log.info(f"  WS reconnect max: 30s")
    log.info(f"  Telegram: {'on' if TELEGRAM_BOT_TOKEN else 'off'} ({len(CHAT_IDS)} channel{'s' if len(CHAT_IDS) != 1 else ''})")
    log.info(f"  Helius: {'on' if HELIUS_API_KEY else 'off'}  Birdeye: {'on' if BIRDEYE_API_KEY else 'off'}")
    log.info("=" * 50)

    if TELEGRAM_BOT_TOKEN:
        await delete_telegram_webhook()
        await asyncio.sleep(2)

    await send_telegram(
        "🎯 <b>TekkiSniPer ONLINE</b> [v2.4]\n"
        f"Threshold: {ALERT_THRESHOLD}/10  |  Min LP: ${MIN_LIQUIDITY:,.0f}  |  Max MCap: ${MAX_ENTRY_MCAP:,.0f}\n"
        f"Data: Birdeye → DexScreener fallback\n"
        f"Commands: /status /leaderboard /narratives /tracking /help\n"
        f"<i>Watching Pump.fun live...</i>"
    )

    tasks = [
        asyncio.create_task(_bot_loop(sniper)),
        asyncio.create_task(track_tokens()),
        asyncio.create_task(leaderboard_scheduler()),
        asyncio.create_task(handle_commands()),
    ]
    
    # Add trading monitor if enabled
    if TRADING_ENABLED and trading_engine:
        tasks.append(asyncio.create_task(monitor_positions()))
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        log.info("[BOT] Shutting down...")
        for t in tasks: t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    except Exception as e:
        log.critical(f"[BOT] Fatal: {e}"); raise

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

    quick = sniper.narrative_engine.match_token_to_narrative(name, symbol, desc)
    if not quick.get("matched"):
        log.info(f"  -> No narrative match")
        return

    # Wait 90s then fetch (gives pools time to form and Birdeye time to index)
    await asyncio.sleep(90)
    token_data, source = await enrich_token(mint, name, symbol, deployer)
    token_data["description"] = desc

    # If no market data yet, wait 60s and try once more
    if source == "none":
        log.info(f"  -> No market data yet — retrying in 60s...")
        await asyncio.sleep(60)
        token_data, source = await enrich_token(mint, name, symbol, deployer)
        token_data["description"] = desc

    # Skip if still no data from either source
    if source == "none":
        log.info(f"  -> No data from Birdeye or DexScreener — skip")
        return

    log.info(f"  -> Source: {source}  liq=${token_data['liquidity_usd']:,.0f}  mcap=${token_data['mcap_usd']:,.0f}")

    # Liquidity filter
    if token_data["liquidity_usd"] < MIN_LIQUIDITY:
        log.info(f"  -> LP ${token_data['liquidity_usd']:,.0f} < ${MIN_LIQUIDITY:,.0f} — skip")
        return
    
    # MCap filter (prefer micro caps with more upside)
    if token_data.get("mcap_usd", 0) > MAX_ENTRY_MCAP:
        log.info(f"  -> MCap ${token_data['mcap_usd']:,.0f} > ${MAX_ENTRY_MCAP:,.0f} — skip")
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

    alert       = sniper.analyze_token(token_data)
    entry_score = alert.entry.get("final_score", 0)
    rug_score   = alert.rug.get("rug_score", 10)
    log.info(f"  -> Entry: {entry_score}/10  Rug: {rug_score}/10  {alert.entry.get('verdict','')}")

    if entry_score >= ALERT_THRESHOLD:
        async with alerts_lock:
            total_alerts_fired += 1
        log.info(f"  🎯 FIRING — {name} (${symbol})  [{source}]")
        await send_telegram(format_alert(alert))
        # Auto-buy if trading enabled
        if TRADING_ENABLED and trading_engine:
            try:
                success, trade_msg, position = await trading_engine.execute_buy(
                    mint=mint,
                    name=name,
                    symbol=symbol,
                    alert_mcap=token_data.get("mcap_usd", 0)
                )
                if success and trade_msg:
                    await send_telegram(trade_msg)
            except Exception as e:
                log.error(f"[TRADING] Auto-buy failed: {e}")
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
        log.info("[MAIN] Interrupted")
    except Exception as e:
        log.critical(f"[MAIN] Fatal: {e}"); raise
