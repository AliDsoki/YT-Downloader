# -*- coding: utf-8 -*-
"""
YT Downloader v3.1 - النسخة النهائية المستقرة (معالجة أخطاء الجودات)
====================================================================
التغييرات الأساسية عن v3.0:
- تبنّي آلية MGV3.py في تحديد الجودات المتاحة (audio/video منفصلين + خريطة ارتفاع).
- إصلاح خطأ "Requested format not available":
    * لا نعتمد على format_id الهش وقت التحميل.
    * نستخدم دائماً selectors مرنة مع fallback (…/best).
    * نمرّر player_client متعدد + إعادة محاولة.
- ffmpeg مدمج (ParcelFileDescriptor) للحفظ عبر SAF.
- خدمة خلفية محصّنة (Foreground + Wake Lock).
- Thread Pool + Retry + Progress Smoothing.
- واجهة عربية بالكامل (Kivy) مع Lazy imports.
- معالجة شاملة للأخطاء لضمان عدم تعطل البرنامج.
"""

import os
import sys
import json
import time
import uuid
import shutil
import logging
import tempfile
import threading
import traceback
import subprocess
from concurrent.futures import ThreadPoolExecutor

# ================================================================
# Logging
# ================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("yt_dl")

# ================================================================
# Service Detection (قبل أي Kivy import)
# ================================================================
IS_SERVICE = (
    "PYTHON_SERVICE_ARGUMENT" in os.environ
    or "--service" in sys.argv
    or os.environ.get("P4A_SERVICE") == "1"
)

logger.info("=" * 60)
logger.info("Starting in %s mode", "SERVICE" if IS_SERVICE else "APP")
logger.info("=" * 60)

# ================================================================
# Kivy imports (Lazy - بس لو مش Service)
# ================================================================
if not IS_SERVICE:
    try:
        from kivy.config import Config
        Config.set("graphics", "fullscreen", "0")

        from kivy.app import App
        from kivy.lang import Builder
        from kivy.factory import Factory
        from kivy.metrics import dp
        from kivy.uix.boxlayout import BoxLayout
        from kivy.uix.floatlayout import FloatLayout
        from kivy.uix.label import Label
        from kivy.uix.button import Button
        from kivy.uix.behaviors import ToggleButtonBehavior
        from kivy.uix.textinput import TextInput
        from kivy.uix.image import AsyncImage
        from kivy.uix.progressbar import ProgressBar
        from kivy.uix.scrollview import ScrollView
        from kivy.uix.spinner import Spinner
        from kivy.uix.switch import Switch
        from kivy.uix.tabbedpanel import TabbedPanel, TabbedPanelItem
        from kivy.core.clipboard import Clipboard
        from kivy.clock import Clock
        from kivy.utils import platform
        from kivy.properties import ListProperty, StringProperty

        logger.info("✓ Kivy imports successful")
    except Exception as e:
        logger.error("✗ Kivy imports failed: %s", e)
        platform = "unknown"
else:
    logger.info("✓ Service mode - skipping Kivy imports")
    platform = "android"

# yt-dlp and Arabic support (متاح في الاتنين)
from yt_dlp import YoutubeDL
import yt_dlp
import arabic_reshaper
from bidi.algorithm import get_display


# ================================================================
# الدوال المشتركة
# ================================================================

APP_DIR = os.path.dirname(os.path.abspath(__file__))
FONT_PATH = os.path.join(APP_DIR, "assets", "NotoNaskhArabic-SemiBold.ttf")


# -------------------------------------------------------------------
# دالة موحدة لجلب Android context (تشتغل في App و Service)
# -------------------------------------------------------------------
def _get_android_context():
    """جيب Android context (يشتغل في app و service)."""
    if platform != "android":
        return None
    try:
        from jnius import autoclass
        try:
            return autoclass("org.kivy.android.PythonActivity").mActivity.getApplicationContext()
        except Exception:
            pass
        try:
            return autoclass("org.kivy.android.PythonService").mService.getApplicationContext()
        except Exception:
            pass
        try:
            return autoclass("android.app.ActivityThread").currentApplication().getApplicationContext()
        except Exception:
            pass
    except Exception:
        pass
    return None


# --- المسارات الأساسية (موحدة بين App و Service) ---
if platform == "android":
    _ctx = _get_android_context()
    if _ctx:
        _BASE = _ctx.getFilesDir().getAbsolutePath()
        logger.info("✓ Android context obtained, BASE: %s", _BASE)
    else:
        _BASE = os.path.join(os.path.expanduser("~"), ".yt_downloader")
        logger.warning("✗ Failed to get Android context, using fallback: %s", _BASE)
else:
    _BASE = os.path.join(os.path.expanduser("~"), ".yt_downloader")
    logger.info("✓ Non-Android platform, BASE: %s", _BASE)

os.makedirs(_BASE, exist_ok=True)

QUEUE_FILE = os.path.join(_BASE, "queue.json")
STATUS_FILE = os.path.join(_BASE, "status.json")
CONTROL_FILE = os.path.join(_BASE, "control.json")
SETTINGS_FILE = os.path.join(_BASE, "settings.json")
STORAGE_URI_FILE = os.path.join(_BASE, "storage_uri.txt")
DOWNLOAD_LOG_FILE = os.path.join(_BASE, "download.log")
ERROR_LOG_FILE = os.path.join(_BASE, "error.log")

logger.info("Queue file: %s", QUEUE_FILE)
logger.info("Queue exists: %s", os.path.isfile(QUEUE_FILE))


# -------------------------------------------------------------------
# نص عربي
# -------------------------------------------------------------------
def ar(text):
    if not text:
        return ""
    try:
        reshaped = arabic_reshaper.reshape(str(text))
        return get_display(reshaped)
    except Exception:
        return str(text)


def fit_label(label):
    label.bind(size=lambda inst, size: setattr(inst, "text_size", size))
    return label


# -------------------------------------------------------------------
# File Locking
# -------------------------------------------------------------------
_file_locks = {}
_locks_mutex = threading.Lock()


def _get_lock(filepath):
    with _locks_mutex:
        if filepath not in _file_locks:
            _file_locks[filepath] = threading.Lock()
        return _file_locks[filepath]


# -------------------------------------------------------------------
# JSON I/O
# -------------------------------------------------------------------
def read_json(filepath, default=None):
    if default is None:
        default = {}
    lock = _get_lock(filepath)
    with lock:
        try:
            if not os.path.isfile(filepath):
                return default
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if data is not None else default
        except Exception:
            return default


def write_json(filepath, data):
    lock = _get_lock(filepath)
    with lock:
        try:
            dir_name = os.path.dirname(filepath) or "."
            fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
                if os.path.exists(filepath):
                    os.replace(tmp_path, filepath)
                else:
                    os.rename(tmp_path, filepath)
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except Exception as e:
            logger.error("write_json failed: %s", e)


# -------------------------------------------------------------------
# Settings
# -------------------------------------------------------------------
_settings_cache = None
_settings_cache_time = 0


def read_settings():
    global _settings_cache, _settings_cache_time
    now = time.monotonic()
    if _settings_cache is not None and (now - _settings_cache_time) < 2.0:
        return dict(_settings_cache)
    data = read_json(SETTINGS_FILE, {"max_concurrent": 2, "pair_low_audio": True, "use_cookies": False})
    _settings_cache = dict(data)
    _settings_cache_time = now
    return dict(data)


def write_settings(settings):
    global _settings_cache, _settings_cache_time
    write_json(SETTINGS_FILE, settings)
    _settings_cache = dict(settings)
    _settings_cache_time = time.monotonic()


# -------------------------------------------------------------------
# Storage URI
# -------------------------------------------------------------------
def load_storage_uri():
    try:
        if os.path.isfile(STORAGE_URI_FILE):
            with open(STORAGE_URI_FILE, "r", encoding="utf-8") as f:
                return f.read().strip()
    except Exception:
        pass
    return ""


def save_storage_uri(uri):
    try:
        with open(STORAGE_URI_FILE, "w", encoding="utf-8") as f:
            f.write(uri)
    except Exception:
        pass


# -------------------------------------------------------------------
# Logging (سجل التحميل والأخطاء)
# -------------------------------------------------------------------
_log_locks = {
    DOWNLOAD_LOG_FILE: threading.Lock(),
    ERROR_LOG_FILE: threading.Lock(),
}


def read_log(filepath):
    lock = _log_locks.get(filepath, _get_lock(filepath))
    with lock:
        try:
            if not os.path.isfile(filepath):
                return ""
            with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            lines = content.strip().split("\n")
            if len(lines) > 50:
                return "\n".join(lines[-50:])
            return content
        except Exception:
            return ""


def append_download_log(msg):
    lock = _log_locks.get(DOWNLOAD_LOG_FILE, _get_lock(DOWNLOAD_LOG_FILE))
    with lock:
        try:
            ts = time.strftime("%H:%M:%S")
            with open(DOWNLOAD_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"[{ts}] {msg}\n")
        except Exception:
            pass


def append_error_log(msg):
    lock = _log_locks.get(ERROR_LOG_FILE, _get_lock(ERROR_LOG_FILE))
    with lock:
        try:
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            with open(ERROR_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"[{ts}] {msg}\n")
        except Exception:
            pass


def clear_log(filepath):
    lock = _log_locks.get(filepath, _get_lock(filepath))
    with lock:
        try:
            with open(filepath, "w", encoding="utf-8") as f:
                f.write("")
        except Exception:
            pass


# ================================================================
# نظام تحديد الجودات (مستوحى من MGV3.py)
# ================================================================

# الجودات الثمانية المعروضة في الواجهة
QUALITY_LABELS = [
    ("audio_low", "صوت منخفض"),
    ("audio_high", "أعلى صوت"),
    ("video_144", "144p"),
    ("video_240", "240p"),
    ("video_360", "360p"),
    ("video_480", "480p"),
    ("video_720", "720p"),
    ("video_1080", "1080p"),
]

