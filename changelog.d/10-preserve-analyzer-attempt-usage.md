# Preserve analyzer attempt accounting

Analyzer reports now retain an independent per-invocation snapshot of native
usage plus requested and provider-reported model/settings for original,
fallback, and post-check repair calls. Aggregate usage remains unknown when
any invocation reports unknown usage; missing token values are not estimated.
