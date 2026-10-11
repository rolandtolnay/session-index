"""Explicit handoffs through public ownership and indexing APIs."""
import json
from pathlib import Path

import pytest

import db
import indexer
from pi_continuations import record_transfer, resolve_identity
from session_identity import canonical_session_id
from summarizer import SummaryResult


@pytest.fixture
def store(tmp_path, monkeypatch):
    root = tmp_path / "data"
    artifacts = root / "transcripts"
    monkeypatch.setattr(db, "DATA_DIR", str(root))
    monkeypatch.setattr(db, "DB_PATH", str(root / "sessions.db"))
    monkeypatch.setattr("transcript.TRANSCRIPT_DIR", str(artifacts))
    monkeypatch.setattr("tool_log.TRANSCRIPT_DIR", str(artifacts))
    monkeypatch.setattr("summarizer.summarize", lambda **kw: pytest.fail("Unexpected model call"))
    monkeypatch.setattr("summarizer.generate_headline", lambda **kw: pytest.fail("Unexpected model call"))
    return tmp_path, artifacts


def exchange(tag, parent=None):
    ts = "2026-10-01T12:00:01Z"
    return [
        {"type": "message", "id": tag + "u", "parentId": parent, "timestamp": ts,
         "message": {"role": "user", "content": "Request " + tag}},
        {"type": "message", "id": tag + "a", "parentId": tag + "u", "timestamp": ts,
         "message": {"role": "assistant", "content": [
             {"type": "text", "text": "Response " + tag},
             {"type": "toolCall", "id": tag + "write", "name": "write", "arguments": {"path": "/tmp/" + tag}}]}},
        {"type": "message", "id": tag + "r", "parentId": tag + "a", "timestamp": ts,
         "message": {"role": "toolResult", "toolCallId": tag + "write", "toolName": "write", "content": "ok"}},
    ]


def side(owner, tag, parent):
    child = f"{tag:08d}-1111-4111-8111-111111111111"
    common = {"version": 1, "ownerSessionId": owner, "sideChatId": child, "timestamp": "2026-10-01T12:01:00Z"}
    return [
        {"type": "custom", "id": f"{tag}open", "parentId": parent, "customType": "side-chat-archive",
         "data": {**common, "kind": "open", "openingLeafId": parent,
                  "focusedContent": {"kind": "selection", "label": "Evidence", "text": f"Focus {tag}"}}},
        {"type": "custom", "id": f"{tag}turn", "parentId": f"{tag}open", "customType": "side-chat-archive",
         "data": {**common, "kind": "turn", "sequence": 1, "question": f"Question {tag}", "answer": f"Answer {tag}", "model": "test"}},
    ]


def raw(path, native, entries, parent=None):
    header = {"type": "session", "version": 3, "id": native, "cwd": str(path.parent), "timestamp": "2026-10-01T12:00:00Z"}
    if parent:
        header["parentSession"] = str(parent)
    path.write_text("".join(json.dumps(e) + "\n" for e in [header, *entries]))
    return path


def transfer(source, destination, *, source_entries=None):
    captured = source_entries if source_entries is not None else [json.loads(line) for line in Path(source).read_text().splitlines()]
    return record_transfer(str(source), str(destination), source_entries=captured)


def copy(source, destination, native):
    entries = [json.loads(line) for line in source.read_text().splitlines()]
    raw(destination, native, entries[1:], source)
    return destination


def subagent(parent, name, *, content=None):
    path = parent.parent / parent.stem / name / "run-0" / "session.jsonl"
    path.parent.mkdir(parents=True)
    raw(path, "child-" + name, exchange(content or name))
    return path


def index(path, options=indexer.NO_SUMMARY_INDEX_OPTIONS, **kw):
    return indexer.index_source_transcript("pi", str(path), options, **kw)


def rows(table):
    conn = db.get_connection()
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM " + table)]
    finally:
        conn.close()


