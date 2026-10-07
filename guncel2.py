

import paho.mqtt.client as mqtt
import json
import time
import random
import os
import traceback

BROKER           = "broker.hivemq.com"
KOMUT_KONUSU     = "aslanoglu/diyaliz/komutlar"
SENSOR_KONUSU    = "aslanoglu/diyaliz/sensor_ham"
FIZIKSEL_FSM     = "aslanoglu/diyaliz/fiziksel_durum"
TELEMETRI_KONUSU = "aslanoglu/diyaliz/telemetri"

# İkizin telemetrisini dinle
ikiz = {"fsm_durumu": "S0_BEKLEME", "hata_kodu": "H00", "acil_durdurma": False,
        "ekg_gerekli": False, "tikaniklik_adim": 0, "mevcut_dongu": 0,
        "hedef_dolum_hacmi": 400.0}

def telemetri_gelince(client, userdata, msg):
    global ikiz
    try:
        veri = json.loads(msg.payload.decode("utf-8"))
        if isinstance(veri, dict):  # Gelen verinin dictionary olduğundan emin ol
            ikiz = veri
    except:
        pass

# MQTT Çakışmasını önlemek için benzersiz bir Client ID üretiyoruz
benzersiz_id = f"sim_hasta_{random.randint(10000, 99999)}"
client = mqtt.Client(client_id=benzersiz_id)
client.on_message = telemetri_gelince

def komut(veri):
    client.publish(KOMUT_KONUSU, json.dumps(veri))
    log(f"📤 KOMUT GÖNDERİLDİ → {veri.get('klinik_aksiyon', 'Bilinmeyen')}")

def sensor(veri):
    client.publish(SENSOR_KONUSU, json.dumps(veri))

def fsm_bildir(durum):
    client.publish(FIZIKSEL_FSM, json.dumps({"fsm_durumu": durum}))

son_olaylar = []
def log(mesaj):
    zaman = time.strftime('%H:%M:%S')
    son_olaylar.append(f"🤖 [{zaman}] {mesaj}")
    if len(son_olaylar) > 8:
        son_olaylar.pop(0)


sim = {
    "giris_sicakligi" : 24.5,
    "hat_basinci"     : 15.0,
    "hasta_potasyum"  : 6.2,
    "mevcut_hacim"    : 0.0,
    "akis_hizi"       : 0.0,
    "effluent_wbc"    : 20.0,
    "torba_yuksekligi": True,
}

OLAYLAR = [
    "peritonit", "tikaniklik", "hiperkalemi",
    "normal", "normal", "normal", "normal", "normal", "normal",
]
bekleme_sayaci = 0


