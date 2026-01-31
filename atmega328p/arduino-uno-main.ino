#include <Wire.h>              // I2C (np. RTC DS1307)
#include <RTClib.h>            // biblioteka do RTC (DS1307/DS3231)
#include <SPI.h>               // SPI (do OLED)
#include <Adafruit_GFX.h>      // grafika (czcionki, rysowanie)
#include <Adafruit_SSD1306.h>  // sterownik OLED SSD1306

// ================== PINY ==================
// Czujnik światła (LDR) -> wejście analogowe
#define PIN_LDR      A0

// "SHT" w tym kodzie jest traktowane jako analogowe źródła napięcia
// (czyli odczyt z ADC i mapowanie). Nie jest to typowe SHT3X I2C!
#define PIN_SHT_TEMP A1
#define PIN_SHT_HUM  A2

// Wyjścia
#define PIN_LED      4         // LED sygnalizacyjna
#define PIN_BUZZ     9         // buzzer pasywny (tone())

// Przyciski do GND, używany jest INPUT_PULLUP (stan LOW = wciśnięty)
#define BTN_MODE     2         // AUTO/MANUAL (lub zmniejsz godzinę w edycji nocy)
#define BTN_NIGHT    3         // wejście do edycji nocy / przełącz etap edycji
#define BTN_TOGGLE   5         // w MANUAL: LED ON/OFF (lub zwiększ godzinę w edycji nocy)

// OLED SSD1306 (SPI)
#define OLED_CS      10
#define OLED_DC      7
#define OLED_RST     6

// ================== KONFIGURACJA I STAŁE ==================
#define SCREEN_W 128
#define SCREEN_H 64

// Progi alarmów temperatury (WARNING i CRITICAL)
#define TEMP_WARN_LOW  16.0
#define TEMP_WARN_HIGH 26.0
#define TEMP_CRIT_LOW  12.0
#define TEMP_CRIT_HIGH 30.0

// Progi alarmów wilgotności
#define HUM_WARN_LOW   30.0
#define HUM_WARN_HIGH  65.0
#define HUM_CRIT_LOW   20.0
#define HUM_CRIT_HIGH  75.0

// Próg "ciemno" dla LDR (surowa wartość ADC 0..1023)
// Jeśli ldr < LDR_LIMIT -> dark = true
#define LDR_LIMIT      50

// Kalibracja ADC: zakresy dla temp/hum (zależne od czujnika / dzielnika napięcia)
// Te wartości mówią: "taki ADC odpowiada minimalnej i maksymalnej wartości"
#define ADC_T_MIN 125
#define ADC_T_MAX 897
#define ADC_H_MIN 102
#define ADC_H_MAX 916

// Debounce przycisków (ms) - filtr drgań styków
const unsigned long DEBOUNCE_MS = 35;

// Miganie edytowanej godziny na OLED (ms)
const unsigned long EDIT_BLINK_MS = 400;

// ================== STRUKTURY I TYPY DANYCH ==================
// Tryb pracy systemu
enum Mode { AUTO, MANUAL };

// Stany edycji trybu nocnego (prosta maszyna stanów)
enum NightEditState { NIGHT_EDIT_OFF, NIGHT_EDIT_START, NIGHT_EDIT_END };

// Struktura przycisku do debouncingu
struct Button {
  uint8_t pin;                 // pin przycisku
  bool stableState;            // ostatni stabilny stan (po debounc)
  bool lastReading;            // ostatni odczyt natychmiastowy
  unsigned long lastChangeMs;  // kiedy ostatnio zmienił się odczyt
};

// ================== OBIEKTY ==================
Adafruit_SSD1306 display(SCREEN_W, SCREEN_H, &SPI, OLED_DC, OLED_RST, OLED_CS);
RTC_DS1307 rtc;

// ================== ZMIENNE GLOBALNE ==================

// --- Konfiguracja czasu i trybu ---
Mode mode = AUTO;                       // start w AUTO
NightEditState nightEdit = NIGHT_EDIT_OFF; // domyślnie brak edycji nocy
unsigned long intervalMs = 1000;        // co ile wysyłać dane UART

// Godziny działania "nocy" (dla AUTO LED)
// np. 20 -> start, 6 -> koniec (przechodzi przez północ)
uint8_t eveningStart = 20;
uint8_t nightEnd = 6;

