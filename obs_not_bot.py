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
        # Final notu bu eşiğin ALTINDA ise, ders adına göre özel alay mesajı eklenir.
        "dusuk_esik": 50,
        # Ders adında şu anahtar kelimelerden biri geçerse, karşısındaki metin eklenir.
        # (küçük harfe çevrilip aranır; Türkçe karakterlere dikkat)
        "dusuk_mesajlar": {
            "organizasyon": "felekle oçgör paspas etmiş seni paspaas koooş",
            "mimari":       "felekle oçgör paspas etmiş seni paspaas koooş",
            "algoritma":    "mamo boruyu vermiş yine paspas olmuşsun kooooş",
        },
    },
]

STUDENT_LOGIN_URL = "https://obs.firat.edu.tr/oibs/std/login.aspx"

# Bölüm duyuruları sayfası (herkese açık, giriş gerektirmez).
DUYURU_URL   = "https://bilgisayarmf.firat.edu.tr/announcements-all"
DUYURU_STATE = "son_duyurular.json"   # görülen duyuru ID'leri burada tutulur

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
    """Bir dersin not durumunun geri döndürülemez özetini (hash) üretir.
    ÖNEMLİ: Ham metni değil, sadece ANLAMLI not değerlerini (vize/final/büt
    sayıları + harf) hash'ler. Böylece 'Büt : --' gibi boş bir alanın sonradan
    eklenmesi ya da durum metninin ('...Sonuçlandırılmadı') değişmesi
    "değişiklik" sayılmaz; sadece gerçek bir not girilince hash değişir."""
    vize  = _sinav_notu(v, "vize")
    final = _sinav_notu(v, "final")
    but   = _sinav_notu(v, "büt")
    harf  = (v.get("harf") or "").strip()
    if harf in ("--", "-"):
        harf = ""
    imza = f"vize={vize}|final={final}|but={but}|harf={harf}"
    return hashlib.sha256(imza.encode("utf-8")).hexdigest()


def _ders_satiri(ders, v):
    return (f"{ders} - {v['ad']}\n"
            f"Durum: {v['durum']}\n"
            f"{v['sinav']}\n"
            f"Ort: {v['ort'] or '-'}   Harf: {v['harf'] or '-'}")


def _final_girildi_mi(v):
    """Bu derste gerçek bir SONUÇ notu (final VEYA büt) girilmiş mi?
    Sadece 'Büt : --' gibi boş alanın eklenmesi bildirim TETİKLEMEZ.
    Final veya büt bir sayıysa, ya da harf notu dolmuşsa True."""
    if _sinav_notu(v, "final") is not None:
        return True
    if _sinav_notu(v, "büt") is not None:
        return True
    harf = (v.get("harf") or "").strip()
    if harf and harf not in ("--", "-"):
        return True
    return False


def _sinav_notu(v, sinav_adi):
    """Sınav metninden belirtilen sınavın (final/büt/vize) notunu sayı olarak
    çıkarır. Yoksa veya '--' ise None döner.
    Örn: 'Vize : 25 Final : 45 Büt : --' -> 'büt' None, 'final' 45"""
    s = _tr_normalize(v.get("sinav"))
    anahtar = _tr_normalize(sinav_adi)
    if anahtar not in s:
        return None
    sonra = s.split(anahtar, 1)[1]
    sayi = ""
    basladi = False
    for ch in sonra:
        if ch.isdigit():
            sayi += ch
            basladi = True
        elif basladi:
            break
        elif ch == "-":
            return None      # rakamdan önce "-" (yani --) gelirse not yok
    if sayi:
        try:
            return int(sayi)
        except ValueError:
            return None
    return None


def _tr_normalize(s):
    """Türkçe karakterleri ASCII'ye indirger ve küçük harfe çevirir.
    Böylece 'ORGANİZASYONU'.lower() ile oluşan noktalı i sorunu çözülür ve
    anahtar eşleştirme güvenilir olur."""
    s = (s or "")
    cevrim = str.maketrans({
        "İ": "i", "I": "i", "ı": "i", "i": "i",
        "Ş": "s", "ş": "s", "Ğ": "g", "ğ": "g",
        "Ü": "u", "ü": "u", "Ö": "o", "ö": "o", "Ç": "c", "ç": "c",
    })
    return s.translate(cevrim).lower()


def _final_notu_sayi(v):
    """Sınav metninden final notunu sayı olarak çıkarır. Yoksa None döner.
    Örn: 'Vize : 44 Final : 38' -> 38 ; 'Final : --' -> None"""
    s = v.get("sinav", "")
    low = s.lower()
    if "final" not in low:
        return None
    sonra = low.split("final", 1)[1]   # finalden sonraki kısım
    sayi = ""
    basladi = False
    for ch in sonra:
        if ch.isdigit():
            sayi += ch
            basladi = True
        elif basladi:
            break
    if sayi:
        try:
            return int(sayi)
        except ValueError:
            return None
    return None


