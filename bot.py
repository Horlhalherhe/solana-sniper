"""
SOLANA_NARRATIVE_SNIPER — BOT LOOP
Pump.fun WebSocket → auto-score → Telegram alert
"""

import asyncio
import json
import os
import sys
import logging
from datetime import datetime
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


async def send_telegram(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code != 200:
                log.error(f"Telegram error: {resp.text}")
    except Exception as e:
        log.error(f"Telegram send failed: {e}")


def format_alert(alert) -> str:
    e = alert.entry
    r = alert.rug
    t = alert.token
    n = alert.narrative

    entry_score = e.get("final_score", 0)
    rug_score   = r.get("rug_score", 10)
    verdict     = e.get("verdict", "")
    rug_verdict = r.get("verdict", "")
    flags       = r.get("flags", [])
    flag_str    = "  ".join([f"⛔ {f['code']}" for f in flags[:3]]) if flags else "✅ None"
    narrative_kw = (n.get("narrative") or {}).get("keyword", "NONE")
    comps = e.get("components", {})

    lines = [
        f"🎯 <b>SNIPER ALERT</b>",
        f"",
        f"<b>{t.get('name','?')}</b>  <code>${t.get('symbol','?')}</code>",
        f"<code>{t.get('mint', 'N/A')}</code>",
        f"",
        f"📊 <b>ENTRY:  {entry_score}/10  {verdict}</b>",
        f"🔒 <b>RUG:    {rug_score}/10  {rug_verdict}</b>",
        f"",
        f"📡 Narrative:  <b>{narrative_kw.upper()}</b>  ({n.get('narrative_score',0):.1f}/10)",
        f"💧 Liquidity:  <b>${t.get('liquidity_usd',0):,.0f}</b>",
        f"👥 Holders:    <b>{t.get('total_holders',0)}</b>",
        f"⏱ Age:        <b>{t.get('age_hours',0):.1f}h</b>",
        f"📈 Vol 1h:     <b>${t.get('volume_1h_usd',0):,.0f}</b>",
        f"",
        f"NAR {comps.get('narrative',{}).get('score','?')}  "
        f"TIM {comps.get('timing',{}).get('score','?')}  "
        f"HOL {comps.get('holders',{}).get('score','?')}  "
        f"DEP {comps.get('deployer',{}).get('score','?')}  "
        f"MOM {comps.get('momentum',{}).get('score','?')}",
        f"",
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


async def enrich_token(mint: str, name: str, symbol: str, deployer: str) -> dict:
    base = {
        "mint": mint, "name": name, "symbol": symbol, "deployer": deployer,
        "age_hours": 0.02, "mcap_usd": 0, "liquidity_usd": 0,
        "total_holders": 1, "top1_pct": 90.0, "top5_pct": 95.0,
        "top10_pct": 99.0, "dev_holds_pct": 80.0, "wallet_clusters": 0,
        "holder_growth_1h": 0, "mint_authority_revoked": False,
        "freeze_authority_revoked": False, "lp_burned": False,
        "lp_locked": False, "pool_age_hours": 0.0, "deployer_age_days": 0,
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
                    data = resp.json().get("data", {})
                    base.update({
                        "mcap_usd": data.get("mc", 0) or 0,
                        "liquidity_usd": data.get("liquidity", 0) or 0,
                        "volume_1h_usd": data.get("v1hUSD", 0) or 0,
                        "volume_5m_usd": data.get("v5mUSD", 0) or 0,
                        "price_change_1h_pct": data.get("priceChange1hPercent", 0) or 0,
                        "price_change_5m_pct": data.get("priceChange5mPercent", 0) or 0,
                        "buy_sell_ratio_1h": (data.get("buy1h", 1) / max(data.get("sell1h", 1), 1)),
                        "total_holders": data.get("holder", 1) or 1,
                    })
        except Exception as e:
            log.warning(f"Birdeye error: {e}")

    if HELIUS_API_KEY:
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                resp = await client.post(
                    f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}",
                    json={"jsonrpc": "2.0", "id": 1, "method": "getAccountInfo",
                          "params": [mint, {"encoding": "jsonParsed"}]}
                )
                if resp.status_code == 200:
                    info = resp.json().get("result", {}).get("value", {})
                    parsed = info.get("data", {}).get("parsed", {}).get("info", {})
                    base["mint_authority_revoked"] = parsed.get("mintAuthority") is None
                    base["freeze_authority_revoked"] = parsed.get("freezeAuthority") is None
        except Exception as e:
            log.warning(f"Helius error: {e}")

    return base


async def run_bot():
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
    log.info("=" * 50)

    await send_telegram(
        "🎯 <b>SOLANA_NARRATIVE_SNIPER ONLINE</b>\n"
        f"Watching Pump.fun live\n"
        f"Threshold: {ALERT_THRESHOLD}/10\n"
        f"<i>Waiting for tokens...</i>"
    )

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

    token_data = await enrich_token(mint, name, symbol, deployer)
    token_data["description"] = desc

    alert = sniper.analyze_token(token_data)
    entry_score = alert.entry.get("final_score", 0)
    rug_score   = alert.rug.get("rug_score", 10)

    log.info(f"  ↳ Entry: {entry_score}/10  Rug: {rug_score}/10  {alert.entry.get('verdict','')}")

    if entry_score >= ALERT_THRESHOLD:
        log.info(f"  🚨 FIRING ALERT")
        await send_telegram(format_alert(alert))


if __name__ == "__main__":
    asyncio.run(run_bot())
