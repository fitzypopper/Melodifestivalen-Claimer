# melo-mitm — SVT Melodifestivalen daily-card claim: findings

Last updated: 2026-09-27 (session with working mitm capture + bundle analysis)

## Goal
Capture the exact API call for the daily-card claim (endpoint, headers, tokens, payload)
and produce a replayable `claim_daily.py`.

## Infrastructure (working)
- mitmdump 12.2.3 on `:8080`, detached, log `diag/mitmdump.log`, addon `diag/logreq.py`
  writes `diag/requests.jsonl` (fixed: mitmproxy 12 `get_text(errors=)` removed → use
  `.content.decode("utf-8","replace")`). Captured record count verified OK.
- mitm CA installed as Android system cert (`c8750f0d.0` in overlay); TLS interception
  confirmed: all app requests return 200 through the proxy, no pinning failures.
- Android global proxy `192.168.240.1:8080` set (persisted).
- Container has NO direct internet (host FORWARD policy drops it) — proxied traffic works
  anyway. Host iptables ACCEPT rules need sudo (`a password is required` last time).
- adb: `~/Downloads/platform-tools/adb`, `adb connect 192.168.240.112:5555`.

## App
- `se.svt.melodifestivalen` v10.4.0 (60385), React Native, plain-JS bundle
  (not Hermes), bundle extracted to `bundle/assets/index.android.bundle` (11.2 MB),
  base.apk pulled to `bundle/base.apk`.
- **`pm clear` fixed the blank UI startup hang**: after clearing app data the app runs its
  full startup pipeline (settings/state/terms/i18n/features/rankdata/themes/combined,
  firebaseinstallations — all HTTP 200). Before the clear it hung on stale stored state.
- After clear: notification permission dialog appeared (dismissed), then the app still
  shows only its purple background: uiautomator = 12 nodes (ImageView + empty ScrollView),
  zero text, zero clickables. ReactNativeJS logcat tag is silent (console stripped in
  release). No JS exceptions (Sentry sessions report `errors: 0`, no error events).
