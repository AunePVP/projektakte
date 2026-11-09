# Projektakte – Hinweise für KI-Sitzungen

Lies zuerst **UEBERGABE.md** (Stand, Aufbau, Test mit der lokalen KI, offene Punkte), dann README.md
und DECISIONS.md.

Arbeitsregeln (Details in UEBERGABE.md, Abschnitt 10):
- Nach jeder Änderung `sh scripts/test.sh` – die Suite muss grün sein; bei Image-Änderungen auch
  `sh scripts/docker-smoke.sh`.
- Ein Commit je Thema, deutsche Commit-Titel; README.md, CHANGELOG.md, DECISIONS.md mitpflegen.
- Code-Kommentare auf Englisch, alles für Nutzer Sichtbare auf Deutsch.
- Keine Cloud-Dienste, keine Telemetrie, Geheimnisse nur aus Umgebungsvariablen, keine echten
  Firmendaten im Repository; Quellordner und Postfächer nur lesen, nie Mails senden.
- `tests/eval_holdout.json` nie öffnen oder zum Abstimmen benutzen – nur
  `python -m projektakte.evaluate --set holdout --summary-only`.
- Fake-KI: lieber ablehnen als falsch antworten.
- Die echten KI-Dienste (192.168.178.62) sind nur im Büronetz erreichbar; Messungen damit liefert
  der Nutzer (UEBERGABE.md, Abschnitt 7).
