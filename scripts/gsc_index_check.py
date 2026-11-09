#!/usr/bin/env python3
"""Karaoke Blogger post URL index-status batch check, Google Search Console URL Inspection API.
Runs server-side only. The service-account secret must never be included in Blogger HTML.
"""
import json
import os
import re
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import requests
from google.oauth2 import service_account
from google.auth.transport.requests import AuthorizedSession

SITE_URL = os.environ.get("GSC_SITE_URL", "https://karaokemuzik.blogspot.com/")
SITE_URL = SITE_URL.rstrip("/") + "/"
MAX_POSTS = max(1, min(200, int(os.environ.get("GSC_MAX_POSTS", "80"))))
OUTPUT = Path(os.environ.get("GSC_OUTPUT", "index-status.json"))
SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"
ENDPOINT = "https://searchconsole.googleapis.com/v1/urlInspection/index:inspect"
TIMEOUT = 25


def valid_post_url(raw):
    try:
        p = urllib.parse.urlsplit(raw)
        site = urllib.parse.urlsplit(SITE_URL)
        if p.scheme != "https" or p.netloc.lower() != site.netloc.lower():
            return None
        if not re.match(r"^/\d{4}/\d{2}/[^/]+\.html$", p.path):
            return None
        return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, "", ""))
    except (ValueError, TypeError):
        return None


def extract_entries_from_json_feed(url):
    res = requests.get(url, timeout=TIMEOUT, headers={"User-Agent": "ForyumIndexMonitor/1.0"})
    res.raise_for_status()
    feed = res.json().get("feed", {})
    posts = []
    for entry in feed.get("entry", []):
        for link in entry.get("link", []):
            if link.get("rel") == "alternate" and link.get("href"):
                normalized = valid_post_url(link["href"])
                if normalized:
                    posts.append(normalized)
                break
    return posts


def extract_entries_from_atom(url):
    res = requests.get(url, timeout=TIMEOUT, headers={"User-Agent": "ForyumIndexMonitor/1.0"})
    res.raise_for_status()
    root = ET.fromstring(res.content)
    ns = {"a": "http://www.w3.org/2005/Atom"}
    posts = []
    for entry in root.findall("a:entry", ns):
        for link in entry.findall("a:link", ns):
            if link.get("rel") == "alternate":
                normalized = valid_post_url(link.get("href", ""))
                if normalized:
                    posts.append(normalized)
                break
    return posts


def extract_sitemaps(url, depth=0):
    if depth > 2:
        return []
    r = requests.get(url, timeout=TIMEOUT, headers={"User-Agent": "ForyumIndexMonitor/1.0"})
    r.raise_for_status()
    root = ET.fromstring(r.content)
    tag = root.tag.split("}")[-1]
    if tag == "urlset":
        return [valid_post_url(x.text) for x in root.findall("{*}url/{*}loc") if x.text]
    if tag == "sitemapindex":
        found = []
        for item in root.findall("{*}sitemap/{*}loc")[:3]:
            if item.text and item.text.startswith(SITE_URL):
                found.extend(extract_sitemaps(item.text, depth + 1))
        return found
    return []


def collect_recent_posts():
    # Blogger feed normally returns the latest published posts first.
    feed_url = SITE_URL + "feeds/posts/default?alt=json&max-results=" + str(MAX_POSTS)
    try:
        urls = extract_entries_from_json_feed(feed_url)
        if urls:
            print("Latest posts from Blogger JSON feed:", len(urls))
            return list(dict.fromkeys(urls))[:MAX_POSTS]
    except Exception as e:
        print("JSON feed unavailable:", str(e)[:160])
    feed_url = SITE_URL + "feeds/posts/default?alt=atom&max-results=" + str(MAX_POSTS)
    try:
        urls = extract_entries_from_atom(feed_url)
        if urls:
            print("Latest posts from Blogger Atom feed:", len(urls))
            return list(dict.fromkeys(urls))[:MAX_POSTS]
    except Exception as e:
        print("Atom feed unavailable:", str(e)[:160])
    try:
        urls = [u for u in extract_sitemaps(SITE_URL + "sitemap.xml") if u]
        if urls:
            # Sitemap order is not guaranteed. This is an approximate fallback.
            print("Fallback sitemap post URLs:", len(urls))
            return list(dict.fromkeys(urls))[-MAX_POSTS:]
    except Exception as e:
        print("Sitemap unavailable:", str(e)[:160])
    raise RuntimeError("No Blogger posts found; check feed settings / site accessibility.")


def get_session():
    raw = os.environ.get("GSC_SERVICE_ACCOUNT_JSON", "")
    if not raw:
        raise RuntimeError("GitHub Actions secret GSC_SERVICE_ACCOUNT_JSON is missing.")
    data = json.loads(raw)
    credentials = service_account.Credentials.from_service_account_info(data, scopes=[SCOPE])
    return AuthorizedSession(credentials)


def index_status(sess, url):
    body = {"inspectionUrl": url, "siteUrl": SITE_URL, "languageCode": "tr-TR"}
    r = sess.post(ENDPOINT, json=body, timeout=TIMEOUT)
    if r.status_code in (401, 403, 429):
        raise RuntimeError(f"Google API failed with {r.status_code}: {r.text[:350]}")
    r.raise_for_status()
    item = r.json().get("inspectionResult", {}).get("indexStatusResult", {})
    verdict = item.get("verdict", "VERDICT_UNSPECIFIED")
    # Google Search Console: PASS means in index, FAIL/NEUTRAL not in index.
    status = {"PASS": "yes", "FAIL": "no", "NEUTRAL": "no"}.get(verdict, "unknown")
    # A canonical on another page may indicate a duplicate: show as not indexed itself.
    google_canonical = item.get("googleCanonical")
    if status == "yes" and google_canonical and valid_post_url(google_canonical) != valid_post_url(url):
        status = "no"
    return {
        "status": status,
        "verdict": verdict,
        "coverage": item.get("coverageState", ""),
        "googleCanonical": google_canonical or "",
        "lastCrawlTime": item.get("lastCrawlTime", ""),
    }


def main():
    posts = collect_recent_posts()
    session = get_session()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    items = {}
    for n, url in enumerate(posts, 1):
        status = index_status(session, url)
        key = urllib.parse.urlsplit(url).path
        items[key] = status
        print(f"[{n}/{len(posts)}] {status['status']:7} {key}")
        time.sleep(0.15)
    payload = {"site": SITE_URL, "updatedAt": now, "checkedCount": len(items), "items": items}
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("Written", OUTPUT, "index verified:", sum(x["status"] == "yes" for x in items.values()))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("ERROR:", exc, file=sys.stderr)
        sys.exit(1)
