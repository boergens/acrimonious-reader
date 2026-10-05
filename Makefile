# make install                       install to ~/.local
# make install PREFIX=/usr DESTDIR=… what the Debian package does
PREFIX ?= $(HOME)/.local
APP_ID = io.github.boergens.AcrimoniousReader
NAME = acrimonious-reader
DATADIR = $(PREFIX)/share
# The program's own files. Not $(DATADIR)/$(NAME): with PREFIX=~/.local that is the user's data
# folder (~/.local/share/acrimonious-reader, where saved signatures live), which install and
# uninstall must never touch.
PKGDIR = $(PREFIX)/lib/$(NAME)
DEST = $(DESTDIR)$(PREFIX)
DESTDATA = $(DESTDIR)$(DATADIR)

.PHONY: all run test install uninstall deb

all:

run:
	./bin/$(NAME)

test:
	python3 -m pytest -q tests

install:
	rm -rf $(DESTDIR)$(PKGDIR)
	install -d $(DESTDIR)$(PKGDIR)/acrimonious_reader $(DEST)/bin
	install -m644 acrimonious_reader/*.py acrimonious_reader/style.css $(DESTDIR)$(PKGDIR)/acrimonious_reader/
	printf '#!/bin/sh\nPYTHONPATH="%s$${PYTHONPATH:+:$$PYTHONPATH}" exec python3 -m acrimonious_reader "$$@"\n' "$(PKGDIR)" > $(DEST)/bin/$(NAME)
	chmod 755 $(DEST)/bin/$(NAME)
	install -Dm644 data/$(NAME).1 $(DESTDATA)/man/man1/$(NAME).1
	install -Dm644 data/$(APP_ID).desktop $(DESTDATA)/applications/$(APP_ID).desktop
	install -Dm644 data/$(APP_ID).metainfo.xml $(DESTDATA)/metainfo/$(APP_ID).metainfo.xml
	install -Dm644 data/icons/hicolor/scalable/apps/$(APP_ID).svg $(DESTDATA)/icons/hicolor/scalable/apps/$(APP_ID).svg
	install -Dm644 -t $(DESTDATA)/icons/hicolor/symbolic/apps data/icons/hicolor/symbolic/apps/$(NAME)-*-symbolic.svg
	if [ -z "$(DESTDIR)" ] && command -v update-desktop-database >/dev/null; then update-desktop-database -q $(DATADIR)/applications; fi
	if [ -z "$(DESTDIR)" ] && command -v gtk4-update-icon-cache >/dev/null; then gtk4-update-icon-cache -q -t -f $(DATADIR)/icons/hicolor; fi

uninstall:
	rm -rf $(PKGDIR) $(PREFIX)/bin/$(NAME) $(DATADIR)/man/man1/$(NAME).1 $(DATADIR)/applications/$(APP_ID).desktop \
		$(DATADIR)/metainfo/$(APP_ID).metainfo.xml $(DATADIR)/icons/hicolor/scalable/apps/$(APP_ID).svg \
		$(DATADIR)/icons/hicolor/symbolic/apps/$(NAME)-*-symbolic.svg

deb:
	dpkg-buildpackage -us -uc -b
