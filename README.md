# melo-mitm-claimer 🎵

Automatisk daglig kort-samling för **Melodifestivalen-appen** (SVT). Körs på din NAS (eller valfri Docker-host) med auto-uppdaterande token, timvis claim av alla 25 album, och ett snyggt webb-gränssnitt.

![Dashboard](docs/dashboard.png)

---

## Funktioner

| Kategori | Detaljer |
|----------|----------|
| **Auto-claimer** | Timvis cykel som claimar alla 25 album (1906 unika kort) med 0,5 s fördröjning |
| **Token-hantering** | PKCE-inloggning via e-postkod · access-token (4h) + refresh-token · auto-refresh varje timme när <2h kvar |
| **Webb-UI (port 8765)** | Mörkt Mello-tema · flikar: **Översikt / Galleri / Logg** · sticky header |
| **Galleri** | 25 album med omslag, namn, progress · klick = kortvy med titel, låt, sällsynthet, serie, badges |
| **Sök** | Global sök på kortnamn/artist/låt → hoppar till album & markerar kortet |
| **Dubbletter / Saknade** | Visa alla dubbletter (c > 1) · Kopiera saknade kort till urklipp (namn + album + kod) |
| **Logg** | Sammankopplad web-UI + claim-cykel-loggar (från `/data/claimer.log`) · filter / autoscroll |
| **Statistik** | Totala claims, unika koder, per-album tabell, senaste 8 claims med "nya"-märke |
| **Svenska texter** | Hela UI:t på svenska med Mello-branding (magenta→violett gradient) |

---

## Snabbstart

```bash
# 1. Klona & bygg
git clone https://github.com/<din-användare>/melo-mitm-claimer.git
cd melo-mitm-claimer
docker compose build

# 2. Skapa data-volym & starta
docker compose up -d

# 3. Öppna http://<NAS-IP>:8765
#    → Fliken "Översikt" → fyll i din SVT-e-post → "Begär inloggningskod"
#    → Kolla din e-post → ange 6-sifferskoden → "Skicka"
#    → Färdigt! Containern claimar nu automatiskt varje hel timme.
```

**Volym**: `./data` → `/data` (token.json, stats.json, claimer.log, albums.json, info/*.json)

---

## API-endpoints (intern)

| Endpoint | Beskrivning |
|----------|-------------|
| `GET /api/token` | Token-status + expiry |
| `POST /api/refresh` | Tvinga token-refresh nu |
| `GET /api/collection` | Alla album + owned/total + progress |
| `GET /api/collection/<id>` | Ett album: kort med titel, låt, rarity, badges, bilder (`?sort=num|rarity|name|owned`) |
| `GET /api/cards` | Alla 1906 kort kompakt (titel, låt, rarity, owned, album) |
| `GET /api/stats` | Total claims, unika, per-album, history |
| `GET /api/log` | Sammankopplade loggar (UI + claim-cykel) |
| `POST /api/claim` | `{album, check?}` – claima ett album |
| `POST /api/claim-all` | `{check?}` – claima alla |
| `POST /api/login/request-code` | `{email}` – skicka inloggningskod |
| `POST /api/login/submit-code` | `{session_id, code}` – verifiera kod |

---

## Arkitektur

```
┌─────────────────────────────────────────────────────────────┐
│  Docker container (melo-mitm-claimer)                       │
│  ┌──────────────────┐  ┌────────────────────────────────┐  │
│  │ entrypoint.sh    │  │ webui.py (Flask, port 8765)    │  │
│  │  • startar webui │  │  • /api/* endpoints            │  │
│  │  • väntar på     │  │  • serverar template.html      │  │
│  │    token/EMAIL   │  │  • cache: albums.json (24h)    │  │
│  │  • timvis cykel  │  │  • cache: info.<id>.json (24h) │  │
│  │    run_claim_    │  │  • prefetch info-filer i bakg. │  │
│  │    cycle()       │  │  • loggar till /data/claimer.  │  │
│  │  • auto-refresh  │  │    log (rot 1 MB)              │  │
│  │    token <2h     │  │                                 │  │
│  └────────┬─────────┘  └──────────────┬─────────────────┘  │
│           │                           │                    │
│           ▼                           ▼                    │
│  ┌─────────────────────────────────────────────────────┐   │
│  │ /data volume (host: ./data)                         │   │
│  │  token.json, stats.json, claimer.log,               │   │
│  │  albums.json, info/1..25.json                       │   │
│  └─────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
         │                    │
         ▼                    ▼
  SVT UNO Auth          SVT Allstars API
  (PKCE email code)     (claim, usercardscount)
         │                    │
         ▼                    ▼
  auth.prod.uno.svt.se  eqhdw40qtf.execute-api...
                        + S3 (albums.json, info.<id>.json,
                          kortbilder _thumb_v{10-13}.jpg)
```

---

## Konfiguration (env-variabler)

| Variabel | Default | Beskrivning |
|----------|---------|-------------|
| `TOKEN_FILE` | `/data/token.json` | Var token sparas |
| `STATS_FILE` | `/data/stats.json` | Statistik-fil |
| `LOG_FILE` | `/data/claimer.log` | Claim-cykel logg (rot 1 MB) |
| `ALBUMS` | `1..25` | Vilka album att claima (t.ex. `1,2,5..10`) |
| `DELAY` | `0.5` | Sekunder mellan album-claims |
| `EMAIL` | *(tom)* | Om satt: auto-login vid första start |
| `LOG_LEVEL` | `INFO` | `DEBUG`/`INFO`/`WARN`/`ERROR` |
| `UI_LOG` | `/data/claimer.log` | Vart entrypoint skriver UI-logg |

---

## Säkerhet / varningar

> ⚠️ **Viktigt**
> - Webb-UI:t (port 8765) har **ingen autentisering** – exponera inte mot internet utan VPN/SSH-tunnel/Tailscale.
> - `token.json` innehåller access+refresh tokens (4h/∞) – skydda filen (chmod 600 görs automatiskt).
> - Endast riktiga e-postdomäner accepteras av SVT (t.ex. `gmail.com`, `hotmail.com` – inte `example.com`).
> - Rate-limit: 1 claim/album/timme (429 om man försöker igen i samma fönster).

---

## Utveckling / felsökning

```bash
# Loggar
docker logs -f melo-mitm-claimer        # entrypoint + claim-cykel + webui
docker exec melo-mitm-claimer cat /data/claimer.log   # samlad logg

# Manuell claim (test)
docker exec melo-mitm-claimer python3 /app/claim_daily.py --token-file /data/token.json --album 1 --check

# Token refresh manuellt
curl -X POST http://localhost:8765/api/refresh

# Healthcheck (körs var 30 min av docker)
docker exec melo-mitm-claimer python3 /app/claim_daily.py --token-file /data/token.json --check --album 1
```

---

## Tack / källor

- SVT / Melodifestivalen-appen för API:et
- `mitmproxy` + Frida för API-upptäckt
- `python3`, `Flask`, `Docker`

---

## Licens

MIT License – se [LICENSE](LICENSE).
