/* Standalone Gateway runtime. Muse networking/pairing/OTA is not compiled. */
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdatomic.h>
#include "cJSON.h"
#include "driver/usb_serial_jtag.h"
#include "esp_crt_bundle.h"
#include "esp_event.h"
#include "esp_heap_caps.h"
#include "esp_http_client.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_netif.h"
#include "esp_psram.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "esp_websocket_client.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "nvs.h"
#include "nvs_flash.h"
#include "imu.h"
#include "muse_audio.h"
#include "muse_board.h"

#define MAX_AUDIO_FRAMES (MUSE_AUDIO_RATE * 10)
#define MAX_REPLY 4096
static const char *TAG = "yyc";
static char s_ssid[33], s_password[65], s_uri[256], s_token[129], s_device[32];
static char s_headers[196];
static esp_websocket_client_handle_t s_ws;
static atomic_bool s_wifi, s_connected, s_record, s_playing, s_audio_ok;
static atomic_bool s_test_requested;
static lv_obj_t *s_label;
static lv_font_t s_font;
LV_FONT_DECLARE(muse_font_cjk_16);
static bool s_imu_ok;
typedef struct { int16_t *pcm; size_t frames; char *text; } upload_t;
typedef struct { int16_t *pcm; size_t frames; } playback_t;
static QueueHandle_t s_uploads, s_playback;

