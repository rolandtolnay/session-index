import { isDismissKey, withFooterSuppressed } from "./current-session-display.ts";

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

export type RecentSessionsDisplayUi = {
	custom<T>(factory: (
		tui: TuiLike,
		theme: ThemeLike,
		keybindings: unknown,
		done: (value: T) => void,
	) => ComponentLike): Promise<T>;
};

type InlineSegment = {
	text: string;
	code: boolean;
};

const CONTENT_INDENT = "  ";
const VALUE_INDENT = "    ";
const LIST_PREFIX = "  • ";
const DISMISSAL_HINT = "Enter/Esc/q close";

function color(theme: ThemeLike, colorName: string, text: string): string {
	return theme.fg?.(colorName, text) ?? text;
}

function bold(theme: ThemeLike, text: string): string {
	return theme.bold?.(text) ?? text;
}

function wrapPlainLine(line: string, width: number): string[] {
	return wrapInlineSegments([{ text: line, code: false }], width)
		.map((segments) => segments.map((segment) => segment.text).join(""));
}

function inlineSegments(text: string): InlineSegment[] {
	const segments: InlineSegment[] = [];
	const pattern = /`([^`]*)`/g;
	let offset = 0;
	for (const match of text.matchAll(pattern)) {
		const index = match.index ?? offset;
		if (index > offset) segments.push({ text: text.slice(offset, index), code: false });
		segments.push({ text: match[1] ?? "", code: true });
		offset = index + match[0].length;
	}
	if (offset < text.length) segments.push({ text: text.slice(offset), code: false });
	return segments.length > 0 ? segments : [{ text: "", code: false }];
}

function wrapInlineSegments(segments: InlineSegment[], width: number): InlineSegment[][] {
	const safeWidth = Math.max(0, Math.floor(width));
	if (safeWidth <= 0) return [[]];

	const lines: InlineSegment[][] = [];
	let line: InlineSegment[] = [];
	let lineWidth = 0;
	const pushPart = (text: string, code: boolean) => {
		if (!text) return;
		const previous = line.at(-1);
		if (previous?.code === code) previous.text += text;
		else line.push({ text, code });
		lineWidth += text.length;
	};
	const finishLine = () => {
		const last = line.at(-1);
		if (last) {
			last.text = last.text.replace(/\s+$/, "");
			if (!last.text) line.pop();
		}
		lines.push(line);
		line = [];
		lineWidth = 0;
	};

	for (const segment of segments) {
		for (const token of segment.text.match(/\s+|\S+/g) ?? []) {
			if (/^\s+$/.test(token)) {
				if (lineWidth === 0) continue;
				if (lineWidth + token.length > safeWidth) finishLine();
				else pushPart(token, segment.code);
				continue;
			}

			let remainingText = token;
			while (remainingText.length > 0) {
				const remainingWidth = safeWidth - lineWidth;
				if (lineWidth > 0 && remainingText.length > remainingWidth) {
					finishLine();
					continue;
				}
				const part = remainingText.slice(0, safeWidth - lineWidth);
				pushPart(part, segment.code);
				remainingText = remainingText.slice(part.length);
				if (lineWidth === safeWidth) finishLine();
			}
		}
	}
	if (line.length > 0 || lines.length === 0) lines.push(line);
	return lines;
}

function renderInlineSegments(theme: ThemeLike, segments: InlineSegment[]): string {
	return segments.map((segment) => color(theme, segment.code ? "mdCode" : "text", segment.text)).join("");
}

function pushIndented(
	lines: string[],
	width: number,
	indent: string,
	text: string,
	style: (text: string) => string,
) {
	const prefix = indent.slice(0, Math.max(0, width));
	const contentWidth = Math.max(0, width - prefix.length);
	for (const wrapped of wrapPlainLine(text, contentWidth)) {
		lines.push(`${prefix}${style(wrapped)}`);
	}
}

function pushListItem(lines: string[], width: number, theme: ThemeLike, text: string) {
	const prefix = LIST_PREFIX.slice(0, Math.max(0, width));
	const continuation = VALUE_INDENT.slice(0, Math.max(0, width));
	const contentWidth = Math.max(0, width - prefix.length);
	const wrapped = wrapInlineSegments(inlineSegments(text), contentWidth);
	wrapped.forEach((segments, index) => {
		const rowPrefix = index === 0 ? prefix : continuation;
		lines.push(`${index === 0 ? color(theme, "accent", rowPrefix) : rowPrefix}${renderInlineSegments(theme, segments)}`);
	});
}

function renderRecentSessionsDisplay(content: string, theme: ThemeLike, width: number): string[] {
	const safeWidth = Math.max(0, Math.floor(width));
	const sourceLines = content.split("\n");
	const firstLine = sourceLines[0] ?? "";
	const hasTitle = firstLine.startsWith("# ");
	const title = hasTitle ? firstLine.slice(2) : "Recent Sessions";
	const body = hasTitle ? sourceLines.slice(1) : sourceLines;
	const lines: string[] = [color(theme, "borderMuted", "─".repeat(safeWidth))];

	pushIndented(lines, safeWidth, CONTENT_INDENT, title, (text) => color(theme, "accent", bold(theme, text)));
	lines.push("");

	for (const line of body) {
		if (line.length === 0) {
			lines.push("");
			continue;
		}
		if (line.startsWith("## ")) {
			pushIndented(lines, safeWidth, CONTENT_INDENT, line.slice(3), (text) => color(theme, "accent", bold(theme, text)));
			continue;
		}
		if (line.startsWith("Transcript root:")) {
			pushIndented(lines, safeWidth, CONTENT_INDENT, "Transcript root", (text) => color(theme, "muted", text));
			pushIndented(lines, safeWidth, VALUE_INDENT, line.slice("Transcript root:".length).trim(), (text) => color(theme, "mdCode", text));
			continue;
		}
		if (line.startsWith("- ")) {
			pushListItem(lines, safeWidth, theme, line.slice(2));
			continue;
		}
		pushIndented(lines, safeWidth, CONTENT_INDENT, line, (text) => color(theme, "dim", text));
	}

	while (lines.at(-1) === "") lines.pop();
	lines.push("");
	lines.push(color(theme, "borderMuted", "─".repeat(safeWidth)));
	pushIndented(lines, safeWidth, CONTENT_INDENT, DISMISSAL_HINT, (text) => color(theme, "dim", text));
	return lines;
}

class RecentSessionsDisplayComponent implements ComponentLike {
	focused = true;
	private readonly content: string;
	private readonly tui: TuiLike;
	private readonly theme: ThemeLike;
	private readonly done: () => void;
	private cachedWidth: number | undefined;
	private cachedLines: string[] | undefined;

	constructor(content: string, tui: TuiLike, theme: ThemeLike, done: () => void) {
		this.content = content;
		this.tui = tui;
		this.theme = theme;
		this.done = done;
	}

	render(width: number): string[] {
		if (this.cachedWidth === width && this.cachedLines) return this.cachedLines;
		this.cachedWidth = width;
		this.cachedLines = renderRecentSessionsDisplay(this.content, this.theme, width);
		return this.cachedLines;
	}

	invalidate(): void {
		this.cachedWidth = undefined;
		this.cachedLines = undefined;
	}

	handleInput(data: string): void {
		if (!isDismissKey(data)) return;
		this.done();
		this.tui.requestRender();
	}
}

export async function showRecentSessionsDisplay(options: {
	ctx: { ui: RecentSessionsDisplayUi };
	content: string;
}): Promise<void> {
	await withFooterSuppressed(() => options.ctx.ui.custom<void>((tui, theme, _keybindings, done) => {
		return new RecentSessionsDisplayComponent(options.content, tui, theme, () => done(undefined));
	}));
}
