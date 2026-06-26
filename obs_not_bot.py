#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Fırat Üniversitesi OBS Not Bildirim Botu (Playwright sürümü)
------------------------------------------------------------
Gerçek bir tarayıcı motoru (headless Chromium) ile OBS'ye giriş yapar, not
listesini çeker, önceki duruma göre değişiklik varsa Telegram'dan bildirim
gönderir. 7/24 döngüde çalışır.

Neden Playwright? Fırat OBS sistemi oturumu JavaScript ile kuruyor; requests
bunu yapamadığı için "Oturum Süresi Sona Erdi" sayfasına düşüyorduk.

KURULUM (terminalde bir kez):
    pip install playwright beautifulsoup4
    python3 -m playwright install chromium

ÇALIŞTIRMA:
    python3 obs_not_bot.py
"""

import os
import json
import time
import hashlib
import logging
import threading
import requests
from datetime import datetime
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

try:
    from zoneinfo import ZoneInfo
    TR = ZoneInfo("Europe/Istanbul")
except Exception:
    from datetime import timezone, timedelta
    TR = timezone(timedelta(hours=3))   # yedek: sabit UTC+3

# ----------------------------------------------------------------------
# AYARLAR
# ----------------------------------------------------------------------
# Telegram bot token'ı ve hedef. Tüm mesajlar tek bir yere (gruba) gider.
TG_TOKEN = os.environ.get("TG_TOKEN", "botfather_token")
TG_CHAT  = os.environ.get("TG_CHAT",  "-5539007255")     # "Ders Notu" grubunun id'si

# Takip edilecek kişiler. Her kişi için: ad, OBS no, OBS şifresi.
# Mesajların hepsi yukarıdaki gruba düşer; mesaj başında kimin notu olduğu yazar.
KISILER = [
    {
        "ad":   "Mert",
        "user": os.environ.get("OBS_USER", "ogrenci_no"),
        "pass": os.environ.get("OBS_PASS", "obs_sifren"),
    },
    {
        "ad":   "Falanca Kişi",
        "user": os.environ.get("OBS2_USER", "arkadas_ogrenci_no"),
        "pass": os.environ.get("OBS2_PASS", "arkadas_sifre"),
        # Bu kişiye özel mesaj başlıkları (isteğe bağlı):
        "kos_baslik":    "🚨 Koş Falanca Kişi KOOOOŞ 🚨 notun açıklandı",
        "stabil_baslik": "Falanca Kişinin not durumu stabil",
    },
]

STUDENT_LOGIN_URL = "https://obs.firat.edu.tr/oibs/std/login.aspx"

# Her kişinin durumu ayrı dosyada tutulur: son_durum_Mert.json gibi.
STATE_PREFIX = "son_durum_"           # dosya adı öneki (not değeri TUTMAZ, sadece özet/hash)
INTERVAL_SEC = 60                     # kaç saniyede bir kontrol (60 = 1 dk)
HEADLESS     = True                   # True = görünmez. Sorun ararken False yap.

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("obs_bot")


# ----------------------------------------------------------------------
# GİRİŞ + NOT SAYFASINI ÇEK  (gerçek tarayıcı motoruyla)
# ----------------------------------------------------------------------
def not_sayfasi_html(kullanici, sifre):
    """Verilen kullanıcı/şifre ile giriş yapıp not sayfasının HTML'ini döndürür."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        ctx = browser.new_context(
            user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0 Safari/537.36")
        )
        page = ctx.new_page()
        try:
            # 1) Öğrenci giriş sayfası -> CAS formuna yönlenir
            page.goto(STUDENT_LOGIN_URL, wait_until="domcontentloaded", timeout=60000)

            # 2) Kullanıcı adı / şifre doldur ve giriş yap
            page.wait_for_selector("#username", timeout=30000)
            page.fill("#username", kullanici)
            page.fill("#password", sifre)
            page.click("input[name='submit']")

            # 3) Girişin tamamlanmasını ve uygulamaya inmesini bekle
            page.wait_for_load_state("networkidle", timeout=60000)

            # Hâlâ CAS'taysak giriş başarısız demektir
            if "cas/login" in page.url or "Enter your Username" in page.content():
                raise RuntimeError("Giriş başarısız: kullanıcı adı/şifre hatalı "
                                   "olabilir ya da CAPTCHA devreye girdi.")
            log.info("Giriş başarılı.")

            # 4a) Ana sayfadayken AGNO'yu (Genel Not Ortalaması) oku.
            #     lblAGNO etiketi index.aspx'te doğrudan yazılı (örn "AGNO: 2,59").
            agno = None
            try:
                agno = page.evaluate("""
                    () => {
                        const el = document.getElementById('lblAGNO');
                        return el ? el.textContent.trim() : null;
                    }
                """)
                # iframe içindeyse orada da ara
                if not agno:
                    for fr in page.frames:
                        try:
                            v = fr.evaluate("""
                                () => {
                                    const el = document.getElementById('lblAGNO');
                                    return el ? el.textContent.trim() : null;
                                }
                            """)
                        except Exception:
                            v = None
                        if v:
                            agno = v
                            break
            except Exception:
                agno = None

            # 4b) Not sayfasına MENÜDEN git. "Not Listesi" bağlantısı menüde
            #    görünür olmadığı için tıklanamıyor; bunun yerine o bağlantının
            #    onclick'inde çağrılan JS'i (menu_close(...)) doğrudan çalıştırıyoruz.
            tiklandi = page.evaluate("""
                () => {
                    const linkler = Array.from(document.querySelectorAll('a'));
                    const hedef = linkler.find(a =>
                        a.textContent.trim() === 'Not Listesi' &&
                        (a.getAttribute('onclick') || '').includes('menu_close')
                    );
                    if (hedef) {
                        if (hedef.onclick) { hedef.onclick(); }
                        else { eval(hedef.getAttribute('onclick')); }
                        return true;
                    }
                    return false;
                }
            """)
            if not tiklandi:
                raise RuntimeError("Menüde 'Not Listesi' bağlantısı bulunamadı.")

            # 5) Not tablosu hangi çerçevede açılırsa açılsın, belirmesini bekle.
            html = None
            son = time.time() + 60          # en fazla 60 sn bekle
            while time.time() < son:
                for fr in page.frames:
                    try:
                        fhtml = fr.content()
                    except Exception:
                        continue
                    if "grd_not_listesi" in fhtml:
                        html = fhtml
                        break
                if html is not None:
                    break
                page.wait_for_timeout(1000)  # 1 sn bekleyip tekrar bak

            if html is None:
                html = page.content()        # son çare: ana sayfanın tamamı
            return html, agno
        finally:
            browser.close()