// Zmienne robocze do edycji godzin (żeby nie zmieniać od razu "na żywo")
uint8_t editStartH = 20;
uint8_t editEndH   = 6;

// --- Dane czujników ---
float tempC = 0.0f;         // temperatura po przeliczeniu z ADC
float humP = 0.0f;          // wilgotność po przeliczeniu z ADC
int ldr = 0;                // surowy ADC z LDR
DateTime now;               // aktualny czas z RTC

// --- Stany wyjść i flagi ---
bool ledState = false;      // "łączny stan LED" (do UART)
bool baseLedOn = false;     // LED wynikający z trybu (AUTO/MANUAL)
bool manualLED = false;     // stan LED w trybie MANUAL
bool buzzState = false;     // czy buzzer ma być aktywny (warning/critical)

bool warningState = false;  // czy jest WARNING
bool criticalState = false; // czy jest CRITICAL

bool editBlinkOn = true;    // miganie edytowanej godziny na OLED

// --- Timery (millis) ---
unsigned long lastReadSend = 0;    // timer dla UART
unsigned long lastReadMain = 0;    // timer dla głównego odświeżania (czujniki/ekran)
unsigned long lastEditBlink = 0;   // timer migania godzin w edycji

// --- Przyciski (stan początkowy) ---
// stableState i lastReading ustawione LOW -> przy pull-up to może być specyficzne,
// ale checkPress ma debounce i wykrywa stabilne przejścia.
Button bMode  = { BTN_MODE,   LOW, LOW, 0 };
Button bNight = { BTN_NIGHT,  LOW, LOW, 0 };
Button bTog   = { BTN_TOGGLE, LOW, LOW, 0 };

// --- Komunikacja UART ---
// Zbieranie komendy od PC do String cmd (np. MA, ML, LO, I2 itd.)
String cmd;

// ================== FUNKCJE POMOCNICZE ==================

// Inicjalizacja pinów na poziomie rejestrów AVR (ATmega328P)
//
// DDRx -> kierunek (1=OUTPUT, 0=INPUT)
// PORTx:
//   - OUTPUT: 1=HIGH, 0=LOW
//   - INPUT:  1=włącz pull-up, 0=bez pull-up (floating)
//
// PD4 = Arduino D4 (LED)
// PB1 = Arduino D9 (BUZZ)  [uwaga: pin D9 = PB1]
//
// PD2 = D2, PD3 = D3, PD5 = D5 (przyciski)
void ioInitRegisters() {
  DDRD |= (1 << DDD4);        // PD4 jako OUTPUT (LED)
  DDRB |= (1 << DDB1);        // PB1 jako OUTPUT (BUZZ)

  // PD2/PD3/PD5 jako INPUT (przyciski)
  DDRD &= ~((1 << DDD2) | (1 << DDD3) | (1 << DDD5));

  // Dla wejść: PORT=1 => włącza pull-up (przyciski do GND)
  PORTD |=  (1 << PORTD2) | (1 << PORTD3) | (1 << PORTD5);

  // Start: LED i BUZZ wyłączone (LOW)
  PORTD &= ~(1 << PORTD4);
  PORTB &= ~(1 << PORTB1);
}

// Zawijanie godzin 0..23.
// Przy edycji godzin: -1 -> 23, 24 -> 0.
uint8_t wrapHour(int8_t h) {
  if (h < 0) return 23;
  if (h > 23) return 0;
  return (uint8_t)h;
}

// Mapowanie liniowe float (odpowiednik map(), ale z float)
float mapFloat(float x, float in_min, float in_max, float out_min, float out_max) {
  return (x - in_min) * (out_max - out_min) / (in_max - in_min) + out_min;
}

// Ograniczenie wartości do przedziału [lo, hi]
float clampf(float v, float lo, float hi) {
  if (v < lo) return lo;
  if (v > hi) return hi;
  return v;
}

// Zamiana ADC -> temperatura (przez skalowanie i clamp)
// ADC_T_MIN..ADC_T_MAX mapowane na -40..125°C
float adcToTemp(int adc) {
  float t = mapFloat((float)adc, ADC_T_MIN, ADC_T_MAX, -40.0f, 125.0f);
  return clampf(t, -40.0f, 125.0f);
}

