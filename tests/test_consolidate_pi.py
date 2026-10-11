"""One-time migration through planning, publication, inspection, and rollback."""
import json
from pathlib import Path

import pytest

import consolidate_pi as migration
import db
import indexer
from evidence_find import find_candidates
from evidence_inspect import inspect_ref
from test_pi_continuations import raw, copy, exchange, side, subagent


@pytest.fixture
def history(tmp_path, monkeypatch):
    root = tmp_path / "index"
    raw_root = tmp_path / "raw"
    (raw_root / "main").mkdir(parents=True)
    (raw_root / "worktree").mkdir()
    monkeypatch.delenv("SESSION_INDEX_SOURCE_PATH", raising=False)
    monkeypatch.setattr("summarizer.summarize", lambda **kw: pytest.fail("migration must not call models"))
    monkeypatch.setattr("summarizer.generate_headline", lambda **kw: pytest.fail("migration must not call models"))
    a = raw(raw_root / "main" / "a.jsonl", "native-a", exchange("initial"))
    b = copy(a, raw_root / "worktree" / "b.jsonl", "native-b")
    with b.open("a") as handle:
        handle.write(json.dumps({"type":"session_info", "id":"handoff", "parentId":"initialr", "name":"Branch: feature/example"})+"\n")
        for e in exchange("continued", "handoff") + side("native-b", 1, "continuedr"):
            handle.write(json.dumps(e)+"\n")
    child = subagent(b, "worker")
    with migration.storage(root):
        first = indexer.index_source_transcript("pi", str(a), indexer.NO_SUMMARY_INDEX_OPTIONS)
        second = indexer.index_source_transcript("pi", str(b), indexer.NO_SUMMARY_INDEX_OPTIONS)
        conn = db.get_connection()
        conn.execute("UPDATE sessions SET project_id='git:/fixture/repo/.git', hidden_from_recents=1")
        conn.execute("UPDATE sessions SET summary='Implementation completed' WHERE session_id=?", (second.session_id,))
        conn.commit(); conn.close()
    # C has no additional exchange, but the full return workflow tree persists.
    c = copy(b, raw_root / "main" / "c.jsonl", "native-c")
    with c.open("a") as handle:
        handle.write(json.dumps({"type":"session_info", "id":"returned", "parentId":"continuedr", "name":"Branch: feature/example"})+"\n")
    return {"root":root, "raw_root":raw_root, "paths":[a,b,c], "child":child,
            "ids":[first.session_id,second.session_id], "output":tmp_path/"rehearsal"}


def inventory(root):
    with db._get_readonly_connection(str(root/"sessions.db")) as conn:
        ids = [r[0] for r in conn.execute("SELECT session_id FROM sessions")]
        return migration.snapshot(conn,ids)


def make_plan(h):
    return migration.plan(h["root"],h["raw_root"],h["output"],idle_seconds=0)


def artifacts(h):
    return {str(p.relative_to(h["root"])): p.read_bytes()
            for p in (h["root"] / "transcripts").rglob("*.md")}


def raw_files(h):
    return {p: p.read_bytes() for p in h["raw_root"].rglob("*") if p.is_file()}


def index_unrelated(h, tag):
    directory = h["raw_root"] / "unrelated"
    directory.mkdir(exist_ok=True)
    entries = exchange(tag)
    entries[0]["message"]["content"] = "Unrelated conversation " + tag
    path = raw(directory / f"{tag}.jsonl", tag, entries)
    with migration.storage(h["root"]):
        return indexer.index_source_transcript("pi", str(path), indexer.NO_SUMMARY_INDEX_OPTIONS).session_id


