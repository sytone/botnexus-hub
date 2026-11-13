# BotNexusHub conventions

- This is a separate app and extension, not a fork of BotNexus core.
- Never change or load the host running world's config, auth, database, code or process.
- Test gateways run only in dedicated containers with synthetic data and separate volumes.
- World connector accepts only explicit new-conversation, message and prompt-answer commands. No arbitrary URLs, shell or config writes from Hub.
- Hub and connector authentication are separate; credentials never appear in snapshots, logs or browser bundles.
- All app changes require Python tests, Node theme tests, connector compile/tests, and isolated container/browser E2E before release.
- Theme modes: system, light, dark. UI uses escaped DOM text for external content.
- One authoritative VERSION file. Update CHANGELOG.md on release.
- Canonical main is deployed; development changes belong in a registered worktree.
