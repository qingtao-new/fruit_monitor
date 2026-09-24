#include <Arduino.h>
#include <ESP8266WiFi.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>
#include <WiFiManager.h>

#define LED_PIN 2
#define KEY_PIN 0
#define SERIAL_BAUD 115200
#define SERIAL_BUFFER_LEN 512

const char* ONENET_PRODUCT_ID = "xmcyqr1v37";
const char* ONENET_DEVICE_NAME = "2";
const char* ONENET_AUTH_TOKEN = "version=2018-10-31&res=products%2Fxmcyqr1v37%2Fdevices%2F2&et=2081155294&method=md5&sign=%2BBIPZAxLEu6lQJ9%2FM7Zk3A%3D%3D";
const char* MQTT_SERVER = "mqtts.heclouds.com";
const uint16_t MQTT_PORT = 1883;

// 建议改成固定内网域名/主机名，避免 PC IP 变化后每次都要重刷固件。
const char* LOCAL_MQTT_HOST = "192.168.1.102";
const uint16_t LOCAL_MQTT_PORT = 1883;
const char* LOCAL_CLIENT_ID = "fruit_monitor_esp8266";
const char* LOCAL_TOPIC = "fruit/GW_001/LORA_NODE_01/sensor";

const char* UPLOAD_TOPIC = "$sys/xmcyqr1v37/2/thing/property/post";
const char* CMD_SUB_TOPIC = "$sys/xmcyqr1v37/2/thing/property/set";
const char* REPLY_SUB_TOPIC = "$sys/xmcyqr1v37/2/thing/property/post/reply";

WiFiClient espClient;
PubSubClient mqttClient(espClient);

WiFiClient espClient2;
PubSubClient mqttClientLocal(espClient2);

WiFiManager wm;

unsigned long lastWifiReconnect = 0;
#define WIFI_RECONNECT_INTERVAL 5000

JsonDocument parseDoc;
JsonDocument reportDoc;
JsonDocument localDoc;

int soil_moisture = 0;
float ph_value = 6.0;
int h2s = 0;
int co2 = 14;
float temperature = 0.0;
int humidity = 0;
int nh3 = 0;
String node_id = "123";
String currentRfidId = "0000";

bool firstReportDone = false;
char serialBuffer[SERIAL_BUFFER_LEN];
int serialIndex = 0;

void ledOn() { digitalWrite(LED_PIN, LOW); }
void ledOff() { digitalWrite(LED_PIN, HIGH); }

void ledBlink(int times, int delayMs) {
  for (int i = 0; i < times; i++) {
    ledOn(); delay(delayMs);
    ledOff(); delay(delayMs);
  }
}

void ledPulse(int pulseMs) {
  ledOn(); delay(pulseMs); ledOff();
}

void ledWifiLost() { ledOn(); }

bool autoConnectWifi() {
  wm.setTimeout(120);
  wm.setAPStaticIPConfig(IPAddress(192,168,4,1), IPAddress(192,168,4,1), IPAddress(255,255,255,0));
  bool res = wm.autoConnect("ESP01_CONFIG");
  if (!res) {
    Serial.println("WiFi配网失败");
    ledBlink(5, 80);
    return false;
  }
  Serial.print("WiFi连接成功:");
  Serial.println(WiFi.SSID());
  Serial.print("STA IP Address:");
  Serial.println(WiFi.localIP());
  ledBlink(3, 80);
  lastWifiReconnect = millis();
  return true;
}

void mqttCallback(char* topic, byte* payload, unsigned int length) {
  String topicStr(topic);
  String rawData;
  rawData.reserve(length + 1);
  for (int i = 0; i < length; i++) rawData += (char)payload[i];

  if (topicStr == CMD_SUB_TOPIC) {
    if (rawData.indexOf("\"LED\":1") != -1) Serial.println("LORA_01:LED_ON");
    else if (rawData.indexOf("\"LED\":0") != -1) Serial.println("LORA_01:LED_OFF");
    return;
  }

  if (topicStr == REPLY_SUB_TOPIC) {
    JsonDocument doc;
    DeserializationError err = deserializeJson(doc, rawData);
    if (!err) {
      long code = doc["code"].as<long>();
      Serial.print("上报回执code:");
      Serial.println(code);
    }
  }
}

