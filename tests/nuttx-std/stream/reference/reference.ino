// Optional same-board benchmark of the installed Espressif camera driver.
// Wi-Fi credentials arrive over no-echo serial and are never compiled in.
#include <WiFi.h>
#include "esp_camera.h"

static WiFiServer server(8080);
static uint32_t sequence;
static unsigned long end_ms;

static bool read_exact(uint8_t *data, size_t size) {
  return Serial.readBytes(data, size) == size;
}

static bool credentials() {
  uint8_t sizes[2];
  char ssid[33] = {}, password[64] = {};
  Serial.println("NXRS_WIFI_CREDENTIALS_READY");
  if (!read_exact(sizes, 2) || !sizes[0] || sizes[0] > 32 || sizes[1] > 63 ||
      !read_exact(reinterpret_cast<uint8_t *>(ssid), sizes[0]) ||
      !read_exact(reinterpret_cast<uint8_t *>(password), sizes[1])) return false;
  WiFi.persistent(false);
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  WiFi.begin(ssid, password);
  memset(password, 0, sizeof(password));
  unsigned long deadline = millis() + 30000;
  while (WiFi.status() != WL_CONNECTED && millis() < deadline) delay(10);
  return WiFi.status() == WL_CONNECTED;
}

static bool write_all(WiFiClient &client, const uint8_t *data, size_t length) {
  const unsigned long deadline = millis() + 3000;
  while (length && client.connected() && millis() < deadline) {
    size_t sent = client.write(data, length);
    data += sent;
    length -= sent;
    if (!sent) delay(1);
  }
  return length == 0;
}

void setup() {
  Serial.begin(115200);
  Serial.setTimeout(30000);
  while (!Serial) delay(10);
  Serial.println("NXRS_REFERENCE_READY");
  // Public numeric settings only: width height XCLK_Hz quality buffers.
  char line[96] = {};
  Serial.readBytesUntil('\n', line, sizeof(line) - 1);
  unsigned width, height, xclk, quality, buffers;
  if (sscanf(line, "%u %u %u %u %u", &width, &height, &xclk, &quality, &buffers) != 5 ||
      quality > 63 || buffers < 1 || buffers > 2 ||
      (xclk != 10000000 && xclk != 16000000 && xclk != 20000000)) {
    Serial.println("NXRS_REFERENCE_FAIL config");
    return;
  }
  framesize_t size;
  if (width == 320 && height == 240) size = FRAMESIZE_QVGA;
  else if (width == 640 && height == 480) size = FRAMESIZE_VGA;
  else if (width == 800 && height == 600) size = FRAMESIZE_SVGA;
  else { Serial.println("NXRS_REFERENCE_FAIL resolution"); return; }
  camera_config_t config = {};
  config.ledc_channel = LEDC_CHANNEL_0;
  config.ledc_timer = LEDC_TIMER_0;
  config.pin_d0 = 11; config.pin_d1 = 9; config.pin_d2 = 8; config.pin_d3 = 10;
  config.pin_d4 = 12; config.pin_d5 = 18; config.pin_d6 = 17; config.pin_d7 = 16;
  config.pin_xclk = 15; config.pin_pclk = 13; config.pin_vsync = 6; config.pin_href = 7;
  config.pin_sscb_sda = 4; config.pin_sscb_scl = 5;
  config.pin_pwdn = -1; config.pin_reset = -1;
  config.xclk_freq_hz = xclk;
  config.pixel_format = PIXFORMAT_JPEG;
  config.frame_size = size;
  config.jpeg_quality = quality;
  config.fb_count = buffers;
  config.fb_location = CAMERA_FB_IN_PSRAM;
  config.grab_mode = CAMERA_GRAB_LATEST;
  if (esp_camera_init(&config) != ESP_OK) {
    Serial.println("NXRS_REFERENCE_FAIL camera");
    return;
  }
  const unsigned long started = millis();
  uint32_t count = 0, bytes = 0, largest = 0;
  while (millis() - started < 30000) {
    camera_fb_t *frame = esp_camera_fb_get();
    if (!frame) { Serial.println("NXRS_REFERENCE_FAIL capture"); return; }
    if (frame->len < 4 || frame->buf[0] != 0xff || frame->buf[1] != 0xd8 ||
        frame->buf[frame->len - 2] != 0xff || frame->buf[frame->len - 1] != 0xd9) {
      esp_camera_fb_return(frame);
      Serial.println("NXRS_REFERENCE_FAIL jpeg");
      return;
    }
    ++count;
    bytes += frame->len;
    if (frame->len > largest) largest = frame->len;
    esp_camera_fb_return(frame);
  }
  Serial.printf("NXRS_REFERENCE_CAPTURE frames=%lu elapsed_ms=%lu bytes=%lu max_bytes=%lu\n",
                (unsigned long)count, millis() - started, (unsigned long)bytes,
                (unsigned long)largest);
  sensor_t *sensor = esp_camera_sensor_get();
  Serial.printf("NXRS_REFERENCE_SENSOR pll0=%d pll1=%d pll2=%d pll3=%d pclk=%d hts=%d vts=%d exposure=%d\n",
                sensor->get_reg(sensor, 0x303a, 0xff), sensor->get_reg(sensor, 0x303b, 0xff),
                sensor->get_reg(sensor, 0x303c, 0xff), sensor->get_reg(sensor, 0x303d, 0xff),
                sensor->get_reg(sensor, 0x3824, 0xff), sensor->get_reg(sensor, 0x380c, 0xffff),
                sensor->get_reg(sensor, 0x380e, 0xffff), sensor->get_reg(sensor, 0x3500, 0xfffff));
  if (!credentials()) { Serial.println("NXRS_REFERENCE_FAIL wifi"); return; }
  server.begin();
  end_ms = millis() + 60000;
  Serial.printf("NXRS_STREAM_ONLINE ip=%s port=8080 ms=%lu\n",
                WiFi.localIP().toString().c_str(), millis());
}

