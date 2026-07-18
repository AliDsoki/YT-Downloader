# بناء YT Downloader v2.0

## قبل البناء
حط ملف الخط هنا (مفقود من المرفوع الأصلي):
```
assets/NotoNaskhArabic-SemiBold.ttf
```
تحميل من: https://fonts.google.com/noto/specimen/Noto+Naskh+Arabic

## البناء (على Linux أو WSL)
```bash
pip install buildozer cython
buildozer android debug
```

الناتج: `bin/ytdownloader-2.0.0-debug.apk`
