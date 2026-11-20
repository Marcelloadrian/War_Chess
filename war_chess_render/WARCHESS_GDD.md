# WAR CHESS: Game Design Document (spesifikasi arsitektur v8)

Hardcore grand-tactical turn-based wargame. **Semua aturan berlaku simetris untuk pemain dan AI.** Backend Python (FastAPI, stateless + signed save), frontend satu file `public/index.html` (Canvas2D), jalan di Render.

## 1. Status implementasi
| # | Fitur | Status |
|---|---|---|
| 1 | Peta 50x50 maritim (pulau, selat, danau) | ✅ `mapgen.generate(50, 50)` |
| 1 | Kamera: zoom (wheel / tombol ＋ －), pan (drag / panah), Fit, Home | ✅ |
| 1 | Resource attrition di luar jangkauan outpost (bukan freeze) | ✅ HQ, Supply Base, Barracks (Dock/Info Base ikut nanti) |
| 1 | Bangunan darat tidak boleh di atas SEA (termasuk saat es) | ✅ |
| 1 | Bangunan direbut lawan | ✅ (sudah ada) |
| 2 | Hujan: Land jadi Mud | ✅ |
| 2 | Freeze: SEA jadi Ice, bisa dilewati unit darat | ✅ |
| 2 | Es mencair: semua unit darat di atasnya tenggelam | ✅ |
| 2 | Ice Trap untuk Commandant (instant defeat) | ✅ |
| 3 | Research Building, batas 1 Engineer | ⏳ |
| 4 | Ship + Dock (hanya SEA, Dock di pesisir) | ⏳ (Destroyer/Transport sudah ada, belum lewat Dock) |
| 5 | Information Base (HQ cadangan, outpost, latih Scout) | ✅ |
| 5 | Agent "nyomot infanteri", defection | ⏳ |
| 6 | Heavy Artillery (1 per game, immobile, global, 2x2, bukan HQ, sabotase Engineer) | ✅ |
| 7 | Fog of war dinamis, moral | ✅ sebagian (vision unit/HQ/Scout, moral ada) |
| 7 | Desersi infanteri low-moral, veteran bocor/gampang disuap | ⏳ (bribe by rank sudah ada) |
| 8 | Commandant Chase (HQ + Info Base hancur) | ✅ |
| 8 | HQ tersembunyi (kedua pihak harus saling mencari, AI ikut mencari) | ✅ |
| - | Modal Victory/Defeat, simpan dan putar replay | ✅ |

## 2. Peta dan kamera
Grid 50x50, tile 30 px dunia. Kamera: zoom 0.2x sampai 3x (minimum = seluruh peta), pan dengan drag atau panah, wheel zoom ke arah kursor, tombol ⌂ (ke HQ) dan ⛶ (seluruh peta). Klik setelah drag tidak dianggap klik tile.

## 3. Cuaca dinamis (re-roll tiap 3 ronde)
Clear 50%, Rain 25%, Storm 10%, **Freeze 15%**. Peta tersimpan = peta dasar; `terrain_at()` menghitung terrain efektif:
* Rain/Storm: Plains menjadi **Mud** (gerak 3, burn Fuel).
* Freeze: Sea menjadi **Ice** (gerak 1, burn Fuel 1.5, hanya unit darat/amfibi; kapal tidak bisa).
* Es mencair (Freeze berganti cuaca lain): setiap unit non-kapal di atas tile yang kembali jadi Sea **tenggelam seketika** (event `ice_thaw_drown`). HUD memberi peringatan merah selama unit kita berdiri di atas es.

## 4. Attrition dan outpost hopping
Outpost = HQ, Supply Base, Barracks yang sudah selesai. Unit yang berjarak lebih dari **8 tile** (Chebyshev) dari semua outpost tidak freeze, tetapi upkeep-nya **x2.5**. Dipakai bersama oleh `pay_upkeep` dan `forecast`, jadi ramalan supply selalu sama dengan kenyataan.

## 5. Ekonomi (ringkas)
Starting stock, trickle tiap ronde (tanpa pengaruh base), payday tiap 5 ronde dengan pengali Fibonacci (1, 2, 3, 5, 8, 13) dibatasi morale. Detail angka di GDD.md bagian 14.

## 6. Spesifikasi yang belum dibangun (kontrak desain)
* **Research Building**: muncul simetris dekat HQ, menguras Money tiap turn, menjamin maksimal 1 Engineer aktif per faksi; kas habis = Engineer kena logistik berat.
* **Information Base**: HQ cadangan, produksi Scout, rekrut Agent. Agent = infanteri musuh yang "disomot": intel gratis radius 2 tile, upkeep 0.5 infanteri, berhenti jika terisolasi, membelot jika krisis logistik 2 turn berturut-turut atau Info Base hancur.
* **Heavy Artillery**: 1 per game, dibuat Engineer dengan menguras seluruh resource, immobile, jangkauan global, ledakan 2x2, tidak boleh menembak HQ utama. Engineer musuh bisa sabotase.
* **Commandant Chase**: HQ utama dan Info Base hancur, Commandant muncul, 1 langkah per giliran di darat, harus dikepung dalam batas turn tertentu. Kabur lewat Ice lalu es mencair = defeat instan.

## 7. Arsitektur
`engine.py` (state, aturan, cuaca, attrition), `combat.py`, `ai.py` (aturan sama dengan pemain), `mapgen.py`, `scenarios.py`, `server.py` (stateless + HMAC). Replay = start state bertanda tangan + daftar aksi, diputar ulang di server (RNG deterministik). `RULES_VERSION = 8`.

## 8. Update v8.1: HQ tersembunyi, Information Base, Heavy Artillery, Commandant
* **HQ tersembunyi**: HQ dan Info Base lawan tidak tampil di peta sampai terlihat oleh unit (lalu diingat). AI tunduk aturan yang sama: ia hanya tahu HQ setelah melihatnya, jika belum ia menjelajah sel 8x8 terdekat yang belum dikunjungi (`search_target`).
* **Information Base** (60 material, 50 money, 3 ronde, 1 per sisi): HQ cadangan, outpost, sight 4, latih Scout (15 money, 10 food, maks 3).
* **Heavy Artillery**: 1 per game (kedua pihak), dibuat Engineer, menguras 70% semua stok (syarat 120 money, 40 food, 60 material, 30 fuel, 4 bomb), mp 0, tembak ke tile mana saja (2 bomb), blast 2x2, 70 damage ke unit, menghancurkan bangunan non-HQ, tidak boleh mengenai HQ. Engineer musuh di sebelahnya bisa sabotase.
* **Commandant Chase**: HQ dan Info Base habis, Commandant muncul dekat HQ awal, 1 langkah per giliran. Kalah jika terbunuh, ditawan, tenggelam saat es mencair, atau dikepung. Jika bertahan `CHASE_TURNS = 10` ronde, ia kabur dan sisinya menang.
* Belum: Research Building + batas Engineer, Dock + Ship, Agent "nyomot infanteri", desersi infanteri moral rendah, veteran bocor/gampang disuap.
