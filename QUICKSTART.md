# شروع سریع (Quickstart)

این پوشه نسخهٔ قابل انتشار پروژه است: کد، تست‌ها، docs و یک فایل تنظیمات
**نمونه** — نه `config.json` واقعی. برای اجرا، نمونه را کپی کنید و
اطلاعات ورود خودتان را پر کنید.

## ۱) پیش‌نیاز

- Python 3.11 یا بالاتر (روی ویندوز `py -3` هم قبول است)
- اتصال اینترنت (نوبیتکس + مدل زبانی)

## ۲) ساخت `config.json`

```bash
# Linux / macOS
cp config.example.json config.json

# Windows (cmd / PowerShell)
copy config.example.json config.json
```

`config.json` **هرگز** نباید کامیت شود؛ در `.gitignore` هست. فایل نمونه
فقط برای این ساخته شده که ساختار کامل را نشان بدهد.

## ۳) پر کردن فیلدهای حیاتی

در `config.json` این‌ها را پر کنید (بقیهٔ فیلدها مقدار پیش‌فرض امن دارند):

| کلید | توضیح |
| --- | --- |
| `exchange.nobitex_username` | نام کاربری نوبیتکس |
| `exchange.nobitex_password` | پسورد حساب نوبیتکس |
| `exchange.nobitex_totp_secret` | سکرت 2FA (Google Authenticator) |
| `exchange.nobitex_api_key` | اختیاری؛ اگر توکن API دارید، به‌جای سه مورد بالا |
| `ai.llm_api_key` | کلید FreeLLMAPI برای لایهٔ تحلیل زبانی |
| `web.password` | پسورد داشبورد |

اگر خواستید پسورد داشبورد هش‌شده ذخیره شود (توصیه‌شده):

```bash
python -m nobitex_bot.webauth --set "your-password"
```

برای اولین اجرا، `exchange.mode` را `demo` بگذارید و `exchange.live_trading`
را `false` نگه دارید تا هیچ سفارش واقعی ارسال نشود.

## ۴) اجرا

```bash
start.bat            # Windows (نصب خودکار وابستگی‌ها)
python run.py        # Linux / macOS
```

سپس داشبورد: `http://127.0.0.1:<engine.server_port>` — پورت از خودِ
`config.json` خوانده می‌شود (نه یک عدد ثابت)؛ `start.bat` در خروجی اولین
خط آدرس کامل را چاپ می‌کند و `NOBOT_PORT` آن را برای یک اجرا عوض می‌کند.

## ۵) قفل شدن پنل پس از سه پسورد اشتباه

بعد از `web.max_attempts` (پیش‌فرض ۳) پسورد اشتباه، پنل قفل می‌شود و قفل در
`data/web_auth.json` می‌ماند تا با ری‌استارت هم باز نشود. برای باز کردن، در
`config.json` داخل بخش `web` مقدار `lock_panel` را از `1` به `0` تغییر بدهید:

```json
"web": { "password": "...", "max_attempts": 3, "lock_panel": 0 }
```

همان لحظه باز می‌شود و پسورد همچنان لازم است؛ برای مسلح کردن دوبارهٔ قفل
`1` کنید. تغییر پسورد (`web.password`) هم قفل را پاک می‌کند. هیچ‌کدام
ری‌استارت لازم ندارند؛ مقدار از فایل زنده خوانده می‌شود.
