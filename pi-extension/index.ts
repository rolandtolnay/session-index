import type { ExtensionAPI } from "@mariozechner/pi-coding-agent";
import { spawn } from "node:child_process";
import { realpathSync } from "node:fs";
import path from "node:path";

const extensionDir = realpathSync(__dirname);
const repoRoot = path.resolve(extensionDir, "..");
const piIndexScript = path.join(repoRoot, "hooks", "pi_index.py");
const piContextScript = path.join(repoRoot, "hooks", "pi_context.py");

function spawnIndexer(mode: "fast" | "full", sessionFile: string) {
	const child = spawn(
		"uv",
		["run", piIndexScript, "--mode", mode, "--session-file", sessionFile],
		{
			cwd: repoRoot,
			detached: true,
			stdio: "ignore",
			env: {
				...process.env,
				SESSION_INDEX_PROVIDER: "pi",
			},
		},
	);
	child.unref();
}

export default function (pi: ExtensionAPI) {
	let injectedForSession: string | undefined;
	let lastFastIndexKey: string | undefined;

	pi.on("session_start", async (_event, _ctx) => {
		injectedForSession = undefined;
		lastFastIndexKey = undefined;
	});

	pi.on("before_agent_start", async (event, ctx) => {
		const sessionFile = ctx.sessionManager.getSessionFile?.() ?? "ephemeral";
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
					ctx.sessionManager.getSessionId?.() ?? "",
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
		const sessionFile = ctx.sessionManager.getSessionFile?.();
		if (!sessionFile) return;
		const leaf = ctx.sessionManager.getLeafId?.() ?? "";
		const key = `${sessionFile}:${leaf}`;
		if (key === lastFastIndexKey) return;
		lastFastIndexKey = key;
		spawnIndexer("fast", sessionFile);
	});

	pi.on("session_shutdown", async (event, ctx) => {
		if (event.reason === "reload") return;
		const sessionFile = ctx.sessionManager.getSessionFile?.();
		if (!sessionFile) return;
		spawnIndexer("full", sessionFile);
	});
}
