# Auftrag für plMail: Mails beim Lesen einem Projekt aus Projektakte zuordnen

> Diesen Text unverändert als Auftrag an das KI-Modell im plMail-Repository
> (`AunePVP/pl_mail`) geben. Er ist auf den Stand `224a1c9` des Forks zugeschnitten und hält sich an
> dessen `CLAUDE.md`/`FORK.md`. Der Schnittstellenvertrag steht auch in Projektakte unter
> `docs/plmail-integration.md`; die Projektakte-Seite ist fertig und gegen einen nachgebauten
> plMail getestet.

---

## Worum es geht

Ich betreibe neben plMail ein zweites, selbst gebautes Werkzeug: **Projektakte**. Es ist ein internes
Werkzeug für unser Ingenieurbüro (Brandschutz). Projektakte liest die Projektordner (jedes Projekt
hat eine Projektnummer, z. B. `BS-0000-0000`) und die Mails, ordnet sie Projekten zu, indexiert
alles (Volltext + Embeddings, lokale KI-Modelle im LAN) und beantwortet Fragen zu einem Projekt mit
Quellenangaben. Projektakte ist in Python/FastAPI geschrieben, läuft als ein Container im LAN und
liest plMail **ausschließlich** über die JMAP-API mit einem App-Passwort
(`Email/query`, `Email/get`, `Email/changes`, Anhänge über `downloadUrl`). Es schreibt nie etwas
in plMail.

Bisher hat Projektakte Mails selbst über die Projektnummer im Betreff zugeordnet. Das will ich nicht
mehr: **Ein Mensch soll in plMail, während er eine Mail liest, das Projekt wählen.** plMail speichert
diese Entscheidung und gibt sie über JMAP heraus; Projektakte übernimmt sie beim nächsten Abgleich.
Projektakte liefert dafür die Projektliste und Vorschläge über eine kleine HTTP-API.

Du sollst die **plMail-Seite** bauen. An Projektakte änderst du nichts.

## Was der Nutzer sieht

1. In der Lese-Ansicht jeder Mail (im Verlauf bei jeder einzelnen Nachricht) steht im Kopfbereich
   ein kleiner Chip **„Projekt zuordnen“**. Ist die Mail schon zugeordnet, zeigt er
   **„BS-0000-0000 Projektname ×“** bzw. **„Kein Projekt“**.
2. Klick öffnet eine Auswahl (Popover, wie das Label-Menü):
   - oben bis zu 3 **Vorschläge** von Projektakte mit Begründung („Projektnummer im Betreff“,
     „62 % der ähnlichsten Abschnitte“); der erste ist vorausgewählt, aber **nichts wird ohne Klick
     zugeordnet**;
   - ein Suchfeld und darunter die Projektliste (Nummer + Name, clientseitig gefiltert);
   - ein Eintrag **„Kein Projektbezug“**.
3. Nach der Wahl aktualisiert sich der Chip sofort (Turbo Stream). Mit „×“ wird die Zuordnung
   entfernt. Umhängen auf ein anderes Projekt geht jederzeit.
4. Optional: Link „In Projektakte öffnen“ zum Projekt (URL kommt von Projektakte).
5. Ist Projektakte nicht konfiguriert, ist die Funktion unsichtbar. Ist sie konfiguriert, aber
   nicht erreichbar, zeigt der Chip eine bestehende Zuordnung weiter an; die Auswahl meldet
   „Projektakte ist gerade nicht erreichbar“ statt eines Fehlers. Eine Zuordnung ohne Projektliste
   ist nicht möglich (nur „Kein Projektbezug“ und „Entfernen“ gehen immer).
6. Optional (gern, wenn es klein bleibt): in der Mailliste ein kleines Badge mit der Projektnummer.

Die Zuordnung gilt **pro Nachricht** (nicht pro Verlauf), bleibt beim Verschieben in andere Ordner
erhalten und wird **nie** zum IMAP-/Gmail-/Graph-Server synchronisiert (kein Keyword, kein Label,
kein Flag).