// Zamiana ADC -> wilgotność (0..100%)
// ADC_H_MIN..ADC_H_MAX mapowane na 0..100%
float adcToHum(int adc) {
  float h = mapFloat((float)adc, ADC_H_MIN, ADC_H_MAX, 0.0f, 100.0f);
  return clampf(h, 0.0f, 100.0f);
}

// Sprawdzenie czy aktualna godzina jest w zakresie "wieczór/noc".
//
// Obsługa dwóch przypadków:
// 1) eveningStart < nightEnd: zakres w obrębie tej samej doby (np. 18-22)
// 2) eveningStart > nightEnd: zakres przechodzi przez północ (np. 20-6)
bool isEvening() {
  uint8_t h = now.hour();

  if (eveningStart < nightEnd) {
    // zakres nie przechodzi przez północ
    return (h >= eveningStart && h < nightEnd);
  } else {
    // zakres przechodzi przez północ
    return (h >= eveningStart || h < nightEnd);
  }
}

// ================== OBSŁUGA PERYFERIÓW (OLED, BUZZER, LED) ==================

// Inicjalizacja OLED: manualny reset + start SPI + start biblioteki SSD1306
void oledInit() {
  pinMode(OLED_RST, OUTPUT);

  // Sekwencja resetu OLED (czasem konieczna przy SPI OLED)
  digitalWrite(OLED_RST, HIGH); delay(5);
  digitalWrite(OLED_RST, LOW);  delay(20);
  digitalWrite(OLED_RST, HIGH); delay(20);

  SPI.begin();
  display.begin(SSD1306_SWITCHCAPVCC, 0, false, true);
  display.clearDisplay();
  display.display();
}

// Rysowanie danych na OLED.
// Pokazuje: tryb, czas, temp, hum, godziny nocy.
// Dodatkowo: migający ekran ostrzeżenia WARNING/CRITICAL.
void drawOLED() {
  // format czasu do "HH:MM:SS"
  char timeBuf[9];
  snprintf(timeBuf, sizeof(timeBuf), "%02d:%02d:%02d",
           now.hour(), now.minute(), now.second());

  display.clearDisplay();
  display.setTextColor(SSD1306_WHITE);

  // Miganie napisu alarmu (lokalne static -> pamiętają stan)
  static bool alarmBlink = false;
  static unsigned long lastAlarmBlink = 0;
  unsigned long ms = millis();

  // CRITICAL miga szybciej niż WARNING
  unsigned long alarmBlinkInterval = criticalState ? 250 : 500;

  // jeśli jest alarm -> przełącz miganie co alarmBlinkInterval
  if ((warningState || criticalState) && (ms - lastAlarmBlink >= alarmBlinkInterval)) {
    lastAlarmBlink = ms;
    alarmBlink = !alarmBlink;
  }

  // Miganie edytowanej godziny w trybie ustawień nocy
  if (nightEdit != NIGHT_EDIT_OFF) {
    if (ms - lastEditBlink >= EDIT_BLINK_MS) {
      lastEditBlink = ms;
      editBlinkOn = !editBlinkOn;
    }
  } else {
    // poza edycją: godziny nie migają
    editBlinkOn = true;
  }

  // --- Górne informacje ---
  display.setTextSize(1);
  display.setCursor(0, 0);
  display.print("Mode: ");
  display.print(mode == AUTO ? "AUTO" : "MANUAL");

  display.setCursor(0, 12);
  display.print("Time: ");
  display.print(timeBuf);

  // --- Czujniki ---
  display.setCursor(0, 24);
  display.print("Temp: ");
  display.print(tempC, 1);
  display.print(" C");

  display.setCursor(0, 36);
  display.print("Hum : ");
  display.print(humP, 1);
  display.print(" %");

  // --- Godziny nocy (lub wartości edytowane) ---
  display.setCursor(0, 48);
  display.print("Night: ");

  // Jeśli edytujesz -> pokazuj editStartH/editEndH
  // Jeśli nie -> pokazuj eveningStart/nightEnd
  uint8_t shownStart = (nightEdit == NIGHT_EDIT_OFF) ? eveningStart : editStartH;
  uint8_t shownEnd   = (nightEdit == NIGHT_EDIT_OFF) ? nightEnd     : editEndH;

  // Miganie: jeśli edytujesz START i editBlinkOn==false -> ukryj liczby
  if (nightEdit == NIGHT_EDIT_START && !editBlinkOn) {
    display.print("  ");
  } else {
    if (shownStart < 10) display.print("0");
    display.print(shownStart);
  }

  display.print(" - ");

  // Miganie: jeśli edytujesz END i editBlinkOn==false -> ukryj liczby
  if (nightEdit == NIGHT_EDIT_END && !editBlinkOn) {
    display.print("  ");
  } else {
    if (shownEnd < 10) display.print("0");
    display.print(shownEnd);
  }

  // --- Duży napis alarmu ---
  if (criticalState || warningState) {
    if (alarmBlink) {
      const char* msg = criticalState ? "CRITICAL" : "WARNING";

      // wyczyść obszar, narysuj ramkę i tekst
      display.fillRect(2, 20, 124, 28, SSD1306_BLACK);
      display.drawRect(2, 20, 124, 28, SSD1306_WHITE);
      display.setTextSize(2);

      // Wycentrowanie tekstu: 1 znak ~12px przy setTextSize(2)
      int msgLen = strlen(msg);
      int textX = (SCREEN_W - (msgLen * 12)) / 2;
      if (textX < 0) textX = 0;
      display.setCursor(textX, 26);
      display.print(msg);

      display.setTextSize(1);
    }
  }

  display.display();
}

