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
import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# ----------------------------------------------------------------------
# AYARLAR
# ----------------------------------------------------------------------
# Bilgiler önce ortam değişkeninden (GitHub Secrets) okunur; yoksa aşağıdaki
# placeholder kullanılır. ÖNEMLİ: Bu dosyayı herkese açık (public) bir GitHub
# deposuna koyacaksan, aşağıya GERÇEK şifreni YAZMA! Placeholder'ları olduğu
# gibi bırak, gerçek bilgileri GitHub Secrets'a gir. Sadece kendi bilgisayarında
# çalıştıracaksan placeholder'ları kendi bilgilerinle değiştirebilirsin.
USERNAME = os.environ.get("OBS_USER", "ogrenci_no")
PASSWORD = os.environ.get("OBS_PASS", "obs_sifren")
TG_TOKEN = os.environ.get("TG_TOKEN", "botfather_token")
TG_CHAT  = os.environ.get("TG_CHAT",  "1560146067")     # senin chat_id'n

STUDENT_LOGIN_URL = "https://obs.firat.edu.tr/oibs/std/login.aspx"

STATE_FILE   = "son_durum.json"       # değişiklik takibi için (not değeri TUTMAZ, sadece özet/hash)
INTERVAL_SEC = 300                    # döngü modunda kaç saniyede bir kontrol (300 = 5 dk)
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
def not_sayfasi_html():
    """Tarayıcı motoruyla giriş yapıp not sayfasının HTML'ini döndürür."""
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
            page.fill("#username", USERNAME)
            page.fill("#password", PASSWORD)
            page.click("input[name='submit']")

            # 3) Girişin tamamlanmasını ve uygulamaya inmesini bekle
            page.wait_for_load_state("networkidle", timeout=60000)

            # Hâlâ CAS'taysak giriş başarısız demektir
            if "cas/login" in page.url or "Enter your Username" in page.content():
                raise RuntimeError("Giriş başarısız: kullanıcı adı/şifre hatalı "
                                   "olabilir ya da CAPTCHA devreye girdi.")
            log.info("Giriş başarılı.")

            # 4) Not sayfasına MENÜDEN git. "Not Listesi" bağlantısı menüde
            #    görünür olmadığı için tıklanamıyor; bunun yerine o bağlantının
            #    onclick'inde çağrılan JS'i (menu_close(...)) doğrudan çalıştırıyoruz.
            #    Bu, sistemin not sayfasını doğru anahtarla açmasını sağlar.
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
            return html
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
def bildir(mesaj):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data={"chat_id": TG_CHAT, "text": mesaj},
            timeout=30,
        )
        log.info("Telegram bildirimi gönderildi.")
    except Exception as e:
        log.error("Telegram bildirimi gönderilemedi: %s", e)


# ----------------------------------------------------------------------
# DURUM KAYDI (JSON)
# ----------------------------------------------------------------------
def eski_durum():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def durum_kaydet(notlar):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(notlar, f, ensure_ascii=False, indent=2)


# ----------------------------------------------------------------------
# KARŞILAŞTIR VE BİLDİR
# ----------------------------------------------------------------------
def _ozet(v):
    """Bir dersin not bilgisinin geri döndürülemez özetini (hash) üretir.
    Böylece kaydedilen dosyada notların kendisi tutulmaz, sadece değişip
    değişmediğini anlamaya yarayan parmak izi tutulur."""
    metin = json.dumps(v, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(metin.encode("utf-8")).hexdigest()


def degisiklik_kontrol(yeni):
    eski = eski_durum()                 # {ders: hash}
    ilk_calisma = (len(eski) == 0)
    yeni_ozet = {ders: _ozet(v) for ders, v in yeni.items()}

    if ilk_calisma:
        # İlk turda mevcut durumun tamamını tek mesajda özetle.
        satirlar = ["📋 Güncel not durumu\n"]
        final_var = False
        for ders, v in yeni.items():
            satirlar.append(f"• {ders} - {v['ad']}\n  {v['sinav']}  "
                            f"(Harf: {v['harf'] or '-'})")
            s = v["sinav"].lower()
            if "final" in s and "--" not in s.split("final")[-1]:
                final_var = True

        if final_var:
            satirlar.append("\nℹ️ Bazı derslerde final/sonuç notu da görünüyor.")
        else:
            satirlar.append("\nℹ️ Şu anda sadece vize notları var, final/sonuç "
                            "notu henüz açıklanmamış.")

        bildir("\n".join(satirlar))
        durum_kaydet(yeni_ozet)
        log.info("İlk çalışma: mevcut durum özetlendi ve gönderildi.")
        return

    # Sonraki turlar: yeni/değişen not olursa "KOŞŞŞ" bildirimi
    for ders, v in yeni.items():
        if eski.get(ders) != yeni_ozet[ders]:
            mesaj = (f"🚨 Yeni Sınav Açıklandı KOŞŞŞ 🚨\n\n"
                     f"{ders} - {v['ad']}\n"
                     f"Durum: {v['durum']}\n"
                     f"{v['sinav']}\n"
                     f"Ort: {v['ort'] or '-'}   Harf: {v['harf'] or '-'}")
            log.info("Değişiklik: %s", ders)
            bildir(mesaj)

    durum_kaydet(yeni_ozet)


# ----------------------------------------------------------------------
# ANA DÖNGÜ
# ----------------------------------------------------------------------
def tek_kontrol():
    html = not_sayfasi_html()
    notlar = notlari_parse(html)
    if notlar is None:
        log.warning("Not tablosu alınamadı, bu tur atlanıyor.")
        return
    log.info("%d ders okundu.", len(notlar))
    degisiklik_kontrol(notlar)


def main():
    # RUN_ONCE=1 ise tek tur çalışıp çıkar (GitHub Actions için).
    # Aksi halde sonsuz döngüde çalışır (kendi bilgisayarın/sunucun için).
    if os.environ.get("RUN_ONCE") == "1":
        log.info("Tek seferlik kontrol modu.")
        tek_kontrol()
        return

    log.info("Bot başladı. Her %d saniyede bir kontrol edilecek.", INTERVAL_SEC)
    while True:
        try:
            tek_kontrol()
        except Exception as e:
            log.error("Hata: %s", e)
        time.sleep(INTERVAL_SEC)


if __name__ == "__main__":
    main()
