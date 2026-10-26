"""
The public trackers added to magnets (see the "trackers" setting), kept in trackers.json in the
add-on's data folder: [{"url": ..., "enabled": true, "failures": 0}, ...], in the order added.
"""

import io
import json
import logging
import os
import re

from lib.kodi import ADDON_DATA

TRACKERS_PATH = os.path.join(ADDON_DATA, "trackers.json")

DEFAULT_TRACKERS = (
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.demonii.com:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://explodie.org:6969/announce",
    "udp://tracker.openbittorrent.com:6969/announce",
    "udp://tracker.tiny-vps.com:6969/announce",
)

TRACKER_RE = re.compile(r"(?:udp|https?|wss?)://[^\s,;\"'<>]+", re.IGNORECASE)


def tracker(url):
    return {"url": url, "enabled": True, "failures": 0}


def load():
    try:
        with io.open(TRACKERS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (IOError, OSError, ValueError):
        return [tracker(url) for url in DEFAULT_TRACKERS]


def save(trackers):
    tmp_path = TRACKERS_PATH + ".tmp"
    with io.open(tmp_path, "w", encoding="utf-8") as f:
        f.write(json.dumps(trackers, indent=2, ensure_ascii=False))
    if os.path.exists(TRACKERS_PATH):
        os.remove(TRACKERS_PATH)
    os.rename(tmp_path, TRACKERS_PATH)


def enabled_urls():
    return [t["url"] for t in load() if t["enabled"]]


def parse(text):
    """Tracker URLs found in pasted text (one per line, or separated by spaces or commas)."""
    return TRACKER_RE.findall(text or "")


def add(urls):
    """Adds the URLs not in the list yet. Returns how many were added."""
    trackers = load()
    known = set(t["url"] for t in trackers)
    added = 0
    for url in urls:
        if url not in known:
            known.add(url)
            trackers.append(tracker(url))
            added += 1
    save(trackers)
    logging.info("Added %d trackers", added)
    return added


def reset():
    save([tracker(url) for url in DEFAULT_TRACKERS])
