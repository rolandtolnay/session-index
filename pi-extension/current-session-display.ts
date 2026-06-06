export {};

type ThemeLike = {
	fg?(color: string, text: string): string;
	bold?(text: string): string;
};

type TuiLike = {
	requestRender(): void;
};

type ComponentLike = {
	focused: boolean;
	render(width: number): string[];
	invalidate(): void;
	handleInput(data: string): void;
};

export type CurrentSessionDisplayMetadata = {
	session_id: string;
	native_session_id: string;
	source_path: string;
	transcript_path: string;
	tool_log_path: string;
	source_path_exists: boolean;
	transcript_exists: boolean;
	tool_log_exists: boolean;
	transcript_written_at?: string;
	tool_log_written_at?: string;
};

export type CurrentSessionDisplayContent =
	| { metadata: CurrentSessionDisplayMetadata }
	| { error: string };

export type CurrentSessionIndexResult =
	| { status: "completed"; content: CurrentSessionDisplayContent; completedAt: string }
	| {
		status: "timeout";
		content: CurrentSessionDisplayContent;
		checkedAt?: string;
		settled?: Promise<CurrentSessionIndexResult>;
	}
	| { status: "failed"; content?: CurrentSessionDisplayContent; message: string };

export type CurrentSessionDisplayUi = {
	custom<T>(factory: (
		tui: TuiLike,
		theme: ThemeLike,
		keybindings: unknown,
		done: (value: T) => void,
	) => ComponentLike): Promise<T>;
};

type SuppressionListener = () => void;

type FocusedFooterSuppressionState = {
	suppressionCount: number;
	listeners: Set<SuppressionListener>;
};

const DISMISSAL_HINT = "Enter/Esc/q close";
const INDEX_ACTION_HINT = "Ctrl+R index current snapshot · Enter/Esc/q close";
const CONTENT_INDENT = "  ";
const VALUE_INDENT = "    ";
const FOOTER_SUPPRESSION_STATE_KEY = Symbol.for("pi.focused-ui-footer.state");

function focusedFooterState(): FocusedFooterSuppressionState {
	const globalState = globalThis as Record<symbol, FocusedFooterSuppressionState | undefined>;
	let state = globalState[FOOTER_SUPPRESSION_STATE_KEY];
	if (!state) {
		state = { suppressionCount: 0, listeners: new Set<SuppressionListener>() };
		globalState[FOOTER_SUPPRESSION_STATE_KEY] = state;
	}
	return state;
}

function isFocusedFooterSuppressed(): boolean {
	return focusedFooterState().suppressionCount > 0;
}

function notifyFooterSuppressionIfChanged(wasSuppressed: boolean) {
	const state = focusedFooterState();
	if (wasSuppressed === isFocusedFooterSuppressed()) return;
	for (const listener of [...state.listeners]) listener();
}

function acquireFocusedFooterSuppression(): () => void {
	const state = focusedFooterState();
	const wasSuppressed = isFocusedFooterSuppressed();
	state.suppressionCount++;
	notifyFooterSuppressionIfChanged(wasSuppressed);

	let released = false;
	return () => {
		if (released) return;
		released = true;

		const state = focusedFooterState();
		const wasSuppressed = isFocusedFooterSuppressed();
		state.suppressionCount = Math.max(0, state.suppressionCount - 1);
		notifyFooterSuppressionIfChanged(wasSuppressed);
	};
}

async function withFooterSuppressed<T>(operation: () => Promise<T>): Promise<T> {
	const release = acquireFocusedFooterSuppression();
	try {
		return await operation();
	} finally {
		release();
	}
}

