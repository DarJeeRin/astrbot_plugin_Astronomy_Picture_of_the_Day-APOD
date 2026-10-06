# astrbot_plugin_Astronomy_Picture_of_the_Day

AstrBot 的 NASA Astronomy Picture of the Day 插件。

插件会通过迁移后的 NASA Science APOD 公共接口获取当天的每日天文图片，并按配置返回图片、标题、日期和说明文本；也支持调用 AstrBot 已配置的 LLM 提供商将标题和说明翻译为简体中文。

## 功能简介

- 通过 `apod` 指令获取 NASA 每日天文图片
- 支持返回图片、视频直链下载、标题、日期、说明
- 支持将标题和说明翻译为简体中文
- 支持分段发送或合并为一条消息链发送
- 支持按 UMO 列表定向自动推送到指定群聊/会话
- 内置 APOD 数据缓存，减少重复请求 NASA API
- 内置翻译结果缓存，避免重复翻译相同文本
- 内置推送状态与推送内容缓存，实现“拉取一次，发送多次”
- 支持超时和失败重试配置

## 适用版本

- AstrBot `>= 4.16`；已使用 `4.28.2` 验证消息组件、插件 KV 缓存和 Context 接口兼容性

## 支持平台

- `aiocqhttp`

## 指令

### 获取今日 APOD

```text
/apod
```

### 获取当前会话 UMO（用于自动推送配置）

```text
/sid
```

## 配置说明

插件配置项定义见 `_conf_schema.json`。下面是各字段的实际含义：

### `token`

旧版 NASA API Token，仅为兼容已有配置保留，不再用于请求。

- 类型：`string`
- 必填：否
- 新接口无需 API Key，可以留空或删除旧值

### `image`

是否发送图片。

- 类型：`bool`
- 默认值：`true`

### `explanation`

图片说明配置。

- 类型：`object`

子项：

- `is_show`：是否发送 NASA 官方说明，类型为 `bool`，默认 `true`
- `is_translate`：是否将说明翻译为简体中文，类型为 `bool`，默认 `true`

### `title`

标题配置。

- 类型：`object`

子项：

- `is_show`：是否发送标题，类型为 `bool`，默认 `true`
- `is_translate`：是否翻译标题，类型为 `bool`，默认 `true`

### `provider`

用于翻译的 LLM 提供商 ID。

- 类型：`string`
- 特殊类型：`select_provider`

说明：

- 只有在开启翻译时才需要配置
- 如果开启了标题或说明翻译，但没有配置 `provider`，插件将无法完成翻译

### `date`

日期配置。

- 类型：`object`

子项：

- `is_show`：是否发送日期，类型为 `bool`，默认 `true`

### `is_divided`

是否将图片和文本分开发送。

- 类型：`bool`
- 默认值：`true`

说明：

- `true`：图片、标题、日期、说明会分多条消息发送
- `false`：内容会组合为一条消息链发送

### `timeout`

请求 NASA APOD 数据的超时时间，单位为秒。

- 类型：`int`
- 默认值：`120`

### `retry_count`

NASA API 临时错误时的重试次数。

- 类型：`int`
- 默认值：`2`

### `push`

自动推送配置。

- 类型：`object`

子项：

- `enabled`：是否启用自动推送，类型为 `bool`，默认 `true`
- `target_unified_msg_origins`：目标会话 UMO 列表，类型为 `list`，默认 `[]`
- `daily_push_time`：每天自动推送时间（本机时区，24 小时制 `HH:MM`），类型为 `string`，默认 `"09:00"`
- `max_groups_per_round`：单轮最多推送数量，类型为 `int`，默认 `0`（不限制）

说明：

- UMO 可通过 `/sid` 获取
- 到达 `daily_push_time` 后执行一次推送，并通过 APOD 日期去重避免重复发送
- `daily_push_time` 使用运行 AstrBot 机器的本地时区

## 配置示例

```json
{
  "image": true,
  "explanation": {
    "is_show": true,
    "is_translate": true
  },
  "title": {
    "is_show": true,
    "is_translate": true
  },
  "provider": "your_provider_id",
  "date": {
    "is_show": true
  },
  "is_divided": true,
  "timeout": 120,
  "retry_count": 2,
  "push": {
    "enabled": true,
    "target_unified_msg_origins": [
      "aiocqhttp:GroupMessage:123456789"
    ],
    "daily_push_time": "09:00",
    "max_groups_per_round": 0
  }
}
```

## 返回行为

根据配置不同，插件会返回以下内容中的一部分：

- 图片：优先使用 `hdurl`，缺失时提取 `basic_html` 中的图片；新接口的 `url` 为文章链接，不作为图片发送
- 标题和说明：先清理 HTML 标签并解码实体，再显示或翻译
- 标题：原文或翻译后的中文标题
- 日期：APOD 对应日期
- 说明：原文或翻译后的中文说明

视频类 APOD 会从新接口的 `basic_html` 提取真实视频来源。开启 `video.download` 时下载视频直链并发送；关闭下载、外部播放器页面、下载失败或超过大小上限时返回视频链接。`image` 控制图片及视频缩略图。

## 缓存机制

插件包含三类缓存：

### APOD 数据缓存

- 会缓存最近获取到的 APOD 数据
- 当缓存仍然有效时，优先使用缓存，避免频繁请求 NASA API
- 缓存过期后会重新拉取最新数据

