

import paho.mqtt.client as mqtt
import json
import time
import os
import math
from collections import deque
from datetime import datetime

BROKER          = "broker.hivemq.com"
TELEMETRI_KONUSU = "aslanoglu/diyaliz/telemetri"      # İkiz → Dış dünya
KOMUT_KONUSU     = "aslanoglu/diyaliz/komutlar"        # İkiz ↔ Fiziksel cihaz
SENSOR_KONUSU    = "aslanoglu/diyaliz/sensor_ham"      # Fiziksel cihaz → İkiz (ham)
FIZIKSEL_FSM     = "aslanoglu/diyaliz/fiziksel_durum"  # Fiziksel FSM → İkiz


class KalmanFiltresi:
    """
    Tek değişkenli Kalman filtresi.
    Fiziksel sensörler gürültülü ölçüm üretir. Ham değeri direkt
    kullanmak modeli kararsız hale getirir. Kalman filtresi
    gerçek değerin en iyi tahminini verir.
    """
    def __init__(self, Q=1e-3, R=0.1, P=1.0, x0=0.0):
        self.Q = Q   
        self.R = R   
        self.P = P  
        self.x = x0  

    def guncelle(self, olcum):

        self.P = self.P + self.Q
        K = self.P / (self.P + self.R)          
        self.x = self.x + K * (olcum - self.x)  
        self.P = (1 - K) * self.P               
        return self.x



class PeritonealMembranModeli:
    
    def __init__(self, hasta_kilo: float, peritoneal_alan: float = 1.72):
        self.kilo          = hasta_kilo
        self.alan          = peritoneal_alan   # m² (yetişkin ortalama)
        self.vucud_suyu    = hasta_kilo * 0.6  # Toplam vücut suyu (L)

    def starling_uf_hesapla(self,
                             dextrose_yuzde: float,
                             dwell_suresi_dk: float,
                             sicaklik: float = 37.0) -> float:
        """
        Starling Prensibi ile ultrafiltrasyon hesabı.

        UF = Lp · A · (σ·ΔP_hidros - Δπ_onkotik)

        Lp  : Peritoneal hidrolik geçirgenlik (mL/h/mmHg/m²)
        σ   : Yansıma katsayısı (orta molekül için ~0.03)
        ΔP  : Hidrostatik basınç farkı
        Δπ  : Onkotik basınç farkı (şeker konsantrasyonuna bağlı)
        """
        Lp    = 0.042    # mL/dk/mmHg/m² (klinik ölçüm ortalaması)
        sigma = 0.03     # Orta molekül yansıma katsayısı

        # Osmotik basınç: dextrose konsantrasyonuna göre (Van't Hoff)
        dextrose_mM = (dextrose_yuzde / 100) * 1000 / 0.180  # g/L → mM
        delta_pi    = 0.0257 * dextrose_mM  # mmHg cinsinden

        # Hidrostatik basınç farkı (intraperitoneal + venöz)
        delta_P = 12.0  # mmHg (klinik ortalama, kateter pozisyonuna göre ±3)

        # Sıcaklık düzeltmesi (Arrhenius)
        sicaklik_duzelme = math.exp(0.03 * (sicaklik - 37.0))

        # Net UF hızı (mL/dk)
        uf_hizi = Lp * self.alan * (sigma * delta_P + delta_pi) * sicaklik_duzelme

        # Glikoz absorpsiyonu ile osmotik gradyan bozunması (üstel azalma)
        # Krediet modeli: etkin konsantrasyon = C0 * e^(-k*t)
        k_absorbsiyon = 0.010  # dk⁻¹ (peritoneal transfer katsayısı)
        etkin_carpan   = math.exp(-k_absorbsiyon * dwell_suresi_dk)

        return uf_hizi * etkin_carpan  # mL/dk

    def ktv_hesapla(self,
                    baslangic_urea: float,
                    son_urea: float,
                    dwell_suresi_dk: float) -> float:
        """
        Kt/V klirens hesabı (Daugirdas yöntemi).
        Hedef: Kt/V ≥ 1.7/hafta (KDOQI kılavuzu)
        """
        if baslangic_urea <= 0:
            return 0.0
        R   = son_urea / baslangic_urea
        t_h = dwell_suresi_dk / 60.0  # saate çevir
        # Tek-havuz Kt/V
        ktv = -math.log(R - 0.008 * t_h) + (4 - 3.5 * R) * (self.vucud_suyu / 1000)
        return max(ktv, 0.0)

    def potasyum_klirens(self,
                         K_plazma: float,
                         K_diyalizat: float,
                         dwell_dk: float) -> float:
        """
        Potasyum klirens modeli (difüzyon + konveksiyon).
        Fick Yasası temelli.
        """
        D_K     = 0.0019   # Peritoneal potasyum diffüzivitesi (cm²/dk)
        mesafe  = 0.025    # Periton kalınlığı (cm)
        J_dif   = D_K * (K_plazma - K_diyalizat) / mesafe  # mmol/cm²/dk
        Alan_cm2 = self.alan * 10000
        klirens_mmol = J_dif * Alan_cm2 * dwell_dk
        delta_K = klirens_mmol / (self.vucud_suyu * 1000 / 68.0)
        return delta_K  # plazma potasyumundan düşülecek değer



