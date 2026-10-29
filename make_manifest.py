"""make_manifest.py - writes tools.json, the list of Hegd's tools (run it before every commit that changes a tool).

    python3 make_manifest.py

A tool = a name, a version and the files that tul puts next to hpip.py on the phone.
Edit TOOLS below to add a tool. "version": "auto" reads  VERSION = "..."  from the first file of the tool.
"""
import hashlib
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))

TOOLS = {
    "hpip": {
        "description": "pip for Hegd: pure-Python libraries from PyPI, with a registry, freeze and verify",
        "version": "auto",
        "files": ["hpiptool/hpip.py"],
    },
    "network": {
        "description": "share a folder in a browser on the same Wi-Fi (sv.py)",
        "version": "1.0",
        "files": ["network/hegd_sv.py", "network/sv.py"],
    },
}


def main():
    tools = {}
    for name, t in TOOLS.items():
        files = []
        for rel in t["files"]:
            path = os.path.join(ROOT, *rel.split("/"))
            if not os.path.isfile(path):
                sys.exit("missing file: %s" % rel)
            data = open(path, "rb").read()
            if rel.endswith(".py"):
                compile(data, rel, "exec")                       # a file that does not compile never gets into the list
            files.append({"path": rel, "name": os.path.basename(rel), "sha256": hashlib.sha256(data).hexdigest()})
        version = t["version"]
        if version == "auto":
            text = open(os.path.join(ROOT, *t["files"][0].split("/")), encoding="utf-8").read()
            m = re.search(r'^VERSION\s*=\s*"([^"]+)"', text, re.M)
            if not m:
                sys.exit("%s: no VERSION = \"...\" line in %s" % (name, t["files"][0]))
            version = m.group(1)
        tools[name] = {"version": version, "description": t["description"], "files": files}
    with open(os.path.join(ROOT, "tools.json"), "w", encoding="utf-8") as f:
        json.dump({"format": 1, "tools": tools}, f, indent=1, ensure_ascii=False)
        f.write("\n")
    for name, t in tools.items():
        print("%-8s %-8s %s" % (name, t["version"], ", ".join(x["name"] for x in t["files"])))
    print("tools.json written. Now: git add -A && git commit && git push")


main()