def test_transfer_chain_one_record_facts_children_selected_history_and_raw_unchanged(store):
    tmp, artifacts = store
    a = raw(tmp / "a.jsonl", "A", exchange("initial") + side("A", 1, "initialr"))
    # Archive on a now-abandoned branch must survive; main selects another leaf.
    with a.open("a") as handle:
        for e in exchange("chosen", "initialr"):
            handle.write(json.dumps(e) + "\n")
    sa = subagent(a, "worker", content="worker-a")
    first = index(a)
    conn = db.get_connection()
    db.set_hidden_from_recents(conn, first.session_id, True)
    conn.close()
    b = copy(a, tmp / "b.jsonl", "B")
    unchanged = {p: p.read_bytes() for p in (a, b, sa)}
    assert transfer(str(a), str(b)) == first.session_id
    assert resolve_identity("B", str(b)) == first.session_id
    # Inherited-only destination qualifies because explicit transfer owns it.
    assert not index(b).skipped_reason
    entries = [json.loads(line) for line in b.read_text().splitlines()][1:]
    raw(b, "B", entries + exchange("continued", "chosenr") + side("B", 2, "continuedr"), a)
    sb = subagent(b, "worker", content="worker-b")
    c = copy(b, tmp / "c.jsonl", "C")
    before = {p: p.read_bytes() for p in (a, b, c, sa, sb)}
    assert transfer(str(b), str(c)) == first.session_id
    assert transfer(str(a), str(tmp / "b.jsonl"), source_entries=[json.loads(line) for line in a.read_text().splitlines()]) == first.session_id
    result = index(c)
    assert result.session_id == first.session_id and not result.skipped_reason
    text = Path(result.transcript_path).read_text()
    for tag in ("initial", "chosen", "continued"):
        assert text.count("Request " + tag) == 1
    assert "Answer 1" not in text
    sessions = rows("sessions")
    assert len(sessions) == 1
    assert sessions[0]["native_session_id"] == "C" and sessions[0]["source_path"] == str(c)
    assert sessions[0]["identity_key"] == "A" and sessions[0]["hidden_from_recents"] == 1
    assert len(rows("side_chats")) == 2
    assert len(rows("subagent_runs")) == 2
    assert len(rows("file_mutations")) == 5  # 3 main writes + 2 children.
    assert {r["session_id"] for r in rows("tool_calls")} == {first.session_id}
    from evidence_find import find_candidates
    from evidence_inspect import inspect_ref
    with db.get_connection() as conn:
        assert db.get_session(conn, "A")["session_id"] == first.session_id
        assert db.get_session(conn, "B")["session_id"] == first.session_id
        found = find_candidates(conn, topic="Request")
        assert [item["ref"] for item in found["results"]] == [f"session/{first.session_id}"]
        packet = inspect_ref(conn, f"session/{first.session_id}")
        refs = packet["inspect_refs"]
        assert len(refs["side_chats"]) == 2 and len(refs["subagents"]) == 2
        for child in refs["side_chats"] + refs["subagents"]:
            evidence = inspect_ref(conn, child["ref"])
            assert evidence["evidence"] and evidence["session"]["session_id"] == first.session_id
        child_packets = [inspect_ref(conn, child["ref"]) for child in refs["subagents"]]
        assert len({packet["evidence"][0]["path"] for packet in child_packets}) == 2
        child_text = "\n".join(packet["evidence"][0]["text"] for packet in child_packets)
        assert "Request worker-a" in child_text and "Request worker-b" in child_text
    for ancestor in (a, b):
        assert index(ancestor).skipped_reason == "unchanged transferred Pi ancestor"
    snapshot = {p: p.read_bytes() for p in artifacts.rglob("*.md")}
    index(c)
    assert rows("sessions")[0]["native_session_id"] == "C"
    assert len(rows("file_mutations")) == 5
    assert {p: p.read_bytes() for p in artifacts.rglob("*.md")} == snapshot
    assert all(p.read_bytes() == content for p, content in before.items())
    assert a.read_bytes() == unchanged[a] and sa.read_bytes() == unchanged[sa]
    assert {p.name for p in artifacts.glob("*.md")} == {first.session_id + ".md", first.session_id + ".tools.md"}


