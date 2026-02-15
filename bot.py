"""
SOLANA_NARRATIVE_SNIPER — BOT LOOP
Pump.fun → score → Telegram alerts
+ Post-alert tracker: X multipliers + migration
+ PnL Leaderboard: 24h, weekly, monthly
"""

import asyncio
import json
import os
import sys
import logging
from datetime import datetime, timedelta
from pathlib import Path
from collections import defaultdict

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


# ─── Tracked Token ────────────────────────────────────────────────────────────

class TrackedToken:
    def __init__(self, mint, name, symbol, entry_mcap, entry_score, narrative):
        self.mint        = mint
        self.name        = name
        self.symbol      = symbol
        self.entry_mcap  = entry_mcap
        self.entry_score = entry_score
        self.narrative   = narrative
        self.peak_mcap   = entry_mcap
        self.current_mcap = entry_mcap
        self.peak_x      = 1.0
        self.alerted_xs  = set()
        self.migrated    = False
        self.added_at    = datetime.utcnow()
        self.status      = "active"   # active | migrated | dead

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


# ─── Telegram ─────────────────────────────────────────────────────────────────

async def send_telegram(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            })
            if resp.status_code != 200:
                log.error(f"Telegram error: {resp.text}")
    except Exception as e:
        log.error(f"Telegram send failed: {e}")


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
        f"<i>🕐 {datetime.utcnow().strftime('%H:%M:%S UTC')}</i>",
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
        f"<i>🕐 {datetime.utcnow().strftime('%H:%M:%S UTC')}</i>",
    ])


def format_migration_alert(token: TrackedToken, current_mcap: float) -> str:
    return "\n".join([
        "🎓 <b>MIGRATION ALERT</b>",
        "",
        f"<b>{token.name}</b>  <code>${token.symbol}</code>",
        f"<code>{token.mint}</code>",
        "",
        f"✅ Graduated Pump.fun → <b>Raydium</b>",
        f"MCap at migration: <b>${current_mcap:,.0f}</b>",
        f"Entry MCap:        <b>${token.entry_mcap:,.0f}</b>",
        f"Multiplier:        <b>{token.current_x()}X</b>",
        "",
        f"🔗 <a href='https://dexscreener.com/solana/{token.mint}'>dexscreener</a>  "
        f"<a href='https://raydium.io/swap/?inputCurrency=sol&outputCurrency={token.mint}'>raydium</a>",
        f"<i>🕐 {datetime.utcnow().strftime('%H:%M:%S UTC')}</i>",
    ])


