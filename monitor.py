#!/usr/bin/env python3
"""
Competitor monitor for Shopify stores.

Each run:
  1. Pulls every product from /products.json (prices, sale prices, stock, sizes,
     descriptions, tags, images), the collection list, tracked collections and
     tracked pages' visible text.
  2. Compares with the previous snapshot in data/<site>/.
  3. Writes a report to reports/, appends every change to changes.csv,
     sends a Telegram and/or email alert, and saves the new snapshot.

Usage:
  python monitor.py              # normal run
  python monitor.py --dry-run    # compare and print, do not save or notify
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import difflib
import json
import os
import re
import smtplib
import ssl
import sys
import time
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
REPORTS = ROOT / "reports"
CHANGES_CSV = ROOT / "changes.csv"

SESSION = requests.Session()
SESSION.headers["User-Agent"] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

CATEGORY_ORDER = [
    ("new_product", "New products"),
    ("removed_product", "Removed / unpublished products"),
    ("price", "Price changes"),
    ("sale", "Sales started / ended / changed"),
    ("stock", "Stock changes"),
    ("variant", "Sizes / variants added or removed"),
    ("title", "Product renamed"),
    ("description", "Description changed"),
    ("tags", "Tags / type changed"),
    ("images", "Product images changed"),
    ("collection", "Collections"),
    ("ranking", "Ranking changes"),
    ("page_text", "Website text changes"),
    ("page_visual", "New banners / images on pages"),
    ("error", "Errors"),
]
CATEGORY_LABEL = dict(CATEGORY_ORDER)


# ---------------------------------------------------------------- fetching ---

def http_get(url: str, as_json: bool = False, tries: int = 4):
    last = None
    for attempt in range(tries):
        try:
            r = SESSION.get(url, timeout=30)
            if r.status_code == 429:
                time.sleep(15 * (attempt + 1))
                last = RuntimeError("429 rate limited")
                continue
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json() if as_json else r.text
        except (requests.RequestException, ValueError) as e:
            last = e
            time.sleep(4 * (attempt + 1))
    raise RuntimeError(f"Failed to fetch {url}: {last}")


def paged_json(base_url: str, key: str, max_pages: int = 60) -> list[dict]:
    """Shopify public JSON endpoints return max 250 items per page."""
    out: list[dict] = []
    sep = "&" if "?" in base_url else "?"
    for page in range(1, max_pages + 1):
        data = http_get(f"{base_url}{sep}limit=250&page={page}", as_json=True)
        items = (data or {}).get(key) or []
        if not items:
            break
        out.extend(items)
        if len(items) < 250:
            break
        time.sleep(0.8)
    return out


# ----------------------------------------------------------- normalising ---

def money(v) -> float | None:
    if v in (None, "", "0", "0.00", 0):
        return None
    try:
        f = round(float(v), 2)
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


def html_to_text(html: str) -> str:
    if not html:
        return ""
    text = BeautifulSoup(html, "html.parser").get_text(" ")
    return re.sub(r"\s+", " ", text).strip()


def strip_img(src: str) -> str:
    """Image URL without size/version params so the same file compares equal."""
    if not src:
        return ""
    path = urlparse(src if src.startswith("http") else "https:" + src).path
    return re.sub(r"_(\d+x\d*|\d*x\d+|small|medium|large|grande|master)(?=\.)", "", path)


def variant_key(title: str) -> str:
    return re.sub(r"\s+", "", (title or "default").lower())


def normalise_product(p: dict, base: str) -> dict:
    tags = p.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    variants = {}
    for v in p.get("variants") or []:
        variants[variant_key(v.get("title"))] = {
            "title": v.get("title") or "Default",
            "price": money(v.get("price")),
            "compare_at": money(v.get("compare_at_price")),
            "available": bool(v.get("available", True)),
            "sku": v.get("sku") or "",
        }
    images = [strip_img(i.get("src", "")) for i in (p.get("images") or [])]
    return {
        "id": str(p.get("id")),
        "title": (p.get("title") or "").strip(),
        "handle": p.get("handle"),
        "url": f"{base}/products/{p.get('handle')}",
        "type": p.get("product_type") or "",
        "vendor": p.get("vendor") or "",
        "tags": sorted(tags),
        "description": html_to_text(p.get("body_html") or ""),
        "published_at": p.get("published_at") or "",
        "created_at": p.get("created_at") or "",
        "images": images,
        "variants": variants,
    }


def page_snapshot(html: str, ignore: list[re.Pattern]) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    title = (soup.title.get_text(strip=True) if soup.title else "")
    meta = soup.find("meta", attrs={"name": "description"})
    meta_desc = meta.get("content", "").strip() if meta else ""
    for tag in soup(["script", "style", "noscript", "svg", "template", "iframe"]):
        tag.decompose()
    lines, seen = [], set()
    for raw in soup.get_text("\n").split("\n"):
        line = re.sub(r"\s+", " ", raw).strip()
        if len(line) < 3 or line in seen:
            continue
        if any(p.search(line) for p in ignore):
            continue
        seen.add(line)
        lines.append(line)
    images, seen_img = [], set()
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""
        if "/files/" not in src and "/products/" not in src:
            continue
        key = strip_img(src)
        if key and key not in seen_img:
            seen_img.add(key)
            images.append(key)
    return {"title": title, "meta_description": meta_desc, "lines": lines, "images": images}


def fetch_site(site: dict, settings: dict) -> dict:
    base = site["url"].rstrip("/")
    ignore = [re.compile(p, re.I) for p in settings.get("ignore_line_patterns", [])]

    products = {}
    for p in paged_json(f"{base}/products.json", "products"):
        np = normalise_product(p, base)
        products[np["id"]] = np

    collections = {}
    for c in paged_json(f"{base}/collections.json", "collections"):
        collections[c.get("handle")] = {
            "title": c.get("title"),
            "products_count": c.get("products_count"),
        }

    tracked = {}
    for handle in site.get("collections", []) or []:
        items = paged_json(f"{base}/collections/{handle}/products.json", "products", max_pages=20)
        tracked[handle] = [i.get("handle") for i in items]
        time.sleep(0.5)

    pages = {}
    for path in site.get("pages", []) or []:
        html = http_get(f"{base}{path}")
        if html:
            pages[path] = page_snapshot(html, ignore)
        time.sleep(0.5)

    return {
        "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "products": products,
        "collections": collections,
        "tracked_collections": tracked,
        "pages": pages,
    }


# ----------------------------------------------------------------- diffing ---

def fmt_price(v):
    return "none" if v is None else f"${v:,.2f}"


def price_range(p: dict) -> str:
    prices = sorted({v["price"] for v in p["variants"].values() if v["price"]})
    if not prices:
        return "n/a"
    return fmt_price(prices[0]) if len(prices) == 1 else f"{fmt_price(prices[0])} to {fmt_price(prices[-1])}"


def short_text_diff(old: str, new: str, limit: int = 280) -> str:
    ow, nw = old.split(), new.split()
    removed, added = [], []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, ow, nw).get_opcodes():
        if op in ("delete", "replace"):
            removed.append(" ".join(ow[i1:i2]))
        if op in ("insert", "replace"):
            added.append(" ".join(nw[j1:j2]))
    parts = []
    if removed:
        parts.append("REMOVED: " + " | ".join(removed))
    if added:
        parts.append("ADDED: " + " | ".join(added))
    s = "  ".join(parts)
    return s if len(s) <= limit else s[: limit - 3] + "..."


def ev(site, category, product="", detail="", old="", new="", url=""):
    return {
        "site": site, "category": category, "product": product,
        "detail": detail, "old": "" if old is None else str(old),
        "new": "" if new is None else str(new), "url": url,
    }


def diff_products(site: str, old: dict, new: dict, settings: dict) -> list[dict]:
    out = []
    min_move = float(settings.get("min_price_change", 0.01))
    old_by_handle = {p["handle"]: p for p in old.values()}

    for pid, np in new.items():
        op = old.get(pid) or old_by_handle.get(np["handle"])
        if not op:
            sizes = ", ".join(v["title"] for v in np["variants"].values())
            out.append(ev(site, "new_product", np["title"],
                          f"{price_range(np)} | sizes: {sizes} | type: {np['type'] or 'n/a'}"
                          f" | published: {(np['published_at'] or 'n/a')[:10]}",
                          url=np["url"]))
            continue

        name, url = np["title"], np["url"]
        if op["title"] != np["title"]:
            out.append(ev(site, "title", name, "Renamed", op["title"], np["title"], url))

        for vk, nv in np["variants"].items():
            ov = op["variants"].get(vk)
            if not ov:
                out.append(ev(site, "variant", name, f"New size/variant: {nv['title']}",
                              new=fmt_price(nv["price"]), url=url))
                continue
            label = nv["title"]
            if ov["price"] is not None and nv["price"] is not None and abs(nv["price"] - ov["price"]) >= min_move:
                pct = (nv["price"] - ov["price"]) / ov["price"] * 100
                arrow = "UP" if pct > 0 else "DOWN"
                out.append(ev(site, "price", name, f"{label}: {arrow} {pct:+.1f}%",
                              fmt_price(ov["price"]), fmt_price(nv["price"]), url))
            if ov["compare_at"] != nv["compare_at"]:
                if not ov["compare_at"] and nv["compare_at"]:
                    d = f"{label}: SALE STARTED (was {fmt_price(nv['compare_at'])}, now {fmt_price(nv['price'])})"
                elif ov["compare_at"] and not nv["compare_at"]:
                    d = f"{label}: SALE ENDED"
                else:
                    d = f"{label}: compare-at price changed"
                out.append(ev(site, "sale", name, d, fmt_price(ov["compare_at"]), fmt_price(nv["compare_at"]), url))
            if ov["available"] != nv["available"]:
                out.append(ev(site, "stock", name,
                              f"{label}: {'BACK IN STOCK' if nv['available'] else 'SOLD OUT'}",
                              "in stock" if ov["available"] else "sold out",
                              "in stock" if nv["available"] else "sold out", url))
        for vk, ovar in op["variants"].items():
            if vk not in np["variants"]:
                out.append(ev(site, "variant", name, f"Size/variant removed: {ovar['title']}",
                              old=fmt_price(ovar["price"]), url=url))

        if op["description"] != np["description"]:
            out.append(ev(site, "description", name,
                          short_text_diff(op["description"], np["description"]), url=url))

        added_tags = sorted(set(np["tags"]) - set(op["tags"]))
        removed_tags = sorted(set(op["tags"]) - set(np["tags"]))
        if added_tags or removed_tags:
            out.append(ev(site, "tags", name, "Tags changed",
                          ", ".join(removed_tags), ", ".join(added_tags), url))
        if op["type"] != np["type"]:
            out.append(ev(site, "tags", name, "Product type changed", op["type"], np["type"], url))

        if op["images"] != np["images"]:
            added_imgs = len(set(np["images"]) - set(op["images"]))
            removed_imgs = len(set(op["images"]) - set(np["images"]))
            if added_imgs or removed_imgs:
                out.append(ev(site, "images", name,
                              f"{added_imgs} image(s) added, {removed_imgs} removed",
                              len(op["images"]), len(np["images"]), url))

    new_handles = {p["handle"] for p in new.values()}
    for pid, op in old.items():
        if pid not in new and op["handle"] not in new_handles:
            out.append(ev(site, "removed_product", op["title"], price_range(op), url=op["url"]))
    return out


def diff_collections(site: str, base: str, old: dict, new: dict, settings: dict) -> list[dict]:
    out = []
    for h, c in new.get("collections", {}).items():
        if h not in old.get("collections", {}):
            out.append(ev(site, "collection", c["title"], "New collection created", url=f"{base}/collections/{h}"))
    for h, c in old.get("collections", {}).items():
        if h not in new.get("collections", {}):
            out.append(ev(site, "collection", c["title"], "Collection removed", url=f"{base}/collections/{h}"))

    titles = {p["handle"]: p["title"] for p in new.get("products", {}).values()}
    titles.update({p["handle"]: p["title"] for p in old.get("products", {}).values() if p["handle"] not in titles})
    top = int(settings.get("rank_watch_top", 10))
    for h, now in new.get("tracked_collections", {}).items():
        before = old.get("tracked_collections", {}).get(h)
        if before is None:
            continue
        url = f"{base}/collections/{h}"
        added = [x for x in now if x not in before]
        removed = [x for x in before if x not in now]
        if added:
            out.append(ev(site, "collection", h, f"Added to collection: {', '.join(titles.get(x, x) for x in added[:15])}"
                          + (f" (+{len(added) - 15} more)" if len(added) > 15 else ""), url=url))
        if removed:
            out.append(ev(site, "collection", h, f"Removed from collection: {', '.join(titles.get(x, x) for x in removed[:15])}"
                          + (f" (+{len(removed) - 15} more)" if len(removed) > 15 else ""), url=url))
        if before[:top] != now[:top]:
            moves = []
            for i, x in enumerate(now[:top], 1):
                prev = before.index(x) + 1 if x in before else None
                # report new entries and real jumps, not 1-slot slides caused by an insert above
                if prev is None or abs(prev - i) >= 2:
                    moves.append(f"#{i} {titles.get(x, x)} ({'new' if prev is None else f'was #{prev}'})")
            if moves:
                out.append(ev(site, "ranking", h, f"Top {top} order changed: " + "; ".join(moves[:8]), url=url))
    return out


def diff_pages(site: str, base: str, old: dict, new: dict) -> list[dict]:
    out = []
    for path, np in new.get("pages", {}).items():
        op = old.get("pages", {}).get(path)
        if not op:
            continue
        url = f"{base}{path}"
        if op["title"] != np["title"]:
            out.append(ev(site, "page_text", path, "SEO title changed", op["title"], np["title"], url))
        if op["meta_description"] != np["meta_description"]:
            out.append(ev(site, "page_text", path, "Meta description changed",
                          op["meta_description"], np["meta_description"], url))
        old_set, new_set = set(op["lines"]), set(np["lines"])
        added = [l for l in np["lines"] if l not in old_set]
        removed = [l for l in op["lines"] if l not in new_set]
        for l in added[:25]:
            out.append(ev(site, "page_text", path, "Text added", new=l[:300], url=url))
        for l in removed[:25]:
            out.append(ev(site, "page_text", path, "Text removed", old=l[:300], url=url))
        if len(added) > 25 or len(removed) > 25:
            out.append(ev(site, "page_text", path,
                          f"Large change: {len(added)} lines added, {len(removed)} removed (redesign or new campaign?)", url=url))
        new_imgs = [i for i in np.get("images", []) if i not in set(op.get("images", []))]
        if new_imgs:
            out.append(ev(site, "page_visual", path,
                          f"{len(new_imgs)} new image(s): " + ", ".join(f"https://{urlparse(base).netloc}{i}" for i in new_imgs[:5]),
                          url=url))
    return out


# ----------------------------------------------------------------- output ---

def render_report(run_at: dt.datetime, results: dict, sites_meta: dict) -> str:
    stamp = run_at.strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"# Competitor monitor report", f"Run: {stamp}", ""]
    for key, res in results.items():
        meta = sites_meta[key]
        events = res["events"]
        lines.append(f"## {meta['name']} ({meta['url']})")
        lines.append(f"Products live: {res['product_count']}"
                     + (" | BASELINE RUN (first snapshot, nothing to compare yet)" if res["baseline"] else "")
                     + f" | Changes: {len(events)}")
        lines.append("")
        if not events:
            lines.append("No changes since last run.\n")
            continue
        for cat, label in CATEGORY_ORDER:
            items = [e for e in events if e["category"] == cat]
            if not items:
                continue
            lines.append(f"### {label} ({len(items)})")
            for e in items:
                bits = [f"**{e['product']}**" if e["product"] else ""]
                if e["detail"]:
                    bits.append(e["detail"])
                if e["old"] or e["new"]:
                    bits.append(f"`{e['old'] or '-'}` → `{e['new'] or '-'}`")
                txt = " | ".join(b for b in bits if b)
                lines.append(f"- {txt}" + (f" ([link]({e['url']}))" if e["url"] else ""))
            lines.append("")
    return "\n".join(lines)


def render_alert(run_at: dt.datetime, results: dict, sites_meta: dict, report_url: str | None) -> str:
    beirut = run_at.astimezone(ZoneInfo("Asia/Beirut"))
    out = [f"Competitor monitor, {beirut:%a %d %b %H:%M} (Beirut)"]
    for key, res in results.items():
        meta, events = sites_meta[key], res["events"]
        out.append("")
        if res["baseline"]:
            out.append(f"{meta['name']}: baseline saved ({res['product_count']} products). Tracking starts next run.")
            continue
        if not events:
            out.append(f"{meta['name']}: no changes")
            continue
        counts = {}
        for e in events:
            counts[e["category"]] = counts.get(e["category"], 0) + 1
        summary = ", ".join(f"{CATEGORY_LABEL[c].split(' /')[0].lower()}: {n}" for c, n in counts.items())
        out.append(f"{meta['name']} ({len(events)} changes) {summary}")
        priority = ["new_product", "price", "sale", "removed_product", "stock", "variant", "title",
                    "collection", "ranking", "page_text", "error"]
        shown = 0
        for cat in priority:
            for e in [x for x in events if x["category"] == cat]:
                if shown >= 15:
                    break
                tail = f": {e['old'] or '-'} → {e['new'] or '-'}" if (e["old"] or e["new"]) and cat not in ("new_product",) else ""
                out.append(f"• [{CATEGORY_LABEL[cat].split(' /')[0]}] {e['product']} {e['detail']}{tail}".strip()[:300])
                shown += 1
        if len(events) > shown:
            out.append(f"…and {len(events) - shown} more")
    if report_url:
        out += ["", f"Full report: {report_url}"]
    return "\n".join(out)


def append_csv(run_at: dt.datetime, events: list[dict]):
    new_file = not CHANGES_CSV.exists()
    with CHANGES_CSV.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["run_at_utc", "site", "category", "product", "detail", "old", "new", "url"])
        for e in events:
            w.writerow([run_at.isoformat(timespec="seconds"), e["site"], CATEGORY_LABEL[e["category"]],
                        e["product"], e["detail"], e["old"], e["new"], e["url"]])


TELEGRAM_CACHE = DATA / "telegram_chats.json"


def resolve_telegram_chats(token: str, alerts: dict) -> list[str]:
    """Turn @usernames into chat ids. Telegram bots can only message people who
    pressed Start on the bot, and only learn their chat id from getUpdates
    (kept ~24h), so found ids are cached in data/telegram_chats.json."""
    cache = json.loads(TELEGRAM_CACHE.read_text()) if TELEGRAM_CACHE.exists() else {}
    wanted = [u.lstrip("@").lower() for u in alerts.get("telegram_usernames", []) or []]
    if any(u not in cache for u in wanted):
        try:
            r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=30).json()
            for upd in r.get("result", []):
                msg = upd.get("message") or upd.get("my_chat_member") or {}
                chat, sender = msg.get("chat") or {}, msg.get("from") or {}
                uname = (sender.get("username") or chat.get("username") or "").lower()
                if uname in wanted and chat.get("type") == "private":
                    cache[uname] = str(chat["id"])
            save_json(TELEGRAM_CACHE, cache)
        except Exception as e:
            print(f"Telegram getUpdates failed: {e}", file=sys.stderr)
    ids = [cache[u] for u in wanted if u in cache]
    missing = [u for u in wanted if u not in cache]
    if missing:
        print(f"Telegram: @{', @'.join(missing)} not found yet. They must open the bot and press Start.",
              file=sys.stderr)
    ids += [str(c) for c in alerts.get("telegram_chat_ids", []) or []]
    if os.getenv("TELEGRAM_CHAT_ID"):
        ids += [c.strip() for c in os.environ["TELEGRAM_CHAT_ID"].split(",") if c.strip()]
    return list(dict.fromkeys(ids))


def send_telegram(text: str, alerts: dict) -> str:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        return "Telegram: skipped (TELEGRAM_BOT_TOKEN secret not set)"
    chats = resolve_telegram_chats(token, alerts)
    if not chats:
        return "Telegram: no recipients yet (press Start on the bot, then rerun)"
    sent = 0
    for chat in chats:
        ok = True
        for i in range(0, len(text), 3800):
            r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                              json={"chat_id": chat, "text": text[i:i + 3800],
                                    "disable_web_page_preview": True}, timeout=30)
            if not r.ok:
                ok = False
                print(f"Telegram error for {chat}: {r.status_code} {r.text}", file=sys.stderr)
        sent += ok
    return f"Telegram: sent to {sent}/{len(chats)} chat(s)"


def md_to_html(md: str) -> str:
    """Tiny markdown renderer for the report email (headings, bullets, bold, code, links)."""
    import html as h
    out = []
    for line in md.splitlines():
        s = h.escape(line)
        s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
        s = re.sub(r"`(.+?)`", r"<code style='background:#f3f3f3;padding:1px 4px;border-radius:4px'>\1</code>", s)
        s = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"<a href='\2'>\1</a>", s)
        if s.startswith("### "):
            out.append(f"<h3 style='margin:18px 0 6px;font-size:15px'>{s[4:]}</h3>")
        elif s.startswith("## "):
            out.append(f"<h2 style='margin:26px 0 4px;font-size:18px;border-bottom:1px solid #ddd;padding-bottom:4px'>{s[3:]}</h2>")
        elif s.startswith("# "):
            out.append(f"<h1 style='font-size:20px'>{s[2:]}</h1>")
        elif s.startswith("- "):
            out.append(f"<div style='margin:4px 0 4px 12px'>• {s[2:]}</div>")
        elif s.strip():
            out.append(f"<p style='margin:4px 0'>{s}</p>")
    return ("<div style='font-family:Arial,sans-serif;font-size:14px;color:#1c1c1c;max-width:760px'>"
            + "\n".join(out) + "</div>")


def send_email(subject: str, summary: str, report_md: str, alerts: dict) -> str:
    host = os.getenv("SMTP_HOST") or "smtp.gmail.com"
    user, pw = os.getenv("SMTP_USER"), os.getenv("SMTP_PASS")
    to = list(alerts.get("email_to", []) or [])
    if os.getenv("EMAIL_TO"):
        to += [e.strip() for e in os.environ["EMAIL_TO"].split(",") if e.strip()]
    to = list(dict.fromkeys(to))
    if not (user and pw):
        return "Email: skipped (SMTP_USER / SMTP_PASS secrets not set)"
    if not to:
        return "Email: no recipients in config.yaml"
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, f"Competitor Monitor <{user}>", ", ".join(to)
    msg.set_content(summary + "\n\n" + report_md)
    msg.add_alternative(
        "<pre style='font-family:Arial,sans-serif;font-size:14px;white-space:pre-wrap;"
        "background:#F3F3C8;padding:12px;border-radius:10px'>"
        + __import__("html").escape(summary) + "</pre>" + md_to_html(report_md), subtype="html")
    port = int(os.getenv("SMTP_PORT") or "465")
    try:
        with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=30) as s:
            s.login(user, pw)
            s.send_message(msg)
        return f"Email: sent to {', '.join(to)}"
    except Exception as e:
        print(f"Email error: {e}", file=sys.stderr)
        return f"Email: FAILED ({e})"


def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1, sort_keys=True, ensure_ascii=False), encoding="utf-8")


# -------------------------------------------------------------------- main ---

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--dry-run", action="store_true", help="compare and print only")
    ap.add_argument("--test-alerts", action="store_true",
                    help="send a test message to every email/Telegram recipient and exit")
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    settings = cfg.get("settings", {}) or {}
    alerts = cfg.get("alerts", {}) or {}

    if args.test_alerts:
        names = ", ".join(s["name"] for s in cfg["sites"])
        now = dt.datetime.now(ZoneInfo("Asia/Beirut"))
        text = (f"Competitor monitor test, {now:%a %d %b %H:%M} (Beirut)\n\n"
                f"Alerts are working. Watching: {names}.\n"
                "You will get a message after each check (6am and 4pm Beirut) whenever "
                "they add products, change prices, start sales, go out of stock or edit their website.")
        sample = ("# Sample report\n\n## Example Store\n\n### Price changes (1)\n"
                  "- **Aventus Creed** | 100ml: DOWN -12.0% | `$24.99` → `$21.99`\n")
        results = [send_telegram(text, alerts),
                   send_email("Competitor monitor: test alert", text, sample, alerts)]
        print("\n".join(results))
        if any("FAILED" in r or "skipped" in r or "no recipients" in r for r in results):
            sys.exit(1)
        return
    run_at = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    results, sites_meta, all_events = {}, {}, []

    for site in cfg["sites"]:
        key, base = site["key"], site["url"].rstrip("/")
        sites_meta[key] = site
        snap_path = DATA / key / "snapshot.json"
        old = json.loads(snap_path.read_text(encoding="utf-8")) if snap_path.exists() else None
        print(f"Fetching {site['name']}...", flush=True)
        try:
            new = fetch_site(site, settings)
        except Exception as e:  # keep going with the other sites
            err = ev(key, "error", site["name"], f"Fetch failed: {e}")
            results[key] = {"events": [err], "product_count": 0, "baseline": False}
            all_events.append(err)
            continue

        events, save = [], True
        if old:
            ratio = len(new["products"]) / max(len(old["products"]), 1)
            if len(old["products"]) > 20 and ratio < float(settings.get("min_product_ratio", 0.5)):
                events.append(ev(key, "error", site["name"],
                                 f"Product count dropped {len(old['products'])} → {len(new['products'])}. "
                                 "Looks like a failed/blocked fetch, snapshot NOT updated."))
                save = False
            else:
                events += diff_products(key, old["products"], new["products"], settings)
                events += diff_collections(key, base, old, new, settings)
                events += diff_pages(key, base, old, new)
        results[key] = {"events": events, "product_count": len(new["products"]), "baseline": old is None}
        all_events += events
        if save and not args.dry_run:
            save_json(snap_path, new)
        print(f"  {len(new['products'])} products, {len(events)} changes")

    report = render_report(run_at, results, sites_meta)
    repo, server = os.getenv("GITHUB_REPOSITORY"), os.getenv("GITHUB_SERVER_URL", "https://github.com")
    report_name = f"{run_at:%Y-%m-%d_%H%M}.md"
    report_url = f"{server}/{repo}/blob/main/reports/{report_name}" if repo else None

    if args.dry_run:
        print(report)
        return

    REPORTS.mkdir(exist_ok=True)
    (REPORTS / report_name).write_text(report, encoding="utf-8")
    (REPORTS / "latest.md").write_text(report, encoding="utf-8")
    if all_events:
        append_csv(run_at, all_events)

    anything = bool(all_events) or any(r["baseline"] for r in results.values())
    if anything or settings.get("notify_when_no_changes"):
        alert = render_alert(run_at, results, sites_meta, report_url)
        print("\n" + alert)
        print(send_telegram(alert, alerts))
        beirut = run_at.astimezone(ZoneInfo("Asia/Beirut"))
        print(send_email(f"Competitor monitor: {len(all_events)} changes ({beirut:%d %b %H:%M} Beirut)",
                         alert, report, alerts))


if __name__ == "__main__":
    main()