def test_divergent_original_gets_stable_fork_only_new_children_and_can_transfer_again(store):
    tmp, _ = store
    a = raw(tmp / "a.jsonl", "A", exchange("old") + side("A", 1, "oldr"))
    subagent(a, "old-child")
    index(a)
    b = copy(a, tmp / "b.jsonl", "B")
    owner = transfer(str(a), str(b))
    index(b)
    entries = [json.loads(line) for line in a.read_text().splitlines()][1:]
    raw(a, "A", entries + exchange("fork", "oldr") + side("A", 2, "forkr"))
    subagent(a, "new-child")
    fork = resolve_identity("A", str(a))
    assert fork != owner and len(fork) == 19
    assert fork != canonical_session_id("pi", "A")
    assert index(a).session_id == fork
    assert index(a).session_id == fork
    with db.get_connection() as conn:
        assert db.get_session(conn, "A")["session_id"] == fork
    assert len(rows("sessions")) == 2
    assert {r["side_chat_id"] for r in rows("side_chats") if r["parent_session_id"] == fork} == {"00000002-1111-4111-8111-111111111111"}
    assert len([r for r in rows("subagent_runs") if r["parent_session_id"] == fork]) == 1
    assert next(r for r in rows("sessions") if r["session_id"] == owner)["native_session_id"] == "B"
    d = copy(a, tmp / "d.jsonl", "D")
    assert transfer(str(a), str(d)) == fork
    index(d)
    assert resolve_identity("A", str(a)) == fork
    assert index(a).skipped_reason
    index(b)
    assert len(rows("side_chats")) == 2
    assert len(rows("subagent_runs")) == 2
    assert next(r for r in rows("sessions") if r["session_id"] == fork)["native_session_id"] == "D"


def test_independent_clone_and_historical_lineage_not_consolidated(store):
    tmp, _ = store
    a = raw(tmp / "a.jsonl", "A", exchange("old") + side("A", 1, "oldr"))
    owner = index(a).session_id
    b = copy(a, tmp / "b.jsonl", "B")
    assert index(b).skipped_reason == "no new Pi conversation after clone"
    entries = [json.loads(line) for line in b.read_text().splitlines()][1:]
    raw(b, "B", entries + exchange("independent", "oldr"), a)
    independent = index(b).session_id
    assert independent == canonical_session_id("pi", "B") and independent != owner
    assert len(rows("sessions")) == 2 and len(rows("side_chats")) == 1
    with pytest.raises(ValueError, match="independent ownership"):
        transfer(str(a), str(b))


def test_live_checkpoint_includes_unflushed_source_and_readonly_resolution(store):
    tmp, _ = store
    a = raw(tmp / "a.jsonl", "A", exchange("old"))
    live = exchange("old") + exchange("live", "oldr")
    b = raw(tmp / "b.jsonl", "B", live, a)
    before = a.read_bytes()
    owner = transfer(str(a), str(b), source_entries=[json.loads(a.read_text().splitlines()[0]), *live])
    index(b)
    assert index(a).skipped_reason
    # Flush of entries already copied at handoff is not divergent activity.
    raw(a, "A", live)
    assert resolve_identity("A", str(a)) == owner
    assert index(a).skipped_reason
    assert resolve_identity("B", str(b), db_path=db.DB_PATH) == owner
    assert len(rows("sessions")) == 1
    with pytest.raises(ValueError, match="path/native"):
        resolve_identity("A", str(b))
    with pytest.raises(ValueError, match="path/native"):
        resolve_identity("wrong", str(b))
    missing_db = tmp / "absent" / "sessions.db"
    assert resolve_identity("X", str(tmp / "missing.jsonl"), db_path=str(missing_db)) == canonical_session_id("pi", "X")
    assert not missing_db.parent.exists()
    assert before != a.read_bytes()  # Fixture flush only; registry never edits raw.


