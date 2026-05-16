"""Tests for Pi extension current-session env wiring helpers."""

import os
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def _run_node(script: str) -> None:
    env = os.environ.copy()
    env.pop("NODE_OPTIONS", None)
    result = subprocess.run(
        ["node", "--experimental-strip-types", "--input-type=module", "-e", script],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        env=env,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_build_session_index_env_exports_pi_contract_with_leaf():
    _run_node(
        r'''
        import assert from "node:assert/strict";
        import { buildSessionIndexEnv } from "./pi-extension/session-index-env.ts";

        const env = buildSessionIndexEnv({
          getSessionFile: () => "/tmp/pi-session.jsonl",
          getSessionId: () => "pi:019pi-session",
          getLeafId: () => "leaf-123",
        });

        assert.deepEqual(env, {
          SESSION_INDEX_SESSION_ID: "pi:019pi-session",
          SESSION_INDEX_NATIVE_SESSION_ID: "019pi-session",
          SESSION_INDEX_SOURCE: "pi",
          SESSION_INDEX_SOURCE_PATH: "/tmp/pi-session.jsonl",
          SESSION_INDEX_LEAF_ID: "leaf-123",
        });
        '''
    )


def test_build_session_index_env_omits_leaf_and_rejects_insufficient_runtime_identity():
    _run_node(
        r'''
        import assert from "node:assert/strict";
        import { buildSessionIndexEnv } from "./pi-extension/session-index-env.ts";

        assert.deepEqual(buildSessionIndexEnv({
          getSessionFile: () => "/tmp/pi-session.jsonl",
          getSessionId: () => "019pi-session",
          getLeafId: () => "   ",
        }), {
          SESSION_INDEX_SESSION_ID: "pi:019pi-session",
          SESSION_INDEX_NATIVE_SESSION_ID: "019pi-session",
          SESSION_INDEX_SOURCE: "pi",
          SESSION_INDEX_SOURCE_PATH: "/tmp/pi-session.jsonl",
        });

        assert.equal(buildSessionIndexEnv({
          getSessionFile: () => "ephemeral",
          getSessionId: () => "019pi-session",
        }), undefined);
        assert.equal(buildSessionIndexEnv({
          getSessionFile: () => "/tmp/pi-session.jsonl",
          getSessionId: () => "",
        }), undefined);
        '''
    )


def test_apply_and_overlay_session_index_env_clear_stale_values():
    _run_node(
        r'''
        import assert from "node:assert/strict";
        import { applySessionIndexEnv, buildSessionIndexEnv, overlaySessionIndexEnv } from "./pi-extension/session-index-env.ts";

        const target = {
          SESSION_INDEX_SESSION_ID: "pi:old",
          SESSION_INDEX_NATIVE_SESSION_ID: "old",
          SESSION_INDEX_SOURCE: "pi",
          SESSION_INDEX_SOURCE_PATH: "/tmp/old.jsonl",
          SESSION_INDEX_LEAF_ID: "old-leaf",
          KEEP_ME: "yes",
        };
        applySessionIndexEnv(target, undefined);
        assert.deepEqual(target, { KEEP_ME: "yes" });

        const sessionEnv = buildSessionIndexEnv({
          getSessionFile: () => "/tmp/new.jsonl",
          getSessionId: () => "new",
          getLeafId: () => "new-leaf",
        });
        applySessionIndexEnv(target, sessionEnv);
        assert.equal(target.SESSION_INDEX_SESSION_ID, "pi:new");
        assert.equal(target.SESSION_INDEX_NATIVE_SESSION_ID, "new");
        assert.equal(target.SESSION_INDEX_SOURCE_PATH, "/tmp/new.jsonl");
        assert.equal(target.SESSION_INDEX_LEAF_ID, "new-leaf");
        assert.equal(target.KEEP_ME, "yes");

        const overlaid = overlaySessionIndexEnv({
          SESSION_INDEX_SESSION_ID: "pi:old",
          SESSION_INDEX_NATIVE_SESSION_ID: "old",
          SESSION_INDEX_SOURCE: "pi",
          SESSION_INDEX_SOURCE_PATH: "/tmp/old.jsonl",
          KEEP_ME: "yes",
        }, undefined);
        assert.deepEqual(overlaid, { KEEP_ME: "yes" });
        '''
    )
