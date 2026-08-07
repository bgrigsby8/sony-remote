"""
Print the numeric value of CrDevicePropertyCode enumerators, parsed from the
real SDK header the way a C compiler would (sequential values, explicit bases,
aliases don't advance).

    export CRSDK_ROOT=/path/to/sdk
    python3 native/enumcodes.py FocusPositionSetting NearFar ...

Names may be given with or without the CrDeviceProperty_ prefix. With no names,
prints the whole table. Pairs with the {"dump_properties": {}} DoCommand: this
maps names to codes, the dump shows which codes the body actually reports.
"""

import os
import re
import sys
from pathlib import Path

root = os.environ.get("CRSDK_ROOT")
if not root:
    sys.exit("CRSDK_ROOT is not set (the extracted SDK directory)")

candidates = [
    Path(root) / "app" / "CRSDK",
    Path(root) / "CRSDK",
    Path(root) / "include" / "CRSDK",
    Path(root),
]
header = next(
    (p / "CrDeviceProperty.h" for p in candidates if (p / "CrDeviceProperty.h").exists()),
    None,
)
if header is None:
    sys.exit(f"CrDeviceProperty.h not found under {root}")

vals: dict = {}
cur, inside = -1, False
for raw in header.read_text(errors="replace").splitlines():
    s = raw.split("//")[0].split("/*")[0].strip()
    if not inside:
        inside = "enum CrDevicePropertyCode" in s
        continue
    if s.startswith("};"):
        break
    m = re.match(r"(CrDeviceProperty_\w+)\s*(?:=\s*(.+?))?\s*,?$", s)
    if not m:
        continue
    name, val = m.groups()
    if val:
        val = val.strip().rstrip(",")
        try:
            cur = int(val, 0)
        except ValueError:
            cur = vals.get(val, cur)  # alias of an earlier enumerator
    else:
        cur += 1
    vals[name] = cur

wanted = sys.argv[1:] or sorted(vals, key=vals.get)
for w in wanted:
    name = w if w.startswith("CrDeviceProperty_") else f"CrDeviceProperty_{w}"
    print(f"{name} = {vals[name]:#x}" if name in vals else f"{name} MISSING")
