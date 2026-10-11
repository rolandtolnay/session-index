"""Regression cases found while reviewing the one-time migration boundary."""
import json
from pathlib import Path

import pytest

import consolidate_pi as migration
import db
import indexer
from test_consolidate_pi import history, inventory, make_plan
from test_pi_continuations import exchange


@pytest.mark.parametrize("boundary", ["checked-artifacts", "published-files"])
def test_raw_append_during_application_cannot_publish_stale_rehearsal(history, monkeypatch, boundary):
    h=history
    receipt=make_plan(h)
    before=inventory(h["root"])
    changed=False
    function="check_files" if boundary=="checked-artifacts" else "publish_files"
    original=getattr(migration,function)
    def append_once(root,*args,**kwargs):
        nonlocal changed
        result=original(root,*args,**kwargs)
        if Path(root)==h["root"] and not changed:
            changed=True
            with h["paths"][-1].open("a") as handle:
                for e in exchange("late-arrival","returned"):
                    handle.write(json.dumps(e)+"\n")
        return result
    monkeypatch.setattr(migration,function,append_once)
    with pytest.raises(ValueError,match="[Rr]aw inputs"):
        migration.apply(receipt["plan"],receipt["digest"])
    assert inventory(h["root"])==before
    group=json.loads(Path(receipt["plan"]).read_text())["manifest"]["groups"][0]
    migration.check_files(h["root"],group["originals"])
    assert "late-arrival" in h["paths"][-1].read_text()


def test_rehearsal_output_cannot_enter_live_storage_through_symlink(history, tmp_path):
    h=history
    link=tmp_path/"index-alias"
    link.symlink_to(h["root"],target_is_directory=True)
    with pytest.raises(ValueError,match="outside"):
        migration.plan(h["root"],h["raw_root"],link/"rehearsal",idle_seconds=0)
    assert not (h["root"]/"rehearsal").exists()


def test_apply_does_not_upgrade_unrelated_legacy_metadata(history):
    h=history
    receipt=make_plan(h)
    with migration.storage(h["root"]):
        conn=db.get_connection()
        conn.execute("INSERT INTO sessions (session_id,source,native_session_id,identity_key) VALUES ('unrelated',NULL,'old','old')")
        conn.commit();conn.close()
    assert migration.apply(receipt["plan"],receipt["digest"])["status"]=="applied"
    assert migration.apply(receipt["plan"],receipt["digest"],rollback=True)["status"]=="rolled_back"
    with db._get_readonly_connection(str(h["root"]/"sessions.db")) as conn:
        assert conn.execute("SELECT source FROM sessions WHERE session_id='unrelated'").fetchone()[0] is None


def test_publication_relocates_navigation_not_literal_evidence(history):
    h=history
    literal=str(h["output"]/"after/transcripts")+"/quoted-path.md"
    for p in h["paths"]:
        entries=migration.read_raw(p)
        entries[1]["message"]["content"] += " Literal: " + literal
        p.write_text("".join(json.dumps(e)+"\n" for e in entries))
    with migration.storage(h["root"]):
        for p in h["paths"][:2]:
            indexer.index_source_transcript("pi",str(p),indexer.NO_SUMMARY_INDEX_OPTIONS)
        conn=db.get_connection()
        conn.execute("UPDATE sessions SET summary=? WHERE session_id=?",(literal,h["ids"][1]))
        conn.commit();conn.close()
    receipt=make_plan(h)
    assert receipt["groups"]==1
    group=json.loads(Path(receipt["plan"]).read_text())["manifest"]["groups"][0]
    assert group["after"]["sessions"][0]["summary"]==literal
    staged=h["output"]/"after/transcripts"/f"{h['ids'][0]}.md"
    assert literal in staged.read_text()
