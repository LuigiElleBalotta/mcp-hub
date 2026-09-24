# mcp-hub

🇬🇧 [Read in English — README.md](README.md)

Un unico processo condiviso, sempre attivo, che ospita i server MCP locali
(mariadb, headroom, gitlab, figma-bridge, chrome-real, windows-mcp, ...) e
li espone via HTTP/SSE, così N sessioni Claude Code si collegano allo
stesso backend invece di avviare (e pagare) ciascuna il proprio albero di
sottoprocessi.

- **Design spec:** `docs/superpowers/specs/2026-09-21-mcp-hub-design.md`
- **Piano di implementazione:** `docs/superpowers/plans/2026-09-21-mcp-hub-implementation.md`

## Perché

Ogni sessione Claude Code che elenca un server MCP in `.claude.json` avvia
una propria copia di quel processo. Con più sessioni aperte contemporaneamente
(più terminali, più progetti) ti ritrovi con N processi per server — N
processi Node/`npx` per gitlab, N per figma-bridge, N processi Python per
mariadb, ecc. mcp-hub avvia ogni server **una sola volta**, lo tiene attivo,
e ogni sessione parla con quell'unica istanza via HTTP/SSE invece di
avviarne una propria.

## Architettura

```
Sessione Claude Code 1 ─┐
Sessione Claude Code 2 ─┼──HTTP/SSE──▶  mcp-hub (uvicorn/Starlette)
Sessione Claude Code N ─┘                 │
                                           ├─ ManagedServer "gitlab"      (npx, sottoprocesso reale, avviato una volta)
                                           ├─ ManagedServer "mariadb"     (npx, sottoprocesso reale, avviato una volta)
                                           ├─ ManagedServer "figma-bridge"(npx, sottoprocesso reale, avviato una volta)
                                           └─ ...
```

- Un'app ASGI **Starlette/uvicorn** (`hub_app.py`) monta un endpoint SSE MCP
  per ogni server configurato e abilitato: `http://127.0.0.1:37450/<nome>/sse`.
- Un **process manager** (`manager.py`) possiede il sottoprocesso reale di
  ogni backend, avviato una volta all'avvio dell'hub, con un buffer di log
  circolare redatto e rilevamento dei crash.
- Un **guard di concorrenza per server** (`concurrency.py`) serializza le
  chiamate verso i server che non gestiscono richieste concorrenti
  (`"exclusive"`, default) o le lascia libere (`"parallel"`).
- Uno **store di configurazione JSON** (`config.py`), fuori dal repo, è
  l'unica fonte di verità su quali server esistono e come avviarli.
- Una **GUI PySide6** parla solo con l'API di gestione HTTP locale dell'hub
  (`management_api.py`) — non tocca mai `config.json` direttamente.
- I comandi CLI **`import`/`apply`** (`claude_config.py`) spostano le
  definizioni dei server tra la config dell'hub e il `.claude.json` di
  Claude Code, così non devi editare entrambi i file a mano.
- **`apply --cleanup`** (`cleanup.py`) può liberare memoria chiudendo i
  processi legacy già in esecuzione per sessione, dopo una conferma unica,
  proteggendo la catena di processi antenati del processo chiamante stesso.

## Requisiti

- Windows (GUI PySide6, autostart via Task Scheduler, gestione processi
  Windows-specifica in `cleanup.py`/`manager.py`)
- Python 3.11+

## Installare l'exe (utenti finali)

Nessun installer, nessun diritto da amministratore richiesto — sono due
file standalone:

