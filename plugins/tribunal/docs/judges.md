# Judges and arbiter

[← Tribunal README](../README.md)

Which model sits where, why, and why two judges rather than a council.

## Who sits where

- **Judge A** is a Claude model, `opus/high` by default, and never the model serving the chat: that
  one wrote the change. With an Opus chat, it moves to Fable.
- **Judge B** is the declared Codex model of the highest tier, at high effort.
- **The arbiter** is a Claude model neither judge is on, and not the chat's unless nothing else is
  left. It reads the work and both verdicts as judge A and judge B, without names, and decides for
  itself. One extra call, only on a split.
- Only models this installation has are picked (`router config --models`). Judge B and the arbiter
  are never on judge A's model; when no other model is left, one judge decides alone and says so.
- When one judge cannot be reached, the other's verdict is still shown to you; it is not a tribunal,
  so nothing is sent back.

## Why two judges and not a council

Karpathy's `llm-council` sends one question to several models, has each one rank the others' answers
and lets a chairman write the final one. With four members that is nine calls per question, and most
of the value comes from mixing providers.

The tribunal keeps the parts that pay: a second, independent
opinion from another company, and a third one only when the first two disagree. No shared answers,
no ranking, no chairman.
