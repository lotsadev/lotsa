"""JSON API routes for the React SPA.

Prefix: /api — all routes return JSON via Pydantic response models.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from starlette.requests import Request

from lotsa import overrides
from lotsa.attachments import (
    MAX_FILE_BYTES,
    MAX_FILES_PER_TASK,
    remove_attachment_file,
    write_attachment,
)
from lotsa.orchestrator import (
    AcknowledgeOverrideNotAllowed,
    AnswerNotAllowed,
    ApproveNotAllowed,
    ArchiveFailed,
    ArchiveNotAllowed,
    OrchestratorService,
    ProcessNotFound,
    ProjectNotFound,
    PromoteNotAllowed,
    RetryNotAllowed,
    ReviseNotAllowed,
    StopNotAllowed,
)
from lotsa.server.schemas import (
    AgentActivityEventResponse,
    AgentActivityResponse,
    AttachmentResponse,
    AvailableOverride,
    DiffResponse,
    FlowResponse,
    FlowStepResponse,
    MessageResponse,
    TaskDetailFullResponse,
    TaskDetailResponse,
    TaskSummaryResponse,
    TotalsResponse,
)

router = APIRouter(prefix="/api")


# ── Request body models ───────────────────────────────────────────


class CreateTaskRequest(BaseModel):
    title: str | None = None
    body: str = ""
    message: str | None = None
    # Name of the loaded process to dispatch this task against. Optional;
    # when omitted the active (default) process is used. Must match a name
    # surfaced by ``GET /api/processes``. Per ADR-021 any LOADED process is a
    # valid target — the task dispatches against that process's flow with no
    # restart; an unknown name yields ``PROCESS_NOT_FOUND``.
    process: str | None = None
    # The registered project (repo) the task belongs to (ADR-029). Optional;
    # when omitted the orchestrator picks the ``default`` project (or the sole
    # registered one). An unknown/non-git project yields ``PROJECT_NOT_FOUND``.
    project: str | None = None


class ProjectSummary(BaseModel):
    """A registered project offered on the new-task picker (ADR-029)."""

    id: str
    name: str
    path: str


class PromotionInputResponse(BaseModel):
    name: str
    description: str


class ProcessSummary(BaseModel):
    name: str
    is_active: bool
    is_default: bool
    step_names: list[str]
    # ADR-027 §3/§4 — surfaced so the promotion modal can describe each
    # destination and render its declared input fields. Both default to
    # empty/None so processes without them serialise cleanly.
    description: str | None = None
    promotion_inputs: list[PromotionInputResponse] = []


class FeedbackRequest(BaseModel):
    feedback: str


class PromoteRequest(BaseModel):
    to_process: str
    initial_artifacts: dict[str, str] | None = None


class AnswerRequest(BaseModel):
    answer: str


class MessageRequest(BaseModel):
    message: str


class AcknowledgeOverrideRequest(BaseModel):
    guard_name: str
    reason: str | None = None


class JumpRequest(BaseModel):
    step_name: str


# ── Helpers ───────────────────────────────────────────────────────


def _get_service(request: Request) -> OrchestratorService:
    return request.app.state.service


def _not_found() -> HTTPException:
    return HTTPException(status_code=404, detail={"error": "Task not found", "code": "TASK_NOT_FOUND"})


def _message_not_found() -> HTTPException:
    return HTTPException(status_code=404, detail={"error": "Message not found", "code": "MESSAGE_NOT_FOUND"})


def _bad_request(exc: Exception, code: str) -> HTTPException:
    return HTTPException(status_code=400, detail={"error": str(exc), "code": code})


async def _build_task_detail(service: OrchestratorService, task_id: str) -> TaskDetailFullResponse:
    """Build the composite task detail response."""
    detail = await service.get_task(task_id)
    if detail is None:
        raise _not_found()

    messages, question, totals, named_artifacts = await asyncio.gather(
        service.get_messages(task_id),
        service.get_question(task_id),
        service.get_task_totals(task_id),
        service.get_named_artifacts(task_id),
    )
    artifacts = {m.metadata.get("artifact_name"): m.content for m in named_artifacts if m.metadata.get("artifact_name")}

    # Resolve the flow from the task's OWN process (ADR-021/027), not the
    # server's active default — otherwise every task's header/stage bar shows the
    # default flow (e.g. "chat") regardless of what it was created as or promoted
    # to. Falls back to the active flow for a task that has vanished mid-request.
    task_row = await service.db.get_task(task_id)
    flow = service.root_flow_for(task_row) if task_row else service.flow
    steps = flow.steps if flow else []
    current_step = detail.current_step

    next_step_name = None
    if current_step and steps:
        step_names = [s.name for s in steps]
        if current_step in step_names:
            idx = step_names.index(current_step)
            if idx + 1 < len(step_names):
                next_step_name = step_names[idx + 1]

    flow_response = None
    if flow:
        flow_response = FlowResponse(
            name=flow.name,
            steps=[
                FlowStepResponse(
                    name=s.name,
                    conversational=s.conversational,
                    evaluate=s.evaluate,
                    output=s.output,
                    inputs=s.inputs,
                    is_gate=s.is_approval_gate,
                )
                for s in steps
            ],
            gate_states=list(flow.gate_states),
        )

    # ADR-019 — surface any guard overrides currently applicable to this task.
    # detect() is called once per registered handler here (on the full detail
    # fetch only, D6); the list is empty for nearly all tasks. Reuses the
    # ``task_row`` fetched above (the OverrideHandler protocol expects a TaskRow).
    available = await overrides.list_available_for(task_row, service.db) if task_row else []

    return TaskDetailFullResponse(
        task=TaskDetailResponse.from_detail(detail),
        messages=[MessageResponse.from_row(m) for m in messages],
        question=question,
        flow=flow_response,
        artifacts=artifacts,
        next_step_name=next_step_name,
        totals=TotalsResponse(**totals),
        available_overrides=[
            AvailableOverride(guard_name=h.guard_name, label=h.label, description=h.description) for h in available
        ],
    )


# ── GET endpoints ─────────────────────────────────────────────────


@router.get("/tasks")
async def list_tasks(request: Request) -> list[TaskSummaryResponse]:
    service = _get_service(request)
    tasks = await service.list_tasks_async()
    return [TaskSummaryResponse.from_summary(t) for t in tasks]


@router.get("/tasks/{task_id}")
async def get_task_detail(request: Request, task_id: str) -> TaskDetailFullResponse:
    service = _get_service(request)
    return await _build_task_detail(service, task_id)


@router.get("/tasks/{task_id}/messages")
async def get_task_messages(request: Request, task_id: str) -> list[MessageResponse]:
    service = _get_service(request)
    if await service.get_task(task_id) is None:
        raise _not_found()
    messages = await service.get_messages(task_id)
    return [MessageResponse.from_row(m) for m in messages]


@router.get("/tasks/{task_id}/messages/{message_id}/raw", response_class=PlainTextResponse)
async def get_message_raw(request: Request, task_id: str, message_id: int) -> str:
    """Return a single message's untruncated content as ``text/plain``.

    The list endpoint truncates oversized messages so the JSON envelope stays
    parseable in the browser. This endpoint serves the full bytes when an
    operator needs to read the original (debugging, audit drill-down).
    Scoping is enforced: the message must belong to the named task.
    """
    service = _get_service(request)
    if await service.get_task(task_id) is None:
        raise _not_found()
    msg = await service.get_message_by_id(task_id, message_id)
    if msg is None:
        raise _message_not_found()
    return msg.content


@router.get("/tasks/{task_id}/diff")
async def get_task_diff(request: Request, task_id: str) -> DiffResponse:
    service = _get_service(request)
    if await service.get_task(task_id) is None:
        raise _not_found()
    diff = await service.get_diff(task_id)
    return DiffResponse(diff=diff)


@router.get("/tasks/{task_id}/attachments")
async def list_attachments(request: Request, task_id: str) -> list[AttachmentResponse]:
    """List the prompt attachments recorded for a task (Path A).

    Reads the JSON records from ``tasks.metadata`` — the bytes stay on disk and
    are never returned here. A genuinely-unknown task is a 404.
    """
    service = _get_service(request)
    row = await service.db.get_task(task_id)
    if row is None:
        raise _not_found()
    records = row.metadata.get("attachments") or []
    return [AttachmentResponse(**a) for a in records]


@router.post("/tasks/{task_id}/attachments")
async def upload_attachment(request: Request, task_id: str, filename: str) -> AttachmentResponse:
    """Upload one file to a task's prompt attachments (Path A).

    Raw-body upload: the file bytes are the request body and ``filename`` is a
    query param (the ``Content-Type`` header supplies the MIME hint). Bytes are
    stored on disk under ``{data_dir}/attachments/{project_id}/{task_id}/``;
    only a JSON metadata record lands in ``tasks.metadata`` — never the audit
    log. Any file type is accepted (no MIME/extension filtering); safety rests
    on filename sanitization, the size/count caps, and the read-only sandbox.
    """
    service = _get_service(request)
    row = await service.db.get_task(task_id)
    if row is None:
        raise _not_found()

    # Reject oversized uploads by their declared Content-Length *before* reading
    # the body, so a 25 MB+ payload isn't buffered fully into memory first. This
    # is the cheap guard; the post-read len(data) check below is the authority
    # (a client may lie about or omit Content-Length).
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > MAX_FILE_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail={"error": "File exceeds the 25 MB limit", "code": "ATTACHMENT_TOO_LARGE"},
                )
        except ValueError:
            raise _bad_request(ValueError("Invalid Content-Length header"), "ATTACHMENT_BAD_LENGTH") from None

    data = await request.body()
    if not data:
        raise _bad_request(ValueError("Empty file"), "ATTACHMENT_EMPTY")
    if len(data) > MAX_FILE_BYTES:
        raise HTTPException(
            status_code=413,
            detail={"error": "File exceeds the 25 MB limit", "code": "ATTACHMENT_TOO_LARGE"},
        )

    existing = row.metadata.get("attachments") or []
    # Pre-check for a clean 4xx in the common case; the append's WHERE clause is
    # the race-safe authority.
    if len(existing) >= MAX_FILES_PER_TASK:
        raise _bad_request(
            ValueError(f"Attachment limit ({MAX_FILES_PER_TASK}) reached for this task"),
            "ATTACHMENT_LIMIT",
        )

    try:
        record = await asyncio.to_thread(
            write_attachment,
            service.config.data_dir,
            row.project_id,
            task_id,
            filename,
            data,
            {a["filename"] for a in existing},
            request.headers.get("content-type"),
        )
    except ValueError as exc:  # unusable / traversal-only filename
        raise _bad_request(exc, "ATTACHMENT_BAD_FILENAME") from None

    appended = await service.db.append_attachment(task_id, record, cap=MAX_FILES_PER_TASK)
    if not appended:
        # Lost the count-cap race (or the task vanished mid-upload): drop the
        # just-written bytes so they don't orphan on disk, then 4xx.
        await asyncio.to_thread(
            remove_attachment_file, service.config.data_dir, row.project_id, task_id, record["filename"]
        )
        raise _bad_request(
            ValueError(f"Attachment limit ({MAX_FILES_PER_TASK}) reached for this task"),
            "ATTACHMENT_LIMIT",
        )
    return AttachmentResponse(**record)


@router.get("/tasks/{task_id}/agent-activity")
async def get_agent_activity(
    request: Request,
    task_id: str,
    since: int = 0,
    limit: int = 100,
) -> AgentActivityResponse:
    """In-flight agent activity for a task (ADR-017).

    Reads the active runner's native session persistence via
    ``read_activity``. ``since``/``limit`` drive incremental polling (``limit``
    capped at 500). Degrades to ``events: []`` (never a 500) on a missing
    session file, a parse error, a not-yet-dispatched task, or a runner without
    activity support; a genuinely-unknown task is a 404.
    """
    service = _get_service(request)
    # Clamp both bounds at the API layer so the contract's limits are visible
    # here rather than only deep in ``_read_activity_sync`` (which floors both
    # too): ``since`` floors at 0 — a negative cursor would otherwise make the
    # ``index >= since_index`` filter always true and return the whole session
    # from index 0; ``limit`` floors at 1 and caps at 500 (ADR-017 §4).
    result = await service.get_agent_activity(task_id, since_index=max(0, since), limit=max(1, min(limit, 500)))
    if result is None:
        raise _not_found()
    session_id, activity = result
    return AgentActivityResponse(
        session_id=session_id,
        runner_supports_activity=activity.supported,
        session_complete=activity.session_complete,
        next_index=activity.next_index,
        events=[
            AgentActivityEventResponse(
                index=e.index,
                timestamp=e.timestamp,
                kind=e.kind,
                summary=e.summary,
                detail=e.detail,
                truncated=bool(e.detail and e.detail.get("truncated")),
            )
            for e in activity.events
        ],
    )


@router.get("/processes")
async def list_processes(request: Request) -> list[ProcessSummary]:
    """List every loaded process — bundled + any defined inline in lotsa.yaml.

    Surfaces the catalog the new-task UI picker renders. Each entry carries an
    ``is_active`` flag marking the *configured default* process (the one new
    tasks dispatch against when the caller doesn't pick one). Per ADR-021 the
    picker is a real selector: ``POST /api/tasks`` accepts ``process: <name>``
    for ANY loaded process and dispatches it against that process's flow with
    no restart.
    """
    service = _get_service(request)
    return [ProcessSummary(**s) for s in service.list_processes_summary()]


@router.get("/projects")
async def list_projects(request: Request) -> list[ProjectSummary]:
    """List the projects offered on the new-task picker (ADR-029).

    Only YAML-declared projects are offered for new tasks; a project removed
    from ``lotsa.yaml`` persists in the DB for its existing tasks but is not
    listed here. ``POST /api/tasks`` accepts ``project: <id>`` for any of these.
    """
    service = _get_service(request)
    return [ProjectSummary(**p) for p in service.list_projects_summary()]


@router.get("/flow")
async def get_flow(request: Request) -> FlowResponse:
    service = _get_service(request)
    flow = service.flow
    if flow is None:
        raise HTTPException(status_code=503, detail={"error": "Flow not loaded", "code": "FLOW_NOT_LOADED"})
    return FlowResponse(
        name=flow.name,
        steps=[
            FlowStepResponse(
                name=s.name,
                conversational=s.conversational,
                evaluate=s.evaluate,
                output=s.output,
                inputs=s.inputs,
                is_gate=s.is_approval_gate,
            )
            for s in flow.steps
        ],
        gate_states=list(flow.gate_states),
    )


# ── POST endpoints ────────────────────────────────────────────────


@router.post("/tasks")
async def create_task(request: Request, body: CreateTaskRequest) -> TaskDetailFullResponse:
    service = _get_service(request)
    try:
        task = await service.create_task(
            title=body.title,
            body=body.body,
            message=body.message,
            process_name=body.process,
            project_id=body.project,
        )
    except ProcessNotFound as exc:
        # Unknown name (not in the catalog at all) — operator action is "add it
        # to lotsa.yaml's processes: block" (or pick a bundled name). Per
        # ADR-021 this is the only process-resolution error: any LOADED process
        # is a valid dispatch target, so there is no longer a "loaded but not
        # active" rejection.
        raise _bad_request(exc, "PROCESS_NOT_FOUND") from None
    except ProjectNotFound as exc:
        # Unknown/non-git project, or no project picked when several are
        # registered (ADR-029) — operator action is "pick a registered project".
        raise _bad_request(exc, "PROJECT_NOT_FOUND") from None
    return await _build_task_detail(service, task.id)


@router.post("/tasks/{task_id}/approve")
async def approve_task(request: Request, task_id: str) -> TaskDetailFullResponse:
    service = _get_service(request)
    try:
        await service.approve(task_id)
    except ApproveNotAllowed as exc:
        raise _bad_request(exc, "APPROVE_NOT_ALLOWED") from None
    return await _build_task_detail(service, task_id)


@router.post("/tasks/{task_id}/revise")
async def revise_task(request: Request, task_id: str, body: FeedbackRequest) -> TaskDetailFullResponse:
    service = _get_service(request)
    try:
        await service.revise(task_id, body.feedback)
    except ReviseNotAllowed as exc:
        raise _bad_request(exc, "REVISE_NOT_ALLOWED") from None
    return await _build_task_detail(service, task_id)


@router.post("/tasks/{task_id}/promote")
async def promote_task(request: Request, task_id: str, body: PromoteRequest) -> TaskDetailFullResponse:
    service = _get_service(request)
    try:
        await service.promote_task(task_id, body.to_process, body.initial_artifacts)
    except PromoteNotAllowed as exc:
        raise _bad_request(exc, "PROMOTE_NOT_ALLOWED") from None
    return await _build_task_detail(service, task_id)


@router.post("/tasks/{task_id}/answer")
async def answer_task(request: Request, task_id: str, body: AnswerRequest) -> TaskDetailFullResponse:
    service = _get_service(request)
    try:
        await service.answer(task_id, body.answer)
    except AnswerNotAllowed as exc:
        raise _bad_request(exc, "ANSWER_NOT_ALLOWED") from None
    return await _build_task_detail(service, task_id)


@router.post("/tasks/{task_id}/message")
async def send_message(request: Request, task_id: str, body: MessageRequest) -> TaskDetailFullResponse:
    service = _get_service(request)
    try:
        await service.send_message(task_id, body.message)
    except ReviseNotAllowed as exc:
        raise _bad_request(exc, "MESSAGE_NOT_ALLOWED") from None
    return await _build_task_detail(service, task_id)


@router.post("/tasks/{task_id}/block")
async def block_task(request: Request, task_id: str) -> TaskDetailFullResponse:
    service = _get_service(request)
    await service.block(task_id)
    return await _build_task_detail(service, task_id)


@router.post("/tasks/{task_id}/retry")
async def retry_task(request: Request, task_id: str) -> TaskDetailFullResponse:
    service = _get_service(request)
    try:
        await service.retry(task_id)
    except RetryNotAllowed as exc:
        raise _bad_request(exc, "RETRY_NOT_ALLOWED") from None
    return await _build_task_detail(service, task_id)


@router.post("/tasks/{task_id}/stop")
async def stop_task(request: Request, task_id: str) -> TaskDetailFullResponse:
    service = _get_service(request)
    try:
        await service.stop(task_id)
    except StopNotAllowed as exc:
        raise _bad_request(exc, "STOP_NOT_ALLOWED") from None
    return await _build_task_detail(service, task_id)


@router.post("/tasks/{task_id}/archive")
async def archive_task(request: Request, task_id: str) -> TaskDetailFullResponse:
    service = _get_service(request)
    try:
        await service.archive(task_id)
    except ArchiveNotAllowed as exc:
        raise _bad_request(exc, "ARCHIVE_NOT_ALLOWED") from None
    except ArchiveFailed as exc:
        # The terminal CAS never converged — the task is NOT archived. Surface
        # a 503 so the caller can retry rather than reading a false 200.
        raise HTTPException(status_code=503, detail={"error": str(exc), "code": "ARCHIVE_FAILED"}) from None
    return await _build_task_detail(service, task_id)


@router.post("/tasks/{task_id}/jump")
async def jump_to_step(request: Request, task_id: str, body: JumpRequest) -> TaskDetailFullResponse:
    service = _get_service(request)
    try:
        await service.jump_to_step(task_id, body.step_name)
    except ValueError:
        raise HTTPException(
            status_code=400, detail={"error": "Invalid step name", "code": "INVALID_STEP_NAME"}
        ) from None
    return await _build_task_detail(service, task_id)


@router.post("/tasks/{task_id}/acknowledge-override")
async def acknowledge_override(
    request: Request, task_id: str, body: AcknowledgeOverrideRequest
) -> TaskDetailFullResponse:
    service = _get_service(request)
    try:
        await service.acknowledge_override(task_id, body.guard_name, body.reason)
    except AcknowledgeOverrideNotAllowed as exc:
        raise _bad_request(exc, "ACKNOWLEDGE_OVERRIDE_NOT_ALLOWED") from None
    return await _build_task_detail(service, task_id)
