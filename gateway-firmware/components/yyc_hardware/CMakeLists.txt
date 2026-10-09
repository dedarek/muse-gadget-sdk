# Reuse the upstream Muse hardware sources without copying or changing them.
# NO muse_app / muse_link / Muse BLE / Noise / Muse account client is linked.
set(MUSE "${CMAKE_CURRENT_LIST_DIR}/../../../esp32/components/muse")
idf_component_register(
    SRCS "${MUSE}/boards/board_m5stack_sticks3.c"
         "${MUSE}/muse_board.c" "${MUSE}/muse_audio.c"
         "${MUSE}/muse_lv_mem.c" "${MUSE}/fonts/muse_font_cjk_16.c"
         "${MUSE}/../../avatar/muse_pixel.c"
         "hardware_hooks.c" "imu.c" "bosch/bmi2.c" "bosch/bmi270.c"
    INCLUDE_DIRS "." "bosch" "${MUSE}"
    WHOLE_ARCHIVE # LVGL references our allocator; same requirement as upstream.
    REQUIRES esp_driver_gpio esp_driver_i2c esp_driver_i2s esp_driver_ledc
             esp_driver_spi esp_lcd esp_timer esp_psram)
