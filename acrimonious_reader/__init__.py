"""Acrimonious Reader, a PDF viewer for GNOME."""

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Poppler", "0.18")
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")

APP_ID = "io.github.boergens.AcrimoniousReader"
APP_NAME = "Acrimonious Reader"
VERSION = "1.0.0"
WEBSITE = "https://github.com/boergens/acrimonious-reader"
