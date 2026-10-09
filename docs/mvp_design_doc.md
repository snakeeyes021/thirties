# Software Design Document: `30s` (Natural Time Scheduler)

---

## 1. System Overview & Core Philosophy

**`30s`** is a personal scheduling and time-awareness application engineered around natural solar cycles and discrete half-hour human cognitive intervals ("Thirties"). Modern time management frequently suffers from false precision: minute-by-minute calendar tetris increases cognitive friction and fails as soon as a single task slips.

`30s` organizes the day into **48 discrete Thirty-Minute blocks (0 to 47)** anchored against local astronomical solar events (sunrise, sunset, twilight). The system differentiates between:

* **Daylight Thirties**: High-cognitive, alert, sunlight-bound intervals.
* **Dark Thirties**: Wind-down, relaxed, evening/night intervals.
* **Fixed Commitments**: Hard blocks derived from work schedules and primary calendar events.
* **Discretionary Thirties**: Open blocks allocated collaboratively between the user and an on-device Small Language Model (SLM).

The system executes completely locally on user-controlled hardware. It ingests hard calendar constraints via Google Calendar, pulls personal backlog tasks directly from the user's local Joplin SQLite vault, orchestrates conversational schedule formulation using on-device Gemma models via LiteRT-LM (with local Ollama fallback), and renders a native GNOME HIG desktop experience using PyGObject and Libadwaita.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                             Desktop Environment                             │
│                                                                             │
│  ┌─────────────────────────────────┐   ┌─────────────────────────────────┐  │
│  │     GNOME UI (Libadwaita)       │   │         Interactive CLI         │  │
│  │  - Circular / Arc 30s Visualizer│   │  - Terminal Planning Dialog     │  │
│  │  - Daily Pill Matrix (0-47)     │   │  - Direct headless execution    │  │
│  │  - Embedded Chat Assistant      │   │                                 │  │
│  └────────────────┬────────────────┘   └────────────────┬────────────────┘  │
│                   │                                     │                   │
│                   ▼                                     ▼                   │
│  ┌───────────────────────────────────────────────────────────────────────┐  │
│  │                     thirties-core (Python Engine)                     │  │
│  │                                                                       │  │
│  │  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌───────────┐  │  │
│  │  │ Astronomy /  │  │   Calendar   │  │    Joplin    │  │ State &   │  │  │
│  │  │ Solar Engine │  │ Synchronizer │  │ Vault Reader │  │ Task Log  │  │  │
│  │  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘  └─────┬─────┘  │  │
│  │         │                 │                 │                │        │  │
│  │         └────────────┬────┴─────────────────┴────────────────┘        │  │
│  │                      ▼                                                │  │
│  │         ┌─────────────────────────┐                                   │  │
│  │         │   Deterministic Block   │                                   │  │
│  │         │   Allocation Engine     │                                   │  │
│  │         └────────────┬────────────┘                                   │  │
│  │                      ▼                                                │  │
│  │         ┌─────────────────────────┐                                   │  │
│  │         │ Inference Layer         │                                   │  │
│  │         │ (LiteRT-LM / Ollama)    │                                   │  │
│  │         │ Gemma-4 E2B / E4B       │                                   │  │
│  │         └─────────────────────────┘                                   │  │
│  └───────────────────────────────────────────────────────────────────────┘  │
│            │                                                  │             │
│            ▼                                                  ▼             │
│  ┌─────────────────────────┐                        ┌──────────────────┐    │
│  │  Google Calendar API    │                        │  ~/.config/      │    │
│  │  (OAuth2 Read/Write)    │                        │  joplin-desktop/ │    │
│  │                         │                        │  database.sqlite │    │
│  └─────────────────────────┘                        └──────────────────┘    │
└─────────────────────────────────────────────────────────────────────────────┘

```

---

## 2. System Architecture & Component Boundaries

The project is structured as a decoupled monorepo:

```text
thirties/
├── pyproject.toml
├── thirties_core/
│   ├── __init__.py
│   ├── config.py             # Configuration and directory locations
│   ├── astronomy.py          # Solar calculation (Astral)
│   ├── calendar_engine.py    # Google Calendar OAuth2, sync, & multi-calendar logic
│   ├── joplin_engine.py      # SQLite read-only parser & Joplin Local API writer
│   ├── models.py             # Data classes (ThirtyBlock, TaskItem, DayPlan)
│   ├── scheduler.py          # Deterministic time budgeter & rolling 7-day model
│   ├── inference.py          # LiteRT-LM & Ollama abstractions
│   └── conversation.py       # Multi-turn planning state machine & tool definitions
├── thirties_gtk/
│   ├── __init__.py
│   ├── main.py               # Adw.Application entrypoint
│   ├── window.py             # Main application window
│   ├── views/
│   │   ├── day_view.py       # Diurnal timeline / 48-block matrix
│   │   ├── chat_panel.py     # Conversational planning sidebar
│   │   └── backlog_view.py   # Filtered Joplin task view
│   └── widgets/
│       ├── block_widget.py   # Discrete "Thirty" UI component
│       └── solar_arc.py      # Sunrise/sunset visualizer
└── tests/

