#!/usr/bin/env python3
"""Compiles the Tailwind classes the dashboard uses into dashboard.html, between the
two markers, so the page needs no script from another site and no compiling in the
browser. Run it after adding or changing a class:

    python server/tools/build_css.py

It needs Node (npx) and the internet the first time. Same Tailwind version the page
used to load from the CDN, so the result looks the same.
"""
import pathlib
import subprocess
import sys
import tempfile

VERSION = "3.4.14"
HTML = pathlib.Path(__file__).resolve().parents[1] / "public" / "dashboard.html"
START, END = "/* tailwind:start */", "/* tailwind:end */"

page = HTML.read_text(encoding="utf-8")
a, b = page.find(START), page.find(END)
if a < 0 or b < a:
    sys.exit("The tailwind markers are missing from dashboard.html")

# Scan the page without the old result, so a build does not feed on the last one.
source = page[: a + len(START)] + page[b:]

with tempfile.TemporaryDirectory() as tmp:
    d = pathlib.Path(tmp)
    (d / "page.html").write_text(source, encoding="utf-8")
    (d / "in.css").write_text("@tailwind base;\n@tailwind components;\n@tailwind utilities;\n", encoding="utf-8")
    (d / "tailwind.config.js").write_text("module.exports = { content: ['./page.html'] };\n", encoding="utf-8")
    subprocess.run(
        f"npx --yes tailwindcss@{VERSION} -i ./in.css -o ./out.css --minify",
        cwd=tmp, shell=True, check=True, timeout=300, stdin=subprocess.DEVNULL,
    )
    css = (d / "out.css").read_text(encoding="utf-8").strip()

if "</style" in css:
    sys.exit("Compiled CSS would break out of its style tag")
HTML.write_text(page[: a + len(START)] + css + page[b:], encoding="utf-8", newline="\n")
print(f"{len(css)} bytes of CSS written into {HTML.name}")
