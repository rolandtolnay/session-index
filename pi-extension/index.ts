import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { spawn, type ChildProcess } from "node:child_process";
import { realpathSync } from "node:fs";
import path from "node:path";
import {
	showCurrentSessionDisplay,
	type CurrentSessionDisplayContent,
	type CurrentSessionDisplayMetadata,
	type CurrentSessionIndexResult,
} from "./current-session-display.ts";
import {
	applySessionIndexEnv,
	buildSessionIndexEnv,
	overlaySessionIndexEnv,
	type SessionIndexEnv,
} from "./session-index-env.ts";

const extensionDir = realpathSync(import.meta.dirname);
const repoRoot = path.resolve(extensionDir, "..");
const piIndexScript = path.join(repoRoot, "hooks", "pi_index.py");
const piContextScript = path.join(repoRoot, "hooks", "pi_context.py");
const MANUAL_INDEX_UI_TIMEOUT_MS = 300_000;
const CURRENT_SESSION_REFRESH_TIMEOUT_MS = 5_000;

type CurrentSessionCommandResult = {
	code: number | null;
	stdout: string;
	stderr: string;
};

type IndexerRunResult = { status: "completed"; code: number | null; stderr: string };

type SpawnProcess = typeof spawn;

type ExtensionDependencies = {
	spawnProcess?: SpawnProcess;
	manualIndexTimeoutMs?: number;
	currentSessionRefreshTimeoutMs?: number;
};

function refreshSessionIndexEnv(sessionManager: Parameters<typeof buildSessionIndexEnv>[0]) {
	const sessionEnv = buildSessionIndexEnv(sessionManager);
	applySessionIndexEnv(process.env, sessionEnv);
	return sessionEnv;
}

function buildIndexerSpawn(mode: "fast" | "full", sessionFile: string, sessionEnv: SessionIndexEnv | undefined) {
	const env = overlaySessionIndexEnv(process.env, sessionEnv);
	env.SESSION_INDEX_PROVIDER = "pi";
	return {
		args: ["run", piIndexScript, "--mode", mode, "--session-file", sessionFile],
		env,
	};
}

function spawnIndexer(
	mode: "fast" | "full",
	sessionFile: string,
	sessionEnv: SessionIndexEnv | undefined,
	spawnProcess: SpawnProcess,
) {
	const indexer = buildIndexerSpawn(mode, sessionFile, sessionEnv);
	const child = spawnProcess(
		"uv",
		indexer.args,
		{
			cwd: repoRoot,
			detached: true,
			stdio: "ignore",
			env: indexer.env,
		},
	);
	child.unref();
}

function runCurrentSessionJson(
	childEnv: Record<string, string | undefined>,
	spawnProcess: SpawnProcess,
	timeoutMs?: number,
): Promise<CurrentSessionCommandResult> {
	return new Promise((resolve) => {
		let completed = false;
		let stdout = "";
		let stderr = "";

		const child = spawnProcess("uv", ["run", "cli.py", "current", "--json"], {
			cwd: repoRoot,
			stdio: ["ignore", "pipe", "pipe"],
			env: childEnv,
		}) as ChildProcess;

		let timer: ReturnType<typeof setTimeout> | undefined;
		const finish = (result: CurrentSessionCommandResult) => {
			if (completed) return;
			completed = true;
			if (timer) clearTimeout(timer);
			resolve(result);
		};

		if (timeoutMs !== undefined) {
			timer = setTimeout(() => {
				child.kill?.();
				finish({ code: null, stdout, stderr: "Timed out refreshing Current Session metadata." });
			}, timeoutMs);
		}

		child.stdout?.setEncoding("utf8");
		child.stderr?.setEncoding("utf8");
		child.stdout?.on("data", (chunk) => {
			stdout += chunk;
		});
		child.stderr?.on("data", (chunk) => {
			stderr += chunk;
		});
		child.on("error", (error) => {
			finish({ code: null, stdout, stderr: error.message });
		});
		child.on("close", (code) => {
			finish({ code, stdout, stderr });
		});
	});
}

function startFullIndexer(
	sessionFile: string,
	sessionEnv: SessionIndexEnv,
	spawnProcess: SpawnProcess,
): Promise<IndexerRunResult> {
	return new Promise((resolve) => {
		let completed = false;
		let stderr = "";
		const indexer = buildIndexerSpawn("full", sessionFile, sessionEnv);

		const finish = (result: IndexerRunResult) => {
			if (completed) return;
			completed = true;
			resolve(result);
		};

		const child = spawnProcess(
			"uv",
			indexer.args,
			{
				cwd: repoRoot,
				stdio: ["ignore", "ignore", "pipe"],
				env: indexer.env,
			},
		) as ChildProcess;

		child.stderr?.setEncoding("utf8");
		child.stderr?.on("data", (chunk) => {
			stderr += chunk;
		});
		child.on("error", (error) => {
			finish({ status: "completed", code: null, stderr: stderr || error.message });
		});
		child.on("close", (code) => {
			finish({ status: "completed", code, stderr });
		});
	});
}

