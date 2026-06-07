# Treat commands and skills as Skill Invocations

Session Index treats reusable slash commands, skill envelopes, provider Skill tool events, and exact `SKILL.md` reads as one **Skill Invocation** concept because the audit question is whether a named prompt or workflow template was invoked, not how a provider encoded it. We intentionally avoid exposing command-vs-tool detection source in the user-facing evidence model; provider-specific formats are parser details normalized into a unified fact table and `skill/<session_id>/<sequence>` Inspection Reference.
