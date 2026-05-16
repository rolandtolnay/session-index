import type { ExtensionAPI } from "@mariozechner/pi-coding-agent";
import { spawn } from "node:child_process";
import { realpathSync } from "node:fs";
import path from "node:path";
import {
	applySessionIndexEnv,
	buildSessionIndexEnv,
	overlaySessionIndexEnv,
	type SessionIndexEnv,
} from "./session-index-env.ts";

const extensionDir = realpathSync(__dirname);
const repoRoot = path.resolve(extensionDir, "..");
const piIndexScript = path.join(repoRoot, "hooks", "pi_index.py");
const piContextScript = path.join(repoRoot, "hooks", "pi_context.py");

function refreshSessionIndexEnv(sessionManager: Parameters<typeof buildSessionIndexEnv>[0]) {
	const sessionEnv = buildSessionIndexEnv(sessionManager);
	applySessionIndexEnv(process.env, sessionEnv);
	return sessionEnv;
}

function spawnIndexer(mode: "fast" | "full", sessionFile: string, sessionEnv: SessionIndexEnv | undefined) {
	const childEnv = overlaySessionIndexEnv(process.env, sessionEnv);
	childEnv.SESSION_INDEX_PROVIDER = "pi";

	const child = spawn(
		"uv",
		["run", piIndexScript, "--mode", mode, "--session-file", sessionFile],
		{
			cwd: repoRoot,
			detached: true,
			stdio: "ignore",
			env: childEnv,
		},
	);
	child.unref();
}

export default function (pi: ExtensionAPI) {
	let injectedForSession: string | undefined;
	let lastFastIndexKey: string | undefined;

	pi.on("session_start", async (_event, ctx) => {
		injectedForSession = undefined;
		lastFastIndexKey = undefined;
		refreshSessionIndexEnv(ctx.sessionManager);
	});

	pi.on("before_agent_start", async (event, ctx) => {
		const sessionEnv = refreshSessionIndexEnv(ctx.sessionManager);
		const sessionFile = sessionEnv?.SESSION_INDEX_SOURCE_PATH ?? ctx.sessionManager.getSessionFile?.() ?? "ephemeral";
		if (injectedForSession === sessionFile) return;
		injectedForSession = sessionFile;

		let result: { code: number; stdout: string };
		try {
			result = await pi.exec(
				"uv",
				[
					"run",
					piContextScript,
					"--cwd",
					ctx.cwd,
					"--session-id",
					sessionEnv?.SESSION_INDEX_NATIVE_SESSION_ID ?? ctx.sessionManager.getSessionId?.() ?? "",
				],
				{ cwd: repoRoot, timeout: 3000 },
			);
		} catch {
			return;
		}

		if (result.code !== 0 || !result.stdout.trim()) return;

		return {
			systemPrompt: `${event.systemPrompt}\n\n${result.stdout.trim()}\n\nUse this recent-session index as lightweight continuity context. For older or specific past work, load the session-search skill and query the index.`,
		};
	});

	pi.on("agent_end", async (_event, ctx) => {
		const sessionEnv = refreshSessionIndexEnv(ctx.sessionManager);
		const sessionFile = sessionEnv?.SESSION_INDEX_SOURCE_PATH ?? ctx.sessionManager.getSessionFile?.();
		if (!sessionFile) return;
		const leaf = sessionEnv?.SESSION_INDEX_LEAF_ID ?? ctx.sessionManager.getLeafId?.() ?? "";
		const key = `${sessionFile}:${leaf}`;
		if (key === lastFastIndexKey) return;
		lastFastIndexKey = key;
		spawnIndexer("fast", sessionFile, sessionEnv);
	});

	pi.on("session_shutdown", async (event, ctx) => {
		if (event.reason === "reload") return;
		const sessionEnv = refreshSessionIndexEnv(ctx.sessionManager);
		const sessionFile = sessionEnv?.SESSION_INDEX_SOURCE_PATH ?? ctx.sessionManager.getSessionFile?.();
		if (!sessionFile) return;
		spawnIndexer("full", sessionFile, sessionEnv);
	});
}
