---
name: play
description: Launch Claude Ski, a terminal downhill skiing arcade game, in a new terminal window and report the final score. Use when the user runs /claude-ski:play (or /claude-ski) or asks to play the skiing game.
---

# Claude Ski

This is a **deterministic terminal game**, not a model task. Do not simulate
gameplay, narrate frames, or read the source to describe it. Your only job is to
launch it and report the result.

## Launch it

Run this single command:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/claude-ski.py" --launch
```

The game needs a real keyboard, which the Claude Code tool subprocess does not
provide — so `--launch` opens the game in a **new terminal window** (a real
TTY) and then waits for the run to finish.

## Report the result

When the command returns it prints a score recap (outcome, score, distance,
time, top speed, jumps, crashes, yetis blasted, session best). **Relay that
recap to the user.**

- If it prints that it could not open a window, show the user the exact fallback
  command it gives so they can run it in their own terminal.
- If it says the run is still in progress, tell the user their score will be
  saved and offer to read it (`python3 -c "import json;print(open('/tmp/claude-ski-result.json').read())"`) when they finish.

## Controls (mention to the user)

Arrow keys / `WASD` steer · `Space` hops · `P` pauses · `Q` quits · `R` restarts.
Grab the **orange Claude crab** for 15s of rainbow invincibility, and ram the
yeti while invincible to blow it up.
