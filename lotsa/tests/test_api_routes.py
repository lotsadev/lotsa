"""Tests for the JSON API routes."""

from __future__ import annotations

import asyncio

from httpx import ASGITransport, AsyncClient

from lotsa.tests.conftest import wait_for_completion, wait_for_status


class TestAPIRoutes:
    def test_list_tasks(self, app_with_service, run):
        app, service = app_with_service

        async def _test():
            await service.create_task("List Task")
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get("/api/tasks")
                assert resp.status_code == 200
                data = resp.json()
                assert isinstance(data, list)
                assert len(data) >= 1
                assert any(t["title"] == "List Task" for t in data)

        run(_test())

    def test_get_task_detail(self, app_with_service, run):
        app, service = app_with_service

        async def _test():
            task = await service.create_task("Detail Task")
            await wait_for_completion(service, task.id)

            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get(f"/api/tasks/{task.id}")
                assert resp.status_code == 200
                data = resp.json()
                assert "task" in data
                assert "messages" in data
                assert "flow" in data
                assert "artifacts" in data
                assert "totals" in data
                assert "next_step_name" in data
                assert "question" in data
                assert data["task"]["title"] == "Detail Task"

        run(_test())

    def test_create_task_json(self, app_with_service, run):
        app, service = app_with_service

        async def _test():
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post("/api/tasks", json={"message": "Build X"})
                assert resp.status_code == 200
                data = resp.json()
                assert "task" in data
                assert data["task"]["id"]

        run(_test())

    def test_get_flow(self, app_with_service, run):
        app, _ = app_with_service

        async def _test():
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get("/api/flow")
                assert resp.status_code == 200
                data = resp.json()
                assert "steps" in data
                assert "gate_states" in data
                assert "name" in data

        run(_test())

    def test_list_processes(self, app_with_service, run):
        """``GET /api/processes`` returns every loaded process with all fields populated.

        Asserts the full ``ProcessSummary`` shape — name, is_active,
        is_default, step_names — not just the active flag. ``is_default``
        and ``step_names`` are what the UI dropdown renders, so missing
        them on the wire would surface as a runtime UI bug only.
        """
        app, service = app_with_service

        async def _test():
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get("/api/processes")
                assert resp.status_code == 200
                data = resp.json()
                assert isinstance(data, list)
                assert len(data) >= 1
                # Exactly one entry is marked active.
                active_entries = [p for p in data if p["is_active"]]
                assert len(active_entries) == 1
                assert active_entries[0]["name"] == service._active_process_name
                # First entry in the response is the active one (stable order).
                assert data[0]["is_active"] is True
                # Every entry carries the full ProcessSummary shape — the
                # UI dropdown reads step_names; is_default lights up the
                # "default" badge.
                for entry in data:
                    assert isinstance(entry["name"], str) and entry["name"]
                    assert isinstance(entry["is_active"], bool)
                    assert isinstance(entry["is_default"], bool)
                    assert isinstance(entry["step_names"], list)
                    assert all(isinstance(s, str) for s in entry["step_names"])
                # The active process in the standard test fixture is the
                # bundled "standard" (no inline default in lotsa.yaml), so
                # the active entry's step_names must be non-empty.
                assert active_entries[0]["step_names"], (
                    "Active process should expose its main flow's step names "
                    "so the UI can show what the task will go through."
                )
                # ``is_default`` is True only for inline ``default: true``
                # entries; the bundled active here has no inline backing,
                # so it must be False. Confirms the field isn't accidentally
                # aliased to is_active.
                assert active_entries[0]["is_default"] is False

        run(_test())

    def test_list_projects(self, app_with_service, run):
        """``GET /api/projects`` returns the offerable projects at the HTTP layer.

        Covers routing, 200 status, and the ``ProjectSummary`` wire shape
        (``id``/``name``/``path``) that the new-task picker reads — the
        service-level ``list_projects_summary`` is unit-tested in
        ``test_adr029_multi_project.py``, but the route serialisation is not.
        The standard test fixture seeds a ``default`` project from ``work_dir``
        (ADR-029), so the list is non-empty and includes it.
        """
        app, service = app_with_service

        async def _test():
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get("/api/projects")
                assert resp.status_code == 200
                data = resp.json()
                assert isinstance(data, list)
                assert len(data) >= 1
                # Every entry carries the full ProjectSummary shape the picker
                # renders — id/name/path, all non-empty strings.
                for entry in data:
                    assert isinstance(entry["id"], str) and entry["id"]
                    assert isinstance(entry["name"], str) and entry["name"]
                    assert isinstance(entry["path"], str) and entry["path"]
                # Only YAML-declared/offered projects appear, matching the
                # service's own offer list (``_yaml_project_ids``).
                ids = {entry["id"] for entry in data}
                assert ids == set(service._yaml_project_ids)
                assert "default" in ids

        run(_test())

    def test_create_task_accepts_loaded_non_active_process(self, app_with_service, run):
        """ADR-021: ``POST /api/tasks`` with any LOADED process succeeds — no
        restart, no ``PROCESS_NOT_ACTIVE`` 400.

        Inverts the pre-ADR-021 behaviour (loaded-but-inactive returned 400
        ``PROCESS_NOT_ACTIVE`` with "restart with --process X" guidance). R5
        removes that rejection path and the ``ProcessNotActive`` class; R7
        drops the ``PROCESS_NOT_ACTIVE`` branch from ``POST /api/tasks``. The
        selected process round-trips into the created task's metadata.
        """
        app, service = app_with_service

        async def _test():
            # Stage a second process in the catalog that isn't the active one.
            service._processes["sibling_process"] = service.process

            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    "/api/tasks",
                    json={"title": "t", "message": "do a thing", "process": "sibling_process"},
                )
                assert resp.status_code == 200, resp.text

            # The selected process round-trips into the task's metadata.
            rows = await service.db.list_tasks()
            sibling_rows = [r for r in rows if (r.metadata or {}).get("process_name") == "sibling_process"]
            assert sibling_rows, (
                "A task created against a loaded non-active process must record "
                "process_name='sibling_process' in its metadata."
            )

        run(_test())

    def test_create_task_rejects_unknown_process(self, app_with_service, run):
        """``POST /api/tasks`` with an unknown ``process:`` returns 400
        with ``PROCESS_NOT_FOUND`` (distinct from the loaded-but-inactive
        case which uses ``PROCESS_NOT_ACTIVE``).
        """
        app, _ = app_with_service

        async def _test():
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    "/api/tasks",
                    json={"title": "t", "message": "do a thing", "process": "not_a_thing"},
                )
                assert resp.status_code == 400
                body = resp.json()
                # Unknown-name path uses PROCESS_NOT_FOUND so the UI can
                # render "add it to lotsa.yaml" guidance instead of the
                # misleading "restart with --process X" message that
                # PROCESS_NOT_ACTIVE carries.
                assert body["detail"]["code"] == "PROCESS_NOT_FOUND"
                assert "not_a_thing" in body["detail"]["error"]

        run(_test())

    def test_create_task_rejects_unknown_project(self, app_with_service, run):
        """``POST /api/tasks`` with an unknown ``project:`` returns 400 with
        ``PROJECT_NOT_FOUND`` (ADR-029) — the HTTP-layer mirror of the
        ``ProjectNotFound`` → ``_bad_request`` mapping, alongside the
        ``PROCESS_NOT_FOUND`` sibling above.
        """
        app, _ = app_with_service

        async def _test():
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    "/api/tasks",
                    json={"title": "t", "message": "do a thing", "project": "not_a_project"},
                )
                assert resp.status_code == 400
                body = resp.json()
                assert body["detail"]["code"] == "PROJECT_NOT_FOUND"
                assert "not_a_project" in body["detail"]["error"]

        run(_test())

    def test_task_not_found(self, app_with_service, run):
        app, _ = app_with_service

        async def _test():
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get("/api/tasks/nonexistent")
                assert resp.status_code == 404

        run(_test())

    def test_approve_task(self, app_with_service, run):
        app, service = app_with_service

        async def _test():
            task = await service.create_task("Approve Task")
            await wait_for_completion(service, task.id)

            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(f"/api/tasks/{task.id}/approve")
                assert resp.status_code == 200
                data = resp.json()
                assert "task" in data

        run(_test())

    def test_jump_invalid(self, app_with_service, run):
        app, service = app_with_service

        async def _test():
            task = await service.create_task("Jump Task")
            await wait_for_completion(service, task.id)

            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    f"/api/tasks/{task.id}/jump",
                    json={"step_name": "nonexistent_step"},
                )
                assert resp.status_code == 400

        run(_test())

    def test_get_messages(self, app_with_service, run):
        app, service = app_with_service

        async def _test():
            task = await service.create_task("Messages Task")
            await wait_for_completion(service, task.id)

            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get(f"/api/tasks/{task.id}/messages")
                assert resp.status_code == 200
                data = resp.json()
                assert isinstance(data, list)

        run(_test())

    def test_get_diff(self, app_with_service, run):
        app, service = app_with_service

        async def _test():
            task = await service.create_task("Diff Task")
            await wait_for_completion(service, task.id)

            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get(f"/api/tasks/{task.id}/diff")
                assert resp.status_code == 200
                data = resp.json()
                assert "diff" in data

        run(_test())

    def test_revise_task(self, app_with_service, run):
        app, service = app_with_service

        async def _test():
            task = await service.create_task("Revise Task")
            await wait_for_completion(service, task.id)

            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    f"/api/tasks/{task.id}/revise",
                    json={"feedback": "Please redo this"},
                )
                assert resp.status_code == 200
                assert "task" in resp.json()

        run(_test())

    def test_answer_task(self, app_with_service, run):
        app, service = app_with_service

        async def _test():
            task = await service.create_task("Answer Task")
            await wait_for_completion(service, task.id)

            # Set status to needs_input before calling answer
            await service.db.update_task(task.id, status="needs_input", current_step="coding")
            await service.db.add_message(task.id, "agent", "coding", "Mock question", "question")

            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    f"/api/tasks/{task.id}/answer",
                    json={"answer": "Yes"},
                )
                assert resp.status_code == 200
                assert "task" in resp.json()

        run(_test())

    def test_send_message(self, app_with_service, run):
        app, service = app_with_service

        async def _test():
            task = await service.create_task("Message Task")
            await wait_for_completion(service, task.id)

            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    f"/api/tasks/{task.id}/message",
                    json={"message": "Hello agent"},
                )
                assert resp.status_code == 200
                assert "task" in resp.json()

        run(_test())

    def test_block_task(self, app_with_service, run):
        app, service = app_with_service

        async def _test():
            task = await service.create_task("Block Task")
            await wait_for_completion(service, task.id)

            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(f"/api/tasks/{task.id}/block")
                assert resp.status_code == 200
                assert "task" in resp.json()

        run(_test())

    def test_retry_task(self, app_with_service, run):
        app, service = app_with_service

        async def _test():
            task = await service.create_task("Retry Task")
            await wait_for_completion(service, task.id)

            # Set status to blocked before calling retry
            await service.db.update_task(task.id, status="blocked", current_step="coding")

            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(f"/api/tasks/{task.id}/retry")
                assert resp.status_code == 200
                assert "task" in resp.json()

        run(_test())

    def test_jump_valid(self, app_with_service, run):
        app, service = app_with_service

        async def _test():
            task = await service.create_task("Jump Valid Task")
            await wait_for_completion(service, task.id)

            # Get a valid step name from the flow
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                flow_resp = await client.get("/api/flow")
                steps = flow_resp.json()["steps"]
                assert steps, "Test fixture flow must have at least one step"
                step_name = steps[0]["name"]
                resp = await client.post(
                    f"/api/tasks/{task.id}/jump",
                    json={"step_name": step_name},
                )
                assert resp.status_code == 200
                assert "task" in resp.json()

        run(_test())

    def test_get_flow_not_loaded(self, app_with_service, run):
        app, service = app_with_service

        async def _test():
            original_flow = service.flow
            service.flow = None
            try:
                transport = ASGITransport(app=app)
                async with AsyncClient(transport=transport, base_url="http://test") as client:
                    resp = await client.get("/api/flow")
                    assert resp.status_code == 503
            finally:
                service.flow = original_flow

        run(_test())