def format_leaderboard(records: list[dict], period: str) -> str:
    if not records:
        return f"📊 <b>{period} LEADERBOARD</b>\n\nNo alerts recorded yet."

    sorted_records = sorted(records, key=lambda x: x.get("peak_x", 0), reverse=True)

    medals = ["🥇", "🥈", "🥉"]
    lines  = [
        f"📊 <b>{period} LEADERBOARD</b>",
        f"<i>{len(records)} tokens tracked</i>",
        "",
    ]

    for i, r in enumerate(sorted_records[:10]):
        medal      = medals[i] if i < 3 else f"{i+1}."
        peak_x     = r.get("peak_x", 1)
        current_x  = r.get("current_x", 1)
        migrated   = "🎓" if r.get("migrated") else ""
        narrative  = r.get("narrative", "").upper()
        entry_score = r.get("entry_score", 0)

        if peak_x >= 10:
            perf_emoji = "💎"
        elif peak_x >= 5:
            perf_emoji = "🌕"
        elif peak_x >= 2:
            perf_emoji = "🚀"
        else:
            perf_emoji = "💀"

        lines.append(
            f"{medal} <b>{r.get('name','?')}</b> <code>${r.get('symbol','?')}</code> {migrated}"
        )
        lines.append(
            f"   {perf_emoji} Peak: <b>{peak_x:.1f}X</b>  Now: {current_x:.1f}X  "
            f"Score: {entry_score}  [{narrative}]"
        )
        lines.append(
            f"   <a href='https://dexscreener.com/solana/{r.get('mint','')}'>chart</a>"
        )
        lines.append("")

    # Summary stats
    peak_xs   = [r.get("peak_x", 1) for r in records]
    avg_x     = sum(peak_xs) / len(peak_xs)
    winners   = len([x for x in peak_xs if x >= 2])
    migrants  = len([r for r in records if r.get("migrated")])
    moon_shots = len([x for x in peak_xs if x >= 10])

    lines += [
        "── Stats ──",
        f"Avg peak X:   <b>{avg_x:.1f}X</b>",
        f"2X+ winners:  <b>{winners}/{len(records)}</b>",
        f"10X+:         <b>{moon_shots}</b>",
        f"Migrations:   <b>{migrants}</b>",
        f"",
        f"<i>🕐 {datetime.utcnow().strftime('%d %b %Y %H:%M UTC')}</i>",
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
                    log.info(f"  ↳ Birdeye: liq=${liquidity:,.0f} vol1h=${v1h:,.0f} holders={holders}")
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


# ─── Tracker Loop ─────────────────────────────────────────────────────────────

async def track_tokens():
    while True:
        await asyncio.sleep(120)
        if not tracked:
            continue
        for mint in list(tracked.keys()):
            token = tracked[mint]
            age_hours = (datetime.utcnow() - token.added_at).total_seconds() / 3600
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
                    log.info(f"[TRACKER] 🎓 MIGRATION: {token.symbol}")
                    await send_telegram(format_migration_alert(token, current_mcap))

                if token.entry_mcap > 0:
                    multiplier = current_mcap / token.entry_mcap
                    for x in X_MILESTONES:
                        if multiplier >= x and x not in token.alerted_xs:
                            token.alerted_xs.add(x)
                            log.info(f"[TRACKER] 🚀 {x}X: {token.symbol}")
                            await send_telegram(format_x_alert(token, current_mcap, x))

            except Exception as e:
                log.error(f"[TRACKER] Error {mint}: {e}")
            await asyncio.sleep(1)


# ─── Leaderboard Scheduler ────────────────────────────────────────────────────

async def leaderboard_scheduler():
    """Posts leaderboard on schedule: 24h, weekly, monthly."""
    last_daily   = datetime.utcnow()
    last_weekly  = datetime.utcnow()
    last_monthly = datetime.utcnow()

    while True:
        await asyncio.sleep(60)
        now = datetime.utcnow()

        # 24h leaderboard
        if (now - last_daily).total_seconds() >= 86400:
            last_daily = now
            cutoff = now - timedelta(days=1)
            records = _get_records_since(cutoff)
            await send_telegram(format_leaderboard(records, "24H"))
            log.info(f"[LB] Posted 24h leaderboard — {len(records)} tokens")

        # Weekly leaderboard
        if (now - last_weekly).total_seconds() >= 604800:
            last_weekly = now
            cutoff = now - timedelta(days=7)
            records = _get_records_since(cutoff)
            await send_telegram(format_leaderboard(records, "WEEKLY"))
            log.info(f"[LB] Posted weekly leaderboard — {len(records)} tokens")

        # Monthly leaderboard
        if (now - last_monthly).total_seconds() >= 2592000:
            last_monthly = now
            cutoff = now - timedelta(days=30)
            records = _get_records_since(cutoff)
            await send_telegram(format_leaderboard(records, "MONTHLY"))
            log.info(f"[LB] Posted monthly leaderboard — {len(records)} tokens")


def _get_records_since(cutoff: datetime) -> list[dict]:
    """Get all records (active + historical) since cutoff."""
    records = []
    # Active tracked tokens
    for t in tracked.values():
        if t.added_at >= cutoff:
            records.append(t.to_record())
    # Historical
    for r in leaderboard_history:
        try:
            added = datetime.fromisoformat(r["added_at"])
            if added >= cutoff:
                records.append(r)
        except Exception:
            pass
    return records


# ─── Bot Loop ─────────────────────────────────────────────────────────────────

async def run_bot():
    load_leaderboard()
    sniper = SolanaNarrativeSniper()
    seed = os.getenv("SEED_NARRATIVES", "")
    if seed:
        for item in seed.split("|"):
            parts = [p.strip() for p in item.split(",")]
            if len(parts) >= 3:
                sniper.inject_narrative(parts[0], parts[1], float(parts[2]))

    log.info("=" * 50)
    log.info("  SOLANA_NARRATIVE_SNIPER — BOT ONLINE")
    log.info(f"  Threshold: {ALERT_THRESHOLD}  MinLP: ${MIN_LIQUIDITY:,.0f}")
    log.info(f"  Telegram: {'✓' if TELEGRAM_BOT_TOKEN else '✗'}")
    log.info(f"  Helius: {'✓' if HELIUS_API_KEY else '✗'}  Birdeye: {'✓' if BIRDEYE_API_KEY else '✗'}")
    log.info(f"  Milestones: {X_MILESTONES}x | Leaderboard: 24h/weekly/monthly")
    log.info("=" * 50)

    await send_telegram(
        "🎯 <b>SOLANA_NARRATIVE_SNIPER ONLINE</b>\n"
        f"Threshold: {ALERT_THRESHOLD}/10\n"
        f"Tracking: {X_MILESTONES}x + migrations\n"
        f"Leaderboard: 24h / weekly / monthly\n"
        f"<i>Watching Pump.fun live...</i>"
    )

    await asyncio.gather(
        _bot_loop(sniper),
        track_tokens(),
        leaderboard_scheduler(),
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
        log.info("✓ Subscribed to new token stream")
        async for raw in ws:
            try:
                msg = json.loads(raw)
                await handle(sniper, msg)
            except Exception as e:
                log.error(f"Error: {e}")


async def handle(sniper, msg: dict):
    if msg.get("txType") != "create":
        return
    mint     = msg.get("mint", "")
    name     = msg.get("name", "")
    symbol   = msg.get("symbol", "")
    deployer = msg.get("traderPublicKey", "")
    desc     = msg.get("description", "")
    if not mint or not name:
        return

    log.info(f"New: {name} (${symbol}) {mint[:12]}...")
    quick = sniper.narrative_engine.match_token_to_narrative(name, symbol, desc)
    if not quick["matched"]:
        log.info(f"  ↳ No narrative match — skip")
        return
    # Validate mint address before API calls
    if len(mint) < 32 or len(mint) > 44:
        log.info(f"  ↳ Invalid mint address — skip")
        return

    await asyncio.sleep(30)
    token_data = await enrich_token(mint, name, symbol, deployer)
    token_data["description"] = desc

    if token_data["liquidity_usd"] == 0:
        log.info(f"  ↳ No liquidity yet — retrying in 60s")
        await asyncio.sleep(60)
        token_data = await enrich_token(mint, name, symbol, deployer)
        token_data["description"] = desc

    if token_data["liquidity_usd"] == 0:
        log.info(f"  ↳ No liquidity after retry — skip")
        return

    # Hard holder filter
    if token_data["top1_pct"] > 5:
        log.info(f"  ↳ Single holder {token_data['top1_pct']:.1f}% > 5% — skip")
        return
    if token_data["top10_pct"] > 30:
        log.info(f"  ↳ Top10 concentration {token_data['top10_pct']:.1f}% > 30% — skip")
        return

    alert = sniper.analyze_token(token_data)
    entry_score = alert.entry.get("final_score", 0)
    log.info(f"  ↳ Entry: {entry_score}/10  Rug: {alert.rug.get('rug_score',10)}/10  {alert.entry.get('verdict','')}")

    if entry_score >= ALERT_THRESHOLD:
        log.info(f"  🚨 FIRING — tracking {symbol}")
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
