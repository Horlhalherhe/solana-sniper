"""
SOLANA_NARRATIVE_SNIPER — BOT LOOP
Pump.fun -> score -> Telegram alerts
+ Post-alert tracker: X multipliers + migration
+ PnL Leaderboard: 24h, weekly, monthly
+ Telegram commands: /rug /status /leaderboard /weekly /monthly /narratives /tracking /help
+ Inline buttons: Refresh / Delete on rug checks
"""

import asyncio
import json
import os
import sys
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

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

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")
HELIUS_API_KEY     = os.getenv("HELIUS_API_KEY", "")
BIRDEYE_API_KEY    = os.getenv("BIRDEYE_API_KEY", "")
ALERT_THRESHOLD    = float(os.getenv("ALERT_THRESHOLD", "5.5"))
MIN_LIQUIDITY      = float(os.getenv("MIN_LIQUIDITY_USD", "5000"))
PUMP_WS_URL        = "wss://pumpportal.fun/api/data"
X_MILESTONES       = [2, 5, 10, 25, 50, 100]

DATA_DIR = Path(os.getenv("SNIPER_DATA_DIR", "./data"))
DATA_DIR.mkdir(exist_ok=True)
LEADERBOARD_FILE = DATA_DIR / "leaderboard.json"

def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ─── Tracked Token ────────────────────────────────────────────────────────────

class TrackedToken:
    def __init__(self, mint, name, symbol, entry_mcap, entry_score, narrative):
        self.mint         = mint
        self.name         = name
        self.symbol       = symbol
        self.entry_mcap   = entry_mcap
        self.entry_score  = entry_score
        self.narrative    = narrative
        self.peak_mcap    = entry_mcap
        self.current_mcap = entry_mcap
        self.peak_x       = 1.0
        self.alerted_xs   = set()
        self.migrated     = False
        self.added_at     = utcnow()
        self.status       = "active"

    def current_x(self):
        if self.entry_mcap <= 0:
            return 0
        return round(self.current_mcap / self.entry_mcap, 2)

    def to_record(self):
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
            "status": self.status,
            "added_at": self.added_at.isoformat(),
        }


tracked: dict[str, TrackedToken] = {}
leaderboard_history: list[dict] = []
sniper_ref: SolanaNarrativeSniper = None
bot_start_time = utcnow()
total_alerts_fired = 0

# mint -> message_id mapping for rug check refresh
rug_message_map: dict[str, dict] = {}   # mint -> {message_id, chat_id}


# ─── Persistence ──────────────────────────────────────────────────────────────

def save_leaderboard():
    try:
        records = [t.to_record() for t in tracked.values()]
        records += leaderboard_history
        with open(LEADERBOARD_FILE, "w") as f:
            json.dump(records, f, indent=2)
    except Exception as e:
        log.error(f"Leaderboard save error: {e}")

def load_leaderboard():
    global leaderboard_history
    try:
        if LEADERBOARD_FILE.exists():
            with open(LEADERBOARD_FILE) as f:
                leaderboard_history = json.load(f)
            log.info(f"[LB] Loaded {len(leaderboard_history)} historical records")
    except Exception as e:
        log.error(f"Leaderboard load error: {e}")


# ─── Telegram API ─────────────────────────────────────────────────────────────

async def send_telegram(text: str, chat_id: str = None) -> int:
    """Send message, returns message_id."""
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
            if resp.status_code == 200:
                return resp.json().get("result", {}).get("message_id", 0)
            else:
                log.error(f"Telegram error: {resp.text}")
    except Exception as e:
        log.error(f"Telegram send failed: {e}")
    return 0


async def send_telegram_with_buttons(text: str, buttons: list, chat_id: str = None) -> int:
    """Send message with inline keyboard buttons, returns message_id."""
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
                    "reply_markup": {
                        "inline_keyboard": buttons
                    }
                }
            )
            if resp.status_code == 200:
                return resp.json().get("result", {}).get("message_id", 0)
            else:
                log.error(f"Telegram buttons error: {resp.text}")
    except Exception as e:
        log.error(f"Telegram send failed: {e}")
    return 0