- Letterbox: portrait-locked app in 1920x1044 display → strip `[694,0]-[1227,1044]`,
  ReactRootView = 533x1044 (layout matches window, NOT cut off — it's Android letterbox).
  Waydroid display tracks the Waydroid window size; making the window portrait would
  give the app full width.

## Feature anatomy (from static bundle analysis)
- Daily system lives in **rankdata** (fetched from `{static}/rankdata`):
  `initialDay: {LOGIN:1, GET_CARD:3, FINISH_QUIZ:3, FEED_LIKE:5, SVT_PLAY:1}`,
  `daily: [LOGIN, GET_CARD, FINISH_QUIZ, FEED_LIKE, SVT_PLAY]`, `seasonal: [<season>_FAVORITE_THIS_YEAR, PRED_<comp>, VOTE_<comp>_R<round>]`.
  → "daily card" = **GET_CARD** daily action (3 pts).
- UI strings: `daily_bonus_header`, `Daglig bonus_desc_multiple`, "du har samlat
  {{count}} av {{totalCount}} kort totalt", "…ditt bonuskort!", `bonusCard`,
  feature flag **`flag-claim`**.
- GraphQL (namespaced gateway): queries `RANKgetRank(userId)`, `QUIZgetGameUpdate(userId)`;
  subscriptions `onRankUpdated`, `onGameUpdated`. **No rank/card mutation in the bundle**
  → action submission goes elsewhere (likely REST, unconfirmed).
- Achievements service: settings `achievements: https://user-achievements.prod.uno.svt.se`,
  bundle contains path fragment `/users/me/achievements/melodifestivalen` and
  `submitUnlockAchievement`, `getCardTrades`, `getCardStyle`.
- Card content path fragment: `<hex-id>/cards/` (content API).
- GraphQL/chat/feed mutations present: Create*/Update*/Delete*/SetUserRead (FEED/CHAT/GROUP
  namespaces) — these WILL show real request shapes once logged in.

## Settings hosts (from `{static}/settings`)
static d9949q48n87u.cloudfront.net · login dxfkhq5yu4kt8.cloudfront.net ·
vote d2xumsact335c1.cloudfront.net · prediction du34s5xeyr2l9 ·
friends d388twhyp0nnvg · account d1ntothrik00gg · chat d30gqlswz5gmfr ·
groups ec28jtlqp8.execute-api (prod) · allstars eqhdw40qtf.execute-api ·
cascade wss://vprfzy74pg.execute-api/dev · **uno https://auth.prod.uno.svt.se/authentication/v5** ·
amigo amigo.prod.uno.svt.se/amigo/v3 · achievements user-achievements.prod.uno.svt.se ·
quiz 1wuiiry273.execute-api (prod) · content content.m17.se ·
terms https://www.svt.se/duo/anvandarvillkor?json=1 · swish · survey.

## Season context
- Current season = 54 (Melodifestivalen 2026, aired 2026-01-31..2026-03-07).
  Today = 2026-09-27 → **off-season**. `/54/combined` returns full lineup (69 KB),
  so content exists; unknown whether daily-bonus UI is season-gated.

## Auth / open questions
- Per-user APIs take `$userId` + need UNO session token → sign-in required.
- Blank-home root cause still open (see "blank UI" above).

---

# FINAL: daily-card claim API (2026-09-27, verified live)

## Endpoint
```
POST https://eqhdw40qtf.execute-api.eu-west-1.amazonaws.com/production/get
```
(settings `allstars` host — obtained at runtime from `{static}/settings`, not hardcoded.)

Headers, verbatim from capture:
```
cache-control: no-cache, no-store, must-revalidate
cache: no-store
pragma: no-cache
authorization: Bearer <UNO accessToken (RS256 JWT)>
expires: 0
user-agent: Melodifestivalen/android/10.4.0/60385
content-type: application/json
```
Body: `{"album": "<album id, e.g. 23>"}` (content-length 14)

Response:
```json
{"data":{"cards":["UOuC:0","YnME:0","EXTW:0","xIVz:0"],
          "next":1790521200,
          "version":{"album":2470,"theme":59},
          "streak":{"bonus":1}}}
```
- `cards` = cards in the current window (`code:count`); `null` when the window is spent.
- `next` = epoch when the next claim window opens (top of the hour, UTC).
- Claiming also grants +3 rank points (action `GET_CARD`), delivered to the app as a
  push on the AppSync subscription `onRankUpdated` (observed: rank 11, score 785).
- Card images then load from `https://svt-hof.s3.eu-west-1.amazonaws.com/<CODE>_big_v10.jpg`.

## Rate limiting (important)
One claim per hour window. Extra attempts → **HTTP 429** `{"type":"TOO_MANY_REQUESTS"}`
with no `Retry-After` header. `next` is the authoritative retry time.
Repeated presses in-window all returned 200 but only the first granted a new card;
the rest returned `cards: null` / 429.

## UNO auth (SVT Konto) — passwordless email code + PKCE
1. `POST {uno}/login/email`
   headers: `x-device-id: <8 chars>`, `x-uno-client: melodifestivalen-android`
   body: `{"email":..., "challenge": base64url(sha256(verifier)) url-encoded, "challengeMethod":"S256"}`
   → `{"message":"email sent"}`
2. user reads 6-digit code from their mail
3. `POST {uno}/login/email/confirm`
   body: `{"email":..., "code":"193895", "challengeVerifier": verifier}`
   → `{"accessToken": <RS256 JWT, 4h exp, sub=QytLmrbhD>, "refreshToken": ...}`
   access token claims: `{terms:4, client_id:"melodifestivalen-android", purpose:"at", iss:"uno"}`
4. (app also calls) `POST {uno}/token/id` with `authorization: Bearer <accessToken>`, body `{}` → `idToken`
5. `POST {uno}/token/refresh` exists in the bundle for renewal (path confirmed, not exercised).

GraphQL (AppSync): `https://72qtqhwtarbo7bwbsxmorfvzmy.appsync-api.eu-west-1.amazonaws.com/graphql`
— `RANKgetRank(userId)` returned `{userId, rank:{rank,score}}` as nulls over plain HTTPS;
live rank data arrives via the websocket instead. Subscriptions observed: `onRankUpdated`,
`onGameUpdated`, `onAnnouncement`, `OnCreateUserFeedPost`, `OnCreateGlobalPost`,
`OnDeleteGlobalPost`.

## Deliverable
`claim_daily.py` — reuses a cached token (`token.json`, chmod 600) or does the full
email-code PKCE login, then POSTs the claim. Exit codes: 0 ok, 1 auth/other error,
2 window already used (429). Flags: `--login`, `--email`, `--album`, `--check`, `--token`.

NOTE: `token.json` currently holds a live token for the user's own account, valid until
~17:51 UTC. Treat as a secret.

