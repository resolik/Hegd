"""sv.py - share a folder in a browser (the script version of +sv).

Usage in Hegd:   py sv.py [folder] [minutes] [up] [local]
  folder   what to share (default: the current folder)
  minutes  how long (default 15, at most 120)
  up       also allow uploads from the browser
  local    only this phone can open it
Stop it with: cancel
It needs hegd_sv.py in the same folder.
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import hegd_sv

STORAGE = "/storage/emulated/0"


def main():
    folder, minutes, up, local = None, hegd_sv.DEFAULT_MINUTES, False, False
    for a in sys.argv[1:]:
        low = a.lower()
        if low == "up":
            up = True
        elif low == "local":
            local = True
        elif a.isdigit():
            minutes = int(a)
        elif folder is None:
            folder = a
        else:
            print("[ERROR] Only one folder can be shared.")
            return
    root = os.path.realpath(folder or os.getcwd())
    # as a plain script it has no Hegd protection, so keep two safety belts of its own
    if root == STORAGE or (root + os.sep).startswith(os.path.join(STORAGE, "Android") + os.sep):
        print("[ERROR] Choose a folder inside the storage, not the whole storage or Android/.")
        return
    srv = hegd_sv.SvServer(root, upload=up, local_only=local, minutes=minutes, log=lambda t: print("[sv] " + t))
    try:
        urls = srv.start()
    except hegd_sv.SvError as e:
        print("[ERROR] Cannot start: %s" % e)
        return
    print("[SUCCESS] Sharing '%s' for %d min%s%s." % (os.path.basename(root) or root, srv.minutes,
                                                     ", uploads allowed" if up else ", read-only",
                                                     ", this phone only" if local else ""))
    for u in urls:
        print(u)
    print("[INFO] Open it in a browser on the same Wi-Fi. Traffic is not encrypted. Stop: cancel")
    try:
        while srv.running():
            time.sleep(0.5)
    finally:
        srv.stop()
        st = srv.status()
        print("[INFO] Stopped. %d requests, %d bytes sent, %d uploads." % (st["requests"], st["sent"], st["uploads"]))


main()
