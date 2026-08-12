import assert from "node:assert/strict";
import test from "node:test";
import { createSessionIndexExtension } from "./index.ts";
import { showRecentSessionsDisplay, type RecentSessionsDisplayUi } from "./recent-sessions-display.ts";

const injectedContext = `# Recent Sessions
Transcript root: /Users/example/.session-index/transcripts

## example (latest 1)
- 2026-08-10 (main) \`pi:session.md\` — Kept verbatim`;

test("showRecentSessionsDisplay renders themed hierarchy, borders, and a final hint while suppressing the footer", async () => {
	const stateKey = Symbol.for("pi.focused-ui-footer.state");
	(globalThis as Record<symbol, unknown>)[stateKey] = { suppressionCount: 0, listeners: new Set() };
	let rendered: string[] = [];
	let suppressedDuringDisplay = false;
	const theme = {
		fg: (name: string, text: string) => `<${name}>${text}</${name}>`,
		bold: (text: string) => `<bold>${text}</bold>`,
	};

	const ui: RecentSessionsDisplayUi = {
		custom: async <T>(factory) => {
			suppressedDuringDisplay = ((globalThis as Record<symbol, { suppressionCount: number }>)[stateKey]?.suppressionCount ?? 0) > 0;
			const component = factory({ requestRender() {} }, theme, undefined, () => undefined);
			rendered = component.render(200);
			return undefined as T;
		},
	};

	await showRecentSessionsDisplay({ ctx: { ui }, content: injectedContext });

	assert.equal(suppressedDuringDisplay, true);
	assert.equal((globalThis as Record<symbol, { suppressionCount: number }>)[stateKey]?.suppressionCount, 0);
	assert.ok(rendered[0]?.startsWith("<borderMuted>─"), rendered.join("\n"));
	assert.ok(rendered.includes("  <accent><bold>Recent Sessions</bold></accent>"), rendered.join("\n"));
	assert.ok(rendered.includes("  <muted>Transcript root</muted>"), rendered.join("\n"));
	assert.ok(rendered.includes("    <mdCode>/Users/example/.session-index/transcripts</mdCode>"), rendered.join("\n"));
	assert.ok(rendered.includes("  <accent><bold>example (latest 1)</bold></accent>"), rendered.join("\n"));
	assert.ok(rendered.some((line) => line.includes("<accent>  • </accent>") && line.includes("<mdCode>pi:session.md</mdCode>")), rendered.join("\n"));
	assert.ok(rendered.at(-2)?.startsWith("<borderMuted>─"), rendered.join("\n"));
	assert.ok(rendered.at(-1)?.startsWith("  <dim>"), rendered.join("\n"));
	assert.equal(rendered.join("\n").includes("# Recent Sessions"), false);
	assert.equal(rendered.join("\n").includes("`pi:session.md`"), false);
});

test("showRecentSessionsDisplay keeps every unstyled rendered row within the available width", async () => {
	let rendered: string[] = [];
	const ui: RecentSessionsDisplayUi = {
		custom: async <T>(factory) => {
			const component = factory({ requestRender() {} }, {}, undefined, () => undefined);
			rendered = component.render(32);
			return undefined as T;
		},
	};

	await showRecentSessionsDisplay({ ctx: { ui }, content: injectedContext });

	assert.ok(rendered.every((line) => line.length <= 32), rendered.join("\n"));
});

test("showRecentSessionsDisplay closes on the established read-only dismissal keys", async () => {
	let closeCount = 0;
	const ui: RecentSessionsDisplayUi = {
		custom: async <T>(factory) => {
			const component = factory(
				{ requestRender() {} },
				{},
				undefined,
				() => { closeCount++; },
			);
			component.handleInput("q");
			return undefined as T;
		},
	};

	await showRecentSessionsDisplay({ ctx: { ui }, content: injectedContext });

	assert.equal(closeCount, 1);
});

