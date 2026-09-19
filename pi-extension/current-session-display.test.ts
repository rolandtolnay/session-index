import assert from "node:assert/strict";
import test from "node:test";
import { formatCurrentSessionDisplay, showCurrentSessionDisplay, type CurrentSessionDisplayContent, type CurrentSessionDisplayUi, type CurrentSessionIndexResult } from "./current-session-display.ts";

const content: CurrentSessionDisplayContent = {
	metadata: {
		session_id: "pi:018f1234-abcd-7000-9000-000000000001",
		native_session_id: "018f1234-abcd-7000-9000-000000000001",
		source_path: "/sessions/project/018f1234.jsonl",
		transcript_path: "/transcripts/pi:018f1234.md",
		tool_log_path: "/transcripts/pi:018f1234.tools.md",
		source_path_exists: true,
		transcript_exists: false,
		tool_log_exists: false,
	},
};

test("formatCurrentSessionDisplay shows copyable pi resume and fork commands with the full session uuid", () => {
	const sessionId = "018f1234-abcd-7000-9000-000000000001";
	const lines = formatCurrentSessionDisplay(content, sessionId);

	assert.ok(lines.includes("Pi commands:"));
	assert.ok(lines.includes(`Resume: pi --session ${sessionId}`));
	assert.ok(lines.includes(`Fork:   pi --fork ${sessionId}`));
});

test("showCurrentSessionDisplay dims command labels but keeps commands foreground", async () => {
	const sessionId = "018f1234-abcd-7000-9000-000000000001";
	let rendered: string[] = [];
	const theme = {
		fg: (color: string, text: string) => `<${color}>${text}</${color}>`,
		bold: (text: string) => `<bold>${text}</bold>`,
	};

	const ui: CurrentSessionDisplayUi = {
		custom: async <T>(factory) => {
			const component = factory({ requestRender() {} }, theme, undefined, () => undefined);
			rendered = component.render(200);
			return undefined as T;
		},
	};

	await showCurrentSessionDisplay({
		ctx: { ui },
		content,
		piCommandSessionId: sessionId,
	});

	assert.ok(rendered.includes(`  <dim>Resume: </dim><text>pi --session ${sessionId}</text>`));
	assert.ok(rendered.includes(`  <dim>Fork:   </dim><text>pi --fork ${sessionId}</text>`));
});

test("formatCurrentSessionDisplay keeps command hints out of unresolved metadata errors", () => {
	const lines = formatCurrentSessionDisplay({ error: "missing runtime identity" }, "018f1234abcd");

	assert.equal(lines.some((line) => line.includes("pi --session")), false);
	assert.equal(lines.some((line) => line.includes("pi --fork")), false);
});

type DisplayComponent = ReturnType<Parameters<CurrentSessionDisplayUi["custom"]>[0]>;

function openDisplay(options: Omit<Parameters<typeof showCurrentSessionDisplay>[0], "ctx">) {
	let component!: DisplayComponent;
	let closeCount = 0;
	let renderCount = 0;
	const finished = showCurrentSessionDisplay({
		...options,
		ctx: {
			ui: {
				custom: <T>(factory) => new Promise<T>((resolve) => {
					component = factory(
						{ requestRender() { renderCount++; } },
						{ fg: (color, text) => `<${color}>${text}</${color}>` },
						undefined,
						(value) => { closeCount++; resolve(value); },
					);
				}),
			},
		},
	});
	return { component, finished, get closeCount() { return closeCount; }, get renderCount() { return renderCount; } };
}

const flushAsync = () => new Promise<void>((resolve) => setImmediate(resolve));