class TestStatusGuards:
    def test_approve_returns_400_when_not_waiting(self, app_with_service, run):
        app, service = app_with_service

        async def _test():
            task = await service.create_task("Guarded")
            await wait_for_completion(service, task.id)
            await service.db.update_task(task.id, status="working")
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(f"/api/tasks/{task.id}/approve")
                assert resp.status_code == 400
                assert "code" in resp.json().get("detail", {})

        run(_test())

    def test_answer_returns_400_when_not_needs_input(self, app_with_service, run):
        app, service = app_with_service

        async def _test():
            task = await service.create_task("Q")
            await wait_for_completion(service, task.id)
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(f"/api/tasks/{task.id}/answer", json={"answer": "no"})
                assert resp.status_code == 400

        run(_test())

    def test_retry_returns_400_when_not_blocked(self, app_with_service, run):
        app, service = app_with_service

        async def _test():
            task = await service.create_task("R")
            await wait_for_completion(service, task.id)
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(f"/api/tasks/{task.id}/retry")
                assert resp.status_code == 400

        run(_test())

    def test_revise_returns_400_when_wrong_status(self, app_with_service, run):
        app, service = app_with_service

        async def _test():
            task = await service.create_task("Revise")
            await wait_for_completion(service, task.id)
            await service.db.update_task(task.id, status="working")
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    f"/api/tasks/{task.id}/revise",
                    json={"feedback": "nope"},
                )
                assert resp.status_code == 400
                assert resp.json()["detail"]["code"] == "REVISE_NOT_ALLOWED"

        run(_test())

    def test_send_message_returns_400_when_wrong_status(self, app_with_service, run):
        app, service = app_with_service

        async def _test():
            task = await service.create_task("Msg")
            await wait_for_completion(service, task.id)
            await service.db.update_task(task.id, status="working")
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    f"/api/tasks/{task.id}/message",
                    json={"message": "hi"},
                )
                assert resp.status_code == 400
                assert resp.json()["detail"]["code"] == "MESSAGE_NOT_ALLOWED"

        run(_test())

    # ------------------------------------------------------------------
    # /api/tasks/{task_id}/messages/{message_id}/raw — full content
    # ------------------------------------------------------------------

    def test_get_task_truncates_oversized_message_in_response(self, app_with_service, run):
        """Per-task response truncates messages above the API cap."""
        app, service = app_with_service

        async def _test():
            task = await service.create_task("Huge output task")
            # Pre-existing chat messages are tiny; add one huge output message
            # to exercise the truncation path.
            huge = "x" * 100_000
            await service.db.add_message(task.id, "agent", "code", huge, "output", metadata={})
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get(f"/api/tasks/{task.id}")
                assert resp.status_code == 200
                data = resp.json()
                huge_msg = next(m for m in data["messages"] if m["type"] == "output")
                assert huge_msg["metadata"]["content_truncated"] is True
                assert huge_msg["metadata"]["original_length"] == 100_000
                # The serialised content stays well under the original size.
                assert len(huge_msg["content"].encode("utf-8")) <= 7_000

        run(_test())

    def test_get_message_raw_returns_full_content(self, app_with_service, run):
        """The /raw endpoint returns the unmodified content as text/plain."""
        app, service = app_with_service

        async def _test():
            task = await service.create_task("Raw content task")
            huge = "y" * 100_000
            await service.db.add_message(task.id, "agent", "code", huge, "output", metadata={})
            messages = await service.get_messages(task.id)
            target = next(m for m in messages if m.type == "output")

            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get(f"/api/tasks/{task.id}/messages/{target.id}/raw")
                assert resp.status_code == 200
                assert resp.headers["content-type"].startswith("text/plain")
                assert resp.text == huge

        run(_test())

    def test_get_message_raw_404_on_missing_message(self, app_with_service, run):
        """Bogus message id returns 404 with MESSAGE_NOT_FOUND (task exists)."""
        app, service = app_with_service

        async def _test():
            task = await service.create_task("Missing-msg task")
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get(f"/api/tasks/{task.id}/messages/999999/raw")
                assert resp.status_code == 404
                # The task exists; only the message is missing. Error code
                # must distinguish so an operator debugging a 404 on a
                # valid task URL isn't sent looking for a missing task.
                assert resp.json()["detail"]["code"] == "MESSAGE_NOT_FOUND"

        run(_test())

    def test_get_message_raw_404_on_message_belonging_to_other_task(self, app_with_service, run):
        """Scoping: message owned by task A 404s as MESSAGE_NOT_FOUND under task B."""
        app, service = app_with_service

        async def _test():
            task_a = await service.create_task("Owner A")
            task_b = await service.create_task("Owner B")
            await service.db.add_message(task_a.id, "agent", "code", "secret", "output", metadata={})
            msgs_a = await service.get_messages(task_a.id)
            target = next(m for m in msgs_a if m.type == "output")

            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get(f"/api/tasks/{task_b.id}/messages/{target.id}/raw")
                assert resp.status_code == 404
                # task_b exists; the message exists but not under task_b
                # → the scoping check makes this a message-not-found case.
                assert resp.json()["detail"]["code"] == "MESSAGE_NOT_FOUND"

        run(_test())

    def test_get_message_raw_404_on_missing_task(self, app_with_service, run):
        """Unknown task id returns 404 with TASK_NOT_FOUND (no message lookup)."""
        app, service = app_with_service

        async def _test():
            task = await service.create_task("Real task")
            await service.db.add_message(task.id, "agent", "code", "stuff", "output", metadata={})
            msgs = await service.get_messages(task.id)
            target = next(m for m in msgs if m.type == "output")
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get(f"/api/tasks/does-not-exist/messages/{target.id}/raw")
                assert resp.status_code == 404
                # Task itself missing → TASK_NOT_FOUND, not MESSAGE_NOT_FOUND.
                assert resp.json()["detail"]["code"] == "TASK_NOT_FOUND"

        run(_test())


