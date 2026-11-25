"""Print the CHANGELOG.md section for a version (used for GitHub Release notes)."""
import re
import sys

ver = sys.argv[1]
text = open("CHANGELOG.md", encoding="utf-8").read()
m = re.search(rf"^## {re.escape(ver)}\s*\n(.*?)(?=^## |\Z)", text, re.S | re.M)
body = m.group(1).strip() if m else f"rtlnc {ver}"
print(body)
print()
print("**Install:** download `rtlnc-setup-%s.exe` below. Your browser and Windows may warn"
      " because the program isn't code-signed: in the browser's downloads list (Ctrl+J)"
      " choose *Keep*, then in the blue Windows box choose *More info → Run anyway*."
      " See the README for the one-time Zadig driver step." % ver)
