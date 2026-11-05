# Competitor Monitor (Alchemi + Khaial)

Checks competitor Shopify stores twice a day and alerts you on Telegram and/or email when something changes. Runs free on GitHub Actions, no server.

## What it tracks

| Change | Example alert |
|---|---|
| New products | `Oud Royale, $17.99 to $24.99, sizes 50ml/100ml` |
| Removed / unpublished products | `Scent 30 removed` |
| Price changes per size | `Aventus 100ml: DOWN -12%, $24.99 → $21.99` |
| Sales started / ended | `SALE STARTED (was $29.99, now $19.99)` |
| Stock | `Sauvage EDT 100ml: SOLD OUT` / `BACK IN STOCK` |
| Sizes added / removed | `New size: 10ml $6.99` |
| Renames, description edits, tags, product type, images | word-level diff of what was added/removed |
| Collections | new collections (e.g. "Black Friday"), products added/removed from tracked collections |
| Best-seller ranking | `#1 Sauvage (was #3); #4 Oud Royale (new)` |
| Website text | offers, announcement bars, banners, SEO title, meta description, policy edits |
| New banner images | links to new hero/campaign images |

Data source is each store's public Shopify JSON (`/products.json`, `/collections.json`), so it is exact data, not scraped guesses.

## Outputs (all committed to the repo each run)

- `reports/latest.md` and `reports/YYYY-MM-DD_HHMM.md`: readable report per run
- `changes.csv`: every change ever detected, one row each. Open in Sheets to answer things like "how often does Alchemi change prices" or "what did Khaial launch in November"
- `data/<site>/snapshot.json`: full catalog snapshot (git history = full price history)

## Setup (10 minutes)

Recipients are already set in `config.yaml` (`alerts:` section): 2 emails + Telegram `@usernamedexg`. Only the sending credentials go in GitHub secrets.

1. **Create a private GitHub repo** (e.g. `aconverts/competitor-monitor`) and upload all files, including the `.github` folder.
2. **Telegram bot:** in Telegram, message `@BotFather` → `/newbot` → pick a name → copy the token. Then from the @usernamedexg account, open the new bot and press **Start** (a bot cannot message anyone who has not pressed Start). The monitor finds the chat by username automatically.
3. **Gmail app password:** on the Gmail account that will send alerts: Google Account → Security → 2-Step Verification on → App passwords → create one (16 characters).
4. **Add secrets:** repo → Settings → Secrets and variables → Actions → New repository secret:
   - `TELEGRAM_BOT_TOKEN` = the BotFather token
   - `SMTP_USER` = the sending Gmail address
   - `SMTP_PASS` = the app password
5. **Settings → Actions → General → Workflow permissions → "Read and write permissions"** (needed to save snapshots).
6. **Test alerts:** Actions tab → Competitor monitor → Run workflow → mode `test-alerts`. Everyone should get a test email + Telegram message. The log says exactly which channel worked or why not.
7. **Go live:** Run workflow → mode `check`. The first run saves a baseline of both stores and sends "baseline saved". Real change alerts start from the next run (automatic at 6am and 4pm Beirut).

Telegram tip: press Start on the bot within 24h before step 6 (Telegram only keeps unread bot messages for 24h). After the first successful find, the chat is remembered for good.

Schedule: 6am and 4pm Beirut (7am / 5pm in summer). Edit the `cron` lines in `.github/workflows/monitor.yml` to change it; GitHub may start scheduled runs a few minutes late.

## Adding or changing sites

Edit `config.yaml`. Any Shopify store works. Per site you can list:
- `collections`: handles to watch for additions/removals and ranking (take them from the URL `/collections/<handle>`)
- `pages`: paths whose text you want tracked (`/`, `/pages/about`, `/policies/refund-policy`...)

Tip: add `ravaperfumes.com` as a site too. Then `changes.csv` and the snapshots hold your prices next to theirs, and you get a record of your own catalog changes for free.

If a page keeps triggering noise (timers, rotating reviews), add a regex to `ignore_line_patterns`.

## Run locally

```
pip install -r requirements.txt
python monitor.py --dry-run   # compare and print, saves nothing
python monitor.py
```

## Safety behaviour

- If a store suddenly returns under 50% of its previous product count (blocked, down, password page), the run reports an error and keeps the old snapshot, so you never get a fake "300 products removed" alert.
- One site failing does not stop the others.
