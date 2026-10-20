# -*- coding: utf-8 -*-
"""
Post Studio — استودیوی پست هوشمند (ربات مادر + مینیاپ)
Stack (مثل الگوی app-6.py): Flask + MongoDB(pymongo / mongomock) + مینیاپ تکفایلی index.html

- ربات: فقط /start و دکمهی باز کردن مینیاپ
- مینیاپ: کاربر توضیح میده، هوش مصنوعی پرامپت حرفهای + کپشن + هشتگ میسازه
- تصویر: زنجیرهی سرویسهای رایگان (Pollinations → Cloudflare Workers AI → AI Horde) با جایگزین خودکار
- ویدیو: چند صحنهی AI + حرکت سینمایی + ترنزیشن + صدای گوینده (edge-tts) با ffmpeg
  (اختیاری: ویدیوی AI واقعی از Pollinations اگه کلید داشته باشی)
- خروجیها توی MongoDB ذخیره میشن و با لینک امضاشده نمایش داده میشن
- (جدید) مدلهای لوکال/متنباز بدون API و نامحدود: SD-Turbo / SDXL-Turbo / FLUX برای تصویر، پارالکس سهبعدی (Depth-Anything) و LTX-Video برای ویدیو
  با LOCAL_MODELS=1 فعال میشن و کاربر داخل مینیاپ مدل رو انتخاب میکنه؛ بدون اون، رفتار قبلی دقیقاً همونه
"""
import os, re, io, json, time, hmac, hashlib, secrets, logging, threading, random, shutil, subprocess, tempfile, asyncio, base64
from urllib.parse import parse_qsl, quote
from datetime import datetime, timezone

import requests
from flask import Flask, request, jsonify, send_from_directory, send_file
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError
from bson import ObjectId, Binary
from bson.errors import InvalidId
from PIL import Image, ImageEnhance, ImageFilter

# ───────────────────────── تنظیمات (Environment) ─────────────────────────
MOTHER_TOKEN = os.environ["MOTHER_TOKEN"]                    # توکن ربات
MONGO_URI    = os.environ.get("MONGO_URI", "").strip()       # خالی = حافظهی موقت (ذخیره نمیشه)
BASE_URL     = os.environ["BASE_URL"].rstrip("/")            # مثلا https://yourapp.onrender.com
SECRET_KEY   = os.environ.get("SECRET_KEY", MOTHER_TOKEN)

# مدل متنی (اختیاری) برای ساخت پرامپت/کپشن؛ هر API سازگار با OpenAI. اگه خالی باشه از Pollinations Text رایگان استفاده میشه
AI_BASE_URL  = re.sub(r"/chat/completions/?$", "", os.environ.get("AI_BASE_URL", "").strip().rstrip("/"))
AI_API_KEY   = os.environ.get("AI_API_KEY", "").strip()
AI_MODEL     = os.environ.get("AI_MODEL", "").strip()

# منابع رایگان تصویر/ویدیو (همه اختیاری؛ Pollinations و Horde بدون کلید هم کار میکنن)
POLLI_KEY         = os.environ.get("POLLI_KEY", "").strip()          # کلید رایگان از enter.pollinations.ai (بدون واترمارک + مدلها/ویدیو)
POLLI_IMAGE_MODEL = os.environ.get("POLLI_IMAGE_MODEL", "flux").strip()
POLLI_VIDEO_MODEL = os.environ.get("POLLI_VIDEO_MODEL", "").strip()
ENABLE_AI_VIDEO   = os.environ.get("ENABLE_AI_VIDEO", "") == "1"     # ویدیوی AI واقعی (نیاز به POLLI_KEY)؛ اگه نشد خودکار میره سراغ حالت چندصحنهای
CF_ACCOUNT_ID     = os.environ.get("CF_ACCOUNT_ID", "").strip()      # Cloudflare Workers AI (سهمیهی رایگان روزانه)
CF_API_TOKEN      = os.environ.get("CF_API_TOKEN", "").strip()
HORDE_KEY         = os.environ.get("HORDE_KEY", "0000000000").strip()  # AI Horde (ناشناس رایگان)
HF_TOKEN          = os.environ.get("HF_TOKEN", "").strip()           # Hugging Face (توکن رایگان؛ FLUX.1-schnell)
TOGETHER_KEY      = os.environ.get("TOGETHER_KEY", "").strip()       # Together AI (اگه مدل رایگانشون فعال بود)
TOGETHER_MODEL    = os.environ.get("TOGETHER_MODEL", "black-forest-labs/FLUX.1-schnell-Free").strip()
IMAGE_PROVIDERS   = [p.strip() for p in os.environ.get("IMAGE_PROVIDERS", "pollinations,cloudflare,huggingface,together,horde").split(",") if p.strip()]

DAILY_IMAGES = int(os.environ.get("DAILY_IMAGES", "12"))     # تعداد تصویر در روز برای هر کاربر
DAILY_VIDEOS = int(os.environ.get("DAILY_VIDEOS", "3"))      # تعداد ویدیو در روز برای هر کاربر
KEEP_PER_USER = int(os.environ.get("KEEP_PER_USER", "30"))   # سقف آیتمهای گالری هر کاربر
MEDIA_TTL_DAYS = int(os.environ.get("MEDIA_TTL_DAYS", "14"))
MAX_JOBS      = int(os.environ.get("MAX_JOBS", "2"))         # ساخت همزمان در کل سرور
DEBUG         = os.environ.get("DEBUG", "") == "1"
MAX_PROMPT    = 800

# ── مدلهای لوکال (اختیاری؛ پیشفرض خاموش) ──
LOCAL_ENABLED  = os.environ.get("LOCAL_MODELS", "") == "1"                 # روشن کردن موتور لوکال (نیاز به requirements-local.txt)
LOCAL_DEVICE   = os.environ.get("LOCAL_DEVICE", "auto").strip().lower()    # auto | cuda | mps | cpu
LOCAL_CACHE    = os.environ.get("MODEL_CACHE_DIR", "").strip()             # مسیر ذخیرهی مدلها (روی Render بذار روی Persistent Disk مثل /var/data/models)
LOCAL_FALLBACK = os.environ.get("LOCAL_FALLBACK", "1") != "0"              # اگه مدل لوکال شکست خورد → خودکار میره سراغ سرویسهای آنلاین
LOCAL_PRELOAD  = os.environ.get("LOCAL_PRELOAD", "").strip()               # مثلا sd-turbo؛ هنگام استارت دانلود و لود میشه تا اولین کاربر منتظر نمونه
LTX_FRAMES     = int(os.environ.get("LTX_FRAMES", "65"))                   # تعداد فریم هر کلیپ LTX (مضرب ۸ بعلاوه ۱) ~ ۲.۷ ثانیه
LTX_STEPS      = int(os.environ.get("LTX_STEPS", "30"))
DEPTH_REPO     = os.environ.get("DEPTH_MODEL", "depth-anything/Depth-Anything-V2-Small-hf").strip()
if LOCAL_CACHE:
    os.environ.setdefault("HF_HOME", LOCAL_CACHE)
JOB_STALE_SEC = int(os.environ.get("JOB_STALE_SEC", "2400" if LOCAL_ENABLED else "900"))

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("studio")

app = Flask(__name__)
if MONGO_URI:
    db = MongoClient(MONGO_URI, serverSelectionTimeoutMS=8000).get_database("poststudio")
else:
    import mongomock
    db = mongomock.MongoClient().get_database("poststudio")
    log.warning("MONGO_URI تنظیم نشده؛ از حافظهی موقت استفاده میشه و با هر ریاستارت همهچی پاک میشه")
db.media.create_index("owner")
for _coll, _days in ((db.media, MEDIA_TTL_DAYS), (db.jobs, 2)):
    try:
        _coll.create_index("created", expireAfterSeconds=_days * 86400)
    except Exception:
        log.warning("TTL index نساخته شد", exc_info=True)

MOTHER_SECRET = hashlib.sha256(("mother" + SECRET_KEY).encode()).hexdigest()[:32]
MEDIA_SECRET = hashlib.sha256(("media" + SECRET_KEY).encode()).digest()
GEN_SEM = threading.Semaphore(MAX_JOBS)
FFMPEG_SEM = threading.Semaphore(1)


class UserError(Exception):
    """خطایی که پیامش مستقیم به کاربر نشون داده میشه"""


def now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def iso(d):
    return d.isoformat() + "Z"


# ───────────────────────── ثابتها: فرمتها و سبکها ─────────────────────────
# gen: اندازهی تولید، horde: اندازهی مضرب ۶۴، out: خروجی نهایی تصویر، vid: اندازهی ویدیو
FORMATS = {
    "story":    {"label": "استوری و ریلز", "ratio": "9:16", "gen": (720, 1280),  "horde": (576, 1024), "out": (1080, 1920), "vid": (720, 1280)},
    "portrait": {"label": "پست اینستاگرام", "ratio": "4:5", "gen": (896, 1120),  "horde": (768, 960),  "out": (1080, 1350), "vid": (720, 900)},
    "square":   {"label": "مربع",          "ratio": "1:1",  "gen": (1024, 1024), "horde": (768, 768),  "out": (1080, 1080), "vid": (720, 720)},
    "wide":     {"label": "افقی و یوتیوب", "ratio": "16:9", "gen": (1280, 720),  "horde": (1024, 576), "out": (1920, 1080), "vid": (1280, 720)},
}