```

---

## 3. Core Engine Mechanics (`thirties_core`)

### 3.1 The Discrete Thirty Grid & Astronomical Calculations

The day is partitioned into 48 equal intervals of 1,800 seconds (30 minutes) beginning at 00:00 local time:


$$\text{Index}(t) = \lfloor \frac{\text{hour}(t) \times 60 + \text{minute}(t)}{30} \rfloor, \quad \text{Index} \in [0, 47]$$

`astronomy.py` uses the Python library `astral` with the observer’s geographical coordinates to determine daily solar phase timestamps:

* `dawn` (civil twilight start)
* `sunrise` (solar disc crests horizon)
* `solar_noon`
* `sunset` (solar disc dips below horizon)
* `dusk` (civil twilight end)

Every block index $i \in [0, 47]$ is assigned solar properties:

* `is_sunlight = True` if the block midpoint $\ge \text{sunrise}$ and $\le \text{sunset}$.
* `is_twilight = True` if within civil twilight windows.

### 3.2 Block Domain Model (`models.py`)

```python
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any, Dict, List, Optional


class BlockKind(str, Enum):
  SLEEP = "sleep"
  WORK = "work"
  BUSY_CALENDAR = "busy_calendar"
  AMBIGUOUS_CALENDAR = "ambiguous_calendar"
  DAYLIGHT_DISCRETIONARY = "daylight_discretionary"
  DARK_DISCRETIONARY = "dark_discretionary"
  ASSIGNED = "assigned"


@dataclass
class ThirtyBlock:
  index: int  # 0 to 47
  start_dt: datetime
  end_dt: datetime
  is_sunlight: bool
  kind: BlockKind
  assigned_task_id: Optional[str] = None
  label: str = ""
  is_locked: bool = False
  source_event_id: Optional[str] = None


@dataclass
class TaskItem:
  id: str  # Joplin note ID or synthetic hash
  source_notebook: str  # e.g., "1. Tasks", "2. Dev", "3. Creative"
  title: str
  description: str
  is_checkbox_item: bool  # True if extracted from "- [ ]" in note body
  parent_note_title: Optional[str] = None
  estimated_thirties: int = 1
  deferred_count: int = 0
  tags: List[str] = field(default_factory=list)


@dataclass
class DayPlan:
  target_date: date
  blocks: List[ThirtyBlock]
  sunrise: datetime
  sunset: datetime
  daylight_available_count: int
  dark_available_count: int
  is_finalized: bool = False

```

---

## 4. External Data Ingestion & Storage

### 4.1 Joplin Ingestion Architecture

#### Read Pipeline (Direct SQLite)

Joplin runs as a Flatpak or host application with its state located at `~/.config/joplin-desktop/database.sqlite`. To eliminate lock contention with the active Joplin instance:

1. Open the database using SQLite URI parameters in read-only mode: `file:~/.config/joplin-desktop/database.sqlite?mode=ro`.
2. Apply an explicit timeout: `timeout=5.0`.
3. Execute the recursive Common Table Expression (CTE) to filter out soft-deleted items, sync conflicts, and archive trees while strictly enforcing the notebook whitelist (`1. Tasks`, `2. Dev`, `3. Creative`, `4. Media`, `5. Misc.`).

```python
# SQL Extraction Query
JOPLIN_EXTRACTION_QUERY = """
WITH RECURSIVE live_folders AS (
    SELECT id, parent_id, title 
    FROM folders 
    WHERE deleted_time = 0 
      AND id NOT IN (SELECT item_id FROM deleted_items WHERE item_type = 2)
),
archive_ids AS (
    SELECT id FROM live_folders WHERE title = 'Archive'
    UNION ALL
    SELECT f.id FROM live_folders f JOIN archive_ids a ON f.parent_id = a.id
),
folder_paths AS (
    SELECT id, title, title AS full_path 
    FROM live_folders 
    WHERE (parent_id = '' OR parent_id IS NULL) 
      AND id NOT IN (SELECT id FROM archive_ids)
    UNION ALL
    SELECT f.id, f.title, fp.full_path || ' > ' || f.title 
    FROM live_folders f 
    JOIN folder_paths fp ON f.parent_id = fp.id 
    WHERE f.id NOT IN (SELECT id FROM archive_ids)
)
SELECT 
    n.id,
    fp.full_path, 
    n.title, 
    n.body,
    n.is_todo,
    n.todo_completed,
    n.todo_due
FROM notes n 
JOIN folder_paths fp ON n.parent_id = fp.id 
WHERE n.deleted_time = 0 
  AND n.id NOT IN (SELECT item_id FROM deleted_items WHERE item_type = 1) 
  AND n.is_conflict = 0 
  AND n.encryption_applied = 0 
ORDER BY fp.full_path, n.title;
"""

