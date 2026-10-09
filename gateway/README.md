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
- 设备不持久保存录音或聊天；配置保存在设备 NVS。Gateway 不持久保存语音/聊天，不记录密码或 Token。

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
`MODEL_MAX_TOKENS`、`MODEL_TEMPERATURE`、`MODEL_TOP_P` 可按服务配置。
本轮 Lenovo Qwen 语音模式使用 `MODEL_ENABLE_THINKING=false`，通过
`chat_template_kwargs.enable_thinking` 控制模板；实测顶层 `enable_thinking` 没有关闭其思考。
关闭思考能避免短语音回复的 token 预算耗在思考上而没有可播报的文本。
不同模型的参数兼容性要以真实端点测试为准，适配器测试不能证明每个模型账号可用。

语音需要独立配置 `ASR_*` 与 `TTS_*`：当前适配 OpenAI-compatible transcription 和 speech 接口。
TTS 服务必须返回 PCM16LE 单声道，配置实际采样率（默认 24 kHz），Gateway 转为设备的 16 kHz。
某个模型可接文本并不代表它自带 ASR/TTS。没有 ASR 时不会把麦克风数据发给云端。

### Mac 本地语音模式（无需第二枚云端 Key）

```text
StickS3 PCM录音 → Mac Whisper.cpp 转文字 → Qwen文本接口
                                            ↓
StickS3扬声器 ← 16kHz mono PCM ← Mac say 中文语音 ← 文本回复
```

本机存在 `whisper-cli` 和模型时可直接复用；不会修改已有模型。
在私有 `.env` 设置：

```dotenv
ASR_PROVIDER=whisper-cpp
WHISPER_MODEL_PATH=/absolute/path/to/ggml-small.bin
WHISPER_CLI=whisper-cli
ASR_LANGUAGE=zh
TTS_PROVIDER=macos-say
MACOS_SAY_VOICE=Tingting
```

Mac 还需要 `ffmpeg`。Whisper 每次以受控子进程识别最多 10 秒音频，使用两个 CPU 线程，
本地语音任务串行执行；不另外开放识别服务端口。`say` 合成后转换为 16 kHz、单声道 PCM16LE，
设备沿用现有二进制音频协议，不需要因换 ASR/TTS 再刷固件。单次语音回复最多 30 秒。

识别和合成阶段使用权限受限的临时 WAV/AIFF/JSON/PCM 文件，成功或失败后删除；不持久保存。
可用 `SPEECH_TMP_DIR` 指定自己的私有工作目录。
此模式只把**识别后的文字**发送到模型接口，原始用户录音不发往云端 ASR。
第一版需要 Mac 开着且设备能访问 Mac 的局域网地址；以后迁移服务器时可换成独立 ASR/TTS 服务。

健康检查会分别报告模型、ASR、TTS 配置状态，但“已配置”不等于设备完整链路已验收。

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

### 需要网页认证的开放 Wi-Fi

先用 USB 诊断网络，再完成该终端的认证，不要把 Mac 自己已登录当作设备已放行：

```json
{"cmd":"wifi.scan","ssid":"YOUR_OPEN_SSID"}
{"cmd":"wifi.configure","ssid":"YOUR_OPEN_SSID","password":""}
{"cmd":"status"}
{"cmd":"netcheck"}
```

`wifi.scan` 返回指定 SSID 的 2.4 GHz AP、频道、信号和认证类型。
`wifi.configure` 只更新 Wi-Fi，保留已有 Gateway 设置；本轮设备的 Gateway 尚未配置，
因此认证阶段不传输 Gateway Token。该命令不是“暂停已配置的 Gateway”。

`netcheck` 从 **StickS3 自己的网络连接**发出不含认证头的 HTTP 请求，禁止自动跟随跳转，
通过 USB 返回状态码、Location 和少量网页预览。可用 `url` 指定下一跳的 HTTP(S) URL。
对于 HTML meta-refresh，需要读取其真实地址并继续由设备探测，不能猜一个普通 Mac 登录链接。

若门户支持按终端 MAC/IP 委托认证，在 Mac 浏览器打开设备收到的专属链接，
核验表单目标确实为该设备，然后由用户输入自己的获授权账号。代码不自动获取、复制或绕过公司账号认证。
完成后再从设备重复联网检查；打开页面、登录 Mac、拿到 DHCP 地址均不单独证明设备已放行。
某些网络按来源 IP 严格绑定，或禁止跨终端认证，需要使用公司提供的设备登记流程或联系 IT。

开放访客网络上不要发送明文 Gateway 凭据或语音；后续 Gateway 接入应使用可验证证书的 TLS。
网页认证通过也不保证访客 VLAN 能访问 Mac 的 Gateway，还需单独检查局域网连通性。

### WPA2-Enterprise / PEAP 企业网络

新增的企业认证只在 RAM 中配置，不写企业密码到 NVS/Flash，也不会烧写 eFuse。
使用公司提供或当前设备明确受信任的公共 CA 和认证服务器域名，始终验证服务器证书及有效期。
不能用网页认证替代 802.1X，也不能为了连接成功而关闭证书校验。

```sh
.venv/bin/python enterprise_provision.py --port /dev/cu.usbmodem11201 \
  --ssid YOUR_ENTERPRISE_SSID --server-name radius.example.com --ca-cert /path/to/company-ca.pem
```

账号和密码通过本机隐藏输入获取，不作为命令行参数，不写日志。
当前实现支持 PEAP（账号密码）；如公司要求设备客户端证书，应由 IT 签发，不要复制 Mac 的私钥。
每次启动只允许一个明确的企业凭据配置；认证失败不自动重复尝试，以免锁定账户。
断电或重启后这些临时凭据消失，恢复此前保存的普通 Wi-Fi 配置，需要重新进行企业配网。

企业 Wi-Fi 已认证且拿到 IP 后，可用 `gateway.session` 仅在 RAM 中绑定 Gateway URI/Token，
不重启设备、不持久化 Token；重启后不会把临时 Token 自动发送到此前的开放网络。
该入口只接受当前已连接的企业认证会话。
`wifi.scan` 的 `ssid="*"` 可读取设备当前可见 AP 的实际 SSID/BSSID，避免名称或大小写误判。

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