STYLES = {
    "auto":      ("✨", "خودکار", ""),
    "cinematic": ("🎬", "سینمایی", "cinematic film still, anamorphic lens, moody teal and orange color grade, volumetric light, shallow depth of field, subtle film grain"),
    "photo":     ("📷", "عکس واقعی", "ultra realistic professional photography, 85mm lens, soft natural light, true-to-life textures, high dynamic range"),
    "luxury":    ("💎", "لوکس و محصول", "luxury commercial product photography, glossy studio lighting, reflective surface, premium minimal set, crisp highlights"),
    "neon":      ("🌆", "نئون و سایبر", "neon cyberpunk night scene, glowing magenta and cyan lights, wet reflective streets, futuristic atmosphere"),
    "3d":        ("🧸", "سهبعدی", "stylized 3D render, soft materials, smooth rounded shapes, vibrant colors, soft global illumination, octane render"),
    "flat":      ("🎨", "ایلاستریشن", "modern flat vector illustration, bold shapes, limited harmonious color palette, clean lines, editorial style"),
    "anime":     ("🌸", "انیمه", "high quality anime key visual, detailed painted background, soft cel shading, expressive lighting"),
    "minimal":   ("◻", "مینیمال", "minimalist composition, generous negative space, soft pastel palette, clean geometry, calm modern aesthetic"),
    "persian":   ("🏺", "ایرانی", "Persian art inspired, Iranian architecture and ornamental patterns, turquoise and gold palette, miniature painting influence, elegant details"),
}

QUALITY_TAIL = ("professional social media visual, scroll-stopping, strong focal point, balanced composition, sharp focus, rich detail, "
                "clean subject separation, no text, no letters, no watermark, no logo")
MOTIONS = ["zoom_in", "zoom_out", "pan_left", "pan_right", "pan_up", "pan_down"]
SHOTS = ["wide establishing shot", "medium shot, dynamic angle", "extreme close-up of a key detail", "low angle hero shot", "high angle overview"]
VARIANTS = ["", "alternative composition, different camera angle", "different lighting mood, fresh perspective", "creative variation, new framing"]
VOICES = {"female": ("fa-IR-DilaraNeural", "en-US-AriaNeural"), "male": ("fa-IR-FaridNeural", "en-US-GuyNeural")}


# ───────────────────────── ابزارهای تلگرام ─────────────────────────
def tg(token, method, **data):
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/{method}", json=data, timeout=15)
        return r.json()
    except Exception as e:
        log.warning("tg %s failed: %s", method, e)
        return {"ok": False, "description": str(e)}


def tg_upload(method, field, filename, data, mime, extra=None, thumb=None):
    files = {field: (filename, data, mime)}
    if thumb:
        files["thumbnail"] = ("thumb.jpg", thumb, "image/jpeg")
    try:
        r = requests.post(f"https://api.telegram.org/bot{MOTHER_TOKEN}/{method}", data=extra or {}, files=files, timeout=180)
        return r.json()
    except Exception as e:
        log.warning("tg upload %s failed: %s", method, e)
        return {"ok": False, "description": str(e)}


