#pragma once
#include "muse_pixel.h"

bool yyc_ui_start(void);
void yyc_ui_caption(const char *text);
void yyc_ui_mode(muse_mode_t mode);
void yyc_ui_level(const int16_t *pcm, size_t frames);
void yyc_ui_connection(bool wifi, bool gateway);
bool yyc_ui_ready(void);
unsigned yyc_ui_frames(void);
int yyc_ui_current_mode(void);
/* USB diagnostic only: capture the actual LVGL screen, not a mockup. */
void yyc_ui_snapshot(void);