async def edit_telegram_message(chat_id: str, message_id: int, text: str, buttons: list = None):
    """Edit an existing message."""
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
            await client.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageText",
                json=payload
            )
    except Exception as e:
        log.error(f"Edit message failed: {e}")


async def delete_telegram_message(chat_id: str, message_id: int):
    """Delete a message."""
    if not TELEGRAM_BOT_TOKEN:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/deleteMessage",
                json={"chat_id": chat_id, "message_id": message_id}
            )
    except Exception as e:
        log.error(f"Delete message failed: {e}")


async def answer_callback(callback_id: str, text: str = ""):
    """Acknowledge a callback query."""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery",
                json={"callback_query_id": callback_id, "text": text}
            )
    except Exception:
        pass


async def get_telegram_updates(offset: int = 0) -> list:
    if not TELEGRAM_BOT_TOKEN:
        return []
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates",
                params={
                    "offset": offset,
                    "timeout": 5,
                    "allowed_updates": ["message", "callback_query"]
                }
            )
            if resp.status_code == 200:
                return resp.json().get("result", [])
    except Exception:
        pass
    return []


def rug_buttons(mint: str) -> list:
    """Inline keyboard for rug check messages."""
    return [[
        {"text": "🔄 Refresh", "callback_data": f"rug_refresh:{mint}"},
        {"text": "🗑 Delete",  "callback_data": f"rug_delete:{mint}"},
    ]]


# ─── Formatters ───────────────────────────────────────────────────────────────