bool mqttConnect() {
  String clientId = String(ONENET_DEVICE_NAME);
  bool ok = mqttClient.connect(clientId.c_str(), ONENET_PRODUCT_ID, ONENET_AUTH_TOKEN);
  if (ok) {
    mqttClient.subscribe(CMD_SUB_TOPIC);
    mqttClient.subscribe(REPLY_SUB_TOPIC);
    Serial.println("MQTT连接成功");
    return true;
  }
  Serial.print("MQTT错误码:");
  Serial.println(mqttClient.state());
  return false;
}

bool mqttConnectLocal() {
  mqttClientLocal.setServer(LOCAL_MQTT_HOST, LOCAL_MQTT_PORT);
  mqttClientLocal.setBufferSize(512);

  if (mqttClientLocal.connected()) {
    mqttClientLocal.loop();
    return true;
  }

  bool ok = mqttClientLocal.connect(LOCAL_CLIENT_ID);
  if (ok) {
    Serial.println("本地 MQTT 连接成功");
  } else {
    Serial.print("本地 MQTT 连接失败，错误码: ");
    Serial.println(mqttClientLocal.state());
  }

  mqttClientLocal.loop();
  return ok;
}

void reportSensorData() {
  if (WiFi.status() != WL_CONNECTED || !mqttClient.connected()) return;

  reportDoc.clear();

  reportDoc["id"] = node_id;
  JsonObject params = reportDoc.createNestedObject("params");

  params.createNestedObject("soil_moisture")["value"] = soil_moisture;
  params.createNestedObject("ph_value")["value"] = ph_value;
  params.createNestedObject("h2s")["value"] = h2s;
  params.createNestedObject("co2")["value"] = co2;
  params.createNestedObject("temperature")["value"] = temperature;
  params.createNestedObject("humidity")["value"] = humidity;
  params.createNestedObject("nh3")["value"] = nh3;
  params.createNestedObject("rfid_id")["value"] = currentRfidId;

  char buf[500];
  size_t len = serializeJson(reportDoc, buf, sizeof(buf));
  Serial.print("上报报文长度:");
  Serial.print(len);
  Serial.print("字节 内容:");
  Serial.println(buf);

  int ret = mqttClient.publish(UPLOAD_TOPIC, buf);
  if (ret) {
    ledBlink(2, 50);
    Serial.println("OneNET 上报OK");
  } else {
    ledBlink(6, 50);
    Serial.print("上报FAIL，MQTT连接状态码:");
    Serial.println(mqttClient.state());
  }
}

void publishToLocal() {
  if (!mqttClientLocal.connected()) return;

  localDoc.clear();

  localDoc["gateway_id"] = "GW_001";
  localDoc["node_id"] = node_id;
  localDoc["timestamp"] = millis() / 1000UL;
  localDoc["soil_moisture"] = soil_moisture;
  localDoc["temperature"] = temperature;
  localDoc["nh3"] = nh3;
  localDoc["h2s"] = h2s;
  localDoc["co2"] = co2;
  localDoc["ph"] = ph_value;
  localDoc["humidity"] = humidity;

  char buf[500];
  size_t len = serializeJson(localDoc, buf, sizeof(buf));
  bool ok = mqttClientLocal.publish(LOCAL_TOPIC, buf);

  Serial.print("本地发送结果: ");
  Serial.println(ok ? "OK" : "FAIL");
  Serial.print("本地报文长度:");
  Serial.print(len);
  Serial.print("字节 内容:");
  Serial.println(buf);
}

