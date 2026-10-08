# Local idle rebalancing validation

## Behavior

Idle preparation allows clients to stay open. Every tracked live session must
report idle and have a known expired prompt cache. Unfinished subagents,
busy/waiting/unknown sessions, unreadable records, and warm caches block it.
Unobserved or changed transcripts, including completed subagents, use a
conservative one-hour quiet period. Main cache timing comes from Claude Code's
status-line `prompt_cache.expires_at`; this is a client prediction.

Fresh quota measurements rank enabled OAuth accounts by earliest weekly reset,
then lowest weekly usage (most remaining quota). Equal rankings keep the active
account. Five-hour usage at or above 90% remains excluded on this host. Idle state is
rechecked before activation; cooldown is retained. Existing quota/failover
switches continue independently. Idle preparation has no nightly time window.

## Evidence, 2026-10-08

- PASS: `uv run --python 3.13 pytest -n 4 -p no:cacheprovider`:
  2206 passed, 3 skipped, 5 upstream deprecation warnings.
- PASS: isolated shell pipeline used the exact status-line wrapper and the
  existing local renderer. It stored cache timing at mode 0600 and preserved
  output byte-for-byte. Observer failure also preserved output.
- PASS: local live observation received 8 known cache records from existing
  clients without restarting them. Session scan found 8 idle and 1 busy;
  the idle guard refused preparation while some observations were incomplete.
- PASS: installed tool includes `cswap-cache-observe`, `idleRebalance=true`,
  `targetFiveHourMaxPct=90`; `cswap-auto.service` is active/running with exit
  status 0 after restart.
- UNPROVEN: an actual all-idle account switch followed by an existing client's
  next provider request. Engine tests fake quota and token refresh, and write
  only isolated Linux credentials; they do not prove provider/client acceptance.
- UNPROVEN: exact server cache eviction. All deadlines are client predictions
  or conservative inactivity bounds, not a server confirmation.

## Rollback

Set `cswap config set autoswitch.idleRebalance false` and restart
`cswap-auto.service` to disable preparation while retaining the five-hour
ceiling. The observer can stay installed without changing status-line output.
To remove it, restore only the old `statusLine.command` from
`local-rollback/claude-settings-before-idle.json`, preserving subsequent user
settings. The previous cswap settings are in
`local-rollback/cswap-settings-before-idle.json`; neither backup is committed.

## Sources

- https://code.claude.com/docs/en/statusline#prompt-cache-fields
- https://code.claude.com/docs/en/prompt-caching
- https://code.claude.com/docs/en/env-vars
