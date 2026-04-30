#!/usr/bin/env node
/**
 * session-index installer
 *
 * Installs Claude Code hooks/skill and/or Pi extension/skill.
 *
 * Usage:
 *   node install.js                         # Install Claude + Pi integrations
 *   node install.js --target claude         # Install Claude only
 *   node install.js --target pi             # Install Pi only
 *   node install.js --uninstall             # Remove Claude + Pi integrations
 *   node install.js --uninstall --target pi # Remove Pi only
 */

const fs = require("fs");
const path = require("path");
const os = require("os");

// ── Config ──────────────────────────────────────────────────────────────────

const TOOLKIT_NAME = "session-index";
const REPO_ROOT = fs.realpathSync(__dirname);

const CLAUDE_DIR = path.join(os.homedir(), ".claude");
const CLAUDE_SETTINGS_PATH = path.join(CLAUDE_DIR, "settings.json");
const CLAUDE_MANIFEST_DIR = path.join(CLAUDE_DIR, TOOLKIT_NAME);
const CLAUDE_MANIFEST_PATH = path.join(CLAUDE_MANIFEST_DIR, ".manifest.json");

const PI_AGENT_DIR = path.join(os.homedir(), ".pi", "agent");
const PI_MANIFEST_DIR = path.join(PI_AGENT_DIR, TOOLKIT_NAME);
const PI_MANIFEST_PATH = path.join(PI_MANIFEST_DIR, ".manifest.json");

const SKILL_NAME = "session-search";
const SKILL_SRC = path.join(REPO_ROOT, "skills", SKILL_NAME);
const CLAUDE_SKILL_DST = path.join(CLAUDE_DIR, "skills", SKILL_NAME);
const PI_SKILL_DST = path.join(PI_AGENT_DIR, "skills", SKILL_NAME);

const PI_EXTENSION_NAME = "session-index";
const PI_EXTENSION_SRC = path.join(REPO_ROOT, "pi-extension");
const PI_EXTENSION_DST = path.join(PI_AGENT_DIR, "extensions", PI_EXTENSION_NAME);

const CLAUDE_HOOKS = [
  {
    event: "Stop",
    command: `uv run ${path.join(REPO_ROOT, "hooks", "stop.py")}`,
    timeout: 10,
  },
  {
    event: "SessionEnd",
    command: `uv run ${path.join(REPO_ROOT, "hooks", "session_end.py")}`,
    timeout: 5,
  },
  {
    event: "SessionStart",
    command: `uv run ${path.join(REPO_ROOT, "hooks", "session_start.py")}`,
    timeout: 5,
  },
];

// ── Helpers ─────────────────────────────────────────────────────────────────

function ensureDir(dir) {
  if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
}

function readJson(file, fallback = {}) {
  if (!fs.existsSync(file)) return fallback;
  return JSON.parse(fs.readFileSync(file, "utf-8"));
}

function writeJson(file, value) {
  ensureDir(path.dirname(file));
  fs.writeFileSync(file, JSON.stringify(value, null, 2) + "\n");
}

function linkResource(src, dst, label) {
  ensureDir(path.dirname(dst));
  let dstExists = false;
  try { fs.lstatSync(dst); dstExists = true; } catch {}

  if (dstExists) {
    try {
      const target = fs.realpathSync(dst);
      if (target === src) {
        console.log(`  [skip] ${label} already linked`);
      } else {
        console.log(`  [WARN] ${dst} exists but points to ${target} — skipping`);
        console.log("         Remove it manually if you want to re-link.");
      }
    } catch {
      fs.rmSync(dst, { recursive: true, force: true });
      fs.symlinkSync(src, dst, fs.statSync(src).isDirectory() ? "dir" : "file");
      console.log(`  [fix]  Re-linked ${label} (was broken)`);
    }
  } else {
    fs.symlinkSync(src, dst, fs.statSync(src).isDirectory() ? "dir" : "file");
    console.log(`  [ok]   Linked ${label}`);
  }
}

function unlinkResource(dst, expectedSrc, label) {
  let dstExists = false;
  try { fs.lstatSync(dst); dstExists = true; } catch {}
  if (!dstExists) {
    console.log(`  [skip] ${label} not installed`);
    return;
  }

  try {
    const target = fs.realpathSync(dst);
    if (target === expectedSrc) {
      fs.rmSync(dst, { recursive: true, force: true });
      console.log(`  [ok]   Removed ${label}`);
    } else {
      console.log(`  [skip] ${label} points elsewhere — not removing`);
    }
  } catch {
    fs.rmSync(dst, { recursive: true, force: true });
    console.log(`  [ok]   Removed broken ${label} symlink`);
  }
}

function isOurHook(entry, repoRoot = REPO_ROOT) {
  return entry.hooks?.some((h) => h.command?.includes(repoRoot));
}

function parseTarget(args) {
  const idx = args.indexOf("--target");
  const target = idx >= 0 ? args[idx + 1] : "all";
  if (!["claude", "pi", "all"].includes(target)) {
    console.error(`Invalid --target: ${target}. Use claude, pi, or all.`);
    process.exit(1);
  }
  return target;
}

function includesTarget(target, name) {
  return target === "all" || target === name;
}

// ── Claude install ──────────────────────────────────────────────────────────