# ----------------------------------------------------------------------
# NOT TABLOSUNU AYIKLA
# ----------------------------------------------------------------------
def notlari_parse(html):
    """not_listesi_op.aspx HTML'inden ders bazlı not sözlüğü üretir."""
    soup = BeautifulSoup(html, "html.parser")
    tablo = soup.find("table", id="grd_not_listesi")
    if not tablo:
        return None  # tablo yok -> giriş/oturum sorunu olabilir

    def temiz(h):
        return h.get_text(" ", strip=True).replace("\xa0", " ").strip()

    notlar = {}
    for satir in tablo.find_all("tr"):
        hucreler = satir.find_all("td")
        if len(hucreler) < 8:        # başlık (th) satırını atla
            continue
        ders_kod = temiz(hucreler[1])
        if not ders_kod:
            continue
        notlar[ders_kod] = {
            "ad":    temiz(hucreler[2]),
            "durum": temiz(hucreler[3]),
            "sinav": temiz(hucreler[4]),
            "ort":   temiz(hucreler[5]),
            "harf":  temiz(hucreler[6]),
        }
    return notlar


# ----------------------------------------------------------------------
# TELEGRAM BİLDİRİM
# ----------------------------------------------------------------------
# Her kişinin SON okunan (okunabilir) notları burada tutulur; /durum komutu
# bunu anında yazar. Örn: {"Mert": {ders: {...}}, "Falanca Kişi": {...}}
SON_NOTLAR = {}
SON_NOTLAR_DOSYA = "son_notlar.json"   # restart sonrası da /durum çalışsın diye

# Her kişinin son bilinen AGNO'su (Genel Not Ortalaması). Örn {"Mert": "AGNO: 2,59"}
SON_AGNO = {}
SON_AGNO_DOSYA = "son_agno.json"


def son_agno_kaydet():
    try:
        with open(SON_AGNO_DOSYA, "w", encoding="utf-8") as f:
            json.dump(SON_AGNO, f, ensure_ascii=False)
    except Exception as e:
        log.error("AGNO diske yazılamadı: %s", e)


