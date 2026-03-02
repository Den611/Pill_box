#include <Arduino.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <ESP32Servo.h>
#include "time.h"
#include <HTTPClient.h>
#include <LiquidCrystal_I2C.h> // ⬅️ ДОДАНО БІБЛІОТЕКУ ЕКРАНУ

// ⬅️ НАЛАШТУВАННЯ ЕКРАНУ (адреса 0x27, 16 символів, 2 рядки)
LiquidCrystal_I2C lcd(0x27, 16, 2);

const char* ssid = "Wokwi-GUEST";
const char* password = "";

String userId = "5118442642";
//лінк
String pythonServerUrl = "https://djpuma-pillbox.loca.lt"; 

const char* ntpServer = "pool.ntp.org";
const long gmtOffset_sec = 7200;
const int daylightOffset_sec = 3600;

// ЧАС
String targetTime = "12:06"; 

bool alreadyOpenedToday = false;
bool isLidOpen = false;
bool pillTaken = false;
unsigned long lastReminder = 0;

const int trigPin = 14;
const int echoPin = 16;
const int servoPin = 18;
const int buzzerPin = 13;

Servo myServo;

void notifyPython(String eventType) {
  if(WiFi.status() == WL_CONNECTED) {
    lcd.clear();
    lcd.setCursor(0, 0);
    lcd.print("Snd: ");
    lcd.print(eventType);
    
    lcd.setCursor(0, 1);
    lcd.print("Connecting..."); 

    // 1. Створюємо БЕЗПЕЧНОГО клієнта
    WiFiClientSecure client;
    client.setInsecure(); // Вимикаємо перевірку сертифікатів (щоб не було -1)
    client.setTimeout(15000); // Даємо час на "рукостискання"

    HTTPClient http;
    String url = pythonServerUrl + "/api/log?user_id=" + userId + "&event=" + eventType;
    Serial.print("🔗 Стукаю сюди: "); Serial.println(url);

    // 2. Передаємо КЛІЄНТА і URL разом
    http.begin(client, url); 
    
    // 3. Дозволяємо перенаправлення (на випадок примх Ngrok)
    http.setFollowRedirects(HTTPC_STRICT_FOLLOW_REDIRECTS);
    http.addHeader("ngrok-skip-browser-warning", "1"); 
    
    int httpCode = http.GET();
    
    lcd.clear();
    if (httpCode == 200) {
      lcd.setCursor(0, 0); lcd.print("Success!");
      lcd.setCursor(0, 1); lcd.print("Code: 200");
      Serial.println("✅ Успіх! Код 200. Повідомлення полетіло в ТГ!");
    } else {
      String errMsg = http.errorToString(httpCode);
      lcd.setCursor(0, 0); lcd.print("Err: "); lcd.print(httpCode);
      lcd.setCursor(0, 1); lcd.print(errMsg.substring(0, 16));
      Serial.print("❌ Помилка: "); Serial.println(errMsg);
    }
    
    http.end();
    delay(3000); 
    lcd.clear();
  } else {
    lcd.clear(); 
    lcd.setCursor(0, 0);
    lcd.print("No WiFi Error");
    delay(2000);
  }
}long getDistance() {
  digitalWrite(trigPin, LOW); delayMicroseconds(2);
  digitalWrite(trigPin, HIGH); delayMicroseconds(10);
  digitalWrite(trigPin, LOW);
  long duration = pulseIn(echoPin, HIGH, 30000);
  if (duration == 0) return 0;
  return duration * 0.034 / 2;
}

void openLid() {
  Serial.println("🔓 Відкриваємо кришку...");
  // ⬅️ ДОДАНО: Екран при відкритті
  lcd.clear(); lcd.print("Time for pill!");
  
  myServo.write(0);
  digitalWrite(buzzerPin, HIGH); delay(1000); digitalWrite(buzzerPin, LOW);
  isLidOpen = true; pillTaken = false;
  notifyPython("open"); 
}

void closeLid() {
  Serial.println("🔒 Закриваємо кришку...");
  // ⬅️ ДОДАНО: Екран при закритті
  lcd.clear(); lcd.print("Pill taken."); lcd.setCursor(0,1); lcd.print("Closing...");
  
  myServo.write(180);
  isLidOpen = false;
  notifyPython("taken"); 
}

void checkTimeAndOpen() {
  struct tm timeinfo;
  if (!getLocalTime(&timeinfo)) return;
  char currentTime[6];
  sprintf(currentTime, "%02d:%02d", timeinfo.tm_hour, timeinfo.tm_min);

  if (String(currentTime) == targetTime) {
    if (!alreadyOpenedToday && !isLidOpen) {
      Serial.println("⏰ НАСТАВ ЧАС ПРИЙОМУ ЛІКІВ!");
      openLid(); alreadyOpenedToday = true;
    }
  }
  if (String(currentTime) != targetTime) alreadyOpenedToday = false;
}

void setup() {
  // ⬅️ ДОДАНО: Вмикаємо екран на старті
  lcd.init();
  lcd.backlight();
  lcd.setCursor(0, 0);
  lcd.print("System Starting");

  Serial.begin(115200); delay(1000);
  Serial.println("\n=== СТАРТ СИСТЕМИ РОЗУМНОЇ АПТЕЧКИ ===");

  pinMode(trigPin, OUTPUT); pinMode(echoPin, INPUT); pinMode(buzzerPin, OUTPUT);
  myServo.attach(servoPin); myServo.write(180);

  Serial.print("⏳ Підключення до WiFi...");
  lcd.setCursor(0, 1); lcd.print("WiFi connect...");
  WiFi.begin(ssid, password);
  while (WiFi.status() != WL_CONNECTED) { delay(500); Serial.print("."); }
  Serial.println("\n✅ WiFi ПІДКЛЮЧЕНО!");

  Serial.print("⏳ Синхронізація часу...");
  configTime(gmtOffset_sec, daylightOffset_sec, ntpServer);
  struct tm timeinfo;
  while (!getLocalTime(&timeinfo)) { Serial.print("."); delay(500); }
  Serial.println("\n✅ Час синхронізовано.");

  Serial.println("✅ Успішний запуск! Сповіщаю бота...");
  notifyPython("connected"); 
  
  delay(2000); // Даємо час почитати екран
  lcd.clear(); lcd.print("System Ready!"); lcd.setCursor(0, 1); lcd.print("Waiting...");
}

void loop() {
  static unsigned long lastTimeCheck = 0;
  if (millis() - lastTimeCheck > 5000) { checkTimeAndOpen(); lastTimeCheck = millis(); }

  if (isLidOpen && !pillTaken) {
    long distance = getDistance();
    static unsigned long lastDistPrint = 0;
    if (millis() - lastDistPrint > 2000) {
      Serial.print("📏 Відстань до таблетки: "); Serial.print(distance); Serial.println(" см");
      lastDistPrint = millis();
    }
    if (distance > 15) {
      delay(1500);
      if (getDistance() > 15) { 
        Serial.println("💊 Таблетку забрали з аптечки!"); pillTaken = true; closeLid(); 
      }
    } else {
      if (millis() - lastReminder > 30000) {
        Serial.println("⚠️ Нагадування: таблетка ще лежить!");
        // ⬅️ ДОДАНО: Екран при нагадуванні
        lcd.clear(); lcd.print("Take your pill!");
        digitalWrite(buzzerPin, HIGH); delay(500); digitalWrite(buzzerPin, LOW);
        notifyPython("remind"); lastReminder = millis();
      }
    }
  }
  delay(200);
}
