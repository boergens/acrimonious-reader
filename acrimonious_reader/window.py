"""The main window: header bar, search bar, sidebar and the document view."""

import threading
from pathlib import Path

from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk

from . import APP_ID, APP_NAME
from . import document as documents
from . import pdfwrite
from .dialogs import ask_password, properties_dialog
from .printing import print_document
from .search import Search
from .session import Session
from .sidebar import Sidebar
from .view import DocumentView

ZOOM_PRESETS = (0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 3.0, 4.0)
RELOAD_DELAY_MS = 400  # wait for a changed file to settle before reloading it
RECENT_LIMIT = 6
PEN_WIDTHS = (("Fine", 0.8), ("Medium", 1.5), ("Bold", 3.0))  # points

# Actions that need an open document.
DOCUMENT_ACTIONS = ("print", "properties", "reload", "zoom-in", "zoom-out", "zoom-reset", "zoom-set",
                    "rotate-left", "rotate-right", "find", "find-next", "find-previous",
                    "go-to-page", "select-all")


def _menu(*sections):
    menu = Gio.Menu()
    for items in sections:
        section = Gio.Menu()
        for label, action in items:
            section.append(label, action)
        menu.append_section(None, section)
    return menu


class Window(Adw.ApplicationWindow):
    __gtype_name__ = "AcrimoniousWindow"

    def __init__(self, app):
        super().__init__(application=app, title=APP_NAME)
        self.state = app.state
        self.session = None
        self.document = None
        self.loading = False
        self._password = None
        self._load_generation = 0
        self._monitor = None
        self._reload_id = 0
        self._save_id = 0
        self._saved_etag = None  # the file as this window last wrote it
        self._close_confirmed = False
        self._annotations_handler = 0
        self._syncing_style = False
        self._recent_rows = []
        self.set_size_request(360, 360)
        width, height = self.state.preference("window-size", (1000, 820))
        self.set_default_size(width, height)
        if self.state.preference("window-maximized", False):
            self.maximize()
        self._build()
        self._add_actions()
        self._update_actions()
        self.connect("close-request", self._on_close_request)

    # Building the interface

    def _build(self):
        self.view = DocumentView()
        self.search = Search()
        self.view.search = self.search
        self.view.connect("notify::page", self._on_page_changed)
        self.view.connect("notify::zoom", lambda *_: self._update_zoom_label())
        for name in ("has-selection", "can-go-back", "can-go-forward"):
            self.view.connect(f"notify::{name}", lambda *_: self._update_actions())
        for name in ("zoom", "zoom-mode", "rotation", "dual-page"):
            self.view.connect(f"notify::{name}", lambda *_: self._schedule_save())
        self.view.connect("toast", lambda _, text: self.toast(text))
        self.view.connect("named-action", self._on_named_action)
        self.view.connect("open-remote", self._on_open_remote)
        self.search.connect("changed", self._on_search_changed)
        self.search.connect("reveal", lambda _, page, index: self.view.reveal_match(page, index))

        scroller = Gtk.ScrolledWindow(child=self.view)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.stack.add_named(self._build_welcome(), "welcome")
        self.stack.add_named(Adw.Spinner(width_request=48, height_request=48, halign=Gtk.Align.CENTER,
                                         valign=Gtk.Align.CENTER), "loading")
        self.stack.add_named(scroller, "document")
        self.error_page = Adw.StatusPage(icon_name="dialog-warning-symbolic")
        retry = Gtk.Button(label="_Open Another…", use_underline=True, action_name="win.open",
                           halign=Gtk.Align.CENTER)
        retry.add_css_class("pill")
        self.error_page.set_child(retry)
        self.stack.add_named(self.error_page, "error")

        header = Adw.HeaderBar()
        sidebar_button = Gtk.ToggleButton(icon_name="sidebar-show-symbolic", action_name="win.sidebar",
                                          tooltip_text="Sidebar")
        header.pack_start(sidebar_button)
        header.pack_start(self._build_page_entry())
        self.title = Adw.WindowTitle(title=APP_NAME)
        header.set_title_widget(self.title)
        main_menu = _menu(
            (("_Open…", "win.open"), ("_New Window", "app.new-window")),
            (("_Save", "win.save"), ("Save _As…", "win.save-as"), ("_Print…", "win.print"),
             ("_Reload", "win.reload"), ("P_roperties", "win.properties")),
            (("Rotate _Left", "win.rotate-left"), ("Rotate R_ight", "win.rotate-right"),
             ("_Dual Pages", "win.dual"), ("Ni_ght Mode", "win.night"), ("_Fullscreen", "win.fullscreen")),
            (("_Keyboard Shortcuts", "app.shortcuts"), (f"_About {APP_NAME}", "app.about")),
        )
        self._show_accel(main_menu, {"win.rotate-left": "<Control>Left", "win.rotate-right": "<Control>Right"})
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=main_menu,
                                       primary=True, tooltip_text="Main Menu"))
        self.zoom_label = Gtk.Label(label="100%")
        self.zoom_label.add_css_class("numeric")
        self.zoom_button = Gtk.MenuButton(child=self.zoom_label, menu_model=self._zoom_menu(),
                                          tooltip_text="Zoom", always_show_arrow=True)
        header.pack_end(self.zoom_button)
        self.search_button = Gtk.ToggleButton(icon_name="edit-find-symbolic", tooltip_text="Find")
        header.pack_end(self.search_button)
        self.edit_button = Gtk.ToggleButton(icon_name="document-edit-symbolic", action_name="win.annotate",
                                            tooltip_text="Write and Draw")
        header.pack_end(self.edit_button)
        self.searchbar = self._build_searchbar()
        self.search_button.bind_property("active", self.searchbar, "search-mode-enabled",
                                         GObject.BindingFlags.BIDIRECTIONAL | GObject.BindingFlags.SYNC_CREATE)
        self._document_widgets = (sidebar_button, self.page_box, self.zoom_button, self.search_button,
                                  self.edit_button)

        self.toolbar = Adw.ToolbarView(content=self.stack)
        self.toolbar.add_top_bar(header)
        self.toolbar.add_top_bar(self.searchbar)
        self.edit_bar = self._build_edit_bar()
        self.toolbar.add_bottom_bar(self.edit_bar)
        self.view.connect("annotation-chosen", lambda *_: self._sync_style_controls())
        self.view.connect("notify::tool", lambda *_: self._sync_style_controls())

        self.sidebar = Sidebar()
        self.sidebar.connect("activate-target", self._on_sidebar_target)
        self.split = Adw.OverlaySplitView(sidebar=self.sidebar, content=self.toolbar,
                                          show_sidebar=self.state.preference("sidebar", False),
                                          min_sidebar_width=200, max_sidebar_width=280,
                                          pin_sidebar=True)
        self.split.connect("notify::show-sidebar", self._on_sidebar_toggled)
        self.split.connect("notify::collapsed", self._on_collapsed)
        self.toasts = Adw.ToastOverlay(child=self.split)
        self.set_content(self.toasts)

        narrow = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 640sp"))
        narrow.add_setter(self.split, "collapsed", True)
        self.add_breakpoint(narrow)
        narrower = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 560sp"))
        narrower.add_setter(self.split, "collapsed", True)
        narrower.add_setter(header, "show-title", False)  # the window title still names the document
        narrower.add_setter(self.page_count, "visible", False)
        narrower.add_setter(self.page_entry, "width-chars", 3)
        narrower.add_setter(self.zoom_button, "always-show-arrow", False)
        self.add_breakpoint(narrower)

        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop.connect("drop", self._on_drop)
        self.toasts.add_controller(drop)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key_pressed)
        self.add_controller(keys)
        # On the window itself, in the capture phase: the top edge of a fullscreen window may lie
        # outside every child (in a border), and no child should be able to swallow the motion.
        motion = Gtk.EventControllerMotion(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        motion.connect("motion", self._on_pointer_motion)
        self.add_controller(motion)
        self.connect("notify::fullscreened", self._on_fullscreen_changed)

    def _build_welcome(self):
        page = Adw.StatusPage(icon_name=APP_ID, title="Open a Document",
                              description="Drag and drop a PDF here, or choose one to open")
        button = Gtk.Button(label="_Open…", use_underline=True, action_name="win.open", halign=Gtk.Align.CENTER)
        button.add_css_class("pill")
        button.add_css_class("suggested-action")
        self.recent_group = Adw.PreferencesGroup(title="Recent Documents")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=36)
        box.append(button)
        box.append(Adw.Clamp(maximum_size=480, child=self.recent_group))
        page.set_child(box)
        recent = Gtk.RecentManager.get_default()
        self._recent_handler = recent.connect("changed", lambda *_: self._fill_recent())
        self._fill_recent()
        return page

    def _fill_recent(self):
        for row in self._recent_rows:
            self.recent_group.remove(row)
        self._recent_rows = []
        items = [item for item in Gtk.RecentManager.get_default().get_items()
                 if item.get_mime_type() == "application/pdf" and item.exists()]
        items.sort(key=lambda item: item.get_modified().to_unix(), reverse=True)
        home = str(Path.home())
        for item in items[:RECENT_LIMIT]:
            gfile = Gio.File.new_for_uri(item.get_uri())
            folder = gfile.get_parent().get_parse_name() if gfile.get_parent() else ""
            if folder.startswith(home):
                folder = "~" + folder[len(home):]
            row = Adw.ActionRow(title=item.get_display_name(), subtitle=folder, activatable=True,
                                use_markup=False, title_lines=1, subtitle_lines=1)
            row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
            row.connect("activated", lambda _, gfile=gfile: self.open_file(gfile))
            self.recent_group.add(row)
            self._recent_rows.append(row)
        self.recent_group.set_visible(bool(self._recent_rows))

    def _build_page_entry(self):
        self.page_entry = Gtk.Entry(width_chars=4, max_width_chars=6, xalign=1,
                                    tooltip_text="Go to Page")
        self.page_entry.update_property([Gtk.AccessibleProperty.LABEL], ["Page"])
        self.page_entry.connect("activate", self._on_page_entry_activate)
        focus = Gtk.EventControllerFocus()
        focus.connect("leave", lambda *_: self._update_page_display())
        self.page_entry.add_controller(focus)
        self.page_count = Gtk.Label()
        self.page_count.add_css_class("dim-label")
        self.page_count.add_css_class("numeric")
        self.page_box = Gtk.Box(spacing=8)
        self.page_box.append(self.page_entry)
        self.page_box.append(self.page_count)
        return self.page_box

    def _zoom_menu(self):
        menu = Gio.Menu()
        buttons = Gio.Menu()
        for label, action, icon in (("Zoom Out", "win.zoom-out", "zoom-out-symbolic"),
                                    ("Original Size", "win.zoom-reset", "zoom-original-symbolic"),
                                    ("Zoom In", "win.zoom-in", "zoom-in-symbolic")):
            item = Gio.MenuItem.new(label, action)
            item.set_attribute_value("verb-icon", GLib.Variant.new_string(icon))
            buttons.append_item(item)
        section = Gio.MenuItem.new_section(None, buttons)
        section.set_attribute_value("display-hint", GLib.Variant.new_string("horizontal-buttons"))
        menu.append_item(section)
        modes = Gio.Menu()
        modes.append("_Automatic", "win.zoom-mode::auto")
        modes.append("Fit _Width", "win.zoom-mode::width")
        modes.append("Fit _Page", "win.zoom-mode::page")
        menu.append_section(None, modes)
        presets = Gio.Menu()
        for zoom in ZOOM_PRESETS:
            presets.append(f"{round(zoom * 100)}%", f"win.zoom-set({zoom!r})")
        menu.append_section(None, presets)
        return menu

    @staticmethod
    def _show_accel(menu, accels):
        """Show accelerator hints for keys the view handles itself (not registered globally)."""
        for i in range(menu.get_n_items()):
            section = menu.get_item_link(i, Gio.MENU_LINK_SECTION)
            items = [(section.get_item_attribute_value(j, "label", None),
                      section.get_item_attribute_value(j, "action", None))
                     for j in range(section.get_n_items())]
            for j, (label, action) in enumerate(items):
                if action is not None and action.get_string() in accels:
                    item = Gio.MenuItem.new(label.get_string(), action.get_string())
                    item.set_attribute_value("accel", GLib.Variant.new_string(accels[action.get_string()]))
                    section.remove(j)
                    section.insert_item(j, item)

    def _build_edit_bar(self):
        tools = Adw.ToggleGroup()
        for name, icon, tooltip in (("browse", "acrimonious-reader-pointer-symbolic", "Select and Move"),
                                    ("text", "insert-text-symbolic", "Text Box"),
                                    ("pen", "acrimonious-reader-pen-symbolic", "Pen"),
                                    ("eraser", "acrimonious-reader-eraser-symbolic", "Eraser")):
            tools.add(Adw.Toggle(name=name, icon_name=icon, tooltip=tooltip))
        tools.bind_property("active-name", self.view, "tool",
                            GObject.BindingFlags.BIDIRECTIONAL | GObject.BindingFlags.SYNC_CREATE)

        self.color_button = Gtk.ColorDialogButton(
            dialog=Gtk.ColorDialog(title="Color", with_alpha=False), tooltip_text="Color")
        self.color_button.set_rgba(_rgba((0, 0, 0)))
        self.color_button.connect("notify::rgba", self._on_color_set)

        self.size_spin = Gtk.SpinButton.new_with_range(4, 72, 0.5)
        self.size_spin.set_value(self.view.style["size"])
        self.size_spin.set_tooltip_text("Text Size (points)")
        self.size_spin.update_property([Gtk.AccessibleProperty.LABEL], ["Text size"])
        self.size_spin.connect("value-changed", self._on_size_set)
        self.size_box = Gtk.Box(spacing=6)
        self.size_box.append(Gtk.Image(icon_name="font-x-generic-symbolic"))
        self.size_box.append(self.size_spin)

        self.width_dropdown = Gtk.DropDown.new_from_strings([name for name, _ in PEN_WIDTHS])
        self.width_dropdown.set_selected(1)
        self.width_dropdown.set_tooltip_text("Pen Width")
        self.width_dropdown.connect("notify::selected", self._on_width_set)

        bar = Gtk.ActionBar(revealed=False)
        for widget in (tools, self.color_button, self.size_box, self.width_dropdown):
            bar.pack_start(widget)
        save = Gtk.Button(label="_Save", use_underline=True, action_name="win.save")
        save.add_css_class("suggested-action")
        bar.pack_end(save)
        undo_redo = Gtk.Box()
        undo_redo.add_css_class("linked")
        undo_redo.append(Gtk.Button(icon_name="edit-undo-symbolic", action_name="win.undo", tooltip_text="Undo"))
        undo_redo.append(Gtk.Button(icon_name="edit-redo-symbolic", action_name="win.redo", tooltip_text="Redo"))
        bar.pack_end(undo_redo)
        return bar

    def _build_searchbar(self):
        self.search_entry = Gtk.SearchEntry(placeholder_text="Find in document", hexpand=True)
        self.search_entry.connect("search-changed", lambda *_: self._start_search())
        self.search_entry.connect("activate", lambda *_: self._find(True))
        self.search_entry.connect("next-match", lambda *_: self._find(True))
        self.search_entry.connect("previous-match", lambda *_: self._find(False))
        self.search_entry.connect("stop-search", lambda *_: self.searchbar.set_search_mode(False))
        keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_search_key)
        self.search_entry.add_controller(keys)
        nav = Gtk.Box()
        nav.add_css_class("linked")
        nav.append(Gtk.Button(icon_name="go-up-symbolic", action_name="win.find-previous",
                              tooltip_text="Previous Match"))
        nav.append(Gtk.Button(icon_name="go-down-symbolic", action_name="win.find-next",
                              tooltip_text="Next Match"))
        self.match_label = Gtk.Label(width_chars=11, xalign=0)
        self.match_label.add_css_class("dim-label")
        self.match_label.add_css_class("numeric")
        options = _menu((("_Match Case", "win.search-case"), ("_Whole Words Only", "win.search-whole-words")))
        box = Gtk.Box(spacing=6)
        box.append(self.search_entry)
        box.append(nav)
        box.append(self.match_label)
        box.append(Gtk.MenuButton(icon_name="emblem-system-symbolic", menu_model=options,
                                  tooltip_text="Search Options"))
        bar = Gtk.SearchBar(child=Adw.Clamp(maximum_size=640, child=box))
        bar.connect_entry(self.search_entry)
        bar.connect("notify::search-mode-enabled", self._on_search_mode)
        return bar

    def _add_actions(self):
        simple = {
            "open": self._on_open,
            "print": self._on_print,
            "properties": lambda: properties_dialog(self.document).present(self),
            "reload": lambda: self.reload(),
            "close": self.close,
            "zoom-in": lambda: self.view.zoom_in(),
            "zoom-out": lambda: self.view.zoom_out(),
            "zoom-reset": lambda: self.view.set_zoom(1.0),
            "rotate-left": lambda: self._rotate(-90),
            "rotate-right": lambda: self._rotate(90),
            "find": self._on_find,
            "find-next": lambda: self._find(True),
            "find-previous": lambda: self._find(False),
            "go-to-page": self.page_entry.grab_focus,
            "back": self.view.go_back,
            "forward": self.view.go_forward,
            "copy": self.view.copy_selection,
            "select-all": self.view.select_all,
            "save": lambda: self.save(),
            "save-as": self._on_save_as,
            "undo": lambda: self.session.annotations.undo(),
            "redo": lambda: self.session.annotations.redo(),
        }
        for name, callback in simple.items():
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda _action, _param, callback=callback: callback())
            self.add_action(action)
        zoom_set = Gio.SimpleAction.new("zoom-set", GLib.VariantType.new("d"))
        zoom_set.connect("activate", lambda _, value: self.view.set_zoom(value.get_double()))
        self.add_action(zoom_set)
        for name, target, prop in (("zoom-mode", self.view, "zoom-mode"), ("dual", self.view, "dual-page"),
                                   ("night", self.view, "night-mode"), ("sidebar", self.split, "show-sidebar"),
                                   ("fullscreen", self, "fullscreened")):
            self.add_action(Gio.PropertyAction.new(name, target, prop))
        annotate = Gio.SimpleAction.new_stateful("annotate", None, GLib.Variant.new_boolean(False))
        annotate.connect("change-state", self._on_annotate)
        self.add_action(annotate)
        for name in ("search-case", "search-whole-words"):
            action = Gio.SimpleAction.new_stateful(name, None, GLib.Variant.new_boolean(False))
            action.connect("change-state", self._on_search_option)
            self.add_action(action)
        self.view.connect("notify::night-mode", self._on_night_mode)
        self.view.props.night_mode = self.state.preference("night-mode", False)

    def _update_actions(self):
        has_document = self.session is not None
        for name in DOCUMENT_ACTIONS:
            self.lookup_action(name).set_enabled(has_document)
        self.lookup_action("copy").set_enabled(self.view.props.has_selection)
        self.lookup_action("back").set_enabled(self.view.props.can_go_back)
        self.lookup_action("forward").set_enabled(self.view.props.can_go_forward)
        editable = has_document and self.document.editable
        annotations = self.session.annotations if has_document else None
        self.lookup_action("annotate").set_enabled(editable)
        self.lookup_action("save").set_enabled(editable and annotations.modified)
        self.lookup_action("save-as").set_enabled(editable)
        self.lookup_action("undo").set_enabled(editable and annotations.can_undo)
        self.lookup_action("redo").set_enabled(editable and annotations.can_redo)
        if not editable and self.edit_bar.get_revealed():
            self.activate_action("win.annotate", GLib.Variant.new_boolean(False))
        for widget in self._document_widgets:
            widget.set_visible(has_document)

    # Opening documents

    def open_file(self, gfile):
        """Open a PDF in this window."""
        def open_it():
            self.save_position()
            self._load(gfile)

        self._confirm_unsaved(open_it)

    def reload(self, quiet=False):
        if self.document is not None:
            self._confirm_unsaved(lambda: self._load(self.document.file, self._password, reload=True, quiet=quiet))

    def _load(self, gfile, password=None, reload=False, quiet=False):
        self._load_generation += 1
        generation = self._load_generation
        if not reload:
            self.loading = True

            def show_spinner():
                if generation == self._load_generation and self.loading:
                    self.stack.set_visible_child_name("loading")
                return GLib.SOURCE_REMOVE

            GLib.timeout_add(250, show_spinner)

        def work():
            try:
                result = documents.load(gfile, password)
            except GLib.Error as error:
                result = error
            GLib.idle_add(self._on_loaded, generation, gfile, password, reload, quiet, result)

        threading.Thread(target=work, name="acrimonious-load", daemon=True).start()

    def _on_loaded(self, generation, gfile, password, reload, quiet, result):
        if generation != self._load_generation:
            return GLib.SOURCE_REMOVE
        self.loading = False
        if isinstance(result, documents.Document):
            self._show_document(result, password, reload)
        elif documents.needs_password(result):
            ask_password(self, gfile.get_basename(), password is not None,
                         lambda password: self._load(gfile, password, reload),
                         self._on_password_cancelled)
        elif reload:
            if not quiet:
                self.toast(f"Could not reload: {result.message}")
        elif self.document is not None:
            self.stack.set_visible_child_name("document")
            self.toast(f"Could not open “{gfile.get_basename()}”: {result.message}")
        else:
            self.error_page.set_title(f"Could Not Open “{gfile.get_basename()}”")
            self.error_page.set_description(result.message)
            self.stack.set_visible_child_name("error")
        return GLib.SOURCE_REMOVE

    def _on_password_cancelled(self):
        self.stack.set_visible_child_name("document" if self.document is not None else "welcome")

    def _show_document(self, document, password, reload):
        same_file = self.document is not None and self.document.file.equal(document.file)
        if same_file:
            position, view_state = self.view.get_position(), self.view.get_view_state()
        else:
            saved = self.state.document(document.file.get_uri()) or {}
            position, view_state = (saved.get("page", 0), saved.get("offset", 0.0)), saved
        old = self.session
        self.session = Session(document, previous=old if same_file else None)
        self.document = document
        self._password = password
        self.view.set_session(self.session, position, view_state)
        if old is not None:
            old.annotations.disconnect(self._annotations_handler)
            old.close()
        self._annotations_handler = self.session.annotations.connect("changed", self._on_annotations_changed)
        self.sidebar.set_session(self.session, keep_tab=same_file)
        self.sidebar.set_current_page(self.view.props.page)
        self._update_title()
        self.stack.set_visible_child_name("document")
        self._update_page_display()
        self._update_zoom_label()
        self._update_actions()
        if self.searchbar.get_search_mode() and self.search_entry.get_text():
            self._start_search()
        else:
            self.search.clear()
        if not same_file:
            self._saved_etag = None
            self._watch(document.file)
            Gtk.RecentManager.get_default().add_item(document.file.get_uri())
            self.view.grab_focus()

    def _watch(self, gfile):
        """Reload the document whenever the file changes, e.g. when LaTeX rebuilds it."""
        if self._monitor is not None:
            self._monitor.cancel()
            self._monitor = None
        try:
            self._monitor = gfile.monitor_file(Gio.FileMonitorFlags.WATCH_MOVES, None)
        except GLib.Error:
            return
        self._monitor.connect("changed", self._on_file_changed)

    def _on_file_changed(self, monitor, gfile, other, event):
        kinds = (Gio.FileMonitorEvent.CHANGED, Gio.FileMonitorEvent.CHANGES_DONE_HINT,
                 Gio.FileMonitorEvent.CREATED, Gio.FileMonitorEvent.RENAMED, Gio.FileMonitorEvent.MOVED_IN)
        if event not in kinds:
            return
        if self._reload_id:
            GLib.source_remove(self._reload_id)

        def reload():
            self._reload_id = 0
            if self.document is None or _etag(self.document.file) == self._saved_etag:
                return GLib.SOURCE_REMOVE  # our own save
            if self.session.annotations.modified:
                toast = Adw.Toast(title="The file changed on disk. Reloading discards your changes.",
                                  button_label="_Reload", timeout=0, use_markup=False)
                toast.connect("button-clicked", lambda _: self._load(
                    self.document.file, self._password, reload=True))
                self.toasts.add_toast(toast)
            else:
                self.reload(quiet=True)
            return GLib.SOURCE_REMOVE

        self._reload_id = GLib.timeout_add(RELOAD_DELAY_MS, reload)

    def _on_open(self):
        pdf = Gtk.FileFilter(name="PDF Documents")
        pdf.add_mime_type("application/pdf")
        pdf.add_suffix("pdf")
        everything = Gtk.FileFilter(name="All Files")
        everything.add_pattern("*")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(pdf)
        filters.append(everything)
        dialog = Gtk.FileDialog(title="Open Document", filters=filters, default_filter=pdf)
        if self.document is not None and self.document.file.get_parent() is not None:
            dialog.set_initial_folder(self.document.file.get_parent())
        dialog.open(self, None, self._on_open_chosen)

    def _on_open_chosen(self, dialog, result):
        try:
            gfile = dialog.open_finish(result)
        except GLib.Error as error:
            if not error.matches(Gtk.dialog_error_quark(), Gtk.DialogError.DISMISSED):
                self.toast(f"Could not choose a file: {error.message}")
            return
        self.open_file(gfile)

    def _on_drop(self, target, files, x, y):
        files = files.get_files()
        if not files:
            return False
        self.open_file(files[0])
        for gfile in files[1:]:
            self.get_application().open_in_window(gfile)
        return True

    def _on_open_remote(self, view, path):
        base = self.document.file.get_parent()
        gfile = base.resolve_relative_path(path) if base is not None else Gio.File.new_for_path(path)
        self.get_application().open_in_window(gfile)

    def _on_named_action(self, view, name):
        actions = {"Print": "print", "Find": "find", "Open": "open", "Close": "close",
                   "FullScreen": "fullscreen", "Quit": "close"}
        if name in actions:
            self.activate_action(f"win.{actions[name]}", None)

    def _on_print(self):
        self.view.commit_editing(refocus=False)
        print_document(self, self.document, self.session.annotations.items,
                       lambda message: self.toast(f"Printing failed: {message}"))

    # Writing and drawing

    def _on_annotate(self, action, value):
        action.set_state(value)
        on = value.get_boolean()
        self.edit_bar.set_revealed(on)
        if on and self.view.props.tool == "browse":
            self.view.props.tool = "text"
        elif not on:
            self.view.props.tool = "browse"

    def _on_annotations_changed(self, annotations):
        self._update_title()
        self._update_actions()
        self._sync_style_controls()

    def _update_title(self):
        name = self.document.display_name
        if self.session.annotations.modified:
            name = "• " + name
        self.title.set_title(name)
        self.title.set_subtitle(self.document.file.get_basename() if self.document.title else "")
        self.set_title(name)

    def _sync_style_controls(self):
        """Show the style of the chosen annotation, and the controls that fit the tool."""
        item, tool = self.view.chosen_item(), self.view.props.tool
        self._syncing_style = True
        if item is not None:
            self.color_button.set_rgba(_rgba(item.color))
            if item.kind == "text":
                self.size_spin.set_value(item.size)
            else:
                self.width_dropdown.set_selected(min(range(len(PEN_WIDTHS)),
                                                     key=lambda i: abs(PEN_WIDTHS[i][1] - item.width)))
        self._syncing_style = False
        kind = item.kind if item is not None else {"text": "text", "pen": "ink"}.get(tool)
        self.size_box.set_visible(kind != "ink")
        self.width_dropdown.set_visible(kind != "text")

    def _on_color_set(self, button, _):
        if not self._syncing_style:
            rgba = button.get_rgba()
            self.view.restyle(color=(round(rgba.red, 4), round(rgba.green, 4), round(rgba.blue, 4)))

    def _on_size_set(self, spin):
        if not self._syncing_style:
            self.view.restyle(size=spin.get_value())

    def _on_width_set(self, dropdown, _):
        if not self._syncing_style:
            self.view.restyle(width=PEN_WIDTHS[dropdown.get_selected()][1])

    def save(self, destination=None):
        """Write the annotations into the file, or into a copy at `destination`. True on success."""
        self.view.commit_editing(refocus=False)
        document, items = self.document, self.session.annotations.items
        target = destination or document.file
        try:
            in_place = pdfwrite.save(document.file.get_path(), items, target.get_path(), self._password)
        except Exception as error:
            self.toast(f"Could not save “{target.get_basename()}”: {error}")
            return False
        self.session.annotations.mark_saved(items)
        if target.equal(document.file):
            self._saved_etag = _etag(document.file)
            if in_place:  # Poppler was still reading the bytes that were just overwritten
                self._load(document.file, self._password, reload=True)
        else:  # continue in the copy, where the view left off
            page, offset = self.view.get_position()
            self.state.set_document(target.get_uri(), {"page": page, "offset": offset, **self.view.get_view_state()})
            self._load(target, self._password)
        return True

    def _on_save_as(self):
        self.view.commit_editing(refocus=False)
        stem = self.document.file.get_basename().removesuffix(".pdf")
        dialog = Gtk.FileDialog(title="Save As", initial_name=f"{stem} (filled).pdf")
        if self.document.file.get_parent() is not None:
            dialog.set_initial_folder(self.document.file.get_parent())
        dialog.save(self, None, self._on_save_as_chosen)

    def _on_save_as_chosen(self, dialog, result):
        try:
            gfile = dialog.save_finish(result)
        except GLib.Error as error:
            if not error.matches(Gtk.dialog_error_quark(), Gtk.DialogError.DISMISSED):
                self.toast(f"Could not choose a file: {error.message}")
            return
        if gfile.get_path() is None:
            self.toast("Only files in local folders can be saved")
            return
        self.save(gfile)

    def _confirm_unsaved(self, then):
        """Run then() once unsaved annotations are saved, or the user chose to discard them."""
        self.view.commit_editing(refocus=False)
        if self.session is None or not self.session.annotations.modified:
            then()
            return
        dialog = Adw.AlertDialog(heading="Save Changes?",
                                 body=f"“{self.document.file.get_basename()}” has changes that are not saved.")
        dialog.add_response("cancel", "_Cancel")
        dialog.add_response("discard", "_Discard")
        dialog.add_response("save", "_Save")
        dialog.set_response_appearance("discard", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("save")
        dialog.set_close_response("cancel")

        def respond(dialog, response):
            if response == "discard" or (response == "save" and self.save()):
                then()

        dialog.connect("response", respond)
        dialog.present(self)

    # Pages and zoom

    def _on_page_changed(self, *_):
        self._update_page_display()
        self.sidebar.set_current_page(self.view.props.page)
        self._schedule_save()

    def _update_page_display(self):
        if self.document is None:
            return
        page, count = self.view.props.page, self.document.n_pages
        focus = self.get_focus()
        if focus is None or not focus.is_ancestor(self.page_entry):
            self.page_entry.set_text(self.document.page_labels[page])
            self.page_entry.remove_css_class("error")
        self.page_count.set_label(f"({page + 1} of {count})" if self.document.has_page_labels else f"of {count}")

    def _on_page_entry_activate(self, entry):
        text, doc = entry.get_text().strip(), self.document
        if text in doc.page_labels:
            page = doc.page_labels.index(text)
        elif text.isdigit() and 1 <= int(text) <= doc.n_pages:
            page = int(text) - 1
        else:
            entry.add_css_class("error")
            return
        entry.remove_css_class("error")
        self.view.go_to(page)
        self.view.grab_focus()

    def _update_zoom_label(self):
        self.zoom_label.set_label(f"{round(self.view.props.zoom * 100)}%")

    def _rotate(self, delta):
        self.view.props.rotation = self.view.props.rotation + delta

    def _on_sidebar_target(self, sidebar, target):
        self.view.go_to_target(target)
        if self.split.get_collapsed():
            self.split.set_show_sidebar(False)

    def _on_night_mode(self, view, _):
        night = view.props.night_mode
        self.state.set_preference("night-mode", night)
        if night:
            self.sidebar.add_css_class("night")
        else:
            self.sidebar.remove_css_class("night")

    def _on_collapsed(self, split, _):
        # Pinned, so libadwaita leaves show-sidebar alone: hide it over narrow windows and bring
        # back the user's choice when there is room again.
        collapsed = split.get_collapsed()
        split.set_show_sidebar(False if collapsed else self.state.preference("sidebar", False))

    def _on_sidebar_toggled(self, split, _):
        if not split.get_collapsed():
            self.state.set_preference("sidebar", split.get_show_sidebar())

    # Search

    def _on_find(self):
        self.searchbar.set_search_mode(True)
        self.search_entry.grab_focus()
        self.search_entry.select_region(0, -1)

    def _search_options(self):
        return (self.lookup_action("search-case").get_state().get_boolean(),
                self.lookup_action("search-whole-words").get_state().get_boolean())

    def _start_search(self):
        if self.session is None:
            return
        case_sensitive, whole_words = self._search_options()
        self.search.start(self.session, self.search_entry.get_text(), self.view.props.page,
                          case_sensitive, whole_words)

    def _find(self, forward):
        if self.session is None:
            return
        if not self.searchbar.get_search_mode():
            self._on_find()
            return
        if self.search.query != self.search_entry.get_text():
            self._start_search()
        else:
            self.search.step(forward, self.view.props.page)

    def _on_search_option(self, action, value):
        action.set_state(value)
        if self.search_entry.get_text():
            self._start_search()

    def _on_search_key(self, controller, keyval, keycode, state):
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and state & Gdk.ModifierType.SHIFT_MASK:
            self._find(False)
            return True
        return False

    def _on_search_mode(self, searchbar, _):
        if searchbar.get_search_mode():
            if self.search_entry.get_text() and not self.search.query:
                self._start_search()
        else:
            self.search.clear()
            self.view.grab_focus()

    def _on_search_changed(self, search):
        if not search.query:
            text = ""
        elif search.total == 0:
            text = "Searching…" if search.running else "No matches"
        elif search.current is None:
            text = f"{search.total} matches"
        else:
            text = f"{search.position} of {search.total}{'+' if search.running else ''}"
        self.match_label.set_label(text)
        self.view.queue_draw()

    # Fullscreen

    def _on_fullscreen_changed(self, *_):
        fullscreen = self.is_fullscreen()
        self.toolbar.set_extend_content_to_top_edge(fullscreen)
        self.toolbar.set_reveal_top_bars(not fullscreen)
        # Over the page, the bars need their own background.
        self.toolbar.set_top_bar_style(Adw.ToolbarStyle.RAISED if fullscreen else Adw.ToolbarStyle.FLAT)

    def _on_pointer_motion(self, controller, x, y):
        # In fullscreen the header bar appears when the pointer touches the top edge.
        if not self.is_fullscreen():
            return
        if y < 6:
            self.toolbar.set_reveal_top_bars(True)
        elif y > 120 and not self.searchbar.get_search_mode():
            self.toolbar.set_reveal_top_bars(False)

    def _on_key_pressed(self, controller, keyval, keycode, state):
        if keyval == Gdk.KEY_Escape and self.is_fullscreen():
            self.unfullscreen()
            return True
        return False

    # Remembering state

    def _schedule_save(self):
        if self.document is not None and not self._save_id:
            self._save_id = GLib.timeout_add(1000, self.save_position)

    def save_position(self):
        if self._save_id:
            GLib.source_remove(self._save_id)
            self._save_id = 0
        if self.document is not None:
            page, offset = self.view.get_position()
            state = {"page": page, "offset": round(offset, 4), **self.view.get_view_state()}
            self.state.set_document(self.document.file.get_uri(), state)
        return GLib.SOURCE_REMOVE

    def _on_close_request(self, window):
        if not self._close_confirmed and self.session is not None:
            self.view.commit_editing(refocus=False)
            if self.session.annotations.modified:
                def close():
                    self._close_confirmed = True
                    self.close()

                self._confirm_unsaved(close)
                return True
        self.save_position()
        if not self.is_maximized() and not self.is_fullscreen():
            self.state.set_preference("window-size", list(self.get_default_size()))
        self.state.set_preference("window-maximized", self.is_maximized())
        Gtk.RecentManager.get_default().disconnect(self._recent_handler)
        if self._monitor is not None:
            self._monitor.cancel()
        if self._reload_id:
            GLib.source_remove(self._reload_id)
        if self.session is not None:
            self.session.close()
        return False

    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=text, use_markup=False))


def _rgba(rgb):
    color = Gdk.RGBA()
    color.red, color.green, color.blue = rgb
    color.alpha = 1
    return color


def _etag(gfile):
    try:
        return gfile.query_info("etag::value", Gio.FileQueryInfoFlags.NONE, None).get_etag()
    except GLib.Error:
        return None
