# Issue watcher

Every 15 minutes this checks the repos in `config.toml` for open issues that
nobody has claimed yet, and sends you one digest by email, Telegram and/or Discord.

An issue is reported only if it is open, has **no assignee**, **no linked PR**, and
**no open PR that mentions it**. Issues where the reporter or a commenter wrote
something like "I can send a PR", "/assign" or "can I work on this?" go in a
separate **Possibly claimed** section so you can judge for yourself.
Stale, docs and support issues are skipped. Each issue is sent only once.

It runs free on GitHub Actions as long as the repository is **public**.

## Setup (about 10 minutes)

### 1. Create the repo in your organization
Create a new **public** repository in your org (e.g. `issue-watcher`) and upload
everything from this folder, keeping the paths:

```
.github/workflows/watch.yml
config.toml
watcher.py
state/seen.json
README.md
```

Tip: file pickers often hide the `.github` folder. Either drag the whole folder
onto the "Upload files" page, or use **Add file → Create new file**, type
`.github/workflows/watch.yml` as the name, and paste the contents.

### 2. Add your notification secrets
In the repo: **Settings → Secrets and variables → Actions → New repository secret**.
Secrets stay encrypted and hidden even though the repo is public.
Set up any of the three; channels without secrets are simply skipped.

**Email (Gmail)**
1. Turn on 2-Step Verification for your Google account.
2. Create an app password at https://myaccount.google.com/apppasswords
3. Add the secrets:

| Secret | Value |
|---|---|
| `SMTP_USER` | your Gmail address |
| `SMTP_PASSWORD` | the 16-character app password (not your normal password) |
| `EMAIL_TO` | where to send the digest (can be the same address) |

Not using Gmail? Also set `SMTP_HOST` and `SMTP_PORT` (465 for SSL or 587 for STARTTLS).

**Telegram (works fine in web.telegram.org)**
1. In Telegram, message **@BotFather**, send `/newbot`, and copy the token.
2. Open your new bot and send it any message, e.g. "hi".
3. Open `https://api.telegram.org/bot<TOKEN>/getUpdates` in your browser and find
   `"chat":{"id": ...}`; that number is your chat ID.
4. Add `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.

**Discord**
1. In your server: **Server Settings → Integrations → Webhooks → New Webhook**,
   pick a channel, **Copy Webhook URL**.
2. Add `DISCORD_WEBHOOK_URL`.

### 3. Test it
Go to **Actions → Issue watcher → Run workflow**:

1. `test-notify`: sends a test message to every channel you configured.
   Check the run log if one doesn't arrive.
2. `dry-run` with `lookback_hours` = `72`: shows in the log what it *would* send
   from the last 3 days, without sending or remembering anything.
3. Optionally `run` with `lookback_hours` = `72` to get a first real digest now.

After that it runs on its own every 15 minutes.

## Changing what it watches
Edit `config.toml` directly on GitHub:

- **Add a repo:** add a `[[repo]]` block with `name = "owner/repo"`.
- **Pause a repo:** add `enabled = false` under it.
- **Busy repos:** `require_any_label = ["help wanted"]` reports only issues with those labels
  (already set for `kubernetes/kubernetes`).
- **Too noisy or too quiet:** edit `exclude_labels`, `watch_labels` or `claim_patterns`.

## Good to know
- Alerts usually arrive 5 to 20 minutes after an issue is opened, since GitHub
  sometimes delays scheduled runs.
- GitHub pauses scheduled workflows in public repos after 60 days without repo
  activity. The watcher commits `state/seen.json` whenever it sends something, which
  normally keeps it active. If it ever stops, open **Actions** and re-enable it.
- If a run fails with a permissions error on `git push`, check
  **Settings → Actions → General → Workflow permissions** in the repo and the org.
- Claim detection is a heuristic. Always read the thread before commenting.
