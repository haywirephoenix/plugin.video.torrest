"""
The torrent manager: a window (TorrestManager.xml) with the daemon's torrents as a table, kept up
to date about every second. A torrent (or a folder) expands in place to show what's in it; files
play when pressed; the context menu has the torrent list's actions.

A skin can draw it its own way with a TorrestManager.xml of its own: see that file for the ids and
row properties it uses.
"""
import logging
import threading

import requests
from xbmc import Monitor
from xbmcgui import WindowXML, Dialog, ListItem, ACTION_CONTEXT_MENU, ACTION_NAV_BACK, ACTION_PARENT_DIR, \
    ACTION_PREVIOUS_MENU

from lib import navigation as nav, settings
from lib.kodi import translate, notification
from lib.kodi_formats import is_music, is_picture, is_text, is_video
from lib.torrest.api import TorrestError, STATUS_SEEDING, STATUS_PAUSED
from lib.utils import sizeof_fmt

ADD_BUTTON_ID = 100
LIST_ID = 200
REFRESH_INTERVAL = 1
INDENT = "      "


class Row(object):
    """A row of the table: a torrent, or one of its folders or files (data is the daemon's
    Torrent, Folder or File, with its status)."""

    def __init__(self, kind, key, info_hash, level, data, paused=False):
        self.kind = kind
        self.key = key
        self.info_hash = info_hash
        self.level = level
        self.data = data
        # Whether its torrent is paused (a file of a paused torrent doesn't download)
        self.paused = paused
        self.item = None

    @property
    def expandable(self):
        return self.kind != "file"


def speed(rate):
    return sizeof_fmt(rate) + "/s"


def state_string(status):
    return translate(30228) if status.paused else nav.get_state_string(status.state)


