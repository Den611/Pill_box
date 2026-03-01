#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <ESP32Servo.h>
#include "time.h"
#include <HTTPClient.h>

const char* ssid = "Wokwi-GUEST";
const char* password = "";

String userId = "5118442642";

// NGROK
String pythonServerUrl = "https://cavernous-decayedness-lennie.ngrok-free.dev"; 

const char* ntpServer = "pool.ntp.org";
const long gmtOffset_sec = 7200;
const int daylightOffset_sec = 3600;

// ЧАС
String targetTime = "17:79"; 

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
    Serial.println("\n➡️ Відправка сигналу на Python-сервер: " + eventType);
    
    HTTPClient http;
    String url = pythonServerUrl + "/api/log?user_id=" + userId + "&event=" + eventType;
    
    // Переробляємо HTTPS на звичайний HTTP, щоб Wokwi не зависав на сертифікатах
    url.replace("https://", "http://"); 
    
    http.begin(url); 
    http.addHeader("ngrok-skip-browser-warning", "1"); 
    
    int httpCode = http.GET();
    Serial.print("✅ Код відповіді від сервера: ");
    Serial.println(httpCode);
    
    // Якщо помилка, друкуємо її текстом!
    if (httpCode < 0) {
      Serial.print("❌ Деталі помилки: ");
      Serial.println(http.errorToString(httpCode));
    }
    
    http.end();
  } else {
    Serial.println("❌ Помилка: WiFi не підключено!");
  }
}

long getDistance() {
  digitalWrite(trigPin, LOW);
  delayMicroseconds(2);
  digitalWrite(trigPin, HIGH);
  delayMicroseconds(10);
  digitalWrite(trigPin, LOW);
  
  long duration = pulseIn(echoPin, HIGH, 30000);
  if (duration == 0) return 0;
  return duration * 0.034 / 2; // Переведення в сантиметри
}

void openLid() {
  Serial.println("🔓 Відкриваємо кришку...");
  myServo.write(0);
  
  digitalWrite(buzzerPin, HIGH);
  delay(1000);
  digitalWrite(buzzerPin, LOW);
  
  isLidOpen = true;
  pillTaken = false;
  notifyPython("open"); 
}

void closeLid() {
  Serial.println("🔒 Закриваємо кришку...");
  myServo.write(180);
  isLidOpen = false;
  notifyPython("taken"); 
}

void checkTimeAndOpen() {
  struct tm timeinfo;
  if (!getLocalTime(&timeinfo)) {
    Serial.println("❌ Не вдалося отримати час");
    return;
  }
  
  char currentTime[6];
  sprintf(currentTime, "%02d:%02d", timeinfo.tm_hour, timeinfo.tm_min);

  if (String(currentTime) == targetTime) {
    if (!alreadyOpenedToday && !isLidOpen) {
      Serial.println("⏰ НАСТАВ ЧАС ПРИЙОМУ ЛІКІВ!");
      openLid();
      alreadyOpenedToday = true;
    }
  }
  
  if (String(currentTime) != targetTime) {
    alreadyOpenedToday = false;
  }
}

void setup() {
  Serial.begin(115200);
  delay(1000); // Даємо час терміналу запуститися
  Serial.println("\n=== СТАРТ СИСТЕМИ РОЗУМНОЇ АПТЕЧКИ ===");

  pinMode(trigPin, OUTPUT);
  pinMode(echoPin, INPUT);
  pinMode(buzzerPin, OUTPUT);

  myServo.attach(servoPin);
  myServo.write(180); // Початкове положення - закрито

  Serial.print("⏳ Підключення до WiFi...");
  WiFi.begin(ssid, password);
  while (WiFi.status() != WL_CONNECTED) { 
    delay(500); 
    Serial.print("."); 
  }
  Serial.println("\n✅ WiFi ПІДКЛЮЧЕНО!");

  Serial.print("⏳ Синхронізація часу...");
  configTime(gmtOffset_sec, daylightOffset_sec, ntpServer);
  
  struct tm timeinfo;
  while (!getLocalTime(&timeinfo)) {
    Serial.print(".");
    delay(500);
  }
  Serial.println("\n✅ Час синхронізовано. Чекаю на спрацювання...");
}

void loop() {
  // Перевірка часу кожні 5 секунд
  static unsigned long lastTimeCheck = 0;
  if (millis() - lastTimeCheck > 5000) {
    checkTimeAndOpen();
    lastTimeCheck = millis();
  }

  // Логіка роботи з таблеткою (якщо кришка відкрита)
  if (isLidOpen && !pillTaken) {
    long distance = getDistance();
    
    // Пишемо дистанцію в термінал кожні 2 секунди
    static unsigned long lastDistPrint = 0;
    if (millis() - lastDistPrint > 2000) {
      Serial.print("📏 Відстань до таблетки: ");
      Serial.print(distance);
      Serial.println(" см");
      lastDistPrint = millis();
    }
    
    // Якщо рука забрала таблетку (відстань > 15 см)
    if (distance > 15) {
      delay(1500); // Чекаємо 1.5 секунди для надійності
      if (getDistance() > 15) { 
        Serial.println("💊 Таблетку забрали з аптечки!");
        pillTaken = true;
        closeLid();
      }
    } else {
      // Якщо таблетка ще лежить, нагадуємо кожні 30 секунд
      if (millis() - lastReminder > 30000) {
        Serial.println("⚠️ Нагадування: таблетка ще лежить!");
        digitalWrite(buzzerPin, HIGH);
        delay(500);
        digitalWrite(buzzerPin, LOW);
        notifyPython("remind"); 
        lastReminder = millis();
      }
    }
  }
  delay(200);
}