[app]
title = YT Downloader
package.name = ytdownloader
package.domain = org.ytdownloader
source.dir = .
source.include_exts = py,png,jpg,kv,ttf,json,so
version = 2.0.0

requirements = python3,kivy==2.3.0,yt-dlp,arabic-reshaper,python-bidi,pyjnius,imageio-ffmpeg

android.permissions = INTERNET,WRITE_EXTERNAL_STORAGE,READ_EXTERNAL_STORAGE,FOREGROUND_SERVICE,WAKE_LOCK,MANAGE_EXTERNAL_STORAGE,POST_NOTIFICATIONS

services = Ytservice:./main.py:foreground

android.api = 33
android.minapi = 21
android.ndk = 25b
android.accept_sdk_license = True
android.wakelock = True
android.archs = arm64-v8a, armeabi-v7a

p4a.branch = master

[buildozer]
log_level = 2
