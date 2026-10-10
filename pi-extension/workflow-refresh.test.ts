import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdirSync, mkdtempSync, readFileSync, realpathSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";
import { createWorkflowRefresh } from "./workflow-refresh.ts";

function fixture(t: any, mode = "ack") {
	const root = realpathSync(mkdtempSync(path.join(tmpdir(), "pi-workflow-refresh-")));
	t.after(() => rmSync(root, { recursive: true, force: true }));
	const extension = path.join(root, "repo", "pi-extension");
	mkdirSync(extension, { recursive: true });
	const link = path.join(root, "installed-extension");
	symlinkSync(extension, link);
	const receipt = path.join(root, "receipt.json");
	const script = path.join(root, "fake-hook.cjs");
	writeFileSync(script, `let input = ''; process.stdin.setEncoding('utf8'); process.stdin.on('data', c => input += c); process.stdin.on('end', () => {
		require('fs').writeFileSync(process.argv[2], JSON.stringify({ input, args: process.argv.slice(4), cwd: process.cwd(), env: Object.fromEntries(Object.entries(process.env).filter(([k]) => k.startsWith('SESSION_INDEX_'))) }));
		if (process.argv[3] === 'hang') { setInterval(() => {}, 1000); return; }
		if (process.argv[3] === 'ack') console.log(JSON.stringify({ session_id: 'pi:stable-owner', queued: true }));
		if (process.argv[3] === 'false-ack') console.log(JSON.stringify({ session_id: 'pi:stable-owner', queued: false }));
		if (process.argv[3] === 'error') { console.error('enqueue failed'); process.exitCode = 0; }
	});`);
	const notifications: string[] = [];
	const ctx = {
		sessionManager: { getSessionFile: () => path.join(root, "destination.jsonl"), getSessionId: () => "destination-native", getLeafId: () => "selected-leaf" },
		ui: { notify(message: string) { notifications.push(message); } },
	};
	const calls: any[] = [];
	const refresh = createWorkflowRefresh({ extensionDir: link, timeoutMs: mode === "hang" ? 500 : 5_000,
		spawnProcess: ((command: string, args: string[], options: any) => {
			calls.push({ command, args, options });
			// Real pipe/process lifecycle; never run the real hook or touch a DB.
			return spawn(process.execPath, [script, receipt, mode, ...args], options);
		}) as typeof spawn,
	});
	return { root, extension, ctx, refresh, calls, notifications, receipt: () => JSON.parse(readFileSync(receipt, "utf8")) };
}

test("transfer enqueue resolves the installed extension and sends the captured source header/tree with actual destination origin", async (t) => {
	const f = fixture(t);
	const sourceFile = path.join(f.root, "missing-source.jsonl");
	const entries = [{ type: "session", id: "source-native", timestamp: "2026-01-01T00:00:00Z", cwd: "/source" }, { type: "message", id: "entry", parentId: null }];
	const sessionIdentityEnv = () => Object.fromEntries(Object.entries(process.env).filter(([key]) => key.startsWith("SESSION_INDEX_")));
	const before = sessionIdentityEnv();
	await f.refresh({ ctx: f.ctx, transfer: { sourceFile, entries } });
	assert.deepEqual(f.notifications, []);
	assert.equal(f.calls[0].command, "uv");
	assert.deepEqual(f.calls[0].args, ["run", path.join(f.extension, "..", "hooks", "pi_index.py"), "--mode", "workflow", "--session-file", f.ctx.sessionManager.getSessionFile(), "--transfer-source-file", sourceFile]);
	const received = f.receipt();
	assert.deepEqual(JSON.parse(received.input), entries);
	assert.equal(received.cwd, path.dirname(f.extension));
	assert.equal(received.env.SESSION_INDEX_NATIVE_SESSION_ID, "destination-native");
	assert.equal(received.env.SESSION_INDEX_SOURCE_PATH, f.ctx.sessionManager.getSessionFile());
	assert.equal(received.env.SESSION_INDEX_LEAF_ID, "selected-leaf");
	assert.deepEqual(sessionIdentityEnv(), before, "enqueue must not change the host's exact-source identity");
});

test("in-place completion enqueues without transfer flags or a new exchange", async (t) => {
	const f = fixture(t);
	await f.refresh({ ctx: f.ctx });
	assert.deepEqual(f.notifications, []);
	assert.equal(f.calls.length, 1);
	assert.equal(f.calls[0].args.includes("--transfer-source-file"), false);
	assert.equal(f.receipt().input, "");
});

for (const mode of ["error", "false-ack", "hang"]) {
	test(`an unacknowledged ${mode} subprocess is bounded and non-fatal`, async (t) => {
		const f = fixture(t, mode);
		await f.refresh({ ctx: f.ctx });
		assert.equal(f.notifications.length, 1);
		assert.match(f.notifications[0], /not acknowledged/);
		if (mode === "hang") assert.match(f.notifications[0], /Timed out/);
	});
}

test("spawn and notification errors do not reject the workflow", async () => {
	const refresh = createWorkflowRefresh({ spawnProcess: (() => { throw new Error("no uv"); }) as typeof spawn });
	await refresh({ ctx: { sessionManager: { getSessionFile: () => "/fixture.jsonl", getSessionId: () => "native" }, ui: { notify() { throw new Error("UI unavailable"); } } } });
});
