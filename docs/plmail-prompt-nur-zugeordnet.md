> **Überholt:** ersetzt durch Push (`docs/plmail-push.md`) – nicht mehr an plMail geben.

# Auftrag für plMail: Projektakte sieht nur Mails, die einem Projekt zugeordnet sind

> Diesen Text unverändert als Auftrag an das KI-Modell im plMail-Repository (`AunePVP/pl_mail`) geben.
> Er baut auf dem früheren Auftrag auf (`docs/plmail-prompt.md`, Eigenschaft `plmail:projektakte`,
> Capability `urn:plmail:params:jmap:projektakte`). Die Projektakte-Seite ist fertig (siehe unten,
> „Was Projektakte schon tut“).

---

## Worum es geht

Projektakte (internes Werkzeug unseres Ingenieurbüros, läuft im LAN) liest plMail über JMAP mit einem
App-Passwort, nur lesend. Bisher hat Projektakte **jede Mail jedes Kontos** kopiert, das das App-Passwort
sieht – auch private und nicht projektbezogene Mails. Das ist falsch. **Projektakte darf nur Mails
bekommen, die ein Mensch in plMail einem Projekt zugeordnet hat** (`plmail:projektakte` mit
`"status": "assigned"`). Alles andere – nicht zugeordnet oder „kein Projektbezug“ – geht plMail-intern
und verlässt plMail nicht.

Projektakte filtert inzwischen selbst: Es liest Listen nur mit `id` und `plmail:projektakte` und holt
Inhalt und Anhänge nur für zugeordnete Mails. Die Sperre soll aber **in plMail** sitzen, damit ein
falsch eingestelltes oder älteres Projektakte gar nicht an andere Mails kommt.

Du sollst die **plMail-Seite** bauen. An Projektakte änderst du nichts.

## Was gebaut werden soll

### 1. App-Passwort mit Bereich „Projektakte“

- Beim Anlegen eines App-Passworts (Einstellungen → App-Passwörter) eine Auswahl **„Nur Projektakte:
  nur Mails mit Projektzuordnung“**. Für neue Passwörter, die Projektakte nutzen soll, ist das die
  Voreinstellung; bestehende Passwörter behalten ihren Bereich.
- Ein Passwort mit diesem Bereich darf **nur lesen** (wie bisher) und **nur zugeordnete Mails sehen**.

### 2. Was ein Passwort mit diesem Bereich über JMAP sieht

Eine Mail ist *sichtbar*, wenn `plmail:projektakte.status == "assigned"` mit einem Projekt gesetzt ist.

- `Email/query`: liefert nur sichtbare Mails (der Filter wird serverseitig immer angewendet,
  zusätzlich zu einem Filter des Aufrufers). `total`/`position` beziehen sich auf die sichtbaren Mails.
- `Email/get`: unsichtbare Mails stehen in `notFound` – auch wenn ihre id bekannt ist. Keine Eigenschaft,
  kein Betreff, keine Vorschau.
- `Email/changes`:
  - wird eine Mail **zugeordnet** → für dieses Passwort `created`,
  - wird die Zuordnung **zurückgenommen** oder auf „kein Projektbezug“ gesetzt → `destroyed`,
  - Änderungen an unsichtbaren Mails tauchen gar nicht auf.
  Jede Änderung an der Zuordnung muss den `state` erhöhen (wie schon im ersten Auftrag gefordert).
- **Anhänge** (`downloadUrl`): nur Blobs sichtbarer Mails, sonst 404.
- `Mailbox/get`, `Thread/get`, `SearchSnippet/get` und andere Methoden: nichts über unsichtbare Mails
  verraten (am einfachsten: für diesen Bereich nicht erlauben, außer was Projektakte braucht:
  `Email/query`, `Email/get`, `Email/changes`, Download).
- Session (`/.well-known/jmap`): weiter alle Konten des Nutzers, die Capability
  `urn:plmail:params:jmap:projektakte` wird gemeldet.

### 3. Zusätzlich ein Filter für alle Passwörter

`Email/query` versteht die Filterbedingung `{"plmail:projektakte": "assigned"}` (nur zugeordnete Mails).
So kann auch ein Passwort ohne den neuen Bereich gezielt abfragen.

## Was Projektakte schon tut (nicht ändern, nur passend bedienen)

- Liest mit `Email/query` (sortiert nach `receivedAt`, absteigend, seitenweise) und `Email/get` mit
  `properties: ["id", "plmail:projektakte"]`, danach `Email/get` mit Inhalt nur für zugeordnete Mails,
  danach Anhänge über `downloadUrl`.
- Folgeabgleiche mit `Email/changes`; `destroyed` löscht die Kopie in Projektakte samt Anhängen.
- Entfernt eine Mail auch, wenn sie ohne Zuordnung oder mit „kein Projektbezug“ zurückkommt.

## Tests (bitte mitbauen)

- Passwort mit Bereich „Projektakte“: `Email/query` ohne Filter liefert nur zugeordnete Mails;
  `Email/get` auf die id einer nicht zugeordneten Mail → `notFound`; Download ihres Anhangs → 404.
- Zuordnen → `Email/changes` meldet `created`; Zuordnung entfernen → `destroyed`; „kein Projektbezug“ →
  `destroyed`; Änderung (Lesen-Markierung) an einer nicht zugeordneten Mail → nichts.
- Schreibende Methoden (`Email/set`, `EmailSubmission/set`) → abgelehnt wie bisher.
- Passwort ohne Bereich: Verhalten unverändert; Filter `{"plmail:projektakte": "assigned"}` wirkt.

## Fertig, wenn

- Ein neues App-Passwort mit „Nur Projektakte“ angelegt werden kann und Projektakte damit nur
  zugeordnete Mails sieht – geprüft mit den Tests oben.
- Die Hilfe/Doku von plMail den Bereich kurz erklärt (Deutsch).
