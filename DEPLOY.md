# 🚀 Deploy Bot to Railway

## Step 1 — Push to GitHub

```bash
cd solana_sniper
git init
git add .
git commit -m "initial: solana narrative sniper"
gh repo create solana-narrative-sniper --private --push --source=.
```

Or manually create a GitHub repo and push.

---

## Step 2 — Create Railway Project

1. Go to **https://railway.app**
2. Click **New Project**
3. Select **Deploy from GitHub repo**
4. Pick your `solana-narrative-sniper` repo
5. Railway auto-detects Python + `Procfile`

---

## Step 3 — Set Environment Variables

In Railway dashboard → your service → **Variables** tab:

| Variable | Value |
|----------|-------|
| `HELIUS_API_KEY` | Your Helius key |
| `BIRDEYE_API_KEY` | Your Birdeye key |
| `ALERT_THRESHOLD` | `5.5` |
| `NARRATIVE_THRESHOLD` | `4.0` |
| `SNIPER_DATA_DIR` | `/app/data` |

Railway auto-sets `PORT` — **do not add it**.

---

## Step 4 — Add a Volume (Persistent Data)

So your deployer DB / token history survives redeploys:

1. Railway dashboard → your service → **Volumes**
2. Mount path: `/app/data`
3. Done — `SNIPER_DATA_DIR=/app/data` already set above

---

## Step 5 — Deploy

Railway deploys automatically on every push to main.

Your app will be live at:
```
https://your-project-name.up.railway.app
```

Dashboard: `https://your-project.up.railway.app/`
API docs:  `https://your-project.up.railway.app/docs`
Health:    `https://your-project.up.railway.app/health`

---

## API Endpoints (once live)

```bash
# Feed narratives
curl -X POST https://your-app.up.railway.app/api/narrative/feed \
  -H "Content-Type: application/json" \
  -d '{"texts": ["deepseek r2 going viral on CT"]}'

# Inject manual narrative
curl -X POST https://your-app.up.railway.app/api/narrative/inject \
  -H "Content-Type: application/json" \
  -d '{"keyword": "capybara", "category": "animals", "score": 7.5}'

# Analyze token
curl -X POST https://your-app.up.railway.app/api/token/analyze \
  -H "Content-Type: application/json" \
  -d '{"data": {"name":"DeepSeek AI","symbol":"DSAI","mint":"So1...","age_hours":1.5,"liquidity_usd":28000,"top10_pct":33,"mint_authority_revoked":true,"freeze_authority_revoked":true,"lp_burned":true}}'

# Get active narratives
curl https://your-app.up.railway.app/api/narrative/active

# Intel summary
curl https://your-app.up.railway.app/api/intel/summary
```

---

## Free Tier Note

Railway free tier: $5 credit/month.
This app uses ~$0.01–0.05/day idle.
Upgrade to Hobby ($5/mo) for always-on.
