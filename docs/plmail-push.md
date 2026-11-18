# Auftrag für plMail: zugeordnete Mails an Projektakte senden (Push)

> Diesen Text unverändert als Auftrag an das KI-Modell im plMail-Repository (`AunePVP/pl_mail`) geben.
> Er baut auf dem früheren Auftrag auf (`docs/plmail-prompt.md`: Projektwahl beim Lesen, Eigenschaft
> `plmail:projektakte`, Integrations-API mit `INTEGRATION_TOKEN`). Die Projektakte-Seite ist fertig und getestet.
> Er ersetzt den Auftrag `docs/plmail-prompt-nur-zugeordnet.md` (App-Passwort-Bereich) – den nicht bauen.

---

## Worum es geht

Projektakte (internes Werkzeug unseres Ingenieurbüros, läuft im LAN) hat bisher selbst über JMAP mit einem
App-Passwort in plMail gelesen und dabei alle Mails aller Konten kopiert. Das soll aufhören. **plMail entscheidet,
was plMail verlässt, und schickt es:** Nur Mails, die ein Mensch einem Projekt zugeordnet hat, gehen an
Projektakte – sonst nichts. Projektakte bekommt danach keinen Zugang zu den Postfächern mehr (das App-Passwort
wird gelöscht).

Du sollst die **plMail-Seite** bauen. An Projektakte änderst du nichts.

## Schnittstelle von Projektakte

Basis-URL und Token sind die der bestehenden Integration (`PROJEKTAKTE_URL`, `INTEGRATION_TOKEN`), jede Anfrage
mit `Authorization: Bearer <INTEGRATION_TOKEN>`. `accountId` und `emailId` sind die JMAP-ids von plMail
(erlaubt: Buchstaben, Ziffern, `.`, `_`, `-`). Die Zuordnung hat dieselbe Form wie die JMAP-Eigenschaft
`plmail:projektakte`:
`{"status": "assigned", "project": "BS-2024-0137", "projectName": "…", "assignedBy": "anna", "assignedAt": "2026-11-18T09:00:00Z"}`.

| Wann | Anfrage | Antwort |
|---|---|---|
| Mail einem Projekt zugeordnet (auch erneut, z. B. nach Änderung der Mail) | `PUT /api/integration/mails/{accountId}/{emailId}`, Körper: die Mail roh als `.eml` (`Content-Type: message/rfc822`, mit allen Anhängen, höchstens 200 MB), Kopfzeile `X-Projektakte-Zuordnung: <JSON der Zuordnung>` (JSON ASCII-codiert, Umlaute als `ü`) | `202 {"status": "angenommen"}` |
| Projekt einer schon gesendeten Mail geändert | `PUT /api/integration/mails/{accountId}/{emailId}/zuordnung`, Körper: JSON der Zuordnung | `200 {"status": "geändert"}`; `404` = Projektakte hat die Mail nicht → die Mail mit `PUT …/mails/…` senden |
| Zuordnung entfernt, „kein Projektbezug“ gewählt oder Mail gelöscht | `DELETE /api/integration/mails/{accountId}/{emailId}` | `200` (`gelöscht` oder `nicht vorhanden` – Wiederholen schadet nicht) |
| Prüfen, ob Projektakte eine Mail hat | `GET /api/integration/mails/{accountId}/{emailId}` | `{"vorhanden": bool, "wartet": bool, "zuordnung": {…}}` |

- Eine Mail **ohne** Zuordnung oder mit `"status": "none"` per `PUT` → `422`. Nicht senden.
- `400` = Anfrage fehlerhaft (nicht wiederholen, Fehler protokollieren). `401/403` = Token falsch.
  `5xx` oder keine Verbindung = später erneut versuchen.

## Was gebaut werden soll

1. **Senden beim Zuordnen:** Sobald ein Mensch eine Mail einem Projekt zuordnet, die Mail mit `PUT` senden.
   Beim Ändern des Projekts `PUT …/zuordnung`; antwortet Projektakte `404`, die ganze Mail senden.
   Beim Entfernen der Zuordnung, bei „kein Projektbezug“ und beim endgültigen Löschen einer zugeordneten
   Mail `DELETE`.
2. **Warteschlange:** Jede Sendung kommt zuerst in eine dauerhafte Warteschlange in plMail (überlebt
   Neustarts) und wird im Hintergrund zugestellt; bei `5xx`/Netzfehler erneut mit wachsendem Abstand
   (1 min … 1 h). Je Mail zählt nur der letzte Stand (ein `DELETE` nach einem noch nicht zugestellten `PUT`
   ersetzt diesen). Das Lesen in plMail wartet nie auf Projektakte.
3. **Nachsenden:** In den Einstellungen der Projektakte-Anbindung ein Knopf „Alle zugeordneten Mails an
   Projektakte senden“ (für den Start und nach Ausfällen): legt für jede zugeordnete Mail ein `PUT` in die
   Warteschlange. Fortschritt anzeigen.
4. **Anzeige:** In den Einstellungen: Projektakte erreichbar ja/nein, Sendungen in der Warteschlange,
   letzter Fehler.
5. **Nichts anderes senden:** keine nicht zugeordneten Mails, keine Listen, keine Vorschauen. Die bestehenden
   Aufrufe (`/projects`, `/suggest`) bleiben, `/notify` ist mit Push nicht mehr nötig.

## Tests (bitte mitbauen)

- Zuordnen → genau ein `PUT` mit der `.eml` und der Zuordnung; Projekt ändern → `PUT …/zuordnung`;
  „kein Projektbezug“ / Zuordnung entfernen / Löschen → `DELETE`.
- Nicht zugeordnete Mails erzeugen keine Anfrage.
- Projektakte nicht erreichbar → Sendung bleibt in der Warteschlange und wird später zugestellt; nach einem
  Neustart von plMail ebenso.
- `PUT` und danach `DELETE` vor der Zustellung → nur `DELETE` wird gesendet.
- Nachsenden-Knopf sendet alle zugeordneten Mails genau einmal.

## Fertig, wenn

- Eine in plMail zugeordnete Mail kurz danach in Projektakte beim Projekt erscheint, eine zurückgenommene
  Zuordnung sie dort entfernt – ohne App-Passwort für Projektakte.
- Die Hilfe von plMail das kurz erklärt (Deutsch).