def son_agno_yukle():
    global SON_AGNO
    try:
        if os.path.exists(SON_AGNO_DOSYA):
            with open(SON_AGNO_DOSYA, encoding="utf-8") as f:
                SON_AGNO = json.load(f)
    except Exception as e:
        log.error("AGNO yüklenemedi: %s", e)


def son_notlar_kaydet():
    """Bellekteki son notları diske yazar (restart'a dayanıklı olsun diye)."""
    try:
        with open(SON_NOTLAR_DOSYA, "w", encoding="utf-8") as f:
            json.dump(SON_NOTLAR, f, ensure_ascii=False)
    except Exception as e:
        log.error("Son notlar diske yazılamadı: %s", e)


def son_notlar_yukle():
    """Açılışta diskteki son notları belleğe yükler."""
    global SON_NOTLAR
    try:
        if os.path.exists(SON_NOTLAR_DOSYA):
            with open(SON_NOTLAR_DOSYA, encoding="utf-8") as f:
                SON_NOTLAR = json.load(f)
            log.info("Diskteki son notlar yüklendi (%d kişi).", len(SON_NOTLAR))
    except Exception as e:
        log.error("Son notlar yüklenemedi: %s", e)


def bildir(mesaj, chat=None):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data={"chat_id": chat or TG_CHAT, "text": mesaj},
            timeout=30,
        )
        log.info("Telegram bildirimi gönderildi.")
    except Exception as e:
        log.error("Telegram bildirimi gönderilemedi: %s", e)


# ----------------------------------------------------------------------
# DURUM KAYDI (JSON)
# ----------------------------------------------------------------------
def _state_dosyasi(kisi_ad):
    """Her kişi için ayrı durum dosyası: son_durum_Mert.json gibi."""
    guvenli = "".join(c for c in kisi_ad if c.isalnum() or c in "-_")
    return f"{STATE_PREFIX}{guvenli}.json"


def eski_durum(kisi_ad):
    """{"notlar": {ders: hash}, "son_saatlik": "YYYY-MM-DDTHH"} döndürür."""
    yol = _state_dosyasi(kisi_ad)
    if os.path.exists(yol):
        try:
            with open(yol, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict) and "notlar" in d:
                return d
            return {"notlar": d, "son_saatlik": None}   # eski düz format
        except Exception:
            return {"notlar": {}, "son_saatlik": None}
    return {"notlar": {}, "son_saatlik": None}


def durum_kaydet(kisi_ad, notlar_ozet, son_saatlik):
    with open(_state_dosyasi(kisi_ad), "w", encoding="utf-8") as f:
        json.dump({"notlar": notlar_ozet, "son_saatlik": son_saatlik},
                  f, ensure_ascii=False, indent=2)


