"""Write the CHANGELOG.md section for a version to a file (GitHub Release notes).

    python packaging/release_notes.py 0.3.1 release-notes.md

Writes UTF-8 directly: printing to a Windows console (cp1252) would fail on
characters like the arrow.
"""
import re
import sys

ver, out = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else None)
text = open("CHANGELOG.md", encoding="utf-8").read()
m = re.search(rf"^## {re.escape(ver)}\s*\n(.*?)(?=^## |\Z)", text, re.S | re.M)
body = (m.group(1).strip() if m else f"rtlnc {ver}") + "\n\n" + (
    f"**Install:** download `rtlnc-setup-{ver}.exe` below. Your browser and Windows may warn "
    "because the program isn't code-signed: in the browser's downloads list (Ctrl+J) choose "
    "*Keep*, then in the blue Windows box choose *More info → Run anyway*. See the README "
    "for the one-time Zadig driver step.\n")
if out:
    with open(out, "w", encoding="utf-8") as f:
        f.write(body)
else:
    sys.stdout.buffer.write(body.encode("utf-8"))