## Schnittstellenvertrag (nicht abweichen – Projektakte erwartet genau das)

### A. Projektakte-API, die plMail aufruft

Nur **serverseitig** (CSP ist `connect-src 'self'`, und das Token darf nie in den Browser). Header
`Authorization: Bearer <PROJEKTAKTE_TOKEN>`, `Accept: application/json`. Fehler kommen als
`{"detail": "…"}` mit 400/401.

- `GET {PROJEKTAKTE_URL}/api/integration/info` →
  `{"name":"Projektakte","version":"0.3.0","assign_mode":"manual","projects":8,
  "jmap":{"capability":"urn:plmail:params:jmap:projektakte","property":"plmail:projektakte"},"url":"…"}`
  – für einen „Testen“-Knopf/Status in den Admin-Einstellungen.
- `GET {PROJEKTAKTE_URL}/api/integration/projects?q=&limit=500` →
  `{"count":N,"projects":[{"number":"BS-0000-0000","name":"…","url":"…/projekte/BS-0000-0000"}, …]}`
  – Projektliste (neueste zuerst). Etwa 300 s cachen, nach einem Fehler nur etwa 60 s.
- `POST {PROJEKTAKTE_URL}/api/integration/suggest` mit
  `{"subject":"…","body":"<bodyText, max. 20000 Zeichen>","from":"absender@…","attachments":["datei.pdf"],"limit":3}`
  (optional zusätzlich `"headers": {"List-Unsubscribe": "…", "Precedence": "…"}`, falls plMail diese
  Kopfzeilen kennt – dann bekommen Newsletter keine Vorschläge)
  → `{"suggestions":[{"number":"…","name":"…","confidence":"sicher|hoch|niedrig","score":1.0,
  "reason":"Projektnummer BS-0000-0000 im Betreff","url":"…"}]}`. Speichert nichts, ordnet nichts
  zu. Leere Liste = kein Vorschlag.
- `POST {PROJEKTAKTE_URL}/api/integration/notify` (Körper egal, Antwort 202) – nach **jeder**
  Änderung einer Zuordnung aufrufen, damit Projektakte sofort abgleicht. Fire-and-forget: Fehler nur
  loggen, nie dem Nutzer zeigen und nie die Zuordnung zurückrollen (der reguläre Abgleich holt sie).

### B. JMAP-Erweiterung, die plMail bereitstellt

1. **Capability** `urn:plmail:params:jmap:projektakte` (Wert `{}`) in der Session unter
   `capabilities` **und** in jedem `accounts[*].accountCapabilities`, und in
   `Capability::SUPPORTED` – sonst lehnt `JmapProcessor::assertCapabilities()` jede Anfrage von
   Projektakte mit `unknownCapability` ab, sobald sie die URN in `using` schickt. Beides gehört in
   dieselbe Änderung.
2. **Email-Eigenschaft `plmail:projektakte`** bei `Email/get`, nur wenn sie in `properties` steht
   (Muster: das vorhandene Opt-in `replyFrom` in `EmailGetMethod`). Werte:

   ```json
   null
   {"status":"assigned","project":"BS-0000-0000","projectName":"Name","assignedBy":"anna","assignedAt":"2025-03-10T09:30:00Z"}
   {"status":"none","project":null,"projectName":"","assignedBy":"anna","assignedAt":"2025-03-10T09:31:00Z"}
   ```

   `null` = noch nicht zugeordnet (auch nach „Entfernen“), `none` = bewusst ohne Projektbezug.
   `assignedBy` = Anzeigename bzw. Login des Benutzers (bleibt erhalten, wenn der Benutzer gelöscht
   wird), `assignedAt` = UTC im Format RFC 3339 mit `Z`. Für alle Nachrichten einer Antwort **in
   einer Abfrage** laden (kein N+1).
