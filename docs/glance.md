# RemboAI Glance display copy

Proposed short, owner-facing labels for a future Glance client. These are display strings only; they do not represent implemented Glance behavior or change case, payment, or delivery state.

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