# ----------------------------------------------------------------------
# KARŞILAŞTIR VE BİLDİR
# ----------------------------------------------------------------------
def _ozet(v):
    """Bir dersin not bilgisinin geri döndürülemez özetini (hash) üretir.
    Böylece kaydedilen dosyada notların kendisi tutulmaz, sadece değişip
    değişmediğini anlamaya yarayan parmak izi tutulur."""
    metin = json.dumps(v, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(metin.encode("utf-8")).hexdigest()


def _ders_satiri(ders, v):
    return (f"{ders} - {v['ad']}\n"
            f"Durum: {v['durum']}\n"
            f"{v['sinav']}\n"
            f"Ort: {v['ort'] or '-'}   Harf: {v['harf'] or '-'}")


def _final_girildi_mi(v):
    """Bu derste gerçek bir final/sonuç notu girilmiş mi?
    'Final : --' ise henüz yok (False). 'Final : 60' gibi bir sayı varsa True.
    Ayrıca harf notu girilmişse (BB, CC...) de sonuç açıklanmış sayılır."""
    sinav = v.get("sinav", "")
    s = sinav.lower()
    # "final" kelimesinden sonrasına bak; içinde "--" varsa not yok demektir
    if "final" in s:
        sonra = s.split("final", 1)[1]
        # finalden sonra bir rakam var mı ve "--" yok mu?
        if "--" not in sonra and any(ch.isdigit() for ch in sonra):
            return True
    # Harf notu dolmuşsa (örn. "BB", "CC", "AA") da sonuç açıklanmıştır
    harf = (v.get("harf") or "").strip()
    if harf and harf not in ("--", "-"):
        return True
    return False


def _ozet_mesaji(notlar, baslik, ad=None):
    satirlar = [baslik + "\n"]
    final_var = False
    for ders, v in notlar.items():
        satirlar.append(f"• {ders} - {v['ad']}\n  {v['sinav']}  "
                        f"(Harf: {v['harf'] or '-'})")
        s = v["sinav"].lower()
        if "final" in s and "--" not in s.split("final")[-1]:
            final_var = True
    if final_var:
        satirlar.append("\nℹ️ Bazı derslerde final/sonuç notu görünüyor.")
    else:
        satirlar.append("\nℹ️ Şu anda sadece vize notları var, final/sonuç "
                        "notu henüz açıklanmamış.")
    # AGNO'yu en sona ekle (biliniyorsa)
    if ad and SON_AGNO.get(ad):
        satirlar.append(f"\n📊 {SON_AGNO[ad]}")
    return "\n".join(satirlar)


def degisiklik_kontrol(kisi, yeni):
    ad = kisi["ad"]
    SON_NOTLAR[ad] = yeni          # /durum komutu için son okunan notları sakla
    son_notlar_kaydet()           # diske de yaz (restart'a dayanıklı)
    state = eski_durum(ad)
    eski_notlar = state.get("notlar", {})
    ilk_calisma = (len(eski_notlar) == 0)

    yeni_ozet = {ders: _ozet(v) for ders, v in yeni.items()}
    degisenler = [d for d in yeni if eski_notlar.get(d) != yeni_ozet[d]]

    # Kişiye özel "KOŞ" başlığı (tanımlı değilse normal şablon)
    kos_baslik = kisi.get("kos_baslik", f"🚨 {ad} — Yeni Sınav Açıklandı KOŞŞŞ 🚨")

    if ilk_calisma:
        # İlk turda mevcut durumu bir kez özetle (sonra saatlik mesaj YOK).
        bildir(_ozet_mesaji(yeni, f"📋 {ad} — Güncel not durumu", ad))
        durum_kaydet(ad, yeni_ozet, None)
        log.info("[%s] İlk çalışma: mevcut durum özetlendi ve gönderildi.", ad)
        return

    # ANLIK: yeni/değişen not varsa "KOŞ" bildir — AMA sadece o derste
    # gerçek bir final/sonuç notu girildiyse. "Final : --" ise mesaj gönderilmez.
    agno_satiri = f"\n\n📊 {SON_AGNO[ad]}" if SON_AGNO.get(ad) else ""
    for ders in degisenler:
        v = yeni[ders]
        if _final_girildi_mi(v):
            bildir(f"{kos_baslik}\n\n" + _ders_satiri(ders, v) + agno_satiri)
            log.info("[%s] Final notu açıklandı: %s", ad, ders)
        else:
            log.info("[%s] Değişiklik var ama final notu yok, atlanıyor: %s", ad, ders)

    durum_kaydet(ad, yeni_ozet, None)


# ----------------------------------------------------------------------
# ANA DÖNGÜ
# ----------------------------------------------------------------------
def tek_kisi_kontrol(kisi):
    """Tek bir kişinin notlarını kontrol edip gerekirse bildirim atar."""
    ad = kisi["ad"]
    try:
        html, agno = not_sayfasi_html(kisi["user"], kisi["pass"])
        notlar = notlari_parse(html)
        if notlar is None:
            log.warning("[%s] Not tablosu alınamadı, atlanıyor.", ad)
            return

        # AGNO'yu sakla (okunabildiyse). /durum ve KOŞ mesajlarında gösterilir.
        if agno:
            SON_AGNO[ad] = agno
            son_agno_kaydet()

        # --- TEST MODU --- TEST_FINAL=1 ise ilk dersin final notunu sahte
        # olarak "60" yapar; bot bunu "yeni final açıklandı" sanıp KOŞ atar.
        if os.environ.get("TEST_FINAL") == "1" and notlar:
            ilk_ders = next(iter(notlar))
            notlar[ilk_ders]["sinav"] = "Vize : 50 Final : 60"
            notlar[ilk_ders]["harf"]  = "BB"
            log.info("[%s] TEST MODU: %s dersine sahte final enjekte edildi.",
                     ad, ilk_ders)

        log.info("[%s] %d ders okundu. AGNO: %s", ad, len(notlar), agno or "-")
        degisiklik_kontrol(kisi, notlar)
    except Exception as e:
        log.error("[%s] Hata: %s", ad, e)


def tek_kontrol():
    """Listedeki tüm kişileri sırayla kontrol eder."""
    for kisi in KISILER:
        tek_kisi_kontrol(kisi)


# ----------------------------------------------------------------------
# /durum KOMUTU DİNLEME
# ----------------------------------------------------------------------
# Telegram'dan en son işlenen güncelleme kimliği (aynı komutu tekrar tekrar
# yanıtlamamak için).
_son_update_id = None

def _durum_cevabi():
    """Son bilinen notlardan herkesin durumunu tek mesaja toplar."""
    if not SON_NOTLAR:
        son_notlar_yukle()        # bellek boşsa diskten yüklemeyi dene
    if not SON_NOTLAR:
        return ("Henüz not bilgisi alınmadı (bot yeni başlamış olabilir). "
                "Bir dakika içinde tekrar dene.")
    parcalar = []
    for ad, notlar in SON_NOTLAR.items():
        parcalar.append(_ozet_mesaji(notlar, f"📋 {ad} — Son bilinen durum", ad))
    return "\n\n———\n\n".join(parcalar)


def komut_dinle_dongu():
    """Ayrı thread'de sürekli çalışır; /durum komutuna anında cevap verir.
    Long polling kullanır, böylece ana döngü OBS'yle meşgulken bile komutları
    kesintisiz dinler."""
    global _son_update_id
    # Açılışta birikmiş eski komutları atla
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates",
            params={"timeout": 0}, timeout=20,
        )
        data = r.json()
        if data.get("ok") and data.get("result"):
            _son_update_id = data["result"][-1]["update_id"]
    except Exception as e:
        log.error("Komut dinleyici başlangıç hatası: %s", e)

    while True:
        try:
            params = {"timeout": 25}     # 25 sn uzun bekleme (long polling)
            if _son_update_id is not None:
                params["offset"] = _son_update_id + 1
            r = requests.get(
                f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates",
                params=params, timeout=35,
            )
            data = r.json()
            if not data.get("ok"):
                time.sleep(3)
                continue
            for upd in data.get("result", []):
                _son_update_id = upd["update_id"]
                msg = upd.get("message") or upd.get("channel_post") or {}
                metin = (msg.get("text") or "").strip().lower()
                gelen_chat = str(msg.get("chat", {}).get("id", ""))
                if gelen_chat != str(TG_CHAT):
                    continue
                if metin == "/derhal" or metin.startswith("/derhal@"):
                    log.info("/derhal komutu alındı, cevap gönderiliyor.")
                    bildir(_durum_cevabi())
        except Exception as e:
            log.error("Komut dinleme hatası: %s", e)
            time.sleep(3)


