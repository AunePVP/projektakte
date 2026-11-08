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

## Umsetzung in Projektakte (Roadmap-Schritt 14)

- Code: `projektakte/mails/jmap_source.py`; Konfiguration `PLMAIL_URL`, `PLMAIL_TOKEN`,
  `PLMAIL_TIMEOUT`, `PLMAIL_PAGE_SIZE`.
- Session über `/.well-known/jmap`, Rückfall `/jmap/session`; `apiUrl` und `downloadUrl` werden nur
  aus der Session gelesen. Alle Konten mit `urn:ietf:params:jmap:mail` werden abgeglichen.
- Erster Abgleich: `Email/query` (neueste zuerst, `position` + `limit`) mit Rückverweis auf
  `Email/get`; die Liste wird nach den `ids` der Query neu sortiert. Der `state` der ersten Seite
  wird gespeichert, damit während des Blätterns eintreffende Mails beim nächsten `Email/changes`
  kommen.
- Danach nur `Email/changes` (mit `maxChanges`, solange `hasMoreChanges`); `created`/`updated` werden
  per `Email/get` geholt, `destroyed` aus dem Index entfernt. Bei `cannotCalculateChanges` folgt ein
  vollständiger Abgleich.
- **Robuster Erstabgleich:** Die Seiten überlappen sich; danach folgt ein Abgleich nur über die IDs
  (`Email/query` ohne `Email/get`). Fehlende Mails werden nachgeholt (Statistik `recovered`),
  gelöscht wird nur, was `Email/get` als nicht vorhanden (`notFound`) bestätigt. Ein vom Server
  begrenztes `limit` wird beachtet. Grund: Ohne Anker-Paging verschiebt ein Löschen während des
  Blätterns die Positionen, sonst fehlte eine Mail dauerhaft.
- **Fehler isoliert:** Eine fehlerhafte Mail oder ein nicht ladbarer Anhang wird protokolliert,
  gezählt (`errors`) und mit wachsendem Abstand erneut versucht (15 min, verdoppelt, höchstens
  1 Tag; Wiederholliste in `meta`, Schlüssel `jmap_retry:<Konto>`). Anhänge, die plMail nicht mehr
  hat (404/410), werden übersprungen; nach 6 Fehlversuchen wird die Mail ohne den fehlenden Anhang
  übernommen. Die übrigen Mails, Embeddings und die Zuordnung laufen weiter.
- Anhänge werden vor dem Schreiben in die Datenbank heruntergeladen und extrahiert; jede Mail wird
  atomar (SAVEPOINT) gespeichert und einzeln committet.
- Reine Flag-Änderungen (gelesen/markiert) lösen keinen erneuten Anhang-Download aus: Ein
  Fingerabdruck aus Betreff, Absender, Datum, Text und Blob-IDs wird verglichen.
- `accountId` wird immer als String gesendet. Fehler auf Request-Ebene (`application/problem+json`)
  und Methodenfehler werden mit Text gemeldet, ein Konto mit Fehler hält die anderen nicht auf.
- Tests: `tests/fake_jmap.py` (Fake-Server mit Session, `Email/query`, `Email/get` in
  Datenbank-Reihenfolge, `Email/changes`, Download) und `tests/test_plmail_jmap.py`; die Tests prüfen
  auch, dass nur `Email/query`, `Email/get` und `Email/changes` aufgerufen werden.

## Vorschläge an plMail (nicht umgangen, nur notiert)

- **Push statt Abfrage**: Ein dokumentierter `eventSourceUrl` bzw. JMAP-Push würde neue Mails ohne
  periodisches `Email/changes` melden.
- **`messageId`/`inReplyTo`/`references` in `Email/get`**: würde erlauben, Antworten über Konten
  hinweg demselben Vorgang zuzuordnen. Projektakte fragt nur die dokumentierten Eigenschaften ab.
- **`Email/queryChanges`** würde das Blättern bei sehr großen Postfächern beim Erstabgleich
  entbehrlich machen (derzeit `position` + `limit` mit Überlappung und ID-Abgleich).
- **`header:List-Unsubscribe` / `header:Precedence` in `Email/get`**: Damit ließen sich Newsletter
  auch bei plMail-Mails an der Kopfzeile erkennen; bisher geht das dort nur über Absender und
  Formulierungen.