test("recent-sessions builds the session snapshot before the first prompt and that exact snapshot is injected", async () => {
	const commands = new Map<string, { handler: (args: string, ctx: any) => Promise<unknown> }>();
	const handlers = new Map<string, (event: any, ctx: any) => Promise<any>>();
	let execCalls = 0;

	createSessionIndexExtension()({
		registerCommand: (name: string, options: any) => commands.set(name, options),
		on: (name: string, handler: any) => handlers.set(name, handler),
		exec: async () => {
			execCalls++;
			return { code: 0, stdout: `${injectedContext}\n` };
		},
	} as any);

	const sessionManager = {
		getSessionFile: () => "/tmp/pi-session.jsonl",
		getSessionId: () => "pi-session",
		getLeafId: () => "leaf-1",
	};
	const ctx = { cwd: "/tmp/project", sessionManager };
	await handlers.get("session_start")?.({ reason: "startup" }, ctx);

	let rendered: string[] = [];
	await commands.get("recent-sessions")?.handler("", {
		...ctx,
		ui: {
			notify: () => assert.fail("the command should lazily build available context"),
			custom: async (factory: any) => {
				const component = factory({ requestRender() {} }, {}, undefined, () => undefined);
				rendered = component.render(500);
			},
		},
	});

	const expected = `${injectedContext}\n\nUse this recent-session index as lightweight continuity context. For older or specific past work, load the session-search skill and query the index.`;
	assert.ok(rendered.some((line) => line.includes("/Users/example/.session-index/transcripts")), rendered.join("\n"));
	assert.ok(rendered.some((line) => line.includes("Kept verbatim")), rendered.join("\n"));
	assert.equal(execCalls, 1);

	const result = await handlers.get("before_agent_start")?.({ systemPrompt: "base prompt" }, ctx);
	assert.equal(result.systemPrompt, `base prompt\n\n${expected}`);
	assert.equal(execCalls, 1, "the first prompt must inject the snapshot already shown by the command");
});

test("recent-sessions displays the exact captured injection without recomputing it", async () => {
	const commands = new Map<string, { handler: (args: string, ctx: any) => Promise<unknown> }>();
	const handlers = new Map<string, (event: any, ctx: any) => Promise<any>>();
	let execCalls = 0;

	createSessionIndexExtension()({
		registerCommand: (name: string, options: any) => commands.set(name, options),
		on: (name: string, handler: any) => handlers.set(name, handler),
		exec: async () => {
			execCalls++;
			return { code: 0, stdout: `${injectedContext}\n` };
		},
	} as any);

	const sessionManager = {
		getSessionFile: () => "/tmp/pi-session.jsonl",
		getSessionId: () => "pi-session",
		getLeafId: () => "leaf-1",
	};
	const ctx = { cwd: "/tmp/project", sessionManager };
	await handlers.get("session_start")?.({ reason: "startup" }, ctx);
	const result = await handlers.get("before_agent_start")?.({ systemPrompt: "base prompt" }, ctx);
	const expected = `${injectedContext}\n\nUse this recent-session index as lightweight continuity context. For older or specific past work, load the session-search skill and query the index.`;

	assert.equal(result.systemPrompt, `base prompt\n\n${expected}`);
	assert.equal(execCalls, 1);

	let rendered: string[] = [];
	const commandResult = await commands.get("recent-sessions")?.handler("", {
		sessionManager,
		ui: {
			notify: () => assert.fail("captured context should be available"),
			custom: async (factory: any) => {
				const component = factory({ requestRender() {} }, {}, undefined, () => undefined);
				rendered = component.render(500);
			},
		},
	});

	assert.equal(commandResult, undefined);
	assert.equal(execCalls, 1, "the display must use the captured injection instead of rebuilding it");
	assert.ok(rendered.some((line) => line.includes("/Users/example/.session-index/transcripts")), rendered.join("\n"));
	assert.ok(rendered.some((line) => line.includes("Kept verbatim")), rendered.join("\n"));
});
