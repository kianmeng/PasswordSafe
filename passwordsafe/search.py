# SPDX-License-Identifier: GPL-3.0-only
from __future__ import annotations

import threading
import typing

from typing import List
from uuid import UUID
from gi.repository import Gdk, GLib, GObject, Gtk, Handy

from passwordsafe.entry_row import EntryRow
from passwordsafe.group_row import GroupRow
from passwordsafe.scrolled_page import ScrolledPage

if typing.TYPE_CHECKING:
    from passwordsafe.database_manager import DatabaseManager
    from passwordsafe.unlocked_database import UnlockedDatabase


class Search:
    #
    # Global Variables
    #

    search_list_box = NotImplemented
    cached_rows: List[GroupRow] = []
    skipped_rows: List[UUID] = []

    #
    # Init
    #

    def __init__(self, unlocked_database: UnlockedDatabase) -> None:
        self.unlocked_database: UnlockedDatabase = unlocked_database
        self._db_manager: DatabaseManager = unlocked_database.database_manager
        self._search_event_connection_id: int = 0
        self._search_changed_id: int = 0

        self._builder: Gtk.Builder = Gtk.Builder()
        self._builder.add_from_resource("/org/gnome/PasswordSafe/search.ui")

        self._overlay: Gtk.Overlay = Gtk.Overlay()
        self._empty_search_overlay: Gtk.Overlay = self._builder.get_object(
            "empty_search_overlay")
        self._info_search_overlay: Gtk.Overlay = self._builder.get_object(
            "info_search_overlay")

        self._search_entry = self._builder.get_object("headerbar_search_entry")

        self._key_pressed: bool = False
        self._timeout_search: int = 0

        self._timeout_info: int = 0
        self._result_list: List[UUID] = []
        self._search_text: str = self._search_entry.props.text

    def initialize(self):
        # Search Headerbar
        headerbar_close_button = self._builder.get_object("headerbar_close_button")
        headerbar_close_button.connect("clicked", self.on_headerbar_search_close_button_clicked)

        self._search_entry.connect("activate", self.on_headerbar_search_entry_enter_pressed)
        self._search_entry.connect("stop-search", self.on_headerbar_search_entry_focused)

        self.unlocked_database.bind_accelerator(
            self.unlocked_database.accelerators,
            self._search_entry, "<Control>f", signal="stop-search")
        self.unlocked_database.connect(
            "notify::search-active", self._on_search_active)
        self._prepare_search_page()
    #
    # Search
    #

    # Search headerbar
    def _on_search_active(
            self, db_view: UnlockedDatabase,
            value: GObject.ParamSpecBoolean) -> None:
        """Update the view when the search view is activated or
        deactivated.

        :param UnlockedDatabase db_view: unlocked_database view
        :param GObject.ParamSpecBoolean value: new value as GParamSpec
        """
        # pylint: disable=unused-argument
        search_active = self.unlocked_database.props.search_active

        if search_active:
            headerbar_search = self._builder.get_object("headerbar_search")
            self.unlocked_database.headerbar_search = headerbar_search
            self.unlocked_database.parent_widget.set_headerbar(
                headerbar_search)
            self.unlocked_database.window.set_titlebar(headerbar_search)

            self._search_changed_id = self._search_entry.connect(
                "search-changed", self._on_search_entry_timeout)
            self._search_entry.grab_focus()

            if self.search_list_box is not NotImplemented:
                self.search_list_box.set_selection_mode(Gtk.SelectionMode.SINGLE)
                self._search_event_connection_id = self._search_entry.connect(
                    "key-release-event", self.on_search_entry_navigation)

            self.unlocked_database.responsive_ui.action_bar()

            self._timeout_info = GLib.timeout_add(
                200, self._display_info_overlay)

        else:
            for result in self.search_list_box:
                self.search_list_box.remove(result)

            if self._empty_search_overlay in self._overlay:
                self._overlay.remove(self._empty_search_overlay)

            self.search_list_box.set_selection_mode(Gtk.SelectionMode.NONE)
            self.unlocked_database.parent_widget.set_headerbar(
                self.unlocked_database.headerbar)
            self.unlocked_database.window.set_titlebar(
                self.unlocked_database.headerbar)
            self._search_entry.disconnect(self._search_event_connection_id)
            self._search_event_connection_id = 0

            self._search_entry.disconnect(self._search_changed_id)
            self._search_changed_id = 0
            self._search_entry.props.text = ""
            self._key_pressed = False
            self.search_list_box.hide()

    #
    # Stack
    #

    # Set Search stack page
    def _prepare_search_page(self):
        scrolled_page = ScrolledPage(False)
        viewport = Gtk.Viewport()
        viewport.set_name("BGPlatform")

        self.search_list_box = self._builder.get_object("list_box")

        # Responsive Container
        hdy_search = Handy.Clamp()
        hdy_search.set_maximum_size(700)
        hdy_search.add(self.search_list_box)
        self._overlay.add(hdy_search)

        self.search_list_box.connect("row-activated", self.unlocked_database.on_list_box_row_activated)
        viewport.add(self._overlay)

        scrolled_page.add(viewport)
        scrolled_page.show_all()
        self.unlocked_database.add_page(scrolled_page, "search")
        self.search_list_box.hide()

    def _display_info_overlay(self) -> bool:
        """When search-mode is activated the search text has not been
        entered yet. The info_search overlay only needs to be displayed
        if the search text is empty."""
        if self._timeout_info > 0:
            GLib.source_remove(self._timeout_info)
            self._timeout_info = 0

        if (not self._key_pressed
                and self._info_search_overlay not in self._overlay):
            self._overlay.add_overlay(self._info_search_overlay)

        return GLib.SOURCE_REMOVE

    #
    # Utils
    #

    def _start_search(self):
        """Update the overlays and start a search
        if the search term is not empty.
        """
        if self._search_text:
            if self._empty_search_overlay in self._overlay:
                self._overlay.remove(self._empty_search_overlay)

            self.search_list_box.show()

            search_thread = threading.Thread(target=self._perform_search)
            search_thread.daemon = True
            search_thread.start()
        else:
            self._overlay.add_overlay(self._info_search_overlay)
            self.search_list_box.hide()

    def _perform_search(self):
        """Search for results in the database."""
        path = self.unlocked_database.current_element.path
        self._result_list = self._db_manager.search(self._search_text, path)

        if not self._result_list:
            self._overlay.add_overlay(self._empty_search_overlay)
            self.search_list_box.hide()
            return

        GLib.idle_add(self._show_results, self._result_list)

    def _show_results(self, results_to_show, load_all=False):
        """Display some results.

        :param list results_to_show: results to show
        :param bool load_all: True if all the results need to be shown
        """
        window_height = self.unlocked_database.parent_widget.get_allocation().height - 120
        group_row_height = 45
        entry_row_height = 60
        search_height = 0

        last_row = NotImplemented
        self.skipped_rows = []

        for uuid in results_to_show:
            skip = False
            row = NotImplemented

            for cached in self.cached_rows:
                if cached.get_uuid() == uuid:
                    skip = True
                    row = cached

            if search_height < window_height or load_all is True:
                if self._db_manager.check_is_group(uuid):
                    search_height += group_row_height

                    if skip is False:
                        row = GroupRow(self.unlocked_database, self._db_manager, self._db_manager.get_group(uuid))
                        self.search_list_box.add(row)
                        self.cached_rows.append(row)
                    else:
                        self.search_list_box.add(row)

                    last_row = row
                else:
                    search_height += entry_row_height

                    if skip is False:
                        row = EntryRow(
                            self.unlocked_database,
                            self._db_manager.get_entry_object_from_uuid(
                                uuid
                            ),
                        )
                        self.search_list_box.add(row)
                        self.cached_rows.append(row)
                    else:
                        self.search_list_box.add(row)

                    last_row = row
            else:
                self.skipped_rows.append(uuid)

        if last_row is not NotImplemented and self.skipped_rows:
            load_more_row = self._builder.get_object("load_more_row")
            self.search_list_box.add(load_more_row)

        self.search_list_box.show()

    #
    # Events
    #

    def on_headerbar_search_close_button_clicked(self, _widget):
        self.unlocked_database.start_database_lock_timer()
        self.unlocked_database.props.search_active = False

    def on_search_entry_navigation(self, _widget, event, _data=None):
        self.unlocked_database.start_database_lock_timer()
        if event.keyval == Gdk.KEY_Escape:
            self.unlocked_database.props.search_active = False
        elif event.keyval == Gdk.KEY_Up:
            selected_row = self.search_list_box.get_selected_row()
            if selected_row is not None:
                row = self.search_list_box.get_row_at_index(selected_row.get_index() - 1)
                if row is not None:
                    self.search_list_box.select_row(row)
        elif event.keyval == Gdk.KEY_Down:
            selected_row = self.search_list_box.get_selected_row()
            if selected_row is None:
                row = self.search_list_box.get_row_at_index(0)
                if row is not None:
                    self.search_list_box.select_row(row)
            else:
                row = self.search_list_box.get_row_at_index(selected_row.get_index() + 1)
                if row is not None:
                    self.search_list_box.select_row(row)

    def _on_search_entry_timeout(self, widget: Gtk.Entry) -> None:
        self._key_pressed = True
        if self._timeout_search > 0:
            GLib.source_remove(self._timeout_search)

        self._timeout_search = GLib.timeout_add(
            500, self._on_search_entry_changed, widget)

    def _on_search_entry_changed(self, widget):
        self._timeout_search = 0

        self.search_list_box.hide()
        self.search_list_box.unselect_all()
        self._result_list.clear()

        if self._info_search_overlay in self._overlay:
            self._overlay.remove(self._info_search_overlay)

        if self._empty_search_overlay in self._overlay:
            self._overlay.remove(self._empty_search_overlay)

        for row in self.search_list_box.get_children():
            self.search_list_box.remove(row)

        self._search_text = widget.get_text()
        self._start_search()

        return False

    def on_headerbar_search_entry_enter_pressed(self, widget_):
        self.unlocked_database.start_database_lock_timer()

        # Do nothing on empty search terms or no search results
        if not self._search_text:
            return
        if not self.search_list_box.get_children():
            return

        uuid = NotImplemented
        first_row = NotImplemented
        selected_row = self.search_list_box.get_selected_row()

        if selected_row is None:
            selected_row = self.search_list_box.get_children()[0]
        uuid = selected_row.get_uuid()
        if selected_row.type == "GroupRow":
            first_row = self._db_manager.get_group(uuid)
        else:
            first_row = self._db_manager.get_entry_object_from_uuid(uuid)

        self.unlocked_database.show_element(first_row)

    def on_load_more_row_clicked(self, row):
        self.search_list_box.remove(row)
        self._show_results(self.skipped_rows, True)

    def on_headerbar_search_entry_focused(self, entry):
        if entry.has_focus() is True:
            return

        entry.grab_focus()