// Sterowanie buzzerem pasywnym.
// - CRITICAL: ciągły 2000 Hz
// - WARNING: przerywany 1200 Hz (ON/OFF co 200ms)
void buzzerPassive(bool warning, bool critical) {
  static unsigned long lastToggle = 0; // kiedy ostatnio przełączono pip
  static bool beepOn = false;          // czy pip jest aktualnie aktywny
  unsigned long nowMs = millis();

  if (critical) {
    // sygnał ciągły
    tone(PIN_BUZZ, 2000);
    return;
  }

  if (warning) {
    // pip-pip: zmiana co 200ms
    if (nowMs - lastToggle >= 200) {
      lastToggle = nowMs;
      beepOn = !beepOn;

      if (beepOn) tone(PIN_BUZZ, 1200);
      else noTone(PIN_BUZZ);
    }
    return;
  }

  // brak alarmów -> wyłącz buzzer
  noTone(PIN_BUZZ);
}

// Sterowanie LED w zależności od alarmów.
//
// Priorytet:
/// CRITICAL: LED stale ON
/// WARNING: LED miga
/// NORMAL: LED wg baseOn (AUTO/MANUAL)
void ledAlarmUpdate(bool warning, bool critical, bool baseOn) {
  static unsigned long lastToggle = 0; // timer migania
  static bool blinkOn = false;         // stan migania
  unsigned long ms = millis();

  if (critical) {
    digitalWrite(PIN_LED, HIGH);
    return;
  }

  if (warning) {
    if (ms - lastToggle >= 200) {
      lastToggle = ms;
      blinkOn = !blinkOn;
    }
    digitalWrite(PIN_LED, blinkOn ? HIGH : LOW);
    return;
  }

  // brak alarmu -> wynik bazowy
  digitalWrite(PIN_LED, baseOn ? HIGH : LOW);
}

// ================== LOGIKA ==================

// Wyliczenie stanów: czy ciemno, czy wieczór, czy warning/critical,
// i ustawienie flag LED/Buzzer.
void updateLogic() {
  // stan światła (ciemno?)
  bool dark = ldr < LDR_LIMIT;

  // stan czasu (czy jest "noc" wg godzin)
  bool evening = isEvening();

  // alarmy temperatury
  bool warningTemp  = (tempC < TEMP_WARN_LOW)  || (tempC > TEMP_WARN_HIGH);
  bool criticalTemp = (tempC < TEMP_CRIT_LOW)  || (tempC > TEMP_CRIT_HIGH);

  // alarmy wilgotności
  bool warningHum   = (humP < HUM_WARN_LOW)    || (humP > HUM_WARN_HIGH);
  bool criticalHum  = (humP < HUM_CRIT_LOW)    || (humP > HUM_CRIT_HIGH);

  // łączny alarm
  bool warning  = warningTemp  || warningHum;
  bool critical = criticalTemp || criticalHum;

  // aktualizacja globalnych stanów alarmu
  warningState  = warning;
  criticalState = critical;

  // buzzer aktywny jeśli jest jakikolwiek alarm
  buzzState = warning || critical;

  // "bazowe" sterowanie LED zależne od trybu
  if (mode == AUTO) baseLedOn = (dark || evening); // AUTO -> zależne od warunków
  else              baseLedOn = manualLED;         // MANUAL -> zależne od użytkownika

  // ledState to ogólny "stan logiczny" do UART (uwzględnia alarmy)
  ledState = baseLedOn || warning || critical;
}

