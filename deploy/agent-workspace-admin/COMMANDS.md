# Approved Admin Commands

Use the narrow `openclaw-cap` wrappers for routine service operations. General
shell access is reserved for explicit administrator requests.

## Public users

- `openclaw-cap users list` — the public-user member store
- `openclaw-cap users <add|remove|sync|check> ...` — manage it

## CV orders

CV orders are approved or denied from WhatsApp with `cv approve <id>` and
`cv deny <id>`. Report user-facing status only; do not reveal phone numbers,
order data, or raw records.
