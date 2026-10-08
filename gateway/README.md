# StickS3 自有 Gateway

基于 Muse Gadget SDK 的独立硬件分支。上游基线：
`facebookincubator/muse-gadget-sdk@86cf33fb4092ba700b4dc33928966d1bcb31556d`。

## 保留和替换的边界

- 原封不动复用 Muse StickS3 的 LCD、背光、ES8311 音频、M5PM1 电源/电池、两按键驱动。
- 使用 Bosch 官方 BMI270 SensorAPI 增加 IMU 初始化、加速度/角速度读取；其 BSD 许可证和上游提交号保留在 `gateway-firmware/components/yyc_hardware/bosch/`。
- 只编译硬件文件、字体和少量硬件状态钩子。**不编译 Muse app、Muse 账户、Muse BLE 配对、Noise/VM 客户端和 Muse OTA**。
- 自有固件在 `gateway-firmware/`，不改变 `esp32/` 的官方固件代码。不会烧写 eFuse、启用 Secure Boot 或 Flash Encryption。
- UI 是独立的状态/中文文本 UI，不是完整移植 Muse 的头像与设置菜单。
- 当前 HTTP 接口提供文本对话；完整语音收发使用 WebSocket。USB 配网不需要 Muse Token。

## 已实现的设备行为

开机测试 PSRAM 的 128 KiB 样本、音频采集/播放速率、PMIC 电池、BMI270 和 Wi-Fi 扫描。
检测到 8 MiB PSRAM 不代表已逐字节测试全部 8 MiB。屏幕、按键的物理表现仍需人确认。

- 正面键按住录音、松开发送；最大 10 秒，16 kHz / mono / PCM16LE。
- 侧键输出设备状态。
- 收到文本回复显示在屏幕；收到合规 PCM 回复通过扬声器播放。
- 默认半双工交互，不在听取用户录音时播放回复。
- Wi-Fi/Gateway 断线重试。未配置模型时服务明确报错，不伪造回复。
- 不把语音、聊天内容、Wi-Fi 密码、Gateway Token 写入日志或文件；配置只保存在设备 NVS。

## Gateway 启动

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
chmod 600 .env
# 编辑 .env：设置强 GATEWAY_TOKEN、模型端点/名称/密钥。
# 连设备时，将 HOST 设置为 Mac 的局域网 IP，而不是 127.0.0.1。
.venv/bin/python server.py
```

模型密钥**只在 Gateway**，不放在设备上：

| 后端 | MODEL_PROVIDER | MODEL_BASE_URL 格式 |
|---|---|---|
| Qwen 的兼容服务 | `openai-compatible` | 服务的 OpenAI 兼容 `/v1` 基地址 |
| GPT | `openai-compatible` | OpenAI `/v1` 基地址 |
| Gemini 兼容服务 | `openai-compatible` | 服务的 OpenAI 兼容基地址 |
| 本地 vLLM | `openai-compatible` | 本地服务 `/v1` 基地址，可没有 API Key |
| Claude 原生 API | `anthropic` | Anthropic `/v1` 基地址 |

必须自己指定 `MODEL_NAME`，没有默认模型，也不会自动选 Astra。
`MODEL_TOKEN_FIELD` 默认 `max_tokens`；只接受 `max_tokens` 或 `max_completion_tokens`，按目标服务设置。
不同模型的参数兼容性要以真实端点测试为准，适配器测试不能证明每个模型账号可用。

语音需要独立配置 `ASR_*` 与 `TTS_*`：当前适配 OpenAI-compatible transcription 和 speech 接口。
TTS 服务必须返回 PCM16LE 单声道，配置实际采样率（默认 24 kHz），Gateway 转为设备的 16 kHz。
某个模型可接文本并不代表它自带 ASR/TTS。没有 ASR 时不会把麦克风数据发给云端。

## USB 配网

```sh
.venv/bin/python provision.py --port /dev/cu.usbmodem101
```

按提示填写 2.4 GHz Wi-Fi SSID、密码和 Gateway URI，例如
`ws://MAC_LAN_IP:8787/ws`，或文本专用 `http://MAC_LAN_IP:8787/v1/chat`。
工具从同目录 `.env` 读取 Gateway Token；密码不回显，不作为命令行参数。
保存成功设备重启。检查：

```sh
.venv/bin/python provision.py --status
```

USB JSON 命令（每行一个 JSON，不需要 `>` 前缀）：

```json
{"cmd":"status"}
{"cmd":"test"}
{"cmd":"say","text":"你好"}
{"cmd":"record_test"}
```

`record_test` 录制约一秒并尝试发送，仅用于显式测试。

## 协议和安全边界

- 所有 HTTP 和 WS 入口都需要 `Authorization: Bearer <GATEWAY_TOKEN>`。
- WS 文本消息：`text`、`telemetry`、`audio.start`、`audio.end`。音频是中间的二进制 PCM 消息。
- Gateway 回复：`ready`、`reply`、`error`，或 `audio.start` + PCM 二进制 + `audio.end`。
- 无鉴权为 401；未配置真实模型为 503。
- `ws://` / `http://` 不加密，**只用于可信局域网开发**；公网必须使用 `wss://` / `https://`。固件使用系统 CA bundle 验证 TLS，不跳过验证。
- 当前设备 NVS 为明文；物理访问者能读取 Wi-Fi/Gateway 凭据。为避免不可逆 eFuse 操作，本轮没有打开 NVS/Flash Encryption。
- 没有远程刷机、自动升级或执行模型返回命令的能力。

## 编译

安装并激活 ESP-IDF **v6.0.1**。源码使用 SDK 的固定依赖版本。

```sh
cd ../gateway-firmware
idf.py -B build -DIDF_TARGET=esp32s3 build
```

Flash 布局沿用 Muse 的 StickS3 8 MiB 分区（partition table `0x10000`，ota_0 `0x20000`），
自己的 NVS namespace 为 `yyc_gateway`，不主动擦除旧 NVS。
USB `303a:1001` 不能独立证明板型，写入前应核验芯片和物理板型。

不要用通用 16 MiB 板型覆盖此设备。写入后用串口验证 `YYC Gateway firmware starting`、
`BMI270 ... initialized`、`READY`，并检查是否有 panic 或重启循环。
仅编译成功不能证明已刷入，更不能证明 Wi-Fi、模型或完整语音链路已接通。

## 本机测试

```sh
.venv/bin/python -m unittest discover -s tests -v
```

这些测试覆盖鉴权、HTTP/WS 协议、音频限制、忙状态、两种模型适配器和语音格式。
模型适配器使用模拟 HTTP 响应，不消耗云服务额度，也不是五家服务的真实账号验收。
