"""
Play status for skins, through Home window (10000) properties.

During playback, a skin that sets "ultra.torrent.osd" to "1" gets the download status as
"torrest.download.*" properties (see publish_download) instead of Torrest's overlay and
background progress dialog.

A skin that draws its own buffering UI sets "ultra.torrent.ui" to "1"; while it is set Torrest
shows none of its own buffering/progress dialogs. Either way, while a play starts and buffers
Torrest keeps the "torrest.*" properties below up to date, and a skin can cancel the play by
setting "torrest.cancel" to "1". skin.ultra's loading screen also sets "ultra.loading.cancel"
when it's left with Back, and clears it when the next play starts: that cancels too, so a Back
pressed before this play started (between choosing a source and Torrest starting) still counts.
"""

import threading
import time
import uuid

import xbmc
import xbmcgui

from lib.kodi import WINDOW_HOME
from lib.torrest.api import STATUS_FINISHED, STATUS_SEEDING, STATUS_PAUSED
from lib.utils import sizeof_fmt

SKIN_UI_PROPERTY = "ultra.torrent.ui"
SKIN_OSD_PROPERTY = "ultra.torrent.osd"
DOWNLOAD_PROGRESS_PROPERTY = "torrest.download.progress"
DOWNLOAD_SPEED_PROPERTY = "torrest.download.speed"
DOWNLOAD_PEERS_PROPERTY = "torrest.download.peers"
CANCEL_PROPERTY = "torrest.cancel"
SKIN_CANCEL_PROPERTY = "ultra.loading.cancel"
SKIN_LOADING_WINDOW = 1111  # the skin's loading screen
STATE_PROPERTY = "torrest.state"
# Which play owns the properties, so a play that ends late doesn't clear a newer play's status
OWNER_PROPERTY = "torrest.owner"
# The newest play (never cleared), so older plays still running know they were replaced
LATEST_PROPERTY = "torrest.latest"
# The newest play's torrent, so an older play's stop handling leaves it alone (no pause, no delete)
LATEST_TORRENT_PROPERTY = "torrest.latest.torrent"
PROPERTIES = ("state", "progress", "label", "speed", "peers", "seeds", "name", "error")

STATE_STARTING = "starting"
STATE_BUFFERING = "buffering"
STATE_PLAYING = "playing"
STATE_FAILED = "failed"

PLAYING_CLEAR_DELAY = 3
FAILED_CLEAR_DELAY = 5


def skin_ui_enabled():
    return xbmcgui.Window(WINDOW_HOME).getProperty(SKIN_UI_PROPERTY) == "1"


def skin_osd_enabled():
    return xbmcgui.Window(WINDOW_HOME).getProperty(SKIN_OSD_PROPERTY) == "1"


def is_downloading(status):
    return status.state not in (STATUS_FINISHED, STATUS_SEEDING, STATUS_PAUSED) and not status.paused


def publish_download(progress, download_rate, peers=None):
    home = xbmcgui.Window(WINDOW_HOME)
    home.setProperty(DOWNLOAD_PROGRESS_PROPERTY, str(max(0, min(100, int(progress)))))
    home.setProperty(DOWNLOAD_SPEED_PROPERTY, sizeof_fmt(download_rate) + "/s")
    if peers is None:
        home.clearProperty(DOWNLOAD_PEERS_PROPERTY)
    else:
        home.setProperty(DOWNLOAD_PEERS_PROPERTY, str(peers))


def clear_download():
    home = xbmcgui.Window(WINDOW_HOME)
    for key in (DOWNLOAD_PROGRESS_PROPERTY, DOWNLOAD_SPEED_PROPERTY, DOWNLOAD_PEERS_PROPERTY):
        home.clearProperty(key)


def rate_fmt(num):
    for unit in ("B", "kB", "MB", "GB"):
        if abs(num) < 1000.0:
            return "{:.1f} {}/s".format(num, unit)
        num /= 1000.0
    return "{:.1f} TB/s".format(num)


