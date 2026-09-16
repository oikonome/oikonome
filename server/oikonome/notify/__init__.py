"""Delivery channels beyond email — SMS and Web Push — for the
scheduled budget summaries (and, later, the transaction digests).

Design rules:
  * The short-form texts derive from the SAME payloads the email renders
    (report.gather / lenses.*_summary) — one computation of the verdict,
    several phrasings (the email-mirror doctrine, extended).
  * Every channel degrades to a NO-OP with a reason when unconfigured:
    no Twilio env → SMS off; the settings API surfaces the reason so the
    UI can say why a toggle is disabled instead of failing silently.
  * SMS is tenant-level (one verified phone per household, in tenant
    config); push is per-browser (control-plane subscriptions, fanned out
    tenant-wide by the worker).
"""
