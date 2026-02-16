# CRITICAL FIX DEPLOYMENT

## Problem
Your deployed `core/` folder is missing the `inject_manual_narrative` method, causing the bot to crash on startup before it can even subscribe to Pump.fun.

## Solution
Replace your entire GitHub `/core` folder with the fixed `/core_fixed` folder.

## Step-by-Step

### 1. In your GitHub repo, DELETE the old `/core` folder

### 2. Upload the NEW `/core_fixed` folder and rename it to `/core`

Files to upload:
```
core/__init__.py
core/narrative_engine.py  
core/rug_analyzer.py
core/entry_scorer.py
core/meta_intelligence.py
```

### 3. Commit and push to GitHub

### 4. Railway will auto-deploy the fix

### 5. Watch logs for these SUCCESS indicators:

```
[SNIPER] Injected: openai (ai_tech) score=8.0
[SNIPER] Injected: bear (animals) score=6.5
[SNIPER] Injected: doge (animals) score=8.0
...
[SNIPER] All engines online.
[WS] Connecting to Pump.fun...
[WS] Subscribed to new token stream
```

### 6. Test narrative matching works:

Wait for a token with "dog", "cat", "monkey", "elon", "musk", or "trump" in the name.

You should see:
```
[NEW] Thief Cat ($CAT) abc123...
  -> Source: dexscreener  liq=$1,200  mcap=$2,500
  -> Entry: 4.5/10  Rug: 5.2/10  🟠 WEAK ENTRY
```

If you see ` -> No narrative match` for tokens with those keywords, the core folder wasn't updated.

## What Was Fixed

1. **narrative_engine.py** - Added missing `inject_manual_narrative()` method
2. **entry_scorer.py** - Added `top1_pct` field to `EntryInput` dataclass (was missing, causing crashes)
3. All modules synced to latest working versions

## Current Settings (for testing)

```
MIN_LIQUIDITY_USD = 500   # Very low to force alerts
ALERT_THRESHOLD = 3.0      # Very low to force alerts
```

Once you confirm alerts are firing, raise them back to production values:
```
MIN_LIQUIDITY_USD = 5000
ALERT_THRESHOLD = 5.0
```