static void ui(const char *text)
{
    if (s_label && muse_board->display_lock(300)) {
        lv_label_set_text(s_label, text);
        muse_board->display_unlock();
    }
}
static void usb_json(cJSON *j)
{
    char *s = cJSON_PrintUnformatted(j);
    if (s) { printf("@yyc %s\n", s); fflush(stdout); free(s); }
    cJSON_Delete(j);
}
static void status(bool send_ws)
{
    cJSON *j = cJSON_CreateObject();
    cJSON_AddStringToObject(j, "type", "telemetry");
    cJSON_AddStringToObject(j, "device", s_device);
    cJSON_AddStringToObject(j, "board", muse_board->name);
    cJSON_AddBoolToObject(j, "wifi", atomic_load(&s_wifi));
    cJSON_AddBoolToObject(j, "gateway", atomic_load(&s_connected));
    cJSON_AddBoolToObject(j, "wifi_configured", s_ssid[0] != 0);
    cJSON_AddBoolToObject(j, "gateway_configured", s_uri[0] != 0);
    cJSON_AddBoolToObject(j, "display_initialized", s_label != NULL);
    cJSON_AddBoolToObject(j, "audio", atomic_load(&s_audio_ok));
    cJSON_AddBoolToObject(j, "imu", s_imu_ok);
    cJSON_AddNumberToObject(j, "psram_bytes", esp_psram_get_size());
    muse_power_t power = {0};
    if (muse_board->read_power(&power) == ESP_OK) {
        cJSON_AddNumberToObject(j, "battery_mv", power.battery_mv);
        cJSON_AddNumberToObject(j, "battery_pct", power.battery_pct);
        cJSON_AddBoolToObject(j, "usb_power", power.usb);
    }
    float a[3], g[3];
    if (yyc_imu_read(a, g) == ESP_OK) {
        cJSON_AddItemToObject(j, "accel_g", cJSON_CreateFloatArray(a, 3));
        cJSON_AddItemToObject(j, "gyro_dps", cJSON_CreateFloatArray(g, 3));
    }
    if (send_ws && s_ws && atomic_load(&s_connected)) {
        char *s = cJSON_PrintUnformatted(j);
        if (s) { esp_websocket_client_send_text(s_ws, s, strlen(s), pdMS_TO_TICKS(1000)); free(s); }
    }
    usb_json(j);
}
static void hardware_test(void)
{
    ESP_LOGI(TAG, "hardware test START; display/button tests still need human observation");
    uint32_t *mem = heap_caps_malloc(128 * 1024, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    bool memory_ok = mem != NULL;
    if (mem) {
        for (size_t i=0; i<32768; i++) mem[i] = 0xA5C30000u ^ i;
        for (size_t i=0; i<32768; i++) if (mem[i] != (0xA5C30000u ^ i)) memory_ok = false;
        free(mem);
    }
    ESP_LOGI(TAG, "PSRAM pattern test: %s", memory_ok ? "PASS" : "FAIL");
    if (atomic_load(&s_audio_ok)) { muse_audio_selftest(); muse_audio_chirp(1); }
    status(false);
    ui("YYC StickS3\nHardware ready\nFront: hold to talk\nSide: status/test\nUSB setup required");
    ESP_LOGI(TAG, "hardware test END; no claim of physical button/visual verification");
}
static bool copy_json(cJSON *j, const char *key, char *dst, size_t cap)
{
    cJSON *v=cJSON_GetObjectItemCaseSensitive(j,key);
    if (!cJSON_IsString(v) || strlen(v->valuestring) >= cap) return false;
    strcpy(dst,v->valuestring); return true;
}
static bool uri_valid(const char *uri)
{
    return (!strncmp(uri,"ws://",5) && strlen(uri)>5) ||
        (!strncmp(uri,"wss://",6) && strlen(uri)>6) ||
        (!strncmp(uri,"http://",7) && strlen(uri)>7) ||
        (!strncmp(uri,"https://",8) && strlen(uri)>8);
}
static void configure(cJSON *j)
{
    char ssid[33],pass[65],uri[256],token[129];
    bool ok=copy_json(j,"ssid",ssid,sizeof(ssid)) && ssid[0] &&
        copy_json(j,"password",pass,sizeof(pass)) &&
        copy_json(j,"gateway",uri,sizeof(uri)) && uri_valid(uri) &&
        copy_json(j,"token",token,sizeof(token)) && strlen(token)>=16 &&
        !strchr(token,'\r') && !strchr(token,'\n');
    nvs_handle_t h;
    if (ok && nvs_open("yyc_gateway",NVS_READWRITE,&h)==ESP_OK) {
        ok=nvs_set_str(h,"ssid",ssid)==ESP_OK && nvs_set_str(h,"password",pass)==ESP_OK &&
            nvs_set_str(h,"gateway",uri)==ESP_OK && nvs_set_str(h,"token",token)==ESP_OK && nvs_commit(h)==ESP_OK;
        nvs_close(h);
    } else ok=false;
    memset(pass,0,sizeof(pass)); memset(token,0,sizeof(token));
    printf("@yyc {\"type\":\"configured\",\"ok\":%s}\n",ok?"true":"false"); fflush(stdout);
    if (ok) { ui("Saved. Rebooting..."); vTaskDelay(pdMS_TO_TICKS(600)); esp_restart(); }
}
static void serial_task(void *p)
{
    (void)p;
    usb_serial_jtag_driver_config_t cfg=USB_SERIAL_JTAG_DRIVER_CONFIG_DEFAULT();
    cfg.rx_buffer_size=4096; cfg.tx_buffer_size=4096;
    if (usb_serial_jtag_driver_install(&cfg)!=ESP_OK) { vTaskDelete(NULL); return; }
    char line[2048]; size_t n=0; bool overflow=false;
    for (;;) {
        uint8_t c;
        if (usb_serial_jtag_read_bytes(&c,1,pdMS_TO_TICKS(100))!=1) continue;
        if (c=='\r') continue;
        if (c!='\n') { if (n+1<sizeof(line)) line[n++]=c; else overflow=true; continue; }
        line[n]=0;
        cJSON *j=overflow?NULL:cJSON_Parse(line);
        if (j) {
            cJSON *cmd=cJSON_GetObjectItemCaseSensitive(j,"cmd");
            if (cJSON_IsString(cmd)) {
                if (!strcmp(cmd->valuestring,"configure")) configure(j);
                else if (!strcmp(cmd->valuestring,"status")) status(false);
                else if (!strcmp(cmd->valuestring,"test")) atomic_store(&s_test_requested,true);
                else if (!strcmp(cmd->valuestring,"say")) {
                    cJSON *v=cJSON_GetObjectItemCaseSensitive(j,"text");
                    if (cJSON_IsString(v) && strlen(v->valuestring)>0 && strlen(v->valuestring)<=2048) {
                        upload_t job={.text=strdup(v->valuestring)};
                        if (job.text && xQueueSend(s_uploads,&job,0)!=pdTRUE) free(job.text);
                    }
                } else if (!strcmp(cmd->valuestring,"record_test") && atomic_load(&s_audio_ok) && !atomic_load(&s_playing)) {
                    atomic_store(&s_record,true); vTaskDelay(pdMS_TO_TICKS(1000)); atomic_store(&s_record,false);
                }
            }
            cJSON_Delete(j);
        } else printf("@yyc {\"type\":\"error\",\"message\":\"invalid command\"}\n");
        memset(line,0,sizeof(line)); n=0; overflow=false;
    }
}
static void wifi_event(void *p,esp_event_base_t base,int32_t id,void *data)
{
    (void)p;
    if (base==IP_EVENT && id==IP_EVENT_STA_GOT_IP) {
        atomic_store(&s_wifi,true);
        ip_event_got_ip_t *e=data;
        ESP_LOGI(TAG,"Wi-Fi IP: " IPSTR,IP2STR(&e->ip_info.ip));
        ui("Wi-Fi connected\nGateway connecting...");
    } else if (base==WIFI_EVENT && id==WIFI_EVENT_STA_DISCONNECTED) {
        atomic_store(&s_wifi,false); atomic_store(&s_connected,false);
        ui("Wi-Fi disconnected\nRetrying...");
    }
}
static void ws_message(const char *data)
{
    cJSON *j=cJSON_Parse(data);
    if (!j) return;
    cJSON *type=cJSON_GetObjectItemCaseSensitive(j,"type");
    if (cJSON_IsString(type)) {
        if (!strcmp(type->valuestring,"reply") || !strcmp(type->valuestring,"error")) {
            cJSON *t=cJSON_GetObjectItemCaseSensitive(j,"text");
            if (!cJSON_IsString(t)) t=cJSON_GetObjectItemCaseSensitive(j,"message");
            if (cJSON_IsString(t)) { ui(t->valuestring); ESP_LOGI(TAG,"Gateway %s received",type->valuestring); }
        } else if (!strcmp(type->valuestring,"ready")) ui("Gateway ready\nHold front to talk");
        else if (!strcmp(type->valuestring,"audio.start")) {
            cJSON *rate=cJSON_GetObjectItemCaseSensitive(j,"sample_rate");
            cJSON *channels=cJSON_GetObjectItemCaseSensitive(j,"channels");
            if (cJSON_IsNumber(rate) && rate->valueint==16000 && cJSON_IsNumber(channels) && channels->valueint==1 && !atomic_load(&s_record)) atomic_store(&s_playing,true);
        } else if (!strcmp(type->valuestring,"audio.end")) {
            playback_t end={0};
            if (xQueueSend(s_playback,&end,0)!=pdTRUE) atomic_store(&s_playing,false);
        }
    }
    cJSON_Delete(j);
}
static void ws_event(void *p,esp_event_base_t base,int32_t id,void *event)
{
    (void)p; (void)base;
    static char text[MAX_REPLY+1]; static size_t n;
    esp_websocket_event_data_t *e=event;
    if (id==WEBSOCKET_EVENT_CONNECTED) { atomic_store(&s_connected,true); ESP_LOGI(TAG,"Gateway WebSocket connected"); }
    else if (id==WEBSOCKET_EVENT_DISCONNECTED || id==WEBSOCKET_EVENT_ERROR) {
        atomic_store(&s_connected,false); atomic_store(&s_playing,false); n=0;
        ui("Gateway disconnected\nRetrying...");
    } else if (id==WEBSOCKET_EVENT_DATA && e->data_len>0) {
        if (e->op_code==0x1) {
            if (e->payload_offset==0) n=0;
            if ((size_t)e->payload_len>MAX_REPLY || n+(size_t)e->data_len>MAX_REPLY) { n=0; return; }
            memcpy(text+n,e->data_ptr,e->data_len); n+=e->data_len;
            if (e->payload_offset+e->data_len==e->payload_len) { text[n]=0; ws_message(text); n=0; }
        } else if (e->op_code==0x2 && atomic_load(&s_playing) && atomic_load(&s_audio_ok) && !(e->data_len&1)) {
            playback_t job={.frames=(size_t)e->data_len/2};
            job.pcm=malloc(e->data_len);
            if (job.pcm) {
                memcpy(job.pcm,e->data_ptr,e->data_len);
                if (xQueueSend(s_playback,&job,0)!=pdTRUE) { free(job.pcm); atomic_store(&s_playing,false); ESP_LOGE(TAG,"playback queue full"); }
            }
        }
    }
}
static void ws_send_json(cJSON *j)
{
    char *s=cJSON_PrintUnformatted(j);
    if (s && s_ws && atomic_load(&s_connected)) esp_websocket_client_send_text(s_ws,s,strlen(s),pdMS_TO_TICKS(5000));
    free(s); cJSON_Delete(j);
}
static void http_text(const char *text)
{
    cJSON *j=cJSON_CreateObject();
    cJSON_AddStringToObject(j,"text",text); cJSON_AddStringToObject(j,"device",s_device);
    char *body=cJSON_PrintUnformatted(j); cJSON_Delete(j);
    if (!body) return;
    esp_http_client_config_t cfg={.url=s_uri,.timeout_ms=60000,.crt_bundle_attach=esp_crt_bundle_attach};
    esp_http_client_handle_t h=esp_http_client_init(&cfg);
    if (!h) { free(body); return; }
    char auth[144]; snprintf(auth,sizeof(auth),"Bearer %s",s_token);
    esp_http_client_set_method(h,HTTP_METHOD_POST);
    esp_http_client_set_header(h,"Content-Type","application/json");
    esp_http_client_set_header(h,"Authorization",auth);
    if (esp_http_client_open(h,strlen(body))==ESP_OK) {
        size_t sent=0;
        while (sent<strlen(body)) {
            int w=esp_http_client_write(h,body+sent,strlen(body)-sent);
            if (w<=0) break;
            sent+=w;
        }
        if (sent==strlen(body) && esp_http_client_fetch_headers(h)>=0) {
            char result[MAX_REPLY+1]; size_t used=0;
            while (used<MAX_REPLY) {
                int r=esp_http_client_read(h,result+used,MAX_REPLY-used);
                if (r<=0) break;
                used+=r;
            }
            result[used]=0;
            if (esp_http_client_get_status_code(h)==200) { atomic_store(&s_connected,true); ws_message(result); }
            else ui("Gateway HTTP error");
        }
    } else ui("Gateway HTTP unavailable");
    esp_http_client_cleanup(h); free(body);
}
static void network_task(void *p)
{
    (void)p;
    int retry=0; int64_t next=0,last_status=0;
    for (;;) {
        int64_t now=esp_timer_get_time();
        if (s_ssid[0] && !atomic_load(&s_wifi) && now>=next) {
            esp_wifi_connect(); retry=retry<5?retry+1:5; next=now+(1<<retry)*1000000LL;
        }
        if (atomic_load(&s_wifi)) {
            retry=0;
            if (!strncmp(s_uri,"ws",2) && !s_ws) {
                snprintf(s_headers,sizeof(s_headers),"Authorization: Bearer %s\r\n",s_token);
                esp_websocket_client_config_t cfg={.uri=s_uri,.headers=s_headers,
                    .crt_bundle_attach=esp_crt_bundle_attach,.buffer_size=2048,
                    .task_stack=6144,.reconnect_timeout_ms=5000,.network_timeout_ms=10000};
                s_ws=esp_websocket_client_init(&cfg);
                if (s_ws) { esp_websocket_register_events(s_ws,WEBSOCKET_EVENT_ANY,ws_event,NULL); esp_websocket_client_start(s_ws); }
            }
            if (s_ws && atomic_load(&s_connected) && now-last_status>15000000LL) { status(true); last_status=now; }
        }
        upload_t job;
        if (xQueueReceive(s_uploads,&job,pdMS_TO_TICKS(50))==pdTRUE) {
            if (!atomic_load(&s_wifi)) ui("No Wi-Fi. USB setup required");
            else if (job.text && !strncmp(s_uri,"http",4)) http_text(job.text);
            else if (!atomic_load(&s_connected)) ui("Gateway not connected");
            else if (job.text) {
                cJSON *j=cJSON_CreateObject(); cJSON_AddStringToObject(j,"type","text");
                cJSON_AddStringToObject(j,"text",job.text); ws_send_json(j); ui("Thinking...");
            } else if (job.pcm && job.frames) {
                cJSON *j=cJSON_CreateObject(); cJSON_AddStringToObject(j,"type","audio.start");
                cJSON_AddNumberToObject(j,"sample_rate",16000); cJSON_AddNumberToObject(j,"channels",1);
                ws_send_json(j);
                bool ok=true;
                for (size_t off=0; off<job.frames; off+=640) {
                    size_t count=job.frames-off<640?job.frames-off:640;
                    if (esp_websocket_client_send_bin(s_ws,(char*)(job.pcm+off),count*2,pdMS_TO_TICKS(5000))!=(int)count*2) { ok=false; break; }
                }
                if (ok) { j=cJSON_CreateObject(); cJSON_AddStringToObject(j,"type","audio.end"); ws_send_json(j); ui("Thinking..."); }
                else ui("Audio upload failed");
            }
            free(job.pcm); free(job.text);
        }
    }
}
static void audio_task(void *p)
{
    (void)p;
    int16_t chunk[MUSE_AUDIO_CHUNK];
    for (;;) {
        if (atomic_exchange(&s_test_requested,false)) hardware_test();
        playback_t play;
        if (xQueueReceive(s_playback,&play,pdMS_TO_TICKS(10))==pdTRUE) {
            if (play.pcm && atomic_load(&s_audio_ok) && !atomic_load(&s_record)) muse_audio_write(play.pcm,play.frames);
            else if (!play.pcm) atomic_store(&s_playing,false);
            free(play.pcm); continue;
        }
        if (!atomic_load(&s_record) || !atomic_load(&s_audio_ok) || atomic_load(&s_playing)) continue;
        int16_t *pcm=heap_caps_malloc(MAX_AUDIO_FRAMES*2,MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT);
        size_t frames=0;
        if (!pcm) { atomic_store(&s_record,false); ui("Audio memory unavailable"); continue; }
        ui("Listening...\nRelease front to send");
        while (atomic_load(&s_record) && frames+MUSE_AUDIO_CHUNK<=MAX_AUDIO_FRAMES) {
            if (muse_audio_read(chunk,MUSE_AUDIO_CHUNK)!=ESP_OK) break;
            memcpy(pcm+frames,chunk,sizeof(chunk)); frames+=MUSE_AUDIO_CHUNK;
        }
        atomic_store(&s_record,false);
        if (frames < 1600) { free(pcm); ui("Recording too short"); continue; }
        upload_t job={.pcm=pcm,.frames=frames};
        if (xQueueSend(s_uploads,&job,0)!=pdTRUE) { free(pcm); ui("Gateway busy"); }
        ESP_LOGI(TAG,"recorded %u mono frames; no raw audio written to disk",(unsigned)frames);
    }
}
void app_main(void)
{
    ESP_LOGI(TAG,"YYC Gateway firmware starting (no Muse backend)");
    esp_err_t e=nvs_flash_init();
    if (e!=ESP_OK) { ESP_LOGE(TAG,"NVS init %s; NOT erasing existing NVS",esp_err_to_name(e)); return; }
    nvs_handle_t h;
    if (nvs_open("yyc_gateway",NVS_READONLY,&h)==ESP_OK) {
        size_t n=sizeof(s_ssid); nvs_get_str(h,"ssid",s_ssid,&n);
        n=sizeof(s_password); nvs_get_str(h,"password",s_password,&n);
        n=sizeof(s_uri); nvs_get_str(h,"gateway",s_uri,&n);
        n=sizeof(s_token); nvs_get_str(h,"token",s_token,&n); nvs_close(h);
    }
    uint8_t mac[6]; esp_read_mac(mac,ESP_MAC_WIFI_STA);
    snprintf(s_device,sizeof(s_device),"StickS3-%02X%02X%02X",mac[3],mac[4],mac[5]);
    muse_board=muse_board_get();
    ESP_ERROR_CHECK(muse_board->init());
    lv_indev_t *touch=NULL;
    if (muse_board->display_start(&touch) && muse_board->display_lock(-1)) {
        s_font=lv_font_montserrat_14; s_font.fallback=&muse_font_cjk_16;
        s_label=lv_label_create(lv_screen_active()); lv_obj_set_size(s_label,125,220);
        lv_label_set_long_mode(s_label,LV_LABEL_LONG_SCROLL);
        lv_obj_set_style_text_font(s_label,&s_font,0);
        lv_obj_align(s_label,LV_ALIGN_TOP_LEFT,5,10); lv_label_set_text(s_label,"YYC StickS3\nHardware testing...");
        muse_board->display_unlock(); muse_board->set_brightness(60);
    }
    atomic_store(&s_audio_ok,muse_audio_init(30,24)==ESP_OK);
    s_imu_ok=yyc_imu_init()==ESP_OK;
    s_uploads=xQueueCreate(2,sizeof(upload_t)); s_playback=xQueueCreate(32,sizeof(playback_t));
    if (!s_uploads || !s_playback) { ESP_LOGE(TAG,"queue allocation failed"); return; }
    hardware_test();
    ESP_ERROR_CHECK(esp_netif_init()); ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();
    wifi_init_config_t w=WIFI_INIT_CONFIG_DEFAULT(); ESP_ERROR_CHECK(esp_wifi_init(&w));
    esp_event_handler_register(WIFI_EVENT,ESP_EVENT_ANY_ID,wifi_event,NULL);
    esp_event_handler_register(IP_EVENT,IP_EVENT_STA_GOT_IP,wifi_event,NULL);
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM)); ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    if (s_ssid[0]) {
        wifi_config_t cfg={0}; strcpy((char*)cfg.sta.ssid,s_ssid); strcpy((char*)cfg.sta.password,s_password);
        ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA,&cfg));
    }
    ESP_ERROR_CHECK(esp_wifi_start());
    if (!s_ssid[0]) {
        wifi_scan_config_t scan={0};
        if (esp_wifi_scan_start(&scan,true)==ESP_OK) {
            uint16_t found=0; esp_wifi_scan_get_ap_num(&found);
            ESP_LOGI(TAG,"Wi-Fi hardware scan: %u APs (SSID names not logged)",found);
            esp_wifi_clear_ap_list();
        }
    }
    xTaskCreate(serial_task,"yyc_usb",6144,NULL,5,NULL);
    xTaskCreate(network_task,"yyc_network",8192,NULL,4,NULL);
    xTaskCreatePinnedToCore(audio_task,"yyc_audio",8192,NULL,6,NULL,1);
    ESP_LOGI(TAG,"READY device=%s; credentials stay on device, provider keys stay on Gateway",s_device);
    for (;;) {
        unsigned buttons=muse_board->poll_buttons();
        if (buttons) ESP_LOGI(TAG,"button edges=0x%x (physical)",buttons);
        if ((buttons&MUSE_BTN_TALK_PRESS) && !atomic_load(&s_playing)) atomic_store(&s_record,true);
        if (buttons&MUSE_BTN_TALK_RELEASE) atomic_store(&s_record,false);
        if (buttons&MUSE_BTN_AUX_PRESS) status(false);
        vTaskDelay(pdMS_TO_TICKS(10));
    }
}
