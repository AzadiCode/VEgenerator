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
