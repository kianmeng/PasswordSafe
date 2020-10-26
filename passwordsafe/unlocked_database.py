from gettext import gettext as _
from threading import Timer
from typing import List
from uuid import UUID
import logging
import ntpath
import os
import re
import time
import threading

from gi.repository import Gio, Gdk, Gtk, GLib, Handy
from passwordsafe.custom_keypress_handler import CustomKeypressHandler
from passwordsafe.database_settings_dialog import DatabaseSettingsDialog
from passwordsafe.entry_page import EntryPage
from passwordsafe.entry_row import EntryRow
from passwordsafe.group_page import GroupPage
from passwordsafe.group_row import GroupRow
from passwordsafe.pathbar import Pathbar
from passwordsafe.properties_dialog import PropertiesDialog
from passwordsafe.references_dialog import ReferencesDialog
from passwordsafe.responsive_ui import ResponsiveUI
from passwordsafe.scrolled_page import ScrolledPage
from passwordsafe.selection_ui import SelectionUI
from passwordsafe.search import Search
import passwordsafe.config_manager


class UnlockedDatabase:
    # Instances
    window = NotImplemented
    database_manager = NotImplemented
    unlock_database = NotImplemented
    responsive_ui = NotImplemented
    selection_ui = NotImplemented
    search = NotImplemented
    entry_page = NotImplemented
    group_page = NotImplemented
    custom_keypress_handler = NotImplemented

    # Widgets
    parent_widget = NotImplemented
    headerbar = NotImplemented
    headerbar_search = NotImplemented
    headerbar_box = NotImplemented
    scrolled_window = NotImplemented
    stack = NotImplemented
    divider = NotImplemented
    revealer = NotImplemented
    action_bar = NotImplemented
    pathbar = NotImplemented
    overlay = NotImplemented
    search_overlay = NotImplemented
    database_settings_dialog = NotImplemented
    references_dialog = NotImplemented
    notes_dialog = NotImplemented

    # Objects
    builder = NotImplemented
    current_group = NotImplemented
    accelerators = NotImplemented
    scheduled_page_destroy: List[UUID] = []
    scheduled_tmpfiles_deletion: List[Gio.File] = []
    clipboard = NotImplemented
    list_box_sorting = NotImplemented
    clipboard_timer = NotImplemented
    database_lock_timer = NotImplemented
    database_locked = False
    save_loop = NotImplemented
    dbus_subscription_id = NotImplemented
    listbox_insert_thread = NotImplemented

    def __init__(self, window, widget, dbm, unlock_database):
        # Instances
        self.window = window
        self.parent_widget = widget
        self.database_manager = dbm
        self.unlock_database = unlock_database
        self.responsive_ui = ResponsiveUI(self)
        self.selection_ui = SelectionUI(self)
        self.search = Search(self)
        self.entry_page = EntryPage(self)
        self.group_page = GroupPage(self)
        self.custom_keypress_handler = CustomKeypressHandler(self)

        # Declare database as opened
        self.window.opened_databases.append(self)

        # Browser Mode
        self.assemble_listbox()
        self.start_save_loop()
        self.custom_keypress_handler.register_custom_events()
        self.register_dbus_signal()

        # Responsive UI
        self.responsive_ui.headerbar_title()
        self.responsive_ui.headerbar_back_button()
        self.responsive_ui.headerbar_selection_button()

    #
    # Stack Pages
    #

    def assemble_listbox(self):
        self.builder = Gtk.Builder()
        self.builder.add_from_resource("/org/gnome/PasswordSafe/unlocked_database.ui")

        self.clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)

        self.accelerators = Gtk.AccelGroup()
        self.window.add_accel_group(self.accelerators)

        self.overlay = Gtk.Overlay()
        self.parent_widget.add(self.overlay)

        database_action_overlay = self.builder.get_object("database_action_overlay")
        self.overlay.add_overlay(database_action_overlay)

        self.current_group = self.database_manager.get_root_group()

        self.stack = self.builder.get_object("list_stack")
        # contains the "main page" with the stack and the revealer inside
        self.divider = self.builder.get_object("divider")
        self.revealer = self.builder.get_object("revealer")
        self.headerbar_box = self.builder.get_object("headerbar_box")
        self.action_bar = self.builder.get_object("action_bar")
        self.overlay.add(self.divider)
        self.overlay.show_all()

        self.set_headerbar()

        self.list_box_sorting = passwordsafe.config_manager.get_sort_order()
        self.start_database_lock_timer()

        self.show_page_of_new_directory(False, False)

    #
    # Headerbar
    #

    # Assemble headerbar
    def set_headerbar(self):
        self.headerbar = self.builder.get_object("headerbar")

        save_button = self.builder.get_object("save_button")
        save_button.connect("clicked", self.on_save_button_clicked)

        lock_button = self.builder.get_object("lock_button")
        lock_button.connect("clicked", self.on_lock_button_clicked)

        mod_box = self.builder.get_object("mod_box")
        browser_buttons_box = self.builder.get_object("browser_buttons_box")
        mod_box.add(browser_buttons_box)

        search_button = self.builder.get_object("search_button")
        search_button.connect("clicked", self.search.set_search_headerbar)
        self.bind_accelerator(self.accelerators, search_button, "<Control>f")

        selection_button = self.builder.get_object("selection_button")
        selection_button.connect("clicked", self.selection_ui.set_selection_headerbar)
        selection_button_mobile = self.builder.get_object("selection_button_mobile")
        selection_button_mobile.connect("clicked", self.selection_ui.set_selection_headerbar)

        back_button_mobile = self.builder.get_object("back_button_mobile")
        back_button_mobile.connect("clicked", self.on_back_button_mobile_clicked)

        # Search UI
        self.search.initialize()

        # Selection UI
        self.selection_ui.initialize()

        self.parent_widget.set_headerbar(self.headerbar)
        self.window.set_titlebar(self.headerbar)
        self.pathbar = Pathbar(self, self.database_manager, self.database_manager.get_root_group())
        # Put pathbar in action_bar by default, will be moved up on desktop
        self.action_bar.add(self.pathbar)

    # Group and entry browser headerbar
    def set_browser_headerbar(self):
        self.builder.get_object("linkedbox_right").show_all()

        filename_label = self.builder.get_object("filename_label")
        filename_label.set_text(ntpath.basename(self.database_manager.database_path))

        secondary_menupopover_button = self.builder.get_object("secondary_menupopover_button")
        secondary_menupopover_button.hide()

        self.responsive_ui.headerbar_back_button()
        self.responsive_ui.headerbar_selection_button()
        self.responsive_ui.action_bar()
        self.responsive_ui.headerbar_title()

    #
    # Keystrokes
    #

    def bind_accelerator(self, accelerators, widget, accelerator, signal="clicked"):
        key, mod = Gtk.accelerator_parse(accelerator)
        widget.add_accelerator(signal, accelerators, key, mod, Gtk.AccelFlags.VISIBLE)

    #
    # Group and Entry Management
    #

    def show_page_of_new_directory(self, edit_group, new_entry):
        # First, remove stack pages which should not exist because they are scheduled for remove
        self.destroy_scheduled_stack_page()

        # Check if we need to remove the search headerbar
        if self.parent_widget.get_headerbar() is not self.headerbar:
            self.search.remove_search_headerbar(None)

        # Creation of group edit page
        if edit_group is True:
            self.destroy_scheduled_stack_page()

            builder = Gtk.Builder()
            builder.add_from_resource("/org/gnome/PasswordSafe/group_page.ui")
            scrolled_window = ScrolledPage(True)
            scrolled_window.properties_list_box = builder.get_object("properties_list_box")

            # Responsive Container
            hdy_page = Handy.Clamp()
            hdy_page.add(scrolled_window.properties_list_box)
            scrolled_window.add(hdy_page)
            scrolled_window.show_all()

            stack_page_uuid = self.current_group.uuid
            if self.stack.get_child_by_name(stack_page_uuid.urn) is not None:
                stack_page = self.stack.get_child_by_name(stack_page_uuid.urn)
                stack_page.destroy()

            self.add_stack_page(scrolled_window)
            self.group_page.insert_group_properties_into_listbox(scrolled_window.properties_list_box)
            self.group_page.set_group_edit_page_headerbar()
        # If the stack page with current group's uuid isn't existing - we need to create it (first time opening of group/entry)
        elif (not self.stack.get_child_by_name(self.current_group.uuid.urn)
              and not edit_group):
            self.database_manager.set_element_atime(self.current_group)
            # Create not existing stack page for group
            if self.database_manager.check_is_group(self.current_group.uuid):
                builder = Gtk.Builder()
                builder.add_from_resource("/org/gnome/PasswordSafe/unlocked_database.ui")
                list_box = builder.get_object("list_box")
                list_box.connect("row-activated", self.on_list_box_row_activated)

                scrolled_window = ScrolledPage(False)
                viewport = Gtk.Viewport()
                overlay = Gtk.Overlay()

                # Responsive Container
                list_box.set_name("BrowserListBox")
                list_box.set_valign(Gtk.Align.START)
                list_box.set_margin_top(18)
                list_box.set_margin_bottom(18)

                hdy_browser = Handy.Clamp()
                hdy_browser.set_maximum_size(700)
                hdy_browser.add(list_box)
                overlay.add(hdy_browser)

                viewport.add(overlay)
                scrolled_window.add(viewport)
                scrolled_window.show_all()

                self.add_stack_page(scrolled_window)

                list_box.hide()

                self.listbox_insert_thread = threading.Thread(target=self.insert_groups_into_listbox, args=(list_box, overlay))
                self.listbox_insert_thread.daemon = True
                self.listbox_insert_thread.start()
            # Create not existing stack page for entry
            else:
                builder = Gtk.Builder()
                builder.add_from_resource("/org/gnome/PasswordSafe/entry_page.ui")

                scrolled_window = ScrolledPage(True)

                viewport = Gtk.Viewport()
                scrolled_window.properties_list_box = builder.get_object("properties_list_box")

                # Responsive Container
                hdy_page = Handy.Clamp()
                hdy_page.set_maximum_size(600)
                hdy_page.set_margin_top(18)
                hdy_page.set_margin_bottom(18)
                hdy_page.add(scrolled_window.properties_list_box)
                viewport.add(hdy_page)

                scrolled_window.add(viewport)
                scrolled_window.show_all()

                self.add_stack_page(scrolled_window)
                if new_entry is True:
                    self.entry_page.insert_entry_properties_into_listbox(scrolled_window.properties_list_box, True)
                else:
                    self.entry_page.insert_entry_properties_into_listbox(scrolled_window.properties_list_box, False)
        # Stack page with current group's uuid already exists, we only need to switch stack page
        else:
            self.database_manager.set_element_atime(self.current_group)
            # For group
            if self.database_manager.check_is_group(self.current_group.uuid):
                self.stack.set_visible_child_name(self.current_group.uuid.urn)
                self.set_browser_headerbar()
            # For entry
            else:
                self.stack.set_visible_child_name(self.database_manager.get_entry_uuid_from_entry_object(self.current_group).urn)
                self.entry_page.set_entry_page_headerbar()

    def add_stack_page(self, scrolled_window):
        self.stack.add_named(scrolled_window, self.current_group.uuid.urn)
        self.switch_stack_page()

    def switch_stack_page(self):
        group_page = NotImplemented
        page_uuid = self.current_group.uuid
        group_page = self.database_manager.check_is_group(page_uuid)

        if page_uuid in self.scheduled_page_destroy:
            stack_page = self.stack.get_child_by_name(page_uuid.urn)
            if stack_page is not None:
                stack_page.destroy()

            self.scheduled_page_destroy.remove(page_uuid)
            self.show_page_of_new_directory(False, False)

        if self.stack.get_child_by_name(page_uuid.urn) is None:
            self.show_page_of_new_directory(False, False)
        else:
            self.stack.set_visible_child_name(page_uuid.urn)

        if group_page:
            self.set_browser_headerbar()
        else:
            self.entry_page.set_entry_page_headerbar()

    def update_current_stack_page(self):
        stack_page_name = self.current_group.uuid.urn
        stack_page = self.stack.get_child_by_name(stack_page_name)
        stack_page.destroy()
        self.show_page_of_new_directory(False, False)

    def set_current_group(self, group):
        self.current_group = group

    def get_current_group(self):
        return self.current_group

    def schedule_stack_page_for_destroy(self, page_name):
        self.scheduled_page_destroy.append(page_name)

    def destroy_scheduled_stack_page(self):
        # FIXME: current_group can confusingly also contain an `Entry`
        page_uuid = self.current_group.uuid

        if page_uuid in self.scheduled_page_destroy:
            stack_page_name = self.stack.get_child_by_name(page_uuid.urn)
            if stack_page_name is not None:
                stack_page_name.destroy()
            self.scheduled_page_destroy.remove(page_uuid)

    #
    # Create Group & Entry Rows
    #

    def insert_groups_into_listbox(self, list_box, overlay):
        groups = NotImplemented
        sorted_list = []

        add_loading_indicator_thread = threading.Thread(target=self.add_loading_indicator_thread, args=(list_box, overlay))
        add_loading_indicator_thread.start()

        if self.current_group.is_root_group:
            groups = self.database_manager.get_groups_in_root()
        else:
            groups = self.database_manager.get_groups_in_folder(self.current_group.uuid)

        GLib.idle_add(self.group_instance_creation, list_box, sorted_list, groups)

        self.insert_entries_into_listbox(list_box, overlay)

    def insert_entries_into_listbox(self, list_box, overlay):
        entries = self.database_manager.get_entries_in_folder(self.current_group.uuid)
        sorted_list = []

        GLib.idle_add(self.entry_instance_creation, list_box, sorted_list, entries, overlay)

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
            entry_row = EntryRow(self, self.database_manager, entry)
            sorted_list.append(entry_row)

        if self.list_box_sorting == "A-Z":
            sorted_list.sort(key=lambda entry: str.lower(entry.label), reverse=False)
        elif self.list_box_sorting == "Z-A":
            sorted_list.sort(key=lambda entry: str.lower(entry.label), reverse=True)

        for entry_row in sorted_list:
            list_box.add(entry_row)

        if list_box.get_children():
            list_box.show()
        else:
            builder = Gtk.Builder()
            builder.add_from_resource("/org/gnome/PasswordSafe/unlocked_database.ui")
            empty_group_overlay = builder.get_object("empty_group_overlay")
            overlay.add_overlay(empty_group_overlay)
            list_box.hide()

    def rebuild_all_pages(self):
        for page in self.stack.get_children():
            if page.check_is_edit_page() is False:
                page.destroy()

        self.show_page_of_new_directory(False, False)

    #
    # Events
    #

    def on_list_box_row_activated(self, _widget, list_box_row):
        self.start_database_lock_timer()

        if list_box_row.get_name() == "LoadMoreRow":
            self.search.on_load_more_row_clicked(list_box_row)
            return

        if list_box_row.get_type() == "EntryRow" and self.selection_ui.selection_mode_active is True:
            self.selection_ui.row_selection_toggled(list_box_row)
        elif list_box_row.get_type() == "EntryRow" and self.selection_ui.selection_mode_active is False:
            self.set_current_group(self.database_manager.get_entry_object_from_uuid(list_box_row.get_uuid()))
            self.pathbar.add_pathbar_button_to_pathbar(list_box_row.get_uuid())
            self.show_page_of_new_directory(False, False)
        elif list_box_row.get_type() == "GroupRow":
            self.set_current_group(self.database_manager.get_group(list_box_row.get_uuid()))
            self.pathbar.add_pathbar_button_to_pathbar(list_box_row.get_uuid())
            self.show_page_of_new_directory(False, False)

    def on_save_button_clicked(self, widget):
        self.start_database_lock_timer()
        if widget is not None:
            self.builder.get_object("menubutton_popover").popdown()

        if self.database_manager.is_dirty is True:
            if self.database_manager.save_running is False:
                save_thread = threading.Thread(target=self.database_manager.save_database)
                save_thread.daemon = False
                save_thread.start()
                self.show_database_action_revealer(_("Database saved"))
            else:
                # NOTE: In-app notification to inform the user that already an unfinished save job is running
                self.show_database_action_revealer(_("Please wait. Another save is running."))
        else:
            # NOTE: In-app notification to inform the user that no save is necessary because there where no changes made
            self.show_database_action_revealer(_("No changes made"))

    def on_lock_button_clicked(self, _widget):
        # shows save dialog if required
        self.show_save_dialog()
        self.lock_database()

    def on_add_entry_button_clicked(self, _widget):
        self.builder.get_object("menubutton_popover").popdown()
        self.start_database_lock_timer()
        self.database_manager.changes = True
        entry = self.database_manager.add_entry_to_database("", "", "", None, None, "0", self.current_group.uuid)
        self.current_group = entry
        self.pathbar.add_pathbar_button_to_pathbar(self.database_manager.get_entry_uuid_from_entry_object(self.current_group))
        self.show_page_of_new_directory(False, True)

    def on_add_group_button_clicked(self, _widget):
        self.builder.get_object("menubutton_popover").popdown()
        self.start_database_lock_timer()
        self.database_manager.changes = True
        group = self.database_manager.add_group_to_database("", "0", "", self.current_group)
        self.current_group = group
        self.pathbar.add_pathbar_button_to_pathbar(self.current_group.uuid)
        self.show_page_of_new_directory(True, False)

    def on_entry_row_button_pressed(self, widget, event):
        self.start_database_lock_timer()
        if event.type == Gdk.EventType.BUTTON_PRESS and event.button == 3 and self.search.search_active is False:
            if self.selection_ui.selection_mode_active is False:
                self.selection_ui.set_selection_headerbar(None, select_row=widget.get_parent())
            else:
                self.selection_ui.row_selection_toggled(widget.get_parent())

    def on_element_delete_menu_button_clicked(self, _action, _param):
        self.start_database_lock_timer()

        ele_uuid = self.current_group.uuid
        self.current_group = self.database_manager.get_parent_group(
            self.current_group)
        self.database_manager.delete_from_database(self.current_group)

        # If the deleted entry is in the pathbar, we need to rebuild the pathbar
        if self.pathbar.uuid_in_pathbar(ele_uuid):
            self.pathbar.rebuild_pathbar(self.current_group)

        self.update_current_stack_page()
        self.show_page_of_new_directory(False, False)

    def on_entry_duplicate_menu_button_clicked(self, _action, _param):
        self.start_database_lock_timer()

        self.database_manager.duplicate_entry(self.current_group)
        parent_group = self.database_manager.get_parent_group(
            self.current_group)

        if self.database_manager.check_is_root_group(parent_group) is True:
            self.pathbar.on_home_button_clicked(self.pathbar.home_button)
        else:
            for button in self.pathbar:
                if button.get_name() == "PathbarButtonDynamic" and isinstance(button, passwordsafe.pathbar_button.PathbarButton):
                    if button.uuid == parent_group.uuid:
                        self.pathbar.on_pathbar_button_clicked(button)

        self.current_group = parent_group
        self.update_current_stack_page()
        self.show_page_of_new_directory(False, False)

    def on_group_row_button_pressed(self, widget, event):
        self.start_database_lock_timer()
        if event.type == Gdk.EventType.BUTTON_PRESS and event.button == 3 and self.search.search_active is False:
            if self.selection_ui.selection_mode_active is False:
                self.selection_ui.set_selection_headerbar(None, select_row=widget.get_parent())
            else:
                self.selection_ui.row_selection_toggled(widget.get_parent())

    def on_group_edit_button_clicked(self, button):
        self.start_database_lock_timer()
        group_uuid = button.get_parent().get_parent().get_parent().get_parent().get_uuid()
        group_object = self.database_manager.get_group(group_uuid)

        self.set_current_group(group_object)
        self.pathbar.add_pathbar_button_to_pathbar(group_uuid)
        self.show_page_of_new_directory(True, False)

    def on_copy_secondary_button_clicked(self, widget, _position, _eventbutton):
        self.send_to_clipboard(widget.get_text())

    def send_to_clipboard(self, text):
        self.start_database_lock_timer()
        if self.clipboard_timer is not NotImplemented:
            self.clipboard_timer.cancel()

        replace_string = text
        for ref in re.finditer("({REF:.*?})", text):
            not_valid = False
            code = ref.group()[5]

            try:
                uuid = UUID(self.reference_to_hex_uuid(ref.group()))
            except Exception:
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
                        print("FAIL")
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

        self.show_database_action_revealer(_("Copied to clipboard"))
        clear_clipboard_time = passwordsafe.config_manager.get_clear_clipboard()
        self.clipboard_timer = Timer(clear_clipboard_time, GLib.idle_add, args=[self.clear_clipboard])
        self.clipboard_timer.start()

    def on_database_settings_entry_clicked(self, _action, _param):
        DatabaseSettingsDialog(self)

    def on_sort_menu_button_entry_clicked(self, _action, _param, sorting):
        self.start_database_lock_timer()
        passwordsafe.config_manager.set_sort_order(sorting)
        self.list_box_sorting = sorting
        self.rebuild_all_pages()

    def on_session_lock(self, _connection, _unique_name, _object_path, _interface, _signal, state):
        if state[0] is True and self.database_locked is False:
            self.lock_timeout_database()

    def on_back_button_mobile_clicked(self, button):
        page_uuid = self.current_group.uuid
        scrolled_page = self.stack.get_child_by_name(page_uuid.urn)
        group_page = self.database_manager.check_is_group(page_uuid)

        parent = NotImplemented

        if scrolled_page.edit_page is True and group_page is True:
            parent = self.database_manager.get_parent_group(page_uuid)
        elif scrolled_page.edit_page is True and group_page is False:
            parent = self.database_manager.get_parent_group(page_uuid)
        elif (not scrolled_page.edit_page
              and not self.selection_ui.selection_mode_active
              and not self.search_active):
            if self.database_manager.check_is_root_group(self.current_group) is True:
                self.on_lock_button_clicked(None)
                return

            parent = self.database_manager.get_parent_group(page_uuid)

        if self.database_manager.check_is_root_group(parent) is True:
            self.pathbar.on_home_button_clicked(self.pathbar.home_button)
            return

        for button in self.pathbar:
            if button.get_name() == "PathbarButtonDynamic" and isinstance(button, passwordsafe.pathbar_button.PathbarButton):
                if button.uuid == parent.uuid:
                    self.pathbar.on_pathbar_button_clicked(button)

    #
    # Dialog Creator
    #

    def show_save_dialog(self) -> bool:
        """ Show the save confirmation dialog

        Saves the db and closes the tab.
        :returns: True if we saved, False if the whole thing should be aborted
        """
        if not self.database_manager.is_dirty \
           or self.database_manager.save_running:
            return True  # no dirty db, do nothing.
        builder = Gtk.Builder()
        builder.add_from_resource("/org/gnome/PasswordSafe/save_dialog.ui")
        save_dialog = builder.get_object("save_dialog")
        save_dialog.set_transient_for(self.window)

        res = save_dialog.run()
        save_dialog.destroy()
        if res in (Gtk.ResponseType.CANCEL, Gtk.ResponseType.DELETE_EVENT):
            # Cancel everything, don't quit. Also activated when pressing escape
            return False

        if res == Gtk.ResponseType.NO:
            # clicked 'Discard'. Close, but don't save
            pass  # We are done with this db.
        elif res == Gtk.ResponseType.YES:
            # "clicked save". Save changes
            save_thread = threading.Thread(
                target=self.database_manager.save_database)
            save_thread.daemon = False
            save_thread.start()
        else:
            assert False, "Unknown Dialog Response!"

        return True

    def show_references_dialog(self, _action, _param):
        ReferencesDialog(self)

    def show_properties_dialog(self, _action: Gio.SimpleAction, _param: None) -> None:
        """Show a Group/Entry property dialog

        Invoked by the app.element.properties action"""
        PropertiesDialog(self).present()

    #
    # Utils
    #

    def show_database_action_revealer(self, message):
        database_action_label = self.builder.get_object("database_action_label")
        database_action_label.set_text(message)

        database_action_revealer = self.builder.get_object("database_action_revealer")
        database_action_revealer.set_reveal_child(not database_action_revealer.get_reveal_child())
        revealer_timer = Timer(3.0, GLib.idle_add, args=[self.hide_database_action_revealer])
        revealer_timer.start()

    def hide_database_action_revealer(self):
        database_action_revealer = self.builder.get_object("database_action_revealer")
        database_action_revealer.set_reveal_child(not database_action_revealer.get_reveal_child())

    def lock_database(self):
        self.cancel_timers()
        self.clipboard.clear()
        self.database_locked = True
        self.unregister_dbus_signal()
        self.stop_save_loop()

        if self.database_settings_dialog is not NotImplemented:
            self.database_settings_dialog.close()

        if self.notes_dialog is not NotImplemented:
            self.notes_dialog.close()

        if self.references_dialog is not NotImplemented:
            self.references_dialog.close()

        for tmpfile in self.scheduled_tmpfiles_deletion:
            try:
                tmpfile.delete()
            except Exception:
                logging.warning("Skipping deletion of tmpfile...")

        if passwordsafe.config_manager.get_save_automatically() is True:
            save_thread = threading.Thread(target=self.database_manager.save_database)
            save_thread.daemon = False
            save_thread.start()

        self.window.opened_databases.remove(self)
        self.window.close_tab(self.parent_widget)

        self.window.start_database_opening_routine(ntpath.basename(self.database_manager.database_path), self.database_manager.database_path)

    def lock_timeout_database(self):
        self.cancel_timers()
        self.database_locked = True
        self.stop_save_loop()
        self.clipboard.clear()

        if self.database_settings_dialog is not NotImplemented:
            self.database_settings_dialog.close()

        if self.references_dialog is not NotImplemented:
            self.references_dialog.close()

        if passwordsafe.config_manager.get_save_automatically() is True:
            save_thread = threading.Thread(target=self.database_manager.save_database)
            save_thread.daemon = False
            save_thread.start()

        self.overlay.hide()
        self.unlock_database.unlock_database(timeout=True, unlocked_database=self)

        # NOTE: Notification that a safe has been locked, Notification title has the safe file name in it
        self.send_notification(_("%s locked") % (os.path.splitext(ntpath.basename(self.database_manager.database_path))[0]), _("Keepass safe locked due to inactivity"))

    #
    # Helper Methods
    #

    def undo_redo_receiver(self, action):
        if not isinstance(self.window.get_focus(), Gtk.Entry) and not isinstance(self.window.get_focus(), Gtk.TextView):
            return

        if not isinstance(self.window.get_focus().get_buffer(), passwordsafe.history_buffer.HistoryTextBuffer) and not isinstance(self.window.get_focus().get_buffer(), passwordsafe.history_buffer.HistoryEntryBuffer):
            return

        if "TabBox" not in self.window.get_focus().get_name():
            return

        buffer = self.window.get_focus().get_buffer()
        text = ""
        if action == "undo":
            text = buffer.logic.do_undo()
        else:
            text = buffer.logic.do_redo()

        if text is not None:
            buffer.set_text(text, len(text))

    def clear_clipboard(self):
        clear_clipboard_time = passwordsafe.config_manager.get_clear_clipboard()
        if clear_clipboard_time:
            self.clipboard.clear()

    def start_database_lock_timer(self):
        if self.database_locked is True:
            return

        if self.database_lock_timer is not NotImplemented:
            self.database_lock_timer.cancel()
        timeout = passwordsafe.config_manager.get_database_lock_timeout() * 60
        if timeout:
            self.database_lock_timer = Timer(timeout, GLib.idle_add, args=[self.lock_timeout_database])
            self.database_lock_timer.start()

    def cancel_timers(self):
        if self.clipboard_timer is not NotImplemented:
            self.clipboard_timer.cancel()

        if self.database_lock_timer is not NotImplemented:
            self.database_lock_timer.cancel()

    def send_notification(self, title, text):
        notification = Gio.Notification.new(title)
        notification.set_body(text)
        self.window.application.send_notification(None, notification)

    def start_save_loop(self):
        self.save_loop = True
        save_loop_thread = threading.Thread(target=self.threaded_save_loop)
        save_loop_thread.daemon = True
        save_loop_thread.start()

    def threaded_save_loop(self):
        while self.save_loop is True:
            if passwordsafe.config_manager.get_save_automatically() is True:
                self.builder.get_object("save_button").set_sensitive(False)
                self.database_manager.save_database()
            else:
                self.builder.get_object("save_button").set_sensitive(True)
            time.sleep(30)

    def stop_save_loop(self):
        self.builder.get_object("save_button").set_sensitive(True)
        self.save_loop = False

    def add_loading_indicator_thread(self, list_box, overlay):
        time.sleep(1)
        if list_box.is_visible() is False and len(overlay.get_children()) < 2:
            GLib.idle_add(self.show_loading_indicator, list_box, overlay)

    def show_loading_indicator(self, list_box, overlay):
        spinner = Gtk.Spinner()
        spinner.show()
        spinner.start()
        overlay.add_overlay(spinner)

        remove_loading_indicator_thread = threading.Thread(target=self.remove_loading_indicator_thread, args=(list_box, overlay, spinner))
        remove_loading_indicator_thread.start()

    def remove_loading_indicator_thread(self, list_box, overlay, spinner):
        while list_box.is_visible() is False:
            continue
        else:
            GLib.idle_add(self.remove_loading_indicator, overlay, spinner)

    def remove_loading_indicator(self, overlay, spinner):
        overlay.remove(spinner)

    def reference_to_hex_uuid(self, reference_string):
        return reference_string[9:-1].lower()

    #
    # DBus
    #

    def register_dbus_signal(self):
        app = Gio.Application.get_default
        self.dbus_subscription_id = app().get_dbus_connection().signal_subscribe(None, "org.gnome.ScreenSaver", "ActiveChanged", "/org/gnome/ScreenSaver", None, Gio.DBusSignalFlags.NONE, self.on_session_lock)

    def unregister_dbus_signal(self):
        app = Gio.Application.get_default
        app().get_dbus_connection().signal_unsubscribe(self.dbus_subscription_id)

    @property
    def search_active(self):
        """Property to know if search is active

        :returns: True is search view is visible
        :rtype: bool
        """
        search_view = self.stack.get_child_by_name("search")
        return self.stack.get_visible_child() is search_view
