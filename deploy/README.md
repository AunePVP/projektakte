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
sudo chown 10001:10001 benutzer.txt && sudo chmod 600 benutzer.txt       # nur für die App (uid 10001) lesbar
# Caddyfile: Hostnamen eintragen; compose.yaml: Pfade der Projektordner und Mails anpassen
docker compose up -d
docker compose ps        # beide Dienste "healthy" bzw. "running"
```

## Projektordner vom Dateiserver (SMB)

Projektakte spricht selbst kein SMB. Der Server bindet die Freigabe als Ordner ein (nur lesend), Docker
reicht diesen Ordner nur lesend in den Container. Für Projektakte ist es dann ein normaler Ordner.

**1. Lesekonto anlegen.** Ein eigenes Windows-/AD-Konto, z. B. `svc-projektakte`, mit **nur
Leserechten** auf die Projektfreigabe (und ggf. die Mail-Ablage). Kein persönliches Konto, kein
Schreibrecht – Projektakte verändert nie etwas, das Konto soll es auch nicht können.

**2. Zugangsdaten auf dem Server hinterlegen** (nur für root lesbar):

```bash
sudo apt install cifs-utils
sudo install -m 600 /dev/null /etc/projektakte-smb.cred
sudo nano /etc/projektakte-smb.cred
```

```
username=svc-projektakte
password=…
domain=FIRMA
```

**3. Freigabe dauerhaft einbinden** – eine Zeile in `/etc/fstab` (Servername und Freigabe anpassen):

```
//dateiserver/Projekte  /srv/projekte  cifs  ro,credentials=/etc/projektakte-smb.cred,vers=3.0,iocharset=utf8,uid=10001,gid=10001,file_mode=0440,dir_mode=0550,_netdev,nofail,x-systemd.automount  0  0
```

| Option | Wozu |
|---|---|
| `ro` | nur lesend eingebunden – zweite Sicherung neben dem Lesekonto |
| `vers=3.0` | SMB 3 (bei neueren Windows-Servern auch `3.1.1`); mit `seal` zusätzlich verschlüsselt |
| `iocharset=utf8` | Umlaute in Ordner- und Dateinamen |
| `uid=10001,gid=10001`, `file_mode`, `dir_mode` | die Anwendung läuft im Container als Benutzer 10001 und darf lesen, sonst niemand |
| `_netdev,nofail,x-systemd.automount` | der Server startet auch, wenn der Dateiserver gerade nicht erreichbar ist; eingebunden wird beim ersten Zugriff |

```bash
sudo mkdir -p /srv/projekte
sudo systemctl daemon-reload
ls /srv/projekte | head          # bindet über automount ein und zeigt die ersten Ordner
```

**4. In den Container durchreichen.** `compose.yaml` bindet `/srv/projekte` nur lesend als
`/quellen/projekte` ein, mit `propagation: rslave`. Das ist nötig, weil die Freigabe auf dem Server oft
erst *nach* dem Container eingebunden wird (automount, Neustart des Dateiservers) – ohne diese
Einstellung sähe der Container nur den leeren Einhängepunkt. Für eine Mail-Ablage als `.eml`-Dateien
gilt dasselbe mit `/srv/mails`; mit plMail den Eintrag für `/srv/mails` aus `compose.yaml` entfernen oder
einen leeren Ordner anlegen (Docker legt fehlende Quellordner in dieser Schreibweise nicht selbst an).

**5. Projektnummern und Ordnerstruktur einstellen.** Projektakte erkennt Projekte an Ordnern, deren
Name mit einer Projektnummer beginnt (`2024-017 Kita Regenbogen`, `BS-2024-0137 Grundschule …`); was
darin liegt, wird mit allen Unterordnern gelesen, egal wie sie heißen. Wie eure Nummern aussehen und
auf welcher Ebene die Projektordner liegen (direkt in der Freigabe, unter Jahresordnern, unter
Kundenordnern), lässt sich auf zwei Wegen festlegen:

*a) Von Hand, mit Hilfe des Prüfbefehls* (empfohlen, wenn ihr euer Schema kennt). Der Befehl liest nur
Ordner- und Dateinamen, nie Inhalte, und verändert nichts:

```bash
docker compose exec app python -m projektakte.structure
```

Er nennt das erkannte Nummernschema in Worten und als Ausdruck, die Ebene, die gefundenen Projekte mit
Beispielen und Fundorten, Ordner ohne Nummer (z. B. `Archiv`, `_Vorlagen`, `00_Allgemein`), weitere
Nummernschemata (z. B. Angebote `AN-2024-001` – nie automatisch als Projekte), Dateitypen, die
Projektakte nicht liest (`.doc`, `.xlsx`, `.msg`, `.dwg` …), und gibt die Zeilen für
`projektakte.env` aus (`PROJECT_NUMBER_PATTERN`, `PROJECT_FOLDER_DEPTH`). Exit-Code 0 = Schema
gefunden, 1 = kein eindeutiges Schema (mit Begründung und den nächsten Kandidaten), 2 = Ordner nicht
lesbar, 3 = falscher Aufruf. Mit vorhandener Datenbank zeigt er außerdem je Projekt die Textmenge in
Token und ob das Projekt ganz in ein Kontextfenster von 32k/128k/1M passen würde.

*b) Vorschlag mit Bestätigung:* `PROJECT_NUMBER_PATTERN=auto`. Projektakte erkennt Schema und Ebene
selbst, **liest aber nichts als Projekt, bevor eine Person den Vorschlag bestätigt hat**. Die
Statusseite zeigt unter „Projektordner“ die Belege – Schema, Ebene, Anzahl, Fundorte, Beispiele,
Ausnahmen, weitere Schemata und „Bitte prüfen“-Hinweise – und die Knöpfe „Übernehmen“ und „Ablehnen“;
alternativ `python -m projektakte.structure --uebernehmen` (bestätigt, was der Befehl gerade
ausgegeben hat). Gespeichert wird, wer wann bestätigt hat. Bis dahin pausieren Projekte, Dokumente und
Mail-Zuordnung („Vorschlag zur Projektstruktur wartet auf Bestätigung“). Eine bestätigte Struktur
ändert sich nie von selbst: Ergibt eine spätere Prüfung (höchstens stündlich, oder „Ordner jetzt neu
prüfen“) etwas anderes, erscheint das als neuer Vorschlag daneben, die bestätigte Struktur gilt weiter.
Warum mit Bestätigung: Aus Ordnernamen allein lässt sich nicht in jedem Fall sicher erkennen, was ein
Projekt ist (Prüfberichtsordner in Projekten, Kundennummern, Angebote im gleichen Format) – ein
falscher Vorschlag fällt an den Beispielen sofort auf, eine stille Automatik würde Projekte
unbemerkt verlieren.

Die beiden ausgegebenen Zeilen zusammen übernehmen – oder `PROJECT_NUMBER_PATTERN=auto` **ohne**
`PROJECT_FOLDER_DEPTH`: Mit `auto` gilt immer die Ebene des bestätigten Vorschlags (genau das, was die
Belege zeigten); eine daneben eingestellte Ebene wird nicht verwendet und beim Start gemeldet. Ist die
Freigabe plötzlich leer, gilt sie als nicht eingebunden: Vorschlag und bestätigte Struktur bleiben.

Namen mit falscher Zeichenkodierung (Freigabe ohne `iocharset=utf8` eingebunden): Ordner liest
Projektakte unter ihrem echten Namen und zeigt sie mit � an; Dateien mit solchen Namen werden
übersprungen (nicht gelesen) und auf der Statusseite gemeldet. Bereits eingelesene Dokumente mit
Umlauten bleiben in diesem Fall im Index, statt nach dem Korrigieren der Einbindung neu gelesen
werden zu müssen. Die Freigabe dann wie oben mit `iocharset=utf8` einbinden.

**6. Wenn die Freigabe fehlt.** Projektakte löscht dann nichts: Fehlt der Ordner oder ist er leer,
obwohl Projekte bekannt sind, bleiben Projekte und Dokumente unverändert, die Statusseite warnt
(„Laufwerk nicht eingebunden?“). Fehlt nur ein einzelner Projektordner, verschwinden seine Dateien
sofort aus Suche und Antworten, gelöscht werden sie erst nach 24 Stunden und 3 Läufen – kommt er
vorher zurück, ist alles ohne erneutes Lesen wieder da.

**7. Netzlast.** Der erste Lauf liest jede Datei einmal über das Netz (bei großen Freigaben am besten
nachts oder am Wochenende starten). Danach prüft jeder Lauf nur Größe und Änderungszeit der Dateien
und liest allein geänderte oder neue Dateien. Bei sehr großen Freigaben (über 100 000 Dateien)
`INDEX_INTERVAL` erhöhen, z. B. auf 900 s.

## Benutzerkonten: eines je Person

Jede Person bekommt ein eigenes Konto (`python -m projektakte.passwd NAME`, eine Zeile je Person in
`benutzer.txt`), Skripte und Überwachung ein eigenes technisches Konto (z. B. `monitor`). Kein
gemeinsames Konto für mehrere Personen – auch nicht für eine Vorführung: **Abmelden beendet alle
Sitzungen dieses Kontos auf allen Geräten** (so gewollt – ein kopierter oder auf einem fremden
Rechner vergessener Cookie wird damit wertlos; die Anmeldeseite meldet „Alle Sitzungen dieses Kontos
sind beendet“). Melden sich alle mit demselben Konto an, fliegen beim Abmelden einer Person alle
anderen mit heraus. Für eine Vorführung also je Teilnehmer ein Konto anlegen (die Zeilen danach
wieder aus `benutzer.txt` löschen) oder vorab ankündigen, dass die vortragende Person sich erst am
Ende abmeldet. Eigene Konten halten auch die Anmeldebremse (siehe unten) und das Log nachvollziehbar.

`APP_ENV=production` (Standard im Image) prüft beim Start: kein `AUTH_DISABLED`, ein zufälliger
`SECRET_KEY` mit mindestens 32 Zeichen, beschreibbares `DATA_DIR`, mindestens ein Benutzer und eine
lesbare `AUTH_USERS_FILE` (die Anwendung läuft als Benutzer-ID 10001 – eine Datei mit `chmod 600`,
die root gehört, kann sie nicht lesen). Andernfalls bricht der Start mit einer deutschen Meldung im
Log ab (`docker compose logs app`). Cookies werden in `production` nur über
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

Stimmt `FORWARDED_ALLOW_IPS` nicht (oder sitzen alle hinter einem Terminalserver bzw. NAT), haben
alle Clients dieselbe Adresse. Die Anmeldung bleibt trotzdem für alle möglich:

- Fehlversuche mit *anderen* Benutzernamen von dieser Adresse (Namen durchprobieren, Tippfehler der
  Kollegen) reihen die Anmeldeversuche dieser Adresse nur in eine Warteschlange (Abstand höchstens
  2 s). Wird sie länger als 20 s – jemand flutet von dieser Adresse mit vielen parallelen Anfragen –,
  bekommen weitere Versuche ohne Passwortprüfung 503 mit `Retry-After: 5`.
- **Vorrang:** Ein vorhandener Benutzername, für den von dieser Adresse in den letzten 15 Minuten kein
  Fehlversuch kam, wird sofort geprüft – an der Warteschlange vorbei. Mit richtigem Passwort kommt
  man so auch während einer Flut ohne Wartezeit hinein (Browser und HTTP Basic). Ein falsches
  Passwort wird dort genauso beantwortet wie in der Warteschlange (gleiche Meldung, gleiche
  Wartezeit, gleiches 503) – die Warteschlange verrät also nicht, welche Namen es gibt.
- Wer sich selbst vertippt hat, verliert den Vorrang bis zur nächsten erfolgreichen Anmeldung bzw.
  für 15 Minuten und stellt sich während einer Flut hinten an (bis zu 20 s oder 503). Dasselbe gilt,
  wenn jemand von dieser Adresse gezielt falsche Passwörter für diesen Namen schickt.
- Gesperrt – mit wachsender Wartezeit, höchstens 5 Minuten, 429 – wird nur ein Benutzername, für den
  falsche Passwörter kamen: ab 5 Fehlversuchen von derselben Adresse (über das Formular *oder* HTTP
  Basic), ab 10 von beliebigen Adressen (nur für das Formular; HTTP Basic wird dann nur verzögert).
- Ein Browser, mit dem sich der Benutzer schon einmal angemeldet hat (Geräte-Cookie), ist von all
  dem ausgenommen und wird nur durch Fehlversuche in diesem Browser gebremst.
- Skripte und Überwachung mit HTTP Basic deshalb mit einem eigenen Benutzer betreiben, dessen Namen
  niemand im Formular eintippt: Fehlversuche für genau diesen Namen von genau dieser Adresse sperren
  ihn dort.

## Zeitlimits (`EMBED_TIMEOUT`, `RERANK_TIMEOUT`, `LLM_TIMEOUT`)

Eine Frage wird synchron beantwortet. Dabei laufen bis zu **4 Suchen** nacheinander – die Frage
selbst, bei Zusammenfassungen eine Überblickssuche und bei genannten Beteiligten („Was hat der
Bauherr geschrieben?“) je eine Suche für bis zu zwei Rollen. Jede Suche wartet auf den
Embedding-Dienst und den Reranker, am Ende kommt das Sprachmodell.

**Gesamtbudget je Frage: `ASK_DEADLINE` (Standard 180 s).** Kein Aufruf eines KI-Dienstes läuft
über dieses Budget hinaus: Für das Sprachmodell bleibt `LLM_TIMEOUT`, höchstens aber die Hälfte des
Budgets reserviert; die Suchen (die Frage selbst, Überblick, Beteiligte) teilen sich den Rest. Jeder
Aufruf bekommt höchstens die verbleibende Zeit – gemessen als Gesamtdauer, auch wenn ein Dienst seine
Antwort nur tröpfchenweise schickt, und eine Formaterkennung mit mehreren Versuchen bleibt zusammen
in dieser Zeit. Reicht die Zeit für einen Aufruf nicht mehr, sucht die Anwendung ohne ihn weiter (nur
Volltext bzw. ohne Reranker). Die Antwort trägt dann einen Hinweis „Zeitbudget der Frage … erreicht –
nicht abgewartet: Vektorsuche, Reranker …“; reicht es nicht mehr für das Sprachmodell, meldet sie
„Das Zeitbudget der Frage ist aufgebraucht …“ und zeigt die gefundenen Stellen. Werte unter 10 s hebt
die Anwendung auf 10 s an; ungültige Zeitlimits (0, negativ, keine Zahl) ersetzt sie durch den
Standardwert. Beides und ein `LLM_TIMEOUT`, das nicht unter `ASK_DEADLINE` liegt, meldet sie beim Start
als Warnung (`docker compose logs app | grep Konfiguration`). Ohne Budget
(`ASK_DEADLINE=0`) dauert eine Frage höchstens `4 × (EMBED_TIMEOUT + RERANK_TIMEOUT) + LLM_TIMEOUT`
(Standard 4 × (60 + 30) + 120 = 480 s). `python -m projektakte.evaluate --real` misst mit demselben
Budget und nennt die Fragen, bei denen es erreicht wurde.

Die Startzeile im Log nennt den Wert für die tatsächlichen Einstellungen („längste Frage bis … s“,
`docker compose logs app | grep Konfiguration`). Das Zeitlimit des Proxys muss darüber liegen (plus
Reserve), sonst bricht er mit 504 ab und der Nutzer sieht statt der gefundenen Stellen eine
Fehlerseite. Caddy: `response_header_timeout 240s` (im Beispiel gesetzt, passend zu
`ASK_DEADLINE=180`). Wer `ASK_DEADLINE` erhöht, erhöht auch das Zeitlimit des Proxys. Manche Browser
brechen eine Anfrage nach 5 Minuten selbst ab (Firefox: 300 s) – deshalb `ASK_DEADLINE` unter 300 s
halten.

nginx hat standardmäßig 60 s – dort:

```nginx
location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_set_header Host $host;                       # sonst lehnt die App Formulare als fremd ab
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_read_timeout 240s;                           # > längste Frage (Startzeile; ASK_DEADLINE=180 s)
}
```

Läuft nginx direkt auf dem Server und die Anwendung in Docker (`-p 127.0.0.1:8000:8000`), kommen
die Anfragen nicht von `127.0.0.1`, sondern von der Gateway-Adresse des Docker-Netzes (Standardnetz:
`172.17.0.1`; nachsehen mit `docker network inspect bridge -f '{{range .IPAM.Config}}{{.Gateway}}{{end}}'`,
bei Compose den Netznamen statt `bridge`). Dann `FORWARDED_ALLOW_IPS=172.17.0.1` setzen – sonst
sieht die Anwendung alle Clients als eine Adresse und meldet auf der Anmeldeseite fälschlich
„unverschlüsseltes HTTP“. Den Port dabei nur an `127.0.0.1` binden (nicht `-p 8000:8000`), denn
alles, was über diesen Port hereinkommt, gilt als vertrauenswürdiger Proxy. Alternativ die
Anwendung mit `--network host` betreiben; dann passt der Standard `127.0.0.1`.

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
  Die Abfrage kostet unabhängig von der Datenmenge nur Millisekunden (kein Tabellenscan);
  `pending_vectors` ist der Stand nach dem letzten Indexierungslauf (`null` vor dem ersten).
- Das Zugriffsprotokoll enthält keine Query-Strings (also keine Suchbegriffe) und keine Dateinamen:
  Aus `/dokumente/12/datei/Protokoll Meier.pdf` wird `/dokumente/12/datei/` (Dateinamen von
  Mailanhängen enthalten oft Personennamen). Erfolgreiche Health-Abfragen werden nicht protokolliert.
  Logs werden im Beispiel auf 5 × 10 MB begrenzt; sie enthalten Benutzernamen und Adressen.
- Caddy schreibt kein Zugriffsprotokoll, aber Proxy-Fehler (502/504, etwa während die Anwendung
  neu startet) mit der Anfrage. Das `Caddyfile` kürzt dort die Adresse um Query-String und Dateinamen
  und lässt die Request-Header weg (`log default` mit `format filter`). Wer im Caddyfile ein
  Zugriffsprotokoll (`log` im Site-Block) einschaltet, muss denselben Filter dort eintragen.
- Die Startzeile im Log nennt Version und wirksame Konfiguration (ohne Geheimnisse):
  `docker compose logs app | grep Konfiguration`.
