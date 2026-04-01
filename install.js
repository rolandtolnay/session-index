#!/usr/bin/env node
/**
 * session-index installer
 *
 * Symlinks the skill into ~/.claude/skills/ and registers hooks
 * in ~/.claude/settings.json so Claude Code can use session search
 * and index conversations automatically.
 *
 * Usage:
 *   node install.js              # Install
 *   node install.js --uninstall  # Remove everything
 */

const fs = require("fs");
const path = require("path");
const os = require("os");

// ── Config ──────────────────────────────────────────────────────────────────

const TOOLKIT_NAME = "session-index";
const CLAUDE_DIR = path.join(os.homedir(), ".claude");
const SETTINGS_PATH = path.join(CLAUDE_DIR, "settings.json");
const MANIFEST_DIR = path.join(CLAUDE_DIR, TOOLKIT_NAME);
const MANIFEST_PATH = path.join(MANIFEST_DIR, ".manifest.json");
const REPO_ROOT = fs.realpathSync(__dirname);

const SKILL_NAME = "session-search";
const SKILL_SRC = path.join(REPO_ROOT, "skills", SKILL_NAME);
const SKILL_DST = path.join(CLAUDE_DIR, "skills", SKILL_NAME);

const HOOKS = [
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

function readSettings() {
  if (!fs.existsSync(SETTINGS_PATH)) return {};
  return JSON.parse(fs.readFileSync(SETTINGS_PATH, "utf-8"));
}

function writeSettings(settings) {
  ensureDir(CLAUDE_DIR);
  fs.writeFileSync(SETTINGS_PATH, JSON.stringify(settings, null, 2) + "\n");
}

/** Check if a hook entry's command references this repo. */
function isOurHook(entry) {
  return entry.hooks?.some((h) => h.command?.includes(REPO_ROOT));
}

// ── Install ─────────────────────────────────────────────────────────────────

function install() {
  console.log(`\nInstalling ${TOOLKIT_NAME} from ${REPO_ROOT}\n`);

  // 1. Symlink skill
  ensureDir(path.dirname(SKILL_DST));
  let dstExists = false;
  try { fs.lstatSync(SKILL_DST); dstExists = true; } catch {}
  if (dstExists) {
    try {
      const target = fs.realpathSync(SKILL_DST);
      if (target === SKILL_SRC) {
        console.log(`  [skip] Skill already linked: ${SKILL_NAME}`);
      } else {
        console.log(
          `  [WARN] ${SKILL_DST} exists but points to ${target} — skipping`
        );
        console.log(`         Remove it manually if you want to re-link.`);
      }
    } catch {
      // Broken symlink — remove and re-create
      fs.unlinkSync(SKILL_DST);
      fs.symlinkSync(SKILL_SRC, SKILL_DST);
      console.log(`  [fix]  Re-linked skill: ${SKILL_NAME} (was broken)`);
    }
  } else {
    fs.symlinkSync(SKILL_SRC, SKILL_DST);
    console.log(`  [ok]   Linked skill: ${SKILL_NAME}`);
  }

  // 2. Register hooks in settings.json
  const settings = readSettings();
  if (!settings.hooks) settings.hooks = {};

  let hooksChanged = false;
  for (const { event, command, timeout } of HOOKS) {
    if (!settings.hooks[event]) settings.hooks[event] = [];

    const already = settings.hooks[event].some(isOurHook);
    if (already) {
      console.log(`  [skip] Hook already registered: ${event}`);
    } else {
      settings.hooks[event].push({
        hooks: [{ type: "command", command, timeout }],
      });
      console.log(`  [ok]   Registered hook: ${event}`);
      hooksChanged = true;
    }
  }

  if (hooksChanged) writeSettings(settings);

  // 3. Write manifest
  ensureDir(MANIFEST_DIR);
  const manifest = {
    version: "1.0.0",
    installedAt: new Date().toISOString(),
    repoRoot: REPO_ROOT,
    skill: SKILL_NAME,
    hookEvents: HOOKS.map((h) => h.event),
  };
  fs.writeFileSync(MANIFEST_PATH, JSON.stringify(manifest, null, 2) + "\n");

  console.log(`\n  Manifest written to ${MANIFEST_PATH}`);
  console.log(`\nDone! Next steps:`);
  console.log(`  1. Make sure Ollama is running: ollama pull qwen3.5:4b`);
  console.log(`  2. Backfill existing sessions:  cd ${REPO_ROOT} && uv run cli.py backfill`);
  console.log(`  3. Search from any conversation: /session-search <query>\n`);
}

// ── Uninstall ───────────────────────────────────────────────────────────────

function uninstall() {
  console.log(`\nUninstalling ${TOOLKIT_NAME}\n`);

  // Read manifest for repo root (handles case where uninstall runs from different dir)
  let repoRoot = REPO_ROOT;
  if (fs.existsSync(MANIFEST_PATH)) {
    try {
      const manifest = JSON.parse(fs.readFileSync(MANIFEST_PATH, "utf-8"));
      repoRoot = manifest.repoRoot || REPO_ROOT;
    } catch {}
  }

  // 1. Remove skill symlink
  let dstExists = false;
  try { fs.lstatSync(SKILL_DST); dstExists = true; } catch {}
  if (dstExists) {
    try {
      const target = fs.realpathSync(SKILL_DST);
      if (target === path.join(repoRoot, "skills", SKILL_NAME)) {
        fs.unlinkSync(SKILL_DST);
        console.log(`  [ok]   Removed skill: ${SKILL_NAME}`);
      } else {
        console.log(`  [skip] Skill points elsewhere — not removing`);
      }
    } catch {
      // Broken symlink
      fs.unlinkSync(SKILL_DST);
      console.log(`  [ok]   Removed broken skill symlink: ${SKILL_NAME}`);
    }
  } else {
    console.log(`  [skip] Skill not installed`);
  }

  // 2. Remove hooks from settings.json
  if (fs.existsSync(SETTINGS_PATH)) {
    const settings = readSettings();
    let changed = false;

    if (settings.hooks) {
      for (const { event } of HOOKS) {
        if (!settings.hooks[event]) continue;

        const before = settings.hooks[event].length;
        settings.hooks[event] = settings.hooks[event].filter((entry) => {
          // Match by repo root from manifest or current script location
          return !entry.hooks?.some(
            (h) => h.command?.includes(repoRoot)
          );
        });
        const after = settings.hooks[event].length;

        if (after < before) {
          console.log(`  [ok]   Removed hook: ${event}`);
          changed = true;
        } else {
          console.log(`  [skip] Hook not found: ${event}`);
        }

        // Clean up empty arrays
        if (settings.hooks[event].length === 0) {
          delete settings.hooks[event];
        }
      }

      // Clean up empty hooks object
      if (Object.keys(settings.hooks).length === 0) {
        delete settings.hooks;
      }
    }

    if (changed) writeSettings(settings);
  }

  // 3. Remove manifest
  if (fs.existsSync(MANIFEST_PATH)) {
    fs.unlinkSync(MANIFEST_PATH);
    console.log(`  [ok]   Removed manifest`);
  }
  if (fs.existsSync(MANIFEST_DIR)) {
    try {
      fs.rmdirSync(MANIFEST_DIR);
    } catch {}
  }

  console.log(`\nDone! session-index has been uninstalled.\n`);
}

// ── Main ────────────────────────────────────────────────────────────────────

const args = process.argv.slice(2);

if (args.includes("--help") || args.includes("-h")) {
  console.log(`
Usage: node install.js [options]

Options:
  --uninstall   Remove skill, hooks, and manifest
  --help        Show this help
`);
  process.exit(0);
}

if (args.includes("--uninstall")) {
  uninstall();
} else {
  install();
}