def format_alert(alert) -> str:
    e = alert.entry
    r = alert.rug
    t = alert.token
    n = alert.narrative
    entry_score  = e.get("final_score", 0)
    rug_score    = r.get("rug_score", 10)
    flags        = r.get("flags", [])
    flag_str     = "  ".join([f"⛔ {f['code']}" for f in flags[:3]]) if flags else "✅ None"
    nar          = n.get("narrative") or {}
    narrative_kw = nar.get("keyword", "NONE").upper()
    comps        = e.get("components", {})
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
    lines += [
        "",
        f"🔗 <a href='https://pump.fun/{t.get('mint','')}'>pump.fun</a>  "
        f"<a href='https://dexscreener.com/solana/{t.get('mint','')}'>dexscreener</a>  "
        f"<a href='https://solscan.io/token/{t.get('mint','')}'>solscan</a>",
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
    return "\n".join([
        "🎓 <b>MIGRATION ALERT</b>",
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
    lines  = [f"📊 <b>{period} LEADERBOARD</b>", f"<i>{len(records)} tokens tracked</i>", ""]
    for i, r in enumerate(sorted_records[:10]):
        medal      = medals[i] if i < 3 else f"{i+1}."
        peak_x     = r.get("peak_x", 1)
        current_x  = r.get("current_x", 1)
        migrated   = "🎓" if r.get("migrated") else ""
        narrative  = r.get("narrative", "").upper()
        entry_score = r.get("entry_score", 0)
        if peak_x >= 10:    perf_emoji = "💎"
        elif peak_x >= 5:   perf_emoji = "🌕"
        elif peak_x >= 2:   perf_emoji = "🚀"
        else:               perf_emoji = "💀"
        lines.append(f"{medal} <b>{r.get('name','?')}</b> <code>${r.get('symbol','?')}</code> {migrated}")
        lines.append(f"   {perf_emoji} Peak: <b>{peak_x:.1f}X</b>  Now: {current_x:.1f}X  Score: {entry_score}  [{narrative}]")
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
        f"💧 Min LP:        <b>${MIN_LIQUIDITY:,.0f}</b>",
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
        age      = int((utcnow() - t.added_at).total_seconds() / 60)
        migrated = "🎓" if t.migrated else ""
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


# ─── Rug Analysis ─────────────────────────────────────────────────────────────

async def analyze_ca(mint: str) -> str:
    """Full rug + stats analysis on any CA."""
    if len(mint) < 32 or len(mint) > 44:
        return "❌ Invalid contract address."

    data = await enrich_token(mint, "Unknown", "???", "unknown")

    from core.rug_analyzer import RugAnalyzer
    report = RugAnalyzer().analyze_manual(data)

    rug_score  = report.rug_score
    flags      = report.flags
    verdict    = report.verdict

    if rug_score <= 2.5:    rug_color = "🟢"
    elif rug_score <= 4.5:  rug_color = "🟡"
    elif rug_score <= 6.5:  rug_color = "🟠"
    else:                   rug_color = "🔴"

    liq        = data.get("liquidity_usd", 0)
    mcap       = data.get("mcap_usd", 0)
    holders    = data.get("total_holders", 0)
    vol1h      = data.get("volume_1h_usd", 0)
    vol5m      = data.get("volume_5m_usd", 0)
    pc1h       = data.get("price_change_1h_pct", 0)
    pc5m       = data.get("price_change_5m_pct", 0)
    buy_sell   = data.get("buy_sell_ratio_1h", 1.0)
    top1       = data.get("top1_pct", 0)
    top10      = data.get("top10_pct", 0)
    dev_holds  = data.get("dev_holds_pct", 0)
    mint_rev   = data.get("mint_authority_revoked", False)
    freeze_rev = data.get("freeze_authority_revoked", False)
    lp_locked  = data.get("lp_locked", False)
    lp_burned  = data.get("lp_burned", False)

    dev_status  = "✅ Sold" if dev_holds == 0 else (f"🟡 Holds {dev_holds:.1f}%" if dev_holds <= 2 else f"🔴 Holds {dev_holds:.1f}%")
    top10_status = f"✅ {top10:.1f}%" if top10 <= 30 else (f"🟡 {top10:.1f}%" if top10 <= 50 else f"🔴 {top10:.1f}%")
    top1_status  = f"✅ {top1:.1f}%" if top1 <= 5 else (f"🟡 {top1:.1f}%" if top1 <= 10 else f"🔴 {top1:.1f}%")
    lp_status    = "✅ Burned" if lp_burned else ("🟡 Locked" if lp_locked else "🔴 Unlocked")
    pc1h_str     = f"{'📈' if pc1h >= 0 else '📉'} {pc1h:+.1f}%"
    pc5m_str     = f"{'📈' if pc5m >= 0 else '📉'} {pc5m:+.1f}%"

    critical_flags = [f for f in flags if f.severity == "critical"]
    high_flags     = [f for f in flags if f.severity == "high"]
    med_flags      = [f for f in flags if f.severity == "medium"]

    flag_lines = []
    for f in critical_flags:
        flag_lines.append(f"  🔴 <b>{f.code}</b> — {f.description}")
    for f in high_flags:
        flag_lines.append(f"  🟡 <b>{f.code}</b> — {f.description}")
    for f in med_flags:
        flag_lines.append(f"  🟠 <b>{f.code}</b> — {f.description}")
    if not flag_lines:
        flag_lines = ["  ✅ Clean — no flags"]

    lines = [
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
    ]
    return "\n".join(lines)


# ─── MCap Fetcher ─────────────────────────────────────────────────────────────

async def get_current_mcap(mint: str) -> tuple[float, bool]:
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
            if resp.status_code == 200:
                data     = resp.json().get("data") or {}
                mcap     = float(data.get("mc") or 0)
                migrated = mcap >= 65000
    except Exception as e:
        log.warning(f"MCap check error {mint}: {e}")
    return mcap, migrated


# ─── Token Enrichment ─────────────────────────────────────────────────────────

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
                    data      = resp.json().get("data") or {}
                    liquidity = float(data.get("liquidity") or 0)
                    v1h       = float(data.get("v1hUSD") or 0)
                    v5m       = float(data.get("v5mUSD") or 0)
                    buy1h     = int(data.get("buy1h") or 1)
                    sell1h    = int(data.get("sell1h") or 1)
                    holders   = int(data.get("holder") or 50)
                    mc        = float(data.get("mc") or 0)
                    pc1h      = float(data.get("priceChange1hPercent") or 0)
                    pc5m      = float(data.get("priceChange5mPercent") or 0)
                    log.info(f"  -> Birdeye: liq=${liquidity:,.0f} vol1h=${v1h:,.0f} holders={holders}")
                    base["liquidity_usd"]       = liquidity
                    base["mcap_usd"]            = mc
                    base["volume_1h_usd"]       = v1h
                    base["volume_5m_usd"]       = v5m
                    base["price_change_1h_pct"] = pc1h
                    base["price_change_5m_pct"] = pc5m
                    base["buy_sell_ratio_1h"]   = buy1h / max(sell1h, 1)
                    base["total_holders"]       = holders
                    base["lp_locked"]           = liquidity > 5000
        except Exception as e:
            log.warning(f"Birdeye error: {e}")
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
                    value  = result.get("value") or {}
                    data   = value.get("data") or {}
                    parsed = data.get("parsed") or {}
                    info   = parsed.get("info") or {}
                    if info:
                        base["mint_authority_revoked"]   = info.get("mintAuthority") is None
                        base["freeze_authority_revoked"] = info.get("freezeAuthority") is None
        except Exception as e:
            log.warning(f"Helius error: {e}")
    return base


# ─── Tracker ──────────────────────────────────────────────────────────────────

async def track_tokens():
    while True:
        await asyncio.sleep(120)
        if not tracked:
            continue
        for mint in list(tracked.keys()):
            token = tracked[mint]
            age_hours = (utcnow() - token.added_at).total_seconds() / 3600
            if age_hours > 24:
                leaderboard_history.append(token.to_record())
                del tracked[mint]
                save_leaderboard()
                log.info(f"[TRACKER] Archived {token.symbol} — peak {token.peak_x:.1f}X")
                continue
            try:
                current_mcap, migrated = await get_current_mcap(mint)
                if current_mcap == 0:
                    continue
                token.current_mcap = current_mcap
                token.peak_mcap    = max(token.peak_mcap, current_mcap)
                token.peak_x       = token.peak_mcap / max(token.entry_mcap, 1)
                if migrated and not token.migrated:
                    token.migrated = True
                    token.status   = "migrated"
                    log.info(f"[TRACKER] MIGRATION: {token.symbol}")
                    await send_telegram(format_migration_alert(token, current_mcap))
                if token.entry_mcap > 0:
                    multiplier = current_mcap / token.entry_mcap
                    for x in X_MILESTONES:
                        if multiplier >= x and x not in token.alerted_xs:
                            token.alerted_xs.add(x)
                            log.info(f"[TRACKER] {x}X: {token.symbol}")
                            await send_telegram(format_x_alert(token, current_mcap, x))
            except Exception as e:
                log.error(f"[TRACKER] Error {mint}: {e}")
            await asyncio.sleep(1)


# ─── Leaderboard Scheduler ────────────────────────────────────────────────────

async def leaderboard_scheduler():
    last_daily   = utcnow()
    last_weekly  = utcnow()
    last_monthly = utcnow()
    while True:
        await asyncio.sleep(60)
        now = utcnow()
        if (now - last_daily).total_seconds() >= 86400:
            last_daily = now
            records = _get_records_since(now - timedelta(days=1))
            await send_telegram(format_leaderboard(records, "24H"))
            log.info("[LB] Posted 24h leaderboard")
        if (now - last_weekly).total_seconds() >= 604800:
            last_weekly = now
            records = _get_records_since(now - timedelta(days=7))
            await send_telegram(format_leaderboard(records, "WEEKLY"))
            log.info("[LB] Posted weekly leaderboard")
        if (now - last_monthly).total_seconds() >= 2592000:
            last_monthly = now
            records = _get_records_since(now - timedelta(days=30))
            await send_telegram(format_leaderboard(records, "MONTHLY"))
            log.info("[LB] Posted monthly leaderboard")


def _get_records_since(cutoff: datetime) -> list[dict]:
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


# ─── Command + Callback Handler ───────────────────────────────────────────────

async def handle_commands():
    offset = 0
    log.info("[CMD] Command listener started")

    while True:
        try:
            updates = await get_telegram_updates(offset)
            for update in updates:
                offset = update["update_id"] + 1

                # ── Callback queries (button presses) ────────────────────────
                if "callback_query" in update:
                    cb       = update["callback_query"]
                    cb_id    = cb["id"]
                    cb_data  = cb.get("data", "")
                    cb_chat  = str(cb["message"]["chat"]["id"])
                    cb_msg   = cb["message"]["message_id"]

                    if cb_data.startswith("rug_refresh:"):
                        mint = cb_data.split(":", 1)[1]
                        await answer_callback(cb_id, "Refreshing...")
                        log.info(f"[CMD] Refresh rug: {mint[:12]}...")
                        new_text = await analyze_ca(mint)
                        await edit_telegram_message(cb_chat, cb_msg, new_text, rug_buttons(mint))

                    elif cb_data.startswith("rug_delete:"):
                        mint = cb_data.split(":", 1)[1]
                        await answer_callback(cb_id, "Deleted")
                        await delete_telegram_message(cb_chat, cb_msg)
                        log.info(f"[CMD] Deleted rug msg: {mint[:12]}...")

                    continue

                # ── Text commands ─────────────────────────────────────────────
                msg     = update.get("message", {})
                text    = msg.get("text", "").strip().lower()
                chat_id = str(msg.get("chat", {}).get("id", ""))

                if not text.startswith("/"):
                    continue

                log.info(f"[CMD] {text} from {chat_id}")

                if text.startswith("/status"):
                    await send_telegram(format_status(), chat_id)

                elif text.startswith("/leaderboard"):
                    cutoff = utcnow() - timedelta(days=1)
                    await send_telegram(format_leaderboard(_get_records_since(cutoff), "24H"), chat_id)

                elif text.startswith("/weekly"):
                    cutoff = utcnow() - timedelta(days=7)
                    await send_telegram(format_leaderboard(_get_records_since(cutoff), "WEEKLY"), chat_id)

                elif text.startswith("/monthly"):
                    cutoff = utcnow() - timedelta(days=30)
                    await send_telegram(format_leaderboard(_get_records_since(cutoff), "MONTHLY"), chat_id)

                elif text.startswith("/narratives"):
                    await send_telegram(format_narratives(), chat_id)

                elif text.startswith("/tracking"):
                    await send_telegram(format_tracking(), chat_id)

                elif text.startswith("/help"):
                    await send_telegram(format_help(), chat_id)

                elif text.startswith("/rug"):
                    parts = text.split()
                    if len(parts) < 2:
                        await send_telegram(
                            "⚠️ Usage: <code>/rug CONTRACT_ADDRESS</code>",
                            chat_id
                        )
                    else:
                        mint = parts[1].strip()
                        await send_telegram(f"🔍 Analyzing <code>{mint}</code>...", chat_id)
                        result = await analyze_ca(mint)
                        msg_id = await send_telegram_with_buttons(result, rug_buttons(mint), chat_id)
                        if msg_id:
                            rug_message_map[mint] = {"message_id": msg_id, "chat_id": chat_id}

        except Exception as e:
            log.error(f"[CMD] Error: {e}")

        await asyncio.sleep(2)


# ─── Bot Loop ─────────────────────────────────────────────────────────────────

async def run_bot():
    global sniper_ref, total_alerts_fired
    load_leaderboard()
    sniper = SolanaNarrativeSniper()
    sniper_ref = sniper

    seed = os.getenv("SEED_NARRATIVES", "")
    if seed:
        for item in seed.split("|"):
            parts = [p.strip() for p in item.split(",")]
            if len(parts) >= 3:
                sniper.inject_narrative(parts[0], parts[1], float(parts[2]))

    log.info("=" * 50)
    log.info("  SOLANA_NARRATIVE_SNIPER — BOT ONLINE")
    log.info(f"  Threshold: {ALERT_THRESHOLD}  MinLP: ${MIN_LIQUIDITY:,.0f}")
    log.info(f"  Telegram: {'on' if TELEGRAM_BOT_TOKEN else 'off'}")
    log.info(f"  Helius: {'on' if HELIUS_API_KEY else 'off'}  Birdeye: {'on' if BIRDEYE_API_KEY else 'off'}")
    log.info(f"  Milestones: {X_MILESTONES}x | Leaderboard: 24h/weekly/monthly")
    log.info(f"  Commands: /rug /status /leaderboard /weekly /monthly /narratives /tracking /help")
    log.info("=" * 50)

    await send_telegram(
        "🎯 <b>SOLANA_NARRATIVE_SNIPER ONLINE</b>\n"
        f"Threshold: {ALERT_THRESHOLD}/10\n"
        f"Tracking: {X_MILESTONES}x + migrations\n"
        f"Leaderboard: 24h / weekly / monthly\n"
        f"Commands: /rug /status /leaderboard /help\n"
        f"<i>Watching Pump.fun live...</i>"
    )

    await asyncio.gather(
        _bot_loop(sniper),
        track_tokens(),
        leaderboard_scheduler(),
        handle_commands(),
    )


async def _bot_loop(sniper):
    while True:
        try:
            await _listen(sniper)
        except Exception as e:
            log.error(f"Disconnected: {e} — reconnecting in 5s")
            await asyncio.sleep(5)


async def _listen(sniper):
    log.info("Connecting to Pump.fun...")
    async with websockets.connect(
        PUMP_WS_URL, ping_interval=20, ping_timeout=10
    ) as ws:
        await ws.send(json.dumps({"method": "subscribeNewToken"}))
        log.info("Subscribed to new token stream")
        async for raw in ws:
            try:
                msg = json.loads(raw)
                await handle(sniper, msg)
            except Exception as e:
                log.error(f"Error: {e}")


async def handle(sniper, msg: dict):
    global total_alerts_fired
    if msg.get("txType") != "create":
        return
    mint     = msg.get("mint", "")
    name     = msg.get("name", "")
    symbol   = msg.get("symbol", "")
    deployer = msg.get("traderPublicKey", "")
    desc     = msg.get("description", "")
    if not mint or not name:
        return
    if len(mint) < 32 or len(mint) > 44:
        return

    log.info(f"New: {name} (${symbol}) {mint[:12]}...")
    quick = sniper.narrative_engine.match_token_to_narrative(name, symbol, desc)
    if not quick["matched"]:
        log.info(f"  -> No narrative match — skip")
        return

    await asyncio.sleep(30)
    token_data = await enrich_token(mint, name, symbol, deployer)
    token_data["description"] = desc

    if token_data["liquidity_usd"] == 0:
        log.info(f"  -> No liquidity yet — retrying in 60s")
        await asyncio.sleep(60)
        token_data = await enrich_token(mint, name, symbol, deployer)
        token_data["description"] = desc

    if token_data["liquidity_usd"] == 0:
        log.info(f"  -> No liquidity after retry — skip")
        return

    if token_data["top1_pct"] > 5:
        log.info(f"  -> Single holder {token_data['top1_pct']:.1f}% > 5% — skip")
        return
    if token_data["top10_pct"] > 30:
        log.info(f"  -> Top10 {token_data['top10_pct']:.1f}% > 30% — skip")
        return

    alert = sniper.analyze_token(token_data)
    entry_score = alert.entry.get("final_score", 0)
    log.info(f"  -> Entry: {entry_score}/10  Rug: {alert.rug.get('rug_score',10)}/10  {alert.entry.get('verdict','')}")

    if entry_score >= ALERT_THRESHOLD:
        total_alerts_fired += 1
        log.info(f"  FIRING — tracking {symbol}")
        await send_telegram(format_alert(alert))
        entry_mcap = token_data.get("mcap_usd", 0)
        if entry_mcap > 0:
            nar_kw = (quick.get("narrative") or {}).get("keyword", "unknown")
            tracked[mint] = TrackedToken(
                mint=mint, name=name, symbol=symbol,
                entry_mcap=entry_mcap, entry_score=entry_score,
                narrative=nar_kw,
            )


if __name__ == "__main__":
    asyncio.run(run_bot())
