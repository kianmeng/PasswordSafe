# SPDX-License-Identifier: GPL-3.0-only
from __future__ import annotations

import typing
from gettext import gettext as _

from gi.repository import GObject, Gtk

from gsecrets.unlocked_database import UnlockedDatabase

if typing.TYPE_CHECKING:
    from gsecrets.database_manager import DatabaseManager
    from gsecrets.safe_element import SafeEntry


@Gtk.Template(resource_path="/org/gnome/World/Secrets/gtk/password_entry_row.ui")
class PasswordEntryRow(Gtk.Box):
    """Widget to set the password of an entry."""

    __gtype_name__ = "PasswordEntryRow"

    _generate_password_button = Gtk.Template.Child()
    _password_level_bar = Gtk.Template.Child()
    _password_entry_row = Gtk.Template.Child()
    _copy_password_button = Gtk.Template.Child()

    _unlocked_database: UnlockedDatabase | None = None

    @GObject.Property(type=UnlockedDatabase)
    def unlocked_database(self) -> UnlockedDatabase:
        return self._unlocked_database

    @unlocked_database.setter  # type: ignore
    def unlocked_database(self, unlocked_database: UnlockedDatabase) -> None:
        self._safe_entry: SafeEntry = unlocked_database.current_element
        self._db_manager: DatabaseManager = unlocked_database.database_manager
        self._unlocked_database = unlocked_database

        self._password_entry_row.props.text = self._safe_entry.props.password
        self._password_entry_row.bind_property("text", self._safe_entry, "password")

    @Gtk.Template.Callback()
    def _on_copy_password_button_clicked(self, _widget: Gtk.Button) -> None:
        self.copy_password()

    @Gtk.Template.Callback()
    def on_password_generated(self, _popover, password):
        self._password_entry_row.props.text = password

    @Gtk.Template.Callback()
    def _on_password_value_changed(self, _entry: Gtk.Entry) -> None:
        """Invoked when the password entry has changed

        Take note that this callback is already invoked after the initial
        population of the entry when nothing has really been changed by
        the user, so be careful of doing things here that could have unwanted
        side-effects.
        """
        if self._unlocked_database:
            self._unlocked_database.start_database_lock_timer()

    def copy_password(self) -> None:
        if self._unlocked_database:
            password: str = self._password_entry_row.props.text
            self._unlocked_database.send_to_clipboard(
                password,
                _("Password copied"),
            )