3. **Pflicht:** Jede Änderung (zuordnen, umhängen, „kein Projekt“, entfernen) muss die Mail als
   *updated* ins JMAP-Change-Log schreiben – über
   `MailChangeRecorder::emailChanged((int) $message->account->id, (string) $message->id, false, $message->thread)`
   und danach `flush()` in derselben Unit of Work (`StateManager::record()` flusht bewusst nicht).
   Sonst taucht die Mail nie in `Email/changes` auf und Projektakte bemerkt die Zuordnung nicht.
4. Kein `Email/set` für diese Eigenschaft, kein Filter in `Email/query` nötig.

Warum eine Email-Eigenschaft und keine eigene Fork-Methode (wie FORK.md 22): Projektakte holt die
Mails ohnehin mit `Email/get` und erkennt Änderungen über `Email/changes`; die Zuordnung muss genau
diesen Weg nehmen. Dafür sind drei kleine, markierte Änderungen an Originaldateien nötig (siehe
unten). Das ist mit mir abgesprochen.

## Umsetzung in plMail

Halte dich an `CLAUDE.md` und `FORK.md`: neue Dateien in `Fork`-Namensräumen bevorzugen, jede
Änderung an Originaldateien mit `FORK (FORK.md): …` markieren, Code/Kommentare/Commits Englisch,
FORK.md Deutsch. Keine echten Hostnamen, Projektnummern, Namen oder Tokens im Repo – nur Platzhalter
wie `https://projektakte.example` und `BS-0000-0000`.

**Datenbank (mit mir abgesprochen):** neue Tabelle `fork_message_project`, keine Spalten in
bestehenden Tabellen.

- `id` (Identity), `message_id` INT NOT NULL → `message(id)` ON DELETE CASCADE, eindeutig
  (`uniq_fork_message_project_message`).
- `status` VARCHAR (`assigned` | `none`), `project_number` VARCHAR(64) NULL
  (`idx_fork_message_project_project`), `project_name` TEXT.
- `assigned_by_id` → `"user"` ON DELETE SET NULL und `assigned_by_name` TEXT.
- `TimestampableTrait` mit `#[ORM\HasLifecycleCallbacks]`; `assignedAt` = `updated_at`.
- „Entfernen“ löscht die Zeile (→ `null` in JMAP).
- Migration `migrations/VersionYYYYMMDDhhmmss.php` mit `FORK (FORK.md): …`-Docblock,
  `getDescription()` = `Fork: add fork_message_project …`, rohes `addSql` und `down()` (Vorlage:
  `Version20261102120000.php`). `tests/Migrations/SchemaInSyncTest.php` muss grün bleiben.
- Bekannte Grenze, bitte im FORK.md-Eintrag nennen: `ImapCopyMerger::fold()` hängt nur die
  Kindtabellen des Originals um. Wird ein altes Duplikat zusammengeführt, verschwindet dessen
  Zuordnung per CASCADE. Das ist so akzeptiert; alternativ eine markierte Zeile dort ergänzen.

**Neue Dateien (Vorschlag, gern anpassen):**

- `src/Entity/Fork/ForkMessageProject.php`, `src/Repository/Fork/ForkMessageProjectRepository.php`
  (inkl. `findByMessageIds(array $ids)` für JMAP und die Liste).
- `src/Service/Fork/Projektakte/ForkProjektakteClient.php` nach dem Vorbild
  `src/Service/Fork/Search/ForkReranker.php`. Eigenschaften:
  - `HttpClientInterface` (nicht der SSRF-geschützte `UserUrlHttpClient` – die URL setzt der Admin);
  - `#[Autowire('%env(default::PROJEKTAKTE_URL)%')]` und `…PROJEKTAKTE_TOKEN`; `isAvailable()` nur,
    wenn beides gesetzt ist;
  - `auth_bearer`, `timeout` ≈ 5 s, `max_duration` ≈ 10 s, `max_redirects` 0;
  - jeden `\Throwable` fangen, mit `warning` loggen (ohne Token) und sauber auf „nicht verfügbar“
    zurückfallen;
  - Methoden `info()`, `projects()` (gecacht über `CacheInterface`), `suggest(Message)` (Betreff,
    `bodyText`, `fromAddress`, Dateinamen der Anhänge aus `messageParts`) und `notify()`.