def _dusuk_not_eki(kisi, v):
    """Kişiye özel: final notu eşiğin altındaysa ve ders adı bir anahtara
    uyuyorsa, eklenecek alay metnini döndürür. Yoksa boş string."""
    esik = kisi.get("dusuk_esik")
    mesajlar = kisi.get("dusuk_mesajlar")
    if not esik or not mesajlar:
        return ""
    not_sayi = _final_notu_sayi(v)
    if not_sayi is None or not_sayi >= esik:
        return ""   # not yok ya da eşik ve üstü -> ek mesaj yok
    ders_ad = _tr_normalize(v.get("ad"))
    for anahtar, metin in mesajlar.items():
        if _tr_normalize(anahtar) in ders_ad:
            return f"\n\n💬 {metin}"
    return ""


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
        # İlk turda (veya durum dosyası boşsa) HİÇBİR mesaj atma; sadece mevcut
        # durumu sessizce kaydet. Böylece "durduk yere bildirim" olmaz.
        durum_kaydet(ad, yeni_ozet, None)
        log.info("[%s] İlk çalışma: durum sessizce kaydedildi (mesaj atılmadı).", ad)
        return

    # ANLIK: yeni/değişen not varsa "KOŞ" bildir — AMA sadece o derste
    # gerçek bir final/sonuç notu girildiyse. "Final : --" ise mesaj gönderilmez.
    agno_satiri = f"\n\n📊 {SON_AGNO[ad]}" if SON_AGNO.get(ad) else ""
    for ders in degisenler:
        v = yeni[ders]
        if _final_girildi_mi(v):
            ek = _dusuk_not_eki(kisi, v)   # not<eşik ve ders uyuyorsa alay metni
            bildir(f"{kos_baslik}\n\n" + _ders_satiri(ders, v) + ek + agno_satiri)
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

        log.info("[%s] %d ders okundu. AGNO: %s", ad, len(notlar), agno or "-")
        degisiklik_kontrol(kisi, notlar)
    except Exception as e:
        log.error("[%s] Hata: %s", ad, e)


def tek_kontrol():
    """Listedeki tüm kişileri sırayla kontrol eder."""
    for kisi in KISILER:
        tek_kisi_kontrol(kisi)


# ----------------------------------------------------------------------
# BÖLÜM DUYURULARI (bilgisayarmf.firat.edu.tr)
# ----------------------------------------------------------------------
def _gorulen_duyurular():
    """Daha önce bildirilen duyuru ID'lerini (set) döndürür."""
    if os.path.exists(DUYURU_STATE):
        try:
            with open(DUYURU_STATE, encoding="utf-8") as f:
                return set(json.load(f))
        except Exception:
            return set()
    return set()


def _duyurular_kaydet(id_set):
    try:
        with open(DUYURU_STATE, "w", encoding="utf-8") as f:
            # en fazla son 200 ID'yi tut (dosya şişmesin)
            json.dump(list(id_set)[-200:], f, ensure_ascii=False)
    except Exception as e:
        log.error("Duyuru durumu yazılamadı: %s", e)


def duyurulari_cek():
    """Duyuru sayfasını çekip [(id, baslik_ozet, link), ...] listesi döndürür.
    En yeni duyuru en başta olur."""
    try:
        r = requests.get(DUYURU_URL, timeout=30, headers={
            "User-Agent": "Mozilla/5.0 (compatible; ObsBot/1.0)"
        })
        r.encoding = "utf-8"
        soup = BeautifulSoup(r.text, "html.parser")
    except Exception as e:
        log.error("Duyuru sayfası çekilemedi: %s", e)
        return []

    duyurular = []
    gorulen_id = set()
    # Duyuru detay linkleri: .../announcements-detail/<ID>
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "announcements-detail/" not in href:
            continue
        did = href.rstrip("/").split("announcements-detail/")[-1]
        if not did.isdigit() or did in gorulen_id:
            continue
        gorulen_id.add(did)
        metin = a.get_text(" ", strip=True)
        metin = " ".join(metin.split())      # fazla boşlukları sadeleştir
        if not href.startswith("http"):
            href = "https://bilgisayarmf.firat.edu.tr/" + href.lstrip("/")
        duyurular.append((did, metin, href))
    return duyurular


def duyuru_kontrol():
    """Yeni duyuru varsa gruba bildirir. İlk çalışmada sessizce kaydeder."""
    duyurular = duyurulari_cek()
    if not duyurular:
        return
    gorulen = _gorulen_duyurular()
    ilk_calisma = (len(gorulen) == 0)

    # Sayfadaki tüm ID'ler (kaydedilecek)
    tum_idler = gorulen | {d[0] for d in duyurular}

    if ilk_calisma:
        # İlk turda mesaj atma; mevcut duyuruları "görüldü" olarak kaydet.
        _duyurular_kaydet(tum_idler)
        log.info("Duyurular ilk kez okundu (%d adet), sessizce kaydedildi.",
                 len(duyurular))
        return

    # Yeni (daha önce görülmemiş) duyuruları bul. En eskiden yeniye doğru bildir
    # ki grupta sıralama düzgün olsun.
    yeniler = [d for d in duyurular if d[0] not in gorulen]
    for did, metin, link in reversed(yeniler):
        # Başlık + özet çok uzunsa kısalt (Telegram'da okunur kalsın)
        if len(metin) > 600:
            metin = metin[:600] + "…"
        bildir(f"📢 Yeni Bölüm Duyurusu\n\n{metin}\n\n🔗 {link}")
        log.info("Yeni duyuru bildirildi: %s", did)

    _duyurular_kaydet(tum_idler)


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
                if metin == "/bekci" or metin.startswith("/bekci@"):
                    log.info("/bekci komutu alındı, cevap gönderiliyor.")
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
        try:
            duyuru_kontrol()      # bölüm duyurularını da kontrol et
        except Exception as e:
            log.error("Duyuru kontrol hatası: %s", e)
        # Turun ne kadar sürdüğünü düş, kalan kadar uyu (negatifse hemen devam et)
        gecen = time.time() - tur_basi
        kalan = INTERVAL_SEC - gecen
        if kalan > 0:
            time.sleep(kalan)


if __name__ == "__main__":
    main()
