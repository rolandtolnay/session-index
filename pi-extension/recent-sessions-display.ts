import { isDismissKey, withFooterSuppressed } from "./current-session-display.ts";

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
		theme: unknown,
		keybindings: unknown,
		done: (value: T) => void,
	) => ComponentLike): Promise<T>;
};

function wrapPlainLine(line: string, width: number): string[] {
	const safeWidth = Math.max(0, Math.floor(width));
	if (safeWidth <= 0) return [""];
	if (line.length <= safeWidth) return [line];

	const lines: string[] = [];
	for (let offset = 0; offset < line.length; offset += safeWidth) {
		lines.push(line.slice(offset, offset + safeWidth));
	}
	return lines;
}

class RecentSessionsDisplayComponent implements ComponentLike {
	focused = true;
	private readonly content: string;
	private readonly tui: TuiLike;
	private readonly done: () => void;
	private cachedWidth: number | undefined;
	private cachedLines: string[] | undefined;

	constructor(content: string, tui: TuiLike, done: () => void) {
		this.content = content;
		this.tui = tui;
		this.done = done;
	}

	render(width: number): string[] {
		if (this.cachedWidth === width && this.cachedLines) return this.cachedLines;
		this.cachedWidth = width;
		this.cachedLines = this.content.split("\n").flatMap((line) => wrapPlainLine(line, width));
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
	await withFooterSuppressed(() => options.ctx.ui.custom<void>((tui, _theme, _keybindings, done) => {
		return new RecentSessionsDisplayComponent(options.content, tui, () => done(undefined));
	}));
}