class TorrentManager(WindowXML):
    def __init__(self, *args, **kwargs):
        super(TorrentManager, self).__init__(*args, **kwargs)
        self._rows = []
        self._expanded = set()
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        # Whether the list should take the focus once it has rows (an empty list can't have it)
        self._focus_list = True
        # A built-in to run once the window has closed (a play)
        self.after = None

    # ------------------------------------------------------------ window events

    def onInit(self):
        with self._lock:
            # The list is drawn again from scratch (also when coming back from a picture)
            self._rows = []
            self._focus_list = self.getFocusId() in (0, ADD_BUTTON_ID, LIST_ID)
        if self._thread is None:
            self._thread = threading.Thread(target=self._refresh_loop)
            self._thread.daemon = True
            self._thread.start()
        else:
            self._wake.set()

    def onClick(self, control_id):
        if control_id == ADD_BUTTON_ID:
            nav.dialog_insert()
            self._wake.set()
        elif control_id == LIST_ID:
            row = self._selected_row()
            if row is None:
                return
            if row.expandable:
                with self._lock:
                    self._expanded.symmetric_difference_update([row.key])
                self._wake.set()
            else:
                self._open_file(row)

    def onAction(self, action):
        action_id = action.getId()
        if action_id in (ACTION_PREVIOUS_MENU, ACTION_NAV_BACK, ACTION_PARENT_DIR):
            self._close()
        elif action_id == ACTION_CONTEXT_MENU and self.getFocusId() == LIST_ID:
            row = self._selected_row()
            if row is not None:
                self._context_menu(row)

    # ------------------------------------------------------------ actions

    def _close(self):
        self._stop.set()
        self._wake.set()
        self.close()

    def _close_and_run(self, builtin):
        self.after = builtin
        self._close()

    def _open_file(self, row):
        f = row.data
        if is_video(f.name) and settings.play_with_ultra_player():
            self._close_and_run("RunPlugin({})".format(nav.ultra_player_url(row.info_hash, f.id, f.name)))
        elif is_video(f.name):
            # As any play: a paused torrent resumes, and it buffers (the skin's loading screen shows it)
            self._close_and_run(nav.media(nav.play_info_hash, info_hash=row.info_hash, file_id=f.id))
        elif is_music(f.name):
            self._close_and_run(nav.media(nav.buffer_and_play, info_hash=row.info_hash, file_id=f.id))
        elif is_picture(f.name):
            nav.display_picture(row.info_hash, f.id)
        elif is_text(f.name):
            nav.display_text(row.info_hash, f.id)

    def _context_menu(self, row):
        api, info_hash = nav.api, row.info_hash
        entries = []  # (label, function)
        if row.kind == "torrent":
            status = row.data.status
            entries.append((translate(30235), lambda: self._close_and_run(
                nav.media(nav.play_info_hash, info_hash=info_hash))))
            if status.state not in (STATUS_SEEDING, STATUS_PAUSED):
                entries.append((translate(30208), lambda: api.stop_torrent(info_hash))
                               if status.total == status.total_wanted else
                               (translate(30209), lambda: self._download(info_hash)))
            entries.extend([
                (translate(30210), lambda: api.resume_torrent(info_hash))
                if status.paused else
                (translate(30211), lambda: api.pause_torrent(info_hash)),
                (translate(30242), lambda: api.remove_torrent(info_hash, delete=False)),
                (translate(30212), lambda: api.remove_torrent(info_hash, delete=True)),
            ])
        elif row.kind == "folder":
            f = row.data
            # A paused torrent's wanted files don't download: Download (which resumes it), not Stop
            entries.append((translate(30208), lambda: api.stop_torrent(info_hash, prefix=f.path))
                           if f.status.wanted_count == f.file_count and not row.paused else
                           (translate(30209), lambda: self._download(info_hash, prefix=f.path)))
        else:
            f = row.data
            if is_video(f.name) or is_music(f.name):
                entries.append((translate(30309), lambda: self._open_file(row)))
                entries.append((translate(30235), lambda: self._close_and_run(
                    nav.media(nav.buffer_and_play, info_hash=info_hash, file_id=f.id))))
            entries.append((translate(30209), lambda: self._download(info_hash, file_id=f.id))
                           if f.status.priority == 0 or (row.paused and f.status.progress < 100) else
                           (translate(30208), lambda: api.stop_file(info_hash, f.id)))

        choice = Dialog().contextmenu([label for label, _ in entries])
        if choice < 0:
            return
        try:
            entries[choice][1]()
        except TorrestError as e:
            logging.error("Torrent manager action failed: %s", e)
            notification(str(e))
        self._wake.set()

    @staticmethod
    def _download(info_hash, prefix="", file_id=None):
        """Downloads a torrent, a folder of it or a file, resuming the torrent if it's paused."""
        nav.resume_if_paused(info_hash)
        if file_id is None:
            nav.api.download_torrent(info_hash, prefix=prefix)
        else:
            nav.api.download_file(info_hash, file_id, buffer=False)

    # ------------------------------------------------------------ the table

    def _selected_row(self):
        with self._lock:
            position = self.getControl(LIST_ID).getSelectedPosition()
            return self._rows[position] if 0 <= position < len(self._rows) else None

    def _refresh_loop(self):
        monitor = Monitor()
        while not self._stop.is_set() and not monitor.abortRequested():
            try:
                rows = self._build_rows()
                self.setProperty("error", "")
            except (requests.exceptions.RequestException, TorrestError) as e:
                logging.debug("Torrent manager: daemon not answering (%s)", e)
                rows = None
                self.setProperty("error", translate(30312))
            if rows is not None and not self._stop.is_set():
                self._show(rows)
            self._wake.wait(REFRESH_INTERVAL)
            self._wake.clear()

    def _build_rows(self):
        with self._lock:
            expanded = set(self._expanded)
        rows = []
        for torrent in nav.api.torrents(status=True):
            rows.append(Row("torrent", torrent.info_hash, torrent.info_hash, 0, torrent))
            if torrent.info_hash in expanded:
                rows.extend(self._children(torrent.info_hash, "", 1, expanded, torrent.status.paused))
        return rows

    def _children(self, info_hash, folder, level, expanded, paused):
        """The rows under a torrent (folder "") or one of its folders, as its listing has them."""
        api = nav.api
        if folder or settings.folder_listing_enabled():
            items = api.items(info_hash, folder, status=True)
            if not folder and settings.skip_root_folder():
                while len(items.files) == 0 and len(items.folders) == 1:
                    items = api.items(info_hash, items.folders[0].path, status=True)
            folders, files = items.folders, items.files
        else:
            folders, files = [], api.files(info_hash, status=True)
        nav.sort_items(folders)
        nav.sort_items(files)

        rows = []
        for f in folders:
            key = (info_hash, "folder", f.path)
            rows.append(Row("folder", key, info_hash, level, f, paused))
            if key in expanded:
                rows.extend(self._children(info_hash, f.path, level + 1, expanded, paused))
        for f in files:
            rows.append(Row("file", (info_hash, "file", f.id), info_hash, level, f, paused))
        return rows

    def _show(self, rows):
        with self._lock:
            control = self.getControl(LIST_ID)
            old_items = {r.key: r.item for r in self._rows}
            for row in rows:
                row.item = old_items.get(row.key) or ListItem()
                self._fill(row)

            if [r.key for r in rows] != [r.key for r in self._rows]:
                # Rows came or went (an expand, a new torrent): the list is set again, keeping the
                # selection on the same row (or where it was, when that row has gone)
                position = control.getSelectedPosition()
                selected = self._rows[position].key if 0 <= position < len(self._rows) else None
                control.reset()
                control.addItems([r.item for r in rows])
                keys = [r.key for r in rows]
                if selected in keys:
                    position = keys.index(selected)
                if rows:
                    control.selectItem(max(0, min(position, len(rows) - 1)))
            if rows and self._focus_list:
                self.setFocusId(LIST_ID)
                self._focus_list = False
            self._rows = rows

    def _fill(self, row):
        """Sets a row's label and column properties (an item already shown updates in place)."""
        d = row.data
        if row.kind == "torrent":
            status = d.status
            # libtorrent stops updating a paused torrent's rates, so they'd stay at the last ones
            values = dict(size=sizeof_fmt(d.size), status=state_string(status),
                          up="" if status.paused else speed(status.upload_rate),
                          down="" if status.paused else speed(status.download_rate),
                          seeds="{}/{}".format(status.seeders, status.seeders_total),
                          peers="{}/{}".format(status.peers, status.peers_total))
            progress = status.progress
        else:
            status = d.status
            progress = status.progress
            if progress >= 100:
                state = translate(30311)
            elif (status.wanted_count if row.kind == "folder" else status.priority) == 0:
                state = translate(30310)
            elif row.paused:
                state = translate(30228)
            elif row.kind == "folder":
                state = translate(30223)
            else:
                state = nav.get_state_string(status.state)
            values = dict(size=sizeof_fmt(d.length), status=state, up="", down="", seeds="", peers="")

        marker = ("- " if row.key in self._expanded else "+ ") if row.expandable else "   "
        row.item.setLabel(INDENT * row.level + marker + d.name)
        values.update(progress=str(int(progress)), percent="{:.1f}%".format(progress), kind=row.kind)
        for k, v in values.items():
            row.item.setProperty(k, v)
