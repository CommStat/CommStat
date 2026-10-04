# Copyright (c) 2026 Manuel Ochoa
# This file is part of CommStat.
# Licensed under the GNU General Public License v3.0.
"""
transmit_base.py - Rig handling shared by the Transmit dialogs.

StatRep, Group Message, Alert, Group Incident, JS8 Direct Message, JS8 Email
and JS8 SMS all talk to JS8Call the same way: a rig dropdown, a mode dropdown,
a read-only frequency field, and a "call selected?" check before transmitting.
RigDialogMixin holds that shared behaviour; each dialog keeps only what is
specific to it.

Use it as the first base class:  class AlertDialog(RigDialogMixin, QDialog)

The dialog must provide: self.tcp_pool, self.connector_manager,
self.rig_combo, self.mode_combo and self.freq_field.
"""

from PyQt5.QtWidgets import QMessageBox

from constants import SPEED_OPTIONS, INTERNET_RIG
from ui_helpers import show_error

_NORMAL_INDEX = 1   # index of "Normal" in SPEED_OPTIONS; used when the rig's speed is unknown
_SPEED_INDEX = {label.upper(): i for i, (label, _value) in enumerate(SPEED_OPTIONS)}


class RigDialogMixin:
    # Dialogs set these class attributes to choose their rig-dropdown rules.
    ALLOW_INTERNET_RIG = False       # offer INTERNET_RIG (when the app is online)
    AUTO_SELECT_SINGLE_RIG = False   # with exactly one rig, select it (no blank first entry)

    # ── Rig dropdown ──────────────────────────────────────────────────────────

    def _internet_available(self) -> bool:
        """True when the main window reports an Internet connection."""
        override = getattr(self, "_internet_available_override", None)
        if override is not None:
            return bool(override)
        parent = self.parent()
        return bool(parent and getattr(parent, "_internet_available", False))

    def _available_rig_names(self) -> list:
        """Names of connected rigs whose connector is enabled."""
        connected = self.tcp_pool.get_connected_rig_names() if self.tcp_pool else []
        if not self.connector_manager:
            return list(connected)
        enabled = self.connector_manager.get_all_connectors(enabled_only=True)
        return [c["rig_name"] for c in enabled if c["rig_name"] in connected]

    def _load_rigs(self) -> None:
        """Fill the rig dropdown, then run _on_rig_changed for a preselected rig.

        With several rigs (or INTERNET_RIG on top of one) the first entry is
        blank, so the user must choose explicitly."""
        rigs = self._available_rig_names()
        include_internet = self.ALLOW_INTERNET_RIG and self._internet_available()

        self.rig_combo.blockSignals(True)
        self.rig_combo.clear()
        if self.ALLOW_INTERNET_RIG:
            if rigs:
                self.rig_combo.addItem("")
                for name in rigs:
                    self.rig_combo.addItem(name)
            if include_internet:
                self.rig_combo.addItem(INTERNET_RIG)
        elif len(rigs) == 1 and self.AUTO_SELECT_SINGLE_RIG:
            self.rig_combo.addItem(rigs[0])
        elif rigs:
            self.rig_combo.addItem("")
            for name in rigs:
                self.rig_combo.addItem(name)
        self.rig_combo.blockSignals(False)

        current = self.rig_combo.currentText()
        if current:
            self._on_rig_changed(current)

    # ── Rig signal connections ────────────────────────────────────────────────
    # Every connection to a rig client goes through _connect_rig_signal so that
    # done() can drop them all. A slot left connected to a rig outlives its
    # dialog and fires into a deleted widget.
    #
    # Groups: "rig" = display updates for the selected rig (reset on every rig
    # change); "tx" = one-shot replies during a transmit.

    def _rig_connections(self) -> list:
        return self.__dict__.setdefault("_rig_connection_list", [])

    def _connect_rig_signal(self, client, signal_name: str, slot, group: str = "rig") -> None:
        """Connect client.<signal_name> to slot (once) and remember it."""
        self._disconnect_rig_signal(client, signal_name, slot)
        getattr(client, signal_name).connect(slot)
        self._rig_connections().append((client, signal_name, slot, group))

    def _disconnect_rig_signal(self, client, signal_name: str, slot) -> None:
        """Disconnect one connection; fine if it was never connected."""
        try:
            getattr(client, signal_name).disconnect(slot)
        except (TypeError, RuntimeError):
            pass
        self.__dict__["_rig_connection_list"] = [
            c for c in self._rig_connections()
            if not (c[0] is client and c[1] == signal_name and c[2] == slot)
        ]

    def _disconnect_rig_signals(self, group: str = None) -> None:
        """Disconnect every remembered connection (of one group, or all if None)."""
        for client, signal_name, slot, conn_group in list(self._rig_connections()):
            if group is None or conn_group == group:
                self._disconnect_rig_signal(client, signal_name, slot)

    def done(self, result: int) -> None:
        """Runs on accept, reject, and window close: drop every rig-signal slot."""
        self._disconnect_rig_signals()
        super().done(result)

    # ── Mode and frequency ────────────────────────────────────────────────────

    def _speed_index(self, client) -> int:
        """Index in SPEED_OPTIONS of the rig's current speed (Normal if unknown)."""
        return _SPEED_INDEX.get((client.speed_name or "").upper(), _NORMAL_INDEX)

    def _sync_mode_combo(self, client) -> None:
        """Preselect the mode dropdown from the rig, without sending anything back."""
        self.mode_combo.blockSignals(True)
        self.mode_combo.setCurrentIndex(self._speed_index(client))
        self.mode_combo.blockSignals(False)

    def _show_frequency(self, client) -> None:
        """Show the rig's cached dial frequency (MHz), or blank if not known yet."""
        frequency = client.frequency
        self.freq_field.setText(f"{frequency:.3f}" if frequency else "")

    def _on_mode_changed(self, _index: int) -> None:
        """Send MODE.SET_SPEED to JS8Call when the mode dropdown changes."""
        rig_name = self.rig_combo.currentText()
        if not rig_name or rig_name == INTERNET_RIG or not self.tcp_pool:
            return
        client = self.tcp_pool.get_client(rig_name)
        if client and client.is_connected():
            client.send_message("MODE.SET_SPEED", "", {"SPEED": self.mode_combo.currentData()})

    def _on_frequency_received(self, rig_name: str, dial_freq: int) -> None:
        """Show a frequency reported by the currently selected rig (Hz -> MHz)."""
        if self.rig_combo.currentText() == rig_name:
            self.freq_field.setText(f"{dial_freq / 1_000_000:.3f}")

    # ── Transmit handshake ────────────────────────────────────────────────────
    # 1. The dialog validates, stores what to send, and calls
    #    _connected_client() then _begin_rf_transmit(client).
    # 2. JS8Call answers whether a call is selected. If one is, the user is
    #    told to click "Deselect" and the transmit stops.
    # 3. Otherwise _on_call_clear() runs. By default it asks the rig for its
    #    frequency and then calls the dialog's _transmit_with_frequency().
    #    A dialog that doesn't need the frequency (JS8 Email/SMS/Direct)
    #    overrides _on_call_clear() and sends right away.

    def _connected_client(self, rig_name: str):
        """The connected client for rig_name, or None after telling the user why not."""
        if not self.tcp_pool:
            show_error(self, "Cannot transmit: TCP pool not available")
            return None
        client = self.tcp_pool.get_client(rig_name)
        if not client or not client.is_connected():
            show_error(self, "Cannot transmit: not connected to rig")
            return None
        return client

    def _begin_rf_transmit(self, client) -> None:
        """Ask JS8Call whether a call is selected; the answer arrives in
        _on_call_selected_for_transmit."""
        self._connect_rig_signal(client, "call_selected_received",
                                 self._on_call_selected_for_transmit, group="tx")
        client.get_call_selected()

    def _on_call_selected_for_transmit(self, rig_name: str, selected_call: str) -> None:
        if self.rig_combo.currentText() != rig_name:
            return

        client = self.tcp_pool.get_client(rig_name)
        if client:
            self._disconnect_rig_signal(client, "call_selected_received",
                                        self._on_call_selected_for_transmit)

        if selected_call:
            QMessageBox.critical(
                self, "ERROR",
                f"JS8Call has {selected_call} selected.\n\n"
                "Go to JS8Call and click the \"Deselect\" button.\n\n"
                "The Deselect button is above the waterfall."
            )
            return

        if client:
            self._on_call_clear(client, rig_name)

    def _on_call_clear(self, client, rig_name: str) -> None:
        """No call is selected in JS8Call: carry on. Default: fetch the frequency first."""
        self._connect_rig_signal(client, "frequency_received",
                                 self._on_frequency_for_transmit, group="tx")
        client.get_frequency()

    def _on_frequency_for_transmit(self, rig_name: str, frequency: int) -> None:
        if self.rig_combo.currentText() != rig_name:
            return
        client = self.tcp_pool.get_client(rig_name)
        if client:
            self._disconnect_rig_signal(client, "frequency_received",
                                        self._on_frequency_for_transmit)
            self._transmit_with_frequency(client, frequency)

    def _transmit_with_frequency(self, client, frequency: int) -> None:
        """Send the pending message and save it. Dialogs that use the default
        _on_call_clear() implement this."""
        raise NotImplementedError
