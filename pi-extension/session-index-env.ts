const PI_SESSION_PREFIX = "pi:";

export const SESSION_INDEX_ENV_KEYS = [
	"SESSION_INDEX_SESSION_ID",
	"SESSION_INDEX_NATIVE_SESSION_ID",
	"SESSION_INDEX_SOURCE",
	"SESSION_INDEX_SOURCE_PATH",
	"SESSION_INDEX_LEAF_ID",
] as const;

export type SessionIndexEnvKey = (typeof SESSION_INDEX_ENV_KEYS)[number];
export type SessionIndexEnv = Partial<Record<SessionIndexEnvKey, string>> & {
	SESSION_INDEX_SESSION_ID: string;
	SESSION_INDEX_NATIVE_SESSION_ID: string;
	SESSION_INDEX_SOURCE: "pi";
	SESSION_INDEX_SOURCE_PATH: string;
};

type MutableEnv = Record<string, string | undefined>;

type SessionManagerLike = {
	getSessionFile?: () => string | undefined | null;
	getSessionId?: () => string | undefined | null;
	getLeafId?: () => string | undefined | null;
};

function clean(value: unknown): string | undefined {
	if (typeof value !== "string") return undefined;
	const trimmed = value.trim();
	return trimmed.length > 0 ? trimmed : undefined;
}

function cleanSessionFile(value: unknown): string | undefined {
	const sessionFile = clean(value);
	if (!sessionFile || sessionFile === "ephemeral") return undefined;
	return sessionFile;
}

function cleanNativeSessionId(value: unknown): string | undefined {
	const sessionId = clean(value)?.replace(/^pi:/, "");
	return sessionId && sessionId.length > 0 ? sessionId : undefined;
}

export function buildSessionIndexEnv(sessionManager: SessionManagerLike | undefined | null): SessionIndexEnv | undefined {
	const sourcePath = cleanSessionFile(sessionManager?.getSessionFile?.());
	const nativeSessionId = cleanNativeSessionId(sessionManager?.getSessionId?.());

	if (!sourcePath || !nativeSessionId) return undefined;

	const env: SessionIndexEnv = {
		SESSION_INDEX_SESSION_ID: `${PI_SESSION_PREFIX}${nativeSessionId}`,
		SESSION_INDEX_NATIVE_SESSION_ID: nativeSessionId,
		SESSION_INDEX_SOURCE: "pi",
		SESSION_INDEX_SOURCE_PATH: sourcePath,
	};

	const leafId = clean(sessionManager?.getLeafId?.());
	if (leafId) env.SESSION_INDEX_LEAF_ID = leafId;

	return env;
}

export function applySessionIndexEnv(targetEnv: MutableEnv, sessionEnv: SessionIndexEnv | undefined): void {
	for (const key of SESSION_INDEX_ENV_KEYS) delete targetEnv[key];
	if (!sessionEnv) return;
	for (const [key, value] of Object.entries(sessionEnv)) targetEnv[key] = value;
}

export function overlaySessionIndexEnv(baseEnv: MutableEnv, sessionEnv: SessionIndexEnv | undefined): MutableEnv {
	const env: MutableEnv = { ...baseEnv };
	applySessionIndexEnv(env, sessionEnv);
	return env;
}