class TestPromoteRoute:
    """ADR-027 §1 — ``POST /api/tasks/{id}/promote`` (PR 1 R3).

    Fails pre-fix: the route does not exist (404 on POST), and
    ``PromoteRequest`` is not importable."""

    def test_promote_to_loaded_process_returns_task_detail(self, app_with_service, run):
        app, service = app_with_service

        async def _test():
            # Stage a loaded destination process in the catalog.
            service._processes["sibling_process"] = service.process
            task = await service.create_task("Promote me")
            await wait_for_completion(service, task.id)

            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    f"/api/tasks/{task.id}/promote",
                    json={"to_process": "sibling_process", "initial_artifacts": None},
                )
                assert resp.status_code == 200, resp.text
                data = resp.json()
                assert data["task"]["id"] == task.id

            row = await service.db.get_task(task.id)
            assert (row.metadata or {}).get("process_name") == "sibling_process"

        run(_test())

    def test_promote_unknown_process_returns_400_promote_not_allowed(self, app_with_service, run):
        app, service = app_with_service

        async def _test():
            task = await service.create_task("Promote me")
            await wait_for_completion(service, task.id)

            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    f"/api/tasks/{task.id}/promote",
                    json={"to_process": "not_a_thing"},
                )
                assert resp.status_code == 400
                assert resp.json()["detail"]["code"] == "PROMOTE_NOT_ALLOWED"

        run(_test())

    def test_promote_seeds_initial_artifacts(self, app_with_service, run):
        app, service = app_with_service

        async def _test():
            service._processes["sibling_process"] = service.process
            task = await service.create_task("Promote me")
            await wait_for_completion(service, task.id)

            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    f"/api/tasks/{task.id}/promote",
                    json={
                        "to_process": "sibling_process",
                        "initial_artifacts": {"draft_spec": "# Spec\nbuild it"},
                    },
                )
                assert resp.status_code == 200, resp.text

            content = await service.get_named_artifact(task.id, "draft_spec")
            assert content == "# Spec\nbuild it"

        run(_test())


