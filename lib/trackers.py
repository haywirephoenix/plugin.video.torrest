"""
The public trackers added to magnets (see the "trackers" setting), kept in trackers.json in the
add-on's data folder: [{"url": ..., "enabled": true, "failures": 0}, ...], in the order added.
"""

import io
import json
import logging
import os
import random
import re
import socket
import struct
import threading

try:
    from urllib.parse import urlparse
except ImportError:
    from urlparse import urlparse

import requests

from lib.kodi import ADDON_DATA

TRACKERS_PATH = os.path.join(ADDON_DATA, "trackers.json")
# Seconds a tracker has to answer a check
CHECK_TIMEOUT = 8

DEFAULT_TRACKERS = (
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.demonii.com:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://explodie.org:6969/announce",
    "udp://open.tracker.cl:1337/announce",
    "udp://tracker.qu.ax:6969/announce",
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


def set_enabled(urls):
    """Enables exactly these URLs; a tracker enabled again starts over on failed checks."""
    trackers = load()
    for t in trackers:
        enabled = t["url"] in urls
        if enabled and not t["enabled"]:
            t["failures"] = 0
        t["enabled"] = enabled
    save(trackers)


def remove(urls):
    save([t for t in load() if t["url"] not in urls])


def reset():
    save([tracker(url) for url in DEFAULT_TRACKERS])


def probe(url, timeout=CHECK_TIMEOUT):
    """True when the tracker answers, False when it doesn't, None when it can't be checked (ws)."""
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    try:
        if scheme == "udp":
            return probe_udp(parsed.hostname, parsed.port or 80, timeout)
        if scheme in ("http", "https"):
            return probe_http(url, timeout)
    except (socket.error, ValueError, requests.exceptions.RequestException) as e:
        logging.debug("Tracker %s failed its check: %s", url, e)
        return False
    return None


def probe_udp(host, port, timeout):
    # BEP 15 connect request: protocol id, action 0 (connect), transaction id
    transaction_id = random.getrandbits(32)
    request = struct.pack(">QII", 0x41727101980, 0, transaction_id)
    family, sock_type, proto, _, address = socket.getaddrinfo(host, port, 0, socket.SOCK_DGRAM)[0]
    sock = socket.socket(family, sock_type, proto)
    try:
        sock.settimeout(timeout / 2.0)
        for _ in range(2):
            sock.sendto(request, address)
            try:
                data = sock.recv(16)
            except socket.timeout:
                continue
            return len(data) >= 16 and struct.unpack(">II", data[:8]) == (0, transaction_id)
        return False
    finally:
        sock.close()


def probe_http(url, timeout):
    # An announce for a random torrent: a tracker that answers at all (even with a failure reason) is up
    r = requests.get(url, timeout=timeout, params={
        "info_hash": os.urandom(20), "peer_id": "-TR0000-{:012d}".format(random.getrandbits(39)),
        "port": 6881, "uploaded": 0, "downloaded": 0, "left": 0, "compact": 1, "event": "started"})
    return r.status_code < 500


def check(max_failures):
    """
    Checks the enabled trackers at once. A failed check adds one to a tracker's failures and a
    successful one clears them; max_failures in a row (when above 0) disables it.
    Returns (answered, checked).
    """
    urls = enabled_urls()
    results = {}

    def run(url):
        results[url] = probe(url)

    threads = [threading.Thread(target=run, args=(url,)) for url in urls]
    for thread in threads:
        thread.daemon = True
        thread.start()
    for thread in threads:
        thread.join(CHECK_TIMEOUT + 5)

    # Read again: the list may have been edited while checking
    trackers = load()
    for t in trackers:
        ok = results.get(t["url"])
        if ok is None or not t["enabled"]:
            continue
        t["failures"] = 0 if ok else t["failures"] + 1
        if 0 < max_failures <= t["failures"]:
            logging.info("Disabling tracker %s after %d failed checks", t["url"], t["failures"])
            t["enabled"] = False
    save(trackers)
    checked = [ok for ok in results.values() if ok is not None]
    return sum(1 for ok in checked if ok), len(checked)