function installClaude() {
  console.log("\nClaude Code integration");

  linkResource(SKILL_SRC, CLAUDE_SKILL_DST, `Claude skill: ${SKILL_NAME}`);

  const settings = readJson(CLAUDE_SETTINGS_PATH, {});
  if (!settings.hooks) settings.hooks = {};

  let hooksChanged = false;
  for (const { event, command, timeout } of CLAUDE_HOOKS) {
    if (!settings.hooks[event]) settings.hooks[event] = [];

    const already = settings.hooks[event].some((entry) => isOurHook(entry));
    if (already) {
      console.log(`  [skip] Hook already registered: ${event}`);
    } else {
      settings.hooks[event].push({ hooks: [{ type: "command", command, timeout }] });
      console.log(`  [ok]   Registered hook: ${event}`);
      hooksChanged = true;
    }
  }

  if (hooksChanged) writeJson(CLAUDE_SETTINGS_PATH, settings);

  writeJson(CLAUDE_MANIFEST_PATH, {
    version: "1.0.0",
    installedAt: new Date().toISOString(),
    target: "claude",
    repoRoot: REPO_ROOT,
    skill: SKILL_NAME,
    hookEvents: CLAUDE_HOOKS.map((h) => h.event),
  });
  console.log(`  [ok]   Manifest: ${CLAUDE_MANIFEST_PATH}`);
}

function uninstallClaude() {
  console.log("\nClaude Code integration");

  let repoRoot = REPO_ROOT;
  if (fs.existsSync(CLAUDE_MANIFEST_PATH)) {
    try { repoRoot = readJson(CLAUDE_MANIFEST_PATH).repoRoot || REPO_ROOT; } catch {}
  }

  unlinkResource(CLAUDE_SKILL_DST, path.join(repoRoot, "skills", SKILL_NAME), `Claude skill: ${SKILL_NAME}`);

  if (fs.existsSync(CLAUDE_SETTINGS_PATH)) {
    const settings = readJson(CLAUDE_SETTINGS_PATH, {});
    let changed = false;

    if (settings.hooks) {
      for (const { event } of CLAUDE_HOOKS) {
        if (!settings.hooks[event]) continue;
        const before = settings.hooks[event].length;
        settings.hooks[event] = settings.hooks[event].filter((entry) => !isOurHook(entry, repoRoot));
        if (settings.hooks[event].length < before) {
          console.log(`  [ok]   Removed hook: ${event}`);
          changed = true;
        } else {
          console.log(`  [skip] Hook not found: ${event}`);
        }
        if (settings.hooks[event].length === 0) delete settings.hooks[event];
      }
      if (Object.keys(settings.hooks).length === 0) delete settings.hooks;
    }

    if (changed) writeJson(CLAUDE_SETTINGS_PATH, settings);
  }

  fs.rmSync(CLAUDE_MANIFEST_PATH, { force: true });
  try { fs.rmdirSync(CLAUDE_MANIFEST_DIR); } catch {}
  console.log("  [ok]   Claude manifest removed if present");
}

// ── Pi install ──────────────────────────────────────────────────────────────

function installPi() {
  console.log("\nPi integration");
  linkResource(SKILL_SRC, PI_SKILL_DST, `Pi skill: ${SKILL_NAME}`);
  linkResource(PI_EXTENSION_SRC, PI_EXTENSION_DST, `Pi extension: ${PI_EXTENSION_NAME}`);

  writeJson(PI_MANIFEST_PATH, {
    version: "1.0.0",
    installedAt: new Date().toISOString(),
    target: "pi",
    repoRoot: REPO_ROOT,
    skill: SKILL_NAME,
    extension: PI_EXTENSION_NAME,
  });
  console.log(`  [ok]   Manifest: ${PI_MANIFEST_PATH}`);
}

function uninstallPi() {
  console.log("\nPi integration");

  let repoRoot = REPO_ROOT;
  if (fs.existsSync(PI_MANIFEST_PATH)) {
    try { repoRoot = readJson(PI_MANIFEST_PATH).repoRoot || REPO_ROOT; } catch {}
  }

  unlinkResource(PI_SKILL_DST, path.join(repoRoot, "skills", SKILL_NAME), `Pi skill: ${SKILL_NAME}`);
  unlinkResource(PI_EXTENSION_DST, path.join(repoRoot, "pi-extension"), `Pi extension: ${PI_EXTENSION_NAME}`);

  fs.rmSync(PI_MANIFEST_PATH, { force: true });
  try { fs.rmdirSync(PI_MANIFEST_DIR); } catch {}
  console.log("  [ok]   Pi manifest removed if present");
}

// ── Main ────────────────────────────────────────────────────────────────────

const args = process.argv.slice(2);

if (args.includes("--help") || args.includes("-h")) {
  console.log(`
Usage: node install.js [options]

Options:
  --target <claude|pi|all>  Integration target (default: all)
  --uninstall               Remove installed integrations
  --help                    Show this help
`);
  process.exit(0);
}

const target = parseTarget(args);
const uninstall = args.includes("--uninstall");

console.log(`\n${uninstall ? "Uninstalling" : "Installing"} ${TOOLKIT_NAME} from ${REPO_ROOT}`);

if (uninstall) {
  if (includesTarget(target, "claude")) uninstallClaude();
  if (includesTarget(target, "pi")) uninstallPi();
  console.log("\nDone.\n");
} else {
  if (includesTarget(target, "claude")) installClaude();
  if (includesTarget(target, "pi")) installPi();
  console.log(`\nDone! Next steps:`);
  console.log(`  1. Make sure Ollama is running: ollama pull qwen3.5:4b`);
  console.log(`  2. Backfill existing sessions: cd ${REPO_ROOT} && uv run cli.py backfill --source all`);
  console.log(`  3. In Pi, run /reload or restart Pi so the extension and skill load.\n`);
}
