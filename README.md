# 🛡️ MapMantra — AI Compliance Intelligence System

**Multi-agent regulatory compliance automation for Indian banks.**
Fully local. No paid APIs. No cloud. No internet after setup.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                    MapMantra System                              │
│                                                                  │
│  ┌─────────┐  ┌───────────┐  ┌────────┐  ┌──────────────────┐  │
│  │Ingestion│→ │ Extractor  │→ │Conflict│→ │     Router       │  │
│  │(4 opts) │  │ (qwen3:8b)│  │Detector│  │ (Sibling MAPs)   │  │
│  └─────────┘  └───────────┘  └────────┘  └──────────────────┘  │
│       │              │             │              │              │
│       ▼              ▼             ▼              ▼              │
│  ┌─────────────────────────────────────────────────────────────┐│
│  │                     SQLite Database                         ││
│  │  regulations │ maps │ evidence │ conflicts │ audit_log      ││
│  └─────────────────────────────────────────────────────────────┘│
│       │              │                        │                 │
│       ▼              ▼                        ▼                 │
│  ┌──────────┐  ┌───────────┐  ┌──────────┐  ┌──────────────┐  │
│  │  Threat  │  │ Validator │  │Supervisor│  │ Risk Engine  │  │
│  │  Mapper  │  │(AI Audit) │  │(LangGraph│  │ (Arithmetic) │  │
│  └──────────┘  └───────────┘  │ Pipeline)│  └──────────────┘  │
│                                └──────────┘                     │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────────┐│
│  │            FastAPI Backend & React UI (port 8001)            ││
│  └──────────────────────────────────────────────────────────────┘│
└──────────────────────────────────────────────────────────────────┘
```

## Quick Start

### 1. Prerequisites

```bash
# Install Ollama (https://ollama.com)
# Pull required models
ollama pull qwen3:8b
ollama pull nomic-embed-text
```

### 2. Install Dependencies

```bash
cd d:\Mapmantra
pip install -r requirements.txt

# For Playwright browser automation (Option 2 ingestion)
playwright install chromium
```

### 3. Configure Environment

```bash
copy .env.example .env
# Edit .env if you have email credentials for Option 3
```

### 4. Start the Server & Dashboard

```bash
python main.py
# Server and premium React UI will run at http://localhost:8001/
# API docs are available at http://localhost:8001/docs
```

---

## Ingestion Pipeline (4 Options)

| Priority | Method | Description |
|----------|--------|-------------|
| 1 | **RSS Feeds** | Official XML feeds from RBI, SEBI, IRDAI |
| 2 | **Playwright** | Headless Chromium scraper for circular pages |
| 3 | **Email IMAP** | Ingests PDF attachments from official regulator emails |
| 4 | **Manual Upload** | Dashboard form — always available |

Each method logs failures and gracefully falls back to the next.

## Agent Architecture

| Agent | Model | Purpose |
|-------|-------|---------|
| Ingestion | None (Python) | Fetches regulatory circulars |
| Extractor | qwen3:8b | Extracts compliance actions (MAPs) |
| Router | qwen3:8b | Assigns MAPs to departments |
| Conflict Detector | qwen3:8b + nomic-embed-text | Detects regulatory conflicts |
| Threat Mapper | qwen3:8b | Maps to MITRE ATT&CK |
| Validator | qwen3:8b | Evaluates evidence against 4 criteria |
| Supervisor | LangGraph (no LLM) | Orchestrates pipeline + maintenance |
| Risk Engine | None (arithmetic) | Calculates risk scores |

## Sibling MAP Architecture

When the Router assigns a compliance action to multiple departments:
- **Original MAP** → Updated to first department (parent)
- **Sibling MAPs** → Created for remaining departments with `parent_map_id` pointing to original
- Each department submits evidence independently
- Regulation resolved only when ALL siblings are completed

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/dashboard` | Dashboard summary |
| GET | `/api/regulations` | List circulars |
| POST | `/api/regulations/upload` | Manual upload |
| GET | `/api/maps` | List MAPs |
| GET | `/api/maps/{id}` | MAP detail + siblings |
| POST | `/api/maps/{id}/submit-evidence` | Submit evidence |
| GET | `/api/conflicts` | List conflicts |
| GET | `/api/threat-intel` | ATT&CK coverage |
| GET | `/api/audit/logs` | Audit trail |
| GET | `/api/audit/verify` | Verify hash chain |
| GET | `/api/risk-report` | Full risk report |
| POST | `/api/pipeline/trigger` | Run full pipeline |
| POST | `/api/ingestion/trigger` | Run ingestion |

## Risk Score Formula

```
score = overdue_high × 12
      + overdue_medium × 6
      + overdue_low × 3
      + conflicts_unresolved × 8
      + pending_high × 3
      − completed_this_month × 2

Clamped: 0 ≤ score ≤ 100
```

## Financial Exposure

| Regulator | Min Penalty | Max Penalty |
|-----------|-------------|-------------|
| RBI | ₹2 Crores | ₹50 Crores |
| SEBI | ₹1 Crore | ₹25 Crores |
| IRDAI | ₹50 Lakhs | ₹5 Crores |

Per overdue MAP. Summed across all overdue MAPs for total exposure range.