class TestProcessesEndpointPromotionFields:
    """ADR-027 §3/§4 — ``GET /api/processes`` exposes ``description`` and
    ``promotion_inputs`` so the promotion modal can render input fields.

    Fails pre-fix: those keys are absent from the ProcessSummary response."""

    def test_processes_expose_description_and_promotion_inputs(self, app_with_service, run):
        app, _ = app_with_service

        async def _test():
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get("/api/processes")
                assert resp.status_code == 200
                for entry in resp.json():
                    assert "description" in entry
                    assert "promotion_inputs" in entry

        run(_test())

    def test_processes_expose_invocable(self, app_with_service, run):
        """ADR-044 Phase 4 — ``GET /api/processes`` exposes the ``invocable``
        property (a list of ``start``/``hand-off`` options) so the frontend
        hand-off picker filters on it instead of the literal name ``"chat"``.

        Fails pre-Phase-4: the ``invocable`` key is absent from the
        ``ProcessSummary`` response."""
        app, _ = app_with_service

        async def _test():
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get("/api/processes")
                assert resp.status_code == 200
                data = resp.json()
                assert data, "expected at least one loaded process"
                for entry in data:
                    assert "invocable" in entry, f"ProcessSummary must expose invocable; got {entry}"
                    assert isinstance(entry["invocable"], list)
                    assert all(isinstance(opt, str) for opt in entry["invocable"])

        run(_test())


