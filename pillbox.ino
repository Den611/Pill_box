#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <ESP32Servo.h>
#include <UniversalTelegramBot.h>
#include "time.h"
#include <HTTPClient.h>

// ---------- WIFI ----------
const char* ssid = "Wokwi-GUEST";
const char* password = "";

// ---------- TELEGRAM ТА СЕРВЕР ----------
const char* botToken ="8622592417:AAFx0RFEPlAtydcD8hQYtkrxzGfL6X0I1Vs";
String userId = "5118442642";

// 👉 СЮДИ ВСТАВТЕ ВАШЕ ПОТОЧНЕ ПОСИЛАННЯ NGROK (без слеша / в кінці)
String pythonServerUrl = " https://cavernous-decayedness-lennie.ngrok-free.dev"; 

// ---------- ЧАС ----------
const char* ntpServer = "pool.ntp.org";
const long gmtOffset_sec = 7200;
const int daylightOffset_sec = 3600;

// 👉 ЧАС ПРИЙОМУ
String targetTime = "14:40";

bool alreadyOpenedToday = false;
bool isLidOpen = false;
bool pillTaken = false;

// ---------- ТАЙМЕР НАГАДУВАННЯ ----------
unsigned long lastReminder = 0;

// ---------- ПІНИ ----------
const int trigPin = 14;
const int echoPin = 16;
const int servoPin = 18;
const int buzzerPin = 13;

Servo myServo;

// Глобальний клієнт ТІЛЬКИ для Телеграму
WiFiClientSecure client;
UniversalTelegramBot bot(botToken, client);


// ---------- СИГНАЛ ДЛЯ PYTHON ----------
void notifyPython(String eventType) {
  if(WiFi.status() == WL_CONNECTED) {
    
    // Створюємо ОКРЕМИЙ клієнт тільки для запитів на сервер, щоб не сварився з Телеграмом
    WiFiClientSecure httpsClient; 
    httpsClient.setInsecure(); 

    HTTPClient http;
    String url = pythonServerUrl + "/api/log?user_id=" + userId + "&event=" + eventType;
    
    http.begin(httpsClient, url); 
    
    // Секретний заголовок, щоб обійти сторінку-попередження Ngrok
    http.addHeader("ngrok-skip-browser-warning", "1"); 
    
    int httpCode = http.GET();
    
    Serial.print("Сигнал пайтону [");
    Serial.print(eventType);
    Serial.print("] Код відповіді: ");
    Serial.println(httpCode);
    
    http.end();
  } else {
    Serial.println("Помилка: WiFi не підключено!");
  }
}

// ---------- ВІДСТАНЬ ----------
long getDistance() {
  digitalWrite(trigPin, LOW);
  delayMicroseconds(2);

  digitalWrite(trigPin, HIGH);
  delayMicroseconds(10);

  digitalWrite(trigPin, LOW);

  long duration = pulseIn(echoPin, HIGH, 30000);

  if (duration == 0) return 0;

  return duration * 0.034 / 2;
}

// ---------- ВІДКРИТИ ----------
void openLid() {
  Serial.println("OPEN");

  myServo.write(0);

  digitalWrite(buzzerPin, HIGH);
  delay(1000);
  digitalWrite(buzzerPin, LOW);

  isLidOpen = true;
  pillTaken = false;

  bot.sendMessage(
    userId,
    "💊 ЧАС ПРИЙМАТИ ЛІКИ (" + targetTime + ")!",
    ""
  );

  notifyPython("open"); // Сповіщаємо Python про відкриття
}

// ---------- ЗАКРИТИ ----------
void closeLid() {
  Serial.println("CLOSE");

  myServo.write(180);
  isLidOpen = false;

  bot.sendMessage(
    userId,
    "✅ Таблетку взято. Комірку закрито.",
    ""
  );

  notifyPython("taken"); // Сповіщаємо Python, що таблетку забрали
}

// ---------- ПЕРЕВІРКА ЧАСУ ----------
void checkTimeAndOpen() {

  struct tm timeinfo;
  if (!getLocalTime(&timeinfo)) return;

  char currentTime[6];
  sprintf(currentTime, "%02d:%02d",
          timeinfo.tm_hour,
          timeinfo.tm_min);

  Serial.println(currentTime);

  if (String(currentTime) == targetTime) {
    if (!alreadyOpenedToday && !isLidOpen) {
      openLid();
      alreadyOpenedToday = true;
    }
  }

  if (String(currentTime) != targetTime) {
    alreadyOpenedToday = false;
  }
}

// ---------- SETUP ----------
void setup() {

  Serial.begin(115200);

  pinMode(trigPin, OUTPUT);
  pinMode(echoPin, INPUT);
  pinMode(buzzerPin, OUTPUT);

  myServo.attach(servoPin);
  myServo.write(180);

  client.setInsecure(); // Дозвіл для Телеграму

  // WiFi
  WiFi.begin(ssid, password);
  Serial.print("WiFi");

  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }

  Serial.println("\n✅ WiFi connected");

  // Час
  configTime(
    gmtOffset_sec,
    daylightOffset_sec,
    ntpServer
  );
}

// ---------- LOOP ----------
void loop() {

  // ✅ перевірка часу кожні 10 сек
  static unsigned long lastTimeCheck = 0;
  if (millis() - lastTimeCheck > 10000) {
    checkTimeAndOpen();
    lastTimeCheck = millis();
  }

  // ✅ логіка таблетки
  if (isLidOpen && !pillTaken) {

    long distance = getDistance();
    Serial.print("Distance: ");
    Serial.println(distance);

    // ---------- ТАБЛЕТКУ ЗАБРАЛИ ----------
    if (distance > 15) {

      delay(1500);

      if (getDistance() > 15) {
        pillTaken = true;
        closeLid();
      }
    }
    // ---------- ТАБЛЕТКА ЩЕ Є ----------
    else {

      // нагадування кожні 30 сек
      if (millis() - lastReminder > 30000) {

        bot.sendMessage(
          userId,
          "⏰ Будь ласка, прийміть ліки!",
          ""
        );

        digitalWrite(buzzerPin, HIGH);
        delay(500);
        digitalWrite(buzzerPin, LOW);

        notifyPython("remind"); // Сповіщаємо Python, що пацієнт ігнорує ліки

        lastReminder = millis();
      }
    }
  }

  delay(200);
}