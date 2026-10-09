# Software Design Document: `30s` (Diurnal Natural Time Scheduler)

---

## 1. System Overview & Core Philosophy

**`30s`** is a personal scheduling and time-awareness application engineered around diurnal cycles and discrete half-hour human cognitive intervals ("Thirties"). Modern time management frequently suffers from false precision: minute-by-minute calendar tetris increases cognitive friction and fails as soon as a single task slips.

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
You are the 30s Diurnal Planning Agent. You organize the user's day into 48 discrete 30-minute intervals (0-47).
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

## 7. GNOME HIG Desktop Application (`thirties_gtk`)

The UI is built with **PyGObject** targeting **GTK 4** and **Libadwaita 1.5+**.

### 7.1 Visual Layout & Components

```
┌─────────────────────────────────────────────────────────────────────────────┐
│ 30s                                                        —  □  ✕          │
├─────────────────────────────────────┬───────────────────────────────────────┤
│          Diurnal Arc & Timeline     │         Agent Planning Chat           │
├─────────────────────────────────────┼───────────────────────────────────────┤
│                                     │                                       │
│        ☼ Daylight (14 Thirties)     │  Agent: Good morning! You finish work │
│  ┌────────────────────────────────┐ │  at block 31 (3:30 PM). That leaves   │
│  │ 22 [11:00 AM] Work             │ │  2 Daylight Thirties and 8 Dark       │
│  │ 23 [11:30 AM] Work             │ │  Thirties before sleep.               │
│  │ 24 [12:00 PM] Lunch Break      │ │                                       │
│  │ ...                            │ │  Rachel has "Doctor Appointment" at   │
│  │ 31 [03:30 PM] Work Ends        │ │  11:30 AM. Are you attending?        │
│  │ 32 [04:00 PM] [Dorico Compose] │ │                                       │
│  │ 33 [04:30 PM] [Dorico Compose] │ │  User: No, skipping that. Let's make  │
│  └────────────────────────────────┘ │  tonight light, focusing on creative. │
│                                     │                                       │
│        ☾ Dark (10 Thirties)         │  Agent: Done. I allocated blocks 32-33│
│  ┌────────────────────────────────┐ │  to Dorico, and left evening dark     │
│  │ 34 [05:00 PM] Free / Dinner    │ │  thirties open for reading.           │
│  │ 35 [05:30 PM] Free             │ │                                       │
│  │ ...                            │ │                                       │
│  │ 46 [11:00 PM] Sleep            │ │                                       │
│  └────────────────────────────────┘ │                                       │
│                                     │                                       │
├─────────────────────────────────────┴───────────────────────────────────────┤
│ [ Input message...                                                ] [ Send ]│
└─────────────────────────────────────────────────────────────────────────────┘

```

1. **Header Bar**: Standard `AdwHeaderBar` with view switchers and date navigation.
2. **Main View (`AdwNavigationSplitView`)**:
* **Left Panel (Content Area)**:
* **Solar Arc (`solar_arc.py`)**: Custom Cairo-drawn or CSS-styled arc displaying the sun's trajectory with current time position.
* **Block Matrix (`day_view.py`)**: Scrollable list of 48 discrete pills (`block_widget.py`).
* Golden/Amber accent for Daylight blocks.
* Deep Indigo/Slate accent for Dark blocks.
* Striated fill for Work/Sleep locked blocks.
* Clear label displaying assigned task.




* **Right Panel (Sidebar)**:
* Interactive conversational stream (`chat_panel.py`).
* Renders message bubbles, quick confirmation pills (e.g., `[Yes, Attending]`, `[Decline]`), and plan finalization triggers.





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