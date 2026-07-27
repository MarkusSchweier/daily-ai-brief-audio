# Phase 1 — read-only live AWS verification evidence

Audio-link-delivery feature (PRD `docs/prd/audio-link-delivery.md`, ADR-0017). All calls below
are read-only (`get-bucket-policy`, `get-object`, `head-object`) against account `740353583786`,
`us-east-1`, run 2026-07-27. No AWS mutation was performed. Real Polly task ids are **redacted**
(middle segments) below — a live key is a working public URL for up to 7 days.

## Step 0 — D2 precondition: no existing bucket policy

```
$ aws s3api get-bucket-policy --bucket cowork-polly-tts-740353583786
An error occurred (NoSuchBucketPolicy) when calling the GetBucketPolicy operation:
The bucket policy does not exist
```

**Result: PASS.** Matches the ADR-0017 D2 precondition exactly — the bucket has no bucket policy
today, so it is safe for `BriefSubscribersStack` to become the sole owner of a new one.
Account identity confirmed first via `aws sts get-caller-identity` → Account `740353583786`.

## Step 1 — locate real audio keys via `briefs/<date>/audio-pointer.json` (no ListBucket)

Recent weekday dates were inferred from the repo's own history (the Haiku-hardened production
config has been live and sending daily since 2026-07-14; today is Monday 2026-07-27) rather than
via `s3:ListBucket`, per the ADR-0017 D9 method. `GetObject` on each candidate date's pointer
file succeeded for four consecutive prior weekdays (2026-07-21 through 2026-07-24); 2026-07-17
was tried as a fallback probe and correctly 404'd (`NoSuchKey`) since a pointer only exists once
a brief has actually archived — not used as evidence. Four samples obtained (more than the
three the ADR asks for).

| Date | `audio_key` (redacted) |
|---|---|
| 2026-07-24 | `audio/.4632****-****-****-****-********2bf.mp3` |
| 2026-07-23 | `audio/.161b****-****-****-****-********3ca.mp3` |
| 2026-07-22 | `audio/.1e07****-****-****-****-********4e0.mp3` |
| 2026-07-21 | `audio/.b9fa****-****-****-****-********3dd.mp3` |

All four share the `audio/.<TaskId>.mp3` shape ADR-0017's Context section documents (Polly's
leading-dot naming from `OutputUri`).

## Step 2 — `head-object` on three of the real keys (D1 + D9 gates)

| Date | ContentType | ContentLength | LastModified (UTC) | `Expiration` header |
|---|---|---|---|---|
| 2026-07-24 | `audio/mpeg` | 1,208,780 bytes | 2026-07-24T04:18:53Z | `expiry-date="Sat, 01 Aug 2026 00:00:00 GMT", rule-id="expire-audio-7-days"` |
| 2026-07-23 | `audio/mpeg` | 1,626,668 bytes | 2026-07-23T04:12:18Z | `expiry-date="Fri, 31 Jul 2026 00:00:00 GMT", rule-id="expire-audio-7-days"` |
| 2026-07-22 | `audio/mpeg` | 2,203,532 bytes | 2026-07-22T04:11:35Z | `expiry-date="Thu, 30 Jul 2026 00:00:00 GMT", rule-id="expire-audio-7-days"` |

**Result: PASS on both D1 and D5 gates.**
- `ContentType` is `audio/mpeg` on all three samples — the D5 Content-Type-override contingency
  is **not needed**.
- `Expiration` is present and parseable on all three samples (HTTP-date shape,
  `email.utils.parsedate_to_datetime`-compatible) — D1's `head_object()`-based expiry read is
  viable with no fallback triggered.
- Sanity check on the Europe/Berlin rendering: `01 Aug 2026 00:00:00 GMT` → Europe/Berlin (CEST,
  UTC+2 in Northern-Hemisphere summer) is `02:00` on the same calendar day, `1 August 2026` — no
  day-rollback observed on real data, consistent with the ADR's note that Berlin, always ahead of
  UTC, never demonstrates the day-changing case on real objects (the unit test for that uses a
  synthetic non-midnight time, per the ADR).

## Step 3 — key-entropy assessment (D9, no further AWS calls)

Pass criterion: the `<TaskId>` segment of `audio/.<TaskId>.mp3` matches the RFC 4122 UUID shape
`[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}`, with the version nibble (first hex
digit of the third group) `4` and the variant nibble (first hex digit of the fourth group) in
`8-b`, giving 122 bits of randomness. All four sampled ids were checked against the full
(unredacted, local-only) value before redaction:

| Date | 8-4-4-4-12 shape | Version nibble | Variant nibble |
|---|---|---|---|
| 2026-07-24 | matches | `4` | `9` |
| 2026-07-23 | matches | `4` | `8` |
| 2026-07-22 | matches | `4` | `8` |
| 2026-07-21 | matches | `4` | `a` |

**Result: PASS.** All four ids are RFC 4122 UUID v4-shaped (122 bits of randomness each). The
four samples share no common prefix (first hex chars `4`, `1`, `1`, `b`) and are not sequential
or timestamp-derived when compared against their respective `LastModified` order (2026-07-21
through 2026-07-24 do **not** produce a lexicographically increasing or otherwise patterned id
sequence). FR-3/AC-6's "verify against a real live object" requirement is satisfied with zero key
changes needed — unguessability comes from Polly's own task id, exactly as the PRD assumed.

## Overall phase-1 outcome

**No stop condition fired.** All four gates (D2 no-existing-policy precondition, D1 `Expiration`
presence, D5/D9 `ContentType == audio/mpeg`, D9 UUID-v4 key entropy) passed on real, live,
current-production objects. Phases 0/2/3/5 may proceed as designed, with no Content-Type-override
contingency needed in the D5 response-headers policy (it stays available as a documented no-op
slot per the ADR, but is not exercised).
