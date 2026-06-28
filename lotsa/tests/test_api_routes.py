"""Tests for the JSON API routes."""

from __future__ import annotations

from httpx import ASGITransport, AsyncClient

from lotsa.tests.conftest import wait_for_completion


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