### 翻译结果缓存

- 标题和说明翻译结果会被缓存
- 相同文本再次出现时，优先使用缓存翻译结果
- 可以减少模型调用次数并提升响应速度

### 自动推送缓存

- `apod_push:last_sent_date`：记录最近一次已推送的 APOD 日期
- `apod_push:last_payload:v3:<date>`：记录某天 APOD 的已组装推送内容
- 一轮定时推送中只拉取一次 APOD，然后复用同一份内容推送给多个目标会话

## 翻译说明

当以下条件同时满足时，插件会自动调用 LLM 进行翻译：

- `title.is_translate` 或 `explanation.is_translate` 为 `true`
- 对应内容开启显示
- `provider` 已正确配置

翻译目标语言为简体中文，提示词偏向准确直译，不附加额外解释。

## 错误处理

插件对以下情况做了处理：

- NASA API 限流
- 接口访问被拒绝或网络代理阻止
- 网络请求超时
- 临时服务错误，如 `502`、`503`、`504`
- 一般网络异常

在可重试错误下，插件会按照 `retry_count` 进行有限次重试。

## 使用建议

- 新接口无需 NASA API Token；部署网络需允许 `science.nasa.gov` 和图片域名 `assets.science.nasa.gov`
- 如果你不需要中文翻译，可以关闭翻译选项并留空 `provider`
- 如果你希望聊天体验更自然，建议开启 `is_divided`
- 如果你更希望一次性返回完整内容，可以关闭 `is_divided`

## 视频下载与失败日志

```json
"video": {
  "download": true,
  "max_download_mb": 100,
  "download_timeout": 120
}
```

`download` 默认 `false`，需主动开启；仅下载可直接访问的视频文件。YouTube、Vimeo 等播放器页面不会下载，并记录 `reason=external_page`。`download_timeout` 是视频下载总超时，独立于 NASA JSON 请求的 `timeout`；低带宽服务器可先调整为 300 或 600 秒，并观察日志。下载使用环境代理并保持 TLS 验证。

AstrBot 日志会记录以下阶段：

- `stage=video_download`：视频下载失败，包含 URL（去除查询参数）、异常类型、耗时、超时阈值、已下载字节及预期大小。即使异常文本为空，也会显示 `TimeoutError`。
- `stage=image_prepare`：AstrBot 图片下载/编码失败，包含异常类型、耗时和图片来源。图片准备沿用框架自身的下载超时，不受 `video.download_timeout` 或 NASA 数据请求 `timeout` 控制。
- `stage=send`：命令回复或定向推送失败，包含目标会话、APOD 日期、媒体类型、异常类型和耗时。媒体回复直接等待适配器发送，使插件能够捕获失败；临时视频会在发送返回后清理。

发送接口返回成功只表明适配器调用完成，不能证明客户端已播放视频。OneBot 端必须能读取视频文件：分容器/分服务器部署需共享文件路径或正确配置 AstrBot 的 `callback_api_base`。发送异常中的路径错误、平台错误码与下载超时是不同问题。

验证旧分支 `feat/video-download` 时，2026-09-13 的 NASA MP4 在原代码的 8 秒诊断阈值内超时；仅启用环境代理后约 0.66 秒下载 4,805,245 字节，`ffprobe` 确认 H.264 视频。这个结果证明直链下载有效，也说明代理配置可能导致失败；不能据此推断实际部署服务器的带宽。

## NASA APOD 迁移

APOD 网站已迁移到 <https://science.nasa.gov/apod/>。插件直接访问官方公共 JSON 接口：

```text
https://science.nasa.gov/wp-json/wp/v2/apod-basic/YYMMDD
```

日期按 `America/New_York` 时区选择，避免亚洲时区已进入次日而上游尚未发布。定时推送时间仍使用运行机器的本地时区。接口不传递旧 `token`，并通过 `trust_env=True` 支持环境 HTTP/HTTPS 代理。

迁移后 `url` / `permalink` 指向文章页面，`explanation` 可包含 HTML。插件会规范化数据，并使用新版数据与推送内容缓存键，避免复用迁移前的文章链接或 HTML 内容。视频类内容会解析 `<video>`、`<source>`、`<iframe>` 或 `<embed>` 的来源，文章 permalink 不作为视频直链。

官方迁移说明与响应示例：<https://github.com/nasa/apod-api/blob/master/README.md>。

## 开发与验证

使用 Python 3.12：

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m pip check
```

测试会将 AstrBot 配置和 SQLite 数据写入临时目录，使用官方迁移响应示例与本地 HTTP 服务，验证新接口、无 Token 请求、文本清理、图片地址、缓存、重试、消息结果、翻译调用契约和推送去重。Context 使用真实 API 签名的模拟对象，不会调用真实 LLM 或发送群消息。已在云环境验证 NASA JSON 请求、图片与 MP4 下载及 OneBot 消息序列化；实际群聊发送仍需在配置适配器的部署环境中验证。

## 项目地址

- Repository: <https://github.com/DarJeeRin/astrbot_plugin_Astronomy_Picture_of_the_Day-APOD>

## 致谢

- [NASA Science APOD](https://science.nasa.gov/apod/)
- [AstrBot](https://github.com/AstrBotDevs/AstrBot)
