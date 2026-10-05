# Fully Automated AI Refactoring Experiment: Java → Python

**English** | [繁體中文](README.md)

> **79 functions, 10 agents, translated from Java to Python automatically and verified by the same test suite. Best run: 20 of 25 tests passed.**
> Three versions, dozens of runs, with success rates, cost, and hours all published, including what it can't do.

## Highlights

- **Multi-agent division of labor.** 10 independent agents wired into a directed graph by LangGraph; parsing and recording, design and planning run in parallel. Code controls the flow and the LLM is used only where judgment is needed; half of the agents never call AI.
- **Tests wrapped around the refactor.** Record the Java service's responses first; the Python service counts as correct only if it passes the same tests.
- **Auto-generated spec + hand-filled collection, test scope aligned with translation scope.** The OpenAPI spec comes from the running Java service, and write cases are filled with real data by a human; for the 9 APIs a human marked as not tested, the 18 methods used only by them are excluded from translation automatically, and all other 24 APIs have a test case (24/24).
- **Found and removed the real bottleneck.** Version 3 lets Claude read the Java source directly, and the average success rate of the top three runs reaches **76%**.
- **Three gates, in order.** Foundation parts → business logic → top-level entry points; later layers read the already-translated Python.
- **Map-reduce for large Claude jobs.** When the input is too big for one prompt, it is split along natural boundaries (controllers, API groups), analyzed in parallel, then merged for cross-boundary decisions; concurrency is computed from the CPU core count minus one.
- **Cloud + local models, automatic fallback.** When the local `qwen2.5-coder:32b` fails, it switches to Claude and the pipeline keeps going.
- **Every AI call is traceable.** A `running` record is written before the call goes out, then completed under the same `trace_id` with the response, tokens, and latency, so a hung or timed-out call still leaves its prompt behind. One SQLite database with an FTS5 full-text index records both Claude and the local model; `llmlog` searches it and tags hallucinations and format errors.
- **Verify in 1–2 minutes after an edit.** Docker hot reload, and `partial_verify.py` skips rerunning the whole pipeline.
- **An honest experiment log.** About 372 hours, about NT$100 per run, about NT$3,700 in total, with the failures written down too.

---

## About the Project

A multi-agent pipeline that automatically translates a Java Spring Boot backend into a Python FastAPI backend, and uses "tests wrapped around the refactor" to verify the translated service behaves like the original.

This is an experiment. The goal is to find out how far fully automated AI refactoring can actually go, and how much time and money it costs. The conclusions are in the last section.

Core principle: **record the responses of the running Java service first (golden output); after translating, run the same tests against the Python service. The refactor passes only when both sides respond identically.**

> Note: the detailed design documents under `docs/` are written in Traditional Chinese.

---

## 1. Libraries and Technologies

### Orchestration and AI

| Category | Technology | Purpose |
|---|---|---|
| Orchestration | LangGraph 1.2.6 | Wires the agents into a directed graph with parallel branches, a retry loop, and a manual-fill gate |
| Cloud model | Claude API (`anthropic` 0.117.0, `claude-sonnet-4-6` in practice) | Parsing, design, translation (the vast majority of functions), debugging |
| Local model | ollama + `qwen2.5-coder:32b` (behind an nginx token-auth proxy) | Originally translated everything; the current design reserves it for the repository layer only, and in practice it now falls back to Claude |
| Java parsing | `javalang` | Parses Java source, builds the call graph, extracts method source |

### Testing and verification

| Category | Technology | Purpose |
|---|---|---|
| API spec | springdoc-openapi-ui 1.7.0 (Java side) | Generates OpenAPI 3.0 from Spring annotations |
| Test scripts | openapi-to-postmanv2, newman (Node.js) | Converts OpenAPI to a Postman Collection and runs it |
| Response comparison | `deepdiff`, `PyYAML` | Masks dynamic fields, then diffs against golden output |
| Database | PostgreSQL (a separate `_TEST` DB), `psycopg2-binary` | Truncate + re-seed before every verification |
| Target service runtime | Docker (`python:3.12-slim`) | Runs the generated Python service in a Linux container with hot reload |

### Other

| Category | Technology |
|---|---|
| Runtime | Python 3.13 (orchestrator), `httpx`, `requests`, `python-dotenv` |
| Dev tools | `pytest`, `ruff` |
| Call logging | SQLite (`logs/llm_traces.db`) + the `llmlog` CLI, shared by Claude and the local model |
| Source project | Java 8+ / Spring Boot 2.7.11 (`lang-exam-api-refactor`) |
| Target project | Python 3.10+ / FastAPI, SQLAlchemy, Pydantic, uvicorn |

---

## 2. Overall Architecture

### Execution flow

