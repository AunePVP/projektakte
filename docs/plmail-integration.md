# Mails in plMail einem Projekt zuordnen – Schnittstellenvertrag

Mails werden **nicht automatisch** Projekten zugeordnet. Ein Mensch wählt das Projekt in plMail,
während er die Mail liest. plMail speichert diese Entscheidung und gibt sie über seine JMAP-API
heraus; Projektakte liest sie beim normalen Abgleich (`Email/changes`) und ordnet die Mail samt
Anhängen dem Projekt zu. Projektakte schreibt nie etwas in plMail.

```
   Mensch liest Mail in plMail
            │  wählt Projekt (Vorschlag vorausgewählt)
            ▼
 ┌──────────────────────┐   GET  /api/integration/projects    ┌──────────────────┐
 │ plMail (Server)      │──────────────────────────────────────▶│ Projektakte      │
 │  speichert Zuordnung │   POST /api/integration/suggest      │  Projektliste,   │
 │  pro Mail            │──────────────────────────────────────▶│  Vorschläge      │
 │                      │   POST /api/integration/notify       │                  │
 │                      │──────────────────────────────────────▶│  (sofort syncen) │
 │  JMAP: Email-Eigen-  │◀──────────────────────────────────────│  JMAP-Client,    │
 │  schaft              │   Email/changes + Email/get           │  nur lesend      │
 │  "plmail:projektakte"│   (mit App-Passwort)                  │                  │
 └──────────────────────┘                                       └──────────────────┘
```

Wahrheit über die Zuordnung ist **plMail**. Projektakte ist eine lesende Kopie; geht der Index von
Projektakte verloren, wird die Zuordnung beim nächsten Abgleich wiederhergestellt.

## Teil 1 – Projektakte-API, die plMail aufruft

Alle Aufrufe **serverseitig** aus plMail (nie aus dem Browser – das Token darf nicht zum Client),
mit `Authorization: Bearer <INTEGRATION_TOKEN>`. Das Token gilt nur für `/api/integration/*`.
Basis-URL z. B. `https://projektakte.lan` (über den TLS-Proxy – das Token nie über unverschlüsseltes
HTTP schicken). Antworten sind JSON, Fehler `{"detail": "…"}` mit
HTTP-Status (401 falsches/fehlendes Token, 400 ungültige Anfrage).

### `GET /api/integration/info`

Für die Statusanzeige in plMail.

```json
{"name": "Projektakte", "version": "0.4.0", "assign_mode": "manual", "projects": 8,
 "jmap": {"capability": "urn:plmail:params:jmap:projektakte", "property": "plmail:projektakte"},
 "url": "https://projektakte.lan"}
```

### `GET /api/integration/projects?q=&limit=500`

Aktive Projekte für die Projektauswahl, neueste Nummer zuerst; `q` filtert nach Nummer oder Name.
plMail sollte die Liste einige Minuten zwischenspeichern.

```json
{"count": 8, "projects": [
  {"number": "BS-2025-0019", "name": "Kita Regenbogen", "url": "https://projektakte.lan/projekte/BS-2025-0019"},
  {"number": "BS-2024-0137", "name": "Grundschule Am Lindenhof", "url": "…/projekte/BS-2024-0137"}]}
```

### `POST /api/integration/suggest`

Vorschläge für die gerade gelesene Mail. Speichert nichts, ordnet nichts zu.

```json
{"subject": "AW: Termin Außentreppe", "body": "Text der Mail (max. 20 000 Zeichen)",
 "from": "feld@nordlicht-architekten.example", "attachments": ["Plan_EG.pdf"], "limit": 3,
 "headers": {"List-Unsubscribe": "<mailto:…>", "Precedence": "bulk"}}
```

`headers` ist optional; ausgewertet werden nur `List-Unsubscribe` und `Precedence`. Newsletter und
Werbung (an diesen Kopfzeilen, an Massenabsendern wie `newsletter@`/`noreply@` oder an
Werbeformulierungen erkannt; Absender mit Projekthistorie gelten nie als Werbung) bekommen keine
inhaltlichen Vorschläge; eine Projektnummer bleibt `sicher`. Inhaltliche Vorschläge brauchen
mindestens 3 gemeinsame Fachbegriffe mit dem Projekt, weitere Projekte (`niedrig`) mindestens 2.

```json
{"suggestions": [
  {"number": "BS-2024-0137", "name": "Grundschule Am Lindenhof", "confidence": "sicher",
   "score": 1.0, "reason": "Projektnummer BS-2024-0137 im Betreff", "url": "…/projekte/BS-2024-0137"},
  {"number": "BS-2025-0019", "name": "Kita Regenbogen", "confidence": "niedrig",
   "score": 0.18, "reason": "18% der ähnlichsten Abschnitte", "url": "…"}]}
```

`confidence`: `sicher` (Projektnummer in Betreff, Text oder Anhangname), `hoch` (klarer inhaltlicher
Vorschlag), `niedrig` (weitere Kandidaten). Leere Liste = kein Vorschlag. plMail darf den ersten
Vorschlag **vorauswählen**, aber nie ohne Klick zuordnen.