# ───────────────────────── احراز هویت مینیاپ (مثل الگو) ─────────────────────────
def verify_init_data(init_data):
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        got = pairs.pop("hash", None)
        if not got:
            return None
        check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        key = hmac.new(b"WebAppData", MOTHER_TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(key, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, got):
            return None
        if time.time() - int(pairs.get("auth_date", 0)) > 86400:
            return None
        return json.loads(pairs["user"])
    except Exception:
        return None


def auth():
    u = verify_init_data(request.headers.get("X-Init-Data", ""))
    return u["id"] if u else None


def oid(s):
    try:
        return ObjectId(str(s))
    except (InvalidId, TypeError):
        return None


def sign(mid):
    return hmac.new(MEDIA_SECRET, str(mid).encode(), hashlib.sha256).hexdigest()[:24]


def public_media(m):
    mid = str(m["_id"])
    s = sign(mid)
    return {
        "id": mid, "kind": m["kind"], "url": f"/media/{mid}?s={s}", "thumb": f"/media/{mid}/thumb?s={s}",
        "prompt": m.get("prompt", ""), "caption": m.get("caption", ""), "hashtags": m.get("hashtags", []),
        "fmt": m.get("fmt", "story"), "style": m.get("style", "auto"), "w": m.get("w"), "h": m.get("h"),
        "size": m.get("size", 0), "provider": m.get("provider", ""), "created": iso(m["created"]), "params": m.get("params", {}),
    }


NO_BLOBS = {"data": 0, "thumb": 0}


# ───────────────────────── سهمیه (مثل الگو، جدا برای تصویر و ویدیو) ─────────────────────────
def limit_of(kind):
    return DAILY_IMAGES if kind == "image" else DAILY_VIDEOS


def quota_key(uid, kind):
    return f"{uid}:{kind}:{now():%Y-%m-%d}"


def quota_take(uid, kind, n):
    lim = limit_of(kind)
    if n > lim:
        return None
    key = quota_key(uid, kind)
    try:
        db.usage.find_one_and_update({"_id": key, "n": {"$lte": lim - n}}, {"$inc": {"n": n}}, upsert=True)
        return key
    except DuplicateKeyError:
        return None


def quota_refund(key, n):
    if key and n > 0:
        db.usage.update_one({"_id": key}, {"$inc": {"n": -n}})


def quota_left(uid, kind):
    d = db.usage.find_one({"_id": quota_key(uid, kind)})
    return max(0, limit_of(kind) - (d["n"] if d else 0))


# ───────────────────────── مدل متنی: پرامپت حرفهای + کپشن + سناریوی ویدیو ─────────────────────────
PLAN_SYSTEM = """You are a senior social-media art director. You turn a short idea into a scroll-stopping, professional visual plan.

Reply with ONE valid JSON object only (no markdown fences, no comments):
{
 "refuse": false,
 "image_prompt": "...",
 "caption": "...",
 "hashtags": ["...", "..."],
 "scenes": [{"prompt": "...", "motion": "zoom_in", "voice": "..."}]
}

Rules:
- "image_prompt": English, 40-90 words. Cover subject, setting, lighting, composition, camera/lens, color palette and mood. Make it visually striking and modern, with one clear focal point. The image must contain NO text, letters, numbers, logos or watermarks. Never name real people or celebrities: describe a generic look instead. Respect the requested style hint and aspect (vertical/square/horizontal composition).
- "caption": a ready-to-post caption in the SAME language as the user's request (Persian if the request is Persian). Start with a short hook line that stops the scroll, then 1-3 short lines of value or emotion, and end with a call to action (save / share / comment). At most 320 characters, 1-3 fitting emojis.
- "hashtags": exactly 8 relevant hashtags (mix of broad and niche; in the caption's language, a couple may be English). No spaces inside a tag (use underscore).
- "scenes": ONLY when the request says VIDEO. Exactly the requested number of scenes that tell a tiny story (hook, development, payoff). Each scene: "prompt" = English image prompt like image_prompt but with the SAME subject, palette and style across all scenes and a different shot type (wide, medium, close-up...). "motion" = one of zoom_in, zoom_out, pan_left, pan_right, pan_up, pan_down. "voice" = one short natural spoken line (max 14 words) in the caption's language. For images set "scenes" to [].
- If the idea is sexual, hateful, graphic violence, involves minors in any unsafe way or is otherwise clearly harmful, return {"refuse": true} only.
"""


def llm_chat(system, user, max_tokens=1800):
    errs = []
    if AI_BASE_URL and AI_API_KEY and AI_MODEL:
        try:
            r = requests.post(f"{AI_BASE_URL}/chat/completions", headers={"Authorization": f"Bearer {AI_API_KEY}"},
                              json={"model": AI_MODEL, "max_tokens": max_tokens, "temperature": 0.7,
                                    "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]},
                              timeout=60)
            if r.status_code >= 400:
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
            return r.json()["choices"][0]["message"]["content"] or ""
        except Exception as e:
            errs.append(f"main: {e}")
            log.warning("LLM اصلی خطا داد: %s", e)
    try:   # جایگزین رایگان
        hdr = {"Authorization": f"Bearer {POLLI_KEY}"} if POLLI_KEY else {}
        url = "https://gen.pollinations.ai/v1/chat/completions" if POLLI_KEY else "https://text.pollinations.ai/openai"
        r = requests.post(url, headers=hdr, json={"model": "openai", "temperature": 0.7,
                                                  "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]},
                          timeout=60)
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
        return r.json()["choices"][0]["message"]["content"] or ""
    except Exception as e:
        errs.append(f"free: {e}")
    raise RuntimeError(" | ".join(errs))


def extract_json(txt):
    txt = re.sub(r"<think>.*?</think>", "", txt or "", flags=re.S)
    m = re.search(r"\{.*\}", txt, re.S)
    if not m:
        raise ValueError(f"JSON پیدا نشد: {txt[:150]!r}")
    return json.loads(m.group(0))


def clean_text(s, n):
    return re.sub(r"\s+", " ", str(s or "")).strip()[:n]


def clean_tags(tags):
    out = []
    for t in (tags if isinstance(tags, list) else []):
        t = str(t).strip().lstrip("#").replace(" ", "_").replace("\u200c", "_")
        if re.match(r"^[\w\u0600-\u06FF]{2,40}$", t) and t not in out:
            out.append(t)
    return ["#" + t for t in out[:10]]


def make_plan(kind, prompt, style, fmt, n_scenes, with_voice):
    """هیچوقت شکست نمیخوره: اگه مدل متنی خراب بود، از توضیح خود کاربر استفاده میکنه"""
    ratio = FORMATS[fmt]["ratio"]
    hint = STYLES[style][2] or "choose the style that best fits the idea"
    req = [f"Request type: {'VIDEO' if kind == 'video' else 'IMAGE'}", f"Aspect ratio: {ratio}", f"Style hint: {hint}",
           f"Number of scenes: {n_scenes if kind == 'video' else 0}", "User idea:\n" + prompt]
    raw = {}
    for attempt in range(2):
        try:
            raw = extract_json(llm_chat(PLAN_SYSTEM, "\n".join(req)))
            break
        except Exception as e:
            log.warning("make_plan attempt %s failed: %s", attempt + 1, e)
            time.sleep(1.5)
    if raw.get("refuse") is True:
        raise UserError("این درخواست قابل انجام نیست. یه توضیح دیگه امتحان کن.")
    image_prompt = clean_text(raw.get("image_prompt"), 900) or clean_text(prompt, 500)
    scenes = []
    for i, s in enumerate(raw.get("scenes") if isinstance(raw.get("scenes"), list) else []):
        if not isinstance(s, dict) or not clean_text(s.get("prompt"), 900):
            continue
        mo = str(s.get("motion", "")).strip().lower()
        scenes.append({"prompt": clean_text(s["prompt"], 900), "motion": mo if mo in MOTIONS else MOTIONS[i % len(MOTIONS)],
                       "voice": clean_text(s.get("voice"), 160) if with_voice else ""})
    if kind == "video":
        scenes = scenes[:n_scenes]
        base = image_prompt
        while len(scenes) < n_scenes:       # کمبود صحنه (یا شکست مدل) → از پرامپت اصلی با شاتهای متنوع
            i = len(scenes)
            scenes.append({"prompt": f"{base}, {SHOTS[i % len(SHOTS)]}", "motion": MOTIONS[i % len(MOTIONS)], "voice": ""})
    return {"image_prompt": image_prompt, "caption": clean_text(raw.get("caption"), 400),
            "hashtags": clean_tags(raw.get("hashtags")), "scenes": scenes if kind == "video" else []}


CLIP_LOCAL = {"sd-turbo", "sdxl-turbo"}     # مدلهای CLIP: سقف ~۷۷ توکن؛ پرامپت باید کوتاه و مهمترینها اول باشه


def local_id(model):
    return model[6:] if str(model or "").startswith("local:") else None


def build_prompt(base, style, fmt, variant=0, local=None):
    if local:   # مدل لوکال: بدون «no text/no logo» (مدلهای دیفیوژن با نفی خوب کار نمیکنن) و برای CLIP کوتاه
        if local in CLIP_LOCAL:
            base = " ".join(base.split()[:34])
        parts = [base]
        if STYLES[style][2]:
            parts.append(" ".join(STYLES[style][2].split()[:18]) if local in CLIP_LOCAL else STYLES[style][2])
        if variant and VARIANTS[variant % len(VARIANTS)]:
            parts.append(VARIANTS[variant % len(VARIANTS)])
        parts.append("sharp focus, highly detailed, professional composition")
        return ", ".join(parts)[:1400]
    parts = [base]
    if STYLES[style][2]:
        parts.append(STYLES[style][2])
    if FORMATS[fmt]["ratio"] == "9:16":
        parts.append("vertical composition")
    if variant and VARIANTS[variant % len(VARIANTS)]:
        parts.append(VARIANTS[variant % len(VARIANTS)])
    parts.append(QUALITY_TAIL)
    return ", ".join(parts)[:1400]


# ───────────────────────── منابع رایگان تصویر ─────────────────────────
class NotConfigured(Exception):
    pass


def _check_image(content, ctype=""):
    if len(content) < 3000:
        raise RuntimeError("پاسخ خالی/خیلی کوچک")
    Image.open(io.BytesIO(content)).verify()
    return content


def prov_pollinations(prompt, fmt, seed):
    w, h = FORMATS[fmt]["gen"]
    q = quote(prompt[:1400], safe="")
    params = {"width": w, "height": h, "seed": seed, "model": POLLI_IMAGE_MODEL, "nologo": "true", "enhance": "false"}
    if POLLI_KEY:
        url, hdr = f"https://gen.pollinations.ai/image/{q}", {"Authorization": f"Bearer {POLLI_KEY}"}
    else:
        url, hdr = f"https://image.pollinations.ai/prompt/{q}", {}
    last = None
    for attempt in range(3):
        r = requests.get(url, params=params, headers=hdr, timeout=120)
        if r.status_code == 200 and r.headers.get("content-type", "").startswith("image/"):
            return _check_image(r.content)
        last = f"HTTP {r.status_code}"
        if r.status_code in (429, 502, 503, 504):
            time.sleep(6 * (attempt + 1))
            continue
        break
    raise RuntimeError(last or "unknown")


def prov_cloudflare(prompt, fmt, seed):
    if not (CF_ACCOUNT_ID and CF_API_TOKEN):
        raise NotConfigured()
    r = requests.post(f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/ai/run/@cf/black-forest-labs/flux-1-schnell",
                      headers={"Authorization": f"Bearer {CF_API_TOKEN}"}, json={"prompt": prompt[:2000], "steps": 6}, timeout=90)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:120]}")
    return _check_image(base64.b64decode(r.json()["result"]["image"]))   # خروجی مربعه؛ بعداً به نسبت مورد نظر کراپ میشه


def prov_horde(prompt, fmt, seed):
    w, h = FORMATS[fmt]["horde"]
    hdr = {"apikey": HORDE_KEY, "Client-Agent": "poststudio:1.0:telegram-bot"}
    body = {"prompt": prompt[:1000], "nsfw": False, "censor_nsfw": True, "r2": True, "slow_workers": True,
            "params": {"width": w, "height": h, "steps": 28, "cfg_scale": 7, "n": 1, "sampler_name": "k_euler_a", "seed": str(seed)}}
    r = requests.post("https://aihorde.net/api/v2/generate/async", headers=hdr, json=body, timeout=30)
    if r.status_code not in (200, 202):
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:120]}")
    job = r.json()["id"]
    deadline = time.time() + 170
    while time.time() < deadline:
        time.sleep(4)
        c = requests.get(f"https://aihorde.net/api/v2/generate/check/{job}", headers=hdr, timeout=20).json()
        if c.get("faulted"):
            raise RuntimeError("faulted")
        if c.get("done"):
            st = requests.get(f"https://aihorde.net/api/v2/generate/status/{job}", headers=hdr, timeout=30).json()
            gens = st.get("generations") or []
            if not gens or gens[0].get("censored"):
                raise RuntimeError("censored/empty")
            img = requests.get(gens[0]["img"], timeout=60)
            return _check_image(img.content)
    try:
        requests.delete(f"https://aihorde.net/api/v2/generate/status/{job}", headers=hdr, timeout=10)
    except Exception:
        pass
    raise RuntimeError("timeout")


def prov_huggingface(prompt, fmt, seed):
    if not HF_TOKEN:
        raise NotConfigured()
    w, h = FORMATS[fmt]["gen"]
    r = requests.post("https://router.huggingface.co/hf-inference/models/black-forest-labs/FLUX.1-schnell",
                      headers={"Authorization": f"Bearer {HF_TOKEN}", "Accept": "image/png"},
                      json={"inputs": prompt[:1500], "parameters": {"width": w, "height": h, "num_inference_steps": 4, "seed": seed}},
                      timeout=120)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:120]}")
    return _check_image(r.content)


def prov_together(prompt, fmt, seed):
    if not TOGETHER_KEY:
        raise NotConfigured()
    w, h = FORMATS[fmt]["gen"]
    r = requests.post("https://api.together.xyz/v1/images/generations", headers={"Authorization": f"Bearer {TOGETHER_KEY}"},
                      json={"model": TOGETHER_MODEL, "prompt": prompt[:1500], "width": w, "height": h, "steps": 4, "n": 1,
                            "seed": seed, "response_format": "b64_json"}, timeout=120)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:120]}")
    return _check_image(base64.b64decode(r.json()["data"][0]["b64_json"]))


PROVIDERS = {"pollinations": prov_pollinations, "cloudflare": prov_cloudflare, "huggingface": prov_huggingface,
             "together": prov_together, "horde": prov_horde}