- `src/Service/Fork/Projektakte/ForkProjectAssigner.php` mit `assign(Message, User, number)`,
  `markNone(Message, User)` und `clear(Message, User)`. Ablauf jeweils:
  1. Projektnummer gegen die (gecachte) Projektliste prüfen, `project_name` von dort übernehmen;
  2. Zeile anlegen/ändern/löschen;
  3. `MailChangeRecorder::emailChanged(…)` aufrufen;
  4. `flush()`;
  5. `notify()`.

  Kein IMAP-/Gmail-/Graph-Job.
- `src/Controller/Fork/ForkProjectController.php` mit `#[IsGranted('IS_AUTHENTICATED')]` und
  `isGranted('own', $message)` (der `OwnershipVoter` kennt `Message`):
  - `GET /mail/message/{id}/fork/project` – Fragment mit Vorschlägen und Projektliste (vom Server
    geholt);
  - `POST /mail/message/{id}/fork/project` – JSON `{"number": "…"}` oder `{"none": true}`, mit
    `assertCsrf($request, 'ajax')`, Antwort Turbo Stream auf `fork_project_{id}`;
  - `DELETE /mail/message/{id}/fork/project` – Zuordnung entfernen.
- `templates/fork/project/_chip.html.twig` (Stil wie der Abmelden-Chip,
  `<span id="fork_project_{{ message.id }}">`), `_picker.html.twig`, `_assigned.stream.html.twig`.
- `assets/controllers/fork/project_picker_controller.js`: Positionierung/Escaping wie
  `mail/label_menu_controller.js`, Laden beim Öffnen und clientseitiger Filter wie
  `compose/template_picker_controller.js`, POST über `jsonCsrfHeaders()` und
  `Turbo.renderStreamMessage()` wie `mail/message_actions_controller.js`.
- Texte in `translations/fork.{de,en,en_PI,fr,zh_CN}.yaml` unter `message.fork_project.*`, Deutsch
  in der Du-Form.
- Optional `src/Twig/Fork/ForkProjectExtension.php` für das Listen-Badge (gebündelt vorladen).
- Optional ein Abschnitt in den Admin-Einstellungen mit „Testen“ (ruft `info()`), nach dem Muster
  von `templates/fork/admin/_rerank.html.twig`.

**Markierte Änderungen an Originaldateien (so wenig wie möglich):**

- `src/Jmap/Protocol/Capability.php`:
  `public const string PROJEKTAKTE = 'urn:plmail:params:jmap:projektakte';` und in `SUPPORTED`
  aufnehmen.
- `src/Jmap/Session/SessionBuilder.php::build()`: `Capability::PROJEKTAKTE => new \stdClass()` in
  jedes `accountCapabilities` und in die obersten `capabilities`. Ob immer oder nur bei
  konfigurierter Integration, entscheidest du – Projektakte kommt mit beidem klar.
- `src/Jmap/Method/Mail/EmailGetMethod.php`: Opt-in wie `replyFrom`. Wenn `'plmail:projektakte'`
  in `properties` steht, die Zuordnungen gebündelt laden und den Wert setzen.
- `templates/mail/_thread_message.html.twig`: ein Include des Chips in `.thread-message-meta`
  (nahe dem Abmelden-Chip). Bei Bedarf auch `templates/mail/_message_content.html.twig` bzw. das
  ⋮-Menü in `_message_actions.html.twig`.

**Konfiguration** (in FORK.md dokumentieren, nicht in `.env`; sonst schlägt
`DocumentationCoverageTest` an):

- `PROJEKTAKTE_URL` = Basis-URL von Projektakte im LAN (https über den TLS-Proxy von Projektakte),
  vom plMail-Container aus erreichbar.