def test_inflight_summary_cannot_replace_transfer_and_deterministic_refresh(store, monkeypatch):
    tmp, _ = store
    a = raw(tmp / "a.jsonl", "A", exchange("old"))
    owner = index(a).session_id
    b = copy(a, tmp / "b.jsonl", "B")
    def during_model(**kw):
        assert transfer(str(a), str(b)) == owner  # No ownership lock held.
        entries = [json.loads(line) for line in b.read_text().splitlines()][1:]
        raw(b, "B", entries + exchange("new", "oldr"), a)
        index(b)
        return SummaryResult("stale summary")
    monkeypatch.setattr("summarizer.summarize", during_model)
    monkeypatch.setattr("summarizer.generate_headline", lambda **kw: "stale headline")
    result = index(a, indexer.FULL_INDEX_OPTIONS)
    assert result.skipped_reason.startswith("stale Pi refresh")
    row = rows("sessions")[0]
    assert row["native_session_id"] == "B" and row["summary"] is None
    assert "Request new" in Path(row["transcript_path"]).read_text()
    assert len(rows("sessions")) == 1


def test_stale_parsed_session_cannot_regress_deterministic_or_summary_publication(store, monkeypatch):
    tmp, _ = store
    a = raw(tmp / "a.jsonl", "A", exchange("old"))
    stale = indexer.parse_session_file("pi", str(a))
    raw(a, "A", exchange("old") + exchange("new", "oldr"))
    result = index(a, parsed_session=stale)
    assert "Request new" in Path(result.transcript_path).read_text()
    def during_model(**kw):
        raw(a, "A", exchange("old") + exchange("new", "oldr") + exchange("later", "newr"))
        index(a)
        return SummaryResult("stale description")
    monkeypatch.setattr("summarizer.summarize", during_model)
    monkeypatch.setattr("summarizer.generate_headline", lambda **kw: "headline")
    assert index(a, indexer.SUMMARY_ONLY_INDEX_OPTIONS).skipped_reason
    row = rows("sessions")[0]
    assert row["summary"] is None and row["user_message_count"] == 3
    assert len(rows("file_mutations")) == 3


def test_lineage_copy_validation_and_collision_safety(store):
    tmp, _ = store
    a = raw(tmp / "a.jsonl", "A", exchange("old"))
    b = raw(tmp / "b.jsonl", "B", exchange("different"), a)
    with pytest.raises(ValueError, match="preserve"):
        transfer(str(a), str(b))
    copy(a, b, "B")
    conn = db.get_connection()
    db.init_db(conn)
    db.upsert_session(conn, session_id=canonical_session_id("pi", "A"), source="pi", native_session_id="wrong")
    conn.close()
    with pytest.raises(ValueError, match="identity conflict"):
        transfer(str(a), str(b))


def test_transfer_before_first_index_is_durable_and_preserves_original_start(store):
    tmp, _ = store
    a = raw(tmp / "a.jsonl", "A", exchange("old"))
    b = copy(a, tmp / "b.jsonl", "B")
    entries = [json.loads(line) for line in b.read_text().splitlines()]
    entries[0]["timestamp"] = "2026-10-02T12:00:00Z"
    b.write_text("".join(json.dumps(e) + "\n" for e in entries))
    before = {p: p.read_bytes() for p in (a, b)}
    owner = transfer(str(a), str(b))
    assert not rows("sessions")
    assert resolve_identity("B", str(b)) == owner
    assert index(b).session_id == owner
    assert rows("sessions")[0]["started_at"] == "2026-10-01T12:00:00Z"
    assert all(p.read_bytes() == text for p, text in before.items())
    conn = db.get_connection()
    db.upsert_session(conn, session_id=owner, summary="updated description")
    assert db.get_session(conn, owner)["identity_key"] == "A"
    conn.close()


def test_child_open_at_transfer_cannot_be_adopted_by_later_fork(store):
    tmp, _ = store
    archive = side("A", 1, "oldr")
    a = raw(tmp / "a.jsonl", "A", exchange("old") + archive[:1])
    index(a)
    b = copy(a, tmp / "b.jsonl", "B")
    owner = transfer(str(a), str(b))
    raw(a, "A", exchange("old") + archive + exchange("fork", "oldr"))
    fork = index(a).session_id
    index(b)
    chats = rows("side_chats")
    assert len(chats) == 1 and chats[0]["parent_session_id"] == owner
    assert fork != owner


