#pragma once
#include "esp_err.h"
esp_err_t yyc_imu_init(void);
esp_err_t yyc_imu_read(float acc_g[3], float gyro_dps[3]);