function waitForIndexerUiResult(completion: Promise<IndexerRunResult>, timeoutMs: number): Promise<IndexerRunResult | { status: "timeout" }> {
	return new Promise((resolve) => {
		const timer = setTimeout(() => resolve({ status: "timeout" }), timeoutMs);
		completion.then((result) => {
			clearTimeout(timer);
			resolve(result);
		});
	});
}

function commandFailureMessage(result: CurrentSessionCommandResult): string {
	const stderr = result.stderr.trim();
	if (stderr) return stderr;
	const stdout = result.stdout.trim();
	if (stdout) return stdout;
	if (result.code === null) return "Failed to run uv run cli.py current --json.";
	return `uv run cli.py current --json exited with code ${result.code}.`;
}

const CURRENT_SESSION_STRING_FIELDS = [
	"session_id",
	"native_session_id",
	"source",
	"source_path",
	"transcript_path",
	"tool_log_path",
	"resolution_method",
] as const;

const CURRENT_SESSION_BOOLEAN_FIELDS = [
	"source_path_exists",
	"transcript_exists",
	"tool_log_exists",
] as const;

function hasFieldsOfType(candidate: Record<string, unknown>, fields: readonly string[], type: "string" | "boolean") {
	return fields.every((field) => typeof candidate[field] === type);
}

function parseCurrentSessionMetadata(value: unknown): CurrentSessionDisplayMetadata | undefined {
	if (!value || typeof value !== "object" || Array.isArray(value)) return undefined;
	const candidate = value as Record<string, unknown>;

	if (
		!hasFieldsOfType(candidate, CURRENT_SESSION_STRING_FIELDS, "string")
		|| !hasFieldsOfType(candidate, CURRENT_SESSION_BOOLEAN_FIELDS, "boolean")
	) return undefined;

	const metadata: CurrentSessionDisplayMetadata = {
		session_id: candidate.session_id,
		native_session_id: candidate.native_session_id,
		source_path: candidate.source_path,
		transcript_path: candidate.transcript_path,
		tool_log_path: candidate.tool_log_path,
		source_path_exists: candidate.source_path_exists,
		transcript_exists: candidate.transcript_exists,
		tool_log_exists: candidate.tool_log_exists,
	};
	if (typeof candidate.transcript_written_at === "string") {
		metadata.transcript_written_at = candidate.transcript_written_at;
	}
	if (typeof candidate.tool_log_written_at === "string") {
		metadata.tool_log_written_at = candidate.tool_log_written_at;
	}
	return metadata;
}

function currentSessionDisplayContent(result: CurrentSessionCommandResult): CurrentSessionDisplayContent {
	if (result.code !== 0) return { error: commandFailureMessage(result) };

	let parsed: unknown;
	try {
		parsed = JSON.parse(result.stdout);
	} catch {
		return { error: "Invalid Current Session metadata JSON from uv run cli.py current --json." };
	}

	const metadata = parseCurrentSessionMetadata(parsed);
	if (!metadata) {
		return { error: "Invalid Current Session metadata JSON from uv run cli.py current --json: missing required v1 fields." };
	}
	return { metadata };
}

async function refreshCurrentSessionContent(
	sessionEnv: SessionIndexEnv,
	spawnProcess: SpawnProcess,
	timeoutMs?: number,
): Promise<CurrentSessionDisplayContent> {
	const childEnv = overlaySessionIndexEnv(process.env, sessionEnv);
	const result = await runCurrentSessionJson(childEnv, spawnProcess, timeoutMs);
	return currentSessionDisplayContent(result);
}

function contentHasMetadata(content: CurrentSessionDisplayContent): content is { metadata: CurrentSessionDisplayMetadata } {
	return "metadata" in content;
}

