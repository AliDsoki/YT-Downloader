[app]

# (str) Title of your application
title = YT Downloader

# (str) Package name
package.name = ytdownloader

# (str) Package domain (needed for android/ios packaging)
package.domain = org.alidsoki

# (str) Source code where the main.py live
source.dir = .

# (list) Source files to include (let empty to include all the files)
source.include_exts = py,png,jpg,jpeg,kv,atlas,json,ttf

# (str) Application versioning
version = 2.0

# (list) Application requirements
# لازم hostpython3 (بايثون البناء) و python3 (بايثون الهدف) يكونوا نفس النسخة بالظبط
# دي متطلبة أساسية من python-for-android، لو مش متطابقين البناء بيفشل فورًا
# "ffmpeg" هنا هو recipe بيبني ffmpeg الحقيقي لمعمار أندرويد (arm64) من المصدر
# ويحطه جوه الـ APK نفسه. ده الطريقة الوحيدة اللي تضمن اشتغال ffmpeg على أي
# جهاز حتى لو مفيش ffmpeg مثبت عليه أصلاً (imageio-ffmpeg ملهاش نسخة أندرويد
# خالص، فمكانتش هتشتغل على الموبايل مهما كان). محتاج yasm/nasm وقت البناء
# (متضافين في الوورك فلو تحت).
requirements = python3==3.11.8,hostpython3==3.11.8,kivy==2.3.0,yt-dlp,arabic-reshaper,python-bidi,pyjnius,ffmpeg,certifi

# (bool) Fullscreen
fullscreen = 0

# (str) Supported orientation (one of landscape, sensorLandscape, portrait or all)
orientation = portrait

# (list) Permissions
android.permissions = INTERNET,WRITE_EXTERNAL_STORAGE,READ_EXTERNAL_STORAGE,MANAGE_EXTERNAL_STORAGE,FOREGROUND_SERVICE,FOREGROUND_SERVICE_DATA_SYNC,WAKE_LOCK,POST_NOTIFICATIONS

# (list) The Android archs to build for
# مبني لمعمار واحد بس (arm64-v8a) - بيغطي كل الموبايلات الحديثة من 2017 لحد دلوقتي
# وده بيقلل وقت البناء وفرصة الأخطاء لأنه بيبني نسخة واحدة بس مش اتنين
android.archs = arm64-v8a

# (int) Target Android API
android.api = 34

# (int) Minimum API your APK / AAB will support
android.minapi = 24

# (str) Android NDK version to use
android.ndk = 25b

# (bool) If True, then skip trying to update the Android sdk
android.skip_update = False

# (bool) If True, then automatically accept SDK license
android.accept_sdk_license = True

# (str) The format used to package the app for release mode (aab or apk)
android.release_artifact = apk

# --------------------------------------------------------------------
# main.py ملف واحد ذاتي الاحتواء: بيشتغل كتطبيق عادي وكمان كخدمة خلفية
# (Foreground Service) من نفس الملف. لذلك الخدمة بتشاور على main.py نفسه
# مش ملف منفصل.
# --------------------------------------------------------------------
services = Ytservice:./main.py:foreground

[buildozer]

# (int) Log level (0 = error only, 1 = info, 2 = debug (with command output))
log_level = 2

# (int) Display warning if buildozer is run as root
warn_on_root = 1
