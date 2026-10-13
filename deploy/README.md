# داشبورد روی HTTPS با پورت ۸۷۸۷ (پشت پروکسی با احراز هویت)

> نمونه‌های این پوشه همه روی پورت ۸۷۸۷ نوشته شده‌اند؛ اگر `engine.server_port`
> در config.json عدد دیگری است (روی این نصب **۹۹۹۹**)، همان عدد را جای ۸۷۸۷
> بگذارید.

خواسته: آدرس داشبورد همان **پورت ۸۷۸۷** بماند، ولی روی **HTTPS** و پشت یک لایهٔ
احراز هویت؛ خود ربات هم روی `127.0.0.1` بماند.

```
مرورگر شما ──https://v42.ir:8787──▶ Caddy  (public IP فقط، TLS + پسورد پروکسی)
                                              │  HTTP روی loopback:8787
                                              ▼
                                    ربات: 127.0.0.1:8787
```

**چرا این دو با هم روی یک پورت تعارض ندارند:** ربات به آدرس **loopback**
(`127.0.0.1:8787`) بایند می‌شود و Caddy فقط روی **IP عمومی** همان پورت
(`185.165.30.181:8787`). این دو سوکت متفاوت‌اند، پس هر دو سر جایشان می‌مانند:
درخواست از سیستم خودت به Caddy می‌رسد و درخواست از خود سرور مستقیم به ربات.
⚠️ تنها کاری که این توازن را می‌شکند این است که `engine.server_host` را روی
`0.0.0.0` بگذاری؛ آن‌وقت Caddy نمی‌تواند بایند کند (پورت اشغال می‌شود).

فایل‌های این پوشه:

| فایل | کار |
|---|---|
| [Caddyfile](deploy/Caddyfile) | گزینهٔ انتخابی: Caddy، TLS + `basic_auth`، روی پورت ۸۷۸۷ |
| [nginx-v42.ir.conf](deploy/nginx-v42.ir.conf) | فقط اگر nginx نصب می‌کنی (جایگزین) |
| [nobitex-bot.service](deploy/nobitex-bot.service) | ربات را زنده نگه می‌دارد |

---

## ۰) قبل از هر کار، روی سرور

```bash
dig +short v42.ir                    # باید IP همین سرور باشد (185.165.30.181)
sudo ss -ltnp | grep -E ':80|:443|:8787'   # چه کسی این پورت‌ها را گرفته؟
```

- اگر ۸۰/۴۴۳ دست یک سرویس دیگر است، **دست نزن**؛ این چیدمان اصلاً به آن‌ها
  کاری ندارد (به همین دلیل گواهی ما از CA داخلی Caddy می‌آید، نه Let’s Encrypt).
- اگر ۸۷۸۷ در لیست نبود، عالی؛ یعنی Caddy می‌تواند آن را بگیرد.
- اگر دامنه اصلاً به این سرور اشاره نمی‌کند، اول A رکورد را درست کن (یا از بلاک
  IP در انتهای Caddyfile استفاده کن و با `https://185.165.30.181:8787` باز کن).

## ۱) ربات را زنده کن (systemd)

```bash
sudo useradd -r -s /usr/sbin/nologin -d /opt/nobitex-bot nobitex
sudo mkdir -p /opt/nobitex-bot && sudo chown -R nobitex:nobitex /opt/nobitex-bot
# کد را در /opt/nobitex-bot بریز (rsync/git)، بعد:
cd /opt/nobitex-bot
sudo -u nobitex python3 -m venv .venv                 # بدون pip install (فقط stdlib)
sudo -u nobitex ./.venv/bin/python run.py --doctor    # باید 0 FAIL بدهد
sudo -u nobitex ./.venv/bin/python -m nobitex_bot.webauth --set "پسورد-داشبورد"

sudo cp deploy/nobitex-bot.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now nobitex-bot
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8787/api/auth/status   # 200
```

`engine.server_host` باید `127.0.0.1` بماند (پیش‌فرض خودش همین است). هشدار
«dashboard bind» در `--doctor` با این معماری **طبیعی** است.

نمای زنده پیش‌فرض **خاموش** است (هر چرخه یک `status()` اضافه می‌خواهد و این
ربات باید هفته‌ها روی یک VPS کوچک بچرخد)، پس `journalctl -u nobitex-bot -f`
فقط بنر شروع و خطوط `[web]` را نشان می‌دهد. اگر برای یک نشست عیب‌یابی
می‌خواهی هر تصمیم/سفارش/ریسک یک خط و هر چرخه یک خط خلاصهٔ حساب ببینی،
`--live` را موقتاً به `ExecStart` اضافه کن و بعد از کار حذفش کن.

## ۲) Caddy روی پورت ۸۷۸۷

```bash
sudo apt install --yes debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo chmod o+r /usr/share/keyrings/caddy-stable-archive-keyring.gpg
sudo chmod o+r /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install caddy

sudo mkdir -p /var/log/caddy && sudo chown caddy:caddy /var/log/caddy
sudo cp deploy/Caddyfile /etc/caddy/Caddyfile
caddy hash-password --plaintext 'پسورد-پروکسی'    # خروجی را جای REPLACE_ME بگذار
sudo nano /etc/caddy/Caddyfile                    # IP در bind + نام دامنه + هش
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy && systemctl status caddy --no-pager
```

سه نکته که در فایل رعایت شده و از مستندات رسمی Caddy آمده است:

- **`auto_https disable_redirects`**: وگرنه Caddy برای ریدایرکت HTTP→HTTPS
  می‌خواهد پورت ۸۰ را بگیرد که مال سرویس دیگر است و **بالا نمی‌آید**.
