#!/usr/bin/env python3
import hashlib
import json
import random
import re
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup

CONFIG_PATH = "config.json"
STATE_PATH = "state.json"

# ---- Tunables (good for up to 50 pages) ----
REQUEST_TIMEOUT_S = 25
MAX_RETRIES = 2
RETRY_BACKOFF_S = 2.0
DELAY_BETWEEN_REQUESTS_S = 0.6   # ~50 pages ≈ ~30 seconds
ALERT_COOLDOWN_S = 0      # 30 min: avoid repeated alerts for same URL 60 * 30

def load_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default

def save_json(path: str, obj) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
    import os
    os.replace(tmp, path)

def notify_ntfy(topic: str, title: str, message: str, priority: str = "default") -> None:
    url = f"https://ntfy.sh/{topic}"
    headers = {"Title": title, "Priority": priority}
    requests.post(url, data=message.encode("utf-8"), headers=headers, timeout=20)

@dataclass
class Watch:
    name: str
    url: str
    mode: str = "hash"               # "hash" | "keyword"
    keyword: Optional[str] = None
    selector: Optional[str] = None   # CSS selector; limits what we hash
    headers: Optional[Dict[str, str]] = None

def normalize_text(html: str, selector: Optional[str]) -> str:
    soup = BeautifulSoup(html, "lxml")

    if selector:
        node = soup.select_one(selector)
        if node is not None:
            soup = BeautifulSoup(str(node), "lxml")

    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    text = soup.get_text("\n", strip=True)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text

def sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

def fetch_with_retries(url: str, headers: Optional[Dict[str, str]]) -> str:
    h = dict(headers or {})
    h.setdefault("User-Agent", "webwatch/1.0 (+github actions monitor)")
    last_err = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            r = requests.get(url, headers=h, timeout=REQUEST_TIMEOUT_S)
            r.raise_for_status()
            return r.text
        except Exception as e:
            last_err = e
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF_S * (attempt + 1))
    raise last_err  # type: ignore

def should_alert_now(entry: dict) -> bool:
    last_alert_ts = entry.get("last_alert_ts")
    if not last_alert_ts:
        return True
    return (time.time() - last_alert_ts) >= ALERT_COOLDOWN_S

def main() -> int:
    cfg = load_json(CONFIG_PATH, {})
    topic = cfg.get("ntfy_topic")
    watches_cfg = cfg.get("watches", [])

    if not topic:
        raise SystemExit("Missing ntfy_topic in config.json")
    if not watches_cfg:
        raise SystemExit("No watches configured in config.json")
    if len(watches_cfg) > 50:
        raise SystemExit("Config has more than 50 watches; trim to <= 50.")

    watches: List[Watch] = [Watch(**w) for w in watches_cfg]
    state = load_json(STATE_PATH, {})  # url -> {hash/found, ts, last_alert_ts}

    random.shuffle(watches)

    changes: List[Tuple[Watch, str]] = []
    errors: List[Tuple[Watch, str]] = []

    for i, w in enumerate(watches):
        try:
            html = fetch_with_retries(w.url, w.headers)
            text = normalize_text(html, w.selector)

            entry = state.get(w.url, {})

            if w.mode == "keyword":
                if not w.keyword:
                    raise ValueError("keyword mode requires 'keyword'")
                found = (w.keyword in text)
                prev_found = entry.get("found")

                if prev_found is None:
                    state[w.url] = {"found": found, "ts": int(time.time())}
                elif found != prev_found:
                    if should_alert_now(entry):
                        changes.append((w, f"Keyword '{w.keyword}' changed: {prev_found} → {found}"))
                        entry["last_alert_ts"] = int(time.time())
                    entry["found"] = found
                    entry["ts"] = int(time.time())
                    state[w.url] = entry

            else:
                h = sha256(text)
                prev = entry.get("hash")
                if prev is None:
                    state[w.url] = {"hash": h, "ts": int(time.time())}
                elif h != prev:
                    if should_alert_now(entry):
                        changes.append((w, "Content changed"))
                        entry["last_alert_ts"] = int(time.time())
                    entry["hash"] = h
                    entry["ts"] = int(time.time())
                    state[w.url] = entry
                else:
                    entry["ts"] = int(time.time())
                    state[w.url] = entry

        except Exception as e:
            errors.append((w, f"{type(e).__name__}: {e}"))

        if i < len(watches) - 1:
            time.sleep(DELAY_BETWEEN_REQUESTS_S)

    save_json(STATE_PATH, state)

    if changes:
        lines = [f"- {w.name}: {reason}\n  {w.url}" for (w, reason) in changes]
        notify_ntfy(topic, f"Webwatch: {len(changes)} change(s)", "\n".join(lines), "high")

    if errors:
        # aggregate and keep low priority
        lines = [f"- {w.name}\n  {w.url}\n  {err}" for (w, err) in errors[:10]]
        extra = "" if len(errors) <= 10 else f"\n(+{len(errors)-10} more errors)"
        notify_ntfy(topic, f"Webwatch: {len(errors)} error(s)", "\n".join(lines) + extra, "low")

    return 0

if __name__ == "__main__":
    raise SystemExit(main())
