"""The sidebar: page thumbnails and the document outline."""

from gi.repository import Adw, Gio, GLib, GObject, Gtk, Pango

from .document import Target

THUMBNAIL_WIDTH = 120  # logical pixels


class _OutlineItem(GObject.Object):
    def __init__(self, entry):
        super().__init__()
        self.entry = entry


class Sidebar(Adw.Bin):
    __gtype_name__ = "AcrimoniousSidebar"
    __gsignals__ = {
        # The user picked a page or an outline entry (a document.Target).
        "activate-target": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
    }

    def __init__(self):
        super().__init__()
        self.session = None
        self._bound = {}  # page -> Gtk.Picture of the thumbnail shown for it
        self._thumbnail_handler = 0

        self._pages = Gtk.StringList()
        self._page_selection = Gtk.SingleSelection(model=self._pages, autoselect=False, can_unselect=True)
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._setup_thumbnail)
        factory.connect("bind", self._bind_thumbnail)
        factory.connect("unbind", self._unbind_thumbnail)
        self._thumbnails = Gtk.ListView(model=self._page_selection, factory=factory,
                                        single_click_activate=True)
        self._thumbnails.add_css_class("thumbnails")
        self._thumbnails.connect("activate", self._on_thumbnail_activated)
        self._thumbnails.connect("map", lambda _: GLib.idle_add(self._scroll_to_current))

        self._outline_root = Gio.ListStore(item_type=_OutlineItem)
        self._outline = Gtk.TreeListModel.new(self._outline_root, False, False, self._outline_children)
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._setup_outline_row)
        factory.connect("bind", self._bind_outline_row)
        outline_list = Gtk.ListView(model=Gtk.NoSelection(model=self._outline), factory=factory,
                                    single_click_activate=True)
        outline_list.add_css_class("navigation-sidebar")
        outline_list.connect("activate", self._on_outline_activated)

        self._outline_stack = Gtk.Stack()
        self._outline_stack.add_named(Gtk.ScrolledWindow(child=outline_list, hscrollbar_policy=Gtk.PolicyType.NEVER), "list")
        empty = Adw.StatusPage(icon_name="view-list-bullet-symbolic", title="No Outline",
                               description="This document has no table of contents")
        empty.add_css_class("compact")
        self._outline_stack.add_named(empty, "empty")

        self.stack = Adw.ViewStack()
        self.stack.add_titled(Gtk.ScrolledWindow(child=self._thumbnails, hscrollbar_policy=Gtk.PolicyType.NEVER),
                              "pages", "Pages")
        self.stack.add_titled(self._outline_stack, "outline", "Outline")
        switcher = Adw.InlineViewSwitcher(stack=self.stack, hexpand=True)
        switcher.add_css_class("flat")
        header = Adw.HeaderBar(title_widget=switcher, show_title=True)
        toolbar = Adw.ToolbarView(content=self.stack)
        toolbar.add_top_bar(header)
        self.set_child(toolbar)

    def set_session(self, session, keep_tab=False):
        if self.session is not None and self._thumbnail_handler:
            self.session.disconnect(self._thumbnail_handler)
        self.session = session
        self._bound.clear()
        self._thumbnail_handler = session.connect("thumbnail-ready", self._on_thumbnail_ready)
        doc = session.document
        self._pages.splice(0, self._pages.get_n_items(), doc.page_labels)
        self._outline_root.remove_all()
        for entry in doc.outline:
            self._outline_root.append(_OutlineItem(entry))
        self._expand_open_entries()
        self._outline_stack.set_visible_child_name("list" if doc.outline else "empty")
        if not keep_tab:
            self.stack.set_visible_child_name("outline" if doc.outline else "pages")

    def set_current_page(self, page):
        if page >= self._pages.get_n_items():
            return  # the view can report a page before set_session() has filled the list
        if self._page_selection.get_selected() != page:
            self._page_selection.set_selected(page)
            if self._thumbnails.get_mapped():
                self._thumbnails.scroll_to(page, Gtk.ListScrollFlags.NONE, None)

    def _scroll_to_current(self):
        page = self._page_selection.get_selected()
        if page != Gtk.INVALID_LIST_POSITION:
            self._thumbnails.scroll_to(page, Gtk.ListScrollFlags.NONE, None)
        return GLib.SOURCE_REMOVE

    # Thumbnails

    def _thumbnail_size(self, page):
        width, height = self.session.document.page_sizes[page]
        return THUMBNAIL_WIDTH, max(1, round(THUMBNAIL_WIDTH * height / width))

    def _setup_thumbnail(self, factory, item):
        picture = Gtk.Picture(content_fit=Gtk.ContentFit.FILL, can_shrink=True)
        picture.add_css_class("thumbnail")
        label = Gtk.Label()
        label.add_css_class("caption")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, halign=Gtk.Align.CENTER,
                      margin_top=6, margin_bottom=6)
        box.append(picture)
        box.append(label)
        item.set_child(box)

    def _bind_thumbnail(self, factory, item):
        page = item.get_position()
        box = item.get_child()
        picture, label = box.get_first_child(), box.get_last_child()
        picture.set_size_request(*self._thumbnail_size(page))
        label.set_label(self.session.document.page_labels[page])
        texture = self.session.thumbnail(page, self._device_width())
        picture.set_paintable(texture or self.session.any_thumbnail(page))
        item.set_accessible_label(f"Page {self.session.document.page_labels[page]}")
        self._bound[page] = picture

    def _unbind_thumbnail(self, factory, item):
        page = item.get_position()
        if self._bound.get(page) is item.get_child().get_first_child():
            del self._bound[page]

    def _device_width(self):
        return THUMBNAIL_WIDTH * self.get_scale_factor()

    def _on_thumbnail_ready(self, session, page):
        picture = self._bound.get(page)
        if picture is not None:
            picture.set_paintable(session.thumbnail(page, self._device_width()))

    def _on_thumbnail_activated(self, list_view, position):
        self.emit("activate-target", Target("page", page=position))

    # Outline

    @staticmethod
    def _outline_children(item):
        if not item.entry.children:
            return None
        store = Gio.ListStore(item_type=_OutlineItem)
        for entry in item.entry.children:
            store.append(_OutlineItem(entry))
        return store

    def _expand_open_entries(self):
        # Expand entries the document marks as open; rows appear as their parents expand.
        i = 0
        while i < self._outline.get_n_items():
            row = self._outline.get_row(i)
            if row.get_item().entry.expanded:
                row.set_expanded(True)
            i += 1

    def _setup_outline_row(self, factory, item):
        title = Gtk.Label(xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END)
        page = Gtk.Label(xalign=1)
        page.add_css_class("dim-label")
        page.add_css_class("numeric")
        box = Gtk.Box(spacing=12)
        box.append(title)
        box.append(page)
        item.set_child(Gtk.TreeExpander(child=box))

    def _bind_outline_row(self, factory, item):
        row = item.get_item()
        expander = item.get_child()
        expander.set_list_row(row)
        entry = row.get_item().entry
        box = expander.get_child()
        title, page = box.get_first_child(), box.get_last_child()
        title.set_label(entry.title)
        box.set_tooltip_text(entry.title)
        target = entry.target
        page.set_label(self.session.document.page_labels[target.page]
                       if target is not None and target.kind == "page" else "")

    def _on_outline_activated(self, list_view, position):
        entry = self._outline.get_row(position).get_item().entry
        if entry.target is not None:
            self.emit("activate-target", entry.target)
