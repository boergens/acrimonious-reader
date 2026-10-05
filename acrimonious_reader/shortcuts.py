"""Keyboard shortcuts: one table for both the accelerators and the shortcuts dialog."""

from gi.repository import Adw, Gdk, Gtk

# (action, keys, description). Actions are installed as application accelerators. Rows without an
# action are keys the document view handles itself, so they do not get in the way of text entries.
SHORTCUTS = (
    ("Documents", (
        ("win.open", ("<Control>o",), "Open a document"),
        ("app.new-window", ("<Control>n",), "New window"),
        ("win.save", ("<Control>s",), "Save"),
        ("win.save-as", ("<Control><Shift>s",), "Save as…"),
        ("win.print", ("<Control>p",), "Print"),
        ("win.reload", ("<Control>r",), "Reload"),
        ("win.properties", ("<Alt>Return",), "Properties"),
        ("win.close", ("<Control>w",), "Close window"),
        ("app.quit", ("<Control>q",), "Quit"),
    )),
    ("Moving Around", (
        (None, ("Up", "Down"), "Scroll"),
        (None, ("space", "<Shift>space"), "Next or previous screen"),
        (None, ("n", "p"), "Next or previous page"),
        (None, ("Home", "End"), "Start or end of the document"),
        ("win.go-to-page", ("<Control>l",), "Go to page…"),
        ("win.back", ("<Alt>Left",), "Back"),
        ("win.forward", ("<Alt>Right",), "Forward"),
        (None, ("Middle-click drag",), "Pan"),
    )),
    ("Zoom and View", (
        ("win.zoom-in", ("<Control>plus", "<Control>equal", "<Control>KP_Add"), "Zoom in"),
        ("win.zoom-out", ("<Control>minus", "<Control>KP_Subtract"), "Zoom out"),
        ("win.zoom-reset", ("<Control>0",), "Original size"),
        (None, ("Ctrl + scroll",), "Zoom at the pointer"),
        (None, ("<Control>Left", "<Control>Right"), "Rotate left or right"),
        ("win.night", ("<Control>i",), "Night mode"),
        ("win.sidebar", ("F9",), "Sidebar"),
        ("win.fullscreen", ("F11",), "Fullscreen"),
    )),
    ("Writing and Drawing", (
        ("win.annotate", ("<Control>e",), "Write and draw"),
        (None, ("<Control>z", "<Control><Shift>z"), "Undo or redo"),
        (None, ("Delete",), "Delete the chosen text box or drawing"),
        (None, ("Left", "<Shift>Left"), "Nudge it by 1 or 10 points (all arrow keys)"),
        (None, ("Return",), "Edit the chosen text box"),
        (None, ("Escape", "<Control>Return"), "Finish typing"),
        (None, ("Escape",), "Cancel placing a signature"),
    )),
    ("Search and Selection", (
        ("win.find", ("<Control>f",), "Find"),
        ("win.find-next", ("<Control>g", "F3"), "Next match"),
        ("win.find-previous", ("<Control><Shift>g", "<Shift>F3"), "Previous match"),
        (None, ("<Control>c",), "Copy the selected text"),
        (None, ("<Control>a",), "Select all text"),
        (None, ("Escape",), "Clear the selection"),
    )),
    ("General", (
        ("app.shortcuts", ("<Control>question",), "Keyboard shortcuts"),
        (None, ("F10",), "Main menu"),
    )),
)


def install(app):
    for _, rows in SHORTCUTS:
        for action, keys, _ in rows:
            if action is not None:
                app.set_accels_for_action(action, keys)


def _keycaps(accel):
    """A row of keycap labels for one accelerator such as '<Control>plus'."""
    box = Gtk.Box(spacing=4, valign=Gtk.Align.CENTER)
    ok, key, mods = Gtk.accelerator_parse(accel)
    if ok and key:
        modifiers = ((Gdk.ModifierType.CONTROL_MASK, "Ctrl"), (Gdk.ModifierType.SHIFT_MASK, "Shift"),
                     (Gdk.ModifierType.ALT_MASK, "Alt"))
        names = [name for mask, name in modifiers if mods & mask]
        names.append(Gtk.accelerator_get_label(key, 0))
    else:
        names = [accel]
    for name in names:
        label = Gtk.Label(label=name)
        label.add_css_class("keycap")
        box.append(label)
    return box


def shortcuts_dialog():
    page = Adw.PreferencesPage()
    for title, rows in SHORTCUTS:
        group = Adw.PreferencesGroup(title=title)
        for _, keys, description in rows:
            row = Adw.ActionRow(title=description, use_markup=False)
            suffix = Gtk.Box(spacing=10)
            for accel in keys[:2]:
                suffix.append(_keycaps(accel))
            row.add_suffix(suffix)
            group.add(row)
        page.add(group)
    toolbar = Adw.ToolbarView(content=page)
    toolbar.add_top_bar(Adw.HeaderBar())
    return Adw.Dialog(title="Keyboard Shortcuts", child=toolbar, content_width=520, content_height=640)
