# The apt repository

Served by Cloudflare Pages (project `acrimonious-reader`) at https://acrimonious-reader.pages.dev,
with suite `trixie`, component `main`, and packages for every architecture (the package is
`Architecture: all`). Built with reprepro.

| Path | What it is |
| --- | --- |
| `conf/` | reprepro's configuration: the distribution, and the output folder `site/` |
| `static/` | Copied into the site on every publish: the landing page, the public key, `_headers` |
| `make-static.py` | Regenerates `static/index.html` and `acrimonious-reader.sources` from the public key |
| `acrimonious-reader.sources` | The apt source file users install, with the signing key built in |
| `publish.sh` | Adds a `.deb` and uploads the site |
| `db/`, `site/` | reprepro's database and output (not in git; keep them, or re-add every package) |

## Releasing a version

```sh
dpkg-buildpackage -us -uc -b                                     # from a clean checkout
apt-repo/publish.sh ../acrimonious-reader_<version>_all.deb
```

reprepro keeps one version per package: publishing a new one replaces the old one in the index.

## The signing key

Ed25519, fingerprint `7C68 A63C 0048 E9DE E29E  0477 07E0 DF27 E6BB AC17`, no passphrase and no
expiry. The private key is only on the machine that publishes, in
`~/.config/acrimonious-reader-apt/gnupg` (mode 700), with a revocation certificate in its
`openpgp-revocs.d/`. If the key is ever replaced, every user has to install the new
`acrimonious-reader.sources`.
