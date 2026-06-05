"""Retries, backoff, and rate limiting (ARCHITECTURE.md §13).

External calls — GitHub and the model — fail transiently, so they are wrapped in
exponential backoff with a bounded retry count and typed give-up behavior (a
retry that never gives up is just a slower infinite loop). A token-bucket
limiter caps the rate of external calls so limpiador is not throttled or banned
during a busy run. Retry counts, backoff base, and bucket size are named
configuration (CLEAN_CODE.md §7); give-up raises ``TransientError`` (errors.py).
"""
