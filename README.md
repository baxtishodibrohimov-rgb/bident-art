# Cliniccards maosh hisoboti

Cliniccards API orqali **shu oyda kassaga tushgan pul** bo'yicha shifokorlar maoshini hisoblaydigan kichik dastur.
Ish qachon qilinganidan qat'i nazar, faqat tanlangan oyda tushgan to'lovlar hisobga olinadi.
Masalan, martda qo'yilgan breket uchun noyabrda tushgan 1 000 000 noyabr maoshiga kiradi.

## Nima qiladi

| Sahifa | Nima ko'rsatadi |
| --- | --- |
| **Bosh ekran** (`/`) | Tanlangan oy yoki sana oralig'i bo'yicha 5 ta summa: bajarilgan ishlar, kassaga tushgan, klinikaga (kassaning 20%), shifokorlarga, xarajatlar. Har birini bosganda tafsiloti ochiladi |
| **Bajarilgan ishlar** (`/ishlar`) | Shu davrda bajarilgan ishlar, shifokor bo'yicha: narx, to'langan, qoldiq |
| **Kassa** (`/kassa`) | Barcha to'lovlar (`GET /payments`), kassa bo'yicha |
| **Klinika ulushi** (`/klinika`) | Kassa × klinika foizi, kassa bo'yicha |
| **Shifokorlar** (`/shifokorlar`) | Kim qancha pul olib keldi, foizi va maoshi. Kassa jami bilan solishtirish («Farq») |
| **Xarajatlar** (`/xarajatlar`) | Cliniccards xarajatlari (`GET /spendings`), toifa bo'yicha |
| **Shifokor** (`/shifokor/<id>`) | Bemor, ish, ish sanasi, shu davrda tushgan pul, to'lov sanasi, shifokor ulushi |
| **Excel / Chop etish** | Har ikkala sahifadan tayyor ro'yxat |
| **Qarzdorlar** (`/qarzdorlar`) | Bajarilgan, lekin to'liq to'lanmagan ishlar: keyingi oylarda kutilayotgan pul |
| **Foizlar** (`/sozlamalar`) | Har bir shifokor foizi (API Cliniccards'dagi foizni bermaydi, shuning uchun bir marta shu yerda kiritiladi) |

«Shifokor belgilanmagan» qatori: ishda `done_by_doctor_id` bo'sh, ya'ni Cliniccards'da «Работы выполнил» to'ldirilmagan.
Bunday ishni Cliniccards'da tuzatish kerak.

## Xodimlar (shifokor bo'lmagan) va davomat

Kassa taqsimoti: klinika 20%, shifokorlar 25%, qolgan 55% xarajatlar. Xodimlar maoshi 55% ichidan hisoblanadi;
bosh ekranda «Kassaning taqsimoti» jadvali 55% dan qancha ishlatilgani va qolganini ko'rsatadi.

- **Xodimlar** (`/maosh-xodimlar`): shablon — ism-familiya, lavozim, kunlik summa, noqulay kun qo'shimchasi.
- **Grafik** (`/grafik`): har kuni kim kelgani; davr tanlanadi; katakni bosib qo'lda o'zgartirish mumkin.
- **Maosh** = ishlagan kunlar × kunlik summa + qo'shimcha kunlar × qo'shimcha summa. Qo'shimcha kuniga bir marta:
  dam olish kunida (`DAM_OLISH_KUNLARI`, standart yakshanba) ishlaganda yoki normadan ko'p ishlaganda.
- **Davomat boti**: har kuni `DAVOMAT_VAQTI` da (standart 20:00) «Davomat» vazifali xodimga: «Bugun klinika ishladimi?» →
  kimlar keldi (tugmalar bilan) → kimlar normadan ko'p ishladi. Natija adminlarga ham boradi.

## Sklad diagnostikasi

`/sklad/diagnostika` — Cliniccards sklad nakladnoylari API'da qanday turlar, omborlar va birliklar bilan kelishini ko'rsatadi.
Ombor qismini Cliniccards bilan bir xil qilib qayta qurishdan oldin shu sahifadagi ma'lumot kerak.

## Sklad va Telegram bot

«Sklad» bo'limi (`/sklad`) har bir mahsulot qoldig'ini yuritadi va bot orqali kunlik hisobot oladi.

**Qoldiq qanday hisoblanadi**
- «Cliniccards'dan yangilash» birinchi marta bosilganda qoldiq Cliniccards'dagi barcha sklad nakladnoylaridan hisoblanadi
  (kirim +, chiqim/hisobdan chiqarish −, skladlar orasidagi ko'chirish hisobga olinmaydi).
- Keyingi yangilashlarda faqat **yangi kirim** nakladnoylari qo'shiladi. Chiqim bot orqali yoziladi, shuning uchun
  Cliniccards'ga keyin kiritilgan chiqim nakladnoylari qayta ayrilmaydi.

**Kunlik tartib**
1. `SKLAD_SOROV_VAQTI` da (standart 19:00) bot «Oladi» xodimlardan so'raydi: «Bugun skladdan narsa oldingizmi?»
   Xodim istalgan vaqtda `/oldim` deb ham yozishi mumkin.
2. Xodim mahsulot nomini yozadi, ro'yxatdan tanlaydi va sonini kiritadi.
3. «Tekshiradi» xodimga: «Skladda shuncha qolishi kerak, to'g'rimi?» Noto'g'ri bo'lsa, haqiqiy sonni yozadi va
   qoldiq shunga tenglashtiriladi.
4. «Admin»ga: «Cliniccards'dan chiqaring: ...» — [Chiqarildi].
5. Qoldiq «chegara»dan kam bo'lsa, adminga taklif: «Sotuvchiga N ta, ~summa, yuborilsinmi?» — [Yuborish] → sotuvchiga
   buyurtma xabari — [Qabul qildim].

**Sozlash**
1. Railway Variables: `TELEGRAM_BOT_TOKEN`, `DB_FAYL=/data/hisobot.db` (ixtiyoriy: `SKLAD_SOROV_VAQTI`, `KLINIKA_NOMI`).
   Bot boshqa dasturda ishlatilmasligi kerak: bitta botdan faqat bitta dastur xabar o'qiy oladi.
2. «Sklad» → «Cliniccards'dan yangilash».
3. Har bir mahsulotga chegara va sotuvchini belgilang.
4. «Xodimlar va bot»: xodimlarni qo'shing, har biriga havolasini yuboring. «Sotuvchilar»: har bir sotuvchiga havolasini yuboring.
   Telegram boti odamga birinchi bo'lib yoza olmaydi, shuning uchun havolani ochib «Start» bosish shart.

## Kirish

Sahifa ochilganda dasturning o'z kirish oynasi chiqadi (`/login`): login, parol va «Eslab qolish (30 kun)».
Login va parol `HISOBOT_LOGIN` / `HISOBOT_PAROL` o'zgaruvchilaridan olinadi. Bir IP'dan 5 daqiqada 5 ta noto'g'ri urinishdan keyin kirish vaqtincha bloklanadi.
Yuqoridagi «Chiqish» tugmasi sessiyani tugatadi.

## Qanday hisoblaydi

1. `GET /planItems/DONE` va `GET /planItems/INVOICED`: Cliniccards'ning **butun davri** bo'yicha, `BOSHLANISH_SANA` (standart 2015-01-01) dan bugungacha, 6 oylik bo'laklarda.
   Server ishga tushganda bu ma'lumot fonda yuklanadi (taxminan 1 daqiqa), keyin barcha oylar uchun ishlatiladi.
2. Har bir ishning `payments` ro'yxatidan sanasi tanlangan oyga tushganlari yig'iladi.
3. `done_by_doctor_id` bo'yicha guruhlanadi va foiz qo'llanadi.
4. `GET /payments` bilan kassa jami olinadi. Farq ishga bog'lanmagan pulni ko'rsatadi: avans, depozit yoki hali bajarilmagan ish uchun to'lov.

API daqiqasiga 60 ta so'rovga ruxsat beradi; dastur 55 tadan oshirmaydi va natijani 15 daqiqa keshlaydi.
Yangi ma'lumot kerak bo'lsa, «Ma'lumotni yangilash» tugmasini bosing.

## 1-qadam: sinov (haqiqiy ma'lumotda)

```bash
git clone https://github.com/baxtishodibrohimov-rgb/bident-art && cd bident-art
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # CLINICCARDS_TOKEN va HISOBOT_PAROL ni yozing
python sinov.py --xodimlar  # xodimlar va ularning ID'lari
python sinov.py --oy 2026-11 --shifokor madina --foiz 25
```

Kutilgan natija: madina, noyabr 2026: tushgan **500 000**, maosh **125 000**.
Bu Cliniccards'dagi «Начисления» bilan mos kelishi kerak.

## 2-qadam: Railway'ga joylash

1. Railway → **New Project → Deploy from GitHub repo** → shu repozitoriyani tanlang.
2. **Variables** bo'limiga qo'shing:
   - `CLINICCARDS_TOKEN` — Cliniccards API kalit
   - `HISOBOT_LOGIN` — masalan `admin`
   - `HISOBOT_PAROL` — sahifaga kirish paroli
   - `FOIZLAR_FAYL` = `/data/foizlar.json`
   - `SESSION_SECRET` — ixtiyoriy, uzun tasodifiy matn (kirish sessiyasini imzolash uchun)
3. **Volume** qo'shing va uni `/data` ga ulang. Usiz shifokor foizlari har deploy'da o'chib ketadi.
4. **Settings → Networking → Generate Domain**: `https://...up.railway.app` manzili chiqadi, HTTPS avtomatik.

Ishga tushirish buyrug'i va `/health` tekshiruvi `railway.json` da berilgan.

## Muqobil: o'z serveringizga o'rnatish (Contabo VPS)

```bash
sudo useradd -r -m -d /opt/cliniccards_hisobot hisobot
sudo cp -r bident-art/. /opt/cliniccards_hisobot/
cd /opt/cliniccards_hisobot
sudo -u hisobot python3 -m venv .venv
sudo -u hisobot .venv/bin/pip install -r requirements.txt
sudo -u hisobot cp .env.example .env && sudo -u hisobot nano .env
sudo chmod 600 .env
sudo cp deploy/cliniccards-hisobot.service /etc/systemd/system/
sudo systemctl enable --now cliniccards-hisobot
```

Dastur `127.0.0.1:8090` da ishlaydi. Tashqaridan kirish uchun oldiga nginx va HTTPS (masalan, Let's Encrypt) qo'ying.
Parol oddiy HTTP orqali ochiq yuborilmasligi kerak.

## Testlar

```bash
pip install pytest
python -m pytest tests
```

Testlar soxta API bilan ishlaydi, haqiqiy kalit kerak emas.

## Cheklovlar va e'tibor beriladigan joylar

- API hujjatdagidan farq qilishi mumkin. Avval `sinov.py` bilan haqiqiy natijani Cliniccards bilan solishtiring.
- Foizlar Cliniccards'dagi Настройки → Зарплата bilan bir xil bo'lishi kerak.
- Klinika valyutasi UZS, narx esa USD bo'lsa, summalar API qaytargan valyutada ko'rsatiladi.
- Cliniccards to'lovni avval eng eski qarzga yozadi. Dastur Cliniccards qaysi ishga yozgan bo'lsa, shuni ko'rsatadi.
