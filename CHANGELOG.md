# Changelog

This file records engineering milestones; dated entries are historical project notes.
## Oct 27
- Project skeleton, MIT licence, configuration from environment variables.
- Migration runner and first migration (inbound message dedupe).
- WhatsApp Cloud API module: signature verification, payload parsing, text sending, delivery-failure explanations.
- Signed, idempotent webhook that echoes the message back.
