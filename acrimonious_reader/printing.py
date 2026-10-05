"""Printing through the GTK print dialog."""

import math

from gi.repository import GLib, Gtk

_settings = None  # print settings carry over between print jobs within a session


def print_document(window, document, annotations, on_error):
    op = Gtk.PrintOperation(job_name=document.display_name, n_pages=document.n_pages,
                            unit=Gtk.Unit.POINTS, embed_page_setup=True, allow_async=True)
    if _settings is not None:
        op.set_print_settings(_settings)
    op.connect("draw-page", _draw_page, document, annotations)
    op.connect("done", _done, on_error)
    try:
        op.run(Gtk.PrintOperationAction.PRINT_DIALOG, window)
    except GLib.Error as error:
        on_error(error.message)


def _draw_page(op, context, index, document, annotations=()):
    cr = context.get_cairo_context()
    width, height = document.page_sizes[index]
    paper_w, paper_h = context.get_width(), context.get_height()
    turn = (width > height) != (paper_w > paper_h)  # print landscape pages sideways on portrait paper
    if turn:
        width, height = height, width
    scale = min(paper_w / width, paper_h / height)
    cr.translate((paper_w - width * scale) / 2, (paper_h - height * scale) / 2)
    cr.scale(scale, scale)
    if turn:
        cr.translate(width, 0)
        cr.rotate(math.pi / 2)
    document.render_for_printing(index, cr)
    for item in annotations:
        if item.page == index:
            item.draw(cr)


def _done(op, result, on_error):
    global _settings
    if result == Gtk.PrintOperationResult.APPLY:
        _settings = op.get_print_settings()
    elif result == Gtk.PrintOperationResult.ERROR:
        try:
            op.get_error()
        except GLib.Error as error:
            on_error(error.message)