// ================== UART ==================

// Wysyłanie linii danych do PC w formacie "KEY=VALUE"
void sendUART() {
  // format czasu
  char t[9];
  snprintf(t, sizeof(t), "%02d:%02d:%02d",
           now.hour(), now.minute(), now.second());

  // Dane w formacie łatwym do parsowania po stronie PC
  Serial.print(F("DATA,"));
  Serial.print(F("t="));    Serial.print(tempC, 1);
  Serial.print(F(",h="));   Serial.print(humP, 1);
  Serial.print(F(",ldr=")); Serial.print(ldr);
  Serial.print(F(",time="));Serial.print(t);
  Serial.print(F(",led=")); Serial.print(ledState ? 1 : 0);
  Serial.print(F(",buzz="));Serial.print(buzzState ? 1 : 0);
  Serial.print(F(",mode="));Serial.print(mode == AUTO ? "AUTO" : "MANUAL");

  // Ustawienia:
  Serial.print(F(",int=")); Serial.print(intervalMs);
  Serial.print(F(",nst=")); Serial.print(eveningStart);
  Serial.print(F(",nend="));Serial.println(nightEnd);
}

// Odczyt komend z PC (np. "MA\n", "I5\n").
// Zbieramy znaki do String cmd aż do '\n'.
void readCmd() {
  while (Serial.available()) {
    char c = Serial.read();

    if (c == '\n') {
      cmd.trim(); // usuń spacje, \r itd.

      // --- tryb pracy ---
      if (cmd == F("MA")) mode = AUTO;
      else if (cmd == F("ML")) {
        mode = MANUAL;
        manualLED = false; // przy wejściu w MANUAL start od OFF
      }

      // --- LED manual ---
      else if (cmd == F("LO"))  manualLED = true;
      else if (cmd == F("LOF")) manualLED = false;

      // --- zmiana godzin nocy ---
      else if (cmd == F("SNI")) eveningStart = wrapHour((int8_t)eveningStart + 1);
      else if (cmd == F("SND")) eveningStart = wrapHour((int8_t)eveningStart - 1);

      else if (cmd == F("SI")) nightEnd = wrapHour((int8_t)nightEnd + 1);
      else if (cmd == F("SD")) nightEnd = wrapHour((int8_t)nightEnd - 1);

      // --- interwał UART ---
      else if (cmd == F("I"))  intervalMs = 1000;
      else if (cmd == F("I2")) intervalMs = 2500;
      else if (cmd == F("I5")) intervalMs = 5000;
      else if (cmd == F("I1")) intervalMs = 10000;

      // reset bufora komendy
      cmd = "";
    }
    else if (c != '\r') {
      // dołącz znak do komendy
      cmd += c;

      // proste zabezpieczenie przed przepełnieniem (śmieci na UART)
      if (cmd.length() > 20) cmd = "";
    }
  }
}

// ================== OBSŁUGA PRZYCISKÓW ==================

// Debounce: zwraca true tylko gdy nastąpi stabilne "wciśnięcie".
//
// Przyciski z INPUT_PULLUP:
/// - puszczony -> HIGH
/// - wciśnięty -> LOW
bool checkPress(Button &b) {
  bool reading = digitalRead(b.pin);
  unsigned long ms = millis();

  // jeśli odczyt się zmienił -> restart licznika stabilizacji
  if (reading != b.lastReading) {
    b.lastChangeMs = ms;
    b.lastReading = reading;
  }

  // jeśli przez DEBOUNCE_MS jest stabilnie -> uznaj nowy stan
  if ((ms - b.lastChangeMs) > DEBOUNCE_MS) {
    if (reading != b.stableState) {
      b.stableState = reading;

      // interesuje nas tylko moment wciśnięcia (LOW)
      if (b.stableState == LOW) return true;
    }
  }
  return false;
}