def test_chain_preserves_evidence_refs_and_compatibility_files_then_rolls_back(history, monkeypatch):
    h = history
    earlier_unrelated = index_unrelated(h, "before-plan")
    before = inventory(h["root"])
    with db._get_readonly_connection(str(h["root"] / "sessions.db")) as conn:
        affected_before = migration.snapshot(conn, h["ids"])
    files = artifacts(h)
    raws = raw_files(h)
    receipt = make_plan(h)
    assert receipt["groups"] == receipt["duplicate_rows_removed"] == 1
    assert inventory(h["root"]) == before
    assert artifacts(h) == files
    assert raw_files(h) == raws
    assert migration.apply(receipt["plan"], receipt["digest"])["status"] == "applied"
    with db._get_readonly_connection(str(h["output"] / "backup.db")) as conn:
        assert migration.snapshot(conn, h["ids"]) == affected_before
    with db._get_readonly_connection(str(h["root"]/"sessions.db")) as conn:
        sessions = list(conn.execute("SELECT * FROM sessions WHERE session_id != ?", (earlier_unrelated,)))
        assert len(sessions) == 1
        assert sessions[0]["session_id"] == h["ids"][0]
        assert sessions[0]["native_session_id"] == "native-c"
        assert sessions[0]["hidden_from_recents"] == 1
        assert sessions[0]["summary"] == "Implementation completed"
        result = find_candidates(conn, topic="Request")
        assert [r["ref"] for r in result["results"]] == [f"session/{h['ids'][0]}"]
        # Existing B child tool ordinal still resolves to the child's write, not
        # a different main-session event with the same tool name.
        packet = inspect_ref(conn,f"tool/{h['ids'][1]}/3")
        assert packet["session"]["session_id"] == h["ids"][0]
        assert "workerwrite" in packet["evidence"][0]["text"]
        assert inspect_ref(conn,f"subagent/{h['ids'][1]}/0")["evidence"]
        chat_id = "00000001-1111-4111-8111-111111111111"
        chat = inspect_ref(conn, f"sidechat/{h['ids'][1]}/{chat_id}")
        assert chat["session"]["session_id"] == h["ids"][0]
        assert chat["match"]["ref"] == f"sidechat/{h['ids'][0]}/{chat_id}"
        assert "Focus 1" in chat["evidence"][0]["text"]
        assert "Question 1" in chat["evidence"][0]["text"]
        assert "Answer 1" in chat["evidence"][0]["text"]
        old_chat = next(row for row in before["side_chats"] if row["parent_session_id"] == h["ids"][1])
        old_chat_path = Path(old_chat["transcript_path"])
        assert old_chat_path.read_bytes() == files[str(old_chat_path.relative_to(h["root"]))]
        import cli
        monkeypatch.setattr(cli, "TRANSCRIPT_DIR", str(h["root"]/"transcripts"))
        issues = cli._check_integrity(conn)
        assert not issues["orphaned_transcripts"] and not issues["orphaned_subagent_dirs"]
    for rel,content in files.items():
        if h["ids"][1] in rel:
            assert (h["root"]/rel).read_bytes() == content
    with migration.storage(h["root"]):
        for p in h["paths"][:-1]:
            assert indexer.index_source_transcript("pi",str(p),indexer.NO_SUMMARY_INDEX_OPTIONS).skipped_reason
    assert migration.apply(receipt["plan"],receipt["digest"])["already_complete"]
    assert raw_files(h) == raws
    later_unrelated = index_unrelated(h, "after-apply")
    with db._get_readonly_connection(str(h["root"] / "sessions.db")) as conn:
        later_rows = migration.snapshot(conn, [later_unrelated])
    later_files = {rel: content for rel, content in artifacts(h).items() if later_unrelated in rel}
    raws.update({p: content for p, content in raw_files(h).items() if p not in raws})
    second = migration.plan(h["root"],h["raw_root"],h["output"].with_name("second"),idle_seconds=0)
    assert second["groups"] == 0
    assert migration.apply(receipt["plan"],receipt["digest"],rollback=True)["status"] == "rolled_back"
    assert inventory(h["root"]) == {
        table: sorted(rows + later_rows[table], key=migration.encoded)
        for table, rows in before.items()
    }
    assert artifacts(h) == {**files, **later_files}
    assert raw_files(h) == raws
    assert Path(h["output"]/"backup.db").is_file()