void loop() {
  if (!end_ms) { delay(10); return; }
  if (millis() >= end_ms) {
    server.end();
    WiFi.disconnect(true);
    esp_camera_deinit();
    Serial.println("NXRS_REFERENCE_PASS stopped=1");
    end_ms = 0;
    return;
  }
  WiFiClient client = server.available();
  if (!client) { delay(1); return; }
  client.setTimeout(2000);
  client.setNoDelay(true);
  String request = client.readStringUntil('\n');
  if (request.startsWith("GET /clock ")) {
    client.printf("HTTP/1.1 200 OK\r\nX-Clock-Ms: %lu\r\nContent-Length: 0\r\nConnection: close\r\n\r\n", millis());
  } else if (request.startsWith("GET /stream ")) {
    client.print("HTTP/1.1 200 OK\r\nContent-Type: multipart/x-mixed-replace; boundary=nxrs-frame\r\nConnection: close\r\n\r\n");
    while (client.connected() && millis() < end_ms) {
      camera_fb_t *frame = esp_camera_fb_get();
      if (!frame) break;
      const unsigned long observed = millis();
      char header[192];
      int length = snprintf(header, sizeof(header),
          "--nxrs-frame\r\nContent-Type: image/jpeg\r\nContent-Length: %u\r\nX-Sequence: %lu\r\nX-Capture-Ms: %lu\r\n\r\n",
          (unsigned)frame->len, (unsigned long)++sequence, observed);
      bool sent = length > 0 && length < (int)sizeof(header) &&
          write_all(client, reinterpret_cast<const uint8_t *>(header), length) &&
          write_all(client, frame->buf, frame->len) &&
          write_all(client, reinterpret_cast<const uint8_t *>("\r\n"), 2);
      esp_camera_fb_return(frame);
      if (!sent) break;
    }
  }
  client.stop();
}