function formatLocalCompactTime(iso: string | undefined): string | undefined {
	if (!iso) return undefined;
	const date = new Date(iso);
	if (Number.isNaN(date.getTime())) return undefined;
	const now = new Date();
	const options: Intl.DateTimeFormatOptions = date.getFullYear() === now.getFullYear()
		? { month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit" }
		: { year: "numeric", month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit" };
	return new Intl.DateTimeFormat(undefined, options).format(date);
}

type CurrentSessionDisplayRow = {
	label: string;
	value: string;
	exists?: boolean;
	writtenAt?: string;
};

function currentSessionRows(metadata: CurrentSessionDisplayMetadata): CurrentSessionDisplayRow[] {
	return [
		{ label: "Canonical Session ID", value: metadata.session_id },
		{ label: "Native Session ID", value: metadata.native_session_id },
		{
			label: "Clean Transcript",
			value: metadata.transcript_path,
			exists: metadata.transcript_exists,
			writtenAt: metadata.transcript_written_at,
		},
		{
			label: "Tool Log",
			value: metadata.tool_log_path,
			exists: metadata.tool_log_exists,
			writtenAt: metadata.tool_log_written_at,
		},
		{ label: "Source Transcript", value: metadata.source_path, exists: metadata.source_path_exists },
	];
}

function withStatus(row: CurrentSessionDisplayRow): string {
	if (row.exists === undefined) return `${row.label}: ${row.value}`;
	const written = formatLocalCompactTime(row.writtenAt);
	const suffix = written ? ` · written ${written}` : "";
	return `${row.label}: ${row.value} [${row.exists ? "exists" : "missing"}]${suffix}`;
}

function displayError(error: string): string {
	const trimmed = error.trim();
	return trimmed.length > 0 ? trimmed : "Current Session metadata is unavailable.";
}

export function formatCurrentSessionDisplay(content: CurrentSessionDisplayContent, piCommandSessionId?: string): string[] {
	if ("error" in content) {
		return [
			"Current Session",
			"Unable to resolve Current Session metadata.",
			displayError(content.error),
			DISMISSAL_HINT,
		];
	}

	return [
		"Current Session",
		...currentSessionRows(content.metadata).map(withStatus),
		...piCommandRows(piCommandSessionId),
		INDEX_ACTION_HINT,
	];
}

function isDismissKey(data: string): boolean {
	const normalized = data.toLowerCase().replace(/[\s_]/g, "-");
	return data === "q"
		|| data === "Q"
		|| data === "\r"
		|| data === "\n"
		|| data === "\x1b"
		|| normalized === "return"
		|| normalized === "enter"
		|| normalized === "escape";
}

function isCtrlModifiedCsiU(data: string, key: string): boolean {
	const codePoint = key.toLowerCase().codePointAt(0);
	if (codePoint === undefined) return false;
	const match = /^\x1b\[(\d+);(\d+)(?::\d+)?u$/.exec(data);
	if (!match) return false;
	const sentCodePoint = Number(match[1]);
	const modifiers = Number(match[2]) - 1;
	return sentCodePoint === codePoint && (modifiers & 4) !== 0;
}

function isIndexSnapshotKey(data: string): boolean {
	const normalized = data.toLowerCase().replace(/[\s_]/g, "-");
	return data === "\x12"
		|| isCtrlModifiedCsiU(data, "r")
		|| normalized === "ctrl+r"
		|| normalized === "ctrl-r"
		|| normalized === "control+r"
		|| normalized === "control-r"
		|| normalized === "c-r";
}

function color(theme: ThemeLike, colorName: string, text: string): string {
	return theme.fg?.(colorName, text) ?? text;
}

function bold(theme: ThemeLike, text: string): string {
	return theme.bold?.(text) ?? text;
}

function truncatePlain(line: string, width: number): string {
	const safeWidth = Math.max(0, Math.floor(width));
	if (line.length <= safeWidth) return line;
	if (safeWidth === 0) return "";
	if (safeWidth === 1) return "…";
	return `${line.slice(0, safeWidth - 1)}…`;
}

function wrapPlain(line: string, width: number, continuationIndent = ""): string[] {
	const safeWidth = Math.max(0, Math.floor(width));
	if (safeWidth <= 0) return [""];
	if (line.length <= safeWidth) return [line];

	const lines: string[] = [];
	let remaining = line;
	let first = true;
	while (remaining.length > 0) {
		const prefix = first ? "" : continuationIndent.slice(0, Math.max(0, safeWidth - 1));
		const available = Math.max(1, safeWidth - prefix.length);
		lines.push(`${prefix}${remaining.slice(0, available)}`);
		remaining = remaining.slice(available);
		first = false;
	}
	return lines;
}

function statusText(theme: ThemeLike, exists: boolean): string {
	const label = exists ? "exists" : "missing";
	return color(theme, exists ? "success" : "warning", `[${label}]`);
}

type PiCommandRow = {
	label: string;
	command: string;
};

function piCommandLines(piCommandSessionId: string): string[] {
	return piCommandParts(piCommandSessionId).map((row) => `${row.label}${row.command}`);
}

function piCommandParts(piCommandSessionId: string): PiCommandRow[] {
	return [
		{ label: "Resume: ", command: `pi --session ${piCommandSessionId}` },
		{ label: "Fork:   ", command: `pi --fork ${piCommandSessionId}` },
	];
}

function piCommandRows(piCommandSessionId: string | undefined): string[] {
	if (!piCommandSessionId) return [];
	return ["Pi commands:", ...piCommandLines(piCommandSessionId)];
}

function pushPiCommandRows(lines: string[], width: number, theme: ThemeLike, piCommandSessionId: string | undefined) {
	if (!piCommandSessionId) return;
	const contentWidth = Math.max(0, width - CONTENT_INDENT.length);
	const add = (line: string, colorName: string) => lines.push(`${CONTENT_INDENT}${color(theme, colorName, truncatePlain(line, contentWidth))}`);
	add("Pi commands:", "muted");
	for (const row of piCommandParts(piCommandSessionId)) {
		const line = truncatePlain(`${row.label}${row.command}`, contentWidth);
		if (line.length <= row.label.length) {
			lines.push(`${CONTENT_INDENT}${color(theme, "dim", line)}`);
		} else {
			lines.push(`${CONTENT_INDENT}${color(theme, "dim", row.label)}${color(theme, "text", line.slice(row.label.length))}`);
		}
	}
}

function pushRow(
	lines: string[],
	width: number,
	theme: ThemeLike,
	label: string,
	value: string,
	exists?: boolean,
	writtenAt?: string,
) {
	const contentWidth = Math.max(0, width - CONTENT_INDENT.length);
	const valueWidth = Math.max(0, width - VALUE_INDENT.length);
	const status = exists === undefined ? "" : ` ${statusText(theme, exists)}`;
	const written = formatLocalCompactTime(writtenAt);
	const labelText = `${color(theme, "muted", label)}${status}`;
	const rowText = written ? `${labelText}${color(theme, "dim", ` · written ${written}`)}` : labelText;
	lines.push(`${CONTENT_INDENT}${truncatePlain(rowText, contentWidth)}`);
	for (const wrapped of wrapPlain(value, valueWidth)) {
		lines.push(`${VALUE_INDENT}${color(theme, "text", wrapped)}`);
	}
}

type RenderState = {
	phase: "idle" | "running" | "completed" | "timeout" | "failed";
	message?: string;
};

function renderCurrentSessionDisplay(
	content: CurrentSessionDisplayContent,
	theme: ThemeLike,
	width: number,
	state: RenderState = { phase: "idle" },
	piCommandSessionId?: string,
): string[] {
	const lines: string[] = [];
	const contentWidth = Math.max(0, width - CONTENT_INDENT.length);
	const add = (line = "") => lines.push(`${CONTENT_INDENT}${truncatePlain(line, contentWidth)}`);

	lines.push(color(theme, "borderMuted", "─".repeat(Math.max(0, width))));
	add(color(theme, "accent", bold(theme, "Current Session")));
	lines.push("");

	if ("error" in content) {
		add(color(theme, "warning", "Unable to resolve Current Session metadata."));
		for (const wrapped of wrapPlain(displayError(content.error), contentWidth, CONTENT_INDENT)) {
			add(color(theme, "text", wrapped));
		}
		add(color(theme, "dim", DISMISSAL_HINT));
		lines.push("");
		return lines;
	}

	for (const row of currentSessionRows(content.metadata)) {
		pushRow(lines, width, theme, row.label, row.value, row.exists, row.writtenAt);
	}
	lines.push("");
	pushPiCommandRows(lines, width, theme, piCommandSessionId);
	lines.push("");
	if (state.phase === "idle") {
		add(color(theme, "dim", "User-only display. Press Ctrl+R to index the current snapshot; not sent to the model."));
	} else {
		const message = state.message ?? (
			state.phase === "running"
				? "Indexing current snapshot…"
				: state.phase === "timeout"
					? "Indexing is still running after the UI wait timeout."
					: ""
		);
		const statusColor = state.phase === "completed"
			? "success"
			: state.phase === "failed"
				? "warning"
				: "accent";
		if (message) add(color(theme, statusColor, message));
	}
	lines.push("");
	add(color(theme, "dim", state.phase === "running" || state.phase === "timeout" ? DISMISSAL_HINT : INDEX_ACTION_HINT));
	lines.push("");
	return lines;
}

class CurrentSessionDisplayComponent implements ComponentLike {
	focused = true;
	private readonly tui: TuiLike;
	private readonly theme: ThemeLike;
	private content: CurrentSessionDisplayContent;
	private readonly onIndexSnapshot: (() => Promise<CurrentSessionIndexResult>) | undefined;
	private readonly piCommandSessionId: string | undefined;
	private readonly done: () => void;
	private state: RenderState = { phase: "idle" };
	private closed = false;
	private cachedWidth: number | undefined;
	private cachedLines: string[] | undefined;

	constructor(
		tui: TuiLike,
		theme: ThemeLike,
		content: CurrentSessionDisplayContent,
		onIndexSnapshot: (() => Promise<CurrentSessionIndexResult>) | undefined,
		piCommandSessionId: string | undefined,
		done: () => void,
	) {
		this.tui = tui;
		this.theme = theme;
		this.content = content;
		this.onIndexSnapshot = onIndexSnapshot;
		this.piCommandSessionId = piCommandSessionId;
		this.done = done;
	}

	render(width: number): string[] {
		if (this.cachedWidth === width && this.cachedLines) return this.cachedLines;
		this.cachedWidth = width;
		this.cachedLines = renderCurrentSessionDisplay(this.content, this.theme, width, this.state, this.piCommandSessionId);
		return this.cachedLines;
	}

	invalidate(): void {
		this.cachedWidth = undefined;
		this.cachedLines = undefined;
	}

	handleInput(data: string): void {
		if (isDismissKey(data)) {
			this.closed = true;
			this.done();
			this.tui.requestRender();
			return;
		}
		if (!isIndexSnapshotKey(data) || !this.onIndexSnapshot || !("metadata" in this.content)) return;
		if (this.state.phase === "running" || this.state.phase === "timeout") return;
		void this.runIndexSnapshot();
	}

	private async runIndexSnapshot(): Promise<void> {
		if (!this.onIndexSnapshot) return;
		this.state = { phase: "running", message: "Indexing current snapshot…" };
		this.invalidate();
		this.tui.requestRender();

		let result: CurrentSessionIndexResult;
		try {
			result = await this.onIndexSnapshot();
		} catch (error) {
			result = { status: "failed", message: error instanceof Error ? error.message : String(error) };
		}
		if (this.closed) return;

		this.applyIndexResult(result);
		if (result.status === "timeout" && result.settled) {
			void this.applySettledIndexResult(result.settled);
		}
	}

	private applyIndexResult(result: CurrentSessionIndexResult): void {
		if (result.status === "completed") {
			this.content = result.content;
			this.state = { phase: "completed", message: `Indexed snapshot at ${formatLocalCompactTime(result.completedAt) ?? result.completedAt}` };
		} else if (result.status === "timeout") {
			this.content = result.content;
			const checkedAt = formatLocalCompactTime(result.checkedAt);
			this.state = {
				phase: "timeout",
				message: checkedAt
					? `Indexing is still running; last checked ${checkedAt}.`
					: "Indexing is still running after the UI wait timeout.",
			};
		} else {
			if (result.content) this.content = result.content;
			this.state = { phase: "failed", message: result.message };
		}
		this.invalidate();
		this.tui.requestRender();
	}

	private async applySettledIndexResult(settled: Promise<CurrentSessionIndexResult>): Promise<void> {
		let result: CurrentSessionIndexResult;
		try {
			result = await settled;
		} catch (error) {
			result = { status: "failed", content: this.content, message: error instanceof Error ? error.message : String(error) };
		}
		if (this.closed) return;
		this.applyIndexResult(result);
	}
}

export async function showCurrentSessionDisplay(options: {
	ctx: { ui: CurrentSessionDisplayUi };
	content: CurrentSessionDisplayContent;
	piCommandSessionId?: string;
	onIndexSnapshot?: () => Promise<CurrentSessionIndexResult>;
}): Promise<void> {
	await withFooterSuppressed(() => options.ctx.ui.custom<void>((tui, theme, _keybindings, done) => {
		return new CurrentSessionDisplayComponent(tui, theme, options.content, options.onIndexSnapshot, options.piCommandSessionId, () => done(undefined));
	}));
}