# ارتفاعات الفيديو المستهدفة
_VIDEO_HEIGHTS = {
    "video_144": 144,
    "video_240": 240,
    "video_360": 360,
    "video_480": 480,
    "video_720": 720,
    "video_1080": 1080,
}

# عملاء yt-dlp بالترتيب (مثل InnerTube في MGV3) لتفادي أخطاء التوقيع/الفورمات
_PLAYER_CLIENTS = ["ios", "android", "tv", "web"]


def _fmt_mb(size_bytes):
    """تنسيق الحجم بالميجابايت كسلسلة نصية مقروءة."""
    if not size_bytes:
        return "?"
    try:
        mb = float(size_bytes) / (1024 * 1024)
    except (TypeError, ValueError):
        return "?"
    if mb >= 100:
        return f"{mb:.0f}"
    if mb >= 10:
        return f"{mb:.1f}"
    return f"{mb:.2f}"


def _fmt_size_val(f, duration=0):
    """
    حساب الحجم التقريبي لفورمات واحد (على غرار information_force في MGV3).
    يُرجع الحجم بالبايت.
    """
    fs = f.get("filesize") or f.get("filesize_approx")
    if fs:
        return int(fs)
    tbr = f.get("tbr")
    if tbr and duration:
        return int((tbr * 1000 / 8) * duration)
    abr = f.get("abr") or 0
    vbr = f.get("vbr") or 0
    if (abr or vbr) and duration:
        return int(((abr + vbr) * 1000 / 8) * duration)
    return 0


def _has_codec(codec_val):
    """التحقق من وجود codec صالح (None / '' / 'none' / 'null' تعتبر غير موجودة)."""
    if not codec_val:
        return False
    return str(codec_val).strip().lower() not in ("", "none", "null")


def analyze_formats(info):
    """
    تحليل الفورماتات المتاحة على غرار MGV3.py.
    يُرجع:
      sorted_audio: قائمة (format_id, abr, filesize) مرتبة تصاعدياً حسب abr.
      video_qualities: dict {quality_key: {"height": h, "size": bytes, "fid": id}}
                       مع اختيار أصغر نسخة (low) لكل ارتفاع.
      video_qualities_high: مثله لكن أكبر نسخة (high) لكل ارتفاع.
    """
    formats = info.get("formats") or []
    duration = info.get("duration") or 0

    audio_formats = []
    video_only = []  # فيديو بدون صوت

    for f in formats:
        # نتجاهل الـ storyboards والصيغ الغريبة
        if f.get("vcodec") is None and f.get("acodec") is None:
            continue
        has_v = _has_codec(f.get("vcodec"))
        has_a = _has_codec(f.get("acodec"))

        if has_a and not has_v:
            audio_formats.append(f)
        elif has_v and not has_a:
            video_only.append(f)
        # نتجاهل الفورماتات المدمجة (has_v and has_a) لأننا ندمج يدوياً

    # ترتيب الصوت تصاعدياً حسب abr
    audio_formats.sort(key=lambda f: (f.get("abr") or f.get("tbr") or 0))

    sorted_audio = []
    for af in audio_formats:
        fid = af.get("format_id", "")
        abr = af.get("abr") or af.get("tbr") or 0
        fsize = _fmt_size_val(af, duration)
        sorted_audio.append((fid, abr, fsize))

    # بناء خرائط الفيديو (low / high) لكل ارتفاع مطلوب
    video_qualities = {}
    video_qualities_high = {}

    for key, target_h in _VIDEO_HEIGHTS.items():
        # المرشحون: كل فيديو ارتفاعه ضمن نطاق حول الارتفاع المستهدف
        candidates = [
            f for f in video_only
            if target_h - 60 <= (f.get("height") or 0) <= target_h + 30
        ]
        if not candidates:
            continue

        # low = أصغر حجم / high = أكبر حجم لنفس الارتفاع
        def _size_key(f):
            s = _fmt_size_val(f, duration)
            return s if s > 0 else float("inf")

        low = min(candidates, key=_size_key)
        high = max(candidates, key=lambda f: _fmt_size_val(f, duration))

        video_qualities[key] = {
            "height": low.get("height") or target_h,
            "size": _fmt_size_val(low, duration),
            "fid": low.get("format_id", ""),
        }
        video_qualities_high[key] = {
            "height": high.get("height") or target_h,
            "size": _fmt_size_val(high, duration),
            "fid": high.get("format_id", ""),
        }

    return sorted_audio, video_qualities, video_qualities_high


def build_format_selector(quality_key, sorted_audio, video_map, use_low_audio=True):
    """
    بناء selector مرن مع fallback (الحل الأساسي لخطأ Requested format not available).

    الفكرة المستوحاة من MGV3 + نصيحة yt-dlp:
    - لا نعتمد على format_id ثابت وحده لأنه قد يفشل وقت التحميل.
    - نستخدم القيود على الارتفاع/abr التي تبقى صالحة بين الجلسات.
    - نضيف دائماً سلسلة fallback تنتهي بـ 'best' حتى لا يفشل التحميل أبداً.

    يُرجع (format_selector_string, size_bytes)
    """
    if quality_key == "audio_low":
        size = sorted_audio[0][2] if sorted_audio else 0
        # أقل صوت متاح ثم أي صوت ثم أفضل صوت
        selector = "worstaudio/bestaudio[abr<=64]/bestaudio/ba"
        return selector, size

    if quality_key == "audio_high":
        size = sorted_audio[-1][2] if sorted_audio else 0
        selector = "bestaudio/ba/best"
        return selector, size

    # جودة فيديو
    info = video_map.get(quality_key)
    h = _VIDEO_HEIGHTS.get(quality_key, 720)
    if info and info.get("height"):
        h = info["height"]

    v_size = info["size"] if info else 0
    a_size = 0
    if sorted_audio:
        a_size = sorted_audio[0][2] if use_low_audio else sorted_audio[-1][2]
    total = (v_size or 0) + (a_size or 0)

    # اختيار الصوت المرافق (قيد مرن بدل format_id)
    if use_low_audio:
        audio_part = "worstaudio/bestaudio[abr<=64]/bestaudio"
    else:
        audio_part = "bestaudio"

    # selector مرن متعدد الطبقات مع fallback نهائي إلى best
    # 1) أفضل فيديو ضمن الارتفاع + الصوت المطلوب
    # 2) أفضل فيديو ضمن الارتفاع + أي صوت
    # 3) صيغة مدمجة جاهزة ضمن الارتفاع
    # 4) أي صيغة ضمن الارتفاع
    # 5) أفضل شيء متاح على الإطلاق (يمنع الفشل نهائياً)
    selector = (
        f"bv*[height<={h}]+{audio_part}/"
        f"bv*[height<={h}]+ba/"
        f"b[height<={h}]/"
        f"bv*[height<={h}]/"
        f"best[height<={h}]/"
        f"bv*+ba/b/best"
    )
    return selector, total


def sanitize_name(name):
    if not name:
        return "unnamed"
    name = str(name)
    for ch in '<>:"/\\|?*':
        name = name.replace(ch, "_")
    # إزالة محارف التحكم
    name = "".join(c for c in name if ord(c) >= 32)
    return name[:100].strip().strip(".") or "unnamed"


# ================================================================
# Queue / Status helpers
# ================================================================

def update_job_status(job_id, status, extra=None):
    queue = read_json(QUEUE_FILE, [])
    for j in queue:
        if j.get("id") == job_id:
            j["status"] = status
            if extra:
                j.update(extra)
            break
    write_json(QUEUE_FILE, queue)


def update_job_progress(job_id, percent, status=None, error=None, saved_uri=None, saved_path=None):
    status_data = read_json(STATUS_FILE, {})
    entry = status_data.get(job_id, {})
    entry["percent"] = percent
    if status:
        entry["status"] = status
    if error:
        entry["error"] = error
    if saved_uri:
        entry["saved_uri"] = saved_uri
    if saved_path:
        entry["saved_path"] = saved_path
    entry["updated"] = time.time()
    status_data[job_id] = entry
    write_json(STATUS_FILE, status_data)


# ================================================================
# FFmpeg Resolver
# ================================================================

_ffmpeg_cached = None


def _is_executable(path):
    return bool(path) and os.path.isfile(path) and os.access(path, os.X_OK)


def _ff_search_bundled():
    names = ["ffmpeg", "ffmpeg.exe", "ffmpeg.bin"]
    subdirs = ["", "assets", "lib", "bin"]
    for subdir in subdirs:
        base = os.path.join(APP_DIR, subdir) if subdir else APP_DIR
        for name in names:
            p = os.path.join(base, name)
            if _is_executable(p):
                return p
    return None


def _ff_search_android():
    ctx = _get_android_context()
    if not ctx:
        return None
    try:
        lib_dir = ctx.getApplicationInfo().nativeLibraryDir
        for name in ("libffmpeg.so", "ffmpeg", "ffmpeg.so"):
            p = os.path.join(lib_dir, name)
            if os.path.isfile(p):
                try:
                    os.chmod(p, 0o755)
                except OSError:
                    pass
                if _is_executable(p):
                    return p
        files_dir = ctx.getFilesDir().getAbsolutePath()
        for name in ("ffmpeg", "ffmpeg.bin"):
            p = os.path.join(files_dir, name)
            if _is_executable(p):
                return p
    except Exception:
        pass
    return None


def _ff_search_imageio():
    try:
        import imageio_ffmpeg
        p = imageio_ffmpeg.get_ffmpeg_exe()
        if p and os.path.isfile(p):
            return p
    except Exception:
        pass
    return None


def ffmpeg_resolve():
    global _ffmpeg_cached
    if _ffmpeg_cached and os.path.isfile(_ffmpeg_cached):
        return _ffmpeg_cached

    for fn in (_ff_search_bundled, _ff_search_android, _ff_search_imageio,
               lambda: shutil.which("ffmpeg")):
        try:
            p = fn()
            if p:
                logger.info("✓ ffmpeg found: %s", p)
                _ffmpeg_cached = p
                return p
        except Exception:
            pass

    logger.warning("✗ ffmpeg not found")
    return None