async function indexerCompletionResult(
	completion: Promise<IndexerRunResult>,
	sessionEnv: SessionIndexEnv,
	fallbackContent: CurrentSessionDisplayContent,
	spawnProcess: SpawnProcess,
	refreshTimeoutMs: number,
): Promise<CurrentSessionIndexResult> {
	const runResult = await completion;
	const refreshedContent = await refreshCurrentSessionContent(sessionEnv, spawnProcess, refreshTimeoutMs);
	const refreshedMetadataContent = contentHasMetadata(refreshedContent) ? refreshedContent : undefined;

	if (runResult.code !== 0) {
		const message = runResult.stderr.trim()
			|| (runResult.code === null
				? "Failed to run manual Current Session indexing."
				: `Manual Current Session indexing exited with code ${runResult.code}.`);
		return { status: "failed", content: refreshedMetadataContent ?? fallbackContent, message };
	}

	if (!refreshedMetadataContent) {
		return { status: "failed", content: fallbackContent, message: "Manual Current Session indexing completed, but refreshed metadata was unavailable." };
	}

	return { status: "completed", content: refreshedMetadataContent, completedAt: new Date().toISOString() };
}

async function runManualCurrentSessionIndex(
	sessionManager: Parameters<typeof buildSessionIndexEnv>[0],
	fallbackContent: CurrentSessionDisplayContent,
	spawnProcess: SpawnProcess,
	timeoutMs: number,
	refreshTimeoutMs: number,
): Promise<CurrentSessionIndexResult> {
	const sessionEnv = refreshSessionIndexEnv(sessionManager);
	if (!sessionEnv) {
		return { status: "failed", content: fallbackContent, message: "Current Session metadata is unavailable: Pi runtime identity is missing." };
	}

	const completion = startFullIndexer(sessionEnv.SESSION_INDEX_SOURCE_PATH, sessionEnv, spawnProcess);
	const runResult = await waitForIndexerUiResult(completion, timeoutMs);
	const settled = indexerCompletionResult(completion, sessionEnv, fallbackContent, spawnProcess, refreshTimeoutMs);

	if (runResult.status === "timeout") {
		const refreshedContent = await refreshCurrentSessionContent(sessionEnv, spawnProcess, refreshTimeoutMs);
		const refreshedMetadataContent = contentHasMetadata(refreshedContent) ? refreshedContent : undefined;
		return {
			status: "timeout",
			content: refreshedMetadataContent ?? fallbackContent,
			checkedAt: new Date().toISOString(),
			settled,
		};
	}

	return settled;
}

export function createSessionIndexExtension(dependencies: ExtensionDependencies = {}) {
	const spawnProcess = dependencies.spawnProcess ?? spawn;
	const manualIndexTimeoutMs = dependencies.manualIndexTimeoutMs ?? MANUAL_INDEX_UI_TIMEOUT_MS;
	const currentSessionRefreshTimeoutMs = dependencies.currentSessionRefreshTimeoutMs ?? CURRENT_SESSION_REFRESH_TIMEOUT_MS;

	return function registerSessionIndexExtension(pi: ExtensionAPI) {
		let injectedForSession: string | undefined;
		let lastFastIndexKey: string | undefined;

		pi.registerCommand("current-session", {
			description: "Show Current Session metadata without sending it to the model",
			handler: async (_args, ctx) => {
				const sessionEnv = refreshSessionIndexEnv(ctx.sessionManager);
				if (!sessionEnv) {
					await showCurrentSessionDisplay({
						ctx,
						content: { error: "Current Session metadata is unavailable: Pi runtime identity is missing." },
					});
					return;
				}

				const childEnv = overlaySessionIndexEnv(process.env, sessionEnv);
				const result = await runCurrentSessionJson(childEnv, spawnProcess);
				const content = currentSessionDisplayContent(result);
				const piCommandSessionId = contentHasMetadata(content)
					? content.metadata.native_session_id
					: undefined;
				let latestContent = content;
				await showCurrentSessionDisplay({
					ctx,
					content,
					piCommandSessionId,
					onIndexSnapshot: async () => {
						const result = await runManualCurrentSessionIndex(
							ctx.sessionManager,
							latestContent,
							spawnProcess,
							manualIndexTimeoutMs,
							currentSessionRefreshTimeoutMs,
						);
						if (result.content) latestContent = result.content;
						return result;
					},
				});
			},
		});

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
			spawnIndexer("fast", sessionFile, sessionEnv, spawnProcess);
		});

		pi.on("session_shutdown", async (event, ctx) => {
			if (event.reason === "reload") return;
			const sessionEnv = refreshSessionIndexEnv(ctx.sessionManager);
			const sessionFile = sessionEnv?.SESSION_INDEX_SOURCE_PATH ?? ctx.sessionManager.getSessionFile?.();
			if (!sessionFile) return;
			spawnIndexer("full", sessionFile, sessionEnv, spawnProcess);
		});
	};
}

export default createSessionIndexExtension();