```
Java project
   ↓
[A] Spec Agent         Starts the Java service and fetches the OpenAPI spec
   ↓
[B] Collection Agent   Converts it to a Postman Collection (has a manual-fill gate; pauses if not filled)
   ↓
① Parse Agent          Parses Java; outputs the module list and call graph
   ↓
 ┌─┴────────────────────────┐   can run in parallel
② Test Agent (record)     ③ Design Agent
  Records golden output       Designs the Python project structure
  from the Java service       and function signatures
 └─┬────────────────────────┘
   ↓
 ┌─┴─────────────────┐   can run in parallel
[P] Plan Agent         ④ Scaffold Agent
  Orders the work,        Creates the empty skeleton
  finds related code
 └─┬─────────────────┘
   ↓
⑤ Implement Agent      Claude reads the Java source directly and translates it to Python, in three gates
   ↑                      Gate 1 foundation parts → Gate 2 business logic → Gate 3 top-level entry points
   │      ↓
   │   ⑥ Test-Run Agent (verify)  Runs the same tests against the Python service, compares with golden
   │      ↓
   │    all pass ─────────────▶ ✅ done
   │      ↓ failures
   └── ⑦ Debug Agent      Analyzes diffs, writes and applies fixes directly, returns to ⑤ to re-verify
          ↓ retry limit exceeded, or judged unfixable
       give_up (notify a human)
```

### Directory layout

| Directory / file | Contents |
|---|---|
| `main.py` | Entry point: builds and runs the graph, backs up the previous run's output, writes the run report |
| `partial_verify.py` | Partial verification: skips the full pipeline and runs selected test cases against an already generated Python service |
| `graph/` | LangGraph state, nodes, the scheduler (three-gate ordering), source extraction |
| `spec_collection_agent/` | Implementation of [A] and [B] |
| `parse_agent/`, `design_agent/`, `plan_agent/`, `scaffold_agent/`, `debug_agent/` | Implementations of ①, ③, [P], ④, ⑦ |
| `translator_cli/` | The tool that writes code: scaffold mode and fill-in mode (backend is Claude or the local model) |
| `refactor_harness/` | Test harness: record, verify, mask, diff, report (shared by ② and ⑥) |
| `python_service/` | Docker container management for the generated Python service |
| `common/` | Shared utilities across agents (Claude client, call logging, type mapping, etc.) |
| `llmlog/` | CLI for querying LLM call logs |
| `config/`, `specs/`, `postman/`, `fixtures/` | Config, OpenAPI spec, Postman Collections, seed data and golden output |
| `docs/` | Design docs (`*a`) and code docs (`*b`) for each agent, plus result reports (Traditional Chinese) |
| `tests/` | Unit tests |

---

## 3. What Each Agent Does

| Agent | What it does | Uses AI? |
|---|---|---|
| **[A] Spec Agent** | Starts the Java service and downloads the full OpenAPI spec | No |
| **[B] Collection Agent** | Converts the spec into Postman test scripts; detects dependencies between APIs (create first, then query); hands cases that need real data to a human to fill in | A little (Claude) |
| **① Parse Agent** | Reads Java source, splits it into modules, builds the call graph, produces the API mapping table | Yes (Claude) |
| **② Test Agent** | Runs the tests against the Java service and stores each API's response as golden output | No |
| **③ Design Agent** | Designs the Python project's directories, files, and each function's signature | Yes (Claude) |
| **[P] Plan Agent** | Orders the translation work and finds, for each function, the other code it uses | No (purely mechanical) |
| **④ Scaffold Agent** | Creates directories and empty functions from the design, and generates table models from Java entities | No (purely mechanical) |
| **⑤ Implement Agent** | Translates each function to Python from the Java source; three gates, the next gate starts only after the previous one fully completes | Yes (Claude; the repository layer is designed for the local model) |
| **⑥ Test-Run Agent** | Runs the same tests against the Python service, compares with golden output, produces a report | No |
| **⑦ Debug Agent** | Reads the failure report and the code, finds the cause, writes and applies the fix directly | Yes (Claude) |

---

## 4. Development Time and Cost

### Development time (estimate)

**About 372 hours (about 46 eight-hour working days)**, from 2026-07-21 to 10-02.

| Phase | Estimate |
|---|---|
| Design and build each step | about 159 hours |
| Version 1: running it for real, verification and debugging | about 106 hours |
| Version 2 (Claude + task memos) | about 27 hours |
| Version 3 (Claude reads Java source directly, three gates) | about 81 hours |

Estimated from self-reported working hours: about 2 weeks of weekdays at 9:00–23:00 (140 h) + the other 25 weekdays at 8 h (200 h) + weekends counted as 4 days (32 h); the per-phase split follows the proportions in the chat logs.