def gen_image(prompt, fmt, seed, model="auto", tick=None):
    """model: auto | online:<provider> | local:<id>. اگه مدل انتخابی خراب بود، بقیهی زنجیره جایگزین میشه"""
    errs = []
    order = list(IMAGE_PROVIDERS)
    mid = local_id(model)
    if mid:
        try:
            return local_image(mid, prompt, fmt, seed, tick), "local-" + mid
        except Exception as e:
            local_release()
            log.warning("local image %s failed: %s", mid, e, exc_info=DEBUG)
            errs.append(f"local-{mid}: {str(e)[:100]}")
            if not LOCAL_FALLBACK:
                raise UserError("مدل لوکال انتخاب‌شده الان قابل اجرا نیست؛ مدل دیگری انتخاب کن." + (f"\n[{errs[-1]}]" if DEBUG else ""))
    elif str(model).startswith("online:") and model[7:] in PROVIDERS:
        order = [model[7:]] + [x for x in order if x != model[7:]]
    for name in order:
        fn = PROVIDERS.get(name)
        if not fn:
            continue
        try:
            return fn(prompt, fmt, seed), name
        except NotConfigured:
            continue
        except Exception as e:
            errs.append(f"{name}: {str(e)[:100]}")
            log.warning("provider %s failed: %s", name, e)
    raise UserError("الان همهی سرویسهای رایگان تصویر شلوغن؛ چند دقیقه بعد دوباره امتحان کن."
                    + (f"\n[{' | '.join(errs)}]" if DEBUG else ""))


# ───────────────────────── موتور مدلهای لوکال (بدون API، بدون سقف) ─────────────────────────
# همهچیز lazy است: تا LOCAL_MODELS=1 نباشه حتی torch ایمپورت نمیشه. همزمان فقط یک مدل سنگین توی حافظه میمونه.
import importlib.util as _iu
from contextlib import contextmanager

LOCAL_IMAGE_MODELS = {
    "sd-turbo":     {"label": "SD-Turbo", "repo": "stabilityai/sd-turbo", "area": 512 * 512, "steps": 2, "cfg": 0.0, "gpu_only": False,
                     "need": ("torch", "diffusers", "transformers"), "note": "سریع و سبک؛ روی CPU هم کار می‌کند (چند ده ثانیه)"},
    "sdxl-turbo":   {"label": "SDXL-Turbo", "repo": "stabilityai/sdxl-turbo", "area": 512 * 512, "steps": 2, "cfg": 0.0, "gpu_only": False,
                     "need": ("torch", "diffusers", "transformers"), "note": "جزئیات بهتر؛ روی GPU توصیه می‌شود"},
    "flux-schnell": {"label": "FLUX.1-schnell", "repo": "black-forest-labs/FLUX.1-schnell", "area": 1024 * 1024, "steps": 4, "cfg": 0.0, "gpu_only": True,
                     "need": ("torch", "diffusers", "transformers"), "note": "بهترین کیفیت و درک پرامپت؛ فقط GPU"},
}
LOCAL_VIDEO_MODELS = {
    "parallax":  {"label": "پارالکس سه‌بعدی", "need": ("torch", "transformers", "numpy", "cv2"), "gpu_only": False,
                  "note": "عمق‌یابی تصویر + حرکت دوربین سه‌بعدی؛ روی CPU هم اجرا می‌شود"},
    "ltx-video": {"label": "LTX-Video", "repo": "Lightricks/LTX-Video", "need": ("torch", "diffusers", "transformers", "numpy"), "gpu_only": True,
                  "note": "ویدیوی AI واقعی از متن؛ فقط GPU و چند دقیقه زمان"},
}
_ids = lambda env, allowed: [x.strip() for x in os.environ.get(env, ",".join(allowed)).split(",") if x.strip() in allowed]
LOCAL_IMAGE_IDS = _ids("LOCAL_IMAGE_MODELS", LOCAL_IMAGE_MODELS)
LOCAL_VIDEO_IDS = _ids("LOCAL_VIDEO_MODELS", LOCAL_VIDEO_MODELS)

_CAPS, _CAPS_LOCK = {}, threading.Lock()
_LOCAL_LOCK = threading.Lock()
_PIPE = {"key": None, "obj": None}
_DEPTH = {"pipe": None}


def local_caps():
    with _CAPS_LOCK:
        if _CAPS:
            return _CAPS
        c = {"enabled": LOCAL_ENABLED, "device": "cpu", "vram": 0.0}
        for k in ("torch", "diffusers", "transformers", "numpy", "cv2"):
            c[k] = bool(_iu.find_spec(k)) if LOCAL_ENABLED else False
        if LOCAL_ENABLED and c["torch"]:
            try:
                import torch
                dev = LOCAL_DEVICE
                if dev == "auto":
                    dev = "cuda" if torch.cuda.is_available() else ("mps" if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available() else "cpu")
                c["device"] = dev
                if dev == "cuda":
                    c["vram"] = torch.cuda.get_device_properties(0).total_memory / 2 ** 30
            except Exception:
                log.warning("تشخیص torch/GPU ناموفق بود", exc_info=True)
        _CAPS.update(c)
        return _CAPS


def local_status(group, mid):
    """(آماده؟، توضیح) برای یک مدل لوکال"""
    spec = (LOCAL_IMAGE_MODELS if group == "image" else LOCAL_VIDEO_MODELS)[mid]
    c = local_caps()
    if not c["enabled"]:
        return False, "LOCAL_MODELS=1 فعال نیست"
    miss = [k for k in spec["need"] if not c.get(k)]
    if miss:
        return False, "کتابخانه نصب نیست: " + ", ".join(miss)
    if spec["gpu_only"] and c["device"] != "cuda":
        return False, "این مدل فقط روی GPU (CUDA) اجرا می‌شود"
    return True, spec["note"]


def model_catalog():
    """فهرست مدلهای قابل انتخاب برای مینیاپ (فقط مدلهایی که الان واقعاً آمادهاند)"""
    img = [{"id": "auto", "label": "خودکار", "emoji": "✨", "note": "زنجیرهٔ سرویس‌ها با جایگزین خودکار"}]
    for k, label, emo, ok in (("pollinations", "Pollinations", "🌸", True), ("cloudflare", "Cloudflare FLUX", "☁️", bool(CF_ACCOUNT_ID and CF_API_TOKEN)),
                              ("huggingface", "HF FLUX", "🤗", bool(HF_TOKEN)), ("together", "Together", "🤝", bool(TOGETHER_KEY)), ("horde", "AI Horde", "🌍", True)):
        if ok:
            img.append({"id": "online:" + k, "label": label, "emoji": emo, "note": "سرویس آنلاین رایگان"})
    vid = [{"id": "auto", "label": "خودکار", "emoji": "✨", "note": "بهترین حالت موجود با جایگزین خودکار"},
           {"id": "kenburns", "label": "چندصحنه + حرکت دوربین", "emoji": "🎞", "note": "تصاویر صحنه‌ها با زوم و پن سینمایی و ترنزیشن"}]
    if ENABLE_AI_VIDEO and POLLI_KEY:
        vid.append({"id": "polli-video", "label": "ویدیوی AI آنلاین", "emoji": "🌸", "note": "ویدیوی AI از Pollinations"})
    for mid in LOCAL_IMAGE_IDS:
        ok, note = local_status("image", mid)
        if ok:
            img.append({"id": "local:" + mid, "label": LOCAL_IMAGE_MODELS[mid]["label"], "emoji": "🖥", "note": note, "local": True})
    for mid in LOCAL_VIDEO_IDS:
        ok, note = local_status("video", mid)
        if ok:
            vid.append({"id": "local:" + mid, "label": LOCAL_VIDEO_MODELS[mid]["label"], "emoji": "🖥", "note": note, "local": True})
    return {"image": img, "video": vid}


@contextmanager
def _local_slot(tick=None):
    """فقط یک کار لوکال همزمان؛ تا نوبتش نرسیده جاب رو زنده نگه میداره"""
    while not _LOCAL_LOCK.acquire(timeout=15):
        if tick:
            tick()
    try:
        yield
    finally:
        _LOCAL_LOCK.release()


def local_release():
    """آزادسازی مدل از RAM/VRAM (بعد از خطا یا هنگام عوض شدن مدل)"""
    _PIPE["key"] = _PIPE["obj"] = None
    try:
        import gc
        gc.collect()
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


def fit_size(fmt, area, mult=16):
    ow, oh = FORMATS[fmt]["out"]
    r = ow / oh
    h = (area / r) ** 0.5
    return max(mult, int(round(h * r / mult)) * mult), max(mult, int(round(h / mult)) * mult)