def test_unflushed_source_handoff_uses_live_header_and_retains_live_archives(store):
    tmp, _ = store
    source = tmp / "never-flushed.jsonl"
    live_header = {"type": "session", "version": 3, "id": "A", "cwd": str(tmp),
                   "timestamp": "2026-09-30T12:00:00Z"}
    archives = side("A", 1, "oldr")
    live = [live_header, *exchange("old"), *archives]
    destination = raw(tmp / "b.jsonl", "B", live[1:], source)
    original = destination.read_bytes()
    owner = transfer(str(source), str(destination), source_entries=live)
    assert owner == canonical_session_id("pi", "A")
    assert transfer(str(source), str(destination), source_entries=live) == owner
    assert resolve_identity("A", str(source)) == owner
    result = index(destination)
    assert not result.skipped_reason and result.session_id == owner
    assert rows("sessions")[0]["started_at"] == live_header["timestamp"]
    child = rows("side_chats")[0]
    assert child["parent_session_id"] == owner
    assert "Answer 1" in Path(child["transcript_path"]).read_text()
    assert not source.exists() and destination.read_bytes() == original
    assert len(rows("pi_continuation_children")) == 1
    assert rows("pi_continuation_children")[0]["source_path"] == str(source)
    # An ordinary copy with new conversation still cannot adopt those archives.
    clone = copy(destination, tmp / "clone.jsonl", "clone")
    copied = [json.loads(line) for line in clone.read_text().splitlines()][1:]
    raw(clone, "clone", copied + exchange("independent", "oldr"), destination)
    assert not index(clone).skipped_reason
    assert len(rows("side_chats")) == 1


def test_unflushed_empty_transfer_keeps_minimum_content_threshold(store):
    tmp, artifacts = store
    source = tmp / "empty-source.jsonl"
    live = [{"type": "session", "id": "empty", "cwd": str(tmp), "timestamp": "2026-09-30T12:00:00Z"}]
    destination = raw(tmp / "empty-destination.jsonl", "empty-copy", [], source)
    owner = transfer(str(source), str(destination), source_entries=live)
    result = index(destination)
    assert result.session_id == owner and result.skipped_reason == "0 user, 0 assistant msgs"
    assert not rows("sessions") and not source.exists()
    assert not list(artifacts.glob("*.md"))


def test_live_only_open_archive_can_complete_in_retained_destination(store):
    tmp, _ = store
    source = tmp / "missing-source.jsonl"
    archive = side("A", 1, "oldr")
    live = [{"type": "session", "id": "A", "timestamp": "2026-10-01T12:00:00Z"},
            *exchange("old"), archive[0]]
    destination = raw(tmp / "b.jsonl", "B", live[1:], source)
    owner = transfer(str(source), str(destination), source_entries=live)
    index(destination)
    assert len(rows("pi_continuation_children")) == 1 and not rows("side_chats")
    with destination.open("a") as handle:
        handle.write(json.dumps(archive[1]) + "\n")
    index(destination)
    assert rows("side_chats")[0]["parent_session_id"] == owner
    assert "Answer 1" in Path(rows("side_chats")[0]["transcript_path"]).read_text()
    assert not source.exists()


def test_live_archives_not_yet_on_disk_are_read_from_transfer_copy(store):
    tmp, _ = store
    source = raw(tmp / "a.jsonl", "A", exchange("old"))
    persisted = source.read_bytes()
    live = [json.loads(line) for line in source.read_text().splitlines()] + side("A", 1, "oldr")
    destination = raw(tmp / "b.jsonl", "B", live[1:], source)
    owner = transfer(str(source), str(destination), source_entries=live)
    index(destination)
    assert rows("side_chats")[0]["parent_session_id"] == owner
    assert source.read_bytes() == persisted


def test_retired_source_archive_event_refreshes_its_current_owner_without_a_new_turn(store, monkeypatch):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hooks"))
    import pi_index
    tmp, _ = store
    archive = side("A", 1, "oldr")
    source = raw(tmp / "a.jsonl", "A", exchange("old") + archive[:1])
    destination = copy(source, tmp / "b.jsonl", "B")
    owner = transfer(str(source), str(destination))
    index(destination)
    assert not rows("side_chats")
    with source.open("a") as handle:
        handle.write(json.dumps(archive[1]) + "\n")
    assert index(source).skipped_reason == "unchanged transferred Pi ancestor"
    monkeypatch.setattr(sys, "argv", ["pi_index.py", "--mode", "side-chat", "--session-file", str(source)])
    pi_index.main()
    assert len(rows("side_chats")) == 1
    assert rows("side_chats")[0]["parent_session_id"] == owner
    assert rows("sessions")[0]["native_session_id"] == "B"
    assert len(rows("sessions")) == 1


