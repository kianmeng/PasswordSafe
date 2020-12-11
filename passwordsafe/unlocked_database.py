# SPDX-License-Identifier: GPL-3.0-only
from __future__ import annotations

import logging
import re
import threading
import typing
from gettext import gettext as _
from threading import Timer
from typing import List, Optional, Union
from uuid import UUID

from gi.repository import Gdk, Gio, GLib, GObject, Gtk

import passwordsafe.config_manager
from passwordsafe.custom_keypress_handler import CustomKeypressHandler
from passwordsafe.database_settings_dialog import DatabaseSettingsDialog
from passwordsafe.entry_page import EntryPage
from passwordsafe.entry_row import EntryRow
from passwordsafe.group_page import GroupPage
from passwordsafe.group_row import GroupRow
from passwordsafe.pathbar import Pathbar
from passwordsafe.properties_dialog import PropertiesDialog
from passwordsafe.references_dialog import ReferencesDialog
from passwordsafe.safe_entry import SafeEntry
from passwordsafe.scrolled_page import ScrolledPage
from passwordsafe.search import Search
from passwordsafe.unlocked_headerbar import UnlockedHeaderBar

if typing.TYPE_CHECKING:
    from pykeepass.entry import Entry
    from pykeepass.group import Group

    # pylint: disable=ungrouped-imports
    from passwordsafe.main_window import MainWindow
    from passwordsafe.container_page import ContainerPage
    from passwordsafe.database_manager import DatabaseManager