- `PROJEKTAKTE_TOKEN` = dasselbe Geheimnis wie `INTEGRATION_TOKEN` in Projektakte.

## Tests und Abnahme

- **JMAP-Tests** (`tests/Jmap/Method/Fork/…` oder `Fork*Test` neben den Mail-Tests):
  - die Session nennt die URN (oben und je Konto);
  - `using` mit der URN wird akzeptiert;
  - `Email/get` mit `plmail:projektakte` liefert `null`, `assigned` bzw. `none`; ohne die
    Eigenschaft in `properties` fehlt sie;
  - nach `assign`/`markNone`/`clear` steigt der `state`, und die Mail steht in `Email/changes` unter
    `updated`;
  - der Wert ist UTC mit `Z`, `assignedBy` überlebt das Löschen des Benutzers.
- **Controller-Tests** (`tests/Controller/Fork/ForkProjectTest.php`, Projektakte über
  `MockHttpClient` wie in `ForkSearchRerankTest`):
  - nur der Besitzer der Mail darf zuordnen, und CSRF ist Pflicht;
  - unbekannte Projektnummern werden abgelehnt;
  - fällt Projektakte aus, gibt es eine freundliche Meldung und keinen 500;
  - `notify` wird nach jeder Änderung aufgerufen, ein Fehler dabei schadet nicht;
  - es wird kein IMAP-/Label-Job erzeugt.
- **Playwright** `tests/e2e/fork-project.spec.ts`: Chip öffnen, Vorschlag ist vorausgewählt, ohne
  Klick passiert nichts, zuordnen, umhängen, „kein Projektbezug“, entfernen.
- PHPUnit, PHPStan und die betroffenen Playwright-Specs lokal laufen lassen
  (`npm run test:unit:docker`, `npm run test:e2e:docker`), weil die CI keine PRs testet.
- **Doku:**
  - FORK.md Abschnitt **27** im üblichen Format: was der Nutzer sieht, neue Dateien, markierte
    Änderungen am Original, Datenbank, Grund, „Kandidat fürs Original: nein“;
  - `changelog.d/fork-projektakte-assignment.md`.

**Fertig ist es, wenn** dieser Befehl die URN zeigt:

```bash
curl -s -H "Authorization: Bearer plmail_…" https://plmail.example/.well-known/jmap | grep projektakte
```

Außerdem muss eine in der Oberfläche zugeordnete Mail bei
`Email/changes` + `Email/get` mit `properties: ["id","plmail:projektakte"]` und
`using: [core, mail, "urn:plmail:params:jmap:projektakte"]` den erwarteten Wert liefern.

## Nicht Teil des Auftrags

- Keine Änderungen an Projektakte und keine automatische Zuordnung in plMail. Selbst bei
  `confidence: "sicher"` entscheidet der Mensch.
- Kein Schreiben der Zuordnung zum Mailserver, kein `Email/set`, keine EventSource-Pflicht.
- Nichts an das Original-Repository `karatektus/pl_mail` (kein Push, PR, Issue).
- Arbeite auf einem eigenen Branch und eröffne einen PR gegen `AunePVP/pl_mail`. Frag mich vor dem
  Abschluss, ob die Funktion auch in die iOS-App soll (Parität).

## Inbetriebnahme (zur Info)

- **plMail:** `PROJEKTAKTE_URL`, `PROJEKTAKTE_TOKEN` setzen.
- **Projektakte:**
  - `PLMAIL_URL`;
  - `PLMAIL_TOKEN` = App-Passwort (`plmail_…`) des plMail-Benutzers, dem die Konten gehören;
  - `INTEGRATION_TOKEN` = derselbe Wert wie `PROJEKTAKTE_TOKEN`;
  - `ASSIGN_MODE=manual` (Standard, sobald plMail angebunden ist);
  - `PLMAIL_WEB_URL=https://plmail.example/mail/message/{emailId}`.
- **Prüfen:** In Projektakte zeigt `GET /health/deps` bei plMail `"project_extension": true`.