class PeritonealDiyalizIkizi:

    def __init__(self):
        # --- Hasta parametreleri ---
        self.hasta_kilo    = 78.0
        self.kuru_agirlik  = 75.0
        self.baslangic_urea = 80.0   

        self.membran = PeritonealMembranModeli(hasta_kilo=self.hasta_kilo)

        self.kalman = {
            "giris_sicakligi"  : KalmanFiltresi(Q=0.01, R=0.5,  x0=24.5),
            "hat_basinci"      : KalmanFiltresi(Q=0.05, R=2.0,  x0=15.0),
            "hasta_potasyum"   : KalmanFiltresi(Q=0.01, R=0.1,  x0=6.2),
            "mevcut_hacim"     : KalmanFiltresi(Q=1.0,  R=10.0, x0=0.0),
            "akis_hizi"        : KalmanFiltresi(Q=0.5,  R=5.0,  x0=0.0),
            "effluent_wbc"     : KalmanFiltresi(Q=1.0,  R=5.0,  x0=20.0),
        }

        self.fiziksel_fsm_durumu   = "BILINMIYOR"
        self.son_sensor_zamani     = time.time()
        self.sensor_zaman_asimi_s  = 5.0  

        self.d = {
            # FSM
            "fsm_durumu"       : "S0_BEKLEME",
            "fiziksel_fsm"     : "BILINMIYOR",   
            "fsm_uyumsuzluk"   : False,          
            "mevcut_dongu"     : 0,
            "toplam_dongu"     : 24,
            "kalan_sure"       : 0,

            # Reçete
            "secilen_dextrose"   : 1.5,
            "secilen_potasyum"   : 3.0,
            "hedef_dolum_hacmi"  : 400,
            "has_heparin"        : False,
            "has_antibiotics"    : False,

            "giris_sicakligi"    : 24.5,
            "mevcut_hacim"       : 0.0,
            "hat_basinci"        : 15.0,
            "akis_hizi"          : 0.0,
            "torba_yuksekligi"   : True,

            # Klinik veriler 
            "hasta_potasyum"     : 6.2,
            "effluent_wbc"       : 20.0,
            "kateter_yasi_yeni"  : True,
            "sizinti_glukoz"     : 0.0,
            "ekg_gerekli"        : False,
            "acil_ilac_verildi"  : False,
            "tikaniklik_adim"    : 0,

            "hesaplanan_uf_hizi"   : 0.0,    # mL/dk (Starling modeli)
            "anlık_ktv"            : 0.0,    # Kt/V (Daugirdas)
            "glikoz_absorbsiyonu"  : 0.0,    # mL cinsinden kaybedilen gradyan
            "dwell_gecen_sure"     : 0.0,    # Bekleme süresince geçen dakika

            
            "toplam_tahliye"   : 0.0,
            "net_uf"           : 0.0,
            "hata_kodu"        : "H00",
            "acil_durdurma"    : False,

            "sensor_baglantisi"    : True,
            "veri_gerceklik_skoru" : 100.0,  
            "son_guncelleme"       : "",
        }

        self._dwell_baslangic = None

    
    def sensor_verisini_asimile_et(self, ham_veri: dict):
        """
        Fiziksel cihazdan gelen ham sensör paketini işler.
        1. Kalman filtresi ile gürültüyü temizler.
        2. Fizyolojik sınır dışı değerleri reddeder (plausibility check).
        3. İkizin iç modelini günceller.
        """
        self.son_sensor_zamani = time.time()
        self.d["sensor_baglantisi"] = True

        sensor_haritasi = {
            "giris_sicakligi" : ("giris_sicakligi", "giris_sicakligi",  15.0, 42.0),
            "hat_basinci"     : ("hat_basinci",      "hat_basinci",       0.0, 200.0),
            "hasta_potasyum"  : ("hasta_potasyum",   "hasta_potasyum",    1.0,  10.0),
            "mevcut_hacim"    : ("mevcut_hacim",      "mevcut_hacim",      0.0, 3000.0),
            "akis_hizi"       : ("akis_hizi",         "akis_hizi",      -300.0, 300.0),
            "effluent_wbc"    : ("effluent_wbc",      "effluent_wbc",      0.0, 5000.0),
        }

        residual_toplam = 0.0
        gelen_sensör_sayisi = 0

        for kalman_adi, (ham_adi, model_adi, alt_sinir, ust_sinir) in sensor_haritasi.items():
            if ham_adi not in ham_veri:
                continue
            ham_deger = ham_veri[ham_adi]

            if not (alt_sinir <= ham_deger <= ust_sinir):
                print(f"  [UYARI] {ham_adi} değeri ({ham_deger}) fizyolojik sınır dışı, reddedildi.")
                continue

            onceki = self.kalman[kalman_adi].x
            filtreli = self.kalman[kalman_adi].guncelle(ham_deger)
            self.d[model_adi] = filtreli

            residual_toplam += abs(ham_deger - onceki)
            gelen_sensör_sayisi += 1

        if gelen_sensör_sayisi > 0:
            ort_residual = residual_toplam / gelen_sensör_sayisi
            self.d["veri_gerceklik_skoru"] = max(0.0, 100.0 - ort_residual * 10)

        if "torba_yuksekligi" in ham_veri:
            self.d["torba_yuksekligi"] = bool(ham_veri["torba_yuksekligi"])

        self.d["son_guncelleme"] = datetime.now().strftime("%H:%M:%S")

   
    def fsm_senkronize_et(self, fiziksel_durum: str):
        """
        Fiziksel cihazın bildirdiği FSM durumunu ikizin iç durumuyla karşılaştırır.
        Uyumsuzluk varsa 'state_drift' alarmı üretir.
        """
        self.fiziksel_fsm_durumu    = fiziksel_durum
        self.d["fiziksel_fsm"]      = fiziksel_durum

        if fiziksel_durum != self.d["fsm_durumu"]:
            self.d["fsm_uyumsuzluk"] = True
            print(f"  [STATE DRIFT] İkiz: {self.d['fsm_durumu']} | Fiziksel: {fiziksel_durum}")
            self.d["fsm_durumu"] = fiziksel_durum
        else:
            self.d["fsm_uyumsuzluk"] = False

   
    def duzeltici_komut_uret(self, client) -> str | None:
        """
        İkiz, ölçülen değerler ile model tahminleri arasındaki sapmaya
        göre fiziksel cihaza düzeltici komut gönderir.
        Bu kapalı-döngü (closed-loop) kontrolün özüdür.
        """
        komut = None
        durum = self.d["fsm_durumu"]

        if durum == "S3_DOLUM":
            hedef_akis = 150.0
            olculen_akis = self.d["akis_hizi"]
            sapma = abs(olculen_akis - hedef_akis)
            if sapma > 20.0:
                duzeltme = round((hedef_akis - olculen_akis) * 0.1, 1)
                komut = {"cihaz_komut": "AKIS_HIZI_AYARLA", "deger": duzeltme}

        if durum in ["S1_ISITMA", "S3_DOLUM", "S4_BEKLEME"]:
            hedef_sicaklik = 37.0
            olculen_sicaklik = self.d["giris_sicakligi"]
            if abs(olculen_sicaklik - hedef_sicaklik) > 0.5:
                komut = {
                    "cihaz_komut" : "SICAKLIK_AYARLA",
                    "deger"       : round(hedef_sicaklik - olculen_sicaklik, 2)
                }

        if durum == "S6_TAHLIYE" and self.d["mevcut_hacim"] > 50 and self.d["akis_hizi"] == 0:
            komut = {"cihaz_komut": "TAHLIYE_VALFI_ZORLA_AC"}

        if komut:
            client.publish(KOMUT_KONUSU, json.dumps(komut))
            return f"KAPALI-DÖNGÜ KOMUT → {komut}"
        return None

    
    def alarm_kontrol(self, client) -> str:
        talimat = ""
        durum = self.d["fsm_durumu"]

        # Senaryo 1: Ağır Hiperkalemi
        if self.d["hasta_potasyum"] > 6.5 and not self.d["acil_ilac_verildi"]:
            self.d["ekg_gerekli"] = True
            talimat = "KRİTİK: Ağır Hiperkalemi! EKG + Hücre İçi Kaydırıcı İlaç!"

        # Senaryo 2: Kateter Sızıntısı
        if self.d["kateter_yasi_yeni"] and self.d["hedef_dolum_hacmi"] > 500 and durum == "S3_DOLUM":
            self._alarm_set(client, "H94_KATETER_SIZINTISI")
            talimat = "ALARM: Yeni kateter + yüksek hacim → Sızıntı riski!"

        # Senaryo 3: Peritonit
        if self.d["effluent_wbc"] > 100 and durum != "S7_ALARM":
            self._alarm_set(client, "H91_PERITONIT_ENFEKSIYONU")
            talimat = "ALARM: WBC > 100 → Peritonit tespit edildi!"

        # Senaryo 4: Kateter Tıkanıklığı
        if self.d["hat_basinci"] > 50 and durum in ["S3_DOLUM", "S6_TAHLIYE"]:
            self._alarm_set(client, "H92_KATETER_TIKANIKLIGI")
            talimat = "ALARM: Hat basıncı > 50 mmHg → Tıkanıklık!"

        #  Senaryo 5: Sensör bağlantı kopukluk alarmı
        gecen = time.time() - self.son_sensor_zamani
        if gecen > self.sensor_zaman_asimi_s:
            self.d["sensor_baglantisi"] = False
            talimat = f"UYARI: Fiziksel cihazdan {gecen:.0f}s süredir sensör verisi gelmiyor!"

        #  Senaryo 6: State drift alarmı
        if self.d["fsm_uyumsuzluk"]:
            talimat += " | STATE DRIFT: Fiziksel/Dijital durum uyumsuz, senkronize edildi."

        return talimat

    def _alarm_set(self, client, kod: str):
        self.d["fsm_durumu"]   = "S7_ALARM"
        self.d["hata_kodu"]    = kod
        self.d["acil_durdurma"] = True
        client.publish(KOMUT_KONUSU, json.dumps({"cihaz_komut": "ACIL_VALF_KAPAT"}))

   
    def fsm_adim_at(self, client) -> str:
        durum = self.d["fsm_durumu"]
        talimat = "SİBER İKİZ AKTİF"

        if durum == "S7_ALARM":
            self.d["akis_hizi"] = 0.0
            talimat = f"!!! OTO-GÜVENLİK KİLİDİ: {self.d['hata_kodu']} !!!"

        elif durum == "S1_ISITMA":
            talimat = f"DÖNGÜ {self.d['mevcut_dongu']} - ISITMA"
            if self.d["giris_sicakligi"] >= 37.0:
                client.publish(KOMUT_KONUSU, json.dumps({"cihaz_komut": "DOLUM_VALFI_AC"}))
                self.d["fsm_durumu"] = "S3_DOLUM"

        elif durum == "S3_DOLUM":
            if not self.d["torba_yuksekligi"]:
                self.d["akis_hizi"] = 0.0
                talimat = "BLOKE: Yerçekimi yetersiz!"
            else:
                talimat = f"DÖNGÜ {self.d['mevcut_dongu']} - DOLUM"
                if self.d["mevcut_hacim"] >= self.d["hedef_dolum_hacmi"]:
                    client.publish(KOMUT_KONUSU, json.dumps({"cihaz_komut": "VALFLERI_KAPAT"}))
                    self.d["fsm_durumu"] = "S4_BEKLEME"
                    self.d["kalan_sure"] = 10
                    self._dwell_baslangic = time.time()
                    self.d["dwell_gecen_sure"] = 0.0

        elif durum == "S4_BEKLEME":
            self.d["akis_hizi"] = 0.0

            if self._dwell_baslangic:
                self.d["dwell_gecen_sure"] = (time.time() - self._dwell_baslangic) / 60.0

            uf_hizi = self.membran.starling_uf_hesapla(
                dextrose_yuzde  = self.d["secilen_dextrose"],
                dwell_suresi_dk = self.d["dwell_gecen_sure"],
                sicaklik        = self.d["giris_sicakligi"]
            )
            self.d["hesaplanan_uf_hizi"] = round(uf_hizi, 3)

            mevcut_urea = self.baslangic_urea * math.exp(-0.005 * self.d["dwell_gecen_sure"])
            self.d["anlık_ktv"] = round(
                self.membran.ktv_hesapla(self.baslangic_urea, mevcut_urea, self.d["dwell_gecen_sure"]),
                3
            )

            if self.d["secilen_potasyum"] == 0.0 and self.d["hasta_potasyum"] > 3.5:
                delta_K = self.membran.potasyum_klirens(
                    K_plazma     = self.d["hasta_potasyum"],
                    K_diyalizat  = self.d["secilen_potasyum"],
                    dwell_dk     = 1.0  
                )
                self.d["hasta_potasyum"] = round(
                    max(3.5, self.d["hasta_potasyum"] - delta_K), 2
                )

            self.d["mevcut_hacim"] += uf_hizi  # mL/dk

            talimat = (f"DÖNGÜ {self.d['mevcut_dongu']} - BEKLEME | "
                       f"UF Hızı: {uf_hizi:.2f} mL/dk | Kt/V: {self.d['anlık_ktv']:.3f}")

            if self.d["kalan_sure"] > 0:
                self.d["kalan_sure"] -= 1
            else:
                client.publish(KOMUT_KONUSU, json.dumps({"cihaz_komut": "TAHLIYE_VALFI_AC"}))
                self.d["fsm_durumu"] = "S6_TAHLIYE"

        elif durum == "S6_TAHLIYE":
            talimat = f"DÖNGÜ {self.d['mevcut_dongu']} - TAHLİYE"
            
            # Kalman gecikmesini aşmak için toleransı 40'a çıkardık ve boşaltma hızını artırdık
            if self.d["mevcut_hacim"] > 40.0:
                self.d["mevcut_hacim"] = max(0.0, self.d["mevcut_hacim"] - 100.0)
                self.d["toplam_tahliye"] += 100.0
            else:
                self.d["net_uf"] = round(self.d["toplam_tahliye"] - self.d["hedef_dolum_hacmi"], 1)
                if self.d["mevcut_dongu"] < self.d["toplam_dongu"]:
                    self.d["mevcut_dongu"]  += 1
                    self.d["fsm_durumu"]     = "S1_ISITMA"
                    self.d["toplam_tahliye"] = 0.0
                    
                    # YENİ DÖNGÜ: Yeni soğuk torba takıldı!
                    self.d["giris_sicakligi"] = 24.5
                    self.d["mevcut_hacim"] = 0.0
                    
                    # Kalman filtrelerinin hafızasını zorla sıfırlıyoruz ki eski değerde takılıp beklemesin
                    self.kalman["giris_sicakligi"].x = 24.5
                    self.kalman["mevcut_hacim"].x = 0.0
                else:
                    self.d["fsm_durumu"] = "S0_BEKLEME"
                    talimat = "AKUT TEDAVİ KÜRÜ TAMAMLANDI!"

        return talimat

    
    def klinik_aksiyon_isle(self, data: dict, client):
        aksiyon = data.get("klinik_aksiyon")
        if not aksiyon:
            return

        if aksiyon == "RECISETI_ONAYLA":
            if self.d["fsm_durumu"] == "S0_BEKLEME" and not self.d["acil_durdurma"]:
                self.hasta_kilo   = data.get("hasta_kilo",   self.hasta_kilo)
                self.kuru_agirlik = data.get("kuru_agirlik", self.kuru_agirlik)
                fazla_sivi = self.hasta_kilo - self.kuru_agirlik
                if fazla_sivi > 2.5:
                    self.d["secilen_dextrose"] = 4.25
                elif fazla_sivi > 1.0:
                    self.d["secilen_dextrose"] = 2.5
                else:
                    self.d["secilen_dextrose"] = 1.5
                self.membran = PeritonealMembranModeli(hasta_kilo=self.hasta_kilo)
                self.d["fsm_durumu"]   = "S1_ISITMA"
                self.d["mevcut_dongu"] = 1
                client.publish(KOMUT_KONUSU, json.dumps({"cihaz_komut": "ISITICI_AC"}))

        elif aksiyon == "EKG_CEK":
            if self.d["ekg_gerekli"]:
                self.d["ekg_gerekli"] = False

        elif aksiyon == "ACIL_ILAC_UYGULA":
            self.d["acil_ilac_verildi"] = True
            if self.d["hasta_potasyum"] > 6.5:
                self.d["hasta_potasyum"] = round(self.d["hasta_potasyum"] - 0.7, 2)

        elif aksiyon == "SIZINTI_SEKER_TESTI":
            if self.d["hata_kodu"] == "H94_KATETER_SIZINTISI":
                self.d["sizinti_glukoz"] = 1500.0

        elif aksiyon == "HASTAYI_CEVIR":
            if self.d["tikaniklik_adim"] == 0:
                self.d["tikaniklik_adim"] = 1

        elif aksiyon == "YUKSEKLIK_DUZELT":
            if self.d["tikaniklik_adim"] == 1:
                self.d["tikaniklik_adim"] = 2

        elif aksiyon == "KATETER_FLUSH_YIKA":
            if self.d["tikaniklik_adim"] == 2:
               
                self.d["tikaniklik_adim"] = 0
                if self.d["hata_kodu"] == "H92_KATETER_TIKANIKLIGI":
                    self.d["fsm_durumu"]     = "S3_DOLUM"
                    self.d["hata_kodu"]      = "H00"
                    self.d["acil_durdurma"]  = False
            else:
                self.d["effluent_wbc"] = 120.0  

        elif aksiyon == "SIFIRLA":
            self.__init__()  