class PlayStatus(object):
    def __init__(self):
        self._home = xbmcgui.Window(WINDOW_HOME)
        self._token = uuid.uuid4().hex
        self._skin_ui = False
        self._failed_time = None
        self.state = None

    @property
    def skin_ui(self):
        # Sticky: once the skin asked for it, this play leaves Torrest's dialogs out
        if not self._skin_ui:
            self._skin_ui = skin_ui_enabled()
        return self._skin_ui

    def _owns(self):
        return self._home.getProperty(OWNER_PROPERTY) == self._token

    def _set(self, **values):
        for key, value in values.items():
            self._home.setProperty("torrest." + key, "" if value is None else str(value))

    def start(self):
        self._home.clearProperty(CANCEL_PROPERTY)
        self._clear_properties()
        self._home.setProperty(OWNER_PROPERTY, self._token)
        self._home.setProperty(LATEST_PROPERTY, self._token)
        self._home.clearProperty(LATEST_TORRENT_PROPERTY)
        self.state = STATE_STARTING
        self._set(state=STATE_STARTING, progress=0)

    def update(self, state, label, progress=0, torrent_status=None, name=None):
        if not self._owns():
            return
        self.state = state
        values = dict(state=state, label=label, progress=max(0, min(100, int(progress))))
        if torrent_status is not None:
            values.update(speed=rate_fmt(torrent_status.download_rate), peers=torrent_status.peers,
                          seeds=torrent_status.seeders)
        if name is not None:
            values["name"] = name
        self._set(**values)

    def superseded(self):
        """True once a newer play started (e.g. another episode was chosen while this one buffered)."""
        return self._home.getProperty(LATEST_PROPERTY) != self._token

    def set_torrent(self, info_hash):
        if not self.superseded():
            self._home.setProperty(LATEST_TORRENT_PROPERTY, info_hash)

    def torrent_taken(self, info_hash):
        """True when a newer play is playing this torrent (the same title played again)."""
        return self.superseded() and self._home.getProperty(LATEST_TORRENT_PROPERTY) == info_hash

    def cancel_requested(self):
        return "1" in (self._home.getProperty(CANCEL_PROPERTY), self._home.getProperty(SKIN_CANCEL_PROPERTY))

    def playing(self, label):
        if not self._owns():
            return
        self.state = STATE_PLAYING
        self._set(state=STATE_PLAYING, progress=100, label=label)
        # Not while the skin's loading screen is up: it closes once the picture shows (after Kodi's
        # refresh rate switch), and closing it now left an empty fullscreen window for seconds
        if (self.skin_ui and not xbmc.getCondVisibility("Window.IsActive(fullscreenvideo)")
                and not xbmc.getCondVisibility("Window.IsVisible({})".format(SKIN_LOADING_WINDOW))):
            # Kodi doesn't go fullscreen when playback starts under a modal dialog (e.g. a video
            # info page left open after the skin's loading screen closed): the video would play
            # unseen behind it.
            xbmc.executebuiltin("Dialog.Close(all,true)")
            xbmc.executebuiltin("ActivateWindow(fullscreenvideo)")
        timer = threading.Timer(PLAYING_CLEAR_DELAY, self.clear)
        timer.daemon = True
        timer.start()

    def failed(self, error):
        """Shows the error (the skin clears it on its own after a moment), or clears the status
        right away when there's no error to show (canceled, aborted)."""
        if error is None:
            self.clear()
        elif self._owns():
            self.state = STATE_FAILED
            self._failed_time = time.time()
            self._set(state=STATE_FAILED, error=error, label=error)

    def linger(self, monitor=None):
        """After failed(error): keeps the error up for FAILED_CLEAR_DELAY seconds, then clears it."""
        if self._failed_time is None:
            return
        remaining = FAILED_CLEAR_DELAY - (time.time() - self._failed_time)
        if remaining > 0:
            (monitor or xbmc.Monitor()).waitForAbort(remaining)
        self.clear()

    def _clear_properties(self):
        for key in PROPERTIES:
            self._home.clearProperty("torrest." + key)

    def clear(self):
        if self._owns():
            self._clear_properties()
            self._home.clearProperty(CANCEL_PROPERTY)
            self._home.clearProperty(OWNER_PROPERTY)