def _get_pipe(group, mid):
    key = (group, mid)
    if _PIPE["key"] == key:
        return _PIPE["obj"]
    local_release()
    import torch
    c = local_caps()
    dev, vram, tok = c["device"], c["vram"], (HF_TOKEN or None)
    log.info("loading local model %s (%s)", mid, dev)
    if group == "image" and mid == "flux-schnell":
        from diffusers import FluxPipeline
        pipe = FluxPipeline.from_pretrained(LOCAL_IMAGE_MODELS[mid]["repo"], torch_dtype=torch.bfloat16, token=tok)
        pipe.to("cuda") if vram >= 40 else pipe.enable_model_cpu_offload()
    elif group == "image":
        from diffusers import AutoPipelineForText2Image
        pipe = AutoPipelineForText2Image.from_pretrained(LOCAL_IMAGE_MODELS[mid]["repo"], torch_dtype=torch.float16 if dev == "cuda" else torch.float32,
                                                          variant="fp16", token=tok)
        pipe.to(dev)
        if dev == "cpu":
            pipe.enable_attention_slicing()
    else:   # ltx-video
        from diffusers import LTXPipeline
        pipe = LTXPipeline.from_pretrained(LOCAL_VIDEO_MODELS[mid]["repo"], torch_dtype=torch.bfloat16, token=tok)
        pipe.to("cuda") if vram >= 40 else pipe.enable_model_cpu_offload()
        try:
            pipe.vae.enable_tiling()
        except Exception:
            pass
    try:
        pipe.set_progress_bar_config(disable=True)
    except Exception:
        pass
    _PIPE["key"], _PIPE["obj"] = key, pipe
    return pipe


def _step_cb(tick):
    def cb(pipe, i, t, kw):
        tick()
        return kw
    return cb


def local_image(mid, prompt, fmt, seed, tick=None):
    spec = LOCAL_IMAGE_MODELS[mid]
    ok, why = local_status("image", mid)
    if not ok:
        raise RuntimeError(why)
    w, h = fit_size(fmt, spec["area"], 16)
    with _local_slot(tick):
        pipe = _get_pipe("image", mid)
        import torch
        kw = {"prompt": prompt, "width": w, "height": h, "num_inference_steps": spec["steps"], "guidance_scale": spec["cfg"],
              "generator": torch.Generator("cpu").manual_seed(int(seed))}
        if mid == "flux-schnell":
            kw["max_sequence_length"] = 256
        if tick:
            kw["callback_on_step_end"] = _step_cb(tick)
        img = pipe(**kw).images[0]
    b = io.BytesIO()
    img.convert("RGB").save(b, "PNG")
    return _check_image(b.getvalue())   # خروجی کوچکتر از قالب نهایی است؛ finish_image با کراپ + LANCZOS + شارپ به اندازهی کامل میرسونه


def _local_boot():
    c = local_caps()
    log.info("local models: device=%s vram=%.1fGB libs=%s", c["device"], c["vram"], {k: c[k] for k in ("torch", "diffusers", "transformers", "numpy", "cv2")})
    for g, ids in (("image", LOCAL_IMAGE_IDS), ("video", LOCAL_VIDEO_IDS)):
        for mid in ids:
            ok, note = local_status(g, mid)
            log.info("  %s/%s: %s — %s", g, mid, "READY" if ok else "off", note)
    if LOCAL_PRELOAD in LOCAL_IMAGE_IDS and local_status("image", LOCAL_PRELOAD)[0]:
        try:
            with _local_slot():
                _get_pipe("image", LOCAL_PRELOAD)
            log.info("preloaded %s", LOCAL_PRELOAD)
        except Exception:
            log.warning("preload failed", exc_info=True)


# ───────────────────────── پردازش تصویر ─────────────────────────
def cover_crop(img, w, h):
    sw, sh = img.size
    s = max(w / sw, h / sh)
    nw, nh = max(w, round(sw * s)), max(h, round(sh * s))
    img = img.resize((nw, nh), Image.LANCZOS)
    l, t = (nw - w) // 2, (nh - h) // 2
    return img.crop((l, t, l + w, t + h))


def jpeg_bytes(img, q=92):
    b = io.BytesIO()
    img.save(b, "JPEG", quality=q, optimize=True, progressive=True)
    return b.getvalue()


def finish_image(raw, fmt):
    """کراپ دقیق به نسبت فرمت + ارتقای کیفیت + کمی وضوح/کنتراست برای ظاهر حرفهای"""
    w, h = FORMATS[fmt]["out"]
    img = cover_crop(Image.open(io.BytesIO(raw)).convert("RGB"), w, h)
    img = img.filter(ImageFilter.UnsharpMask(radius=1.4, percent=55, threshold=3))
    img = ImageEnhance.Contrast(img).enhance(1.04)
    img = ImageEnhance.Color(img).enhance(1.06)
    return jpeg_bytes(img, 92)


def make_thumb(data, width=480):
    img = Image.open(io.BytesIO(data)).convert("RGB")
    img.thumbnail((width, width * 3))
    return jpeg_bytes(img, 78)


# ───────────────────────── ساخت ویدیو (ffmpeg) ─────────────────────────
def ffmpeg_bin():
    p = shutil.which("ffmpeg")
    if p:
        return p
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def media_duration(path):
    r = subprocess.run([ffmpeg_bin(), "-hide_banner", "-i", path], capture_output=True, text=True)
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", r.stderr)
    return int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3]) if m else 0.0


def make_voice(text, gender, path):
    """صدای گوینده با edge-tts (رایگان). اگه نشد None برمیگردونه و ویدیو بیصدا ساخته میشه"""
    try:
        import edge_tts
        fa = len(re.findall(r"[\u0600-\u06FF]", text)) >= len(re.findall(r"[A-Za-z]", text))
        voice = VOICES[gender][0 if fa else 1]

        async def go():
            await edge_tts.Communicate(text, voice).save(path)
        asyncio.run(go())
        if os.path.exists(path) and os.path.getsize(path) > 2000:
            return media_duration(path)
    except Exception as e:
        log.warning("TTS ناموفق: %s", e)
    return None


def motion_expr(motion, F):
    c = "iw/2-(iw/zoom/2)"
    r = "ih/2-(ih/zoom/2)"
    return {
        "zoom_in":   (f"1+0.14*on/{F}", c, r),
        "zoom_out":  (f"1.14-0.14*on/{F}", c, r),
        "pan_left":  ("1.14", f"(iw-iw/zoom)*(1-on/{F})", r),
        "pan_right": ("1.14", f"(iw-iw/zoom)*on/{F}", r),
        "pan_up":    ("1.14", c, f"(ih-ih/zoom)*(1-on/{F})"),
        "pan_down":  ("1.14", c, f"(ih-ih/zoom)*on/{F}"),
    }[motion]


def render_video(img_paths, motions, fmt, per_scene, voice_path, out_path):
    W, H = FORMATS[fmt]["vid"]
    fps, xf, n = 25, 0.7, len(img_paths)
    F = round(per_scene * fps)
    total = n * per_scene - (n - 1) * xf
    cmd = [ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error"]
    for p in img_paths:
        cmd += ["-i", p]
    if voice_path:
        cmd += ["-i", voice_path]
    chains = []
    for i, mo in enumerate(motions):
        z, x, y = motion_expr(mo, F)
        chains.append(f"[{i}:v]zoompan=z='{z}':x='{x}':y='{y}':d={F}:s={W}x{H}:fps={fps},setsar=1,format=yuv420p[v{i}]")
    trans = ["fade", "smoothleft", "fade", "smoothright"]
    last = "v0"
    for k in range(1, n):
        chains.append(f"[{last}][v{k}]xfade=transition={trans[(k - 1) % len(trans)]}:duration={xf}:offset={round(k * (per_scene - xf), 3)}[x{k}]")
        last = f"x{k}"
    chains.append(f"[{last}]fade=t=in:st=0:d=0.4,fade=t=out:st={round(total - 0.5, 3)}:d=0.5,eq=saturation=1.06:contrast=1.03[vout]")
    cmd += ["-filter_complex", ";".join(chains), "-map", "[vout]"]
    if voice_path:
        chains_a = f"[{n}:a]apad=whole_dur={round(total, 3)},afade=t=out:st={round(total - 0.8, 3)}:d=0.8[aout]"
        cmd[cmd.index("-filter_complex") + 1] += ";" + chains_a
        cmd += ["-map", "[aout]", "-c:a", "aac", "-b:a", "128k"]
    else:
        cmd += ["-an"]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p", "-r", str(fps),
            "-movflags", "+faststart", "-t", str(round(total, 3)), out_path]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    if r.returncode != 0 or not os.path.exists(out_path):
        raise RuntimeError("ffmpeg: " + r.stderr[-400:])
    return total


def ai_video_pollinations(prompt, fmt):
    """ویدیوی AI واقعی (اختیاری). هر خطایی → None تا حالت چندصحنهای جایگزین بشه"""
    if not (ENABLE_AI_VIDEO and POLLI_KEY):
        return None
    try:
        params = {"aspectRatio": FORMATS[fmt]["ratio"], "duration": 5}
        if POLLI_VIDEO_MODEL:
            params["model"] = POLLI_VIDEO_MODEL
        r = requests.get(f"https://gen.pollinations.ai/video/{quote(prompt[:900], safe='')}", params=params,
                         headers={"Authorization": f"Bearer {POLLI_KEY}"}, timeout=240)
        if r.status_code == 200 and "video" in r.headers.get("content-type", "") and len(r.content) > 50000:
            return r.content
        log.warning("AI video HTTP %s", r.status_code)
    except Exception as e:
        log.warning("AI video failed: %s", e)
    return None