def test_rejects_chain_when_original_owner_child_ordinals_move(history):
    h = history
    subagent(h["paths"][0],"planning-worker")
    with migration.storage(h["root"]):
        indexer.index_source_transcript("pi",str(h["paths"][0]),indexer.NO_SUMMARY_INDEX_OPTIONS)
    before = inventory(h["root"])
    result = make_plan(h)
    assert result["groups"] == 0
    assert inventory(h["root"]) == before
    # Tentative transfers from a rejected rehearsal must not survive in scratch.
    with db._get_readonly_connection(str(h["output"]/"after/sessions.db")) as conn:
        assert not conn.execute("SELECT 1 FROM pi_transfers").fetchone()
        assert conn.execute("SELECT count(*) FROM sessions").fetchone()[0] == 2


@pytest.mark.parametrize("change", ["raw", "indexed", "artifact", "new-child"])
def test_changed_inputs_refuse_application_without_partial_publication(history, change):
    h = history
    receipt = make_plan(h)
    assert receipt["groups"] == 1
    if change == "raw":
        with h["paths"][1].open("a") as handle:
            for e in exchange("later","continuedr"):
                handle.write(json.dumps(e)+"\n")
    elif change == "indexed":
        with migration.storage(h["root"]):
            conn=db.get_connection()
            conn.execute("UPDATE sessions SET hidden_from_recents=0")
            conn.commit(); conn.close()
    elif change == "artifact":
        (h["root"]/"transcripts"/f"{h['ids'][0]}.md").write_text("newer content")
    else:
        subagent(h["paths"][0],"new-worker")
    before = inventory(h["root"])
    files = artifacts(h)
    raws = raw_files(h)
    with pytest.raises(ValueError):
        migration.apply(receipt["plan"],receipt["digest"])
    assert inventory(h["root"]) == before
    assert artifacts(h) == files
    assert raw_files(h) == raws
    assert not (h["output"]/"journal.json").exists()


def test_uncertain_downstream_copy_blocks_otherwise_valid_parent_pair(history):
    h = history
    entries = migration.read_raw(h["paths"][2])
    entries[1]["message"]["content"] = "different source content"
    h["paths"][2].write_text("".join(json.dumps(e)+"\n" for e in entries))
    receipt = make_plan(h)
    assert receipt["groups"] == 0
    assert receipt["excluded_reasons"]["chain contains an uncertain transfer"] == 1


def test_failure_during_publication_restores_original_rows_and_artifacts(history, monkeypatch):
    h=history
    receipt=make_plan(h)
    before=inventory(h["root"])
    files={str(p):p.read_bytes() for p in (h["root"]/"transcripts").rglob("*.md")}
    restore=migration.restore
    calls=0
    def fail_once(*args):
        nonlocal calls
        calls+=1
        result = restore(*args)
        if calls==1:
            raise OSError("injected publication failure")
        return result
    monkeypatch.setattr(migration,"restore",fail_once)
    with pytest.raises(OSError,match="injected"):
        migration.apply(receipt["plan"],receipt["digest"])
    assert inventory(h["root"])==before
    assert {str(p):p.read_bytes() for p in (h["root"]/"transcripts").rglob("*.md")}==files
    assert migration.apply(receipt["plan"],receipt["digest"])["status"]=="applied"


def test_interrupted_publication_recovers_only_known_partial_files(history):
    h=history
    receipt=make_plan(h)
    group=json.loads(Path(receipt["plan"]).read_text())["manifest"]["groups"][0]
    before=inventory(h["root"])
    files = artifacts(h)
    raws = raw_files(h)
    rel=next(iter(group["publications"]))
    target = h["root"] / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"Unknown interrupted artifact contents\n")
    migration.save_json(h["output"]/"journal.json",{"status":"applying","direction":"applied"})
    unknown_files = artifacts(h)
    journal = (h["output"] / "journal.json").read_bytes()
    with pytest.raises(ValueError):
        migration.apply(receipt["plan"],receipt["digest"],rollback=True)
    assert inventory(h["root"]) == before
    assert artifacts(h) == unknown_files
    assert raw_files(h) == raws
    assert (h["output"] / "journal.json").read_bytes() == journal
    migration.publish_files(h["root"],h["output"]/"after",{rel:group["publications"][rel]})
    assert migration.apply(receipt["plan"],receipt["digest"],rollback=True)["status"]=="rolled_back"
    assert inventory(h["root"])==before
    assert artifacts(h) == files
    assert raw_files(h) == raws
    migration.check_files(h["root"],group["originals"])


