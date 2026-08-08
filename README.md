# AstrBot LogisticsAI Plugin

This AstrBot plugin captures supported AstrBot message events for the
LogisticsAI platform. It is compatible with QQ official (`qq_official`) and
NapCat/OneBot adapters. Text-only messages remain transport-only. When a
message contains an image or file, AstrBot's configured native multimodal
provider is allowed to produce its normal reply first; the plugin intercepts
that final result before the platform sends it, forwards the reply to the
existing backend analysis endpoint, and suppresses the platform send.

## Features

- Captures group/private/channel, sender, text, image, file, role, and
  receive-time data.
- Accepts all AstrBot message event types instead of filtering only
  `GROUP_MESSAGE`.
- Preserves the source platform as `qq_official` or `napcat` in the payload.
- Gives QQ official private/C2C events a bounded synthetic `groupId` such as
  `private:<userId>` because the existing backend contract requires a group
  identifier for every message.
- Converts oversized QQ official message IDs to deterministic, platform-
  prefixed SHA-256 IDs so they fit the backend's 128-character limit while
  remaining stable for native reply correlation.
- Ignores messages sent by the bot itself.
- Captures AstrBot `Reply` components and the quoted message snapshot.
- Promotes images/files inside `Reply.chain` into the current media context,
  so “quoted image + current text” follows the same native MiMo path as a
  directly attached image.
- Uploads every raw message immediately and asynchronously with retry and
  exponential backoff, including image-only messages.
- Reuses a shared `aiohttp` connection pool.
- Supports Bearer tokens and custom API-key headers.
- Lets AstrBot's native multimodal provider handle media messages.
- Forwards the native assistant reply to the backend without sending it to QQ.
- Keeps all structured analysis and MySQL writes inside the backend.
- Shuts down background tasks and HTTP sessions cleanly.

## Processing Order

```text
QQ official or NapCat message
    -> capture current message and explicit Reply context
    -> immediately POST the raw message to LogisticsAI
    -> if media: let AstrBot run its native provider in parallel
    -> intercept the final result before platform send
    -> for media: update the same raw row with native MiMo text
    -> queue that row for another backend analysis pass
    -> suppress the native platform send (text and media are both silent)

Backend background worker
    -> read the stored message from MySQL
    -> call the configured backend MiMo model for text and media raw messages
    -> persist structured events, sailings, and freight rates
```

The backend remains responsible for MiMo extraction, historical context
resolution, business-event matching, database projection, and customer chat.
For media, the raw row exists before native MiMo finishes. The plugin uses the
database ID returned by that first upload to update the same row through the
second-stage endpoint. If native MiMo returns no text, the original raw row is
still retained and the backend can analyze its media normally.

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

Any `2xx` response is treated as a successful raw upload. The response includes
the stored ID so the backend queue can process the message:

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

## Backend AI Analysis

The backend automatically queues every newly persisted raw message. It reads
the message and its explicit Reply snapshot from MySQL, calls the configured
MiMo model, and stores the structured result. When AstrBot later supplies a
native media description, the backend replaces that same row's content and
queues it again without making the plugin wait for backend AI.

The existing `POST /api/messages/{database_id}/text-analysis` endpoint remains
available for manual text re-analysis and troubleshooting. New media
interception no longer relies on that endpoint: the native AstrBot reply is
uploaded through the normal `POST /api/messages` raw-message endpoint, so the
backend worker analyzes exactly the intercepted text. The backend worker uses
the same structured schema:

```http
POST /api/messages/{database_id}/text-analysis
Content-Type: application/json

{"assistantReply":"Native MiMo reply text"}
```

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

## Configuration

Configure the plugin from the AstrBot plugin settings page.

| Setting | Default | Purpose |
| --- | --- | --- |
| `enabled` | `true` | Enable raw message uploads. |
| `api_url` | `http://127.0.0.1:5000/api/messages` | Full raw-message endpoint URL. |
| `api_token` | empty | Backend authentication token. |
| `token_header` | `Authorization` | Header that carries the token. |
| `timeout` | `30.0` | Raw-message upload timeout in seconds. It no longer includes AI time. |
| `assistant_reply_timeout` | `300.0` | Timeout for the background native-reply update request. |
| `retry_count` | `3` | Retry count after a failed HTTP request. |
| `retry_interval` | `1.0` | Initial retry delay in seconds. |
| `verify_ssl` | `true` | Verify HTTPS certificates. |
| `max_concurrency` | `4` | Maximum concurrent backend requests. |

The backend stores the image/file references supplied in the raw message. The
native provider is responsible for reading media through AstrBot's platform
adapter. A future media-storage endpoint can still be added when the website
must retain and serve the actual image bytes permanently.

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

Send these messages from a QQ official test chat and a NapCat test group:

1. A text-only logistics message. Confirm exactly one raw `POST`; the backend
   log should later show `Background text analysis stored`.
2. A reply to an earlier message. Confirm the raw payload contains `reply`.
3. An image with a short caption. Confirm the original raw `POST` happens
   immediately, the native reply does not appear on the source platform, and
   `POST /api/messages/{id}/text-analysis` later updates the same row.

4. A QQ official C2C message. Confirm the payload uses
   `platform=qq_official` and `messageType=private_message`.

5. A reply sent through either adapter. Confirm the quoted message snapshot is
   linked even if AstrBot creates a different event object for the result hook.

Run the local standard-library tests from the repository parent directory:

```bash
python -m unittest discover -s astrbot_plugins_logistics_ai/tests -v
```

## Failure Isolation

- A raw upload failure stops processing for that message because there is no
  stored record to attach analysis to.
- A backend model timeout or invalid provider response is isolated inside the
  backend worker and never makes AstrBot's raw upload fail.
- The raw record remains available for a later manual analysis retry.
- If native media text is empty or cannot be attached, the native QQ reply is
  still suppressed and the original raw row remains available for backend
  analysis or a later retry.
- A successful media interception suppresses the native QQ send and keeps the
  model output only in the backend workflow. Text replies are also suppressed
  and never uploaded as assistant evidence.

## License

See `LICENSE`.
