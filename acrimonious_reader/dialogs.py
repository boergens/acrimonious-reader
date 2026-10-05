"""Smaller dialogs: document properties and password prompts."""

from gi.repository import Adw, Gtk


def properties_dialog(document):
    group = Adw.PreferencesGroup()
    for name, value in document.properties:
        if value:
            row = Adw.ActionRow(title=name, subtitle=value, subtitle_selectable=True, use_markup=False)
            row.add_css_class("property")
            group.add(row)
    page = Adw.PreferencesPage()
    page.add(group)
    toolbar = Adw.ToolbarView(content=page)
    toolbar.add_top_bar(Adw.HeaderBar())
    return Adw.Dialog(title="Properties", child=toolbar, content_width=460, content_height=620)


def ask_password(parent, name, retry, on_password, on_cancel):
    """Ask for a document's password; calls on_password(password) or on_cancel()."""
    body = f"“{name}” is protected with a password."
    if retry:
        body = "That password was not correct. Please try again."
    dialog = Adw.AlertDialog(heading="Password Required", body=body)
    entry = Gtk.PasswordEntry(show_peek_icon=True, activates_default=True)
    entry.update_property([Gtk.AccessibleProperty.LABEL], ["Password"])
    dialog.set_extra_child(entry)
    dialog.add_response("cancel", "_Cancel")
    dialog.add_response("unlock", "_Unlock")
    dialog.set_response_appearance("unlock", Adw.ResponseAppearance.SUGGESTED)
    dialog.set_default_response("unlock")
    dialog.set_close_response("cancel")

    def respond(dialog, response):
        if response == "unlock":
            on_password(entry.get_text())
        else:
            on_cancel()

    dialog.connect("response", respond)
    dialog.present(parent)
    entry.grab_focus()