# ───────────────────────── ویدیوی لوکال: پارالکس سهبعدی + LTX-Video ─────────────────────────
def _encode_frames(frames, W, H, fps, out_path):
    """فریمهای RGB (numpy uint8) رو مستقیم به ffmpeg میریزه"""
    cmd = [ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(fps), "-i", "-",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "19", "-pix_fmt", "yuv420p", "-movflags", "+faststart", out_path]
    with tempfile.TemporaryFile() as err:
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=err)
        try:
            for f in frames:
                p.stdin.write(f.tobytes())
            p.stdin.close()
            p.wait(timeout=600)
        except Exception:
            p.kill()
            raise
        if p.returncode != 0 or not os.path.exists(out_path):
            err.seek(0)
            raise RuntimeError("ffmpeg: " + err.read().decode(errors="ignore")[-300:])


def local_depth(img, tick=None):
    """نقشهی عمق (۰ دور … ۱ نزدیک) با Depth-Anything-V2-Small؛ روی CPU هم چند ثانیه"""
    import numpy as np
    with _local_slot(tick):
        if _DEPTH["pipe"] is None:
            from transformers import pipeline
            dev = local_caps()["device"]
            _DEPTH["pipe"] = pipeline("depth-estimation", model=DEPTH_REPO, device=0 if dev == "cuda" else ("mps" if dev == "mps" else -1))
        out = _DEPTH["pipe"](img.convert("RGB"))
    return np.asarray(out["depth"].convert("L"), dtype=np.float32) / 255.0


