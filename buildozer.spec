[app]
title = YT Downloader
package.name = ytdownloader
package.domain = org.alidsoki
source.dir = .
source.include_exts = py,png,jpg,jpeg,kv,atlas,json,ttf
version = 2.1

# -------------------------------------------------------------------
# Dependencies
# -------------------------------------------------------------------
requirements = python3==3.11.8,hostpython3==3.11.8,kivy==2.3.0,yt-dlp,arabic-reshaper,python-bidi,pyjnius,ffmpeg,certifi

# -------------------------------------------------------------------
# Android settings
# -------------------------------------------------------------------
fullscreen = 0
orientation = portrait
android.permissions = INTERNET,WRITE_EXTERNAL_STORAGE,READ_EXTERNAL_STORAGE,MANAGE_EXTERNAL_STORAGE,FOREGROUND_SERVICE,FOREGROUND_SERVICE_DATA_SYNC,WAKE_LOCK,POST_NOTIFICATIONS
android.archs = arm64-v8a
android.api = 34
android.minapi = 24
android.ndk = 25b
android.skip_update = False
android.accept_sdk_license = True
android.release_artifact = apk

# -------------------------------------------------------------------
# Service (Foreground Service - main.py يشتغل كـ app وكـ service)
# -------------------------------------------------------------------
services = Ytservice:./main.py:foreground

[buildozer]
log_level = 2
warn_on_root = 1