```

#### Parsing Tasks & In-Note Checkboxes

For each retrieved row:

1. If `is_todo == 1`:
* If `todo_completed != 0`, ignore.
* Otherwise, yield `TaskItem(id=n.id, source_notebook=fp.full_path, title=n.title, ...)`.


2. If `is_todo == 0` (standard Markdown note):
* Scan `n.body` line-by-line using regular expressions:
```python
CHECKBOX_REGEX = re.compile(r"^\s*-\s*\[ \]\s+(.+)$")

```


* For every match, yield a child `TaskItem` referencing the parent note:
* Synthetic ID: `sha256(f"{n.id}_{line_number}")[:16]`
* `is_checkbox_item = True`
* `parent_note_title = n.title`





#### Write Pipeline (Joplin Local Data API)

Direct writes to `database.sqlite` while Joplin is active will corrupt internal indices or trigger sync replication conflicts.

* All mutations (marking a checkbox complete, updating task notes, appending decomposed subtasks) are executed over HTTP via Joplin's native **Local Data API** (`[http://127.0.0.1:41184/](http://127.0.0.1:41184/)`).
* The user configures their Joplin Web Clipper API token in `~/.config/thirties/config.toml`.
* Example endpoint call to update note body: `PUT /notes/{id}` with updated Markdown text containing `- [x]`.

---

### 4.2 Google Calendar Synchronization & Disambiguation

#### Calendar Ingestion

Using Google APIs Client Library (`google-api-python-client`), authenticate with scopes:

* `[https://www.googleapis.com/auth/calendar.readonly](https://www.googleapis.com/auth/calendar.readonly)`
* `[https://www.googleapis.com/auth/calendar.events](https://www.googleapis.com/auth/calendar.events)` (for scheduling requests)

#### Disambiguation Rules for Shared / Family Calendars

The engine enumerates the user’s available calendars via `calendarList().list()`:

* **Primary Calendar**: Events are categorized as hard blocks (`BlockKind.BUSY_CALENDAR`).
* **Shared / Secondary Calendars (e.g., Spouse, Family)**:
* Check event metadata:
1. Look for user's email in `attendees[]`.
2. Inspect `responseStatus`:
* `accepted` $\rightarrow$ Hard block (`BlockKind.BUSY_CALENDAR`).
* `declined` $\rightarrow$ Ignored entirely.
* `needsAction`, `tentative`, or user email is absent $\rightarrow$ Tagged as `BlockKind.AMBIGUOUS_CALENDAR`.




* Ambiguous blocks are preserved on the calendar map with metadata for conversational resolution during the morning briefing.



---


### 4.3 Backlog Taxonomy & Flexible Note Classification

Not all lists and tasks in a user's knowledge vault are created equal. Feeding an entire Joplin vault into a local on-device SLM (such as Gemma-4 E4B) would overwhelm context limits and produce unfocused, generic planning. To allow small local models to punch well above their weight, Thirties establishes a structured note and task taxonomy:

1. **Batch Routine Checklists**:
   * *Characteristics*: Recurring daily habits, morning routines, or wind-down rituals that reset daily.
   * *Scheduling Behavior*: The entire checklist can be batched together and executed inside a single 30-minute block (e.g., Block 1 Sunrise routine). It does not roll over as an overdue project task.
2. **Imminent Obligations**:
   * *Characteristics*: Time-sensitive external commitments, bills, preparations, or hard deadlines with immediate target dates.
   * *Scheduling Behavior*: High-priority candidate for immediate daylight or early dark block allocation. Proactively highlighted by the assistant.
3. **Rolling Focus Tasks**:
   * *Characteristics*: Core project and development tasks (e.g. from `1. Tasks` and `2. Dev`).
   * *Scheduling Behavior*: If uncompleted by day's end, they roll over to the next day with their `deferred_count` incremented. After 3 deferrals, the assistant triggers a decomposition intervention.
4. **Self-Fulfillment & Creative Menus**:
   * *Characteristics*: High-value enriching endeavors (e.g. from `3. Creative` and `4. Media` — composition in Dorico, reading lists, writing).
   * *Scheduling Behavior*: Not treated as nagging chores or strict obligations, but as an *inspiration menu* presented to the user to fill open Daylight Thirties or contemplative Dark Thirties.
5. **Backburner / Scrape-Away Backlog**:
   * *Characteristics*: Non-urgent, low-pressure tasks that can be chipped away at incrementally during buffer blocks.

#### Taxonomy Mapping Configuration
Users can configure their notebook and title mapping in `config.toml`:
```toml
[taxonomy.routines]
notebooks = ["1. Tasks"]
note_patterns = ["*Daily*", "*Routine*", "*Habits*"]
batch_into_single_thirty = true

[taxonomy.creative_menu]
notebooks = ["3. Creative", "4. Media"]
prompt_as_menu = true

[taxonomy.rolling_tasks]
notebooks = ["1. Tasks", "2. Dev"]
track_deferrals = true
```
This enables the core engine to digest, partition, and rank candidate items into structured JSON or concise markdown tiers before presenting them to the model prompt.

## 5. Local State & Configuration

All local data is isolated in XDG-compliant directories:

* Configuration: `~/.config/thirties/config.toml`
* Data & History: `~/.local/share/thirties/`

### 5.1 Configuration Schema (`config.toml`)

```toml
[general]
latitude = 38.8799
longitude = -77.1067
timezone = "America/New_York"
work_start_thirty = 17   # 08:30 AM
work_end_thirty = 31     # 03:30 PM
sleep_start_thirty = 46  # 11:00 PM
sleep_end_thirty = 14    # 07:00 AM

[joplin]
db_path = "~/.config/joplin-desktop/database.sqlite"
api_token = "YOUR_JOPLIN_API_TOKEN"
api_port = 41184
allowed_notebooks = [
    "1. Tasks",
    "2. Dev",
    "3. Creative",
    "4. Media",
    "5. Misc."
]
excluded_notebooks = [
    "Archive"
]
whitelist_notes = [
    "30s Completed Log"
]

[calendar]
google_client_secrets_path = "~/.config/thirties/client_secret.json"
token_storage_path = "~/.local/share/thirties/google_token.json"
primary_calendar_id = "primary"
default_ignore_secondary = false

[inference]
backend = "litert" # "litert" or "ollama"
litert_model_path = "~/.local/share/thirties/models/gemma-4-e2b.litertlm"
ollama_endpoint = "http://localhost:11434"
ollama_model = "gemma4:e4b"
temperature = 0.2

```

### 5.2 Task Rollover & Staleness Store (`state.sqlite`)

A local SQLite database at `~/.local/share/thirties/state.sqlite` tracks task history independent of Joplin:

```sql
CREATE TABLE IF NOT EXISTS task_history (
    task_id TEXT PRIMARY KEY,
    first_scheduled_date TEXT NOT NULL,
    last_scheduled_date TEXT NOT NULL,
    deferred_count INTEGER DEFAULT 0,
    completed_date TEXT,
    allocated_thirties INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS day_snapshots (
    plan_date TEXT PRIMARY KEY,
    plan_json TEXT NOT NULL,
    is_finalized INTEGER DEFAULT 0
);

```

**Task Rollover & Decomposition Rules**:

* When a daily plan solidifies, tasks allocated to that day have `allocated_thirties` incremented.
* If a day passes and a task was not marked complete:
* `deferred_count += 1`.
* If `deferred_count >= 3`, flag for **Decomposition Intervention**. The model proactively suggests breaking the task down into subtasks or moving it back to backlog.



---


### 5.3 Historical Analytics, Velocity & Behavioral Learning

To enable the assistant to give intelligent, personalized suggestions (e.g., recommending realistic composing times rather than arbitrary morning blocks), `state.sqlite` accumulates historical completion metrics and exposes them via SQL views:

```sql
-- Completion history by logical block of the day
CREATE TABLE IF NOT EXISTS block_execution_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_date TEXT NOT NULL,
    logical_block INTEGER NOT NULL,
    task_category TEXT NOT NULL, -- e.g., 'Creative', 'Dev', 'Admin'
    task_id TEXT,
    completed INTEGER DEFAULT 1,
    duration_thirties INTEGER DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Analytical view: Preferred block hours by task category
CREATE VIEW IF NOT EXISTS v_category_block_affinity AS
SELECT 
    task_category,
    logical_block,
    COUNT(*) AS completions_count,
    ROUND(AVG(completed), 2) AS completion_rate
FROM block_execution_history
GROUP BY task_category, logical_block
ORDER BY task_category, completions_count DESC;

-- Analytical view: Average thirties required by task type
CREATE VIEW IF NOT EXISTS v_category_duration_stats AS
SELECT
    task_category,
    ROUND(AVG(duration_thirties), 1) AS avg_thirties,
    MAX(duration_thirties) AS max_thirties
FROM block_execution_history
GROUP BY task_category;
```

**Assistant Integration**:
When the assistant prepares a suggestion for creative work, development, or admin, it queries these views to identify:
1. Which logical blocks the user historically succeeds in completing that category of work.
2. The user's historical pacing (whether composing typically takes 2 thirties rather than 1).
This allows recommendations to be empirically justified rather than arbitrary.

## 6. On-Device SLM Inference & Tool Calling

### 6.1 LiteRT-LM & Ollama Abstraction (`inference.py`)

The inference subsystem defines a standard protocol:

```python
from typing import Any, Dict, List, Protocol


class InferenceEngine(Protocol):

  def generate(self, prompt: str, schema: Optional[Dict[str, Any]]) -> str:
    ...

  def chat(
      self,
      messages: List[Dict[str, str]],
      tools: Optional[List[Dict[str, Any]]],
  ) -> Dict[str, Any]:
    ...

```

1. **LiteRT-LM Engine**: Leverages Google LiteRT-LM Python bindings to run quantized Gemma-4 E2B/E4B `.litertlm` bundles directly on CPU/GPU.
2. **Ollama Fallback Engine**: Calls the local Ollama REST API (`/api/chat`) supporting structured outputs via JSON schema enforcement.

### 6.2 Tool Definitions for the Conversational Loop

When engaging the user, the model has access to explicit tools:

```json
[
  {
    "type": "function",
    "function": {
      "name": "resolve_calendar_event",
      "description": "Mark an ambiguous calendar event as attended (hard block) or ignored.",
      "parameters": {
        "type": "object",
        "properties": {
          "event_id": {"type": "string"},
          "attending": {"type": "boolean"}
        },
        "required": ["event_id", "attending"]
      }
    }
  },
  {
    "type": "function",
    "function": {
      "name": "allocate_thirty_block",
      "description": "Assign a task or label to a specific Thirty index.",
      "parameters": {
        "type": "object",
        "properties": {
          "block_index": {"type": "integer", "minimum": 0, "maximum": 47},
          "task_id": {"type": "string"},
          "custom_label": {"type": "string"}
        },
        "required": ["block_index"]
      }
    }
  },
  {
    "type": "function",
    "function": {
      "name": "create_calendar_entry",
      "description": "Add an appointment or hard block to Google Calendar.",
      "parameters": {
        "type": "object",
        "properties": {
          "summary": {"type": "string"},
          "start_time": {"type": "string", "format": "date-time"},
          "end_time": {"type": "string", "format": "date-time"}
        },
        "required": ["summary", "start_time", "end_time"]
      }
    }
  },
  {
    "type": "function",
    "function": {
      "name": "decompose_task",
      "description": "Break a heavily deferred task into smaller subtasks in Joplin.",
      "parameters": {
        "type": "object",
        "properties": {
          "parent_task_id": {"type": "string"},
          "subtasks": {
            "type": "array",
            "items": {"type": "string"}
          }
        },
        "required": ["parent_task_id", "subtasks"]
      }
    }
  },
  {
    "type": "function",
    "function": {
      "name": "finalize_day_plan",
      "description": "Lock in the day's schedule once the user is satisfied.",
      "parameters": {
        "type": "object",
        "properties": {
          "notes": {"type": "string"}
        }
      }
    }
  }
]

```

### 6.3 System Prompt Blueprint

```text
You are the 30s Planning Agent. You organize the user's day into 48 discrete 30-minute intervals (0-47).
You do not speak in granular minutes or seconds. You speak exclusively in units of "Thirties", Daylight Thirties, and Dark Thirties.

CURRENT ASTRONOMICAL CONTEXT:
- Sunrise: {sunrise_time} (Block {sunrise_block})
- Sunset: {sunset_time} (Block {sunset_block})
- Available Daylight Thirties: {daylight_count}
- Available Dark Thirties: {dark_count}

DETERMINISTIC CONSTRAINTS:
- Sleep Blocks: {sleep_blocks}
- Work Blocks: {work_blocks}
- Hard Calendar Blocks: {busy_calendar_blocks}

AMBIGUOUS EVENTS REQUIRING CLARIFICATION:
{ambiguous_calendar_list}

TOP BACKLOG TASKS:
{candidate_tasks_json}

BEHAVIOR RULES:
1. Always resolve ambiguous calendar commitments first by asking the user directly.
2. Respect the user's daily energy level. High focus and creative composition work belongs in Daylight Thirties; administrative tasks, reading, and light dev tasks fit into Dark Thirties.
3. If a task has been deferred >= 3 times, actively suggest breaking it down into smaller subtasks or deferring it back to the Joplin backlog.
4. Execute tool calls to assign blocks or adjust calendar entries when the user agrees. Never hallucinate available time.

```

---


### 6.3 Temporal Awareness, Duration Arithmetic & Schedule Mutators

To prevent hallucinated past allocations, incorrect duration math, and over-eager scheduling, the conversational prompt and parser enforce strict boundaries:
* **Current Clock Time & Active Block**: The prompt explicitly identifies current local time and the active block.
* **Thirty Arithmetic & Multi-Block Allocation**:
  * 1 block = 30 minutes, 2 blocks = 1 hour, 4 blocks = 2 hours ($N$ hours $= 	ext{round}(N 	imes 2)$ blocks).
  * When a duration or span is requested (e.g. "Block 28 for at least two hours"):
    * Start block: 28, Span: 4 blocks $ightarrow$ Blocks 28 through 31 (e.g. 08:30 PM to 10:30 PM).
    * Model outputs multi-block directive: `ALLOCATE_BLOCKS: 28-31 | Composing`.
    * Engine assigns all 4 blocks simultaneously and updates the daily plan and state store.
* **Schedule Deallocation & Days Off**:
  * When the user takes a day off ("I don't have work today", "clear my work blocks"):
    * Model outputs `CLEAR_WORK_BLOCKS` (or engine detects user intent).
    * All 15 work blocks are unlocked and converted to open discretionary daylight/dark blocks, immediately increasing available discretionary tallies.
  * When clearing specific blocks ("clear blocks 4-18", "unassign block 28"):
    * Model outputs `CLEAR_BLOCKS: <start>-<end>`.
* **Separation of Suggestions vs. Allocations**:
  * *Suggestion Requests* ("Where would you suggest I compose?", "What should I do next?"): Proposes 1–2 upcoming open blocks with concise reasoning based on energy affinity and asks for confirmation. **Must not** emit `ALLOCATE_BLOCK`.
  * *Direct Commands & Confirmations* ("Allocate block 20 to Dorico", "Yes, let's do that"): Emits `ALLOCATE_BLOCK(S): ...`.
* **Forward Planning vs. Retrospective Logging**: Forward suggestions are strictly limited to upcoming open blocks. The assistant only references or schedules past blocks when the user explicitly requests retroactive logging of completed work ("Earlier this morning at 8:00 AM I finished X").

### 6.4 Vault-Backed Cross-Device State Sync ("Hidden Joplin Sync Note")

#### The Architecture Dilemma
Joplin notes sync seamlessly across all user devices (via Dropbox, Nextcloud, Joplin Cloud, or WebDAV), and Google Calendar / Evolution Data Server syncs via standard CalDAV/Google protocols. However, the local assistant state (`state.sqlite` — containing conversation history, deferral counts, task execution history, and learned user statistics) is local to each machine.
Requiring users to host an external Docker/NAS database or configuring a third-party cloud service introduces unwanted operational overhead.

#### The Hidden Joplin Note Solution
Thirties implements a zero-infrastructure cross-device sync mechanism by leveraging Joplin's existing multi-device sync engine:
1. **Sync Storage Note**: Thirties creates and maintains a dedicated sync state note (e.g. title: `.thirties_state_vault`) located inside the excluded `Archive` notebook.
2. **Data Payload**: The note body stores a structured, compressed JSON delta log (or base64-encoded SQLite snapshot/WAL transaction log) containing:
   * Block allocation history and finalized day plans.
   * Task deferral counters and rollover history.
   * Learned category affinities and completion statistics.
3. **Sync Lifecycle**:
   * **On Startup / Refresh**: Thirties inspects the sync note via the Joplin Local Data API or read-only SQLite database. If the remote revision timestamp is newer than the local `state.sqlite`, deltas are merged into the local SQLite database.
   * **On Day Finalization / Plan Mutation**: When the user finalizes a day plan or allocates blocks, Thirties generates a delta update and writes it to the sync note via Joplin's Local Data API (`PUT /notes/{sync_note_id}`).
   * **Joplin Native Transport**: Joplin's background sync automatically propagates the note to all other laptops and workstations without Thirties needing any external server.


### 6.5 Multi-Session Chat Threads, Context Economy & Hierarchical Memory

#### Thread Management & Context Economy
Small on-device SLMs (such as Gemma-4 E4B with ~32k context) incur quadratic KV cache latency and memory penalties as conversations grow long. To keep inference snappy:
1. **Multiple Daily Threads**: The UI provides a chat session list allowing multiple planning conversations per day.
2. **Scope Filter**: Users can toggle between "Today's Chats" and "All Chats".
3. **Fresh Thread Steering**: When a planning topic concludes, users are steered toward a clean thread rather than carrying monolithic context.

#### Hierarchical Memory Architecture (Agent Memory vs. User Profile)
To support cross-conversation recall ("Remember a few days ago we talked about squeezing in exercise? Can we prioritize that today?"), Thirties implements a two-tier memory architecture:

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                             Active Planning Turn                            │
│           (Slim System Prompt + Astronomical State + Top 10 Tasks)           │
└──────────────────────────────────────┬──────────────────────────────────────┘
                                       │
                ┌──────────────────────┴──────────────────────┐
                ▼                                             ▼
┌──────────────────────────────┐              ┌──────────────────────────────┐
│        Agent Memory          │              │     User Profile Memory      │
│  (Episodic Chat Summaries)   │              │   (Multi-Horizon Profile)    │
├──────────────────────────────┤              ├──────────────────────────────┤
│ • "Oct 06: User allocated    │              │ Tier 1: Daily Habits         │
│   Block 20 to exercise"      │              │   • Morning routine Block 1  │
│ • "Oct 08: User took day     │              │ Tier 2: Short-Term Focus     │
│   off from work blocks"      │              │   • "Prioritize health/doc"  │
│ • "Oct 08: Dorico composing  │              │ Tier 3: Long-Term Vision     │
│   took 4 blocks (2 hours)"   │              │   • "[Oct 08, 2025] Release  │
│                              │              │     creative infrastructure" │
└──────────────────────────────┘              └──────────────────────────────┘
```

1. **Agent Memory (Episodic Summarizer)**:
   * When a thread concludes, a background SLM pass generates a 1–2 sentence factual summary of decisions made and task completions.
   * Stored in `state.sqlite` with topic tags for vector or keyword retrieval.
2. **User Profile Memory (Hierarchical Horizons)**:
   * **Daily / Immediate**: Operating habits, wake/sleep targets, recurring rhythms.
   * **Short-Term Horizon (Monthly / Seasonal)**: Current areas of focus (e.g., medical checkups, sprint on album demo).
   * **Long-Term Vision (Multi-Year)**: Big aspirations with origin date stamps (e.g., "[First noted Oct 8, 2025]: Establish creative release infrastructure and publishing website").
3. **Cross-Thread Recall Protocol**:
   * When a user prompt references past conversations or long-term goals, the system retrieves relevant profile memory facts into prompt context rather than loading hundreds of previous chat turns.

## 7. GNOME HIG Desktop Application (`thirties_gtk`)

The UI is built with **PyGObject** targeting **GTK 4** and **Libadwaita 1.5+**, adhering closely to GNOME Human Interface Guidelines (HIG).

### 7.1 Core Layout: View Switcher Architecture (Pattern A)

Rather than cluttering the screen with a persistent, cramped right sidebar, Thirties employs an `AdwViewSwitcher` in the header bar with two focused, distraction-free views:
1. **Schedule View**: A spacious, uncluttered presentation of the Solar Arc and daily blocks.
2. **Assistant View**: A dedicated, full-height conversational stream for negotiating the day.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│ [<] 📅 [>] [Today Icon]      [  Schedule  |  Assistant  ]                ⚙  │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│                        ☼ Solar Arc & Trajectory                             │
│                  ☼ Sunrise: 7:10 AM  •  ☾ Sunset: 6:41 PM                   │
│                    [ Daylight: 14 ]   [ Dark: 10 ]                          │
│                                                                             │
│  ┌───────────────────────────────────────────────────────────────────────┐  │
│  │ [16 Thirties] 11:00 PM – 07:00 AM • Sleep (8.0 hrs) [▼ Collapsed]     │  │
│  │ 14 [07:00 AM] Open Daylight Thirty                                    │  │
│  │ ...                                                                   │  │
│  │ [15 Thirties] 08:30 AM – 03:30 PM • Work (7.5 hrs)  [▼ Collapsed]     │  │
│  │ 32 [04:00 PM] Dorico Compose                                          │  │
│  │ ...                                                                   │  │
│  └───────────────────────────────────────────────────────────────────────┘  │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 7.2 Broad Brushstrokes & Collapsed Locked Chunks
The app prioritizes big time, broad brushstrokes, and low cognitive noise:
* Contiguous locked blocks (e.g., 16 sleep blocks from 11 PM to 7 AM, or 15 work blocks from 8:30 AM to 3:30 PM) are automatically grouped into single consolidated overview cards displaying total duration and interval ranges.
* Clicking a grouped card expands/collapses the underlying discrete 30-minute intervals.
* Discretionary blocks and active tasks stand out prominently as individual units of opportunity.





---


### 7.3 Nocturnal Lunar Arc & Nighttime Continuity

When viewing the schedule during nighttime (after sunset or before sunrise), the Solar Arc transforms into the **Nocturnal Lunar Arc**:
* **Cairo Styling**:
  * Daylight parabolic arc: Golden amber `rgba(0.95, 0.72, 0.20, 0.80)`.
  * Nocturnal parabolic arc: Cool twilight blue / indigo `rgba(0.40, 0.62, 0.95, 0.85)`.
* **Luminous Moon Disc**:
  * Moon marker rendered at the current position along the nocturnal trajectory with a cool cyan/indigo halo (`rgba(0.40, 0.65, 1.0, 0.35)`) and silver core disc (`rgba(0.90, 0.95, 1.0, 0.95)`).
* **Astronomical Lunar Phase**:
  * The center label displays the real-time calculated synodic lunar phase glyph and name (e.g. `🌑 New Moon`, `🌓 First Quarter`, `🌕 Full Moon`, `🌘 Waning Crescent`).
* **Temporal Continuity Across Midnight**:
  * The nocturnal thirties (Blocks 24 to 48) logically belong to the preceding day's waking cycle.
  * Past midnight but before sunrise, the active day remains anchored to the logical day, presenting clear nocturnal context (e.g. "Tonight — Friday, Oct 09 into Saturday, Oct 10") preserving 48-block continuity until sunrise.

---

## 8. Step-by-Step Implementation Roadmap for Antigravity

Antigravity agents should execute the implementation according to these numbered milestones:

### Milestone 1: Environment & Core Data Structures

1. Initialize the Python package with `pyproject.toml` (dependencies: `astral`, `google-api-python-client`, `google-auth-oauthlib`, `pygobject`, `litert-lm`).
2. Implement `thirties_core/models.py` with `ThirtyBlock`, `TaskItem`, and `DayPlan`.
3. Implement `thirties_core/astronomy.py` with functions:
* `get_solar_phases(lat: float, lon: float, target_date: date) -> Dict[str, datetime]`
* `build_base_thirties(lat: float, lon: float, target_date: date) -> List[ThirtyBlock]`


4. Write unit tests in `tests/test_astronomy.py` validating that 48 blocks are generated and midday blocks report `is_sunlight == True`.

### Milestone 2: Joplin Ingestion & Whitelisting

1. Implement `thirties_core/joplin_engine.py`:
* SQLite reader opening `~/.config/joplin-desktop/database.sqlite` in `?mode=ro`.
* Implement the recursive CTE query filtering out `Archive` notebooks and soft deletes.
* Add regex extraction for `- [ ] ` checkboxes from note markdown bodies.
* Add HTTP client wrapper for Joplin Local Data API (`[http://127.0.0.1:41184/](http://127.0.0.1:41184/)`) to support updating checkboxes.


2. Write unit tests against a temporary fixture SQLite database verifying that deleted notes, conflict notes, and archive notebooks are omitted.

### Milestone 3: Google Calendar Integration

1. Implement `thirties_core/calendar_engine.py`:
* OAuth2 token handshake and cached credential loading from `token_storage_path`.
* `fetch_events_for_day(target_date: date) -> List[CalendarEvent]`
* Event parsing logic mapping UTC/local start-end ranges to Thirty indices $[0, 47]$.
* Multi-calendar attendee inspection flagging `AMBIGUOUS_CALENDAR` blocks.


2. Implement mapping tests verifying event overlaps mark the correct indices in a 48-element array.

### Milestone 4: Deterministic Scheduler & State Database

1. Implement `thirties_core/scheduler.py`:
* Overlay work hours, sleep hours, and hard calendar blocks onto the base astronomical grid.
* Calculate exact counts: `daylight_discretionary_count`, `dark_discretionary_count`.


2. Implement `state.sqlite` schema and storage routines for tracking task deferral counts and daily plan snapshots.

### Milestone 5: Inference Layer & Conversational Engine

1. Implement `thirties_core/inference.py`:
* Implement `LiteRTInferenceEngine` loading `.litertlm` model files.
* Implement `OllamaInferenceEngine` calling `http://localhost:11434/api/chat`.


2. Implement `thirties_core/conversation.py`:
* Construct system prompt with solar parameters and available Thirty tallies.
* Bind the JSON tool call schema.
* Handle tool-execution dispatching (`resolve_calendar_event`, `allocate_thirty_block`, `decompose_task`, `finalize_day_plan`).



### Milestone 6: GNOME Libadwaita Interface

1. Scaffold `thirties_gtk/main.py` using `Adw.Application`.
2. Implement `block_widget.py` rendering individual Thirty blocks with daylight/dark visual styling.
3. Implement `day_view.py` as an `Adw.Clamp` containing the 48-block scrollable list.
4. Implement `chat_panel.py` with an entry bar and message list connected asynchronously to `conversation.py`.

---

## 9. Verification & Acceptance Criteria

* **Deterministic Grid Accuracy**: The system must consistently produce exactly 48 blocks per 24-hour cycle. Noon blocks during summer months must register as sunlight; midnight blocks must register as dark.
* **Database Safety**: Reading Joplin's `database.sqlite` while Joplin Desktop is open and actively syncing must never produce unhandled lock exceptions or database write locks.
* **Time Calculation Integrity**: The LLM must never be allowed to mathematically calculate remaining daylight or dark blocks; it must receive deterministic integer counts directly from `scheduler.py`.
* **Zero Leakage of Archived Items**: Tasks or notes residing in `Archive` or with `deleted_time > 0` must not appear in the candidate task backlog.