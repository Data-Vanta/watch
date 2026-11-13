#!/usr/bin/env python3
"""Issue watcher.

Finds open issues that nobody has claimed yet (no assignee, no linked or open PR)
in the repos listed in config.toml, and sends one digest per run by email,
Telegram and/or Discord. Uses only the Python standard library.

Modes (flag, or WATCHER_MODE env var in GitHub Actions):
  run          normal run: notify and remember what was sent (default)
  dry-run      print the digest to the log; send nothing, remember nothing
  test-notify  send a short test message to every configured channel
"""

import argparse
import html
import json
import os
import re
import smtplib
import ssl
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

API = "https://api.github.com"
ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "state" / "seen.json"
SEARCH_PAUSE = 6.5  # stays under 10 searches a minute, the lowest limit GitHub applies
USER_AGENT = "issue-watcher (github actions)"


def log(msg):
    print(msg, flush=True)


# ---------------------------------------------------------------- GitHub API

def http_json(url, data=None, headers=None, method=None):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method=method, headers={
        "User-Agent": USER_AGENT,
        **({"Content-Type": "application/json"} if body else {}),
        **(headers or {}),
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else None


def gh(path, params=None):
    url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = "Bearer " + os.environ["GITHUB_TOKEN"]
    for attempt in range(3):
        try:
            return http_json(url, headers=headers)
        except urllib.error.HTTPError as err:
            last = attempt == 2
            if err.code in (403, 429) and not last:
                h = err.headers
                retry_after, reset = h.get("retry-after"), h.get("x-ratelimit-reset")
                if retry_after:
                    wait = int(retry_after)
                elif reset:
                    wait = int(reset) - int(time.time()) + 1
                else:
                    wait = 30
                wait = max(5, min(wait, 65))
                log(f"  rate limited ({h.get('x-ratelimit-resource', '?')}: limit "
                    f"{h.get('x-ratelimit-limit', '?')}, remaining {h.get('x-ratelimit-remaining', '?')}), "
                    f"waiting {wait}s")
                time.sleep(wait)
                continue
            if err.code >= 500 and not last:
                time.sleep(5)
                continue
            raise


_last_search = [0.0]


def search_issues(query, max_pages=3):
    """Run one search (up to max_pages x 100 results), pacing calls to stay under the limit."""
    items = []
    for page in range(1, max_pages + 1):
        wait = _last_search[0] + SEARCH_PAUSE - time.time()
        if wait > 0:
            time.sleep(wait)
        data = gh("/search/issues", {"q": query, "per_page": 100, "page": page,
                                     "sort": "updated", "order": "desc"})
        _last_search[0] = time.time()
        batch = data.get("items", [])
        items.extend(batch)
        if len(batch) < 100:
            break
    return items


def inspect_timeline(repo, number, claim_res):
    """Return (open PRs referencing the issue, (who, snippet) for comments that claim it)."""
    events = gh(f"/repos/{repo}/issues/{number}/timeline", {"per_page": 100}) or []
    open_prs, claims = [], []
    for ev in events:
        kind = ev.get("event")
        if kind == "cross-referenced":
            src = (ev.get("source") or {}).get("issue") or {}
            if src.get("pull_request") and src.get("state") == "open":
                open_prs.append(src.get("html_url"))
        elif kind == "commented":
            snippet = find_claim(ev.get("body") or "", claim_res)
            if snippet:
                who = (ev.get("user") or ev.get("actor") or {}).get("login", "someone")
                claims.append((who, snippet))
    return open_prs, claims


# ------------------------------------------------------------------ filtering

def find_claim(text, claim_res):
    for rx in claim_res:
        m = rx.search(text)
        if m:
            start, end = max(0, m.start() - 30), min(len(text), m.end() + 50)
            return " ".join(text[start:end].split())
    return None


def parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def ago(dt, now):
    hours = (now - dt).total_seconds() / 3600
    if hours < 1:
        return f"{max(1, int(hours * 60))} min ago"
    if hours < 48:
        return f"{int(hours)} h ago"
    return f"{int(hours / 24)} days ago"


def build_queries(cfg, since_q):
    """Group repos into as few searches as possible.

    Normal repos share one query (several repo: qualifiers, kept under GitHub's
    256-character query limit) for every issue updated in the window; new issues
    and newly labelled ones are picked out locally. Repos with require_any_label
    get one query per required label instead.
    """
    tail = f"is:issue is:open no:assignee -linked:pr updated:>={since_q}"
    queries, batch = [], []
    for r in cfg.get("repo", []):
        if not r.get("enabled", True):
            continue
        if r.get("require_any_label"):
            for label in r["require_any_label"]:
                queries.append(f'repo:{r["name"]} label:"{label}" {tail}')
            continue
        candidate = batch + [r["name"]]
        if batch and len(" ".join(f"repo:{n}" for n in candidate) + " " + tail) > 250:
            queries.append(" ".join(f"repo:{n}" for n in batch) + " " + tail)
            candidate = [r["name"]]
        batch = candidate
    if batch:
        queries.append(" ".join(f"repo:{n}" for n in batch) + " " + tail)
    return queries


def collect(cfg, since, now, seen, deadline):
    s = cfg["settings"]
    exclude_labels = {l.lower() for l in s.get("exclude_labels", [])}
    exclude_title = [re.compile(p) for p in s.get("exclude_title_patterns", [])]
    exclude_authors = {a.lower() for a in s.get("exclude_authors", [])}
    watch_labels = {l.lower() for l in s.get("watch_labels", [])}
    claim_res = [re.compile(p, re.IGNORECASE) for p in s.get("claim_patterns", [])]
    repo_cfgs = {r["name"].lower(): r for r in cfg.get("repo", []) if r.get("enabled", True)}
    since_q = since.strftime("%Y-%m-%dT%H:%M:%S+00:00")

    found, failed = {}, []
    queries = build_queries(cfg, since_q)
    log(f"{len(queries)} search queries for {len(repo_cfgs)} repos")
    for q in queries:
        if time.time() > deadline:
            log("Time budget reached; skipping the remaining searches this run.")
            failed.append(q.split(" is:issue")[0])
            break
        try:
            for item in search_issues(q):
                repo = item["repository_url"].split("/repos/", 1)[1]
                found[f"{repo}#{item['number']}"] = (repo, item)
        except Exception as err:  # keep going with the other queries
            log(f"search failed ({err}) for: {q.split(' is:issue')[0]}")
            failed.append(q.split(" is:issue")[0])

    available, claimed = [], []
    per_repo = {}
    for key, (repo, item) in sorted(found.items()):
        rc = repo_cfgs.get(repo.lower(), {})
        required = {l.lower() for l in rc.get("require_any_label", [])}
        labels = [l["name"] for l in item.get("labels", [])]
        lower = {l.lower() for l in labels}
        author = (item.get("user") or {}).get("login", "")
        created = parse_time(item["created_at"])
        is_new = created >= since

        if key in seen or item.get("assignees") or item.get("pull_request"):
            continue
        if lower & exclude_labels or author.lower() in exclude_authors:
            continue
        if any(rx.search(item["title"]) for rx in exclude_title):
            continue
        if required:
            if not lower & required:
                continue
        elif not is_new and not lower & watch_labels:
            continue  # an old issue that was merely updated
        if time.time() > deadline:
            log(f"Time budget reached; {key} will be checked next run.")
            continue

        try:
            open_prs, comment_claims = inspect_timeline(repo, item["number"], claim_res)
        except Exception as err:
            log(f"{key}: timeline check failed ({err}); reporting without it")
            open_prs, comment_claims = [], []
        if open_prs:
            continue  # someone is already fixing it

        claims = []
        body_claim = find_claim(item.get("body") or "", claim_res)
        if body_claim:
            claims.append((author or "reporter", body_claim))
        claims.extend(comment_claims)

        entry = {
            "key": key,
            "repo": repo,
            "number": item["number"],
            "title": item["title"].strip(),
            "url": item["html_url"],
            "labels": labels,
            "author": author,
            "age": ago(created, now),
            "reason": "new issue" if is_new else "has a watched label, recently labelled or updated",
            "claims": claims,
        }
        if claims:
            if s.get("show_possibly_claimed", True):
                claimed.append(entry)
        else:
            available.append(entry)
        per_repo[repo] = per_repo.get(repo, 0) + 1

    log(f"{len(found)} issue(s) from search; reported per repo: "
        + (", ".join(f"{r.split('/')[1]} {n}" for r, n in sorted(per_repo.items())) or "none"))
    return available, claimed, failed


# ---------------------------------------------------------------- formatting

def meta_line(e):
    labels = ", ".join(e["labels"]) if e["labels"] else "no labels"
    return f"{labels} · by {e['author']} · opened {e['age']} · {e['reason']}"


def claim_line(e):
    who, snippet = e["claims"][0]
    return f'{who}: "{snippet[:120]}"'


def format_text(available, claimed):
    lines = []
    for title, entries in (("Looks available", available), ("Possibly claimed", claimed)):
        if not entries:
            continue
        lines.append(f"== {title} ({len(entries)}) ==")
        for e in entries:
            lines.append(f"[{e['repo']}] #{e['number']} {e['title']}")
            lines.append(f"  {e['url']}")
            lines.append(f"  {meta_line(e)}")
            if e["claims"]:
                lines.append(f"  claim? {claim_line(e)}")
        lines.append("")
    return "\n".join(lines).strip()


def format_html(available, claimed):
    esc = html.escape
    parts = ['<div style="font-family:system-ui,Segoe UI,Arial,sans-serif;font-size:14px;line-height:1.45">']
    for title, entries, color in (("Looks available", available, "#1a7f37"),
                                  ("Possibly claimed", claimed, "#9a6700")):
        if not entries:
            continue
        parts.append(f'<h3 style="color:{color};margin:16px 0 8px">{title} ({len(entries)})</h3>')
        for e in entries:
            parts.append('<div style="margin:0 0 12px">')
            parts.append(f'<div style="color:#57606a;font-size:12px">{esc(e["repo"])}</div>')
            parts.append(f'<a href="{esc(e["url"])}" style="font-weight:600">#{e["number"]} {esc(e["title"])}</a>')
            parts.append(f'<div style="color:#57606a;font-size:12px">{esc(meta_line(e))}</div>')
            if e["claims"]:
                parts.append(f'<div style="color:#9a6700;font-size:12px">claim? {esc(claim_line(e))}</div>')
            parts.append("</div>")
    parts.append("</div>")
    return "".join(parts)


def format_chat(available, claimed, kind):
    """Telegram (HTML) or Discord (Markdown) message blocks, one per issue."""
    blocks = []
    for title, entries in (("✅ Looks available", available), ("⚠️ Possibly claimed", claimed)):
        if not entries:
            continue
        blocks.append(f"<b>{title} ({len(entries)})</b>" if kind == "telegram" else f"**{title} ({len(entries)})**")
        for e in entries:
            if kind == "telegram":
                b = (f"<i>{html.escape(e['repo'])}</i>\n"
                     f"<a href=\"{html.escape(e['url'])}\">#{e['number']} {html.escape(e['title'])}</a>\n"
                     f"{html.escape(meta_line(e))}")
                if e["claims"]:
                    b += f"\nclaim? {html.escape(claim_line(e))}"
            else:
                title_md = e["title"].replace("[", "(").replace("]", ")")
                b = f"*{e['repo']}*\n[#{e['number']} {title_md}](<{e['url']}>)\n{meta_line(e)}"
                if e["claims"]:
                    b += f"\nclaim? {claim_line(e)}"
            blocks.append(b)
    return blocks


def chunk(blocks, limit):
    out, cur = [], ""
    for b in blocks:
        b = b[: limit - 10]
        if cur and len(cur) + len(b) + 2 > limit:
            out.append(cur)
            cur = ""
        cur = f"{cur}\n\n{b}" if cur else b
    if cur:
        out.append(cur)
    return out


# ------------------------------------------------------------------- senders

def env(name, default=""):
    return (os.environ.get(name) or default).strip()


def channels():
    found = {}
    if env("SMTP_USER") and env("SMTP_PASSWORD") and env("EMAIL_TO"):
        found["email"] = send_email
    if env("TELEGRAM_BOT_TOKEN") and env("TELEGRAM_CHAT_ID"):
        found["telegram"] = send_telegram
    if env("DISCORD_WEBHOOK_URL"):
        found["discord"] = send_discord
    return found


def send_email(subject, available, claimed, test=False):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = env("EMAIL_FROM", env("SMTP_USER"))
    msg["To"] = env("EMAIL_TO")
    text = "Test message from your issue watcher. Email works." if test else format_text(available, claimed)
    msg.set_content(text)
    if not test:
        msg.add_alternative(format_html(available, claimed), subtype="html")
    host, port = env("SMTP_HOST", "smtp.gmail.com"), int(env("SMTP_PORT", "465"))
    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) as smtp:
            smtp.login(env("SMTP_USER"), env("SMTP_PASSWORD"))
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            smtp.starttls(context=ctx)
            smtp.login(env("SMTP_USER"), env("SMTP_PASSWORD"))
            smtp.send_message(msg)


