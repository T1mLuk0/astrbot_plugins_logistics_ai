# astrbot_plugin_logistics_ai

监听 AstrBot 接收到的 QQ 群消息，并将群、发送者、文本、图片和文件信息异步上传到 LogisticsAI 后端。

## 功能

- 监听群消息
- 忽略机器人自身消息
- 提取文本内容
- 提取图片地址
- 提取文件地址
- 异步上传消息
- 上传失败自动重试
- 支持 Bearer Token 或自定义 API Key 请求头
- 插件停止时自动释放 HTTP 连接

## 目录结构

```text
astrbot_plugin_logistics_ai/
├── __init__.py
├── main.py
├── api.py
├── models.py
├── exceptions.py
├── metadata.yaml
├── _conf_schema.json
├── requirements.txt
├── README.md
├── LICENSE
└── .gitignore
```

## 数据流

```text
QQ 群消息
    ↓
AstrBot 平台适配器
    ↓
main.py 监听事件
    ↓
解析群、用户、文本、图片、文件
    ↓
构建标准消息数据
    ↓
api.py 异步 POST
    ↓
LogisticsAI ASP.NET Core API
    ↓
数据库 / OCR / AI / WebHook / WebSocket
```

## 后端接口

默认地址：

```text
POST http://127.0.0.1:5000/api/messages
```

请求内容：

```json
{
  "groupId": "123456789",
  "groupName": "物流业务群",
  "userId": "987654321",
  "nickname": "张三",
  "messageId": "10001",
  "messageType": "group_message",
  "content": "今天的货物已经发出",
  "images": [
    "https://example.com/image.jpg"
  ],
  "files": [
    "https://example.com/file.pdf"
  ],
  "receiveTime": "2026-07-29T07:31:04.249000+00:00"
}
```

后端返回任意 `2xx` 状态码即表示上传成功。

## 插件配置

在 AstrBot 插件配置页面填写：

- `enabled`：是否启用上传
- `api_url`：后端完整接口地址
- `api_token`：认证令牌
- `token_header`：认证请求头名称
- `timeout`：请求超时秒数
- `retry_count`：失败重试次数
- `retry_interval`：首次重试间隔
- `verify_ssl`：是否验证 HTTPS 证书
- `max_concurrency`：最大并发上传数量

## Docker 网络说明

如果 AstrBot 和 LogisticsAI 后端都运行在 Docker 中，不要使用：

```text
http://127.0.0.1:5000
```

应使用后端容器的服务名称，例如：

```text
http://logistics-api:8080/api/messages
```

如果后端运行在宿主机，可以根据环境使用：

```text
http://host.docker.internal:5000/api/messages
```

## 安装

将整个插件目录压缩为 ZIP，ZIP 内第一层应为：

```text
astrbot_plugin_logistics_ai/
```

不要形成以下错误结构：

```text
astrbot_plugin_logistics_ai/
└── astrbot_plugin_logistics_ai/
    ├── main.py
    └── metadata.yaml
```