### Claude API cost (assuming US$1 ≈ NT$32)

| Version | Cost of one full run | Average success rate of the top three runs |
|---|---|---|
| Version 1: local model + task memos | about US$2.1 (about **NT$66**) | about 30% |
| Version 2: Claude + task memos | about US$3.15 (about **NT$100**) | about 62% |
| Version 3 (current): Claude reads Java source directly | about US$3.0–3.3 (about **NT$100**) | about 76% |

The whole refactor (including all experiments and reruns) cost about US$114 in total, **about NT$3,700**. The local model is free to run. These are estimates, not an invoice.

"Task memo" means the short per-function description that an LLM wrote and the translating model then worked from (versions 1 and 2).

---

## 5. Takeaways and Conclusions

- **How the versions evolved:** Version 1 had a local model translate from "task memos", and its best success rate was only about 30%. Version 2 swapped in Claude and results were still erratic. The problem was the memos, which left out or distorted details of the Java code. So version 3 dropped the memos and let Claude read the source directly, in a fixed order, which raised the success rate to about 76% without relying on heavy after-the-fact patching.
- **AI tends to "fix A, break B":** the bigger the project, the more the documentation lags behind; one wrong edit makes later edits build on a mistake and trigger different bugs. Repeated logic should be extracted into shared blocks rather than copied and maintained separately.
- **The engineer's role is essential:** AI is very good at small pieces, but the overall architecture needs an engineer to set direction. When AI heads the wrong way, an engineer has to pull it back, and an engineer's intuition is often more accurate than AI's judgment, which stops AI from guessing.
- **Full automation can't reach 100%:** AI retrying on its own does not find the right answer; once the direction is wrong it only drifts further. It takes an engineer questioning the direction and fixing the root cause to hit the core problem.
- **When automation pays off:** when there is a large volume of work in a fairly fixed format, so automation gets economies of scale. Building a whole refactoring-automation system for a single project is using a sledgehammer to crack a nut.

**Conclusion: senior engineers working together with AI get the most value. Rather than chase perfect full automation, correct course step by step so that every step rests on solid ground, and the result doesn't grow crooked.**

---

## Environment Setup (`.env` template)

`.env` holds keys and connection details and is not uploaded to git (it is listed in `.gitignore`). Create `.env` in the project root yourself and replace the values in angle brackets with your own:

```ini
# Claude API
ANTHROPIC_API_KEY=<your Anthropic API key>
SPEC_COLLECTION_AGENT_MODEL=claude-sonnet-4-6

# Project paths (relative to this project's folder; the Java project and the Python target project sit next to this project, each independent)
JAVA_PROJECT_PATH=../lang-exam-api-refactor
PYTHON_PROJECT_PATH=../exam-platform-api

# Java service ([A] and ② start it with java -jar)
JAVA_BASE_URL=http://localhost:8080
JAVA_JAR_PATH=../lang-exam-api-refactor/target/lang-exam-api-1.0.1.jar
JAVA_EXECUTABLE_PATH=<full path to the java executable, or just java if the version on PATH is correct>

# Target Python service (address exposed by the Docker container)
PYTHON_BASE_URL=http://localhost:8000

# Local model: address and token of the nginx in front of ollama (only needed when qwen is used)
OLLAMA_BASE_URL=http://<local model machine IP>:<nginx port>/v1
OLLAMA_API_KEY=<token configured in nginx>

# Test DB (separate from the production/dev DB, name ends in _TEST; the harness truncates and re-seeds it before every verification)
TEST_DB_DSN=postgresql://postgres:<DB password>@127.0.0.1:5432/MOC_MATSUEXAM_TEST
SPRING_DATASOURCE_URL=jdbc:postgresql://127.0.0.1:5432/MOC_MATSUEXAM_TEST
SPRING_DATASOURCE_USERNAME=postgres
SPRING_DATASOURCE_PASSWORD=<DB password>

# The Python service's own DB connection, same value as TEST_DB_DSN
DATABASE_URL=postgresql://postgres:<DB password>@127.0.0.1:5432/MOC_MATSUEXAM_TEST
```

> Other tunable parameters (timeouts, log paths, etc.) have built-in defaults and don't need to go in `.env`. For the full list see chapter 2 of [`docs/01_langgraph_architecture.md`](docs/01_langgraph_architecture.md) (in Traditional Chinese).

---

## More Documentation

All documents below are written in Traditional Chinese.

- Full result reports: [`docs/refactor_result.md`](docs/refactor_result.md) (plain-language version), [`docs/refactor_result_detail.md`](docs/refactor_result_detail.md) (detailed version)
- Overall architecture design: [`docs/00_refactor_architecture.md`](docs/00_refactor_architecture.md)
- Design and code docs for each agent: `docs/01` to `docs/11`