// Funkcja jest w praktyce MASZYNĄ STANÓW dla przycisków.
// Przy edycji nocy przyciski MODE/TOGGLE zmieniają godziny,
// a poza edycją sterują trybem AUTO/MANUAL i LED w MANUAL.
void handleButtons() {
  // --- Przycisk NIGHT: przechodzenie po etapach edycji ---
  if (checkPress(bNight)) {
    if (nightEdit == NIGHT_EDIT_OFF) {
      // wejście do edycji START
      nightEdit = NIGHT_EDIT_START;
      editStartH = eveningStart;  // kopiuj bieżące ustawienia
      editEndH   = nightEnd;
      editBlinkOn = true;
      lastEditBlink = millis();
    }
    else if (nightEdit == NIGHT_EDIT_START) {
      // przejście do edycji END
      nightEdit = NIGHT_EDIT_END;
      editBlinkOn = true;
      lastEditBlink = millis();
    }
    else {
      // zakończenie edycji: zapis ustawień i wyjście
      eveningStart = editStartH;
      nightEnd     = editEndH;
      nightEdit = NIGHT_EDIT_OFF;
      editBlinkOn = true;
    }
  }

  // --- Jeśli edytujemy noc: MODE/TOGGLE działają jako -/+ godzin ---
  if (nightEdit != NIGHT_EDIT_OFF) {
    // MODE -> minus godzina
    if (checkPress(bMode)) {
      if (nightEdit == NIGHT_EDIT_START)
        editStartH = wrapHour((int8_t)editStartH - 1);
      else
        editEndH = wrapHour((int8_t)editEndH - 1);

      // reset migania po zmianie, żeby wartość była widoczna
      editBlinkOn = true;
      lastEditBlink = millis();
    }

    // TOGGLE -> plus godzina
    if (checkPress(bTog)) {
      if (nightEdit == NIGHT_EDIT_START)
        editStartH = wrapHour((int8_t)editStartH + 1);
      else
        editEndH = wrapHour((int8_t)editEndH + 1);

      editBlinkOn = true;
      lastEditBlink = millis();
    }

    // WAŻNE: w edycji nie wykonuj dalej normalnej obsługi przycisków
    return;
  }

  // --- Normalny tryb pracy: MODE zmienia AUTO/MANUAL ---
  if (checkPress(bMode)) {
    mode = (mode == AUTO) ? MANUAL : AUTO;

    // przy wejściu do MANUAL startuj od LED=OFF
    if (mode == MANUAL) manualLED = false;
  }

  // --- Normalny tryb: TOGGLE przełącza LED tylko w MANUAL ---
  if (checkPress(bTog)) {
    if (mode == MANUAL) manualLED = !manualLED;
  }
}

// ================== SETUP & LOOP ==================

// Jednorazowa inicjalizacja
void setup() {
  Serial.begin(115200);  // UART do PC
  ioInitRegisters();     // ustaw porty AVR (LED/BUZZ/przyciski)
  Wire.begin();          // start I2C
  rtc.begin();           // start RTC
  oledInit();            // start OLED
}

// Główna pętla bez delay (harmonogram na millis)
void loop() {
  // 1) komunikacja i przyciski (wykonuj często)
  readCmd();
  handleButtons();

  // 2) aktualizacja LED i buzzera (działają w tle na millis)
  ledAlarmUpdate(warningState, criticalState, baseLedOn);
  buzzerPassive(warningState, criticalState);

  // 3) co intervalMs wyślij pakiet danych do PC
  if (millis() - lastReadSend >= intervalMs) {
    lastReadSend = millis();
    sendUART();
  }

  // 4) co 250ms: odczyt czujników + logika + OLED
  if (millis() - lastReadMain >= 250) {
    lastReadMain = millis();

    // odczyty ADC
    ldr = analogRead(PIN_LDR);
    tempC = adcToTemp(analogRead(PIN_SHT_TEMP));
    humP  = adcToHum(analogRead(PIN_SHT_HUM));

    // odczyt czasu z RTC
    now = rtc.now();

    // aktualizacja alarmów + sterowania bazowego
    updateLogic();

    // odśwież OLED
    drawOLED();
  }
}
