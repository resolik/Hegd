[app]
p4a.branch = v2024.01.21

# ---- the name ----
# title is what the phone shows under the icon. package.name / package.domain are the app's ID for Android:
# changing them makes a DIFFERENT app (no update over the old one, the private data stays behind; files in
# /sdcard/butter stay). If nobody has installed it yet, decide before the first public release (F-Droid, GitHub).
title = Butter
package.name = butter
package.domain = org.butter
version = 0.1

# ---- the files: main.py and kiwi_core.py are in the same folder as this spec ----
source.dir = .
source.include_exts = py,png
# tul.py and make_manifest.py are tools, not the app; test_*.py are tests
source.exclude_patterns = tul.py,make_manifest.py,test_*.py
# the tool folders are downloaded by tul from GitHub, so they must NOT go into the APK
source.exclude_dirs = bin, .buildozer, __pycache__, icon, icon_hegd, icon_butter, hpiptool, network, gittool, asmtool, sshtool, asm_examples, tools_backup, site-packages, repos, .git

# ---- what the app needs ----
# openssl + certifi: without them https downloads (download) do not work inside the APK
# pyjnius: lets the app open the "all files access" screen on Android 11+ (and talks to the SSH library)
requirements = python3,kivy,pyjnius,openssl,certifi

orientation = portrait
fullscreen = 0

# ---- the icon (icon.png lies next to this spec; use the 512x512 picture) ----
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

# ---- SSH (the ssh tool) ----
# The ssh tool talks to the Java library JSch through pyjnius. Uncomment this line ONLY for the SSH test build:
# it makes the APK bigger, and it is not tested with this p4a version yet.
# android.gradle_dependencies = com.github.mwiede:jsch:0.2.22

[buildozer]
log_level = 2
warn_on_root = 1