def ffmpeg_version():
    path = ffmpeg_resolve()
    if not path:
        return None
    try:
        r = subprocess.run([path, "-version"], capture_output=True, text=True, timeout=10)
        return r.stdout.split("\n", 1)[0].strip() if r.stdout else None
    except Exception:
        return None


# ================================================================
# yt-dlp options builder (موحّد بين التحليل والتحميل)
# ================================================================

def _base_ydl_opts():
    """خيارات yt-dlp أساسية محصّنة ضد الأخطاء الشائعة."""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "cachedir": False,
        "socket_timeout": 30,
        "nocheckcertificate": True,
        "geo_bypass": True,
        "ignoreerrors": False,
        "retries": 10,
        "fragment_retries": 10,
        # تمرير عملاء متعددين لتفادي أخطاء التوقيع/الفورمات (مثل MGV3 InnerTube)
        "extractor_args": {
            "youtube": {
                "player_client": _PLAYER_CLIENTS,
            }
        },
    }
    return opts


def _maybe_add_cookies(opts):
    """إضافة الكوكيز إن وُجد ملف كوكيز صالح (اختياري)."""
    try:
        cookie_file = os.path.join(_BASE, "cookies.txt")
        if os.path.isfile(cookie_file) and os.path.getsize(cookie_file) > 50:
            opts["cookiefile"] = cookie_file
    except Exception:
        pass
    return opts


# ================================================================
# Service Logic
# ================================================================

_wake_lock = None
_notification_started = False
_executor = None
_active_futures = {}
_shutdown_event = threading.Event()
_progress_lock = threading.Lock()
_last_progress_update = {}


def _acquire_wake_lock():
    global _wake_lock
    if platform != "android" or _wake_lock is not None:
        return
    try:
        from jnius import autoclass
        ctx = _get_android_context()
        if not ctx:
            return
        PowerManager = autoclass("android.os.PowerManager")
        pm = ctx.getSystemService("power")
        _wake_lock = pm.newWakeLock(
            PowerManager.PARTIAL_WAKE_LOCK, "yt_downloader:download_lock"
        )
        _wake_lock.acquire(4 * 60 * 60 * 1000)
        logger.info("✓ Wake lock acquired")
    except Exception as e:
        logger.warning("✗ Wake lock failed: %s", e)


def _release_wake_lock():
    global _wake_lock
    if _wake_lock is not None:
        try:
            if _wake_lock.isHeld():
                _wake_lock.release()
        except Exception:
            pass
        _wake_lock = None


def _create_notification_channel():
    if platform != "android":
        return
    try:
        from jnius import autoclass
        Build = autoclass("android.os.Build$VERSION")
        if Build.SDK_INT >= 26:
            ctx = _get_android_context()
            if not ctx:
                return
            NotificationChannel = autoclass("android.app.NotificationChannel")
            NotificationManager = autoclass("android.app.NotificationManager")
            channel = NotificationChannel(
                "yt_downloader_channel",
                "تحميلات يوتيوب",
                NotificationManager.IMPORTANCE_LOW
            )
            channel.setDescription("إشعارات التحميل في الخلفية")
            nm = ctx.getSystemService("notification")
            nm.createNotificationChannel(channel)
            logger.info("✓ Notification channel created")
    except Exception as e:
        logger.warning("✗ Notification channel failed: %s", e)


def _update_notification(title, text, progress=-1):
    global _notification_started
    if platform != "android":
        return
    try:
        from jnius import autoclass
        ctx = _get_android_context()
        if not ctx:
            return

        NotificationCompat = autoclass("androidx.core.app.NotificationCompat$Builder")
        builder = NotificationCompat(ctx, "yt_downloader_channel")
        builder.setContentTitle(title)
        builder.setContentText(text)
        builder.setSmallIcon(ctx.getApplicationInfo().icon)
        builder.setOngoing(True)

        if 0 <= progress <= 100:
            builder.setProgress(100, int(progress), False)
        elif progress == -1:
            builder.setProgress(0, 0, True)

        notification = builder.build()

        if not _notification_started:
            try:
                service_obj = autoclass("org.kivy.android.PythonService").mService
                if service_obj:
                    service_obj.startForeground(1, notification)
                    _notification_started = True
                    logger.info("✓ Started foreground service")
            except Exception as e:
                logger.warning("✗ Failed to start foreground: %s", e)

        nm = ctx.getSystemService("notification")
        nm.notify(1, notification)
    except Exception as e:
        logger.debug("Notification update failed: %s", e)


def _stop_foreground():
    global _notification_started
    if platform != "android" or not _notification_started:
        return
    try:
        from jnius import autoclass
        service_obj = autoclass("org.kivy.android.PythonService").mService
        if service_obj:
            service_obj.stopForeground(True)
            _notification_started = False
    except Exception:
        pass


def _init_executor():
    global _executor
    if _executor is None or getattr(_executor, "_shutdown", False):
        settings = read_settings()
        max_workers = max(1, min(settings.get("max_concurrent", 2), 4))
        _executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="dl_worker")
        logger.info("✓ Thread pool: %d workers", max_workers)


def _should_update_progress(job_id):
    now = time.monotonic()
    with _progress_lock:
        last = _last_progress_update.get(job_id, 0)
        if now - last >= 2.0:
            _last_progress_update[job_id] = now
            return True
    return False


def _check_control(job_id):
    controls = read_json(CONTROL_FILE, {})
    action = controls.get(job_id)
    if action:
        controls.pop(job_id, None)
        write_json(CONTROL_FILE, controls)
    return action


def _progress_hook(job_id, d):
    status = d.get("status")
    if status == "downloading":
        total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
        downloaded = d.get("downloaded_bytes", 0)
        percent = (downloaded / total * 100) if total > 0 else 0
        if _should_update_progress(job_id):
            update_job_progress(job_id, percent, status="downloading")
            _update_notification("جاري التحميل", f"{percent:.0f}%", progress=percent)
    elif status == "finished":
        update_job_progress(job_id, 100, status="downloading")

    action = _check_control(job_id)
    if action == "cancel":
        raise Exception("CANCELLED_BY_USER")
    elif action == "pause":
        raise Exception("PAUSED_BY_USER")


def _default_format(quality_key):
    """
    fallback نهائي لو المهمة أُنشئت بدون format_selector (مثل عناصر القوائم).
    كلها مرنة وتنتهي بـ best لتفادي أي فشل.
    """
    mapping = {
        "audio_low": "worstaudio/bestaudio[abr<=64]/bestaudio/ba/best",
        "audio_high": "bestaudio/ba/best",
        "video_144": "bv*[height<=144]+ba/b[height<=144]/best[height<=144]/bv*+ba/b/best",
        "video_240": "bv*[height<=240]+ba/b[height<=240]/best[height<=240]/bv*+ba/b/best",
        "video_360": "bv*[height<=360]+ba/b[height<=360]/best[height<=360]/bv*+ba/b/best",
        "video_480": "bv*[height<=480]+ba/b[height<=480]/best[height<=480]/bv*+ba/b/best",
        "video_720": "bv*[height<=720]+ba/b[height<=720]/best[height<=720]/bv*+ba/b/best",
        "video_1080": "bv*[height<=1080]+ba/b[height<=1080]/best[height<=1080]/bv*+ba/b/best",
    }
    return mapping.get(quality_key, "bv*+ba/b/best")