- **`bind 185.165.30.181`**: چون `bind` فقط *هاست* می‌گیرد (نه پورت)، پورت از
  آدرس سایت (`v42.ir:8787`) می‌آید و ربات روی loopback همان پورت دست‌نخورده
  می‌ماند.
- **`tls internal`**: گواهی را CA داخلی خودِ Caddy امضا می‌کند؛ پس نه پورت ۸۰
  لازم است نه ۴۴۳. نام دستور پسورد هم از Caddy 2.8 به بعد `basic_auth` است
  (قبلاً `basicauth` بود).

### حذف هشدار مرورگر (یک‌بار روی سیستم خودت)

```bash
# روی سرور: مسیر ریشهٔ CA را پیدا کن
sudo find /var/lib/caddy -name root.crt
# و روی سیستم خودت آن را کپی و نصب کن:
scp root@185.165.30.181:/var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt .
```

روی ویندوز: دوبار کلیک روی `root.crt` → *Install Certificate* → **Local Machine**
→ *Place all certificates in the following store* → **Trusted Root Certification
Authorities** → Finish، و مرورگر را کامل ببند و باز کن. بعد از این
`https://v42.ir:8787` بدون هشدار باز می‌شود. (اگر گواهی واقعی داری — از
Let’s Encrypt با DNS‑01 یا فایل موجود — همان دو خط `tls fullchain.pem
privkey.pem` را در Caddyfile جای `tls internal` بگذار.)

## ۳) تأیید از سیستم خودت

```bash
curl -sk -o /dev/null -w '%{http_code}\n' https://v42.ir:8787/api/auth/status
#   401  → لایهٔ پروکسی سر جایش است (پسورد پروکسی را می‌خواهد)
curl -sk -u ops:'پسورد-پروکسی' https://v42.ir:8787/api/auth/status
#   {"password_set":true,...} → به داشبورد رسیدیم (لایهٔ ۲ هم روشن است)
curl -s -o /dev/null -w '%{http_code}\n' http://v42.ir:8787/     # نباید HTML بدهد
```

بعد در مرورگر `https://v42.ir:8787` → پرامپت پسورد پروکسی → صفحهٔ ورود فارسی →
ورود. **یک POST را هم امتحان کن** (ذخیرهٔ یک تنظیم یا دکمهٔ توقف/شروع) تا مطمئن
شوی گارد CSRF راضی است.

## ۴) عیب‌یابی

| نشانه | علت و راه‌حل |
|---|---|
| هشدار گواهی مرورگر | ریشهٔ CA را نصب نکرده‌ای (بخش قبل) یا با IP باز می‌کنی نه با نام |
| `connection refused` روی ۸۷۸۷ | Caddy بالا نیست (`systemctl status caddy`) یا `bind` اشتباه است |
| Caddy بالا نمی‌آید / `address already in use` | کسی روی همان پورت `0.0.0.0` گوش می‌دهد: `sudo ss -ltnp \| grep 8787`؛ اگر خودِ ربات با `server_host: 0.0.0.0` است، به `127.0.0.1` برگردان |
| `502 Bad Gateway` | ربات بالا نیست: `journalctl -u nobitex-bot -n 50` |
| `403` روی POSTها | در Caddy نباید پیش بیاید (خودش `Host` را رد می‌کند)؛ در nginx یعنی `proxy_set_header Host $http_host;` جا افتاده |
| ورود نوبیتکس `429/403` | IP سرور ایرانی نیست (`run.py --doctor` کشور IP خروجی را چاپ می‌کند) |
| «از خانه باز نمی‌شود» ولی `--doctor` می‌گوید بایند OK است | اول بپرس **از بیرون** پورت باز است یا نه، نه از خود سرور (درخواست از سرور به IP عمومی خودش فایروال را دور می‌زند): `curl -s 'https://check-host.net/check-tcp?host=185.165.30.181:8787&output=json'` و بعد `curl -s https://check-host.net/check-result/<request_id>`؛ اگر نودها وصل شدند مشکل سمت مرورگر/پروکسی سیستم خودت است، وگرنه فایروال پروایدر را باز کن |
| یک سرویس نامرتبط روی 8787 چیست؟ | `--doctor` حالا مالک را از خودِ داشبورد جدا می‌کند (اول `/api/auth/status` را می‌پرسد، بعد با `SO_EXCLUSIVEADDRUSE` بایند می‌کند) — اگر سرویس دیگری پورت را گرفته باشد، `ss -ltnp \| grep 8787` (لینوکس) یا `netstat -ano \| findstr :8787` + `tasklist /FI "PID eq <pid>"` (ویندوز) مالک را نشان می‌دهد |
| هشدار «dashboard bind» در `--doctor` | با این معماری طبیعی است؛ با `0.0.0.0` «حل» نکن |

## ۵) امنیت این چیدمان

- در فایروال فقط همین یک پورت لازم است: `sudo ufw allow 8787/tcp`. نکتهٔ ظریف:
  پورت **۸۷۸۷** حالا مال Caddy است؛ خودِ ربات روی همان شماره پورت ولی فقط روی
  loopback گوش می‌دهد، پس هیچ درخواستی از بیرون مستقیم به خودِ ربات نمی‌رسد.
- فایل پسورد پروکسی و هش داخل Caddyfile **رمز** هستند: در گیت نگذار
  ([deploy/.gitignore](deploy/.gitignore)) و دسترسی‌شان را محدود کن.
- هر دو لایه را روشن نگه دار. برای سخت‌تر کردن: بلاک `@stranger` در Caddyfile
  (allow-list روی IP خودت).
- `web.allow_remote_without_password` را `false` نگه دار.
