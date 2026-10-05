#!/bin/sh
# Add a package to the apt repository and upload it to https://acrimonious-reader.pages.dev
#
#   apt-repo/publish.sh path/to/acrimonious-reader_<version>_all.deb
#
# Needs reprepro, the signing key in ~/.config/acrimonious-reader-apt/gnupg and a wrangler login.
set -eu
here=$(dirname "$(readlink -f "$0")")
export GNUPGHOME="$HOME/.config/acrimonious-reader-apt/gnupg"
reprepro -b "$here" includedeb trixie "$1"
cp -r "$here/static/." "$here/site/"
wrangler pages deploy "$here/site" --project-name acrimonious-reader --branch main --commit-dirty=true