def _download_single_job(job):
    job_id = job["id"]
    url = job["url"]
    title = job.get("title", "video")
    quality_key = job.get("quality_key", "video_720")
    format_selector = job.get("format_selector", "")
    storage_uri = job.get("storage_uri", "")
    playlist_name = job.get("playlist_name", "")

    logger.info("=" * 60)
    logger.info("Starting job %s: %s [%s]", job_id[:8], title[:40], quality_key)
    logger.info("URL: %s", url[:80])

    update_job_status(job_id, "downloading")
    update_job_progress(job_id, 0, status="downloading")
    _update_notification("جاري التحميل", title[:30], progress=0)

    ffmpeg_path = ffmpeg_resolve()
    temp_dir = os.path.join(_BASE, "temp", job_id[:12])
    os.makedirs(temp_dir, exist_ok=True)

    # selector النهائي: المخزّن مع المهمة، وإلا fallback مرن
    if format_selector:
        # نضمن أن للـ selector المخزّن fallback نهائي إلى best
        if "best" not in format_selector.split("/")[-1]:
            format_selector = format_selector + "/best"
        chosen_format = format_selector
    else:
        chosen_format = _default_format(quality_key)

    logger.info("Format selector: %s", chosen_format)

    ydl_opts = _base_ydl_opts()
    _maybe_add_cookies(ydl_opts)
    ydl_opts.update({
        "noprogress": False,
        "progress_hooks": [lambda d: _progress_hook(job_id, d)],
        "outtmpl": os.path.join(temp_dir, "%(id)s.%(ext)s"),
        "format": chosen_format,
        # لو فشل التنسيق المطلوب، لا نرمي استثناء فوراً — نترك fallback يعمل
        "format_sort": ["res", "ext:mp4:m4a"],
    })

    if ffmpeg_path:
        ydl_opts["ffmpeg_location"] = ffmpeg_path

    if quality_key.startswith("audio_"):
        ydl_opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "48" if quality_key == "audio_low" else "0",
        }]
    else:
        ydl_opts["merge_output_format"] = "mp4"

    # --- التحميل مع retry + معالجة خطأ الفورمات ---
    max_retries = 3
    downloaded_files = []
    last_error = None

    for attempt in range(1, max_retries + 1):
        try:
            logger.info("Attempt %d/%d for job %s", attempt, max_retries, job_id[:8])

            # في المحاولة الأخيرة نستخدم أبسط selector مضمون
            if attempt == max_retries and quality_key.startswith("audio_"):
                ydl_opts["format"] = "bestaudio/best"
            elif attempt == max_retries:
                ydl_opts["format"] = "bv*+ba/b/best"

            with YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)

            if info:
                requested = info.get("requested_downloads") or []
                if requested:
                    for rd in requested:
                        fpath = rd.get("filepath") or rd.get("filename")
                        if fpath and os.path.isfile(fpath):
                            downloaded_files.append(fpath)
                            logger.info("✓ Downloaded: %s", fpath)
                else:
                    fpath = info.get("filepath") or info.get("filename")
                    if fpath and os.path.isfile(fpath):
                        downloaded_files.append(fpath)
                        logger.info("✓ Downloaded: %s", fpath)

            # لو لم نجد ملفات عبر info، افحص المجلد المؤقت
            if not downloaded_files:
                downloaded_files = _scan_temp_files(temp_dir, quality_key)

            if downloaded_files:
                break

        except yt_dlp.utils.DownloadError as de:
            last_error = str(de)
            logger.warning("✗ Attempt %d DownloadError: %s", attempt, last_error[:150])
            if "CANCELLED_BY_USER" in last_error:
                logger.info("Job %s cancelled", job_id[:8])
                update_job_status(job_id, "cancelled")
                update_job_progress(job_id, 0, status="cancelled")
                _cleanup_temp(temp_dir)
                return
            if "PAUSED_BY_USER" in last_error:
                logger.info("Job %s paused", job_id[:8])
                update_job_status(job_id, "paused")
                update_job_progress(job_id, 0, status="paused")
                return
            # خطأ فورمات: خفّض التنسيق للمحاولة التالية
            if "Requested format is not available" in last_error or "requested format" in last_error.lower():
                logger.info("Format not available -> switching to flexible fallback")
                if quality_key.startswith("audio_"):
                    ydl_opts["format"] = "bestaudio/best"
                else:
                    ydl_opts["format"] = "bv*+ba/b/best"
            if attempt < max_retries:
                time.sleep(min(2 ** attempt, 8))
            else:
                _fail_job(job_id, last_error, temp_dir)
                return

        except Exception as e:
            last_error = str(e)
            if "CANCELLED_BY_USER" in last_error:
                update_job_status(job_id, "cancelled")
                update_job_progress(job_id, 0, status="cancelled")
                _cleanup_temp(temp_dir)
                return
            if "PAUSED_BY_USER" in last_error:
                update_job_status(job_id, "paused")
                update_job_progress(job_id, 0, status="paused")
                return
            logger.warning("✗ Attempt %d failed: %s", attempt, last_error[:150])
            if attempt < max_retries:
                time.sleep(min(2 ** attempt, 8))
            else:
                _fail_job(job_id, last_error, temp_dir)
                return

    if not downloaded_files:
        _fail_job(job_id, last_error or "No files downloaded after all retries", temp_dir)
        return

    # --- الحفظ ---
    try:
        update_job_status(job_id, "saving")
        update_job_progress(job_id, 100, status="saving")
        _update_notification("جاري الحفظ", title[:30], progress=100)

        saved_path = ""
        saved_uri = ""

        for src_file in downloaded_files:
            if storage_uri:
                saved_uri = _save_to_saf(src_file, title, playlist_name, storage_uri) or saved_uri
            else:
                saved_path = _save_to_filesystem(src_file, title, playlist_name) or saved_path

        if not saved_uri and not saved_path:
            _fail_job(job_id, "فشل حفظ الملف في الوجهة", temp_dir)
            return

        update_job_status(job_id, "finished", {"saved_uri": saved_uri, "saved_path": saved_path})
        update_job_progress(job_id, 100, status="finished", saved_uri=saved_uri, saved_path=saved_path)
        append_download_log(f"✓ {title} - {quality_key}")
        _cleanup_temp(temp_dir)
        _update_notification("تم التحميل", title[:30], progress=100)
        logger.info("✓ Job %s completed successfully!", job_id[:8])
        logger.info("=" * 60)
    except Exception as e:
        _fail_job(job_id, f"خطأ أثناء الحفظ: {e}", temp_dir)


def _fail_job(job_id, error, temp_dir):
    """تسجيل فشل المهمة بأمان دون تعطيل الخدمة."""
    err = (str(error) or "خطأ غير معروف")[:150]
    logger.error("✗ Job %s failed: %s", job_id[:8], err)
    update_job_status(job_id, "error", {"error": err})
    update_job_progress(job_id, 0, status="error", error=err)
    append_error_log(f"{job_id[:8]}: {err}")
    _cleanup_temp(temp_dir)


def _scan_temp_files(temp_dir, quality_key):
    """فحص المجلد المؤقت عن ملفات ناتجة (احتياطي إن لم يُرجع info المسار)."""
    result = []
    try:
        skip = (".part", ".ytdl", ".tmp")
        for name in os.listdir(temp_dir):
            p = os.path.join(temp_dir, name)
            if os.path.isfile(p) and not name.lower().endswith(skip):
                result.append(p)
        # الأكبر أولاً (الملف الرئيسي)
        result.sort(key=lambda x: os.path.getsize(x) if os.path.exists(x) else 0, reverse=True)
    except Exception:
        pass
    return result


def _save_to_saf(src_file, title, playlist_name, storage_uri):
    """حفظ ملف عبر Android SAF باستخدام ParcelFileDescriptor."""
    if platform != "android":
        return ""
    try:
        from jnius import autoclass
        ctx = _get_android_context()
        if not ctx:
            logger.error("✗ Cannot get Android context for SAF save")
            return ""
        Uri = autoclass("android.net.Uri")
        DocumentsContract = autoclass("android.provider.DocumentsContract")

        tree_uri = Uri.parse(storage_uri)
        # تحويل tree uri إلى document uri للمجلد الجذر
        try:
            root_doc_id = DocumentsContract.getTreeDocumentId(tree_uri)
            tree_uri = DocumentsContract.buildDocumentUriUsingTree(tree_uri, root_doc_id)
        except Exception:
            pass

        # إنشاء مجلد قائمة التشغيل لو موجود
        if playlist_name:
            try:
                folder_uri = DocumentsContract.createDocument(
                    ctx.getContentResolver(), tree_uri,
                    "vnd.android.document/directory", sanitize_name(playlist_name)
                )
                if folder_uri:
                    tree_uri = folder_uri
            except Exception:
                pass

        ext = os.path.splitext(src_file)[1] or ".mp4"
        safe_title = sanitize_name(title)
        mime_type = "video/mp4" if ext.lower() in (".mp4", ".mkv", ".webm") else "audio/mpeg"

        file_uri = DocumentsContract.createDocument(
            ctx.getContentResolver(), tree_uri, mime_type, safe_title + ext
        )
        if not file_uri:
            logger.error("✗ Failed to create SAF document")
            return ""

        pfd = ctx.getContentResolver().openFileDescriptor(file_uri, "w")
        if not pfd:
            logger.error("✗ Failed to open ParcelFileDescriptor")
            return ""

        try:
            fd = pfd.getFd()
            file_size = os.path.getsize(src_file)
            written = 0
            logger.info("SAF save: writing %d bytes to %s", file_size, safe_title)

            with open(src_file, "rb") as src:
                while True:
                    chunk = src.read(131072)
                    if not chunk:
                        break
                    offset = 0
                    while offset < len(chunk):
                        n = os.write(fd, chunk[offset:])
                        if n <= 0:
                            raise IOError("os.write returned 0")
                        offset += n
                        written += n

            logger.info("✓ SAF save: wrote %d / %d bytes", written, file_size)
        finally:
            pfd.close()

        return file_uri.toString()

    except Exception as e:
        logger.error("✗ SAF save failed: %s", e)
        append_error_log(f"SAF save error: {e}")
        return ""


def _save_to_filesystem(src_file, title, playlist_name):
    try:
        if platform == "android":
            try:
                from jnius import autoclass
                Environment = autoclass("android.os.Environment")
                base_dir = Environment.getExternalStorageDirectory().getAbsolutePath()
            except Exception:
                base_dir = os.path.expanduser("~")
        else:
            base_dir = os.path.expanduser("~")

        save_dir = os.path.join(base_dir, "Downloads", "YT_Downloader")
        if playlist_name:
            save_dir = os.path.join(save_dir, sanitize_name(playlist_name))
        os.makedirs(save_dir, exist_ok=True)

        ext = os.path.splitext(src_file)[1] or ".mp4"
        safe_title = sanitize_name(title)
        dest_path = os.path.join(save_dir, safe_title + ext)

        counter = 1
        while os.path.exists(dest_path):
            dest_path = os.path.join(save_dir, f"{safe_title}_{counter}{ext}")
            counter += 1

        shutil.copy2(src_file, dest_path)
        logger.info("✓ Filesystem save: %s", dest_path)
        return dest_path
    except Exception as e:
        logger.error("✗ Filesystem save failed: %s", e)
        append_error_log(f"Save error: {e}")
        return ""


def _cleanup_temp(temp_dir):
    try:
        if os.path.isdir(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)
    except Exception:
        pass


def _process_queue():
    queue = read_json(QUEUE_FILE, [])
    settings = read_settings()
    max_concurrent = settings.get("max_concurrent", 2)

    queued_count = sum(1 for j in queue if j.get("status") == "queued")
    if queued_count > 0:
        logger.info("Found %d queued jobs", queued_count)

    active_count = sum(
        1 for j in queue
        if j.get("status") == "downloading"
        and j["id"] in _active_futures
        and not _active_futures[j["id"]].done()
    )

    for job in queue:
        if _shutdown_event.is_set():
            break
        if active_count >= max_concurrent:
            break
        if job.get("status") != "queued":
            continue

        job_id = job["id"]
        if job_id in _active_futures:
            future = _active_futures[job_id]
            if not future.done():
                continue
            del _active_futures[job_id]

        future = _executor.submit(_download_single_job, job)
        _active_futures[job_id] = future
        active_count += 1
        update_job_status(job_id, "downloading")

    done_ids = [jid for jid, f in _active_futures.items() if f.done()]
    for jid in done_ids:
        future = _active_futures.pop(jid)
        try:
            exc = future.exception(timeout=0)
            if exc:
                logger.error("✗ Job %s crashed: %s", jid[:8], exc)
                update_job_status(jid, "error")
                update_job_progress(jid, 0, status="error", error=str(exc)[:100])
                append_error_log(f"{jid[:8]}: {exc}")
        except Exception:
            pass


