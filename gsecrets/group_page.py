# SPDX-License-Identifier: GPL-3.0-only
from __future__ import annotations

import typing

from gi.repository import GObject, Gtk

from gsecrets.pathbar import Pathbar
from gsecrets.widgets.notes_dialog import NotesDialog

if typing.TYPE_CHECKING:
    from gsecrets.safe_element import SafeGroup


@Gtk.Template(resource_path="/org/gnome/World/Secrets/gtk/group_page.ui")
class GroupPage(Gtk.Box):

    __gtype_name__ = "GroupPage"

    _pathbar_bin = Gtk.Template.Child()

    title_entry_row = Gtk.Template.Child()
    notes_text_view = Gtk.Template.Child()

    def __init__(self, unlocked_database):
        super().__init__()

        self.unlocked_database = unlocked_database

        safe_group = self.unlocked_database.current_element

        # Setup Widgets
        notes_buffer = self.notes_text_view.get_buffer()

        self._pathbar_bin.set_child(Pathbar(unlocked_database))

        # Connect Signals
        safe_group.updated.connect(self._on_safe_group_updated)
        safe_group.bind_property(
            "name",
            self.title_entry_row,
            "text",
            GObject.BindingFlags.SYNC_CREATE | GObject.BindingFlags.BIDIRECTIONAL,
        )
        safe_group.bind_property(
            "notes",
            notes_buffer,
            "text",
            GObject.BindingFlags.SYNC_CREATE | GObject.BindingFlags.BIDIRECTIONAL,
        )

        self._pathbar_bin.bind_property(
            "visible",
            unlocked_database.action_bar,
            "revealed",
            GObject.BindingFlags.BIDIRECTIONAL
            | GObject.BindingFlags.INVERT_BOOLEAN
            | GObject.BindingFlags.SYNC_CREATE,
        )

    @Gtk.Template.Callback()
    def on_notes_detach_button_clicked(self, _button):
        self.unlocked_database.start_database_lock_timer()
        safe_group = self.unlocked_database.current_element
        NotesDialog(self.unlocked_database, safe_group).present()

    def _on_safe_group_updated(self, _safe_group: SafeGroup) -> None:
        self.unlocked_database.start_database_lock_timer()
