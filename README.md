# oanda-mcp — read-only OANDA connector for Claude

Lets Claude (web, desktop **and phone**) read OANDA's order book, position book,
prices, candles and your account — live, instead of from screenshots.

**Read-only:** the server only sends GET requests. It cannot place, change or close trades.

## Tools
| Tool | What it does |
|---|---|
| `read_books` | Both books for one instrument, summarised: trapped side, stop/limit zones, top clusters, price profile |
| `get_position_book` / `get_order_book` | One book, optional past snapshot via `time` |
| `usd_positioning_scan` | Headline positioning across the USD pairs + gold, to check the story is consistent |
| `get_prices` | Live bid/ask |
| `get_candles` | OHLC candles (M5 … W) |
| `get_account_overview` | Balance, NAV, open trades |

## 1. Get your OANDA details
1. Log in to your OANDA **practice** account → *Manage API Access* → generate a token.
2. Note your account ID (format `101-004-XXXXXXX-001`).

## 2. Test locally (PowerShell)
```powershell
pip install -r requirements.txt
$env:OANDA_TOKEN="your-token"
$env:OANDA_ACCOUNT_ID="your-account-id"
$env:MCP_SECRET = python -c "import secrets; print(secrets.token_urlsafe(32))"
echo $env:MCP_SECRET        # save this
python server.py            # serves http://localhost:8000/<MCP_SECRET>/mcp
```
Check the book endpoints return data for your account region, especially `XAU_USD`.

## 3. Deploy (Render example — any host with a public HTTPS URL works)
1. Push this folder to a **private** GitHub repo.
2. Render → New → Web Service → connect the repo.
   - Build command: `pip install -r requirements.txt`
   - Start command: `python server.py`
3. Environment variables: `OANDA_TOKEN`, `OANDA_ACCOUNT_ID`, `OANDA_ENV=practice`,
   `MCP_SECRET`, and `PUBLIC_HOST=<your-service>.onrender.com`.
4. Your connector URL is: `https://<your-service>.onrender.com/<MCP_SECRET>/mcp`

Note: Render's free tier sleeps when idle, so the first call after a pause can be slow
or time out — just retry. An always-on host (paid tier, a small VPS, or Cloud Run with
min instances) avoids this.

## 4. Add to Claude
1. On **claude.ai (web)**: Settings → Connectors → *Add custom connector*.
2. Paste the connector URL. No OAuth needed.
3. It then appears in the Claude **mobile app** too (connectors can't be added from mobile,
   only used there).

## Security
- The URL path contains your secret — treat the full URL like a password. To rotate it,
  change `MCP_SECRET`, redeploy, and update the connector.
- OANDA tokens aren't scoped read-only by OANDA; the read-only guarantee comes from this
  code. Don't add POST/PUT endpoints. Start with a practice-account token.
- Never commit tokens: keep them in the host's environment variables.
