# Slack on iPhone with ProjectOS (Socket Mode)

Slack on your phone talks to Slack’s cloud. ProjectOS on this PC opens an **outbound** Socket Mode connection to Slack. There is **no** public URL, Cloudflare Tunnel, or ngrok.

The PC must stay on, and ProjectOS must be running.

## One-time Slack app

1. Open [https://api.slack.com/apps](https://api.slack.com/apps) → **Create New App** → **From an app manifest**.
2. Paste `integrations/slack/projectos-slack-manifest.yaml`.
3. Create the app in your workspace.
4. **Basic Information → App-Level Tokens → Generate Token and Scopes**
   - Token name: `projectos-socket`
   - Scope: `connections:write`
   - Copy the **xapp-** token.
5. **OAuth & Permissions → Install to Workspace**.
6. Copy the **Bot User OAuth Token** (**xoxb-**).
7. Invite the app to the channel you will bind (`/invite @ProjectOS`).

No slash-command Request URL is required for Socket Mode.

## Tokens on this Windows PC

In PowerShell:

```powershell
setx PROJECTOS_SLACK_APP_TOKEN "xapp-..."
setx PROJECTOS_SLACK_BOT_TOKEN "xoxb-..."
```

Close the terminal, then **restart ProjectOS** (desktop shortcut). `setx` does not update an already-open process.

Confirm:

```powershell
python -m projectos slack doctor
```

You should see `app_token: xapp-...configured` and `bot_token: xoxb-...configured`. Tokens are never printed in full.

## Bind the channel

1. In Slack, open the channel → copy **Channel ID** (and Team ID if shown).
2. In the ProjectOS dashboard, open the project → **Slack**.
3. Bind Channel ID (and Team ID).
4. In that channel:

```text
/projectos status
```

Also useful:

```text
/projectos
/projectos help
/projectos work
/projectos quality
/projectos releases
```

The bound channel is the project. `/projectos --project PRJ-003` is rejected.

## If Slack says the command is unknown

The Slack app slash command must be exactly `/projectos` (not `/projecos`). The app must be installed in that workspace and invited to the channel. Then restart ProjectOS so Socket Mode is connected (dashboard Slack page → Connection status **Connected**).