def parallax_clip(img, depth, motion, W, H, dur, out_path, fps=25):
    """کلیپ ۲.۵بعدی: اجسام نزدیک بیشتر از پسزمینه جابجا میشن (حس واقعی عمق، نه فقط زوم ساده)"""
    import numpy as np, cv2
    S = 1.25
    src = np.ascontiguousarray(np.asarray(cover_crop(img.convert("RGB"), int(W * S), int(H * S))))
    d = cv2.resize(depth.astype("float32"), (W, H), interpolation=cv2.INTER_CUBIC)
    d = cv2.GaussianBlur(d, (0, 0), max(2.0, W / 240))
    lo, hi = np.percentile(d, (2, 98))
    dn = np.clip((d - lo) / max(float(hi - lo), 1e-3), 0, 1).astype("float32")
    gx, gy = np.meshgrid(np.arange(W, dtype="float32"), np.arange(H, dtype="float32"))
    cx, cy, A = W / 2.0, H / 2.0, 0.045 * W
    F = max(3, int(round(dur * fps)))
    f_par = 0.3 + 1.4 * dn          # ضریب پارالکس: نزدیک بیشتر حرکت میکنه
    f_zoom = 0.6 + 0.8 * dn

    def frames():
        for k in range(F):
            t = k / (F - 1)
            e = t * t * (3 - 2 * t)
            zamp, camx, camy = 0.0, 0.0, 0.0
            if motion == "zoom_in":
                zamp, camx = 0.14 * e, 0.5 * A * (2 * e - 1)
            elif motion == "zoom_out":
                zamp, camx = 0.14 * (1 - e), 0.5 * A * (1 - 2 * e)
            elif motion == "pan_left":
                camx = A * (1 - 2 * e)
            elif motion == "pan_right":
                camx = A * (2 * e - 1)
            elif motion == "pan_up":
                camy = A * (1 - 2 * e)
            else:
                camy = A * (2 * e - 1)
            zl = 1.0 + (0.06 if motion not in ("zoom_in", "zoom_out") else 0.02) + zamp * f_zoom
            mx = (cx + (gx - cx) / zl + camx * f_par) * S
            my = (cy + (gy - cy) / zl + camy * f_par) * S
            yield cv2.remap(src, mx, my, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    _encode_frames(frames(), W, H, fps, out_path)


def make_parallax_clips(jid, paths, motions, fmt, per, td):
    W, H = FORMATS[fmt]["vid"]
    clips = []
    for i, (pth, mo) in enumerate(zip(paths, motions)):
        jset(jid, step=f"پارالکس سه‌بعدی صحنه {i + 1} از {len(paths)}…", pct=76 + int(10 * i / len(paths)))
        img = Image.open(pth)
        depth = local_depth(img, tick=lambda: jset(jid))
        out = os.path.join(td, f"p{i}.mp4")
        parallax_clip(img, depth, mo, W, H, per, out)
        clips.append(out)
    return clips


def local_ltx_clip(prompt, fmt, seed, out_path, tick=None):
    ok, why = local_status("video", "ltx-video")
    if not ok:
        raise RuntimeError(why)
    import numpy as np, torch
    w, h = fit_size(fmt, 704 * 480, 32)
    nf = max(9, (LTX_FRAMES - 1) // 8 * 8 + 1)
    with _local_slot(tick):
        pipe = _get_pipe("video", "ltx-video")
        kw = {"prompt": prompt, "negative_prompt": "worst quality, inconsistent motion, blurry, jittery, distorted, watermark",
              "width": w, "height": h, "num_frames": nf, "num_inference_steps": LTX_STEPS, "generator": torch.Generator("cpu").manual_seed(int(seed))}
        if tick:
            kw["callback_on_step_end"] = _step_cb(tick)
        frames = pipe(**kw).frames[0]
    _encode_frames((np.asarray(f.convert("RGB")) for f in frames), w, h, 24, out_path)


def render_video_from_clips(clips, durs, fmt, voice_path, out_path, vdur=None):
    """چسباندن کلیپها با ترنزیشن + صدای گوینده (کلیپها هر اندازه/فریمریتی باشن یکدست میشن)"""
    W, H = FORMATS[fmt]["vid"]
    fps, n = 25, len(clips)
    xf = min(0.7, min(durs) / 3)
    total = sum(durs) - (n - 1) * xf
    pad = max(0.0, (vdur or 0) + 0.8 - total)
    cmd = [ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error"]
    for c in clips:
        cmd += ["-i", c]
    if voice_path:
        cmd += ["-i", voice_path]
    chains = [f"[{i}:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1,fps={fps},format=yuv420p[v{i}]" for i in range(n)]
    trans = ["fade", "smoothleft", "fade", "smoothright"]
    last, acc = "v0", durs[0]
    for k in range(1, n):
        chains.append(f"[{last}][v{k}]xfade=transition={trans[(k - 1) % len(trans)]}:duration={xf}:offset={round(acc - xf, 3)}[x{k}]")
        last, acc = f"x{k}", acc + durs[k] - xf
    tot = total + pad
    tp = f"tpad=stop_mode=clone:stop_duration={round(pad, 3)}," if pad > 0 else ""
    chains.append(f"[{last}]{tp}fade=t=in:st=0:d=0.4,fade=t=out:st={round(tot - 0.5, 3)}:d=0.5,eq=saturation=1.06:contrast=1.03[vout]")
    if voice_path:
        chains.append(f"[{n}:a]apad=whole_dur={round(tot, 3)},afade=t=out:st={round(tot - 0.8, 3)}:d=0.8[aout]")
    cmd += ["-filter_complex", ";".join(chains), "-map", "[vout]"]
    cmd += ["-map", "[aout]", "-c:a", "aac", "-b:a", "128k"] if voice_path else ["-an"]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p", "-r", str(fps), "-movflags", "+faststart", "-t", str(round(tot, 3)), out_path]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    if r.returncode != 0 or not os.path.exists(out_path):
        raise RuntimeError("ffmpeg: " + r.stderr[-400:])
    return tot


# ───────────────────────── ذخیرهی خروجی و گالری ─────────────────────────
def save_media(owner, kind, mime, data, thumb, meta):
    doc = {"owner": owner, "kind": kind, "mime": mime, "data": Binary(data), "thumb": Binary(thumb),
           "size": len(data), "created": now(), **meta}
    return db.media.insert_one(doc).inserted_id


def trim_gallery(owner):
    ids = [m["_id"] for m in db.media.find({"owner": owner}, {"_id": 1}).sort("created", -1)]
    for i in ids[KEEP_PER_USER:]:
        db.media.delete_one({"_id": i})


def jset(jid, **f):
    f["updated"] = now()
    db.jobs.update_one({"_id": jid}, {"$set": f})


def do_image(job):
    jid, uid, p = job["_id"], job["owner"], job["params"]
    fmt, style, n = p["fmt"], p["style"], p["count"]
    jset(jid, step="دارم ایدهت رو به یه پرامپت حرفهای تبدیل میکنم…", pct=6)
    plan = make_plan("image", p["prompt"], style, fmt, 0, False)
    items = []
    model = p.get("model", "auto")
    for i in range(n):
        jset(jid, step=f"در حال ساخت تصویر {i + 1} از {n}…", pct=12 + int(80 * i / n))
        try:
            raw, prov = gen_image(build_prompt(plan["image_prompt"], style, fmt, i, local=local_id(model)), fmt, random.randint(1, 2 ** 31 - 1),
                                  model=model, tick=lambda: jset(jid))
        except UserError:
            if items:
                break
            raise
        data = finish_image(raw, fmt)
        w, h = FORMATS[fmt]["out"]
        mid = save_media(uid, "image", "image/jpeg", data, make_thumb(data), {
            "prompt": p["prompt"], "caption": plan["caption"], "hashtags": plan["hashtags"], "fmt": fmt, "style": style,
            "w": w, "h": h, "provider": prov, "params": p})
        items.append(str(mid))
        jset(jid, items=items)
    return items


def do_video(job):
    jid, uid, p = job["_id"], job["owner"], job["params"]
    fmt, style, n, voice = p["fmt"], p["style"], p["scenes"], p["voice"]
    W, H = FORMATS[fmt]["vid"]
    jset(jid, step="دارم سناریوی ویدیو و کپشن رو مینویسم…", pct=5)
    plan = make_plan("video", p["prompt"], style, fmt, n, voice != "off")
    scenes = plan["scenes"]
    meta = {"prompt": p["prompt"], "caption": plan["caption"], "hashtags": plan["hashtags"], "fmt": fmt, "style": style,
            "w": W, "h": H, "params": p}

    vm, im = p.get("model", "auto"), p.get("image_model", "auto")
    if vm == "local:ltx-video":
        try:
            return do_video_ltx(job, plan, meta)
        except UserError:
            raise
        except Exception as e:
            local_release()
            log.warning("LTX failed: %s", e, exc_info=DEBUG)
            if not LOCAL_FALLBACK:
                raise UserError("مدل ویدیوی لوکال الان قابل اجرا نیست؛ موتور دیگری انتخاب کن." + (f"\n[{str(e)[:200]}]" if DEBUG else ""))
            jset(jid, step="مدل ویدیوی لوکال در دسترس نبود؛ می‌روم سراغ حالت چندصحنه…", pct=9)
            vm = "kenburns"

    jset(jid, step="در حال ساخت ویدیوی AI…" if vm in ("auto", "polli-video") else "در حال ساخت صحنه‌ها…", pct=10)
    ai = ai_video_pollinations(build_prompt(plan["image_prompt"], style, fmt), fmt) if vm in ("auto", "polli-video") else None
    if ai:
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "ai.mp4")
            open(src, "wb").write(ai)
            poster = os.path.join(td, "p.jpg")
            subprocess.run([ffmpeg_bin(), "-y", "-loglevel", "error", "-i", src, "-frames:v", "1", poster], capture_output=True)
            thumb = make_thumb(open(poster, "rb").read()) if os.path.exists(poster) else b""
        mid = save_media(uid, "video", "video/mp4", ai, thumb or make_thumb(finish_image(gen_image(plan["image_prompt"], fmt, 1)[0], fmt)),
                         {**meta, "provider": "pollinations-video"})
        return [str(mid)]

    with tempfile.TemporaryDirectory() as td:
        paths, first_thumb, prov_used = [], None, ""
        for i, sc in enumerate(scenes):
            jset(jid, step=f"ساخت صحنه {i + 1} از {len(scenes)}…", pct=12 + int(55 * i / len(scenes)))
            raw, prov_used = gen_image(build_prompt(sc["prompt"], style, fmt, local=local_id(im)), fmt, random.randint(1, 2 ** 31 - 1),
                                       model=im, tick=lambda: jset(jid))
            img = cover_crop(Image.open(io.BytesIO(raw)).convert("RGB"), W * 2, H * 2)
            path = os.path.join(td, f"s{i}.jpg")
            img.save(path, "JPEG", quality=93)
            paths.append(path)
            if first_thumb is None:
                first_thumb = make_thumb(jpeg_bytes(img, 85))

        vpath, vdur = None, None
        narration = " . ".join(s["voice"] for s in scenes if s["voice"])
        if voice != "off" and narration:
            jset(jid, step="در حال ضبط صدای گوینده…", pct=72)
            vpath = os.path.join(td, "voice.mp3")
            vdur = make_voice(narration, voice, vpath)
            if not vdur:
                vpath = None
        xf = 0.7
        per = 4.6
        if vdur:   # طول صحنهها طوری که ویدیو از صدا کوتاهتر نباشه
            per = min(9.0, max(per, (vdur + 1.0 + (n - 1) * xf) / n))
        jset(jid, step="در حال مونتاژ ویدیو (حرکت دوربین، ترنزیشن، صدا)…", pct=78)
        out = os.path.join(td, "out.mp4")
        done = False
        if vm == "local:parallax":
            try:
                clips = make_parallax_clips(jid, paths, [s["motion"] for s in scenes], fmt, per, td)
                jset(jid, step="در حال مونتاژ ویدیو (پارالکس، ترنزیشن، صدا)…", pct=88)
                with FFMPEG_SEM:
                    render_video_from_clips(clips, [per] * len(clips), fmt, vpath, out, vdur)
                prov_used, done = prov_used + "+parallax", True
            except Exception as e:
                local_release()
                log.warning("parallax failed: %s", e, exc_info=DEBUG)
                if not LOCAL_FALLBACK:
                    raise UserError("پارالکس سه‌بعدی الان قابل اجرا نیست؛ موتور دیگری انتخاب کن." + (f"\n[{str(e)[:200]}]" if DEBUG else ""))
        if not done:
            with FFMPEG_SEM:
                render_video(paths, [s["motion"] for s in scenes], fmt, per, vpath, out)
        data = open(out, "rb").read()
        if len(data) > 15 * 1024 * 1024:
            raise UserError("حجم ویدیو زیاد شد؛ با مدت کمتر دوباره امتحان کن.")
        jset(jid, step="در حال ذخیره…", pct=96)
        mid = save_media(uid, "video", "video/mp4", data, first_thumb, {**meta, "provider": prov_used if prov_used.endswith("+parallax") else prov_used + "+ffmpeg"})
    return [str(mid)]


def do_video_ltx(job, plan, meta):
    """ویدیوی AI واقعی و لوکال: هر صحنه یک کلیپ LTX-Video؛ بعد مونتاژ با ترنزیشن و صدا"""
    jid, uid, p = job["_id"], job["owner"], job["params"]
    fmt, style, voice = p["fmt"], p["style"], p["voice"]
    scenes = plan["scenes"]
    n = len(scenes)
    with tempfile.TemporaryDirectory() as td:
        clips, durs = [], []
        for i, sc in enumerate(scenes):
            jset(jid, step=f"ساخت کلیپ AI لوکال {i + 1} از {n}… (روی GPU چند دقیقه)", pct=8 + int(62 * i / n))
            cp = os.path.join(td, f"c{i}.mp4")
            local_ltx_clip(build_prompt(sc["prompt"], style, fmt, local="ltx-video"), fmt, random.randint(1, 2 ** 31 - 1), cp, tick=lambda: jset(jid))
            clips.append(cp)
            durs.append(media_duration(cp) or 2.7)
        vpath, vdur = None, None
        narration = " . ".join(s["voice"] for s in scenes if s["voice"])
        if voice != "off" and narration:
            jset(jid, step="در حال ضبط صدای گوینده…", pct=74)
            vpath = os.path.join(td, "voice.mp3")
            vdur = make_voice(narration, voice, vpath)
            if not vdur:
                vpath = None
        jset(jid, step="در حال مونتاژ ویدیو…", pct=82)
        out = os.path.join(td, "out.mp4")
        with FFMPEG_SEM:
            render_video_from_clips(clips, durs, fmt, vpath, out, vdur)
        poster = os.path.join(td, "p.jpg")
        subprocess.run([ffmpeg_bin(), "-y", "-loglevel", "error", "-i", out, "-frames:v", "1", poster], capture_output=True)
        thumb = make_thumb(open(poster, "rb").read()) if os.path.exists(poster) else b""
        data = open(out, "rb").read()
        if len(data) > 15 * 1024 * 1024:
            raise UserError("حجم ویدیو زیاد شد؛ با تعداد صحنهٔ کمتر دوباره امتحان کن.")
        jset(jid, step="در حال ذخیره…", pct=96)
        mid = save_media(uid, "video", "video/mp4", data, thumb, {**meta, "provider": "local-ltx-video"})
    return [str(mid)]


def fail_job(job, msg):
    j = db.jobs.find_one({"_id": job["_id"]}) or job
    made = len(j.get("items") or [])
    refund = j["cost"] - made if j["kind"] == "image" else (j["cost"] if made == 0 else 0)
    quota_refund(j.get("qkey"), refund)
    jset(job["_id"], status="done" if made else "error", error=msg, step="تمام شد" if made else "ناموفق", pct=100 if made else j.get("pct", 0))


def run_job(jid):
    job = db.jobs.find_one({"_id": jid})
    if not job:
        return
    try:
        with GEN_SEM:
            jset(jid, status="running", step="شروع…", pct=3)
            items = do_image(job) if job["kind"] == "image" else do_video(job)
        if job["kind"] == "image" and len(items) < job["cost"]:   # بخشی ساخته نشد → سهمیهی همون بخش برگرده
            quota_refund(job.get("qkey"), job["cost"] - len(items))
        jset(jid, status="done", items=items, pct=100, step="آماده شد",
             error=(f"فقط {len(items)} از {job['cost']} تصویر ساخته شد؛ سهمیهی بقیه برگشت داده شد." if job["kind"] == "image" and len(items) < job["cost"] else None))
        trim_gallery(job["owner"])
    except UserError as e:
        fail_job(job, str(e))
    except Exception as e:
        log.exception("job failed")
        msg = "ساخت ناموفق بود؛ دوباره امتحان کن یا توضیح رو سادهتر بنویس."
        if DEBUG:
            msg += f"\n[{type(e).__name__}] {str(e)[:300]}"
        fail_job(job, msg)


def expire_if_stale(job):
    if job["status"] in ("queued", "running") and (now() - job["updated"]).total_seconds() > JOB_STALE_SEC:
        fail_job(job, "ساخت بیش از حد طول کشید و متوقف شد. دوباره امتحان کن.")
        return db.jobs.find_one({"_id": job["_id"]})
    return job


# ───────────────────────── API مینیاپ ─────────────────────────
@app.get("/")
def index():
    r = send_from_directory(os.path.dirname(os.path.abspath(__file__)), "index.html")
    r.headers["Cache-Control"] = "no-store"
    return r


@app.get("/health")
def health():
    return "ok"


def gallery(uid, limit=KEEP_PER_USER):
    return [public_media(m) for m in db.media.find({"owner": uid}, NO_BLOBS).sort("created", -1).limit(limit)]


@app.get("/api/me")
def api_me():
    uid = auth()
    if not uid:
        return jsonify(error="unauthorized"), 401
    return jsonify(
        quota={"image": quota_left(uid, "image"), "video": quota_left(uid, "video")},
        limits={"image": DAILY_IMAGES, "video": DAILY_VIDEOS},
        formats={k: {"label": v["label"], "ratio": v["ratio"]} for k, v in FORMATS.items()},
        styles=[{"id": k, "emoji": v[0], "label": v[1]} for k, v in STYLES.items()],
        models=model_catalog(),
        gallery=gallery(uid))


@app.post("/api/generate")
def api_generate():
    uid = auth()
    if not uid:
        return jsonify(error="unauthorized"), 401
    b = request.get_json(silent=True) or {}
    kind = b.get("kind")
    prompt = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", str(b.get("prompt", ""))).strip()[:MAX_PROMPT]
    if kind not in ("image", "video"):
        return jsonify(error="نوع خروجی معتبر نیست"), 400
    if len(prompt) < 5:
        return jsonify(error="توضیح خیلی کوتاهه؛ کمی بیشتر بنویس"), 400
    style = b.get("style") if b.get("style") in STYLES else "auto"
    fmt = b.get("fmt") if b.get("fmt") in FORMATS else ("story" if kind == "video" else "portrait")
    params = {"kind": kind, "prompt": prompt, "style": style, "fmt": fmt}
    def _int(v, d):
        try:
            return int(v)
        except (TypeError, ValueError):
            return d
    cat = model_catalog()

    def _pick(group, val):
        val = str(val or "auto")
        return val if any(m["id"] == val for m in cat[group]) else None
    params["model"] = _pick("image" if kind == "image" else "video", b.get("model"))
    if params["model"] is None:
        return jsonify(error="مدل انتخاب‌شده الان در دسترس نیست؛ یکی دیگر را انتخاب کن"), 400
    if kind == "image":
        params["count"] = min(4, max(1, _int(b.get("count"), 1)))
        cost = params["count"]
    else:
        params["image_model"] = _pick("image", b.get("image_model"))
        if params["image_model"] is None:
            return jsonify(error="مدل تصویر انتخاب‌شده الان در دسترس نیست"), 400
        params["scenes"] = min(5, max(3, _int(b.get("scenes"), 3)))
        params["voice"] = b.get("voice") if b.get("voice") in ("off", "female", "male") else "off"
        cost = 1

    act = db.jobs.find_one({"owner": uid, "status": {"$in": ["queued", "running"]}})
    if act:
        act = expire_if_stale(act)
        if act["status"] in ("queued", "running"):
            return jsonify(error="یه ساخت دیگه هنوز در حال انجامه؛ صبر کن تموم بشه", job_id=str(act["_id"])), 409
    qk = quota_take(uid, kind, cost)
    if not qk:
        return jsonify(error="سهمیهی امروزت برای این بخش کافی نیست؛ فردا دوباره امتحان کن"), 429
    jid = db.jobs.insert_one({"owner": uid, "kind": kind, "params": params, "status": "queued", "pct": 0, "step": "در صف…",
                              "items": [], "error": None, "qkey": qk, "cost": cost, "created": now(), "updated": now()}).inserted_id
    threading.Thread(target=run_job, args=(jid,), daemon=True).start()
    return jsonify(job_id=str(jid))


@app.get("/api/jobs/<jid>")
def api_job(jid):
    uid = auth()
    if not uid:
        return jsonify(error="unauthorized"), 401
    i = oid(jid)
    job = db.jobs.find_one({"_id": i, "owner": uid}) if i else None
    if not job:
        return jsonify(error="پیدا نشد"), 404
    job = expire_if_stale(job)
    items = []
    for mid in job.get("items") or []:
        m = db.media.find_one({"_id": oid(mid)}, NO_BLOBS)
        if m:
            items.append(public_media(m))
    return jsonify(status=job["status"], pct=job.get("pct", 0), step=job.get("step", ""), error=job.get("error"), items=items,
                   quota={"image": quota_left(uid, "image"), "video": quota_left(uid, "video")})


@app.get("/api/media")
def api_media():
    uid = auth()
    if not uid:
        return jsonify(error="unauthorized"), 401
    return jsonify(items=gallery(uid))


@app.delete("/api/media/<mid>")
def api_media_delete(mid):
    uid = auth()
    if not uid:
        return jsonify(error="unauthorized"), 401
    i = oid(mid)
    if i:
        db.media.delete_one({"_id": i, "owner": uid})
    return jsonify(ok=True)


@app.post("/api/media/<mid>/send")
def api_media_send(mid):
    """ارسال خروجی به چت خود کاربر با ربات (بهصورت عکس/ویدیو یا فایل با کیفیت کامل)"""
    uid = auth()
    if not uid:
        return jsonify(error="unauthorized"), 401
    i = oid(mid)
    m = db.media.find_one({"_id": i, "owner": uid}) if i else None
    if not m:
        return jsonify(error="پیدا نشد"), 404
    as_file = bool((request.get_json(silent=True) or {}).get("as_file"))
    cap = (m.get("caption", "") + ("\n\n" + " ".join(m.get("hashtags", [])) if m.get("hashtags") else "")).strip()[:1020]
    data, extra = bytes(m["data"]), {"chat_id": uid}
    if cap:
        extra["caption"] = cap
    if m["kind"] == "image":
        r = tg_upload("sendDocument", "document", "post.jpg", data, "image/jpeg", extra) if as_file else \
            tg_upload("sendPhoto", "photo", "post.jpg", data, "image/jpeg", extra)
    else:
        extra.update({"supports_streaming": "true", "width": m.get("w") or 720, "height": m.get("h") or 1280})
        r = tg_upload("sendDocument", "document", "reel.mp4", data, "video/mp4", extra) if as_file else \
            tg_upload("sendVideo", "video", "reel.mp4", data, "video/mp4", extra, thumb=bytes(m["thumb"]) if m.get("thumb") else None)
    if not r.get("ok"):
        return jsonify(error="ارسال به چت ناموفق بود. یه بار ربات رو /start کن و دوباره امتحان کن."), 502
    return jsonify(ok=True)


def _serve(mid, field):
    i = oid(mid)
    if not i or not hmac.compare_digest(request.args.get("s", ""), sign(mid)):
        return "forbidden", 403
    m = db.media.find_one({"_id": i})
    if not m:
        return "not found", 404
    mime = "image/jpeg" if field == "thumb" else m["mime"]
    ext = "jpg" if mime == "image/jpeg" else "mp4"
    resp = send_file(io.BytesIO(bytes(m[field])), mimetype=mime, conditional=True, max_age=86400,
                     download_name=f"post-{str(mid)[-6:]}.{ext}", etag=f"{mid}-{field}")
    return resp


@app.get("/media/<mid>")
def media_full(mid):
    return _serve(mid, "data")


@app.get("/media/<mid>/thumb")
def media_thumb(mid):
    return _serve(mid, "thumb")


# ───────────────────────── ربات مادر (مثل الگو) ─────────────────────────
@app.post("/mother")
def mother_hook():
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != MOTHER_SECRET:
        return "forbidden", 403
    msg = (request.get_json(silent=True) or {}).get("message")
    if msg and msg.get("chat", {}).get("type") == "private":
        tg(MOTHER_TOKEN, "sendMessage", chat_id=msg["chat"]["id"],
           text="🎨 سلام! اینجا با هوش مصنوعی تصویر و ویدیوی حرفهای برای پست و ریلز میسازی.\nفقط ایدهت رو بنویس؛ کپشن و هشتگ هم آماده میشه.",
           reply_markup={"inline_keyboard": [[{"text": "🚀 ورود به استودیو", "web_app": {"url": BASE_URL}}]]})
    return "ok"


def setup_mother():
    r = tg(MOTHER_TOKEN, "setWebhook", url=f"{BASE_URL}/mother", secret_token=MOTHER_SECRET, allowed_updates=["message"])
    tg(MOTHER_TOKEN, "setChatMenuButton", menu_button={"type": "web_app", "text": "استودیو", "web_app": {"url": BASE_URL}})
    log.info("mother webhook: %s", r)


setup_mother()
if LOCAL_ENABLED:
    threading.Thread(target=_local_boot, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)), threaded=True)