class TestAttachmentsEndpoint:
    """``POST/GET /api/tasks/{id}/attachments`` — prompt file attachments (Path A).

    The raw-body upload endpoint stores bytes on disk and records only JSON
    metadata in ``tasks.metadata``. Written before the endpoint exists, so the
    POST/GET currently 404/405 — that is the expected red.
    """

    @staticmethod
    async def _new_task(service):
        task = await service.create_task("Attach task")
        await wait_for_completion(service, task.id)
        return task

    @staticmethod
    def _attach_dir(service, task_id, project_id="default"):
        return service.config.data_dir / "attachments" / project_id / task_id

    def test_upload_png_stores_bytes_and_lists_it(self, app_with_service, run):
        app, service = app_with_service

        async def _test():
            task = await self._new_task(service)
            data = b"\x89PNG\r\n\x1a\nscreenshot-bytes"
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    f"/api/tasks/{task.id}/attachments?filename=shot.png",
                    content=data,
                    headers={"content-type": "image/png"},
                )
                assert resp.status_code == 200
                record = resp.json()
                assert record["filename"] == "shot.png"
                assert record["rel_path"] == ".lotsa/attachments/shot.png"
                assert record["size_bytes"] == len(data)

                # Bytes live under {data_dir}/attachments/{project}/{task}/.
                stored = self._attach_dir(service, task.id) / "shot.png"
                assert stored.exists()
                assert stored.read_bytes() == data

                # Listing endpoint surfaces it.
                listing = await client.get(f"/api/tasks/{task.id}/attachments")
                assert listing.status_code == 200
                names = [a["filename"] for a in listing.json()]
                assert names == ["shot.png"]

            # Metadata carries the record (audit log does not — see below).
            fresh = await service.db.get_task(task.id)
            assert [a["filename"] for a in fresh.metadata["attachments"]] == ["shot.png"]

        run(_test())

    def test_accepts_any_file_type(self, app_with_service, run):
        app, service = app_with_service

        async def _test():
            task = await self._new_task(service)
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                for name, ctype in [
                    ("doc.pdf", "application/pdf"),
                    ("data.csv", "text/csv"),
                    ("sheet.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
                ]:
                    resp = await client.post(
                        f"/api/tasks/{task.id}/attachments?filename={name}",
                        content=b"payload-bytes",
                        headers={"content-type": ctype},
                    )
                    assert resp.status_code == 200, f"{name} should be accepted (no type filtering)"
                    assert resp.json()["filename"] == name

        run(_test())

    def test_traversal_filename_is_sanitized(self, app_with_service, run):
        app, service = app_with_service

        async def _test():
            task = await self._new_task(service)
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    f"/api/tasks/{task.id}/attachments?filename=../../evil.sh",
                    content=b"#!/bin/sh\n",
                    headers={"content-type": "text/x-sh"},
                )
                assert resp.status_code == 200
                assert resp.json()["filename"] == "evil.sh"

            # Nothing escaped the task's own attachment directory.
            assert (self._attach_dir(service, task.id) / "evil.sh").exists()
            assert not (service.config.data_dir / "attachments" / "evil.sh").exists()

        run(_test())

    def test_rejects_oversized_file(self, app_with_service, run):
        app, service = app_with_service

        async def _test():
            task = await self._new_task(service)
            oversized = b"\0" * (25 * 1024 * 1024 + 1)
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    f"/api/tasks/{task.id}/attachments?filename=big.bin",
                    content=oversized,
                    headers={"content-type": "application/octet-stream"},
                )
                assert resp.status_code == 400 or resp.status_code == 413
            # Rejected upload leaves no record.
            fresh = await service.db.get_task(task.id)
            assert not fresh.metadata.get("attachments")

        run(_test())

    def test_rejects_oversized_without_content_length(self, app_with_service, run):
        """A client that omits Content-Length can't force unbounded buffering.

        Sending a chunked body (async generator → no Content-Length header)
        bypasses the header pre-check, so only the streaming size cap can stop
        it. The upload must still be rejected and leave no record.
        """
        app, service = app_with_service

        async def _test():
            task = await self._new_task(service)

            async def _oversized_chunks():
                # 26 × 1 MB = 26 MB > the 25 MB cap, streamed without a
                # Content-Length header (httpx uses chunked transfer-encoding).
                for _ in range(26):
                    yield b"\0" * (1024 * 1024)

            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    f"/api/tasks/{task.id}/attachments?filename=big.bin",
                    content=_oversized_chunks(),
                    headers={"content-type": "application/octet-stream"},
                )
                assert resp.status_code == 413
            fresh = await service.db.get_task(task.id)
            assert not fresh.metadata.get("attachments")

        run(_test())

    def test_rejects_eleventh_file(self, app_with_service, run):
        app, service = app_with_service

        async def _test():
            task = await self._new_task(service)
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                for i in range(10):
                    resp = await client.post(
                        f"/api/tasks/{task.id}/attachments?filename=f{i}.txt",
                        content=b"x",
                        headers={"content-type": "text/plain"},
                    )
                    assert resp.status_code == 200, f"file {i} should succeed"
                # The 11th is rejected — count cap is per task, across its lifetime.
                overflow = await client.post(
                    f"/api/tasks/{task.id}/attachments?filename=f10.txt",
                    content=b"x",
                    headers={"content-type": "text/plain"},
                )
                assert overflow.status_code == 400

            fresh = await service.db.get_task(task.id)
            assert len(fresh.metadata["attachments"]) == 10

        run(_test())

    def test_duplicate_name_is_suffixed_not_overwritten(self, app_with_service, run):
        app, service = app_with_service

        async def _test():
            task = await self._new_task(service)
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                first = await client.post(
                    f"/api/tasks/{task.id}/attachments?filename=bug.png",
                    content=b"AAAA",
                    headers={"content-type": "image/png"},
                )
                second = await client.post(
                    f"/api/tasks/{task.id}/attachments?filename=bug.png",
                    content=b"BBBB",
                    headers={"content-type": "image/png"},
                )
                assert first.json()["filename"] == "bug.png"
                assert second.json()["filename"] == "bug (1).png"

            root = self._attach_dir(service, task.id)
            assert (root / "bug.png").read_bytes() == b"AAAA"
            assert (root / "bug (1).png").read_bytes() == b"BBBB"

        run(_test())

    def test_unknown_task_is_404(self, app_with_service, run):
        app, _ = app_with_service

        async def _test():
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                post = await client.post(
                    "/api/tasks/deadbeef/attachments?filename=x.png",
                    content=b"x",
                    headers={"content-type": "image/png"},
                )
                assert post.status_code == 404
                get = await client.get("/api/tasks/deadbeef/attachments")
                assert get.status_code == 404

        run(_test())

    def test_attachment_bytes_absent_from_audit_log(self, app_with_service, run):
        app, service = app_with_service

        async def _test():
            task = await self._new_task(service)
            marker = b"ATTACHMENT_CONTENT_MARKER_XYZ"
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    f"/api/tasks/{task.id}/attachments?filename=note.txt",
                    content=marker,
                    headers={"content-type": "text/plain"},
                )
                assert resp.status_code == 200
                # The audit/message log must never carry the uploaded bytes —
                # only tasks.metadata does (spec AC 4).
                messages = await client.get(f"/api/tasks/{task.id}/messages")
                assert messages.status_code == 200
                assert all("ATTACHMENT_CONTENT_MARKER_XYZ" not in m["content"] for m in messages.json())

        run(_test())

    def test_deferred_create_then_upload_then_dispatch(self, app_with_service, run):
        """The create-then-upload flow: POST /tasks with defer_dispatch, upload
        the file, then POST /tasks/{id}/dispatch to run the first step.

        Holding the first dispatch is what lets the operator's attachment land
        before the agent runs (spec AC 1). Asserted at the HTTP layer via the
        gate: a dispatched first step reaches ``waiting``; a deferred one does
        not, until the dispatch endpoint releases it.
        """
        app, service = app_with_service

        async def _test():
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                created = await client.post(
                    "/api/tasks",
                    json={"message": "Build this from the screenshot", "defer_dispatch": True},
                )
                assert created.status_code == 200
                task_id = created.json()["task"]["id"]

                # Deferred: the first step has NOT run, so the evaluate gate has
                # not parked it at ``waiting``.
                await asyncio.sleep(0.2)
                row = await service.db.get_task(task_id)
                assert row.status != "waiting", "deferred create must not dispatch the first step"

                # Upload the attachment now that the task exists.
                up = await client.post(
                    f"/api/tasks/{task_id}/attachments?filename=shot.png",
                    content=b"PNGDATA",
                    headers={"content-type": "image/png"},
                )
                assert up.status_code == 200

                # Release the first dispatch — the first step runs and parks at
                # the gate, and the attachment was materialized into its worktree.
                disp = await client.post(f"/api/tasks/{task_id}/dispatch")
                assert disp.status_code == 200
                await wait_for_status(service, task_id, "waiting")

            # The durable attachment survived and is recorded exactly once.
            fresh = await service.db.get_task(task_id)
            assert [a["filename"] for a in fresh.metadata["attachments"]] == ["shot.png"]

        run(_test())

    def test_second_dispatch_is_rejected(self, app_with_service, run):
        """A repeat ``POST /dispatch`` on an already-released task is a clean 400,
        not a silent second dispatch. Guards the orchestrator-owned single-agent
        invariant at the HTTP boundary (see the orchestrator-level regression for
        the mid-run self-loop it prevents)."""
        app, service = app_with_service

        async def _test():
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                created = await client.post(
                    "/api/tasks",
                    json={"message": "Build this", "defer_dispatch": True},
                )
                task_id = created.json()["task"]["id"]

                first = await client.post(f"/api/tasks/{task_id}/dispatch")
                assert first.status_code == 200
                await wait_for_status(service, task_id, "waiting")

                second = await client.post(f"/api/tasks/{task_id}/dispatch")
                assert second.status_code == 400
                assert second.json()["detail"]["code"] == "DISPATCH_NOT_ALLOWED"

        run(_test())

    def test_dispatch_unknown_task_is_404(self, app_with_service, run):
        app, _ = app_with_service

        async def _test():
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post("/api/tasks/deadbeef/dispatch")
                assert resp.status_code == 404

        run(_test())


