#!/usr/bin/env python3
import difflib
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
ALERT_COOLDOWN_S = 0             # set to 60*30 for 30 min cooldown

# ---- Diff / storage limits ----
MAX_DIFF_LINES = 40              # keep notifications readable
MAX_TEXT_CHARS_STORED = 50_000   # limit state.json size


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
    selector: Optional[str] = None   # CSS selector; limits what we hash/diff
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


def clamp_text(s: str) -> str:
    return s[:MAX_TEXT_CHARS_STORED]


def make_diff(old_text: str, new_text: str) -> str:
    old_lines = old_text.splitlines()
    new_lines = new_text.splitlines()
    diff_lines = list(
        difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile="before",
            tofile="after",
            lineterm="",
        )
    )

    if not diff_lines:
        return "(No textual diff found)"

    if len(diff_lines) > MAX_DIFF_LINES:
        head = diff_lines[: MAX_DIFF_LINES // 2]
        tail = diff_lines[-MAX_DIFF_LINES // 2 :]
        diff_lines = head + ["... (diff truncated) ..."] + tail

    return "\n".join(diff_lines)


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
    state = load_json(STATE_PATH, {})  # url -> {hash/text or found, ts, last_alert_ts}

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
                    entry["ts"] = int(time.time())
                    state[w.url] = entry

            else:
                h = sha256(text)
                prev = entry.get("hash")

                if prev is None:
                    # baseline: store hash + text so future runs can diff
                    state[w.url] = {"hash": h, "text": clamp_text(text), "ts": int(time.time())}

                elif h != prev:
                    old_text = entry.get("text", "")
                    diff_text = make_diff(old_text, text)

                    if should_alert_now(entry):
                        changes.append((w, diff_text))
                        entry["last_alert_ts"] = int(time.time())

                    entry["hash"] = h
                    entry["text"] = clamp_text(text)
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
        blocks = []
        for (w, diff_text) in changes:
            blocks.append(f"{w.name}\n{w.url}\n{diff_text}")
        notify_ntfy(topic, f"Webwatch: {len(changes)} change(s)", "\n\n---\n\n".join(blocks), "high")

    if errors:
        lines = [f"- {w.name}\n  {w.url}\n  {err}" for (w, err) in errors[:10]]
        extra = "" if len(errors) <= 10 else f"\n(+{len(errors)-10} more errors)"
        notify_ntfy(topic, f"Webwatch: {len(errors)} error(s)", "\n".join(lines) + extra, "low")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
