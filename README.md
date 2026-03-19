# Agentic SDLC

**Plain English in. Production code out.**

Describe what you want to build — in a text file, in any language, at any level of detail. The pipeline designs the architecture, writes all the code, containerises it, and tests it.

---

## Quickstart

```bash
git clone https://github.com/vb-nattamai/agentic-sdlc && cd agentic-sdlc
pip install -r requirements.txt
gh auth login
echo "Build a todo REST API in FastAPI with PostgreSQL" > reqs.txt
python3 main.py --requirements reqs.txt
```

---

## What gets generated

The pipeline adapts to whatever you describe. Examples:

| You write | What gets built |
|---|---|
| FastAPI todo API | Python FastAPI, PostgreSQL, Alembic, Docker Compose |
| React + Node.js dashboard | React 18 + TypeScript, Express, Prisma, Docker Compose |
| iOS app with a shared backend | SwiftUI, Kotlin Spring Boot API, PostgreSQL |
| Flutter app for iOS + Android | Flutter (Dart), Node.js API, MongoDB |
| gRPC microservices in Go | auth + user + product services, Protobuf definitions |
| AWS Lambda serverless API | Node.js Lambdas, API Gateway, DynamoDB, SAM template |
| Kubernetes app on EKS | Terraform, Helm charts, Deployments, HPA, Ingress |
| Django monolith | Django 5, DRF, Celery, Redis, PostgreSQL |
| Rust REST API | Axum, SQLx, PostgreSQL, Docker |
| CLI tool in Go | Cobra, cross-platform build scripts, tests |

Any language. Any framework. Any cloud.

---

## How it works

```
requirements.txt → Discovery → Architecture → Spec
                                                 ↓
                              Code gen (parallel, per service)
                                                 ↓
                              Infrastructure → Review → Deploy → Tests ✅
```

1. **Discovery** — structures your requirements into goals and constraints
2. **Architecture** — LLM designs the system; one blueprint per service
3. **Spec** — OpenAPI + SQL DDL generated → pipeline pauses for review
4. **Code gen** — each service generated in parallel; dependent services receive peer contracts automatically
5. **Infrastructure** — Dockerfiles + Docker Compose
6. **Review** — OWASP security scan + quality gate; auto-retries on failure
7. **Deploy + Test** — containers started, live HTTP tests run

---

## Prerequisites

- Python 3.11+
- Docker running locally
- [GitHub CLI](https://cli.github.com) authenticated with Models API access

---

## Common commands

```bash
# Basic run (pauses for human review at key stages)
python3 main.py --requirements reqs.txt

# Fully automated — no pauses
python3 main.py --requirements reqs.txt --auto

# Specify tech stack
python3 main.py --requirements reqs.txt \
  --tech-constraints "Go 1.22 only" \
  --arch-constraints "stateless, JWT auth"

# Add features to an existing project — no re-explaining needed
python3 main.py --from-run artifacts/run_... --requirements new_features.txt

# Resume after a pause or interruption
python3 main.py --resume artifacts/run_.../checkpoints/step_3.json --auto
```

All options: `python3 main.py --help`

---

## Output

```
artifacts/run_YYYYMMDD_HHMMSS/
├── PROJECT_CONTEXT.md        ← summary of everything built; use with --from-run
├── generated/
│   ├── <service>/            ← source code per service
│   ├── specs/openapi.yaml
│   └── docker-compose.yml
└── *_artifact.json           ← structured outputs from each pipeline stage
```

`PROJECT_CONTEXT.md` is written after every successful run. Pass its directory to `--from-run` on the next run to continue the project without re-explaining it.

---

## Human review

At key stages the pipeline pauses:

```
⏸  Human Review Required — Spec complete

  [Enter]          Continue
  c <text>         Inject a constraint  (e.g. c Use Redis not Memcached)
  e <name>         Edit an artifact
  s                Save and exit (resume later)
  a                Abort
```

Use `--auto` to skip all pauses.

---

## Customise behaviour

Edit the prompt files — no code changes needed:

| File | Controls |
|---|---|
| `prompts/dynamic_agent.md` | Code generation rules for all services |
| `prompts/architecture_agent.md` | System design and blueprint generation |
| `prompts/review_agent.md` | Security checklist and quality thresholds |
| `prompts/orchestrator.md` | Pipeline routing logic |

---

## Contributing

1. Fork + clone
2. Edit a prompt (`prompts/*.md`) or agent (`agents/*.py`)
3. Test: `python3 main.py --requirements test_requirements.txt --auto`
4. `pytest`
5. Open a PR

---

## Licence

MIT


---

