# App chat attachments

`GET /api/v1/tenants/me/` advertises the additive capability:

```json
{"multi_attachments": {"images": 6, "documents": 3}}
```

This endpoint versions its cache key so older backend revisions cannot serve a
cached response advertising array support during rollout or rollback.

When present, clients may send `images: [base64, …]` and
`documents: [base64, …]` in one `POST /api/v1/chat/messages/`. Images keep the
existing 1,500,000-byte per-file cap and supported formats; documents remain PDF
only, at most 10 MiB each. All decoded files together must fit within 20 MiB.
Existing validation, including PDF structure checks, applies to every file
before any file is stored. Empty arrays are allowed; each array entry must
contain a file. Base64 strings and data URLs retain their existing support.

The legacy `image` and `document` fields still work. When combined with arrays,
each singular file precedes its corresponding array and counts toward the same
limits. Delivery order is images then documents, preserving order within each
array. Only an image count above six or a document count above three returns
`multiple_attachments` (400). An aggregate decoded-size overflow returns
`attachments_too_large` (400); the coarse raw-body ceiling returns
`request_too_large` (413). Per-file validation errors keep their existing codes.
The global Django body-read backstop is 32 MiB so it admits these base64 bodies;
the chat view retains its tighter raw-body ceiling and decoded-file limits.

The message/detail and thread-history representations add
`attachments: [{"kind": "image"}, {"kind": "document"}, …]`, in stored-file order,
and retain the existing `has_image` and `has_document` fields. Since-sync emits
this list on the user row, including uploads with no caption, and an empty list
on other rows. Paths remain internal; these entries describe attachment types,
without download URLs or thumbnails. Failed stores use the existing resend
notice in the runtime turn and do not produce an attachment entry.

`AppChatMessage.attachment_paths` stores every successful workspace path;
`attachment_path` retains the first. Historical rows fall back to their existing
singular path without a backfill. The new column retains a database default of
`[]` so older backend workers can continue inserting messages while the additive
schema rolls out. Runtime delivery uses the existing per-file
untrusted-data marker and singleton queue behavior. A single file's runtime
framing and no-caption text are unchanged. Each PDF keeps its existing async
extraction behavior. The supplied chat thread continues to carry the turn's
context.

Multi-file turns hash complete file contents for storage names, preventing
distinct files with identical first 1 KiB from overwriting each other. Single-file
turns retain the legacy hash and paths.

Clients should only send arrays when the capability is present, otherwise send
one legacy attachment per message. This backend change does not implement the
iOS picker, staging strip, or bubble grid.