void parseSerialLine(const char* line) {
  Serial.print("[收到原始报文] 长度:");
  Serial.print(strlen(line));
  Serial.print(" 内容:");
  Serial.println(line);

  if (strncmp(line, "UP:", 3) != 0) return;
  const char* payload = line + 3;

  parseDoc.clear();
  DeserializationError err = deserializeJson(parseDoc, payload);
  if (err) {
    Serial.print("JSON解析错误: ");
    Serial.println(err.c_str());
    return;
  }

  if (parseDoc.containsKey("node_id")) node_id = parseDoc["node_id"].as<String>();
  else if (parseDoc.containsKey("id")) node_id = parseDoc["id"].as<String>();
  if (parseDoc.containsKey("soil_moisture")) soil_moisture = parseDoc["soil_moisture"].as<int>();
  if (parseDoc.containsKey("ph")) ph_value = parseDoc["ph"].as<float>();
  if (parseDoc.containsKey("h2s")) h2s = parseDoc["h2s"].as<int>();
  if (parseDoc.containsKey("co2")) co2 = parseDoc["co2"].as<int>();
  if (parseDoc.containsKey("temperature")) temperature = parseDoc["temperature"].as<float>();
  if (parseDoc.containsKey("humidity")) humidity = parseDoc["humidity"].as<int>();
  if (parseDoc.containsKey("nh3")) nh3 = parseDoc["nh3"].as<int>();
  if (parseDoc.containsKey("rfid_id")) currentRfidId = parseDoc["rfid_id"].as<String>();

  reportSensorData();
  publishToLocal();
  ledPulse(15);
}

void setup() {
  pinMode(LED_PIN, OUTPUT);
  pinMode(KEY_PIN, INPUT_PULLUP);
  ledOn();
  delay(3000);

  Serial.begin(SERIAL_BAUD);
  while (Serial.available()) Serial.read();
  memset(serialBuffer, 0, sizeof(serialBuffer));
  serialIndex = 0;

  WiFi.persistent(true);
  WiFi.mode(WIFI_STA);

  mqttClient.setServer(MQTT_SERVER, MQTT_PORT);
  mqttClient.setBufferSize(512);
  mqttClient.setCallback(mqttCallback);

  mqttClientLocal.setBufferSize(512);

  ledOff();
  autoConnectWifi();
  if (WiFi.status() == WL_CONNECTED) mqttConnect();
}

void loop() {
  unsigned long now = millis();
  static unsigned long keyPressStart = 0;

  if (digitalRead(KEY_PIN) == LOW) {
    if (keyPressStart == 0) keyPressStart = now;
    if (now - keyPressStart >= 2000) {
      Serial.println("清除WiFi配置并重启");
      wm.resetSettings();
      ESP.restart();
      return;
    }
  } else {
    keyPressStart = 0;
  }

  if (WiFi.status() != WL_CONNECTED) {
    ledWifiLost();
    if (now - lastWifiReconnect > WIFI_RECONNECT_INTERVAL) {
      lastWifiReconnect = now;
      autoConnectWifi();
      if (WiFi.status() == WL_CONNECTED) mqttConnect();
    }
  } else {
    ledOff();
    mqttClient.loop();
    mqttConnectLocal();
    if (!mqttClient.connected()) mqttConnect();
  }

  if (!firstReportDone && WiFi.status() == WL_CONNECTED && mqttClient.connected()) {
    reportSensorData();
    publishToLocal();
    firstReportDone = true;
    Serial.println("首次默认值上报完成");
  }

  while (Serial.available() > 0) {
    char ch = Serial.read();
    if (ch == '\n' || ch == '\r') {
      if (serialIndex > 0) {
        serialBuffer[serialIndex] = '\0';
        parseSerialLine(serialBuffer);
        memset(serialBuffer, 0, sizeof(serialBuffer));
        serialIndex = 0;
      }
    } else if (serialIndex < (SERIAL_BUFFER_LEN - 1)) {
      serialBuffer[serialIndex++] = ch;
    } else {
      memset(serialBuffer, 0, sizeof(serialBuffer));
      serialIndex = 0;
      Serial.println("[WARN] 串口数据超长，清空缓冲区");
    }
  }

  delay(10);
}
