# Phone se Render + Telegram setup

## 1. Telegram bot banao

1. Telegram ke official **@BotFather** mein `/newbot` bhejo. Name aur unique username do.
2. Mila hua token private rakho. Naya bot open karke **Start** dabao, ek message bhejo.
3. Apna numeric private chat ID chahiye. Apne device par Telegram ki official Bot API
   `getUpdates` method se private message ka `message.chat.id` dekho. Token wali address ko
   screenshot/share/commit mat karo. Bot private chat mein chat ID aur user ID same hote hain.
   Setup ke baad bot polling karega, tab doosra getUpdates poller mat chalao.

   Official request template (apne browser par `YOUR_TOKEN` replace karo):
   `https://api.telegram.org/botYOUR_TOKEN/getUpdates`
   Response mein `result` → `message` → `chat` → `id` number copy karo.

## 2. Durable free PostgreSQL ready karo

Free external PostgreSQL (e.g. Supabase) project banao. Provider ke **Connect** panel se
PostgreSQL connection string copy karo. Render-compatible IPv4 **session pooler** connection
prefer karo; actual host/port/user provider se lo. Database password ko URL-encode karo agar
usmein `@`, `:`, `#`, `/` jaise special characters hain. App SSL require karta hai.

Yeh value `DATABASE_URL` hogi. Render ka free filesystem history ke liye durable nahi hai.
Free database ki storage/usage/pause limits hoti hain; provider dashboard par dekhte raho.
Project/password/connection URL kisi public repo mein mat daalo.

## 3. ZIP GitHub par upload karo

1. ZIP extract karo. Phone file manager se bhi kar sakte ho.
2. GitHub mein naya **private repository** banao.
3. Extracted files repo ke **root** mein upload karo. Root mein `render.yaml`, `Dockerfile`,
   `app.py`, `requirements.txt` seedhe dikhne chahiye. Sirf ZIP file upload mat karna.
4. Hidden `.dockerignore`, `.gitignore` aur `.env.example` bhi include karo. Real `.env` nahi.

## 4. Render par deploy karo

1. Render dashboard → **New → Blueprint** → GitHub repository connect karo.
2. `render.yaml` detect hoga. Resource **Web Service**, runtime **Docker**, plan **Free** check karo.
3. Required environment fields fill karo:

| Name | Kya daalna hai |
|---|---|
| TELEGRAM_BOT_TOKEN | BotFather token |
| TELEGRAM_CHAT_ID | Tumhara numeric private chat ID |
| DATABASE_URL | External PostgreSQL connection string |
| RUN_SECRET | Blueprint automatically generates; manual setup mein random32+chars |

4. Blueprint create/deploy karo. Chromium ke kaaran first build ko time lag sakta hai.
5. Agar Blueprint option use nahi karna: New Web Service → repo → Docker → **Free** →
   health path `/health`; same env vars add karo; start command Dockerfile se aayega.
6. Group mein use karna ho to `TELEGRAM_CHAT_ID` group ka negative ID rakho aur
   `TELEGRAM_OWNER_ID` apna personal numeric user ID add karo. Bot ko document-send permission do.
7. Deploy ke baad Render ka actual `https://...onrender.com` URL copy karo.
8. `/health` par `alive`, `/ready` par `true` check karo. `ready=false` ho to Render logs mein
   missing-config/database diagnosis dekho. Secrets logs mein paste mat karo.

## 5. UptimeRobot monitor

1. UptimeRobot → Add New Monitor → HTTP(s).
2. URL = tumhara **actual Render URL + `/health`**.
   Example: `https://YOUR-ACTUAL-SERVICE.onrender.com/health` (placeholder ko replace karna).
3. Interval **5minutes**, expected status **200**, method **GET**, monitor active rakho.
4. Is monitor ko `/run` ya Telegram API par point mat karna. Scheduler report timing khud sambhalta hai.

Free Render15minute idle par sleep kar sakta hai.5minute requests idle sleep avoid karne mein
madad karti hain, lekin monthly free-hours quota, cold starts, maintenance, resource limits,
provider policies aur browser/API access ki wajah se **forever uptime/roz48successful reports
guarantee nahi hoti**. `/health` process ka heartbeat hai; source completeness `/status` aur Excel
Data Status mein dikhegi. Chromium512MB plan par memory limit hit kar sakta hai.

## 6. Telegram se operate karo

- `/start` ya `/help`: commands.
- `/status`: latest scan aur data quality.
- `/daily`: latest stored Excel dubara.
- `/weeklycoin`: last7 complete IST trading days ki336-slot coverage.
- `/expand BTC` ya `/EXPANDBTC`: stored coin history + observed OHLCV.
- `/del BTC,ETH` ya `/DEL(BTC,ETH)`: selected coins hatao; history rahegi.
- `/delall`: active list clear; removed coins auto re-add nahi honge.
- `/allow BTC,ETH`: removed coins par admission suppression hatao. Entry rules phir bhi apply honge.

Deployment ke baad first current-slot report late-capture label ke saath aa sakti hai. New
entries ke liye next valid06:00 IST baseline zaroori hai. Qualifying coins na hon to empty
watchlist valid hai; Daily Gainers alag sheet hai. Fake qualifying coins add nahi hote.
No separate trade alerts. Har scheduled report silent XLSX document hai.

## Agar report partial/blank aaye

- **BASELINE_MISSING:** next valid06:00 capture ka wait.
- **CMC incomplete/schema/access error:** top500 verify nahi hua, new entries paused.
- **Coinglass unavailable/stale/pagination error:** CG columns blank; exchange-rule RSI separately visible.
- **HTTP403/NETWORK_UNAVAILABLE:** public exchange blocked/unreachable from that region.
  Iska matlab coin not-listed nahi. Spot par silently switch nahi hota.
- **INSUFFICIENT_CLOSED_HISTORY:**100 contiguous closed bars nahi; naya coin qualify nahi hota.
- **SCAN_BUDGET_EXCEEDED:** retained list/public-source response workload current resources se zyada.
- **UNCERTAIN delivery:** Telegram upload ka acceptance confirm nahi; `/daily` explicit repeat hai.
- Bot response slow ho to active scan khatam hone do; commands same serialized queue mein chalti hain.
- Agar existing webhook conflict aaye: dedicated new bot use karo; app existing webhook delete nahi karta.

Repo/image redeploy se PostgreSQL history bachi rahegi. Database URL same rakho, ek hi active
service/bot poller chalao. Paid upgrade automatically nahi hota; changes tumhare dashboard se honge.
