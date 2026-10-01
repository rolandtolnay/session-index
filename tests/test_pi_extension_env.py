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


def test_side_chat_notifications_index_captured_parent_without_triggering_main_turn():
    _run_node(r'''
        import assert from "node:assert/strict";
        import { EventEmitter } from "node:events";
        import { createSessionIndexExtension } from "./pi-extension/index.ts";
        const events = new EventEmitter();
        const spawns = [];
        const pi = { events, registerCommand() {}, on() {} };
        const register = createSessionIndexExtension({spawnProcess: (...args) => {
          const child = new EventEmitter(); child.unref = () => {};
          spawns.push(args);
          return child;
        }});
        register(pi);
        process.env.SESSION_INDEX_SOURCE_PATH = "/tmp/wrong-active.jsonl";
        process.env.SESSION_INDEX_NATIVE_SESSION_ID = "wrong-active";
        events.emit("side-chat:archived", {parentSessionId: "captured-parent", parentSessionFile: "/tmp/parent.jsonl", closed: false});
        events.emit("side-chat:archived", {parentSessionId: "captured-parent", parentSessionFile: "/tmp/parent.jsonl", closed: true});
        events.emit("side-chat:archived", {parentSessionId: "x", parentSessionFile: "relative.jsonl", closed: true});
        assert.equal(spawns.length, 2);
        assert.equal(spawns[0][1][3], "side-chat");
        assert.equal(spawns[1][1][3], "side-chat-close");
        for (const [cmd, args, options] of spawns) {
          assert.equal(args.at(-1), "/tmp/parent.jsonl");
          assert.equal(options.env.SESSION_INDEX_NATIVE_SESSION_ID, "captured-parent");
          assert.equal(options.env.SESSION_INDEX_SOURCE_PATH, "/tmp/parent.jsonl");
          assert.equal(options.detached, true);
        }
    ''')


def test_build_session_index_env_exports_pi_contract_with_leaf():
    _run_node(
        r'''
        import assert from "node:assert/strict";
        import { buildSessionIndexEnv } from "./pi-extension/session-index-env.ts";

        const env = buildSessionIndexEnv({
          getSessionFile: () => "/tmp/pi-session.jsonl",
          getSessionId: () => "019pi-session",
          getLeafId: () => "leaf-123",
        });

        assert.deepEqual(env, {
          SESSION_INDEX_SESSION_ID: "pi:4f4d748a63162aa9",
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
          SESSION_INDEX_SESSION_ID: "pi:4f4d748a63162aa9",
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
        assert.equal(buildSessionIndexEnv({
          getSessionFile: () => "/tmp/pi-session.jsonl",
          getSessionId: () => "pi:019pi-session",
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
        assert.equal(target.SESSION_INDEX_SESSION_ID, "pi:1e0592193c456fa9");
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


def test_direct_user_shell_refreshes_selected_leaf_without_indexing_or_handling_command():
    _run_node(r'''
        import assert from "node:assert/strict";
        import { createSessionIndexExtension } from "./pi-extension/index.ts";
        const handlers = new Map();
        createSessionIndexExtension({spawnProcess: () => { throw new Error("No indexing at shell dispatch"); }})({
          registerCommand() {}, on: (name, handler) => handlers.set(name, handler),
        });
        let leaf = "before-navigation";
        const ctx = {sessionManager: {
          getSessionFile: () => "/tmp/source.jsonl",
          getSessionId: () => "origin",
          getLeafId: () => leaf,
        }};
        await handlers.get("session_start")({}, ctx);
        leaf = "selected-after-navigation";
        const result = await handlers.get("user_bash")({command: "picommit"}, ctx);
        assert.equal(process.env.SESSION_INDEX_LEAF_ID, leaf);
        assert.equal(process.env.SESSION_INDEX_NATIVE_SESSION_ID, "origin");
        assert.equal(result, undefined, "identity refresh must preserve normal shell handling");
    ''')