class TestAttachmentVisibility:
    """Make uploaded attachments visible in the dashboard (spec: thumbnails +
    right-panel + raw endpoint).

    Three coordinated pieces are exercised here:

    * ``GET /api/tasks/{id}/attachments/{filename}/raw`` — the missing
      bytes-serving primitive both the bubble thumbnail and the right-panel
      list need. Written before the route exists, so the positive tests below
      currently 404 — that is the expected red.
    * message ↔ attachment linkage stamped into ``message.metadata.attachments``
      at INSERT (append-only-safe). Currently the message-POST bodies drop the
      attachment field on the floor, so the stamped-metadata assertions fail.
    * the first-message (deferred) path carries its attachments on the first
      ``You`` bubble once dispatch releases.
    """

    @staticmethod
    async def _waiting_task(service, message="Look at the screenshot"):
        """Create a task and drive it to ``waiting`` (the single evaluate gate in
        the server test flow parks it there after the agent runs)."""
        task = await service.create_task(message=message)
        await wait_for_status(service, task.id, "waiting")
        return task

    @staticmethod
    async def _upload(client, task_id, filename, data, ctype):
        resp = await client.post(
            f"/api/tasks/{task_id}/attachments?filename={filename}",
            content=data,
            headers={"content-type": ctype},
        )
        assert resp.status_code == 200, resp.text
        return resp.json()

    # ── raw bytes endpoint ──────────────────────────────────────────

    def test_raw_serves_stored_bytes_inline_with_recorded_mime(self, app_with_service, run):
        """The raw endpoint returns the exact stored bytes, the recorded MIME as
        Content-Type, and an inline disposition so a screenshot opens in-browser.

        Red pre-fix: the route does not exist → 404, so the 200 assertion fails.
        """
        app, service = app_with_service

        async def _test():
            task = await self._waiting_task(service)
            png = b"\x89PNG\r\n\x1a\nSCREENSHOT-BYTES"
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                await self._upload(client, task.id, "shot.png", png, "image/png")

                raw = await client.get(f"/api/tasks/{task.id}/attachments/shot.png/raw")
                assert raw.status_code == 200
                assert raw.content == png
                assert raw.headers["content-type"].startswith("image/png")
                assert "inline" in raw.headers.get("content-disposition", "")

        run(_test())

    def test_raw_unknown_filename_is_404(self, app_with_service, run):
        app, service = app_with_service

        async def _test():
            task = await self._waiting_task(service)
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get(f"/api/tasks/{task.id}/attachments/nope.png/raw")
                assert resp.status_code == 404

        run(_test())

    def test_raw_unknown_task_is_404(self, app_with_service, run):
        app, _ = app_with_service

        async def _test():
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get("/api/tasks/deadbeef/attachments/x.png/raw")
                assert resp.status_code == 404

        run(_test())

    def test_raw_is_task_scoped(self, app_with_service, run):
        """A filename recorded on task A must not be served under task B — the
        record lookup is keyed on the requesting task's own metadata."""
        app, service = app_with_service

        async def _test():
            task_a = await self._waiting_task(service)
            task_b = await self._waiting_task(service)
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                await self._upload(client, task_a.id, "secret.png", b"AAAA", "image/png")
                # Same filename, different task → not found.
                resp = await client.get(f"/api/tasks/{task_b.id}/attachments/secret.png/raw")
                assert resp.status_code == 404

        run(_test())

    def test_raw_rejects_unrecorded_file_on_disk(self, app_with_service, run):
        """A file physically present in the attach dir but absent from the task's
        metadata records must 404 — the endpoint serves only recorded
        attachments, never arbitrary bytes found on disk."""
        app, service = app_with_service

        async def _test():
            task = await self._waiting_task(service)
            attach_dir = service.config.data_dir / "attachments" / "default" / task.id
            attach_dir.mkdir(parents=True, exist_ok=True)
            (attach_dir / "orphan.png").write_bytes(b"NOT-RECORDED")
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get(f"/api/tasks/{task.id}/attachments/orphan.png/raw")
                assert resp.status_code == 404

        run(_test())

    def test_raw_html_upload_is_forced_to_download_not_inline(self, app_with_service, run):
        """An uploaded ``text/html`` file must never be served ``inline`` with its
        stored type — that renders as a same-origin script document against the
        dashboard. It is neutralized to ``application/octet-stream`` +
        ``attachment`` (download).

        Red pre-fix: the endpoint echoed the stored MIME with a hardcoded
        ``inline`` disposition → content-type ``text/html`` and no ``attachment``.
        """
        app, service = app_with_service

        async def _test():
            task = await self._waiting_task(service)
            html = b"<script>fetch('/api/tasks')</script>"
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                await self._upload(client, task.id, "evil.html", html, "text/html")

                raw = await client.get(f"/api/tasks/{task.id}/attachments/evil.html/raw")
                assert raw.status_code == 200
                assert raw.content == html  # bytes unchanged; only headers differ
                assert raw.headers["content-type"].startswith("application/octet-stream")
                assert "attachment" in raw.headers.get("content-disposition", "")
                assert raw.headers.get("x-content-type-options") == "nosniff"

        run(_test())

    def test_raw_svg_upload_is_forced_to_download_not_inline(self, app_with_service, run):
        """``image/svg+xml`` is image-ish but can carry script, so it is excluded
        from the inline allowlist and forced to download like any other
        non-raster type."""
        app, service = app_with_service

        async def _test():
            task = await self._waiting_task(service)
            svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                await self._upload(client, task.id, "x.svg", svg, "image/svg+xml")

                raw = await client.get(f"/api/tasks/{task.id}/attachments/x.svg/raw")
                assert raw.status_code == 200
                assert raw.headers["content-type"].startswith("application/octet-stream")
                assert "attachment" in raw.headers.get("content-disposition", "")

        run(_test())

    def test_raw_image_still_served_inline_with_nosniff(self, app_with_service, run):
        """An allowlisted raster image keeps its real type + ``inline`` (the
        screenshot case) and additionally carries ``nosniff`` so the declared
        image type can't be sniffed back to HTML."""
        app, service = app_with_service

        async def _test():
            task = await self._waiting_task(service)
            png = b"\x89PNG\r\n\x1a\nSCREENSHOT"
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                await self._upload(client, task.id, "shot.png", png, "image/png")

                raw = await client.get(f"/api/tasks/{task.id}/attachments/shot.png/raw")
                assert raw.status_code == 200
                assert raw.content == png
                assert raw.headers["content-type"].startswith("image/png")
                assert "inline" in raw.headers.get("content-disposition", "")
                assert raw.headers.get("x-content-type-options") == "nosniff"

        run(_test())

    # ── message ↔ attachment linkage (bubble) ───────────────────────

    def test_send_message_stamps_attachment_onto_bubble(self, app_with_service, run):
        """A file uploaded with a chat message is stamped onto that message's
        ``metadata.attachments`` at insert — the data the ``You`` bubble reads to
        render a thumbnail.

        Red pre-fix: ``/message`` ignores the ``attachments`` body field, so the
        inserted user message carries no attachment metadata.
        """
        app, service = app_with_service

        async def _test():
            task = await self._waiting_task(service)
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                rec = await self._upload(client, task.id, "shot.png", b"PNG", "image/png")

                sent = await client.post(
                    f"/api/tasks/{task.id}/message",
                    json={"message": "here is the screenshot", "attachments": [rec["filename"]]},
                )
                assert sent.status_code == 200

                msgs = await client.get(f"/api/tasks/{task.id}/messages")
                mine = [m for m in msgs.json() if m["role"] == "user" and m["content"] == "here is the screenshot"]
                assert len(mine) == 1, "the operator's message should be recorded exactly once"
                names = [a["filename"] for a in (mine[0]["metadata"].get("attachments") or [])]
                assert names == ["shot.png"]

        run(_test())

    def test_revise_stamps_attachment_onto_feedback(self, app_with_service, run):
        """The same linkage on the revise (feedback) path."""
        app, service = app_with_service

        async def _test():
            task = await self._waiting_task(service)
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                rec = await self._upload(client, task.id, "diagram.png", b"PNG", "image/png")

                revised = await client.post(
                    f"/api/tasks/{task.id}/revise",
                    json={"feedback": "match this diagram", "attachments": [rec["filename"]]},
                )
                assert revised.status_code == 200

                msgs = await client.get(f"/api/tasks/{task.id}/messages")
                mine = [m for m in msgs.json() if m["role"] == "user" and m["content"] == "match this diagram"]
                assert len(mine) == 1
                names = [a["filename"] for a in (mine[0]["metadata"].get("attachments") or [])]
                assert names == ["diagram.png"]

        run(_test())

    def test_deferred_first_message_carries_attachment(self, app_with_service, run):
        """The empty-state motivating case: attach a screenshot, create the task,
        and the first ``You`` bubble shows it after dispatch.

        The create-then-upload-then-dispatch sequence uploads *after* the first
        message would normally be inserted; the message must still carry the
        attachment (stamped when the deferred first step is released). Red
        pre-fix: the first message is inserted at create time with no attachment
        metadata and nothing stamps it afterwards.
        """
        app, service = app_with_service

        async def _test():
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                created = await client.post(
                    "/api/tasks",
                    json={"message": "Build this from the screenshot", "defer_dispatch": True},
                )
                task_id = created.json()["task"]["id"]

                await self._upload(client, task_id, "mockup.png", b"PNGDATA", "image/png")

                disp = await client.post(f"/api/tasks/{task_id}/dispatch")
                assert disp.status_code == 200
                await wait_for_status(service, task_id, "waiting")

                msgs = await client.get(f"/api/tasks/{task_id}/messages")
                chats = [
                    m for m in msgs.json() if m["role"] == "user" and m["content"] == "Build this from the screenshot"
                ]
                assert len(chats) == 1, "the first operator message should exist exactly once (append-only)"
                names = [a["filename"] for a in (chats[0]["metadata"].get("attachments") or [])]
                assert names == ["mockup.png"]

        run(_test())