1. Vai alla [pagina Releases](https://github.com/LuigiElleBalotta/mcp-hub/releases)
   del repo e scarica `mcp-hub.exe` e `mcp-hub-gui.exe` dalla versione che
   preferisci (un tag `x.y.z` semplice è stabile; `x.y.z-n` è beta).
2. Metti **entrambi i file nella stessa cartella** (es.
   `C:\Users\<tu>\AppData\Local\Programs\mcp-hub\`) — l'auto-update della
   GUI presuppone che `mcp-hub.exe` sia proprio accanto a sé stessa, ed è
   anche il layout che l'auto-updater ripristina.
3. Avvia `mcp-hub-gui.exe`. Senza `config.json` ancora esistente, si apre
   un **wizard di primo avvio** invece di una finestra vuota: scegli
   "Importa da un .claude.json esistente" e seleziona quali server importati
   abilitare, oppure salta per partire da zero (in entrambi i casi viene
   scritta una config, editabile dopo a mano o dalla GUI). Il wizard avvia
   anche l'hub per te, in background, così la GUI ha subito qualcosa a cui
   connettersi. Puoi anche copiare tu stesso `config.example.json` in
   `%LOCALAPPDATA%\mcp-hub\config.json` — il wizard appare solo quando
   quel file non esiste ancora (vedi [Configurazione](#configurazione)).
4. Da qui in poi, avvia `mcp-hub-gui.exe` quando vuoi gestire i server,
   oppure `mcp-hub.exe serve` direttamente se vuoi solo l'hub senza GUI.

Le nuove versioni si installano col pulsante **Installa e riavvia** della
GUI — niente più download manuali.

## Setup (sviluppo)

```
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
```

Esegui i test per confermare che l'installazione sia sana:

```
.venv\Scripts\pytest -q
```

`python -m mcp_hub serve` e `python -m mcp_hub.gui` eseguono lo stesso
codice degli exe compilati, direttamente dai sorgenti — vedi
[Avviare l'hub](#avviare-lhub) / [Avviare la GUI](#avviare-la-gui).

## Configurazione

La config vive in **`%LOCALAPPDATA%\mcp-hub\config.json`** — mai dentro
questo repo (il repo distribuisce solo `config.example.json` come modello).
Copiala lì ed editala a mano, oppure popolala da un `.claude.json`
esistente con `mcp_hub import` (vedi sotto).

```json
{
  "hub": {
    "host": "127.0.0.1",
    "port": 37450,
    "authToken": null,
    "autostart": false,
    "checkForUpdates": true,
    "includeBetaUpdates": false
  },
  "servers": {
    "example-server": {
      "enabled": false,
      "command": "npx",
      "args": ["-y", "some-mcp-server"],
      "env": {},
      "concurrency": "exclusive"
    }
  }
}
```

**Campi di `hub`:**

| Campo                | Default       | Significato |
|-----------------------|---------------|---------|
| `host`               | `127.0.0.1`   | Indirizzo di bind. Qualsiasi cosa diversa da `127.0.0.1`/`localhost` viene rifiutata all'avvio a meno che `authToken` non sia impostato. |
| `port`               | `37450`       | Porta di bind. |
| `authToken`          | `null`        | Obbligatorio se `host` non è localhost. Non ancora applicato sulle singole richieste — solo restrizione di binding. |
| `autostart`          | `false`       | Flag informativo rispecchiato dalla checkbox "Avvia con Windows" della GUI, che registra/rimuove una voce di Windows Task Scheduler (vedi sotto). |
| `checkForUpdates`    | `true`        | La GUI controlla GitHub Releases per una versione più recente all'avvio e mostra un banner se esiste. Non scarica o installa mai nulla senza un click esplicito su "Installa e riavvia" (vedi [Auto-update](#auto-update)). |
| `includeBetaUpdates` | `false`       | Se `true`, il controllo aggiornamenti considera anche i tag beta (prerelease), non solo le release stabili. |

**Campi per server (`servers.<nome>`):**

| Campo         | Significato |
|---------------|---------|
| `enabled`     | Se l'hub avvia questo server. I server disabilitati non vengono mai avviati e non ottengono alcun mount HTTP. |
| `command`     | Eseguibile da lanciare (risolto via `PATH`/`PATHEXT`, così un `npx`/`uvx` bare funziona su Windows). |
| `args`        | Lista di argomenti passata a `command`. |
| `env`         | Variabili d'ambiente extra, unite sopra l'ambiente del processo hub stesso (così `PATH` ecc. sono preservate). Qualsiasi chiave che corrisponde a `TOKEN\|SECRET\|PASS\|KEY\|AUTH` (case-insensitive) è oscurata (`***`) ovunque venga loggata o restituita dall'API — mai oscurata nel file di config stesso, dato che l'hub ha bisogno dei valori reali per avviare il processo. |
| `concurrency` | `"exclusive"` (default, serializza ogni richiesta a questo server dietro un `asyncio.Lock`) oppure `"parallel"` (nessuna serializzazione). Usa `"exclusive"` per tutto ciò che non gestisce chiamate sovrapposte (es. una sessione browser stateful); `"parallel"` per strumenti stateless/di sola lettura. |

## Avviare l'hub

```
.venv\Scripts\python -m mcp_hub serve
```

- Avvia ogni server `enabled`, poi serve l'app ASGI su `hub.host:hub.port`.
- Rifiuta di avviarsi (esce con un messaggio chiaro) se `host` non è
  `127.0.0.1`/`localhost` e nessun `authToken` è impostato.
- Allo spegnimento (Ctrl-C o altro), ogni sottoprocesso gestito viene
  fermato — nulla resta orfano.
- Verifica che sia vivo: `curl http://127.0.0.1:37450/api/status`

## Avviare la GUI

```
.venv\Scripts\python -m mcp_hub.gui
```

La GUI è un client leggero sopra l'API di gestione dell'hub per tutto
tranne il setup di primo avvio e l'import da Claude Code (sotto) — quelli
scrivono `config.json` direttamente, dato che non c'è ancora un hub attivo
a cui instradare la prima volta.

- **Wizard di primo avvio**: se `%LOCALAPPDATA%\mcp-hub\config.json` non
  esiste ancora, avviare la GUI mostra questo invece di una finestra vuota
  — offre di importare server da un `.claude.json` esistente e scegliere
  quali abilitare, o partire da una config vuota. In entrambi i casi scrive
  `config.json` e avvia l'hub per te (in background), così la finestra
  successiva ha subito qualcosa a cui connettersi. Compare una volta sola;
  dopo che `config.json` esiste, la GUI apre direttamente sulla tabella
  server.
- **Tabella server**: stato live (`stopped`/`starting`/`running`/`crashed`)
  per ogni server configurato, aggiornato ogni 2 secondi, con pulsanti
  Start/Stop per riga.
- **Add server**: apre un dialogo per definire un nuovo server (comando,
  argomenti, concorrenza, variabili d'ambiente). I campi env la cui chiave
  sembra un segreto (`TOKEN`/`SECRET`/`PASS`/`KEY`/`AUTH`) sono mascherati
  di default, con una checkbox "show" per riga.
- **Pannello log**: seleziona una riga per vedere le ultime 500 righe di
  log (oscurate) di quel server.
- **Avvia con Windows**: registra/rimuove l'autostart via Windows Task
  Scheduler (vedi sotto).
- **Import from Claude Code config / Apply to Claude Code config**:
  wrapper GUI dei comandi CLI `import`/`apply` sotto, con un dialogo di
  conferma prima di ogni scrittura.
- **Banner aggiornamento**: se una versione più recente è pubblicata su
  GitHub Releases (e `hub.checkForUpdates` è vero), compare un banner con
  un link per scaricarla.
- **Installa e riavvia** (mostrato solo eseguendo l'`.exe` compilato, non
  `python -m mcp_hub.gui`, e solo quando la release ha entrambi gli asset
  exe): un click scarica `mcp-hub.exe`/`mcp-hub-gui.exe` per la nuova
  versione, poi sostituisce gli exe installati e rilancia entrambi — vedi
  [Auto-update](#auto-update) sotto per cosa succede esattamente e perché
  serve un click invece che girare in silenzio.

## Spostare i server da/verso la config di Claude Code

Questi comandi leggono/scrivono il `.claude.json` di Claude Code stesso (o
la config scoped di un progetto con `--project`), mai il `config.json`
dell'hub direttamente, tranne per registrare cosa è stato importato.

**Import** delle definizioni server esistenti da `.claude.json` nell'hub,
disabilitate di default (così nulla parte finché non le rivedi e abiliti):

```
mcp_hub import --from C:\Users\<tu>\.claude.json [--project <scope>]
```

**Apply** dei server hub abilitati dentro `.claude.json`, puntando Claude
Code verso l'hub invece che un processo avviato localmente:

```
mcp_hub apply --to C:\Users\<tu>\.claude.json [--project <scope>] [--cleanup] [--yes]
```

- Fa sempre un backup con timestamp (`.claude.json.bak-<timestamp>`)
  accanto al file target prima di scrivere, e verifica che il risultato
  sia JSON valido prima di considerare la scrittura riuscita.
- Riscrive ogni voce di server migrato come
  `{"type": "http", "url": "http://<host:porta hub>/<nome>/sse"}`.
- `--cleanup`: dopo l'apply, cerca processi legacy già in esecuzione che
  corrispondono a `command`/`args` dei server migrati e offre di chiuderli
  (conferma singola sì/no, non una checklist per processo). **Esclude
  sempre la catena di processi antenati del processo che esegue il
  comando stesso** — ma *non* sa di altri terminali/sessioni che hanno le
  proprie copie di quei server in esecuzione; chiuderle è esattamente lo
  scopo di `--cleanup`: eseguilo solo quando va bene che vengano chiusi
  anche i processi corrispondenti di altre sessioni (riotterranno
  connessioni fresche via hub alla prossima necessità di quello strumento).
- `--yes`: salta la conferma interattiva (utile per script; stampa comunque
  prima il piano).

Cutover tipico una tantum per un `.claude.json` esistente:

```
mcp_hub import --from C:\Users\<tu>\.claude.json
:: rivedi %LOCALAPPDATA%\mcp-hub\config.json, metti "enabled": true su ciò che vuoi migrare
mcp_hub serve                                    :: (lascialo attivo, o usa l'autostart di Task Scheduler)
mcp_hub apply --to C:\Users\<tu>\.claude.json --cleanup
:: avvia una sessione Claude Code fresca e conferma che gli strumenti migrati funzionino ancora
```

## Guida alla migrazione: far vincere ovunque la config a livello USER

`mcp_hub apply` (sopra) riscrive le voci `mcpServers` per puntare all'hub,
al singolo scope a cui lo punti (`.claude.json` a livello top = scope
**user**, si applica a ogni progetto; `--project <path>` = scope
**project**, si applica solo lì). **Claude Code risolve il nome di un
server a livello progetto PRIMA che a livello user** — una voce a livello
progetto con lo stesso nome vince sempre, anche se quella a livello user
punta già all'hub. Lasciate lì da prima che adottassi l'hub (o da una
vecchia configurazione per-progetto), queste oscurano silenziosamente la
tua voce hub-backed: le sessioni di quel progetto continuano ad avviare
una propria copia locale del server invece di usare l'istanza condivisa.
Sintomo: `claude mcp list` in quel progetto mostra la riga
`command`/`args` grezza, non l'URL `http://127.0.0.1:37450/<nome>/sse`
dell'hub.

Esattamente il problema incontrato adottando l'hub su questa stessa
macchina: `gitlab`/`figma-bridge`/`chrome-devtools`/`windows-mcp` erano
definiti sia a livello user (verso l'hub) sia a livello progetto (comando
locale diretto) per un singolo progetto — quel progetto continuava a
ignorare l'hub.

### 1. Trova le voci che oscurano

`.claude.json` contiene anche molto stato non correlato (cronologia,
cache, statistiche per progetto) — non aprirlo in un editor e iniziare a
cancellare a mano. Salva questo come `check-overrides.js` (i one-liner
`node -e "..."` inline vengono storpiati in modo diverso da
PowerShell/cmd/bash — un file evita tutto questo). Legge il file in modo
strutturale e riporta solo i nomi di server a livello progetto che
esistono ANCHE a livello user (cioè duplicati genuini da rimuovere — un
server esclusivo di un progetto senza controparte a livello user è
probabilmente lì di proposito, lascialo stare):

```js
// check-overrides.js
const fs = require("fs");
const path = process.env.USERPROFILE + "/.claude.json";
const c = JSON.parse(fs.readFileSync(path, "utf8"));
const userNames = new Set(Object.keys(c.mcpServers || {}));
for (const [proj, cfg] of Object.entries(c.projects || {})) {
  const shadowed = Object.keys(cfg.mcpServers || {}).filter((n) => userNames.has(n));
  if (shadowed.length) console.log(proj, "->", shadowed.join(", "));
}
```

```
node check-overrides.js
```

### 2. Rimuovile (dopo aver rivisto l'output del passo 1)

Salva questo come `clean-overrides.js`. Fa sempre un backup di
`.claude.json` (con timestamp, accanto all'originale) prima di toccare
qualsiasi cosa, e cancella solo i nomi a livello progetto che duplicano
uno a livello user:

```js
// clean-overrides.js
const fs = require("fs");
const path = process.env.USERPROFILE + "/.claude.json";
fs.copyFileSync(path, `${path}.bak-${Date.now()}`);
const c = JSON.parse(fs.readFileSync(path, "utf8"));
const userNames = new Set(Object.keys(c.mcpServers || {}));
for (const cfg of Object.values(c.projects || {})) {
  if (!cfg.mcpServers) continue;
  for (const name of Object.keys(cfg.mcpServers)) {
    if (userNames.has(name)) delete cfg.mcpServers[name];
  }
}
fs.writeFileSync(path, JSON.stringify(c, null, 2));
console.log("fatto");
```

```
node clean-overrides.js
```

### 3. Verifica

Riavvia ogni sessione Claude Code aperta nel/nei progetto/i interessati
(la config MCP si legge all'avvio sessione, non c'è hot-reload), poi:

```
claude mcp list
```

Il server ripulito dovrebbe ora mostrare l'URL `http://127.0.0.1:37450/<nome>/sse`
dell'hub invece di una riga `command`/`args` locale.

### Prompt da dare a un agente

Incolla questo in una sessione Claude Code (aggiustando percorsi/nomi
server) per far fare a un agente l'intero flusso di installazione e
migrazione, incluso questo cleanup a livello progetto, da solo:

```
Installa e adotta mcp-hub (https://github.com/LuigiElleBalotta/mcp-hub) su
questa macchina Windows, poi migra i miei server MCP Claude Code esistenti
verso di esso:

1. Scarica l'ultimo `mcp-hub.exe` e `mcp-hub-gui.exe` dalla pagina GitHub
   Releases del repo e mettili entrambi in
   `%LOCALAPPDATA%\Programs\mcp-hub\` (creala se manca).
2. Esegui `mcp-hub.exe import --from %USERPROFILE%\.claude.json`, poi
   mostrami il risultante `%LOCALAPPDATA%\mcp-hub\config.json` e chiedimi
   su quali server importati mettere `"enabled": true` prima di
   continuare.
3. Avvia l'hub (`mcp-hub.exe serve`, oppure imposta l'autostart via
   `scripts\install_task.ps1 -Enable` da un checkout, oppure lancia
   semplicemente `mcp-hub-gui.exe`) e conferma che
   `http://127.0.0.1:37450/api/status` risponda.
4. Esegui `mcp-hub.exe apply --to %USERPROFILE%\.claude.json --cleanup`
   (conferma il piano che stampa prima che chiuda processi legacy) così
   il mio `.claude.json` a livello USER punta i server abilitati
   all'hub.
5. Fai un audit di `.claude.json` per voci `mcpServers` a livello
   PROGETTO che duplicano un nome ora definito a livello user (lo scope
   progetto vince su quello user in Claude Code, quindi una voce a
   livello progetto rimasta lì continuerebbe silenziosamente a bypassare
   l'hub per quel progetto). Mostrami la lista prima di toccare qualsiasi
   cosa. Fai prima un backup di `.claude.json`, poi rimuovi solo i nomi
   duplicati dal `mcpServers` di ogni progetto — non toccare mai un
   server a livello progetto che non ha una controparte a livello user.
6. Dimmi quali sessioni/progetti devono essere riavviati per recepire la
   modifica, e verifica con `claude mcp list` che i server migrati
   mostrino ora l'URL http dell'hub invece di un comando locale.

Chiedi conferma prima di ogni passo distruttivo (chiudere processi,
cancellare voci di config). Fermati e riporta se qualcosa non corrisponde
a quanto descritto qui.
```

## Autostart (Windows Task Scheduler)

```
scripts\install_task.ps1 -Enable    # registra un task al logon ("McpHub") che esegue `mcp_hub serve`
scripts\install_task.ps1 -Disable   # lo rimuove
```

Disponibile anche come checkbox "Avvia con Windows" nella GUI. Richiede il
permesso di creare scheduled task per l'utente corrente — su una macchina
gestita/bloccata può fallire con "Accesso negato"; in quel caso chiedi
all'IT i diritti di creazione su Task Scheduler, o avvia l'hub a mano ogni
sessione.

## Release e versioning (git flow)

Questo repo segue **git flow**: `master` contiene la storia rilasciata,
`develop` è il branch di integrazione per il lavoro in corso.

Il push di un tag di versione attiva `.github/workflows/release.yml`, che
compila due eseguibili Windows con PyInstaller (`mcp-hub.exe` — la CLI,
`serve`/`import`/`apply`; `mcp-hub-gui.exe` — la GUI) e li pubblica come
asset su una GitHub Release per quel tag:

| Pattern tag         | Esempio      | Risultato |
|----------------------|--------------|---------|
| `x.y.z` (prefisso `v` opzionale) | `1.2.0`, `v1.2.0` | Build stabile, pubblicata come GitHub Release normale (non prerelease). |
| `x.y.z-n`            | `1.2.0-1`     | Build beta, pubblicata come GitHub Release **prerelease**. |

Qualsiasi altra forma di tag viene ignorata dal workflow.

Il controllo aggiornamenti della GUI (`hub.checkForUpdates`) interroga
questa stessa lista di GitHub Releases; imposta `hub.includeBetaUpdates:
true` per essere avvisato anche delle prerelease `x.y.z-n`, non solo delle
release stabili.

## Auto-update

**Layout di installazione da cui dipende:** `mcp-hub.exe` e
`mcp-hub-gui.exe` devono stare nella stessa cartella (esattamente come li
compila il workflow di release e come dovresti estrarli/posizionarli). La
GUI localizza l'exe dell'hub come "il file chiamato `mcp-hub.exe` accanto
a me stessa" — `self_update.py`.

Cliccare **Installa e riavvia** nel banner di aggiornamento:

1. Conferma una volta (questo riavvia l'hub — vedi l'avviso che mostra).
2. Recupera `hub_pid` dall'hub in esecuzione (`GET /api/pid`).
3. Scarica il nuovo `mcp-hub.exe`/`mcp-hub-gui.exe` dagli asset della
   GitHub Release in `%LOCALAPPDATA%\mcp-hub\update-staging\`.
4. Lancia un helper PowerShell in background che aspetta che sia il
   processo hub sia questo processo GUI escano davvero (i loro file exe
   sono bloccati finché sono in esecuzione — Windows non può sovrascriverli
   sul posto), poi copia i nuovi exe sopra i vecchi e rilancia
   `mcp-hub.exe serve` e `mcp-hub-gui.exe`.
5. Dice all'hub di spegnersi in modo pulito (`POST /api/shutdown` — lo
   stesso percorso di uscita pulito di Ctrl-C, così ogni sottoprocesso
   gestito viene fermato invece che orfanato) e poi chiude la propria
   finestra, così entrambi i lock si liberano e l'attesa dell'helper al
   passo 4 si sblocca.

Questo gira solo dopo il tuo click — nulla scarica o riavvia nulla da solo.
Compare solo eseguendo l'exe compilato (`sys.frozen`); in un checkout di
sviluppo (`python -m mcp_hub.gui`) ottieni comunque il banner e il suo
link di download manuale, dato che non c'è un exe installato da sostituire
per la GUI.

**Cosa succede alle altre sessioni Claude Code aperte:** la stessa cosa
che succederebbe se l'hub si riavviasse per qualsiasi altro motivo — ogni
sessione che sta parlando con l'hub perde la connessione per i pochi
secondi che l'hub impiega a rilanciarsi, poi si riconnette
automaticamente. Questo è esattamente il motivo per cui il pulsante
richiede un click e un avviso invece di girare senza supervisione.

## Note di sicurezza

- L'hub rifiuta di fare bind su qualsiasi cosa diversa da
  `127.0.0.1`/`localhost` a meno che `hub.authToken` non sia impostato —
  applicato all'avvio, non solo suggerito nella GUI.
- Ogni riga di log (log dell'hub, buffer circolari per server, l'endpoint
  `/api/servers/{name}/logs` dell'API di gestione) oscura il valore di
  qualsiasi chiave env/config che corrisponde a
  `TOKEN|SECRET|PASS|KEY|AUTH` (case-insensitive) prima di essere scritta
  o restituita — il segreto in chiaro esiste solo in
  `%LOCALAPPDATA%\mcp-hub\config.json` e nell'ambiente reale del processo
  avviato.
- `apply` non sovrascrive mai `.claude.json` senza un backup con timestamp
  e un controllo di validità JSON post-scrittura.

## Sviluppo

```
.venv\Scripts\pytest -q                 # esegue l'intera test suite
.venv\Scripts\python scripts\smoke_test.py <nome-server>   # confronta una chiamata tool via hub contro una chiamata stdio diretta
.venv\Scripts\python scripts\concurrency_check.py           # verifica la serializzazione in modalità exclusive sotto carico concorrente reale
```

Struttura del progetto, storia task-per-task, e le interfacce esatte
esposte da ogni modulo sono nel piano di implementazione linkato in cima a
questo file.
