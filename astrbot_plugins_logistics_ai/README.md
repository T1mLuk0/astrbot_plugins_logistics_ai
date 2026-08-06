# AstrBot LogisticsAI Plugin

This AstrBot plugin captures QQ group messages for the LogisticsAI platform. It
uploads the original message first, preserves explicit reply context, and can
optionally run silent visual extraction through AstrBot's active multimodal
provider.

The plugin does not send a bot reply to the group. Raw message persistence is
the primary operation: multimodal analysis starts only after the original
message has been accepted by the backend, and an analysis failure never
invalidates the raw upload.

## Features

- Captures group, sender, text, image, file, role, and receive-time data.
- Ignores messages sent by the bot itself.
- Captures AstrBot `Reply` components and the quoted message snapshot.
- Uploads raw messages asynchronously with retry and exponential backoff.
- Reuses a shared `aiohttp` connection pool.
- Supports Bearer tokens and custom API-key headers.
- Optionally analyzes messages that contain current-message images.
- Uses AstrBot's active chat provider, such as Xiaomi MiMo, without changing
  the provider configured in AstrBot.
- Uploads visual extraction through an independent second-stage endpoint.
- Does not call the multimodal model for text-only messages.
- Shuts down background tasks and HTTP sessions cleanly.

## Processing Order

```text
QQ group message
    -> capture current message and explicit Reply context
    -> POST the raw message to LogisticsAI
    -> receive the stored database ID
    -> if enabled and the current message contains images:
         call AstrBot's active multimodal provider silently
         PUT the structured visual result to the analysis endpoint
```

The backend remains responsible for text-only extraction, historical context
resolution, business-event matching, database projection, and customer chat.
The AstrBot-side model performs visual observation only and must not guess
which existing database row should be updated.

## Raw Message Endpoint

The plugin sends the original message to:

```http
POST /api/messages
Content-Type: application/json
```

Example payload:

```json
{
  "platform": "qq",
  "groupId": "123456789",
  "groupName": "Shipping Operations",
  "userId": "987654321",
  "nickname": "Operator",
  "senderRole": "member",
  "messageId": "10002",
  "messageType": "group_message",
  "content": "Price pending, including released bookings.",
  "images": [
    "https://example.com/current-message-image.jpg"
  ],
  "files": [],
  "reply": {
    "messageId": "10001",
    "userId": "222333444",
    "nickname": "Sales",
    "content": "KMTC NHAVA SHEVA 2605W ETD moved to the 16th.",
    "images": [],
    "files": [],
    "receiveTime": "2026-08-06T01:00:00+00:00"
  },
  "receiveTime": "2026-08-06T01:05:00+00:00"
}
```

The `reply` property is omitted when the source event does not contain an
explicit reply. Existing ASP.NET Core DTOs normally ignore the new
`senderRole` and `reply` properties until backend support is added, so the
original endpoint contract remains compatible.

Any `2xx` response is treated as a successful raw upload. The default analysis
URL requires the response to include the stored ID in this shape:

```json
{
  "data": {
    "id": 123,
    "platform": "qq",
    "messageId": "10002"
  },
  "traceId": "optional-trace-id"
}
```

## Multimodal Analysis Endpoint

When multimodal analysis is enabled, the plugin uploads the result to:

```http
PUT /api/messages/{database_id}/analysis
Content-Type: application/json
```

The payload is the JSON object returned by the active AstrBot model, normalized
with analyzer metadata, the source message ID, and an analysis timestamp. The
schema can contain:

```text
sailings
freightRates
events
unmappedFacts
evidence
warnings
requiresBackendContextResolution
```

An event may describe a schedule delay, price change, pending price, space
shortage, booking suspension, or another logistics update. Uncertain values
remain in raw expression fields for backend review instead of being forced into
an unreliable frontend field.

Keep `multimodal_analysis_enabled` set to `false` until the backend implements
the analysis endpoint. Raw uploads continue to work while analysis is disabled.

## Configuration

Configure the plugin from the AstrBot plugin settings page.

| Setting | Default | Purpose |
| --- | --- | --- |
| `enabled` | `true` | Enable raw message uploads. |
| `api_url` | `http://127.0.0.1:5000/api/messages` | Full raw-message endpoint URL. |
| `analysis_url_template` | empty | Optional analysis URL template. |
| `api_token` | empty | Backend authentication token. |
| `token_header` | `Authorization` | Header that carries the token. |
| `timeout` | `15.0` | Backend HTTP timeout in seconds. |
| `retry_count` | `3` | Retry count after a failed HTTP request. |
| `retry_interval` | `1.0` | Initial retry delay in seconds. |
| `verify_ssl` | `true` | Verify HTTPS certificates. |
| `max_concurrency` | `4` | Maximum concurrent backend requests. |
| `multimodal_analysis_enabled` | `false` | Enable silent analysis for messages with images. |
| `multimodal_analysis_timeout` | `120.0` | Model request timeout in seconds. |
| `multimodal_analysis_concurrency` | `1` | Concurrent model requests, from 1 to 4. |
| `multimodal_analysis_temperature` | `0.1` | Structured extraction temperature. |

`analysis_url_template` supports these placeholders:

```text
{database_id}
{platform}
{message_id}
```

When the template is empty, the plugin appends
`/{database_id}/analysis` to `api_url`.

For the deployed website, a typical raw endpoint is:

```text
https://eshinetong.com/api/messages
```

## Docker Networking

If AstrBot and the LogisticsAI backend run in different Docker containers,
`127.0.0.1` points back to the AstrBot container and normally cannot reach the
backend. Use a shared Docker network and the backend service name, for example:

```text
http://logistics-api:8080/api/messages
```

If AstrBot should use the public reverse proxy, use the HTTPS website endpoint:

```text
https://eshinetong.com/api/messages
```

## Installation

The plugin requires Python 3.10 or later and AstrBot 4.26.7 or a compatible
version. Install or update the plugin through AstrBot, or place the repository
under AstrBot's plugin directory.

The top level must contain the plugin files directly:

```text
astrbot_plugins_logistics_ai/
|-- __init__.py
|-- main.py
|-- api.py
|-- analyzer.py
|-- prompts.py
|-- models.py
|-- exceptions.py
|-- metadata.yaml
|-- _conf_schema.json
|-- requirements.txt
|-- README.md
|-- LICENSE
`-- .gitignore
```

Do not create a duplicated nested plugin directory inside the ZIP archive.

## Verification

Before enabling multimodal analysis, send these messages from a test group:

1. A text-only message. Confirm one raw `POST` and no model call.
2. A reply to an earlier message. Confirm the raw payload contains `reply`.
3. An image with a short caption. Confirm the raw `POST` completes first.
4. Enable multimodal analysis only after the backend analysis endpoint exists.
5. Repeat the image test and confirm one second-stage `PUT` is recorded.

Run the local standard-library tests from the repository parent directory:

```bash
python -m unittest discover -s astrbot_plugins_logistics_ai/tests -v
```

## Failure Isolation

- A raw upload failure stops processing for that message because there is no
  stored record to attach analysis to.
- A provider timeout or invalid provider response produces a bounded failed
  analysis payload.
- A failed analysis upload is logged and does not remove or alter the raw
  message that was already stored.
- The plugin never sends model output back into the QQ group.

## License

See `LICENSE`.