def send_telegram(subject, available, claimed, test=False):
    url = f"https://api.telegram.org/bot{env('TELEGRAM_BOT_TOKEN')}/sendMessage"
    blocks = ["Test message from your issue watcher. Telegram works."] if test else \
        [f"<b>{html.escape(subject)}</b>"] + format_chat(available, claimed, "telegram")
    for text in chunk(blocks, 4000):
        http_json(url, {"chat_id": env("TELEGRAM_CHAT_ID"), "text": text,
                        "parse_mode": "HTML", "disable_web_page_preview": True})


def send_discord(subject, available, claimed, test=False):
    blocks = ["Test message from your issue watcher. Discord works."] if test else \
        [f"**{subject}**"] + format_chat(available, claimed, "discord")
    for text in chunk(blocks, 1900):
        http_json(env("DISCORD_WEBHOOK_URL"), {"content": text, "allowed_mentions": {"parse": []}})
        time.sleep(1)


def notify(subject, available, claimed, test=False):
    """Send to every configured channel. Returns True if at least one succeeded."""
    chans = channels()
    if not chans:
        log("No notification channel is configured (see README: repository secrets).")
        return False
    ok = False
    for name, sender in chans.items():
        try:
            sender(subject, available, claimed, test=test)
            log(f"sent via {name}")
            ok = True
        except Exception as err:
            log(f"{name} failed: {err}")
    return ok


