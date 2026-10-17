# TeamSlot API v0.1

Base URL: `http://127.0.0.1:8000`。所有 POST 請求使用 `Content-Type: application/json`，本文上限 16 KiB。時間必須含 `Z` 或數值時區偏移；回應統一為帶六位微秒的 UTC 字串。

## 資源

`POST /api/resources`

```json
{"name": "Meeting Room A"}
```

成功為 `201`，回傳 `id`、`name`、`created_at`。重複名稱為 `409 resource_exists`。名稱長度 1–80 字元，去除前後空白。

`GET /api/resources` 回傳 `200`：

```json
{"resources": [{"id": "<resource UUID>", "name": "Meeting Room A", "created_at": "2030-01-01T00:00:00.000000Z"}]}
```

## 建立預約

`POST /api/bookings`

```json
{
  "resource_id": "<resource UUID>",
  "title": "Sprint planning",
  "organizer": "Demo user",
  "start": "2030-01-01T18:00:00+08:00",
  "end": "2030-01-01T19:00:00+08:00"
}
```

`resource_id` 請替換為真實資源 UUID。用途長度 1–120 字元，顯示名稱 1–80 字元。結束必須晚於開始，每筆最多 8 小時。允許過去日期供測試與展示使用。缺少或多餘的欄位都會得到 `400 invalid_fields`。

成功 `201` 範例：

```json
{
  "id": "<booking UUID>",
  "resource_id": "<resource UUID>",
  "title": "Sprint planning",
  "organizer": "Demo user",
  "start": "2030-01-01T10:00:00.000000Z",
  "end": "2030-01-01T11:00:00.000000Z",
  "status": "confirmed",
  "created_at": "2030-01-01T00:00:00.000000Z",
  "cancel_token": "<one-time-displayed random capability>"
}
```

保存 `id` 與 `cancel_token`。一般查詢不會再次顯示憑證；本版本沒有遺失憑證恢復功能。

## 查詢行程

`GET /api/bookings?resource_id=<UUID>&start=<ISO8601>&end=<ISO8601>`

回傳 `{"bookings": [...]}`，包含與查詢區間重疊且尚未取消的預約，依開始時間排序。回傳完整的預約時間，可能超出查詢區間；前端會將當日畫面限制在當地日期內。回應不含 `cancel_token` 或 `cancel_token_hash`。

查詢範圍最多 31 天，必須是正長度。查詢參數必須非空且不可重複。不接受未定義參數。帶有 `+08:00` 等偏移的 query 值必須做 URL encoding，建議使用 `URLSearchParams` 或 `urllib.parse.urlencode`。

## 查詢可用區間

`GET /api/availability?resource_id=<UUID>&start=<ISO8601>&end=<ISO8601>&duration_minutes=30`

`duration_minutes` 預設 30，允許 1–480 的整數，代表最短連續空檔長度。這不是固定長度候選時間片；每一項是可容納該長度預約的完整連續區間。

```json
{
  "intervals": [
    {"start": "2030-01-01T09:00:00.000000Z", "end": "2030-01-01T10:00:00.000000Z"},
    {"start": "2030-01-01T11:00:00.000000Z", "end": "2030-01-01T12:00:00.000000Z"}
  ]
}
```

可用區間查詢是當下的快照，不保留時段。真正建立時仍會再次檢查衝突。

## 取消預約

`POST /api/bookings/{booking_id}/cancel`

Headers:

```text
Content-Type: application/json
Authorization: Bearer <cancel_token>
```

Body:

```json
{}
```

成功 `200` 回傳預約與 `status: "cancelled"`，不回傳憑證。使用相同憑證重複取消仍為 `200`；缺少或錯誤憑證為 `403`。取消後立刻釋出原時段。

## 錯誤格式

```json
{"error": {"code": "booking_conflict", "message": "This resource is already booked for that time."}}
```

| HTTP | 常見 code | 原因 |
| --- | --- | --- |
| 400 | `invalid_field`, `invalid_fields` | 欄位型別、UUID、長度或欄位集合錯誤 |
| 400 | `invalid_time`, `invalid_window`, `invalid_duration` | 時區、範圍或時長錯誤 |
| 400 | `invalid_json`, `invalid_query`, `invalid_length`, `invalid_transfer_encoding` | 請求格式錯誤 |
| 403 | `invalid_cancel_token` | 取消憑證錯誤 |
| 404 | `resource_not_found`, `booking_not_found`, `not_found` | 找不到資料或路徑 |
| 405 | `method_not_allowed` | 路徑不支援此方法 |
| 409 | `resource_exists`, `booking_conflict` | 名稱重複或預約衝突 |
| 413 | `body_too_large` | 本文超過 16 KiB |
| 415 | `unsupported_media_type` | 未使用 JSON |
| 503 | `database_unavailable` | 資料庫暫時無法使用，可稍後重試 |

## Python 完整操作範例

先啟動服務，再將以下內容放入互動式 Python 或自己的測試檔：

```python
import json
from urllib.request import Request, urlopen

def post(path, payload, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    request = Request("http://127.0.0.1:8000" + path, data=json.dumps(payload).encode(), headers=headers, method="POST")
    with urlopen(request) as response:
        return json.load(response)

resource = post("/api/resources", {"name": "API demo room"})
booking = post("/api/bookings", {
    "resource_id": resource["id"], "title": "Demo", "organizer": "Sample user",
    "start": "2030-01-01T10:00:00Z", "end": "2030-01-01T11:00:00Z",
})
cancelled = post("/api/bookings/" + booking["id"] + "/cancel", {}, booking["cancel_token"])
print(cancelled["status"])
```

重跑範例前請改資源名稱，否則會得到名稱已存在的 `409`。
