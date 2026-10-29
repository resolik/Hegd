[app]
p4a.branch = v2024.01.21

# ---- the name ----
title = Hegd
package.name = hegd
package.domain = org.hegd
version = 0.1

# ---- the files: main.py and kiwi_core.py are in the same folder as this spec ----
source.dir = .
source.include_exts = py,png
source.exclude_patterns = tul.py,make_manifest.py
source.exclude_dirs = bin, .buildozer, __pycache__, icon, icon_hegd, hpiptool, network, tools_backup, site-packages, .git

# ---- what the app needs ----
# openssl + certifi: without them https downloads (+gf) do not work inside the APK
# pyjnius: lets the app open the "all files access" screen on Android 11+
requirements = python3,kivy,pyjnius,openssl,certifi

orientation = portrait
fullscreen = 0

# ---- the icon (icon.png lies next to this spec) ----
icon.filename = %(source.dir)s/icon.png

# ---- the picture shown while the app starts: the icon on the same grey (instead of the Kivy logo) ----
presplash.filename = %(source.dir)s/icon.png
android.presplash_color = #7D7C7A

# ---- Android ----
# MANAGE_EXTERNAL_STORAGE = the "all files access" switch (Android 11+); the app opens its screen by itself
android.permissions = INTERNET,READ_EXTERNAL_STORAGE,WRITE_EXTERNAL_STORAGE,MANAGE_EXTERNAL_STORAGE,WAKE_LOCK,REQUEST_IGNORE_BATTERY_OPTIMIZATIONS
android.api = 33
android.minapi = 21
android.ndk = 25b
android.ndk_api = 21
# Two kinds of processors. If your phone is 64-bit (almost all are), you can delete ", armeabi-v7a"
# to make the build faster.
android.archs = arm64-v8a
android.accept_sdk_license = True
android.logcat_filters = *:S python:D

[buildozer]
log_level = 2
warn_on_root = 1
