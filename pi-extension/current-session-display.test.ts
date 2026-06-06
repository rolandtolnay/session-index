import assert from "node:assert/strict";
import test from "node:test";
import { formatCurrentSessionDisplay, showCurrentSessionDisplay, type CurrentSessionDisplayContent, type CurrentSessionDisplayUi } from "./current-session-display.ts";

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
