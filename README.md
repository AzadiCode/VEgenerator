# Post Studio — استودیوی پست هوشمند

فایل‌ها: `app.py` (سرور + ربات)، `index.html` (مینی‌اپ)، `requirements.txt`

## اجرا (مثلاً روی Render)
- Build: `pip install -r requirements.txt`
- Start: `gunicorn app:app --workers 1 --threads 8 --timeout 180`
  (حتماً `--workers 1`؛ صف ساخت و سمافورها داخل حافظهٔ پروسه‌اند)

## متغیرهای محیطی
ضروری: `MOTHER_TOKEN`، `BASE_URL`
پیشنهادی: `MONGO_URI` (بدون آن با هر ریستارت همه‌چیز پاک می‌شود)
منابع رایگان (همه اختیاری، هر کدام را داشتی بگذار):
- `POLLI_KEY` (enter.pollinations.ai) ← بدون واترمارک + امکان ویدیوی AI با `ENABLE_AI_VIDEO=1`
- `CF_ACCOUNT_ID` + `CF_API_TOKEN` ← Cloudflare Workers AI
- `HF_TOKEN` ← Hugging Face (FLUX.1-schnell)
- `TOGETHER_KEY` ← Together AI (اگر مدل رایگانشان فعال بود)
- AI Horde بدون کلید کار می‌کند
متن (کپشن/پرامپت): `AI_BASE_URL` + `AI_API_KEY` + `AI_MODEL` (اختیاری؛ وگرنه Pollinations Text)
ترتیب تلاش منابع: `IMAGE_PROVIDERS=pollinations,cloudflare,huggingface,together,horde`
سقف‌ها: `DAILY_IMAGES`، `DAILY_VIDEOS`، `MAX_JOBS`، `DEBUG=1` (نمایش خطای فنی)

---

## مدل‌های لوکال (رایگان و بدون سقف) — اختیاری

به‌صورت پیش‌فرض **خاموش** است و پروژه دقیقاً مثل قبل کار می‌کند. برای روشن کردن:

1. نصب: `pip install -r requirements-local.txt` (روی سرور بدون GPU برای torch نسخهٔ CPU: `--extra-index-url https://download.pytorch.org/whl/cpu`)
2. متغیر محیطی: `LOCAL_MODELS=1`
3. ریستارت. در لاگ استارت می‌بینی کدام مدل `READY` است و کدام به چه دلیلی خاموش است.

کاربر داخل مینیاپ بخش «مدل تصویر» / «موتور ویدیو» را می‌بیند و **فقط مدل‌های آماده** نمایش داده می‌شوند.

| مدل | نوع | سخت‌افزار | توضیح |
|---|---|---|---|
| `sd-turbo` | تصویر | CPU یا GPU | سبک و سریع، ۲ مرحله |
| `sdxl-turbo` | تصویر | GPU توصیه می‌شود | جزئیات بهتر |
| `flux-schnell` | تصویر | فقط GPU (۱۶GB+ با offload) | بهترین کیفیت |
| `parallax` | ویدیو | CPU یا GPU | عمق‌یابی (Depth-Anything-V2-Small) + حرکت دوربین سه‌بعدی روی تصاویر صحنه‌ها |
| `ltx-video` | ویدیو | فقط GPU | ویدیوی AI واقعی از متن (هر صحنه یک کلیپ) |

برای ویدیو دو انتخاب جدا هست: «موتور ویدیو» و «مدل تصویر صحنه‌ها» (برای `parallax` و حالت چندصحنه).

### متغیرهای محیطی لوکال
- `LOCAL_MODELS=1` روشن‌کردن موتور لوکال
- `LOCAL_DEVICE=auto|cuda|mps|cpu` (پیش‌فرض auto)
- `MODEL_CACHE_DIR=/var/data/models` مسیر ذخیرهٔ مدل‌ها؛ حتماً روی دیسک پایدار بگذار وگرنه با هر دیپلوی دوباره دانلود می‌شوند
- `LOCAL_IMAGE_MODELS=sd-turbo,sdxl-turbo,flux-schnell` و `LOCAL_VIDEO_MODELS=parallax,ltx-video` برای محدود کردن فهرست
- `LOCAL_PRELOAD=sd-turbo` دانلود و لود مدل هنگام استارت تا اولین کاربر منتظر نماند
- `LOCAL_FALLBACK=1` (پیش‌فرض) اگر مدل لوکال خطا داد، خودکار سراغ سرویس‌های آنلاین/حالت چندصحنه می‌رود؛ با `0` خطا به کاربر نشان داده می‌شود
- `LTX_FRAMES=65` و `LTX_STEPS=30` طول و کیفیت هر کلیپ LTX
- `DEPTH_MODEL` مدل عمق‌یابی (پیش‌فرض `depth-anything/Depth-Anything-V2-Small-hf`)
- `JOB_STALE_SEC` (با فعال بودن لوکال پیش‌فرض ۲۴۰۰ ثانیه)

### نکات مهم
- مدل‌های لوکال همزمان فقط یک‌کاره اجرا می‌شوند (یک مدل سنگین در حافظه)، پس `--workers 1` همچنان لازم است.
- پلن رایگان Render (بدون GPU و با RAM کم) برای این مدل‌ها مناسب نیست؛ VPS با حداقل ۸GB RAM برای `sd-turbo`/`parallax` یا سرور GPU لازم است.
- دانلود اولین‌بار حجیم است (چند GB تا ده‌ها GB برای FLUX).
- لایسنس هر مدل را برای استفادهٔ تجاری خودت چک کن (FLUX.1-schnell و Depth-Anything-V2-Small آپاچی ۲.۰ هستند؛ SD-Turbo/SDXL-Turbo شرایط Stability AI را دارند).