def _service_main_loop():
    logger.info("=" * 60)
    logger.info("Service main loop started")
    logger.info("BASE: %s", _BASE)
    logger.info("=" * 60)

    _acquire_wake_lock()
    _init_executor()

    ffmpeg_path = ffmpeg_resolve()
    if ffmpeg_path:
        logger.info("✓ ffmpeg: %s", ffmpeg_path)
    else:
        logger.warning("✗ ffmpeg not found - merge will fail")
        append_error_log("ffmpeg not found! Video+audio merge will not work.")

    _update_notification("خدمة التحميل", "جاهز", progress=-1)

    try:
        while not _shutdown_event.is_set():
            try:
                _process_queue()
            except Exception as e:
                logger.error("✗ Queue error: %s", e)
                append_error_log(f"Queue error: {e}")
            _shutdown_event.wait(timeout=3.0)
    finally:
        logger.info("Service shutting down...")
        try:
            if _executor and not getattr(_executor, "_shutdown", False):
                _executor.shutdown(wait=True, cancel_futures=False)
        except Exception:
            pass
        _release_wake_lock()
        _stop_foreground()
        logger.info("Service stopped")


def run_service():
    try:
        _service_main_loop()
    except Exception as e:
        logger.critical("✗ Service crashed: %s", e)
        append_error_log(f"Service crash: {e}\n{traceback.format_exc()}")
    finally:
        _release_wake_lock()


# ================================================================
# Kivy App (UI)
# ================================================================