ikiz = PeritonealDiyalizIkizi()

def mesaj_gelince(client, userdata, msg):
    try:
        data = json.loads(msg.payload.decode("utf-8"))

        if msg.topic == SENSOR_KONUSU:
            ikiz.sensor_verisini_asimile_et(data)

        elif msg.topic == FIZIKSEL_FSM:
            ikiz.fsm_senkronize_et(data.get("fsm_durumu", "BILINMIYOR"))

        elif msg.topic == KOMUT_KONUSU:
            if "sensor_guncelleme" in data:
                ikiz.sensor_verisini_asimile_et(data["sensor_guncelleme"])
            elif "klinik_aksiyon" in data:
                ikiz.klinik_aksiyon_isle(data, client)

    except Exception as e:
        print(f"  [HATA] Mesaj işleme: {e}")



client = mqtt.Client()
client.on_message = mesaj_gelince
client.connect(BROKER, 1883)
client.subscribe([(KOMUT_KONUSU, 0), (SENSOR_KONUSU, 0), (FIZIKSEL_FSM, 0)])
client.loop_start()

try:
    while True:
        # 1. Alarm kontrolü
        alarm_talimat = ikiz.alarm_kontrol(client)

        # 2. FSM adımı 
        fsm_talimat = ikiz.fsm_adim_at(client)

        #  3. Kapalı döngü düzeltici komut
        kapali_dongu = ikiz.duzeltici_komut_uret(client)

        # 4. Telemetri yayını 
        client.publish(TELEMETRI_KONUSU, json.dumps(ikiz.d))

        # Konsol çıktısı
        talimat = alarm_talimat if alarm_talimat else fsm_talimat
        os.system('cls' if os.name == 'nt' else 'clear')
        print("=" * 80)
        print(f" 🫁 PERİTONEAL DİYALİZ DİJİTAL İKİZİ : {talimat}")
        print("=" * 80)

        bag = "✅ BAĞLI" if ikiz.d["sensor_baglantisi"] else "❌ KESİLDİ"
        sync = "✅ SENKRON" if not ikiz.d["fsm_uyumsuzluk"] else "⚠️  STATE DRIFT"
        print(f" Sensör: {bag} | FSM Sync: {sync} | Gerçeklik: %{ikiz.d['veri_gerceklik_skoru']:.1f}")
        print("-" * 80)

        for k, v in ikiz.d.items():
            if isinstance(v, float):
                print(f"  {k.ljust(28)} : {v:.3f}")
            else:
                print(f"  {k.ljust(28)} : {v}")

        if kapali_dongu:
            print(f"\n  ⟳ {kapali_dongu}")
        print("-" * 80)

        time.sleep(1)

except KeyboardInterrupt:
    print("\nSistem kapatılıyor...")
    client.loop_stop()
except Exception as e:
    print(f"Kritik hata: {e}")
    client.loop_stop()