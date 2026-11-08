# Betrieb hinter einem TLS-Reverse-Proxy

Beispiel für den Büroserver: Projektakte und [Caddy](https://caddyserver.com) als TLS-Proxy mit
Docker Compose. Die Anwendung selbst spricht nur HTTP und ist im Beispiel nicht direkt erreichbar;
Passwörter, Sitzungs-Cookies, Tokens und Inhalte gehen nur verschlüsselt durchs Netz.

## Einrichten

```bash
cd deploy
cp projektakte.env.example projektakte.env && chmod 600 projektakte.env   # SECRET_KEY usw. eintragen
docker compose build
docker run --rm -it projektakte:latest python -m projektakte.passwd anna  # je Benutzer; die ausgegebene
                                                                          # Zeile in benutzer.txt eintragen
chmod 600 benutzer.txt
# Caddyfile: Hostnamen eintragen; compose.yaml: Pfade der Projektordner und Mails anpassen
docker compose up -d
docker compose ps        # beide Dienste "healthy" bzw. "running"
```

`APP_ENV=production` (Standard im Image) prüft beim Start: kein `AUTH_DISABLED`, ein zufälliger
`SECRET_KEY` mit mindestens 32 Zeichen, beschreibbares `DATA_DIR`. Andernfalls bricht der Start mit
einer deutschen Meldung im Log ab (`docker compose logs app`). Cookies werden in `production` nur über
HTTPS gesendet (`COOKIE_SECURE` ist dort standardmäßig an).

## Zertifikat

- **Ohne öffentliches DNS (Standard im Beispiel):** `tls internal` – Caddy betreibt eine eigene
  Zertifizierungsstelle. Ihr Stammzertifikat einmal auf die Arbeitsplätze verteilen (z. B. per
  Gruppenrichtlinie):
  `docker compose cp caddy:/data/caddy/pki/authorities/local/root.crt ./projektakte-root.crt`
- **Firmenzertifikat:** Zertifikat und Schlüssel einbinden und im `Caddyfile`
  `tls /zertifikate/projektakte.crt /zertifikate/projektakte.key` setzen.

## Proxy-Vertrauen (`FORWARDED_ALLOW_IPS`)

Hinter einem Proxy sieht die Anwendung als Absender nur den Proxy. Damit die Anmeldebremse je
Rechner wirkt und im Log die echte Adresse steht, wertet uvicorn `X-Forwarded-For` und
`X-Forwarded-Proto` aus – aber nur von den Adressen in `FORWARDED_ALLOW_IPS` (Standard: nur
`127.0.0.1`). Im Beispiel hat Caddy die feste Adresse `172.31.250.10`. Nie `*` setzen, solange der
Port der Anwendung auch ohne Proxy erreichbar ist: Dann könnte jeder seine Adresse fälschen.

## Zeitlimits (`LLM_TIMEOUT`)

Eine Frage wird synchron beantwortet und darf bis `LLM_TIMEOUT` (Standard 120 s) dauern. Das
Zeitlimit des Proxys muss darüber liegen, sonst bricht er mit 504 ab und die Fundstellen gehen
verloren. Caddy: `response_header_timeout 180s` (im Beispiel gesetzt). nginx hat standardmäßig
60 s – dort:

```nginx
location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_set_header Host $host;                       # sonst lehnt die App Formulare als fremd ab
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_read_timeout 180s;                           # > LLM_TIMEOUT
}
```

## Datenverzeichnis

Die Anwendung läuft im Container als Benutzer-ID 10001. Das benannte Volume `projektakte-daten`
übernimmt die Rechte automatisch. Bei einem Bind-Mount (`-v /srv/projektakte-daten:/data`) muss das
Verzeichnis diesem Benutzer gehören: `sudo chown -R 10001:10001 /srv/projektakte-daten`. Sonst bricht
der Start mit „DATA_DIR … ist nicht beschreibbar“ ab.

## Sicherung

Nicht wiederherstellbar sind nur die menschlichen Entscheidungen im Zuordnungs-Eingang; alles andere
entsteht beim Indexieren neu. Die Sicherung läuft im Betrieb und ist konsistent (SQLite-Backup-API):

```bash
docker compose exec app python -m projektakte.backup /data/sicherungen
docker compose cp app:/data/sicherungen ./sicherungen-$(date +%F)    # vom Server wegkopieren
```

Es entstehen `projektakte-<Zeit>.sqlite3` (vollständige Datenbank) und `zuordnungen-<Zeit>.json`
(die Entscheidungen lesbar). Täglich per cron, alte Sicherungen regelmäßig löschen. Wiederherstellen:
`docker compose stop app`, die `.sqlite3`-Datei als `/data/projektakte.sqlite3` ins Volume kopieren,
dort `projektakte.sqlite3-wal` und `-shm` löschen, `docker compose start app`.

## Überwachung und Logs

- `GET /health/ready` (ohne Anmeldung, ohne Inhalte): 200 mit `status` `ok` oder `degraded`
  (Indexierung veraltet oder letzter Lauf fehlgeschlagen), 503 wenn die Datenbank nicht nutzbar ist
  oder der Indexierungs-Thread nicht läuft. Der Docker-`HEALTHCHECK` nutzt denselben Endpunkt.
- Das Zugriffsprotokoll enthält keine Query-Strings (also keine Suchbegriffe); erfolgreiche
  Health-Abfragen werden nicht protokolliert. Logs werden im Beispiel auf 5 × 10 MB begrenzt.