if not IS_SERVICE:
    KV = """
<Card>:
    padding: [dp(12), dp(16), dp(12), dp(12)]
    spacing: dp(10)
    canvas.before:
        Color:
            rgba: 0, 0, 0, 0.45
        RoundedRectangle:
            pos: self.x, self.y - dp(3)
            size: self.size
            radius: [dp(16)]
        Color:
            rgba: 0.15, 0.15, 0.19, 1
        RoundedRectangle:
            pos: self.pos
            size: self.size
            radius: [dp(16)]

<Button3D>:
    background_normal: ""
    background_down: ""
    background_color: 0, 0, 0, 0
    bg_color: 0.20, 0.45, 0.85, 1
    color: 1, 1, 1, 1
    bold: True
    font_name: app.font_path
    halign: "center"
    valign: "middle"
    text_size: self.size
    shorten: True
    canvas.before:
        Color:
            rgba: 0, 0, 0, 0.55
        RoundedRectangle:
            pos: self.x, (self.y - dp(4)) if self.state == "normal" else (self.y - dp(1))
            size: self.size
            radius: [dp(12)]
        Color:
            rgba: self.bg_color
        RoundedRectangle:
            pos: self.x, (self.y + dp(3)) if self.state == "normal" else self.y
            size: self.size
            radius: [dp(12)]
        Color:
            rgba: 1, 1, 1, 0.16
        RoundedRectangle:
            pos: self.x, ((self.y + dp(3)) if self.state == "normal" else self.y) + self.height * 0.5
            size: self.width, self.height * 0.5
            radius: [dp(12), dp(12), 0, 0]

<QualityToggle>:
    bg_color: (0.22, 0.62, 0.36, 1) if self.state == "down" else (0.26, 0.26, 0.30, 1)
    opacity: 0.45 if self.disabled else 1
    canvas.before:
        Color:
            rgba: 0, 0, 0, 0.55
        RoundedRectangle:
            pos: self.x, (self.y - dp(3)) if self.state == "normal" else (self.y - dp(1))
            size: self.size
            radius: [dp(10)]
        Color:
            rgba: self.bg_color
        RoundedRectangle:
            pos: self.x, (self.y + dp(2)) if self.state == "normal" else self.y
            size: self.size
            radius: [dp(10)]
        Color:
            rgba: 1, 1, 1, 0.16
        RoundedRectangle:
            pos: self.x, ((self.y + dp(2)) if self.state == "normal" else self.y) + self.height * 0.5
            size: self.width, self.height * 0.5
            radius: [dp(10), dp(10), 0, 0]
    Label:
        text: root.label_text
        font_name: app.font_path
        font_size: "11sp"
        bold: True
        size_hint: (0.92, 0.44)
        pos_hint: {"center_x": 0.5, "top": 0.93}
        halign: "center"
        valign: "middle"
        text_size: self.size
        shorten: True
        color: 1, 1, 1, 0.90
    Label:
        text: root.size_text
        font_name: app.font_path
        font_size: "12sp"
        bold: True
        size_hint: (0.92, 0.40)
        pos_hint: {"center_x": 0.5, "y": 0.10}
        halign: "center"
        valign: "middle"
        text_size: self.size
        shorten: True
        color: 1, 1, 1, 1

<SmallButton3D@Button3D>:
    font_size: "13sp"

<DownloadCard>:
    orientation: "vertical"
    size_hint_y: None
    height: dp(160)
    padding: dp(10)
    spacing: dp(6)
    canvas.before:
        Color:
            rgba: 0, 0, 0, 0.4
        RoundedRectangle:
            pos: self.x, self.y - dp(2)
            size: self.size
            radius: [dp(14)]
        Color:
            rgba: 0.17, 0.17, 0.21, 1
        RoundedRectangle:
            pos: self.pos
            size: self.size
            radius: [dp(14)]
"""

    class Card(BoxLayout):
        pass

    class Button3D(Button):
        bg_color = ListProperty([0.20, 0.45, 0.85, 1])

    class QualityToggle(ToggleButtonBehavior, FloatLayout):
        bg_color = ListProperty([0.26, 0.26, 0.30, 1])
        label_text = StringProperty("")
        size_text = StringProperty("")

    class DownloadCard(BoxLayout):
        pass

    Builder.load_string(KV)
    SmallButton3D = Factory.SmallButton3D

    class YTDownloaderApp(App):
        font_path = FONT_PATH

        def build(self):
            self.title = ar("منزّل يوتيوب")
            self.picked_url = ""
            self.video_title = ""
            self.video_thumb = ""
            self.selected_quality_index = 6  # 720p
            self.storage_uri = load_storage_uri()
            self.quality_buttons = []
            self.download_widgets = {}
            # بيانات الجودات (بديلة للـ quality_data القديمة)
            self.sorted_audio = []
            self.video_map = {}
            self.video_map_high = {}
            self.is_playlist = False
            self.playlist_entries = []
            self.playlist_title = ""
            self._has_active = False

            root = TabbedPanel(do_default_tab=False)

            dl_tab = TabbedPanelItem(text=ar("تحميل"))
            dl_tab.font_name = FONT_PATH
            dl_tab.content = self._build_download_tab()
            root.add_widget(dl_tab)

            st_tab = TabbedPanelItem(text=ar("الإعدادات"))
            st_tab.font_name = FONT_PATH
            st_tab.content = self._build_settings_tab()
            root.add_widget(st_tab)

            def _fit_tab_width(*_):
                root.tab_width = root.width / 2.0
            root.bind(width=_fit_tab_width)
            Clock.schedule_once(_fit_tab_width)

            self._poll_event = Clock.schedule_interval(self._poll_downloads, 1.5)
            Clock.schedule_interval(self._refresh_logs, 3.0)
            return root

        def _build_download_tab(self):
            layout = BoxLayout(orientation="vertical", padding=dp(14), spacing=dp(16))

            self.btn_analyze = Button3D(
                text=ar("التقط الرابط وحلّل"), font_size="19sp",
                size_hint=(1, None), height=dp(56),
            )
            self.btn_analyze.bind(on_release=self._on_analyze_pressed)
            layout.add_widget(self.btn_analyze)

            self.analyze_card = Card(orientation="vertical", size_hint=(1, None))
            self.analyze_card.bind(minimum_height=self.analyze_card.setter("height"))

            header_row = BoxLayout(orientation="horizontal", size_hint=(1, None), height=dp(64), spacing=dp(10))
            self.thumb = AsyncImage(size_hint=(None, None), size=(0, 0), opacity=0)
            header_row.add_widget(self.thumb)
            self.title_label = fit_label(Label(
                text="", font_size="15sp", font_name=FONT_PATH,
                halign="right", valign="middle", shorten=True,
            ))
            header_row.add_widget(self.title_label)
            self.analyze_card.add_widget(header_row)

            self.playlist_row = BoxLayout(
                orientation="horizontal", size_hint=(1, None),
                height=0, spacing=dp(8), opacity=0,
            )
            lbl_from = fit_label(Label(
                text=ar("من فيديو رقم"), font_name=FONT_PATH, font_size="13sp",
                size_hint=(0.40, 1), halign="right", valign="middle"
            ))
            self.input_from = TextInput(
                text="1", multiline=False, input_filter="int", font_size="14sp",
                font_name=FONT_PATH, halign="center", size_hint=(0.20, 1),
                foreground_color=(1, 1, 1, 1),
                background_color=(0.10, 0.10, 0.13, 1),
                cursor_color=(1, 1, 1, 1)
            )
            lbl_to = fit_label(Label(
                text=ar("إلى رقم"), font_name=FONT_PATH, font_size="13sp",
                size_hint=(0.18, 1), halign="right", valign="middle"
            ))
            self.input_to = TextInput(
                text="1", multiline=False, input_filter="int", font_size="14sp",
                font_name=FONT_PATH, halign="center", size_hint=(0.22, 1),
                foreground_color=(1, 1, 1, 1),
                background_color=(0.10, 0.10, 0.13, 1),
                cursor_color=(1, 1, 1, 1)
            )
            self.playlist_row.add_widget(self.input_to)
            self.playlist_row.add_widget(lbl_to)
            self.playlist_row.add_widget(self.input_from)
            self.playlist_row.add_widget(lbl_from)
            self.analyze_card.add_widget(self.playlist_row)

            row_audio = BoxLayout(orientation="horizontal", size_hint=(1, None), height=dp(48), spacing=dp(8))
            row_v1 = BoxLayout(orientation="horizontal", size_hint=(1, None), height=dp(48), spacing=dp(8))
            row_v2 = BoxLayout(orientation="horizontal", size_hint=(1, None), height=dp(48), spacing=dp(8))
            rows_map = [row_audio, row_audio, row_v1, row_v1, row_v1, row_v2, row_v2, row_v2]

            self.quality_buttons = []
            for idx, (key, label) in enumerate(QUALITY_LABELS):
                btn = QualityToggle(group="quality_select")
                btn.quality_key = key
                btn.base_label = label
                btn.quality_index = idx
                btn.label_text = ar(label)
                btn.size_text = ""
                if idx == self.selected_quality_index:
                    btn.state = "down"
                btn.bind(on_release=self._on_quality_selected)
                self.quality_buttons.append(btn)
                rows_map[idx].add_widget(btn)

            self.analyze_card.add_widget(row_audio)
            self.analyze_card.add_widget(row_v1)
            self.analyze_card.add_widget(row_v2)

            self.btn_add_queue = Button3D(text=ar("أضف للتحميل"), size_hint=(1, None), height=dp(50))
            self.btn_add_queue.bg_color = [0.22, 0.62, 0.36, 1]
            self.btn_add_queue.bind(on_release=self._on_add_to_queue)
            self.analyze_card.add_widget(self.btn_add_queue)
            layout.add_widget(self.analyze_card)

            dl_header = BoxLayout(orientation="horizontal", size_hint=(1, None), height=dp(38), spacing=dp(8))
            btn_reset = SmallButton3D(text=ar("تصفير السجل"), size_hint=(0.38, 1))
            btn_reset.bg_color = [0.55, 0.30, 0.15, 1]
            btn_reset.bind(on_release=self._on_reset_downloads)
            dl_title = fit_label(Label(
                text=ar("التحميلات"), font_name=FONT_PATH, bold=True, font_size="16sp",
                size_hint=(0.62, 1), halign="right", valign="middle"
            ))
            dl_header.add_widget(btn_reset)
            dl_header.add_widget(dl_title)
            layout.add_widget(dl_header)

            scroll = ScrollView(size_hint=(1, 1))
            self.downloads_list = BoxLayout(orientation="vertical", spacing=dp(8), size_hint_y=None)
            self.downloads_list.bind(minimum_height=self.downloads_list.setter("height"))
            scroll.add_widget(self.downloads_list)
            layout.add_widget(scroll)

            self.status_label = fit_label(Label(
                text="", font_size="13sp", font_name=FONT_PATH,
                size_hint=(1, None), height=dp(24), halign="right", valign="middle"
            ))
            layout.add_widget(self.status_label)
            return layout

        def _build_settings_tab(self):
            scroll = ScrollView(size_hint=(1, 1))
            layout = BoxLayout(orientation="vertical", padding=dp(14), spacing=dp(14), size_hint_y=None)
            layout.bind(minimum_height=layout.setter("height"))

            ff_card = Card(orientation="vertical", size_hint=(1, None), height=dp(90))
            ff_card.add_widget(fit_label(Label(
                text=ar("محرك الدمج (FFmpeg)"), font_name=FONT_PATH, bold=True,
                font_size="15sp", size_hint=(1, None), height=dp(24),
                halign="right", valign="middle"
            )))
            self.lbl_ffmpeg = fit_label(Label(
                text=ar("جاري الفحص..."), font_name=FONT_PATH, font_size="12sp",
                size_hint=(1, None), height=dp(40), halign="right", valign="middle",
                color=(0.6, 0.9, 0.6, 1)
            ))
            ff_card.add_widget(self.lbl_ffmpeg)
            layout.add_widget(ff_card)
            Clock.schedule_once(lambda dt: self._check_ffmpeg(), 0.5)

            st_card = Card(orientation="vertical", size_hint=(1, None), height=dp(110))
            st_card.add_widget(fit_label(Label(
                text=ar("مجلد التخزين"), font_name=FONT_PATH, bold=True,
                font_size="15sp", size_hint=(1, None), height=dp(24),
                halign="right", valign="middle"
            )))
            self.btn_choose_folder = Button3D(
                text=ar("تم اختيار المجلد") if self.storage_uri else ar("اختر مجلد التخزين"),
                size_hint=(1, None), height=dp(48),
            )
            self.btn_choose_folder.bind(on_release=self._on_choose_folder)
            st_card.add_widget(self.btn_choose_folder)
            layout.add_widget(st_card)

            ap_card = Card(orientation="horizontal", size_hint=(1, None), height=dp(70))
            ap_lbl = fit_label(Label(
                text=ar("دمج الفيديو مع أقل جودة صوت (لتوفير البيانات)"),
                font_name=FONT_PATH, font_size="14sp",
                halign="right", valign="middle", size_hint=(0.8, 1),
            ))
            s = read_settings()
            self.switch_low_audio = Switch(
                active=bool(s.get("pair_low_audio", True)), size_hint=(0.20, 1),
            )
            self.switch_low_audio.bind(active=self._on_pair_audio_changed)
            ap_card.add_widget(self.switch_low_audio)
            ap_card.add_widget(ap_lbl)
            layout.add_widget(ap_card)

            cc_card = Card(orientation="vertical", size_hint=(1, None), height=dp(110))
            cc_card.add_widget(fit_label(Label(
                text=ar("عدد التحميلات في نفس الوقت"), font_name=FONT_PATH, bold=True,
                font_size="15sp", size_hint=(1, None), height=dp(24),
                halign="right", valign="middle"
            )))
            self.spinner_concurrency = Spinner(
                text=str(s.get("max_concurrent", 2)),
                values=("1", "2", "3", "4"),
                font_name=FONT_PATH, size_hint=(1, None), height=dp(48),
            )
            self.spinner_concurrency.bind(text=self._on_concurrency_changed)
            cc_card.add_widget(self.spinner_concurrency)
            layout.add_widget(cc_card)

            dl_card = Card(orientation="vertical", size_hint=(1, None), height=dp(240))
            dl_card.add_widget(fit_label(Label(
                text=ar("سجل التحميلات"), font_name=FONT_PATH, bold=True,
                font_size="15sp", size_hint=(1, None), height=dp(24),
                halign="right", valign="middle"
            )))
            dl_scroll = ScrollView(size_hint=(1, 1))
            self.dl_log_label = Label(
                text="", font_size="12sp", font_name=FONT_PATH,
                size_hint_y=None, halign="right", valign="top"
            )
            self.dl_log_label.bind(
                width=lambda i, w: setattr(i, "text_size", (w, None)),
                texture_size=lambda i, sz: setattr(i, "height", sz[1]),
            )
            dl_scroll.add_widget(self.dl_log_label)
            dl_card.add_widget(dl_scroll)
            btn_copy_dl = SmallButton3D(text=ar("نسخ سجل التحميلات"), size_hint=(1, None), height=dp(40))
            btn_copy_dl.bind(on_release=lambda *_: self._copy_log(DOWNLOAD_LOG_FILE))
            dl_card.add_widget(btn_copy_dl)
            layout.add_widget(dl_card)

            er_card = Card(orientation="vertical", size_hint=(1, None), height=dp(240))
            er_card.add_widget(fit_label(Label(
                text=ar("سجل الأخطاء"), font_name=FONT_PATH, bold=True,
                font_size="15sp", size_hint=(1, None), height=dp(24),
                halign="right", valign="middle"
            )))
            er_scroll = ScrollView(size_hint=(1, 1))
            self.err_log_label = Label(
                text="", font_size="12sp", font_name=FONT_PATH,
                size_hint_y=None, halign="right", valign="top"
            )
            self.err_log_label.bind(
                width=lambda i, w: setattr(i, "text_size", (w, None)),
                texture_size=lambda i, sz: setattr(i, "height", sz[1]),
            )
            er_scroll.add_widget(self.err_log_label)
            er_card.add_widget(er_scroll)
            btn_copy_er = SmallButton3D(text=ar("نسخ سجل الأخطاء"), size_hint=(1, None), height=dp(40))
            btn_copy_er.bind(on_release=lambda *_: self._copy_log(ERROR_LOG_FILE))
            er_card.add_widget(btn_copy_er)
            layout.add_widget(er_card)

            scroll.add_widget(layout)
            return scroll

        def _check_ffmpeg(self):
            def _do():
                p = ffmpeg_resolve()
                v = ffmpeg_version() if p else None
                Clock.schedule_once(lambda dt: self._set_ffmpeg_label(p, v))
            threading.Thread(target=_do, daemon=True).start()

        def _set_ffmpeg_label(self, path, ver):
            if path:
                src = "مدمج" if "assets" in (path or "") else "متاح"
                txt = f"✓ ffmpeg {src}"
                if ver:
                    txt += f"\n{ver.split(chr(10))[0][:50]}"
                self.lbl_ffmpeg.text = ar(txt)
                self.lbl_ffmpeg.color = (0.4, 0.9, 0.4, 1)
            else:
                self.lbl_ffmpeg.text = ar("✗ ffmpeg غير موجود\nثبّت imageio-ffmpeg أو ضع الملف في assets/")
                self.lbl_ffmpeg.color = (0.9, 0.4, 0.4, 1)

        def _on_concurrency_changed(self, spinner, value):
            s = read_settings()
            try:
                s["max_concurrent"] = int(value)
            except ValueError:
                s["max_concurrent"] = 2
            write_settings(s)

        def _on_pair_audio_changed(self, switch, value):
            s = read_settings()
            s["pair_low_audio"] = bool(value)
            write_settings(s)

        def _copy_log(self, path):
            try:
                Clipboard.copy(read_log(path))
                self._set_status("تم نسخ السجل")
            except Exception:
                self._set_status("تعذر النسخ")

        def _refresh_logs(self, dt):
            try:
                if hasattr(self, "dl_log_label"):
                    c = read_log(DOWNLOAD_LOG_FILE)
                    self.dl_log_label.text = ar(c) if c else ar("(فارغ)")
                if hasattr(self, "err_log_label"):
                    c = read_log(ERROR_LOG_FILE)
                    self.err_log_label.text = ar(c) if c else ar("(فارغ)")
            except Exception:
                pass

        def _on_reset_downloads(self, *_):
            try:
                queue = read_json(QUEUE_FILE, [])
                remaining = [j for j in queue if j.get("status") in ("queued", "downloading", "paused")]
                write_json(QUEUE_FILE, remaining)
                ids = {j["id"] for j in remaining}
                sd = read_json(STATUS_FILE, {})
                write_json(STATUS_FILE, {k: v for k, v in sd.items() if k in ids})
                clear_log(DOWNLOAD_LOG_FILE)
                clear_log(ERROR_LOG_FILE)
                self._set_status("تم تنظيف السجل")
            except Exception as e:
                self._set_status(f"خطأ: {e}")

        def _on_quality_selected(self, btn):
            self.selected_quality_index = btn.quality_index

        def _on_analyze_pressed(self, *_):
            threading.Thread(target=self._analyze, daemon=True).start()

        def _analyze(self):
            logger.info("=" * 60)
            logger.info("Starting analysis...")
            try:
                Clock.schedule_once(lambda dt: self._set_btn(self.btn_analyze, ar("جاري التحليل...")))

                url = ""
                try:
                    url = Clipboard.paste()
                except Exception as e:
                    logger.error("Clipboard failed: %s", e)
                    url = ""

                if not url or not url.strip():
                    Clock.schedule_once(lambda dt: self._set_status("الحافظة فارغة - انسخ رابط أولاً"))
                    Clock.schedule_once(lambda dt: self._set_btn(self.btn_analyze, ar("التقط الرابط وحلّل")))
                    return

                url = url.strip().split()[0]

                if not (url.startswith("http://") or url.startswith("https://")):
                    Clock.schedule_once(lambda dt: self._set_status("الرابط مش صالح - لازم يبدأ بـ http"))
                    Clock.schedule_once(lambda dt: self._set_btn(self.btn_analyze, ar("التقط الرابط وحلّل")))
                    return

                self.picked_url = url
                logger.info("Analyzing URL: %s", url[:80])

                # فحص أولي: قائمة أم فيديو مفرد
                probe_opts = _base_ydl_opts()
                probe_opts.update({
                    "extract_flat": "in_playlist",
                    "noplaylist": False,
                })

                try:
                    with YoutubeDL(probe_opts) as ydl:
                        probe = ydl.extract_info(url, download=False)
                except Exception as e:
                    logger.error("✗ probe extract_info failed: %s", e)
                    raise

                if not probe:
                    Clock.schedule_once(lambda dt: self._set_status("فشل تحليل الرابط"))
                    Clock.schedule_once(lambda dt: self._set_btn(self.btn_analyze, ar("التقط الرابط وحلّل")))
                    return

                if probe.get("_type") == "playlist" or "entries" in probe:
                    self.is_playlist = True
                    self.playlist_entries = [e for e in (probe.get("entries") or []) if e]
                    self.playlist_title = probe.get("title") or "Playlist"
                    self.video_title = self.playlist_title
                    thumbs = probe.get("thumbnails") or []
                    self.video_thumb = thumbs[-1].get("url", "") if thumbs else ""
                    self.sorted_audio = []
                    self.video_map = {}
                    self.video_map_high = {}
                    logger.info("Playlist: %d videos", len(self.playlist_entries))
                else:
                    self.is_playlist = False
                    full_opts = _base_ydl_opts()
                    full_opts["noplaylist"] = True
                    try:
                        with YoutubeDL(full_opts) as y2:
                            info = y2.extract_info(url, download=False)
                    except Exception as e:
                        logger.error("✗ Full extract failed: %s", e)
                        raise

                    self.video_title = info.get("title", "")
                    self.video_thumb = info.get("thumbnail", "")
                    sa, vmap, vmap_high = analyze_formats(info)
                    self.sorted_audio = sa
                    self.video_map = vmap
                    self.video_map_high = vmap_high
                    logger.info("✓ Formats: %d audio, %d video qualities",
                                len(sa), len(vmap))

                Clock.schedule_once(lambda dt: self._on_analyze_done())
                logger.info("✓ Analysis completed!")

            except Exception as e:
                logger.error("✗ ANALYSIS FAILED: %s", e)
                logger.error("Traceback:", exc_info=True)
                error_msg = str(e)[:100]
                Clock.schedule_once(lambda dt: self._set_status(f"خطأ: {error_msg}"))
                Clock.schedule_once(lambda dt: self._set_btn(self.btn_analyze, ar("التقط الرابط وحلّل")))

        def _on_analyze_done(self):
            self.title_label.text = ar(self.video_title)
            if self.video_thumb:
                self.thumb.source = self.video_thumb
                self.thumb.size = (dp(64), dp(64))
                self.thumb.opacity = 1
            else:
                self.thumb.size = (0, 0)
                self.thumb.opacity = 0

            s = read_settings()
            use_low = bool(s.get("pair_low_audio", True))

            for btn in self.quality_buttons:
                key = btn.quality_key
                if not self.is_playlist:
                    # احسب الحجم والتوفر من البيانات الجديدة
                    _, size = build_format_selector(key, self.sorted_audio, self.video_map, use_low)
                    available = self._is_quality_available(key)
                    if available:
                        btn.size_text = f"{_fmt_mb(size)} MB" if size else ""
                        btn.disabled = False
                    else:
                        btn.size_text = ar("غير متاح")
                        btn.disabled = True
                else:
                    btn.size_text = ""
                    btn.disabled = False

            if self.is_playlist:
                count = len(self.playlist_entries)
                self.playlist_row.height = dp(46)
                self.playlist_row.opacity = 1
                self.input_from.text = "1"
                self.input_to.text = str(count) if count else "1"
                self.btn_add_queue.text = ar("حمّل النطاق المحدد")
                self._set_status(f"قائمة تشغيل بها {count} فيديو")
            else:
                self.playlist_row.height = 0
                self.playlist_row.opacity = 0
                self.btn_add_queue.text = ar("أضف للتحميل")

            self._set_btn(self.btn_analyze, ar("التقط الرابط وحلّل"))

        def _is_quality_available(self, key):
            """تحقق من توفر الجودة بناءً على البيانات المحلّلة."""
            if key in ("audio_low", "audio_high"):
                return bool(self.sorted_audio)
            return key in self.video_map

        def _on_add_to_queue(self, *_):
            if not self.picked_url or not self.video_title:
                self._set_status("حلّل رابط أولًا")
                return
            if platform == "android" and not self._has_storage_perm():
                self._set_status("من فضلك اسمح بالوصول للملفات")
                self._req_storage_perm()
                return
            try:
                if self.is_playlist:
                    self._enqueue_playlist()
                else:
                    self._enqueue_single()
            except Exception as e:
                self._set_status(f"خطأ: {e}")

        def _enqueue_single(self):
            key, label = QUALITY_LABELS[self.selected_quality_index]
            if not self._is_quality_available(key):
                self._set_status("الجودة دي مش متاحة")
                return

            s = read_settings()
            use_low = bool(s.get("pair_low_audio", True))
            fmt, size = build_format_selector(key, self.sorted_audio, self.video_map, use_low)

            active = ("queued", "downloading", "paused", "merging", "saving")
            queue = read_json(QUEUE_FILE, [])
            for j in queue:
                if j.get("url") == self.picked_url and j.get("quality_key") == key and j.get("status") in active:
                    self._set_status("الفيديو موجود بالفعل في التحميلات")
                    return

            job = {
                "id": uuid.uuid4().hex, "url": self.picked_url, "title": self.video_title,
                "thumbnail": self.video_thumb, "quality_key": key, "format_selector": fmt,
                "storage_uri": self.storage_uri, "playlist_name": "", "status": "queued",
            }
            queue.append(job)
            write_json(QUEUE_FILE, queue)
            append_download_log(f"{job['title']} ({label}, {_fmt_mb(size)} MB) - queued")
            self._start_service()
            self._set_status("تمت الإضافة للتحميل")

        def _enqueue_playlist(self):
            count = len(self.playlist_entries)
            if count == 0:
                self._set_status("مفيش فيديوهات في القائمة")
                return
            try:
                fi = int(self.input_from.text or "1")
                ti = int(self.input_to.text or str(count))
            except ValueError:
                self._set_status("اكتب أرقام صحيحة")
                return

            fi = max(1, min(fi, count))
            ti = max(1, min(ti, count))
            if fi > ti:
                fi, ti = ti, fi

            key, label = QUALITY_LABELS[self.selected_quality_index]
            folder = sanitize_name(self.playlist_title)

            active = ("queued", "downloading", "paused", "merging", "saving")
            queue = read_json(QUEUE_FILE, [])
            existing = {(j.get("url"), j.get("quality_key")) for j in queue if j.get("status") in active}

            added = skipped = 0
            for i in range(fi - 1, ti):
                entry = self.playlist_entries[i]
                vid = entry.get("id")
                vurl = entry.get("url") or (f"https://www.youtube.com/watch?v={vid}" if vid else "")
                if not vurl:
                    continue
                if (vurl, key) in existing:
                    skipped += 1
                    continue
                title = entry.get("title") or f"video {i + 1}"
                thumbs = entry.get("thumbnails") or []
                thumb = thumbs[-1].get("url", "") if thumbs else ""

                # لعناصر القائمة نترك format_selector فارغاً ليُبنى fallback مرن وقت التحميل
                job = {
                    "id": uuid.uuid4().hex, "url": vurl, "title": title, "thumbnail": thumb,
                    "quality_key": key, "format_selector": "",
                    "storage_uri": self.storage_uri, "playlist_name": folder, "status": "queued",
                }
                queue.append(job)
                existing.add((vurl, key))
                added += 1

            write_json(QUEUE_FILE, queue)
            append_download_log(f"{self.playlist_title}: {added} videos ({label}) - queued")
            self._start_service()
            msg = f"تمت إضافة {added} فيديو من القائمة"
            if skipped:
                msg += f" (تم تخطي {skipped})"
            self._set_status(msg)

        def _has_storage_perm(self):
            if self.storage_uri:
                return True
            try:
                from jnius import autoclass
                return bool(autoclass("android.os.Environment").isExternalStorageManager())
            except Exception:
                return True

        def _req_storage_perm(self):
            try:
                from jnius import autoclass
                Intent = autoclass("android.content.Intent")
                Settings = autoclass("android.provider.Settings")
                Uri = autoclass("android.net.Uri")
                PythonActivity = autoclass("org.kivy.android.PythonActivity")
                activity = PythonActivity.mActivity
                intent = Intent(Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION)
                intent.setData(Uri.parse("package:" + activity.getPackageName()))
                activity.startActivity(intent)
            except Exception as e:
                self._set_status(f"خطأ: {e}")

        def _on_choose_folder(self, *_):
            if platform != "android":
                self._set_status("اختيار المجلد شغال بس على أندرويد")
                return
            try:
                from jnius import autoclass
                from android import activity
                Intent = autoclass("android.content.Intent")
                PythonActivity = autoclass("org.kivy.android.PythonActivity")
                act = PythonActivity.mActivity
                intent = Intent(Intent.ACTION_OPEN_DOCUMENT_TREE)
                intent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
                intent.addFlags(Intent.FLAG_GRANT_WRITE_URI_PERMISSION)
                intent.addFlags(Intent.FLAG_GRANT_PERSISTABLE_URI_PERMISSION)
                activity.bind(on_activity_result=self._on_folder_picked)
                act.startActivityForResult(intent, 4321)
            except Exception as e:
                self._set_status(f"خطأ: {e}")

        def _on_folder_picked(self, req, res, intent):
            if req != 4321:
                return
            try:
                from jnius import autoclass
                Activity = autoclass("android.app.Activity")
                if res != Activity.RESULT_OK or intent is None:
                    Clock.schedule_once(lambda dt: self._set_status("تم إلغاء اختيار المجلد"))
                    return
                Intent = autoclass("android.content.Intent")
                PythonActivity = autoclass("org.kivy.android.PythonActivity")
                act = PythonActivity.mActivity
                uri = intent.getData()
                flags = intent.getFlags() & (Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_GRANT_WRITE_URI_PERMISSION)
                act.getContentResolver().takePersistableUriPermission(uri, flags)
                uri_str = uri.toString()
                self.storage_uri = uri_str
                save_storage_uri(uri_str)
                Clock.schedule_once(lambda dt: self._set_btn(self.btn_choose_folder, ar("تم اختيار المجلد")))
                Clock.schedule_once(lambda dt: self._set_status("تم حفظ مجلد التخزين"))
            except Exception as e:
                Clock.schedule_once(lambda dt: self._set_status(f"خطأ: {e}"))

        def _start_service(self):
            if platform != "android":
                self._set_status("الخدمة شغالة بس على أندرويد")
                return
            try:
                from jnius import autoclass
                PythonActivity = autoclass("org.kivy.android.PythonActivity")
                activity = PythonActivity.mActivity
                ServiceClass = autoclass("{}.ServiceYtservice".format(activity.getPackageName()))
                ServiceClass.start(activity, "{}")
            except Exception as e:
                self._set_status(f"خطأ في تشغيل الخدمة: {e}")

        _STATUS_WORDS = {
            "queued": "في الانتظار", "downloading": "جاري التحميل",
            "merging": "جاري الدمج", "saving": "جاري الحفظ",
            "finished": "تم بنجاح", "paused": "متوقف مؤقتًا",
            "cancelled": "ملغي", "error": "خطأ",
        }

        def _poll_downloads(self, dt):
            try:
                queue = read_json(QUEUE_FILE, [])
                sd = read_json(STATUS_FILE, {})
                current_ids = set()
                has_active = False

                for job in queue:
                    jid = job["id"]
                    current_ids.add(jid)
                    info = sd.get(jid, {})
                    status = info.get("status", job.get("status", "queued"))
                    percent = info.get("percent", 0.0)
                    if status in ("queued", "downloading", "merging", "saving", "paused"):
                        has_active = True
                    if jid not in self.download_widgets:
                        self._create_card(job, status, percent, info)
                    else:
                        self._update_card(jid, job, status, percent, info)

                for jid in list(self.download_widgets.keys()):
                    if jid not in current_ids:
                        w = self.download_widgets.pop(jid)
                        self.downloads_list.remove_widget(w["card"])

                if has_active != self._has_active:
                    self._has_active = has_active
                    if self._poll_event:
                        self._poll_event.cancel()
                        self._poll_event = Clock.schedule_interval(
                            self._poll_downloads, 1.5 if has_active else 5.0
                        )
            except Exception as e:
                logger.debug("poll error: %s", e)

        def _create_card(self, job, status, percent, info):
            jid = job["id"]
            card = DownloadCard()

            top = BoxLayout(orientation="horizontal", size_hint=(1, None), height=dp(46), spacing=dp(8))
            thumb = AsyncImage(source=job.get("thumbnail", ""), size_hint=(None, None), size=(dp(40), dp(40)))
            top.add_widget(thumb)
            top.add_widget(fit_label(Label(
                text=ar(job.get("title", "")), font_name=FONT_PATH, font_size="13sp",
                halign="right", valign="middle", shorten=True,
            )))
            card.add_widget(top)

            progress = ProgressBar(max=100, value=percent, size_hint=(1, None), height=dp(22))
            card.add_widget(progress)

            stxt = self._fmt_status(status, percent, info)
            slbl = fit_label(Label(
                text=stxt, font_size="11sp", font_name=FONT_PATH,
                size_hint=(1, None), height=dp(18), halign="right", valign="middle",
            ))
            card.add_widget(slbl)

            brow = BoxLayout(orientation="horizontal", size_hint=(1, None), height=dp(34), spacing=dp(6))
            bp = SmallButton3D(text=ar("إيقاف مؤقت"), size_hint=(1, 1))
            bp.bind(on_release=lambda *_: self._on_pause(jid))
            bc = SmallButton3D(text=ar("إلغاء"), size_hint=(1, 1))
            bc.bg_color = [0.65, 0.20, 0.20, 1]
            bc.bind(on_release=lambda *_: self._on_cancel(jid))
            bo = SmallButton3D(text=ar("فتح"), size_hint=(1, 1), disabled=True)
            bo.bind(on_release=lambda *_: self._on_open(jid))
            brow.add_widget(bo)
            brow.add_widget(bc)
            brow.add_widget(bp)
            card.add_widget(brow)

            self.downloads_list.add_widget(card)
            self.download_widgets[jid] = {
                "card": card, "progress": progress, "status_lbl": slbl,
                "btn_pause": bp, "btn_cancel": bc, "btn_open": bo,
            }
            self._apply_state(jid, status)

        def _update_card(self, jid, job, status, percent, info):
            w = self.download_widgets[jid]
            w["progress"].value = percent
            w["status_lbl"].text = self._fmt_status(status, percent, info)
            self._apply_state(jid, status)

        def _fmt_status(self, status, percent, info):
            if status == "error" and info.get("error"):
                return ar(f"خطأ: {info['error'][:50]}")
            return ar(f"{self._STATUS_WORDS.get(status, status)} - {percent:.1f}%")

        def _apply_state(self, jid, status):
            w = self.download_widgets[jid]
            bp = w["btn_pause"]
            if status == "downloading":
                bp.text = ar("إيقاف مؤقت")
                bp.disabled = False
            elif status == "paused":
                bp.text = ar("استكمال")
                bp.disabled = False
            elif status == "error":
                bp.text = ar("إعادة")
                bp.disabled = False
            else:
                bp.disabled = True
            w["btn_cancel"].disabled = status in ("finished", "cancelled")
            w["btn_open"].disabled = status != "finished"

        def _send_control(self, jid, action):
            c = read_json(CONTROL_FILE, {})
            c[jid] = action
            write_json(CONTROL_FILE, c)

        def _on_pause(self, jid):
            sd = read_json(STATUS_FILE, {})
            st = sd.get(jid, {}).get("status")
            if st in ("paused", "error"):
                # استئناف / إعادة المحاولة
                queue = read_json(QUEUE_FILE, [])
                for j in queue:
                    if j["id"] == jid:
                        j["status"] = "queued"
                write_json(QUEUE_FILE, queue)
                update_job_progress(jid, 0, status="queued")
                self._start_service()
            else:
                self._send_control(jid, "pause")

        def _on_cancel(self, jid):
            self._send_control(jid, "cancel")
            update_job_status(jid, "cancelled")
            update_job_progress(jid, 0, status="cancelled")

        def _on_open(self, jid):
            sd = read_json(STATUS_FILE, {})
            info = sd.get(jid, {})
            uri = info.get("saved_uri", "")
            path = info.get("saved_path", "")
            if uri:
                self._open_uri(uri)
            elif path:
                self._set_status(f"محفوظ في: {path}")
            else:
                self._set_status("الملف لسه مش جاهز")

        def _open_uri(self, uri_str):
            try:
                from jnius import autoclass
                Intent = autoclass("android.content.Intent")
                Uri = autoclass("android.net.Uri")
                PythonActivity = autoclass("org.kivy.android.PythonActivity")
                activity = PythonActivity.mActivity
                mime = "audio/*" if uri_str.lower().endswith((".mp3", ".m4a", ".opus", ".aac")) else "video/*"
                intent = Intent(Intent.ACTION_VIEW)
                intent.setDataAndType(Uri.parse(uri_str), mime)
                intent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
                activity.startActivity(intent)
            except Exception as e:
                self._set_status(f"تعذر فتح الملف: {e}")

        def _set_status(self, text):
            self.status_label.text = ar(text)

        def _set_btn(self, widget, text):
            widget.text = text


# ================================================================
# Entry Point
# ================================================================

def main():
    if IS_SERVICE:
        logger.info("Running as service...")
        run_service()
    else:
        logger.info("Running as app...")
        YTDownloaderApp().run()


if __name__ == "__main__":
    if IS_SERVICE:
        _create_notification_channel()
        run_service()
    else:
        main()
