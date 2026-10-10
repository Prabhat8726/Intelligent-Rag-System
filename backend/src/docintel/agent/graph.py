"""The investigation graph (Module 14), built with LangGraph.

  START -> understand_request -> identify_documents -> inspect_extraction -> run_rules
        -> [compare_documents] -> retrieve_knowledge -> analyze -> (retrieve_knowledge -> analyze)
        -> determine_confidence -> recommend -> approval_gate
        -> execute_safe_action | propose_for_approval | (none) -> finalize -> END

Every fact comes from a controlled tool called as the requesting user; the model (if any) only
plans and writes findings that are validated before use. Bounds: AGENT_MAX_TOOL_CALLS,
AGENT_MAX_LLM_CALLS, one follow-up retrieval round, AGENT_TIMEOUT_SECONDS for the whole run.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, cast

from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from docintel.agent import analysis as analysis_module
from docintel.agent import planner
from docintel.agent.policy import assess_confidence, gate, recommend
from docintel.agent.state import (
    ActionRecord,
    ActionType,
    Confidence,
    Finding,
    Intent,
    InvestigationState,
    Plan,
    Recommendation,
)
from docintel.agent.tools import Caller, ToolRegistry, ToolResult
from docintel.ai.base import LLMProvider, LLMRequest, ModelTier
from docintel.ai.errors import ProviderError
from docintel.ai.routing import ExternalAIGate
from docintel.audit.service import RequestMeta
from docintel.core.config import Settings
from docintel.core.logging import get_logger
from docintel.db.models import Sensitivity, ToolCallStatus, ToolChannel
from docintel.matching.facts import reference_key
from docintel.processing.sensitivity import assess_pages

logger = get_logger(__name__)

NodeFn = Callable[[InvestigationState], Awaitable[dict[str, Any]]]
COMPARABLE = {
    "INVOICE": "INVOICE",
    "PURCHASE_ORDER": "PURCHASE_ORDER",
    "DELIVERY_NOTE": "DELIVERY_NOTE",
}
KNOWLEDGE_TOP_K = 3
MAX_PASSAGES = 8
MAX_KNOWLEDGE_QUERIES = 3
EVIDENCE_PER_DOCUMENT = 3


@dataclass(slots=True)
class AgentDeps:
    """Built once per worker process."""

    registry: ToolRegistry
    settings: Settings
    llm: LLMProvider | None
    gate: ExternalAIGate


@dataclass(slots=True)
class RunContext:
    deps: AgentDeps
    run_id: uuid.UUID
    user_id: uuid.UUID
    on_node: Callable[[str], Awaitable[None]] | None = None
    tool_calls: int = 0
    llm_calls: int = 0
    budget_exhausted: bool = False
    model: dict[str, str] | None = None
    meta: RequestMeta = field(default_factory=RequestMeta)

    @property
    def model_enabled(self) -> bool:
        return (
            self.deps.llm is not None
            and self.deps.settings.agent_llm_enabled
            and self.llm_calls < self.deps.settings.agent_max_llm_calls
        )

    async def tool(
        self, node: str, name: str, arguments: dict[str, Any], log: list[dict[str, Any]]
    ) -> ToolResult | None:
        """One controlled call; None once AGENT_MAX_TOOL_CALLS is used up."""
        if self.tool_calls >= self.deps.settings.agent_max_tool_calls:
            self.budget_exhausted = True
            return None
        self.tool_calls += 1
        caller = Caller(
            user_id=self.user_id,
            via=ToolChannel.AGENT,
            meta=self.meta,
            run_id=self.run_id,
            node=node,
        )
        result = await self.deps.registry.call(name, arguments, caller)
        log.append(
            {
                "call_id": str(result.call_id),
                "node": node,
                "tool": name,
                "status": result.status.value,
                "latency_ms": result.latency_ms,
                "error": result.error,
            }
        )
        return result


def _sensitivity(value: str | None) -> Sensitivity | None:
    return Sensitivity(value) if value else None


class Investigation:
    """One run of the graph for one request (the graph closes over the run's context)."""

    def __init__(self, context: RunContext) -> None:
        self.ctx = context
        self.settings = context.deps.settings

    # ------------------------------------------------------------------ plumbing
    def _node(self, name: str, fn: NodeFn) -> NodeFn:
        async def run(state: InvestigationState) -> dict[str, Any]:
            if self.ctx.on_node is not None:
                await self.ctx.on_node(name)
            started = time.perf_counter()
            calls_before = self.ctx.tool_calls
            update = await fn(state)
            trace = {
                "node": name,
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                "tool_calls": self.ctx.tool_calls - calls_before,
            }
            return {**update, "trace": [trace]}

        return run

    async def _model(self, request: LLMRequest, schema: type[Any]) -> tuple[Any | None, str | None]:
        """(validated output, None) or (None, notice)."""
        llm = self.ctx.deps.llm
        assert llm is not None  # noqa: S101 - callers check model_enabled
        self.ctx.llm_calls += 1
        try:
            response = await llm.generate_structured(request, schema)
        except (ProviderError, ValidationError) as exc:
            logger.warning("agent.model_failed", purpose=request.purpose, error=type(exc).__name__)
            return (
                None,
                "The language model was unavailable or answered invalidly; rules were used.",
            )
        self.ctx.model = {"provider": response.usage.provider, "model": response.usage.model}
        return response.data, None

    def build(self) -> Any:
        graph = StateGraph(InvestigationState)
        nodes: dict[str, NodeFn] = {
            "understand_request": self.understand_request,
            "identify_documents": self.identify_documents,
            "inspect_extraction": self.inspect_extraction,
            "run_rules": self.run_rules,
            "compare_documents": self.compare_documents,
            "retrieve_knowledge": self.retrieve_knowledge,
            "analyze": self.analyze,
            "determine_confidence": self.determine_confidence,
            "recommend": self.recommend,
            "approval_gate": self.approval_gate,
            "execute_safe_action": self.execute_safe_action,
            "propose_for_approval": self.propose_for_approval,
            "finalize": self.finalize,
        }
        for name, fn in nodes.items():
            # LangGraph's overloads cannot match a TypedDict state through a closure.
            graph.add_node(name, cast(Any, self._node(name, fn)))
        graph.add_edge(START, "understand_request")
        graph.add_edge("understand_request", "identify_documents")
        graph.add_conditional_edges(
            "identify_documents",
            self._after_identify,
            {"inspect": "inspect_extraction", "knowledge": "retrieve_knowledge"},
        )
        graph.add_edge("inspect_extraction", "run_rules")
        graph.add_conditional_edges(
            "run_rules",
            self._after_rules,
            {"compare": "compare_documents", "knowledge": "retrieve_knowledge"},
        )
        graph.add_edge("compare_documents", "retrieve_knowledge")
        graph.add_edge("retrieve_knowledge", "analyze")
        graph.add_conditional_edges(
            "analyze",
            lambda state: "more" if state.get("pending_questions") else "conclude",
            {"more": "retrieve_knowledge", "conclude": "determine_confidence"},
        )
        graph.add_edge("determine_confidence", "recommend")
        graph.add_edge("recommend", "approval_gate")
        graph.add_conditional_edges(
            "approval_gate",
            lambda state: state.get("action_route", "none"),
            {
                "execute": "execute_safe_action",
                "propose": "propose_for_approval",
                "none": "finalize",
            },
        )
        graph.add_edge("execute_safe_action", "finalize")
        graph.add_edge("propose_for_approval", "finalize")
        graph.add_edge("finalize", END)
        return graph.compile()

    # ------------------------------------------------------------------ routing
    @staticmethod
    def _after_identify(state: InvestigationState) -> str:
        return "inspect" if state.get("documents") else "knowledge"

    @staticmethod
    def _after_rules(state: InvestigationState) -> str:
        plan = state.get("plan", {})
        roles = state.get("document_roles", {})
        subjects = [doc_id for doc_id, role in roles.items() if role == "subject"]
        if plan.get("intent") == Intent.COMPARE_DOCUMENTS and len(subjects) >= 2:
            return "compare"
        return "knowledge"

    # ------------------------------------------------------------------ nodes
    async def understand_request(self, state: InvestigationState) -> dict[str, Any]:
        query = state["query"]
        has_documents = bool(state.get("requested_documents"))
        plan = planner.rule_plan(query, has_documents=has_documents)
        notices: list[str] = []
        if self.ctx.model_enabled:
            detected = assess_pages([(1, query)]).detected
            decision = self.ctx.deps.gate.decide(detected)
            if decision.allowed:
                output, notice = await self._model(
                    LLMRequest(
                        prompt=planner.build_prompt(query),
                        system_instruction=planner.SYSTEM_INSTRUCTION,
                        tier=ModelTier.FAST,
                        max_output_tokens=512,
                        purpose="agent.plan",
                        prompt_version=planner.PROMPT_VERSION,
                        agent_run_id=self.ctx.run_id,
                    ),
                    planner.ModelPlan,
                )
                if output is not None:
                    plan = planner.validate_plan(output, query, has_documents=has_documents)
                elif notice:
                    notices.append(notice)
            else:
                notices.append(
                    "The request holds data above the external AI limit; it was planned by "
                    "keyword rules and not sent to the model."
                )
        return {"plan": plan.model_dump(mode="json"), "notices": notices}

    async def identify_documents(self, state: InvestigationState) -> dict[str, Any]:
        plan = Plan.model_validate(state["plan"])
        limit = self.settings.agent_max_documents
        calls: list[dict[str, Any]] = []
        notices: list[str] = []
        documents: dict[str, dict[str, Any]] = {}
        identified_by = "none"
        candidates: list[str] = []
        if state.get("requested_documents"):
            candidates = state["requested_documents"][:limit]
            identified_by = "request"
            if len(state["requested_documents"]) > limit:
                notices.append(f"Only the first {limit} documents were investigated.")
        elif plan.intent != Intent.POLICY_QUESTION and not plan.document_query:
            notices.append(
                "The request does not identify a document (by number, vendor or month); "
                "name one or start the investigation from the document."
            )
        elif plan.intent != Intent.POLICY_QUESTION and plan.document_query:
            found = await self.ctx.tool(
                "identify_documents",
                "search_documents",
                # With a document number, look wider: only exact matches are kept below.
                {"query": plan.document_query, "limit": 20 if plan.identifiers else limit},
                calls,
            )
            if found is not None and found.ok and found.output is not None:
                hits = self._narrow(found.output["results"], plan, notices)
                candidates = [hit["document_id"] for hit in hits][:limit]
                identified_by = "search" if candidates else "none"
                if len(hits) > limit:
                    notices.append(f"Only the first {limit} matching documents were investigated.")
            elif found is not None:
                notices.append(f"Document search failed: {found.error}")
        for document_id in candidates:
            result = await self.ctx.tool(
                "identify_documents", "get_document", {"document_id": document_id}, calls
            )
            if result is not None and result.ok and result.output is not None:
                documents[document_id] = result.output
            elif result is not None and identified_by == "request":
                notices.append(f"Document {document_id} was not found or is not accessible.")
        if not documents and identified_by == "request":
            identified_by = "none"
        return {
            "documents": documents,
            "document_roles": dict.fromkeys(documents, "subject"),
            "identified_by": identified_by,
            "tool_calls": calls,
            "notices": notices,
        }

    @staticmethod
    def _narrow(hits: list[dict[str, Any]], plan: Plan, notices: list[str]) -> list[dict[str, Any]]:
        """Keep the documents carrying a number the request names (their own number first,
        else the order number they quote); without numbers, every hit counts."""
        if not plan.identifiers:
            if len(hits) > 1:
                notices.append(
                    f"{len(hits)} documents matched the request and were investigated; "
                    "name a document number to narrow it down."
                )
            return hits
        keys = {key for key in map(reference_key, plan.identifiers) if key}
        own = [hit for hit in hits if reference_key(hit["document_number"]) in keys]
        if own:
            return own
        quoting = [hit for hit in hits if reference_key(hit["order_reference"]) in keys]
        names = ", ".join(plan.identifiers)
        if quoting:
            notices.append(f"No document is numbered {names}; documents quoting it were used.")
        else:
            notices.append(f"No document you can access is numbered {names}.")
        return quoting

    async def inspect_extraction(self, state: InvestigationState) -> dict[str, Any]:
        plan = Plan.model_validate(state["plan"])
        calls: list[dict[str, Any]] = []
        extractions: dict[str, dict[str, Any]] = {}
        evidence: dict[str, list[dict[str, Any]]] = {}
        for document_id, document in state.get("documents", {}).items():
            if document.get("extraction") is None:
                continue
            fields = await self.ctx.tool(
                "inspect_extraction", "get_extracted_fields", {"document_id": document_id}, calls
            )
            if fields is None or not fields.ok or fields.output is None:
                continue
            extractions[document_id] = fields.output
            values = fields.output["fields"]
            header = {item["field_path"]: item for item in values if "[" not in item["field_path"]}
            wanted = [path for path in ("total", *plan.focus_fields) if path in header]
            wanted += [
                item["field_path"]
                for item in sorted(header.values(), key=lambda item: item["confidence"])
                if item["required"] and item["confidence"] < 0.85
            ]
            for path in list(dict.fromkeys(wanted))[:EVIDENCE_PER_DOCUMENT]:
                found = await self.ctx.tool(
                    "inspect_extraction",
                    "get_document_evidence",
                    {"document_id": document_id, "field_path": path},
                    calls,
                )
                if found is not None and found.ok and found.output is not None:
                    evidence.setdefault(document_id, []).append(found.output)
        return {"extractions": extractions, "evidence": evidence, "tool_calls": calls}

    async def run_rules(self, state: InvestigationState) -> dict[str, Any]:
        calls: list[dict[str, Any]] = []
        documents = dict(state.get("documents", {}))
        roles = dict(state.get("document_roles", {}))
        subject_ids = [doc_id for doc_id, role in roles.items() if role == "subject"]
        rules: dict[str, dict[str, Any]] = {}
        if subject_ids:
            result = await self.ctx.tool(
                "run_rules", "run_business_rules", {"document_ids": subject_ids}, calls
            )
            if result is not None and result.ok and result.output is not None:
                rules = {item["document_id"]: item for item in result.output["documents"]}
        # Counterparts (the order, delivery notes, duplicates) are shown with the subject.
        related: list[str] = []
        for item in rules.values():
            related += (item.get("comparison") or {}).get("counterpart_ids", [])
            related += [duplicate["document_id"] for duplicate in item.get("duplicates", [])]
        cap = 2 * self.settings.agent_max_documents
        for document_id in dict.fromkeys(related):
            if document_id in documents or len(documents) >= cap:
                continue
            found = await self.ctx.tool(
                "run_rules", "get_document", {"document_id": document_id}, calls
            )
            if found is not None and found.ok and found.output is not None:
                documents[document_id] = found.output
                roles[document_id] = "related"
        return {
            "rules": rules,
            "documents": documents,
            "document_roles": roles,
            "tool_calls": calls,
        }

    async def compare_documents(self, state: InvestigationState) -> dict[str, Any]:
        calls: list[dict[str, Any]] = []
        notices: list[str] = []
        roles = state.get("document_roles", {})
        documents = state.get("documents", {})
        members = [
            {"document_id": doc_id, "role": COMPARABLE[documents[doc_id]["document_type"]]}
            for doc_id, role in roles.items()
            if role == "subject" and documents[doc_id].get("document_type") in COMPARABLE
        ]
        covered = {
            doc_id: set(item.get("comparison", {}).get("counterpart_ids", []) or [])
            for doc_id, item in state.get("rules", {}).items()
            if item.get("comparison")
        }
        ids = {member["document_id"] for member in members}
        already = any(ids - {doc_id} <= partners for doc_id, partners in covered.items())
        comparisons: list[dict[str, Any]] = []
        if len(members) < 2:
            notices.append(
                "Only invoices, purchase orders and delivery notes can be compared; "
                "no comparison was made."
            )
        elif already:
            notices.append("These documents are already compared by matching; see the rules.")
        else:
            result = await self.ctx.tool(
                "compare_documents", "compare_documents", {"documents": members}, calls
            )
            if result is not None and result.ok and result.output is not None:
                comparisons.append({**result.output, "members": members})
            elif result is not None:
                notices.append(f"The comparison could not be made: {result.error}")
        return {"comparisons": comparisons, "tool_calls": calls, "notices": notices}

    def _knowledge_queries(self, state: InvestigationState) -> list[str]:
        pending = state.get("pending_questions") or []
        if pending:
            return list(dict.fromkeys(pending))[:MAX_KNOWLEDGE_QUERIES]
        plan = Plan.model_validate(state["plan"])
        queries = list(plan.knowledge_questions)
        severity = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
        flagged = [
            result
            for item in state.get("rules", {}).values()
            for result in item.get("results", [])
            if result["outcome"] in analysis_module.ATTENTION
        ]
        flagged.sort(key=lambda result: severity.get(result["severity"], 4))
        queries += [f"{result['rule_name']}: what does the policy require?" for result in flagged]
        done = set(state.get("knowledge_queries", []))
        return [q for q in dict.fromkeys(queries) if q not in done][:MAX_KNOWLEDGE_QUERIES]

    async def retrieve_knowledge(self, state: InvestigationState) -> dict[str, Any]:
        calls: list[dict[str, Any]] = []
        notices: list[str] = []
        passages = list(state.get("knowledge", []))
        seen = {passage["chunk_id"] for passage in passages}
        queries = self._knowledge_queries(state)
        for query in queries:
            result = await self.ctx.tool(
                "retrieve_knowledge",
                "search_knowledge_base",
                {"query": query, "top_k": KNOWLEDGE_TOP_K},
                calls,
            )
            if result is None or not result.ok or result.output is None:
                continue
            if not result.output["sufficient_evidence"]:
                notices.append(f"No policy passage in force answers: {query[:120]}")
                continue
            for passage in result.output["passages"]:
                if passage["chunk_id"] in seen or len(passages) >= MAX_PASSAGES:
                    continue
                seen.add(passage["chunk_id"])
                passages.append({**passage, "query": query, "relevant": True})
        return {
            "knowledge": passages,
            "knowledge_queries": [*state.get("knowledge_queries", []), *queries],
            "pending_questions": [],
            "tool_calls": calls,
            "notices": notices,
        }

    async def analyze(self, state: InvestigationState) -> dict[str, Any]:
        catalogue = analysis_module.build_catalogue(state)
        findings: list[Finding] = analysis_module.rule_findings(state, catalogue)
        summary, summary_source = analysis_module.rule_summary(state), "rules"
        notices: list[str] = []
        proposal: ActionType | None = None
        rationale: str | None = None
        rationale_evidence: list[str] = []
        follow_ups: list[str] = []
        dropped = 0
        sent: set[str] = set()
        if self.ctx.model_enabled:
            levels = [
                _sensitivity(document.get("effective_sensitivity"))
                for document in state.get("documents", {}).values()
            ]
            levels.append(assess_pages([(1, state["query"])]).detected)
            decision = self.ctx.deps.gate.decide(*levels)
            if decision.allowed:
                sent = {
                    catalogue.knowledge_labels[p["chunk_id"]]
                    for p in state.get("knowledge", [])
                    if self.ctx.deps.gate.decide(_sensitivity(p["sensitivity"])).allowed
                }
                output, notice = await self._model(
                    LLMRequest(
                        prompt=analysis_module.build_prompt(
                            state["query"], catalogue, findings, sent
                        ),
                        system_instruction=analysis_module.SYSTEM_INSTRUCTION,
                        tier=ModelTier.DEFAULT,
                        max_output_tokens=self.settings.agent_max_output_tokens,
                        purpose="agent.analysis",
                        prompt_version=analysis_module.PROMPT_VERSION,
                        agent_run_id=self.ctx.run_id,
                    ),
                    analysis_module.ModelAnalysis,
                )
                if output is None:
                    sent = set()
                    if notice:
                        notices.append(notice)
                else:
                    validated = analysis_module.validate_analysis(output, catalogue, sent)
                    findings += validated.findings
                    if validated.summary:
                        summary, summary_source = validated.summary, "model"
                    proposal = validated.proposed_action
                    rationale = validated.rationale
                    rationale_evidence = validated.rationale_evidence
                    follow_ups = validated.follow_up_questions
                    dropped = validated.dropped
                    notices += validated.notices
            else:
                notices.append(
                    "Document content above the external AI sensitivity limit was not sent to "
                    "the model; the analysis uses the rules only."
                )
        cited = {label for f in findings if f.source == "model" for label in f.evidence}
        findings += analysis_module.knowledge_references(state, catalogue, cited)
        rounds = state.get("rounds", 0) + 1
        more = bool(follow_ups) and rounds < 2 and not self.ctx.budget_exhausted
        return {
            "analysis": {
                "summary": summary,
                "summary_source": summary_source,
                "findings": [finding.model_dump(mode="json") for finding in findings],
                "evidence": [item.model_dump(mode="json") for item in catalogue.items],
                "sent_to_model": sorted(sent),
                "proposal": proposal.value if proposal else None,
                "rationale": rationale,
                "rationale_evidence": rationale_evidence,
                "dropped": dropped,
            },
            "pending_questions": follow_ups if more else [],
            "rounds": rounds,
            "notices": notices,
        }

    async def determine_confidence(self, state: InvestigationState) -> dict[str, Any]:
        confidence = assess_confidence(
            state, dropped_model_findings=int(state["analysis"].get("dropped", 0))
        )
        return {"confidence": confidence.model_dump(mode="json")}

    async def recommend(self, state: InvestigationState) -> dict[str, Any]:
        analysis = state["analysis"]
        proposal = analysis.get("proposal")
        recommendation = recommend(
            state,
            Confidence.model_validate(state["confidence"]),
            proposed=ActionType(proposal) if proposal else None,
            rationale=analysis.get("rationale"),
            rationale_evidence=list(analysis.get("rationale_evidence", [])),
        )
        return {"recommendation": recommendation.model_dump(mode="json")}

    async def approval_gate(self, state: InvestigationState) -> dict[str, Any]:
        recommendation = Recommendation.model_validate(state["recommendation"])
        route = gate(recommendation, allow_safe_actions=bool(state.get("allow_safe_actions")))
        update: dict[str, Any] = {"action_route": route}
        if route == "none" and recommendation.action == ActionType.HOLD_FOR_REVIEW:
            update["action"] = ActionRecord(
                action=recommendation.action,
                status="SKIPPED",
                detail="Safe actions were not allowed for this investigation.",
            ).model_dump(mode="json")
        return update

    async def execute_safe_action(self, state: InvestigationState) -> dict[str, Any]:
        recommendation = Recommendation.model_validate(state["recommendation"])
        calls: list[dict[str, Any]] = []
        summary = state["analysis"]["summary"]
        rule_lines = [
            f["statement"] for f in state["analysis"]["findings"] if f["category"] == "RULE_RESULT"
        ]
        reason = f"Investigation {str(self.ctx.run_id)[:8]}: {summary}"
        if rule_lines:
            reason += " Findings: " + " | ".join(rule_lines[:3])
        severe = any(
            result["severity"] in ("HIGH", "CRITICAL") and result["outcome"] == "FAIL"
            for item in state.get("rules", {}).values()
            for result in item.get("results", [])
        )
        assert recommendation.target_document_id is not None  # noqa: S101 - guardrail ensures it
        result = await self.ctx.tool(
            "execute_safe_action",
            "create_review_task",
            {
                "document_id": recommendation.target_document_id,
                "reason": reason[:1000],
                "priority": "HIGH" if severe else "NORMAL",
            },
            calls,
        )
        if result is None:
            record = ActionRecord(
                action=recommendation.action,
                status="SKIPPED",
                detail="The tool-call budget was used up before the action.",
            )
        elif result.ok and result.output is not None:
            created = result.output["created"]
            record = ActionRecord(
                action=recommendation.action,
                status="EXECUTED",
                detail="Review requested on the document's review task."
                if created
                else "The same review request was already on file.",
                tool_call_id=str(result.call_id),
                review_task_id=result.output["task_id"],
            )
        else:
            record = ActionRecord(
                action=recommendation.action,
                status="SKIPPED" if result.status == ToolCallStatus.DENIED else "FAILED",
                detail=result.error or "The review task could not be created.",
                tool_call_id=str(result.call_id),
            )
        return {"action": record.model_dump(mode="json"), "tool_calls": calls}

    async def propose_for_approval(self, state: InvestigationState) -> dict[str, Any]:
        recommendation = Recommendation.model_validate(state["recommendation"])
        record = ActionRecord(
            action=recommendation.action,
            status="PROPOSED",
            detail=f"Needs approval by a {recommendation.required_role or 'manager'} other than "
            "the person who requested the investigation; nothing was executed.",
            required_role=recommendation.required_role,
        )
        return {"action": record.model_dump(mode="json")}

    async def finalize(self, state: InvestigationState) -> dict[str, Any]:
        notices = []
        if self.ctx.budget_exhausted:
            notices.append(
                "The tool-call budget (AGENT_MAX_TOOL_CALLS) was reached; some steps were skipped."
            )
        return {"notices": notices}


def compose_result(state: InvestigationState, context: RunContext) -> dict[str, Any]:
    """The stored result: what was found and decided, with evidence - no model reasoning."""
    roles = state.get("document_roles", {})
    sent = set(state.get("analysis", {}).get("sent_to_model", []))
    catalogue_labels = {
        item["ref"]: item["label"]
        for item in state.get("analysis", {}).get("evidence", [])
        if item["kind"] in ("DOCUMENT", "KNOWLEDGE")
    }
    return {
        "summary": state["analysis"]["summary"],
        "summary_source": state["analysis"]["summary_source"],
        "intent": state["plan"]["intent"],
        "documents": [
            {
                "label": catalogue_labels.get(doc_id),
                "role": roles.get(doc_id, "subject"),
                **{
                    key: document.get(key)
                    for key in (
                        "document_id",
                        "filename",
                        "document_type",
                        "document_number",
                        "status",
                        "vendor_name",
                        "document_date",
                        "total",
                        "currency",
                        "effective_sensitivity",
                    )
                },
            }
            for doc_id, document in analysis_module.ordered_documents(state)
        ],
        "findings": state["analysis"]["findings"],
        "evidence": state["analysis"]["evidence"],
        "sources": [
            {
                "label": catalogue_labels.get(passage["chunk_id"]),
                "sent_to_model": catalogue_labels.get(passage["chunk_id"]) in sent,
                **{
                    key: passage.get(key)
                    for key in (
                        "chunk_id",
                        "knowledge_document_id",
                        "title",
                        "version_label",
                        "section_path",
                        "page_start",
                        "page_end",
                        "effective_from",
                        "effective_to",
                        "content",
                        "query",
                    )
                },
            }
            for passage in state.get("knowledge", [])
        ],
        "comparisons": [
            {
                "comparison_id": comparison["comparison_id"],
                "comparison_type": comparison["comparison_type"],
                "summary": comparison["summary"],
            }
            for comparison in state.get("comparisons", [])
        ],
        "confidence": state["confidence"],
        "recommendation": state["recommendation"],
        "action": state.get("action"),
        "notices": list(dict.fromkeys(state.get("notices", []))),
        "model": context.model,
    }
