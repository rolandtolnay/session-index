"""Tests for the Pi extension Current Session Display formatter."""

import os
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]

NODE_HELPERS = r'''
function currentSessionMetadata(overrides = {}) {
  return {
    session_id: "pi:019session",
    native_session_id: "019session",
    source: "pi",
    source_path: "/absolute/source.jsonl",
    transcript_path: "/absolute/pi:019session.md",
    tool_log_path: "/absolute/pi:019session.tools.md",
    source_path_exists: true,
    transcript_exists: true,
    tool_log_exists: false,
    resolution_method: "session_index_env",
    ...overrides,
  };
}

function fakeChildProcess(EventEmitter) {
  const child = new EventEmitter();
  child.stdout = new EventEmitter();
  child.stderr = new EventEmitter();
  child.stdout.setEncoding = () => {};
  child.stderr.setEncoding = () => {};
  child.unref = () => {};
  child.kill = () => {
    child.killed = true;
    child.killCalls = (child.killCalls ?? 0) + 1;
  };
  return child;
}

const flushTimers = () => new Promise((resolve) => setTimeout(resolve, 0));
'''


def _run_node(script: str) -> None:
    env = os.environ.copy()
    env.pop("NODE_OPTIONS", None)
    result = subprocess.run(
        ["node", "--experimental-strip-types", "--input-type=module", "-e", f"{NODE_HELPERS}\n{script}"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        env=env,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_current_session_display_formatter_renders_v1_fields_in_stable_order():
    _run_node(
        r'''
        import assert from "node:assert/strict";
        import { formatCurrentSessionDisplay } from "./pi-extension/current-session-display.ts";

        const lines = formatCurrentSessionDisplay({
          metadata: currentSessionMetadata({
            leaf_id: "leaf-out-of-scope",
            cwd: "/do/not/show",
          }),
        });

        assert.deepEqual(lines, [
          "Current Session",
          "Canonical Session ID: pi:019session",
          "Native Session ID: 019session",
          "Clean Transcript: /absolute/pi:019session.md [exists]",
          "Tool Log: /absolute/pi:019session.tools.md [missing]",
          "Source Transcript: /absolute/source.jsonl [exists]",
          "Ctrl+R index current snapshot · Enter/Esc/q close",
        ]);
        assert.equal(lines.join("\n").includes("leaf-out-of-scope"), false);
        assert.equal(lines.join("\n").includes("/do/not/show"), false);
        assert.equal(lines.join("\n").includes("session_index_env"), false);
        '''
    )


def test_current_session_display_formatter_marks_missing_and_preserves_absolute_paths():
    _run_node(
        r'''
        import assert from "node:assert/strict";
        import { formatCurrentSessionDisplay } from "./pi-extension/current-session-display.ts";

        const lines = formatCurrentSessionDisplay({
          metadata: currentSessionMetadata({
            session_id: "pi:missing",
            native_session_id: "missing",
            source_path: "/Users/roland/.pi/agent/sessions/source.jsonl",
            transcript_path: "/Users/roland/.session-index/transcripts/pi:missing.md",
            tool_log_path: "/Users/roland/.session-index/transcripts/pi:missing.tools.md",
            source_path_exists: false,
            transcript_exists: false,
            tool_log_exists: true,
          }),
        });

        assert.deepEqual(lines.slice(1, 6), [
          "Canonical Session ID: pi:missing",
          "Native Session ID: missing",
          "Clean Transcript: /Users/roland/.session-index/transcripts/pi:missing.md [missing]",
          "Tool Log: /Users/roland/.session-index/transcripts/pi:missing.tools.md [exists]",
          "Source Transcript: /Users/roland/.pi/agent/sessions/source.jsonl [missing]",
        ]);
        '''
    )


def test_current_session_display_formatter_shows_generated_artifact_times_only_on_generated_rows():
    _run_node(
        r'''
        import assert from "node:assert/strict";
        import { formatCurrentSessionDisplay } from "./pi-extension/current-session-display.ts";

        const lines = formatCurrentSessionDisplay({
          metadata: currentSessionMetadata({
            session_id: "pi:timed",
            native_session_id: "timed",
            transcript_path: "/absolute/pi:timed.md",
            tool_log_path: "/absolute/pi:timed.tools.md",
            tool_log_exists: true,
            transcript_written_at: "2026-05-23T15:42:10.123456+00:00",
            tool_log_written_at: "2026-05-23T15:43:10.123456+00:00",
            source_written_at: "SHOULD_NOT_RENDER",
          }),
        });

        assert.match(lines[3], /Clean Transcript: \/absolute\/pi:timed\.md \[exists\] · written /);
        assert.match(lines[4], /Tool Log: \/absolute\/pi:timed\.tools\.md \[exists\] · written /);
        assert.equal(lines[5], "Source Transcript: /absolute/source.jsonl [exists]");
        assert.equal(lines.join("\n").includes("SHOULD_NOT_RENDER"), false);
        '''
    )


def test_current_session_display_formatter_renders_readable_error_without_fallback_language():
    _run_node(
        r'''
        import assert from "node:assert/strict";
        import { formatCurrentSessionDisplay } from "./pi-extension/current-session-display.ts";

        const lines = formatCurrentSessionDisplay({ error: "missing required env: SESSION_INDEX_SESSION_ID" });

        assert.deepEqual(lines, [
          "Current Session",
          "Unable to resolve Current Session metadata.",
          "missing required env: SESSION_INDEX_SESSION_ID",
          "Enter/Esc/q close",
        ]);
        assert.equal(/fallback|guess/i.test(lines.join("\n")), false);
        '''
    )


def test_current_session_display_component_wraps_lines_to_render_width_and_suppresses_footer():
    _run_node(
        r'''
        import assert from "node:assert/strict";
        import { showCurrentSessionDisplay } from "./pi-extension/current-session-display.ts";

        const stateKey = Symbol.for("pi.focused-ui-footer.state");
        globalThis[stateKey] = { suppressionCount: 0, listeners: new Set() };

        let rendered;
        let suppressedDuringDisplay;
        await showCurrentSessionDisplay({
          ctx: {
            ui: {
              custom: async (factory) => {
                suppressedDuringDisplay = globalThis[stateKey].suppressionCount > 0;
                const component = factory({ requestRender: () => {} }, {}, undefined, () => {});
                rendered = component.render(40);
              },
            },
          },
          content: {
            metadata: currentSessionMetadata({
              source_path: "/Users/rolandtolnay/.pi/agent/sessions/--very-long-path/source.jsonl",
              transcript_path: "/Users/rolandtolnay/.session-index/transcripts/pi:019session.md",
              tool_log_path: "/Users/rolandtolnay/.session-index/transcripts/pi:019session.tools.md",
            }),
          },
        });

        assert.equal(suppressedDuringDisplay, true);
        assert.equal(globalThis[stateKey].suppressionCount, 0);
        assert.ok(rendered.length > 0);
        assert.equal(rendered.at(-1), "");
        assert.ok(rendered.every((line) => line.length <= 40), rendered.join("\n"));
        assert.equal(rendered[2], "");
        assert.ok(rendered.some((line) => line === "  Canonical Session ID"), rendered.join("\n"));
        assert.ok(rendered.findIndex((line) => line === "  Canonical Session ID") < rendered.findIndex((line) => line === "  Clean Transcript [exists]"), rendered.join("\n"));
        assert.ok(rendered.some((line) => line === "  Clean Transcript [exists]"), rendered.join("\n"));
        assert.ok(rendered.some((line) => line.includes(".session-index")), rendered.join("\n"));
        const noteIndex = rendered.findIndex((line) => line.includes("User-only display"));
        const hintIndex = rendered.findIndex((line) => line.includes("Ctrl+R index current snapshot"));
        assert.ok(noteIndex > 0, rendered.join("\n"));
        assert.equal(rendered[noteIndex - 1], "");
        assert.equal(rendered[hintIndex - 1], "");
        '''
    )


def test_current_session_display_ctrl_r_runs_single_index_action_and_refreshes_when_completed():
    _run_node(
        r'''
        import assert from "node:assert/strict";
        import { showCurrentSessionDisplay } from "./pi-extension/current-session-display.ts";

        let resolveIndex;
        let calls = 0;
        let component;
        const renders = [];
        await showCurrentSessionDisplay({
          ctx: {
            ui: {
              custom: async (factory) => {
                component = factory({ requestRender: () => renders.push(component.render(120)) }, {}, undefined, () => {});
                component.handleInput("\x1b[114;5u");
                assert.equal(calls, 1);
                const runningRender = component.render(120).join("\n");
                assert.ok(runningRender.includes("Indexing current snapshot"), runningRender);
                assert.equal(runningRender.includes("User-only display"), false, runningRender);
                assert.equal(runningRender.includes("Ctrl+R index current snapshot"), false, runningRender);
                assert.ok(runningRender.includes("Enter/Esc/q close"), runningRender);
                component.handleInput("\x12");
                assert.equal(calls, 1);
                resolveIndex({
                  status: "completed",
                  completedAt: "2026-05-23T15:45:10.000Z",
                  content: {
                    metadata: currentSessionMetadata({
                      tool_log_exists: true,
                    }),
                  },
                });
                await flushTimers();
              },
            },
          },
          content: {
            metadata: currentSessionMetadata({
              transcript_exists: false,
            }),
          },
          onIndexSnapshot: async () => {
            calls++;
            return await new Promise((resolve) => { resolveIndex = resolve; });
          },
        });

        const finalRender = component.render(120).join("\n");
        assert.ok(finalRender.includes("Indexed snapshot at "), finalRender);
        assert.ok(finalRender.includes("Clean Transcript [exists]"), finalRender);
        assert.ok(finalRender.includes("Tool Log [exists]"), finalRender);
        '''
    )


def test_current_session_display_generated_artifact_written_time_uses_dim_color():
    _run_node(
        r'''
        import assert from "node:assert/strict";
        import { showCurrentSessionDisplay } from "./pi-extension/current-session-display.ts";

        let rendered;
        await showCurrentSessionDisplay({
          ctx: {
            ui: {
              custom: async (factory) => {
                const component = factory(
                  { requestRender: () => {} },
                  { fg: (name, text) => `<${name}>${text}</${name}>` },
                  undefined,
                  () => {},
                );
                rendered = component.render(160).join("\n");
              },
            },
          },
          content: {
            metadata: currentSessionMetadata({
              transcript_written_at: "2026-05-23T15:42:10.000Z",
            }),
          },
        });

        assert.ok(rendered.includes("<dim> · written "), rendered);
        '''
    )


def test_current_session_display_completion_message_uses_success_color():
    _run_node(
        r'''
        import assert from "node:assert/strict";
        import { showCurrentSessionDisplay } from "./pi-extension/current-session-display.ts";

        let component;
        await showCurrentSessionDisplay({
          ctx: {
            ui: {
              custom: async (factory) => {
                component = factory(
                  { requestRender: () => {} },
                  { fg: (name, text) => `<${name}>${text}</${name}>` },
                  undefined,
                  () => {},
                );
                component.handleInput("\x12");
                await flushTimers();
              },
            },
          },
          content: {
            metadata: currentSessionMetadata({ transcript_exists: false }),
          },
          onIndexSnapshot: async () => ({
            status: "completed",
            completedAt: "2026-05-23T15:45:10.000Z",
            content: { metadata: currentSessionMetadata() },
          }),
        });

        const rendered = component.render(120).join("\n");
        assert.ok(rendered.includes("<success>Indexed snapshot at "), rendered);
        '''
    )


def test_current_session_display_dismissal_keys_close_without_blocking_started_indexing():
    _run_node(
        r'''
        import assert from "node:assert/strict";
        import { showCurrentSessionDisplay } from "./pi-extension/current-session-display.ts";

        let closed = false;
        let calls = 0;
        await showCurrentSessionDisplay({
          ctx: {
            ui: {
              custom: async (factory) => {
                const component = factory({ requestRender: () => {} }, {}, undefined, () => { closed = true; });
                component.handleInput("\x12");
                component.handleInput("q");
                assert.equal(closed, true);
                assert.equal(calls, 1);
              },
            },
          },
          content: {
            metadata: currentSessionMetadata({
              transcript_exists: false,
            }),
          },
          onIndexSnapshot: async () => {
            calls++;
            return await new Promise(() => {});
          },
        });
        '''
    )


def test_current_session_display_timeout_keeps_refreshed_artifact_statuses_truthful():
    _run_node(
        r'''
        import assert from "node:assert/strict";
        import { showCurrentSessionDisplay } from "./pi-extension/current-session-display.ts";

        let component;
        let calls = 0;
        await showCurrentSessionDisplay({
          ctx: {
            ui: {
              custom: async (factory) => {
                component = factory({ requestRender: () => {} }, {}, undefined, () => {});
                component.handleInput("\x12");
                await flushTimers();
                component.handleInput("\x12");
                assert.equal(calls, 1);
              },
            },
          },
          content: {
            metadata: currentSessionMetadata({
              transcript_exists: false,
            }),
          },
          onIndexSnapshot: async () => {
            calls++;
            return {
              status: "timeout",
              content: {
                metadata: currentSessionMetadata(),
              },
            };
          },
        });

        const rendered = component.render(120).join("\n");
        assert.ok(rendered.includes("Indexing is still running"), rendered);
        assert.ok(rendered.includes("Clean Transcript [exists]"), rendered);
        assert.ok(rendered.includes("Tool Log [missing]"), rendered);
        '''
    )


def test_current_session_command_manual_indexing_uses_refreshed_pi_env_and_full_index_then_refreshes_display():
    _run_node(
        r'''
        import assert from "node:assert/strict";
        import { EventEmitter } from "node:events";
        import { createSessionIndexExtension } from "./pi-extension/index.ts";

        const childProcess = () => fakeChildProcess(EventEmitter);

        const initial = currentSessionMetadata({
          session_id: "pi:initial",
          native_session_id: "initial",
          source_path: "/tmp/initial.jsonl",
          transcript_path: "/tmp/pi:initial.md",
          tool_log_path: "/tmp/pi:initial.tools.md",
          transcript_exists: false,
        });
        const refreshed = currentSessionMetadata({
          session_id: "pi:fresh",
          native_session_id: "fresh",
          source_path: "/tmp/fresh.jsonl",
          transcript_path: "/tmp/pi:fresh.md",
          tool_log_path: "/tmp/pi:fresh.tools.md",
          tool_log_exists: true,
          transcript_written_at: "2026-05-23T15:42:10.000Z",
          tool_log_written_at: "2026-05-23T15:43:10.000Z",
        });
        const currentResponses = [initial, refreshed];
        let indexSpawn;
        const currentSpawnEnvs = [];
        const spawnProcess = (_command, args, options) => {
          const child = childProcess();
          if (args.includes("cli.py")) {
            currentSpawnEnvs.push(options.env);
            const response = currentResponses.shift();
            queueMicrotask(() => {
              child.stdout.emit("data", JSON.stringify(response));
              child.emit("close", 0);
            });
            return child;
          }
          indexSpawn = { args, options, child };
          return child;
        };

        const commands = new Map();
        createSessionIndexExtension({ spawnProcess, manualIndexTimeoutMs: 1000 })({
          registerCommand: (name, options) => commands.set(name, options),
          on: () => {},
          exec: async () => ({ code: 0, stdout: "" }),
        });

        const command = commands.get("current-session");
        let sessionIdCalls = 0;
        let sessionFileCalls = 0;
        let finalRender = "";
        await command.handler("", {
          sessionManager: {
            getSessionFile: () => sessionFileCalls++ === 0 ? "/tmp/initial.jsonl" : "/tmp/fresh.jsonl",
            getSessionId: () => sessionIdCalls++ === 0 ? "initial" : "fresh",
            getLeafId: () => "leaf-1",
          },
          ui: {
            custom: async (factory) => {
              const component = factory({ requestRender: () => {} }, {}, undefined, () => {});
              assert.ok(component.render(120).join("\n").includes("Clean Transcript [missing]"));
              component.handleInput("\x12");
              await flushTimers();
              assert.ok(indexSpawn, "manual indexing should spawn an indexer");
              indexSpawn.child.emit("close", 0);
              await flushTimers();
              await flushTimers();
              finalRender = component.render(120).join("\n");
            },
          },
        });

        assert.deepEqual(indexSpawn.args, ["run", indexSpawn.args[1], "--mode", "full", "--session-file", "/tmp/fresh.jsonl"]);
        assert.ok(indexSpawn.args[1].endsWith("/hooks/pi_index.py"));
        assert.equal(indexSpawn.options.env.SESSION_INDEX_PROVIDER, "pi");
        assert.equal(indexSpawn.options.env.SESSION_INDEX_SESSION_ID, "pi:fresh");
        assert.equal(indexSpawn.options.env.SESSION_INDEX_SOURCE_PATH, "/tmp/fresh.jsonl");
        assert.equal(currentSpawnEnvs.at(-1).SESSION_INDEX_SESSION_ID, "pi:fresh");
        assert.ok(finalRender.includes("Indexed snapshot at "), finalRender);
        assert.ok(finalRender.includes("Clean Transcript [exists]"), finalRender);
        assert.ok(finalRender.includes("Tool Log [exists]"), finalRender);
        '''
    )


def test_current_session_command_manual_indexing_timeout_does_not_kill_child():
    _run_node(
        r'''
        import assert from "node:assert/strict";
        import { EventEmitter } from "node:events";
        import { createSessionIndexExtension } from "./pi-extension/index.ts";

        const childProcess = () => fakeChildProcess(EventEmitter);

        const metadata = currentSessionMetadata({
          session_id: "pi:slow",
          native_session_id: "slow",
          source_path: "/tmp/slow.jsonl",
          transcript_path: "/tmp/pi:slow.md",
          tool_log_path: "/tmp/pi:slow.tools.md",
          transcript_exists: false,
        });
        const indexChildren = [];
        const spawnProcess = (_command, args) => {
          const child = childProcess();
          if (args.includes("cli.py")) {
            queueMicrotask(() => {
              child.stdout.emit("data", JSON.stringify(metadata));
              child.emit("close", 0);
            });
          } else {
            indexChildren.push(child);
          }
          return child;
        };

        const commands = new Map();
        createSessionIndexExtension({ spawnProcess, manualIndexTimeoutMs: 20 })({
          registerCommand: (name, options) => commands.set(name, options),
          on: () => {},
          exec: async () => ({ code: 0, stdout: "" }),
        });

        let rendered = "";
        await commands.get("current-session").handler("", {
          sessionManager: {
            getSessionFile: () => "/tmp/slow.jsonl",
            getSessionId: () => "slow",
            getLeafId: () => "leaf-1",
          },
          ui: {
            custom: async (factory) => {
              const component = factory({ requestRender: () => {} }, {}, undefined, () => {});
              component.handleInput("\x12");
              await new Promise((resolve) => setTimeout(resolve, 50));
              rendered = component.render(120).join("\n");
              component.handleInput("\x12");
              assert.equal(indexChildren.length, 1, "timeout state should block duplicate indexing before child closes");
              indexChildren[0].emit("close", 0);
              await flushTimers();
              await flushTimers();
              component.handleInput("\x12");
              await flushTimers();
            },
          },
        });

        assert.equal(indexChildren[0].killCalls ?? 0, 0);
        assert.equal(indexChildren.length, 2, "after the timed-out child closes, a later Ctrl+R may index a new snapshot");
        assert.ok(rendered.includes("Indexing is still running"), rendered);
        assert.ok(rendered.includes("Clean Transcript [missing]"), rendered);
        '''
    )


def test_current_session_command_timeout_finishes_even_when_metadata_refresh_hangs():
    _run_node(
        r'''
        import assert from "node:assert/strict";
        import { EventEmitter } from "node:events";
        import { createSessionIndexExtension } from "./pi-extension/index.ts";

        const childProcess = () => fakeChildProcess(EventEmitter);
        const metadata = currentSessionMetadata({ transcript_exists: false });
        let currentRuns = 0;
        const refreshChildren = [];
        let indexChild;
        const spawnProcess = (_command, args) => {
          const child = childProcess();
          if (args.includes("cli.py")) {
            currentRuns++;
            if (currentRuns === 1) {
              queueMicrotask(() => {
                child.stdout.emit("data", JSON.stringify(metadata));
                child.emit("close", 0);
              });
            } else {
              refreshChildren.push(child);
            }
          } else {
            indexChild = child;
          }
          return child;
        };

        const commands = new Map();
        createSessionIndexExtension({
          spawnProcess,
          manualIndexTimeoutMs: 20,
          currentSessionRefreshTimeoutMs: 20,
        })({
          registerCommand: (name, options) => commands.set(name, options),
          on: () => {},
          exec: async () => ({ code: 0, stdout: "" }),
        });

        let rendered = "";
        await commands.get("current-session").handler("", {
          sessionManager: {
            getSessionFile: () => "/tmp/hangs.jsonl",
            getSessionId: () => "hangs",
            getLeafId: () => "leaf-1",
          },
          ui: {
            custom: async (factory) => {
              const component = factory({ requestRender: () => {} }, {}, undefined, () => {});
              component.handleInput("\x12");
              await new Promise((resolve) => setTimeout(resolve, 80));
              rendered = component.render(120).join("\n");
            },
          },
        });

        assert.ok(indexChild, "manual indexer should still be running");
        assert.equal(indexChild.killCalls ?? 0, 0, "UI timeout must not kill the indexer");
        assert.equal(refreshChildren[0].killCalls, 1, "hung metadata refresh should be timed out and killed");
        assert.ok(rendered.includes("Indexing is still running"), rendered);
        assert.equal(rendered.includes("Indexing current snapshot"), false, rendered);
        '''
    )


def test_current_session_command_registers_and_fails_missing_pi_identity_without_fallback():
    _run_node(
        r'''
        import assert from "node:assert/strict";
        import registerSessionIndexExtension from "./pi-extension/index.ts";

        const commands = new Map();
        registerSessionIndexExtension({
          registerCommand: (name, options) => commands.set(name, options),
          on: () => {},
          exec: async () => ({ code: 0, stdout: "" }),
        });

        const command = commands.get("current-session");
        assert.ok(command, "current-session command should be registered");

        process.env.CLAUDE_SESSION_ID = "wrong-ambient-session";
        process.env.CLAUDE_TRANSCRIPT_PATH = "/tmp/wrong-ambient.jsonl";

        let rendered;
        await command.handler("", {
          sessionManager: {
            getSessionFile: () => "ephemeral",
            getSessionId: () => "",
            getLeafId: () => undefined,
          },
          ui: {
            custom: async (factory) => {
              const component = factory({ requestRender: () => {} }, {}, undefined, () => {});
              rendered = component.render(80);
            },
          },
        });

        const trimmed = rendered.map((line) => line.trim());
        assert.ok(trimmed.includes("Current Session"), rendered.join("\n"));
        assert.ok(trimmed.includes("Unable to resolve Current Session metadata."), rendered.join("\n"));
        assert.ok(trimmed.includes("Current Session metadata is unavailable: Pi runtime identity is missing."), rendered.join("\n"));
        assert.ok(trimmed.includes("Enter/Esc/q close"), rendered.join("\n"));
        assert.equal(rendered.join("\n").includes("wrong-ambient-session"), false);
        assert.equal(/fallback|guess/i.test(rendered.join("\n")), false);
        '''
    )
