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
JOB_STALE_SEC = 900

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


def build_prompt(base, style, fmt, variant=0):
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


def gen_image(prompt, fmt, seed):
    errs = []
    for name in IMAGE_PROVIDERS:
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
    for i in range(n):
        jset(jid, step=f"در حال ساخت تصویر {i + 1} از {n}…", pct=12 + int(80 * i / n))
        try:
            raw, prov = gen_image(build_prompt(plan["image_prompt"], style, fmt, i), fmt, random.randint(1, 2 ** 31 - 1))
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

    jset(jid, step="در حال ساخت ویدیوی AI…", pct=10)
    ai = ai_video_pollinations(build_prompt(plan["image_prompt"], style, fmt), fmt)
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
            raw, prov_used = gen_image(build_prompt(sc["prompt"], style, fmt), fmt, random.randint(1, 2 ** 31 - 1))
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
        with FFMPEG_SEM:
            render_video(paths, [s["motion"] for s in scenes], fmt, per, vpath, out)
        data = open(out, "rb").read()
        if len(data) > 15 * 1024 * 1024:
            raise UserError("حجم ویدیو زیاد شد؛ با مدت کمتر دوباره امتحان کن.")
        jset(jid, step="در حال ذخیره…", pct=96)
        mid = save_media(uid, "video", "video/mp4", data, first_thumb, {**meta, "provider": prov_used + "+ffmpeg"})
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
    if kind == "image":
        params["count"] = min(4, max(1, _int(b.get("count"), 1)))
        cost = params["count"]
    else:
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

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)), threaded=True)