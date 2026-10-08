#include "imu.h"
#include "bmi270.h"
#include "driver/i2c_master.h"
#include "esp_rom_sys.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include <string.h>

static i2c_master_dev_handle_t s_i2c;
static struct bmi2_dev s_dev;
static bool s_ready;
static int8_t rd(uint8_t reg, uint8_t *buf, uint32_t n, void *p)
{
    (void)p;
    return i2c_master_transmit_receive(s_i2c, &reg, 1, buf, n, 200) == ESP_OK ? BMI2_OK : BMI2_E_COM_FAIL;
}
static int8_t wr(uint8_t reg, const uint8_t *buf, uint32_t n, void *p)
{
    (void)p;
    uint8_t data[65];
    if (n > 64) return BMI2_E_COM_FAIL;
    data[0] = reg; memcpy(data + 1, buf, n);
    return i2c_master_transmit(s_i2c, data, n + 1, 200) == ESP_OK ? BMI2_OK : BMI2_E_COM_FAIL;
}
static void delay_us(uint32_t us, void *p)
{
    (void)p;
    if (us >= 2000) vTaskDelay(pdMS_TO_TICKS((us + 999) / 1000));
    else esp_rom_delay_us(us);
}
esp_err_t yyc_imu_init(void)
{
    i2c_master_bus_handle_t bus;
    if (i2c_master_get_bus_handle(I2C_NUM_0, &bus) != ESP_OK) return ESP_FAIL;
    i2c_device_config_t cfg = {.dev_addr_length=I2C_ADDR_BIT_LEN_7, .device_address=0x68, .scl_speed_hz=100000};
    if (i2c_master_bus_add_device(bus, &cfg, &s_i2c) != ESP_OK) return ESP_FAIL;
    s_dev = (struct bmi2_dev){.intf=BMI2_I2C_INTF, .read=rd, .write=wr,
        .delay_us=delay_us, .read_write_len=32, .intf_ptr=&s_i2c};
    int8_t r = bmi270_init(&s_dev);
    if (r != BMI2_OK) { ESP_LOGE("yyc.imu", "BMI270 init rc=%d", r); return ESP_FAIL; }
    uint8_t sensors[] = {BMI2_ACCEL, BMI2_GYRO};
    r = bmi270_sensor_enable(sensors, 2, &s_dev);
    if (r != BMI2_OK) return ESP_FAIL;
    s_ready = true;
    ESP_LOGI("yyc.imu", "BMI270 chip=0x%02x initialized", s_dev.chip_id);
    return ESP_OK;
}
esp_err_t yyc_imu_read(float a[3], float g[3])
{
    if (!s_ready) return ESP_ERR_INVALID_STATE;
    struct bmi2_sens_data data;
    if (bmi2_get_sensor_data(&data, &s_dev) != BMI2_OK) return ESP_FAIL;
    struct bmi2_sens_config cfg[2] = {{.type=BMI2_ACCEL}, {.type=BMI2_GYRO}};
    if (bmi270_get_sensor_config(cfg, 2, &s_dev) != BMI2_OK) return ESP_FAIL;
    float acc_scale = (float)(2 << cfg[0].cfg.acc.range) / 32768.f;
    float gyro_scale = (float)(2000 >> cfg[1].cfg.gyr.range) / 32768.f;
    a[0]=data.acc.x*acc_scale; a[1]=data.acc.y*acc_scale; a[2]=data.acc.z*acc_scale;
    g[0]=data.gyr.x*gyro_scale; g[1]=data.gyr.y*gyro_scale; g[2]=data.gyr.z*gyro_scale;
    return ESP_OK;
}