def _komut_dinleyici_baslat():
    """Komut dinleyiciyi thread'de başlatır; çökerse otomatik yeniden başlatır."""
    def sarmal():
        while True:
            try:
                komut_dinle_dongu()
            except Exception as e:
                log.error("Komut dinleyici thread çöktü, yeniden başlatılıyor: %s", e)
                time.sleep(5)
    t = threading.Thread(target=sarmal, daemon=True)
    t.start()


def main():
    log.info("Bot başladı. Her %d saniyede bir, %d kişi kontrol edilecek.",
             INTERVAL_SEC, len(KISILER))
    son_notlar_yukle()    # restart sonrası /durum hemen cevap verebilsin
    son_agno_yukle()      # AGNO da restart'a dayanıklı olsun
    # Komut dinleyiciyi ayrı thread'de başlat (çökerse kendini toparlar)
    _komut_dinleyici_baslat()

    # Ana döngü: her tur tam INTERVAL_SEC aralıkla BAŞLAR (tur süresi düşülür).
    while True:
        tur_basi = time.time()
        try:
            tek_kontrol()
        except Exception as e:
            log.error("Genel hata: %s", e)
        # Turun ne kadar sürdüğünü düş, kalan kadar uyu (negatifse hemen devam et)
        gecen = time.time() - tur_basi
        kalan = INTERVAL_SEC - gecen
        if kalan > 0:
            time.sleep(kalan)


if __name__ == "__main__":
    main()
