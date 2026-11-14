"""«Telegram bot» sahifasidagi suhbat namunalari: bot kim bilan qanday gaplashadi."""

# [kimdan ("bot" | "men"), matn, tugmalar]
CHATS = {
    "Sklad hisoboti": {"who": "Xodim · Oladi", "msgs": [
        ("bot", "Assalomu alaykum, Aziza!\nBugun skladdan biror narsa oldingizmi?", ["Ha, oldim", "Yo'q"]),
        ("men", "Ha, oldim", None),
        ("bot", "Qaysi mahsulotni oldingiz? Nomini (yoki bir qismini) yozing.", None),
        ("men", "plomba", None),
        ("bot", "Qaysi biri?", ["Kompozit plomba A2 (7 dona)"]),
        ("men", "Kompozit plomba A2", None),
        ("bot", "Kompozit plomba A2 — nechta oldingiz? (dona)", None),
        ("men", "3", None),
        ("bot", "Yozildi:\n• Kompozit plomba A2 — 3 dona", ["Yana qo'shish", "Tugatdim"]),
        ("men", "Tugatdim", None),
        ("bot", "Rahmat! Hisobot tekshiruvchiga yuborildi.", None),
    ]},
    "Sklad tekshiruvi": {"who": "Tekshiruvchi", "msgs": [
        ("bot", "Sklad tekshiruvi. Aziza Karimova bugun oldi:\n• Kompozit plomba A2 — 3 dona → skladda 4 dona qolishi kerak\n\n"
                "Skladga borib tekshiring. Qoldiq to'g'rimi?", ["To'g'ri", "Noto'g'ri"]),
        ("men", "To'g'ri", None),
        ("bot", "Rahmat! Qoldiq yangilandi.", None),
    ]},
    "Admin": {"who": "Admin", "msgs": [
        ("bot", "Tekshirildi. Aziza Karimova bugun oldi:\n• Kompozit plomba A2 — 3 dona\n\n"
                "Cliniccards'dan chiqarish kerak (Склад → Списание).", ["Chiqarildi"]),
        ("bot", "Skladda kam qoldi: Kompozit plomba A2 — 4 dona (chegara 5).\nTaklif: 10 dona × 45 000 = ~450 000 so'm\n"
                "Sotuvchi: Dental Market\n\nSotuvchiga buyurtma yuborilsinmi?", ["Yuborish", "Bekor"]),
        ("men", "Yuborish", None),
        ("bot", "Buyurtma Dental Market'ga yuborildi: Kompozit plomba A2 — 10 dona", None),
        ("bot", "Dental Market buyurtmani qabul qildi: Kompozit plomba A2 — 10 dona", None),
    ]},
    "Sotuvchi": {"who": "Sotuvchi", "msgs": [
        ("bot", "Assalomu alaykum! Biodent klinikasidan buyurtma:\nKompozit plomba A2 — 10 dona\n\n"
                "Iltimos, qabul qilganingizni tasdiqlang.", ["Qabul qildim"]),
        ("men", "Qabul qildim", None),
        ("bot", "Rahmat! Buyurtmangiz qayd etildi.", None),
    ]},
    "Davomat": {"who": "Xodim · Davomat", "msgs": [
        ("bot", "Davomat: bugun klinika ishladimi?", ["Ha, ishladi", "Yo'q, dam olish"]),
        ("men", "Ha, ishladi", None),
        ("bot", "Kimlar keldi? Kelmaganlarni bosib olib tashlang, keyin «Tayyor».",
         ["✅ Aziza Karimova", "✅ Bekzod Aliyev", "⬜ Dilnoza Rasulova", "Tayyor"]),
        ("men", "Tayyor", None),
        ("bot", "Kimlar normadan ko'p ishladi? Belgilang (hech kim bo'lmasa, darhol «Tayyor»).",
         ["⬜ Aziza Karimova", "✅ Bekzod Aliyev", "Tayyor"]),
        ("men", "Tayyor", None),
        ("bot", "Rahmat! Davomat:\n• Aziza Karimova — 150 000 so'm\n• Bekzod Aliyev (+qo'shimcha) — 160 000 so'm\n"
                "Jami: 310 000 so'm.", None),
    ]},
}