### `POST /api/integration/notify`

Nach jeder Zuordnung/Änderung aufrufen (Antwort `202`, Körper wird ignoriert). Projektakte gleicht
dann sofort ab statt erst nach `INDEX_INTERVAL`. Schlägt der Aufruf fehl, ist nichts verloren – der
nächste reguläre Abgleich holt die Änderung.

## Teil 2 – JMAP-Erweiterung, die plMail bereitstellt

### Capability

In der Session (`/.well-known/jmap` bzw. `/jmap/session`) unter `capabilities` **und** in
`accounts[*].accountCapabilities`:

```json
"urn:plmail:params:jmap:projektakte": {}
```

### Email-Eigenschaft `plmail:projektakte`

Wird bei `Email/get` nur geliefert, wenn der Client sie in `properties` anfordert **und**
`urn:plmail:params:jmap:projektakte` in `using` steht (sonst Fehler `invalidArguments` bzw.
Eigenschaft weglassen, wie bei anderen unbekannten Eigenschaften). Wert:

```json
null                                             // noch nicht zugeordnet
{"status": "assigned", "project": "BS-2024-0137", "projectName": "Grundschule Am Lindenhof",
 "assignedBy": "anna", "assignedAt": "2025-03-10T09:30:00Z"}
{"status": "none", "project": null, "projectName": "",
 "assignedBy": "ben", "assignedAt": "2025-03-10T09:31:00Z"}  // bewusst „kein Projektbezug“
```

- `project`: Projektnummer genau wie von `/api/integration/projects` geliefert.
- `assignedBy`: Anzeigename oder Login des plMail-Benutzers; `assignedAt`: UTC, RFC 3339.
- **Pflicht:** Jede Änderung (zuordnen, umhängen, entfernen, „kein Projekt“) muss den `state` des
  Email-Typs des Kontos ändern und die Mail in `Email/changes` unter `updated` melden – sonst
  bemerkt Projektakte sie nicht.
- Die Zuordnung gehört zur Mail in plMail, **nicht** zum IMAP-Server (kein IMAP-Keyword/Flag).
  Sie bleibt beim Verschieben der Mail in einen anderen Ordner erhalten.
- Projektakte verlangt nichts Schreibendes; ein `Email/set` für diese Eigenschaft ist nicht nötig.

### Was Projektakte daraus macht

| Wert | Ergebnis in Projektakte |
|---|---|
| `null` / fehlt | Mail „in plMail offen“, keinem Projekt zugeordnet, nicht im Projektakte-Eingang |
| `status: assigned`, bekanntes Projekt | Mail und Anhänge gehören zum Projekt (Suche, Fragen, Zeitachse); Begründung „in plMail von anna am 10.03.2025 zugeordnet“ |
| `status: assigned`, unbekanntes Projekt | bleibt offen, Hinweis „Projekt … ist in Projektakte unbekannt“ |
| `status: none` | „ohne Projektbezug“ |

Rangfolge: Zuordnung in plMail > Entscheidung in Projektakte > automatische Regel. Mails aus plMail
lassen sich in Projektakte (mit `ASSIGN_MODE=manual`) nicht umhängen; die Mail-Seite verweist auf
plMail (Link über `PLMAIL_WEB_URL`). Bietet plMail die Capability nicht an, funktioniert der
Abgleich wie bisher, die Mails bleiben dann offen.

## Konfiguration

| Seite | Variable | Bedeutung |
|---|---|---|
| Projektakte | `PLMAIL_URL`, `PLMAIL_TOKEN` | JMAP-Zugang (App-Passwort eines plMail-Benutzers, der alle relevanten Konten sieht) |
| Projektakte | `ASSIGN_MODE` | `manual` (Standard, sobald plMail angebunden ist) oder `auto` |
| Projektakte | `INTEGRATION_TOKEN` | gemeinsames Geheimnis für `/api/integration/*` (z. B. `openssl rand -hex 32`) |
| Projektakte | `PLMAIL_WEB_URL` | Link-Vorlage zum Öffnen einer Mail in plMail, bei plMail `https://mail.lan/mail/message/{emailId}` (`{accountId}` ist ebenfalls möglich) |
| Projektakte | `PUBLIC_URL` | eigene Adresse für Links in den API-Antworten (sonst die Anfrage-URL) |
| plMail | `PROJEKTAKTE_URL`, `PROJEKTAKTE_TOKEN` | Basis-URL von Projektakte und dasselbe `INTEGRATION_TOKEN` |

## Prüfen

```bash
curl -s -H "Authorization: Bearer $INTEGRATION_TOKEN" https://projektakte.lan/api/integration/info
curl -s -H "Authorization: Bearer $INTEGRATION_TOKEN" https://projektakte.lan/api/integration/projects
curl -s -H "Authorization: Bearer $PLMAIL_TOKEN" https://mail.lan/.well-known/jmap | grep projektakte
```

Danach zeigt `GET /health/deps` in Projektakte bei plMail `"project_extension": true`.
Die Gegenseite ist in Projektakte mit einem Fake-plMail getestet (`tests/fake_jmap.py`,
`tests/test_plmail_assignment.py`).
