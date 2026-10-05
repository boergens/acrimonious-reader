#!/usr/bin/env python3
"""Write the apt source file and the repository's landing page from the public signing key.

    apt-repo/make-static.py

The key goes into the source file itself (deb822 allows that), so installing needs no curl or wget
and the key cannot be swapped on the way.
"""

import html
from pathlib import Path

HERE = Path(__file__).resolve().parent
URL = "https://acrimonious-reader.pages.dev"
FINGERPRINT = "7C68 A63C 0048 E9DE E29E  0477 07E0 DF27 E6BB AC17"


def sources():
    key = (HERE / "static" / "acrimonious-reader.asc").read_text().rstrip("\n").split("\n")
    signed_by = "\n".join(" " + (line if line else ".") for line in key)
    return f"Types: deb\nURIs: {URL}\nSuites: trixie\nComponents: main\nSigned-By:\n{signed_by}\n"


def install_commands():
    return ("sudo tee /etc/apt/sources.list.d/acrimonious-reader.sources > /dev/null <<'END'\n"
            f"{sources()}END\n"
            "sudo apt update\n"
            "sudo apt install acrimonious-reader")


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Acrimonious Reader apt repository</title>
<style>
  :root {{ color-scheme: light dark; --bg: #fafafa; --fg: #222; --dim: #666; --code: #ececec; --accent: #1c71d8; }}
  @media (prefers-color-scheme: dark) {{ :root {{ --bg: #1e1e1e; --fg: #eee; --dim: #aaa; --code: #2c2c2c; --accent: #78aeed; }} }}
  body {{ margin: 0; background: var(--bg); color: var(--fg); font: 16px/1.55 system-ui, sans-serif; }}
  main {{ max-width: 46rem; margin: 0 auto; padding: 2.5rem 16px 4rem; }}
  h1 {{ font-size: 1.7rem; margin: 0 0 .3rem; }}
  p.lead {{ color: var(--dim); margin-top: 0; }}
  pre {{ background: var(--code); padding: 1rem; border-radius: 8px; overflow-x: auto; font-size: .85rem; line-height: 1.4; }}
  code {{ font-family: ui-monospace, monospace; }}
  a {{ color: var(--accent); }}
</style>
</head>
<body>
<main>
<h1>Acrimonious Reader</h1>
<p class="lead">apt repository for Debian 13 (trixie): a PDF viewer for GNOME that can fill in and sign forms.
Source and releases: <a href="https://github.com/boergens/acrimonious-reader">github.com/boergens/acrimonious-reader</a>.</p>
<h2>Install</h2>
<p>Paste this into a terminal. It adds the repository, with its signing key built in, and installs the
app. New versions then arrive with your normal updates.</p>
<pre><code>{commands}</code></pre>
<p>Signing key fingerprint: <code>{fingerprint}</code> (<a href="acrimonious-reader.asc">acrimonious-reader.asc</a>).</p>
<h2>Remove</h2>
<pre><code>sudo apt remove acrimonious-reader
sudo rm /etc/apt/sources.list.d/acrimonious-reader.sources</code></pre>
</main>
</body>
</html>
"""

if __name__ == "__main__":
    (HERE / "acrimonious-reader.sources").write_text(sources())
    (HERE / "static" / "index.html").write_text(
        PAGE.format(commands=html.escape(install_commands()), fingerprint=FINGERPRINT))
