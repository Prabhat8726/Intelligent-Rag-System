# Documentation

## Architecture (Phase 0)

| # | Document | Contents |
|---|---|---|
| 01 | [Requirements](architecture/01-requirements.md) | Functional/non-functional requirements, personas → roles, **contradictions & resolutions**, assumptions |
| 02 | [System architecture](architecture/02-system-architecture.md) | Modular monolith, component diagram, responsibilities, layering, environments, repository structure |
| 03 | [Data flow](architecture/03-data-flow.md) | Upload, processing pipeline, RAG, agent + approval, demo path, data classification |
| 04 | [Database schema](architecture/04-database-schema.md) | ER diagram and table catalogue for every phase |
| 05 | [API specification](architecture/05-api-specification.md) | Conventions, full endpoint surface, role/permission matrix |
| 06 | [AI / ML architecture](architecture/06-ai-architecture.md) | Provider abstraction, model choices, OCR, classification, extraction, confidence model, cost control |
| 07 | [RAG architecture](architecture/07-rag-architecture.md) | Chunking, hybrid retrieval, context assembly, citation safeguards |
| 08 | [Agent architecture](architecture/08-agent-architecture.md) | LangGraph state graph, tools, HITL state machine, MCP |
| 09 | [Security architecture](architecture/09-security-architecture.md) | Threat model, controls per layer, security test plan |
| 10 | [Evaluation plan](architecture/10-evaluation-plan.md) | Datasets, metrics, execution — no fabricated numbers |
| 11 | [Implementation plan](architecture/11-implementation-plan.md) | Phases and acceptance criteria |
| 12 | [Technology choices](architecture/12-technology-choices.md) | Choices, alternatives, free-tier strategy |
| 13 | [Decision log](architecture/13-decision-log.md) | ADRs |

## Development

* [Local setup](development/local-setup.md)
* [Configuration reference](development/configuration.md) (including Gemini key handling)
