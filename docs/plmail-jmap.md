# plMail-Anbindung über JMAP

Das Büro nutzt **plMail** (eigener Fork, Symfony/PostgreSQL), einen selbst gehosteten Mail-Client,
der mehrere IMAP-Konten synchronisiert. plMail bietet eine **JMAP-API** (RFC 8620 / RFC 8621). Das ist
die einzige stabile Schnittstelle für Fremdprogramme – **nicht** die HTML-Routen der Weboberfläche
und **nicht** direkt die Datenbank.

Zusammengefasst aus der Client-Dokumentation von plMail (`docs/CLIENT_DEVELOPMENT.md` im plMail-Repo):

## Anmeldung

- Ein Mensch legt in plMail unter **Einstellungen → App-Passwörter** ein App-Passwort an:
  `plmail_<64 Hex-Zeichen>` (wird nur einmal angezeigt).
- Senden als `Authorization: Bearer plmail_…`.
- Das Passwort gilt für **alle** Mailkonten des Benutzers (user-scoped).
- Konfiguration in Projektakte: `PLMAIL_URL` (z. B. `https://mail.example.local`),
  `PLMAIL_TOKEN` (Secret, nur aus der Umgebung).

## Session

```http
GET /.well-known/jmap          (oder GET /jmap/session)
Authorization: Bearer plmail_…
```

Liefert `apiUrl`, `downloadUrl` (Template `{accountId}/{blobId}/{name}?accept={type}`), `accounts`
und `primaryAccounts`. **Pfade immer aus der Session lesen, nie fest codieren.**
**Ein JMAP-Account pro angebundenem Mailkonto** – alle Accounts durchlaufen.

## Abfragen

```http
POST {apiUrl}
Authorization: Bearer plmail_…
Content-Type: application/json

{"using": ["urn:ietf:params:jmap:core", "urn:ietf:params:jmap:mail"],
 "methodCalls": [
   ["Email/query", {"accountId": "7", "sort": [{"property": "receivedAt", "isAscending": false}],
                    "position": 0, "limit": 100, "calculateTotal": false}, "q0"],
   ["Email/get", {"accountId": "7", "#ids": {"resultOf": "q0", "name": "Email/query", "path": "/ids"},
                  "properties": ["id", "threadId", "subject", "from", "to", "cc", "receivedAt",
                                 "preview", "hasAttachment", "attachments", "bodyValues", "textBody",
                                 "htmlBody"], "fetchTextBodyValues": true}, "g0"]]}
```

Fallstricke (laut plMail-Doku):

- `accountId` **muss ein String** sein, eine Zahl wird abgelehnt.
- `Email/get` liefert die Liste **in Datenbank-Reihenfolge**, nicht in der angefragten – nach den
  `ids` der Query neu sortieren.
- Kein `Email/queryChanges`, kein Anker-Paging: mit `position` + `limit` blättern; für neue Mails
  `Email/changes` mit dem gespeicherten `state` nutzen.
- Anhänge über `downloadUrl` mit der `blobId` laden.
- Fehler auf Request-Ebene kommen als `application/problem+json` (Status 400).

## Grundsätze für Projektakte

- **Nur lesen.** Kein `Email/set`, kein `EmailSubmission/set` – Projektakte ändert und sendet nichts.
- Inkrementell: `state` je Account speichern, danach nur `Email/changes`.
- Tests gegen einen kleinen **Fake-JMAP-Server** (Session + `Email/query` + `Email/get` +
  `Email/changes` + Download), nie gegen ein echtes plMail.
- Fehlt plMail eine Funktion, die Projektakte braucht, nicht umgehen (kein Scraping der Weboberfläche),
  sondern als Vorschlag notieren – plMail wird von Menschen gepflegt.