def test_wrong_approval_refuses_without_mutation(history):
    h = history
    receipt = make_plan(h)
    assert receipt["groups"] == 1
    before, files, raws = inventory(h["root"]), artifacts(h), raw_files(h)
    with pytest.raises(ValueError):
        migration.apply(receipt["plan"], "0" * 64)
    assert inventory(h["root"]) == before
    assert artifacts(h) == files
    assert raw_files(h) == raws
    assert not (h["output"] / "journal.json").exists()
    assert not (h["output"] / "backup.db").exists()


def test_tampered_manifest_with_old_digest_refuses_without_mutation(history):
    h = history
    receipt = make_plan(h)
    assert receipt["groups"] == 1
    plan_path = Path(receipt["plan"])
    plan = json.loads(plan_path.read_text())
    plan["manifest"]["groups"][0]["after"]["sessions"][0]["summary"] = "Unapproved summary"
    migration.save_json(plan_path, plan)
    before, files, raws = inventory(h["root"]), artifacts(h), raw_files(h)
    with pytest.raises(ValueError):
        migration.apply(receipt["plan"], receipt["digest"])
    assert inventory(h["root"]) == before
    assert artifacts(h) == files
    assert raw_files(h) == raws
    assert not (h["output"] / "journal.json").exists()
    assert not (h["output"] / "backup.db").exists()


def test_chain_without_workflow_marker_is_ineligible_without_mutation(history):
    h = history
    # Preserve the full copied tree in B and C, changing only the transfer marker.
    for path in h["paths"][1:]:
        entries = migration.read_raw(path)
        next(e for e in entries if e["id"] == "handoff")["name"] = "Unmarked continuation"
        path.write_text("".join(json.dumps(e) + "\n" for e in entries))
    before, files, raws = inventory(h["root"]), artifacts(h), raw_files(h)
    receipt = make_plan(h)
    assert receipt["groups"] == 0
    plan = json.loads(Path(receipt["plan"]).read_text())
    assert any(e["reason"] == "no worktree workflow marker" for e in plan["manifest"]["excluded"])
    assert inventory(h["root"]) == before
    assert artifacts(h) == files
    assert raw_files(h) == raws


def test_mismatched_visibility_is_ineligible_without_mutation(history):
    h = history
    with migration.storage(h["root"]):
        conn = db.get_connection()
        conn.execute("UPDATE sessions SET hidden_from_recents=0 WHERE session_id=?", (h["ids"][1],))
        conn.commit(); conn.close()
    before, files, raws = inventory(h["root"]), artifacts(h), raw_files(h)
    receipt = make_plan(h)
    assert receipt["groups"] == 0
    assert receipt["excluded_reasons"]["visibility differs"] == 1
    assert inventory(h["root"]) == before
    assert artifacts(h) == files
    assert raw_files(h) == raws


def test_rollback_refuses_intervening_affected_artifact_change(history):
    h = history
    receipt = make_plan(h)
    assert migration.apply(receipt["plan"], receipt["digest"])["status"] == "applied"
    (h["root"] / "transcripts" / f"{h['ids'][0]}.md").write_bytes(b"Intervening generated content\n")
    before, files, raws = inventory(h["root"]), artifacts(h), raw_files(h)
    journal = (h["output"] / "journal.json").read_bytes()
    with pytest.raises(ValueError):
        migration.apply(receipt["plan"], receipt["digest"], rollback=True)
    assert inventory(h["root"]) == before
    assert artifacts(h) == files
    assert raw_files(h) == raws
    assert (h["output"] / "journal.json").read_bytes() == journal
