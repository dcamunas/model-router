# What reaches Codex

[← Tribunal README](../README.md)

What each judge can read, and what leaves your machine.

Only what the prompt carries: your request, the diff, and the changed files as they are now. Codex
runs in an empty directory with its shell, image viewer, web search, browser, desktop, apps and code
tools turned off, so it cannot open any file or page.

This is on purpose. With a shell, Codex's read-only sandbox still reads any file your user can read:
in a test here it read a decoy file outside the repository. Its image viewer did the same with an
image. What they read goes to OpenAI. Without those tools the judge sees the material and nothing
else, and it is told to say so when something it needed was cut.

The Claude judge reads inside the repository with Read, Grep and Glob; reading outside it is denied,
also tested. It loads only your user settings, not the repository's, whose hooks would otherwise run.