class UnlockedDatabase(GObject.GObject):
    # pylint: disable=too-many-instance-attributes
    # pylint: disable=too-many-public-methods

    # Widgets
    headerbar = NotImplemented
    scrolled_window = NotImplemented
    divider = NotImplemented
    revealer = NotImplemented
    action_bar = NotImplemented
    pathbar = NotImplemented
    overlay = NotImplemented

    # Objects
    builder = NotImplemented
    scheduled_page_destroy: List[UUID] = []
    scheduled_tmpfiles_deletion: List[Gio.File] = []
    clipboard = NotImplemented
    list_box_sorting = NotImplemented
    clipboard_timer = NotImplemented
    database_lock_timer = NotImplemented
    save_loop: Optional[int] = None  # If int, a thread periodically saves the database
    dbus_subscription_id = NotImplemented
    listbox_insert_thread = NotImplemented

    selection_mode = GObject.Property(
        type=bool, default=False, flags=GObject.ParamFlags.READWRITE
    )

    def __init__(self, window: MainWindow, widget: ContainerPage, dbm: DatabaseManager):
        super().__init__()
        # Instances
        self.window: MainWindow = window
        self.parent_widget: ContainerPage = widget
        self.database_manager: DatabaseManager = dbm
        self.search: Search = Search(self)
        self.custom_keypress_handler: CustomKeypressHandler = CustomKeypressHandler(
            self
        )
        # UnlockedDatabase-specific key accelerators
        self.accelerators: Gtk.AccelGroup = Gtk.AccelGroup()
        self.window.add_accel_group(self.accelerators)

        root_group: Group = self.database_manager.get_root_group()
        self._current_element: Union[SafeEntry, Group] = root_group

        # Declare database as opened
        self.window.opened_databases.append(self)

        self._search_active = False

        # Browser Mode
        self.assemble_listbox()
        self.start_save_loop()
        self.custom_keypress_handler.register_custom_events()
        self.register_dbus_signal()

        self.database_manager.connect("notify::locked", self._on_database_lock_changed)
        self.database_manager.connect("save-notification", self.on_database_save_notification)

    #
    # Stack Pages
    #

    def assemble_listbox(self):
        self.builder = Gtk.Builder()
        self.builder.add_from_resource("/org/gnome/PasswordSafe/unlocked_database.ui")

        self.pathbar = Pathbar(self, self.database_manager)
        self._stack = self.builder.get_object("list_stack")
        self._unlocked_db_stack = self.builder.get_object("_unlocked_db_stack")
        self.revealer = self.builder.get_object("revealer")
        self.action_bar = self.builder.get_object("action_bar")

        self.headerbar = UnlockedHeaderBar(self)
        self.selection_ui = self.headerbar.selection_ui

        self.clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)

        # Contains the "main page" with the BrowserListBox.
        self.divider = self.builder.get_object("divider")
        self._unlocked_db_stack.add_named(self.search.scrolled_page, "search")
        self.search.scrolled_page.show_all()
        self.parent_widget.add(self.divider)

        self.search.initialize()
        self._update_headerbar()

        self.list_box_sorting = passwordsafe.config_manager.get_sort_order()
        self.start_database_lock_timer()

        self.show_page_of_new_directory(False, False)

    #
    # Headerbar
    #

    def _update_headerbar(self) -> None:
        """Display the correct headerbar according to search state."""
        if self.props.search_active:
            self.parent_widget.set_headerbar(self.search.headerbar)
            self.window.set_titlebar(self.search.headerbar)
        else:
            self.parent_widget.set_headerbar(self.headerbar)
            self.window.set_titlebar(self.headerbar)

    #
    # Keystrokes
    #

    def bind_accelerator(self, widget, accelerator, signal="clicked"):
        """bind accelerators to self, aka this `UnlockedDatabase`"""
        key, mod = Gtk.accelerator_parse(accelerator)
        widget.add_accelerator(
            signal, self.accelerators, key, mod, Gtk.AccelFlags.VISIBLE
        )

    #
    # Group and Entry Management
    #

    def show_page_of_new_directory(self, edit_group, new_entry):
        # pylint: disable=too-many-statements

        # First, remove stack pages which should not exist because they are scheduled for remove
        self.destroy_current_page_if_scheduled()

        # Creation of group edit page
        if edit_group is True:
            builder = Gtk.Builder()
            builder.add_from_resource("/org/gnome/PasswordSafe/group_page.ui")
            scrolled_window = ScrolledPage(True)

            # Responsive Container
            hdy_page = GroupPage(self)
            scrolled_window.add(hdy_page)
            scrolled_window.show_all()

            stack_page_uuid = self.current_element.uuid
            if self._stack.get_child_by_name(stack_page_uuid.urn) is not None:
                stack_page = self._stack.get_child_by_name(stack_page_uuid.urn)
                stack_page.destroy()

            self.add_page(scrolled_window, self.current_element.uuid.urn)
            self.switch_page(self.current_element)
            self.headerbar.props.mode = UnlockedHeaderBar.Mode.GROUP_EDIT
        # If the stack page with current group's uuid isn't existing - we need to create it (first time opening of group/entry)
        elif (
            not self._stack.get_child_by_name(self.current_element.uuid.urn)
            and not edit_group
        ):
            self.database_manager.set_element_atime(self.current_element)
            # Create not existing stack page for group
            if self.database_manager.check_is_group(self.current_element.uuid):
                builder = Gtk.Builder()
                builder.add_from_resource(
                    "/org/gnome/PasswordSafe/unlocked_database.ui"
                )
                list_box = builder.get_object("list_box")
                list_box.connect("row-activated", self.on_list_box_row_activated)

                scrolled_window = ScrolledPage(False)

                hdy_clamp = builder.get_object("browser_clamp")
                scrolled_window.add(hdy_clamp)
                scrolled_window.show_all()

                self.add_page(scrolled_window, self.current_element.uuid.urn)
                self.switch_page(self.current_element)

                list_box.hide()

                self.listbox_insert_thread = threading.Thread(
                    target=self.insert_groups_into_listbox, args=(list_box, hdy_clamp)
                )
                self.listbox_insert_thread.daemon = True
                self.listbox_insert_thread.start()
            # Create not existing stack page for entry
            else:
                if new_entry:
                    scrolled_window = EntryPage(self, True)
                else:
                    scrolled_window = EntryPage(self, False)

                self.add_page(scrolled_window, self.current_element.uuid.urn)
                self.switch_page(self.current_element)
        # Stack page with current group's uuid already exists, we only need to switch stack page
        else:
            self.database_manager.set_element_atime(self.current_element)
            # For group
            if self.database_manager.check_is_group(self.current_element.uuid):
                self._stack.set_visible_child_name(self.current_element.uuid.urn)
                self.headerbar.props.mode = UnlockedHeaderBar.Mode.GROUP
            # For entry
            else:
                self._stack.set_visible_child_name(self.current_element.uuid.urn)
                self.headerbar.props.mode = UnlockedHeaderBar.Mode.ENTRY

    def add_page(self, scrolled_window: ScrolledPage, name: str) -> None:
        """Add a new page to the stack

        :param ScrolledPage scrolled_window: scrolled_page to add
        :param str name: name of the page
        """
        self._stack.add_named(scrolled_window, name)

    def switch_page(self, element: Union[SafeEntry, Group]) -> None:
        """Set the current element and display it

        :param element: Entry or Group
        """
        self.current_element = element

        page_uuid = self.current_element.uuid
        group_page = self.database_manager.check_is_group(page_uuid)

        if page_uuid in self.scheduled_page_destroy:
            stack_page = self._stack.get_child_by_name(page_uuid.urn)
            if stack_page is not None:
                stack_page.destroy()

            self.scheduled_page_destroy.remove(page_uuid)
            self.show_page_of_new_directory(False, False)

        if self._stack.get_child_by_name(page_uuid.urn) is None:
            self.show_page_of_new_directory(False, False)
        else:
            self._stack.set_visible_child_name(page_uuid.urn)

        if group_page and not self.props.selection_mode:
            self.headerbar.mode = UnlockedHeaderBar.Mode.GROUP
        elif not group_page:
            self.headerbar.mode = UnlockedHeaderBar.Mode.ENTRY

    def _remove_page(self, element: Union[SafeEntry, Group]) -> None:
        """Remove an element (SafeEntry, Group) from the stack if present."""
        stack_page_name = element.uuid.urn
        stack_page = self._stack.get_child_by_name(stack_page_name)
        if stack_page:
            stack_page.destroy()

    @property
    def current_element(self) -> Union[SafeEntry, Group]:
        return self._current_element

    @current_element.setter
    def current_element(self, element: Union[SafeEntry, Group]) -> None:
        self._current_element = element

    def get_current_page(self) -> ScrolledPage:
        """Returns the page associated with current_element.

        :returns: current page
        :rtype: Gtk.Widget
        """
        element_uuid = self.current_element.uuid
        return self._stack.get_child_by_name(element_uuid.urn)

    def get_pages(self) -> List[ScrolledPage]:
        """Returns all the children of the stack.

        :returns: All the children of the stack.
        :rtype: list
        """
        return self._stack.get_children()

    def schedule_stack_page_for_destroy(self, page_uuid: UUID) -> None:
        """Add page to the list of pages to be destroyed"""
        logging.debug("Scheduling page %s for destruction", page_uuid)
        self.scheduled_page_destroy.append(page_uuid)

    def destroy_current_page_if_scheduled(self) -> None:
        """If the current_element is in self.scheduled_page_destroy, destroy it"""
        page_uuid = self.current_element.uuid
        logging.debug("Test if we should destroy page %s", page_uuid)
        if page_uuid in self.scheduled_page_destroy:
            logging.debug("Yes, destroying page %s", page_uuid)
            stack_page_name = self._stack.get_child_by_name(page_uuid.urn)
            if stack_page_name is not None:
                stack_page_name.destroy()
            self.scheduled_page_destroy.remove(page_uuid)

    #
    # Create Group & Entry Rows
    #

    def show_element(self, element: Union[SafeEntry, Group]) -> None:
        """Sets the current element and display it

        :param element: Entry or Group to display
        """
        self.current_element = element
        self.pathbar.add_pathbar_button_to_pathbar(element)
        self.show_page_of_new_directory(False, False)

    def insert_groups_into_listbox(self, list_box, overlay):
        groups = NotImplemented
        sorted_list = []

        groups = self.current_element.subgroups
        GLib.idle_add(self.group_instance_creation, list_box, sorted_list, groups)

        self.insert_entries_into_listbox(list_box, overlay)

    def insert_entries_into_listbox(self, list_box, overlay):
        entries = self.current_element.entries
        sorted_list = []

        GLib.idle_add(
            self.entry_instance_creation, list_box, sorted_list, entries, overlay
        )

    def group_instance_creation(self, list_box, sorted_list, groups):
        for group in groups:
            group_row = GroupRow(self, self.database_manager, group)
            sorted_list.append(group_row)

        if self.list_box_sorting == "A-Z":
            sorted_list.sort(key=lambda group: str.lower(group.label), reverse=False)
        elif self.list_box_sorting == "Z-A":
            sorted_list.sort(key=lambda group: str.lower(group.label), reverse=True)

        for group_row in sorted_list:
            list_box.add(group_row)

    def entry_instance_creation(self, list_box, sorted_list, entries, overlay):
        for entry in entries:
            safe_entry: SafeEntry = SafeEntry(self.database_manager, entry)
            entry_row = EntryRow(self, safe_entry)
            sorted_list.append(entry_row)

        if self.list_box_sorting == "A-Z":
            sorted_list.sort(key=lambda entry: str.lower(entry.safe_entry.name), reverse=False)
        elif self.list_box_sorting == "Z-A":
            sorted_list.sort(key=lambda entry: str.lower(entry.safe_entry.name), reverse=True)

        for entry_row in sorted_list:
            list_box.add(entry_row)

        if list_box.get_children():
            list_box.get_children()[0].grab_focus()
            list_box.show()
        else:
            builder = Gtk.Builder()
            builder.add_from_resource("/org/gnome/PasswordSafe/unlocked_database.ui")
            empty_group_overlay = builder.get_object("empty_group_overlay")
            overlay.add_overlay(empty_group_overlay)
            list_box.hide()

    def rebuild_all_pages(self):
        # FIXME find a more elegant way to do this without
        # obliterating everything.
        for page in self._stack.get_children():
            if page.edit_page is False:
                page.destroy()

        self.show_page_of_new_directory(False, False)

    #
    # Events
    #

    def on_list_box_row_activated(self, _widget, list_box_row):
        self.start_database_lock_timer()

        if list_box_row.get_name() == "LoadMoreRow":
            self.search.on_load_more_row_clicked(list_box_row)
        else:
            if self.props.search_active:
                self.props.search_active = False

            if list_box_row.type == "GroupRow":
                uuid = self.database_manager.get_group(
                    list_box_row.get_uuid())
                self.show_element(uuid)
            else:
                if self.selection_mode:
                    active = list_box_row.selection_checkbox.props.active
                    list_box_row.selection_checkbox.props.active = not active
                    return

                safe_entry = list_box_row.safe_entry
                self.show_element(safe_entry)

    def on_database_save_notification(self, _database_manager: DatabaseManager, saved: bool) -> None:
        if saved:
            self.window.notify(_("Safe saved"))
        else:
            self.window.notify(_("Could not save Safe"))

    def save_safe(self):
        self.start_database_lock_timer()

        if self.database_manager.is_dirty is True:
            if self.database_manager.save_running is False:
                self.save_database(notification=True)
            else:
                # NOTE: In-app notification to inform the user that already an unfinished save job is running
                self.window.notify(
                    _("Please wait. Another save is running.")
                )
        else:
            # NOTE: In-app notification to inform the user that no save is necessary because there where no changes made
            self.window.notify(_("No changes made"))

    def lock_safe(self):
        self.database_manager.props.locked = True

    def on_add_entry_button_clicked(self, _widget):
        """CB when the Add Entry menu was clicked"""
        self.start_database_lock_timer()
        new_entry: Entry = self.database_manager.add_entry_to_database(self.current_element)
        safe_entry: SafeEntry = SafeEntry(self.database_manager, new_entry)
        self.current_element = safe_entry
        self.pathbar.add_pathbar_button_to_pathbar(safe_entry)
        self.show_page_of_new_directory(False, True)

    def on_add_group_button_clicked(self, _param: None) -> None:
        """CB when menu entry Add Group is clicked"""
        self.start_database_lock_timer()
        self.database_manager.is_dirty = True
        group = self.database_manager.add_group_to_database(
            "", "0", "", self.current_element
        )
        self.current_element = group
        self.pathbar.add_pathbar_button_to_pathbar(self.current_element)
        self.show_page_of_new_directory(True, False)

    def on_element_delete_menu_button_clicked(
        self, _action: Gio.SimpleAction, _param: None
    ) -> None:
        """Delete the visible entry from the menu."""
        self.start_database_lock_timer()

        parent_group = self.current_element.parentgroup
        self.database_manager.delete_from_database(self.current_element.entry)

        self._remove_page(self.current_element)
        self.current_element = parent_group
        # Remove the parent group from the stack and add it again with
        # a show_page_of_new_directory call to force a full refresh of
        # the group view.
        # FIXME: This operation is not efficient, it should be possible
        # to update the group view without removing it and adding it
        # again to the stack.
        self._remove_page(parent_group)
        self.show_page_of_new_directory(False, False)
        self.pathbar.rebuild_pathbar(self.current_element)

    def on_entry_duplicate_menu_button_clicked(self, _action, _param):
        self.start_database_lock_timer()

        self.database_manager.duplicate_entry(self.current_element.entry)
        parent_group = self.current_element.parentgroup

        if self.database_manager.check_is_root_group(parent_group) is True:
            self.pathbar.on_home_button_clicked(self.pathbar.home_button)
        else:
            for button in self.pathbar:
                if button.get_name() == "PathbarButtonDynamic" and isinstance(
                    button, passwordsafe.pathbar_button.PathbarButton
                ):
                    if button.element.uuid == parent_group.uuid:
                        self.pathbar.on_pathbar_button_clicked(button)

        # Remove the parent group from the stack and add it again with
        # a show_page_of_new_directory call to force a full refresh of
        # the group view.
        # FIXME: This operation is not efficient, it should be possible
        # to update the group view without removing it and adding it
        # again to the stack.
        self._remove_page(parent_group)
        self.current_element = parent_group
        self.show_page_of_new_directory(False, False)

    def on_group_edit_button_clicked(self, button: Gtk.Button) -> None:
        """Edit button in a GroupRow was clicked

        button: The edit button in the GroupRow"""
        self.start_database_lock_timer()  # Reset the lock timer
        widget = button
        # Get the ancestor GroupRow object
        while not isinstance(widget, GroupRow):
            widget = widget.get_parent()
        group_uuid = widget.get_uuid()

        self.current_element = self.database_manager.get_group(group_uuid)
        self.pathbar.add_pathbar_button_to_pathbar(self.current_element)
        self.show_page_of_new_directory(True, False)

    def send_to_clipboard(self, text, message=_("Copied to clipboard")):
        # pylint: disable=too-many-branches
        self.start_database_lock_timer()
        if self.clipboard_timer is not NotImplemented:
            self.clipboard_timer.cancel()

        replace_string = text
        for ref in re.finditer("({REF:.*?})", text):
            not_valid = False
            code = ref.group()[5]

            try:
                uuid = UUID(self.reference_to_hex_uuid(ref.group()))
            except Exception:  # pylint: disable=broad-except
                # TODO: find out which Exceptions could
                # reasonably happen and only protect against these.
                not_valid = True

            value = NotImplemented

            if not_valid is False:
                if code == "T":
                    try:
                        value = self.database_manager.get_entry_name(uuid)
                    except AttributeError:
                        value = ref.group()
                elif code == "U":
                    try:
                        value = self.database_manager.get_entry_username(uuid)
                    except AttributeError:
                        value = ref.group()
                elif code == "P":
                    try:
                        value = self.database_manager.get_entry_password(uuid)
                    except AttributeError:
                        logging.debug("FAIL")
                        value = ref.group()
                elif code == "A":
                    try:
                        value = str(self.database_manager.get_entry_url(uuid))
                    except AttributeError:
                        value = ref.group()
                elif code == "N":
                    try:
                        value = str(self.database_manager.get_notes(uuid))
                    except AttributeError:
                        value = ref.group()

                replace_string = replace_string.replace(ref.group(), value)

        self.clipboard.set_text(replace_string, -1)

        self.window.notify(message)
        clear_clipboard_time = passwordsafe.config_manager.get_clear_clipboard()
        self.clipboard_timer = Timer(
            clear_clipboard_time, GLib.idle_add, args=[self.clear_clipboard]
        )
        self.clipboard_timer.start()

    def on_database_settings_entry_clicked(self, _action, _param):
        DatabaseSettingsDialog(self).present()

    def on_sort_menu_button_entry_clicked(self, _action, _param, sorting):
        self.start_database_lock_timer()
        passwordsafe.config_manager.set_sort_order(sorting)
        self.list_box_sorting = sorting
        self.rebuild_all_pages()

    def on_session_lock(
        self, _connection, _unique_name, _object_path, _interface, _signal, state
    ):
        if state[0] and not self.database_manager.props.locked:
            self.lock_timeout_database()

    #
    # Dialog Creator
    #

    def show_references_dialog(self, _action: Gio.SimpleAction, _param: None) -> None:
        """Show a Group/Entry reference dialog

        Invoked by the app.entry.references action"""
        ReferencesDialog(self).present()

    def show_properties_dialog(self, _action: Gio.SimpleAction, _param: None) -> None:
        """Show a Group/Entry property dialog

        Invoked by the app.element.properties action"""
        PropertiesDialog(self).present()

    #
    # Utils
    #

    def _on_database_lock_changed(self, _database_manager, _value):
        locked = self.database_manager.props.locked
        if locked:
            self.cleanup(False)

            for tmpfile in self.scheduled_tmpfiles_deletion:
                try:
                    tmpfile.delete()
                except GLib.Error as exc:  # pylint: disable=broad-except
                    logging.warning(
                        "Skipping deletion of tmpfile %s: %s",
                        tmpfile.get_path(),
                        exc.message,
                    )

                if passwordsafe.config_manager.get_save_automatically():
                    self.save_database()

            self.divider.hide()
        else:
            self._update_headerbar()
            self.start_save_loop()
            self.divider.show()
            self.start_database_lock_timer()

    def lock_timeout_database(self):
        self.database_manager.props.locked = True

        # NOTE: Notification that a safe has been locked.
        self.window.notify(_("Safe locked due to inactivity"))

    #
    # Helper Methods
    #

    def cleanup(self, delete_tmp_file: bool = True) -> None:
        """Stop all ongoing operations:

        * stop the save loop
        * cancel all timers
        * unregistrer from dbus
        * delete all temporary file is delete_tmp_file is True

        :param bool show_save_dialog: chooe to delete temporary files
        """
        logging.debug("Cleaning database %s", self.database_manager.database_path)

        if self.clipboard_timer is not NotImplemented:
            self.clipboard_timer.cancel()

        if self.database_lock_timer is not NotImplemented:
            self.database_lock_timer.cancel()

        # Do not listen to screensaver kicking in anymore
        connection = Gio.Application.get_default().get_dbus_connection()
        connection.signal_unsubscribe(self.dbus_subscription_id)

        # stop the save loop
        if self.save_loop:
            GLib.source_remove(self.save_loop)
            self.save_loop = None

        self.clipboard.clear()

        if not delete_tmp_file:
            return

        for tmpfile in self.scheduled_tmpfiles_deletion:
            try:
                tmpfile.delete()
            except Gio.Error:
                logging.warning("Skipping deletion of tmpfile...")

    def save_database(self, notification: bool = False) -> None:
        """Save the database.

        If auto_save is False, a dialog asking for confirmation
        will be displayed.
        """
        if not self.database_manager.is_dirty or self.database_manager.save_running:
            return

        save_thread = threading.Thread(target=self.database_manager.save_database, args=[notification])
        save_thread.daemon = False
        save_thread.start()

        logging.debug("Saving database %s", self.database_manager.database_path)

    def clear_clipboard(self):
        clear_clipboard_time = passwordsafe.config_manager.get_clear_clipboard()
        if clear_clipboard_time:
            self.clipboard.clear()

    def start_database_lock_timer(self):
        if self.database_manager.props.locked:
            return

        if self.database_lock_timer is not NotImplemented:
            self.database_lock_timer.cancel()
        timeout = passwordsafe.config_manager.get_database_lock_timeout() * 60
        if timeout:
            self.database_lock_timer = Timer(
                timeout, GLib.idle_add, args=[self.lock_timeout_database]
            )
            self.database_lock_timer.start()

    def start_save_loop(self):
        self.save_loop = GLib.timeout_add_seconds(30, self.threaded_save_loop)

    def threaded_save_loop(self) -> bool:
        """Saves the safe as long as it returns True."""
        if passwordsafe.config_manager.get_save_automatically() is True:
            self.database_manager.save_database()

        return True

    def reference_to_hex_uuid(self, reference_string):
        return reference_string[9:-1].lower()

    #
    # DBus
    #

    def register_dbus_signal(self) -> None:
        """Register a listener so we get notified about screensave kicking in

        In this case we will call self.on_session_lock()"""
        connection = Gio.Application.get_default().get_dbus_connection()
        self.dbus_subscription_id = connection.signal_subscribe(
            None, "org.gnome.ScreenSaver", "ActiveChanged",
            "/org/gnome/ScreenSaver", None,
            Gio.DBusSignalFlags.NONE, self.on_session_lock,)

    def __can_go_back(self):
        db_manager = self.database_manager
        current_element = self.current_element

        if db_manager.check_is_group_object(
            current_element
        ) and db_manager.check_is_root_group(current_element):
            return False
        return True

    def go_back(self):
        db_manager = self.database_manager

        if self.props.selection_mode:
            self.props.selection_mode = False
            return
        if self.props.search_active:
            self.props.search_active = False
            return
        if not self.__can_go_back():
            return

        parent_group = self.current_element.parentgroup

        if db_manager.check_is_root_group(parent_group):
            pathbar = self.pathbar
            pathbar.on_home_button_clicked(pathbar.home_button)

        pathbar_btn_type = passwordsafe.pathbar_button.PathbarButton
        for button in self.pathbar:
            if (
                isinstance(button, pathbar_btn_type)
                and button.element.uuid == parent_group.uuid
            ):
                pathbar = self.pathbar
                pathbar.on_pathbar_button_clicked(button)

        self.pathbar.rebuild_pathbar(parent_group)

    @GObject.Property(type=bool, default=False, flags=GObject.ParamFlags.READWRITE)
    def search_active(self) -> bool:
        """Property to know if search is active.

        It is used by Search to update the widgets (mainly the
        headerbar) accordingly.

        :returns: True is search is active
        :rtype: bool
        """
        return self._search_active

    @search_active.setter  # type: ignore
    def search_active(self, value: bool) -> None:
        """Set the search mode

        :param value: new search_active
        """
        self._search_active = value
        if self._search_active:
            self._unlocked_db_stack.set_visible_child_name("search")
        else:
            self._unlocked_db_stack.set_visible_child_name("browser")
            self.show_page_of_new_directory(False, False)

        self._update_headerbar()

    @GObject.Property(type=bool, default=False, flags=GObject.ParamFlags.READWRITE)
    def database_locked(self):
        """Get database lock status

        :returns: True if the database is locked
        :rtype: bool
        """
        return self.database_manager.props.locked