def test_source_append_between_capture_and_registration_preserves_transfer_and_fork_checkpoint(store):
    tmp, _ = store
    archive = side("A", 1, "oldr")
    source = raw(tmp / "a.jsonl", "A", exchange("old") + archive[:1])
    captured = [json.loads(line) for line in source.read_text().splitlines()]
    destination = copy(source, tmp / "b.jsonl", "B")
    # Pi can finish archival after copying the tree but before registration.
    # Any independent conversation activity added then is not in the checkpoint.
    with source.open("a") as handle:
        for entry in [archive[1], *exchange("fork", "oldr")]:
            handle.write(json.dumps(entry) + "\n")
    owner = transfer(str(source), str(destination), source_entries=captured)
    index(destination)
    assert rows("side_chats")[0]["parent_session_id"] == owner
    assert index(source).session_id != owner
    assert next(row for row in rows("sessions") if row["session_id"] == owner)["native_session_id"] == "B"


def test_full_live_snapshot_still_validates_native_source_and_copied_tree(store):
    tmp, _ = store
    source = raw(tmp / "a.jsonl", "A", exchange("old"))
    live = [json.loads(line) for line in source.read_text().splitlines()]
    destination = copy(source, tmp / "b.jsonl", "B")
    with pytest.raises(ValueError, match="native identity"):
        transfer(str(source), str(destination), source_entries=[{**live[0], "id": "wrong"}, *live[1:]])
    with pytest.raises(ValueError, match="preserve"):
        transfer(str(source), str(destination), source_entries=[*live, *exchange("uncopied", "oldr")])
    with pytest.raises(ValueError, match="source tree"):
        altered = json.loads(json.dumps(live))
        altered[1]["message"]["content"] = "Conflicting captured entry"
        raw(destination, "B", altered[1:], source)
        transfer(str(source), str(destination), source_entries=altered)
    assert not rows("pi_transfers")


def test_live_extension_details_do_not_relax_content_or_destination_copy_checks(store):
    tmp, _ = store
    custom = {"type": "custom_message", "id": "progress", "parentId": "oldr",
              "customType": "workflow-progress", "content": "Working", "details": {"status": "running"}}
    source = raw(tmp / "a.jsonl", "A", [*exchange("old"), custom])
    live = [json.loads(line) for line in source.read_text().splitlines()]
    live[-1]["details"] = {"status": "success"}
    # Updating metadata does not permit replacing the actual custom message.
    live[-1]["content"] = "Different instructions"
    destination = raw(tmp / "b.jsonl", "B", live[1:], source)
    with pytest.raises(ValueError, match="source tree"):
        transfer(source, destination, source_entries=live)
    live[-1]["content"] = custom["content"]
    # The destination must retain the latest details, not the stale disk copy.
    raw(destination, "B", [*exchange("old"), custom], source)
    with pytest.raises(ValueError, match="preserve"):
        transfer(source, destination, source_entries=live)
    assert not rows("pi_transfers")
    raw(destination, "B", live[1:], source)
    assert transfer(source, destination, source_entries=live) == canonical_session_id("pi", "A")


def test_fork_collision_checks_not_bypassed(store):
    tmp, _ = store
    a = raw(tmp / "a.jsonl", "A", exchange("old"))
    index(a)
    b = copy(a, tmp / "b.jsonl", "B")
    transfer(str(a), str(b))
    raw(a, "A", exchange("old") + exchange("new", "oldr"))
    fork = resolve_identity("A", str(a))
    conn = db.get_connection()
    db.upsert_session(conn, session_id=fork, source="pi", native_session_id="collision")
    conn.close()
    with pytest.raises(ValueError, match="identity conflict"):
        index(a)