test("c copies the unformatted path even before the artifact exists, without closing or indexing", async () => {
	const path = `/transcripts/a path with spaces/雪-${"long".repeat(25)}.md`;
	const copied: string[] = [];
	let indexCalls = 0;
	const display = openDisplay({
		content: { metadata: { ...content.metadata, transcript_path: path, transcript_exists: false } },
		copyToClipboard: async (text) => { copied.push(text); },
		onIndexSnapshot: async () => { indexCalls++; return { status: "failed", message: "not expected" }; },
	});
	const before = display.component.render(80);
	for (const key of ["y", "\x03", "\x1b[99;5u", "\x1b[99;3u", "\x1b[200~c\x1b[201~"]) {
		display.component.handleInput(key);
	}
	assert.deepEqual(copied, []);
	for (const key of ["c", "\x1b[99u"]) {
		display.component.handleInput(key);
		await flushAsync();
	}
	assert.deepEqual(copied, [path, path]);
	assert.equal(indexCalls, 0);
	assert.equal(display.closeCount, 0);
	assert.notDeepEqual(display.component.render(80), before);
	assert.match(display.component.render(120).join("\n"), /<success>/);
	display.component.handleInput("\r");
	await display.finished;
	assert.equal(display.closeCount, 1);
});

test("copy failure is visible and retry works without interfering with running indexing", async () => {
	const copied: string[] = [];
	let attempts = 0;
	let indexCalls = 0;
	let completeIndex!: (result: CurrentSessionIndexResult) => void;
	const display = openDisplay({
		content,
		copyToClipboard: async (text) => {
			if (++attempts === 1) throw new Error("clipboard unavailable");
			copied.push(text);
		},
		onIndexSnapshot: () => {
			indexCalls++;
			return new Promise((resolve) => { completeIndex = resolve; });
		},
	});
	display.component.handleInput("\x12");
	display.component.handleInput("c");
	await flushAsync();
	assert.match(display.component.render(200).join("\n"), /<warning>/);
	assert.equal(display.closeCount, 0);
	display.component.handleInput("c");
	await flushAsync();
	display.component.handleInput("\x12");
	assert.equal(indexCalls, 1, "copy must not reset the running-index guard");
	assert.deepEqual(copied, [content.metadata.transcript_path]);
	const refreshed = { metadata: { ...content.metadata, transcript_path: "/refreshed.md" } };
	completeIndex({ status: "completed", content: refreshed, completedAt: new Date().toISOString() });
	await flushAsync();
	display.component.handleInput("c");
	await flushAsync();
	assert.deepEqual(copied, [content.metadata.transcript_path, "/refreshed.md"]);
	display.component.handleInput("q");
	await display.finished;
});

test("unavailable metadata leaves the clipboard untouched", async () => {
	let copyCalls = 0;
	const display = openDisplay({
		content: { error: "missing runtime identity" },
		copyToClipboard: async () => { copyCalls++; },
	});
	display.component.handleInput("c");
	await flushAsync();
	assert.equal(copyCalls, 0);
	assert.equal(display.closeCount, 0);
	display.component.handleInput("\x1b");
	await display.finished;
});

test("pending copy ignores repeat presses and can finish after dismissal without rendering", async () => {
	let copyCalls = 0;
	let finishCopy!: () => void;
	const display = openDisplay({
		content,
		copyToClipboard: () => {
			copyCalls++;
			return new Promise((resolve) => { finishCopy = resolve; });
		},
	});
	display.component.handleInput("c");
	display.component.handleInput("c");
	assert.equal(copyCalls, 1);
	display.component.handleInput("q");
	await display.finished;
	const rendersAfterClose = display.renderCount;
	finishCopy();
	await flushAsync();
	assert.equal(display.renderCount, rendersAfterClose);
	assert.equal(display.closeCount, 1);
});

test("showCurrentSessionDisplay closes on Kitty keyboard protocol Escape", async () => {
	let closeCount = 0;
	const ui: CurrentSessionDisplayUi = {
		custom: async <T>(factory) => {
			const component = factory(
				{ requestRender() {} },
				{},
				undefined,
				() => { closeCount++; },
			);
			component.handleInput("\x1b[27u");
			return undefined as T;
		},
	};

	await showCurrentSessionDisplay({ ctx: { ui }, content });

	assert.equal(closeCount, 1);
});
