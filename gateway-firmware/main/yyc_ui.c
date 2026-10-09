/* Gateway-owned layout; original upstream Muse avatar renderer is unchanged.
 * This is not the full Muse settings/account UI. */
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <stdatomic.h>
#include "esp_heap_caps.h"
#include "esp_timer.h"
#include "mbedtls/base64.h"
#include "muse_board.h"
#include "yyc_ui.h"
#include "src/misc/cache/instance/lv_image_cache.h"

#define AVATAR_SIZE 128
static lv_obj_t *s_avatar, *s_caption, *s_status, *s_mode_label;
static lv_image_dsc_t s_image;
static lv_font_t s_font, s_cjk_font;
static atomic_int s_mode = MUSE_MODE_BOOT, s_level;
static atomic_uint s_frames;
static atomic_bool s_wifi, s_gateway;
LV_FONT_DECLARE(muse_font_cjk_16);
static const char *const MODES[MUSE_MODE_COUNT] = {
    "启动中", "待命", "聆听中", "思考中", "说话中", "连接异常", "休息中"
};

static void frame_tick(lv_timer_t *timer)
{
    (void)timer;
    static int last_mode = -1;
    static float started, level;
    static int64_t last_header;
    float now = (float)esp_timer_get_time() / 1e6f;
    int mode = atomic_load(&s_mode);
    if (mode != last_mode) {
        started = now; last_mode = mode;
        lv_label_set_text(s_mode_label, MODES[mode]);
        lv_obj_set_style_text_color(s_mode_label, lv_color_hex(muse_pixel_accent(mode)), 0);
    }
    float target = atomic_load(&s_level) / 1000.f;
    level += (target - level) * (target > level ? .6f : .2f);
    atomic_store(&s_level, (int)(target * 800));
    muse_pose_t pose = {.mode=mode, .t=now, .mode_t=now-started, .level=level};
    muse_pixel_render(&pose);
    muse_pixel_scale((uint16_t *)s_image.data, AVATAR_SIZE, 0, AVATAR_SIZE-1, 0, AVATAR_SIZE-1);
    lv_image_cache_drop(&s_image);
    lv_obj_invalidate(s_avatar);
    atomic_fetch_add(&s_frames, 1);
    if (esp_timer_get_time() - last_header > 1000000) {
        last_header = esp_timer_get_time();
        muse_power_t power={0}; muse_board->read_power(&power);
        lv_label_set_text_fmt(s_status, "%s %s %d%%", atomic_load(&s_wifi)?"Wi-Fi":"Offline",
            atomic_load(&s_gateway)?"AI":"--", power.battery_pct);
    }
}

static lv_obj_t *label(int x, int y, int w, int h)
{
    lv_obj_t *obj=lv_label_create(lv_screen_active());
    lv_obj_set_pos(obj,x,y); lv_obj_set_size(obj,w,h);
    lv_obj_set_style_text_font(obj,&s_font,0);
    lv_obj_set_style_text_color(obj,lv_color_hex(0xe8e8ef),0);
    return obj;
}

