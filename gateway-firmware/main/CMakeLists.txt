idf_component_register(SRCS "gateway_main.c" INCLUDE_DIRS "."
    REQUIRES yyc_hardware nvs_flash esp_wifi esp_event esp_netif esp_http_client
             esp_driver_usb_serial_jtag esp-tls esp_timer esp_system)
