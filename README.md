# EUIC 2027 watcher

Gira su GitHub Actions ogni 15 minuti, controlla RK9 e pokemon.com e ti spamma di email quando escono novità sulle iscrizioni **TCG** all'EUIC 2027.

| Livello | Quando | Email |
|---|---|---|
| 👀 ANNOUNCED | L'EUIC 2027 compare tra gli eventi RK9 o nelle news pokemon.com | 3 (una volta) |
| 📅 SCHEDULED | RK9 mostra "Registration opens …" (la data di apertura) | `SPAM_COUNT` (una volta) |
| 🚨 OPEN | La pagina torneo TCG risulta aperta | `SPAM_COUNT`, poi `REPEAT_COUNT` a ogni run finché non disattivi il workflow |

Fonti controllate: `rk9.gg/events/pokemon` (sezione upcoming), `rk9.gg/event/pokemon-euic-2027` (+ link "Registration details" della sezione TCG), news di pokemon.com.

## Setup (5 minuti)

1. Crea un repo **privato** su GitHub e carica questi file (compresa la cartella `.github/`).
2. Password SMTP: con Gmail attiva la verifica in 2 passaggi e crea una **App password** (Account Google → Sicurezza → Password per le app).
3. In *Settings → Secrets and variables → Actions → Secrets* aggiungi:

   | Secret | Esempio |
   |---|---|
   | `SMTP_USER` | `tuoaccount@gmail.com` |
   | `SMTP_PASS` | la app password (16 caratteri) |
   | `MAIL_TO` | `tu@esempio.it` (anche più indirizzi separati da virgola) |

4. (Opzionale) In *Variables*: `SMTP_HOST` (default `smtp.gmail.com`), `SMTP_PORT` (465), `SPAM_COUNT` (10), `SPAM_DELAY_SEC` (5), `REPEAT_WHEN_OPEN` (true), `REPEAT_COUNT` (1), `EVENT_YEAR` (2027). Servono solo se non usi Gmail o vuoi cambiare i default.
5. *Actions → EUIC watcher → Run workflow* con **test_email** spuntato: deve arrivarti la mail di prova.
6. Lancialo una volta senza test e guarda il log: deve mostrare `[ok] RK9 events`. Se vedi `[fail] … HTTP 403`, RK9 sta bloccando gli IP di GitHub (vedi sotto).

## Note

- Lo stato (`state.json`) viene committato dal bot solo quando cambia, così non ricevi lo stesso avviso due volte.
- Se tutte le fonti falliscono per ~3 ore di fila ricevi **una** mail di warning (sito cambiato o IP bloccato).
- Per fermare lo spam: *Actions → EUIC watcher → ··· → Disable workflow*.
- I cron di GitHub possono ritardare di qualche minuto, e GitHub disattiva i workflow schedulati dopo 60 giorni senza attività sul repo.
- Test locale senza inviare mail: `pip install -r requirements.txt && python watcher.py --dry-run`