try:
    print("Sunucuya bağlanılıyor...")
    client.connect(BROKER, 1883, keepalive=60)
    client.subscribe(TELEMETRI_KONUSU)
    client.loop_start()

    log("İkiz bekleniyor...")
    time.sleep(3)
    log("Reçete onaylanıyor, tedavi başlatılıyor...")
    komut({"klinik_aksiyon": "SIFIRLA"})
    time.sleep(2)
    komut({"klinik_aksiyon": "RECISETI_ONAYLA", "hasta_kilo": 78.0, "kuru_agirlik": 75.0})

    while True:
        durum  = str(ikiz.get("fsm_durumu") or "S0_BEKLEME")
        hata   = str(ikiz.get("hata_kodu") or "H00")
        acil   = bool(ikiz.get("acil_durdurma") or False)
        ekg    = bool(ikiz.get("ekg_gerekli") or False)
        t_adim = int(ikiz.get("tikaniklik_adim") or 0)
        dongu  = int(ikiz.get("mevcut_dongu") or 0)
        hedef  = float(ikiz.get("hedef_dolum_hacmi") or 400.0)

        if durum == "S1_ISITMA":
            sim["giris_sicakligi"] = min(37.5, sim["giris_sicakligi"] + 0.7)
            sim["akis_hizi"] = 0.0
            fsm_bildir("S1_ISITMA")
        elif durum == "S3_DOLUM":
            sim["akis_hizi"] = 150.0
            sim["mevcut_hacim"] = min(hedef + 50.0, sim["mevcut_hacim"] + 50.0)
            sim["hat_basinci"] = 15.0 + random.uniform(-1, 1)
            fsm_bildir("S3_DOLUM")
        elif durum == "S4_BEKLEME":
            sim["akis_hizi"] = 0.0
            sim["mevcut_hacim"] += random.uniform(2, 5)
            sim["hat_basinci"] = 15.0 + random.uniform(-0.5, 0.5)
            fsm_bildir("S4_BEKLEME")
        elif durum == "S6_TAHLIYE":
            sim["akis_hizi"] = -150.0
            sim["mevcut_hacim"] = max(0.0, sim["mevcut_hacim"] - 50.0)
            fsm_bildir("S6_TAHLIYE")
        elif durum == "S7_ALARM":
            sim["akis_hizi"] = 0.0
            fsm_bildir("S7_ALARM")
        elif durum == "S0_BEKLEME":
            sim["akis_hizi"] = 0.0
            sim["giris_sicakligi"] = 24.5
            sim["mevcut_hacim"] = 0.0
            fsm_bildir("S0_BEKLEME")

        sim["giris_sicakligi"] += random.uniform(-0.05, 0.05)
        sim["hasta_potasyum"]  += random.uniform(-0.01, 0.01)
        sim["effluent_wbc"]    += random.uniform(-0.5, 0.5)
        sim["effluent_wbc"]     = max(0.0, sim["effluent_wbc"])

        sensor_paketi = {
            "giris_sicakligi" : round(float(sim["giris_sicakligi"]), 2),
            "hat_basinci"     : round(float(sim["hat_basinci"]), 2),
            "hasta_potasyum"  : round(float(sim["hasta_potasyum"]), 2),
            "mevcut_hacim"    : round(float(sim["mevcut_hacim"]), 1),
            "akis_hizi"       : round(float(sim["akis_hizi"]), 1),
            "effluent_wbc"    : round(float(sim["effluent_wbc"]), 1),
            "torba_yuksekligi": bool(sim["torba_yuksekligi"]),
        }
        sensor(sensor_paketi)

        os.system('cls' if os.name == 'nt' else 'clear')
        print("="*65)
        print(f" 🩸 FİZİKSEL HASTA VE CİHAZ MONİTÖRÜ | Durum: {durum}")
        print("="*65)
        print(f" 🌡️ Sıcaklık : {sensor_paketi['giris_sicakligi']:>5.2f} °C   | 🎈 Hacim : {sensor_paketi['mevcut_hacim']:>6.1f} mL")
        print(f" 🩸 Basınç   : {sensor_paketi['hat_basinci']:>5.2f} mmHg | 💧 Akış  : {sensor_paketi['akis_hizi']:>6.1f} mL/dk")
        print(f" 🧪 WBC (Enf): {sensor_paketi['effluent_wbc']:>5.1f}        | ⚡ Potas : {sensor_paketi['hasta_potasyum']:>5.2f}")
        print("-" * 65)
        print(" 📜 SON OLAYLAR VE KOMUTLAR:")
        for satir in son_olaylar:
            print("  " + satir)
        print("="*65)

        if ekg and not acil:
            time.sleep(2)
            log("⚡ KRİTİK MÜDAHALE: EKG çekiliyor...")
            komut({"klinik_aksiyon": "EKG_CEK"})
            time.sleep(1)
            log("⚡ KRİTİK MÜDAHALE: Acil İlaç uygulanıyor...")
            komut({"klinik_aksiyon": "ACIL_ILAC_UYGULA"})
            sim["hasta_potasyum"] = 5.8

        elif hata == "H92_KATETER_TIKANIKLIGI" and acil:
            if t_adim == 0:
                time.sleep(random.uniform(2, 3))
                log("🛑 Tıkanıklık! Adım 1: Hasta çevriliyor...")
                komut({"klinik_aksiyon": "HASTAYI_CEVIR"})
            elif t_adim == 1:
                time.sleep(random.uniform(2, 3))
                log("🛑 Adım 2: Yükseklik düzeltiliyor...")
                komut({"klinik_aksiyon": "YUKSEKLIK_DUZELT"})
            elif t_adim == 2:
                time.sleep(random.uniform(2, 3))
                log("🛑 Adım 3: Flush yapılıyor...")
                komut({"klinik_aksiyon": "KATETER_FLUSH_YIKA"})
                sim["hat_basinci"] = 15.0

        elif acil and hata not in ["H92_KATETER_TIKANIKLIGI", "H00"]:
            time.sleep(random.uniform(3, 5))
            log(f"🔔 Alarm ({hata}) müdahale edildi. Sıfırlanıyor...")
            komut({"klinik_aksiyon": "SIFIRLA"})
            sim["effluent_wbc"]    = 20.0
            sim["hasta_potasyum"]  = 6.0
            sim["hat_basinci"]     = 15.0
            sim["giris_sicakligi"] = 24.5
            sim["mevcut_hacim"]    = 0.0
            time.sleep(3)
            komut({"klinik_aksiyon": "RECISETI_ONAYLA", "hasta_kilo": 78.0, "kuru_agirlik": 75.0})

        elif durum == "S0_BEKLEME" and dongu > 0 and not acil:
            time.sleep(random.uniform(3, 6))
            log("🏁 Tedavi tamamlandı. Yeni tedavi başlatılıyor...")
            komut({"klinik_aksiyon": "SIFIRLA"})
            sim["giris_sicakligi"] = 24.5
            sim["mevcut_hacim"]    = 0.0
            time.sleep(2)
            komut({"klinik_aksiyon": "RECISETI_ONAYLA", "hasta_kilo": 78.0, "kuru_agirlik": 75.0})

        elif durum in ["S3_DOLUM", "S4_BEKLEME", "S6_TAHLIYE"] and not acil:
            bekleme_sayaci += 1
            if bekleme_sayaci >= random.randint(5, 10):
                bekleme_sayaci = 0
                olay = random.choice(OLAYLAR)
                if olay == "peritonit":
                    log("⚠️ DİKKAT: Peritonit gelişti — WBC hızla yükseliyor!")
                    sim["effluent_wbc"] = random.randint(110, 180)
                elif olay == "tikaniklik":
                    log("⚠️ DİKKAT: Kateter tıkandı — Basınç artıyor!")
                    sim["hat_basinci"] = random.randint(55, 80)
                elif olay == "hiperkalemi":
                    log("⚠️ DİKKAT: Hiperkalemi krizi — Potasyum tehlikeli seviyede!")
                    sim["hasta_potasyum"] = round(random.uniform(6.6, 7.0), 1)

        time.sleep(1)

except KeyboardInterrupt:
    print("\nSimülatör kullanıcı tarafından kapatıldı.")
except Exception as e:
    print("\n" + "!"*60)
    print(" SİMÜLATÖR KRİTİK BİR HATA NEDENİYLE ÇÖKTÜ:")
    print("!"*60)
    traceback.print_exc()
    print("!"*60)
    input("\nKonsolu kapatmak için Enter'a basın...")
finally:
    client.loop_stop()