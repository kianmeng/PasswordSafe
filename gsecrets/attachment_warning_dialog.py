# SPDX-License-Identifier: GPL-3.0-only
from __future__ import annotations

import logging
import os
import typing
from gettext import gettext as _

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from gsecrets import const

if typing.TYPE_CHECKING:
    from pykeepass.attachment import Attachment

    from gsecrets.entry_page import EntryPage  # pylint: disable=ungrouped-imports


@Gtk.Template(resource_path="/org/gnome/World/Secrets/gtk/attachment_warning_dialog.ui")
class AttachmentWarningDialog(Adw.MessageDialog):

    __gtype_name__ = "AttachmentWarningDialog"

    def __init__(self, entry_page: EntryPage, attachment: Attachment) -> None:
        """Dialog to confirm opening an attachment

        :param entry_page: entry page
        """
        super().__init__(transient_for=entry_page.unlocked_database.window)

        self.__unlocked_database = entry_page.unlocked_database
        self.__attachment = attachment

    @Gtk.Template.Callback()
    def _on_proceed(self, _dialog, _response):
        attachment = self.__attachment
        u_db = self.__unlocked_database
        self.__open_tmp_file(
            u_db.database_manager.db.binaries[attachment.id], attachment.filename
        )

    def __open_tmp_file(self, bytes_buffer, filename):
        cache_dir = os.path.join(GLib.get_user_cache_dir(), const.SHORT_NAME, "tmp")
        file_path = os.path.join(cache_dir, filename)
        window = self.__unlocked_database.window
        if not os.path.exists(cache_dir):
            os.makedirs(cache_dir)

        gfile = Gio.File.new_for_path(file_path)

        def callback(gfile, result):
            try:
                gfile.replace_contents_finish(result)
            except GLib.Error as err:
                logging.debug("Could not load attachment: %s", err.message)
                window.send_notification(_("Could not load attachment"))
            else:
                Gtk.show_uri(window, gfile.get_uri(), Gdk.CURRENT_TIME)

        contents = GLib.Bytes.new(bytes_buffer)
        gfile.replace_contents_bytes_async(
            contents,
            None,
            False,
            Gio.FileCreateFlags.PRIVATE | Gio.FileCreateFlags.REPLACE_DESTINATION,
            None,
            callback,
        )
