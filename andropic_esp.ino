/*
  andropic GO — прошивка для ESP32 / ESP8266 (Arduino IDE).
  Плата подключается к Wi-Fi и отвечает на команды приложения.

  Что поменять: WIFI_SSID, WIFI_PASS и (по желанию) API_TOKEN.
  После загрузки открой Serial Monitor (115200) — там будет IP-адрес платы.

  Обычная Arduino Uno/Nano по Wi-Fi не умеет: нужна ESP32, ESP8266 (NodeMCU, Wemos D1)
  или плата с Wi-Fi вроде Nano 33 IoT.
*/

#if defined(ESP32)
  #include <WiFi.h>
  #include <WebServer.h>
  WebServer server(80);
  #define CHIP_NAME "ESP32"
  #define LED_ON  HIGH
  #define LED_OFF LOW
#else
  #include <ESP8266WiFi.h>
  #include <ESP8266WebServer.h>
  ESP8266WebServer server(80);
  #define CHIP_NAME "ESP8266"
  #define LED_ON  LOW   // у ESP8266 встроенный светодиод включается нулём
  #define LED_OFF HIGH
#endif

const char* WIFI_SSID = "ИМЯ_ТВОЕЙ_WIFI";
const char* WIFI_PASS = "ПАРОЛЬ_WIFI";
const char* API_TOKEN = "andropic";   // тот же код укажи в приложении (Опции → Arduino)

#ifndef LED_BUILTIN
  #define LED_BUILTIN 2
#endif

bool ledState = false;

bool authorized() {
  return server.header("X-Token") == API_TOKEN || server.arg("token") == API_TOKEN;
}

void sendJson(int code, const String& body) {
  server.sendHeader("Access-Control-Allow-Origin", "*");
  server.send(code, "application/json", body);
}

void setLed(bool on) {
  ledState = on;
  digitalWrite(LED_BUILTIN, on ? LED_ON : LED_OFF);
}

void handlePing() {  // без кода: нужен для поиска в сети
  sendJson(200, String("{\"app\":\"andropic\",\"type\":\"arduino\",\"name\":\"") + CHIP_NAME + "\"}");
}

void handleStatus() {
  if (!authorized()) { sendJson(401, "{\"error\":\"unauthorized\"}"); return; }
  String j = "{\"app\":\"andropic\",\"chip\":\"" CHIP_NAME "\"";
  j += ",\"led\":" + String(ledState ? "true" : "false");
  j += ",\"rssi\":" + String(WiFi.RSSI());
  j += ",\"uptime\":" + String(millis() / 1000);
  j += ",\"heap\":" + String(ESP.getFreeHeap()) + "}";
  sendJson(200, j);
}

void handleLed() {  // /api/led?state=on|off|toggle
  if (!authorized()) { sendJson(401, "{\"error\":\"unauthorized\"}"); return; }
  String s = server.arg("state");
  if (s == "on") setLed(true);
  else if (s == "off") setLed(false);
  else setLed(!ledState);
  sendJson(200, String("{\"ok\":true,\"led\":") + (ledState ? "true" : "false") + "}");
}

void handlePin() {  // /api/pin?n=5&v=1
  if (!authorized()) { sendJson(401, "{\"error\":\"unauthorized\"}"); return; }
  if (!server.hasArg("n") || !server.hasArg("v")) { sendJson(400, "{\"error\":\"need n and v\"}"); return; }
  int n = server.arg("n").toInt();
  int v = server.arg("v").toInt();
  pinMode(n, OUTPUT);
  digitalWrite(n, v ? HIGH : LOW);
  sendJson(200, String("{\"ok\":true,\"pin\":") + n + ",\"v\":" + v + "}");
}

void handleAnalog() {  // /api/analog?n=0
  if (!authorized()) { sendJson(401, "{\"error\":\"unauthorized\"}"); return; }
  int n = server.arg("n").toInt();
#if defined(ESP32)
  int value = analogRead(n) / 4;   // 12 бит → 10 бит, чтобы шкала была 0..1023
#else
  int value = analogRead(A0);      // у ESP8266 единственный аналоговый вход
  (void)n;
#endif
  sendJson(200, String("{\"ok\":true,\"value\":") + value + "}");
}

void setup() {
  Serial.begin(115200);
  pinMode(LED_BUILTIN, OUTPUT);
  setLed(false);

  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  Serial.print("Подключаюсь к Wi-Fi");
  while (WiFi.status() != WL_CONNECTED) {
    delay(400);
    Serial.print(".");
    digitalWrite(LED_BUILTIN, !digitalRead(LED_BUILTIN));  // мигаем, пока ищем сеть
  }
  setLed(false);
  Serial.println();
  Serial.print("Готово! IP платы: ");
  Serial.println(WiFi.localIP());

  const char* keys[] = {"X-Token"};
  server.collectHeaders(keys, 1);
  server.on("/api/ping", handlePing);
  server.on("/api/status", handleStatus);
  server.on("/api/led", handleLed);
  server.on("/api/pin", handlePin);
  server.on("/api/analog", handleAnalog);
  server.begin();
}

void loop() {
  server.handleClient();
}
