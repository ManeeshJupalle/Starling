# Connecting real integrations (Gmail · Google Calendar · Slack)

Starling-Claw acts on your real accounts through MCP servers declared in
`mcp_servers.json`. They ship **disabled** (name prefixed with `_`, which the loader
skips). Set up the ones you want, then **remove the leading `_`** from that server's
key to switch it on. Anything still disabled or missing credentials is simply skipped at
startup — it never breaks the others.

> **Privacy note:** once connected, the agent can read this data, which means it is sent
> to your LLM provider as context. That is expected for a personal agent — just know it
> happens. Writes (send email, create event, post to Slack) always pause for your
> approval in Telegram before they run.

Requires **Node.js** (the servers launch via `npx`).

---

## A. Google: one OAuth client for both Gmail and Calendar

You only need **one** Google Cloud OAuth client; enable both APIs on it.

1. Go to <https://console.cloud.google.com/> → create (or pick) a project.
2. **APIs & Services → Library** → enable **Gmail API** and **Google Calendar API**.
3. **APIs & Services → OAuth consent screen** → External → add yourself as a **Test user**.
4. **APIs & Services → Credentials → Create credentials → OAuth client ID** →
   application type **Desktop app** → Create → **Download JSON**.

### Gmail (`@gongrzhe/server-gmail-autoauth-mcp`)
1. Save the downloaded JSON as `~/.gmail-mcp/gcp-oauth.keys.json`
   (Windows: `C:\Users\<you>\.gmail-mcp\gcp-oauth.keys.json`).
2. Run the one-time browser auth:
   ```
   npx -y @gongrzhe/server-gmail-autoauth-mcp auth
   ```
   Log in, approve. Tokens are saved to `~/.gmail-mcp/credentials.json`.
3. In `mcp_servers.json`, rename `"_gmail"` → `"gmail"`.

### Google Calendar (`@cocal/google-calendar-mcp`)
1. Save the OAuth JSON somewhere stable (you can reuse the same Desktop-app JSON),
   e.g. `C:\Users\<you>\.gmail-mcp\gcp-oauth.keys.json`.
2. In `mcp_servers.json`, set the `calendar` server's
   `GOOGLE_OAUTH_CREDENTIALS` to that file's **absolute** path, then rename
   `"_calendar"` → `"calendar"`. (npx needs an absolute path here.)
3. First use triggers a browser auth; approve it.

---

## B. Slack (`@modelcontextprotocol/server-slack`)
1. <https://api.slack.com/apps> → **Create New App** → From scratch.
2. **OAuth & Permissions → Bot Token Scopes**, add at least:
   `channels:history`, `channels:read`, `chat:write`, `users:read`.
3. **Install to Workspace**, then copy the **Bot User OAuth Token** (`xoxb-…`).
4. Get your **Team ID** (`T…`) from your workspace URL / admin page.
5. In `mcp_servers.json`, fill `SLACK_BOT_TOKEN` and `SLACK_TEAM_ID`, then rename
   `"_slack"` → `"slack"`.

---

## Verify
Run the bot:
```
python -m starling
```
You should see e.g. `[mcp] connected 'gmail': N tools`. Then in Telegram try a **read**
(auto-runs, no approval):

- *"What's on my calendar today?"*
- *"Summarize my unread emails."*

Writes (sending, creating, posting) come in Phase G2 and will pause for your yes/no.

**If a server hangs or misbehaves at startup**, prefix its key with `_` again to disable
it — the rest keep working.
