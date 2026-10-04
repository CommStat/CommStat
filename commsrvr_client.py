# Copyright (c) 2026 Manuel Ochoa
# This file is part of CommStat.
# Licensed under the GNU General Public License v3.0.
"""
commsrvr_client.py - Submit a transmitted message to the commsrvr datafeed.

Shared by the StatRep, Group Message, Alert and Group Incident dialogs.

The POST runs on a background thread, but the result is always delivered back
on the main (GUI) thread through a queued signal. That means `on_complete`
can safely touch widgets, write to the database, and close the dialog.
(A QTimer.singleShot started from the worker thread never fires: the thread
has no Qt event loop.)

Datafeed reply contract: success is a bare integer global_id; failure is
"ERR::{message}". Timeouts and connection errors are turned into messages
here. Every failure is shown in the styled InternetDeliveryFailureDialog.
"""

import threading
import traceback
import urllib.parse
import urllib.request
from typing import Callable, Optional

from PyQt5 import QtCore, QtWidgets, sip
from PyQt5.QtCore import QDateTime

from constants import DATAFEED_URL
from ssl_utils import create_verified_ssl_context

_TIMEOUT_S = 5


class _Dispatcher(QtCore.QObject):
    """Lives on the main thread; worker threads emit `result`, the slot runs there."""
    result = QtCore.pyqtSignal(object, object, int, str)  # parent, on_complete, global_id, error

    def __init__(self):
        super().__init__()
        self.result.connect(self._deliver)

    def _deliver(self, parent, on_complete, global_id: int, error: str) -> None:
        # A slot must never raise: PyQt5 aborts the whole program on an unhandled exception.
        try:
            if on_complete:
                on_complete(global_id)
        except Exception:
            traceback.print_exc()
        if error:
            try:
                _show_delivery_failure(parent, error)
            except Exception:
                traceback.print_exc()


_dispatcher: Optional[_Dispatcher] = None


def _get_dispatcher() -> _Dispatcher:
    global _dispatcher
    if _dispatcher is None:
        _dispatcher = _Dispatcher()
        _dispatcher.moveToThread(QtWidgets.QApplication.instance().thread())
    return _dispatcher


def _show_delivery_failure(parent, message: str) -> None:
    from qrz_lookup import InternetDeliveryFailureDialog
    if parent is not None and sip.isdeleted(parent):
        parent = None
    if parent is not None and not parent.isVisible():
        parent = parent.parent() or parent   # the dialog may have closed already
    InternetDeliveryFailureDialog(message, parent=parent).exec_()


def _post(callsign: str, data_string: str) -> "tuple[int, str]":
    """Blocking POST. Returns (global_id, error_message); global_id is 0 on failure."""
    try:
        post_data = urllib.parse.urlencode({'cs': callsign, 'data': data_string}).encode('utf-8')
        req = urllib.request.Request(DATAFEED_URL, data=post_data, method='POST')
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S, context=create_verified_ssl_context()) as response:
            result = response.read().decode('utf-8').strip()
        if result.isdigit():
            print(f"[Commsrvr] Submitted successfully (global_id={result})")
            return int(result), ""
        print(f"[Commsrvr] Submission failed — server returned: {result}")
        return 0, (result[5:] if result.startswith("ERR::") else (result or "Unknown server error"))
    except Exception as e:
        reason = getattr(e, 'reason', e)
        if isinstance(reason, TimeoutError):
            error = "Server timeout — the server did not respond in time."
        else:
            error = f"Connection error — {e}"
        print(f"[Commsrvr] Submission failed — {error}")
        return 0, error


def submit_to_commsrvr(parent, frequency: int, callsign: str, message_data: str,
                       now: str = "", snr: int = 30,
                       on_complete: Callable[[int], None] = None) -> None:
    """Submit *message_data* to the datafeed without blocking the GUI.

    parent: the dialog; the failure popup is shown over it (or over its parent
        if it has already closed).
    frequency: transmit frequency in Hz (0 for Internet-only).
    now: "yyyy-MM-dd HH:mm:ss" UTC; defaults to the current time.
    snr: the datafeed line's SNR field (the heartbeat parser reads
        "date time freq_hz unused snr callsign: message").
    on_complete: optional callable(global_id: int), always run on the main
        thread. global_id is 0 on failure. If the failure popup is needed it
        is shown right after on_complete returns.

    In Off-Grid mode nothing is sent and on_complete(0) runs immediately,
    with no popup.

    Call from the main thread only.
    """
    import netguard
    if not netguard.is_network_enabled():
        if on_complete:
            on_complete(0)
        return

    if not now:
        now = QDateTime.currentDateTimeUtc().toString("yyyy-MM-dd HH:mm:ss")
    data_string = f"{now}\t{frequency}\t0\t{snr}\t{message_data}"
    dispatcher = _get_dispatcher()

    def worker():
        global_id, error = _post(callsign, data_string)
        try:
            dispatcher.result.emit(parent, on_complete, global_id, error)
        except RuntimeError:
            pass   # application is shutting down

    threading.Thread(target=worker, daemon=True).start()