# --------------------------------------------------------------------- state

def load_seen():
    try:
        return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_seen(seen, now, forget_days):
    cutoff = now - timedelta(days=forget_days)
    kept = {k: v for k, v in seen.items() if parse_time(v) >= cutoff}
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(kept, indent=1, sort_keys=True) + "\n")


# ---------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=["run", "dry-run", "test-notify"],
                    default=env("WATCHER_MODE") or "run")
    ap.add_argument("--lookback-hours", type=float, default=None)
    ap.add_argument("--config", default=str(ROOT / "config.toml"))
    args = ap.parse_args()

    cfg = tomllib.loads(Path(args.config).read_text())
    s = cfg.setdefault("settings", {})

    if args.mode == "test-notify":
        sys.exit(0 if notify("Issue watcher test", [], [], test=True) else 1)

    lookback = args.lookback_hours
    if lookback is None and env("WATCHER_LOOKBACK_HOURS"):
        try:
            lookback = float(env("WATCHER_LOOKBACK_HOURS"))
        except ValueError:
            log("Ignoring WATCHER_LOOKBACK_HOURS: not a number")
    lookback = lookback if lookback is not None else float(s.get("lookback_hours", 6))
    lookback = min(max(lookback, 0.25), 24 * 30)

    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=lookback)
    seen = load_seen() if args.mode == "run" else {}
    log(f"Mode {args.mode}; looking back {lookback:g} h (since {since:%Y-%m-%d %H:%M} UTC)")

    deadline = time.time() + 7 * 60  # leave time to send before the 10-minute job timeout
    available, claimed, failed = collect(cfg, since, now, seen, deadline)
    log(f"Result: {len(available)} available, {len(claimed)} possibly claimed"
        + (f"; incomplete searches: {'; '.join(failed)}" if failed else ""))

    if not available and not claimed:
        return
    repos = sorted({e["repo"].split("/")[1] for e in available + claimed})
    subject = f"[issue watcher] {len(available)} open issue(s)"
    if claimed:
        subject += f", {len(claimed)} possibly claimed"
    subject += " in " + ", ".join(repos[:4]) + (" …" if len(repos) > 4 else "")

    if args.mode == "dry-run":
        log("\n" + subject + "\n\n" + format_text(available, claimed))
        return

    if not notify(subject, available, claimed):
        log("Nothing was delivered; these issues will be retried next run.")
        sys.exit(1)
    stamp = now.isoformat(timespec="seconds")
    for e in available + claimed:
        seen[e["key"]] = stamp
    save_seen(seen, now, int(s.get("forget_after_days", 21)))


if __name__ == "__main__":
    main()
