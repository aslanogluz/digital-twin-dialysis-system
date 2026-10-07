"""
=============================================================================
  MEDİKAL DİJİTAL İKİZ KONTROL PANELİ 
=============================================================================
"""
import customtkinter as ctk
import paho.mqtt.client as mqtt
import json

BROKER = "broker.hivemq.com"
KOMUT_KONUSU = "aslanoglu/diyaliz/komutlar"
TELEMETRI_KONUSU = "aslanoglu/diyaliz/telemetri"

ctk.set_appearance_mode("dark")  
ctk.set_default_color_theme("blue") 

class DijitalIkizPaneli(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("🫁 Peritoneal Diyaliz Dijital İkizi - Kontrol Paneli")
        self.geometry("850x500")
        self.resizable(False, False)

        # --- SOL PANEL: KLİNİK KOMUTLAR (BUTONLAR) ---
        self.sol_panel = ctk.CTkFrame(self, width=250, corner_radius=10)
        self.sol_panel.pack(side="left", fill="y", padx=10, pady=10)

        ctk.CTkLabel(self.sol_panel, text="KLİNİK MÜDAHALE", font=("Arial", 16, "bold")).pack(pady=15)

        # Buton Üretici Fonksiyon 
        def buton_ekle(metin, komut_adi, renk=None):
            btn = ctk.CTkButton(self.sol_panel, text=metin, 
                                command=lambda: self.komut_gonder(komut_adi),
                                fg_color=renk)
            btn.pack(pady=10, padx=20, fill="x")

        buton_ekle("Tedaviyi Başlat (Onayla)", "RECISETI_ONAYLA", renk="#2FA572")
        buton_ekle("Sistemi Sıfırla (Reset)", "SIFIRLA", renk="#D35B58")
        
        ctk.CTkLabel(self.sol_panel, text="Acil Durum Müdahaleleri", font=("Arial", 12)).pack(pady=(20,5))
        buton_ekle("EKG Çek", "EKG_CEK")
        buton_ekle("Acil İlaç (Kalsiyum/İnsülin)", "ACIL_ILAC_UYGULA")
        buton_ekle("Hasta Çevir (Tıkanıklık 1)", "HASTAYI_CEVIR")
        buton_ekle("Yükseklik Düzelt (Tıkanıklık 2)", "YUKSEKLIK_DUZELT")
        buton_ekle("Kateter Flush (Tıkanıklık 3)", "KATETER_FLUSH_YIKA")

        # --- SAĞ PANEL:  (SENSÖR EKRANI) ---
        self.sag_panel = ctk.CTkFrame(self, corner_radius=10)
        self.sag_panel.pack(side="right", fill="both", expand=True, padx=10, pady=10)

        ctk.CTkLabel(self.sag_panel, text="CANLI SİBER İKİZ VERİLERİ", font=("Arial", 18, "bold")).pack(pady=15)

        # Veri Gösterge Etiketleri
        self.lbl_durum = ctk.CTkLabel(self.sag_panel, text="Durum: Bekleniyor...", font=("Arial", 22), text_color="#F39C12")
        self.lbl_durum.pack(pady=10)

        self.lbl_hata = ctk.CTkLabel(self.sag_panel, text="Hata Kodu: H00", font=("Arial", 16, "bold"), text_color="#E74C3C")
        self.lbl_hata.pack(pady=5)

        self.sensor_frame = ctk.CTkFrame(self.sag_panel, fg_color="transparent")
        self.sensor_frame.pack(pady=20, fill="x")

        self.lbl_sicaklik = ctk.CTkLabel(self.sensor_frame, text="🌡️ Sıcaklık: -- °C", font=("Arial", 16))
        self.lbl_sicaklik.pack(anchor="w", padx=30, pady=5)

        self.lbl_hacim = ctk.CTkLabel(self.sensor_frame, text="🎈 Mevcut Hacim: -- mL", font=("Arial", 16))
        self.lbl_hacim.pack(anchor="w", padx=30, pady=5)

        self.lbl_basinc = ctk.CTkLabel(self.sensor_frame, text="🩸 Hat Basıncı: -- mmHg", font=("Arial", 16))
        self.lbl_basinc.pack(anchor="w", padx=30, pady=5)

        self.lbl_potasyum = ctk.CTkLabel(self.sensor_frame, text="⚡ Potasyum: -- mEq/L", font=("Arial", 16))
        self.lbl_potasyum.pack(anchor="w", padx=30, pady=5)

        self.lbl_wbc = ctk.CTkLabel(self.sensor_frame, text="🧪 Effluent WBC: --", font=("Arial", 16))
        self.lbl_wbc.pack(anchor="w", padx=30, pady=5)

        # --- MQTT BAĞLANTISI ---
        self.mqtt_client = mqtt.Client(client_id="ui_panel_12345")
        self.mqtt_client.on_message = self.mesaj_gelince
        self.mqtt_client.connect(BROKER, 1883)
        self.mqtt_client.subscribe(TELEMETRI_KONUSU)
        self.mqtt_client.loop_start()

    def komut_gonder(self, komut_adi):
        mesaj = {"klinik_aksiyon": komut_adi}
        if komut_adi == "RECISETI_ONAYLA":
            mesaj["hasta_kilo"] = 78.0
            mesaj["kuru_agirlik"] = 75.0
            
        self.mqtt_client.publish(KOMUT_KONUSU, json.dumps(mesaj))
        print(f"Komut Gönderildi: {komut_adi}")

    def mesaj_gelince(self, client, userdata, msg):
        # İkizden veri geldiğinde ekranı günceller (UI Thread'i yormamak için .after kullanılır)
        try:
            veri = json.loads(msg.payload.decode("utf-8"))
            self.after(0, self.ekrani_guncelle, veri)
        except Exception as e:
            print("Veri parse hatası:", e)

    def ekrani_guncelle(self, veri):
        # Gelen JSON verisini arayüzdeki etiketlere yerleştir
        self.lbl_durum.configure(text=f"Durum: {veri.get('fsm_durumu', 'Bilinmiyor')}")
        self.lbl_hata.configure(text=f"Hata Kodu: {veri.get('hata_kodu', 'H00')}")
        
        self.lbl_sicaklik.configure(text=f"🌡️ Sıcaklık: {veri.get('giris_sicakligi', 0):.2f} °C")
        self.lbl_hacim.configure(text=f"🎈 Mevcut Hacim: {veri.get('mevcut_hacim', 0):.1f} mL")
        self.lbl_basinc.configure(text=f"🩸 Hat Basıncı: {veri.get('hat_basinci', 0):.2f} mmHg")
        self.lbl_potasyum.configure(text=f"⚡ Potasyum: {veri.get('hasta_potasyum', 0):.2f} mEq/L")
        self.lbl_wbc.configure(text=f"🧪 Effluent WBC: {veri.get('effluent_wbc', 0):.1f}")

        if veri.get('hata_kodu') != "H00":
            self.lbl_durum.configure(text_color="#E74C3C")
        else:
            self.lbl_durum.configure(text_color="#F39C12")

if __name__ == "__main__":
    app = DijitalIkizPaneli()
    app.mainloop()
