# RemboAI Glance

RemboAI Glance is the owner escalation companion for Even Realities G2. It brings open customer handoffs from the RemboAI booking receptionist to a glanceable display, with the paired phone application used for full context review and exact-text reply editing.

The RemboAI backend implements paired owner sessions, salon-scoped inbox and case context, AI and manual drafts, reply approval and status, and safe case resolution. The companion client is maintained separately. End-to-end HTTPS staging and packaged Even Hub WebView acceptance have not yet been completed, so these capabilities should not be presented as a live service.

These short labels are UI copy. Case, payment, and delivery state must always come from the backend.

| Situation | Display copy |
|---|---|
| New escalation | New request needs your review |
| Payment review | Check payment in your account |
| Complaint | Customer shared a concern |
| Booking issue | Booking needs your attention |
| AI draft ready | Reply draft is ready |
| Listening | Listening… |
| Transcribing | Turning speech into text… |
| Transcript ready | Transcript ready. Review it. |
| Approval required | Review and approve before sending |
| Message queued | Reply queued |
| Message submitted | Reply accepted by the messaging service |
| Message failed | Reply was not accepted. Try again. |
| Delivery unconfirmed | Sending result is unclear. Check before retrying. |
| Case resolved | Case marked resolved |
| Session expired | Glance session expired. Pair again. |
| Connection lost | Connection lost. Reconnect to continue. |

## State wording rules

- “Queued” means RemboAI saved the approved reply for sending.
- “Submitted” means the messaging provider accepted the request. It does not mean the customer received or read it.
- “Delivery unconfirmed” means the provider may have accepted the request, but RemboAI cannot establish the outcome. Do not suggest an automatic retry.
- “Case resolved” describes the owner's explicit case action. It does not imply that a payment was verified, refunded, or otherwise settled.
- Payment review asks the owner to check the receiving account. A screenshot or extracted receipt fields are not proof of payment.
- Keep customer-visible replies separate from these owner-only labels and from internal case notes.
