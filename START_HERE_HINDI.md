# Bhai, deploy aise karna hai

1. ZIP extract kar. Andar ki saari files GitHub repo ke root mein replace/upload kar; `.github` folder bhi include kar. Sirf `app.py` change mat karna: scanner, commands aur storage ab saath update honge.
2. Render mein **existing service** use kar, Runtime **Docker** rakho. Dockerfile `./Dockerfile`, context `.`. Purana custom Start Command `python app.py` hata do. Image khud `python serve.py` se teen processes start karegi.
3. Existing bot token, chat ID, database URL aur RUN_SECRET rakho. Naya `DASHBOARD_PASSWORD` add kar, minimum 12 characters ka unique password. `COINGLASS_ENABLED=false`, `SYSTEM_ALERTS_ENABLED=true`, `TOP_N=15`, `SCAN_BUDGET_SECONDS=900` rakho. Group chat hai to `TELEGRAM_OWNER_ID` bhi chahiye.
4. Manual Deploy kar. Database delete/reset nahi karna. Existing watchlist aur history reuse hogi; naye jobs/indexes automatically add honge.
5. Browser mein `https://YOUR-SERVICE.onrender.com/health` khol: 200 alive milna chahiye. `/ready` database aur dono workers ready hone par 200 dega. Service root `/` par dashboard password se login hoga.
6. UptimeRobot HTTPS monitor mein wahi `/health` URL, 5-minute interval rakho. GET aur HEAD dono supported hain. `/ready` separate monitor rakh sakte ho. Actual 48-slot delivery ka hisaab Telegram `/health` aur daily digest mein milega.
7. Telegram `/help`, `/status`, `/daily`, `/coin BTC`, `/top`, `/below70`, `/research` check kar. `/help` mein daily/weekly buttons bhi hain.

## Supabase connection

Supabase project ke **Connect** panel se PostgreSQL URI copy karo. API anon/service-role key nahi chahiye. IPv4 connectivity ke liye reachable session pooler use karo; password URI mein URL-encoded hona chahiye. App TLS use karta hai aur pooler compatibility ke liye prepared statements disable hain. Secrets ko GitHub files mein paste mat karna.

## Kya badla

- Bot commands aur scanning alag processes mein hain, ek hi free Render service ke andar.
- Coinglass default off hai; memory guard aur optional capture code retained hain. Main RSI exchange ke completed candles se aayegi.
- CMC ka complete returned count aur unique IDs verify karke local volume ranking hoti hai. Wrong/incomplete source par fake top500 nahi banegi.
- Bitget daily history pagination fix hai. KuCoin gaps blank/unavailable rahenge.
- Excel mein clean source rows, coverage counts, correct scan timing aur future `NOT_DUE` slots hain.
- Failure/recovery aur daily delivery summary system alerts hain. Coin-price/trading alerts nahi hain.
- `/expand BTC` last 30 observed days dega; `/coin BTC` seven-day chart; `/research` prospective forward returns dega.

## Dhyan dene wali actual limits

Free host par 48 successful files roz guarantee nahi ho sakti. Sleep, provider restart, quota, database failure ya regional HTTP 403/451 se gap aa sakta hai. App usko clearly record karega. Futures request blocked hone par Binance Spot ko chupke se substitute nahi karega; ye tera original Futures-first rule hai.

Nayi service 06:03 IST ke baad start hui aur valid baseline database mein nahi hai, to us din admission paused rahega; next valid 06:00 capture ka wait hoga. Existing watchlist tracking phir bhi chalegi. Deploy ke baad kuch din `/research` mein immature/missing outcomes normal hain; purani XLSX se unavailable history invent nahi hoti.

## Weekly backup enable karna

GitHub repo → Settings → Secrets and variables → Actions mein `BACKUP_DATABASE_URL` aur random `BACKUP_ENCRYPTION_KEY` (32+ characters) add karo. Actions → Weekly encrypted database backup → Run workflow ek baar chalao. Successful encrypted artifact upload ke baad hi database backup status record hoga. Encryption key alag safe jagah save karo. Restore test ek nayi empty database par karna hai; backup setup is ZIP se apne aap tumhare account mein configure nahi hua.

First deploy se pehle Actions ke deployment checks dekh lena. Local tests aur public APIs verify hue hain; tumhari Render service/real PostgreSQL par live rollout abhi verify nahi hua. Details `VALIDATION.md` mein hain.
