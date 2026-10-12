import type { SessionManager } from "@earendil-works/pi-coding-agent";
import { spawn } from "node:child_process";
import { existsSync, realpathSync } from "node:fs";
import path from "node:path";
import { buildSessionIndexEnv, overlaySessionIndexEnv } from "./session-index-env.ts";

export interface WorkflowRefreshInput {
	ctx: {
		sessionManager: Parameters<typeof buildSessionIndexEnv>[0] & Pick<SessionManager, "getEntries">;
		ui: { notify(message: string, type: "warning"): void };
	};
	transfer?: { sourceFile: string | undefined; entries: unknown[] };
}

export type WorkflowRefresh = (input: WorkflowRefreshInput) => Promise<void>;

/** Await only durable ownership/enqueue acknowledgement, never background indexing. */
export function createWorkflowRefresh(deps: { spawnProcess?: typeof spawn; timeoutMs?: number; extensionDir?: string } = {}): WorkflowRefresh {
	return async ({ ctx, transfer }) => {
		try {
			const sessionEnv = buildSessionIndexEnv(ctx.sessionManager);
			if (!sessionEnv) throw new Error("The current Pi session has no persisted native origin.");
			if (!existsSync(sessionEnv.SESSION_INDEX_SOURCE_PATH)) {
				// Pi assigns a path before persisting: setup/custom entries alone do
				// not create a file. Only that fresh in-place case has nothing to refresh.
				const hasConversation = ctx.sessionManager.getEntries().some((entry) =>
					entry.type === "message" && (entry.message.role === "user" || entry.message.role === "assistant"));
				if (!transfer && !hasConversation) return;
				throw new Error(`Missing Pi session file: ${sessionEnv.SESSION_INDEX_SOURCE_PATH}`);
			}
			const repoRoot = path.resolve(realpathSync(deps.extensionDir ?? import.meta.dirname), "..");
			const args = ["run", path.join(repoRoot, "hooks", "pi_index.py"), "--mode", "workflow", "--session-file", sessionEnv.SESSION_INDEX_SOURCE_PATH];
			if (transfer?.sourceFile) args.push("--transfer-source-file", transfer.sourceFile);
			await new Promise<void>((resolve, reject) => {
				const child = (deps.spawnProcess ?? spawn)("uv", args, {
					cwd: repoRoot, stdio: ["pipe", "pipe", "pipe"],
					env: { ...overlaySessionIndexEnv(process.env, sessionEnv), SESSION_INDEX_PROVIDER: "pi" },
				});
				let settled = false;
				let stdout = "";
				let stderr = "";
				let timer: ReturnType<typeof setTimeout> | undefined;
				const finish = (error?: Error) => {
					if (settled) return;
					settled = true;
					if (timer) clearTimeout(timer);
					if (error) reject(error); else resolve();
				};
				timer = setTimeout(() => {
					child.kill();
					finish(new Error("Timed out waiting for the Session Index enqueue acknowledgement."));
				}, deps.timeoutMs ?? 5_000);
				child.stdout?.setEncoding("utf8");
				child.stderr?.setEncoding("utf8");
				child.stdout?.on("data", (chunk) => { stdout = (stdout + chunk).slice(-65_536); });
				child.stderr?.on("data", (chunk) => { stderr = (stderr + chunk).slice(-4_096); });
				child.on("error", (error) => finish(error));
				child.stdin?.on("error", (error) => finish(error));
				child.on("close", (code) => {
					if (settled) return;
					try {
						const ack = JSON.parse(stdout);
						if (code !== 0 || ack?.queued !== true || typeof ack.session_id !== "string" || !ack.session_id.trim()) throw new Error("Missing enqueue acknowledgement.");
						finish();
					} catch {
						finish(new Error(stderr.trim() || "Session Index did not acknowledge the workflow refresh."));
					}
				});
				// The first element is the captured source header, including native ID
				// and start time. Never materialize a missing/unflushed source JSONL.
				child.stdin?.end(transfer?.sourceFile ? JSON.stringify(transfer.entries) : "");
			});
		} catch (error) {
			try { ctx.ui.notify(`Session Index workflow refresh was not acknowledged: ${error instanceof Error ? error.message : String(error)}`, "warning"); }
			catch { /* Indexing and notification failures must not stop the workflow. */ }
		}
	};
}

export const enqueueWorkflowRefresh = createWorkflowRefresh();