bool yyc_ui_start(void)
{
    uint16_t *pixels=heap_caps_calloc(AVATAR_SIZE*AVATAR_SIZE,2,MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT);
    if (!pixels || !muse_board->display_lock(-1)) { free(pixels); return false; }
    s_font=lv_font_montserrat_14; s_font.fallback=&muse_font_cjk_16;
    s_cjk_font=muse_font_cjk_16; s_cjk_font.fallback=&lv_font_montserrat_14;
    lv_obj_t *screen=lv_screen_active();
    lv_obj_set_style_bg_color(screen,lv_color_black(),0);
    lv_obj_remove_flag(screen,LV_OBJ_FLAG_SCROLLABLE);
    s_status=label(4,2,127,19); lv_label_set_text(s_status,"Starting...");
    s_image=(lv_image_dsc_t){.header={.magic=LV_IMAGE_HEADER_MAGIC,.cf=LV_COLOR_FORMAT_RGB565,
        .w=AVATAR_SIZE,.h=AVATAR_SIZE,.stride=AVATAR_SIZE*2},
        .data_size=AVATAR_SIZE*AVATAR_SIZE*2,.data=(const uint8_t *)pixels};
    s_avatar=lv_image_create(screen); lv_image_set_src(s_avatar,&s_image);
    lv_obj_set_pos(s_avatar,(muse_board->width-AVATAR_SIZE)/2,22);
    s_mode_label=label(4,151,127,20);
    lv_obj_set_style_text_font(s_mode_label,&s_cjk_font,0);
    lv_obj_set_style_text_align(s_mode_label,LV_TEXT_ALIGN_CENTER,0);
    s_caption=label(5,174,125,muse_board->height-177);
    lv_obj_set_style_text_font(s_caption,&s_cjk_font,0);
    lv_obj_set_style_text_line_space(s_caption,3,0);
    /* Three wrapped lines with ellipsis; never a single overflowing marquee.
     * The complete reply is still spoken by the audio pipeline. */
    lv_label_set_long_mode(s_caption,LV_LABEL_LONG_MODE_DOTS);
    lv_obj_set_size(s_caption,125,muse_board->height-177);
    lv_label_set_text(s_caption,"按住正面键\n说话后松开发送");
    muse_pixel_set_size(AVATAR_SIZE);
    frame_tick(NULL);
    lv_timer_create(frame_tick,muse_board->frame_ms,NULL);
    muse_board->display_unlock();
    return true;
}

void yyc_ui_caption(const char *text)
{
    if (s_caption && muse_board->display_lock(300)) {
        lv_label_set_text(s_caption,text); muse_board->display_unlock();
    }
}
void yyc_ui_mode(muse_mode_t mode) { if (mode>=0 && mode<MUSE_MODE_COUNT) atomic_store(&s_mode,mode); }
void yyc_ui_level(const int16_t *pcm, size_t frames)
{
    if (!frames) return;
    double sum=0;
    for (size_t i=0;i<frames;i++) sum+=(double)pcm[i]*pcm[i];
    int level=(int)(sqrt(sum/frames)/32768. * 6000);
    atomic_store(&s_level,level>1000?1000:level);
}
void yyc_ui_connection(bool wifi, bool gateway) { atomic_store(&s_wifi,wifi); atomic_store(&s_gateway,gateway); }
bool yyc_ui_ready(void) { return s_avatar != NULL; }
unsigned yyc_ui_frames(void) { return atomic_load(&s_frames); }
int yyc_ui_current_mode(void) { return atomic_load(&s_mode); }

void yyc_ui_snapshot(void)
{
#if LV_USE_SNAPSHOT
    if (!yyc_ui_ready() || !muse_board->display_lock(1000)) return;
    lv_draw_buf_t *buf=lv_snapshot_take(lv_screen_active(),LV_COLOR_FORMAT_RGB565);
    muse_board->display_unlock();
    if (!buf) { printf("@yyc {\"type\":\"snapshot\",\"error\":\"no memory\"}\n"); return; }
    printf("@yyc {\"type\":\"snapshot\",\"width\":%u,\"height\":%u}\n",buf->header.w,buf->header.h);
    for (uint32_t y=0;y<buf->header.h;y++) {
        unsigned char encoded[364]; size_t n;
        mbedtls_base64_encode(encoded,sizeof(encoded),&n,buf->data+y*buf->header.stride,buf->header.w*2);
        encoded[n]=0; printf("@snap %s\n",encoded);
    }
    printf("@yyc {\"type\":\"snapshot.end\"}\n"); fflush(stdout);
    lv_draw_buf_destroy(buf);
#else
    printf("@yyc {\"type\":\"snapshot\",\"error\":\"disabled\"}\n");
#endif
}
