"""The application: windows, app-wide actions and resources."""

from pathlib import Path

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from . import APP_ID, APP_NAME, VERSION, WEBSITE, shortcuts
from .state import StateStore
from .window import Window

_HERE = Path(__file__).resolve().parent


class Application(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_OPEN)
        GLib.set_application_name(APP_NAME)
        self.state = StateStore()

    def do_startup(self):
        Adw.Application.do_startup(self)
        display = Gdk.Display.get_default()
        css = Gtk.CssProvider()
        css.load_from_path(str(_HERE / "style.css"))
        Gtk.StyleContext.add_provider_for_display(display, css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        icons = _HERE.parent / "data" / "icons"  # when running from the source tree
        if icons.is_dir():
            Gtk.IconTheme.get_for_display(display).add_search_path(str(icons))
        Gtk.Window.set_default_icon_name(APP_ID)

        for name, callback in (("new-window", lambda: self.new_window().present()),
                               ("shortcuts", self._on_shortcuts),
                               ("about", self._on_about),
                               ("quit", self._on_quit)):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda _action, _param, callback=callback: callback())
            self.add_action(action)
        shortcuts.install(self)

    def do_activate(self):
        window = self.get_active_window() or self.new_window()
        window.present()

    def do_open(self, files, n_files, hint):
        for gfile in files:
            self.open_in_window(gfile)

    def do_shutdown(self):
        for window in self.get_windows():
            if isinstance(window, Window):
                window.save_position()  # quitting from elsewhere skips the windows' close handlers
        self.state.save()
        Adw.Application.do_shutdown(self)

    def new_window(self):
        return Window(self)

    def open_in_window(self, gfile):
        """Show a document: in the window that already has it, else an empty one, else a new one."""
        windows = [window for window in self.get_windows() if isinstance(window, Window)]
        for window in windows:
            if window.document is not None and window.document.file.equal(gfile):
                window.present()
                return window
        window = next((w for w in windows if w.document is None and not w.loading), None)
        window = window or self.new_window()
        window.open_file(gfile)
        window.present()
        return window

    def _on_shortcuts(self):
        shortcuts.shortcuts_dialog().present(self.get_active_window())

    def _on_about(self):
        about = Adw.AboutDialog(
            application_name=APP_NAME,
            application_icon=APP_ID,
            version=VERSION,
            comments="Read PDFs, fill in forms and sign them",
            developer_name="Kevin Boergens",
            license_type=Gtk.License.GPL_3_0,
            website=WEBSITE,
            issue_url=f"{WEBSITE}/issues",
        )
        about.add_credit_section("Built with", ["GTK", "libadwaita", "Poppler"])
        about.present(self.get_active_window())

    def _on_quit(self):
        # Closing each window lets it ask about unsaved changes; the last one ends the application.
        for window in self.get_windows():
            window.close()
